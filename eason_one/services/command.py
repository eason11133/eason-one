from datetime import datetime, time, timezone, timedelta
from decimal import Decimal
import json
from sqlalchemy import func
from ..extensions import db
from ..models import (AgentRun,CostEvent,Department,Employee,Meeting,MeetingParticipant,
  MeetingStep,Operation,Project,Proposal,Task,WorkMessage,HiringRequest,TalentTemplate)
from .company import get_company,spent,remaining
from .meetings import usage as meeting_usage

ACTIVE_PROJECT={"PLANNING","ACTIVE","BLOCKED","REVIEW"}
ACTIVE_TASK={"ASSIGNED","WORKING","REVIEW"}
ATTENTION_MEETING={"WAITING_FOR_FOUNDER"}
OPEN_MEETING={"RUNNING","PAUSED","WAITING_FOR_FOUNDER"}

def employee_status(employee):
    if not employee.active: return "DISABLED"
    in_meeting=(MeetingParticipant.query.join(Meeting).filter(
      MeetingParticipant.employee_id==employee.id,MeetingParticipant.removed_at.is_(None),
      Meeting.status=="RUNNING").first())
    if in_meeting: return "IN MEETING"
    task=(Task.query.join(Project).filter(Task.assigned_employee_id==employee.id,
      Project.environment=="LIVE",Task.status.in_(ACTIVE_TASK)).first())
    return "WORKING" if task else "AVAILABLE"

def employee_view(employee):
    tasks=(Task.query.join(Project).filter(Task.assigned_employee_id==employee.id,
      Project.environment=="LIVE",Task.status.in_(ACTIVE_TASK)).order_by(Task.updated_at.desc()).all())
    return {"employee":employee,"status":employee_status(employee),"current_work":tasks[0] if tasks else None}

def project_view(project):
    tasks=Task.query.filter_by(project_id=project.id).all()
    done=sum(task.status=="DONE" for task in tasks)
    active=[task for task in tasks if task.status in ACTIVE_TASK|{"BLOCKED"}]
    team=list({task.assigned_employee.id:task.assigned_employee for task in tasks if task.assigned_employee}.values())
    cost=Decimal(db.session.query(func.coalesce(func.sum(CostEvent.real_cost_delta),0)).filter_by(project_id=project.id).scalar())
    pending=Proposal.query.filter_by(project_id=project.id,status="PENDING").count()
    waiting=Meeting.query.filter_by(project_id=project.id,status="WAITING_FOR_FOUNDER").count()
    attention=bool(pending or waiting or any(task.status=="BLOCKED" for task in tasks))
    return {"project":project,"tasks":tasks,"done":done,"total":len(tasks),"active_tasks":active,
      "team":team,"cost":cost,"attention":attention}

def _brief(active,attention,recent_tasks,recent_meetings):
    if attention:
        item=attention[0]
        return {"eyebrow":"Founder decision required","title":item["title"],
          "summary":item["summary"],"recommendation":item["recommendation"],"watch":item.get("watch")}
    blocked=Task.query.join(Project).filter(Project.environment=="LIVE",Task.status=="BLOCKED").order_by(Task.updated_at.desc()).first()
    if blocked:
        return {"eyebrow":"Blocked work","title":blocked.project.name,"summary":blocked.title,
          "recommendation":"Review the blocker and decide whether to revise, continue, or stop.",
          "watch":blocked.result_summary}
    if active:
        project=active[0]["project"]
        return {"eyebrow":"Active work","title":project.name,
          "summary":project.current_state_summary or project.objective,
          "recommendation":project.next_milestone or "Review current work and authorize the next executable step.",
          "watch":project.known_constraints}
    if recent_tasks:
        task=recent_tasks[0]
        return {"eyebrow":"Recent result","title":task.project.name,"summary":task.result_summary or task.title,
          "recommendation":"Review the result and decide the next Company action.","watch":None}
    if recent_meetings:
        meeting=recent_meetings[0]
        return {"eyebrow":"Recent result","title":meeting.title,"summary":"Meeting result is ready for review.",
          "recommendation":"Review the deterministic Meeting result.","watch":None}
    return {"eyebrow":"CEO brief","title":"No active Company work.",
      "summary":"The Company is available.","recommendation":"What should we work on, Boss?","watch":None}

def _attention():
    items=[]
    for proposal in Proposal.query.filter_by(status="PENDING").order_by(Proposal.created_at.desc()).all():
        if proposal.project_id:
            scoped=db.session.get(Project,proposal.project_id)
            if scoped and scoped.environment!="LIVE":
                continue
        plan=(proposal.payload_json or {}).get("plan",{})
        project=plan.get("project") or {}
        items.append({"kind":"PROPOSAL","title":project.get("name") or "CEO proposed work",
          "summary":"CEO plan waiting for approval.","recommendation":"Review and approve or reject the plan.",
          "proposal":proposal,"href":f"/inbox#{proposal.id}","created_at":proposal.created_at})
    for task in Task.query.join(Project).filter(Project.environment=="LIVE",Task.status=="BLOCKED").order_by(Task.updated_at.desc()).all():
        if task.operation_id and task.operation and task.operation.status=="RUNNING":
            continue
        items.append({"kind":"BLOCKED","title":task.project.name,"summary":task.title,
          "recommendation":"Review blocked work.","href":f"/projects/{task.project_id}","created_at":task.updated_at})
    for meeting in Meeting.query.filter(Meeting.status.in_(["WAITING_FOR_FOUNDER","PAUSED"])).order_by(Meeting.updated_at.desc() if hasattr(Meeting,"updated_at") else Meeting.created_at.desc()).all():
        if meeting.status=="PAUSED" and not meeting.paid_failure_json: continue
        items.append({"kind":"MEETING","title":meeting.title,
          "summary":"Founder input required." if meeting.status=="WAITING_FOR_FOUNDER" else "Paid Meeting step requires a Founder decision.",
          "recommendation":"Open the Meeting.","href":f"/meetings/{meeting.id}","created_at":meeting.created_at})
    return sorted(items,key=lambda item:item["created_at"],reverse=True)

def _unresolved_founder_failure():
    return AgentRun.query.filter(
      AgentRun.purpose=="CEO_FOUNDER_REQUEST",
      AgentRun.status=="FAILED",
      AgentRun.resolution_status.is_(None),
      AgentRun.real_cost>0,
    ).order_by(AgentRun.started_at.desc()).first()

def unresolved_founder_failures():
    return AgentRun.query.filter(
      AgentRun.purpose=="CEO_FOUNDER_REQUEST",
      AgentRun.status=="FAILED",
      AgentRun.resolution_status.is_(None),
      AgentRun.real_cost>0,
    ).order_by(AgentRun.started_at.desc()).all()

def resolve_founder_failure(run, status, note=None, replacement=None):
    from ..models import now
    if run.purpose!="CEO_FOUNDER_REQUEST" or run.status!="FAILED":
        raise ValueError("Only a failed Founder request can be resolved")
    if run.resolution_status:
        raise ValueError("Founder request failure is already resolved")
    if status not in {"ACKNOWLEDGED","REPLACED"}:
        raise ValueError("Unknown Founder failure resolution")
    if status=="REPLACED" and (
      not replacement or replacement.status!="SUCCEEDED"
    ):
        raise ValueError("Replacement must succeed before resolution")
    run.resolution_status=status
    run.resolved_at=now()
    run.resolution_note=note
    run.replacement_run_id=getattr(replacement,"id",None)
    db.session.commit()
    return run

def _safe_partial_response(run):
    if not run or not run.raw_output:
        return None
    import re
    match=re.search(
      r'"executive_response"\s*:\s*"((?:[^"\\]|\\.)*)',
      run.raw_output,re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(f'"{match.group(1)}"')
    except Exception:
        return match.group(1).replace("\\n"," ").strip() or None

def _activity():
    rows=[]
    for run in AgentRun.query.filter_by(purpose="CEO_FOUNDER_REQUEST").order_by(AgentRun.started_at.desc()).limit(8):
        payload=run.parsed_output_json or {}
        mode=(payload.get("mode") or "").replace("_"," ").title()
        text=(f"CEO returned {mode.lower()} guidance" if run.status=="SUCCEEDED" and mode
          else "CEO response needs review" if run.status=="FAILED" else "CEO responded to the Founder")
        rows.append({"at":run.finished_at or run.started_at,"kind":"CEO","text":text,"href":f"/runs/{run.id}"})
    for proposal in Proposal.query.order_by(Proposal.created_at.desc()).limit(8):
        plan=(proposal.payload_json or {}).get("plan",{})
        project=(plan.get("project") or {}).get("name")
        subject=f" for {project}" if project else ""
        text=(f"CEO proposed Company work{subject}" if proposal.status=="PENDING"
          else f"Founder {proposal.status.lower()} proposed work{subject}")
        rows.append({"at":proposal.reviewed_at or proposal.created_at,"kind":"PROPOSAL","text":text,"href":f"/inbox"})
    for task in (Task.query.join(Project).filter(
      Project.environment=="LIVE",Task.status=="DONE").order_by(Task.completed_at.desc()).limit(8)):
        rows.append({"at":task.completed_at or task.updated_at,"kind":"RESULT","text":f"{task.project.name}: {task.title} completed","href":f"/projects/{task.project_id}"})
    for meeting in Meeting.query.filter(Meeting.status.in_(["ENDED","TERMINATED_BY_FOUNDER"])).order_by(Meeting.ended_at.desc()).limit(8):
        rows.append({"at":meeting.ended_at or meeting.created_at,"kind":"MEETING","text":f"{meeting.title}: {meeting.status.replace('_',' ').title()}","href":f"/meetings/{meeting.id}"})
        recovery=next((step for step in meeting.steps if step.kind=="LOCAL_RECOVERY" and step.status=="SUCCEEDED"),None)
        if recovery: rows.append({"at":recovery.finished_at or recovery.created_at,"kind":"RECOVERY","text":f"{meeting.title}: intelligence recovered locally","href":f"/meetings/{meeting.id}"})
    return sorted([row for row in rows if row["at"]],key=lambda row:row["at"],reverse=True)[:10]

def _conversation():
    rows=[]
    runs=AgentRun.query.filter_by(purpose="CEO_FOUNDER_REQUEST").order_by(AgentRun.started_at.desc()).limit(10).all()
    for run in reversed(runs):
        payload=run.parsed_output_json or {}
        rows.append({"run":run,"founder":run.user_request,
          "ceo":payload.get("executive_response") or _safe_partial_response(run)
            or (run.error_text if run.status=="FAILED" else "No validated response."),
          "mode":payload.get("mode")})
    return rows

def _today_spend():
    taipei=timezone(timedelta(hours=8))
    start=datetime.combine(datetime.now(taipei).date(),time.min,taipei).astimezone(timezone.utc)
    return Decimal(db.session.query(func.coalesce(func.sum(CostEvent.real_cost_delta),0)).filter(CostEvent.created_at>=start).scalar())


def _operation_report(operation):
    actual=Decimal(db.session.query(func.coalesce(
      func.sum(CostEvent.real_cost_delta),0
    )).filter_by(operation_id=operation.id).scalar())
    remaining_budget=Decimal(operation.approved_budget_twd)-actual
    completed=[task for task in operation.tasks if task.status=="DONE"]
    active=next((task for task in operation.tasks if task.status in {
      "ASSIGNED","WORKING","REVIEW","BLOCKED"}),None)
    results=[task.result_summary for task in completed if task.result_summary][-3:]
    verification=(operation.memory_json or {}).get("goal_verification") or {}
    failed=[item for item in verification.get("criteria") or []
      if item.get("status")!="SATISFIED"]
    blocked=[task for task in operation.tasks if task.status=="BLOCKED"]
    progress=(f"{len(completed)} of {len(operation.tasks)} required Tasks are accepted."
      + (f" Latest result: {results[-1]}" if results else ""))
    risk=None
    if failed:
        risk="; ".join(
          f"{item.get('criterion')}: {item.get('reason')}" for item in failed[:3])
    elif blocked:
        risk="; ".join(
          task.result_summary or f"{task.title} requires remediation"
          for task in blocked[:3])
    if operation.status=="PLANNED":
        return {"headline":"I prepared a bounded company operation.",
          "summary":operation.objective,
          "next_move":"Approve once to authorize the bounded internal work and maximum spend.",
          "risk":"No internal work begins before your approval.",
          "requires_founder":True,
          "decision_needed":"Approve or reject the proposed objective, scope, and budget.",
          "why_founder":"Only the Founder may authorize a new Operation.",
          "after_decision":"If approved, I will manage Tasks, Reviews, Meetings, HR, and Goal Verification.",
          "actual_cost_twd":actual,"remaining_budget_twd":remaining_budget,
          "operation":operation}
    if operation.status=="WAITING_FOR_FOUNDER":
        persisted=operation.founder_report_json or {}
        return {"headline":persisted.get("headline") or "I need one Founder decision.",
          "summary":persisted.get("summary") or operation.waiting_reason,
          "next_move":persisted.get("next_move") or "Provide the requested authority or evidence.",
          "risk":risk,"requires_founder":True,
          "decision_needed":operation.waiting_reason or persisted.get("next_action"),
          "why_founder":"The next action crosses existing Founder authority or recovery boundaries.",
          "after_decision":"I will resume the same bounded Operation or stop as directed.",
          "actual_cost_twd":actual,"remaining_budget_twd":remaining_budget,
          "operation":operation}
    if operation.status=="PAUSED":
        return {"headline":f"{operation.title} is paused.",
          "summary":progress,"next_move":"Resume or stop the approved Operation.",
          "risk":risk,"requires_founder":True,
          "decision_needed":"Choose whether the paused Operation should resume.",
          "why_founder":"Execution was explicitly paused.",
          "after_decision":"I will continue from persisted state without replaying completed paid work.",
          "actual_cost_twd":actual,"remaining_budget_twd":remaining_budget,
          "operation":operation}
    if failed:
        next_move=verification.get("recommended_action") or (
          "I will create bounded remediation work inside the approved Operation.")
    elif active:
        next_move=(
          f"I will resolve {active.title} through the existing operating loop."
          if active.status=="BLOCKED" else
          f"I will continue {active.title}, then obtain its required Review.")
    else:
        next_move="I will run Goal Verification before producing the final report."
    return {"headline":f"I am pursuing: {operation.objective}",
      "summary":progress,"next_move":next_move,"risk":risk,
      "requires_founder":False,"decision_needed":None,"why_founder":None,
      "after_decision":None,"actual_cost_twd":actual,
      "remaining_budget_twd":remaining_budget,"operation":operation}

def snapshot():
    projects=Project.query.filter_by(environment="LIVE").order_by(Project.updated_at.desc()).all()
    active=[project_view(project) for project in projects if project.status in ACTIVE_PROJECT]
    recent_tasks=(Task.query.join(Project).filter(Project.environment=="LIVE",Task.status=="DONE")
      .order_by(Task.completed_at.desc()).limit(6).all())
    recent_meetings=(Meeting.query.filter(Meeting.status.in_(["ENDED","TERMINATED_BY_FOUNDER"]))
      .order_by(Meeting.ended_at.desc()).limit(4).all())
    attention=_attention()
    unresolved=_unresolved_founder_failure()
    meetings=[]
    for meeting in Meeting.query.filter(Meeting.status.in_(OPEN_MEETING)).order_by(Meeting.created_at.desc()).all():
        tokens,cost=meeting_usage(meeting)
        meetings.append({"meeting":meeting,"tokens":tokens,"cost":cost,
          "participants":[p.employee for p in meeting.participants if p.removed_at is None]})
    operation=Operation.query.filter(Operation.status.in_(
      ["PLANNED","WAITING_FOR_FOUNDER","RUNNING","PAUSED"])).order_by(
      Operation.updated_at.desc()).first()
    latest_interaction=_conversation()[-1] if _conversation() else None
    if unresolved:
        cost=Decimal(unresolved.real_cost or 0)
        partial=_safe_partial_response(unresolved)
        report={"headline":"A Founder request needs recovery.",
          "summary":partial or "The CEO response was incomplete and no Company authority was created.",
          "next_move":"Review the preserved paid response, then explicitly retry or provide a revised request.",
          "risk":f"{unresolved.failure_reason or 'FAILED'} · TWD {cost:.2f}",
          "requires_founder":True,
          "decision_needed":"Choose whether to retry or replace this unresolved Founder request.",
          "why_founder":"A paid Founder request failed before valid authority could be created.",
          "after_decision":"The CEO will use a new explicit request; the historical paid response remains auditable.",
          "operation":None,"unresolved_run":unresolved}
    elif attention and not operation:
        brief=_brief(active,attention,recent_tasks,recent_meetings)
        report={"headline":brief["title"],"summary":brief["summary"],
          "next_move":brief["recommendation"],"risk":brief.get("watch"),
          "requires_founder":True,
          "decision_needed":brief["summary"],
          "why_founder":"This item requires existing Founder authority.",
          "after_decision":"I will continue the governed Company path.",
          "operation":None}
    elif latest_interaction and latest_interaction["run"].status=="SUCCEEDED" and not operation:
        report={"headline":"CEO response",
          "summary":latest_interaction["ceo"],
          "next_move":"Continue the conversation or approve proposed work when shown.",
          "risk":None,"requires_founder":False,"decision_needed":None,
          "why_founder":None,"after_decision":None,"operation":None}
    elif operation:
        report=_operation_report(operation)
    else:
        if active:
            brief=_brief(active,attention,recent_tasks,recent_meetings)
            report={"headline":brief["title"],"summary":brief["summary"],
              "next_move":brief["recommendation"],"risk":brief.get("watch"),
              "requires_founder":False,"decision_needed":None,
              "why_founder":None,"after_decision":None,"operation":None}
        else:
            report={"headline":"The company currently has no active work.",
              "summary":"Boss, what would you like us to do?",
              "next_move":"Give me one objective and I will prepare the bounded Company operation.",
              "risk":None,"requires_founder":False,
              "decision_needed":None,"why_founder":None,
              "after_decision":None,"operation":None}
    recent_operation=Operation.query.filter_by(status="COMPLETED").order_by(
      Operation.ended_at.desc(),Operation.updated_at.desc()).first()
    return {"company":get_company(),"ceo":Employee.query.filter_by(slug="ceo").first(),
      "spent":spent(),"remaining":remaining(),"today_spend":_today_spend(),
      "active":active[:3],"attention":attention,"recent_tasks":recent_tasks,
      "recent_meetings":recent_meetings,"meetings":meetings,"activity":_activity(),
      "conversation":_conversation(),"brief":_brief(active,attention,recent_tasks,recent_meetings),
      "report":report,"recent_operation":recent_operation,
      "executive_state":("UNRESOLVED_FAILURE" if unresolved else
        "FOUNDER_ATTENTION" if report["requires_founder"] else
        "ACTIVE_WORK" if operation or active else "IDLE")}

def shell_snapshot():
    company=get_company()
    if not company: return {"ceo":None,"ceo_status":"OFFLINE","today_spend":Decimal(0)}
    ceo=Employee.query.filter_by(slug="ceo").first()
    return {"ceo":ceo,"ceo_status":employee_status(ceo) if ceo else "OFFLINE",
      "today_spend":_today_spend()}

def work_snapshot():
    projects=[project_view(project) for project in Project.query.filter_by(environment="LIVE").order_by(Project.updated_at.desc()).all()]
    return {"attention":[item for item in projects if item["attention"]],
      "active":[item for item in projects if item["project"].status in ACTIVE_PROJECT],
      "on_hold":[item for item in projects if item["project"].status not in ACTIVE_PROJECT],
      "recent_tasks":Task.query.join(Project).filter(Project.environment=="LIVE",Task.status=="DONE").order_by(Task.completed_at.desc()).limit(8).all(),
      "recent_meetings":Meeting.query.join(Project,Meeting.project_id==Project.id).filter(
        Project.environment=="LIVE",Meeting.status.in_(["ENDED","TERMINATED_BY_FOUNDER"])).order_by(Meeting.ended_at.desc()).limit(6).all(),
      "operations":Operation.query.order_by(Operation.updated_at.desc()).all(),
      "unresolved_events":unresolved_founder_failures()}

def team_snapshot():
    employees=Employee.query.order_by(Employee.id).all()
    views={e.id:employee_view(e) for e in employees}
    ceo=next((views[e.id] for e in employees if e.slug=="ceo"),None)
    def node(employee):
        return {"view":views[employee.id],"reports":[
          node(report) for report in employees if report.manager_id==employee.id]}
    branches=[node(e) for e in employees if ceo and e.manager_id==ceo["employee"].id]
    groups=[]
    for department in Department.query.order_by(Department.name).all():
        members=[views[e.id] for e in employees if e.department_id==department.id and e.slug!="ceo"]
        if members: groups.append({"department":department,"members":members})
    ungrouped=[views[e.id] for e in employees if not e.department_id and e.slug!="ceo"]
    return {"ceo":ceo,"branches":branches,"groups":groups,"ungrouped":ungrouped,
      "open_hiring":HiringRequest.query.filter(HiringRequest.status.in_(
        ["REQUESTED","HR_REVIEW","FOUNDER_REVIEW"])).count(),
      "founder_review":HiringRequest.query.filter_by(status="FOUNDER_REVIEW").count(),
      "talent_pool":TalentTemplate.query.filter_by(active_in_pool=True).count(),
      "active_employees":Employee.query.filter_by(active=True).count()}
