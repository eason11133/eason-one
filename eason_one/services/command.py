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
        plan=(proposal.payload_json or {}).get("plan",{})
        project=plan.get("project") or {}
        items.append({"kind":"PROPOSAL","title":project.get("name") or "CEO proposed work",
          "summary":"CEO plan waiting for approval.","recommendation":"Review and approve or reject the plan.",
          "proposal":proposal,"href":f"/inbox#{proposal.id}","created_at":proposal.created_at})
    for task in Task.query.join(Project).filter(Project.environment=="LIVE",Task.status=="BLOCKED").order_by(Task.updated_at.desc()).all():
        items.append({"kind":"BLOCKED","title":task.project.name,"summary":task.title,
          "recommendation":"Review blocked work.","href":f"/projects/{task.project_id}","created_at":task.updated_at})
    for meeting in Meeting.query.filter(Meeting.status.in_(["WAITING_FOR_FOUNDER","PAUSED"])).order_by(Meeting.updated_at.desc() if hasattr(Meeting,"updated_at") else Meeting.created_at.desc()).all():
        if meeting.status=="PAUSED" and not meeting.paid_failure_json: continue
        items.append({"kind":"MEETING","title":meeting.title,
          "summary":"Founder input required." if meeting.status=="WAITING_FOR_FOUNDER" else "Paid Meeting step requires a Founder decision.",
          "recommendation":"Open the Meeting.","href":f"/meetings/{meeting.id}","created_at":meeting.created_at})
    return sorted(items,key=lambda item:item["created_at"],reverse=True)

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
          "ceo":payload.get("executive_response") or (run.error_text if run.status=="FAILED" else "No validated response."),
          "mode":payload.get("mode")})
    return rows

def _today_spend():
    taipei=timezone(timedelta(hours=8))
    start=datetime.combine(datetime.now(taipei).date(),time.min,taipei).astimezone(timezone.utc)
    return Decimal(db.session.query(func.coalesce(func.sum(CostEvent.real_cost_delta),0)).filter(CostEvent.created_at>=start).scalar())

def snapshot():
    projects=Project.query.filter_by(environment="LIVE").order_by(Project.updated_at.desc()).all()
    active=[project_view(project) for project in projects if project.status in ACTIVE_PROJECT]
    recent_tasks=(Task.query.join(Project).filter(Project.environment=="LIVE",Task.status=="DONE")
      .order_by(Task.completed_at.desc()).limit(6).all())
    recent_meetings=(Meeting.query.filter(Meeting.status.in_(["ENDED","TERMINATED_BY_FOUNDER"]))
      .order_by(Meeting.ended_at.desc()).limit(4).all())
    attention=_attention()
    meetings=[]
    for meeting in Meeting.query.filter(Meeting.status.in_(OPEN_MEETING)).order_by(Meeting.created_at.desc()).all():
        tokens,cost=meeting_usage(meeting)
        meetings.append({"meeting":meeting,"tokens":tokens,"cost":cost,
          "participants":[p.employee for p in meeting.participants if p.removed_at is None]})
    operation=Operation.query.filter(Operation.status.in_(
      ["PLANNED","WAITING_FOR_FOUNDER","COMPLETED","RUNNING","PAUSED"])).order_by(
      Operation.updated_at.desc()).first()
    if operation and operation.founder_report_json:
        report=operation.founder_report_json|{"operation":operation}
    elif operation and operation.status=="PLANNED":
        report={"headline":"CEO proposed an operation.",
          "summary":operation.objective,
          "next_move":"Approve once to authorize bounded internal execution.",
          "operation":operation}
    elif operation and operation.status in {"RUNNING","PAUSED"}:
        active_task=next((task for task in operation.tasks if task.status not in {
          "DONE","FAILED","CANCELLED"}),None)
        report={"headline":f"{operation.title} is {'paused' if operation.status=='PAUSED' else 'in progress'}.",
          "summary":active_task.title if active_task else operation.objective,
          "next_move":"CEO will continue the next bounded browser-driven step.",
          "operation":operation}
    else:
        brief=_brief(active,attention,recent_tasks,recent_meetings)
        report={"headline":brief["title"],"summary":brief["summary"],
          "next_move":brief["recommendation"],"operation":None}
    return {"company":get_company(),"ceo":Employee.query.filter_by(slug="ceo").first(),
      "spent":spent(),"remaining":remaining(),"today_spend":_today_spend(),
      "active":active[:3],"attention":attention,"recent_tasks":recent_tasks,
      "recent_meetings":recent_meetings,"meetings":meetings,"activity":_activity(),
      "conversation":_conversation(),"brief":_brief(active,attention,recent_tasks,recent_meetings),
      "report":report}

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
      "operations":Operation.query.order_by(Operation.updated_at.desc()).all()}

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
