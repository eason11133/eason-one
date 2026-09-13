import json
import hashlib
from decimal import Decimal
from sqlalchemy import func, or_
from ..extensions import db
from ..models import (
    Proposal, Employee, Project, Task, Work, WorkAssignment, Operation, CostEvent, WorkMessage, AgentRun,
    ContributionEvent, Meeting, MeetingParticipant, now,
)
from .execution import execute
from .company import get_company,spent,remaining
from .brain import current
from ..schemas import (
    CEO_SCHEMA, CEO_EXECUTION_SCHEMA, SYNTHESIS_SCHEMA, CEO_STATUS_REPORT_SCHEMA,
)
from .stabilization import REAL_WORK, operation_kind
from .text_normalization import clean_rows, clean_text

MODES={"NEW_PROJECT","PROJECT_ACTION","STATUS_QUERY","ADVISORY","OPERATION_PLAN",
  "OPERATION_FOLLOW_UP"}
PRIORITIES={"LOW","MEDIUM","HIGH","CRITICAL"}
TASK_FIELDS={"title","objective","assignee_slug","reviewer_slug","required_output","acceptance_criteria"}
CEO_CHAT_TYPES={"FOUNDER_TO_CEO","CEO_TO_FOUNDER"}
EXECUTION_ROUTES={"AUTO_DELEGATION","SINGLE_WORKER","SHORT_MEETING","FULL_PROJECT"}

def recent_ceo_conversation(limit=10):
    rows=(WorkMessage.query.filter(WorkMessage.message_type.in_(CEO_CHAT_TYPES))
      .order_by(WorkMessage.id.desc()).limit(limit).all())
    rows.reverse()
    return rows

def ceo_conversation_context(limit=8):
    rows=recent_ceo_conversation(limit)
    if not rows:
        return "No prior Founder/CEO dialogue is persisted."
    rendered=[]
    for row in rows:
        role="CEO" if row.message_type=="CEO_TO_FOUNDER" else "FOUNDER"
        rendered.append(f"{role}: {row.content}")
    return "\n".join(rendered)

def record_founder_message(content, project_id=None, task_id=None, run=None):
    # A browser/network retry can legitimately re-enter the same durable CEO
    # request.  Bind dialogue rows to that AgentRun so replay never duplicates
    # the Founder/CEO conversation merely because the HTTP response was lost.
    run_id=getattr(run,"id",None)
    if run_id is not None:
        existing=WorkMessage.query.filter_by(
          agent_run_id=run_id,message_type="FOUNDER_TO_CEO"
        ).order_by(WorkMessage.id).first()
        if existing:
            return existing
    row=WorkMessage(project_id=project_id,task_id=task_id,agent_run_id=run_id,
      recipient_employee_id=getattr(Employee.query.filter_by(slug="ceo").first(),"id",None),
      message_type="FOUNDER_TO_CEO",content=content.strip())
    db.session.add(row); db.session.commit(); return row

def record_ceo_message(content, run=None, project_id=None, task_id=None):
    ceo=Employee.query.filter_by(slug="ceo").first()
    run_id=getattr(run,"id",None)
    if run_id is not None:
        existing=WorkMessage.query.filter_by(
          agent_run_id=run_id,message_type="CEO_TO_FOUNDER"
        ).order_by(WorkMessage.id).first()
        if existing:
            return existing
    row=WorkMessage(project_id=project_id,task_id=task_id,
      sender_employee_id=getattr(ceo,"id",None),agent_run_id=run_id,
      message_type="CEO_TO_FOUNDER",content=content.strip())
    db.session.add(row); db.session.commit(); return row

def _normalize_founder_request_id(value):
    value=clean_text(str(value or ""),multiline=False).strip()
    if not value:
        return None
    if len(value)>160:
        raise ValueError("Founder request ID is too long")
    return value

def _existing_founder_request_run(request_id, request, project_id=None):
    """Return the durable Run for one browser request id, if it exists.

    The request id is generated before paid CEO execution.  It is intentionally
    stored in AgentRun context JSON rather than a transient Flask session so a
    process restart can recover the exact paid planning attempt.  Reusing an id
    for different text or Project scope is a hard conflict, never a cache hit.
    """
    request_id=_normalize_founder_request_id(request_id)
    if request_id is None:
        return None
    match=None
    for row in AgentRun.query.filter_by(purpose="CEO_FOUNDER_REQUEST").order_by(AgentRun.id.desc()).all():
        composition=dict(row.context_composition_json or {})
        if composition.get("founder_request_id")==request_id:
            match=row
            break
    if match is None:
        return None
    composition=dict(match.context_composition_json or {})
    if str(match.user_request or "") != str(request or ""):
        raise ValueError("FOUNDER_REQUEST_ID_CONFLICT: request text differs from the durable paid request")
    stored_project=composition.get("founder_request_project_id")
    if int(stored_project or 0) != int(project_id or 0):
        raise ValueError("FOUNDER_REQUEST_ID_CONFLICT: Project scope differs from the durable paid request")
    return match

def operating_context():
    roster=[]
    contract_api=__import__(
      "eason_one.services.project_contract",fromlist=["read_projection","is_vnext_governed"]
    )
    for e in Employee.query.filter_by(active=True).order_by(Employee.id):
        governed_works=(Work.query.join(WorkAssignment,WorkAssignment.work_id==Work.id)
          .join(Project,Work.project_id==Project.id)
          .filter(WorkAssignment.employee_id==e.id,WorkAssignment.ended_at.is_(None),
            Project.environment=="LIVE",
            Project.status.in_(["PLANNING","ACTIVE","BLOCKED","REVIEW"]),
            Work.state.in_(["READY","EXECUTING","WAITING","VERIFYING"]))
          .order_by(Work.id).all())
        legacy=(Task.query.join(Project,Task.project_id==Project.id)
          .outerjoin(Operation,Task.operation_id==Operation.id)
          .filter(Task.assigned_employee_id==e.id,Project.environment=="LIVE",
            Project.status.in_(["PLANNING","ACTIVE","BLOCKED","REVIEW"]),
            Task.work_id.is_(None),
            Task.status.in_(["ASSIGNED","WORKING","BLOCKED","REVIEW"]),
            or_(Task.operation_id.is_(None),Operation.status=="RUNNING")).all())
        legacy=[task for task in legacy if task.operation_id is None or operation_kind(task.operation)==REAL_WORK]
        project_names={work.project.name for work in governed_works if work.project}
        project_names.update(task.project.name for task in legacy if task.project)
        model_text=(f"{e.current_model.label} / {e.current_model.model_name}" if e.current_model
          else "UNASSIGNED / execution unavailable")
        experience = __import__(
          "eason_one.services.employee_memory", fromlist=["roster_experience_summary"]
        ).roster_experience_summary(e)
        roster.append(f"{e.name}\nID: {e.id}; slug: {e.slug}; department: {e.department.name if e.department else 'CEO Office / Assurance'}; "
          f"position: {e.position.name} / level {e.position.level}; manager: {e.manager.name if e.manager else 'Founder'}; "
          f"role: {e.role_description}; capabilities: {', '.join(sorted(__import__('eason_one.services.team_formation',fromlist=['employee_capabilities']).employee_capabilities(e))) or '-'}; "
          f"model: {model_text}; active work: {len(governed_works)+len(legacy)}; projects: {', '.join(sorted(project_names)) or '-'}; "
          f"{experience}")
    summaries=[]
    for p in Project.query.filter(Project.status.in_(["PLANNING","ACTIVE","BLOCKED","REVIEW"]),Project.environment=="LIVE").order_by(Project.id):
        governed=contract_api.is_vnext_governed(p)
        if governed:
            works=Work.query.filter_by(project_id=p.id).filter(Work.work_type!="MANAGEMENT").all()
            counts=dict(db.session.query(Work.state,func.count(Work.id)).filter(
              Work.project_id==p.id,Work.work_type!="MANAGEMENT").group_by(Work.state).all())
            recent=[x.title for x in Work.query.filter_by(project_id=p.id,state="ACCEPTED").filter(
              Work.work_type!="MANAGEMENT").order_by(Work.accepted_at.desc()).limit(3)]
            work_summary=(f"works: {len(works)}; ready: {counts.get('READY',0)}; executing: {counts.get('EXECUTING',0)}; "
              f"blocked: {counts.get('WAITING',0)}; verifying: {counts.get('VERIFYING',0)}")
        else:
            counts=dict(db.session.query(Task.status,func.count(Task.id)).filter_by(project_id=p.id).group_by(Task.status).all())
            recent=[x.title for x in Task.query.filter_by(project_id=p.id,status="DONE").order_by(Task.completed_at.desc()).limit(3)]
            work_summary=(f"tasks: {sum(counts.values())}; blocked: {counts.get('BLOCKED',0)}; review: {counts.get('REVIEW',0)}")
        cost=Decimal(db.session.query(func.coalesce(func.sum(CostEvent.real_cost_delta),0)).filter_by(project_id=p.id).scalar())
        contract_read=contract_api.read_projection(p)
        terms=contract_read.get("terms") or {}
        integrity=contract_read.get("integrity_error")
        summaries.append(f"Project #{p.id}: {p.name}; origin: {p.origin}; status: {p.status}; priority: {p.priority}; owner: {p.owner.name}; "
          f"{work_summary}; recent completed: {', '.join(recent) or '-'}; cost: {p.owner.current_model.currency if p.owner.current_model else 'TWD'} {cost}; "
          f"objective: {terms.get('objective') or '-'}; constraints: {', '.join(terms.get('constraints') or []) or '-'}; "
          f"deadline: {terms.get('deadline') or '-'}; governance integrity: {integrity or 'OK'}; "
          f"current state: {p.current_state_summary or '-'}; next milestone: {p.next_milestone or '-'}")
    company=get_company()
    return "ORGANIZATION\n\n"+"\n\n".join(roster)+"\n\nCOMPANY STATUS\n"+("\n".join(summaries) or "No active Projects.")+(
      f"\n\nBUDGET\nlimit: {company.currency} {company.real_budget_limit}; spent: {spent()}; remaining: {remaining()}; "
      f"pending Founder Project proposals: {__import__('eason_one.services.proposal_authority',fromlist=['pending_initial_count']).pending_initial_count()}")

def _validate_tasks(tasks):
    if not isinstance(tasks,list) or not 1<=len(tasks)<=12: raise ValueError("Plan requires 1–12 tasks")
    active={e.slug for e in Employee.query.filter_by(active=True)}
    for item in tasks:
        if not isinstance(item,dict) or set(item)!=TASK_FIELDS: raise ValueError("Invalid task fields")
        for field in {"title","objective","assignee_slug","required_output","acceptance_criteria"}:
            if not isinstance(item[field],str) or not item[field].strip(): raise ValueError(f"Task {field} is required")
        if item["assignee_slug"] not in active: raise ValueError("Unknown or inactive assignee")
        if item["reviewer_slug"] is not None and item["reviewer_slug"] not in active: raise ValueError("Unknown or inactive reviewer")

def validate_plan(payload):
    if isinstance(payload,dict) and payload.get("mode")=="OPERATION_PLAN":
        compact={"mode":payload.get("mode"),"executive_response":payload.get("executive_response"),
          "operation":payload.get("operation")}
        return __import__("eason_one.services.operations",fromlist=["validate_plan"]).validate_plan(compact)
    if not isinstance(payload,dict) or payload.get("mode") not in MODES: raise ValueError("Invalid CEO request mode")
    if not isinstance(payload.get("executive_response"),str) or not payload["executive_response"].strip(): raise ValueError("Missing executive response")
    mode=payload["mode"]
    expected={"mode","executive_response"}
    if mode=="NEW_PROJECT":
        expected|={"project","tasks"}; project=payload.get("project")
        if not isinstance(project,dict) or set(project)!={"name","objective","priority"}: raise ValueError("Invalid project fields")
        if not project["name"].strip() or not project["objective"].strip() or project["priority"] not in PRIORITIES: raise ValueError("Invalid project")
        _validate_tasks(payload.get("tasks"))
    elif mode=="PROJECT_ACTION":
        expected|={"project_id","tasks"}; _validate_tasks(payload.get("tasks"))
        if not db.session.get(Project,payload.get("project_id")): raise ValueError("CEO referenced an unknown Project ID")
    else:
        expected|={"project_id"}
        if payload.get("project_id") is not None and not db.session.get(Project,payload["project_id"]): raise ValueError("CEO referenced an unknown Project ID")
    full={"mode","executive_response","project","project_id","tasks","operation"}
    keys=frozenset(payload)
    if keys not in {frozenset(expected),frozenset(full)}: raise ValueError("CEO response has unexpected fields")
    if keys==frozenset(full):
        if payload.get("operation") is not None: raise ValueError("Only OPERATION_PLAN may define an operation")
        if mode=="NEW_PROJECT" and payload["project_id"] is not None: raise ValueError("NEW_PROJECT cannot reference an existing Project")
        if mode in {"PROJECT_ACTION","STATUS_QUERY","ADVISORY","OPERATION_FOLLOW_UP"} and payload["project"] is not None: raise ValueError("Non-project-creation modes cannot define a new Project")
        if mode in {"STATUS_QUERY","ADVISORY","OPERATION_FOLLOW_UP"} and payload["tasks"]: raise ValueError(f"{mode} cannot propose Tasks")
        if mode=="ADVISORY" and payload["project_id"] is not None: raise ValueError("ADVISORY cannot bind authoritative Project state")
    return payload



def is_company_status_query(request):
    """Return True only for a narrow, read-only whole-company status request.

    Mixed requests such as "review status and prepare a plan" must create a
    governed CEO Run instead of being collapsed into a deterministic snapshot.
    """
    return __import__(
      "eason_one.services.ceo_intent",fromlist=["is_strict_company_status_request"]
    ).is_strict_company_status_request(request)


def _status_stage(operation, tasks):
    if not operation:
        return "NO ACTIVE MISSION"
    if operation.status in {"PLANNED", "WAITING_FOR_FOUNDER"}:
        return "CONTRACT"
    if operation.status == "PAUSED":
        return "ATTENTION"
    if operation.status == "COMPLETED":
        return "DELIVERY"
    if operation.status in {"FAILED", "TERMINATED_BY_FOUNDER"}:
        return "INCIDENT"
    if any(task.status == "BLOCKED" for task in tasks):
        return "BLOCKED"
    if any(task.status == "REVIEW" for task in tasks):
        return "REVIEW"
    if tasks and all(task.status == "DONE" for task in tasks):
        return "INTEGRATION"
    return "EXECUTION" if tasks else "PLANNING"


def company_status_facts():
    """Return the authoritative six-part Founder status report.

    The model never selects Mission IDs, Employee names, incident counts, or
    Founder actions.  Those facts come directly from persisted SQL state.
    """
    view=__import__("eason_one.services.current_company",fromlist=["projection"]).projection()
    operations=list(view["operations"])
    priority_order={"WAITING_FOR_FOUNDER":0,"BLOCKED":1,"ACTIVE":2,"TERMINAL":3}
    operations.sort(
        key=lambda row: (
            priority_order.get(row["classification"], 9),
            -int(getattr(row["operation"], "id", 0)),
        )
    )
    focus=operations[0] if operations else None
    operation=focus["operation"] if focus else None
    tasks=focus["tasks"] if focus else []
    active_task=(focus or {}).get("current_task") if focus else None

    workers=[]
    seen=set()
    for task in view["active_tasks"]:
        employee=task.assigned_employee
        if not employee or employee.id in seen:
            continue
        seen.add(employee.id)
        workers.append({
            "employee_id":employee.id,
            "name":employee.name,
            "status":task.status,
            "current_task":task.title,
            "mission_id":task.operation_id,
        })
    running_meetings=Meeting.query.filter_by(status="RUNNING").order_by(Meeting.id.desc()).all()
    for meeting in running_meetings:
        participants=(db.session.query(MeetingParticipant,Employee)
          .join(Employee,MeetingParticipant.employee_id==Employee.id)
          .filter(MeetingParticipant.meeting_id==meeting.id,
                  MeetingParticipant.removed_at.is_(None)).all())
        for participant,employee in participants:
            if employee.id in seen:
                continue
            seen.add(employee.id)
            workers.append({
                "employee_id":employee.id,
                "name":employee.name,
                "status":"IN_MEETING",
                "current_task":meeting.title,
                "mission_id":meeting.operation_id,
            })

    blocking=[]
    for group in view["failures"]:
        if group.get("blocking"):
            blocking.append({
                "workflow":group["workflow"],
                "failure_type":group["failure_type"],
                "count":group["count"],
                "summary":group["impact"],
            })

    founder_actions=[]
    for item in view["governance"]:
        linked=item.get("operation")
        founder_actions.append({
            "type":item["type"],
            "title":linked.title if linked else "Governed proposal",
            "operation_id":linked.id if linked else None,
        })

    next_result="No result is currently scheduled."
    if focus:
        next_result=(
            getattr(active_task,"required_output",None)
            or getattr(active_task,"title",None)
            or getattr(operation.project,"next_milestone",None)
            or "CEO integration and Founder delivery"
        )

    priority_mission=None
    if focus:
        priority_mission={
            "operation_id":operation.id,
            "project_id":operation.project_id,
            "title":operation.title,
            "status":operation.status,
            "classification":focus["classification"],
            "stage":_status_stage(operation,tasks),
            "done":focus["done"],
            "total":focus["total"],
        }
    return {
        "company":view["company"].name if view.get("company") else "Eason One",
        "priority_mission":priority_mission,
        "employees_working":workers,
        "blocking_incidents":blocking,
        "founder_actions_required":founder_actions,
        "next_expected_result":next_result,
        "generated_from":"persisted company state",
    }


def _validate_status_summary(summary, facts):
    value=" ".join((summary or "").split())
    errors=[]
    if not value:
        errors.append("executive_summary is empty")
    if len(value)>320:
        errors.append("executive_summary exceeds 320 characters")
    if facts.get("priority_mission") and "no mission record" in value.lower():
        errors.append("summary contradicts the persisted priority Mission")
    return value,errors


def _existing_status_report_run(request_id, request):
    request_id=_normalize_founder_request_id(request_id)
    if request_id is None:
        return None
    rows=(AgentRun.query.filter_by(purpose="CEO_STATUS_REPORT")
      .order_by(AgentRun.id.desc()).limit(80).all())
    for run in rows:
        marker=dict((run.context_composition_json or {}).get("status_report_request") or {})
        if marker.get("request_id") != request_id:
            continue
        if str(run.user_request or "") != str(request or ""):
            raise ValueError("STATUS_REPORT_REQUEST_ID_CONFLICT: request text differs from the durable paid request")
        return run
    return None


def _status_report_facts_for_run(run):
    try:
        facts=json.loads(run.context_snapshot or "{}")
    except Exception as exc:
        raise ValueError(f"STATUS_REPORT_SNAPSHOT_INVALID: {exc}") from exc
    if not isinstance(facts,dict):
        raise ValueError("STATUS_REPORT_SNAPSHOT_INVALID: deterministic facts are not an object")
    return facts


def _materialize_status_report(run, facts):
    """Validate/project one already-paid status response without another provider call."""
    if run.status != "SUCCEEDED":
        return run,None
    parsed=dict(run.parsed_output_json or {})
    if parsed.get("status_report") and parsed.get("mode") == "STATUS_QUERY":
        return run,None
    try:
        payload=json.loads(run.raw_output or "{}")
        if set(payload)!={"executive_summary"}:
            raise ValueError("unexpected status report fields")
        summary,errors=_validate_status_summary(payload.get("executive_summary"),facts)
        if errors:
            raise ValueError("; ".join(errors))
        run.structured_validation_status="PASSED"
        run.structured_validation_errors_json=[]
        run.parsed_output_json={
            "mode":"STATUS_QUERY",
            "executive_response":summary,
            "project":None,
            "project_id":(facts.get("priority_mission") or {}).get("project_id"),
            "tasks":[],
            "operation":None,
            "status_report":facts,
        }
        db.session.commit()
        return run,None
    except Exception as exc:
        run.status="FAILED"
        run.failure_reason="STATUS_REPORT_VALIDATION_FAILED"
        run.outcome="FAILED_KNOWN"
        run.failure_stage="POSTPROCESS"
        run.structured_validation_status="FAILED"
        run.structured_validation_errors_json=[str(exc)]
        run.error_text=f"CEO status report validation failed: {exc}"
        db.session.commit()
        return run,None


def founder_status_report(ceo,request,request_id=None):
    durable_request_id=_normalize_founder_request_id(request_id)
    prior=_existing_status_report_run(durable_request_id,request)
    if prior is not None:
        facts=_status_report_facts_for_run(prior)
        if prior.status=="SUCCEEDED":
            return _materialize_status_report(prior,facts)
        if prior.status=="RUNNING" or str(prior.outcome or "")=="FAILED_AMBIGUOUS":
            return prior,None
        # One browser request may own at most one automatic replacement.  A
        # refresh must never become an unbounded retry loop that keeps buying
        # status summaries after repeated known failures.
        if int(prior.attempt_number or 1) >= 2:
            return prior,None
        retry_ok,_=__import__(
          "eason_one.services.external_effects",fromlist=["retry_authorized"]
        ).retry_authorized(prior)
        if not retry_ok:
            return prior,None
        composition=dict(prior.context_composition_json or {})
        marker=dict(composition.get("status_report_request") or {})
        marker["retry_of_run_id"]=prior.id
        composition["status_report_request"]=marker
        run=execute(ceo,"CEO_STATUS_REPORT",request,context_override=prior.context_snapshot,
          context_composition=composition,system_prompt_override=prior.system_prompt_snapshot,
          response_schema=prior.response_schema_snapshot_json or CEO_STATUS_REPORT_SCHEMA,
          max_output_tokens_override=int(prior.effective_max_output_tokens or min(384,ceo.current_model.max_output_tokens)),
          prompt_version="ceo-status-report-v2-retry",retry_of_run=prior)
        if run.status!="SUCCEEDED":
            return run,None
        return _materialize_status_report(run,facts)

    facts=company_status_facts()
    context=json.dumps(facts,ensure_ascii=False,sort_keys=True,separators=(",",":"))
    prompt=ceo.system_instructions+(
      "\nCEO_STATUS_REPORT\nThe context is the complete authoritative company snapshot. "
      "Return only strict JSON with executive_summary. Summarize the operational "
      "meaning in one concise Founder-facing paragraph. Do not search for a Mission "
      "named Eason One. Do not invent IDs, counts, people, blockers, decisions, or "
      "results. The application will render the six factual sections itself.")
    composition={"source":"deterministic_company_snapshot","fields":6}
    if durable_request_id:
        composition["status_report_request"]={"request_id":durable_request_id}
    run=execute(ceo,"CEO_STATUS_REPORT",request,context_override=context,
      context_composition=composition,system_prompt_override=prompt,response_schema=CEO_STATUS_REPORT_SCHEMA,
      max_output_tokens_override=min(384,ceo.current_model.max_output_tokens),prompt_version="ceo-status-report-v2")
    if run.status!="SUCCEEDED":
        return run,None
    return _materialize_status_report(run,facts)


def _current_operation_follow_up(request):
    text=" ".join(request.lower().strip().split())
    starters=("continue","resume","proceed","carry on","keep going")
    references=("current operation","this operation","the operation")
    return text.startswith(starters) and any(item in text for item in references)

def _founder_request_output_limit(ceo, request):
    # Founder planning produces the largest structured authority contract in the
    # product. ModelConfig is already the configured provider/output boundary;
    # do not add a second, stale product-layer ceiling below it.
    return int(ceo.current_model.max_output_tokens)

def _select_real_scope(request):
    """Bind CEO context only when the Founder explicitly refers to real work.

    A new request must never be hijacked by the newest paused validation Mission
    or by an unrelated real Mission. System Validation remains auditable history,
    not current company authority.
    """
    Operation=__import__("eason_one.models",fromlist=["Operation"]).Operation
    candidates=[
      row for row in Operation.query.filter(
        Operation.status.in_(["PLANNED","RUNNING","WAITING_FOR_FOUNDER","PAUSED"])
      ).order_by(Operation.updated_at.desc(),Operation.id.desc()).all()
      if operation_kind(row)==REAL_WORK
      and (
        row.project is None
        or str(row.project.status or "").upper() in {"PLANNING","ACTIVE","BLOCKED","REVIEW","PAUSED"}
      )
    ]
    lowered=" ".join((request or "").lower().split())
    projects=Project.query.filter_by(environment="LIVE").order_by(Project.updated_at.desc()).all()
    current_projects=[
      item for item in projects
      if str(item.status or "").upper() in {"PLANNING","ACTIVE","BLOCKED","REVIEW","PAUSED"}
    ]
    project=next((item for item in projects if item.name and item.name.lower() in lowered),None)
    operation=next((item for item in candidates if item.title and item.title.lower() in lowered),None)
    explicit_project_follow_up=any(phrase in lowered for phrase in (
      "continue project","continue the project","add task to the project",
      "add task and continue project"))
    if not project and explicit_project_follow_up and len(current_projects)==1:
        project=current_projects[0]
    if not operation and project:
        operation=next((item for item in candidates if item.project_id==project.id),None)
    if not operation and _current_operation_follow_up(request):
        operation=candidates[0] if candidates else None
    if not project and operation:
        project=operation.project
    return operation,project

def founder_request(ceo,request,project_id=None,request_id=None):
    routing=__import__(
      "eason_one.services.operation_kernel",fromlist=["route_command"]
    ).route_command(request)
    if is_company_status_query(request):
        return founder_status_report(ceo,request,request_id=request_id)
    route_type=routing["route_type"]
    prompt=ceo.system_instructions+(
      "\nCEO_FOUNDER_REQUEST_V4\nReturn only strict JSON using ADVISORY, "
      "OPERATION_PLAN, OPERATION_FOLLOW_UP, PROJECT_ACTION, or STATUS_QUERY. "
      "The executive_response is the actual CEO reply to the Founder. Make it "
      "sound like a real, direct executive conversation: answer the specific "
      "request, acknowledge uncertainty honestly, and avoid canned headings or "
      "dashboard narration. When the Founder asks for a plan, the reply itself "
      "must give a concise executive summary of the objective, proposed organization, "
      "material risk, and approval decision; do not duplicate Task-by-Task details or "
      "acceptance criteria that already exist in the structured authority fields. Never "
      "reply only that a plan was prepared. A Founder question that asks the company to solve, "
      "research, investigate, analyze, compare, build, or deliver something is "
      "an OPERATION_PLAN, not merely ADVISORY. For OPERATION_PLAN, state who you "
      "recommend using, why, the bounded budget, and whether a Meeting is worth "
      "its cost. The "
      "structured operation fields remain the authority contract. The current "
      "Engineering organization has one Engineer reporting directly to the CEO. "
      "For repository/code work, create one bounded Engineer Task, assign the "
      "Engineer as both accountable assignee and self-validator, set Meeting to "
      "NEVER, and expect the Engineer to produce a precise Codex Job Spec, invoke "
      "the local Codex tool, inspect diff/tests, and retry within approved limits. "
      "Do not assign Engineering Director; that role is inactive. Do not use a "
      "general model to write repository code. Every SOFTWARE_ENGINEERING Task that may modify files must set write_scope to CODEX_WRITE_SCOPE_V1 with the exact repository-relative file paths that Codex may create or modify; do not use directories, wildcards, prose, or a null scope for writable engineering. If the engineering Task is genuinely analysis-only, say that it is read-only explicitly in the Task objective/acceptance criteria and set write_scope to null. Every non-engineering Task must set write_scope to null. The Founder approves these exact paths as part of the structured proposal, so choose the smallest sufficient file set before approval. For every Operation Task, declare exactly one accountable required_capability inside required_capabilities using a concise uppercase tag such as "
      "RESEARCH, SOFTWARE_ENGINEERING, CRITICAL_REVIEW, PRODUCT_STRATEGY, PRODUCT_DESIGN, MARKETING, FINANCE, LEGAL_COMPLIANCE, OPERATIONS, or CONTENT. "
      "If one outcome genuinely needs materially different capabilities, split it into separate single-purpose Tasks instead of stuffing multiple specialties into one Work owner; describe each Task objective clearly enough that Company Runtime can establish the required artifact dependency after approval. If the active roster lacks the required capability, do not invent an Employee ID: assign the closest accountable manager or CEO temporarily and keep the real required_capability explicit; Company Team Formation will create governed staffing before execution. "
      "For non-engineering work, choose the "
      "smallest sufficient set of single-purpose Tasks. When current external facts are material to a later strategy/design/marketing decision, create a separate RESEARCH Task first and let downstream specialist Work consume its accepted Artifact; never ask a non-research Employee to pretend it performed live web research. When the work genuinely contains "
      "independent perspectives or branches that can reduce wall-clock time or improve coverage, "
      "you may assign 2-4 bounded specialist Tasks; do not manufacture extra Tasks merely to use "
      "more agents. Runtime will determine safe dependencies and parallel execution after Founder "
      "approval. The Research Department is a persistent organization, not a transient agent pool. "
      "Its durable provider-family specialists are OpenAI Researcher, Claude Researcher, Gemini Researcher, "
      "and Perplexity Researcher when those Employees are active, all reporting to Research Director. "
      "If the Founder explicitly asks for multiple-AI or multiple-model research, use 2-4 distinct active "
      "model-specialized Researcher Employees for independent RESEARCH Tasks and then one Research Director "
      "RESEARCH synthesis Task that consumes their accepted outputs. Do not create or hire duplicate Researcher "
      "Employees just to obtain another model opinion. If the Founder explicitly names Claude, GPT/OpenAI, Gemini, "
      "or Perplexity, keep accountability with that dedicated Researcher. If the Founder asks the Research Director "
      "or Research Department to choose who should handle ordinary research, assign Research Director; Team Formation "
      "will delegate to one already-capable persistent specialist before execution. Provider API cost is operating spend; "
      "Employee EC compensation and durable memory remain attached to the accountable Researcher. Do not add a reviewer by "
      "default; set reviewer_employee_id to null unless independent review has a "
      "specific material-risk, uncertainty, or compliance purpose. Any budget ceiling stated "
      "by the Founder is a hard boundary. Fit the proposed organization inside it; budget_twd is "
      "an estimate, not authority, and the deterministic Governor will price every stage. Select meeting_config "
      "yourself: NEVER for simple deterministic work, ON_MATERIAL_CONFLICT for "
      "execution where review may disagree, and BEFORE_FINAL_REPORT when the "
      "Founder asks the team to solve a difficult question that benefits from "
      "reconciling specialist views. Keep Meeting limits proportional to the "
      "problem: 1-2 rounds normally, no more than 4 participants, one retry "
      "maximum, and a Meeting budget inside the total Operation budget. Use "
      "OPERATION_FOLLOW_UP only for the current approved Operation. Never "
      "execute new authority before Founder approval and never mutate authority."
      f"\nDETERMINISTIC ROUTING CONTRACT\nSelected path: {route_type}. "
      f"Reason: {routing['reason']} "
      "DIRECT_RESPONSE must return ADVISORY and must not create an Operation. "
      "DETERMINISTIC_ACTION must not invent model work. SINGLE_WORKER must create "
      "exactly one bounded specialist Task and Meeting NEVER. AUTO_DELEGATION means "
      "the Founder delegated the outcome: use validated CEO policy and similar Mission "
      "History to choose one Employee, a bounded multi-Task Project, or a Meeting only "
      "when the extra coordination has a concrete expected benefit. SHORT_MEETING is "
      "allowed only because the Founder explicitly requested cross-role discussion; "
      "keep one round by default and no more than two specialists. FULL_PROJECT may "
      "create multiple Tasks only when the request truly has multiple stages or deliverables. "
      "When the selected path is AUTO_DELEGATION, SINGLE_WORKER, SHORT_MEETING, or FULL_PROJECT, "
      "ADVISORY is not a valid response. Return a complete OPERATION_PLAN with at least one "
      "accountable Employee Task, explicit acceptance criteria, bounded cost, and a Meeting gate. "
      "PROJECT-FIRST CONTRACT: the Founder manages durable Projects, not Missions. If the Founder "
      "is creating a new Project, populate the top-level project object with a short durable Project "
      "name, final outcome, priority, success criteria, relevant constraints, and deadline when known. "
      "The operation is the smallest sufficient execution phase that moves the Project toward its "
      "success criteria. For a small bounded Project, it may cover the full Project; do not split work "
      "artificially just to create more Missions. For a larger Project, cover the next meaningful, "
      "verifiable phase. If the Founder is working on an existing Project, set operation.project_id "
      "to that Project and leave the top-level project null. Keep Project direction stable across "
      "Missions and do not manufacture hierarchy or review steps without concrete expected benefit.")
    operation,project=_select_real_scope(request)
    forced_project = db.session.get(Project, int(project_id)) if project_id else None
    if project_id and not forced_project:
        raise ValueError("Project not found")
    if forced_project:
        project = forced_project
        scoped = [item for item in Operation.query.filter_by(project_id=project.id).order_by(Operation.id.desc()).all()
                  if item.status in {"RUNNING","WAITING_FOR_FOUNDER","PLANNED"}]
        operation = scoped[0] if scoped else None
    durable_request_id=_normalize_founder_request_id(request_id)
    prior_run=_existing_founder_request_run(
      durable_request_id,request,getattr(forced_project,"id",None)
    )
    if prior_run is not None and prior_run.status=="SUCCEEDED":
        if prior_run.operation_id:
            linked=db.session.get(Operation,prior_run.operation_id)
            if linked is None:
                raise ValueError("FOUNDER_REQUEST_RECOVERY_INCONSISTENT: linked Operation is missing")
            return prior_run,linked
        prior_payload=prior_run.parsed_output_json or {}
        # A fully materialized non-execution answer is already complete.  An
        # OPERATION_PLAN without operation_id is deliberately allowed through:
        # this is the exact crash window after paid provider persistence and
        # before deterministic proposal materialization.
        if prior_payload.get("mode") in {"STATUS_QUERY","ADVISORY","OPERATION_FOLLOW_UP"}:
            return prior_run,None
    composed=__import__("eason_one.services.ceo_context",fromlist=["compose"]).compose(
      ceo,founder_request=request,operation=operation,project=project)
    dialogue=ceo_conversation_context(limit=8)
    composed=type(composed)(
      text=composed.text + "\n\nRECENT FOUNDER/CEO DIALOGUE\n" + dialogue,
      composition={
        **composed.composition,
        "ceo_dialogue":{"messages":len(recent_ceo_conversation(8))},
        **({
          "founder_request_id":durable_request_id,
          "founder_request_project_id":getattr(forced_project,"id",None),
          "founder_request_route_type":route_type,
        } if durable_request_id else {}),
      },
      character_budget=composed.character_budget,
    )
    # CEO_FOUNDER_REQUEST can produce the largest structured contract in V1.
    # Keep the limit bounded by ModelConfig, but do not impose the old 512-token
    # meeting-style ceiling on an Operation plan.
    output_limit=_founder_request_output_limit(ceo,request)
    execution_required=route_type in EXECUTION_ROUTES
    response_schema=CEO_EXECUTION_SCHEMA if execution_required else CEO_SCHEMA
    run=prior_run or execute(ceo,"CEO_FOUNDER_REQUEST",request,context_override=composed.text,
      context_composition=composed.composition,system_prompt_override=prompt,
      response_schema=response_schema,max_output_tokens_override=output_limit,
      prompt_version="ceo_founder_request-v5")
    if (run.status!="SUCCEEDED" and run.failure_reason=="OUTPUT_TRUNCATED"
        and int(run.attempt_number or 1) < 2):
        # OUTPUT_TRUNCATED is a known, bounded provider response, not Founder
        # authority. Keep the paid first attempt as audit evidence and let the
        # Company own one same-provider recovery inside the original request.
        # The retry uses the same configured output ceiling but a materially
        # tighter response contract so it can fit without inventing more budget
        # or silently changing models.
        recovery_prompt=prompt+(
          "\nCEO_FOUNDER_REQUEST_TRUNCATION_RECOVERY\n"
          "The immediately prior planning attempt hit the configured output ceiling. "
          "Return the same complete authority contract in materially more compact form. "
          "Use the shortest sufficient strings. Keep executive_response to 2-4 short sentences. "
          "Do not repeat acceptance criteria in prose. Prefer 1-4 single-purpose Tasks unless "
          "the Founder request truly requires more. Each acceptance criterion must be one concise "
          "testable sentence. Preserve every required JSON field, exact budget boundary, capability, "
          "reviewer decision, meeting gate, and exact engineering write_scope. Do not omit authority "
          "fields merely to save tokens."
        )
        recovery_composition={
          **composed.composition,
          "company_recovery":{
            "reason":"OUTPUT_TRUNCATED",
            "prior_run_id":run.id,
            "policy":"ONE_SAME_PROVIDER_COMPACT_RETRY",
            "max_attempts":2,
          },
        }
        run=execute(ceo,"CEO_FOUNDER_REQUEST",request,context_override=composed.text,
          context_composition=recovery_composition,system_prompt_override=recovery_prompt,
          response_schema=response_schema,max_output_tokens_override=output_limit,
          prompt_version="ceo_founder_request-v5-truncation-recovery",
          retry_of_run=run)
    if run.status!="SUCCEEDED": return run,None
    try:
        raw_plan=json.loads(run.raw_output)
        warnings=[]
        if forced_project and isinstance(raw_plan, dict) and raw_plan.get("mode") == "OPERATION_PLAN":
            # Project scope selected by the Founder/UI is authoritative. The model
            # may plan the work, but it may not silently create or switch Projects.
            raw_plan["project"] = None
            raw_plan["project_id"] = forced_project.id
            operation_data = dict(raw_plan.get("operation") or {})
            operation_data["project_id"] = forced_project.id
            raw_plan["operation"] = operation_data
        if execution_required and (
            not isinstance(raw_plan,dict)
            or raw_plan.get("mode")!="OPERATION_PLAN"
            or not isinstance(raw_plan.get("operation"),dict)
        ):
            raise ValueError(
                "Founder delegated an outcome, but the CEO returned no executable Operation proposal"
            )
        if isinstance(raw_plan,dict) and raw_plan.get("mode")=="OPERATION_PLAN":
            raw_plan, research_notes = __import__(
                "eason_one.services.research_department", fromlist=["normalize_ceo_plan"]
            ).normalize_ceo_plan(raw_plan, request)
            warnings.extend(research_notes)
        if isinstance(raw_plan,dict) and raw_plan.get("mode")=="OPERATION_PLAN":
            operation_data=raw_plan.get("operation") or {}
            if route_type in {"DIRECT_RESPONSE", "DETERMINISTIC_ACTION"}:
                raw_plan={
                    "mode":"ADVISORY",
                    "executive_response":raw_plan.get("executive_response") or (
                        "This request does not require a new Mission or model-driven execution."),
                    "project_id":None,
                }
                operation_data={}
            elif route_type=="SINGLE_WORKER":
                tasks=list(operation_data.get("tasks") or [])
                operation_data["tasks"]=tasks[:1]
                operation_data["meeting_policy"]="NEVER"
                config=dict(operation_data.get("meeting_config") or {})
                config.update({
                    "trigger":"NEVER", "participant_employee_ids":[],
                    "max_rounds":1, "max_speakers_per_round":1,
                    "contribution_output_cap":192, "token_limit":6000,
                    "budget_twd":0, "retry_limit":0,
                })
                operation_data["meeting_config"]=config
            elif route_type=="SHORT_MEETING":
                operation_data["meeting_policy"]="REQUIRED_ON_MATERIAL_CONFLICT"
                config=dict(operation_data.get("meeting_config") or {})
                config["trigger"]="BEFORE_FINAL_REPORT"
                config["max_rounds"]=min(1,int(config.get("max_rounds") or 1))
                config["max_speakers_per_round"]=min(2,int(config.get("max_speakers_per_round") or 2))
                config["retry_limit"]=0
                operation_data["meeting_config"]=config
            elif route_type=="AUTO_DELEGATION":
                config=dict(operation_data.get("meeting_config") or {})
                tasks=list(operation_data.get("tasks") or [])
                trigger=config.get("trigger") or "NEVER"
                if len(tasks)>1:
                    route_type="FULL_PROJECT"
                elif trigger!="NEVER":
                    route_type="SHORT_MEETING"
                else:
                    route_type="SINGLE_WORKER"
                routing["reason"]=(
                    f"CEO selected {route_type} as the smallest sufficient organization "
                    f"under active policy; tasks={len(tasks)}, meeting_trigger={trigger}."
                )
            trigger=(operation_data.get("meeting_config") or {}).get("trigger")
            if trigger in {"NEVER","ON_MATERIAL_CONFLICT","BEFORE_FINAL_REPORT"}:
                canonical={
                    "NEVER":"NEVER",
                    "ON_MATERIAL_CONFLICT":"REQUIRED_ON_MATERIAL_CONFLICT",
                    "BEFORE_FINAL_REPORT":"REQUIRED_ON_MATERIAL_CONFLICT",
                }[trigger]
                if operation_data.get("meeting_policy") != canonical:
                    operation_data["meeting_policy"] = canonical
            response=" ".join(str(raw_plan.get("executive_response") or "").split())
            dangling_words=(" and", " or", " with", " including", " owners and", " remains or")
            visibly_incomplete=(
                bool(response)
                and (response[-1:] not in ".?!。！？" or response.casefold().endswith(dangling_words))
            )
            if visibly_incomplete:
                # Do not spend another Provider call merely to repair presentation.
                # Keep only complete sentences, then close from the validated
                # structured authority contract.
                cut=max(response.rfind("."), response.rfind("?"), response.rfind("!"))
                prefix=response[:cut+1].strip() if cut>=0 else response.rstrip(" ,;:")
                closure=(
                    "The structured proposal below is the complete authority contract, "
                    "including Tasks, reviewers, acceptance criteria, risks, budget, "
                    "and implementation prerequisites."
                )
                raw_plan["executive_response"]=(prefix+" "+closure).strip()
                warnings.append("executive_response ended mid-sentence and was closed deterministically")
        plan=validate_plan(raw_plan)
        run.parsed_output_json=plan
        run.structured_validation_status="PASSED"
        run.structured_validation_warnings_json=warnings
        run.structured_validation_errors_json=[]
        active_follow_up=(
          operation and operation.status=="RUNNING"
          and (plan["mode"]=="OPERATION_FOLLOW_UP"
               or _current_operation_follow_up(request)))
        if active_follow_up:
            memory=dict(operation.memory_json or {})
            followups=list(memory.get("founder_followups") or [])
            followups.append({"instruction":request,
              "ceo_response":"I will continue the current approved Operation."})
            memory["founder_followups"]=followups[-6:]
            operation.memory_json=memory
            run.parsed_output_json={"mode":"OPERATION_FOLLOW_UP",
              "executive_response":"I will continue the current approved Operation.",
              "project":None,"project_id":operation.project_id,
              "tasks":[],"operation":None}
            db.session.commit()
            return run,operation
        if plan["mode"]=="OPERATION_FOLLOW_UP":
            raise ValueError("No RUNNING Operation is available to continue")
        if plan["mode"] in {"STATUS_QUERY","ADVISORY"}: db.session.commit(); return run,None
        if plan["mode"]=="OPERATION_PLAN":
            operation=__import__("eason_one.services.operations",fromlist=["propose_operation"]).propose_operation(
                ceo,plan,route_type=route_type,route_reason=routing["reason"],
                founder_request=request,
            )
            # Keep durable Project identity separate from the first internal Mission.
            # The operations validator intentionally consumes only the authority
            # contract, so persist the already schema-validated Project proposal
            # in Operation memory until Founder approval materializes it.
            project_spec = raw_plan.get("project") if isinstance(raw_plan, dict) else None
            if operation.project_id is None and isinstance(project_spec, dict):
                memory = dict(operation.memory_json or {})
                # propose_operation already compiled Founder execution authority
                # into durable Project constraint rows. Preserve those rows while
                # adding the CEO's schema-validated Project metadata; never let a
                # later presentation-normalization step erase hard authority.
                durable_spec = dict(memory.get("new_project_spec") or {})
                constraints = clean_rows(project_spec.get("constraints"))
                for row in clean_rows(durable_spec.get("constraints")):
                    normalized = " ".join(str(row or "").casefold().split())
                    if not any(" ".join(str(existing or "").casefold().split()) == normalized for existing in constraints):
                        constraints.append(row)
                memory["new_project_spec"] = {
                    "name": clean_text(project_spec.get("name") or durable_spec.get("name") or operation.title, multiline=False)[:160],
                    "objective": clean_text(project_spec.get("objective") or durable_spec.get("objective") or operation.objective, multiline=False),
                    "priority": clean_text(project_spec.get("priority") or durable_spec.get("priority") or "HIGH", multiline=False).upper(),
                    "success_criteria": clean_rows(project_spec.get("success_criteria") or durable_spec.get("success_criteria"))[:8],
                    # Founder authority is not a display list; do not truncate it.
                    "constraints": constraints,
                    "deadline": project_spec.get("deadline") or durable_spec.get("deadline"),
                }
                operation.memory_json = memory
                # One deterministic authority compiler owns the Founder-visible
                # Project envelope and first-Mission allocation. This runs only
                # before approval, makes no provider/tool calls, and guarantees
                # that the approval surface, materialized Project, and immutable
                # Project Contract all use the same budget truth. When the
                # Founder did not type a numeric cap, it also reserves exactly
                # one bounded continuation/recovery move plus continuation
                # planning headroom instead of silently making Project autonomy
                # impossible after Mission #1.
                __import__(
                    "eason_one.services.operations",
                    fromlist=["reconcile_pending_proposal_authority"],
                ).reconcile_pending_proposal_authority(operation)
            # The Provider Run predates the governed Operation by design. Once the
            # validated proposal is materialized, back-link the immutable Run so
            # Founder audit surfaces can traverse Run -> Operation without
            # charging the pre-approval planning call against execution budget.
            run.operation_id=operation.id
            if operation.project_id and not run.project_id:
                run.project_id=operation.project_id
            if operation.project_id:
                CostEvent.query.filter_by(agent_run_id=run.id).update(
                    {"project_id": operation.project_id}, synchronize_session=False
                )
            db.session.commit()
            return run,operation
        # Pre-v0.20 NEW_PROJECT / PROJECT_ACTION shapes are retained only as
        # historical provider evidence. They cannot become Founder authority or
        # materialize Project/Task state.
        proposal=Proposal(project_id=plan.get("project_id"),agent_run_id=run.id,proposed_by_employee_id=ceo.id,
          payload_json={"type":"PROJECT_PLAN","plan":plan,"historical_only":True},status="SUPERSEDED",
          review_note="Historical PROJECT_PLAN shape retired by v0.20 canonical Project authority.",reviewed_at=now())
        db.session.add(proposal); db.session.commit(); return run,None
    except Exception as exc:
        # Provider transport success is not Mission success. A structurally or
        # semantically unusable CEO reply must be auditable as FAILED, otherwise
        # Headquarters displays a false Validation PASSED as Run #80 did.
        run.status="FAILED"
        run.failure_reason="STRUCTURED_OUTPUT_INVALID"
        run.structured_validation_status="FAILED"
        run.structured_validation_errors_json=[str(exc)]
        run.error_text=f"CEO plan validation failed: {exc}"
        db.session.commit()
        return run,None

def _add_tasks(project,plan,owner,fault_after_task=None):
    for index,item in enumerate(plan["tasks"]):
        assignee=Employee.query.filter_by(slug=item["assignee_slug"],active=True).one()
        reviewer=Employee.query.filter_by(slug=item["reviewer_slug"],active=True).one() if item["reviewer_slug"] else None
        db.session.add(Task(project_id=project.id,title=item["title"],objective=item["objective"],status="ASSIGNED",
          priority=plan.get("project",{}).get("priority",project.priority),created_by_employee_id=owner.id,assigned_employee_id=assignee.id,
          reviewer_employee_id=getattr(reviewer,"id",None),required_output=item["required_output"],acceptance_criteria=item["acceptance_criteria"]))
        db.session.flush()
        if fault_after_task is not None and index==fault_after_task: raise RuntimeError("Injected materialization failure")

def materialize_project_plan(proposal, fault_after_task=None):
    """Historical PROJECT_PLAN materialization is permanently retired.

    Current Project creation authority is exclusively:
    CEO OPERATION_PLAN -> Founder approval -> Project Contract -> Work.
    Old Proposal rows remain readable audit evidence only.
    """
    raise ValueError("PROJECT_PLAN_MATERIALIZER_RETIRED")

SYNTHESIS_FIELDS={"executive_summary","result","key_findings","disagreements_or_risks","unresolved_questions","founder_decisions_required","recommended_next_actions"}


def _materialize_legacy_project_briefing(ceo,project,run):
    if run.status != "SUCCEEDED":
        return run
    try:
        payload=dict(run.parsed_output_json or json.loads(run.raw_output or "{}"))
        if set(payload)!=SYNTHESIS_FIELDS or not all(
            isinstance(payload[x],str) if x in {"executive_summary","result"} else isinstance(payload[x],list)
            for x in SYNTHESIS_FIELDS
        ):
            raise ValueError("Invalid CEO briefing")
        run.parsed_output_json=payload
        run.structured_validation_status="PASSED"
        run.structured_validation_errors_json=[]
        existing=WorkMessage.query.filter_by(
          project_id=project.id,agent_run_id=run.id,message_type="REPORT"
        ).order_by(WorkMessage.id).first()
        if existing is None:
            db.session.add(WorkMessage(
              project_id=project.id,sender_employee_id=ceo.id,message_type="REPORT",
              content=payload["executive_summary"],agent_run_id=run.id))
        db.session.commit()
        return run
    except Exception as exc:
        run.status="FAILED"
        run.outcome="FAILED_KNOWN"
        run.failure_reason="STRUCTURED_OUTPUT_INVALID"
        run.failure_stage="POSTPROCESS"
        run.structured_validation_status="FAILED"
        run.structured_validation_errors_json=[str(exc)]
        run.error_text=f"Briefing validation failed: {exc}"
        db.session.commit()
        return run


def generate_project_briefing(ceo,project):
    contracts = __import__(
      "eason_one.services.project_contract", fromlist=["is_vnext_governed"]
    )
    if contracts.is_vnext_governed(project):
        raise ValueError("GOVERNED_PROJECT_BRIEFING_REQUIRES_WORK_AUTHORITY")
    tasks=Task.query.filter_by(project_id=project.id).order_by(Task.id).all()
    messages=WorkMessage.query.filter_by(project_id=project.id).order_by(WorkMessage.created_at).all()
    knowledge=current(project.id)
    contributions=db.session.query(Employee.name,func.sum(ContributionEvent.value)).join(Employee).filter(
      ContributionEvent.project_id==project.id,ContributionEvent.scope=="PROJECT").group_by(Employee.name).all()
    project_objective = project.objective
    context=f"PROJECT\n#{project.id} {project.name}; status {project.status}; objective: {project_objective}\n\nTASK RESULTS\n"+(
      "\n".join(f"{t.status} {t.title}: {t.result_summary or 'unresolved'}" for t in tasks))
    context+="\n\nREVIEWS / WORK MESSAGES\n"+"\n".join(m.content for m in messages)
    context+="\n\nEFFECTIVE BRAIN\n"+"\n".join(f"{k.kind}: {k.title} — {k.content}" for k in knowledge)
    context+="\n\nPROJECT CONTRIBUTION\n"+"\n".join(f"{n}: {v}" for n,v in contributions)
    context+=f"\n\nPROJECT COST\n{db.session.query(func.coalesce(func.sum(CostEvent.real_cost_delta),0)).filter_by(project_id=project.id).scalar()}"
    prompt=ceo.system_instructions+"\nCEO_PROJECT_SYNTHESIS\nReturn only strict JSON briefing fields."
    user_request=f"Synthesize Project #{project.id}"
    context_hash=hashlib.sha256(context.encode("utf-8")).hexdigest()
    prompt_hash=hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    attempts=(AgentRun.query.filter_by(project_id=project.id,purpose="CEO_PROJECT_SYNTHESIS")
      .filter(AgentRun.user_request==user_request,AgentRun.context_hash==context_hash,AgentRun.prompt_hash==prompt_hash)
      .order_by(AgentRun.id).all())
    latest=attempts[-1] if attempts else None
    if latest is not None:
        if latest.status=="SUCCEEDED":
            return _materialize_legacy_project_briefing(ceo,project,latest)
        if latest.status=="RUNNING" or str(latest.outcome or "")=="FAILED_AMBIGUOUS":
            return latest
        if len(attempts)>=2:
            return latest
        retry_ok,_=__import__(
          "eason_one.services.external_effects",fromlist=["retry_authorized"]
        ).retry_authorized(latest)
        if not retry_ok:
            return latest
        run=execute(ceo,"CEO_PROJECT_SYNTHESIS",user_request,project=project,
          context_override=context,system_prompt_override=prompt,response_schema=SYNTHESIS_SCHEMA,
          prompt_version="legacy-project-briefing-v2-retry",retry_of_run=latest)
    else:
        run=execute(ceo,"CEO_PROJECT_SYNTHESIS",user_request,project=project,
          context_override=context,system_prompt_override=prompt,response_schema=SYNTHESIS_SCHEMA,
          prompt_version="legacy-project-briefing-v2")
    if run.status!="SUCCEEDED":
        return run
    return _materialize_legacy_project_briefing(ceo,project,run)
