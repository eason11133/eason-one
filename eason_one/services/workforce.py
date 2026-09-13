import csv
import hashlib
import json
import re
from decimal import Decimal, ROUND_CEILING
from datetime import timedelta
from pathlib import Path

from ..extensions import db
from ..models import (
    AgentRun, Decision, Department, Employee, EmployeeModelHistory, HiringRequest,
    ModelConfig, Position, TalentTemplate, now,
)
from .company import remaining
from .costs import calculate, estimate_execution
from .execution import execute
from ..schemas import HR_ASSESSMENT_SCHEMA

TEMPLATE_FIELDS = {
    "name", "role_title", "department_hint", "mission", "responsibilities",
    "instructions", "skills", "suggested_tools", "deliverables",
    "success_metrics", "source",
}
REQUEST_STATES = {
    "REQUESTED", "HR_REVIEW", "FOUNDER_REVIEW", "APPROVED", "REJECTED",
    "HIRED", "CANCELLED", "ASSESSMENT_COMPLETE", "SYSTEM_RECOVERY",
}

HR_ASSESSMENT_MAX_AUTOMATIC_ATTEMPTS = 3
HR_ASSESSMENT_RETRY_DELAYS_SECONDS = (10, 30, 120)
HR_ASSESSMENT_OUTPUT_POLICY = "MODEL_CONFIG_FULL_ENVELOPE_WITH_ONE_COMPACT_RETRY"


def _list(value, field):
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise ValueError(f"{field} must be a list of text values")
    return value


def _source_key(payload):
    raw = "|".join((
        payload["source"], payload["name"], payload["role_title"],
        payload["mission"],
    ))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def create_talent_template(payload):
    if not isinstance(payload, dict) or not TEMPLATE_FIELDS.issubset(payload):
        raise ValueError("TalentTemplate fields are incomplete")
    for field in ("name", "role_title", "mission", "instructions", "source"):
        if not isinstance(payload[field], str) or not payload[field].strip():
            raise ValueError(f"TalentTemplate {field} is required")
    key = payload.get("source_key") or _source_key(payload)
    existing = TalentTemplate.query.filter_by(source_key=key).first()
    if existing:
        return existing
    template = TalentTemplate(
        name=payload["name"].strip(),
        role_title=payload["role_title"].strip(),
        department_hint=(payload.get("department_hint") or "").strip() or None,
        mission=payload["mission"].strip(),
        responsibilities_json=_list(payload["responsibilities"], "responsibilities"),
        instructions=payload["instructions"].strip(),
        skills_json=_list(payload["skills"], "skills"),
        suggested_tools_json=_list(payload["suggested_tools"], "suggested_tools"),
        deliverables_json=_list(payload["deliverables"], "deliverables"),
        success_metrics_json=_list(payload["success_metrics"], "success_metrics"),
        source=payload["source"].strip(),
        source_key=key,
        active_in_pool=bool(payload.get("active_in_pool", True)),
        suggested_model_class=payload.get("suggested_model_class"),
        notes=payload.get("notes"),
    )
    db.session.add(template)
    db.session.commit()
    return template


def _split(value):
    if isinstance(value, list):
        return value
    if value is None or value == "":
        return []
    return [item.strip() for item in str(value).split("|") if item.strip()]


def import_talent(path):
    path = Path(path)
    if path.suffix.lower() == ".json":
        rows = json.loads(path.read_text(encoding="utf-8-sig"))
    elif path.suffix.lower() == ".csv":
        with path.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
    else:
        raise ValueError("Talent importer supports JSON or CSV")
    if not isinstance(rows, list):
        raise ValueError("Talent source must contain a list")
    imported = []
    for row in rows:
        normalized = dict(row)
        for field in (
            "responsibilities", "skills", "suggested_tools", "deliverables",
            "success_metrics",
        ):
            normalized[field] = _split(normalized.get(field))
        imported.append(create_talent_template(normalized))
    return imported


def find_founder_talent_source():
    root = Path(__file__).resolve().parents[2]
    candidates = []
    for pattern in (
        "talent-library*.json", "talent_library*.json", "talent-pool*.json",
        "ai-employees*.json", "talent-library*.csv", "talent_library*.csv",
        "talent-pool*.csv", "ai-employees*.csv",
    ):
        candidates.extend(root.glob(pattern))
        candidates.extend((root / "data").glob(pattern) if (root / "data").exists() else [])
    return sorted(candidates)[0] if candidates else None


def import_founder_library():
    source = find_founder_talent_source()
    if source is None:
        return {
            "imported": 0,
            "source": None,
            "message": "147-source file required for import.",
        }
    items = import_talent(source)
    return {"imported": len(items), "source": str(source), "message": "Imported"}


def request_hire(*, requested_by_type, role_needed, problem, why_now,
                 responsibilities, capabilities, urgency, use_frequency,
                 requester=None, operation=None, project=None,
                 talent_template=None, source_execution_id=None, source_work=None):
    if source_execution_id is not None:
        existing = HiringRequest.query.filter_by(source_execution_id=source_execution_id).first()
        if existing:
            return existing
    if source_work is not None:
        control = dict(source_work.runtime_control_json or {})
        staffing = dict(control.get("team_formation") or {})
        prior_id = staffing.get("hiring_request_id")
        prior = db.session.get(HiringRequest, prior_id) if prior_id else None
        if prior and prior.status != "REJECTED":
            return prior
    if project is not None and __import__(
        "eason_one.services.work_runtime", fromlist=["project_is_terminal"]
    ).project_is_terminal(project):
        raise ValueError(
            f"PROJECT_TERMINAL_HIRING_REQUEST_FORBIDDEN:{str(project.status or '').upper()}"
        )
    if requested_by_type not in {"FOUNDER", "EMPLOYEE"}:
        raise ValueError("Unknown hiring requester type")
    if requested_by_type == "EMPLOYEE":
        if not requester or requester.position.level < 3:
            raise ValueError("Only management Employees may submit a hiring request")
    for value, label in (
        (role_needed, "role"), (problem, "problem"), (why_now, "why now"),
    ):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"Hiring {label} is required")
    hr=Employee.query.filter_by(slug="hr-director").first()
    assessment_budget=(assessment_authorization(hr) if
      requested_by_type=="FOUNDER" and hr and hr.current_model else None)
    request = HiringRequest(
        source_execution_id=source_execution_id,
        requested_by_type=requested_by_type,
        requester_employee_id=getattr(requester, "id", None),
        operation_id=getattr(operation, "id", None),
        project_id=getattr(project, "id", None),
        talent_template_id=getattr(talent_template, "id", None),
        role_needed=role_needed.strip(),
        problem=problem.strip(),
        why_now=why_now.strip(),
        responsibilities_json=_list(responsibilities, "responsibilities"),
        capabilities_json=_list(capabilities, "capabilities"),
        urgency=urgency,
        use_frequency=use_frequency,
        status="REQUESTED",
        assessment_budget_twd=assessment_budget,
    )
    db.session.add(request)
    db.session.flush()
    # A management-originated staffing request tied to an executing vNext Work
    # becomes explicit Work truth. It blocks only the affected Work; unrelated
    # Employees continue while HR evaluates the capability gap.
    if project is not None:
        models = __import__("eason_one.models", fromlist=["AgentRun", "Work"])
        resolved_source_work = source_work
        if resolved_source_work is None and source_execution_id is not None:
            source_run = db.session.get(models.AgentRun, source_execution_id)
            resolved_source_work = db.session.get(models.Work, source_run.work_id) if source_run and source_run.work_id else None
        if resolved_source_work and resolved_source_work.project_id == getattr(project, "id", None):
            __import__(
                "eason_one.services.work_runtime", fromlist=["open_wait"]
            ).open_wait(
                resolved_source_work, "CAPABILITY_GAP",
                f"Work requires governed staffing capability: {request.role_needed} (HiringRequest #{request.id}).",
                issue_code=f"HIRING_REQUEST_{request.id}",
            )
            control = dict(resolved_source_work.runtime_control_json or {})
            staffing = dict(control.get("team_formation") or {})
            staffing["hiring_request_id"] = request.id
            staffing.setdefault("state", "STAFFING_REQUESTED")
            control["team_formation"] = staffing
            resolved_source_work.runtime_control_json = control
    db.session.commit()
    return request


def estimate_mission_cost(model, input_tokens, output_tokens, calls):
    return calculate(
        model, int(input_tokens) * int(calls), int(output_tokens) * int(calls)
    )


def assessment_authorization(hr):
    if not hr or not hr.current_model:
        return None
    testing = bool(__import__("flask", fromlist=["current_app"]).current_app.config.get("TESTING"))
    try:
        model = (
            hr.current_model if testing else
            __import__(
                "eason_one.services.execution_policy", fromlist=["select_execution_model"]
            ).select_execution_model(hr, None, "HR_ASSESSMENT_ESTIMATE", require_real=True)
        )
    except ValueError:
        return None
    if not model.active or model.archived or int(model.max_output_tokens or 0) <= 0:
        return None
    context="Bounded HR assessment context including workforce, candidates, models, and budgets."
    estimate=estimate_execution(
        model,hr.system_instructions,context,
        "Assess one governed HiringRequest",
        int(model.max_output_tokens),
        HR_ASSESSMENT_SCHEMA)
    return Decimal(estimate.real_cost).quantize(
      Decimal("0.0001"),rounding=ROUND_CEILING)


def _assessment_context(request):
    operation=db.session.get(
      __import__("eason_one.models",fromlist=["Operation"]).Operation,
      request.operation_id) if request.operation_id else None
    lines=[
      f"HIRING REQUEST #{request.id}",
      f"Role needed: {request.role_needed}",
      f"Problem: {request.problem}",
      f"Why now: {request.why_now}",
      f"Responsibilities: {json.dumps(request.responsibilities_json,ensure_ascii=False)}",
      f"Capabilities: {json.dumps(request.capabilities_json,ensure_ascii=False)}",
      f"Urgency: {request.urgency}; use frequency: {request.use_frequency}",
      f"Requester: {request.requester.name if request.requester else 'Founder'}",
      f"Company remaining budget: TWD {remaining()}",
    ]
    if operation:
        from .operations import remaining_budget
        lines.append(f"Operation #{operation.id}: {operation.title}; objective: {operation.objective}; remaining TWD {remaining_budget(operation)}")
    lines.append("ACTIVE WORKFORCE")
    for employee in Employee.query.filter_by(active=True).order_by(Employee.id):
        lines.append(f"EMPLOYEE #{employee.id}: {employee.name}; {employee.position.name}; department {employee.department.name if employee.department else 'CEO Office'}; manager {employee.manager.name if employee.manager else 'Founder'}")
    lines.append("DEPARTMENTS")
    for department in Department.query.filter_by(active=True).order_by(Department.id):
        lines.append(f"DEPARTMENT #{department.id}: {department.name}")
    lines.append("TALENT POOL")
    for candidate in TalentTemplate.query.filter_by(active_in_pool=True).order_by(TalentTemplate.id).limit(20):
        lines.append(f"CANDIDATE #{candidate.id}: {candidate.role_title}; {candidate.department_hint or '-'}; skills {candidate.skills_json}")
    lines.append("AVAILABLE MODELS")
    policy = __import__(
        "eason_one.services.execution_policy",
        fromlist=["_configured", "_runtime_eligible", "_critical_review_staffing_request"],
    )
    allow_claude = policy._critical_review_staffing_request(request)
    for model in ModelConfig.query.filter_by(active=True,archived=False).order_by(ModelConfig.id):
        # Persistent Employee intelligence may use a real general provider.
        # Codex remains an Engineer tool and Mock remains simulation-only.
        if model.provider_key not in {"openai", "anthropic", "gemini", "perplexity"}:
            continue
        if not policy._runtime_eligible(model):
            continue
        if model.provider_key == "anthropic" and not allow_claude:
            continue
        lines.append(f"MODEL #{model.id}: {model.label}; input {model.input_price_per_million}; output {model.output_price_per_million}; max output {model.max_output_tokens}")
    return "\n".join(lines)


def _assert_project_hiring_mutable(request, *, action: str):
    if not request or not getattr(request, "project_id", None):
        return None
    Project = __import__("eason_one.models", fromlist=["Project"]).Project
    project = db.session.get(Project, request.project_id)
    if project is not None and __import__(
        "eason_one.services.work_runtime", fromlist=["project_is_terminal"]
    ).project_is_terminal(project):
        raise ValueError(
            f"PROJECT_TERMINAL_HIRING_MUTATION_FORBIDDEN:{action}:{str(project.status or '').upper()}"
        )
    return project


def _delegated_hiring_authority(request):
    """Return exact Company authority basis when Founder review is unnecessary."""
    if not request or request.requested_by_type != "EMPLOYEE" or not request.project_id:
        return None
    requester = request.requester
    if not requester or not requester.active or requester.position.level < 3:
        return None
    Project = __import__("eason_one.models", fromlist=["Project"]).Project
    project = db.session.get(Project, request.project_id)
    if not project or project.status not in {"PLANNING", "ACTIVE", "BLOCKED"}:
        return None
    contracts = __import__(
        "eason_one.services.project_contract",
        fromlist=["is_vnext_governed", "governing_terms", "effective_authority"],
    )
    if not contracts.is_vnext_governed(project):
        return None
    terms = contracts.governing_terms(project)
    authority = contracts.effective_authority(project)
    return {
        "version": "DELEGATED_HIRING_AUTHORITY_V1",
        "authority": "COMPANY_DELEGATED",
        "project_id": project.id,
        "requester_employee_id": requester.id,
        "governing_contract_hash": terms.get("governing_contract_hash") or terms.get("contract_hash"),
        "budget_authority_hash": authority.get("authority_hash"),
        # Employee existence is organizational capacity, never spend authority.
        "new_budget_authority_twd": "0",
    }


def _materialize_hire(request, *, authority_kind: str, authority_basis: dict | None = None):
    if request.status == "HIRED" and request.created_employee:
        return request.created_employee
    _assert_project_hiring_mutable(request, action="MATERIALIZE_HIRE")
    template = request.talent_template
    role = request.target_position or (template.role_title if template else request.role_needed)
    position = Position.query.filter_by(name=role).first()
    if not position:
        position = Position(name=role, level=1, description=request.problem)
        db.session.add(position)
        db.session.flush()
    department=db.session.get(Department,request.target_department_id)
    manager=db.session.get(Employee,request.manager_employee_id)
    if not department or not manager:
        raise ValueError("Approved organizational placement is incomplete")
    if manager.department_id!=department.id and manager.slug!="ceo":
        raise ValueError("Approved manager is outside target Department")
    instructions=(request.hr_assessment_json or {}).get("instructions") or (
      template.instructions if template else "Perform the approved role within Project-governed Work authority.")
    employee = Employee(
        name=template.name if template else role,
        slug=_slug(template.name if template else role),
        department_id=department.id,
        position_id=position.id,
        manager_id=manager.id,
        role_description=template.mission if template else request.problem,
        system_instructions=instructions,
        current_model_config_id=request.recommended_model_config_id,
        salary_credits_per_week=0,
        active=True,
        employment_status="PROBATION",
        probation_target_assignments=int(
            (request.hr_assessment_json or {}).get("probation_assignments", 3)
        ),
        hiring_request_id=request.id,
    )
    db.session.add(employee)
    db.session.flush()
    if employee.current_model_config_id:
        db.session.add(EmployeeModelHistory(
            employee_id=employee.id,
            model_config_id=employee.current_model_config_id,
            reason=(
                f"Company-delegated Project hire request #{request.id}"
                if authority_kind == "COMPANY_DELEGATED"
                else f"Founder-approved hire request #{request.id}"
            ),
        ))
    request.status = "HIRED"
    request.created_employee_id = employee.id

    if authority_kind == "COMPANY_DELEGATED":
        basis = dict(authority_basis or {})
        basis["hiring_request_id"] = request.id
        basis["created_employee_id"] = employee.id
        existing = Decision.query.filter_by(
            legacy_source="DELEGATED_HIRING", legacy_source_id=request.id
        ).first()
        if not existing:
            hr = Employee.query.filter_by(slug="hr-director").first()
            decision = Decision(
                legacy_source="DELEGATED_HIRING", legacy_source_id=request.id,
                project_id=request.project_id,
                work_id=None,
                proposed_by_employee_id=request.requester_employee_id,
                decided_by_employee_id=getattr(hr, "id", None),
                question=f"Materialize persistent Employee for capability gap: {request.role_needed}",
                decision="HIRE",
                rationale=(request.hr_assessment_json or {}).get("reasoning") or request.why_now,
                state="COMMITTED",
                authority_basis=json.dumps(basis, ensure_ascii=False, sort_keys=True),
                committed_at=now(),
            )
            db.session.add(decision)
            db.session.flush()
            __import__(
                "eason_one.services.company_events", fromlist=["emit"]
            ).emit(
                "DELEGATED_HIRE_COMMITTED", actor_type="EMPLOYEE",
                actor_id=getattr(hr, "id", None), project_id=request.project_id,
                decision_id=decision.id, correlation_id=f"project:{request.project_id}",
                payload={
                    "hiring_request_id": request.id,
                    "employee_id": employee.id,
                    "requester_employee_id": request.requester_employee_id,
                    "new_budget_authority_twd": "0",
                    "governing_contract_hash": basis.get("governing_contract_hash"),
                    "budget_authority_hash": basis.get("budget_authority_hash"),
                },
            )
        request.founder_decision = "NOT_REQUIRED"
        request.founder_decision_note = "Company delegated hire inside valid Founder Project Contract; new budget authority = 0."
        request.founder_decided_at = None
    else:
        request.founder_decision = "APPROVED"
        request.founder_decision_note = "Founder approved governed hire"
        request.founder_decided_at = now()

    if request.operation_id:
        models = __import__("eason_one.models", fromlist=["Operation", "AgentRun", "Work"])
        operation = db.session.get(models.Operation, request.operation_id)
        company_runtime = __import__(
            "eason_one.services.company_runtime", fromlist=["is_work_vnext", "wake_company_runtime"]
        )
        if operation and company_runtime.is_work_vnext(operation):
            source_run = db.session.get(models.AgentRun, request.source_execution_id) if request.source_execution_id else None
            source_work = db.session.get(models.Work, source_run.work_id) if source_run and source_run.work_id else None
            if source_work is None and request.project_id:
                # Team Formation may discover a capability gap before any paid
                # execution exists. In that case the Work stores exact request
                # identity in its durable runtime control instead of fabricating
                # an AgentRun merely to satisfy legacy HR linkage.
                for candidate in models.Work.query.filter_by(project_id=request.project_id).all():
                    staffing = dict((candidate.runtime_control_json or {}).get("team_formation") or {})
                    if staffing.get("hiring_request_id") == request.id:
                        source_work = candidate
                        break
            if source_work:
                waits = __import__("eason_one.services.work_runtime", fromlist=["resolve_waits"])
                waits.resolve_waits(
                    source_work, "FOUNDER_DECISION",
                    note=(
                        "Company materialized the delegated hire inside Project authority."
                        if authority_kind == "COMPANY_DELEGATED"
                        else "Founder approved the requested hire."
                    ),
                )
                waits.resolve_waits(
                    source_work, "CAPABILITY_GAP",
                    note="Hiring capability gap was resolved by persistent Employee materialization.",
                )
                waits.resolve_waits(
                    source_work, "INTERNAL_RECOVERY",
                    note="Hiring capability gap was resolved by persistent Employee materialization.",
                )
            company_runtime.wake_company_runtime()
        elif operation and operation.status=="WAITING_FOR_FOUNDER" and authority_kind != "COMPANY_DELEGATED":
            __import__("eason_one.services.operation_kernel",fromlist=["enqueue"]).enqueue(
                operation,actor_type="FOUNDER",reason="Founder approved the requested hire."
            )
            operation.waiting_reason=None
    db.session.commit()
    return employee


def reconcile_invalid_use_existing_staff(request, capability: str):
    """Correct an impossible HR USE EXISTING STAFF recommendation.

    The model may recommend existing staff, but roster capability truth is
    deterministic. If no active non-CEO Employee actually owns the required
    capability, delegated Project staffing falls back to HIRE without asking
    Founder or buying another HR assessment.
    """
    assessment = dict(request.hr_assessment_json or {})
    if assessment.get("recommendation") != "USE EXISTING STAFF":
        return None
    team = __import__(
        "eason_one.services.team_formation", fromlist=["best_existing_employee"]
    )
    existing = team.best_existing_employee(capability)
    if existing is not None:
        return {"status": "EXISTING", "employee": existing}
    basis = _delegated_hiring_authority(request)
    if not basis:
        return {"status": "NO_DELEGATED_AUTHORITY", "employee": None}
    assessment["original_recommendation"] = "USE EXISTING STAFF"
    assessment["recommendation"] = "HIRE"
    assessment["deterministic_recommendation_override"] = {
        "reason": "NO_EXISTING_EMPLOYEE_HAS_REQUIRED_CAPABILITY",
        "required_capability": capability,
        "authority": "COMPANY_DELEGATED",
    }
    request.hr_assessment_json = assessment
    request.status = "FOUNDER_REVIEW"
    db.session.flush()
    employee = commit_delegated_hire(request)
    return {"status": "HIRED", "employee": employee}


def commit_delegated_hire(request):
    if not request.hr_assessment_json or request.hr_assessment_json.get("recommendation") != "HIRE":
        raise ValueError("HR HIRE assessment is required before delegated materialization")
    basis = _delegated_hiring_authority(request)
    if not basis:
        raise ValueError("Hiring request is outside delegated Project authority")
    return _materialize_hire(request, authority_kind="COMPANY_DELEGATED", authority_basis=basis)


def assessment_attempts(request):
    """Return HR model attempts that belong to exactly one HiringRequest."""
    rows = (
        AgentRun.query
        .filter_by(purpose="HR_ASSESSMENT")
        .order_by(AgentRun.id)
        .all()
    )
    request_id = int(request.id)
    return [
        row for row in rows
        if int(((row.context_composition_json or {}).get("hiring_request_id") or 0)) == request_id
    ]


def _assessment_run_matches_current_terms(request, run):
    """Whether one paid HR result still belongs to the current Project authority.

    HiringRequest rows are immutable demand records, but the governing Project
    may later receive Founder scope/constraint/deadline amendments.  A restart
    may resume an already-paid HR response only when those execution terms are
    still the same.  Budget-only amendments intentionally do not stale work.
    """
    if not request.project_id:
        return True
    project = db.session.get(
        __import__("eason_one.models", fromlist=["Project"]).Project,
        request.project_id,
    )
    if project is None:
        return False
    contracts = __import__(
        "eason_one.services.project_contract",
        fromlist=["is_vnext_governed", "execution_terms_hash", "execution_terms_changed_after"],
    )
    if not contracts.is_vnext_governed(project):
        return True
    context = dict(run.context_composition_json or {})
    snapshot = str(context.get("project_execution_terms_hash") or "").strip()
    if snapshot:
        return snapshot == contracts.execution_terms_hash(project)
    return not contracts.execution_terms_changed_after(project, run.started_at)


def reusable_successful_assessment(request):
    """Newest exact paid HR success that can be projected without another call.

    A provider response may be fully persisted while the process dies before the
    HiringRequest fields are updated.  Later accidental failed duplicates must
    not hide that durable result and cause a third charge.
    """
    for run in reversed(assessment_attempts(request)):
        if run.status != "SUCCEEDED":
            continue
        if not _assessment_run_matches_current_terms(request, run):
            continue
        if not str(run.raw_output or "").strip():
            continue
        return run
    return None


def assessment_retry_state(request, *, current=None):
    """Derive durable retry/reconciliation state from persisted AgentRuns.

    No timer lives only in process memory: restart reconstructs the same answer
    from the exact HiringRequest-tagged attempts. Ambiguous post-dispatch calls
    are never retried automatically because their spend/effect truth must be
    reconciled first.  An older exact paid success wins over a newer accidental
    failure because it can be projected without another provider call.
    """
    current = current or now()
    attempts = assessment_attempts(request)
    failures = [row for row in attempts if row.status != "SUCCEEDED"]
    last_attempt = attempts[-1] if attempts else None

    # Unknown/newer external-effect truth outranks an older usable output.  The
    # Company may already have enough HR content, but it still cannot move past
    # an unproven charge/effect or create another provider call.
    if last_attempt is not None and last_attempt.status == "RUNNING":
        return {"state": "IN_PROGRESS", "attempts": len(failures), "last_run": last_attempt, "retry_after": None}
    if last_attempt is not None and str(getattr(last_attempt, "outcome", "") or "") == "FAILED_AMBIGUOUS":
        return {"state": "RECONCILIATION", "attempts": len(failures), "last_run": last_attempt, "retry_after": None}

    reusable = reusable_successful_assessment(request)
    if reusable is not None:
        return {
            "state": "READY", "attempts": len(failures),
            "last_run": reusable, "retry_after": None, "reusable_run": reusable,
        }
    if not failures:
        return {"state": "READY", "attempts": 0, "last_run": None, "retry_after": None}
    last = failures[-1]
    execution_policy = __import__(
        "eason_one.services.execution_policy",
        fromlist=["_provider_family_retry_fault"],
    )
    if (
        getattr(last, "failure_reason", None) == "PROVIDER_REQUEST_REJECTED"
        or execution_policy._provider_family_retry_fault(last)
    ):
        # A historical SYSTEM_RECOVERY is not permanent if Company configuration
        # later exposes a new lawful, untried provider/model.  Re-evaluate current
        # policy without dispatching anything; normal assess_request() will use the
        # exact failed Run as retry lineage if a candidate now exists.
        if len(failures) < HR_ASSESSMENT_MAX_AUTOMATIC_ATTEMPTS:
            try:
                operation = None
                if getattr(request, "operation_id", None):
                    Operation = __import__("eason_one.models", fromlist=["Operation"]).Operation
                    operation = db.session.get(Operation, request.operation_id)
                hr = Employee.query.filter_by(slug="hr-director").one()
                prompt = (
                    hr.system_instructions
                    + "\nHR_ASSESSMENT\nReturn one strict workforce recommendation. Application code computes all currency arithmetic."
                )
                alternate = _select_hr_recovery_model(
                    request, hr, last, operation, prompt=prompt, context=_assessment_context(request)
                )
            except Exception:
                alternate = None
            if alternate is not None:
                return {
                    "state": "READY", "attempts": len(failures), "last_run": last,
                    "retry_after": None, "recovery_model_id": alternate.id,
                    "recovery_reason": "NEW_LAWFUL_PROVIDER_PATH",
                }
        return {"state": "SYSTEM_RECOVERY", "attempts": len(failures), "last_run": last, "retry_after": None}
    if len(failures) >= HR_ASSESSMENT_MAX_AUTOMATIC_ATTEMPTS:
        return {"state": "EXHAUSTED", "attempts": len(failures), "last_run": last, "retry_after": None}
    finished = getattr(last, "finished_at", None) or getattr(last, "started_at", None) or current
    delay = HR_ASSESSMENT_RETRY_DELAYS_SECONDS[min(len(failures) - 1, len(HR_ASSESSMENT_RETRY_DELAYS_SECONDS) - 1)]
    rejection = dict((last.context_composition_json or {}).get("provider_rejection") or {})
    try:
        provider_delay = int(rejection.get("retry_after_seconds")) if rejection.get("retry_after_seconds") is not None else 0
    except (TypeError, ValueError):
        provider_delay = 0
    delay = max(delay, min(120, max(0, provider_delay)))
    retry_after = finished + timedelta(seconds=delay)
    due = retry_after <= current
    return {
        "state": "READY" if due else "BACKOFF",
        "attempts": len(failures),
        "last_run": last,
        "retry_after": retry_after,
    }


def source_work_for_request(request):
    if not request.source_execution_id:
        return None
    models = __import__("eason_one.models", fromlist=["Work"])
    source_run = db.session.get(AgentRun, request.source_execution_id)
    return db.session.get(models.Work, source_run.work_id) if source_run and source_run.work_id else None


def _select_hr_recovery_model(request, hr, failed_run, operation, *, prompt, context):
    """Choose one lawful alternate HR model without expanding authority.

    HR is a generic Company function rather than a provider-family identity. If
    one real provider is definitively unavailable/rejected, the Company may use
    another already-configured model that satisfies the same Project/provider
    constraints.  Standalone Founder HR requests have an explicit assessment
    authorization, so an alternate is also rejected when its worst-case estimate
    would exceed that already-approved amount.

    Previously attempted models are excluded to prevent A -> B -> A bounce loops
    across restarts or bounded recovery.
    """
    policy = __import__(
        "eason_one.services.execution_policy",
        fromlist=["select_retry_model"],
    )
    attempted_model_ids = {
        int(row.model_config_id)
        for row in assessment_attempts(request)
        if getattr(row, "model_config_id", None) is not None
    }
    excluded = set(attempted_model_ids)
    while True:
        model = policy.select_retry_model(
            hr, failed_run, operation, "HR_ASSESSMENT",
            exclude_model_ids=excluded,
        )
        if model is None:
            return None
        # SYSTEM_RECOVERY is about restoring a real Company execution path.
        # TESTING permits MockProvider for isolated first-attempt fixtures, but
        # mock must never count as a newly lawful provider recovery candidate.
        if str(getattr(model, "provider_key", "") or "").casefold() == "mock":
            excluded.add(int(model.id))
            continue
        if operation is None:
            authorized = (
                Decimal(str(request.assessment_budget_twd))
                if request.assessment_budget_twd is not None else None
            )
            estimate = estimate_execution(
                model,
                prompt,
                context,
                f"Assess HiringRequest #{request.id}",
                int(model.max_output_tokens),
                HR_ASSESSMENT_SCHEMA,
            )
            if authorized is None or Decimal(estimate.real_cost) > authorized:
                excluded.add(int(model.id))
                continue
        return model


def assess_request(request):
    _assert_project_hiring_mutable(request, action="ASSESS_REQUEST")
    if request.status not in {"REQUESTED","HR_REVIEW","SYSTEM_RECOVERY"}:
        if request.status=="FOUNDER_REVIEW" and request.hr_agent_run_id:
            return db.session.get(AgentRun,request.hr_agent_run_id)
        raise ValueError("Hiring request is not ready for HR assessment")
    retry_state = assessment_retry_state(request, current=now())
    if request.status == "SYSTEM_RECOVERY" and not retry_state.get("recovery_model_id"):
        raise ValueError("HR system recovery has no newly lawful automatic path")
    if retry_state["state"] == "IN_PROGRESS":
        raise ValueError("HR assessment is already in progress; duplicate dispatch is forbidden")
    if retry_state["state"] == "RECONCILIATION":
        raise ValueError("HR assessment has unresolved post-dispatch truth; reconcile before replay")
    if retry_state["state"] == "SYSTEM_RECOVERY":
        raise ValueError("HR provider/configuration requires system recovery before replay")
    if retry_state["state"] == "BACKOFF":
        raise ValueError("HR assessment retry backoff has not elapsed")
    if retry_state["state"] == "EXHAUSTED":
        raise ValueError("HR assessment automatic retry budget is exhausted")
    operation=db.session.get(
      __import__("eason_one.models",fromlist=["Operation"]).Operation,
      request.operation_id) if request.operation_id else None
    project=db.session.get(__import__("eason_one.models",fromlist=["Project"]).Project,request.project_id) if request.project_id else None

    # Resume the exact already-paid provider response before consulting the
    # current HR model binding or buying any new assessment. This is the crash
    # boundary between provider persistence and HiringRequest projection.
    run = reusable_successful_assessment(request)
    if run is None:
        hr=Employee.query.filter_by(slug="hr-director").one()
        if not hr.current_model:
            raise ValueError("HR Director has no ModelConfig; Founder assignment is required")
        context=_assessment_context(request)
        if not operation:
            maximum=assessment_authorization(hr)
            if request.assessment_budget_twd is None or Decimal(request.assessment_budget_twd)<Decimal(maximum):
                raise ValueError("Founder HR assessment authorization is insufficient")
        request.status="HR_REVIEW"; db.session.commit()
        management_work = (
          __import__("eason_one.services.work_runtime",fromlist=["ensure_management_work"]).ensure_management_work(operation)
          if operation is not None and project is not None and __import__(
            "eason_one.services.project_contract",fromlist=["is_vnext_governed"]
          ).is_vnext_governed(project) else None
        )
        base_prompt=(
          hr.system_instructions
          + "\nHR_ASSESSMENT\nReturn one strict workforce recommendation. Application code computes all currency arithmetic."
        )
        base_composition={"hiring_request_id": request.id, "authority": "GOVERNED_HR_ASSESSMENT"}

        def _execute_assessment(*, prompt, prompt_version, retry_of_run=None, recovery=None, model_override=None):
            composition=dict(base_composition)
            if recovery:
                composition["company_recovery"] = dict(recovery)
            return execute(
              hr,"HR_ASSESSMENT",f"Assess HiringRequest #{request.id}",
              project=project,operation=operation,work=management_work,context_override=context,
              context_composition=composition,
              system_prompt_override=prompt,
              response_schema=HR_ASSESSMENT_SCHEMA,
              prompt_version=prompt_version,
              retry_of_run=retry_of_run,
              model_override=model_override,
            )

        recovery_model_id = retry_state.get("recovery_model_id")
        recovery_source = retry_state.get("last_run")
        if recovery_model_id and recovery_source is not None:
            recovery_model = db.session.get(ModelConfig, recovery_model_id)
            if recovery_model is None:
                raise ValueError("HR automatic recovery ModelConfig disappeared before dispatch")
            run = _execute_assessment(
                prompt=base_prompt,
                prompt_version="hr-assessment-v2-system-recovery-resume",
                retry_of_run=recovery_source,
                model_override=recovery_model,
                recovery={
                    "reason": retry_state.get("recovery_reason") or recovery_source.failure_reason,
                    "prior_run_id": recovery_source.id,
                    "policy": "RESUME_ONLY_WITH_NEW_LAWFUL_UNTRIED_MODEL",
                    "selected_model_config_id": recovery_model.id,
                    "selected_provider": recovery_model.provider_key,
                    "max_attempts": HR_ASSESSMENT_MAX_AUTOMATIC_ATTEMPTS,
                },
            )
        else:
            run=_execute_assessment(prompt=base_prompt,prompt_version="hr-assessment-v2")
        if (run.status!="SUCCEEDED" and run.failure_reason=="OUTPUT_TRUNCATED"
            and len(assessment_attempts(request)) < HR_ASSESSMENT_MAX_AUTOMATIC_ATTEMPTS):
            prior=run
            run=_execute_assessment(
              prompt=base_prompt+(
                "\nHR_ASSESSMENT_TRUNCATION_RECOVERY\n"
                "The previous assessment hit the configured output ceiling. Return the same complete workforce recommendation compactly. "
                "Use short strings and the minimum sufficient alternatives/success criteria while preserving every required schema field, "
                "organizational placement, recommended model, cost-estimation inputs, recommendation, and probation terms."
              ),
              prompt_version="hr-assessment-v2-truncation-recovery",
              retry_of_run=prior,
              recovery={
                "reason":"OUTPUT_TRUNCATED",
                "prior_run_id":prior.id,
                "policy":"ONE_SAME_PROVIDER_COMPACT_RETRY",
                "max_attempts":2,
              },
            )
        # HR is not a provider-family identity. A known provider/configuration
        # failure may therefore fail over to one different model/provider as
        # long as the same Founder/Project constraints and assessment budget are
        # still satisfied. Ambiguous post-dispatch truth is intentionally absent
        # here: retry_of_run's external-effect guard would refuse it anyway.
        while (
            run.status != "SUCCEEDED"
            and run.failure_reason in {
                "PROVIDER_PREFLIGHT_FAILED",
                "PROVIDER_REQUEST_REJECTED",
                "PROVIDER_TRANSIENT_REJECTED",
            }
            and len(assessment_attempts(request)) < HR_ASSESSMENT_MAX_AUTOMATIC_ATTEMPTS
        ):
            alternate = _select_hr_recovery_model(
                request, hr, run, operation, prompt=base_prompt, context=context
            )
            if alternate is None:
                break
            prior = run
            run = _execute_assessment(
                prompt=base_prompt,
                prompt_version="hr-assessment-v2-provider-failover",
                retry_of_run=prior,
                model_override=alternate,
                recovery={
                    "reason": prior.failure_reason,
                    "prior_run_id": prior.id,
                    "policy": "DIFFERENT_GOVERNED_PROVIDER_WHEN_AVAILABLE",
                    "selected_model_config_id": alternate.id,
                    "selected_provider": alternate.provider_key,
                    "max_attempts": HR_ASSESSMENT_MAX_AUTOMATIC_ATTEMPTS,
                },
            )
            if (
                run.status != "SUCCEEDED"
                and run.failure_reason == "OUTPUT_TRUNCATED"
                and len(assessment_attempts(request)) < HR_ASSESSMENT_MAX_AUTOMATIC_ATTEMPTS
            ):
                prior = run
                run = _execute_assessment(
                    prompt=base_prompt+(
                        "\nHR_ASSESSMENT_TRUNCATION_RECOVERY\n"
                        "The previous assessment hit the configured output ceiling. Return the same complete workforce recommendation compactly. "
                        "Use short strings and the minimum sufficient alternatives/success criteria while preserving every required schema field, "
                        "organizational placement, recommended model, cost-estimation inputs, recommendation, and probation terms."
                    ),
                    prompt_version="hr-assessment-v2-provider-failover-truncation-recovery",
                    retry_of_run=prior,
                    model_override=alternate,
                    recovery={
                        "reason": "OUTPUT_TRUNCATED",
                        "prior_run_id": prior.id,
                        "policy": "ONE_SAME_PROVIDER_COMPACT_RETRY_AFTER_FAILOVER",
                        "selected_model_config_id": alternate.id,
                        "selected_provider": alternate.provider_key,
                        "max_attempts": HR_ASSESSMENT_MAX_AUTOMATIC_ATTEMPTS,
                    },
                )
    else:
        request.status = "HR_REVIEW"
        request.hr_agent_run_id = run.id
        db.session.commit()
    request.hr_agent_run_id=run.id
    if run.status!="SUCCEEDED":
        # Persist the exact failed attempt before returning control to the
        # scheduler. The Company Kernel reconstructs bounded retry/backoff from
        # these durable request-tagged AgentRuns after any restart.
        request.status="HR_REVIEW"
        db.session.commit()
        raise ValueError(run.error_text or "HR assessment failed")
    payload=json.loads(run.raw_output)
    department=db.session.get(Department,payload["target_department_id"])
    manager=db.session.get(Employee,payload["manager_employee_id"])
    proposed_model=db.session.get(ModelConfig,payload["recommended_model_config_id"])
    if not department or not department.active: raise ValueError("HR proposed unknown Department")
    if not manager or not manager.active or manager.position.level<3: raise ValueError("HR proposed invalid manager")
    if manager.department_id!=department.id and manager.slug!="ceo": raise ValueError("HR manager does not belong to target Department")
    policy = __import__(
        "eason_one.services.execution_policy",
        fromlist=["select_staffing_model"],
    )
    # HR output is a recommendation, not authority to bind an unavailable model.
    # A paid assessment may survive a restart while the proposed ModelConfig is
    # later inactive, unconfigured, mock-only, or otherwise outside current
    # Founder policy.  Repair that local projection deterministically from the
    # already-approved staffing constraints instead of buying another HR call.
    model = policy.select_staffing_model(request, proposed_model)
    if model is None:
        raise ValueError("No configured real ModelConfig satisfies the Founder staffing cost policy")
    proposed_model_id = getattr(proposed_model, "id", None)
    if proposed_model_id != model.id:
        payload["recommended_model_config_id"] = model.id
    mission=estimate_mission_cost(model,payload["estimated_input_tokens"],
      payload["estimated_output_tokens"],payload["expected_calls_per_mission"])
    monthly=mission*int(payload["missions_per_month"])
    assessment=dict(payload)
    if proposed_model_id != model.id:
        assessment["model_policy_override"] = {
            "proposed_model_config_id": proposed_model_id,
            "selected_model_config_id": model.id,
            "reason": "DETERMINISTIC_CURRENT_STAFFING_POLICY_REPLACEMENT",
        }
    assessment["financial_math"]={
      "estimated_input_cost_twd":str(calculate(model,payload["estimated_input_tokens"]*payload["expected_calls_per_mission"],0)),
      "estimated_output_cost_twd":str(calculate(model,0,payload["estimated_output_tokens"]*payload["expected_calls_per_mission"])),
      "estimated_mission_cost_twd":str(mission),
      "estimated_monthly_cost_twd":str(monthly),
      "maximum_proposed_exposure_twd":str(Decimal(str(payload["max_mission_budget_twd"]))),
      "company_budget_remaining_twd":str(remaining())}
    assessment["resource_envelope"]={
      "max_mission_budget_twd":str(Decimal(str(payload["max_mission_budget_twd"]))),
      "expected_calls_per_mission":payload["expected_calls_per_mission"],
      "missions_per_month":payload["missions_per_month"]}
    request.hr_assessment_json=assessment
    request.hr_agent_run_id=run.id
    request.target_department_id=department.id
    request.target_position=payload["target_position"]
    request.manager_employee_id=manager.id
    request.recommended_model_config_id=model.id
    request.resource_envelope_json=assessment["resource_envelope"]
    request.status=("FOUNDER_REVIEW" if payload["recommendation"]=="HIRE"
      else "ASSESSMENT_COMPLETE")
    run.parsed_output_json=payload
    db.session.commit()
    if payload["recommendation"] == "HIRE" and _delegated_hiring_authority(request):
        commit_delegated_hire(request)
    return run


def review_request(request, *, existing_staff_alternative, recommendation,
                   recommended_model, estimated_input_tokens,
                   estimated_output_tokens, expected_calls,
                   max_mission_budget_twd, expected_benefit,
                   redundancy_risk, alternatives, success_criteria,
                   probation_assignments):
    _assert_project_hiring_mutable(request, action="REVIEW_REQUEST")
    if request.status not in {"REQUESTED", "HR_REVIEW"}:
        raise ValueError("Hiring request is not open for HR review")
    if recommendation not in {"HIRE", "DO NOT HIRE", "TEMPORARY", "USE EXISTING STAFF"}:
        raise ValueError("Invalid HR recommendation")
    if recommended_model and (not recommended_model.active or recommended_model.archived):
        raise ValueError("HR may recommend only an active ModelConfig")
    cost = (
        estimate_mission_cost(
            recommended_model, estimated_input_tokens, estimated_output_tokens,
            expected_calls,
        )
        if recommended_model else Decimal("0")
    )
    assessment = {
        "existing_staff_alternative": existing_staff_alternative,
        "need_duration": request.use_frequency,
        "overlap_review": "Compared with active workforce and Talent Pool.",
        "recommendation": recommendation,
        "estimated_input_tokens": int(estimated_input_tokens),
        "estimated_output_tokens": int(estimated_output_tokens),
        "expected_calls": int(expected_calls),
        "estimated_cost_per_mission_twd": str(cost),
        "max_mission_budget_twd": str(Decimal(max_mission_budget_twd)),
        "company_budget_remaining_twd": str(remaining()),
        "expected_benefit": expected_benefit,
        "redundancy_risk": redundancy_risk,
        "alternatives": _list(alternatives, "alternatives"),
        "success_criteria": _list(success_criteria, "success criteria"),
        "probation_assignments": int(probation_assignments),
    }
    request.hr_assessment_json = assessment
    request.recommended_model_config_id = getattr(recommended_model, "id", None)
    request.status = ("FOUNDER_REVIEW" if recommendation=="HIRE"
      else "ASSESSMENT_COMPLETE")
    department=_department(request.talent_template.department_hint if request.talent_template else "Human Resources")
    request.target_department_id=department.id
    request.target_position=request.talent_template.role_title if request.talent_template else request.role_needed
    manager=(Employee.query.filter_by(department_id=department.id)
      .join(Position).filter(Position.level>=3).order_by(Position.level.desc()).first()
      or Employee.query.filter_by(slug="hr-director").one())
    request.manager_employee_id=manager.id
    request.resource_envelope_json={"max_mission_budget_twd":str(Decimal(max_mission_budget_twd))}
    db.session.commit()
    # Management-originated HIRE inside a valid governed Project is Company
    # authority, not a Founder approval queue item. The Employee gains no new
    # spend authority; every later execution remains constrained by Project/Work
    # budget. This also keeps the manual HR-review path aligned with the
    # provider-backed assess_request() path.
    if recommendation == "HIRE" and _delegated_hiring_authority(request):
        commit_delegated_hire(request)
    return request


def _slug(text):
    base = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "employee"
    slug = base
    index = 2
    while Employee.query.filter_by(slug=slug).first():
        slug = f"{base}-{index}"
        index += 1
    return slug


def _department(hint):
    if hint:
        exact = Department.query.filter(
            db.func.lower(Department.name) == hint.lower()
        ).first()
        if exact:
            return exact
        partial = Department.query.filter(
            db.func.lower(Department.name).like(f"%{hint.lower()}%")
        ).first()
        if partial:
            return partial
    return Department.query.filter_by(name="Human Resources").one()


def approve_hire(request):
    if request.status == "HIRED" and request.created_employee:
        return request.created_employee
    _assert_project_hiring_mutable(request, action="APPROVE_HIRE")
    if (request.status != "FOUNDER_REVIEW" or not request.hr_assessment_json
      or request.hr_assessment_json.get("recommendation")!="HIRE"):
        raise ValueError("HR review is required before Founder approval")
    # Reconcile rows created by older builds: if current Project authority now
    # proves the hire is delegated, a Founder click must not manufacture a fake
    # Founder hiring decision.
    basis = _delegated_hiring_authority(request)
    if basis:
        return _materialize_hire(
            request, authority_kind="COMPANY_DELEGATED", authority_basis=basis
        )
    return _materialize_hire(request, authority_kind="FOUNDER")


def reject_request(request, note):
    if request.status == "HIRED":
        raise ValueError("A completed hire cannot be rejected")
    request.status = "REJECTED"
    request.founder_decision = "REJECTED"
    request.founder_decision_note = note
    request.founder_decided_at = now()
    db.session.commit()
    return request
