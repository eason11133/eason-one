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
            or (run.error_text if run.status=="FAILED" else None),
          "mode":payload.get("mode")})
    return rows

def _today_spend():
    taipei=timezone(timedelta(hours=8))
    start=datetime.combine(datetime.now(taipei).date(),time.min,taipei).astimezone(timezone.utc)
    return Decimal(db.session.query(func.coalesce(func.sum(CostEvent.real_cost_delta),0)).filter(CostEvent.created_at>=start).scalar())


def _operation_briefing(operation):
    plan=(operation.plan_json or {}).get("operation") or {}
    task_rows=[]
    if operation.tasks:
        for task in operation.tasks:
            task_rows.append({
              "title":task.title,"status":task.status,
              "employee":task.assigned_employee,"reviewer":task.reviewer,
              "result":task.result_summary})
    else:
        for item in plan.get("tasks") or []:
            task_rows.append({
              "title":item.get("title"),"status":"PLANNED",
              "employee":db.session.get(
                Employee,item.get("assignee_employee_id")),
              "reviewer":db.session.get(
                Employee,item.get("reviewer_employee_id")),
              "result":None})
    done=sum(row["status"]=="DONE" for row in task_rows)
    total=len(task_rows)
    runs=AgentRun.query.filter_by(operation_id=operation.id)
    input_tokens=int(runs.with_entities(
      func.coalesce(func.sum(AgentRun.input_tokens),0)).scalar() or 0)
    output_tokens=int(runs.with_entities(
      func.coalesce(func.sum(AgentRun.output_tokens),0)).scalar() or 0)
    cost=Decimal(db.session.query(func.coalesce(
      func.sum(CostEvent.real_cost_delta),0
    )).filter_by(operation_id=operation.id).scalar())
    budget=Decimal(operation.approved_budget_twd)
    current=__import__(
      "eason_one.services.operations",fromlist=["state"]).state(operation)
    return {
      "operation":operation,"tasks":task_rows,"done":done,"total":total,
      "progress_percent":round((done/total)*100) if total else 0,
      "input_tokens":input_tokens,"output_tokens":output_tokens,
      "total_tokens":input_tokens+output_tokens,"cost_twd":cost,
      "budget_twd":budget,"remaining_twd":budget-cost,
      "current_stage":current["current_step"],
      "founder_attention":(
        operation.waiting_reason or (operation.founder_report_json or {}).get(
          "summary") if operation.status in {
            "WAITING_FOR_FOUNDER","PAUSED"} else None),
      "verification":(
        (operation.memory_json or {}).get("goal_verification") or {}),
      "completion_criteria":plan.get("completion_criteria") or [],
      "result":operation.founder_report_json or {}}

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
    latest_run=latest_interaction["run"] if latest_interaction else None
    latest_is_unresolved=(
      unresolved and latest_run and unresolved.id==latest_run.id)
    if latest_is_unresolved:
        cost=Decimal(unresolved.real_cost or 0)
        partial=_safe_partial_response(unresolved)
        report={"headline":None,
          "summary":partial,
          "next_move":None,
          "risk":f"{unresolved.failure_reason or 'FAILED'} · TWD {cost:.2f}",
          "requires_founder":True,
          "decision_needed":None,
          "why_founder":None,
          "after_decision":None,
          "operation":None,"unresolved_run":unresolved}
    elif latest_interaction and latest_run.status=="SUCCEEDED" and not operation:
        report={"headline":"CEO response",
          "summary":latest_interaction["ceo"],
          "next_move":None,
          "risk":None,"requires_founder":bool(attention),
          "decision_needed":attention[0]["summary"] if attention else None,
          "why_founder":None,
          "after_decision":None,
          "operation":None}
    elif attention and not operation:
        brief=_brief(active,attention,recent_tasks,recent_meetings)
        report={"headline":brief["title"],"summary":brief["summary"],
          "next_move":brief["recommendation"],"risk":brief.get("watch"),
          "requires_founder":True,
          "decision_needed":brief["summary"],
          "why_founder":None,
          "after_decision":None,
          "operation":None}
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
            report={"headline":None,
              "summary":None,
              "next_move":None,
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
      "unresolved_attention":(
        [unresolved] if unresolved and not latest_is_unresolved else []),
      "executive_state":("UNRESOLVED_FAILURE" if latest_is_unresolved else
        "FOUNDER_ATTENTION" if report["requires_founder"] else
        "ACTIVE_WORK" if operation or active else "IDLE")}

def snapshot():
    projects=Project.query.filter_by(
      environment="LIVE").order_by(Project.updated_at.desc()).all()
    active=[project_view(project) for project in projects
      if project.status in ACTIVE_PROJECT]
    recent_tasks=(Task.query.join(Project).filter(
      Project.environment=="LIVE",Task.status=="DONE")
      .order_by(Task.completed_at.desc()).limit(6).all())
    recent_meetings=(Meeting.query.filter(Meeting.status.in_(
      ["ENDED","TERMINATED_BY_FOUNDER"]))
      .order_by(Meeting.ended_at.desc()).limit(4).all())
    attention=_attention()
    meetings=[]
    for meeting in Meeting.query.filter(
      Meeting.status.in_(OPEN_MEETING)).order_by(Meeting.created_at.desc()).all():
        tokens,cost=meeting_usage(meeting)
        meetings.append({"meeting":meeting,"tokens":tokens,"cost":cost,
          "participants":[p.employee for p in meeting.participants
            if p.removed_at is None]})
    operation=Operation.query.filter(Operation.status.in_(
      ["PLANNED","WAITING_FOR_FOUNDER","RUNNING","PAUSED"])).order_by(
      Operation.updated_at.desc()).first()
    conversation=_conversation()
    latest=conversation[-1] if conversation else None
    primary=(latest if latest and latest["run"].status=="SUCCEEDED"
      and latest.get("ceo") else None)
    recent_operation=Operation.query.filter_by(status="COMPLETED").order_by(
      Operation.ended_at.desc(),Operation.updated_at.desc()).first()
    visible_active=[item for item in active if not (
      recent_operation and item["project"].id==recent_operation.project_id)]
    failures=unresolved_founder_failures()
    latest_failed=bool(
      latest and latest["run"].status=="FAILED"
      and latest["run"].resolution_status is None)
    return {
      "company":get_company(),
      "ceo":Employee.query.filter_by(slug="ceo").first(),
      "spent":spent(),"remaining":remaining(),"today_spend":_today_spend(),
      "active":visible_active[:3],"attention":attention,"recent_tasks":recent_tasks,
      "recent_meetings":recent_meetings,"meetings":meetings,
      "activity":_activity(),"conversation":conversation,
      "primary_response":primary,
      "report":{"summary":(
        primary["ceo"] if primary else latest.get("ceo")
        if latest_failed and latest else None)},
      "operation_briefing":_operation_briefing(operation) if operation else None,
      "recent_operation":(
        _operation_briefing(recent_operation) if recent_operation else None),
      "unresolved_attention":failures,
      "executive_state":(
        "UNRESOLVED_FAILURE" if latest_failed else
        "FOUNDER_ATTENTION" if attention or failures or (
          operation and operation.status in {
            "PLANNED","WAITING_FOR_FOUNDER","PAUSED"})
        else "ACTIVE_WORK" if operation or visible_active else "IDLE")}

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
