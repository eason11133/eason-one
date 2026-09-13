from decimal import Decimal
import json
from dataclasses import dataclass
from types import SimpleNamespace
from sqlalchemy import func,update
from sqlalchemy.exc import IntegrityError
from ..extensions import db
from ..models import (Meeting,MeetingParticipant,MeetingMessage,MeetingStep,FounderFeedbackEvent,Employee,
    AgentRun,Artifact,ArtifactVersion,CostEvent,ContributionEvent,Work,now)
from .company import get_company,remaining
from .costs import estimate_execution
from .execution import execute
from .execution_policy import select_execution_model
from . import meeting_kernel
from .context import relevant_existing_evidence
from .brain import current as current_knowledge
from ..schemas import (MEETING_SYNTHESIS_FIELDS,MEETING_SYNTHESIS_SCHEMA,
  MEETING_ROUND1_SCHEMA,MEETING_LATER_SCHEMA,MEETING_ROUTER_SCHEMA)
from ..schemas import MEETING_RESPONSE_SCHEMA

ACTIVE="ACTIVE"
TERMINAL={"ENDED","TERMINATED_BY_FOUNDER"}
SIGNALS={"VALUABLE","LOW_VALUE","KEY_INSIGHT","UNSUPPORTED","WASTEFUL","CRITICAL_CATCH"}
COMPACT_TARGET={"position":180,"action":120,"risk":180,"core_point":180,"control":120}
HARD_LIMIT={"position":600,"action":400,"risk":600,"core_point":600,"control":400}
PROMPT_VERSIONS={"router":"meeting-router-v2.0","round1":"meeting-round1-v2.0","response":"meeting-response-v2.0","later":"meeting-later-v2.0","repair":"meeting-compact-repair-v1.0","synthesis":"meeting-synthesis-v2.0"}




def _assert_project_meeting_mutable(meeting, *, action: str):
    project = getattr(meeting, "project", None)
    if project is not None and __import__(
        "eason_one.services.work_runtime", fromlist=["project_is_terminal"]
    ).project_is_terminal(project):
        raise ValueError(
            f"PROJECT_TERMINAL_MEETING_MUTATION_FORBIDDEN:{action}:{str(project.status or '').upper()}"
        )


def _governing_work(meeting):
    """Return the Work-first authority owner for a vNext Meeting, if any."""
    if not getattr(meeting, "related_work_id", None):
        return None
    return db.session.get(Work, meeting.related_work_id)

def _active_speakers(meeting):
    active = [
        item.employee for item in meeting.participants
        if item.removed_at is None and item.employee.active
    ]
    operation = meeting.operation
    config = ((operation.plan_json or {}).get("operation", {}).get("meeting_config") or {}) if operation else {}
    approved_ids = set(config.get("participant_employee_ids") or [])
    if operation and approved_ids and meeting.chair_employee_id not in approved_ids:
        specialists = [employee for employee in active if employee.id in approved_ids]
        if specialists:
            return specialists
    return active

@dataclass(frozen=True)
class StructuredValidation:
    status: str
    data: dict
    warnings: list

class StructuredValidationError(ValueError):
    def __init__(self,message,code="STRUCTURED_OUTPUT_INVALID",field=None):
        super().__init__(message)
        self.errors=[{"code":code,"field":field}]

def _invalid(message,field=None):
    raise StructuredValidationError(message,field=field)

def _compact_warning(field,target,actual):
    return {"code":"COMPACTNESS_TARGET_EXCEEDED","field":field,"target":target,"actual":actual}
PROFILES={
  "ECONOMY":{"max_rounds":2,"max_speakers":2,"contribution_cap":512,"router_cap":192,"synthesis_cap":512,"cost":1,"tokens":12000},
  "STANDARD":{"max_rounds":3,"max_speakers":3,"contribution_cap":640,"router_cap":256,"synthesis_cap":768,"cost":5,"tokens":24000},
  "DEEP":{"max_rounds":5,"max_speakers":4,"contribution_cap":1024,"router_cap":384,"synthesis_cap":1280,"cost":20,"tokens":50000},
}

def create(title,purpose,agenda,chair,participants,project=None,max_rounds=None,token_limit=None,real_cost_limit_twd=None,
           profile=None,max_speakers_per_round=None,contribution_output_cap=None,router_output_cap=None,synthesis_output_cap=None,
           max_messages=None):
    if project is not None and __import__(
        "eason_one.services.work_runtime", fromlist=["project_is_terminal"]
    ).project_is_terminal(project):
        raise ValueError(
            f"PROJECT_TERMINAL_MEETING_CREATION_FORBIDDEN:{str(project.status or '').upper()}"
        )
    if not title.strip() or not purpose.strip() or not agenda.strip(): raise ValueError("Meeting title, purpose, and agenda are required")
    if not chair.id or db.session.get(Employee,chair.id) is not chair: raise ValueError("Meeting chair must be a valid persisted Employee")
    if any(not e.id or db.session.get(Employee,e.id) is not e for e in participants): raise ValueError("Only valid persisted Employees may participate")
    unique={e.id:e for e in participants}
    if chair.id not in unique: unique[chair.id]=chair
    if any(not e.active for e in unique.values()): raise ValueError("Only active Employees may participate")
    profile_selected=profile is not None
    profile=(profile or "STANDARD").upper()
    if profile not in PROFILES: raise ValueError("Invalid Meeting profile")
    defaults=PROFILES[profile]
    if max_rounds is None: max_rounds=defaults["max_rounds"] if profile_selected else 3
    if token_limit is None: token_limit=defaults["tokens"] if profile_selected else 12000
    if real_cost_limit_twd is None: real_cost_limit_twd=defaults["cost"] if profile_selected else 100
    if max_speakers_per_round is None: max_speakers_per_round=defaults["max_speakers"]
    if contribution_output_cap is None: contribution_output_cap=defaults["contribution_cap"]
    if router_output_cap is None: router_output_cap=defaults["router_cap"]
    if synthesis_output_cap is None: synthesis_output_cap=defaults["synthesis_cap"]
    if any(int(x)<=0 for x in (max_rounds,token_limit,max_speakers_per_round,contribution_output_cap,router_output_cap,synthesis_output_cap)) or Decimal(real_cost_limit_twd)<0:
        raise ValueError("Invalid Meeting limits")
    meeting=Meeting(company_id=get_company().id,project_id=getattr(project,"id",None),title=title.strip(),purpose=purpose.strip(),
      agenda=agenda.strip(),chair_employee_id=chair.id,max_rounds=int(max_rounds),token_limit=int(token_limit),
      real_cost_limit_twd=Decimal(real_cost_limit_twd),status="PLANNED",kernel_status="PLANNED",
      current_stage="PLANNING",execution_profile=profile,
      max_messages=int(max_messages or (int(max_rounds)*int(max_speakers_per_round)+2)),
      max_speakers_per_round=int(max_speakers_per_round),contribution_output_cap=int(contribution_output_cap),
      router_output_cap=int(router_output_cap),synthesis_output_cap=int(synthesis_output_cap))
    db.session.add(meeting); db.session.flush()
    for employee in unique.values():
        db.session.add(MeetingParticipant(meeting_id=meeting.id,employee_id=employee.id,role="CHAIR" if employee.id==chair.id else "PARTICIPANT"))
    meeting_kernel.transition(meeting,"READY","MEETING_READY",stage="READY",legacy_status="PLANNED",commit=False,
      payload={"participant_count":len(unique),"max_rounds":meeting.max_rounds,"max_messages":meeting.max_messages})
    db.session.commit(); return meeting

def start(meeting):
    _assert_project_meeting_mutable(meeting, action="START")
    if meeting_kernel.status(meeting) not in {"PLANNED","READY"}: raise ValueError("Only a ready Meeting may start")
    meeting_kernel.ensure_message_capacity(meeting)
    meeting_kernel.transition(meeting,"ACTIVE","MEETING_STARTED",stage="ACTIVE",legacy_status=ACTIVE,actor_type="FOUNDER",commit=False)
    db.session.add(MeetingMessage(meeting_id=meeting.id,speaker_type="SYSTEM",round_number=0,message_type="SYSTEM",content="Meeting started. Founder is observing."))
    db.session.commit(); return meeting

def usage(meeting):
    tokens=db.session.query(func.coalesce(func.sum(
      AgentRun.input_tokens+AgentRun.output_tokens+
      AgentRun.cache_creation_input_tokens+AgentRun.cache_read_input_tokens),0)).filter_by(meeting_id=meeting.id).scalar()
    cost=db.session.query(func.coalesce(func.sum(CostEvent.real_cost_delta),0)).join(AgentRun,CostEvent.agent_run_id==AgentRun.id).filter(AgentRun.meeting_id==meeting.id).scalar()
    return int(tokens or 0),Decimal(cost or 0)

def _compact_message_content(message):
    try:
        data=json.loads(message.content)
    except Exception:
        return None
    allowed={"position","actions","risk","evidence_ids","confidence","relation","core_point","controls",
      "has_material_contribution","type","target_message_ids"}
    compact={key:data[key] for key in data if key in allowed}
    return json.dumps(compact,separators=(",",":"),ensure_ascii=False)

def _meeting_question(meeting):
    question=(meeting.agenda or "").strip()
    if not question or question==meeting.purpose.strip():
        question=f"What bounded decision should this Meeting reach about {meeting.title}?"
    return question[:240]

def _context_packet(meeting,employee,target_round=None,include_invited=True):
    """Build a bounded shared Meeting packet.

    The full transcript remains authoritative in the database.  Active prompts
    receive only the deterministic Meeting state plus the latest validated
    deltas needed for this speaker.  This keeps cross-round continuity without
    replaying the entire transcript on every provider call.
    """
    components={
      "meeting":f"MEETING\nID: {meeting.id}\nTITLE: {meeting.title}\nPROFILE: {meeting.execution_profile}",
      "mission":f"MISSION_CONTEXT\n{meeting.purpose.strip()}",
      "question":f"MEETING_QUESTION\n{_meeting_question(meeting)}",
      "employee":f"EMPLOYEE\nNAME: {employee.name}\nROLE: {employee.role_description}",
      "protocol":("COMMUNICATION CONTRACT\nOnly add a material delta: a claim, challenge, evidence, control, or correction. "
        "Do not repeat accepted points. Use EVIDENCE_ID only for Company Brain evidence and "
        "MEETING_MESSAGE_ID only for Meeting messages. Return the exact JSON schema only."),
    }
    memory = __import__(
        "eason_one.services.employee_memory",
        fromlist=["relevant_experience", "founder_feedback_context"],
    )
    seed_task = meeting.operation.tasks[0] if meeting.operation and meeting.operation.tasks else None
    experience_text, experience_meta = memory.relevant_experience(
        employee, meeting.project, seed_task, limit=4
    )
    if experience_text:
        components["employee_experience"] = experience_text
    feedback_text, feedback_meta = memory.founder_feedback_context(employee, limit=4)
    if feedback_text:
        components["founder_feedback"] = feedback_text
    operation = meeting.operation
    if operation:
        operation_lines = [
            f"OPERATION #{operation.id}: {operation.title}",
            f"OBJECTIVE: {operation.objective}",
        ]
        executive = ((operation.plan_json or {}).get("executive_response") or "").strip()
        if executive:
            operation_lines.append("CEO AUTHORITY CONTEXT: " + executive[:1200])
        works = [work for work in sorted(operation.works, key=lambda row: row.id) if work.work_type != "MANAGEMENT"]
        if works:
            for work in works:
                task = next((row for row in operation.tasks if row.work_id == work.id), None)
                assignment = __import__(
                    "eason_one.services.work_runtime", fromlist=["active_assignment"]
                ).active_assignment(work)
                owner = db.session.get(Employee, assignment.employee_id) if assignment else None
                operation_lines.append(
                    f"WORK #{work.id} — {work.state} — {work.title} — owner: {owner.name if owner else '-'}"
                )
                version = (
                    ArtifactVersion.query.join(Artifact)
                    .filter(
                        Artifact.work_id == work.id,
                        ArtifactVersion.status == "ACCEPTED",
                    )
                    .order_by(ArtifactVersion.id.desc()).first()
                )
                if version is not None:
                    operation_lines.append(
                        f"ACCEPTED ARTIFACT #{version.id} hash={version.content_hash}: "
                        + " ".join((version.content_text or "").split())[:1400]
                    )
                elif task and task.result_summary:
                    operation_lines.append(
                        f"COMPATIBILITY RESULT SUMMARY: {task.result_summary[:1000]}"
                    )
        else:
            for task in operation.tasks:
                if task.result_summary:
                    operation_lines.append(
                        f"LEGACY TASK #{task.id} RESULT — {task.title}: {task.result_summary[:1200]}"
                    )
        components["operation_results"] = "PERSISTED OPERATION RESULTS\n" + "\n".join(operation_lines)
        seed_task = operation.tasks[0] if operation.tasks else None
        rows, _ = relevant_existing_evidence(seed_task) if seed_task else ([], {})
        if rows:
            components["retrieved_evidence"] = "RETRIEVED EXISTING EASON ONE EVIDENCE\n" + "\n".join(
                f"SOURCE: {row['source']} | CLAIM: {row['content']}" for row in rows[:6]
            )
    state=_compact_live_brief(meeting) if MeetingMessage.query.filter_by(
      meeting_id=meeting.id,speaker_type="EMPLOYEE").count() else {
        "current_topic":meeting.agenda,
        "agreement":"None established.",
        "key_disagreement":"None established.",
        "evidence":"None presented.",
        "open_question":_meeting_question(meeting),
        "next_step":"Provide the first bounded contribution.",
      }
    components["state"]="STRUCTURED MEETING STATE\n"+json.dumps(
      state,separators=(",",":"),ensure_ascii=False)
    visible=list({item.id:item for item in current_knowledge(meeting.project_id)}.values())[:12]
    if visible:
        components["evidence"]="AVAILABLE COMPANY BRAIN EVIDENCE\n"+"\n".join(
          f"EVIDENCE_ID: {item.id} | TYPE: {item.kind} | CLAIM: {(item.content or item.title)[:240]}"
          for item in visible)
    else:
        components["evidence"]="AVAILABLE COMPANY BRAIN EVIDENCE\nNone. Return evidence_ids: []."

    query=MeetingMessage.query.filter_by(meeting_id=meeting.id,speaker_type="EMPLOYEE")
    if target_round == 1:
        query=query.filter(MeetingMessage.round_number==1)
    elif target_round and target_round > 1:
        query=query.filter(MeetingMessage.round_number < target_round)
    rows=query.order_by(MeetingMessage.id.desc()).limit(6).all()
    compact=[(m,_compact_message_content(m)) for m in reversed(rows)]
    lines=[f"MEETING_MESSAGE_ID: {m.id} | ROUND: {m.round_number} | SPEAKER: {m.employee.name} | {content}"
      for m,content in compact if content]
    components["prior_contributions"]="RELEVANT VALIDATED CONTRIBUTIONS\n"+("\n".join(lines) if lines else "None.")

    founder=MeetingMessage.query.filter_by(meeting_id=meeting.id,speaker_type="FOUNDER",
      message_type="FOUNDER_INTERVENTION").order_by(MeetingMessage.id.desc()).first()
    if founder:
        components["founder"]="LATEST FOUNDER INTERVENTION\n"+founder.content[:500]
    if include_invited:
        components["invited"]="INVITED EMPLOYEES\n"+"\n".join(
          f"INVITED_SLUG: {employee.slug}" for employee in _active_speakers(meeting))
    context="\n\n".join(components.values())
    composition={"total_chars":len(context),"estimated_tokens":max(1,(len(context)+3)//4),"components":{
      name:{"chars":len(value),"estimated_tokens":max(1,(len(value)+3)//4)}
      for name,value in components.items()},
      "employee_memory":{"experience":experience_meta,"founder_feedback":feedback_meta}}
    return context,composition

def compact_context(meeting,employee,target_round=None):
    return _context_packet(meeting,employee,target_round)[0]

def _check_step(meeting,employee,system_prompt,context,user_request,response_schema=None,allow_final=False,max_output_tokens_override=None):
    state = meeting_kernel.status(meeting)
    if state != "ACTIVE" and not (allow_final and state == "SYNTHESIZING"):
        raise ValueError("Meeting is not executable")
    meeting_kernel.ensure_message_capacity(meeting)
    if not allow_final and meeting.current_round>=meeting.max_rounds: raise ValueError("Meeting maximum rounds reached")
    tokens,cost=usage(meeting)
    model=select_execution_model(employee, meeting.operation, "MEETING")
    effective=model.max_output_tokens if max_output_tokens_override is None else min(int(max_output_tokens_override),model.max_output_tokens)
    estimate=estimate_execution(model,system_prompt,context,user_request,effective,response_schema)
    remaining_cost=Decimal(meeting.real_cost_limit_twd)-cost
    def blocked(reason):
        meeting.last_blocked_json={"reason":reason,"employee":employee.name,"model":model.label,
          "estimated_cost_twd":str(estimate.real_cost),"remaining_cost_twd":str(max(Decimal(0),remaining_cost)),
          "message":"BLOCKED BEFORE API CALL — No provider call was made."}
        db.session.commit()
        raise ValueError(f"{reason}. BLOCKED BEFORE API CALL; Employee: {employee.name}; Model: {model.label}; "
          f"Conservative next-call estimate: TWD {estimate.real_cost}; Meeting remaining: TWD {max(Decimal(0),remaining_cost)}; No provider call was made.")
    if tokens+estimate.input_tokens+estimate.output_tokens>meeting.token_limit: blocked("Meeting token limit would be exceeded")
    if cost+estimate.real_cost>Decimal(meeting.real_cost_limit_twd): blocked("Meeting real-cost limit would be exceeded")
    if estimate.real_cost>remaining(): blocked("Company real budget would be exceeded")
    meeting.last_blocked_json=None
    return model

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
    _assert_project_meeting_mutable(meeting, action="ADVANCE")
    if meeting.status!=ACTIVE: raise ValueError("Meeting is not ACTIVE")
    if meeting.current_round>=meeting.max_rounds: raise ValueError("Meeting maximum rounds reached")
    next_round=meeting.current_round+1
    for employee in _active_speakers(meeting):
        completed=MeetingMessage.query.join(AgentRun,MeetingMessage.agent_run_id==AgentRun.id).filter(
          MeetingMessage.meeting_id==meeting.id,MeetingMessage.round_number==next_round,
          MeetingMessage.employee_id==employee.id,MeetingMessage.speaker_type=="EMPLOYEE",
          AgentRun.status=="SUCCEEDED").first()
        if completed: continue
        context=compact_context(meeting,employee)
        prompt=employee.system_instructions+"\nMEETING_CONTRIBUTION\nBe compact. Follow the round protocol. Do not mutate authoritative company state."
        user_request=f"Contribute to round {next_round}"
        model=_check_step(meeting,employee,prompt,context,user_request)
        run=execute(employee,"MEETING_CONTRIBUTION",user_request,meeting.project,
          context_override=context,system_prompt_override=prompt,meeting=meeting,
          operation=meeting.operation,work=_governing_work(meeting),model_override=model)
        if run.status!="SUCCEEDED": raise ValueError(run.error_text or "Meeting contribution failed")
        msg_type="POSITION" if next_round==1 else "MATERIAL_REFINEMENT"
        db.session.add(MeetingMessage(meeting_id=meeting.id,employee_id=employee.id,speaker_type="EMPLOYEE",
          round_number=next_round,message_type=msg_type,content=run.raw_output,agent_run_id=run.id))
        db.session.commit()
    meeting.current_round=next_round; _derive_summary(meeting); db.session.commit(); return meeting

def join_founder(meeting):
    if meeting.status not in {ACTIVE,"RUNNING","PAUSED","WAITING_FOR_FOUNDER"}: raise ValueError("Meeting is not joinable")
    if not meeting.founder_joined_at:
        meeting.founder_joined_at=now()
        db.session.add(MeetingMessage(meeting_id=meeting.id,speaker_type="SYSTEM",round_number=meeting.current_round,
          message_type="SYSTEM",content="Founder joined the Meeting."))
        db.session.commit()
    return meeting

def intervene(meeting,content):
    if meeting.status not in {ACTIVE,"RUNNING","PAUSED","WAITING_FOR_FOUNDER"}: raise ValueError("Meeting is not joinable")
    if not meeting.founder_joined_at: raise ValueError("Founder must join before intervening")
    msg=MeetingMessage(meeting_id=meeting.id,speaker_type="FOUNDER",round_number=meeting.current_round,
      message_type="FOUNDER_INTERVENTION",content=content.strip())
    db.session.add(msg); db.session.commit(); return msg

def command(meeting,kind,content):
    if meeting.status not in {ACTIVE,"RUNNING","PAUSED","WAITING_FOR_FOUNDER"}: raise ValueError("Meeting is not controllable")
    if kind not in {"FOCUS","REQUEST_EVIDENCE"}: raise ValueError("Invalid Meeting command")
    msg=MeetingMessage(meeting_id=meeting.id,speaker_type="FOUNDER",round_number=meeting.current_round,message_type=kind,content=content.strip())
    db.session.add(msg); db.session.commit(); return msg

def skip(meeting, reason):
    """Deterministically close a planned escalation that was not needed.

    This is not a Founder cancellation. It records that the approved Meeting
    gate was evaluated after inputs were ready and no material conflict was
    found, so no provider call was made.
    """
    if meeting_kernel.status(meeting) not in {"PLANNED", "READY"}:
        raise ValueError("Only a planned Meeting may be skipped")
    reason=(reason or "No material conflict required escalation.").strip()
    meeting.minutes_json={
      "agreements":["Meeting gate evaluated; specialist outputs did not require cross-role debate."],
      "disagreements":[],"evidence_referenced":[],"actions":[],
      "founder_decisions_required":[],"rejected_or_unresolved":[],
      "positions":[],"responses":[],"token_usage":0,"real_cost_twd":"0",
      "provider_calls":0,"provider_call_breakdown":{"total":0,"successful":0,"failed":0,"retries":0,"truncated":0},
      "skip_reason":reason,
    }
    meeting.termination_reason=reason
    meeting.current_summary_json={
      "current_topic":meeting.agenda,
      "agreement":"No material conflict required a Meeting.",
      "key_disagreement":"None detected.",
      "evidence":"Persisted Task and Review results were sufficient.",
      "open_question":"None.",
      "next_step":"Continue to goal verification and Founder delivery.",
    }
    meeting_kernel.transition(
      meeting,"CANCELLED","MEETING_SKIPPED_NO_MATERIAL_CONFLICT",stage="COMPLETED",
      legacy_status="ENDED",actor_type="CEO",commit=False,
      payload={"reason":reason,"provider_calls":0},
    )
    db.session.add(MeetingMessage(
      meeting_id=meeting.id,speaker_type="SYSTEM",round_number=0,
      message_type="SYSTEM",content="Meeting gate evaluated and skipped at zero provider cost: "+reason,
    ))
    db.session.commit(); return meeting


def stop(meeting,reason=None):
    if meeting_kernel.status(meeting) in meeting_kernel.TERMINAL: raise ValueError("Meeting is not stoppable")
    meeting_kernel.transition(meeting,"CANCELLED","MEETING_CANCELLED",stage="CANCELLED",
      legacy_status="TERMINATED_BY_FOUNDER",actor_type="FOUNDER",commit=False,payload={"reason":reason})
    meeting.termination_reason=reason
    meeting.current_summary_json=_compact_live_brief(meeting)
    meeting.current_summary_json=(meeting.current_summary_json or {})|{
      "next_step":"No further execution. Meeting was terminated by Founder."}
    db.session.add(MeetingMessage(meeting_id=meeting.id,speaker_type="SYSTEM",round_number=meeting.current_round,message_type="SYSTEM",content="Meeting terminated by Founder."))
    db.session.commit(); return meeting

def end_and_synthesize(meeting):
    _assert_project_meeting_mutable(meeting, action="SYNTHESIZE")
    if meeting.status!=ACTIVE: raise ValueError("Meeting is not ACTIVE")
    step,execute_now=_reserve_step(
      meeting,"FINAL_SYNTHESIS","FINAL_SYNTHESIS",meeting.current_round,meeting.chair)
    if not execute_now:
        if step.status=="SUCCEEDED":
            return meeting
        if step.status=="RUNNING" and recover_interrupted_step(step):
            return db.session.get(Meeting,meeting.id)
        raise ValueError("Meeting synthesis already has an unresolved durable step")
    context,composition=_meeting_context(meeting,meeting.chair)
    composition=_step_composition(composition,step)
    prompt=meeting.chair.system_instructions+"\nMEETING_SYNTHESIS\nSummarize agreements, disagreements, evidence, unresolved issues, actions, and Founder decisions required."
    user_request="Produce final Meeting synthesis"
    run=None
    meeting_kernel.transition(
      meeting,"SYNTHESIZING","MEETING_SYNTHESIS_STARTED",stage="SYNTHESIZING",legacy_status=ACTIVE,commit=False)
    db.session.commit()
    try:
        model=_check_step(
          meeting,meeting.chair,prompt,context,user_request,
          response_schema=MEETING_SYNTHESIS_SCHEMA,allow_final=True,
          max_output_tokens_override=meeting.synthesis_output_cap)
        run=execute(
          meeting.chair,"MEETING_SYNTHESIS",user_request,meeting.project,
          context_override=context,system_prompt_override=prompt,
          response_schema=MEETING_SYNTHESIS_SCHEMA,meeting=meeting,
          operation=meeting.operation,work=_governing_work(meeting),model_override=model,
          max_output_tokens_override=min(meeting.synthesis_output_cap,model.max_output_tokens),
          context_composition=composition,prompt_version=PROMPT_VERSIONS["synthesis"])
        if run.status!="SUCCEEDED":
            raise ValueError(run.error_text or "Meeting synthesis failed")
        _commit_synthesis_run(meeting,step,run)
        return meeting
    except Exception as exc:
        _handle_step_failure(meeting,step,run,exc)
        raise

def feedback(message,signal,note=None):
    if signal not in SIGNALS or message.speaker_type!="EMPLOYEE" or not message.employee_id: raise ValueError("Invalid feedback")
    row=FounderFeedbackEvent(meeting_id=message.meeting_id,message_id=message.id,employee_id=message.employee_id,signal=signal,note=note)
    db.session.add(row); db.session.commit(); return row

def start_auto(meeting):
    _assert_project_meeting_mutable(meeting, action="START_AUTO")
    # Idempotent Founder action: re-clicking Start on an already-running room
    # attaches the live runner instead of creating a false error state.
    if meeting.status=="RUNNING":
        return meeting
    if meeting.status=="WAITING_FOR_FOUNDER":
        raise ValueError("WAITING_FOR_FOUNDER requires a new Founder intervention and explicit resume")
    if meeting.status not in {"PLANNED","PAUSED"}:
        raise ValueError("Meeting cannot start or resume from its current state")
    if meeting.paid_failure_json:
        raise ValueError("Paid failed step must use the bounded Meeting recovery path")
    if meeting.status=="PLANNED":
        meeting.started_at=now()
        existing=MeetingMessage.query.filter_by(meeting_id=meeting.id,message_type="SYSTEM").filter(
          MeetingMessage.content.like("Autonomous Meeting started%" )).first()
        if not existing:
            db.session.add(MeetingMessage(meeting_id=meeting.id,speaker_type="SYSTEM",round_number=0,
              message_type="SYSTEM",content="Autonomous Meeting started. Founder is observing."))
    meeting_kernel.transition(meeting,"ACTIVE","MEETING_AUTO_STARTED",stage="ACTIVE",legacy_status="RUNNING",commit=False)
    db.session.commit(); return meeting

def pause(meeting):
    if meeting_kernel.status(meeting)!="ACTIVE": raise ValueError("Only an active Meeting may pause")
    meeting_kernel.transition(meeting,"WAITING_FOR_INPUTS","MEETING_PAUSED",stage="WAITING_FOR_INPUTS",
      legacy_status="PAUSED",actor_type="FOUNDER")
    return meeting

def resume(meeting):
    _assert_project_meeting_mutable(meeting, action="RESUME")
    if meeting.status not in {"PAUSED","WAITING_FOR_FOUNDER"}: raise ValueError("Meeting is not paused")
    if meeting.paid_failure_json:
        raise ValueError("Paid failed step must use the bounded Meeting recovery path")
    if meeting.status=="WAITING_FOR_FOUNDER":
        latest=MeetingMessage.query.filter_by(meeting_id=meeting.id,message_type="FOUNDER_INTERVENTION").order_by(MeetingMessage.id.desc()).first()
        waited_at=(meeting.routing_json or {}).get("founder_message_id_at_wait",0)
        if not latest or latest.id<=waited_at:
            raise ValueError("A new Founder intervention is required before resuming")
        meeting.routing_json={"founder_resume_message_id":latest.id}
    meeting_kernel.transition(meeting,"ACTIVE","MEETING_RESUMED",stage="ACTIVE",legacy_status="RUNNING",
      actor_type="FOUNDER",commit=False)
    db.session.commit(); return meeting

def auto_state(meeting):
    tokens,cost=usage(meeting)
    breakdown=call_breakdown(meeting)
    calls=breakdown["total"]
    latest_message=MeetingMessage.query.filter_by(meeting_id=meeting.id).order_by(MeetingMessage.id.desc()).first()
    latest_step=MeetingStep.query.filter(MeetingStep.meeting_id==meeting.id,MeetingStep.status=="SUCCEEDED",
      MeetingStep.kind!="HTTP_REQUEST").order_by(MeetingStep.id.desc()).first()
    event=None
    if latest_message:
        parsed=_compact_message_content(latest_message)
        event={"type":latest_message.message_type,"speaker":latest_message.employee.name if latest_message.employee else latest_message.speaker_type,
          "employee_id":latest_message.employee_id,"round":latest_message.round_number,
          "contribution":json.loads(parsed) if parsed else {"content":latest_message.content[:300]},
          "validation_status":latest_message.validation_status,
          "warning_count":len(latest_message.validation_warnings_json or [])}
    if latest_step and latest_step.kind=="CHAIR_ROUTER":
        event={"type":"CHAIR_ROUTER","speaker":meeting.chair.name,"employee_id":meeting.chair.id,
          "round":latest_step.round_number,"contribution":latest_step.result_json}
    return {"meeting_id":meeting.id,"status":meeting.status,"round":meeting.current_round,
      "tokens":tokens,"cost_twd":str(cost),"calls":calls,"call_breakdown":breakdown,"continue_allowed":meeting.status=="RUNNING",
      "blocked":meeting.last_blocked_json,"paid_failure":meeting.paid_failure_json,"latest_event":event,
      "live_brief":meeting.current_summary_json or {}}

def primary_status_text(meeting):
    if meeting.paid_failure_json and meeting.status=="PAUSED":
        failure=meeting.paid_failure_json
        return (f"{failure.get('employee','A participant')} failed on "
          f"{failure.get('provider','provider')} / {failure.get('model','model')}: "
          f"{failure.get('reason','PAID_PROVIDER_FAILURE')}. Bounded Company recovery is pending.")
    return {"PLANNED":"Ready to start.","RUNNING":"Waiting for the next bounded event.",
      "PAUSED":"Meeting paused.","WAITING_FOR_FOUNDER":"Founder input required.",
      "ENDED":"Meeting complete.","TERMINATED_BY_FOUNDER":"Meeting terminated by Founder."}.get(
      meeting.status,"Meeting status unavailable.")

def result_view(meeting):
    minutes=meeting.minutes_json or {}
    if meeting.status not in TERMINAL or not minutes: return None
    positions=minutes.get("positions") or []
    responses=minutes.get("responses") or []
    synthesized=bool(minutes.get("agreements") is not None)
    qualification=next((x for x in reversed(responses) if x.get("relation")=="QUALIFY"),None)
    disagreement=next((x for x in reversed(responses) if x.get("relation")=="DISAGREE"),None)
    controls=[item for response in responses for item in response.get("controls",[])]
    risks=[x.get("risk") for x in positions+responses if x.get("risk")]
    recovery=minutes.get("recovery")
    tokens,cost=usage(meeting)
    return {"recovered":bool(recovery),"original_status":(recovery or {}).get("original_status"),
      "provider":(recovery or {}).get("provider"),"model":(recovery or {}).get("model"),
      "position":positions[-1].get("position") if positions else None,
      "actions":(minutes.get("actions") or []) if synthesized else [item for position in positions for item in position.get("actions",[])],
      "agreement":minutes.get("agreements") or minutes.get("agreement") or [],
      "qualification_or_disagreement":("; ".join(minutes.get("disagreements") or [])
        if synthesized else (qualification or disagreement or {}).get("core_point")),
      "controls":controls,"risk":risks[-1] if risks else None,
      "evidence_ids":minutes.get("evidence_referenced") or minutes.get("evidence_ids") or [],
      "founder_decisions":minutes.get("founder_decisions_required") or [],
      "unresolved":minutes.get("rejected_or_unresolved") or [],
      "cost_twd":minutes.get("real_cost_twd",str(cost)),"tokens":minutes.get("token_usage",tokens),
      "calls":minutes.get("provider_calls",confirmed_provider_calls(meeting)),
      "call_breakdown":minutes.get("provider_call_breakdown",call_breakdown(meeting)),
      "recovery":recovery}

def _reserve_step(meeting,key,kind,round_number,employee=None):
    step=MeetingStep.query.filter_by(meeting_id=meeting.id,logical_key=key).first()
    if step:
        claimed=db.session.execute(update(MeetingStep).where(
          MeetingStep.id==step.id,MeetingStep.status.in_(["PENDING","FAILED"])).values(
          status="RUNNING",error_text=None,finished_at=None)).rowcount
        db.session.commit()
        step=db.session.get(MeetingStep,step.id)
        return step,claimed==1
    step=MeetingStep(meeting_id=meeting.id,logical_key=key,kind=kind,round_number=round_number,
      employee_id=getattr(employee,"id",None),status="RUNNING")
    db.session.add(step)
    try:
        db.session.commit(); return step,True
    except IntegrityError:
        db.session.rollback()
        return MeetingStep.query.filter_by(meeting_id=meeting.id,logical_key=key).one(),False

def _finish_step(step,run=None,result=None):
    step.status="SUCCEEDED"; step.agent_run_id=getattr(run,"id",None)
    step.result_json=result; step.finished_at=now(); db.session.commit()

def _step_composition(composition, step):
    """Bind one paid provider attempt to the exact durable MeetingStep.

    MeetingStep is reserved before the provider call.  The provider result is
    durably committed inside ``execute()`` before the caller materializes the
    router/contribution/synthesis result.  Persisting this binding on AgentRun
    lets restart recovery finish that local materialization without buying the
    same answer again.
    """
    payload=dict(composition or {})
    payload["meeting_step"]={
      "id":int(step.id),"logical_key":step.logical_key,"kind":step.kind,
      "round_number":int(step.round_number or 0),
    }
    return payload

def _possible_unbound_runs(step):
    purpose={
      "CHAIR_ROUTER":"CHAIR_ROUTER",
      "CONTRIBUTION":"MEETING_CONTRIBUTION",
      "FINAL_SYNTHESIS":"MEETING_SYNTHESIS",
    }.get(step.kind)
    if not purpose:
        return []
    query=AgentRun.query.filter(
      AgentRun.meeting_id==step.meeting_id,
      AgentRun.purpose==purpose,
      AgentRun.started_at>=step.created_at,
    )
    if step.employee_id:
        query=query.filter(AgentRun.employee_id==step.employee_id)
    return query.order_by(AgentRun.id).all()

def _run_bound_to_step(step):
    if step.agent_run_id:
        run=db.session.get(AgentRun,step.agent_run_id)
        if run is not None:
            return run
    # JSON operators are intentionally avoided here so SQLite/Postgres behave
    # the same.  A Meeting is bounded, therefore scanning its own Runs is cheap.
    for run in AgentRun.query.filter_by(meeting_id=step.meeting_id).order_by(AgentRun.id.desc()).all():
        meta=dict(run.context_composition_json or {}).get("meeting_step") or {}
        if int(meta.get("id") or 0)==int(step.id):
            return run
    # Conservative bridge for already-existing RUNNING MeetingSteps created by
    # older code before the explicit step binding existed.  Only one uniquely
    # plausible Run may be adopted. Multiple candidates are deliberately left
    # unresolved so restart recovery cannot guess and double-spend.
    candidates=_possible_unbound_runs(step)
    return candidates[0] if len(candidates)==1 else None

def _commit_router_run(meeting,step,run,target_round):
    result=_parse_router(run.raw_output,meeting)
    run.parsed_output_json=result
    _finish_step(step,run,result)
    _apply_router_result(meeting,result,target_round)
    return result

def _commit_contribution_run(meeting,step,run,employee,round_number):
    shape=step.contribution_shape
    if shape=="ROUND1_FIRST_SPEAKER":
        validation=_validate_round1(run.raw_output,meeting); message_type="POSITION"
    elif shape=="ROUND1_RESPONSE":
        validation=_validate_response(run.raw_output,meeting); message_type=validation.data["relation"]
    elif shape=="LATER_ROUND_RESPONSE":
        validation=_validate_later(run.raw_output,meeting,round_number); message_type=validation.data["type"]
    else:
        raise ValueError("Meeting contribution shape is missing; paid response cannot be materialized safely")
    result=validation.data
    run.structured_validation_status=validation.status
    run.structured_validation_warnings_json=validation.warnings
    existing=MeetingMessage.query.filter_by(
      meeting_id=meeting.id,agent_run_id=run.id,speaker_type="EMPLOYEE"
    ).first()
    if existing is None:
        db.session.add(MeetingMessage(
          meeting_id=meeting.id,employee_id=employee.id,speaker_type="EMPLOYEE",
          round_number=round_number,message_type=message_type,
          content=json.dumps(result,separators=(",",":")),agent_run_id=run.id,
          validation_status=validation.status,validation_warnings_json=validation.warnings))
        db.session.flush()
    meeting.current_summary_json=_compact_live_brief(meeting)
    _finish_step(step,run,result)
    return result

def _validate_synthesis_payload(raw_output):
    synthesis=json.loads(raw_output); expected=set(MEETING_SYNTHESIS_FIELDS)
    if not isinstance(synthesis,dict) or set(synthesis)!=expected or any(
      not isinstance(synthesis[x],list) or len(synthesis[x])>8 or
      any(not isinstance(item,str) or not item.strip() or len(item)>500 for item in synthesis[x])
      for x in expected):
        raise ValueError("Meeting synthesis validation failed")
    return synthesis

def _commit_synthesis_run(meeting,step,run):
    synthesis=_validate_synthesis_payload(run.raw_output)
    run.parsed_output_json=synthesis
    existing=MeetingMessage.query.filter_by(
      meeting_id=meeting.id,agent_run_id=run.id,message_type="CHAIR_SYNTHESIS"
    ).first()
    if existing is None:
        db.session.add(MeetingMessage(
          meeting_id=meeting.id,employee_id=meeting.chair.id,speaker_type="EMPLOYEE",
          round_number=meeting.current_round,message_type="CHAIR_SYNTHESIS",
          content=json.dumps(synthesis,separators=(",",":")),agent_run_id=run.id))
    tokens,cost=usage(meeting)
    interventions=MeetingMessage.query.filter_by(
      meeting_id=meeting.id,message_type="FOUNDER_INTERVENTION").all()
    meeting.minutes_json={
      "purpose":meeting.purpose,"participants":[p.employee.name for p in meeting.participants],
      **synthesis,"founder_interventions":[m.content for m in interventions],
      "token_usage":tokens,"real_cost_twd":str(cost),
      "provider_calls":confirmed_provider_calls(meeting),
      "provider_call_breakdown":call_breakdown(meeting)}
    meeting_kernel.transition(
      meeting,"COMPLETED","MEETING_COMPLETED",stage="COMPLETED",legacy_status="ENDED",commit=False)
    meeting.current_summary_json=_compact_live_brief(meeting)
    meeting.current_summary_json=(meeting.current_summary_json or {})|{"next_step":"Meeting complete."}
    _finish_step(step,run,synthesis)
    return synthesis

def recover_interrupted_step(step):
    """Materialize an already-paid Meeting provider result after restart.

    Returns ``True`` only when a durable SUCCEEDED AgentRun was consumed.  It
    never invokes a provider and never guesses an ambiguous dispatch outcome.
    """
    if step.status!="RUNNING":
        return False
    run=_run_bound_to_step(step)
    if run is None or run.status!="SUCCEEDED" or not run.raw_output:
        return False
    meeting=db.session.get(Meeting,step.meeting_id)
    if meeting is None:
        return False
    try:
        if step.kind=="CHAIR_ROUTER":
            _commit_router_run(meeting,step,run,step.round_number)
        elif step.kind=="CONTRIBUTION":
            employee=db.session.get(Employee,step.employee_id)
            if employee is None:
                raise ValueError("Meeting contribution Employee no longer exists")
            _commit_contribution_run(meeting,step,run,employee,step.round_number)
        elif step.kind=="FINAL_SYNTHESIS":
            _commit_synthesis_run(meeting,step,run)
        else:
            return False
    except Exception as exc:
        # The provider result is already durable.  Local validation failure is
        # classified using the same no-blind-replay policy as live execution.
        _handle_step_failure(meeting,step,run,exc)
        return True
    return True

def confirmed_provider_calls(meeting):
    runs=AgentRun.query.filter_by(meeting_id=meeting.id).all()
    return sum(bool(run.provider_response_id
      or run.input_tokens is not None or run.output_tokens is not None
      or (run.real_cost is not None and Decimal(run.real_cost)>0)) for run in runs)

def call_breakdown(meeting):
    runs=[run for run in AgentRun.query.filter_by(meeting_id=meeting.id).order_by(AgentRun.id).all()
      if bool(run.provider_response_id or run.input_tokens is not None or run.output_tokens is not None
        or (run.real_cost is not None and Decimal(run.real_cost)>0))]
    return {
      "total":len(runs),
      "successful":sum(run.status=="SUCCEEDED" for run in runs),
      "failed":sum(run.status=="FAILED" for run in runs),
      "retries":sum(bool(run.retry_of_run_id) for run in runs),
      "truncated":sum(run.failure_reason=="OUTPUT_TRUNCATED" for run in runs),
    }

def _fail_step(meeting,step,run,error):
    step.status="FAILED"; step.agent_run_id=getattr(run,"id",None)
    step.error_text=str(error); step.finished_at=now()
    meeting_kernel.transition(meeting,"WAITING_FOR_INPUTS","MEETING_STEP_FAILED",stage="WAITING_FOR_INPUTS",
      legacy_status="PAUSED",commit=False,payload={"step_id":step.id,"error":str(error)})
    meeting.last_blocked_json={"local_failure":True,"reason":str(error),
      "run_id":getattr(run,"id",None),
      "message":"Meeting paused before a provider response; no provider cost was recorded."}
    db.session.commit()

def _billable(run):
    return bool(run and run.real_cost is not None and Decimal(run.real_cost)>0
      and CostEvent.query.filter_by(agent_run_id=run.id).count()==1)

def _handle_step_failure(meeting,step,run,error):
    """Classify a Meeting step failure without ever blind-replaying ambiguity."""
    ambiguous=bool(run and (
      run.outcome=="FAILED_AMBIGUOUS" or
      run.failure_reason in {"PROVIDER_DISPATCH_AMBIGUOUS","COST_RECONCILIATION_REQUIRED",
                             "PROCESS_RESTART_AFTER_DISPATCH"}))
    if ambiguous:
        step.status="AMBIGUOUS"; step.agent_run_id=getattr(run,"id",None)
        step.error_text=str(error); step.finished_at=now(); db.session.commit()
        close_for_internal_recovery(
          meeting,
          "Meeting provider dispatch outcome is unresolved; automatic replay is forbidden so the Project must recover without buying the same unknown effect again.",
        )
        return
    if _billable(run):
        _paid_fail_step(meeting,step,run,error)
    else:
        _fail_step(meeting,step,run,error)

def _paid_fail_step(meeting,step,run,error):
    if run.status=="SUCCEEDED":
        run.status="FAILED"
        run.failure_reason="STRUCTURED_OUTPUT_INVALID"
        run.error_text=f"STRUCTURED_OUTPUT_INVALID: {error}"
    if isinstance(error,StructuredValidationError):
        run.structured_validation_status="INVALID"
        run.structured_validation_errors_json=error.errors
    reason=run.failure_reason or "PAID_PROVIDER_FAILURE"
    step.status="PAID_FAILED"; step.agent_run_id=run.id
    step.error_text=str(error); step.finished_at=now()
    meeting_kernel.transition(meeting,"WAITING_FOR_INPUTS","MEETING_PAID_STEP_FAILED",stage="WAITING_FOR_INPUTS",
      legacy_status="PAUSED",commit=False,payload={"step_id":step.id,"run_id":run.id,"error":str(error)})
    historical_model=SimpleNamespace(
      input_price_per_million=run.input_price_snapshot,
      output_price_per_million=run.output_price_snapshot,
      max_output_tokens=run.effective_max_output_tokens)
    repair_mode=(step.kind=="CONTRIBUTION" and reason in {"OUTPUT_TRUNCATED","STRUCTURED_OUTPUT_INVALID"})
    retry_output_cap=min(run.effective_max_output_tokens or meeting.contribution_output_cap,
      meeting.contribution_output_cap,384) if repair_mode else run.effective_max_output_tokens
    retry_estimate=estimate_execution(historical_model,run.system_prompt_snapshot,
      run.context_snapshot,run.user_request,retry_output_cap,
      run.response_schema_snapshot_json)
    meeting.paid_failure_json={
      "reason":reason,"message":str(error),"step_id":step.id,"run_id":run.id,
      "employee":run.employee.name,"provider":run.provider_key_snapshot,"model":run.model_name_snapshot,
      "input_tokens":run.input_tokens or 0,"output_tokens":run.output_tokens or 0,
      "cost_twd":str(run.real_cost),"provider_stop_reason":run.provider_stop_reason,
      "retry_estimate_twd":str(retry_estimate.real_cost),
      "retry_estimated_input_tokens":retry_estimate.input_tokens,
      "retry_output_cap":retry_output_cap,
      "retry_schema_included":run.response_schema_snapshot_json is not None,
      "retry_strategy":("COMPACT_REPAIR" if repair_mode else "REPLAY"),
      "retry_changes":("Shorter prompt, same exact schema, no repeated background, and an explicit retry link to this run."
        if step.kind=="CONTRIBUTION" else "The same bounded step may be replayed once within the approved Meeting recovery envelope.")}
    db.session.commit()

def retry_paid_step(meeting, actor_type="RUNTIME"):
    _assert_project_meeting_mutable(meeting, action="RETRY_PAID_STEP")
    failure=meeting.paid_failure_json or {}
    step_id=failure.get("step_id")
    step=db.session.get(MeetingStep,step_id) if step_id is not None else None
    if meeting.status!="PAUSED" or not step or step.meeting_id!=meeting.id or step.status!="PAID_FAILED":
        raise ValueError("Meeting has no paid failed step eligible for explicit retry")
    claimed=db.session.execute(update(MeetingStep).where(
      MeetingStep.id==step.id,MeetingStep.status=="PAID_FAILED").values(status="FAILED")).rowcount
    if claimed!=1:
        db.session.rollback(); raise ValueError("Paid failed step was already handled")
    failed_run=db.session.get(AgentRun,failure.get("run_id")) if failure.get("run_id") else None
    retry_mode=("COMPACT_REPAIR" if step.kind=="CONTRIBUTION" and failed_run and
      (failed_run.failure_reason in {"OUTPUT_TRUNCATED","STRUCTURED_OUTPUT_INVALID"} or
       failed_run.provider_stop_reason in {"max_tokens","max_output_tokens"}) else "REPLAY")
    step.result_json={"retry_mode":retry_mode,"retry_of_run_id":getattr(failed_run,"id",None)}
    meeting.paid_failure_json=None
    meeting_kernel.transition(meeting,"ACTIVE","MEETING_RETRY_AUTHORIZED",stage="ACTIVE",legacy_status="RUNNING",
      actor_type=actor_type,commit=False,payload={"step_id":step.id})
    db.session.commit(); return meeting

def close_for_internal_recovery(meeting, reason):
    """Close a failed Work-scoped Meeting without inventing Founder authority.

    The related Work returns to management recovery.  This Meeting remains an
    auditable failed coordination attempt and is consumable by the Operation
    timeline, but no Founder gate is opened merely because its retry allowance
    was exhausted.
    """
    reason=(reason or "Meeting recovery requires an internal Project replan.").strip()
    tokens,cost=usage(meeting)
    breakdown=call_breakdown(meeting)
    failure=dict(meeting.paid_failure_json or {})
    meeting.minutes_json={
      "agreements":[],"disagreements":[reason],"evidence_referenced":[],
      "actions":["Return the related Work to internal management recovery."],
      "founder_decisions_required":[],"rejected_or_unresolved":[reason],
      "positions":[],"responses":[],"token_usage":tokens,"real_cost_twd":str(cost),
      "provider_calls":confirmed_provider_calls(meeting),
      "provider_call_breakdown":breakdown,
      "recovery":{"kind":"INTERNAL_REPLAN_REQUIRED","failure":failure},
    }
    meeting.termination_reason=reason
    meeting.current_summary_json={
      "current_topic":meeting.agenda,
      "agreement":"No reliable Meeting outcome was committed.",
      "key_disagreement":reason,
      "evidence":"Persisted Meeting execution history remains available.",
      "open_question":"Project management must choose a bounded internal recovery strategy.",
      "next_step":"Return the related Work to internal management recovery.",
    }
    meeting.paid_failure_json=None
    meeting_kernel.transition(
      meeting,"CANCELLED","MEETING_INTERNAL_RECOVERY_REQUIRED",stage="CANCELLED",
      legacy_status="ENDED",actor_type="RUNTIME",commit=False,payload={"reason":reason},
    )
    db.session.add(MeetingMessage(
      meeting_id=meeting.id,speaker_type="SYSTEM",round_number=meeting.current_round,
      message_type="SYSTEM",content="Meeting closed for internal recovery: "+reason,
    ))
    db.session.commit(); return meeting


def _revalidate_failed_contribution(meeting,step,run):
    if step.kind!="CONTRIBUTION" or not run or not run.raw_output:
        raise ValueError("Existing response is not available for local recovery")
    if run.failure_reason=="OUTPUT_TRUNCATED" or run.provider_stop_reason in {
      "max_tokens","max_output_tokens","model_context_window_exceeded"}:
        raise ValueError("Truncated responses cannot be locally recovered")
    if run.failure_reason!="STRUCTURED_OUTPUT_INVALID":
        raise ValueError("Original failure is not eligible for safe structured-output recovery")
    schema_name=(run.response_schema_snapshot_json or {}).get("name")
    shape=step.contribution_shape or {
      "meeting_round1_contribution":"ROUND1_FIRST_SPEAKER",
      "meeting_same_round_response":"ROUND1_RESPONSE",
      "meeting_later_contribution":"LATER_ROUND_RESPONSE",
    }.get(schema_name)
    if not shape:
        data=_json_object(run.raw_output)
        keys=set(data)
        if keys=={"position","actions","risk","evidence_ids","confidence"}: shape="ROUND1_FIRST_SPEAKER"
        elif keys=={"relation","core_point","controls","risk","evidence_ids"}: shape="ROUND1_RESPONSE"
        elif keys=={"has_material_contribution","type","core_point","controls","risk","evidence_ids","target_message_ids"}: shape="LATER_ROUND_RESPONSE"
        else: raise ValueError("Historical contribution shape cannot be determined safely")
    if shape=="ROUND1_FIRST_SPEAKER": return _validate_round1(run.raw_output,meeting),shape
    if shape=="ROUND1_RESPONSE": return _validate_response(run.raw_output,meeting),shape
    if shape=="LATER_ROUND_RESPONSE": return _validate_later(run.raw_output,meeting,step.round_number),shape
    raise ValueError("Historical operation is not eligible for contribution recovery")

def salvage_preview(meeting):
    failure=meeting.paid_failure_json or {}
    step=db.session.get(MeetingStep,failure.get("step_id")) if failure.get("step_id") else None
    run=db.session.get(AgentRun,failure.get("run_id")) if failure.get("run_id") else None
    if meeting.status not in {"PAUSED","TERMINATED_BY_FOUNDER"} or not step or step.status!="PAID_FAILED": return None
    try:
        validation,_=_revalidate_failed_contribution(meeting,step,run)
        if validation.status not in {"VALID","VALID_WITH_WARNINGS"}: return None
        return {"eligible":True,"warning_count":len(validation.warnings)}
    except Exception:
        return None

def accept_existing_response(meeting):
    meeting=db.session.get(Meeting,meeting.id)
    terminal=meeting.status=="TERMINATED_BY_FOUNDER"
    failure=meeting.paid_failure_json or {}
    step=db.session.get(MeetingStep,failure.get("step_id")) if failure.get("step_id") else None
    run=db.session.get(AgentRun,failure.get("run_id")) if failure.get("run_id") else None
    if meeting.status not in {"PAUSED","TERMINATED_BY_FOUNDER"} or not step or step.meeting_id!=meeting.id or step.status!="PAID_FAILED":
        raise ValueError("Meeting has no paid failed step eligible for local recovery")
    validation,shape=_revalidate_failed_contribution(meeting,step,run)
    if validation.status not in {"VALID","VALID_WITH_WARNINGS"}:
        raise ValueError("Existing response does not pass current hard validation")
    result=validation.data
    message_type=("POSITION" if shape=="ROUND1_FIRST_SPEAKER" else
      (result["relation"] if shape=="ROUND1_RESPONSE" else result["type"]))
    claimed=db.session.execute(update(MeetingStep).where(
      MeetingStep.id==step.id,MeetingStep.status=="PAID_FAILED").values(
      status="SUCCEEDED",result_json={"recovered_existing_response":True,
        "validation_status":validation.status,"warnings":validation.warnings,**result},
      error_text=None,finished_at=now())).rowcount
    if claimed!=1:
        db.session.rollback(); raise ValueError("Paid failed step was already handled")
    db.session.add(MeetingMessage(meeting_id=meeting.id,employee_id=step.employee_id,
      speaker_type="EMPLOYEE",round_number=step.round_number,message_type=message_type,
      content=json.dumps(result,separators=(",",":")),agent_run_id=run.id,
      validation_status=validation.status,validation_warnings_json=validation.warnings))
    db.session.add(MeetingStep(meeting_id=meeting.id,logical_key=f"LOCAL_RECOVERY_{step.id}",
      kind="LOCAL_RECOVERY",round_number=step.round_number,employee_id=step.employee_id,
      status="SUCCEEDED",agent_run_id=run.id,result_json={"action":"FOUNDER_ACCEPT_EXISTING_RESPONSE",
        "provider_calls":0,"additional_cost_twd":"0","validation_status":validation.status,
        "warning_count":len(validation.warnings)},finished_at=now()))
    meeting.paid_failure_json=None
    if not terminal:
        meeting_kernel.transition(meeting,"ACTIVE","MEETING_LOCAL_RECOVERY_ACCEPTED",stage="ACTIVE",
          legacy_status="RUNNING",actor_type="FOUNDER",commit=False,payload={"run_id":run.id})
    db.session.flush(); meeting.current_summary_json=_compact_live_brief(meeting)
    if terminal:
        meeting.current_summary_json=(meeting.current_summary_json or {})|{
          "next_step":"Meeting remains terminated. Recovered result is available for review."}
        meeting.minutes_json=_deterministic_minutes(meeting,{
          "recovered":True,"recovery_kind":"LOCAL","additional_provider_calls":0,
          "additional_cost_twd":"0","original_status":"TERMINATED_BY_FOUNDER",
          "provider":run.provider_key_snapshot,"model":run.model_name_snapshot,
          "recovered_run_id":run.id,"validation_status":validation.status,
          "warning_count":len(validation.warnings),"recovered_at":now().isoformat()})
    db.session.commit(); return meeting

def _validate_evidence_ids(meeting,ids):
    visible={item.id for item in current_knowledge(meeting.project_id)}
    if any(ident not in visible for ident in ids):
        _invalid("Contribution references nonexistent, historical, or invisible evidence","evidence_ids")

def _validate_target_message_ids(meeting,ids,round_number):
    if not ids: return
    rows=MeetingMessage.query.filter(MeetingMessage.id.in_(ids)).all()
    eligible={row.id for row in rows if row.meeting_id==meeting.id and row.speaker_type=="EMPLOYEE"
      and row.agent_run_id is not None and row.round_number<=round_number}
    if set(ids)!=eligible: _invalid("Contribution targets an ineligible or cross-Meeting message","target_message_ids")

def _json_object(text):
    try: data=json.loads(text)
    except Exception as exc: _invalid(f"Malformed structured JSON: {exc}")
    if not isinstance(data,dict): _invalid("Structured output must be an object")
    return data

def _text_field(data,field,hard_limit,required=True):
    value=data.get(field)
    if not isinstance(value,str) or (required and not value.strip()): _invalid(f"Invalid {field}",field)
    if len(value)>hard_limit: _invalid(f"{field} exceeds hard acceptance ceiling",field)
    return value

def _tier(data,warnings):
    return StructuredValidation("VALID_WITH_WARNINGS" if warnings else "VALID",data,warnings)

def _validate_round1(text,meeting):
    data=_json_object(text); expected={"position","actions","risk","evidence_ids","confidence"}
    if set(data)!=expected: _invalid("Invalid Round 1 contribution fields")
    warnings=[]
    position=_text_field(data,"position",HARD_LIMIT["position"])
    risk=_text_field(data,"risk",HARD_LIMIT["risk"])
    if len(position)>COMPACT_TARGET["position"]: warnings.append(_compact_warning("position",180,len(position)))
    if len(risk)>COMPACT_TARGET["risk"]: warnings.append(_compact_warning("risk",180,len(risk)))
    if not isinstance(data["actions"],list) or len(data["actions"])>3: _invalid("Invalid compact actions","actions")
    for index,item in enumerate(data["actions"]):
        if not isinstance(item,str) or not item.strip(): _invalid("Invalid compact action",f"actions[{index}]")
        if len(item)>HARD_LIMIT["action"]: _invalid("Action exceeds hard acceptance ceiling",f"actions[{index}]")
        if len(item)>COMPACT_TARGET["action"]: warnings.append(_compact_warning(f"actions[{index}]",120,len(item)))
    if not isinstance(data["evidence_ids"],list) or len(data["evidence_ids"])>8 or any(type(x) is not int for x in data["evidence_ids"]): _invalid("Invalid evidence IDs","evidence_ids")
    _validate_evidence_ids(meeting,data["evidence_ids"])
    if isinstance(data["confidence"],bool) or not isinstance(data["confidence"],(int,float)) or not 0<=data["confidence"]<=1: _invalid("Invalid confidence","confidence")
    return _tier(data,warnings)

def _validate_response(text,meeting):
    data=_json_object(text); expected={"relation","core_point","controls","risk","evidence_ids"}
    if set(data)!=expected: _invalid("Invalid same-round response fields")
    if data["relation"] not in {"AGREE","DISAGREE","ADD_INFORMATION","QUALIFY"}: _invalid("Invalid response relation","relation")
    warnings=[]
    core=_text_field(data,"core_point",HARD_LIMIT["core_point"])
    risk=_text_field(data,"risk",HARD_LIMIT["risk"],required=False)
    if len(core)>COMPACT_TARGET["core_point"]: warnings.append(_compact_warning("core_point",180,len(core)))
    if len(risk)>COMPACT_TARGET["risk"]: warnings.append(_compact_warning("risk",180,len(risk)))
    if not isinstance(data["controls"],list) or len(data["controls"])>3: _invalid("Invalid response controls","controls")
    for index,item in enumerate(data["controls"]):
        if not isinstance(item,str) or not item.strip(): _invalid("Invalid response control",f"controls[{index}]")
        if len(item)>HARD_LIMIT["control"]: _invalid("Control exceeds hard acceptance ceiling",f"controls[{index}]")
        if len(item)>COMPACT_TARGET["control"]: warnings.append(_compact_warning(f"controls[{index}]",120,len(item)))
    if not isinstance(data["evidence_ids"],list) or len(data["evidence_ids"])>8 or any(type(x) is not int for x in data["evidence_ids"]): _invalid("Invalid evidence IDs","evidence_ids")
    _validate_evidence_ids(meeting,data["evidence_ids"])
    return _tier(data,warnings)

def _validate_later(text,meeting,round_number):
    data=_json_object(text); expected={"has_material_contribution","type","core_point","controls","risk","evidence_ids","target_message_ids"}
    allowed={"DISAGREEMENT","COUNTEREVIDENCE","NEW_INFORMATION","MATERIAL_REFINEMENT"}
    if set(data)!=expected or type(data["has_material_contribution"]) is not bool: _invalid("Invalid later contribution fields")
    if data["type"] not in allowed: _invalid("Invalid later contribution type","type")
    warnings=[]
    core=_text_field(data,"core_point",HARD_LIMIT["core_point"],required=False)
    risk=_text_field(data,"risk",HARD_LIMIT["risk"],required=False)
    if len(core)>180: warnings.append(_compact_warning("core_point",180,len(core)))
    if len(risk)>180: warnings.append(_compact_warning("risk",180,len(risk)))
    if not isinstance(data["controls"],list) or len(data["controls"])>3: _invalid("Invalid later controls","controls")
    for index,item in enumerate(data["controls"]):
        if not isinstance(item,str) or not item.strip(): _invalid("Invalid later control",f"controls[{index}]")
        if len(item)>400: _invalid("Control exceeds hard acceptance ceiling",f"controls[{index}]")
        if len(item)>120: warnings.append(_compact_warning(f"controls[{index}]",120,len(item)))
    for field in ("evidence_ids","target_message_ids"):
        if not isinstance(data[field],list) or len(data[field])>8 or any(type(x) is not int for x in data[field]): _invalid(f"Invalid {field}",field)
    _validate_evidence_ids(meeting,data["evidence_ids"])
    _validate_target_message_ids(meeting,data["target_message_ids"],round_number)
    return _tier(data,warnings)

def _parse_round1(text,meeting): return _validate_round1(text,meeting).data
def _parse_response(text,meeting): return _validate_response(text,meeting).data
def _parse_later(text,meeting,round_number): return _validate_later(text,meeting,round_number).data

def _parse_router(text,meeting):
    data=json.loads(text); expected={"continue_meeting","next_speakers","reason","founder_input_required","founder_question"}
    if not isinstance(data,dict) or set(data)!=expected: raise ValueError("Invalid chair router fields")
    if type(data["continue_meeting"]) is not bool or type(data["founder_input_required"]) is not bool: raise ValueError("Invalid chair router booleans")
    if not isinstance(data["reason"],str) or len(data["reason"])>240: raise ValueError("Invalid chair router reason")
    if data["founder_question"] is not None and (not isinstance(data["founder_question"],str) or len(data["founder_question"])>240): raise ValueError("Invalid Founder question")
    if data["founder_input_required"] and (
      not isinstance(data["founder_question"],str) or not data["founder_question"].strip()
      or data["continue_meeting"]):
        raise ValueError("Founder-input routing requires a question and a wait")
    if not data["founder_input_required"] and data["founder_question"] is not None:
        raise ValueError("Founder question is only valid when Founder input is required")
    invited={employee.slug for employee in _active_speakers(meeting)}
    speakers=data["next_speakers"]
    if not isinstance(speakers,list) or len(speakers)>meeting.max_speakers_per_round or len(set(speakers))!=len(speakers):
        raise ValueError("Invalid chair router speaker count")
    if any(slug not in invited for slug in speakers): raise ValueError("Chair router selected an invalid Employee slug")
    return data

def _meeting_context(meeting,employee,target_round=None):
    return _context_packet(meeting,employee,target_round)

def _contribution_protocol(marker):
    if marker=="MEETING_CONTRIBUTION_ROUND1":
        return ("Position/risk <=180 chars; <=3 actions, each <=120 chars. evidence_ids may use only listed "
          "EVIDENCE_ID values; use [] when none.")
    if marker=="MEETING_CONTRIBUTION_RESPONSE":
        return ("Use an allowed relation; core_point/risk <=180 chars; <=3 controls, each <=120 chars. "
          "Use only listed EVIDENCE_ID values (or []); react only to material prior contributions; do not restate background.")
    return ("core_point/risk <=180 chars; <=3 controls, each <=120 chars. Use only listed EVIDENCE_ID values "
      "(or []); target only listed MEETING_MESSAGE_ID values.")

def _compile_contribution_prompt(employee,marker,round_number,repair=False):
    version={
      "MEETING_CONTRIBUTION_ROUND1":PROMPT_VERSIONS["round1"],
      "MEETING_CONTRIBUTION_RESPONSE":PROMPT_VERSIONS["response"],
      "MEETING_CONTRIBUTION_LATER":PROMPT_VERSIONS["later"],
    }[marker]
    if repair:
        version=PROMPT_VERSIONS["repair"]
    task={
      "MEETING_CONTRIBUTION_ROUND1":(
        "State one decision-relevant position, up to three concrete actions, one risk, "
        "and only evidence IDs that exist in the packet."),
      "MEETING_CONTRIBUTION_RESPONSE":(
        "Respond to the prior validated contribution. Choose AGREE, DISAGREE, "
        "ADD_INFORMATION, or QUALIFY. Add only a new point or control."),
      "MEETING_CONTRIBUTION_LATER":(
        "Resolve one remaining disagreement or add genuinely new evidence. Set "
        "has_material_contribution=false when nothing useful remains."),
    }[marker]
    recovery=(
      "\nRECOVERY MODE\nThe previous paid attempt did not produce a usable complete response. "
      "Use the shortest valid wording possible. Do not reconstruct or repeat the previous essay."
      if repair else "")
    prompt=(f"{employee.system_instructions}\n\n"
      f"PROMPT VERSION\n{version}\n\n"
      f"ROLE\nYou are {employee.name}, acting only within this Employee role.\n\n"
      f"CURRENT TURN\nRound {round_number}. {task}\n\n"
      f"OUTPUT CONTRACT\n{marker}\n{_contribution_protocol(marker)}\n"
      "Return one complete JSON object matching the supplied schema.\n\n"
      "COMPLETION STANDARD\nEvery required field is present, concise, and grounded in the Meeting packet.\n\n"
      "PROHIBITED\nNo essay. No chain-of-thought. No restated background. No invented evidence IDs. "
      "No company-state mutation. No text outside JSON."
      f"{recovery}")
    return prompt,version

def _apply_router_result(meeting,result,target_round):
    if result["founder_input_required"]:
        meeting_kernel.transition(meeting,"WAITING_FOR_INPUTS","MEETING_FOUNDER_INPUT_REQUIRED",
          stage="WAITING_FOR_INPUTS",legacy_status="WAITING_FOR_FOUNDER",commit=False,
          payload={"question":result["founder_question"]})
        latest=MeetingMessage.query.filter_by(meeting_id=meeting.id,message_type="FOUNDER_INTERVENTION").order_by(MeetingMessage.id.desc()).first()
        meeting.routing_json={"waiting_for_founder":True,
          "founder_message_id_at_wait":getattr(latest,"id",0),"question":result["founder_question"]}
        meeting.current_summary_json=(meeting.current_summary_json or {})|{"founder_decision":result["founder_question"]}
    elif not result["continue_meeting"] or not result["next_speakers"]:
        meeting.routing_json=({"ready_for_economy_close":True,"reason":result["reason"]}
          if meeting.execution_profile=="ECONOMY" else {"ready_for_synthesis":True,"reason":result["reason"]})
    else:
        meeting.routing_json={"round":target_round,"speakers":result["next_speakers"],"reason":result["reason"]}
    db.session.commit()

def _route_step(meeting,founder_message_id=None):
    target_round=meeting.current_round+1
    phase="BEFORE" if meeting.current_round==0 else "AFTER"
    key=(f"ROUTER_AFTER_FOUNDER_{founder_message_id}_ROUND_{meeting.current_round}" if founder_message_id
      else f"ROUTER_{phase}_ROUND_{meeting.current_round if meeting.current_round else 1}")
    step,execute_now=_reserve_step(meeting,key,"CHAIR_ROUTER",target_round,meeting.chair)
    if not execute_now:
        if step.status=="SUCCEEDED" and step.result_json:
            _apply_router_result(meeting,step.result_json,target_round)
        return auto_state(meeting)
    context,composition=_meeting_context(meeting,meeting.chair)
    composition=_step_composition(composition,step)
    prompt=(meeting.chair.system_instructions+"\n\nPROMPT VERSION\n"+PROMPT_VERSIONS["router"]+
      "\n\nCHAIR_ROUTER\nUse the structured Meeting state and validated message IDs. Select only Employees "
      "who can resolve an open issue. Stop when there is no material conflict or when the Artifact is ready. "
      "Do not restate the transcript and do not mutate company state. Return only the router JSON schema.")
    request=(f"Re-evaluate after Founder intervention Message #{founder_message_id}" if founder_message_id else
      (f"Select speakers for round {target_round}" if meeting.current_round==0 else
       f"Route after round {meeting.current_round}; stop early when recommendation is ready."))
    run=None
    try:
        model=_check_step(meeting,meeting.chair,prompt,context,request,MEETING_ROUTER_SCHEMA,
          max_output_tokens_override=meeting.router_output_cap)
        run=execute(meeting.chair,"CHAIR_ROUTER",request,meeting.project,context_override=context,
          system_prompt_override=prompt,response_schema=MEETING_ROUTER_SCHEMA,meeting=meeting,
          operation=meeting.operation,work=_governing_work(meeting),model_override=model,
          max_output_tokens_override=min(meeting.router_output_cap,model.max_output_tokens),
          context_composition=composition,prompt_version=PROMPT_VERSIONS["router"])
        if run.status!="SUCCEEDED": raise ValueError(run.error_text or "Chair routing failed")
        _commit_router_run(meeting,step,run,target_round)
        return auto_state(meeting)
    except Exception as exc:
        _handle_step_failure(meeting,step,run,exc)
        raise

def _contribution_step(meeting,employee,round_number):
    key=f"ROUND_{round_number}_{employee.slug.upper().replace('-','_')}_CONTRIBUTION"
    step,execute_now=_reserve_step(meeting,key,"CONTRIBUTION",round_number,employee)
    if not execute_now: return auto_state(meeting)
    prior=MeetingMessage.query.filter_by(meeting_id=meeting.id,round_number=round_number,
      speaker_type="EMPLOYEE").count()
    if round_number==1 and prior==0:
        schema=MEETING_ROUND1_SCHEMA; marker="MEETING_CONTRIBUTION_ROUND1"; shape="ROUND1_FIRST_SPEAKER"
    elif round_number==1:
        schema=MEETING_RESPONSE_SCHEMA; marker="MEETING_CONTRIBUTION_RESPONSE"; shape="ROUND1_RESPONSE"
    else:
        schema=MEETING_LATER_SCHEMA; marker="MEETING_CONTRIBUTION_LATER"; shape="LATER_ROUND_RESPONSE"
    if not step.contribution_shape:
        step.contribution_shape=shape; db.session.commit()
    retry_meta=step.result_json if isinstance(step.result_json,dict) else {}
    retry_of=db.session.get(AgentRun,retry_meta.get("retry_of_run_id")) if retry_meta.get("retry_of_run_id") else None
    repair=bool(retry_of and retry_meta.get("retry_mode")=="COMPACT_REPAIR")
    prompt,prompt_version=_compile_contribution_prompt(employee,marker,round_number,repair=repair)
    request=(f"Repair the failed contribution for round {round_number} using the compact contract"
      if repair else f"Provide one bounded material contribution for round {round_number}")
    context,composition=_meeting_context(meeting,employee,round_number)
    if repair:
        context += ("\n\nRETRY FACTS\n"
          f"PREVIOUS_RUN_ID: {retry_of.id}\n"
          f"PREVIOUS_FAILURE: {retry_of.failure_reason or retry_of.provider_stop_reason or 'INVALID_OUTPUT'}\n"
          "Do not repeat the failed output. Produce a fresh, shorter, complete JSON object.")
        composition=dict(composition)
        composition["compact_repair"]={"previous_run_id":retry_of.id,"mode":"COMPACT_REPAIR"}
    composition=_step_composition(composition,step)
    run=None
    try:
        output_cap=(min(meeting.contribution_output_cap,384) if repair else meeting.contribution_output_cap)
        model=_check_step(meeting,employee,prompt,context,request,schema,
          max_output_tokens_override=output_cap)
        run=execute(employee,"MEETING_CONTRIBUTION",request,meeting.project,context_override=context,
          system_prompt_override=prompt,response_schema=schema,meeting=meeting,
          operation=meeting.operation,work=_governing_work(meeting),model_override=model,
          max_output_tokens_override=min(output_cap,model.max_output_tokens),
          context_composition=composition,prompt_version=prompt_version,retry_of_run=retry_of)
        if run.status!="SUCCEEDED": raise ValueError(run.error_text or "Meeting contribution failed")
        _commit_contribution_run(meeting,step,run,employee,round_number)
        return auto_state(meeting)
    except Exception as exc:
        _handle_step_failure(meeting,step,run,exc)
        raise

def _compact_live_brief(meeting):
    rows=MeetingMessage.query.filter_by(meeting_id=meeting.id,speaker_type="EMPLOYEE").order_by(MeetingMessage.id.desc()).limit(8).all()
    parsed=[]
    for row in reversed(rows):
        try: parsed.append((row,json.loads(row.content)))
        except Exception: continue
    positions=[x["position"] for _,x in parsed if "position" in x]
    responses=[x for _,x in parsed if "relation" in x]
    later=[x for _,x in parsed if "has_material_contribution" in x and x["has_material_contribution"]]
    evidence=sorted({ident for _,x in parsed for ident in x.get("evidence_ids",[])})
    agrees=[x for x in responses if x["relation"]=="AGREE"]
    disagrees=[x for x in responses if x["relation"]=="DISAGREE"]
    counter=[x for x in later if x["type"] in {"DISAGREEMENT","COUNTEREVIDENCE"}]
    agreement=((agrees[-1].get("core_point") or agrees[-1].get("content",""))[:180] if agrees else "No explicit shared agreement established.")
    disagreement=(((disagrees[-1].get("core_point") or disagrees[-1].get("content","")) if disagrees
      else (counter[-1].get("core_point") or counter[-1].get("content","")))[:180]
      if disagrees or counter else "No explicit material disagreement presented.")
    gaps=[x.get("risk") for x in responses if x.get("risk")]
    qualifications=[x for x in responses if x["relation"]=="QUALIFY"]
    risks=[x.get("risk") for _,x in parsed if x.get("risk")]
    open_question=(gaps[-1] if gaps else (risks[-1] if risks else "No unresolved question identified."))
    return {"current_topic":meeting.agenda,
      "agreement":agreement,
      "key_disagreement":disagreement,
      "qualification":((qualifications[-1].get("core_point") or "Qualification recorded.")[:180]
        if qualifications else "No explicit qualification recorded."),
      "evidence":(", ".join(f"Knowledge #{x}" for x in evidence) if evidence else "No qualifying evidence presented."),
      "open_question":open_question[:180],
      "next_step":"Continue only for material information, otherwise synthesize.",
      "founder_decision":None}

def _auto_synthesis_step(meeting):
    key="FINAL_SYNTHESIS"; step,execute_now=_reserve_step(meeting,key,"FINAL_SYNTHESIS",meeting.current_round,meeting.chair)
    if not execute_now: return auto_state(meeting)
    context,composition=_meeting_context(meeting,meeting.chair)
    composition=_step_composition(composition,step)
    prompt=meeting.chair.system_instructions+"\nMEETING_SYNTHESIS\nReturn only the compact structured final synthesis."
    request="Produce final Meeting synthesis"; run=None
    meeting_kernel.transition(meeting,"SYNTHESIZING","MEETING_SYNTHESIS_STARTED",stage="SYNTHESIZING",
      legacy_status=ACTIVE,commit=False)
    db.session.commit()
    try:
        model=_check_step(meeting,meeting.chair,prompt,context,request,MEETING_SYNTHESIS_SCHEMA,allow_final=True,
          max_output_tokens_override=meeting.synthesis_output_cap)
        run=execute(meeting.chair,"MEETING_SYNTHESIS",request,meeting.project,context_override=context,
          system_prompt_override=prompt,response_schema=MEETING_SYNTHESIS_SCHEMA,meeting=meeting,
          operation=meeting.operation,work=_governing_work(meeting),model_override=model,
          max_output_tokens_override=min(meeting.synthesis_output_cap,model.max_output_tokens),
          context_composition=composition,prompt_version=PROMPT_VERSIONS["synthesis"])
        if run.status!="SUCCEEDED": raise ValueError(run.error_text or "Meeting synthesis failed")
        _commit_synthesis_run(meeting,step,run)
        return auto_state(meeting)
    except Exception as exc:
        _handle_step_failure(meeting,step,run,exc)
        raise

def _deterministic_minutes(meeting,recovery=None):
    rows=MeetingMessage.query.filter_by(meeting_id=meeting.id,speaker_type="EMPLOYEE").order_by(MeetingMessage.id).all()
    entries=[]
    for row in rows:
        try:
            data=json.loads(row.content)
            if any(key in data for key in ("position","relation","has_material_contribution")):
                entries.append((row,data))
        except Exception: continue
    speakers=list(dict.fromkeys(row.employee.name for row,_ in entries))
    positions=[{"speaker":row.employee.name,"position":data["position"],"actions":data["actions"],
      "risk":data["risk"],"evidence_ids":data["evidence_ids"],"confidence":data["confidence"],
      "validation_status":row.validation_status or "VALID",
      "warning_count":len(row.validation_warnings_json or [])}
      for row,data in entries if "position" in data]
    responses=[{"speaker":row.employee.name,"relation":data["relation"],"core_point":data["core_point"],
      "controls":data["controls"],"risk":data["risk"],"evidence_ids":data["evidence_ids"],
      "validation_status":row.validation_status or "VALID",
      "warning_count":len(row.validation_warnings_json or [])}
      for row,data in entries if "relation" in data]
    agrees=[item for item in responses if item["relation"]=="AGREE"]
    disagreements=[item for item in responses if item["relation"]=="DISAGREE"]
    later_conflicts=[{"speaker":row.employee.name,"type":data["type"],"core_point":data["core_point"]}
      for row,data in entries if data.get("has_material_contribution") and data.get("type") in {"DISAGREEMENT","COUNTEREVIDENCE"}]
    evidence=sorted({ident for _,data in entries for ident in data.get("evidence_ids",[])})
    gaps=[data.get("risk") for _,data in entries if data.get("risk")]
    interventions=MeetingMessage.query.filter_by(meeting_id=meeting.id,message_type="FOUNDER_INTERVENTION").all()
    tokens,cost=usage(meeting)
    unresolved=[item["core_point"] for item in disagreements]+[item["core_point"] for item in later_conflicts]
    minutes={"purpose":meeting.purpose,"participants_who_spoke":speakers,
      "positions":positions,"responses":responses,
      "agreement":[item["core_point"] for item in agrees],
      "material_disagreement":unresolved,"evidence_ids":evidence,"risk_or_gap":gaps,
      "founder_interventions":[row.content for row in interventions],
      "next_action_or_unresolved":(unresolved or ["Founder reviews the bounded recommendation."]),
      "token_usage":tokens,"real_cost_twd":str(cost),
      "provider_calls":confirmed_provider_calls(meeting),
      "provider_call_breakdown":call_breakdown(meeting)}
    if recovery: minutes["recovery"]=recovery
    return minutes

def _economy_close(meeting):
    step,close_now=_reserve_step(meeting,"DETERMINISTIC_ECONOMY_CLOSE","DETERMINISTIC_CLOSE",meeting.current_round)
    if not close_now: return auto_state(meeting)
    meeting.minutes_json=_deterministic_minutes(meeting)
    meeting.current_summary_json=_compact_live_brief(meeting)
    meeting.current_summary_json=(meeting.current_summary_json or {})|{"next_step":"Meeting complete."}
    meeting_kernel.transition(meeting,"COMPLETED","MEETING_COMPLETED",stage="COMPLETED",legacy_status="ENDED",commit=False)
    db.session.add(MeetingMessage(meeting_id=meeting.id,speaker_type="SYSTEM",round_number=meeting.current_round,
      message_type="SYSTEM",content="Economy Meeting closed deterministically from validated structured contributions."))
    _finish_step(step,result={"minutes":"deterministic","calls":confirmed_provider_calls(meeting)})
    return auto_state(meeting)

def next_step(meeting):
    _assert_project_meeting_mutable(meeting, action="NEXT_STEP")
    if meeting.status!="RUNNING": return auto_state(meeting)
    if meeting.routing_json and meeting.routing_json.get("founder_resume_message_id"):
        return _route_step(meeting,meeting.routing_json["founder_resume_message_id"])
    if meeting.routing_json and meeting.routing_json.get("ready_for_synthesis"):
        return _auto_synthesis_step(meeting)
    if meeting.routing_json and meeting.routing_json.get("ready_for_economy_close"):
        return _economy_close(meeting)
    target=meeting.current_round+1
    routing=meeting.routing_json
    active=_active_speakers(meeting)
    if not routing:
        if meeting.current_round==0 and len(active)<=2:
            routing={"round":1,"speakers":[e.slug for e in active[:meeting.max_speakers_per_round]]}
            meeting.routing_json=routing; db.session.commit()
        else:
            if meeting.current_round>=meeting.max_rounds:
                meeting.routing_json=({"ready_for_economy_close":True,"reason":"Maximum rounds reached."}
                  if meeting.execution_profile=="ECONOMY" else {"ready_for_synthesis":True,"reason":"Maximum rounds reached."}); db.session.commit()
                return auto_state(meeting)
            return _route_step(meeting)
    if routing.get("ready_for_synthesis"): return _auto_synthesis_step(meeting)
    if routing.get("ready_for_economy_close"): return _economy_close(meeting)
    round_number=routing["round"]
    for slug in routing["speakers"]:
        employee=next(e for e in active if e.slug==slug)
        done=MeetingMessage.query.filter(
          MeetingMessage.meeting_id==meeting.id,MeetingMessage.round_number==round_number,
          MeetingMessage.employee_id==employee.id,MeetingMessage.speaker_type=="EMPLOYEE").first()
        if not done: return _contribution_step(meeting,employee,round_number)
    meeting.current_round=round_number; meeting.current_summary_json=_compact_live_brief(meeting); meeting.routing_json=None
    if meeting.execution_profile=="ECONOMY":
        round_rows=MeetingMessage.query.filter_by(meeting_id=meeting.id,round_number=round_number,speaker_type="EMPLOYEE").all()
        conflict=False
        for row in round_rows:
            try:
                data=json.loads(row.content)
                conflict=conflict or data.get("relation")=="DISAGREE" or (
                  data.get("has_material_contribution") and data.get("type") in {"DISAGREEMENT","COUNTEREVIDENCE"})
            except Exception: pass
        if len(active)<=2:
            meeting.routing_json={"ready_for_economy_close":True,
              "reason":"Two-person Economy closes after bounded round-one contributions."}
        elif not conflict:
            meeting.routing_json={"ready_for_economy_close":True,"reason":"No explicit material conflict."}
    later=MeetingMessage.query.filter_by(meeting_id=meeting.id,round_number=round_number).all()
    if round_number>1:
        material=False
        for row in later:
            try: material=material or json.loads(row.content).get("has_material_contribution",False)
            except Exception: pass
        if not material: meeting.routing_json=({"ready_for_economy_close":True,"reason":"No material continuation."}
          if meeting.execution_profile=="ECONOMY" else {"ready_for_synthesis":True,"reason":"No material continuation."})
    if meeting.current_round>=meeting.max_rounds:
        meeting.routing_json=({"ready_for_economy_close":True,"reason":"Maximum rounds reached."}
          if meeting.execution_profile=="ECONOMY" else {"ready_for_synthesis":True,"reason":"Maximum rounds reached."})
    db.session.commit(); return auto_state(meeting)

def next_step_idempotent(meeting,request_key):
    _assert_project_meeting_mutable(meeting, action="NEXT_STEP_IDEMPOTENT")
    if not request_key or len(request_key)>120: raise ValueError("A bounded idempotency key is required")
    logical=f"HTTP_REQUEST_{request_key}"
    request_step=MeetingStep.query.filter_by(meeting_id=meeting.id,logical_key=logical).first()
    if request_step:
        return request_step.result_json or auto_state(meeting)
    request_step=MeetingStep(meeting_id=meeting.id,logical_key=logical,kind="HTTP_REQUEST",
      round_number=meeting.current_round,status="RUNNING")
    db.session.add(request_step)
    try: db.session.commit()
    except IntegrityError:
        db.session.rollback()
        existing=MeetingStep.query.filter_by(meeting_id=meeting.id,logical_key=logical).one()
        return existing.result_json or auto_state(meeting)
    try:
        result=next_step(meeting)
        request_step.status="SUCCEEDED"; request_step.result_json=result; request_step.finished_at=now()
        db.session.commit(); return result
    except Exception as exc:
        result=auto_state(meeting)|{"error":str(exc)}
        request_step.status="FAILED"; request_step.result_json=result; request_step.error_text=str(exc); request_step.finished_at=now()
        db.session.commit(); raise
