import json
from decimal import Decimal
from sqlalchemy import func
from ..extensions import db
from ..models import Proposal,Employee,Project,Task,CostEvent,WorkMessage,ContributionEvent,now
from .execution import execute
from .company import get_company,spent,remaining
from .brain import current
from ..schemas import CEO_SCHEMA,SYNTHESIS_SCHEMA

MODES={"NEW_PROJECT","PROJECT_ACTION","STATUS_QUERY","ADVISORY","OPERATION_PLAN",
  "OPERATION_FOLLOW_UP"}
PRIORITIES={"LOW","MEDIUM","HIGH","CRITICAL"}
TASK_FIELDS={"title","objective","assignee_slug","reviewer_slug","required_output","acceptance_criteria"}

def operating_context():
    roster=[]
    for e in Employee.query.filter_by(active=True).order_by(Employee.id):
        active=(Task.query.join(Project,Task.project_id==Project.id)
          .filter(Task.assigned_employee_id==e.id,Project.environment=="LIVE",
            Task.status.in_(["ASSIGNED","WORKING","BLOCKED","REVIEW"])).all())
        names=sorted({t.project.name for t in active})
        model_text=(f"{e.current_model.label} / {e.current_model.model_name}" if e.current_model
          else "UNASSIGNED / execution unavailable")
        roster.append(f"{e.name}\nID: {e.id}; slug: {e.slug}; department: {e.department.name if e.department else 'CEO Office / Assurance'}; "
          f"position: {e.position.name} / level {e.position.level}; manager: {e.manager.name if e.manager else 'Founder'}; "
          f"role: {e.role_description}; model: {model_text}; active tasks: {len(active)}; projects: {', '.join(names) or '-'}")
    summaries=[]
    for p in Project.query.filter(Project.status.in_(["PLANNING","ACTIVE","BLOCKED","REVIEW"]),Project.environment=="LIVE").order_by(Project.id):
        counts=dict(db.session.query(Task.status,func.count(Task.id)).filter_by(project_id=p.id).group_by(Task.status).all())
        recent=[x.title for x in Task.query.filter_by(project_id=p.id,status="DONE").order_by(Task.completed_at.desc()).limit(3)]
        cost=Decimal(db.session.query(func.coalesce(func.sum(CostEvent.real_cost_delta),0)).filter_by(project_id=p.id).scalar())
        summaries.append(f"Project #{p.id}: {p.name}; origin: {p.origin}; status: {p.status}; priority: {p.priority}; owner: {p.owner.name}; "
          f"tasks: {sum(counts.values())}; blocked: {counts.get('BLOCKED',0)}; review: {counts.get('REVIEW',0)}; "
          f"recent completed: {', '.join(recent) or '-'}; cost: {p.owner.current_model.currency} {cost}; "
          f"current state: {p.current_state_summary or '-'}; constraints: {p.known_constraints or '-'}; next milestone: {p.next_milestone or '-'}")
    company=get_company()
    return "ORGANIZATION\n\n"+"\n\n".join(roster)+"\n\nCOMPANY STATUS\n"+("\n".join(summaries) or "No active Projects.")+(
      f"\n\nBUDGET\nlimit: {company.currency} {company.real_budget_limit}; spent: {spent()}; remaining: {remaining()}; "
      f"pending Founder Proposals: {Proposal.query.filter_by(status='PENDING').count()}")

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


def _current_operation_follow_up(request):
    text=" ".join(request.lower().strip().split())
    starters=("continue","resume","proceed","carry on","keep going")
    references=("current operation","this operation","the operation")
    return text.startswith(starters) and any(item in text for item in references)

def founder_request(ceo,request):
    prompt=ceo.system_instructions+"\nCEO_FOUNDER_REQUEST\nReturn only strict JSON using ADVISORY, OPERATION_PLAN, OPERATION_FOLLOW_UP, PROJECT_ACTION, or STATUS_QUERY. Use OPERATION_PLAN for a genuinely new internal objective. Use OPERATION_FOLLOW_UP when the Founder asks to continue the current approved Operation. Never execute new authority before Founder approval. Never mutate authority."
    operation=__import__("eason_one.models",fromlist=["Operation"]).Operation.query.filter(
      __import__("eason_one.models",fromlist=["Operation"]).Operation.status.in_(
        ["PLANNED","RUNNING","WAITING_FOR_FOUNDER","PAUSED"])).order_by(
        __import__("eason_one.models",fromlist=["Operation"]).Operation.updated_at.desc()).first()
    projects=Project.query.filter_by(environment="LIVE").order_by(Project.updated_at.desc()).all()
    lowered=request.lower()
    project=next((item for item in projects if item.name.lower() in lowered),None)
    if not project and operation: project=operation.project
    if not project and len(projects)==1: project=projects[0]
    composed=__import__("eason_one.services.ceo_context",fromlist=["compose"]).compose(
      ceo,founder_request=request,operation=operation,project=project)
    # CEO_FOUNDER_REQUEST can produce the largest structured contract in V1.
    # Keep the limit bounded by ModelConfig, but do not impose the old 512-token
    # meeting-style ceiling on an Operation plan.
    output_limit=min(2048,ceo.current_model.max_output_tokens)
    run=execute(ceo,"CEO_FOUNDER_REQUEST",request,context_override=composed.text,
      context_composition=composed.composition,system_prompt_override=prompt,
      response_schema=CEO_SCHEMA,max_output_tokens_override=output_limit)
    if run.status!="SUCCEEDED": return run,None
    try:
        plan=validate_plan(json.loads(run.raw_output)); run.parsed_output_json=plan
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
            operation=__import__("eason_one.services.operations",fromlist=["propose_operation"]).propose_operation(ceo,plan)
            return run,operation
        proposal=Proposal(project_id=plan.get("project_id"),agent_run_id=run.id,proposed_by_employee_id=ceo.id,
          payload_json={"type":"PROJECT_PLAN","plan":plan},status="PENDING")
        db.session.add(proposal); db.session.commit(); return run,proposal
    except Exception as exc:
        run.error_text=f"CEO plan validation failed: {exc}"; db.session.commit(); return run,None

def _add_tasks(project,plan,owner,fault_after_task=None):
    for index,item in enumerate(plan["tasks"]):
        assignee=Employee.query.filter_by(slug=item["assignee_slug"],active=True).one()
        reviewer=Employee.query.filter_by(slug=item["reviewer_slug"],active=True).one() if item["reviewer_slug"] else None
        db.session.add(Task(project_id=project.id,title=item["title"],objective=item["objective"],status="ASSIGNED",
          priority=plan.get("project",{}).get("priority",project.priority),created_by_employee_id=owner.id,assigned_employee_id=assignee.id,
          reviewer_employee_id=getattr(reviewer,"id",None),required_output=item["required_output"],acceptance_criteria=item["acceptance_criteria"]))
        db.session.flush()
        if fault_after_task is not None and index==fault_after_task: raise RuntimeError("Injected materialization failure")

def materialize_project_plan(proposal,fault_after_task=None):
    if proposal.status!="PENDING" or proposal.payload_json.get("type")!="PROJECT_PLAN": raise ValueError("Proposal is not a pending project plan")
    try:
        plan=validate_plan(proposal.payload_json["plan"]); owner=db.session.get(Employee,proposal.proposed_by_employee_id)
        if plan["mode"]=="NEW_PROJECT":
            project=Project(name=plan["project"]["name"],objective=plan["project"]["objective"],priority=plan["project"]["priority"],status="ACTIVE",owner_employee_id=owner.id)
            db.session.add(project); db.session.flush()
        else: project=db.session.get(Project,plan["project_id"])
        _add_tasks(project,plan,owner,fault_after_task)
        proposal.status="APPROVED"; proposal.review_note="Founder materialized validated CEO plan"; proposal.reviewed_at=now()
        db.session.commit(); return project
    except Exception: db.session.rollback(); raise

SYNTHESIS_FIELDS={"executive_summary","result","key_findings","disagreements_or_risks","unresolved_questions","founder_decisions_required","recommended_next_actions"}
def generate_project_briefing(ceo,project):
    tasks=Task.query.filter_by(project_id=project.id).order_by(Task.id).all()
    messages=WorkMessage.query.filter_by(project_id=project.id).order_by(WorkMessage.created_at).all()
    knowledge=current(project.id)
    contributions=db.session.query(Employee.name,func.sum(ContributionEvent.value)).join(Employee).filter(
      ContributionEvent.project_id==project.id,ContributionEvent.scope=="PROJECT").group_by(Employee.name).all()
    context=f"PROJECT\n#{project.id} {project.name}; status {project.status}; objective: {project.objective}\n\nTASK RESULTS\n"+(
      "\n".join(f"{t.status} {t.title}: {t.result_summary or 'unresolved'}" for t in tasks))
    context+="\n\nREVIEWS / WORK MESSAGES\n"+"\n".join(m.content for m in messages)
    context+="\n\nEFFECTIVE BRAIN\n"+"\n".join(f"{k.kind}: {k.title} — {k.content}" for k in knowledge)
    context+="\n\nPROJECT CONTRIBUTION\n"+"\n".join(f"{n}: {v}" for n,v in contributions)
    context+=f"\n\nPROJECT COST\n{db.session.query(func.coalesce(func.sum(CostEvent.real_cost_delta),0)).filter_by(project_id=project.id).scalar()}"
    prompt=ceo.system_instructions+"\nCEO_PROJECT_SYNTHESIS\nReturn only strict JSON briefing fields."
    run=execute(ceo,"CEO_PROJECT_SYNTHESIS",f"Synthesize Project #{project.id}",project=project,context_override=context,system_prompt_override=prompt,response_schema=SYNTHESIS_SCHEMA)
    if run.status!="SUCCEEDED": return run
    try:
        payload=json.loads(run.raw_output)
        if set(payload)!=SYNTHESIS_FIELDS or not all(isinstance(payload[x],str) if x in {"executive_summary","result"} else isinstance(payload[x],list) for x in SYNTHESIS_FIELDS): raise ValueError("Invalid CEO briefing")
        run.parsed_output_json=payload
        db.session.add(WorkMessage(project_id=project.id,sender_employee_id=ceo.id,message_type="REPORT",
          content=payload["executive_summary"],agent_run_id=run.id)); db.session.commit(); return run
    except Exception as exc:
        run.error_text=f"Briefing validation failed: {exc}"; db.session.commit(); return run
