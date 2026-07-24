from decimal import Decimal
import json
from sqlalchemy import func
from ..extensions import db
from ..models import (Meeting,MeetingParticipant,MeetingMessage,FounderFeedbackEvent,Employee,
    AgentRun,CostEvent,ContributionEvent,now)
from .company import get_company,remaining
from .costs import estimate_execution
from .execution import execute
from .brain import current as current_knowledge
from ..schemas import MEETING_SYNTHESIS_FIELDS,MEETING_SYNTHESIS_SCHEMA

ACTIVE="ACTIVE"
TERMINAL={"ENDED","TERMINATED_BY_FOUNDER"}
SIGNALS={"VALUABLE","LOW_VALUE","KEY_INSIGHT","UNSUPPORTED","WASTEFUL","CRITICAL_CATCH"}

def create(title,purpose,agenda,chair,participants,project=None,max_rounds=3,token_limit=12000,real_cost_limit_twd=100):
    if not title.strip() or not purpose.strip() or not agenda.strip(): raise ValueError("Meeting title, purpose, and agenda are required")
    if not chair.id or db.session.get(Employee,chair.id) is not chair: raise ValueError("Meeting chair must be a valid persisted Employee")
    if any(not e.id or db.session.get(Employee,e.id) is not e for e in participants): raise ValueError("Only valid persisted Employees may participate")
    unique={e.id:e for e in participants}
    if chair.id not in unique: unique[chair.id]=chair
    if any(not e.active for e in unique.values()): raise ValueError("Only active Employees may participate")
    if int(max_rounds)<=0 or int(token_limit)<=0 or Decimal(real_cost_limit_twd)<0: raise ValueError("Invalid Meeting limits")
    meeting=Meeting(company_id=get_company().id,project_id=getattr(project,"id",None),title=title.strip(),purpose=purpose.strip(),
      agenda=agenda.strip(),chair_employee_id=chair.id,max_rounds=int(max_rounds),token_limit=int(token_limit),
      real_cost_limit_twd=Decimal(real_cost_limit_twd),status="PLANNED")
    db.session.add(meeting); db.session.flush()
    for employee in unique.values():
        db.session.add(MeetingParticipant(meeting_id=meeting.id,employee_id=employee.id,role="CHAIR" if employee.id==chair.id else "PARTICIPANT"))
    db.session.commit(); return meeting

def start(meeting):
    if meeting.status!="PLANNED": raise ValueError("Only PLANNED Meetings may start")
    meeting.status=ACTIVE; meeting.started_at=now()
    db.session.add(MeetingMessage(meeting_id=meeting.id,speaker_type="SYSTEM",round_number=0,message_type="SYSTEM",content="Meeting started. Founder is observing."))
    db.session.commit(); return meeting

def usage(meeting):
    tokens=db.session.query(func.coalesce(func.sum(AgentRun.input_tokens+AgentRun.output_tokens),0)).filter_by(meeting_id=meeting.id).scalar()
    cost=db.session.query(func.coalesce(func.sum(CostEvent.real_cost_delta),0)).join(AgentRun,CostEvent.agent_run_id==AgentRun.id).filter(AgentRun.meeting_id==meeting.id).scalar()
    return int(tokens or 0),Decimal(cost or 0)

def compact_context(meeting,employee):
    previous=max(0,meeting.current_round)
    recent=MeetingMessage.query.filter_by(meeting_id=meeting.id,round_number=previous).order_by(MeetingMessage.id.desc()).limit(6).all()
    founder=MeetingMessage.query.filter_by(meeting_id=meeting.id,speaker_type="FOUNDER").order_by(MeetingMessage.id.desc()).limit(5).all()
    controls=MeetingMessage.query.filter(MeetingMessage.meeting_id==meeting.id,MeetingMessage.message_type.in_(["FOCUS","REQUEST_EVIDENCE"])).order_by(MeetingMessage.id.desc()).limit(3).all()
    parts=[f"MEETING\n{meeting.title}\nPurpose: {meeting.purpose}\nAgenda: {meeting.agenda}",
      f"ROUND PROTOCOL\nNext round: {meeting.current_round+1}. Round 1: POSITION, BASIS, RISK only. Later rounds: contribute only disagreement, counterevidence, new information, or material refinement.",
      f"EMPLOYEE\n{employee.name}; role: {employee.role_description}"]
    if meeting.project:
        parts.append(
          f"PROJECT #{meeting.project.id}\n{meeting.project.name}; status: {meeting.project.status}; "
          f"origin: {meeting.project.origin}; current state: {meeting.project.current_state_summary or 'Not recorded'}; "
          f"constraints: {meeting.project.known_constraints or 'None recorded'}")
    visible=current_knowledge(meeting.project_id) if meeting.project_id else current_knowledge(None)
    knowledge=list({item.id:item for item in visible}.values())[:12]
    if knowledge:
        parts.append("RELEVANT COMPANY BRAIN IDS\n"+"\n".join(
          f"#{item.id} [{item.kind}] {item.title}" for item in knowledge))
    if meeting.current_summary_json: parts.append("PREVIOUS ROUND SUMMARY\n"+str(meeting.current_summary_json))
    if recent: parts.append("RECENT RELEVANT DISCUSSION\n"+"\n".join(f"{m.message_type}: {m.content}" for m in reversed(recent)))
    if controls: parts.append("FOUNDER COMMANDS\n"+"\n".join(m.content for m in reversed(controls)))
    if founder: parts.append("FOUNDER INTERVENTIONS\n"+"\n".join(m.content for m in reversed(founder)))
    return "\n\n".join(parts)

def _check_step(meeting,employee,system_prompt,context,user_request,allow_final=False):
    if meeting.status!=ACTIVE: raise ValueError("Meeting is not ACTIVE")
    if not allow_final and meeting.current_round>=meeting.max_rounds: raise ValueError("Meeting maximum rounds reached")
    tokens,cost=usage(meeting)
    estimate=estimate_execution(employee.current_model,system_prompt,context,user_request)
    if tokens+estimate.input_tokens+estimate.output_tokens>meeting.token_limit: raise ValueError("Meeting token limit would be exceeded")
    if cost+estimate.real_cost>Decimal(meeting.real_cost_limit_twd): raise ValueError("Meeting real-cost limit would be exceeded")
    if estimate.real_cost>remaining(): raise ValueError("Company real budget would be exceeded")

def _derive_summary(meeting):
    messages=MeetingMessage.query.filter_by(meeting_id=meeting.id).order_by(MeetingMessage.id.desc()).limit(12).all()
    employee=[m for m in messages if m.speaker_type=="EMPLOYEE"]
    summary={"current_topic":meeting.agenda,"agreement":employee[0].content if employee else "No positions yet.",
      "disagreement":"Unresolved disagreement remains." if meeting.current_round>1 else "None recorded.",
      "evidence":"Participants must distinguish evidence from reasoning.","pending_question":"What requires Founder decision?",
      "next_likely_action":"Advance the bounded round or end and synthesize.",
      "founder_decision_required":"Review final recommendations."}
    meeting.current_summary_json=summary; return summary

def advance(meeting):
    if meeting.status!=ACTIVE: raise ValueError("Meeting is not ACTIVE")
    if meeting.current_round>=meeting.max_rounds: raise ValueError("Meeting maximum rounds reached")
    next_round=meeting.current_round+1
    for participant in [p for p in meeting.participants if p.removed_at is None]:
        employee=participant.employee
        completed=MeetingMessage.query.join(AgentRun,MeetingMessage.agent_run_id==AgentRun.id).filter(
          MeetingMessage.meeting_id==meeting.id,MeetingMessage.round_number==next_round,
          MeetingMessage.employee_id==employee.id,MeetingMessage.speaker_type=="EMPLOYEE",
          AgentRun.status=="SUCCEEDED").first()
        if completed: continue
        context=compact_context(meeting,employee)
        prompt=employee.system_instructions+"\nMEETING_CONTRIBUTION\nBe compact. Follow the round protocol. Do not mutate authoritative company state."
        user_request=f"Contribute to round {next_round}"
        _check_step(meeting,employee,prompt,context,user_request)
        run=execute(employee,"MEETING_CONTRIBUTION",user_request,meeting.project,
          context_override=context,system_prompt_override=prompt,meeting=meeting)
        if run.status!="SUCCEEDED": raise ValueError(run.error_text or "Meeting contribution failed")
        msg_type="POSITION" if next_round==1 else "MATERIAL_REFINEMENT"
        db.session.add(MeetingMessage(meeting_id=meeting.id,employee_id=employee.id,speaker_type="EMPLOYEE",
          round_number=next_round,message_type=msg_type,content=run.raw_output,agent_run_id=run.id))
        db.session.commit()
    meeting.current_round=next_round; _derive_summary(meeting); db.session.commit(); return meeting

def join_founder(meeting):
    if meeting.status!=ACTIVE: raise ValueError("Meeting is not ACTIVE")
    if not meeting.founder_joined_at:
        meeting.founder_joined_at=now()
        db.session.add(MeetingMessage(meeting_id=meeting.id,speaker_type="SYSTEM",round_number=meeting.current_round,
          message_type="SYSTEM",content="Founder joined the Meeting."))
        db.session.commit()
    return meeting

def intervene(meeting,content):
    if meeting.status!=ACTIVE: raise ValueError("Meeting is not ACTIVE")
    if not meeting.founder_joined_at: raise ValueError("Founder must join before intervening")
    msg=MeetingMessage(meeting_id=meeting.id,speaker_type="FOUNDER",round_number=meeting.current_round,
      message_type="FOUNDER_INTERVENTION",content=content.strip())
    db.session.add(msg); db.session.commit(); return msg

def command(meeting,kind,content):
    if meeting.status!=ACTIVE: raise ValueError("Meeting is not ACTIVE")
    if kind not in {"FOCUS","REQUEST_EVIDENCE"}: raise ValueError("Invalid Meeting command")
    msg=MeetingMessage(meeting_id=meeting.id,speaker_type="FOUNDER",round_number=meeting.current_round,message_type=kind,content=content.strip())
    db.session.add(msg); db.session.commit(); return msg

def stop(meeting,reason=None):
    if meeting.status!=ACTIVE: raise ValueError("Meeting is not ACTIVE")
    meeting.status="TERMINATED_BY_FOUNDER"; meeting.termination_reason=reason; meeting.ended_at=now()
    db.session.add(MeetingMessage(meeting_id=meeting.id,speaker_type="SYSTEM",round_number=meeting.current_round,message_type="SYSTEM",content="Meeting terminated by Founder."))
    db.session.commit(); return meeting

def end_and_synthesize(meeting):
    if meeting.status!=ACTIVE: raise ValueError("Meeting is not ACTIVE")
    context=compact_context(meeting,meeting.chair)
    prompt=meeting.chair.system_instructions+"\nMEETING_SYNTHESIS\nSummarize agreements, disagreements, evidence, unresolved issues, actions, and Founder decisions required."
    user_request="Produce final Meeting synthesis"
    _check_step(meeting,meeting.chair,prompt,context,user_request,allow_final=True)
    run=execute(meeting.chair,"MEETING_SYNTHESIS",user_request,meeting.project,
      context_override=context,system_prompt_override=prompt,response_schema=MEETING_SYNTHESIS_SCHEMA,meeting=meeting)
    if run.status!="SUCCEEDED": raise ValueError(run.error_text or "Meeting synthesis failed")
    try:
        synthesis=json.loads(run.raw_output)
        expected=set(MEETING_SYNTHESIS_FIELDS)
        if not isinstance(synthesis,dict) or set(synthesis)!=expected: raise ValueError("unexpected fields")
        for field in expected:
            values=synthesis[field]
            if not isinstance(values,list) or len(values)>8 or any(not isinstance(x,str) or not x.strip() or len(x)>500 for x in values):
                raise ValueError(f"invalid {field}")
        run.parsed_output_json=synthesis
        db.session.commit()
    except Exception as exc:
        run.status="FAILED"; run.error_text=f"Meeting synthesis validation failed: {exc}"
        db.session.commit()
        raise ValueError(run.error_text)
    db.session.add(MeetingMessage(meeting_id=meeting.id,employee_id=meeting.chair.id,speaker_type="EMPLOYEE",
      round_number=meeting.current_round,message_type="CHAIR_SYNTHESIS",content=run.raw_output,agent_run_id=run.id))
    tokens,cost=usage(meeting)
    founder_messages=MeetingMessage.query.filter_by(meeting_id=meeting.id,speaker_type="FOUNDER",message_type="FOUNDER_INTERVENTION").all()
    meeting.minutes_json={"purpose":meeting.purpose,"participants":[p.employee.name for p in meeting.participants],
      **synthesis,"founder_interventions":[m.content for m in founder_messages],
      "token_usage":tokens,"real_cost_twd":str(cost)}
    meeting.status="ENDED"; meeting.ended_at=now(); _derive_summary(meeting); db.session.commit(); return meeting

def feedback(message,signal,note=None):
    if signal not in SIGNALS or message.speaker_type!="EMPLOYEE" or not message.employee_id: raise ValueError("Invalid feedback")
    row=FounderFeedbackEvent(meeting_id=message.meeting_id,message_id=message.id,employee_id=message.employee_id,signal=signal,note=note)
    db.session.add(row); db.session.commit(); return row
