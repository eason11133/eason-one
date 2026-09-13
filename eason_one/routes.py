from decimal import Decimal
from datetime import datetime, timezone
from time import perf_counter
from uuid import uuid4
from importlib.metadata import PackageNotFoundError, version
import json
from pathlib import Path
import tomllib
from flask import Blueprint, render_template, request, redirect, url_for, flash, abort, session, jsonify, Response, current_app, send_file
from sqlalchemy import String, cast, func, or_
from .extensions import db
from .models import *
from .services.company import get_company, spent, remaining
from .services.projects import create_project,classify
from .services.tasks import create_task, transition, review
from .services.execution import execute
from .services.ceo import founder_request, materialize_project_plan,generate_project_briefing
from .services.interviews import start, ask
from .services.contributions import total, project_totals, meaningful_total
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

@bp.get("/api/healthz")
def healthz():
    return jsonify({"ok": True, "service": "eason-one"})

@bp.get("/api/build-info")
def build_info():
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    try:
        application_version = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["version"]
    except (FileNotFoundError, KeyError, tomllib.TOMLDecodeError):
        try:
            application_version = version("eason-one")
        except PackageNotFoundError:
            application_version = "unknown"
    return jsonify({"service": "eason-one", "version": application_version})

def _wake_runtime_for(operation):
    core = __import__(
        "eason_one.services.core_v018",
        fromlist=["is_v018_operation", "is_dormant_legacy_project_operation"],
    )
    company_runtime = __import__(
        "eason_one.services.company_runtime",
        fromlist=["is_work_vnext", "wake_company_runtime"],
    )
    if core.is_v018_operation(operation):
        company_runtime.ensure_company_runtime(current_app._get_current_object())
        return {"owner": "COMPANY_RUNTIME", "work_vnext": True, "runtime": company_runtime.runtime_health()}
    if core.is_dormant_legacy_project_operation(operation):
        raise ValueError("Historical pre-v0.18 Project Operations cannot start a runtime.")
    if current_app.config.get("AUTO_START_OPERATION_RUNTIME", not current_app.testing):
        return __import__(
            "eason_one.services.operation_runtime", fromlist=["start_background"]
        ).start_background(operation.id)
    return {"owner": "LEGACY_OPERATION_RUNTIME", "work_vnext": False}

@bp.app_context_processor
def shell_context(): return {"shell":command_service.shell_snapshot()}
@bp.route("/")
def index(): return redirect(url_for("main.headquarters"))
@bp.route("/language/<language>",methods=["POST"])
def language(language):
    if language not in {"en","zh-TW"}: abort(400)
    session["language"]=language
    return redirect(request.form.get("next") or request.referrer or url_for("main.headquarters"))

@bp.route("/ceo",methods=["GET","POST"])
def ceo():
    if request.method=="GET": return redirect(url_for("main.headquarters"))
    return _command_submit()

def _command_submit():
    text=request.form["request"]
    lowered=text.lower()
    if any(phrase in lowered for phrase in (
      "company status","current status","factual briefing",
      "status of the company","how is the company")):
        service=__import__(
          "eason_one.services.headquarters",fromlist=["local_ceo_answer"])
        session["hq_ceo_answer"]=service.local_ceo_answer(text)
        session["hq_ceo_query"]=text
        return redirect(url_for("main.headquarters"))
    try:
        run,proposal=founder_request(Employee.query.filter_by(slug="ceo").one(),text)
        if isinstance(proposal,Operation) and proposal.status=="RUNNING": pass
        elif isinstance(proposal,Operation): pass
        elif proposal: pass
        elif run.parsed_output_json: pass
    except Exception:
        db.session.rollback()
    return redirect(url_for("main.headquarters"))


def _operation_proposal_view(operation):
    data=(operation.plan_json or {}).get("operation") or {}
    employees={employee.id:employee for employee in Employee.query.filter_by(active=True).all()}
    tasks=[]
    for item in data.get("tasks") or []:
        assignee=employees.get(item.get("assignee_employee_id"))
        reviewer=employees.get(item.get("reviewer_employee_id"))
        write_scope = dict(item.get("write_scope") or {})
        capabilities = list(item.get("required_capabilities") or [])
        read_only_engineering = False
        if "SOFTWARE_ENGINEERING" in {str(value or "").upper() for value in capabilities} and not write_scope:
            read_only_engineering = __import__(
                "eason_one.services.codex_connector", fromlist=["plan_task_is_read_only"]
            ).plan_task_is_read_only(item)
        tasks.append({
            "title":item.get("title"),
            "objective":item.get("objective"),
            "assignee":assignee.name if assignee else f"Employee #{item.get('assignee_employee_id')}",
            "reviewer":reviewer.name if reviewer else "No independent reviewer",
            "required_capabilities":capabilities,
            "acceptance_criteria":item.get("acceptance_criteria") or [],
            "write_paths":list(write_scope.get("paths") or []),
            "read_only_engineering":read_only_engineering,
        })
    config=data.get("meeting_config") or {}
    participants=[employees.get(employee_id) for employee_id in config.get("participant_employee_ids") or []]
    memory = dict(operation.memory_json or {})
    project_spec = dict(memory.get("new_project_spec") or {})
    staffing_gaps = [
        {
            "capability": row.get("capability"),
            "estimated_twd": row.get("estimated_twd"),
        }
        for row in (memory.get("execution_budget_breakdown") or [])
        if row.get("kind") == "HR_CAPABILITY_ASSESSMENT"
    ]
    engineering_required = any(
        "SOFTWARE_ENGINEERING" in {str(value or "").upper() for value in (item.get("required_capabilities") or [])}
        for item in (data.get("tasks") or [])
    )
    engineering_readiness = None
    if engineering_required:
        connector = __import__(
            "eason_one.services.codex_connector",
            fromlist=["codex_runtime_descriptor", "preapproval_repository_readiness"],
        )
        descriptor = connector.codex_runtime_descriptor()
        if operation.project_id and operation.project:
            contracts = __import__(
                "eason_one.services.project_contract", fromlist=["is_vnext_governed", "governing_terms"]
            )
            constraints = (
                list(contracts.governing_terms(operation.project).get("constraints") or [])
                if contracts.is_vnext_governed(operation.project)
                else str(operation.project.known_constraints or "").splitlines()
            )
        else:
            constraints = list(project_spec.get("constraints") or [])
        repository = connector.preapproval_repository_readiness(constraints=constraints)
        source = str(descriptor.get("source") or "unverified")
        engineering_readiness = {
            "required": True,
            "ready": bool(descriptor.get("ready")) and bool(repository.get("ready")),
            "cli_ready": bool(descriptor.get("ready")),
            "repository_ready": bool(repository.get("ready")),
            "verified": bool(descriptor.get("ready")) and not bool(descriptor.get("stale")) and bool(repository.get("ready")),
            "source": source,
            "transport": descriptor.get("transport"),
            "version": descriptor.get("version"),
            "error": descriptor.get("error") or repository.get("error"),
            "repository": repository.get("path"),
            "approval_probe_required": True,
        }
    return {
        "operation_id":operation.id,
        "title":operation.title,
        "objective":operation.objective,
        "creates_project": bool(project_spec and not operation.project_id),
        "project_name": project_spec.get("name"),
        "project_objective": project_spec.get("objective"),
        "project_budget_twd": memory.get("project_authorized_budget_twd"),
        "project_success_criteria": project_spec.get("success_criteria") or [],
        "project_constraints": project_spec.get("constraints") or [],
        "status":operation.status,
        "budget_twd":str(operation.approved_budget_twd),
        "staffing_gaps": staffing_gaps,
        "engineering_readiness": engineering_readiness,
        "tasks":tasks,
        "completion_criteria":data.get("completion_criteria") or [],
        "meeting":{
            "policy":data.get("meeting_policy"),
            "trigger":config.get("trigger"),
            "participants":[employee.name for employee in participants if employee],
            "max_rounds":config.get("max_rounds"),
            "max_speakers_per_round":config.get("max_speakers_per_round"),
            "contribution_output_cap":config.get("contribution_output_cap"),
            "token_limit":config.get("token_limit"),
            "budget_twd":config.get("budget_twd"),
            "retry_limit":config.get("retry_limit"),
        },
        "approve_url":url_for("main.headquarters_ceo_operation_approve",id=operation.id),
        "open_url":url_for("main.headquarters_mission",id=operation.id),
    }


def _ceo_execution_answer(service, query, project_id=None, request_id=None):
    """Run one governed CEO request and normalize it for Founder surfaces.

    Project Detail commands keep the visible Founder message untouched while
    adding a tiny deterministic Project scope marker to the CEO execution
    request.  This avoids relying on the model to guess which Project the
    Founder meant and prevents unrelated recent Missions from hijacking context.
    """
    ceo_service=__import__("eason_one.services.ceo",fromlist=["record_founder_message","record_ceo_message"])
    project = db.session.get(Project, int(project_id)) if project_id else None
    execution_query = query
    if project:
        execution_query = f"PROJECT SCOPE: {project.name} (Project #{project.id})\nFOUNDER REQUEST: {query}"
    run, proposal = founder_request(
        Employee.query.filter_by(slug="ceo").one(), execution_query,
        project_id=getattr(project, "id", None), request_id=request_id,
    )
    # AgentRun itself durably contains the Founder request before any provider
    # dispatch.  Persist the conversational projection afterwards and key it to
    # that Run so an HTTP/browser replay cannot duplicate dialogue rows.
    ceo_service.record_founder_message(
        query, project_id=getattr(project, "id", None), run=run
    )
    payload = run.parsed_output_json or {}
    if payload.get("status_report") and run.status == "SUCCEEDED":
        answer=service.status_report_answer(run)
        ceo_service.record_ceo_message(answer["summary"],run=run)
        return answer, run, proposal

    href = None
    title = "CEO judgment ready" if run.status == "SUCCEEDED" else "CEO request failed"
    facts = [f"Run status: {run.status}", f"Purpose: {run.purpose}"]
    proposal_view=None
    if isinstance(proposal, Operation):
        href = url_for("main.headquarters_mission", id=proposal.id)
        title = "CEO proposal ready for Founder review" if proposal.status != "RUNNING" else "CEO resumed approved work"
        facts.extend([
            f"Mission: {proposal.title}",
            f"Authority state: {proposal.status.replace('_', ' ')}",
            "No new authority was auto-approved.",
        ])
        proposal_view=_operation_proposal_view(proposal)
    elif isinstance(proposal, Proposal):
        href = url_for("main.headquarters_attention")
        title = "CEO project plan ready for Founder review"
        facts.extend([
            f"Proposal ID: {proposal.id}",
            "The plan remains pending until Founder approval.",
        ])
    elif run.status == "SUCCEEDED":
        facts.append("No authoritative company state was changed.")

    answer={
        "kind": "COMMAND_RESULT",
        "title": title,
        "summary": payload.get("executive_response") or run.error_text or "The CEO request was recorded.",
        "facts": facts,
        "run_id": run.id,
        "href": href,
        "proposal":proposal_view,
    }
    # Every CEO reply is part of the auditable Founder dialogue, including
    # failed Runs.  The previous implementation persisted only successful
    # replies, so a refresh or server restart left an orphan Founder message.
    ceo_service.record_ceo_message(
        answer["summary"], run=run,
        project_id=getattr(proposal, "project_id", None) or getattr(project, "id", None),
    )
    return answer, run, proposal


def _request_payload():
    return request.get_json(silent=True) or request.form


@bp.route("/headquarters/ceo/preview", methods=["POST"])
def headquarters_ceo_preview():
    """Return immediate read-only context before any paid CEO judgment."""
    started = perf_counter()
    payload = _request_payload()
    query = (payload.get("request") or "").strip()
    if not query:
        return jsonify({"error": "Tell the CEO what you need."}), 400
    service = __import__(
        "eason_one.services.headquarters",
        fromlist=["route_ceo_intent"],
    )
    project_service = __import__(
        "eason_one.services.project_company",
        fromlist=["preview_snapshot", "local_brief"],
    )
    snapshot = project_service.preview_snapshot()
    focus_project = db.session.get(Project, int(payload.get("project_id"))) if payload.get("project_id") else None
    routed = service.route_ceo_intent(query, payload.get("mode"))
    if routed["route"] == "BRIEF":
        immediate = project_service.local_brief(query, project_id=getattr(focus_project, "id", None))
        ceo_service=__import__("eason_one.services.ceo",fromlist=["record_founder_message","record_ceo_message"])
        ceo_service.record_founder_message(query, project_id=getattr(focus_project, "id", None))
        ceo_service.record_ceo_message(immediate.get("summary") or immediate.get("title") or "Briefing ready")
        execute_required = False
    else:
        immediate = {
            "kind": "IMMEDIATE_CONTEXT",
            "title": f"{focus_project.name} context loaded" if focus_project else "Project context loaded",
            "summary": (
                (f"Focused on {focus_project.name}. " if focus_project else "")
                + f"{snapshot['projects']} open Projects · {snapshot['working']} live Employee Runs · "
                + f"{snapshot['attention']} Founder decisions."
            ),
            "facts": [
                f"Open Projects: {snapshot['projects']}",
                f"Employees working now: {snapshot['working']}",
                f"Founder decisions: {snapshot['attention']}",
                "Recent Projects: " + (", ".join(snapshot['project_names']) or "None"),
            ],
            "href": "/headquarters/projects",
        }
        execute_required = True
    request_id = str(uuid4())
    return jsonify({
        "request_id": request_id,
        "route": routed["route"],
        "route_type": routed.get("route_type"),
        "route_reason": routed["reason"],
        "execute_required": execute_required,
        "immediate": immediate,
        "server_preview_ms": round((perf_counter() - started) * 1000, 2),
    })


@bp.route("/headquarters/ceo/execute", methods=["POST"])
def headquarters_ceo_execute():
    """Execute only ADVISE/ACT paths after the instant office preview."""
    started = perf_counter()
    payload = _request_payload()
    query = (payload.get("request") or "").strip()
    if not query:
        return jsonify({"error": "Tell the CEO what you need."}), 400
    service = __import__(
        "eason_one.services.headquarters",
        fromlist=["route_ceo_intent", "local_ceo_answer", "status_report_answer"],
    )
    routed = service.route_ceo_intent(query, payload.get("mode"))
    if routed["route"] == "BRIEF":
        answer = service.local_ceo_answer(query)
        return jsonify({
            "route": "BRIEF",
            "route_type": routed.get("route_type"),
            "answer": answer,
            "server_total_ms": round((perf_counter() - started) * 1000, 2),
        })
    if routed.get("route_type") == "DETERMINISTIC_ACTION":
        try:
            result=__import__(
                "eason_one.services.operation_kernel",
                fromlist=["execute_deterministic_action"],
            ).execute_deterministic_action(query)
            if result.get("action")=="RESUMED":
                resumed = db.session.get(Operation, result["operation_id"])
                if resumed:
                    _wake_runtime_for(resumed)
            return jsonify({
                "route":"ACT","route_type":"DETERMINISTIC_ACTION",
                "answer":{
                    "kind":"DETERMINISTIC_ACTION",
                    "title":f"Operation #{result['operation_id']} {result['action'].replace('_',' ').title()}",
                    "summary":f"{result['title']} is now {result['status'].replace('_',' ')}.",
                    "facts":[
                        f"Stage: {result['stage']}",
                        f"Local model cost: NT$ {result['budget']['actual']}",
                        f"Reserved: NT$ {result['budget']['reserved']}",
                        "No model call was used.",
                    ],
                    "href":url_for("main.headquarters_mission",id=result["operation_id"]),
                },
                "server_total_ms":round((perf_counter()-started)*1000,2),
            })
        except Exception as ex:
            db.session.rollback()
            return jsonify({"route":"ACT","route_type":"DETERMINISTIC_ACTION","error":str(ex)}),409
    try:
        answer, run, proposal = _ceo_execution_answer(
            service, query, payload.get("project_id"), payload.get("request_id")
        )
        status_code = 200 if run.status == "SUCCEEDED" else 202 if run.status == "RUNNING" else 502
        return jsonify({
            "route": routed["route"],
            "route_type": routed.get("route_type"),
            "request_id": payload.get("request_id"),
            "answer": answer,
            "run_id": run.id,
            "run_status": run.status,
            "proposal_type": (
                "OPERATION" if isinstance(proposal, Operation)
                else "PROJECT_PLAN" if isinstance(proposal, Proposal)
                else None
            ),
            "server_total_ms": round((perf_counter() - started) * 1000, 2),
        }), status_code
    except Exception as ex:
        db.session.rollback()
        summary = str(ex)
        # The Founder request is committed before provider execution. Persist
        # the matching CEO failure reply as well, otherwise the browser can
        # display it temporarily while the durable conversation loses it.
        try:
            ceo_service = __import__(
                "eason_one.services.ceo", fromlist=["record_ceo_message"]
            )
            ceo_service.record_ceo_message(summary)
        except Exception:
            db.session.rollback()
        return jsonify({
            "route": routed["route"],
            "route_type": routed.get("route_type"),
            "error": summary,
            "answer": {
                "title": "CEO request could not be completed",
                "summary": summary,
                "facts": ["No successful result or authority change was fabricated."],
                "href": url_for("main.run_audit"),
            },
            "server_total_ms": round((perf_counter() - started) * 1000, 2),
        }), 500


@bp.route("/headquarters/ceo/metrics", methods=["POST"])
def headquarters_ceo_metrics():
    """Capture report-ready CEO office latency without manual Research entry."""
    payload = request.get_json(silent=True) or {}
    request_id = str(payload.get("request_id") or uuid4())[:80]
    key = f"CEO-UX-{request_id}"
    existing = ResearchRecord.query.filter_by(record_key=key).first()
    if existing:
        return jsonify({"saved": True, "record_id": existing.id, "duplicate": True})
    def number(name):
        try:
            return round(float(payload.get(name) or 0), 2)
        except (TypeError, ValueError):
            return 0.0
    route = str(payload.get("route") or "UNKNOWN")[:20]
    first_useful = number("first_useful_ms")
    total = number("total_ms")
    record = ResearchRecord(
        record_key=key,
        record_type="UX_METRIC",
        title="CEO office interaction latency",
        summary=f"{route}: first useful information {first_useful} ms; complete {total} ms.",
        status="CAPTURED",
        version_label="Headquarters V0.13",
        agent_run_id=int(payload["run_id"]) if str(payload.get("run_id") or "").isdigit() else None,
        source_ref="CEO Office automatic telemetry",
        metadata_json={
            "route": route,
            "first_useful_ms": first_useful,
            "total_ms": total,
            "preview_server_ms": number("preview_server_ms"),
            "execution_server_ms": number("execution_server_ms"),
            "success": bool(payload.get("success", True)),
        },
        founder_approved=False,
    )
    db.session.add(record)
    db.session.commit()
    return jsonify({"saved": True, "record_id": record.id})


@bp.route("/headquarters/ceo/operations/<int:id>/approve",methods=["POST"])
def headquarters_ceo_operation_approve(id):
    operation=db.get_or_404(Operation,id)
    try:
        __import__("eason_one.services.founder_decisions",fromlist=["decide"]).decide(
          operation,"APPROVE",reason="Founder approved from CEO dialogue")
        # Successful vNext approval always hands process ownership to Company
        # Runtime immediately. Do not make thread wake-up depend on a transient
        # compatibility Operation.status projection; the kernel itself decides
        # whether Work is runnable, waiting, or blocked.
        if operation.approved_at is not None:
            _wake_runtime_for(operation)
        live=__import__("eason_one.services.headquarters",fromlist=["operation_live_snapshot"]).operation_live_snapshot(operation)
        return jsonify({"approved":True,"operation":live})
    except Exception as ex:
        db.session.rollback()
        return jsonify({"approved":False,"error":str(ex)}),409


@bp.route("/headquarters/ceo/operations/<int:id>/decline",methods=["POST"])
def headquarters_ceo_operation_decline(id):
    operation=db.get_or_404(Operation,id)
    try:
        __import__("eason_one.services.founder_decisions",fromlist=["decide"]).decide(
          operation,"REJECT",reason="Founder declined from CEO dialogue")
        return jsonify({
            "declined": True,
            "operation_id": operation.id,
            "project_id": operation.project_id,
            "message": "Declined. No Project execution authority was granted.",
        })
    except Exception as ex:
        db.session.rollback()
        return jsonify({"declined":False,"error":str(ex)}),409


@bp.route("/headquarters/ceo/operations/<int:id>/state")
def headquarters_ceo_operation_state(id):
    operation=db.get_or_404(Operation,id)
    live=__import__("eason_one.services.headquarters",fromlist=["operation_live_snapshot"]).operation_live_snapshot(operation)
    return jsonify(live)


@bp.route("/headquarters/ceo/operations/<int:id>/budget",methods=["POST"])
def headquarters_ceo_operation_budget(id):
    operation=db.get_or_404(Operation,id)
    payload=_request_payload()
    try:
        amount=__import__("eason_one.services.operations",fromlist=["budget_authorization_amount"]).budget_authorization_amount(payload.get("additional_budget_twd"))
        contracts = __import__(
            "eason_one.services.project_contract", fromlist=["is_vnext_governed"]
        )
        if operation.project and contracts.is_vnext_governed(operation.project):
            governance = __import__(
                "eason_one.services.governance", fromlist=["request_budget_gate", "resolve_gate"]
            )
            gate = governance.request_budget_gate(
                project=operation.project, additional_twd=amount,
                reason="Founder requested an exact additional Project budget authorization from CEO Mission controls.",
                operation=operation, allow_founder_reconsideration=True,
            )
            governance.resolve_gate(
                gate, "APPROVE",
                reason="Founder explicitly approved this exact additional Project budget amount.",
            )
            __import__(
                "eason_one.services.company_runtime", fromlist=["wake_company_runtime"]
            ).wake_company_runtime()
        else:
            operation.approved_budget_twd=Decimal(operation.approved_budget_twd)+amount
            operation.hard_cost_cap_twd=operation.approved_budget_twd
            db.session.commit()
            __import__("eason_one.services.operations",fromlist=["resume"]).resume(operation)
            _wake_runtime_for(operation)
        live=__import__("eason_one.services.headquarters",fromlist=["operation_live_snapshot"]).operation_live_snapshot(operation)
        return jsonify({"approved":True,"additional_twd":str(amount),"operation":live})
    except Exception as ex:
        db.session.rollback()
        return jsonify({"approved":False,"error":str(ex)}),409


@bp.route("/hq",methods=["GET","POST"])
@bp.route("/headquarters",methods=["GET","POST"])
def headquarters():
    service=__import__(
      "eason_one.services.headquarters",fromlist=["route_ceo_intent", "local_ceo_answer"])
    project_service=__import__(
      "eason_one.services.project_company",fromlist=["home_snapshot", "local_brief"])
    company=get_company()
    if not company:
        return render_template("unseeded.html")
    if request.method=="POST":
        query=(request.form.get("request") or "").strip()
        raw_mode=(request.form.get("mode") or "AUTO").strip()
        if not query:
            flash("Tell the CEO what you need.","error")
            return redirect(url_for("main.headquarters"))
        explicit=(
          "ACT" if raw_mode.lower()=="command" else
          "BRIEF" if raw_mode.lower()=="briefing" else raw_mode
        )
        routed=service.route_ceo_intent(query,explicit)
        if routed["route"]=="BRIEF":
            session["hq_ceo_answer"]=project_service.local_brief(query, project_id=request.form.get("project_id"))
        else:
            try:
                session["hq_ceo_answer"],_,_=_ceo_execution_answer(service,query,request.form.get("project_id"))
            except Exception as ex:
                db.session.rollback()
                summary = str(ex)
                try:
                    ceo_service = __import__(
                        "eason_one.services.ceo", fromlist=["record_ceo_message"]
                    )
                    ceo_service.record_ceo_message(summary)
                except Exception:
                    db.session.rollback()
                session["hq_ceo_answer"]={
                  "title":"CEO request could not be completed",
                  "summary":summary,"facts":[
                    "No successful result was fabricated. Review provider and runtime state in Engine Room."],
                  "href":url_for("main.run_audit")}
        session["hq_ceo_query"]=query
        return redirect(url_for("main.headquarters"))
    answer=session.pop("hq_ceo_answer",None)
    query=session.pop("hq_ceo_query",None)
    started=perf_counter()
    snapshot=project_service.home_snapshot()
    focus_id = request.args.get("project", type=int)
    snapshot["focus_project"] = db.session.get(Project, focus_id) if focus_id else None
    snapshot["snapshot_ms"]=round((perf_counter()-started)*1000,2)
    snapshot["rendered_at"]=datetime.now(timezone.utc).isoformat()
    session["hq_last_visit_at"]=snapshot["rendered_at"]
    if answer:
        # Server-rendered POST replies remain auditable in the persisted CEO thread;
        # the lightweight Project-first HQ does not duplicate them as dashboard cards.
        snapshot["last_answer"]=answer
        snapshot["last_query"]=query
    return render_template("headquarters.html",snapshot=snapshot)


@bp.get("/headquarters/pulse")
def headquarters_pulse():
    service = __import__(
        "eason_one.services.project_company", fromlist=["company_pulse_snapshot"]
    )
    response = Response(
        render_template("_company_pulse.html", pulse=service.company_pulse_snapshot()),
        mimetype="text/html",
    )
    response.headers["Cache-Control"] = "no-store, max-age=0"
    return response


@bp.route("/headquarters/missions/<int:id>")
def headquarters_mission(id):
    service=__import__(
      "eason_one.services.headquarters",fromlist=["mission_snapshot"])
    operation=db.get_or_404(Operation,id)
    kind=__import__(
      "eason_one.services.stabilization",fromlist=["operation_kind","REAL_WORK"]
    )
    if kind.operation_kind(operation)==kind.REAL_WORK:
        session["hq_last_real_mission_id"]=operation.id
    return render_template("hq_mission.html",snapshot=service.mission_snapshot(operation))


@bp.route("/headquarters/employees/<int:id>")
def headquarters_employee(id):
    service=__import__(
      "eason_one.services.headquarters",fromlist=["employee_snapshot"])
    employee=db.get_or_404(Employee,id)
    return render_template("hq_employee.html",snapshot=service.employee_snapshot(employee))


@bp.route("/headquarters/results")
def headquarters_results():
    service=__import__(
      "eason_one.services.project_company",fromlist=["results_snapshot"])
    return render_template("hq_results.html",snapshot=service.results_snapshot())


@bp.route("/headquarters/artifacts/<int:version_id>")
def headquarters_artifact(version_id):
    """Read one accepted DB-native ArtifactVersion as a Founder result."""
    version = db.session.get(ArtifactVersion, version_id)
    if (
        version is None
        or version.status != "ACCEPTED"
        or version.artifact is None
        or version.artifact.project is None
        or version.artifact.project.environment != "LIVE"
    ):
        abort(404)
    artifact_service = __import__(
        "eason_one.services.artifacts", fromlist=["readable_artifact", "deliverable_targets"]
    )
    readable = artifact_service.readable_artifact(version)
    if not readable.get("available"):
        abort(404)
    run = version.execution
    verification_rows = VerificationRecord.query.filter_by(artifact_version_id=version.id).order_by(VerificationRecord.id).all()
    return render_template(
        "hq_artifact.html",
        version=version,
        artifact=version.artifact,
        project=version.artifact.project,
        work=version.artifact.work,
        employee=version.producer,
        run=run,
        readable=readable,
        verifications=verification_rows,
        deliverables=artifact_service.deliverable_targets(version),
    )


@bp.route("/headquarters/artifacts/<int:version_id>/deliverables/<int:index>")
def headquarters_artifact_deliverable(version_id, index):
    """Open one exact accepted Founder deliverable, never an arbitrary repository path."""
    version = db.session.get(ArtifactVersion, version_id)
    if (
        version is None
        or version.status != "ACCEPTED"
        or version.artifact is None
        or version.artifact.project is None
        or version.artifact.project.environment != "LIVE"
    ):
        abort(404)
    artifact_service = __import__(
        "eason_one.services.artifacts", fromlist=["resolve_deliverable"]
    )
    try:
        path = artifact_service.resolve_deliverable(version, index)
    except (FileNotFoundError, OSError, RuntimeError, ValueError):
        abort(404)

    if path.suffix.lower() in {".html", ".htm"}:
        try:
            body = path.read_bytes()
        except OSError:
            abort(404)
        response = Response(body, mimetype="text/html")
        response.headers["Content-Security-Policy"] = (
            "sandbox; default-src 'none'; style-src 'unsafe-inline'; "
            "img-src data: https:; font-src data:; media-src data: https:; "
            "script-src 'none'; connect-src 'none'; form-action 'none'; "
            "object-src 'none'; base-uri 'none'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        return response
    return send_file(path, as_attachment=False, download_name=path.name, conditional=True)


@bp.route("/headquarters/projects")
def headquarters_projects():
    service=__import__(
      "eason_one.services.project_company",fromlist=["projects_snapshot"])
    return render_template("hq_projects.html",snapshot=service.projects_snapshot())


@bp.route("/headquarters/projects/<int:id>")
def headquarters_project(id):
    service=__import__(
      "eason_one.services.project_company",fromlist=["project_snapshot"])
    project=db.get_or_404(Project,id)
    return render_template("hq_project.html",snapshot=service.project_snapshot(project))



@bp.route("/headquarters/projects/<int:id>/governance/<int:escalation_id>", methods=["POST"])
def headquarters_project_governance(id, escalation_id):
    """Resolve one precise Project-scoped Founder authority question."""
    project = db.session.get(Project, id)
    if not project or project.environment != "LIVE":
        abort(404)
    Escalation = __import__("eason_one.models", fromlist=["Escalation"]).Escalation
    escalation = db.session.get(Escalation, escalation_id)
    if not escalation or escalation.project_id != project.id or escalation.state != "OPEN":
        abort(404)
    governance = __import__(
        "eason_one.services.governance", fromlist=["is_founder_type", "normalize_type", "resolve_gate"]
    )
    if not governance.is_founder_type(escalation.escalation_type):
        abort(404)
    payload = _request_payload()
    action = str(payload.get("action") or "").strip().upper()
    kind = governance.normalize_type(escalation.escalation_type)
    changes = None
    if action == "MODIFY":
        if kind == "BUDGET_AUTHORIZATION":
            changes = {
                "additional_budget_twd": payload.get("additional_budget_twd"),
                "scope": "PROJECT",
            }
        elif kind == "PROJECT_DEADLINE_CHANGE":
            changes = {"new_deadline": payload.get("new_deadline")}
        elif kind == "PROJECT_SCOPE_CHANGE":
            criteria = payload.get("success_criteria")
            if isinstance(criteria, str):
                criteria = [row.strip() for row in criteria.splitlines() if row.strip()]
            changes = {
                "objective": payload.get("objective"),
                "success_criteria": criteria,
            }
        elif kind == "PROJECT_CONSTRAINT_CHANGE":
            constraints = payload.get("constraints")
            if isinstance(constraints, str):
                constraints = [row.strip() for row in constraints.splitlines() if row.strip()]
            changes = {"constraints": constraints}
        else:
            raise ValueError(f"{kind} does not support MODIFY from this Project governance action.")
    try:
        decision = governance.resolve_gate(
            escalation, action, reason=payload.get("reason"), changes=changes
        )
        __import__(
            "eason_one.services.company_runtime", fromlist=["wake_company_runtime"]
        ).wake_company_runtime()
    except Exception as exc:
        db.session.rollback()
        if request.accept_mimetypes.best == "application/json":
            return jsonify({"ok": False, "error": str(exc)}), 409
        flash(str(exc), "error")
        return redirect(url_for("main.headquarters_project", id=id))
    if request.accept_mimetypes.best == "application/json":
        return jsonify({"ok": True, "decision_id": decision.id, "action": decision.decision})
    flash("Founder Project governance decision committed.", "success")
    return redirect(url_for("main.headquarters_project", id=id))


@bp.route("/headquarters/projects/<int:id>/cancel", methods=["POST"])
def headquarters_project_cancel(id):
    """Explicit whole-Project cancellation; Mission Stop never uses this path."""
    project = db.session.get(Project, id)
    if not project or project.environment != "LIVE":
        abort(404)
    try:
        governance = __import__(
            "eason_one.services.governance", fromlist=["open_gate", "resolve_gate"]
        )
        gate = governance.open_gate(
            project=project, escalation_type="PROJECT_CANCEL",
            reason="Founder explicitly requested cancellation of the entire Project Contract.",
            authority_payload={},
            recommendation="Cancel the entire Project only if all remaining Work should stop.",
        )
        decision = governance.resolve_gate(
            gate, "APPROVE", reason="Founder explicitly cancelled the entire Project."
        )
    except Exception as exc:
        db.session.rollback()
        if request.accept_mimetypes.best == "application/json":
            return jsonify({"ok": False, "error": str(exc)}), 409
        flash(str(exc), "error")
        return redirect(url_for("main.headquarters_project", id=id))
    if request.accept_mimetypes.best == "application/json":
        return jsonify({"ok": True, "decision_id": decision.id, "project_status": project.status})
    flash("Entire Project cancelled. Mission history remains auditable.", "success")
    return redirect(url_for("main.headquarters_project", id=id))


@bp.route("/headquarters/projects/<int:id>/complete", methods=["POST"])
def headquarters_project_complete(id):
    """Founder accepts the real Project delivery without invoking a model."""
    project = db.session.get(Project, id)
    if not project or project.environment != "LIVE":
        abort(404)
    project_service = __import__(
        "eason_one.services.project_company", fromlist=["project_snapshot"]
    )
    # Completion POST is intentionally replay-safe. A browser/network retry for
    # an already COMPLETED Project is delegated to the canonical outcome service
    # so it can verify and return the exact existing Founder Decision/event pair
    # without minting a second terminal event.
    if project.status not in {"REVIEW", "COMPLETED"}:
        flash("Only a Project with a delivered result can be completed.", "error")
        return redirect(url_for("main.headquarters_project", id=id))
    if project.status == "REVIEW":
        snapshot = project_service.project_snapshot(project)
        if snapshot["working"] or snapshot["attention"]:
            flash("The Project still has live work or a Founder decision gate.", "error")
            return redirect(url_for("main.headquarters_project", id=id))
    # Generic validated Mission/Result rows are not sufficient authority.
    # Founder completion must consume the durable v0.20 PROJECT_RESULT artifact
    # whose verification is bound to the current immutable Project Contract.
    outcome = __import__(
        "eason_one.services.project_outcome", fromlist=["accept_founder_completion"]
    )
    try:
        completion = outcome.accept_founder_completion(project)
    except ValueError as exc:
        flash(f"Project completion is blocked by Project-level proof: {exc}", "error")
        return redirect(url_for("main.headquarters_project", id=id))
    if completion.get("status") == "ALREADY_COMPLETED":
        flash("Project was already completed. Existing Founder acceptance was replayed without duplicate effects.", "success")
    else:
        flash("Project completed. Its Results, Decisions, People, Meetings, and History remain available.", "success")
    return redirect(url_for("main.headquarters_project", id=id))

@bp.route("/headquarters/attention")
def headquarters_attention():
    service=__import__(
      "eason_one.services.headquarters",fromlist=["attention_snapshot"])
    return render_template("hq_attention.html",snapshot=service.attention_snapshot())


@bp.route("/headquarters/missions")
def headquarters_missions():
    service=__import__(
      "eason_one.services.headquarters",fromlist=["missions_snapshot"])
    view=(request.args.get("view") or "real").strip().lower()
    if view not in {"real","validation"}:
        view="real"
    return render_template("hq_missions.html",snapshot=service.missions_snapshot(view=view))


@bp.route("/headquarters/people")
def headquarters_people():
    service=__import__(
      "eason_one.services.headquarters",fromlist=["people_snapshot"])
    return render_template("hq_people.html",snapshot=service.people_snapshot())


@bp.route("/headquarters/people/talent")
def headquarters_talent():
    service=__import__(
      "eason_one.services.headquarters",fromlist=["talent_snapshot"])
    snapshot=service.talent_snapshot(
      (request.args.get("q") or "").strip(),
      (request.args.get("department") or "").strip())
    return render_template("hq_talent.html",snapshot=snapshot)


@bp.route("/headquarters/people/talent/import",methods=["POST"])
def headquarters_talent_import():
    try:
        result=__import__("eason_one.services.workforce",fromlist=["import_founder_library"]).import_founder_library()
        if result["source"]:
            flash(f"Imported {result['imported']} verified role templates from the Founder source.","ok")
        else:
            flash(result["message"],"error")
    except Exception as ex:
        db.session.rollback(); flash(str(ex),"error")
    return redirect(url_for("main.headquarters_talent"))


@bp.route("/headquarters/meetings")
def headquarters_meetings():
    service=__import__(
      "eason_one.services.headquarters",fromlist=["meetings_snapshot"])
    return render_template("hq_meetings.html",snapshot=service.meetings_snapshot())


@bp.route("/headquarters/meetings/<int:id>")
def headquarters_meeting(id):
    service=__import__(
      "eason_one.services.headquarters",fromlist=["meeting_snapshot"])
    meeting=db.get_or_404(Meeting,id)
    return render_template("hq_meeting.html",snapshot=service.meeting_snapshot(meeting))


@bp.route("/headquarters/memory")
def headquarters_memory():
    service=__import__(
      "eason_one.services.headquarters",fromlist=["memory_snapshot"])
    return render_template("hq_memory.html",snapshot=service.memory_snapshot())


@bp.route("/headquarters/finance")
def headquarters_finance():
    service=__import__(
      "eason_one.services.headquarters",fromlist=["finance_snapshot"])
    return render_template("hq_finance.html",snapshot=service.finance_snapshot())


@bp.route("/headquarters/market")
def headquarters_market():
    service=__import__("eason_one.services.market",fromlist=["snapshot"])
    return render_template("hq_market.html",snapshot=service.snapshot())


@bp.route("/headquarters/system")
def headquarters_system():
    service=__import__(
      "eason_one.services.headquarters",fromlist=["system_snapshot"])
    return render_template("hq_system.html",snapshot=service.system_snapshot())


@bp.route("/headquarters/system/models")
def headquarters_models():
    service=__import__(
      "eason_one.services.headquarters",fromlist=["models_snapshot"])
    return render_template("hq_models.html",snapshot=service.models_snapshot())


@bp.route("/headquarters/research")
def headquarters_research():
    service=__import__("eason_one.services.research",fromlist=["snapshot"])
    return render_template("hq_research.html",snapshot=service.snapshot())


@bp.route("/headquarters/research/records",methods=["POST"])
def headquarters_research_record():
    service=__import__("eason_one.services.research",fromlist=["create_record"])
    def optional_int(name):
        value=(request.form.get(name) or "").strip()
        return int(value) if value else None
    metadata={}
    for key in ("baseline","treatment","metric","result","next_action"):
        value=(request.form.get(key) or "").strip()
        if value: metadata[key]=value
    try:
        record=service.create_record(
          record_type=request.form.get("record_type"),title=request.form.get("title"),
          summary=request.form.get("summary"),status=request.form.get("status") or "OPEN",
          version_label=request.form.get("version_label"),project_id=optional_int("project_id"),
          operation_id=optional_int("operation_id"),task_id=optional_int("task_id"),
          meeting_id=optional_int("meeting_id"),agent_run_id=optional_int("agent_run_id"),
          source_ref=request.form.get("source_ref"),metadata=metadata)
        flash(f"Research record {record.record_key} captured.","ok")
    except Exception as ex:
        db.session.rollback(); flash(str(ex),"error")
    return redirect(url_for("main.headquarters_research"))


@bp.route("/headquarters/research/export.json")
def headquarters_research_export_json():
    service=__import__("eason_one.services.research",fromlist=["full_export"])
    payload=json.dumps(service.full_export(),ensure_ascii=False,indent=2)
    return Response(payload,mimetype="application/json",headers={
      "Content-Disposition":"attachment; filename=eason-one-research-export.json"})


@bp.route("/headquarters/research/runs.csv")
def headquarters_research_runs_csv():
    service=__import__("eason_one.services.research",fromlist=["runs_csv"])
    return Response(service.runs_csv(),mimetype="text/csv",headers={
      "Content-Disposition":"attachment; filename=eason-one-agent-runs.csv"})


@bp.route("/headquarters/system/runs")
def headquarters_runs():
    service=__import__(
      "eason_one.services.headquarters",fromlist=["system_runs_snapshot"])
    return render_template("hq_runs.html",snapshot=service.system_runs_snapshot())


@bp.route("/headquarters/system/runs/<int:id>")
def headquarters_run(id):
    service=__import__(
      "eason_one.services.headquarters",fromlist=["run_snapshot"])
    run=db.get_or_404(AgentRun,id)
    return render_template("hq_run.html",snapshot=service.run_snapshot(run))


@bp.route("/mobile",methods=["GET","POST"])
def mobile_ceo_line():
    service=__import__(
      "eason_one.services.headquarters",fromlist=["mobile_snapshot"])
    if not get_company():
        return render_template("unseeded.html")
    if request.method=="POST":
        query=(request.form.get("request") or "").strip()
        mode=(request.form.get("mode") or "briefing").lower()
        if not query:
            return redirect(url_for("main.mobile_ceo_line"))
        if mode=="command":
            try:
                run,proposal=founder_request(
                  Employee.query.filter_by(slug="ceo").one(),query)
                payload=run.parsed_output_json or {}
                href=(url_for("main.headquarters_mission",id=proposal.id)
                  if isinstance(proposal,Operation) else None)
                if payload.get("status_report") and run.status=="SUCCEEDED":
                    session["mobile_ceo_answer"]=service.status_report_answer(run)
                else:
                    session["mobile_ceo_answer"]={
                      "kind":"COMMAND_RESULT",
                      "title":"CEO command received" if run.status=="SUCCEEDED" else "CEO command failed",
                      "summary":payload.get("executive_response") or run.error_text
                        or "The command was recorded.",
                      "facts":[f"Run status: {run.status}"],"run_id":run.id,"href":href}
            except Exception as ex:
                db.session.rollback()
                session["mobile_ceo_answer"]={
                  "title":"CEO command could not be completed",
                  "summary":str(ex),"facts":[],"href":None}
        else:
            session["mobile_ceo_answer"]=service.local_ceo_answer(query,mobile=True)
        session["mobile_ceo_query"]=query
        return redirect(url_for("main.mobile_ceo_line"))
    return render_template("mobile_ceo.html",snapshot=service.mobile_snapshot(),
      ceo_answer=session.pop("mobile_ceo_answer",None),
      ceo_query=session.pop("mobile_ceo_query",None))


@bp.route("/api/headquarters/runtime-focus")
def headquarters_runtime_focus():
    snapshot=__import__(
      "eason_one.services.stabilization",fromlist=["global_runtime_snapshot"]
    ).global_runtime_snapshot()
    return jsonify(snapshot)


@bp.route("/api/headquarters/state")
def headquarters_state():
    service=__import__(
      "eason_one.services.project_company",fromlist=["home_snapshot"])
    snapshot=service.home_snapshot()
    if not snapshot.get("company"):
        return jsonify({"ready":False}),404
    return jsonify({
      "ready":True,
      "company_state":snapshot["state"],
      "projects":len(snapshot["active_projects"]),
      "working_employees":len(snapshot["working"]),
      "attention":len(snapshot["attention"]),
      "focus_project":({
        "id":snapshot["active_projects"][0]["project"].id,
        "title":snapshot["active_projects"][0]["project"].name,
        "state":snapshot["active_projects"][0]["state"],
        "progress":snapshot["active_projects"][0]["progress"],
      } if snapshot["active_projects"] else None)})


@bp.route("/api/headquarters/projects/<int:id>/status")
def headquarters_project_status(id):
    """Project-scoped, read-only status from authoritative persisted Work."""
    project = db.session.get(Project, id)
    if not project or project.environment != "LIVE":
        abort(404)
    service = __import__(
        "eason_one.services.project_company", fromlist=["work_command_snapshot"]
    )
    snapshot = service.work_command_snapshot(project.id)
    return jsonify({
        "project": {"id": project.id, "name": project.name, "status": project.status},
        "counts": snapshot["counts"],
        "work": [{
            "id": row["work"].id,
            "title": row["work"].title,
            "status": row["state"],
            "runtime_state": row["work"].state,
            "project": {"id": row["project"].id, "name": row["project"].name},
            "accountable_employee": ({
                "id": row["employee"].id, "name": row["employee"].name,
            } if row["employee"] else None),
            "waiting_reason": row["wait"].reason if row["wait"] else None,
        } for row in snapshot["rows"]],
    })


@bp.route("/command",methods=["GET","POST"])
def command_center():
    if request.method=="POST": return _command_submit()
    return redirect(url_for("main.headquarters"))

@bp.route("/command/failures/<int:id>/acknowledge",methods=["POST"])
def command_failure_acknowledge(id):
    run=db.get_or_404(AgentRun,id)
    try:
        command_service.resolve_founder_failure(
          run,"ACKNOWLEDGED",request.form.get("note") or "Founder acknowledged")
        flash("The unresolved request was acknowledged; its audit remains unchanged.","ok")
    except Exception:
        db.session.rollback()
    return redirect(url_for("main.headquarters"))

@bp.route("/command/failures/<int:id>/replace",methods=["POST"])
def command_failure_replace(id):
    failed=db.get_or_404(AgentRun,id)
    try:
        replacement,_=founder_request(
          Employee.query.filter_by(slug="ceo").one(),request.form["request"])
        if replacement.status!="SUCCEEDED":
            raise ValueError(
              "Replacement did not complete; the original remains unresolved")
        command_service.resolve_founder_failure(
          failed,"REPLACED","Founder submitted an explicit replacement",
          replacement)
    except Exception:
        db.session.rollback()
    return redirect(url_for("main.headquarters"))

@bp.route("/command/proposals/<int:id>/approve",methods=["POST"])
def command_approve(id):
    # PROJECT_PLAN is historical audit evidence only. Current Project authority
    # is created exclusively through governed CEO OPERATION_PLAN approval.
    proposal=db.get_or_404(Proposal,id)
    if (proposal.payload_json or {}).get("type") == "PROJECT_PLAN":
        abort(410)
    abort(400)

def _founder_operation_destination(operation, *, autorun=False):
    """Land v0.20 Founder decisions on the Project/company surface.

    Mission pages remain an advanced audit/compatibility surface. Once a
    governed Project exists, Founder approval should reveal the Company Runtime
    that just took ownership instead of visually dropping the Founder back into
    the old CEO/Mission control loop.
    """
    if (
        operation.project_id
        and (operation.memory_json or {}).get("runtime_semantics") == "WORK_CORE_V018"
    ):
        return redirect(url_for(
            "main.headquarters_project",
            id=operation.project_id,
            live=1 if autorun else None,
        ))
    return redirect(url_for(
        "main.headquarters_mission", id=operation.id,
        autorun=1 if autorun else None,
        _anchor="ceo-runner" if autorun else None,
    ))


@bp.route("/operations/<int:id>/approve",methods=["POST"])
def operation_approve(id):
    operation=db.get_or_404(Operation,id)
    approved=False
    try:
        __import__(
          "eason_one.services.founder_decisions",
          fromlist=["decide"]).decide(operation,"APPROVE")
        approved=operation.approved_at is not None
        if approved:
            _wake_runtime_for(operation)
        flash("Operation approved. Company runtime started.","ok")
    except Exception as ex:
        db.session.rollback()
        flash(str(ex),"error")
    return _founder_operation_destination(operation, autorun=approved)

@bp.route("/operations/<int:id>/founder-decision",methods=["POST"])
def operation_founder_decision(id):
    operation=db.get_or_404(Operation,id)
    autorun=False
    try:
        action=(request.form.get("action") or "").upper()
        __import__(
          "eason_one.services.founder_decisions",
          fromlist=["decide"]).decide(
            operation,action,
            reason=request.form.get("reason"),
            objective=request.form.get("objective"),
            budget_twd=request.form.get("budget_twd"),
            completion_criteria=request.form.get("completion_criteria"))
        autorun=action in {"APPROVE","MODIFY"} and operation.status=="RUNNING"
        if autorun:
            _wake_runtime_for(operation)
    except Exception as ex:
        db.session.rollback()
        flash(str(ex),"error")
    return _founder_operation_destination(operation, autorun=autorun)

@bp.route("/operations/<int:id>")
def operation_detail(id):
    db.get_or_404(Operation,id)
    return redirect(url_for("main.headquarters_mission",id=id))

@bp.route("/operations/<int:id>/next-step",methods=["POST"])
def operation_next_step(id):
    operation=db.get_or_404(Operation,id)
    company_runtime=__import__(
        "eason_one.services.company_runtime",fromlist=["is_work_vnext","wake_company_runtime"]
    )
    if company_runtime.is_work_vnext(operation):
        # Compatibility endpoint only. Founder/browser stepping is not an
        # execution mechanism for Work-first Projects.
        company_runtime.wake_company_runtime()
        return jsonify({
            "status": operation.status,
            "continue_allowed": True,
            "runtime_owner": "COMPANY_RUNTIME",
            "message": "Approved Work advances autonomously.",
        }), 202
    try:
        result=__import__("eason_one.services.operations",fromlist=["next_step"]).next_step(
          operation,request.headers.get("Idempotency-Key") or request.form.get("idempotency_key") or f"browser-{id}")
        state=__import__("eason_one.services.operations",fromlist=["state"]).state(operation)
        return jsonify(result|{"operation_status":state["status"],
          "continue_allowed":state["continue_allowed"]})
    except Exception as ex: return jsonify({"error":str(ex),"status":operation.status}),409

@bp.route("/operations/<int:id>/runtime/start", methods=["POST"])
def operation_runtime_start(id):
    operation = db.get_or_404(Operation,id)
    company_runtime = __import__(
        "eason_one.services.company_runtime",
        fromlist=["is_work_vnext", "wake_company_runtime", "runtime_snapshot"],
    )
    if company_runtime.is_work_vnext(operation):
        # Compatibility only: the Company Runtime is already responsible for
        # approved Work. This request cannot resume/step business state.
        company_runtime.wake_company_runtime()
        return jsonify({
            "runtime": company_runtime.runtime_snapshot(operation),
            "operation": __import__(
                "eason_one.services.headquarters", fromlist=["operation_live_snapshot"]
            ).operation_live_snapshot(operation),
            "message": "Company Runtime already owns this Project.",
        }), 202
    try:
        archive = dict((operation.memory_json or {}).get("archive") or {})
        if archive or (operation.founder_report_json or {}).get("decision_kind") == "RUNTIME_VALIDATION_ARCHIVE":
            raise ValueError("This Mission is archived and cannot resume.")
        if operation.status == "PAUSED":
            report = operation.founder_report_json or {}
            decision_kind = report.get("decision_kind")
            if decision_kind == "ENGINEERING_RUNTIME_REPAIR":
                __import__(
                    "eason_one.services.engineering_runtime",
                    fromlist=["prepare_engineering_repair"],
                ).prepare_engineering_repair(operation, activate=True)
            elif decision_kind == "ENGINEERING_STEP_REVIEW":
                raise ValueError(
                    "The bounded Engineer/Codex step is complete. Founder review is required; "
                    "the generic Resume control cannot launch the next Critic or paid step."
                )
            else:
                __import__("eason_one.services.operations", fromlist=["resume"]).resume(operation)
        elif operation.status == "WAITING_FOR_FOUNDER":
            raise ValueError("Founder authority is required before this Mission can resume")
        elif operation.status != "RUNNING":
            raise ValueError(f"Operation in {operation.status} cannot start")
        state = _wake_runtime_for(operation)
        operation = db.session.get(Operation, id)
        live = __import__(
            "eason_one.services.headquarters", fromlist=["operation_live_snapshot"]
        ).operation_live_snapshot(operation)
        return jsonify({"runtime": state, "operation": live})
    except Exception as ex:
        db.session.rollback()
        operation = db.session.get(Operation, id)
        return jsonify({"error": str(ex), "status": operation.status if operation else "MISSING"}), 409

@bp.route("/operations/<int:id>/runtime")
def operation_runtime_state(id):
    operation = db.get_or_404(Operation,id)
    company_runtime = __import__(
        "eason_one.services.company_runtime", fromlist=["is_work_vnext", "runtime_snapshot"]
    )
    state = (
        company_runtime.runtime_snapshot(operation)
        if company_runtime.is_work_vnext(operation)
        else __import__(
            "eason_one.services.operation_runtime", fromlist=["runtime_snapshot"]
        ).runtime_snapshot(operation)
    )
    live = __import__(
        "eason_one.services.headquarters", fromlist=["operation_live_snapshot"]
    ).operation_live_snapshot(operation)
    return jsonify({"runtime": state, "operation": live})


@bp.route("/operations/<int:id>/archive-validation", methods=["POST"])
def operation_archive_validation(id):
    operation = db.get_or_404(Operation,id)
    try:
        reason = (request.form.get("reason") or "").strip() or None
        result = __import__(
            "eason_one.services.engineering_runtime",
            fromlist=["archive_validation_mission"],
        ).archive_validation_mission(operation, reason=reason)
        if request.accept_mimetypes.best == "application/json" or request.headers.get("Accept") == "application/json":
            operation = db.session.get(Operation, id)
            live = __import__(
                "eason_one.services.headquarters", fromlist=["operation_live_snapshot"]
            ).operation_live_snapshot(operation)
            state = __import__(
                "eason_one.services.operation_runtime", fromlist=["runtime_snapshot"]
            ).runtime_snapshot(operation)
            return jsonify({"result": result, "runtime": state, "operation": live})
        flash("Runtime validation Mission archived. No downstream Provider was started.", "ok")
    except Exception as ex:
        db.session.rollback()
        if request.accept_mimetypes.best == "application/json" or request.headers.get("Accept") == "application/json":
            return jsonify({"error": str(ex)}), 409
        flash(str(ex), "error")
    return redirect(url_for("main.headquarters_mission", id=id))


@bp.route("/operations/<int:id>/pause",methods=["POST"])
def operation_pause(id):
    operation=db.get_or_404(Operation,id)
    try: __import__("eason_one.services.operations",fromlist=["pause"]).pause(operation)
    except Exception as ex: flash(str(ex),"error")
    return redirect(url_for("main.headquarters_mission",id=id))

@bp.route("/operations/<int:id>/stop",methods=["POST"])
def operation_stop(id):
    operation=db.get_or_404(Operation,id)
    try:
        __import__("eason_one.services.operations",fromlist=["stop"]).stop(operation)
    except Exception as ex:
        db.session.rollback()
        flash(str(ex),"error")
    return redirect(url_for("main.headquarters_mission",id=id))

@bp.route("/operations/<int:id>/budget",methods=["POST"])
def operation_budget(id):
    operation=db.get_or_404(Operation,id)
    try:
        core = __import__(
          "eason_one.services.core_v018", fromlist=["is_dormant_legacy_project_operation"]
        )
        if core.is_dormant_legacy_project_operation(operation):
            raise ValueError("Historical pre-v0.18 Project authority cannot be modified.")
        amount=__import__(
          "eason_one.services.operations",
          fromlist=["budget_authorization_amount"]
        ).budget_authorization_amount(request.form["additional_budget_twd"])
        contracts = __import__(
          "eason_one.services.project_contract", fromlist=["is_vnext_governed"]
        )
        if operation.project and contracts.is_vnext_governed(operation.project):
            governance = __import__(
              "eason_one.services.governance", fromlist=["request_budget_gate", "resolve_gate"]
            )
            gate = governance.request_budget_gate(
              project=operation.project, additional_twd=amount,
              reason="Founder requested an exact additional Project budget authorization from Mission controls.",
              operation=operation, allow_founder_reconsideration=True,
            )
            governance.resolve_gate(
              gate, "APPROVE",
              reason="Founder explicitly approved this exact additional Project budget amount.",
            )
            __import__(
              "eason_one.services.company_runtime", fromlist=["wake_company_runtime"]
            ).wake_company_runtime()
        else:
            operation.approved_budget_twd=(
              Decimal(operation.approved_budget_twd)+amount)
            operation.hard_cost_cap_twd=operation.approved_budget_twd
            db.session.commit()
            __import__(
              "eason_one.services.operations",fromlist=["resume"]).resume(operation)
        flash(f"Founder authorized an additional Project NT$ {amount}.","ok")
    except Exception as ex: flash(str(ex),"error")
    return redirect(url_for("main.headquarters_mission",id=id,autorun=1))

@bp.route("/work")
def work(): return redirect(url_for("main.headquarters_projects"))

@bp.route("/team")
def team(): return redirect(url_for("main.headquarters_people"))

@bp.route("/team/hr")
def team_hr():
    return redirect(url_for("main.headquarters_talent"))

def _legacy_team_hr():
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
    return redirect(url_for("main.headquarters_talent"))

@bp.route("/team/hr/requests",methods=["POST"])
def hiring_request_create():
    try:
        service=__import__("eason_one.services.workforce",fromlist=["request_hire","assess_request"])
        item=service.request_hire(
          requested_by_type="FOUNDER",role_needed=request.form["role_needed"],
          problem=request.form["problem"],
          why_now=request.form.get("why_now") or "Founder requested HR assessment.",
          responsibilities=[x.strip() for x in request.form.get(
            "responsibilities","").splitlines() if x.strip()] or [
              "Solve the stated capability problem"],
          capabilities=[x.strip() for x in request.form.get(
            "capabilities","").splitlines() if x.strip()] or [
              request.form["role_needed"]],
          urgency=request.form.get("urgency") or "MEDIUM",
          use_frequency=request.form.get("use_frequency") or "OCCASIONAL",
          talent_template=(db.session.get(
            TalentTemplate,int(request.form["talent_template_id"]))
            if request.form.get("talent_template_id") else None))
        hr=Employee.query.filter_by(slug="hr-director").one()
        if hr.current_model:
            service.assess_request(item)
            flash("HR Director completed the authorized assessment. Founder review is ready.","ok")
        else:
            flash("Hiring need recorded. Assign an active model to HR Director before assessment.","error")
    except Exception as ex: flash(str(ex),"error")
    return redirect(url_for("main.headquarters_talent"))

@bp.route("/employees/<int:id>/hiring-requests",methods=["POST"])
def management_hiring_request(id):
    manager=db.get_or_404(Employee,id)
    operation=db.get_or_404(Operation,request.form["operation_id"])
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
    item=db.get_or_404(HiringRequest,id)
    try:
        __import__("eason_one.services.workforce",fromlist=["review_request"]).review_request(
          item,existing_staff_alternative=request.form["existing_staff_alternative"],
          recommendation=request.form["recommendation"],
          recommended_model=db.session.get(ModelConfig,request.form.get("model_config_id")),
          estimated_input_tokens=request.form["estimated_input_tokens"],
          estimated_output_tokens=request.form["estimated_output_tokens"],
          expected_calls=request.form["expected_calls"],
          max_mission_budget_twd=Decimal(request.form["max_mission_budget_twd"]),
          expected_benefit=request.form["expected_benefit"],
          redundancy_risk=request.form["redundancy_risk"],
          alternatives=[x.strip() for x in request.form["alternatives"].splitlines() if x.strip()],
          success_criteria=[x.strip() for x in request.form["success_criteria"].splitlines() if x.strip()],
          probation_assignments=request.form["probation_assignments"])
        flash(
          "Company materialized the delegated Project hire; no Founder approval was required."
          if item.status == "HIRED" else "HR proposal is ready for Founder review.",
          "ok")
    except Exception as ex: flash(str(ex),"error")
    return redirect(url_for("main.headquarters_talent"))

@bp.route("/team/hr/requests/<int:id>/approve",methods=["POST"])
def hiring_request_approve(id):
    try:
        employee=__import__("eason_one.services.workforce",fromlist=["approve_hire"]).approve_hire(
          db.get_or_404(HiringRequest,id))
        flash(f"{employee.name} hired on probation. No provider call was made.","ok")
    except Exception as ex: flash(str(ex),"error")
    return redirect(url_for("main.headquarters_talent"))

@bp.route("/team/hr/requests/<int:id>/reject",methods=["POST"])
def hiring_request_reject(id):
    try: __import__("eason_one.services.workforce",fromlist=["reject_request"]).reject_request(
      db.get_or_404(HiringRequest,id),request.form.get("note") or "Founder rejected")
    except Exception as ex: flash(str(ex),"error")
    return redirect(url_for("main.headquarters_talent"))

@bp.route("/company")
def company_hub():
    return redirect(url_for("main.headquarters"))

@bp.route("/company/brain")
def company_brain():
    return redirect(url_for("main.headquarters_memory"))

@bp.route("/company/runs")
def run_audit():
    return redirect(url_for("main.headquarters_runs"))

@bp.route("/projects")
def projects():
    return redirect(url_for("main.headquarters_projects"))
@bp.route("/projects/existing",methods=["POST"])
def existing_project():
    # Retired current writer: governed Projects enter through CEO intake so the
    # immutable Project Contract is created with the authority source.
    abort(410)
@bp.route("/projects/<int:id>/classification",methods=["POST"])
def project_classification(id):
    try: classify(db.get_or_404(Project,id),request.form["environment"])
    except Exception as ex: flash(str(ex),"error")
    return redirect(request.referrer or url_for("main.projects"))
@bp.route("/projects/new",methods=["POST"])
def project_new():
    abort(410)
@bp.route("/projects/<int:id>")
def project_detail(id):
    db.get_or_404(Project,id)
    return redirect(url_for("main.headquarters_project",id=id))
@bp.route("/projects/<int:id>/tasks",methods=["POST"])
def task_new(id):
    abort(410)
@bp.route("/tasks/<int:id>/run",methods=["POST"])
def task_run(id):
    abort(410)
@bp.route("/tasks/<int:id>/review",methods=["POST"])
def task_review(id):
    abort(410)
@bp.route("/tasks/<int:id>/run-review",methods=["POST"])
def task_run_review(id):
    abort(410)
@bp.route("/projects/<int:id>/briefing",methods=["POST"])
def project_briefing(id):
    # Paid Project synthesis without a governing Work was an authority bypass.
    abort(410)
@bp.route("/projects/<int:id>/knowledge",methods=["POST"])
def project_knowledge(id):
    p=db.get_or_404(Project,id)
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
    return redirect(url_for("main.headquarters_people"))

@bp.route("/employees/<int:id>")
def employee_detail(id):
    db.get_or_404(Employee,id)
    return redirect(url_for("main.headquarters_employee",id=id))
@bp.route("/employees/<int:id>/learning",methods=["POST"])
def employee_learning(id):
    e=db.get_or_404(Employee,id); project=db.session.get(Project,request.form.get("project_id")) if request.form.get("project_id") else None
    task=db.session.get(Task,request.form.get("task_id")) if request.form.get("task_id") else None
    try: create_learning(e,request.form["title"],request.form["content"],project,task,request.form.get("source_ref"),request.form.get("validated")=="1")
    except Exception as ex: flash(str(ex),"error")
    return redirect(url_for("main.employee_detail",id=id))
@bp.route("/employees/<int:id>/learning/<int:record_id>/validate",methods=["POST"])
def employee_learning_validate(id, record_id):
    e=db.get_or_404(Employee,id)
    record=db.get_or_404(__import__("eason_one.models",fromlist=["EmployeeLearningRecord"]).EmployeeLearningRecord,record_id)
    if record.employee_id!=e.id:
        abort(404)
    __import__("eason_one.services.ceo_learning",fromlist=["validate_learning"]).validate_learning(record)
    flash("Founder validated this Employee learning. Future context may use it according to the Employee role.","ok")
    return redirect(url_for("main.headquarters_employee",id=id)+"#learning")


@bp.route("/employees/<int:id>/model",methods=["POST"])
def employee_model(id):
    e=db.get_or_404(Employee,id); model=db.get_or_404(ModelConfig,request.form["model_config_id"])
    if not model.active or model.archived: abort(400)
    reason=(request.form.get("reason") or "Founder reassignment").strip()
    change_model(e,model,reason)
    flash(f"AI Core changed to {model.label}. Employee identity and history were preserved.","ok")
    return redirect(url_for("main.headquarters_employee",id=id)+"#ai-core")
@bp.route("/employees/<int:id>/interview",methods=["GET","POST"])
def interview(id):
    e=db.get_or_404(Employee,id); interview=FounderInterview.query.filter_by(employee_id=id,ended_at=None).order_by(FounderInterview.started_at.desc()).first()
    if not interview: interview=start(e)
    if request.method=="POST":
        try:
            ask(interview,request.form["content"],request_id=request.form.get("request_id"))
            return redirect(url_for("main.interview",id=id))
        except Exception as ex: flash(str(ex),"error")
    return render_template("interview.html",e=e,interview=interview,interview_request_id=str(uuid4()))

@bp.route("/inbox")
def inbox():
    return redirect(url_for("main.headquarters_attention"))
@bp.route("/inbox/<int:id>/materialize",methods=["POST"])
def materialize(id):
    proposal=db.get_or_404(Proposal,id)
    if (proposal.payload_json or {}).get("type") == "PROJECT_PLAN":
        abort(410)
    abort(400)
@bp.route("/inbox/<int:id>/reject",methods=["POST"])
def reject(id):
    p=db.get_or_404(Proposal,id)
    if p.status!="PENDING": abort(400)
    p.status="REJECTED"; p.reviewed_at=now(); db.session.commit(); return redirect(url_for("main.inbox"))
@bp.route("/inbox/<int:id>/knowledge-review",methods=["POST"])
def knowledge_review(id):
    p=db.get_or_404(Proposal,id); decision=request.form["decision"]
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
              request.form["input_price_per_million"],request.form["output_price_per_million"],request.form["currency"],request.form["max_output_tokens"],request.form.get("request_price_per_call","0"))
            flash("Model configuration created.","ok")
        except Exception as ex:
            db.session.rollback(); flash(str(ex),"error")
    return redirect(url_for("main.headquarters_models"))
@bp.route("/models/<int:id>/edit",methods=["POST"])
def model_edit(id):
    model=db.get_or_404(ModelConfig,id)
    try:
        model_config_service.edit(model,request.form["label"],request.form["model_name"],
          request.form["input_price_per_million"],request.form["output_price_per_million"],
          request.form["currency"],request.form["max_output_tokens"],request.form.get("request_price_per_call","0"))
    except Exception as ex: db.session.rollback(); flash(str(ex),"error")
    return redirect(url_for("main.headquarters_models"))
@bp.route("/models/<int:id>/toggle",methods=["POST"])
def model_toggle(id):
    try: model_config_service.toggle(db.get_or_404(ModelConfig,id))
    except Exception as ex: flash(str(ex),"error")
    return redirect(url_for("main.headquarters_models"))
@bp.route("/models/<int:id>/archive",methods=["POST"])
def model_archive(id):
    try:
        model_config_service.archive(db.get_or_404(ModelConfig,id))
        flash("Model configuration archived.", "ok")
    except Exception as ex:
        db.session.rollback(); flash(str(ex), "error")
    return redirect(url_for("main.headquarters_models"))
@bp.route("/models/<int:id>/restore",methods=["POST"])
def model_restore(id):
    try:
        model_config_service.restore(db.get_or_404(ModelConfig,id))
        flash("Model configuration restored and activated.", "ok")
    except Exception as ex:
        db.session.rollback(); flash(str(ex), "error")
    return redirect(url_for("main.headquarters_models"))
@bp.route("/models/<int:id>/delete",methods=["POST"])
def model_delete(id):
    try: model_config_service.delete(db.get_or_404(ModelConfig,id))
    except Exception as ex: flash(str(ex),"error")
    return redirect(url_for("main.headquarters_models"))
@bp.route("/costs")
def costs():
    return redirect(url_for("main.headquarters_finance"))
@bp.route("/meetings",methods=["GET","POST"])
def meetings():
    if request.method=="POST":
        try:
            chair=db.get_or_404(Employee,request.form["chair_employee_id"])
            participants=Employee.query.filter(Employee.id.in_([int(x) for x in request.form.getlist("participant_ids")])).all()
            project=db.session.get(Project,request.form.get("project_id")) if request.form.get("project_id") else None
            profile=request.form.get("execution_profile","STANDARD")
            defaults=meeting_service.PROFILES[profile]
            mission=request.form.get("mission_context") or request.form["purpose"]
            question=request.form.get("meeting_question") or request.form.get("agenda") or request.form["purpose"]
            created=meeting_service.create(request.form["title"],mission,question,chair,participants,project,
              request.form.get("max_rounds") or defaults["max_rounds"],request.form.get("token_limit") or defaults["tokens"],
              request.form.get("real_cost_limit_twd") or defaults["cost"],profile=profile,
              max_speakers_per_round=request.form.get("max_speakers_per_round") or defaults["max_speakers"],
              contribution_output_cap=request.form.get("contribution_output_cap") or defaults["contribution_cap"],
              router_output_cap=request.form.get("router_output_cap") or defaults["router_cap"],
              synthesis_output_cap=request.form.get("synthesis_output_cap") or defaults["synthesis_cap"])
            return redirect(url_for("main.headquarters_meeting",id=created.id))
        except Exception as ex:
            flash(str(ex),"error")
            return redirect(url_for("main.meetings"))

    # Compatibility contract: the proven Meeting Lobby remains a direct,
    # zero-provider GET surface. /headquarters/meetings remains available too.
    rows=[]
    for meeting in Meeting.query.order_by(Meeting.created_at.desc()).all():
        tokens,cost=meeting_service.usage(meeting)
        result=meeting_service.result_view(meeting)
        participant_rows=(MeetingParticipant.query.filter_by(meeting_id=meeting.id)
          .filter(MeetingParticipant.removed_at.is_(None)).order_by(MeetingParticipant.id).all())
        participant_names=[row.employee.name for row in participant_rows if row.employee is not None]
        recovered=bool((result or {}).get("recovered"))
        if recovered:
            founder_status="RESULT RECOVERED"
        elif meeting.status=="TERMINATED_BY_FOUNDER":
            founder_status="TERMINATED"
        elif meeting.status=="WAITING_FOR_FOUNDER":
            founder_status="NEEDS FOUNDER"
        elif meeting.status in {"RUNNING","ACTIVE"}:
            founder_status="LIVE"
        elif meeting.status=="PAUSED":
            founder_status="PAUSED"
        elif meeting.status=="ENDED":
            founder_status="COMPLETED"
        else:
            founder_status=meeting.status.replace("_"," ")
        result_excerpt=None
        if result:
            agreement=result.get("agreement")
            if isinstance(agreement,list):
                agreement=agreement[0] if agreement else None
            result_excerpt=result.get("position") or agreement
        rows.append({
            "meeting":meeting,
            "tokens":tokens,
            "cost":cost,
            "calls":meeting_service.confirmed_provider_calls(meeting),
            "founder_status":founder_status,
            "participant_label":", ".join(participant_names) or "No participants",
            "result_excerpt":result_excerpt,
        })
    return render_template("meetings.html",rows=rows,
      employees=Employee.query.filter_by(active=True).all(),
      projects=Project.query.filter(
        Project.environment=="LIVE",
        ~Project.status.in_(["COMPLETED","FAILED","CANCELLED"]),
      ).all(),
      profiles=meeting_service.PROFILES)
@bp.route("/meetings/<int:id>")
def meeting_room(id):
    db.get_or_404(Meeting,id)
    return redirect(url_for("main.headquarters_meeting",id=id))

def _assert_meeting_current(meeting):
    if meeting.operation and __import__(
        "eason_one.services.core_v018", fromlist=["is_dormant_legacy_project_operation"]
    ).is_dormant_legacy_project_operation(meeting.operation):
        raise ValueError("Historical pre-v0.18 Project Meetings are audit-only and cannot be mutated or restarted.")

def _meeting_action(id,fn,*args):
    meeting=db.get_or_404(Meeting,id)
    try:
        _assert_meeting_current(meeting)
        fn(meeting,*args)
    except Exception as ex: flash(str(ex),"error")
    return redirect(url_for("main.headquarters_meeting",id=id))
@bp.route("/meetings/<int:id>/start",methods=["POST"])
def meeting_start(id):
    meeting=db.get_or_404(Meeting,id)
    try:
        _assert_meeting_current(meeting)
        meeting_service.start_auto(meeting)
    except Exception as ex: flash(str(ex),"error"); return redirect(url_for("main.headquarters_meeting",id=id))
    return redirect(url_for("main.headquarters_meeting",id=id,autorun=1))
@bp.route("/meetings/<int:id>/next-step",methods=["POST"])
def meeting_next_step(id):
    meeting=db.get_or_404(Meeting,id)
    try:
        _assert_meeting_current(meeting)
        return jsonify(meeting_service.next_step_idempotent(meeting,request.headers.get("Idempotency-Key")))
    except Exception as ex: return jsonify(meeting_service.auto_state(meeting)|{"error":str(ex)}),409
@bp.route("/meetings/<int:id>/pause",methods=["POST"])
def meeting_pause(id): return _meeting_action(id,meeting_service.pause)
@bp.route("/meetings/<int:id>/resume",methods=["POST"])
def meeting_resume(id):
    meeting=db.get_or_404(Meeting,id)
    try:
        _assert_meeting_current(meeting)
        meeting_service.resume(meeting)
    except Exception as ex: flash(str(ex),"error"); return redirect(url_for("main.headquarters_meeting",id=id))
    return redirect(url_for("main.headquarters_meeting",id=id,autorun=1))
@bp.route("/meetings/<int:id>/retry-paid-step",methods=["POST"])
def meeting_retry_paid_step(id):
    meeting=db.get_or_404(Meeting,id)
    try:
        _assert_meeting_current(meeting)
        meeting_service.retry_paid_step(meeting, actor_type="FOUNDER_CONTROL")
    except Exception as ex: flash(str(ex),"error"); return redirect(url_for("main.headquarters_meeting",id=id))
    return redirect(url_for("main.headquarters_meeting",id=id,autorun=1))
@bp.route("/meetings/<int:id>/accept-existing-response",methods=["POST"])
def meeting_accept_existing_response(id):
    meeting=db.get_or_404(Meeting,id)
    try:
        _assert_meeting_current(meeting)
        meeting_service.accept_existing_response(meeting)
    except Exception as ex: flash(str(ex),"error"); return redirect(url_for("main.headquarters_meeting",id=id))
    return redirect(url_for("main.headquarters_meeting",id=id,autorun=1))
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
    message=db.get_or_404(MeetingMessage,id)
    try: meeting_service.feedback(message,request.form["signal"],request.form.get("note"))
    except Exception as ex: flash(str(ex),"error")
    return redirect(url_for("main.headquarters_meeting",id=message.meeting_id))
@bp.route("/runs/<int:id>")
def run_detail(id):
    db.get_or_404(AgentRun,id)
    return redirect(url_for("main.headquarters_run",id=id))
