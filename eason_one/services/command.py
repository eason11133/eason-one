from decimal import Decimal
import json
from sqlalchemy import func, or_
from ..extensions import db
from ..models import (AgentRun,CostEvent,Department,Employee,Escalation,Meeting,MeetingParticipant,
  MeetingStep,Operation,Project,Proposal,Task,Work,WorkAssignment,WorkMessage,HiringRequest,TalentTemplate)
from .company import get_company,spent,remaining
from .meetings import usage as meeting_usage

ACTIVE_PROJECT={"PLANNING","ACTIVE","BLOCKED","REVIEW"}
ACTIVE_TASK={"ASSIGNED","WORKING","REVIEW"}
ATTENTION_MEETING={"WAITING_FOR_FOUNDER"}
OPEN_MEETING={"RUNNING","PAUSED","WAITING_FOR_FOUNDER"}

def _current_work_view(employee):
    """Return live Work truth while preserving legacy Task-shaped UI access.

    A governed Employee is WORKING only when a live Execution exists for a Work
    currently assigned to that Employee. Work state alone is not enough to fake
    activity after a crash. Legacy Tasks remain a fallback only when work_id is
    absent.
    """
    run = (
        AgentRun.query.join(Work, AgentRun.work_id == Work.id)
        .join(Project, Work.project_id == Project.id)
        .join(
            WorkAssignment,
            (WorkAssignment.work_id == Work.id)
            & (WorkAssignment.employee_id == employee.id)
            & (WorkAssignment.ended_at.is_(None)),
        )
        .filter(
            AgentRun.employee_id == employee.id,
            AgentRun.status.in_(["CREATED", "RUNNING"]),
            Project.environment == "LIVE",
            Project.status.in_(ACTIVE_PROJECT),
            Work.state.in_(["EXECUTING", "VERIFYING"]),
        )
        .order_by(AgentRun.id.desc()).first()
    )
    if run and run.work_id:
        work = db.session.get(Work, run.work_id)
        if work:
            task = Task.query.filter_by(work_id=work.id).order_by(Task.id).first()
            if task:
                return task
            return {
                "id": work.id, "title": work.title, "project": work.project,
                "project_id": work.project_id, "status": work.state,
                "work_id": work.id, "kind": "WORK",
            }
    return (
        Task.query.join(Project)
        .filter(
            Task.assigned_employee_id == employee.id,
            Task.work_id.is_(None),
            Project.environment == "LIVE",
            Project.status.in_(ACTIVE_PROJECT),
            Task.status.in_(ACTIVE_TASK),
        ).order_by(Task.updated_at.desc()).first()
    )


def employee_status(employee):
    if not employee.active: return "DISABLED"
    in_meeting=(MeetingParticipant.query.join(Meeting)
      .outerjoin(Project, Meeting.project_id==Project.id).filter(
      MeetingParticipant.employee_id==employee.id,MeetingParticipant.removed_at.is_(None),
      Meeting.status=="RUNNING",
      or_(Meeting.project_id.is_(None), Project.status.in_(ACTIVE_PROJECT))).first())
    if in_meeting: return "IN MEETING"
    return "WORKING" if _current_work_view(employee) else "AVAILABLE"


def employee_view(employee):
    current = _current_work_view(employee)
    return {"employee":employee,"status":employee_status(employee),"current_work":current}


def project_view(project):
    contract_api = __import__(
        "eason_one.services.project_contract", fromlist=["is_vnext_governed"]
    )
    governed = contract_api.is_vnext_governed(project)
    tasks=Task.query.filter_by(project_id=project.id).all()
    if governed:
        works = Work.query.filter_by(project_id=project.id).filter(Work.work_type != "MANAGEMENT").all()
        done = sum(work.state == "ACCEPTED" for work in works)
        active_works = [work for work in works if work.state in {"READY","EXECUTING","WAITING","VERIFYING"}]
        by_work = {task.work_id: task for task in tasks if task.work_id is not None}
        active = [by_work[work.id] for work in active_works if work.id in by_work]
        employee_ids = {
            assignment.employee_id
            for work in works
            for assignment in work.assignments
            if assignment.ended_at is None
        }
        team = [db.session.get(Employee, employee_id) for employee_id in sorted(employee_ids)]
        team = [employee for employee in team if employee is not None]
        total = len(works)
    else:
        done=sum(task.status=="DONE" for task in tasks)
        active=[task for task in tasks if task.status in ACTIVE_TASK|{"BLOCKED"}]
        team=list({task.assigned_employee.id:task.assigned_employee for task in tasks if task.assigned_employee}.values())
        total = len(tasks)
    cost=Decimal(db.session.query(func.coalesce(func.sum(CostEvent.real_cost_delta),0)).filter_by(project_id=project.id).scalar())
    pending=__import__("eason_one.services.proposal_authority",fromlist=["pending_initial_count"]).pending_initial_count(project_id=project.id)
    meeting_coordination=__import__(
      "eason_one.services.meeting_coordination",fromlist=["requires_founder_input"]
    )
    waiting=sum(1 for meeting in Meeting.query.filter_by(
      project_id=project.id,status="WAITING_FOR_FOUNDER"
    ).all() if meeting_coordination.requires_founder_input(meeting))
    governance_count=len(__import__(
      "eason_one.services.governance",fromlist=["attention"]
    ).attention(project))
    attention=bool(pending or waiting or governance_count)
    return {"project":project,"tasks":tasks,"done":done,"total":total,"active_tasks":active,
      "team":team,"cost":cost,"attention":attention}


def _attention():
    items=[]
    governance = __import__(
      "eason_one.services.governance",fromlist=["attention","normalize_type"]
    )
    for escalation in governance.attention():
        project = db.session.get(Project, escalation.project_id)
        items.append({
          "kind":"GOVERNANCE",
          "title":getattr(project,"name",None) or "Project authority",
          "summary":escalation.reason,
          "recommendation":escalation.recommendation or "Review the exact Founder authority request.",
          "escalation":escalation,
          "href":f"/headquarters/projects/{escalation.project_id}#needs-you",
          "created_at":escalation.created_at})
    for proposal in Proposal.query.filter_by(status="PENDING").order_by(Proposal.created_at.desc()).all():
        if not __import__(
            "eason_one.services.proposal_authority", fromlist=["is_initial_project_proposal"]
        ).is_initial_project_proposal(proposal):
            continue
        plan=(proposal.payload_json or {}).get("plan",{})
        project=plan.get("project") or {}
        items.append({"kind":"PROPOSAL","title":project.get("name") or "CEO proposed work",
          "summary":"CEO plan waiting for approval.","recommendation":"Review and approve or reject the plan.",
          "proposal":proposal,"href":f"/inbox#{proposal.id}","created_at":proposal.created_at})
    # Meeting runtime PAUSED is Company bounded recovery, including paid/provider
    # failures. Only an explicit WAITING_FOR_FOUNDER conversational question is
    # Founder attention; provider/debug recovery must not be escalated by a read model.
    meeting_coordination=__import__(
      "eason_one.services.meeting_coordination",fromlist=["requires_founder_input"]
    )
    for meeting in Meeting.query.filter_by(status="WAITING_FOR_FOUNDER").order_by(
      Meeting.updated_at.desc() if hasattr(Meeting,"updated_at") else Meeting.created_at.desc()
    ).all():
        if not meeting_coordination.requires_founder_input(meeting):
            continue
        items.append({"kind":"MEETING","title":meeting.title,
          "summary":"Founder input required for this Meeting conversation.",
          "recommendation":"Open the Meeting and answer the explicit question.",
          "href":f"/meetings/{meeting.id}","created_at":meeting.created_at})
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
    cost=Decimal(db.session.query(func.coalesce(
      func.sum(CostEvent.real_cost_delta),0
    )).filter_by(operation_id=operation.id).scalar())
    team=[]
    for row in task_rows:
        employee=row["employee"]
        if not employee or any(item["employee"].id==employee.id for item in team):
            continue
        employee_rows=[item for item in task_rows
          if item["employee"] and item["employee"].id==employee.id]
        employee_cost=Decimal(db.session.query(func.coalesce(
          func.sum(CostEvent.real_cost_delta),0)).filter_by(
            operation_id=operation.id,employee_id=employee.id).scalar())
        team.append({"employee":employee,"cost_twd":employee_cost,
          "tasks":employee_rows,
          "status":next((item["status"] for item in employee_rows
            if item["status"] in {"WORKING","REVIEW","ASSIGNED","BLOCKED"}),
            employee_rows[-1]["status"])})
    budget=Decimal(operation.approved_budget_twd)
    current=__import__(
      "eason_one.services.operations",fromlist=["state"]).state(operation)
    founder_decision=None
    contracts=__import__(
      "eason_one.services.project_contract",fromlist=["is_vnext_governed"]
    )
    governance=__import__(
      "eason_one.services.governance",fromlist=["current_gate","normalize_type"]
    )
    governed=bool(operation.project and contracts.is_vnext_governed(operation.project))
    canonical_gate=(governance.current_gate(operation.project) if governed else None)
    gate_for_operation=bool(
      canonical_gate and canonical_gate.operation_id == operation.id
    )
    if governed and gate_for_operation:
        approve=next((dict(item) for item in (canonical_gate.options_json or [])
          if isinstance(item,dict) and str(item.get("action") or "").upper()=="APPROVE"),{})
        kind=governance.normalize_type(canonical_gate.escalation_type)
        additional=(Decimal(str(approve.get("additional_budget_twd")))
          if kind=="BUDGET_AUTHORIZATION" and approve.get("additional_budget_twd") is not None else None)
        founder_decision={
          "kind":kind,
          "title":f"{operation.title} — {kind.replace('_',' ').title()}",
          "recommendation":canonical_gate.reason,
          "budget_twd":budget,
          "authorized_twd":approve.get("authorized_twd"),
          "spent_twd":approve.get("spent_twd"),
          "additional_required_twd":additional,
          "resulting_total_twd":approve.get("resulting_total_twd"),
          "completion_criteria":plan.get("completion_criteria") or [],
          "objective":operation.objective,
          "waiting":True,
          "escalation_id":canonical_gate.id,
        }
    elif not governed and operation.status in {"PLANNED","WAITING_FOR_FOUNDER"}:
        report=operation.founder_report_json or {}
        budget_decision=(report.get("decision_kind")=="BUDGET_AUTHORIZATION")
        additional=None
        resulting_total=None
        if budget_decision:
            raw=report.get("additional_budget_twd") or "0"
            additional=__import__(
              "eason_one.services.operations",
              fromlist=["budget_authorization_amount"]
            ).budget_authorization_amount(raw)
            resulting_total=budget+additional
        founder_decision={
          "kind":report.get("decision_kind") or "OPERATION_APPROVAL",
          "title":(f"{operation.title} — Additional budget required" if budget_decision else operation.title),
          "recommendation":operation.waiting_reason or report.get("summary") or operation.objective,
          "budget_twd":budget,
          "authorized_twd":report.get("approved_twd"),
          "spent_twd":report.get("actual_twd"),
          "additional_required_twd":additional,
          "resulting_total_twd":resulting_total,
          "completion_criteria":plan.get("completion_criteria") or [],
          "objective":operation.objective,
          "waiting":operation.status=="WAITING_FOR_FOUNDER"}
    founder_attention=(
      canonical_gate.reason if governed and gate_for_operation
      else (operation.waiting_reason or (operation.founder_report_json or {}).get("summary")
        if (not governed and operation.status in {"WAITING_FOR_FOUNDER","PAUSED"}) else None)
    )
    return {
      "operation":operation,"tasks":task_rows,"team":team,
      "done":done,"total":total,
      "progress_percent":round((done/total)*100) if total else 0,
      "cost_twd":cost,
      "budget_twd":budget,"remaining_twd":budget-cost,
      "current_stage":current["current_step"],
      "founder_decision":founder_decision,
      "founder_attention":founder_attention,
      "verification":(
        (operation.memory_json or {}).get("goal_verification") or {}),
      "completion_criteria":plan.get("completion_criteria") or [],
      "result":operation.founder_report_json or {}}


def _recover_legacy_pending_operation():
    """Rebuild the one pre-V1 structured follow-up shape as a pending plan."""
    exact_error=(
      "CEO plan validation failed: Only OPERATION_PLAN may define an operation")
    run=next((
      item for item in AgentRun.query.filter_by(
        purpose="CEO_FOUNDER_REQUEST",status="SUCCEEDED",
        error_text=exact_error).order_by(AgentRun.started_at.desc()).all()
      if item.parsed_output_json is None),None)
    if not run or not run.raw_output:
        return None
    try:
        payload=json.loads(run.raw_output)
    except (TypeError,json.JSONDecodeError):
        return None
    if (payload.get("mode")!="OPERATION_FOLLOW_UP"
        or not isinstance(payload.get("operation"),dict)):
        return None
    normalized={
      "mode":"OPERATION_PLAN",
      "executive_response":payload.get("executive_response"),
      "operation":payload["operation"]}
    operation_service=__import__(
      "eason_one.services.operations",fromlist=["validate_plan"])
    operation_service.validate_plan(normalized)
    operation=next((
      item for item in Operation.query.order_by(Operation.id.desc()).all()
      if (item.memory_json or {}).get(
        "legacy_source_agent_run_id")==run.id),None)
    if not operation:
        ceo=db.session.get(Employee,run.employee_id)
        operation=operation_service.propose_operation(ceo,normalized)
        memory=dict(operation.memory_json or {})
        memory["legacy_source_agent_run_id"]=run.id
        operation.memory_json=memory
    run.parsed_output_json=normalized
    run.error_text=None
    db.session.commit()
    return operation if operation.status in {
      "PLANNED","WAITING_FOR_FOUNDER","RUNNING","PAUSED"} else None


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
    if not operation:
        operation=_recover_legacy_pending_operation()
    if operation and operation.status=="WAITING_FOR_FOUNDER":
        __import__(
          "eason_one.services.operations",
          fromlist=["recover_budget_governance"]
        ).recover_budget_governance(operation)
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
    current_failure=latest["run"] if latest_failed else None
    historical_failure_count=sum(
      run.id!=getattr(current_failure,"id",None) for run in failures)
    briefing=_operation_briefing(operation) if operation else None
    response_signal=(
      "failure" if current_failure else
      "founder_decision" if briefing and briefing["founder_decision"] else
      "normal_response" if primary or briefing or visible_active
        or recent_operation else "idle")
    return {
      "company":get_company(),
      "ceo":Employee.query.filter_by(slug="ceo").first(),
      "spent":spent(),"remaining":remaining(),
      "active":visible_active[:3],"attention":attention,"recent_tasks":recent_tasks,
      "recent_meetings":recent_meetings,"meetings":meetings,
      "activity":_activity(),"conversation":conversation,
      "primary_response":primary,
      "response_signal":response_signal,
      "report":{"summary":(
        primary["ceo"] if primary else latest.get("ceo")
        if latest_failed and latest else None)},
      "operation_briefing":briefing,
      "recent_operation":(
        _operation_briefing(recent_operation) if recent_operation else None),
      "current_failure":current_failure,
      "current_failure_message":(
        founder_failure_message(current_failure) if current_failure else None),
      "historical_failure_count":historical_failure_count,
      "executive_state":(
        "UNRESOLVED_FAILURE" if latest_failed else
        "FOUNDER_ATTENTION" if attention or failures or (
          operation and operation.status in {
            "PLANNED","WAITING_FOR_FOUNDER","PAUSED"})
        else "ACTIVE_WORK" if operation or visible_active else "IDLE")}

def founder_failure_message(run):
    error=run.error_text or ""
    if "API_KEY" in error or "provider is not configured" in error:
        return (
          "A provider credential is unavailable. No new paid call was made. "
          "Open Company → Models to restore configuration; technical detail "
          "remains in Audit.")
    if run.failure_reason=="OUTPUT_TRUNCATED":
        return "The provider response ended before a usable result was produced."
    return "The request did not produce a usable result. Open Audit for details."


def shell_snapshot():
    company=get_company()
    if not company: return {"ceo":None,"ceo_status":"OFFLINE",
      "total_spend":Decimal(0)}
    ceo=Employee.query.filter_by(slug="ceo").first()
    # This context processor runs for every HTML response. It must not build the
    # complete Company projection merely to paint the legacy sidebar status.
    # The dedicated HQ page owns the detailed reliability/governance read model.
    has_attention = db.session.query(Escalation.id).filter(
      Escalation.resolved_at.is_(None)
    ).first() is not None
    has_live_execution = db.session.query(AgentRun.id).join(
      Project, AgentRun.project_id == Project.id
    ).filter(
      Project.environment == "LIVE",
      Project.status.in_(ACTIVE_PROJECT),
      AgentRun.status.in_(["CREATED", "RUNNING"]),
    ).first() is not None
    ceo_status = "FOUNDER_ATTENTION" if has_attention else (
      "WORKING" if has_live_execution else "AVAILABLE"
    )
    return {"ceo":ceo,"ceo_status":ceo_status,
      "total_spend":spent()}

def work_snapshot():
    view=__import__(
      "eason_one.services.current_company",fromlist=["projection"]).projection()
    operation_project_ids={row["operation"].project_id for row in view["operations"]
      if row["classification"]!="TERMINAL" and row["operation"].project_id}
    projects=[project_view(project) for project in Project.query.filter_by(
      environment="LIVE").order_by(Project.updated_at.desc()).all()
      if project.id not in operation_project_ids]
    return {"attention":[item for item in projects if item["attention"]],
      "active":[item for item in projects if item["project"].status in ACTIVE_PROJECT],
      "on_hold":[item for item in projects if item["project"].status not in ACTIVE_PROJECT],
      "recent_tasks":Task.query.join(Project).filter(Project.environment=="LIVE",Task.status=="DONE").order_by(Task.completed_at.desc()).limit(8).all(),
      "recent_meetings":Meeting.query.join(Project,Meeting.project_id==Project.id).filter(
        Project.environment=="LIVE",Meeting.status.in_(["ENDED","TERMINATED_BY_FOUNDER"])).order_by(Meeting.ended_at.desc()).limit(6).all(),
      "operations":view["operations"],"governance":view["governance"],
      "failure_groups":view["failures"],
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
