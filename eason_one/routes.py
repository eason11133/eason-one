from decimal import Decimal
import json
from flask import Blueprint, render_template, request, redirect, url_for, flash, abort, session, jsonify
from sqlalchemy import String, cast, func, or_
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
from .services import command as command_service

bp=Blueprint("main",__name__)
@bp.app_context_processor
def shell_context(): return {"shell":command_service.shell_snapshot()}
@bp.route("/")
def index(): return redirect(url_for("main.command_center"))
@bp.route("/language/<language>",methods=["POST"])
def language(language):
    if language not in {"en","zh-TW"}: abort(400)
    session["language"]=language
    return redirect(request.form.get("next") or request.referrer or url_for("main.command_center"))

@bp.route("/ceo",methods=["GET","POST"])
def ceo():
    if request.method=="GET": return redirect(url_for("main.command_center"))
    return _command_submit()

def _command_submit():
    try:
        run,proposal=founder_request(Employee.query.filter_by(slug="ceo").one(),request.form["request"])
        if isinstance(proposal,Operation) and proposal.status=="RUNNING": flash("CEO is continuing the current approved Operation.","ok")
        elif isinstance(proposal,Operation): flash("CEO proposed a bounded operation. Founder approval is required.","ok")
        elif proposal: flash("CEO proposed governed work. Founder approval is required.","ok")
        elif run.parsed_output_json: flash(run.parsed_output_json["executive_response"],"ok")
        else: flash("CEO output failed validation; no Company state changed.","error")
    except Exception as ex: flash(str(ex),"error")
    return redirect(url_for("main.command_center"))

@bp.route("/command",methods=["GET","POST"])
def command_center():
    company=get_company()
    if not company: return render_template("unseeded.html")
    if request.method=="POST": return _command_submit()
    return render_template("command.html",snapshot=command_service.snapshot())

@bp.route("/command/proposals/<int:id>/approve",methods=["POST"])
def command_approve(id):
    proposal=Proposal.query.get_or_404(id)
    try:
        project=materialize_project_plan(proposal)
        flash(f"{project.name} approved. Governed Tasks are ready for execution.","ok")
    except Exception as ex: flash(str(ex),"error")
    return redirect(url_for("main.command_center"))

@bp.route("/operations/<int:id>/approve",methods=["POST"])
def operation_approve(id):
    operation=Operation.query.get_or_404(id)
    try:
        __import__("eason_one.services.operations",fromlist=["approve"]).approve(operation)
        flash("Operation approved. CEO may now manage bounded internal execution.","ok")
    except Exception as ex: flash(str(ex),"error")
    return redirect(url_for("main.operation_detail",id=id,autorun=1))

@bp.route("/operations/<int:id>")
def operation_detail(id):
    operation=Operation.query.get_or_404(id)
    state=__import__("eason_one.services.operations",fromlist=["state"]).state(operation)
    return render_template("operation.html",operation=operation,state=state)

@bp.route("/operations/<int:id>/next-step",methods=["POST"])
def operation_next_step(id):
    operation=Operation.query.get_or_404(id)
    try:
        result=__import__("eason_one.services.operations",fromlist=["next_step"]).next_step(
          operation,request.headers.get("Idempotency-Key") or request.form.get("idempotency_key") or f"browser-{id}")
        state=__import__("eason_one.services.operations",fromlist=["state"]).state(operation)
        return jsonify(result|{"operation_status":state["status"],
          "continue_allowed":state["continue_allowed"]})
    except Exception as ex: return jsonify({"error":str(ex),"status":operation.status}),409

@bp.route("/operations/<int:id>/pause",methods=["POST"])
def operation_pause(id):
    operation=Operation.query.get_or_404(id)
    try: __import__("eason_one.services.operations",fromlist=["pause"]).pause(operation)
    except Exception as ex: flash(str(ex),"error")
    return redirect(url_for("main.work"))

@bp.route("/operations/<int:id>/stop",methods=["POST"])
def operation_stop(id):
    operation=Operation.query.get_or_404(id)
    __import__("eason_one.services.operations",fromlist=["stop"]).stop(operation)
    return redirect(url_for("main.work"))

@bp.route("/operations/<int:id>/budget",methods=["POST"])
def operation_budget(id):
    operation=Operation.query.get_or_404(id)
    try:
        amount=Decimal(request.form["additional_budget_twd"])
        if amount<=0: raise ValueError("Additional authorization must be positive")
        operation.approved_budget_twd=Decimal(operation.approved_budget_twd)+amount
        db.session.commit()
        __import__("eason_one.services.operations",fromlist=["resume"]).resume(operation)
        flash(f"Founder authorized an additional NT$ {amount}.","ok")
    except Exception as ex: flash(str(ex),"error")
    return redirect(url_for("main.operation_detail",id=id,autorun=1))

@bp.route("/work")
def work(): return render_template("work.html",snapshot=command_service.work_snapshot())

@bp.route("/team")
def team(): return render_template("team.html",snapshot=command_service.team_snapshot())

@bp.route("/team/hr")
def team_hr():
    q=(request.args.get("q") or "").strip()
    department=(request.args.get("department") or "").strip()
    candidates=TalentTemplate.query.filter_by(active_in_pool=True)
    if q:
        like=f"%{q}%"
        candidates=candidates.filter(or_(TalentTemplate.name.ilike(like),
          TalentTemplate.role_title.ilike(like),
          cast(TalentTemplate.skills_json,String).ilike(like)))
    if department: candidates=candidates.filter(TalentTemplate.department_hint.ilike(f"%{department}%"))
    hr=Employee.query.filter_by(slug="hr-director").first()
    authorization=__import__("eason_one.services.workforce",fromlist=["assessment_authorization"]).assessment_authorization(hr)
    return render_template("hr.html",
      requests=HiringRequest.query.order_by(HiringRequest.updated_at.desc()).all(),
      candidates=candidates.order_by(TalentTemplate.role_title).all(),
      employees=Employee.query.order_by(Employee.name).all(),hr=hr,
      hr_assessment_authorization=authorization,
      models=ModelConfig.query.filter_by(active=True,archived=False).order_by(ModelConfig.label).all())

@bp.route("/team/hr/candidates",methods=["POST"])
def talent_candidate_create():
    try:
        __import__("eason_one.services.workforce",fromlist=["create_talent_template"]).create_talent_template({
          "name":request.form["name"],"role_title":request.form["role_title"],
          "department_hint":request.form.get("department_hint"),"mission":request.form["mission"],
          "responsibilities":[x.strip() for x in request.form["responsibilities"].splitlines() if x.strip()],
          "instructions":request.form["instructions"],
          "skills":[x.strip() for x in request.form["skills"].splitlines() if x.strip()],
          "suggested_tools":[x.strip() for x in request.form.get("suggested_tools","").splitlines() if x.strip()],
          "deliverables":[x.strip() for x in request.form["deliverables"].splitlines() if x.strip()],
          "success_metrics":[x.strip() for x in request.form["success_metrics"].splitlines() if x.strip()],
          "source":"Founder manual"})
        flash("Candidate added to Talent Pool. No Employee was hired.","ok")
    except Exception as ex: flash(str(ex),"error")
    return redirect(url_for("main.team_hr"))

@bp.route("/team/hr/requests",methods=["POST"])
def hiring_request_create():
    try:
        service=__import__("eason_one.services.workforce",fromlist=["request_hire","assess_request"])
        item=service.request_hire(
          requested_by_type="FOUNDER",role_needed=request.form["role_needed"],
          problem=request.form["problem"],why_now=request.form["why_now"],
          responsibilities=[x.strip() for x in request.form["responsibilities"].splitlines() if x.strip()],
          capabilities=[x.strip() for x in request.form["capabilities"].splitlines() if x.strip()],
          urgency=request.form["urgency"],use_frequency=request.form["use_frequency"],
          talent_template=TalentTemplate.query.get(request.form.get("talent_template_id")))
        hr=Employee.query.filter_by(slug="hr-director").one()
        if hr.current_model:
            service.assess_request(item)
            flash("HR Director completed the authorized assessment. Founder review is ready.","ok")
        else:
            flash("Hiring need recorded. Assign an active model to HR Director before assessment.","error")
    except Exception as ex: flash(str(ex),"error")
    return redirect(url_for("main.team_hr"))

@bp.route("/employees/<int:id>/hiring-requests",methods=["POST"])
def management_hiring_request(id):
    manager=Employee.query.get_or_404(id)
    operation=Operation.query.get_or_404(request.form["operation_id"])
    try:
        if operation.status!="RUNNING": raise ValueError("Staffing requests require a running approved Operation")
        __import__("eason_one.services.workforce",fromlist=["request_hire"]).request_hire(
          requester=manager,requested_by_type="EMPLOYEE",operation=operation,
          project=operation.project,role_needed=request.form["role_needed"],
          problem=request.form["problem"],why_now=request.form["why_now"],
          responsibilities=[x.strip() for x in request.form["responsibilities"].splitlines() if x.strip()],
          capabilities=[x.strip() for x in request.form["capabilities"].splitlines() if x.strip()],
          urgency=request.form["urgency"],use_frequency=request.form["use_frequency"])
        flash("Management staffing request routed to HR under the approved Operation.","ok")
    except Exception as ex: flash(str(ex),"error")
    return redirect(url_for("main.employee_detail",id=id))

@bp.route("/team/hr/requests/<int:id>/review",methods=["POST"])
def hiring_request_review(id):
    item=HiringRequest.query.get_or_404(id)
    try:
        __import__("eason_one.services.workforce",fromlist=["review_request"]).review_request(
          item,existing_staff_alternative=request.form["existing_staff_alternative"],
          recommendation=request.form["recommendation"],
          recommended_model=ModelConfig.query.get(request.form.get("model_config_id")),
          estimated_input_tokens=request.form["estimated_input_tokens"],
          estimated_output_tokens=request.form["estimated_output_tokens"],
          expected_calls=request.form["expected_calls"],
          max_mission_budget_twd=Decimal(request.form["max_mission_budget_twd"]),
          expected_benefit=request.form["expected_benefit"],
          redundancy_risk=request.form["redundancy_risk"],
          alternatives=[x.strip() for x in request.form["alternatives"].splitlines() if x.strip()],
          success_criteria=[x.strip() for x in request.form["success_criteria"].splitlines() if x.strip()],
          probation_assignments=request.form["probation_assignments"])
        flash("HR proposal is ready for Founder review.","ok")
    except Exception as ex: flash(str(ex),"error")
    return redirect(url_for("main.team_hr"))

@bp.route("/team/hr/requests/<int:id>/approve",methods=["POST"])
def hiring_request_approve(id):
    try:
        employee=__import__("eason_one.services.workforce",fromlist=["approve_hire"]).approve_hire(
          HiringRequest.query.get_or_404(id))
        flash(f"{employee.name} hired on probation. No provider call was made.","ok")
    except Exception as ex: flash(str(ex),"error")
    return redirect(url_for("main.team_hr"))

@bp.route("/team/hr/requests/<int:id>/reject",methods=["POST"])
def hiring_request_reject(id):
    try: __import__("eason_one.services.workforce",fromlist=["reject_request"]).reject_request(
      HiringRequest.query.get_or_404(id),request.form.get("note") or "Founder rejected")
    except Exception as ex: flash(str(ex),"error")
    return redirect(url_for("main.team_hr"))

@bp.route("/company")
def company_hub(): return render_template("company.html",company=get_company(),spent=spent(),remaining=remaining())

@bp.route("/company/brain")
def company_brain(): return render_template("brain.html",knowledge=current(None))

@bp.route("/company/runs")
def run_audit():
    runs=AgentRun.query.order_by(AgentRun.started_at.desc()).limit(100).all()
    return render_template("runs.html",runs=runs)

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
      model_configs=ModelConfig.query.filter_by(active=True,archived=False).order_by(ModelConfig.label).all(),
      employee_view=command_service.employee_view(e),
      active_operations=Operation.query.filter_by(status="RUNNING").order_by(Operation.updated_at.desc()).all())
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
    if not model.active or model.archived: abort(400)
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
    rows=[{"model":m,"assigned":Employee.query.filter_by(current_model_config_id=m.id).all(),
      "runs":AgentRun.query.filter_by(model_config_id=m.id).count()} for m in models]
    return render_template("models.html",models=models,rows=rows,estimates=estimates,remaining=remaining())
@bp.route("/models/<int:id>/edit",methods=["POST"])
def model_edit(id):
    model=ModelConfig.query.get_or_404(id)
    try:
        model_config_service.edit(model,request.form["label"],request.form["model_name"],
          request.form["input_price_per_million"],request.form["output_price_per_million"],
          request.form["currency"],request.form["max_output_tokens"])
    except Exception as ex: db.session.rollback(); flash(str(ex),"error")
    return redirect(url_for("main.models"))
@bp.route("/models/<int:id>/toggle",methods=["POST"])
def model_toggle(id):
    try: model_config_service.toggle(ModelConfig.query.get_or_404(id))
    except Exception as ex: flash(str(ex),"error")
    return redirect(url_for("main.models"))
@bp.route("/models/<int:id>/archive",methods=["POST"])
def model_archive(id):
    model_config_service.archive(ModelConfig.query.get_or_404(id)); return redirect(url_for("main.models"))
@bp.route("/models/<int:id>/delete",methods=["POST"])
def model_delete(id):
    try: model_config_service.delete(ModelConfig.query.get_or_404(id))
    except Exception as ex: flash(str(ex),"error")
    return redirect(url_for("main.models"))
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
            profile=request.form.get("execution_profile","STANDARD")
            defaults=meeting_service.PROFILES[profile]
            mission=request.form.get("mission_context") or request.form["purpose"]
            question=request.form.get("meeting_question") or request.form.get("agenda") or request.form["purpose"]
            meeting_service.create(request.form["title"],mission,question,chair,participants,project,
              request.form.get("max_rounds") or defaults["max_rounds"],request.form.get("token_limit") or defaults["tokens"],
              request.form.get("real_cost_limit_twd") or defaults["cost"],profile=profile,
              max_speakers_per_round=request.form.get("max_speakers_per_round") or defaults["max_speakers"],
              contribution_output_cap=request.form.get("contribution_output_cap") or defaults["contribution_cap"],
              router_output_cap=request.form.get("router_output_cap") or defaults["router_cap"],
              synthesis_output_cap=request.form.get("synthesis_output_cap") or defaults["synthesis_cap"])
        except Exception as ex: flash(str(ex),"error")
        return redirect(url_for("main.meetings"))
    rows=[]
    for meeting in Meeting.query.order_by(Meeting.created_at.desc()).all():
        tokens,cost=meeting_service.usage(meeting)
        calls=AgentRun.query.filter_by(meeting_id=meeting.id).count()
        result=meeting_service.result_view(meeting)
        participant_names=[item.employee.name for item in meeting.participants]
        visible_names=participant_names[:3]
        participant_label=" · ".join(visible_names)
        if len(participant_names)>len(visible_names):
            participant_label+=f" +{len(participant_names)-len(visible_names)}"
        recovered=bool(result and result.get("recovered"))
        if meeting.status=="WAITING_FOR_FOUNDER":
            founder_status="NEEDS FOUNDER"
        elif meeting.status=="PAUSED" and meeting.paid_failure_json:
            founder_status="ACTION REQUIRED"
        elif meeting.status in {"ACTIVE","RUNNING"}:
            founder_status="LIVE"
        elif meeting.status=="ENDED":
            founder_status="COMPLETE"
        elif meeting.status=="TERMINATED_BY_FOUNDER":
            founder_status="RESULT RECOVERED" if recovered else "TERMINATED"
        else:
            founder_status=meeting.status.replace("_"," ")
        rows.append({"meeting":meeting,"tokens":tokens,"cost":cost,"calls":calls,
          "result":result,"founder_status":founder_status,
          "result_excerpt":((result or {}).get("position") or meeting.termination_reason),
          "participant_names":participant_names,"participant_label":participant_label})
    return render_template("meetings.html",rows=rows,employees=Employee.query.filter_by(active=True).all(),
      projects=Project.query.filter_by(environment="LIVE").all(),profiles=meeting_service.PROFILES)
@bp.route("/meetings/<int:id>")
def meeting_room(id):
    meeting=Meeting.query.get_or_404(id); tokens,cost=meeting_service.usage(meeting)
    messages=MeetingMessage.query.filter_by(meeting_id=id).order_by(MeetingMessage.id).all()
    runs=AgentRun.query.filter_by(meeting_id=id).order_by(AgentRun.id).all()
    display=[]
    for message in messages:
        parsed=None
        if message.speaker_type=="EMPLOYEE":
            try: parsed=json.loads(message.content)
            except Exception: pass
        display.append({"message":message,"parsed":parsed})
    return render_template("meeting.html",meeting=meeting,tokens=tokens,cost=cost,calls=len(runs),runs=runs,
      messages=messages,display=display,recent=display[-6:],feedback_signals=sorted(meeting_service.SIGNALS),
      salvage=meeting_service.salvage_preview(meeting),primary_status=meeting_service.primary_status_text(meeting),
      result=meeting_service.result_view(meeting))
def _meeting_action(id,fn,*args):
    meeting=Meeting.query.get_or_404(id)
    try: fn(meeting,*args)
    except Exception as ex: flash(str(ex),"error")
    return redirect(url_for("main.meeting_room",id=id))
@bp.route("/meetings/<int:id>/start",methods=["POST"])
def meeting_start(id):
    meeting=Meeting.query.get_or_404(id)
    try: meeting_service.start_auto(meeting)
    except Exception as ex: flash(str(ex),"error"); return redirect(url_for("main.meeting_room",id=id))
    return redirect(url_for("main.meeting_room",id=id,autorun=1))
@bp.route("/meetings/<int:id>/next-step",methods=["POST"])
def meeting_next_step(id):
    meeting=Meeting.query.get_or_404(id)
    try: return jsonify(meeting_service.next_step_idempotent(meeting,request.headers.get("Idempotency-Key")))
    except Exception as ex: return jsonify(meeting_service.auto_state(meeting)|{"error":str(ex)}),409
@bp.route("/meetings/<int:id>/pause",methods=["POST"])
def meeting_pause(id): return _meeting_action(id,meeting_service.pause)
@bp.route("/meetings/<int:id>/resume",methods=["POST"])
def meeting_resume(id):
    meeting=Meeting.query.get_or_404(id)
    try: meeting_service.resume(meeting)
    except Exception as ex: flash(str(ex),"error"); return redirect(url_for("main.meeting_room",id=id))
    return redirect(url_for("main.meeting_room",id=id,autorun=1))
@bp.route("/meetings/<int:id>/retry-paid-step",methods=["POST"])
def meeting_retry_paid_step(id):
    meeting=Meeting.query.get_or_404(id)
    try: meeting_service.retry_paid_step(meeting)
    except Exception as ex: flash(str(ex),"error"); return redirect(url_for("main.meeting_room",id=id))
    return redirect(url_for("main.meeting_room",id=id,autorun=1))
@bp.route("/meetings/<int:id>/accept-existing-response",methods=["POST"])
def meeting_accept_existing_response(id):
    meeting=Meeting.query.get_or_404(id)
    try: meeting_service.accept_existing_response(meeting)
    except Exception as ex: flash(str(ex),"error"); return redirect(url_for("main.meeting_room",id=id))
    return redirect(url_for("main.meeting_room",id=id,autorun=1))
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
