from decimal import Decimal
from flask import Blueprint, render_template, request, redirect, url_for, flash, abort, session
from sqlalchemy import func
from .extensions import db
from .models import *
from .services.company import get_company, spent, remaining
from .services.projects import create_project,classify
from .services.tasks import create_task, transition, review
from .services.execution import execute
from .services.ceo import founder_request, materialize_project_plan,generate_project_briefing
from .services.interviews import start, ask
from .services.contributions import total, project_totals
from .services.learning import create as create_learning
from .services.reviews import run_review
from .services.brain import add_knowledge,active_hypotheses,current
from .services.approvals import review_proposal
from .services.employees import change_model
from .services.task_execution import run_task
from .services import model_configs as model_config_service
from .services.costs import conservative_estimate
from .services import meetings as meeting_service

bp=Blueprint("main",__name__)
@bp.route("/")
def index(): return redirect(url_for("main.ceo"))
@bp.route("/language/<language>",methods=["POST"])
def language(language):
    if language not in {"en","zh-TW"}: abort(400)
    session["language"]=language
    return redirect(request.form.get("next") or request.referrer or url_for("main.ceo"))

@bp.route("/ceo",methods=["GET","POST"])
def ceo():
    company=get_company()
    if not company: return render_template("unseeded.html")
    if request.method=="POST":
        try:
            run,proposal=founder_request(Employee.query.filter_by(slug="ceo").one(),request.form["request"])
            if proposal: flash(run.parsed_output_json["executive_response"]+" Plan is waiting in Founder Inbox.","ok")
            elif run.parsed_output_json and run.parsed_output_json.get("mode")=="STATUS_QUERY": flash(run.parsed_output_json["executive_response"],"ok")
            else: flash("CEO output failed validation; no proposal was created.","error")
            return redirect(url_for("main.ceo"))
        except Exception as e: flash(str(e),"error")
    live_ids=Project.query.with_entities(Project.id).filter_by(environment="LIVE")
    metrics={"active_projects":Project.query.filter(Project.environment=="LIVE",Project.status.in_(["PLANNING","ACTIVE","BLOCKED","REVIEW"])).count(),
      "active_tasks":Task.query.filter(Task.project_id.in_(live_ids),Task.status.in_(["ASSIGNED","WORKING","REVIEW"])).count(),
      "blocked":Task.query.filter(Task.project_id.in_(live_ids),Task.status=="BLOCKED").count(),
      "pending":Proposal.query.filter_by(status="PENDING").count()}
    projects=Project.query.filter_by(environment="LIVE").order_by(Project.updated_at.desc()).all()
    recent=Task.query.filter(Task.project_id.in_(live_ids),Task.status=="DONE").order_by(Task.completed_at.desc()).limit(6).all()
    latest_ceo=AgentRun.query.filter_by(purpose="CEO_FOUNDER_REQUEST",status="SUCCEEDED").order_by(AgentRun.started_at.desc()).first()
    return render_template("ceo.html",company=company,spent=spent(),remaining=remaining(),metrics=metrics,projects=projects,recent=recent,latest_ceo=latest_ceo)

@bp.route("/projects")
def projects(): return render_template("projects.html",projects=Project.query.order_by(Project.updated_at.desc()).all(),employees=Employee.query.filter_by(active=True).all())
@bp.route("/projects/existing",methods=["POST"])
def existing_project():
    try:
        create_project(request.form["name"],request.form["objective"],Employee.query.get_or_404(request.form["owner_id"]),
          priority=request.form["priority"],status=request.form["status"],environment="LIVE",origin="EXISTING",
          current_state_summary=request.form["current_state_summary"],known_constraints=request.form.get("known_constraints"),
          next_milestone=request.form.get("next_milestone"))
    except Exception as ex: flash(str(ex),"error")
    return redirect(url_for("main.projects"))
@bp.route("/projects/<int:id>/classification",methods=["POST"])
def project_classification(id):
    try: classify(Project.query.get_or_404(id),request.form["environment"])
    except Exception as ex: flash(str(ex),"error")
    return redirect(request.referrer or url_for("main.projects"))
@bp.route("/projects/new",methods=["POST"])
def project_new():
    p=create_project(request.form["name"],request.form["objective"],Employee.query.get_or_404(request.form["owner_id"]),status="ACTIVE")
    return redirect(url_for("main.project_detail",id=p.id))
@bp.route("/projects/<int:id>")
def project_detail(id):
    p=Project.query.get_or_404(id)
    counts=dict(db.session.query(Task.status,func.count(Task.id)).filter_by(project_id=id).group_by(Task.status).all())
    costs=Decimal(db.session.query(func.coalesce(func.sum(CostEvent.real_cost_delta),0)).filter_by(project_id=id).scalar())
    contributions=db.session.query(Employee.name,func.sum(ContributionEvent.value)).join(Employee).filter(ContributionEvent.project_id==id,ContributionEvent.scope=="PROJECT").group_by(Employee.name).all()
    knowledge=current(id); history=KnowledgeItem.query.filter_by(project_id=id,founder_approved=True).order_by(KnowledgeItem.created_at.desc()).all()
    reports=WorkMessage.query.filter_by(project_id=id,message_type="REPORT").order_by(WorkMessage.created_at.desc()).all()
    basis_refs=KnowledgeReference.query.filter(KnowledgeReference.to_knowledge_id.in_([k.id for k in knowledge] or [-1]),KnowledgeReference.relation_type=="BASIS_FOR").all()
    decision_bases={}
    for ref in basis_refs: decision_bases.setdefault(ref.to_knowledge_id,[]).append(db.session.get(KnowledgeItem,ref.from_knowledge_id))
    return render_template("project.html",p=p,counts=counts,costs=costs,contributions=contributions,knowledge=knowledge,
      employees=Employee.query.filter_by(active=True).all(),reports=reports,knowledge_targets=KnowledgeItem.query.filter_by(project_id=id,founder_approved=True).all(),
      active_hypotheses=active_hypotheses(id),knowledge_history=history,
      basis_options=[k for k in knowledge if k.kind in {"FACT","HYPOTHESIS","EVIDENCE","CORRECTION"}],decision_bases=decision_bases)
@bp.route("/projects/<int:id>/tasks",methods=["POST"])
def task_new(id):
    p=Project.query.get_or_404(id); assignee=Employee.query.get(request.form.get("assignee_id")); reviewer=Employee.query.get(request.form.get("reviewer_id"))
    create_task(p,request.form["title"],request.form["objective"],Employee.query.filter_by(slug="ceo").first(),assignee,reviewer,
      required_output=request.form.get("required_output"),acceptance_criteria=request.form.get("acceptance_criteria"))
    return redirect(url_for("main.project_detail",id=id))
@bp.route("/tasks/<int:id>/run",methods=["POST"])
def task_run(id):
    t=Task.query.get_or_404(id)
    try:
        if t.status=="ASSIGNED": transition(t,"WORKING")
        run=run_task(t)
        if not run.parsed_output_json: raise ValueError(run.error_text or "Task result failed validation")
        transition(t,"REVIEW"); flash("Structured Task result stored; governed knowledge proposals are in Founder Inbox.","ok")
        return redirect(url_for("main.run_detail",id=run.id))
    except Exception as e: flash(str(e),"error"); return redirect(url_for("main.project_detail",id=t.project_id))
@bp.route("/tasks/<int:id>/review",methods=["POST"])
def task_review(id):
    t=Task.query.get_or_404(id)
    try: review(t,t.reviewer or Employee.query.filter_by(slug="ceo").one(),request.form["content"],request.form["decision"]=="accept")
    except Exception as e: flash(str(e),"error")
    return redirect(url_for("main.project_detail",id=t.project_id))
@bp.route("/tasks/<int:id>/run-review",methods=["POST"])
def task_run_review(id):
    t=Task.query.get_or_404(id)
    try: run_review(t,instruction=request.form.get("instruction","Review this Task")); flash("Reviewer execution completed.","ok")
    except Exception as ex: flash(str(ex),"error")
    return redirect(url_for("main.project_detail",id=t.project_id))
@bp.route("/projects/<int:id>/briefing",methods=["POST"])
def project_briefing(id):
    p=Project.query.get_or_404(id); run=generate_project_briefing(Employee.query.filter_by(slug="ceo").one(),p)
    flash("CEO briefing generated." if run.parsed_output_json else "CEO briefing failed validation.","ok" if run.parsed_output_json else "error")
    return redirect(url_for("main.project_detail",id=id))
@bp.route("/projects/<int:id>/knowledge",methods=["POST"])
def project_knowledge(id):
    p=Project.query.get_or_404(id)
    try:
        add_knowledge(request.form["kind"],request.form["title"],request.form["content"],project_id=p.id,founder_approved=True,
          source_ref=request.form.get("source_ref") or None,rationale=request.form.get("rationale") or None,
          target_knowledge_id=int(request.form["target_knowledge_id"]) if request.form.get("target_knowledge_id") else None,
          basis_knowledge_ids=[int(x) for x in request.form.getlist("basis_knowledge_ids")])
        flash("Founder knowledge recorded.","ok")
    except Exception as ex: flash(str(ex),"error")
    return redirect(url_for("main.project_detail",id=id))

@bp.route("/employees")
def employees():
    rows=[]
    for e in Employee.query.order_by(Employee.id).all():
        rows.append({"e":e,"active_tasks":Task.query.filter_by(assigned_employee_id=e.id).filter(Task.status.notin_(["DONE","FAILED","CANCELLED"])).all(),
      "project_contribution":sum(v for _,v in project_totals(e.id)),"company_contribution":total(e.id,"COMPANY"),
          "cost":Decimal(db.session.query(func.coalesce(func.sum(CostEvent.real_cost_delta),0)).filter_by(employee_id=e.id).scalar()),
          "tokens":db.session.query(func.coalesce(func.sum(AgentRun.input_tokens+AgentRun.output_tokens),0)).filter_by(employee_id=e.id).scalar()})
    return render_template("employees.html",rows=rows)
@bp.route("/employees/<int:id>")
def employee_detail(id):
    e=Employee.query.get_or_404(id); tasks=Task.query.filter_by(assigned_employee_id=id).order_by(Task.updated_at.desc()).all()
    runs=AgentRun.query.filter_by(employee_id=id).order_by(AgentRun.started_at.desc()).limit(12).all()
    history=EmployeeModelHistory.query.filter_by(employee_id=id).order_by(EmployeeModelHistory.started_at.desc()).all()
    learning=EmployeeLearningRecord.query.filter_by(employee_id=id).order_by(EmployeeLearningRecord.created_at.desc()).limit(10).all()
    return render_template("employee.html",e=e,tasks=tasks,runs=runs,history=history,learning=learning,
      project_contributions=project_totals(id),cc=total(id,"COMPANY"),projects=Project.query.order_by(Project.name).all(),
      model_configs=ModelConfig.query.filter_by(active=True).order_by(ModelConfig.label).all())
@bp.route("/employees/<int:id>/learning",methods=["POST"])
def employee_learning(id):
    e=Employee.query.get_or_404(id); project=Project.query.get(request.form.get("project_id")) if request.form.get("project_id") else None
    task=Task.query.get(request.form.get("task_id")) if request.form.get("task_id") else None
    try: create_learning(e,request.form["title"],request.form["content"],project,task,request.form.get("source_ref"),request.form.get("validated")=="1")
    except Exception as ex: flash(str(ex),"error")
    return redirect(url_for("main.employee_detail",id=id))
@bp.route("/employees/<int:id>/model",methods=["POST"])
def employee_model(id):
    e=Employee.query.get_or_404(id); model=ModelConfig.query.get_or_404(request.form["model_config_id"])
    if not model.active: abort(400)
    change_model(e,model,request.form.get("reason") or "Founder reassignment")
    return redirect(url_for("main.employee_detail",id=id))
@bp.route("/employees/<int:id>/interview",methods=["GET","POST"])
def interview(id):
    e=Employee.query.get_or_404(id); interview=FounderInterview.query.filter_by(employee_id=id,ended_at=None).order_by(FounderInterview.started_at.desc()).first()
    if not interview: interview=start(e)
    if request.method=="POST":
        try: ask(interview,request.form["content"]); return redirect(url_for("main.interview",id=id))
        except Exception as ex: flash(str(ex),"error")
    return render_template("interview.html",e=e,interview=interview)

@bp.route("/inbox")
def inbox(): return render_template("inbox.html",proposals=Proposal.query.order_by(Proposal.created_at.desc()).all())
@bp.route("/inbox/<int:id>/materialize",methods=["POST"])
def materialize(id):
    proposal=Proposal.query.get_or_404(id)
    if proposal.status!="PENDING" or proposal.payload_json.get("type")!="PROJECT_PLAN": abort(400)
    try: p=materialize_project_plan(proposal)
    except Exception as ex: flash(str(ex),"error"); return redirect(url_for("main.inbox"))
    return redirect(url_for("main.project_detail",id=p.id))
@bp.route("/inbox/<int:id>/reject",methods=["POST"])
def reject(id):
    p=Proposal.query.get_or_404(id)
    if p.status!="PENDING": abort(400)
    p.status="REJECTED"; p.reviewed_at=now(); db.session.commit(); return redirect(url_for("main.inbox"))
@bp.route("/inbox/<int:id>/knowledge-review",methods=["POST"])
def knowledge_review(id):
    p=Proposal.query.get_or_404(id); decision=request.form["decision"]
    try:
        corrected=None
        if decision=="CORRECTED":
            corrected={"kind":request.form["kind"],"title":request.form["title"],"content":request.form["content"],
              "source_ref":request.form.get("source_ref") or None,"rationale":request.form.get("rationale") or None,
              "target_knowledge_id":int(request.form["target_knowledge_id"]) if request.form.get("target_knowledge_id") else None,
              "basis_knowledge_ids":[int(x) for x in request.form.getlist("basis_knowledge_ids")] or p.payload_json.get("basis_knowledge_ids",[])}
        review_proposal(p,decision,request.form.get("note"),corrected)
    except Exception as ex: flash(str(ex),"error")
    return redirect(url_for("main.inbox"))
@bp.route("/models",methods=["GET","POST"])
def models():
    if request.method=="POST":
        try:
            model_config_service.create(request.form["label"],request.form["provider_key"],request.form["model_name"],
              request.form["input_price_per_million"],request.form["output_price_per_million"],request.form["currency"],request.form["max_output_tokens"])
        except Exception as ex: db.session.rollback(); flash(str(ex),"error")
    models=ModelConfig.query.order_by(ModelConfig.created_at.desc()).all()
    estimates={m.id:conservative_estimate(m,"Representative pending execution framing",m.max_output_tokens) for m in models}
    return render_template("models.html",models=models,estimates=estimates,remaining=remaining())
@bp.route("/models/<int:id>/toggle",methods=["POST"])
def model_toggle(id):
    m=ModelConfig.query.get_or_404(id); model_config_service.toggle(m); return redirect(url_for("main.models"))
@bp.route("/costs")
def costs():
    c=get_company(); return render_template("costs.html",company=c,spent=spent(),remaining=remaining(),events=CostEvent.query.order_by(CostEvent.created_at.desc()).all())
@bp.route("/meetings",methods=["GET","POST"])
def meetings():
    if request.method=="POST":
        try:
            chair=Employee.query.get_or_404(request.form["chair_employee_id"])
            participants=Employee.query.filter(Employee.id.in_([int(x) for x in request.form.getlist("participant_ids")])).all()
            project=Project.query.get(request.form.get("project_id")) if request.form.get("project_id") else None
            meeting_service.create(request.form["title"],request.form["purpose"],request.form["agenda"],chair,participants,project,
              request.form["max_rounds"],request.form["token_limit"],request.form["real_cost_limit_twd"])
        except Exception as ex: flash(str(ex),"error")
        return redirect(url_for("main.meetings"))
    rows=[]
    for meeting in Meeting.query.order_by(Meeting.created_at.desc()).all():
        tokens,cost=meeting_service.usage(meeting); rows.append((meeting,tokens,cost))
    return render_template("meetings.html",rows=rows,employees=Employee.query.filter_by(active=True).all(),projects=Project.query.filter_by(environment="LIVE").all())
@bp.route("/meetings/<int:id>")
def meeting_room(id):
    meeting=Meeting.query.get_or_404(id); tokens,cost=meeting_service.usage(meeting)
    messages=MeetingMessage.query.filter_by(meeting_id=id).order_by(MeetingMessage.id).all()
    return render_template("meeting.html",meeting=meeting,tokens=tokens,cost=cost,messages=messages,recent=messages[-6:],feedback_signals=sorted(meeting_service.SIGNALS))
def _meeting_action(id,fn,*args):
    meeting=Meeting.query.get_or_404(id)
    try: fn(meeting,*args)
    except Exception as ex: flash(str(ex),"error")
    return redirect(url_for("main.meeting_room",id=id))
@bp.route("/meetings/<int:id>/start",methods=["POST"])
def meeting_start(id): return _meeting_action(id,meeting_service.start)
@bp.route("/meetings/<int:id>/advance",methods=["POST"])
def meeting_advance(id): return _meeting_action(id,meeting_service.advance)
@bp.route("/meetings/<int:id>/join",methods=["POST"])
def meeting_join(id): return _meeting_action(id,meeting_service.join_founder)
@bp.route("/meetings/<int:id>/intervene",methods=["POST"])
def meeting_intervene(id): return _meeting_action(id,meeting_service.intervene,request.form["content"])
@bp.route("/meetings/<int:id>/command",methods=["POST"])
def meeting_command(id): return _meeting_action(id,meeting_service.command,request.form["kind"],request.form["content"])
@bp.route("/meetings/<int:id>/end",methods=["POST"])
def meeting_end(id): return _meeting_action(id,meeting_service.end_and_synthesize)
@bp.route("/meetings/<int:id>/stop",methods=["POST"])
def meeting_stop(id): return _meeting_action(id,meeting_service.stop,request.form.get("reason"))
@bp.route("/meeting-messages/<int:id>/feedback",methods=["POST"])
def meeting_feedback(id):
    message=MeetingMessage.query.get_or_404(id)
    try: meeting_service.feedback(message,request.form["signal"],request.form.get("note"))
    except Exception as ex: flash(str(ex),"error")
    return redirect(url_for("main.meeting_room",id=message.meeting_id))
@bp.route("/runs/<int:id>")
def run_detail(id): return render_template("run.html",run=AgentRun.query.get_or_404(id))
