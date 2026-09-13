import json
import re
import hashlib
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation, ROUND_CEILING

from sqlalchemy import func, or_

from ..extensions import db
from ..models import (
    AgentRun, CostEvent, Employee, HiringRequest, KnowledgeItem, Meeting, ModelConfig, Operation,
    OperationStep, Project, Task, WorkMessage, now,
)
from ..schemas import (
    CEO_DECISION_SCHEMA, GOAL_VERIFICATION_SCHEMA, SYNTHESIS_SCHEMA,
)
from .company import remaining as company_remaining
from .execution import execute
from . import meetings as meeting_service
from .reviews import DECISIONS, run_review
from .task_execution import run_task
from .tasks import transition
from .text_normalization import clean_rows, clean_text

PLAN_FIELDS = {"mode", "executive_response", "operation"}
PROJECT_PLAN_FIELDS = PLAN_FIELDS | {"project", "project_id"}
OPERATION_FIELDS = {
    "title", "objective", "project_id", "budget_twd", "tasks",
    "meeting_policy", "meeting_config", "completion_criteria",
}
LEGACY_OPERATION_FIELDS = OPERATION_FIELDS - {"meeting_config"}
MEETING_TRIGGERS = {"NEVER", "ON_MATERIAL_CONFLICT", "BEFORE_FINAL_REPORT"}


def _sanitize_operation_plan(payload):
    """Remove provider/control garbage before it can become durable Work truth."""
    value = json.loads(json.dumps(payload or {}, ensure_ascii=False))
    if isinstance(value, dict):
        if isinstance(value.get("executive_response"), str):
            value["executive_response"] = clean_text(value["executive_response"], multiline=False)
        operation = value.get("operation")
        if isinstance(operation, dict):
            for field in ("title", "objective", "meeting_policy"):
                if isinstance(operation.get(field), str):
                    operation[field] = clean_text(operation[field], multiline=False)
            operation["completion_criteria"] = clean_rows(operation.get("completion_criteria"))
            tasks = operation.get("tasks")
            if isinstance(tasks, list):
                for item in tasks:
                    if not isinstance(item, dict):
                        continue
                    for field in ("title", "objective"):
                        if isinstance(item.get(field), str):
                            item[field] = clean_text(item[field], multiline=False)
                    item["acceptance_criteria"] = clean_rows(item.get("acceptance_criteria"))
                    item["required_capabilities"] = [
                        clean_text(value, multiline=False).upper().replace(" ", "_")
                        for value in (item.get("required_capabilities") or [])
                        if isinstance(value, str) and clean_text(value, multiline=False)
                    ][:1]
                    item.setdefault("write_scope", None)
    return value

_BUDGET_AUTHORITY_LANGUAGE = re.compile(
    r"(?:NT\$|TWD|新台幣|預算(?:上限)?|花費上限|成本上限|budget(?:\s+cap)?|spending\s+cap|cost\s+cap|hard\s+cap)",
    flags=re.IGNORECASE,
)

def budget_authority_language(value):
    return bool(_BUDGET_AUTHORITY_LANGUAGE.search(str(value or "")))

def normalize_project_constraints(constraints, *, founder_cap=None):
    """Keep only Founder-owned budget authority in the Project contract.

    CEO/model prose may suggest a cost, but it may not manufacture a Project
    hard cap.  Budget-looking constraints are therefore stripped unless the
    Founder actually declared a cap, in which case one canonical constraint is
    persisted from deterministic authority truth.
    """
    cleaned = [
        value for value in clean_rows(constraints)
        if not budget_authority_language(value)
    ]
    if founder_cap is not None:
        cap = Decimal(str(founder_cap)).quantize(BUDGET_QUANTUM, rounding=ROUND_CEILING)
        cleaned.append(f"Founder budget cap: NT$ {_budget_text(cap)}")
    # Founder authority must never disappear merely because a UI/schema once
    # preferred eight display rows. Presentation can summarize later; the
    # immutable Project Contract retains every approved constraint.
    return cleaned

def _priced_operation_envelope(
    plan, *, founder_cap=None, existing_project=None, include_orchestration=False,
    execution_constraints_override=None,
):
    """Return the deterministic pre-approval authority envelope for one move."""
    estimate, breakdown, meeting_estimate = execution_budget_estimate(
        plan, include_orchestration=include_orchestration,
        execution_constraints_override=dict(execution_constraints_override or {}),
    )
    contingency = max(
        BUDGET_QUANTUM,
        (estimate * Decimal("0.15")).quantize(BUDGET_QUANTUM, rounding=ROUND_CEILING),
    )
    if founder_cap is not None:
        founder_cap = Decimal(str(founder_cap)).quantize(BUDGET_QUANTUM, rounding=ROUND_CEILING)
        if founder_cap < estimate:
            raise ValueError(
                f"The priced workflow estimate (NT${estimate}) exceeds the Founder-declared hard cap (NT${founder_cap}). "
                "The CEO must reduce scope or request a larger boundary before approval."
            )
        normalized = min(
            founder_cap,
            max(BUDGET_QUANTUM, (estimate * Decimal("1.15")).quantize(BUDGET_QUANTUM, rounding=ROUND_CEILING)),
        )
    else:
        normalized = max(BUDGET_QUANTUM, estimate + contingency)
    remaining = project_remaining_authority(existing_project) if existing_project else None
    if remaining is not None:
        if estimate > remaining:
            raise ValueError(
                f"The next bounded move estimate (NT${estimate}) exceeds remaining Project authority (NT${remaining})."
            )
        normalized = min(normalized, remaining)
    return normalized, estimate, breakdown, meeting_estimate, contingency

def proposal_authority_snapshot(operation):
    """Compile the single Founder-visible authority truth for an unapproved proposal."""
    plan = validate_plan(operation.plan_json)
    data = plan["operation"]
    memory = dict(operation.memory_json or {})
    founder_cap_raw = memory.get("founder_declared_budget_cap_twd")
    founder_cap = Decimal(str(founder_cap_raw)) if founder_cap_raw not in (None, "") else None
    existing_project = db.session.get(Project, data.get("project_id")) if data.get("project_id") else None
    include_orchestration = bool(memory.get("multi_agent_enabled"))
    durable_execution_constraints = dict(memory.get("execution_constraints") or {})
    envelope, estimate, breakdown, meeting_estimate, contingency = _priced_operation_envelope(
        plan, founder_cap=founder_cap, existing_project=existing_project,
        include_orchestration=include_orchestration,
        execution_constraints_override=durable_execution_constraints,
    )
    project_spec = dict(memory.get("new_project_spec") or {})
    project_constraints = normalize_project_constraints(
        project_spec.get("constraints") or [], founder_cap=founder_cap
    )
    if founder_cap is not None:
        project_envelope = founder_cap
    elif existing_project is not None:
        contracts = __import__(
            "eason_one.services.project_contract",
            fromlist=["is_vnext_governed", "assert_authority_ledger"],
        )
        if contracts.is_vnext_governed(existing_project):
            authority = contracts.assert_authority_ledger(existing_project)
            raw = authority.get("effective_budget_limit_twd")
            if raw in (None, ""):
                raise ValueError("PROJECT_BUDGET_AUTHORITY_MISSING")
            project_envelope = Decimal(str(raw))
        elif existing_project.real_budget_limit is not None:
            project_envelope = Decimal(existing_project.real_budget_limit)
        else:
            project_envelope = Decimal("0")
    else:
        # A Founder who did not type a numeric cap still approves one explicit
        # deterministic Project envelope. Reserve one additional bounded move
        # plus one CEO continuation-planning call so Project autonomy is real,
        # not exhausted immediately after Mission #1. Anything beyond this
        # bounded reserve requires new Founder budget authority.
        ceo = Employee.query.filter_by(slug="ceo").first()
        continuation_plan = _estimate_call(ceo, 7000, 2600)
        continuation_reserve = (
            envelope + continuation_plan * Decimal("1.20")
        ).quantize(BUDGET_QUANTUM, rounding=ROUND_CEILING)
        project_envelope = (envelope + continuation_reserve).quantize(
            BUDGET_QUANTUM, rounding=ROUND_CEILING
        )
    config = dict(data.get("meeting_config") or {})
    meeting_trigger = config.get("trigger") or "NEVER"
    meeting_budget = Decimal("0") if meeting_trigger == "NEVER" else max(
        Decimal(str(config.get("budget_twd") or 0)),
        (meeting_estimate * Decimal("1.20")).quantize(BUDGET_QUANTUM, rounding=ROUND_CEILING),
    )
    issues=[]
    if founder_cap is None and any(budget_authority_language(v) for v in (project_spec.get("constraints") or [])):
        issues.append("MODEL_INVENTED_PROJECT_BUDGET_CONSTRAINT")
    if founder_cap is not None:
        stored = memory.get("project_authorized_budget_twd")
        if stored not in (None, "") and Decimal(str(stored)) != founder_cap:
            issues.append("PROJECT_BUDGET_DOES_NOT_MATCH_FOUNDER_CAP")
    return {
        "founder_declared_cap_twd": founder_cap,
        "project_envelope_twd": project_envelope,
        "project_envelope_source": (
            "FOUNDER_DECLARED_HARD_CAP" if founder_cap is not None
            else "SYSTEM_PRICED_BOUNDED_PROJECT_ENVELOPE"
        ),
        "first_move_envelope_twd": envelope,
        "execution_estimate_twd": estimate,
        "contingency_twd": contingency,
        "meeting_budget_twd": meeting_budget,
        "meeting_trigger": meeting_trigger,
        "project_constraints": project_constraints,
        "breakdown": breakdown,
        "issues": issues,
    }

def reconcile_pending_proposal_authority(operation):
    """Reprice and normalize a still-unapproved proposal before Founder approval.

    This is deterministic reconciliation only.  It does not expand a Founder
    cap and never makes a provider/tool call.
    """
    if operation.approved_at is not None or operation.status != "PLANNED":
        return proposal_authority_snapshot(operation)
    snapshot = proposal_authority_snapshot(operation)
    memory = dict(operation.memory_json or {})
    plan = json.loads(json.dumps(operation.plan_json or {}))
    data = dict(plan.get("operation") or {})
    envelope = max(
        Decimal(snapshot["first_move_envelope_twd"]),
        Decimal(snapshot["meeting_budget_twd"]),
    )
    data["budget_twd"] = str(envelope)
    config = dict(data.get("meeting_config") or {})
    config["budget_twd"] = str(snapshot["meeting_budget_twd"])
    data["meeting_config"] = config
    plan["operation"] = data
    operation.plan_json = plan
    operation.approved_budget_twd = envelope
    operation.estimated_cost_twd = Decimal(snapshot["execution_estimate_twd"])
    operation.hard_cost_cap_twd = envelope
    operation.stage_cost_cap_twd = envelope
    operation.single_call_cost_cap_twd = envelope
    memory["execution_budget_estimate_twd"] = str(snapshot["execution_estimate_twd"])
    memory["execution_budget_breakdown"] = snapshot["breakdown"]
    memory["budget_authority_source"] = snapshot["project_envelope_source"]
    spec = dict(memory.get("new_project_spec") or {})
    if spec:
        # New Project approval freezes the Founder-visible total Project
        # envelope separately from this first Mission's internal allocation.
        memory["project_authorized_budget_twd"] = str(snapshot["project_envelope_twd"])
    elif snapshot["founder_declared_cap_twd"] is not None:
        memory["project_authorized_budget_twd"] = str(snapshot["founder_declared_cap_twd"])
    else:
        memory.pop("project_authorized_budget_twd", None)
    if spec:
        spec["constraints"] = snapshot["project_constraints"]
        memory["new_project_spec"] = spec
    completion = sum(
        (Decimal(str(row.get("estimated_twd") or 0)) for row in snapshot["breakdown"]
         if row.get("kind") in {"PROJECT_OUTCOME_REVIEW", "DETERMINISTIC_ENGINEERING_VERIFICATION"}),
        Decimal("0"),
    )
    report = sum(
        (Decimal(str(row.get("estimated_twd") or 0)) for row in snapshot["breakdown"]
         if row.get("kind") == "CEO_OPERATION_REPORT"),
        Decimal("0"),
    )
    memory["completion_reserve_twd"] = str(completion)
    memory["report_reserve_twd"] = str(report)
    stage_budget_caps = {}
    for row in snapshot["breakdown"]:
        kind = row.get("kind")
        if kind == "MEETING_ENVELOPE":
            kind = "MEETING"
        if kind == "ENGINEER_CODEX_TOOL":
            kind = "TASK_EXECUTION"
        stage_budget_caps[kind] = str(
            (Decimal(str(stage_budget_caps.get(kind) or 0))
             + Decimal(str(row.get("estimated_twd") or 0)) * Decimal("1.20"))
            .quantize(BUDGET_QUANTUM, rounding=ROUND_CEILING)
        )
    stage_budget_caps["CONTINGENCY"] = str(snapshot["contingency_twd"])
    memory["stage_budget_caps"] = stage_budget_caps
    operation.memory_json = memory
    db.session.commit()
    return proposal_authority_snapshot(operation)

def _minimum_meeting_token_limit(config):
    """Return a bounded cumulative token envelope that can finish the plan.

    Meeting.token_limit counts all provider input and output tokens across the
    room.  A token ceiling below the number of approved speakers/rounds is a
    false economy: the runner would predictably stop before the team can finish.
    The Founder still sees and approves the normalized ceiling before execution.
    """
    rounds = int(config.get("max_rounds", 1))
    speakers = int(config.get("max_speakers_per_round", 1))
    # The runtime guard uses a deliberately conservative UTF-8 byte bound, not
    # the model's eventual billed token count.  In practice one compact
    # contribution can reserve roughly 4k tokens.  A 6k envelope therefore
    # cannot complete even a one-round, two-specialist Meeting.  Normalize the
    # Founder-visible ceiling before approval so the room can actually finish.
    if rounds == 1 and speakers <= 1:
        return 6000
    if rounds == 1 and speakers <= 2:
        return 10000
    return 12000

def default_meeting_config(operation=None):
    policy = normalize_meeting_policy((operation or {}).get("meeting_policy") if isinstance(operation, dict) else None)
    trigger = "NEVER" if policy == "NEVER" else "ON_MATERIAL_CONFLICT"
    return {
        "trigger": trigger,
        "participant_employee_ids": [],
        "max_rounds": 2,
        "max_speakers_per_round": 3,
        "contribution_output_cap": 512,
        "token_limit": 6000,
        "budget_twd": 0.50,
        "retry_limit": 1,
    }

def _normalize_meeting_config(operation):
    config = dict(operation.get("meeting_config") or default_meeting_config(operation))
    defaults = default_meeting_config(operation)
    for key, value in defaults.items():
        config.setdefault(key, value)
    operation["meeting_config"] = config
    return config

TASK_FIELDS = {
    "title", "objective", "assignee_employee_id", "reviewer_employee_id",
    "acceptance_criteria", "required_capabilities", "write_scope",
}
TERMINAL = {"COMPLETED", "FAILED", "TERMINATED_BY_FOUNDER", "SUPERSEDED"}
OPEN_STEP = {"EXECUTION_STARTED", "PROVIDER_CALL_STARTED", "AMBIGUOUS"}
GOAL_STATUSES = {
    "SATISFIED", "NOT_SATISFIED", "INSUFFICIENT_EVIDENCE",
}
GOAL_EVIDENCE_BUDGET = 16000
BUDGET_QUANTUM = Decimal("0.0001")


def is_vnext_operation(operation):
    memory = dict(operation.memory_json or {}) if operation else {}
    return bool(
        operation
        and (
            memory.get("runtime_semantics") in {"WORK_VNEXT", "WORK_CORE_V018"}
            or bool(getattr(operation, "works", None))
        )
    )


def _internal_recovery_key(reason, *, condition_type="INTERNAL_RECOVERY"):
    """Stable key for one distinct internal failure class on one Work.

    Retry budgets are failure-scoped, not Work-lifetime-scoped.  A Work that
    recovered from three unrelated local faults must not have a fourth, new
    failure misclassified as exhausted merely because the same Work id was
    involved.  The raw reason remains audit evidence; this compact digest is
    only the ownership key for retry/gate accounting.
    """
    normalized = " ".join(str(reason or "").strip().split())
    basis = f"{str(condition_type or 'INTERNAL_RECOVERY').upper()}|{normalized}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:20]


def pause_for_internal_runtime_recovery(operation, reason, *, work=None, condition_type="INTERNAL_RECOVERY", issue_code=None):
    """Persist a Work-owned internal wait without reviving OperationKernel control.

    INTERNAL_RECOVERY is a bounded timer, not a permanent limbo state. The
    three-attempt allowance belongs to one exact failure signature, not to the
    lifetime of the Work. After three scheduled retries for that same failure,
    the next identical request becomes scoped SYSTEM_RECOVERY so Founder-facing
    truth no longer says the Company is automatically retrying when no timer
    actually exists.
    """
    if not is_vnext_operation(operation):
        return False
    work_runtime = __import__(
        "eason_one.services.work_runtime", fromlist=["ensure_management_work", "open_wait"]
    )
    target = work or work_runtime.ensure_management_work(operation)
    retry_after = None
    effective_condition = str(condition_type or "INTERNAL_RECOVERY").upper()
    requested_reason = str(reason)
    recovery_key = _internal_recovery_key(
        requested_reason, condition_type=effective_condition
    )
    explicit_issue_code = str(issue_code or "").strip() or None
    issue_code = explicit_issue_code
    if effective_condition == "INTERNAL_RECOVERY":
        event_model = __import__(
            "eason_one.models", fromlist=["CompanyEvent"]
        ).CompanyEvent
        prior = 0
        history = event_model.query.filter_by(
            event_type="VNEXT_INTERNAL_RECOVERY", work_id=target.id
        ).all()
        total_prior = len(history)
        for row in history:
            payload = dict(getattr(row, "payload_json", None) or {})
            prior_key = str(payload.get("recovery_key") or "")
            if not prior_key:
                prior_key = _internal_recovery_key(
                    payload.get("reason"),
                    condition_type=payload.get("requested_condition_type") or "INTERNAL_RECOVERY",
                )
            if prior_key == recovery_key:
                prior += 1
        if prior < 3 and total_prior < 12:
            delay_seconds = (5, 15, 30)[min(prior, 2)]
            retry_after = now() + timedelta(seconds=delay_seconds)
            issue_code = explicit_issue_code or f"INTERNAL_RECOVERY:{recovery_key}"
        else:
            effective_condition = "SYSTEM_RECOVERY"
            if prior >= 3:
                issue_code = (
                    f"{explicit_issue_code}:EXHAUSTED"
                    if explicit_issue_code else f"INTERNAL_RECOVERY_EXHAUSTED:{recovery_key}"
                )
                prefix = "Bounded internal recovery exhausted after three scheduled retries for the same failure. "
            else:
                issue_code = (
                    f"{explicit_issue_code}:GLOBAL_EXHAUSTED"
                    if explicit_issue_code else f"INTERNAL_RECOVERY_GLOBAL_EXHAUSTED:{int(target.id)}"
                )
                prefix = "The Work reached the global internal-recovery anomaly ceiling across distinct failures. "
            requested_reason = (
                prefix
                + "Stop automatic retry and repair/reconcile this exact Company execution path before resuming. "
                + requested_reason
            )
    wait_kwargs = {"retry_after": retry_after}
    if issue_code:
        wait_kwargs["issue_code"] = issue_code
    work_runtime.open_wait(
        target, effective_condition, requested_reason, **wait_kwargs
    )
    __import__(
        "eason_one.services.company_events", fromlist=["emit"]
    ).emit(
        "VNEXT_INTERNAL_RECOVERY" if effective_condition == "INTERNAL_RECOVERY" else "VNEXT_INTERNAL_RECOVERY_EXHAUSTED",
        actor_type="RUNTIME", project_id=target.project_id, work_id=target.id,
        correlation_id=f"work:{target.id}",
        payload={
            "reason": requested_reason[:1200],
            "requested_condition_type": str(condition_type or "INTERNAL_RECOVERY").upper(),
            "condition_type": effective_condition,
            "issue_code": issue_code,
            "recovery_key": recovery_key,
            "retry_after": retry_after.isoformat() if retry_after else None,
        },
    )
    # Operation is compatibility/audit projection only for approved vNext work.
    operation.status = "RUNNING"
    operation.kernel_status = "RUNNING"
    operation.current_stage = "WORK_RUNTIME"
    operation.waiting_reason = None
    operation.founder_report_json = None
    if operation.project:
        operation.project.current_state_summary = (
            "CEO is recovering an internal execution problem automatically."
            if retry_after else
            "Company automatic retry is exhausted; system recovery is required inside the existing Founder authority."
            if effective_condition == "SYSTEM_RECOVERY" else
            "Company Runtime recorded an internal recovery issue; no Founder authority is requested."
        )
    db.session.commit()
    __import__(
        "eason_one.services.company_runtime", fromlist=["wake_company_runtime"]
    ).wake_company_runtime()
    return True


def _budget_extension_from_run(run):
    if not run or run.failure_reason != "FOUNDER_BUDGET_EXTENSION_REQUIRED":
        return None
    failure = dict((run.context_composition_json or {}).get("authority_failure") or {})
    if str(failure.get("scope") or "PROJECT").upper() != "PROJECT":
        return None
    try:
        additional = Decimal(str(failure.get("additional_twd") or 0))
    except (InvalidOperation, TypeError, ValueError):
        return None
    # Never manufacture the historical 0.0001 budget gate. Founder sees only a
    # deterministic exact positive Project shortfall.
    return additional if additional > 0 else None


def project_spent(project):
    if not project:
        return Decimal("0")
    # CEO planning happens before Founder approval and remains Company cost, but
    # it must not consume the Project execution envelope that did not yet exist.
    return Decimal(
        db.session.query(func.coalesce(func.sum(CostEvent.real_cost_delta), 0))
        .outerjoin(AgentRun, CostEvent.agent_run_id == AgentRun.id)
        .filter(CostEvent.project_id == project.id)
        .filter(or_(CostEvent.agent_run_id.is_(None), AgentRun.purpose != "CEO_FOUNDER_REQUEST"))
        .scalar()
        or 0
    )


def project_remaining_authority(project):
    if not project:
        return None
    contract = __import__(
        "eason_one.services.project_contract",
        fromlist=["is_vnext_governed", "effective_authority", "assert_authority_ledger"],
    )
    if contract.is_vnext_governed(project):
        # vNext budget truth is the validated Founder Contract ledger. A
        # missing/corrupt/mismatched ledger fails closed; mutable Project
        # compatibility columns are not an alternate authority source.
        authority = contract.assert_authority_ledger(project)
        raw = authority.get("effective_budget_limit_twd")
        if raw in (None, ""):
            return None
        cap = Decimal(str(raw))
    else:
        if project.real_budget_limit is None:
            return None
        cap = Decimal(project.real_budget_limit)
    return max(Decimal("0"), cap - project_spent(project))


def _parse_project_deadline(value):
    if not value:
        return None
    text = clean_text(value, multiline=False)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except ValueError:
        return None


def _engineer():
    return Employee.query.filter_by(slug="engineer", active=True).first()


def _normalize_engineering_plan(payload):
    """Keep the current one-Engineer organization honest and inexpensive."""
    if not isinstance(payload, dict):
        return payload
    operation = payload.get("operation")
    if not isinstance(operation, dict):
        return payload
    engineer = _engineer()
    if not engineer:
        return payload
    tasks = operation.get("tasks") or []
    engineering_tasks = []
    director = Employee.query.filter_by(slug="engineering-director").first()
    for item in tasks:
        if not isinstance(item, dict):
            continue
        if item.get("assignee_employee_id") == getattr(director, "id", None):
            item["assignee_employee_id"] = engineer.id
        if not item.get("required_capabilities"):
            assignee = db.session.get(Employee, item.get("assignee_employee_id"))
            legacy_role_capability = {
                "engineer": "SOFTWARE_ENGINEERING",
                "researcher": "RESEARCH",
                "openai-researcher": "RESEARCH",
                "claude-researcher": "RESEARCH",
                "gemini-researcher": "RESEARCH",
                "perplexity-researcher": "RESEARCH",
                "critic": "CRITICAL_REVIEW",
                "research-director": "RESEARCH",
                "product-strategist": "PRODUCT_STRATEGY",
            }.get(getattr(assignee, "slug", None))
            if legacy_role_capability:
                item["required_capabilities"] = [legacy_role_capability]
        if item.get("assignee_employee_id") == engineer.id:
            engineering_tasks.append(item)
    config = dict(operation.get("meeting_config") or {})
    participants = [value for value in (config.get("participant_employee_ids") or []) if value != getattr(director, "id", None)]
    config["participant_employee_ids"] = participants
    if tasks and len(engineering_tasks) == len(tasks):
        operation["meeting_policy"] = "NEVER"
        # Keep execution-local Task acceptance separate from Project completion.
        # Codex is an implementation tool and cannot truthfully prove company-level
        # criteria such as total cost, persisted artifacts, or CEO reporting. Those
        # are verified from runtime/Company Truth during Project closure.
        # Codex uses the Founder's local ChatGPT/Codex allowance, not paid API
        # tokens. Keep only the minimum accounting quantum; autonomous retries
        # are governed separately by the Codex usage/retry envelope.
        operation["budget_twd"] = float(BUDGET_QUANTUM)
        config.update({
            "trigger": "NEVER", "participant_employee_ids": [],
            "max_rounds": 1, "max_speakers_per_round": 1,
            "contribution_output_cap": 192, "token_limit": 6000,
            "budget_twd": 0, "retry_limit": 0,
        })
    operation["meeting_config"] = config
    return payload


def _codex_task(item):
    employee = db.session.get(Employee, item.get("assignee_employee_id")) if isinstance(item, dict) else None
    return bool(employee and employee.slug == "engineer")


def _codex_only_operation(operation):
    tasks = list(operation.tasks)
    return bool(tasks and all(task.assigned_employee and task.assigned_employee.slug == "engineer" for task in tasks))


def budget_authorization_amount(amount):
    value = Decimal(str(amount))
    if value <= 0:
        raise ValueError("Additional authorization must be positive")
    return value.quantize(BUDGET_QUANTUM, rounding=ROUND_CEILING)


def _budget_text(value):
    text = format(Decimal(value), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _normalized_plan_text(value):
    return " ".join(str(value or "").casefold().split())


def plan_fingerprint(plan):
    """Stable fingerprint used to prevent repeated CEO prompts creating duplicates."""
    data = (plan or {}).get("operation") or {}
    task_rows = [
        {
            "title": _normalized_plan_text(item.get("title")),
            "objective": _normalized_plan_text(item.get("objective")),
            "assignee": item.get("assignee_employee_id"),
            "reviewer": item.get("reviewer_employee_id"),
        }
        for item in data.get("tasks") or []
    ]
    payload = {
        "title": _normalized_plan_text(data.get("title")),
        "objective": _normalized_plan_text(data.get("objective")),
        "tasks": task_rows,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _estimate_call(employee, input_tokens, output_tokens, *, execution_constraints_override=None):
    if not employee or not employee.current_model:
        raise ValueError("Every planned Employee requires an active ModelConfig")
    model = employee.current_model
    constraints = dict(execution_constraints_override or {})
    excluded = {
        str(value or "").strip().casefold()
        for value in (constraints.get("excluded_providers") or [])
        if str(value or "").strip()
    }
    # Live formal Operations must be priced using a model that Runtime can
    # actually execute. Persistent identity bindings are not pricing truth.
    flask = __import__("flask", fromlist=["current_app"])
    live_formal = not bool(
        flask.current_app.config.get("TESTING")
        or flask.current_app.config.get("ALLOW_MOCK_PROVIDER")
    )
    if live_formal:
        policy = __import__(
            "eason_one.services.execution_policy",
            fromlist=["_configured", "_score", "_runtime_eligible", "_dedicated_provider", "_claude_allowed_for_employee", "_is_premium"],
        )
        dedicated = policy._dedicated_provider(employee)
        if dedicated and dedicated in excluded:
            raise ValueError(
                f"FOUNDER_PROVIDER_EXCLUDED: {employee.name} requires provider {dedicated}, which Founder forbade."
            )
        candidates = []
        for row in ModelConfig.query.filter_by(active=True, archived=False).all():
            if not policy._runtime_eligible(row):
                continue
            if row.provider_key in excluded:
                continue
            if dedicated and row.provider_key != dedicated:
                continue
            if not dedicated and not policy._claude_allowed_for_employee(employee) and row.provider_key == "anthropic":
                continue
            if constraints.get("no_premium") and policy._is_premium(row):
                continue
            if constraints.get("low_cost_only") and policy._is_premium(row):
                continue
            candidates.append(row)
        if not candidates:
            raise ValueError(
                "MODEL_POLICY_BLOCKED: no configured real ModelConfig satisfies the approved execution constraints."
            )
        model = model if model in candidates else sorted(candidates, key=lambda row: (policy._score(row), row.id))[0]
    capped_output = min(int(output_tokens), int(model.max_output_tokens))
    return __import__("eason_one.services.costs", fromlist=["calculate"]).calculate(
        model, int(input_tokens), capped_output
    )



def _estimate_research_call(employee, input_tokens, output_tokens, *, execution_constraints_override=None):
    """Price the same live research substrate runtime will use before Founder approval.

    A provider-specialized Researcher may never be priced as one provider and
    silently executed by another. OpenAI/Perplexity specialists therefore use
    only their own provider family for hosted search. If that family has no
    governed search configuration, Runtime degrades that specialist to an
    explicitly unsourced model perspective and this estimator prices the same
    regular model call.
    """
    flask = __import__("flask",fromlist=["current_app"])
    if flask.current_app.config.get("TESTING"):
        return _estimate_call(
            employee, input_tokens, output_tokens,
            execution_constraints_override=execution_constraints_override,
        )
    constraints = dict(execution_constraints_override or {})
    excluded = {
        str(value or "").strip().casefold()
        for value in (constraints.get("excluded_providers") or [])
        if str(value or "").strip()
    }
    policy = __import__("eason_one.services.execution_policy", fromlist=["_configured", "_score", "_runtime_eligible"])
    research_department = __import__(
        "eason_one.services.research_department", fromlist=["provider_family"]
    )
    dedicated = research_department.provider_family(employee)
    if dedicated and dedicated in excluded:
        raise ValueError(
            f"FOUNDER_PROVIDER_EXCLUDED: {employee.name} requires provider {dedicated}, which Founder forbade."
        )
    candidates = [
        row for row in ModelConfig.query.filter_by(active=True, archived=False).all()
        if row.provider_key in {"openai", "perplexity"}
        and row.provider_key not in excluded
        and (not dedicated or row.provider_key == dedicated)
        and policy._configured(row)
        and policy._runtime_eligible(row, web_search=True)
    ]
    if not candidates:
        if dedicated in {"openai", "perplexity"}:
            return _estimate_call(employee, input_tokens, output_tokens, execution_constraints_override=execution_constraints_override)
        raise ValueError(
            "RESEARCH_TOOL_UNAVAILABLE: live RESEARCH Tasks require a configured OpenAI Web Search or Perplexity/Sonar ModelConfig with explicit request_price_per_call before Founder approval."
        )
    model = sorted(
        candidates,
        key=lambda row: (Decimal(getattr(row,"request_price_per_call",0) or 0), policy._score(row), row.id),
    )[0]
    capped_output=min(int(output_tokens),int(model.max_output_tokens))
    return __import__("eason_one.services.costs",fromlist=["calculate"]).calculate(
        model,int(input_tokens),capped_output,include_request_fee=True
    )


def _project_outcome_reviewer_reserve(ceo, *, execution_constraints_override=None):
    """Price a conservative active reviewer pool for final Project review.

    Runtime excludes accepted delivery producers when the Project actually closes.
    Pre-approval cannot know that future producer set, so reserve the most
    expensive currently plausible CRITICAL_REVIEW/CEO call rather than assuming
    the Critic will always be eligible.
    """
    formation = __import__(
        "eason_one.services.team_formation", fromlist=["employee_capabilities"]
    )
    candidates = [
        employee for employee in Employee.query.filter_by(active=True).order_by(Employee.id).all()
        if employee.slug == "ceo" or "CRITICAL_REVIEW" in formation.employee_capabilities(employee)
    ]
    if ceo and ceo not in candidates:
        candidates.append(ceo)
    priced = [(
        _estimate_call(employee, 5200, 1200, execution_constraints_override=execution_constraints_override), employee
    ) for employee in candidates if employee and employee.current_model]
    if not priced:
        return Decimal("0"), ceo
    priced.sort(key=lambda row: (row[0], getattr(row[1], "id", 0)), reverse=True)
    return priced[0]


def _planned_meeting_chair(data, ceo):
    """Mirror current Meeting materialization ownership for pre-approval pricing."""
    config = data.get("meeting_config") or {}
    trigger = str(config.get("trigger") or "NEVER").upper()
    if trigger != "ON_MATERIAL_CONFLICT":
        return ceo
    participant_ids = {int(value) for value in (config.get("participant_employee_ids") or [])}
    reviewer_ids = {
        int(item.get("reviewer_employee_id"))
        for item in (data.get("tasks") or [])
        if item.get("reviewer_employee_id")
    }
    critic = Employee.query.filter_by(slug="critic", active=True).first()
    if critic and (critic.id in participant_ids or critic.id in reviewer_ids):
        return critic
    for item in data.get("tasks") or []:
        reviewer_id = item.get("reviewer_employee_id")
        reviewer = db.session.get(Employee, reviewer_id) if reviewer_id else None
        if reviewer and reviewer.active:
            return reviewer
    return ceo


def _plan_uses_existing_evidence_only(plan) -> bool:
    text=json.dumps(plan or {},ensure_ascii=False).casefold()
    return any(phrase in text for phrase in (
        "existing evidence only", "only existing", "use existing eason one evidence",
        "using only eason one evidence", "no extensive new research",
    ))

def _staffing_assessment_reserve(data):
    """Reserve governed HR assessment for explicit capability gaps before approval.

    Founder should not approve an Operation that promises autonomous staffing
    when the current roster lacks a required capability but the HR execution
    core is unavailable or unpriced. One assessment is reserved per unique
    missing capability; multiple Works may share the same resulting hire.
    """
    formation = __import__(
        "eason_one.services.team_formation",
        fromlist=["best_existing_employee", "CANONICAL_DELIVERY_CAPABILITIES"],
    )
    gaps = []
    for item in data.get("tasks") or []:
        capability = str((item.get("required_capabilities") or [""])[0] or "").upper()
        if not capability or capability not in formation.CANONICAL_DELIVERY_CAPABILITIES:
            continue
        if formation.best_existing_employee(capability) is None and capability not in gaps:
            gaps.append(capability)
    if not gaps:
        return Decimal("0"), []

    hr = Employee.query.filter_by(slug="hr-director", active=True).first()
    if not hr or not hr.current_model:
        raise ValueError(
            "STAFFING_RUNTIME_UNAVAILABLE: proposal requires a new capability, but HR Director has no active execution model."
        )
    workforce = __import__(
        "eason_one.services.workforce", fromlist=["assessment_authorization"]
    )
    per_gap = workforce.assessment_authorization(hr)
    if per_gap is None:
        raise ValueError(
            "STAFFING_RUNTIME_UNAVAILABLE: HR assessment model cannot satisfy the governed assessment envelope."
        )
    per_gap = Decimal(per_gap)
    total = per_gap * len(gaps)
    return total, [
        {
            "kind": "HR_CAPABILITY_ASSESSMENT",
            "capability": capability,
            "estimated_twd": str(per_gap),
        }
        for capability in gaps
    ]


def execution_budget_estimate(plan, *, include_orchestration=False, execution_constraints_override=None):
    """Conservative pre-approval estimate for the complete governed workflow.

    This is intentionally an envelope, not a promise. It prices Task execution,
    independent review, Goal Verification, Founder report, the approved
    Meeting policy, and (for new Multi-Agent Operations) one bounded
    orchestration call without making a Provider call.
    """
    validated = validate_plan(plan)
    data = validated["operation"]
    execution_constraints_override = dict(execution_constraints_override or {})
    total = Decimal("0")
    breakdown = []
    codex_only = all(_codex_task(item) for item in data["tasks"])
    staffing_reserve, staffing_breakdown = _staffing_assessment_reserve(data)
    total += staffing_reserve
    breakdown.extend(staffing_breakdown)
    for item in data["tasks"]:
        assignee = db.session.get(Employee, item["assignee_employee_id"])
        reviewer_id = item.get("reviewer_employee_id")
        reviewer = db.session.get(Employee, reviewer_id) if reviewer_id else None
        required_capability = str((item.get("required_capabilities") or [""])[0] or "").upper()
        research_department = __import__(
            "eason_one.services.research_department", fromlist=["provider_family", "is_synthesis_text"]
        )
        dedicated_provider = research_department.provider_family(assignee)
        department_synthesis = bool(
            required_capability == "RESEARCH" and getattr(assignee, "slug", None) == "research-director"
            and research_department.is_synthesis_text(item.get("title"), item.get("objective"))
        )
        perspective_research = bool(
            required_capability == "RESEARCH" and dedicated_provider in {"anthropic", "gemini"}
        )
        live_research = bool(
            required_capability == "RESEARCH" and not _plan_uses_existing_evidence_only(validated)
            and not department_synthesis and not perspective_research
        )
        # Semantic Task review is always an ordinary reviewer model call.
        # Even when the producer performed hosted/live research, review_work()
        # uses select_execution_model(..., "TASK_REVIEW") rather than a web
        # search substrate. Pricing a research request fee here would make the
        # Founder approve a different workflow than Runtime actually executes.
        review_cost = (
            _estimate_call(reviewer, 3800, 900, execution_constraints_override=execution_constraints_override)
            if reviewer and assignee and reviewer.id != assignee.id else Decimal("0")
        )
        if assignee and assignee.slug == "engineer":
            # Codex itself is plan/tool usage rather than a provider-billed model
            # call, but an independent semantic reviewer is still a real model
            # call and must be priced before Founder approval.
            task_cost = Decimal("0")
            kind = "ENGINEER_CODEX_TOOL"
        else:
            if live_research:
                task_cost = _estimate_research_call(assignee, 3200, 1100, execution_constraints_override=execution_constraints_override)
            else:
                task_cost = _estimate_call(assignee, 3200, 1100, execution_constraints_override=execution_constraints_override)
            kind = "TASK_AND_REVIEW" if review_cost > 0 else "TASK_ONLY"
        total += task_cost + review_cost
        breakdown.append({
            "kind": "TASK_EXECUTION" if kind != "ENGINEER_CODEX_TOOL" else kind,
            "title": item["title"],
            "estimated_twd": str(task_cost),
        })
        if review_cost > 0:
            breakdown.append({
                "kind": "TASK_REVIEW",
                "title": item["title"],
                "estimated_twd": str(review_cost),
            })
    ceo = db.session.get(Employee, data.get("proposed_by_employee_id") or 0)
    if not ceo:
        # The CEO is the Operation proposer in V1. The payload itself does not
        # carry the proposer ID, so resolve the persistent CEO role.
        ceo = Employee.query.filter_by(slug="ceo").first()
    if include_orchestration and len(data["tasks"]) > 1 and not codex_only:
        orchestration = _estimate_call(ceo, 3600, 900, execution_constraints_override=execution_constraints_override)
        total += orchestration
        breakdown.append({"kind": "ORCHESTRATION_PLAN", "estimated_twd": str(orchestration)})
    # v0.20: Operation/Mission closure is deterministic bookkeeping. It may not
    # buy a CEO prose/report call or use Mission criteria as Project acceptance.
    # Reserve at most one Project-level semantic evidence review. If accepted
    # Work contracts directly prove the Founder Project criteria, the kernel
    # skips this call and the reserve remains unspent.
    project_review, outcome_reviewer = _project_outcome_reviewer_reserve(ceo, execution_constraints_override=execution_constraints_override)
    total += project_review
    breakdown.append({
        "kind": "PROJECT_OUTCOME_REVIEW",
        "estimated_twd": str(project_review),
        "employee_id": getattr(outcome_reviewer, "id", None),
        "pricing_basis": "MAX_ACTIVE_INDEPENDENT_REVIEWER_POOL",
    })
    if codex_only:
        breakdown.append({
            "kind": "DETERMINISTIC_ENGINEERING_VERIFICATION",
            "estimated_twd": "0",
        })
    config = data.get("meeting_config") or {}
    meeting_estimate = Decimal("0")
    if config.get("trigger") != "NEVER":
        participant_ids = config.get("participant_employee_ids") or []
        participants = [db.session.get(Employee, employee_id) for employee_id in participant_ids]
        participants = [employee for employee in participants if employee and employee.active]
        chair = _planned_meeting_chair(data, ceo)
        if chair and chair.id not in {employee.id for employee in participants}:
            participants = [chair, *participants]
        participants = list({employee.id: employee for employee in participants if employee and employee.active}.values())[:4]
        router = _estimate_call(chair, 2600, 256, execution_constraints_override=execution_constraints_override)
        meeting_estimate += router
        rounds = int(config.get("max_rounds") or 1)
        speakers = min(int(config.get("max_speakers_per_round") or 1), len(participants) or 1)
        output_cap = int(config.get("contribution_output_cap") or 512)
        for _ in range(rounds):
            for employee in (participants[:speakers] or [chair]):
                meeting_estimate += _estimate_call(employee, 3000, output_cap, execution_constraints_override=execution_constraints_override)
            meeting_estimate += _estimate_call(chair, 3600, 768, execution_constraints_override=execution_constraints_override)
        total += meeting_estimate
        breakdown.append({
            "kind": "MEETING_ENVELOPE",
            "estimated_twd": str(meeting_estimate),
        })
    # Reserve covers context growth and one bounded revise/review cycle without
    # pretending the estimate is exact.
    total = (total * Decimal("1.20")).quantize(BUDGET_QUANTUM, rounding=ROUND_CEILING)
    # Local Codex execution itself may be zero provider cost, but semantic
    # review and CEO closure are priced above when present.  Keep only a tiny
    # accounting quantum as the floor; never use "Codex-only" to erase planned
    # model calls from the Founder-visible envelope.
    minimum = BUDGET_QUANTUM
    return max(total, minimum), breakdown, meeting_estimate


def reconcile_completion_reserve(operation):
    """Backfill a truthful delivery reserve for nonterminal legacy Missions.

    V0.12.1 introduced reserve enforcement for new Operations. Older persisted
    Missions may still display NT$0.00 because their memory predates that
    contract. Rebuild the deterministic estimate without changing Founder
    authority or charging a provider.
    """
    if operation.status in TERMINAL or _codex_only_operation(operation):
        return False
    memory = dict(operation.memory_json or {})
    try:
        existing = Decimal(str(memory.get("completion_reserve_twd") or 0))
    except (InvalidOperation, TypeError):
        existing = Decimal("0")
    if existing > 0:
        return False
    try:
        runtime_constraints = __import__(
            "eason_one.services.execution_policy", fromlist=["execution_constraints"]
        ).execution_constraints(operation)
        if runtime_constraints.get("constraint_conflict"):
            return False
        estimate, breakdown, _ = execution_budget_estimate(
            operation.plan_json,
            execution_constraints_override=runtime_constraints,
        )
    except Exception:
        return False
    completion = sum(
        (Decimal(str(row.get("estimated_twd") or 0)) for row in breakdown
         if row.get("kind") in {
             "PROJECT_OUTCOME_REVIEW",
             "DETERMINISTIC_ENGINEERING_VERIFICATION",
         }),
        Decimal("0"),
    )
    report = sum(
        (Decimal(str(row.get("estimated_twd") or 0)) for row in breakdown
         if row.get("kind") == "CEO_OPERATION_REPORT"),
        Decimal("0"),
    )
    if completion <= 0:
        return False
    memory["execution_budget_estimate_twd"] = str(estimate)
    memory["execution_budget_breakdown"] = breakdown
    memory["completion_reserve_twd"] = str(completion)
    memory["report_reserve_twd"] = str(report)
    operation.memory_json = memory
    db.session.commit()
    return True


def ensure_full_execution_authority(operation):
    """Keep execution inside Project authority without re-asking the Founder.

    A Project budget is the Founder-approved hard envelope. Operation budgets are
    internal CEO allocations. When a bounded step needs more Operation headroom
    but the Project still has enough authority, the runtime delegates only the
    required shortfall deterministically instead of manufacturing a Founder gate.
    """
    has_execution = bool(AgentRun.query.filter(
        AgentRun.operation_id == operation.id,
        AgentRun.purpose != "CEO_FOUNDER_REQUEST",
    ).count())
    # The full priced workflow is an admission check, not a charge repeated on
    # every step. Once execution has started, already-paid Work must reduce the
    # *remaining* requirement rather than forcing the original full estimate to
    # fit again. vNext therefore only normalizes compatibility safety fields on
    # later steps; per-call reservation + remaining Project authority govern
    # actual spend from that point onward.
    if has_execution:
        if operation.works:
            operation.single_call_cost_cap_twd = Decimal(operation.approved_budget_twd)
            operation.stage_cost_cap_twd = Decimal(operation.approved_budget_twd)
            delivery = [work for work in operation.works if work.work_type != "MANAGEMENT"]
            operation.max_calls = max(
                int(operation.max_calls or 0),
                6 + (len(delivery) * 2)
                + sum(max(0, int(work.retry_limit or 0)) for work in delivery),
            )
            db.session.commit()
        return True
    runtime_constraints = __import__(
        "eason_one.services.execution_policy", fromlist=["execution_constraints"]
    ).execution_constraints(operation)
    if runtime_constraints.get("constraint_conflict"):
        raise ValueError(
            "FOUNDER_PROVIDER_CONSTRAINT_CONFLICT: current Project and Mission execution authority have no permitted intersection."
        )
    estimate, breakdown, meeting_estimate = execution_budget_estimate(
        operation.plan_json,
        include_orchestration=bool((operation.memory_json or {}).get("multi_agent_enabled")),
        execution_constraints_override=runtime_constraints,
    )
    memory = dict(operation.memory_json or {})
    memory["execution_budget_estimate_twd"] = str(estimate)
    memory["execution_budget_breakdown"] = breakdown
    # Reconcile stale per-stage controls against the same priced workflow used
    # by the actual execution policy.  This is especially important for older
    # Operations created while an Employee's persistent default binding was
    # mock but formal execution was later routed to a real model.
    stage_budget_caps = {}
    for row in breakdown:
        kind = row.get("kind")
        if kind == "MEETING_ENVELOPE":
            kind = "MEETING"
        if kind == "ENGINEER_CODEX_TOOL":
            kind = "TASK_EXECUTION"
        stage_budget_caps[kind] = str(
            (Decimal(str(stage_budget_caps.get(kind) or 0))
             + Decimal(str(row.get("estimated_twd") or 0)) * Decimal("1.20"))
            .quantize(BUDGET_QUANTUM, rounding=ROUND_CEILING)
        )
    previous_contingency = (dict(memory.get("stage_budget_caps") or {}).get("CONTINGENCY"))
    if previous_contingency is not None:
        stage_budget_caps["CONTINGENCY"] = str(previous_contingency)
    memory["stage_budget_caps"] = stage_budget_caps
    completion = sum(
        (Decimal(str(row.get("estimated_twd") or 0)) for row in breakdown
         if row.get("kind") in {"PROJECT_OUTCOME_REVIEW", "DETERMINISTIC_ENGINEERING_VERIFICATION"}),
        Decimal("0"),
    )
    report = sum(
        (Decimal(str(row.get("estimated_twd") or 0)) for row in breakdown
         if row.get("kind") == "CEO_OPERATION_REPORT"),
        Decimal("0"),
    )
    memory["completion_reserve_twd"] = str(completion)
    memory["report_reserve_twd"] = str(report)
    operation.memory_json = memory
    available = remaining_budget(operation)
    if available < estimate:
        _delegate_project_authority(operation, estimate)
        available = remaining_budget(operation)
    if available >= estimate:
        # Existing V0.10.5 Operations may carry the model's tiny placeholder
        # budget (for example NT$0.03) even after the Founder authorizes the
        # system envelope. Normalize the contract only after sufficient
        # authority exists, so the later Meeting cannot fail on its stale
        # sub-envelope.
        plan = dict(operation.plan_json or {})
        data = dict(plan.get("operation") or {})
        data["budget_twd"] = str(max(Decimal(str(data.get("budget_twd") or 0)), Decimal(operation.approved_budget_twd)))
        config = dict(data.get("meeting_config") or {})
        if config.get("trigger") != "NEVER":
            reserved_meeting = (meeting_estimate * Decimal("1.20")).quantize(
                BUDGET_QUANTUM, rounding=ROUND_CEILING
            )
            config["budget_twd"] = str(max(
                Decimal(str(config.get("budget_twd") or 0)), reserved_meeting
            ))
            data["meeting_config"] = config
        plan["operation"] = data
        operation.plan_json = plan
        if operation.works:
            # vNext: this field is compatibility/read-model metadata only.  A
            # provider reservation is governed by Work + Project authority,
            # never by a stale token estimate from the original proposal.
            operation.single_call_cost_cap_twd = Decimal(operation.approved_budget_twd)
            operation.stage_cost_cap_twd = Decimal(operation.approved_budget_twd)
            operation.max_calls = max(
                int(operation.max_calls or 0),
                6 + (len([work for work in operation.works if work.work_type != "MANAGEMENT"]) * 2)
                + sum(max(0, int(work.retry_limit or 0)) for work in operation.works if work.work_type != "MANAGEMENT"),
            )
        else:
            largest_planned_call = max(
                (Decimal(str(row.get("estimated_twd") or 0)) * Decimal("1.20") for row in breakdown),
                default=BUDGET_QUANTUM,
            ).quantize(BUDGET_QUANTUM, rounding=ROUND_CEILING)
            operation.single_call_cost_cap_twd = min(
                Decimal(operation.approved_budget_twd),
                max(BUDGET_QUANTUM, largest_planned_call),
            )
        db.session.commit()
        return True
    additional = estimate - available
    wait_for_founder(
        operation,
        "The approved envelope is below the system estimate for the complete "
        "Task, review, verification, report, and Meeting path. No Employee Run "
        "was started.",
        additional_budget=additional,
        decision_kind="BUDGET_AUTHORIZATION",
    )
    return False


class OperationBudgetRequired(ValueError):
    def __init__(self, approved, spent, additional):
        self.approved = Decimal(approved)
        self.spent = Decimal(spent)
        self.additional = Decimal(additional)
        super().__init__(
            "operation budget requires additional Founder authorization"
        )


def normalize_meeting_policy(value):
    text = (value or "").strip().upper()
    if text in {"NEVER", "NONE", "NO MEETINGS"} or "NEVER" in text:
        return "NEVER"
    if text == "AUTO":
        return "AUTO"
    return "REQUIRED_ON_MATERIAL_CONFLICT"


def _assert_known_accountable_capabilities(operation):
    """Reject semantically unknown delivery capabilities before other plan rewrites.

    Verification-owner assignment and other normalization may legitimately inspect
    a Task, but they must never mask the more fundamental contract error that the
    Company does not recognize the accountable delivery capability.
    """
    if not isinstance(operation, dict):
        return
    formation = __import__(
        "eason_one.services.team_formation", fromlist=["CANONICAL_DELIVERY_CAPABILITIES"]
    )
    for item in operation.get("tasks") or []:
        if not isinstance(item, dict):
            continue
        capabilities = item.get("required_capabilities")
        if not (
            isinstance(capabilities, list)
            and len(capabilities) == 1
            and isinstance(capabilities[0], str)
            and capabilities[0].strip()
        ):
            continue
        primary_capability = str(capabilities[0]).strip().upper()
        if primary_capability not in formation.CANONICAL_DELIVERY_CAPABILITIES:
            raise ValueError(
                f"Unknown accountable capability {primary_capability!r}; CEO must use one canonical delivery capability "
                "or split/reframe the Task before Founder approval"
            )


def _assign_verification_owners(operation):
    """Make verification ownership explicit before Founder approval.

    Semantic criteria may not be self-certified by the producing Employee.
    Purely deterministic criteria use host proof and therefore do not consume a
    second Employee review merely because the model proposed one.
    """
    if not isinstance(operation, dict):
        return operation
    acceptance = __import__(
        "eason_one.services.acceptance_contract", fromlist=["build"]
    )
    ceo = Employee.query.filter_by(slug="ceo", active=True).first()
    critic = Employee.query.filter_by(slug="critic", active=True).first()
    active = Employee.query.filter_by(active=True).order_by(Employee.id).all()

    for item in operation.get("tasks") or []:
        if not isinstance(item, dict):
            continue
        assignee_id = item.get("assignee_employee_id")
        reviewer_id = item.get("reviewer_employee_id")
        contract = acceptance.build(
            title=item.get("title") or "",
            objective=item.get("objective") or "",
            criteria=item.get("acceptance_criteria") or [],
            reviewer_employee_id=reviewer_id,
            owner_employee_id=assignee_id,
        )
        needs_independent = bool(acceptance.semantic_criteria(contract))
        if needs_independent and reviewer_id in (None, assignee_id):
            # Independent semantic verification is company work, not an excuse
            # to route every specialist result back through the CEO. Prefer the
            # persistent Critic, then another active non-CEO Employee; CEO is a
            # last-resort verifier only when the current organization has no
            # other independent reviewer.
            reviewer_candidates = []
            if critic is not None:
                reviewer_candidates.append(critic)
            reviewer_candidates.extend(
                employee for employee in active
                if employee.id != getattr(critic, "id", None)
                and employee.id != getattr(ceo, "id", None)
            )
            if ceo is not None:
                reviewer_candidates.append(ceo)
            candidate = next(
                (employee for employee in reviewer_candidates if employee.id != assignee_id),
                None,
            )
            if candidate is None:
                raise ValueError(
                    "Approved acceptance contains semantic criteria but no independent reviewer Employee is available."
                )
            item["reviewer_employee_id"] = candidate.id
        elif not needs_independent:
            # Deterministic host evidence does not need a paid Employee review.
            # Clear model-proposed reviewers so verification scope cannot waste
            # tokens or accidentally bypass host proof.
            item["reviewer_employee_id"] = None
    return operation


def validate_plan(payload):
    if not isinstance(payload, dict) or set(payload) not in (PLAN_FIELDS, PROJECT_PLAN_FIELDS):
        raise ValueError("Invalid OPERATION_PLAN fields")
    if payload.get("mode") != "OPERATION_PLAN":
        raise ValueError("Operation mode must be OPERATION_PLAN")
    if not isinstance(payload.get("executive_response"), str) or not payload[
        "executive_response"
    ].strip():
        raise ValueError("Executive response is required")
    payload = _sanitize_operation_plan(payload)
    payload = _normalize_engineering_plan(payload)
    operation = payload.get("operation")
    _assert_known_accountable_capabilities(operation)
    operation = _assign_verification_owners(operation)
    payload["operation"] = operation
    if not isinstance(operation, dict) or set(operation) not in (
        OPERATION_FIELDS, LEGACY_OPERATION_FIELDS,
    ):
        raise ValueError("Invalid operation fields")
    meeting_config = _normalize_meeting_config(operation)
    for field in ("title", "objective", "meeting_policy"):
        if not isinstance(operation[field], str) or not operation[field].strip():
            raise ValueError(f"Operation {field} is required")
    try:
        budget = Decimal(str(operation["budget_twd"]))
    except (InvalidOperation, TypeError):
        raise ValueError("Operation budget must be numeric")
    if budget <= 0:
        raise ValueError("Operation budget must be positive")
    if operation["project_id"] is not None and not db.session.get(
        Project, operation["project_id"]
    ):
        raise ValueError("Operation references an unknown Project")
    tasks = operation["tasks"]
    if not isinstance(tasks, list) or not 1 <= len(tasks) <= 12:
        raise ValueError("Operation requires 1–12 tasks")
    for item in tasks:
        if not isinstance(item, dict) or set(item) != TASK_FIELDS:
            raise ValueError("Invalid operation task fields")
        if not item["title"].strip() or not item["objective"].strip():
            raise ValueError("Task title and objective are required")
        if (
            not isinstance(item["acceptance_criteria"], list)
            or not 1 <= len(item["acceptance_criteria"]) <= 8
            or not all(
                isinstance(value, str) and value.strip() and len(value) <= 280
                for value in item["acceptance_criteria"]
            )
        ):
            raise ValueError("Task acceptance criteria require 1-8 clean rows of at most 280 characters")
        capabilities = item.get("required_capabilities")
        if (
            not isinstance(capabilities, list)
            or len(capabilities) != 1
            or not all(isinstance(value, str) and value.strip() and len(value) <= 60 for value in capabilities)
        ):
                raise ValueError("Each Task must declare exactly one accountable required capability; split materially different capabilities into separate Tasks")
        scope = item.get("write_scope")
        primary_capability = str(capabilities[0] or "").upper()
        connector = __import__(
            "eason_one.services.codex_connector",
            fromlist=["normalize_write_scope", "plan_task_is_read_only"],
        )
        if primary_capability == "SOFTWARE_ENGINEERING":
            if scope is None:
                if not connector.plan_task_is_read_only(item):
                    raise ValueError(
                        "SOFTWARE_ENGINEERING Task requires an explicit CODEX_WRITE_SCOPE_V1 "
                        "before Founder approval; only an explicitly read-only engineering Task may use null write_scope"
                    )
            else:
                connector.normalize_write_scope(scope)
        elif scope is not None:
            raise ValueError(
                "Only SOFTWARE_ENGINEERING Task may declare CODEX write_scope"
            )
        assignee = db.session.get(Employee, item["assignee_employee_id"])
        reviewer_id = item.get("reviewer_employee_id")
        reviewer = db.session.get(Employee, reviewer_id) if reviewer_id else None
        if not assignee or not assignee.active:
            raise ValueError("Unknown or inactive assignee")
        if reviewer_id is not None and (not reviewer or not reviewer.active):
            raise ValueError("Unknown or inactive reviewer")
    if meeting_config["trigger"] not in MEETING_TRIGGERS:
        raise ValueError("Invalid Meeting trigger")
    if meeting_config["trigger"] != "NEVER":
        meeting_config["token_limit"] = max(
            int(meeting_config["token_limit"]),
            _minimum_meeting_token_limit(meeting_config),
        )
    participant_ids = meeting_config["participant_employee_ids"]
    if not isinstance(participant_ids, list) or len(participant_ids) > 4:
        raise ValueError("Meeting allows at most four participants")
    if len(set(participant_ids)) != len(participant_ids):
        raise ValueError("Meeting participants must be unique")
    for employee_id in participant_ids:
        employee = db.session.get(Employee, employee_id)
        if not employee or not employee.active:
            raise ValueError("Meeting participant must be an active Employee")
    bounds = {
        "max_rounds": (1, 3),
        "max_speakers_per_round": (1, 4),
        "contribution_output_cap": (192, 1024),
        "token_limit": (1200, 12000),
        "retry_limit": (0, 1),
    }
    for field, (minimum, maximum) in bounds.items():
        value = meeting_config[field]
        if not isinstance(value, int) or not minimum <= value <= maximum:
            raise ValueError(f"Meeting {field} is outside the governed range")
    meeting_budget = Decimal(str(meeting_config["budget_twd"]))
    if meeting_budget < 0 or meeting_budget > budget:
        raise ValueError("Meeting budget must fit inside the Operation budget")
    if meeting_config["trigger"] != "NEVER" and meeting_budget <= 0:
        raise ValueError("A planned Meeting requires a positive bounded budget")
    if meeting_config["trigger"] != "NEVER" and not participant_ids:
        participant_ids = []
        for item in tasks:
            for employee_id in (
                item["assignee_employee_id"], item.get("reviewer_employee_id"),
            ):
                if employee_id is not None and employee_id not in participant_ids:
                    participant_ids.append(employee_id)
        ceo_id = payload.get("operation", {}).get("proposed_by_employee_id")
        meeting_config["participant_employee_ids"] = participant_ids[:4]
    criteria = operation["completion_criteria"]
    if not isinstance(criteria, list) or not 1 <= len(criteria) <= 8 or not all(
        isinstance(value, str) and value.strip() and len(value) <= 280 for value in criteria
    ):
        raise ValueError(
            "Operation requires between 1 and 8 clean completion criteria of at most 280 characters"
        )
    return payload


def founder_declared_budget_cap(founder_request):
    """Return an explicit Founder TWD ceiling without interpreting unrelated numbers."""
    text = str(founder_request or "")
    patterns = [
        r"(?:NT\$|TWD|新台幣)\s*([0-9]+(?:\.[0-9]+)?)",
        r"(?:預算(?:上限)?|花費上限|成本上限|budget|spending\s+cap|cost\s+cap|hard\s+cap)\s*(?:is|of|為|是|:|：|<=|≤)?\s*(?:NT\$|TWD|新台幣)?\s*([0-9]+(?:\.[0-9]+)?)\s*(?:元|塊)?",
        r"(?:最多|不超過|以內|within|under|not\s+exceed(?:ing)?)\s*(?:NT\$|TWD|新台幣)\s*([0-9]+(?:\.[0-9]+)?)",
        r"([0-9]+(?:\.[0-9]+)?)\s*(?:元|塊)\s*(?:以內|內|上限|最多|不超過)",
    ]
    values = []
    for pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            try:
                value = Decimal(match.group(1))
            except (InvalidOperation, TypeError):
                continue
            if value > 0:
                values.append(value.quantize(BUDGET_QUANTUM, rounding=ROUND_CEILING))
    return min(values) if values else None


def _merge_provider_execution_constraints(*sources) -> dict:
    """Merge durable Founder/Project execution authority without widening it.

    The historical name is kept to avoid churn, but this now persists every
    runtime restriction currently enforced by execution policy, not only
    provider choice. Project Contract constraints are inherited by every later
    Mission; a Mission may narrow them, never silently forget them.
    """
    policy = __import__(
        "eason_one.services.execution_policy", fromlist=["compile_text_constraints"]
    )
    excluded: set[str] = set()
    allowlists: list[set[str]] = []
    boolean_restrictions = {
        "low_cost_only": False,
        "no_premium": False,
        "existing_evidence_only": False,
    }
    for source in sources:
        if source in (None, "", [], ()):
            continue
        if isinstance(source, (list, tuple, set)):
            text = " ; ".join(str(value or "") for value in source if str(value or "").strip())
        else:
            text = str(source or "")
        if not text.strip():
            continue
        compiled = policy.compile_text_constraints(text)
        excluded.update(str(value).casefold() for value in compiled.get("excluded_providers") or [])
        allowed = {str(value).casefold() for value in compiled.get("research_allowed_providers") or []}
        if allowed:
            allowlists.append(allowed)
        for key in boolean_restrictions:
            boolean_restrictions[key] = bool(boolean_restrictions[key] or compiled.get(key))

    result = {key: True for key, value in boolean_restrictions.items() if value}
    if excluded:
        result["excluded_providers"] = sorted(excluded)
    if allowlists:
        allowed = set.intersection(*allowlists) - excluded
        if not allowed:
            raise ValueError(
                "FOUNDER_PROVIDER_CONSTRAINT_CONFLICT: Project/Mission Research provider constraints leave no permitted provider."
            )
        result["research_allowed_providers"] = sorted(allowed)
    return result


def _persist_new_project_execution_constraints(project_spec: dict, execution_constraints: dict) -> dict:
    """Embed machine-enforced Founder execution authority in a new Project Contract.

    A first Mission can carry restrictions that the CEO omitted from its
    generated Project prose. If the Project is being created now, those
    restrictions must survive into later CEO-delegated Missions. Existing
    Projects are never amended here; changing their Contract remains Founder
    authority.
    """
    result = dict(project_spec or {})
    rows = clean_rows(result.get("constraints") or [])
    policy = __import__(
        "eason_one.services.execution_policy", fromlist=["constraint_rows_from_execution_constraints"]
    )
    for row in policy.constraint_rows_from_execution_constraints(execution_constraints):
        normalized = _normalized_plan_text(row)
        if not any(_normalized_plan_text(existing) == normalized for existing in rows):
            rows.append(row)
    if rows:
        result["constraints"] = rows
    return result


def _operation_authority_signature(*, founder_request, project_spec, existing_project, execution_constraints) -> dict:
    """Authority-sensitive duplicate signature for still-open Operations.

    A repeated click with the same authority may reuse an open proposal. A new
    budget cap or provider restriction may not be swallowed by title-based
    deduplication.
    """
    project_terms_hash = None
    if existing_project is not None:
        contract = __import__(
            "eason_one.services.project_contract", fromlist=["execution_terms_hash"]
        )
        try:
            project_terms_hash = contract.execution_terms_hash(existing_project)
        except ValueError:
            # Governed contract integrity errors are execution blockers, not a
            # reason to pretend two authority envelopes are equivalent.
            project_terms_hash = "INVALID_PROJECT_TERMS"
    cap = founder_declared_budget_cap(founder_request)
    return {
        "founder_budget_cap_twd": str(cap) if cap is not None else None,
        "execution_constraints": execution_constraints or {},
        "project_terms_hash": project_terms_hash,
        "project_spec_constraints": list((project_spec or {}).get("constraints") or []),
    }


def _stored_operation_authority_signature(operation) -> dict:
    memory = dict(getattr(operation, "memory_json", None) or {})
    stored = memory.get("authority_signature")
    if isinstance(stored, dict):
        return stored
    project_terms_hash = None
    if getattr(operation, "project_id", None):
        project = getattr(operation, "project", None) or db.session.get(Project, operation.project_id)
        if project is not None:
            try:
                project_terms_hash = __import__(
                    "eason_one.services.project_contract", fromlist=["execution_terms_hash"]
                ).execution_terms_hash(project)
            except ValueError:
                project_terms_hash = "INVALID_PROJECT_TERMS"
    return {
        "founder_budget_cap_twd": memory.get("founder_declared_budget_cap_twd"),
        "execution_constraints": dict(memory.get("execution_constraints") or {}),
        "project_terms_hash": project_terms_hash,
        "project_spec_constraints": list((memory.get("new_project_spec") or {}).get("constraints") or []),
    }


def propose_operation(ceo, payload, route_type=None, route_reason=None, founder_request=None, authority_source="FOUNDER_APPROVAL"):
    authority_source = str(authority_source or "FOUNDER_APPROVAL").upper()
    if authority_source not in {"FOUNDER_APPROVAL", "PROJECT_DELEGATED_CEO"}:
        raise ValueError("Unsupported Operation authority source")
    plan = validate_plan(payload)
    project_spec = dict(plan.get("project") or {})
    if "project_id" in plan:
        plan["operation"]["project_id"] = plan.get("project_id")
    plan["operation"]["meeting_policy"] = normalize_meeting_policy(
        plan["operation"]["meeting_policy"]
    )
    data = plan["operation"]
    fingerprint = plan_fingerprint(plan)
    normalized_title = _normalized_plan_text(data.get("title"))
    existing_project = db.session.get(Project, data.get("project_id")) if data.get("project_id") else None
    inherited_project_constraints = []
    if existing_project is not None:
        terms = __import__(
            "eason_one.services.project_contract", fromlist=["governing_terms"]
        ).governing_terms(existing_project)
        inherited_project_constraints = list(terms.get("constraints") or [])
    durable_execution_constraints = _merge_provider_execution_constraints(
        inherited_project_constraints,
        project_spec.get("constraints") or [],
        founder_request,
    )
    if existing_project is None:
        project_spec = _persist_new_project_execution_constraints(
            project_spec, durable_execution_constraints
        )
        if project_spec:
            # Once deterministic Founder constraints materialize a new Project
            # spec, the plan must use the complete canonical PROJECT_PLAN_FIELDS
            # shape.  A compact CEO OPERATION_PLAN intentionally omits top-level
            # project metadata before proposal construction; adding only
            # ``project`` here would leave a four-field hybrid that our own
            # validator later rejects during authority reconciliation.
            plan["project"] = project_spec
            plan["project_id"] = data.get("project_id")
    authority_signature = _operation_authority_signature(
        founder_request=founder_request, project_spec=project_spec,
        existing_project=existing_project, execution_constraints=durable_execution_constraints,
    )
    operation_kind=__import__("eason_one.services.stabilization",fromlist=["operation_kind"]).operation_kind
    existing = next((
        row for row in Operation.query.filter(
            Operation.status.in_(["PLANNED", "RUNNING", "WAITING_FOR_FOUNDER", "PAUSED"])
        ).order_by(Operation.id.desc()).all()
        if operation_kind(row)=="REAL_WORK"
        and (row.memory_json or {}).get("authority_source", "FOUNDER_APPROVAL") == authority_source
        and _stored_operation_authority_signature(row) == authority_signature
        and (
            plan_fingerprint(row.plan_json) == fingerprint
            or (
                _normalized_plan_text(row.title) == normalized_title
                and (row.project_id or 0) == (data.get("project_id") or 0)
            )
        )
    ), None)
    if existing:
        memory = dict(existing.memory_json or {})
        duplicates = list(memory.get("duplicate_founder_requests") or [])
        duplicates.append({"status": existing.status, "fingerprint": fingerprint})
        memory["duplicate_founder_requests"] = duplicates[-12:]
        existing.memory_json = memory
        db.session.commit()
        return existing

    model_proposed_budget = Decimal(str(data["budget_twd"]))
    multi_agent_enabled = bool(len(data["tasks"]) > 1 and not all(_codex_task(item) for item in data["tasks"]))
    estimate, breakdown, meeting_estimate = execution_budget_estimate(
        plan,
        include_orchestration=multi_agent_enabled,
        execution_constraints_override=durable_execution_constraints,
    )
    # The CEO may estimate work, but it may not invent an arbitrary Mission
    # authority ceiling. The initial Founder envelope is derived
    # deterministically from the priced workflow plus a bounded contingency.
    contingency = max(BUDGET_QUANTUM, (estimate * Decimal("0.15")).quantize(BUDGET_QUANTUM, rounding=ROUND_CEILING))
    derived_budget = max(BUDGET_QUANTUM, estimate + contingency)
    founder_cap = founder_declared_budget_cap(founder_request)
    if founder_cap is not None and founder_cap < estimate:
        raise ValueError(
            f"The priced workflow estimate (NT${estimate}) exceeds the Founder-declared hard cap (NT${founder_cap}). "
            "The CEO must reduce scope or request a larger boundary before creating a Mission."
        )
    normalized_budget = founder_cap if founder_cap is not None else derived_budget
    project_remaining = project_remaining_authority(existing_project)
    if project_remaining is not None:
        if estimate > project_remaining:
            raise ValueError(
                f"The next Mission estimate (NT${estimate}) exceeds remaining Project authority "
                f"(NT${project_remaining}). Founder authorization is required before more paid work."
            )
        normalized_budget = min(normalized_budget, project_remaining)
    data["budget_twd"] = str(normalized_budget)
    config = data.get("meeting_config") or {}
    if config.get("trigger") != "NEVER":
        reserved_meeting_estimate = (
            meeting_estimate * Decimal("1.20")
        ).quantize(BUDGET_QUANTUM, rounding=ROUND_CEILING)
        config["budget_twd"] = str(max(
            Decimal(str(config.get("budget_twd") or 0)),
            reserved_meeting_estimate,
        ))
        normalized_budget = max(normalized_budget, Decimal(config["budget_twd"]))
        data["budget_twd"] = str(normalized_budget)
    route_type=(route_type or (
        "SHORT_MEETING" if config.get("trigger") != "NEVER"
        else "FULL_PROJECT" if len(data["tasks"]) > 1
        else "SINGLE_WORKER"
    )).upper()
    planned_reviews=sum(1 for item in data["tasks"] if item.get("reviewer_employee_id") not in (None,item.get("assignee_employee_id")))
    base_delivery_calls=0 if all(_codex_task(item) for item in data["tasks"]) else 2
    if config.get("trigger") != "NEVER":
        meeting_rounds = min(2, int(config.get("max_rounds") or 1))
        meeting_speakers = min(3, int(config.get("max_speakers_per_round") or 1), len(config.get("participant_employee_ids") or []) or 1)
        meeting_calls = meeting_rounds * (1 + meeting_speakers) + 1
    else:
        meeting_calls = 0
    orchestration_calls = 1 if multi_agent_enabled else 0
    # vNext needs bounded recovery headroom.  The old happy-path-only ceiling
    # made the first legitimate retry or CEO replan look like a Founder issue.
    # Keep a deterministic ceiling, but include one retry per delivery Work and
    # three bounded management/recovery calls before verification/reporting.
    max_calls=max(
        6,
        len(data["tasks"]) * 2
        + planned_reviews
        + base_delivery_calls
        + meeting_calls
        + orchestration_calls
        + 3,
    )
    completion_reserve = sum(
        (Decimal(str(row.get("estimated_twd") or 0)) for row in breakdown
         if row.get("kind") in {"PROJECT_OUTCOME_REVIEW", "DETERMINISTIC_ENGINEERING_VERIFICATION"}),
        Decimal("0"),
    )
    report_reserve = sum(
        (Decimal(str(row.get("estimated_twd") or 0)) for row in breakdown if row.get("kind") == "CEO_OPERATION_REPORT"),
        Decimal("0"),
    )
    stage_budget_caps = {}
    for row in breakdown:
        kind = row.get("kind")
        if kind == "MEETING_ENVELOPE":
            kind = "MEETING"
        if kind == "ENGINEER_CODEX_TOOL":
            kind = "TASK_EXECUTION"
        stage_budget_caps[kind] = str(
            (Decimal(str(stage_budget_caps.get(kind) or 0)) + Decimal(str(row.get("estimated_twd") or 0)) * Decimal("1.20"))
            .quantize(BUDGET_QUANTUM, rounding=ROUND_CEILING)
        )
    stage_budget_caps["CONTINGENCY"] = str(contingency)
    # Founder provider exclusions were normalized before pricing so proposal
    # cost and runtime dispatch obey the same durable provider authority.
    largest_planned_call = max(
        (Decimal(str(row.get("estimated_twd") or 0)) * Decimal("1.20") for row in breakdown),
        default=BUDGET_QUANTUM,
    ).quantize(BUDGET_QUANTUM, rounding=ROUND_CEILING)
    operation = Operation(
        title=data["title"], objective=data["objective"],
        project_id=data["project_id"], proposed_by_employee_id=ceo.id,
        status="PLANNED", kernel_status="CREATED",
        route_type=route_type, route_reason=route_reason, current_stage="CREATED",
        plan_json=plan, approved_budget_twd=normalized_budget,
        estimated_cost_twd=estimate, hard_cost_cap_twd=normalized_budget,
        stage_cost_cap_twd=normalized_budget,
        # Compatibility field only for new vNext Work Operations.  The actual
        # per-call authority is checked against Work + Project truth at durable
        # reservation time, not against the proposal's guessed largest call.
        single_call_cost_cap_twd=normalized_budget,
        max_calls=max_calls, max_revisions=min(2, planned_reviews),
        max_messages=max(8, max_calls*2), max_elapsed_seconds=3600,
        memory_json={
            "mission_kind": "REAL_WORK",
            "multi_agent_policy_version": "ma-work-v1" if multi_agent_enabled else None,
            "multi_agent_enabled": multi_agent_enabled,
            "meeting_results": [], "decisions": [], "hiring": [],
            "plan_fingerprint": fingerprint,
            "model_proposed_budget_twd": str(model_proposed_budget),
            "founder_declared_budget_cap_twd": str(founder_cap) if founder_cap is not None else None,
            "execution_budget_estimate_twd": str(estimate),
            "execution_budget_breakdown": breakdown,
            "completion_reserve_twd": str(completion_reserve),
            "report_reserve_twd": str(report_reserve),
            "stage_budget_caps": stage_budget_caps,
            "ceo_policy_version": "ceo-policy-v1.0",
            "runtime_semantics": "WORK_CORE_V018",
            "core_cutover_version": "0.20.0",
            "core_rebuild_version": "0.20.0",
            "budget_authority_source": (
                "FOUNDER_DECLARED_HARD_CAP_WITH_DETERMINISTIC_STAGE_LIMITS"
                if founder_cap is not None else
                "DETERMINISTIC_ESTIMATE_PLUS_15_PERCENT_CONTINGENCY"
            ),
            "authority_source": authority_source,
            "execution_constraints": durable_execution_constraints,
            "authority_signature": authority_signature,
            "new_project_spec": project_spec or None,
            "founder_attention_events": ([{
                "kind": "OPERATION_APPROVAL",
                "status": "PENDING",
                "reason": "Approve, modify, or reject the CEO execution proposal.",
                "choices": ["APPROVE", "MODIFY", "REJECT"],
            }] if authority_source == "FOUNDER_APPROVAL" else [{
                "kind": "PROJECT_DELEGATED_SEQUENCING",
                "status": "DELEGATED",
                "reason": "CEO sequenced this bounded Mission inside the already Founder-approved Project Contract.",
                "choices": [],
            }]),
        },
    )
    db.session.add(operation)
    db.session.flush()
    kernel=__import__("eason_one.services.operation_kernel",fromlist=["set_route","transition"])
    kernel.set_route(
        operation, route_type, route_reason or "Smallest sufficient route derived from the validated plan.",
        commit=False,
    )
    kernel.transition(
        operation,"WAITING_APPROVAL",
        "FOUNDER_APPROVAL_REQUESTED" if authority_source == "FOUNDER_APPROVAL" else "PROJECT_DELEGATED_MISSION_PROPOSED",
        stage="APPROVAL",actor_type="CEO",commit=False,
        payload={"estimated_cost_twd":str(estimate),"hard_cap_twd":str(normalized_budget),"authority_source":authority_source},
    )
    db.session.commit()
    return operation


def _plan_requires_codex(data) -> bool:
    """Return whether this bounded Mission requires the Engineer's Codex tool.

    Capability is the governed tool-routing contract. Assignee identity is only
    a proposal-time placeholder because Team Formation may legally rematch the
    accountable specialist before execution.
    """
    for item in (data or {}).get("tasks") or []:
        capabilities = [str(value or "").upper() for value in (item.get("required_capabilities") or [])]
        if "SOFTWARE_ENGINEERING" in capabilities:
            return True
    return False


def _preapproval_tool_readiness_snapshot(operation, data) -> dict | None:
    """Return current local engineering readiness without mutating approval truth."""
    if not _plan_requires_codex(data):
        return None
    connector = __import__(
        "eason_one.services.codex_connector",
        fromlist=["codex_runtime_descriptor", "preapproval_repository_readiness"],
    )
    descriptor = connector.codex_runtime_descriptor(force_probe=True)
    if not descriptor.get("ready"):
        detail = str(descriptor.get("error") or "Codex CLI readiness probe failed").strip()
        raise ValueError(
            "ENGINEERING_TOOL_UNAVAILABLE: this proposal requires SOFTWARE_ENGINEERING, "
            f"but the local Codex execution core is not ready. {detail}"
        )
    memory = dict(operation.memory_json or {})
    if operation.project_id:
        project = db.session.get(Project, operation.project_id)
        contracts = __import__(
            "eason_one.services.project_contract", fromlist=["is_vnext_governed", "governing_terms"]
        )
        if project and contracts.is_vnext_governed(project):
            constraints = list(contracts.governing_terms(project).get("constraints") or [])
        else:
            constraints = str(getattr(project, "known_constraints", "") or "").splitlines()
    else:
        constraints = list((memory.get("new_project_spec") or {}).get("constraints") or [])
    repository = connector.preapproval_repository_readiness(constraints=constraints)
    if not repository.get("ready"):
        raise ValueError(
            "ENGINEERING_REPOSITORY_UNAVAILABLE: this proposal requires SOFTWARE_ENGINEERING, "
            + str(repository.get("error") or "the governed repository boundary is not ready")
        )
    return {
        "ready": True,
        "transport": descriptor.get("transport"),
        "version": descriptor.get("version"),
        "login": descriptor.get("login"),
        "checked_at": descriptor.get("checked_at"),
        "source": descriptor.get("source"),
        "repository": repository,
    }


def _assert_preapproval_tool_readiness(operation, data) -> None:
    """Probe execution-critical local tools before Founder authority is frozen.

    This is deliberately a non-job readiness probe: it may warm WSL and run
    `codex --version` / `codex login status`, but it never starts a Codex model
    turn or touches a repository. A Project that promises software delivery must
    not be created first and discover a broken local Codex installation later.
    """
    snapshot = _preapproval_tool_readiness_snapshot(operation, data)
    if snapshot is None:
        return
    memory = dict(operation.memory_json or {})
    memory["approval_tool_readiness"] = {"codex": snapshot}
    operation.memory_json = memory



def delegated_approval_readiness(operation) -> dict:
    """Pure structured readiness probe for a pending CEO-delegated Mission.

    The probe inspects current Project authority, configured execution models,
    staffing/research readiness, and local Codex/repository readiness. It never
    approves, dispatches, materializes Work, or commits proposal normalization.
    """
    def result(ready: bool, kind: str, reason: str | None = None, **extra):
        return {"ready": bool(ready), "kind": str(kind), "reason": reason, **extra}

    kernel = __import__("eason_one.services.operation_kernel", fromlist=["authoritative_status"])
    if operation is None or operation.approved_at is not None or kernel.authoritative_status(operation) != "WAITING_APPROVAL":
        return result(False, "RECONCILIATION", "Delegated Operation is no longer awaiting approval.")
    memory = dict(operation.memory_json or {})
    if memory.get("authority_source") != "PROJECT_DELEGATED_CEO" or not operation.project_id:
        return result(False, "RECONCILIATION", "Operation is not a CEO-delegated continuation inside an existing Project.")
    project = db.session.get(Project, operation.project_id)
    if project is None:
        return result(False, "RECONCILIATION", "Delegated Operation references a missing Project.")
    company_kernel = __import__(
        "eason_one.services.company_kernel",
        fromlist=["_is_delegated_continuation", "_delegated_continuation_currentness"],
    )
    if company_kernel._is_delegated_continuation(operation):
        currentness = company_kernel._delegated_continuation_currentness(operation)
        if not currentness.get("current"):
            return result(
                False, str(currentness.get("kind") or "RECONCILIATION"),
                str(currentness.get("reason") or "Delegated continuation currentness is unproven."),
                **{key: value for key, value in currentness.items() if key not in {"current", "kind", "reason"}},
            )
    try:
        remaining = project_remaining_authority(project)
        frozen_envelope = Decimal(operation.approved_budget_twd or 0)
        if remaining is not None and frozen_envelope > Decimal(remaining):
            shortfall = frozen_envelope - Decimal(remaining)
            return result(
                False, "BUDGET_AUTHORIZATION",
                f"Mission authority NT${frozen_envelope} exceeds remaining Project authority NT${remaining}.",
                additional_budget_twd=str(shortfall),
            )
        __import__(
            "eason_one.services.project_contract", fromlist=["assert_internal_authority"]
        ).assert_internal_authority(project, requested_budget_twd=operation.approved_budget_twd)
        plan = validate_plan(operation.plan_json)
        estimate, _breakdown, _meeting = execution_budget_estimate(
            plan,
            include_orchestration=bool(memory.get("multi_agent_enabled")),
            execution_constraints_override=dict(memory.get("execution_constraints") or {}),
        )
        if remaining is not None and Decimal(estimate) > Decimal(remaining):
            shortfall = Decimal(estimate) - Decimal(remaining)
            return result(
                False, "BUDGET_AUTHORIZATION",
                f"The next bounded move estimate (NT${estimate}) exceeds remaining Project authority (NT${remaining}).",
                additional_budget_twd=str(shortfall),
            )
        _preapproval_tool_readiness_snapshot(operation, plan["operation"])
    except ValueError as exc:
        reason = str(exc)
        system_prefixes = (
            "ENGINEERING_TOOL_UNAVAILABLE:",
            "ENGINEERING_REPOSITORY_UNAVAILABLE:",
            "RESEARCH_TOOL_UNAVAILABLE:",
            "STAFFING_RUNTIME_UNAVAILABLE:",
            "MODEL_POLICY_BLOCKED:",
        )
        return result(
            False,
            "SYSTEM_RECOVERY" if reason.startswith(system_prefixes) else "RECONCILIATION",
            reason,
        )
    return result(True, "READY", None, estimated_twd=str(estimate))



def approve(operation, *, authority_source="FOUNDER"):
    authority_source = str(authority_source or "FOUNDER").upper()
    delegated = authority_source == "PROJECT_DELEGATED_CEO"
    kernel=__import__("eason_one.services.operation_kernel",fromlist=["authoritative_status"])
    state=kernel.authoritative_status(operation)
    if operation.approved_at is not None and state in {"QUEUED", "RUNNING", "VERIFYING"}:
        return operation
    if state != "WAITING_APPROVAL" or operation.approved_at is not None:
        raise ValueError("Only an unapproved Operation awaiting Founder authority may be approved")
    # Recompile the pending authority from deterministic pricing. For Founder
    # approval this freezes new authority; for delegated sequencing it verifies
    # that the Mission still fits inside the existing immutable Project Contract.
    if delegated:
        if not operation.project_id:
            raise ValueError("PROJECT_DELEGATED_CEO requires an existing Project")
        project_for_authority = db.session.get(Project, operation.project_id)
        __import__(
            "eason_one.services.project_contract", fromlist=["assert_internal_authority"]
        ).assert_internal_authority(project_for_authority, requested_budget_twd=operation.approved_budget_twd)
        memory = dict(operation.memory_json or {})
        if memory.get("authority_source") != "PROJECT_DELEGATED_CEO":
            raise ValueError("Operation was not proposed under delegated Project authority")
        company_kernel = __import__(
            "eason_one.services.company_kernel",
            fromlist=["_is_delegated_continuation", "_delegated_continuation_currentness"],
        )
        if company_kernel._is_delegated_continuation(operation):
            currentness = company_kernel._delegated_continuation_currentness(operation)
            if not currentness.get("current"):
                kind = str(currentness.get("kind") or "RECONCILIATION")
                reason = str(currentness.get("reason") or "Delegated continuation currentness is unproven.")
                raise ValueError(f"{kind}: {reason}")
    authority = reconcile_pending_proposal_authority(operation)
    if authority.get("issues"):
        raise ValueError(
            "Proposal authority is internally inconsistent and cannot be approved: "
            + ", ".join(authority["issues"])
        )
    plan = validate_plan(operation.plan_json)
    data = plan["operation"]
    estimate, breakdown, _ = execution_budget_estimate(
        plan, include_orchestration=bool((operation.memory_json or {}).get("multi_agent_enabled")),
        execution_constraints_override=dict((operation.memory_json or {}).get("execution_constraints") or {}),
    )
    if Decimal(operation.approved_budget_twd) < estimate:
        memory = dict(operation.memory_json or {})
        memory["execution_budget_estimate_twd"] = str(estimate)
        memory["execution_budget_breakdown"] = breakdown
        operation.memory_json = memory
        db.session.commit()
        raise ValueError(
            f"Execution authority is below the system estimate. Review and authorize at least NT$ {_budget_text(estimate)} before starting."
        )
    if operation.project_id:
        project = db.session.get(Project, operation.project_id)
        remaining = project_remaining_authority(project)
        if remaining is not None and Decimal(operation.approved_budget_twd) > remaining:
            raise ValueError(
                f"Mission authority NT${operation.approved_budget_twd} exceeds remaining Project authority NT${remaining}."
            )
    # Tool readiness is checked after deterministic budget/Project authority
    # validation but before Project creation, Work materialization, or approval
    # timestamps. A failed local tool probe therefore leaves no half-approved
    # Project behind.
    _assert_preapproval_tool_readiness(operation, data)
    if not operation.project_id:
        memory = dict(operation.memory_json or {})
        spec = dict(memory.get("new_project_spec") or {})
        project_limit = Decimal(str(
            memory.get("project_authorized_budget_twd")
            or operation.approved_budget_twd
        ))
        success_criteria = clean_rows(spec.get("success_criteria"))[:8]
        # Never truncate Founder authority rows when freezing the immutable
        # Project Contract. UI surfaces may summarize them separately.
        constraints = clean_rows(spec.get("constraints"))
        project = Project(
            name=clean_text(spec.get("name") or data["title"], multiline=False)[:160],
            objective=clean_text(spec.get("objective") or data["objective"], multiline=False),
            status="ACTIVE",
            priority=spec.get("priority") if spec.get("priority") in {"LOW","MEDIUM","HIGH","CRITICAL"} else "HIGH",
            environment="LIVE", origin="CEO_PROJECT",
            owner_employee_id=operation.proposed_by_employee_id,
            real_budget_limit=project_limit,
            deadline=_parse_project_deadline(spec.get("deadline")),
            known_constraints="\n".join(constraints) if constraints else None,
            current_state_summary="Founder approved the Project. The CEO is starting the first bounded Mission.",
            next_milestone=(data["completion_criteria"][0] if data["completion_criteria"] else None),
        )
        db.session.add(project)
        db.session.flush()
        operation.project_id = project.id
        if success_criteria:
            db.session.add(KnowledgeItem(
                project_id=project.id, kind="DECISION", title="Project success criteria",
                content="\n".join(f"- {value}" for value in success_criteria),
                rationale="Founder-approved Project contract captured before execution.",
                source_ref=f"project:{project.id}:founder_contract",
                origin_employee_id=operation.proposed_by_employee_id,
                founder_approved=True,
            ))
        __import__(
            "eason_one.services.project_contract", fromlist=["freeze"]
        ).freeze(
            project, success_criteria=success_criteria, constraints=constraints,
            origin_employee_id=operation.proposed_by_employee_id,
        )
    planning_runs = AgentRun.query.filter_by(
        operation_id=operation.id, purpose="CEO_FOUNDER_REQUEST"
    ).all()
    for planning_run in planning_runs:
        planning_run.project_id = project.id
        CostEvent.query.filter_by(agent_run_id=planning_run.id).update(
            {"project_id": project.id}, synchronize_session=False
        )
    if not operation.tasks:
        __import__(
            "eason_one.services.work_runtime", fromlist=["materialize_operation_works"]
        ).materialize_operation_works(operation, data)
        db.session.flush()
        db.session.expire(operation, ["tasks", "works"])
    operation.approved_at = operation.approved_at or now()
    operation.hard_cost_cap_twd = Decimal(operation.approved_budget_twd)
    memory = dict(operation.memory_json or {})
    memory["authority_source"] = "PROJECT_DELEGATED_CEO" if delegated else "FOUNDER_APPROVAL"
    if delegated:
        memory["founder_attention_events"] = [{
            "kind": "PROJECT_DELEGATED_SEQUENCING",
            "status": "DELEGATED",
            "reason": "Bounded Mission authorized by the existing Founder Project Contract.",
            "choices": [],
        }]
    operation.memory_json = memory
    project.current_state_summary = (
        "CEO sequenced the next bounded Mission inside the Founder Project Contract."
        if delegated else "Approved. Company Runtime owns the next step automatically."
    )

    # A Meeting is a visible, persistent escalation object, never an invisible
    # post-Task side effect. It is materialized at approval only when the route
    # contract explicitly permits it.
    config=data.get("meeting_config") or {}
    if (operation.route_type=="SHORT_MEETING" or config.get("trigger") != "NEVER") and not Meeting.query.filter_by(operation_id=operation.id).first():
        participant_ids=list(dict.fromkeys(config.get("participant_employee_ids") or []))[:4]
        participants=[db.session.get(Employee,employee_id) for employee_id in participant_ids]
        participants=[employee for employee in participants if employee and employee.active]
        ceo_chair=db.session.get(Employee,operation.proposed_by_employee_id)
        if ceo_chair and not participants:
            participants=[task.assigned_employee for task in operation.tasks if task.assigned_employee][:2]
        trigger=str(config.get("trigger") or "NEVER").upper()
        # Material specialist disagreement is not automatically a CEO meeting.
        # Prefer the persistent Critic / an approved independent reviewer so the
        # company can coordinate horizontally; CEO remains the final-report and
        # fallback chair.
        chair=ceo_chair
        if trigger == "ON_MATERIAL_CONFLICT":
            critic=Employee.query.filter_by(slug="critic",active=True).first()
            reviewer_ids=[task.reviewer_employee_id for task in operation.tasks if task.reviewer_employee_id]
            if critic and (critic.id in participant_ids or critic.id in reviewer_ids):
                chair=critic
            else:
                chair=next((task.reviewer for task in operation.tasks if task.reviewer and task.reviewer.active),ceo_chair)
        if chair and chair.id not in {employee.id for employee in participants}:
            participants=[chair,*participants]
        participants=list({employee.id:employee for employee in participants if employee and employee.active}.values())[:4]
        meeting=meeting_service.create(
            title=f"{operation.title} — bounded decision meeting",
            purpose="Resolve the explicit cross-role decision in the approved Mission.",
            agenda=operation.objective,chair=chair,participants=participants,project=project,
            max_rounds=min(2,int(config.get("max_rounds") or 1)),
            token_limit=int(config.get("token_limit") or 6000),
            real_cost_limit_twd=Decimal(str(config.get("budget_twd") or 0)),
            profile="ECONOMY",max_speakers_per_round=min(3,int(config.get("max_speakers_per_round") or 2)),
            contribution_output_cap=int(config.get("contribution_output_cap") or 512),
        )
        meeting.operation_id=operation.id
        db.session.flush()
        __import__("eason_one.services.company_events",fromlist=["emit"]).emit(
            "MEETING_PLANNED", actor_type="CEO", actor_id=operation.proposed_by_employee_id,
            project_id=project.id, work_id=None, correlation_id=f"project:{project.id}",
            payload={"operation_id": operation.id, "meeting_id":meeting.id,"trigger":config.get("trigger")},
        )
    # Company Core v0.18 cutover: approved WORK_CORE_V018 Missions are adopted by
    # the Work-first Company Runtime. Operation remains a proposal/audit
    # envelope and compatibility projection; its kernel queue no longer owns
    # scheduling, retry or completion for new company work.
    __import__(
        "eason_one.services.company_runtime", fromlist=["adopt_operation"]
    ).adopt_operation(operation, commit=False)
    db.session.commit()
    __import__(
        "eason_one.services.company_runtime", fromlist=["wake_company_runtime"]
    ).wake_company_runtime()
    return operation


def actual_cost(operation):
    # Founder planning is a Company cost, but it is outside the Mission's
    # execution envelope. Only execution/review/meeting/report costs reduce the
    # authorized Mission remainder.
    value = (db.session.query(func.coalesce(func.sum(CostEvent.real_cost_delta), 0))
        .outerjoin(AgentRun, CostEvent.agent_run_id == AgentRun.id)
        .filter(CostEvent.operation_id == operation.id)
        .filter(or_(
            CostEvent.agent_run_id.is_(None),
            AgentRun.purpose != "CEO_FOUNDER_REQUEST",
        )).scalar())
    operation.actual_cost_twd = Decimal(value)
    return Decimal(value)


def remaining_budget(operation):
    return Decimal(operation.approved_budget_twd) - actual_cost(operation)


def _delegate_project_authority(operation, required_available):
    """Delegate Project authority to one Operation without changing Project cap.

    Returns the additional Operation authority delegated, Decimal(0) when no
    delegation is needed, or None when the Project envelope cannot cover the
    bounded requirement. This is deterministic resource allocation, not new
    Founder authority.
    """
    required = max(Decimal("0"), Decimal(str(required_available or 0))).quantize(
        BUDGET_QUANTUM, rounding=ROUND_CEILING
    )
    kernel = __import__(
        "eason_one.services.operation_kernel", fromlist=["budget_snapshot", "append_event"]
    )
    snapshot = kernel.budget_snapshot(operation)
    available = Decimal(snapshot["available"])
    shortfall = max(Decimal("0"), required - available).quantize(
        BUDGET_QUANTUM, rounding=ROUND_CEILING
    )
    if shortfall <= 0:
        return Decimal("0")
    project = operation.project
    project_remaining = project_remaining_authority(project)
    # Project remaining authority is the total future spend still allowed. The
    # Operation's resulting available headroom may not exceed that envelope.
    if project_remaining is None or required > project_remaining:
        return None

    prior = Decimal(operation.approved_budget_twd)
    delegated_total = (prior + shortfall).quantize(BUDGET_QUANTUM, rounding=ROUND_CEILING)
    operation.approved_budget_twd = delegated_total
    operation.hard_cost_cap_twd = delegated_total
    operation.stage_cost_cap_twd = max(
        Decimal(str(operation.stage_cost_cap_twd or 0)), delegated_total
    )
    operation.single_call_cost_cap_twd = min(
        delegated_total,
        max(Decimal(str(operation.single_call_cost_cap_twd or 0)), required, BUDGET_QUANTUM),
    )
    if operation.works:
        # Work ceilings are delegated internal resources, not separate Founder
        # approvals.  Existing Batch-A rows inherited the original Operation
        # envelope, so extend those stale ceilings when Project authority is
        # deterministically delegated to the Operation.
        operation.single_call_cost_cap_twd = delegated_total
        operation.stage_cost_cap_twd = delegated_total
        for work in operation.works:
            if work.resource_ceiling_twd is None or Decimal(work.resource_ceiling_twd) <= prior:
                work.resource_ceiling_twd = delegated_total

    plan = dict(operation.plan_json or {})
    data = dict(plan.get("operation") or {})
    data["budget_twd"] = str(delegated_total)
    plan["operation"] = data
    operation.plan_json = plan

    memory = dict(operation.memory_json or {})
    delegations = list(memory.get("project_authority_delegations") or [])
    delegations.append({
        "source": "PROJECT_AUTHORITY",
        "prior_operation_authority_twd": str(prior),
        "delegated_twd": str(shortfall),
        "resulting_operation_authority_twd": str(delegated_total),
        "project_remaining_before_delegation_twd": str(project_remaining),
    })
    memory["project_authority_delegations"] = delegations[-20:]
    memory["budget_authority_source"] = "PROJECT_HARD_ENVELOPE_WITH_AUTONOMOUS_OPERATION_ALLOCATION"
    operation.memory_json = memory
    kernel.append_event(
        operation, "PROJECT_AUTHORITY_DELEGATED",
        from_status=kernel.authoritative_status(operation),
        to_status=kernel.authoritative_status(operation),
        stage=operation.current_stage, actor_type="CEO_RUNTIME",
        payload={
            "delegated_twd": str(shortfall),
            "resulting_operation_authority_twd": str(delegated_total),
            "project_id": getattr(project, "id", None),
        },
    )
    db.session.commit()
    return shortfall


def _completion_reserve_for_stage(operation, stage=None):
    memory = dict(operation.memory_json or {})
    stage = str(stage or "").upper()
    if stage in {"CEO_OPERATION_REPORT", "CEO_PROJECT_CLOSURE"}:
        return Decimal("0")
    if stage == "GOAL_VERIFICATION":
        return Decimal(str(memory.get("report_reserve_twd") or 0))
    return Decimal(str(memory.get("completion_reserve_twd") or 0))


def ensure_budget(operation, estimate=Decimal("0"), company_checked=False, *, stage=None, work=None):
    estimate = Decimal(estimate)
    kernel=__import__("eason_one.services.operation_kernel",fromlist=[
        "authoritative_status","budget_snapshot"
    ])
    if kernel.authoritative_status(operation) not in {"QUEUED","RUNNING","VERIFYING"}:
        raise ValueError("Operation is not authorized for execution")
    if not company_checked and (
        company_remaining() <= 0 or estimate > company_remaining()
    ):
        raise ValueError("Company real budget is exhausted")
    snapshot=kernel.budget_snapshot(operation)
    remaining=Decimal(snapshot["available"])
    vnext = bool(work is not None or operation.works)
    required_available = estimate
    if vnext:
        # Preserve the deterministic closure reserve while allowing actual
        # vNext call estimates to differ from the proposal's static stage/call
        # estimate. Project authority is inherited before the reservation.
        required_available += _completion_reserve_for_stage(operation, stage)
    if remaining <= 0 or required_available > remaining:
        delegated = _delegate_project_authority(operation, required_available)
        if delegated is not None:
            snapshot = kernel.budget_snapshot(operation)
            remaining = Decimal(snapshot["available"])
    if remaining <= 0 or required_available > remaining:
        additional = max(Decimal("0"), required_available - max(remaining, Decimal("0")))
        raise OperationBudgetRequired(
            snapshot["hard_cap"], snapshot["actual"], additional
        )
    return True


def completion_guard(operation):
    """Return whether delivery truth is ready for Goal Verification/final report.

    Company Core vNext treats Work/Artifact/Escalation as authoritative when an
    Operation has been migrated.  Legacy Task status remains a fallback only for
    pre-vNext Operations with no Work spine.  The MANAGEMENT Work intentionally
    stays EXECUTING until the final report is persisted, so it is excluded from
    this delivery-readiness guard.
    """
    reasons = []
    works = [work for work in operation.works if work.work_type != "MANAGEMENT"]
    if works:
        unresolved = [
            work for work in works if work.state not in {"ACCEPTED", "CANCELLED"}
        ]
        if unresolved:
            summary = ", ".join(
                f"Work #{work.id} {work.state}" for work in unresolved[:6]
            )
            reasons.append("Required Work remains unfinished: " + summary)

        models = __import__(
            "eason_one.models", fromlist=["Artifact", "ArtifactVersion", "Escalation"]
        )
        accepted_without_artifact = []
        accepted_with_superseded_artifact = []
        for work in works:
            if work.state != "ACCEPTED" or not (work.expected_output or "").strip():
                continue
            # Completion authority follows the newest durable ArtifactVersion,
            # not merely any historical version that once reached ACCEPTED.  A
            # newer SUBMITTED/REJECTED version means the Work's current evidence
            # and its ACCEPTED state disagree; fail closed instead of letting an
            # older accepted version prove Project completion.
            latest_version = (
                models.ArtifactVersion.query
                .join(models.Artifact)
                .filter(models.Artifact.work_id == work.id)
                .order_by(models.ArtifactVersion.id.desc())
                .first()
            )
            if latest_version is None:
                accepted_without_artifact.append(work.id)
            elif latest_version.status != "ACCEPTED":
                accepted_with_superseded_artifact.append(work.id)
        if accepted_without_artifact:
            reasons.append(
                "Accepted Work is missing an accepted Artifact: "
                + ", ".join(f"#{item}" for item in accepted_without_artifact[:6])
            )
        if accepted_with_superseded_artifact:
            reasons.append(
                "Accepted Work has a newer non-accepted ArtifactVersion: "
                + ", ".join(f"#{item}" for item in accepted_with_superseded_artifact[:6])
            )
        work_runtime_service = __import__(
            "eason_one.services.work_runtime", fromlist=["open_gates"]
        )
        accepted_with_open_gates = [
            work.id for work in works
            if work.state == "ACCEPTED" and work_runtime_service.open_gates(work)
        ]
        if accepted_with_open_gates:
            reasons.append(
                "Accepted Work still has unresolved governance/recovery gate(s): "
                + ", ".join(f"#{item}" for item in accepted_with_open_gates[:6])
            )

        open_escalations = models.Escalation.query.filter_by(
            operation_id=operation.id, state="OPEN"
        ).count()
        if open_escalations:
            reasons.append(f"{open_escalations} Company escalation(s) remain unresolved")
    else:
        statuses = {task.status for task in operation.tasks}
        if "BLOCKED" in statuses:
            reasons.append("Blocked Task remains unresolved")
        unfinished = statuses - {"DONE", "CANCELLED"}
        if unfinished:
            reasons.append("Required Task or Review remains unfinished")
        if operation.status == "WAITING_FOR_FOUNDER":
            reasons.append("Founder decision remains unresolved")

    meetings = Meeting.query.filter_by(operation_id=operation.id).all()
    if any(meeting.status not in {
        "ENDED", "TERMINATED_BY_FOUNDER"
    } for meeting in meetings):
        reasons.append("Required Meeting remains unresolved")
    requests = HiringRequest.query.filter_by(operation_id=operation.id).all()
    if any(item.status in {
        "REQUESTED", "HR_REVIEW", "FOUNDER_REVIEW"
    } for item in requests):
        reasons.append("Hiring decision remains unresolved")
    criteria = operation.plan_json["operation"].get("completion_criteria") or []
    if not criteria:
        reasons.append("Completion criteria are missing")
    return not reasons, reasons

def _compact_value(value, string_limit=700, list_limit=12):
    if isinstance(value, str):
        return value if len(value) <= string_limit else value[:string_limit - 1] + "…"
    if isinstance(value, list):
        return [
            _compact_value(item, string_limit, list_limit)
            for item in value[-list_limit:]
        ]
    if isinstance(value, dict):
        return {
            key: _compact_value(item, string_limit, list_limit)
            for key, item in value.items()
        }
    return value


def goal_evidence_packet(operation):
    completion_criteria = list(
        operation.plan_json["operation"].get("completion_criteria") or []
    )
    if not 1 <= len(completion_criteria) <= 12:
        raise ValueError(
            "Goal Verification requires between 1 and 12 approved "
            "completion criteria"
        )
    models = __import__(
        "eason_one.models",
        fromlist=["Artifact", "ArtifactVersion", "WorkAssignment"],
    )
    delivery_works = [
        work for work in operation.works if work.work_type != "MANAGEMENT"
    ]
    accepted_works = []
    accepted_artifacts = []
    accepted_version_by_work = {}
    acceptance_contract_hash_by_work = {}
    for work in delivery_works:
        if work.state != "ACCEPTED":
            # Historical ACCEPTED ArtifactVersions remain audit history when a
            # Work has moved back into execution/review/reconciliation. They are
            # never current Goal Verification evidence.
            continue
        assignment = __import__(
            "eason_one.services.work_runtime", fromlist=["active_assignment"]
        ).active_assignment(work)
        accepted_works.append({
            "work_id": work.id,
            "title": work.title,
            "purpose": work.purpose,
            "state": work.state,
            "owner_employee_id": getattr(assignment, "employee_id", None),
            "acceptance_criteria": work.acceptance_criteria,
        })
        # Current authority is the newest ArtifactVersion of the Work. Selecting
        # the newest *accepted* row would silently resurrect an older accepted
        # version when a newer revision is SUBMITTED/REJECTED.
        version = (
            models.ArtifactVersion.query
            .join(models.Artifact)
            .filter(models.Artifact.work_id == work.id)
            .order_by(models.ArtifactVersion.id.desc())
            .first()
        )
        if version is None or version.status != "ACCEPTED":
            raise ValueError(
                f"Accepted Work #{work.id} does not have a current latest ACCEPTED ArtifactVersion"
            )
        current_contract = dict((work.runtime_control_json or {}).get("acceptance_contract") or {})
        contract_hash = str(current_contract.get("contract_hash") or "")
        acceptance_contract_hash_by_work[int(work.id)] = contract_hash
        accepted_version_by_work[int(work.id)] = version
        accepted_artifacts.append({
            "artifact_id": version.artifact_id,
            "artifact_version_id": version.id,
            "work_id": work.id,
            "title": version.artifact.title,
            "artifact_type": version.artifact.artifact_type,
            "producer_employee_id": version.producer_employee_id,
            "execution_id": version.execution_id,
            "content": version.content_text,
            "content_hash": version.content_hash,
            "acceptance_contract_hash": contract_hash,
        })

    # Goal Verification may consume semantic review evidence only when that paid
    # review was durably bound to the same currently-authoritative accepted
    # ArtifactVersion/content. Historical reviews remain audit history, not
    # completion authority for a superseded Artifact.
    reviews = AgentRun.query.filter_by(
        operation_id=operation.id, purpose="TASK_REVIEW", status="SUCCEEDED"
    ).order_by(AgentRun.id.desc()).all()
    latest_reviews = {}
    for run in reviews:
        work_id = int(getattr(run, "work_id", 0) or 0)
        version = accepted_version_by_work.get(work_id)
        if version is None:
            continue
        target = dict((run.context_composition_json or {}).get("review_target") or {})
        try:
            target_version_id = int(target.get("artifact_version_id") or 0)
        except (TypeError, ValueError):
            target_version_id = 0
        if target_version_id != int(version.id):
            continue
        target_hash = str(target.get("artifact_content_hash") or "")
        if target_hash != str(version.content_hash or ""):
            continue
        target_contract_hash = str(target.get("acceptance_contract_hash") or "")
        current_contract_hash = acceptance_contract_hash_by_work.get(work_id, "")
        if target_contract_hash != current_contract_hash:
            continue
        # Work is the vNext completion authority. Compatibility Task IDs remain
        # metadata only and must not decide which semantic review is current.
        if work_id not in latest_reviews:
            latest_reviews[work_id] = run

    memory = operation.memory_json or {}
    from .brain import current
    brain = current(operation.project_id)
    packet = {
        "founder_objective": operation.objective,
        "completion_criteria": completion_criteria,
        "operation": {
            "id": operation.id,
            "status": operation.status,
            "approved_budget_twd": str(operation.approved_budget_twd),
        },
        "accepted_works": accepted_works,
        "accepted_artifacts": accepted_artifacts,
        # Legacy Task evidence remains visible during migration, but no longer
        # carries primary completion authority when Work exists.
        "completed_tasks": [{
            "task_id": task.id,
            "work_id": task.work_id,
            "title": task.title,
            "objective": task.objective,
            "status": task.status,
            "result": task.result_summary,
            "acceptance_criteria": task.acceptance_criteria,
        } for task in operation.tasks if task.status in {"DONE", "CANCELLED"}],
        "latest_reviews": [{
            "work_id": work_id,
            "task_id": run.task_id,
            "decision": (run.parsed_output_json or {}).get("decision"),
            "summary": (run.parsed_output_json or {}).get("summary"),
            "issues": (run.parsed_output_json or {}).get("issues") or [],
            "required_changes": (
                (run.parsed_output_json or {}).get("required_changes") or []
            ),
        } for work_id, run in sorted(latest_reviews.items())],
        "meeting_results": (memory.get("meeting_results") or [])[-4:],
        "ceo_decisions": [
            decision for decision in (memory.get("decisions") or [])
            if decision.get("action") != "COMPLETE"
        ][-6:],
        "company_brain_evidence": [{
            "id": item.id, "kind": item.kind, "title": item.title,
            "content": item.content,
        } for item in brain[-12:]],
    }
    supporting = {
        key: value for key, value in packet.items()
        if key != "completion_criteria"
    }
    packet = _compact_value(supporting)
    packet["completion_criteria"] = completion_criteria
    serialized = json.dumps(
        packet, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    if len(serialized) > GOAL_EVIDENCE_BUDGET:
        packet = _compact_value(
            supporting, string_limit=250, list_limit=8
        )
        packet["completion_criteria"] = completion_criteria
        serialized = json.dumps(
            packet, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        )
    if len(serialized) > GOAL_EVIDENCE_BUDGET:
        packet = _compact_value(
            supporting, string_limit=80, list_limit=4
        )
        packet["completion_criteria"] = completion_criteria
        serialized = json.dumps(
            packet, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        )
    if len(serialized) > GOAL_EVIDENCE_BUDGET:
        raise ValueError("Goal verification evidence exceeds bounded context")
    digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    return packet, serialized, digest

def _validate_goal_verification(payload, criteria):
    required = {"overall_status", "criteria", "summary", "recommended_action"}
    if not isinstance(payload, dict) or set(payload) != required:
        raise ValueError("Invalid Goal Verification fields")
    if payload["overall_status"] not in GOAL_STATUSES:
        raise ValueError("Invalid Goal Verification status")
    if not isinstance(payload["summary"], str) or not payload["summary"].strip():
        raise ValueError("Goal Verification summary is required")
    if (
        not isinstance(payload["recommended_action"], str)
        or not payload["recommended_action"].strip()
    ):
        raise ValueError("Goal Verification recommendation is required")
    rows = payload["criteria"]
    if not isinstance(rows, list) or len(rows) != len(criteria):
        raise ValueError("Goal Verification must assess every criterion")
    seen = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != {
            "criterion", "status", "evidence", "reason",
        }:
            raise ValueError("Invalid Goal Verification criterion fields")
        if row["status"] not in GOAL_STATUSES:
            raise ValueError("Invalid criterion status")
        if (
            not isinstance(row["evidence"], list)
            or not all(isinstance(item, str) for item in row["evidence"])
            or not isinstance(row["reason"], str)
            or not row["reason"].strip()
        ):
            raise ValueError("Invalid Goal Verification criterion evidence")
        seen.append(row["criterion"])
    if seen != list(criteria):
        raise ValueError("Goal Verification criteria do not match approval")
    if payload["overall_status"] == "SATISFIED" and any(
        row["status"] != "SATISFIED" for row in rows
    ):
        raise ValueError("Satisfied verification contains an unsatisfied criterion")
    return payload


def _persist_goal_verification(operation, payload, digest):
    memory = dict(operation.memory_json or {})
    memory["goal_verification"] = {
        **payload, "evidence_digest": digest,
    }
    operation.memory_json = memory
    db.session.commit()


def _current_goal_verification(operation):
    _, _, digest = goal_evidence_packet(operation)
    step = OperationStep.query.filter_by(
        operation_id=operation.id,
        logical_key=f"goal_verification:{digest}",
        kind="GOAL_VERIFICATION", status="SUCCEEDED",
    ).first()
    return step, digest


def _logical_key(operation, kind, task=None, subject=None):
    if task:
        suffix = "review" if kind == "REVIEW" else "execute"
        attempts = OperationStep.query.filter_by(
            operation_id=operation.id, kind=kind, task_id=task.id
        ).count()
        return f"task:{task.id}:{suffix}:{attempts + 1}"
    if subject is not None:
        return f"{kind.lower()}:{subject}"
    count = OperationStep.query.filter_by(
        operation_id=operation.id, kind=kind
    ).count()
    return f"{kind.lower()}:{count + 1}"


def _terminal_project_status(operation):
    project = getattr(operation, "project", None) if operation is not None else None
    status = str(getattr(project, "status", "") or "").upper()
    return status if status in {"COMPLETED", "CANCELLED"} else None


def _assert_operation_project_executable(operation, *, action: str):
    status = _terminal_project_status(operation)
    if status:
        raise ValueError(f"PROJECT_TERMINAL_OPERATION_{action}_FORBIDDEN:{status}")


def _claim(operation, request_key, logical_key, kind, task=None):
    existing = OperationStep.query.filter_by(
        operation_id=operation.id, idempotency_key=request_key
    ).first()
    if not existing:
        existing = OperationStep.query.filter_by(
            operation_id=operation.id, logical_key=logical_key
        ).first()
    if existing:
        return existing, False
    step = OperationStep(
        operation_id=operation.id, idempotency_key=request_key,
        logical_key=logical_key, kind=kind,
        task_id=getattr(task, "id", None), status="CLAIMED",
    )
    db.session.add(step)
    db.session.commit()
    return step, True


def _claimed_step_is_safe_to_resume(step) -> bool:
    """True only inside the local crash window before any provider boundary.

    _claim() commits CLAIMED before _provider_boundary() records
    EXECUTION_STARTED.  A process death in that narrow window must not strand a
    final verification/report forever, and resuming it cannot duplicate an
    external effect because no AgentRun/provider boundary exists yet.
    """
    return bool(
        step is not None
        and step.status == "CLAIMED"
        and step.agent_run_id is None
        and step.provider_started_at is None
    )


def _provider_boundary(step):
    # Legacy compatibility only.  Company Core vNext provider truth lives in
    # ExternalEffectAttempt beside AgentRun/Execution, immediately around the
    # actual provider invocation.  OperationStep must never claim a provider
    # call merely because orchestration reached this local step.
    step.status = "EXECUTION_STARTED"
    step.provider_started_at = None
    db.session.commit()


def _finish(step, run, result):
    step.agent_run_id = getattr(run, "id", None)
    step.status = "SUCCEEDED"
    step.result_json = result
    step.finished_at = now()
    db.session.commit()
    return result


def _existing_result(step):
    if step.result_json is not None:
        return step.result_json
    return {"kind": step.kind, "status": step.status}


def _parallel_wave_active(operation):
    current = dict(operation.memory_json or {}).get("multi_agent_current_wave") or {}
    return current.get("status") == "RUNNING"


def _defer_parallel_attention(operation, *, reason, decision_kind="FOUNDER_AUTHORITY", additional_budget=None):
    """Record one branch blocker without freezing independent sibling work.

    A parallel wave is one governed execution unit.  A single branch may fail
    before another independent branch has even reached its provider boundary.
    Turning the whole Operation into WAITING_APPROVAL from that worker creates
    a race where healthy siblings are rejected as unauthorized.  During an
    active wave we therefore persist the blocker and let the wave coordinator
    escalate once all already-dispatched siblings have settled.
    """
    if not _parallel_wave_active(operation):
        wait_for_founder(
            operation, reason, additional_budget=additional_budget,
            decision_kind=decision_kind,
        )
        return False
    memory = dict(operation.memory_json or {})
    rows = list(memory.get("multi_agent_deferred_attention") or [])
    rows.append({
        "reason": reason,
        "decision_kind": decision_kind,
        "additional_budget_twd": (str(additional_budget) if additional_budget is not None else None),
    })
    memory["multi_agent_deferred_attention"] = rows[-12:]
    operation.memory_json = memory
    db.session.commit()
    return True


def _mark_paid_or_ambiguous(operation, step, run, error):
    if isinstance(error, OperationBudgetRequired):
        return _mark_budget_required(operation, step, error)
    step.agent_run_id = getattr(run, "id", None)
    work=None
    if step.task_id:
        task=db.session.get(Task,step.task_id)
        work=__import__("eason_one.services.work_runtime",fromlist=["work_for_task"]).work_for_task(task)
    if work is None and run is not None and getattr(run, "work_id", None):
        work = db.session.get(
            __import__("eason_one.models", fromlist=["Work"]).Work,
            run.work_id,
        )
    if run and run.outcome == "FAILED_SAFE":
        step.status="FAILED_SAFE"
        step.error_text=str(error)
        step.finished_at=now()
        if work:
            pause_for_internal_runtime_recovery(
                operation, run.error_text or str(error), work=work,
                condition_type="INTERNAL_RECOVERY",
                issue_code=f"OPERATION_STEP_{int(step.id)}_RUN_{int(run.id)}_INTERNAL_RECOVERY",
            )
        db.session.commit()
        return None
    if run and run.outcome == "FAILED_KNOWN":
        step.status="FAILED_KNOWN"
        step.error_text=str(error)
        step.finished_at=now()
        if work:
            pause_for_internal_runtime_recovery(
                operation, run.error_text or str(error), work=work,
                condition_type="INTERNAL_RECOVERY",
                issue_code=f"OPERATION_STEP_{int(step.id)}_RUN_{int(run.id)}_INTERNAL_RECOVERY",
            )
        db.session.commit()
        return None
    if run and run.outcome == "FAILED_AMBIGUOUS":
        step.status="AMBIGUOUS"
        step.error_text=str(error)
        step.finished_at=now()
        if work:
            pause_for_internal_runtime_recovery(
                operation, run.error_text or str(error), work=work,
                condition_type="RECONCILIATION",
                issue_code=f"OPERATION_STEP_{int(step.id)}_RUN_{int(run.id)}_EFFECT_RECONCILIATION",
            )
        db.session.commit()
        return None
    paid = bool(run and run.real_cost and Decimal(run.real_cost) > 0)
    authority_blocked = bool(run and run.failure_reason == "AUTHORITY_BLOCKED")
    definite_pre_provider = run is None or authority_blocked
    if is_vnext_operation(operation):
        # vNext never turns a routine internal failure into Founder work.
        # Budget-extension requests remain explicit through OperationBudgetRequired;
        # everything else is scoped to the affected Work/management Work.
        step.status = "PAID_FAILED" if paid else ("BLOCKED" if definite_pre_provider else "AMBIGUOUS")
        step.error_text = str(error)
        step.finished_at = now()
        condition = "RECONCILIATION" if step.status == "AMBIGUOUS" else "INTERNAL_RECOVERY"
        pause_for_internal_runtime_recovery(
            operation,
            run.error_text if run and run.error_text else str(error),
            work=work,
            condition_type=condition,
            issue_code=(
                f"OPERATION_STEP_{int(step.id)}_RUN_{int(run.id)}_"
                + ("EFFECT_RECONCILIATION" if condition == "RECONCILIATION" else "INTERNAL_RECOVERY")
                if run is not None else f"OPERATION_STEP_{int(step.id)}_{condition}"
            ),
        )
        return None
    if paid:
        step.status = "PAID_FAILED"
        reason = "A paid internal step failed. Founder recovery authority is required."
        decision_kind = "PAID_INTERNAL_STEP_FAILED"
    elif definite_pre_provider:
        step.status = "BLOCKED"
        reason = f"Execution stopped before any provider call: {error}"
        decision_kind = "EXECUTION_PRE_PROVIDER_BLOCKED"
    else:
        step.status = "AMBIGUOUS"
        reason = "Provider-call truth is ambiguous; recovery is required before continuing."
        decision_kind = "PROVIDER_CALL_AMBIGUOUS"
    step.error_text = str(error)
    step.finished_at = now()
    _defer_parallel_attention(
        operation, reason=reason, decision_kind=decision_kind,
    )
    db.session.commit()


def _mark_budget_required(operation, step, error):
    step.status = "BLOCKED"
    step.error_text = "Additional Founder budget authorization is required."
    step.finished_at = now()
    _defer_parallel_attention(
        operation,
        reason=("The next bounded execution step exceeds the remaining authorized "
                "Operation budget."),
        additional_budget=error.additional,
        decision_kind="BUDGET_AUTHORIZATION",
    )
    return None


def _materialize_task_run(task, run):
    payload = run.parsed_output_json or json.loads(run.raw_output)
    task.result_summary = payload["result_summary"]
    if task.status == "ASSIGNED":
        task.status = "WORKING"
    if task.status == "WORKING":
        task.status = "REVIEW"
    db.session.commit()


def _materialize_review_run(task, run):
    payload = run.parsed_output_json or json.loads(run.raw_output)
    target = DECISIONS[payload["decision"]]
    task.status = target
    if target == "DONE":
        task.completed_at = now()
    work_runtime=__import__("eason_one.services.work_runtime",fromlist=["work_for_task"])
    work=work_runtime.work_for_task(task)
    if work:
        artifacts=__import__("eason_one.services.artifacts",fromlist=["verify_and_accept","reject"])
        models=__import__("eason_one.models",fromlist=["ArtifactVersion","Decision"])
        version=(models.ArtifactVersion.query
            .join(__import__("eason_one.models",fromlist=["Artifact"]).Artifact)
            .filter(__import__("eason_one.models",fromlist=["Artifact"]).Artifact.work_id==work.id)
            .order_by(models.ArtifactVersion.id.desc()).first())
        if version:
            if payload["decision"]=="ACCEPT":
                artifacts.verify_and_accept(
                    work,version,method="AI_REVIEW",verifier_employee_id=run.employee_id,
                    agent_run_id=run.id,details=payload,
                )
            else:
                artifacts.reject(
                    work,version,method="AI_REVIEW",verifier_employee_id=run.employee_id,
                    agent_run_id=run.id,details=payload,
                )
        decision=models.Decision.query.filter_by(source_execution_id=run.id).first()
        if not decision:
            decision=models.Decision(
                project_id=task.project_id,work_id=work.id,
                proposed_by_employee_id=run.employee_id,decided_by_employee_id=run.employee_id,
                question=f"Accept Work #{work.id} artifact?",decision=payload["decision"],
                rationale=payload.get("summary"),state="COMMITTED",
                authority_basis="Assigned independent reviewer",source_execution_id=run.id,committed_at=now(),
            )
            db.session.add(decision); db.session.flush()
            __import__("eason_one.services.company_events",fromlist=["emit"]).emit(
                "DECISION_COMMITTED",actor_type="EMPLOYEE",actor_id=run.employee_id,
                project_id=task.project_id,work_id=work.id,execution_id=run.id,
                decision_id=decision.id,correlation_id=f"work:{work.id}",
                payload={"decision":payload["decision"],"summary":payload.get("summary")},
            )
    db.session.commit()


_STEP_PURPOSE = {
    "TASK": "TASK_EXECUTION",
    "REVIEW": "TASK_REVIEW",
    "ORCHESTRATION": "ORCHESTRATION_PLAN",
    "DECISION": "CEO_OPERATION_DECISION",
    "GOAL_VERIFICATION": "GOAL_VERIFICATION",
    "REPORT": "CEO_OPERATION_REPORT",
}


def _recover_unbound_step_execution(step):
    """Find a pre-existing AgentRun from the legacy step-link crash window.

    Older source linked OperationStep.agent_run_id only after execute() returned.
    If the process died after the AgentRun/effect/cost commit, the Step remained
    EXECUTION_STARTED and the old recovery path incorrectly treated it as safe
    pre-dispatch replay.  Search only the exact step time window and exact
    purpose/task lineage.  Multiple independent roots fail closed rather than
    guessing which paid call belongs to this step.
    """
    purpose = _STEP_PURPOSE.get(step.kind)
    if purpose is None:
        return None, None
    query = AgentRun.query.filter(
        AgentRun.operation_id == step.operation_id,
        AgentRun.purpose == purpose,
        AgentRun.started_at >= step.created_at,
    )
    if step.kind in {"TASK", "REVIEW"}:
        if not step.task_id:
            return None, f"Open {step.kind} step #{step.id} has no Task lineage."
        query = query.filter(AgentRun.task_id == step.task_id)

    # A later step of the same kind is a hard upper bound. This prevents an old
    # orphaned step from adopting a genuinely later execution.
    next_step = (
        OperationStep.query.filter(
            OperationStep.operation_id == step.operation_id,
            OperationStep.kind == step.kind,
            OperationStep.id > step.id,
        )
        .order_by(OperationStep.id)
        .first()
    )
    if next_step is not None:
        query = query.filter(AgentRun.started_at < next_step.created_at)

    candidates = query.order_by(AgentRun.id).all()
    if not candidates:
        return None, None

    ids = {int(row.id) for row in candidates}
    # A candidate already owned by another OperationStep cannot be adopted.
    bound_elsewhere = {
        int(value) for (value,) in db.session.query(OperationStep.agent_run_id).filter(
            OperationStep.operation_id == step.operation_id,
            OperationStep.id != step.id,
            OperationStep.agent_run_id.in_(ids),
        ).all() if value is not None
    }
    if bound_elsewhere:
        candidates = [row for row in candidates if int(row.id) not in bound_elsewhere]
        ids = {int(row.id) for row in candidates}
    if not candidates:
        return None, None

    roots = [
        row for row in candidates
        if not getattr(row, "retry_of_run_id", None)
        or int(row.retry_of_run_id) not in ids
    ]
    if len(roots) != 1:
        return None, (
            f"Cannot prove one AgentRun lineage for open {step.kind} step #{step.id}; "
            f"found {len(roots)} independent roots across candidate Runs {sorted(ids)}."
        )

    # Every non-root attempt must descend from a candidate that was already in
    # this same retry chain. Otherwise two unrelated calls merely happened to
    # share purpose/task/time and recovery must not guess.
    connected = {int(roots[0].id)}
    pending = [row for row in candidates if int(row.id) not in connected]
    progress = True
    while pending and progress:
        progress = False
        rest = []
        for row in pending:
            parent = int(getattr(row, "retry_of_run_id", 0) or 0)
            if parent in connected:
                connected.add(int(row.id))
                progress = True
            else:
                rest.append(row)
        pending = rest
    if pending:
        return None, (
            f"AgentRun retry lineage for open {step.kind} step #{step.id} is disconnected; "
            "internal reconciliation is required before replay."
        )

    latest = max(candidates, key=lambda row: int(row.id))
    step.agent_run_id = latest.id
    db.session.commit()
    return latest, None


def _recover_open_step(operation):
    step = OperationStep.query.filter(
        OperationStep.operation_id == operation.id,
        OperationStep.status.in_(OPEN_STEP | {"CLAIMED"}),
    ).order_by(OperationStep.id).first()
    if not step:
        return None
    terminal_status = _terminal_project_status(operation)
    if terminal_status:
        # Founder completion/cancellation is a hard operational freeze.  We may
        # reconcile that an already-dispatched external effect exists, but must
        # not materialize stale Work/Decision/report side effects into a terminal
        # Project.  AgentRun/effect/cost truth remains the durable evidence.
        run = db.session.get(AgentRun, step.agent_run_id) if step.agent_run_id else None
        if run is None and not step.agent_run_id and step.status != "CLAIMED":
            run, _binding_error = _recover_unbound_step_execution(step)
        if _claimed_step_is_safe_to_resume(step):
            step.status = "FAILED_PRE_PROVIDER_TERMINAL"
            step.error_text = "Terminal Project retired a local pre-provider claim; no external effect was replayed."
        elif run is not None:
            step.status = "TERMINAL_RECONCILED"
            step.agent_run_id = run.id
            step.result_json = {
                "kind": step.kind,
                "status": "TERMINAL_RECONCILED",
                "project_status": terminal_status,
                "agent_run_id": run.id,
                "provider_replayed": False,
                "domain_materialized": False,
            }
            step.error_text = None
        else:
            step.status = "TERMINAL_RECONCILIATION_REQUIRED"
            step.result_json = {
                "kind": step.kind,
                "status": "TERMINAL_RECONCILIATION_REQUIRED",
                "project_status": terminal_status,
                "provider_replayed": False,
                "domain_materialized": False,
            }
            step.error_text = "Terminal Project has an unresolved historical provider boundary; audit/reconciliation may inspect it but execution cannot resume."
        step.finished_at = now()
        db.session.commit()
        return step.result_json or {
            "kind": "RECOVERY", "status": step.status,
            "project_status": terminal_status, "provider_replayed": False,
        }
    if _claimed_step_is_safe_to_resume(step):
        # A different scheduler/idempotency key may arrive after restart.  The
        # old local claim has no AgentRun/provider boundary, so retire only that
        # orphaned breadcrumb and let a later normal tick claim fresh work.
        step.status = "FAILED_PRE_PROVIDER_RECOVERED"
        step.error_text = "Recovered process death after local OperationStep claim and before any provider boundary."
        step.finished_at = now()
        db.session.commit()
        return {
            "kind": "RECOVERY", "status": "SAFE_REPLAY",
            "operation_step_id": step.id, "provider_replayed": False,
        }
    run = db.session.get(AgentRun, step.agent_run_id) if step.agent_run_id else None
    if run is None and not step.agent_run_id:
        run, binding_error = _recover_unbound_step_execution(step)
        if binding_error:
            step.status = "AMBIGUOUS"
            step.error_text = binding_error
            management_work = __import__(
                "eason_one.services.work_runtime", fromlist=["ensure_management_work"]
            ).ensure_management_work(operation)
            pause_for_internal_runtime_recovery(
                operation, binding_error, work=management_work,
                condition_type="RECONCILIATION",
                issue_code=f"OPERATION_STEP_{int(step.id)}_UNBOUND_EXECUTION_RECONCILIATION",
            )
            db.session.commit()
            return {
                "kind": "RECOVERY", "status": "RECONCILIATION_REQUIRED",
                "work_id": management_work.id, "agent_run_id": None,
                "founder_action_required": False,
            }
    if run and run.status == "SUCCEEDED" and (
        run.parsed_output_json or run.raw_output
    ):
        task = db.session.get(Task, step.task_id) if step.task_id else None
        if step.kind == "TASK" and task:
            result = _materialize_task_success(operation, task, run)
            result["status"] = result.get("status") or "RECOVERED"
            result["recovered"] = True
            return _finish(step, run, result)
        elif step.kind == "REVIEW" and task:
            _materialize_review_run(task, run)
        elif step.kind == "GOAL_VERIFICATION":
            payload = run.parsed_output_json or json.loads(run.raw_output)
            criteria = (
                operation.plan_json["operation"].get("completion_criteria") or []
            )
            payload = _validate_goal_verification(payload, criteria)
            digest = step.logical_key.split(":", 1)[1]
            _persist_goal_verification(operation, payload, digest)
            return _finish(step, run, {
                "kind": "GOAL_VERIFICATION",
                "overall_status": payload["overall_status"],
                "verification": payload,
                "evidence_digest": digest,
                "agent_run_id": run.id,
                "status": "RECOVERED",
            })
        elif step.kind == "REPORT":
            verification_step, _ = _current_goal_verification(operation)
            verification = (
                (verification_step.result_json or {}).get("verification")
                if verification_step else None
            )
            if not verification or verification.get("overall_status") != "SATISFIED":
                raise ValueError(
                    "Recovered Founder report lacks a current SATISFIED Goal Verification"
                )
            result = _materialize_report_run(operation, run, verification)
            result["recovered"] = True
            return _finish(step, run, result)
        elif step.kind == "ORCHESTRATION":
            multi = __import__(
                "eason_one.services.multi_agent", fromlist=["persist_plan", "validate_payload"]
            )
            payload = run.parsed_output_json or json.loads(run.raw_output)
            plan = multi.validate_payload(operation, payload)
            multi.persist_plan(operation, plan, planner_run_id=run.id)
            return _finish(step, run, {
                "kind": "ORCHESTRATION",
                "status": "RECOVERED",
                "strategy": plan["strategy"],
                "max_parallelism": plan["max_parallelism"],
                "agent_run_id": run.id,
            })
        elif step.kind == "DECISION":
            decision = run.parsed_output_json or json.loads(run.raw_output)
            task = db.session.get(Task, step.task_id) if step.task_id else None
            result = _apply_management_decision(
                operation, run, decision, blocked_task=task
            )
            result = dict(result)
            result["status"] = "RECOVERED"
            return _finish(step, run, result)
        else:
            step.status = "AMBIGUOUS"
            step.error_text = (
                f"Unknown persisted paid OperationStep kind {step.kind!r}; Company runtime must "
                "reconcile/materialize the already-paid result before continuing. Founder action is not required."
            )
            management_work = __import__(
                "eason_one.services.work_runtime", fromlist=["ensure_management_work"]
            ).ensure_management_work(operation)
            pause_for_internal_runtime_recovery(
                operation,
                step.error_text,
                work=management_work,
                condition_type="RECONCILIATION",
                issue_code=f"OPERATION_STEP_{int(step.id)}_RUN_{int(run.id)}_MATERIALIZATION_RECONCILIATION",
            )
            return {
                "kind": "RECOVERY", "status": "RECONCILIATION_REQUIRED",
                "work_id": management_work.id, "agent_run_id": run.id,
                "founder_action_required": False,
            }
        return _finish(step, run, {
            "kind": step.kind, "status": "RECOVERED",
            "task_id": getattr(task, "id", None), "agent_run_id": run.id,
        })
    task=db.session.get(Task,step.task_id) if step.task_id else None
    work=__import__("eason_one.services.work_runtime",fromlist=["work_for_task"]).work_for_task(task) if task else None
    if run and run.outcome in {"FAILED_SAFE","FAILED_KNOWN"}:
        step.status=run.outcome
        step.error_text=run.error_text
        step.finished_at=now()
        if task and task.status not in {"DONE","CANCELLED"}:
            task.status="BLOCKED"
        if work:
            __import__("eason_one.services.work_runtime",fromlist=["open_wait"]).open_wait(
                work,"INTERNAL_RECOVERY",run.error_text or "Recovered failed Execution requires local replan.",
                issue_code=f"OPERATION_STEP_{int(step.id)}_RUN_{int(run.id)}_INTERNAL_RECOVERY",
            )
        db.session.commit()
        return {"kind":"RECOVERY","status":"INTERNAL_RECOVERY","task_id":getattr(task,"id",None),"work_id":getattr(work,"id",None),"agent_run_id":run.id}
    if run and run.outcome=="FAILED_AMBIGUOUS":
        step.status="AMBIGUOUS"; step.error_text=run.error_text; step.finished_at=now()
        if task and task.status not in {"DONE","CANCELLED"}:
            task.status="BLOCKED"
        if work:
            __import__("eason_one.services.work_runtime",fromlist=["open_wait"]).open_wait(
                work,"RECONCILIATION",run.error_text or "Provider effect requires reconciliation.",
                issue_code=f"OPERATION_STEP_{int(step.id)}_RUN_{int(run.id)}_EFFECT_RECONCILIATION",
            )
        db.session.commit()
        return {"kind":"RECOVERY","status":"RECONCILIATION_REQUIRED","task_id":getattr(task,"id",None),"work_id":getattr(work,"id",None),"agent_run_id":run.id}
    if run is None and step.status=="EXECUTION_STARTED" and step.provider_started_at is None:
        # Crash happened after claiming the orchestration step but before a
        # durable provider dispatch boundary was crossed.  Replay is safe.
        step.status="FAILED_PRE_PROVIDER_RECOVERED"; step.finished_at=now()
        if task and task.status=="WORKING": task.status="ASSIGNED"
        if work and work.state=="EXECUTING":
            __import__("eason_one.services.work_runtime",fromlist=["transition"]).transition(
                work,"READY",reason="Recovered orchestration crash before provider dispatch"
            )
        db.session.commit()
        return {"kind":"RECOVERY","status":"SAFE_REPLAY","task_id":getattr(task,"id",None),"work_id":getattr(work,"id",None)}
    # Legacy steps created before vNext may genuinely lack enough effect truth.
    step.status="AMBIGUOUS"
    if work:
        __import__("eason_one.services.work_runtime",fromlist=["open_wait"]).open_wait(
            work,"RECONCILIATION","Legacy provider-call truth is ambiguous and requires reconciliation.",
            issue_code=f"OPERATION_STEP_{int(step.id)}_LEGACY_EFFECT_RECONCILIATION",
        )
    __import__("eason_one.services.operation_kernel",fromlist=["transition"]).transition(
        operation,"WAITING_INPUT","LEGACY_PROVIDER_RECONCILIATION_REQUIRED",
        stage="RECOVERY",actor_type="RUNTIME",commit=False,
    )
    operation.waiting_reason="Legacy provider-call truth is ambiguous; internal reconciliation is required."
    db.session.commit()
    return {"kind":"RECOVERY","status":"RECONCILIATION_REQUIRED","task_id":getattr(task,"id",None),"work_id":getattr(work,"id",None)}


def _materialize_task_success(operation, task, run):
    """Idempotently turn a successful Execution into Work/Artifact truth."""
    # vNext Work/Artifact acceptance and Founder authority are owned by
    # company_kernel/work_execution/governance. This legacy Operation materializer
    # contains historical self-validation/escalation behavior and must never
    # become an alternate writer for governed Work.
    if __import__(
        "eason_one.services.core_v018", fromlist=["is_v018_operation"]
    ).is_v018_operation(operation):
        raise ValueError("VNEXT_LEGACY_TASK_MATERIALIZER_FORBIDDEN")
    payload = run.parsed_output_json or json.loads(run.raw_output or "{}")
    if isinstance(payload, dict) and payload.get("result_summary"):
        task.result_summary = payload["result_summary"]
    work_runtime = __import__(
        "eason_one.services.work_runtime", fromlist=["work_for_task", "transition", "open_wait"]
    )
    work = work_runtime.work_for_task(task)
    artifacts = __import__(
        "eason_one.services.artifacts", fromlist=["submit_from_execution", "verify_and_accept"]
    )
    is_engineer = bool(task.assigned_employee and task.assigned_employee.slug == "engineer")
    artifact_version = None
    if work:
        artifact_version = artifacts.submit_from_execution(
            work, run,
            artifact_type="CODE_CHANGE" if is_engineer else "WORK_RESULT",
            title=task.title,
            content_text=(task.result_summary or run.raw_output),
        )
    codex_payload = (payload or {}).get("codex") or {} if isinstance(payload, dict) else {}
    if is_engineer:
        if codex_payload.get("needs_founder"):
            task.status = "BLOCKED"
            if work:
                work_runtime.open_wait(
                    work, "FOUNDER_DECISION",
                    codex_payload.get("founder_reason")
                    or "Codex identified a change outside approved engineering authority.",
                )
            __import__(
                "eason_one.services.escalations", fromlist=["open_escalation"]
            ).open_escalation(
                project_id=task.project_id, work_id=getattr(work, "id", None),
                operation_id=operation.id, escalation_type="CODEX_RISK_APPROVAL",
                reason=codex_payload.get("founder_reason")
                or "Codex identified a change outside approved engineering authority.",
                created_by_employee_id=task.assigned_employee_id,
            )
            db.session.commit()
            return {
                "kind": "TASK", "status": "WORK_WAITING_FOUNDER",
                "task_id": task.id, "work_id": getattr(work, "id", None),
                "agent_run_id": run.id,
                "artifact_version_id": getattr(artifact_version, "id", None),
            }
        task.status = "DONE"
        task.completed_at = task.completed_at or now()
        if work and artifact_version:
            artifacts.verify_and_accept(
                work, artifact_version, method="HOST_ENGINEERING_VALIDATION",
                verifier_employee_id=task.assigned_employee_id, agent_run_id=run.id,
                details={"codex": codex_payload},
            )
        db.session.commit()
        return {
            "kind": "TASK", "status": "SUCCEEDED", "task_id": task.id,
            "work_id": getattr(work, "id", None), "agent_run_id": run.id,
            "artifact_version_id": getattr(artifact_version, "id", None),
            "review": "ENGINEER_CODEX_SELF_VALIDATED",
        }

    if task.reviewer_employee_id and task.reviewer_employee_id != task.assigned_employee_id:
        if task.status != "REVIEW":
            task.status = "REVIEW"
        if work and work.state not in {"VERIFYING", "ACCEPTED"}:
            work_runtime.transition(work, "VERIFYING", reason="Independent review required")
        review_mode = "INDEPENDENT_REVIEW"
    else:
        task.status = "DONE"
        task.completed_at = task.completed_at or now()
        if work and artifact_version:
            # No independent reviewer does not mean semantic verification magically
            # happened.  Record only the deterministic facts Runtime can prove here:
            # a successful, schema-validated Execution produced this exact durable
            # artifact.  Project Goal Verification remains responsible for deciding
            # whether the delivered evidence actually satisfies the Project outcome.
            if run.status != "SUCCEEDED" or run.structured_validation_status != "PASSED":
                task.status = "BLOCKED"
                work_runtime.open_wait(
                    work, "VERIFICATION",
                    "Artifact exists, but the producing Execution lacks deterministic structured validation.",
                )
                db.session.commit()
                return {
                    "kind": "TASK", "status": "WORK_WAITING", "task_id": task.id,
                    "work_id": work.id, "agent_run_id": run.id,
                    "artifact_version_id": artifact_version.id,
                    "failure_reason": "ARTIFACT_VALIDATION_INCOMPLETE",
                }
            artifacts.verify_and_accept(
                work, artifact_version, method="DETERMINISTIC_EXECUTION_VALIDATION",
                agent_run_id=run.id,
                details={
                    "execution_status": run.status,
                    "structured_validation_status": run.structured_validation_status,
                    "artifact_content_hash": artifact_version.content_hash,
                    "scope": "execution_schema_and_durable_artifact_integrity",
                    "semantic_acceptance_criteria_verified": False,
                },
            )
        review_mode = "DETERMINISTIC_EXECUTION_VALIDATION"
    db.session.commit()
    return {
        "kind": "TASK", "status": "SUCCEEDED", "task_id": task.id,
        "work_id": getattr(work, "id", None), "agent_run_id": run.id,
        "artifact_version_id": getattr(artifact_version, "id", None),
        "review": review_mode,
    }


def _run_task_step(operation, request_key, task):
    work_runtime=__import__(
        "eason_one.services.work_runtime",
        fromlist=["work_for_task","transition","open_wait","sync_task_projection","project_is_terminal"],
    )
    project = getattr(operation, "project", None) or getattr(task, "project", None)
    if project is not None and work_runtime.project_is_terminal(project):
        raise ValueError(
            f"PROJECT_TERMINAL_OPERATION_TASK_FORBIDDEN:{str(project.status or '').upper()}"
        )
    work=work_runtime.work_for_task(task)
    if work:
        # Work owns vNext execution truth. Task is synchronized only so the
        # proven legacy execution helper can run without becoming a second gate.
        work_runtime.sync_task_projection(work, task)
        # A task runner is never a gate resolver.  If Governance/recovery truth
        # says this Work is waiting, the owning resolver must retire the exact
        # gate first.  Guard before claiming an OperationStep so a blocked Work
        # cannot even leave a false durable "execution started" breadcrumb.
        if work.state == "WAITING" and work_runtime.has_open_gate(work):
            raise ValueError(
                "WORK_WAIT_GATE_STILL_OPEN: Work execution cannot bypass unresolved governance/recovery truth"
            )
    logical = _logical_key(operation, "TASK", task)
    step, created = _claim(operation, request_key, logical, "TASK", task)
    if not created:
        if _claimed_step_is_safe_to_resume(step):
            # Crash happened after the durable local claim but before any
            # provider boundary.  Re-enter the same exact step; no external
            # effect can be duplicated because AgentRun/provider truth is absent.
            pass
        elif step.status in OPEN_STEP:
            return _recover_open_step(operation)
        else:
            return _existing_result(step)
    if work:
        if work.state in {"READY","WAITING"}:
            work_runtime.transition(
                work,"EXECUTING",actor_type="EMPLOYEE",actor_id=task.assigned_employee_id,
                reason="Execution attempt started",
            )
        work_runtime.sync_task_projection(work, task)
    elif task.status == "ASSIGNED":
        transition(task, "WORKING")
    _provider_boundary(step)

    run = run_task(task)
    # One Work may have multiple immutable Execution attempts.  Safe/known
    # failures recover locally before management/Founder escalation.
    retry_limit=int(getattr(work,"retry_limit",1) or 0) if work else 0
    retryable_reasons={
        "PROVIDER_PREFLIGHT_FAILED","REFUSAL","OUTPUT_TRUNCATED","PROVIDER_INCOMPLETE",
        "STRUCTURED_OUTPUT_INVALID","POSTPROCESS_FAILED","FOUNDER_BUDGET_EXTENSION_REQUIRED",
    }
    for _ in range(retry_limit):
        if run.status=="SUCCEEDED":
            break
        if run.outcome not in {"FAILED_SAFE","FAILED_KNOWN"}:
            break
        if run.failure_reason not in retryable_reasons:
            break
        alternate=__import__(
            "eason_one.services.execution_policy",fromlist=["select_retry_model"]
        ).select_retry_model(task.assigned_employee,run,operation,"TASK_EXECUTION")
        if alternate is None:
            break
        __import__("eason_one.services.company_events",fromlist=["emit"]).emit(
            "EXECUTION_RETRIED",actor_type="RUNTIME",project_id=task.project_id,
            work_id=getattr(work,"id",None),execution_id=run.id,
            correlation_id=(f"work:{work.id}" if work else f"task:{task.id}"),
            payload={"failed_execution_id":run.id,"next_provider":alternate.provider_key,"next_model":alternate.model_name},
        )
        db.session.commit()
        run=run_task(task,retry_of_run=run,model_override=alternate)

    step.agent_run_id=run.id
    db.session.commit()
    is_engineer=bool(task.assigned_employee and task.assigned_employee.slug=="engineer")
    if run.status!="SUCCEEDED" or not run.parsed_output_json:
        task.status="BLOCKED"
        if work:
            if run.outcome=="FAILED_AMBIGUOUS":
                work_runtime.open_wait(
                    work,"RECONCILIATION",
                    run.error_text or "Provider effect requires reconciliation before this Work can continue.",
                    issue_code=f"TASK_EXECUTION_RUN_{int(run.id)}_EFFECT_RECONCILIATION",
                )
            elif run.failure_reason=="MISSING_EVIDENCE":
                evidence_meta = dict((run.context_composition_json or {}).get("evidence_retrieval") or {})
                basis_hash = str(evidence_meta.get("basis_hash") or "")
                issue_code = f"MISSING_EVIDENCE:{basis_hash}" if basis_hash else f"MISSING_EVIDENCE:RUN:{int(run.id)}"
                work_runtime.open_wait(
                    work,"DEPENDENCY",
                    run.error_text or "Required persisted evidence is missing.",
                    issue_code=issue_code,
                    resume_state="EXECUTING",
                )
            elif run.failure_reason in {"AUTHORITY_BLOCKED", "FOUNDER_BUDGET_EXTENSION_REQUIRED"}:
                work_runtime.open_wait(
                    work,"BUDGET",
                    run.error_text or "Execution lacks current budget/authority.",
                )
            else:
                work_runtime.open_wait(
                    work,"INTERNAL_RECOVERY",
                    run.error_text or "Local retry was exhausted; Project management must replan this Work.",
                    issue_code=f"TASK_EXECUTION_RUN_{int(run.id)}_INTERNAL_RECOVERY",
                )
        additional = _budget_extension_from_run(run)
        if additional is not None:
            # The runtime already exhausted safe alternate-intelligence
            # recovery. This is now a real Project budget-extension request,
            # one of the few cases that legitimately needs Founder authority.
            wait_for_founder(
                operation,
                "The affected Work cannot continue within the remaining Founder-approved Project budget after bounded internal recovery.",
                additional_budget=additional,
                decision_kind="BUDGET_AUTHORIZATION",
            )
        return _finish(step,run,{
            "kind":"TASK","status":("WORK_WAITING_FOUNDER" if additional is not None else "WORK_WAITING"),"task_id":task.id,
            "work_id":getattr(work,"id",None),"agent_run_id":run.id,
            "execution_outcome":run.outcome,"failure_reason":run.failure_reason,
        })

    result = _materialize_task_success(operation, task, run)
    return _finish(step, run, result)

def _run_review_step(operation, request_key, task):
    work_runtime = __import__(
        "eason_one.services.work_runtime",
        fromlist=["work_for_task", "sync_task_projection", "project_is_terminal"],
    )
    project = getattr(operation, "project", None) or getattr(task, "project", None)
    if project is not None and work_runtime.project_is_terminal(project):
        raise ValueError(
            f"PROJECT_TERMINAL_OPERATION_REVIEW_FORBIDDEN:{str(project.status or '').upper()}"
        )
    work = work_runtime.work_for_task(task)
    if work:
        work_runtime.sync_task_projection(work, task)
    if not task.reviewer_employee_id or task.reviewer_employee_id == task.assigned_employee_id:
        task.status = "DONE"
        task.completed_at = now()
        db.session.commit()
        return {"kind": "REVIEW", "status": "SKIPPED", "task_id": task.id, "reason": "No independent reviewer was approved."}
    logical = _logical_key(operation, "REVIEW", task)
    step, created = _claim(operation, request_key, logical, "REVIEW", task)
    if not created:
        if _claimed_step_is_safe_to_resume(step):
            pass
        elif step.status in OPEN_STEP:
            return _recover_open_step(operation)
        else:
            return _existing_result(step)
    _provider_boundary(step)
    run = run_review(task)
    step.agent_run_id = run.id
    db.session.commit()
    if run.status != "SUCCEEDED" or not run.parsed_output_json:
        task.status="BLOCKED"
        _mark_paid_or_ambiguous(operation, step, run, run.error_text or "Review execution failed")
        return _finish(step,run,{
            "kind":"REVIEW","status":"WORK_WAITING","task_id":task.id,
            "work_id":getattr(task,"work_id",None),"agent_run_id":run.id,
            "execution_outcome":run.outcome,"failure_reason":run.failure_reason,
        })
    _materialize_review_run(task,run)
    return _finish(step, run, {
        "kind": "REVIEW", "status": "SUCCEEDED", "task_id": task.id,
        "work_id":getattr(task,"work_id",None),
        "decision": run.parsed_output_json["decision"],
        "agent_run_id": run.id,
    })


def create_meeting(operation, reason, participants=None, budget_twd=None, source_execution_id=None):
    if source_execution_id is not None:
        existing = Meeting.query.filter_by(source_execution_id=source_execution_id).first()
        if existing:
            return existing
    project = getattr(operation, "project", None)
    if project is not None and __import__(
        "eason_one.services.work_runtime", fromlist=["project_is_terminal"]
    ).project_is_terminal(project):
        raise ValueError(
            f"PROJECT_TERMINAL_OPERATION_MEETING_FORBIDDEN:{str(project.status or '').upper()}"
        )
    config = dict(
        (operation.plan_json or {}).get("operation", {}).get("meeting_config")
        or default_meeting_config((operation.plan_json or {}).get("operation") or {})
    )
    budget = Decimal(str(
        budget_twd if budget_twd is not None else config.get("budget_twd", "0.5")
    ))
    ensure_budget(operation, budget)
    if not reason.strip():
        raise ValueError("CEO Meeting reason is required")
    ceo = db.session.get(Employee, operation.proposed_by_employee_id)
    if participants is None:
        participants = [
            db.session.get(Employee, employee_id)
            for employee_id in config.get("participant_employee_ids") or []
        ]
        participants = [employee for employee in participants if employee and employee.active]
    unique = []
    for employee in [ceo, *(participants or [])]:
        if employee and employee.id not in {item.id for item in unique}:
            unique.append(employee)
    if len(unique) < 2:
        fallback = next((
            task.reviewer or task.assigned_employee for task in operation.tasks
            if (task.reviewer or task.assigned_employee)
            and (task.reviewer or task.assigned_employee).id != ceo.id
        ), None)
        if fallback:
            unique.append(fallback)
    meeting = meeting_service.create(
        title=f"{operation.title} — Internal Review",
        purpose=reason, agenda=reason, chair=ceo,
        participants=unique[:4], project=operation.project,
        max_rounds=int(config.get("max_rounds", 2)),
        token_limit=int(config.get("token_limit", 6000)),
        real_cost_limit_twd=budget, profile="ECONOMY",
        max_speakers_per_round=int(config.get("max_speakers_per_round", 3)),
        contribution_output_cap=int(config.get("contribution_output_cap", 512)),
    )
    # Runtime routing state must stay empty until the Meeting router selects
    # a round.  Governance metadata remains authoritative in the approved
    # Operation plan; mixing it into routing_json makes the runner treat
    # metadata as an active route.
    meeting.routing_json = None
    meeting.created_by = "CEO"
    meeting.operation_id = operation.id
    meeting.source_execution_id = source_execution_id
    db.session.commit()
    return meeting


def create_meeting_for_conflict(operation, task, reason, budget_twd, source_execution_id=None):
    if source_execution_id is not None:
        existing = Meeting.query.filter_by(source_execution_id=source_execution_id).first()
        if existing:
            return existing
    employees = []
    for employee in (
        db.session.get(Employee, operation.proposed_by_employee_id),
        task.assigned_employee, task.reviewer,
    ):
        if employee and employee.id not in {item.id for item in employees}:
            employees.append(employee)
    meeting = create_meeting(
        operation, reason, employees, budget_twd,
        source_execution_id=source_execution_id,
    )
    meeting.related_work_id = getattr(task, "work_id", None)
    db.session.commit()
    meeting_service.start_auto(meeting)
    return meeting


def _consume_meeting(operation, meeting, request_key):
    logical = _logical_key(operation, "MEETING_RESULT", subject=meeting.id)
    step, created = _claim(
        operation, request_key, logical, "MEETING_RESULT"
    )
    if not created and not _claimed_step_is_safe_to_resume(step):
        return _existing_result(step)
    result = meeting_service.result_view(meeting)
    if not result:
        raise ValueError("Meeting has no usable result")
    memory = dict(operation.memory_json or {})
    outcomes = list(memory.get("meeting_results") or [])
    # A crash can occur after the deterministic Meeting result was projected
    # into Operation memory but before the local OperationStep was finished.
    # Re-entering a pre-provider CLAIMED step must therefore be idempotent.
    existing_index = next((
        index for index, item in enumerate(outcomes)
        if int(item.get("meeting_id") or 0) == int(meeting.id)
    ), None)
    row = {"meeting_id": meeting.id, "result": result}
    if existing_index is None:
        outcomes.append(row)
    else:
        outcomes[existing_index] = row
    memory["meeting_results"] = outcomes
    operation.memory_json = memory
    db.session.commit()
    return _finish(step, None, {
        "kind": "MEETING_RESULT", "status": "CONSUMED",
        "meeting_id": meeting.id,
    })


def _active_employee(employee_id, label):
    employee = db.session.get(Employee, employee_id)
    if not employee or not employee.active:
        raise ValueError(f"{label} must be an active Employee")
    return employee


def _optional_employee(employee_id, label):
    if employee_id is None:
        return None
    return _active_employee(employee_id, label)


def create_remediation_task(operation, plan, agent_run_id=None):
    if agent_run_id:
        audit = WorkMessage.query.filter_by(
            agent_run_id=agent_run_id, message_type="CEO_REMEDIATION"
        ).first()
        if audit and audit.task_id:
            return db.session.get(Task, audit.task_id)
    if operation.status != "RUNNING" or not operation.approved_at:
        raise ValueError("Operation must be Founder-approved and running")
    required = {
        "title", "objective", "assignee_employee_id", "reviewer_employee_id",
        "acceptance_criteria", "reason",
    }
    if not isinstance(plan, dict) or not required.issubset(plan):
        raise ValueError("CREATE_TASK requires a complete task_plan")
    if not all(
        isinstance(plan.get(field), str) and plan[field].strip()
        for field in ("title", "objective", "reason")
    ):
        raise ValueError("Remediation title, objective, and reason are required")
    criteria = plan["acceptance_criteria"]
    if not isinstance(criteria, list) or not criteria or not all(
        isinstance(item, str) and item.strip() for item in criteria
    ):
        raise ValueError("Remediation acceptance criteria are required")
    assignee = _active_employee(plan["assignee_employee_id"], "Assignee")
    reviewer = _optional_employee(plan.get("reviewer_employee_id"), "Reviewer")
    remediation_contract = __import__(
        "eason_one.services.acceptance_contract", fromlist=["build"]
    ).build(
        title=plan["title"], objective=plan["objective"], criteria=criteria,
        reviewer_employee_id=getattr(reviewer, "id", None),
        owner_employee_id=assignee.id,
    )
    if __import__(
        "eason_one.services.acceptance_contract", fromlist=["semantic_criteria"]
    ).semantic_criteria(remediation_contract) and (
        reviewer is None or reviewer.id == assignee.id
    ):
        reviewer = next((
            employee for employee in Employee.query.filter_by(active=True).order_by(Employee.id).all()
            if employee.id != assignee.id
        ), None)
        if reviewer is None:
            raise ValueError(
                "Remediation acceptance contains semantic criteria but no independent reviewer Employee is available."
            )
    blocked = next(
        (item for item in operation.tasks if item.status == "BLOCKED"), None
    )
    verification = (operation.memory_json or {}).get("goal_verification") or {}
    verification_requires_work = verification.get(
        "overall_status"
    ) in {"NOT_SATISFIED", "INSUFFICIENT_EVIDENCE"}
    if not blocked and not HiringRequest.query.filter_by(
        operation_id=operation.id, status="HIRED"
    ).first() and not verification_requires_work:
        raise ValueError(
            "Remediation Work requires blocked work, an approved capability "
            "change, or an unsatisfied Goal Verification"
        )

    work_runtime = __import__(
        "eason_one.services.work_runtime", fromlist=["create_work", "work_for_task"]
    )
    delivery_count = sum(
        1 for work in operation.works if work.work_type != "MANAGEMENT"
    )
    if delivery_count >= 20:
        raise ValueError(
            "Operation reached the deterministic ceiling of 20 delivery Works; "
            "management must replan existing Work or explicitly escalate."
        )
    parent_work = work_runtime.work_for_task(blocked) if blocked else None
    work = work_runtime.create_work(
        project_id=operation.project_id,
        operation_id=operation.id,
        parent_work_id=getattr(parent_work, "id", None),
        title=plan["title"].strip(),
        purpose=plan["objective"].strip(),
        owner_employee_id=assignee.id,
        created_by_employee_id=operation.proposed_by_employee_id,
        expected_output="Remediation result",
        acceptance_criteria="\n".join(item.strip() for item in criteria),
        priority="HIGH",
        work_type="REMEDIATION",
        resource_ceiling_twd=operation.approved_budget_twd,
        reason=plan["reason"].strip(),
    )
    task = Task(
        operation_id=operation.id, project_id=operation.project_id,
        work_id=work.id,
        parent_task_id=getattr(blocked, "id", None),
        title=plan["title"].strip(), objective=plan["objective"].strip(),
        status="ASSIGNED", priority="HIGH",
        created_by_employee_id=operation.proposed_by_employee_id,
        assigned_employee_id=assignee.id, reviewer_employee_id=getattr(reviewer, "id", None),
        required_output="Remediation result",
        acceptance_criteria="\n".join(item.strip() for item in criteria),
    )
    db.session.add(task)
    db.session.flush()
    __import__(
        "eason_one.services.acceptance_contract", fromlist=["ensure_for_work"]
    ).ensure_for_work(work, task=task)
    db.session.add(WorkMessage(
        project_id=operation.project_id, task_id=task.id,
        sender_employee_id=operation.proposed_by_employee_id,
        recipient_employee_id=assignee.id,
        message_type="CEO_REMEDIATION",
        content=plan["reason"].strip(),
        agent_run_id=agent_run_id,
    ))
    db.session.commit()
    return task

def reassign_task(
    operation, task, assignee, reviewer, reason, agent_run_id=None
):
    if agent_run_id:
        audit = WorkMessage.query.filter_by(
            agent_run_id=agent_run_id, message_type="CEO_REASSIGNMENT"
        ).first()
        if audit:
            return task
    if operation.status != "RUNNING" or not operation.approved_at:
        raise ValueError("Operation must be Founder-approved and running")
    if task.operation_id != operation.id or task.project_id != operation.project_id:
        raise ValueError("Task is outside the approved Operation")
    if task.status not in {"ASSIGNED", "WORKING", "BLOCKED"}:
        raise ValueError(f"Task in {task.status} cannot be reassigned")
    assignee = _active_employee(getattr(assignee, "id", assignee), "Assignee")
    reviewer = _optional_employee(getattr(reviewer, "id", reviewer), "Reviewer")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("Reassignment audit reason is required")
    previous_assignee_id = task.assigned_employee_id
    previous_reviewer_id = task.reviewer_employee_id
    work_runtime = __import__(
        "eason_one.services.work_runtime",
        fromlist=["work_for_task", "reassign", "resolve_waits", "open_gates"],
    )
    work = work_runtime.work_for_task(task)
    if work:
        contract = __import__(
            "eason_one.services.acceptance_contract", fromlist=["ensure_for_work", "semantic_criteria"]
        )
        frozen = contract.ensure_for_work(work, task=task)
        semantic = contract.semantic_criteria(frozen)
        if semantic:
            frozen_reviewer_id = frozen.get("reviewer_employee_id")
            if assignee.id == frozen_reviewer_id:
                raise ValueError(
                    "Reassignment would make the frozen independent reviewer the Artifact producer; replan as new Work instead."
                )
            if reviewer is not None and reviewer.id != frozen_reviewer_id:
                raise ValueError(
                    "Semantic reviewer authority is frozen for this Work; changing it requires a new/replanned Work."
                )
            reviewer = db.session.get(Employee, frozen_reviewer_id) if frozen_reviewer_id else None
        else:
            reviewer = None
        work_runtime.reassign(
            work, assignee.id,
            assigned_by_employee_id=operation.proposed_by_employee_id,
            reason=reason.strip(),
        )
        # Reassignment owns only execution/assignment recovery.  It must not
        # erase an unrelated Meeting/kernel/internal gate merely because both
        # happen to use INTERNAL_RECOVERY on the same durable Work.
        for gate in list(work_runtime.open_gates(work)):
            if str(gate.get("condition_type") or "").upper() != "INTERNAL_RECOVERY":
                continue
            issue = str(gate.get("issue_code") or "")
            if not (
                issue.startswith("TASK_EXECUTION_RUN_")
                or issue.startswith("OPERATION_STEP_")
            ):
                continue
            work_runtime.resolve_waits(
                work, "INTERNAL_RECOVERY", issue_code=issue,
                note="Management reassigned Work to a valid Employee for this exact execution recovery.",
            )
    task.assigned_employee_id = assignee.id
    task.reviewer_employee_id = getattr(reviewer, "id", None)
    if task.status == "BLOCKED":
        task.status = "ASSIGNED"
    db.session.add(WorkMessage(
        project_id=operation.project_id, task_id=task.id,
        sender_employee_id=operation.proposed_by_employee_id,
        recipient_employee_id=assignee.id,
        message_type="CEO_REASSIGNMENT",
        content=(
            f"{reason.strip()} Previous assignee #{previous_assignee_id}; "
            f"previous reviewer #{previous_reviewer_id}; new assignee "
            f"#{assignee.id}; new reviewer #{getattr(reviewer, 'id', None)}."
        ),
        agent_run_id=agent_run_id,
    ))
    db.session.commit()
    return task


def _handle_paid_meeting_failure(operation, meeting, step, run):
    """Own paid Meeting recovery inside the Company, never by generic Founder escalation.

    One retry is governed by the already-approved Meeting envelope.  When that
    allowance is exhausted, a Work-scoped Meeting returns its Work to bounded
    management recovery; an Operation-scoped Meeting returns the MANAGEMENT Work
    to the same recovery path.  Founder attention is reserved for an exact
    canonical authority delta (budget/scope/constraint/etc.), not provider or
    coordination failure.
    """
    if not meeting.paid_failure_json:
        return None
    config = (
        (operation.plan_json or {}).get("operation", {}).get("meeting_config")
        or default_meeting_config((operation.plan_json or {}).get("operation") or {})
    )
    retry_limit = int(config.get("retry_limit", 0))
    retries_used = meeting_service.call_breakdown(meeting).get("retries", 0)
    if retries_used < retry_limit:
        failure = dict(meeting.paid_failure_json or {})
        meeting_service.retry_paid_step(meeting, actor_type="RUNTIME")
        return _finish(step, run, {
            "kind": "MEETING_AUTO_RETRY",
            "meeting_id": meeting.id,
            "status": "RETRY_SCHEDULED",
            "retry_strategy": failure.get("retry_strategy"),
            "failed_run_id": failure.get("run_id"),
            "retry_limit": retry_limit,
        })

    work_runtime = __import__(
        "eason_one.services.work_runtime",
        fromlist=["ensure_management_work", "open_wait", "task_for_work"],
    )
    work = None
    if meeting.related_work_id:
        work = db.session.get(
            __import__("eason_one.models", fromlist=["Work"]).Work,
            meeting.related_work_id,
        )
    if work is None:
        work = work_runtime.ensure_management_work(operation)

    task = work_runtime.task_for_work(work)
    if task and task.status not in {"DONE", "CANCELLED"}:
        task.status = "BLOCKED"
    scope = "Work" if meeting.related_work_id else "Operation management"
    reason = (
        f"The {scope}-scoped Meeting exhausted its approved retry allowance; "
        "Company management must choose a bounded internal recovery strategy "
        "inside the existing Founder Project Contract."
    )
    work_runtime.open_wait(
        work, "INTERNAL_RECOVERY", reason,
        issue_code=f"MEETING_{int(meeting.id)}_PAID_FAILURE_EXHAUSTED",
    )
    meeting_service.close_for_internal_recovery(meeting, reason)
    step.agent_run_id = getattr(run, "id", None)
    step.status = "PAID_FAILED"
    step.finished_at = now()
    step.result_json = {
        "kind": "MEETING_STEP",
        "status": "WORK_WAITING",
        "meeting_id": meeting.id,
        "work_id": work.id,
        "agent_run_id": getattr(run, "id", None),
        "founder_action_required": False,
    }
    db.session.commit()
    return step.result_json


def _advance_meeting(operation, meeting, request_key):
    _assert_operation_project_executable(operation, action="MEETING_ADVANCE")
    sequence = OperationStep.query.filter_by(
        operation_id=operation.id, kind="MEETING_STEP"
    ).count() + 1
    logical = _logical_key(
        operation, "MEETING_STEP", subject=f"{meeting.id}:{sequence}"
    )
    step, created = _claim(
        operation, request_key, logical, "MEETING_STEP"
    )
    if not created:
        if _claimed_step_is_safe_to_resume(step):
            pass
        else:
            return _existing_result(step)
    _provider_boundary(step)
    try:
        result = meeting_service.next_step_idempotent(meeting, logical)
        run = AgentRun.query.filter_by(
            meeting_id=meeting.id
        ).order_by(AgentRun.id.desc()).first()
        if meeting.paid_failure_json:
            handled = _handle_paid_meeting_failure(
                operation, meeting, step, run
            )
            if handled is not None:
                return handled
            raise RuntimeError("MEETING_PAID_FAILURE_RECOVERY_DID_NOT_RETURN_RESULT")
        return _finish(step, run, {
            "kind": "MEETING_STEP", "meeting_id": meeting.id,
            "meeting_status": meeting.status, "status": "SUCCEEDED",
        })
    except Exception as exc:
        run = AgentRun.query.filter_by(
            meeting_id=meeting.id
        ).order_by(AgentRun.id.desc()).first()
        if meeting.paid_failure_json:
            handled = _handle_paid_meeting_failure(
                operation, meeting, step, run
            )
            if handled is not None:
                return handled
            raise RuntimeError("MEETING_PAID_FAILURE_RECOVERY_DID_NOT_RETURN_RESULT") from exc
        if step.status != "PAID_FAILED":
            _mark_paid_or_ambiguous(operation, step, run, exc)
        raise


def _persist_management_decision(operation, run, decision_payload, result, management_work):
    """Persist one auditable management decision per CEO Execution Attempt."""
    models = __import__("eason_one.models", fromlist=["Decision"])
    existing = models.Decision.query.filter_by(source_execution_id=run.id).first()
    if existing:
        return existing
    action = result.get("action") or decision_payload.get("action") or "UNKNOWN"
    row = models.Decision(
        project_id=operation.project_id,
        work_id=getattr(management_work, "id", None),
        proposed_by_employee_id=run.employee_id,
        decided_by_employee_id=run.employee_id,
        question=f"Choose the next bounded internal action for Operation #{operation.id}",
        decision=str(action),
        rationale=decision_payload.get("reason") or result.get("reason"),
        state="COMMITTED",
        authority_basis="Founder-approved Project/Operation management authority",
        source_execution_id=run.id,
        committed_at=now(),
    )
    db.session.add(row)
    db.session.flush()
    __import__("eason_one.services.company_events", fromlist=["emit"]).emit(
        "DECISION_COMMITTED",
        actor_type="EMPLOYEE",
        actor_id=run.employee_id,
        project_id=operation.project_id,
        work_id=getattr(management_work, "id", None),
        execution_id=run.id,
        decision_id=row.id,
        correlation_id=(f"work:{management_work.id}" if management_work else f"project:{operation.project_id}"),
        payload={"action": action, "reason": row.rationale},
    )
    return row



def _apply_management_decision(operation, run, decision, blocked_task=None):
    """Apply one persisted CEO decision idempotently inside approved authority.

    Recovery calls this with the *same* successful AgentRun.  Side effects that
    can create durable rows use ``source_execution_id`` or an existing audit row
    as their idempotency key, so a process crash never requires a second CEO
    provider call merely to finish an internal commit.
    """
    ceo = db.session.get(Employee, operation.proposed_by_employee_id)
    action = decision["action"]
    persist_decision = True
    management_work = __import__(
        "eason_one.services.work_runtime", fromlist=["ensure_management_work"]
    ).ensure_management_work(operation)

    if action == "MEETING":
        if normalize_meeting_policy(
            operation.plan_json["operation"]["meeting_policy"]
        ) == "NEVER":
            raise ValueError("Approved Meeting policy forbids a CEO Meeting")
        if not blocked_task:
            blocked_task = next(
                (item for item in operation.tasks if item.status == "BLOCKED"),
                None,
            )
        if not blocked_task:
            raise ValueError("MEETING requires related blocked Work/Task context")
        details = decision.get("meeting") or {}
        meeting = create_meeting_for_conflict(
            operation, blocked_task,
            details.get("question") or decision["reason"],
            Decimal(str(details.get("budget_twd") or "0.5")),
            source_execution_id=run.id,
        )
        result = {
            "kind": "DECISION", "action": action,
            "meeting_id": meeting.id, "agent_run_id": run.id,
        }
    elif action == "HIRING_REQUEST":
        from .workforce import request_hire
        need = decision.get("hiring_need") or {}
        request = request_hire(
            requester=ceo, requested_by_type="EMPLOYEE",
            operation=operation, project=operation.project,
            source_execution_id=run.id, **need,
        )
        result = {
            "kind": "DECISION", "action": action,
            "hiring_request_id": request.id, "agent_run_id": run.id,
        }
    elif action == "FOUNDER":
        # A model may recommend talking to the Founder, but recommendation is
        # not authority. Canonical Founder gates are opened only by deterministic
        # Governance with an exact typed delta/action. A generic CEO request is
        # therefore Company recovery work, not Founder orchestration.
        persist_decision = False
        reason = (
            decision.get("founder_request") or decision["reason"]
            or "CEO proposed generic Founder attention without a canonical authority delta."
        )
        pause_for_internal_runtime_recovery(
            operation,
            "CEO generic Founder action was rejected by Governance: " + str(reason),
            work=management_work,
            condition_type="INTERNAL_RECOVERY",
        )
        result = {
            "kind": "DECISION",
            "action": "FOUNDER_REJECTED_INTERNAL",
            "requested_action": "FOUNDER",
            "status": "WAITING_INTERNAL",
            "work_id": management_work.id,
            "agent_run_id": run.id,
            "founder_action_required": False,
        }
    elif action == "CONTINUE":
        target = db.session.get(Task, decision.get("next_task_id")) if (
            decision.get("next_task_id")
        ) else blocked_task
        if target and target.status == "BLOCKED":
            target.status = "ASSIGNED"
        if target:
            work_runtime = __import__(
                "eason_one.services.work_runtime",
                fromlist=["work_for_task", "resolve_waits"],
            )
            target_work = work_runtime.work_for_task(target)
            if target_work:
                work_runtime.resolve_waits(
                    target_work, "INTERNAL_RECOVERY",
                    note="CEO chose a bounded internal continuation strategy.",
                )
        result = {
            "kind": "DECISION", "action": action,
            "next_task_id": getattr(target, "id", None),
            "agent_run_id": run.id,
        }
    elif action == "CREATE_TASK":
        delivery_count = sum(
            1 for item in operation.works if item.work_type != "MANAGEMENT"
        )
        if delivery_count >= 20:
            persist_decision = False
            result = {
                "kind": "DECISION", "action": "CREATE_TASK_REJECTED",
                "requested_action": "CREATE_TASK",
                "reason": (
                    "The deterministic delivery-Work ceiling is reached. "
                    "CEO must reassign, continue, or explicitly request Founder authority."
                ),
                "agent_run_id": run.id,
            }
        else:
            task = create_remediation_task(
                operation, decision.get("task_plan") or {},
                agent_run_id=run.id,
            )
            result = {
                "kind": "DECISION", "action": action,
                "task_id": task.id, "work_id": task.work_id,
                "agent_run_id": run.id,
            }
    elif action == "REASSIGN_TASK":
        plan = decision.get("task_plan") or {}
        task = db.session.get(Task, plan.get("task_id"))
        if not task:
            raise ValueError("REASSIGN_TASK references an unknown Task")
        assignee = _active_employee(
            plan.get("assignee_employee_id"), "Assignee"
        )
        reviewer = _optional_employee(
            plan.get("reviewer_employee_id"), "Reviewer"
        )
        reassign_task(
            operation, task, assignee, reviewer,
            plan.get("reason") or decision["reason"],
            agent_run_id=run.id,
        )
        result = {
            "kind": "DECISION", "action": action,
            "task_id": task.id, "assignee_employee_id": assignee.id,
            "reviewer_employee_id": getattr(reviewer, "id", None),
            "agent_run_id": run.id,
        }
    elif action == "COMPLETE":
        allowed, reasons = completion_guard(operation)
        if not allowed:
            raise ValueError(
                "CEO cannot complete operation: " + "; ".join(reasons)
            )
        verification_step, _ = _current_goal_verification(operation)
        verification = (
            (verification_step.result_json or {}).get("verification")
            if verification_step else None
        )
        if (
            not verification
            or verification.get("overall_status") != "SATISFIED"
        ):
            persist_decision = False
            result = {
                "kind": "DECISION", "action": "COMPLETE_REJECTED",
                "requested_action": "COMPLETE",
                "reason": (
                    "Current Goal Verification is not SATISFIED; "
                    "the Operation remains in remediation."
                ),
                "agent_run_id": run.id,
            }
        else:
            result = {
                "kind": "DECISION", "action": action,
                "agent_run_id": run.id,
            }
    else:
        raise ValueError("Unknown CEO decision action")

    if persist_decision:
        memory = dict(operation.memory_json or {})
        decisions = list(memory.get("decisions") or [])
        if decision not in decisions:
            decisions.append(decision)
            memory["decisions"] = decisions
            operation.memory_json = memory
        _persist_management_decision(
            operation, run, decision, result, management_work
        )
    db.session.commit()
    return result

def _decision_step(operation, request_key, blocked_task=None):
    _assert_operation_project_executable(operation, action="DECISION")
    from .ceo_context import compose
    iteration = OperationStep.query.filter_by(
        operation_id=operation.id, kind="DECISION"
    ).count() + 1
    logical = _logical_key(operation, "DECISION", subject=iteration)
    step, created = _claim(
        operation, request_key, logical, "DECISION", blocked_task
    )
    if not created and not _claimed_step_is_safe_to_resume(step):
        return _existing_result(step)
    ceo = db.session.get(Employee, operation.proposed_by_employee_id)
    composed = compose(
        ceo, founder_request="Choose the next internal operation action.",
        operation=operation, project=operation.project,
    )
    _provider_boundary(step)
    run = None
    management_work = __import__(
        "eason_one.services.work_runtime", fromlist=["ensure_management_work"]
    ).ensure_management_work(operation)
    base_decision_prompt = (
        ceo.system_instructions
        + "\nCEO_OPERATION_DECISION\nReturn one strict bounded internal decision. "
        "Normal planning, staffing, retry, provider failure, review failure, Meeting failure, handoff, and recovery are Company work. "
        "Do not use FOUNDER as a generic recovery action. In governed Projects only deterministic Governance may open an exact Founder authority gate; "
        "choose CONTINUE, CREATE_TASK, REASSIGN_TASK, HIRING_REQUEST, or MEETING when those bounded actions can resolve the issue inside current authority."
    )

    def _execute_decision(*, model_override=None, retry_of_run=None, prompt=None, prompt_version="ceo-operation-decision-v2", recovery=None):
        composition = dict(composed.composition or {})
        if recovery:
            composition["company_recovery"] = dict(recovery)
        selected_model = model_override or ceo.current_model
        return execute(
            ceo, "CEO_OPERATION_DECISION",
            f"Decide operation #{operation.id} next action",
            project=operation.project, operation=operation, work=management_work,
            context_override=composed.text,
            context_composition=composition,
            system_prompt_override=prompt or base_decision_prompt,
            response_schema=CEO_DECISION_SCHEMA,
            # This decision can legally carry a hiring proposal or bounded Task
            # plan.  The old 900-token magic ceiling could turn normal internal
            # management into Founder-visible failure.  Use the selected model's
            # configured envelope and one bounded compact retry instead.
            max_output_tokens_override=int(selected_model.max_output_tokens),
            model_override=model_override,
            retry_of_run=retry_of_run,
            prompt_version=prompt_version,
        )

    try:
        run = _execute_decision()
        if run.status != "SUCCEEDED" and run.failure_reason == "OUTPUT_TRUNCATED":
            prior = run
            run = _execute_decision(
                retry_of_run=prior,
                prompt=base_decision_prompt + (
                    "\nCEO_OPERATION_DECISION_TRUNCATION_RECOVERY\n"
                    "The previous internal decision hit the configured output ceiling. Return the same complete decision compactly. "
                    "Use one short reason, the minimum sufficient arrays, and no repeated context. Preserve every required schema field."
                ),
                prompt_version="ceo-operation-decision-v2-truncation-recovery",
                recovery={
                    "reason": "OUTPUT_TRUNCATED",
                    "prior_run_id": prior.id,
                    "policy": "ONE_SAME_PROVIDER_COMPACT_RETRY",
                    "max_attempts": 2,
                },
            )
        step.agent_run_id = run.id
        db.session.commit()
        if run.status != "SUCCEEDED":
            # One safe management failure gets an alternate-intelligence retry
            # inside the approved Project authority. It is company recovery,
            # not Founder work.
            if run.outcome in {"FAILED_SAFE", "FAILED_KNOWN"}:
                alternate = __import__(
                    "eason_one.services.execution_policy", fromlist=["select_retry_model"]
                ).select_retry_model(ceo, run, operation, "CEO_OPERATION_DECISION")
                if alternate is not None:
                    __import__("eason_one.services.company_events", fromlist=["emit"]).emit(
                        "EXECUTION_RETRIED",
                        actor_type="RUNTIME",
                        project_id=operation.project_id,
                        work_id=management_work.id,
                        execution_id=run.id,
                        correlation_id=f"work:{management_work.id}",
                        payload={
                            "failed_execution_id": run.id,
                            "next_provider": alternate.provider_key,
                            "next_model": alternate.model_name,
                            "purpose": "CEO_OPERATION_DECISION",
                        },
                    )
                    db.session.commit()
                    run = _execute_decision(model_override=alternate, retry_of_run=run)
                    step.agent_run_id = run.id
                    db.session.commit()
            if run.status != "SUCCEEDED":
                error = run.error_text or "CEO management execution failed safely."
                additional = _budget_extension_from_run(run)
                if additional is not None:
                    wait_for_founder(
                        operation,
                        "Company management exhausted bounded internal recovery and requires additional Project budget authority.",
                        additional_budget=additional,
                        decision_kind="BUDGET_AUTHORIZATION",
                    )
                    step.status = "BLOCKED"
                    step.error_text = error
                    step.finished_at = now()
                    db.session.commit()
                    return {
                        "kind": "DECISION",
                        "status": "WORK_WAITING_FOUNDER",
                        "work_id": management_work.id,
                        "agent_run_id": run.id,
                        "execution_outcome": run.outcome,
                        "failure_reason": run.failure_reason,
                    }
                _mark_paid_or_ambiguous(operation, step, run, ValueError(error))
                # _mark_paid_or_ambiguous records the Work/reconciliation truth.
                # Do not rethrow into the durable runtime's legacy Founder trap.
                if __import__(
                    "eason_one.services.operation_kernel", fromlist=["authoritative_status"]
                ).authoritative_status(operation) == "RUNNING":
                    pause_for_internal_runtime_recovery(
                        operation,
                        error,
                        work=management_work,
                        condition_type=(
                            "RECONCILIATION"
                            if run.outcome == "FAILED_AMBIGUOUS"
                            else "INTERNAL_RECOVERY"
                        ),
                    )
                return {
                    "kind": "DECISION",
                    "status": "WAITING_INTERNAL",
                    "work_id": management_work.id,
                    "agent_run_id": run.id,
                    "execution_outcome": run.outcome,
                    "failure_reason": run.failure_reason,
                }
        decision = json.loads(run.raw_output)
        run.parsed_output_json = decision
        db.session.commit()
        result = _apply_management_decision(
            operation, run, decision, blocked_task=blocked_task
        )
        return _finish(step, run, result)
    except Exception as exc:
        run = run or AgentRun.query.filter_by(
            operation_id=operation.id, purpose="CEO_OPERATION_DECISION"
        ).order_by(AgentRun.id.desc()).first()
        _mark_paid_or_ambiguous(operation, step, run, exc)
        raise


def _reconcile_hiring_founder_review(operation, request):
    """Keep normal vNext staffing inside Company authority.

    A management-originated HIRE inside a live governed Project carries zero new
    budget authority and is delegated to HR/Company management. Older/stale rows
    may still say FOUNDER_REVIEW; reconcile those deterministically instead of
    turning normal staffing into Founder work. Historical non-vNext Operations
    retain their original explicit approval policy.
    """
    project = getattr(operation, "project", None)
    contracts = __import__(
        "eason_one.services.project_contract", fromlist=["is_vnext_governed"]
    )
    if not project or not contracts.is_vnext_governed(project):
        return None
    workforce = __import__(
        "eason_one.services.workforce",
        fromlist=["commit_delegated_hire", "source_work_for_request"],
    )
    try:
        employee = workforce.commit_delegated_hire(request)
        return {
            "status": "HIRED",
            "employee_id": getattr(employee, "id", None),
            "founder_action_required": False,
        }
    except ValueError as exc:
        source_work = workforce.source_work_for_request(request)
        pause_for_internal_runtime_recovery(
            operation,
            "Governed staffing could not prove delegated hiring authority; Company management must reconcile the capability gap without inventing Founder approval: "
            + str(exc),
            work=source_work,
            condition_type="RECONCILIATION",
        )
        return {
            "status": "WAITING_INTERNAL",
            "error": str(exc),
            "founder_action_required": False,
        }


def _hr_step(operation, request, request_key):
    _assert_operation_project_executable(operation, action="HR")
    from .workforce import assess_request
    logical = _logical_key(
        operation, "HR_ASSESSMENT", subject=request.id
    )
    step, created = _claim(
        operation, request_key, logical, "HR_ASSESSMENT"
    )
    if not created and not _claimed_step_is_safe_to_resume(step):
        return _existing_result(step)
    _provider_boundary(step)
    try:
        run = assess_request(request)
        step.agent_run_id = run.id
        if request.status == "FOUNDER_REVIEW":
            reconciled = _reconcile_hiring_founder_review(operation, request)
            if reconciled is None:
                wait_for_founder(
                    operation,
                    f"HR recommendation for {request.role_needed} requires Founder review.",
                )
            else:
                return _finish(step, run, {
                    "kind": "HR_ASSESSMENT",
                    "status": reconciled["status"],
                    "hiring_request_id": request.id,
                    "agent_run_id": run.id,
                    **{key: value for key, value in reconciled.items() if key != "status"},
                })
        return _finish(step, run, {
            "kind": "HR_ASSESSMENT", "status": request.status,
            "hiring_request_id": request.id, "agent_run_id": run.id,
        })
    except Exception as exc:
        run = AgentRun.query.filter_by(
            operation_id=operation.id, purpose="HR_ASSESSMENT"
        ).order_by(AgentRun.id.desc()).first()
        _mark_paid_or_ambiguous(operation, step, run, exc)
        raise


def _goal_verification_step(operation, request_key):
    _assert_operation_project_executable(operation, action="GOAL_VERIFICATION")
    allowed, reasons = completion_guard(operation)
    if not allowed:
        raise ValueError(
            "Workflow is not ready for Goal Verification: " + "; ".join(reasons)
        )
    packet, serialized, digest = goal_evidence_packet(operation)
    logical = f"goal_verification:{digest}"
    step, created = _claim(
        operation, request_key, logical, "GOAL_VERIFICATION"
    )
    if not created:
        if _claimed_step_is_safe_to_resume(step):
            pass
        elif step.status in OPEN_STEP:
            return _recover_open_step(operation)
        else:
            return _existing_result(step)
    ceo = db.session.get(Employee, operation.proposed_by_employee_id)
    _provider_boundary(step)
    run = None
    try:
        management_work = __import__(
            "eason_one.services.work_runtime", fromlist=["ensure_management_work"]
        ).ensure_management_work(operation)
        base_prompt = (
            ceo.system_instructions
            + "\nGOAL_VERIFICATION\nIndependently assess whether the "
            "Founder objective and every completion criterion are supported "
            "by the supplied persisted evidence. Return only strict JSON."
        )
        base_composition = {
            "goal_verification_evidence": {
                "characters": len(serialized),
                "budget": GOAL_EVIDENCE_BUDGET,
                "completed_tasks": len(packet["completed_tasks"]),
                "latest_reviews": len(packet["latest_reviews"]),
                "meeting_results": len(packet["meeting_results"]),
                "brain_items": len(packet["company_brain_evidence"]),
                "evidence_digest": digest,
            },
        }

        def _execute_goal_verification(*, prompt, prompt_version, retry_of_run=None, recovery=None):
            composition = dict(base_composition)
            if recovery:
                composition["company_recovery"] = dict(recovery)
            return execute(
                ceo, "GOAL_VERIFICATION",
                f"Verify achievement of Operation #{operation.id}",
                project=operation.project, operation=operation, work=management_work,
                context_override="GOAL VERIFICATION EVIDENCE\n" + serialized,
                context_composition=composition,
                system_prompt_override=prompt,
                response_schema=GOAL_VERIFICATION_SCHEMA,
                # Goal verification is authority-bearing structured output.  Do
                # not clip the configured model envelope with an old magic cap;
                # bounded Company recovery handles the rare truncation instead.
                max_output_tokens_override=int(ceo.current_model.max_output_tokens),
                prompt_version=prompt_version,
                retry_of_run=retry_of_run,
            )

        run = _execute_goal_verification(
            prompt=base_prompt,
            prompt_version="goal-verification-v2",
        )
        if run.status != "SUCCEEDED" and run.failure_reason == "OUTPUT_TRUNCATED":
            prior = run
            run = _execute_goal_verification(
                prompt=base_prompt + (
                    "\nGOAL_VERIFICATION_TRUNCATION_RECOVERY\n"
                    "The previous verification hit the configured output ceiling. Return the same complete criterion coverage compactly. "
                    "Use the shortest sufficient evidence references and one short reason per criterion. Preserve every criterion, status, "
                    "overall_status, summary, and recommended_action. Do not repeat source prose."
                ),
                prompt_version="goal-verification-v2-truncation-recovery",
                retry_of_run=prior,
                recovery={
                    "reason": "OUTPUT_TRUNCATED",
                    "prior_run_id": prior.id,
                    "policy": "ONE_SAME_PROVIDER_COMPACT_RETRY",
                    "max_attempts": 2,
                },
            )
        step.agent_run_id = run.id
        db.session.commit()
        if run.status != "SUCCEEDED":
            raise ValueError(run.error_text or "Goal Verification failed")
        payload = _validate_goal_verification(
            json.loads(run.raw_output), packet["completion_criteria"]
        )
        run.parsed_output_json = payload
        _persist_goal_verification(operation, payload, digest)
        return _finish(step, run, {
            "kind": "GOAL_VERIFICATION",
            "overall_status": payload["overall_status"],
            "verification": payload,
            "evidence_digest": digest,
            "agent_run_id": run.id,
        })
    except Exception as exc:
        run = run or AgentRun.query.filter_by(
            operation_id=operation.id, purpose="GOAL_VERIFICATION"
        ).order_by(AgentRun.id.desc()).first()
        _mark_paid_or_ambiguous(operation, step, run, exc)
        raise


def _materialize_report_run(operation, run, verification):
    """Idempotently persist the final company outcome from a successful report Execution."""
    payload = run.parsed_output_json or json.loads(run.raw_output or "{}")
    run.parsed_output_json = payload
    management_work = __import__(
        "eason_one.services.work_runtime", fromlist=["ensure_management_work"]
    ).ensure_management_work(operation)
    artifact_service = __import__(
        "eason_one.services.artifacts",
        fromlist=["submit_from_execution", "verify_and_accept"],
    )
    version = artifact_service.submit_from_execution(
        management_work, run, artifact_type="FOUNDER_REPORT",
        title=f"Founder outcome: {operation.title}", content_text=run.raw_output,
    )
    artifact_service.verify_and_accept(
        management_work, version, method="GOAL_VERIFICATION",
        verifier_employee_id=run.employee_id, agent_run_id=run.id,
        details={
            "goal_verification": verification,
            "reason": "SATISFIED Goal Verification plus persisted Founder report.",
        },
    )
    models = __import__("eason_one.models", fromlist=["CompanyEvent"])
    if not models.CompanyEvent.query.filter_by(
        event_type="PROJECT_OUTCOME_READY", execution_id=run.id
    ).first():
        __import__("eason_one.services.company_events", fromlist=["emit"]).emit(
            "PROJECT_OUTCOME_READY",
            actor_type="EMPLOYEE", actor_id=run.employee_id,
            project_id=operation.project_id, work_id=management_work.id,
            execution_id=run.id, artifact_id=version.artifact_id,
            correlation_id=f"project:{operation.project_id}",
            payload={"operation_id": operation.id, "verification": "SATISFIED"},
        )
    kernel = __import__(
        "eason_one.services.operation_kernel", fromlist=["transition", "authoritative_status"]
    )
    current_status = kernel.authoritative_status(operation)
    if current_status == "QUEUED":
        kernel.transition(
            operation, "RUNNING", "RECOVERY_MATERIALIZATION_RESUMED",
            stage="RECOVERY", actor_type="RUNTIME", commit=False,
        )
        current_status = "RUNNING"
    if current_status != "COMPLETED":
        kernel.transition(
            operation, "COMPLETED", "FOUNDER_REPORT_COMPLETED",
            stage="COMPLETED", commit=False,
        )
    operation.founder_report_json = {
        "headline": "Boss, it's complete.",
        "summary": payload["executive_summary"],
        "result": payload["result"],
        "next_move": (payload["recommended_next_actions"] or [
            "Review the completed result."
        ])[0],
        "cost_twd": str(actual_cost(operation)),
        "authorized_twd": str(operation.approved_budget_twd),
        "artifact_version_id": version.id,
        "verification": "SATISFIED",
    }
    operation.project.status = "REVIEW"
    operation.project.current_state_summary = payload["executive_summary"]
    if not WorkMessage.query.filter_by(
        agent_run_id=run.id, message_type="CEO_TO_FOUNDER"
    ).first():
        db.session.add(WorkMessage(
            project_id=operation.project_id,
            sender_employee_id=operation.proposed_by_employee_id,
            agent_run_id=run.id,
            message_type="CEO_TO_FOUNDER",
            content=payload["executive_summary"] + "\n\nResult: " + payload["result"],
        ))
    return {
        "kind": "REPORT", "status": "COMPLETED",
        "agent_run_id": run.id, "artifact_version_id": version.id,
        "work_id": management_work.id,
    }


def _report_step(operation, request_key):
    _assert_operation_project_executable(operation, action="REPORT")
    from .ceo_context import compose
    allowed, reasons = completion_guard(operation)
    if not allowed:
        return _decision_step(
            operation, request_key,
            next((task for task in operation.tasks if task.status == "BLOCKED"),
                 None),
        )
    verification_step, _ = _current_goal_verification(operation)
    verification = (
        (verification_step.result_json or {}).get("verification")
        if verification_step else None
    )
    if not verification or verification.get("overall_status") != "SATISFIED":
        raise ValueError(
            "Operation cannot complete without a current SATISFIED "
            "Goal Verification"
        )
    logical = _logical_key(operation, "REPORT", subject="final")
    step, created = _claim(
        operation, request_key, logical, "REPORT"
    )
    if not created:
        if _claimed_step_is_safe_to_resume(step):
            pass
        elif step.status in OPEN_STEP:
            return _recover_open_step(operation)
        else:
            return _existing_result(step)
    ceo = db.session.get(Employee, operation.proposed_by_employee_id)
    composed = compose(
        ceo, founder_request="Produce the final Founder operation report.",
        operation=operation, project=operation.project,
    )
    _provider_boundary(step)
    run = None
    try:
        management_work = __import__(
            "eason_one.services.work_runtime", fromlist=["ensure_management_work"]
        ).ensure_management_work(operation)
        run = execute(
            ceo, "CEO_OPERATION_REPORT",
            f"Report completion of operation #{operation.id}",
            project=operation.project, operation=operation, work=management_work,
            context_override=composed.text,
            context_composition=composed.composition,
            system_prompt_override=(
                ceo.system_instructions
                + "\nCEO_PROJECT_SYNTHESIS\nReturn only strict JSON briefing fields."
            ),
            response_schema=SYNTHESIS_SCHEMA,
        )
        step.agent_run_id = run.id
        db.session.commit()
        if run.status != "SUCCEEDED":
            raise ValueError(run.error_text or "CEO report failed")
        result = _materialize_report_run(operation, run, verification)
        return _finish(step, run, result)
    except Exception as exc:
        run = run or AgentRun.query.filter_by(
            operation_id=operation.id, purpose="CEO_OPERATION_REPORT"
        ).order_by(AgentRun.id.desc()).first()
        _mark_paid_or_ambiguous(operation, step, run, exc)
        raise


def _material_conflict_detected(operation):
    """Deterministic Meeting gate from persisted execution evidence."""
    if any(task.status in {"BLOCKED", "FAILED"} for task in operation.tasks):
        return True
    review_runs = AgentRun.query.filter_by(
        operation_id=operation.id, purpose="TASK_REVIEW", status="SUCCEEDED"
    ).order_by(AgentRun.id).all()
    for run in review_runs:
        decision = (run.parsed_output_json or {}).get("decision")
        if decision in {"REVISE", "BLOCK"}:
            return True
    decisions = (operation.memory_json or {}).get("decisions") or []
    return any(item.get("action") in {"MEETING", "REASSIGN_TASK"} for item in decisions)


def _meeting_required_before_final(operation):
    config = (
        (operation.plan_json or {}).get("operation", {}).get("meeting_config")
        or default_meeting_config((operation.plan_json or {}).get("operation") or {})
    )
    if config.get("trigger") != "BEFORE_FINAL_REPORT":
        return False
    existing = Meeting.query.filter_by(operation_id=operation.id).first()
    return existing is None


def _start_planned_final_meeting(operation):
    config = (
        (operation.plan_json or {}).get("operation", {}).get("meeting_config")
        or default_meeting_config((operation.plan_json or {}).get("operation") or {})
    )
    reason = (
        "Reconcile the completed specialist work, challenge unsupported claims, "
        "and agree on the best Founder-facing answer for the approved objective: "
        + operation.objective
    )
    meeting = create_meeting(
        operation, reason,
        budget_twd=Decimal(str(config.get("budget_twd") or "0.5")),
    )
    meeting_service.start_auto(meeting)
    return meeting

def reconcile_completed_founder_attention(operation, *, commit=True):
    """Resolve stale Founder gates that cannot remain actionable after completion.

    A completed Operation is authoritative evidence that execution moved past any
    earlier readiness/budget/recovery gate. Historical gate records stay in memory
    for audit, but are marked RESOLVED so Founder-facing read models and Project
    completion guards do not keep treating superseded recovery requests as live
    authority requirements.
    """
    if operation.status != "COMPLETED":
        return False
    memory = dict(operation.memory_json or {})
    events = list(memory.get("founder_attention_events") or [])
    changed = False
    for index, event in enumerate(events):
        if event.get("status") != "PENDING":
            continue
        events[index] = dict(
            event,
            status="RESOLVED",
            resolution="SUPERSEDED_BY_COMPLETED_OPERATION",
            resolution_reason=(
                "The Operation subsequently completed, so this earlier Founder "
                "gate is historical audit evidence rather than a live decision."
            ),
        )
        changed = True
    if not changed:
        return False
    memory["founder_attention_events"] = events
    operation.memory_json = memory
    operation.waiting_reason = None
    if commit:
        db.session.commit()
    return True


def _codex_run_has_independent_acceptance(run):
    """Return True only when persisted host evidence independently verifies a write job."""
    context = dict(run.context_composition_json or {})
    if context.get("read_only"):
        # Read-only engineering work has no repository delta to prove. Its
        # structured acceptance remains review evidence rather than a write claim.
        return run.structured_validation_status == "PASSED"
    host = dict(context.get("host_validation") or {})
    return bool(
        host.get("attempted")
        and host.get("success")
        and host.get("repository_delta_match") is True
    )


def _deterministic_engineering_delivery(operation, request_key):
    _assert_operation_project_executable(operation, action="ENGINEERING_DELIVERY")
    """Close a Codex-only Mission only from independently verified host evidence."""
    logical = "engineering_delivery:v1"
    step, created = _claim(operation, request_key, logical, "REPORT")
    if not created and not _claimed_step_is_safe_to_resume(step):
        return _existing_result(step)
    evidence = []
    for task in operation.tasks:
        run = AgentRun.query.filter_by(
            operation_id=operation.id, task_id=task.id,
            provider_key_snapshot="codex", status="SUCCEEDED",
        ).order_by(AgentRun.id.desc()).first()
        if task.status != "DONE" or not run or run.structured_validation_status != "PASSED":
            step.status = "BLOCKED"
            step.error_text = f"Task #{task.id} lacks validated Codex evidence"
            work = __import__(
                "eason_one.services.work_runtime", fromlist=["work_for_task"]
            ).work_for_task(task)
            if work:
                __import__(
                    "eason_one.services.work_runtime", fromlist=["open_wait"]
                ).open_wait(
                    work, "INTERNAL_RECOVERY",
                    "Engineering delivery is not yet claimable because validated Codex evidence is missing. Company runtime owns retry/replan; Founder action is not required.",
                    issue_code=f"ENGINEERING_EVIDENCE_MISSING_TASK_{task.id}",
                )
            db.session.commit()
            return {
                "kind": "REPORT", "status": "WAITING_INTERNAL", "task_id": task.id,
                "work_id": getattr(work, "id", None), "founder_action_required": False,
            }
        if not _codex_run_has_independent_acceptance(run):
            step.status = "BLOCKED"
            step.error_text = f"Task #{task.id} lacks independent host acceptance evidence"
            work = __import__(
                "eason_one.services.work_runtime", fromlist=["work_for_task"]
            ).work_for_task(task)
            if work:
                __import__(
                    "eason_one.services.work_runtime", fromlist=["open_wait"]
                ).open_wait(
                    work, "RECONCILIATION",
                    "Engineering delivery lacks independent host acceptance for the exact Artifact/repository delta. Company runtime must repair/reconcile this proof before Result Ready; Founder action is not required.",
                    issue_code=f"ENGINEERING_HOST_ACCEPTANCE_MISSING_TASK_{task.id}",
                )
            db.session.commit()
            return {
                "kind": "REPORT", "status": "RECONCILIATION_REQUIRED", "task_id": task.id,
                "work_id": getattr(work, "id", None), "founder_action_required": False,
            }
        payload = (run.parsed_output_json or {}).get("codex") or {}
        host_validation = dict((run.context_composition_json or {}).get("host_validation") or {})
        evidence.append({
            "task_id": task.id, "run_id": run.id,
            "summary": task.result_summary,
            "changed_files": payload.get("changed_files") or [],
            "tests": payload.get("tests") or [],
            "acceptance": payload.get("acceptance") or [],
            "risks": payload.get("risks") or [],
            "host_validation": host_validation,
            "repository_delta": (run.context_composition_json or {}).get("repository_delta") or [],
        })
    memory = dict(operation.memory_json or {})
    criteria = operation.plan_json["operation"].get("completion_criteria") or []
    memory["goal_verification"] = {
        "overall_status": "SATISFIED",
        "criteria": [{
            "criterion": criterion, "status": "SATISFIED",
            "evidence": [f"Validated Codex Run #{row['run_id']}" for row in evidence],
            "reason": "All bounded Engineering Tasks completed with persisted Codex diff/test evidence.",
        } for criterion in criteria],
        "summary": "Engineering acceptance was verified from persisted Codex evidence plus independent host repository-delta and deterministic acceptance checks.",
        "recommended_action": "Deliver the recorded engineering result to the Founder.",
        "verification_mode": "DETERMINISTIC_CODEX_EVIDENCE",
    }
    operation.memory_json = memory
    __import__("eason_one.services.operation_kernel",fromlist=["transition"]).transition(
        operation,"COMPLETED","ENGINEERING_DELIVERY_VERIFIED",stage="COMPLETED",commit=False
    )
    operation.founder_report_json = {
        "decision_kind": "DELIVERY",
        "headline": "Engineering Mission completed.",
        "summary": "The Engineer completed the bounded Codex workflow and independent host acceptance verification passed.",
        "next_move": "Founder reviews the changed files, tests, risks, and final result.",
        "engineering_evidence": evidence,
        "actual_twd": str(actual_cost(operation)),
    }
    if operation.project:
        operation.project.status = "REVIEW"
        operation.project.current_state_summary = "Engineering delivery is ready for Founder acceptance."
    reconcile_completed_founder_attention(operation, commit=False)
    db.session.commit()
    return _finish(step, None, {
        "kind": "REPORT", "status": "COMPLETED",
        "verification_mode": "DETERMINISTIC_CODEX_EVIDENCE",
        "task_count": len(evidence),
    })


def _run_orchestration_step(operation, request_key):
    _assert_operation_project_executable(operation, action="ORCHESTRATION")
    multi = __import__(
        "eason_one.services.multi_agent",
        fromlist=["POLICY_VERSION", "call_orchestrator", "conservative_fallback", "current_plan", "persist_plan", "validate_payload"],
    )
    existing_plan = multi.current_plan(operation)
    if existing_plan:
        return {
            "kind": "ORCHESTRATION", "status": "EXISTS",
            "strategy": existing_plan.get("strategy"),
            "max_parallelism": existing_plan.get("max_parallelism"),
        }
    logical = f"orchestration:{multi.POLICY_VERSION}"
    step, created = _claim(operation, request_key, logical, "ORCHESTRATION")
    if not created:
        if _claimed_step_is_safe_to_resume(step):
            pass
        elif step.status in OPEN_STEP:
            return _recover_open_step(operation)
        else:
            return _existing_result(step)
    _provider_boundary(step)
    run = None
    error = None
    try:
        run = multi.call_orchestrator(operation)
        step.agent_run_id = run.id
        db.session.commit()
        if run.status == "SUCCEEDED" and run.parsed_output_json:
            plan = multi.validate_payload(operation, run.parsed_output_json)
            multi.persist_plan(operation, plan, planner_run_id=run.id)
        else:
            error = run.error_text or run.failure_reason or "Orchestration model did not return a validated plan."
            plan = multi.conservative_fallback(operation, error)
            multi.persist_plan(operation, plan, planner_run_id=None)
    except Exception as exc:
        db.session.rollback()
        operation = db.session.get(Operation, operation.id)
        run = AgentRun.query.filter_by(
            operation_id=operation.id, purpose="ORCHESTRATION_PLAN"
        ).order_by(AgentRun.id.desc()).first()
        step = db.session.get(OperationStep, step.id)
        if step and run:
            step.agent_run_id = run.id
        error = str(exc)
        plan = multi.conservative_fallback(operation, error)
        multi.persist_plan(operation, plan, planner_run_id=None)
    return _finish(step, run, {
        "kind": "ORCHESTRATION",
        "status": "PLANNED" if not plan.get("fallback") else "CONSERVATIVE_FALLBACK",
        "strategy": plan.get("strategy"),
        "max_parallelism": plan.get("max_parallelism"),
        "agent_run_id": getattr(run, "id", None),
        "error": error,
    })



def next_step(operation, idempotency_key):
    # Terminal Project lifecycle outranks runtime ownership. A v0.18 Operation
    # may still carry a local pre-provider claim or an already-dispatched
    # provider boundary after completion/cancellation; reconcile that history
    # here before Company Runtime can wake or schedule anything new.
    terminal_status = _terminal_project_status(operation)
    if terminal_status:
        existing = OperationStep.query.filter_by(
            operation_id=operation.id, idempotency_key=idempotency_key
        ).first()
        if existing:
            if existing.status in OPEN_STEP or existing.status == "CLAIMED":
                return _recover_open_step(operation)
            return _existing_result(existing)
        raise ValueError(f"PROJECT_TERMINAL_OPERATION_NEXT_STEP_FORBIDDEN:{terminal_status}")

    core = __import__(
        "eason_one.services.core_v018",
        fromlist=["is_v018_operation", "is_dormant_legacy_project_operation"],
    )
    if core.is_v018_operation(operation):
        runtime = __import__(
            "eason_one.services.company_runtime",
            fromlist=["wake_company_runtime", "runtime_snapshot"],
        )
        runtime.wake_company_runtime()
        return {
            "kind": "WORK_RUNTIME",
            "status": "COMPANY_RUNTIME_OWNS_EXECUTION",
            "runtime": runtime.runtime_snapshot(operation),
        }
    if core.is_dormant_legacy_project_operation(operation):
        raise ValueError(
            "This pre-v0.18 Project Operation is historical and cannot execute through the legacy runtime."
        )
    existing = OperationStep.query.filter_by(
        operation_id=operation.id, idempotency_key=idempotency_key
    ).first()
    if existing:
        if existing.status in OPEN_STEP:
            return _recover_open_step(operation)
        if _claimed_step_is_safe_to_resume(existing):
            if existing.kind == "TASK" and existing.task_id:
                task = db.session.get(Task, existing.task_id)
                if task is not None:
                    return _run_task_step(operation, idempotency_key, task)
            if existing.kind == "REVIEW" and existing.task_id:
                task = db.session.get(Task, existing.task_id)
                if task is not None:
                    return _run_review_step(operation, idempotency_key, task)
            if existing.kind == "DECISION":
                task = db.session.get(Task, existing.task_id) if existing.task_id else None
                return _decision_step(operation, idempotency_key, task)
            if existing.kind == "HR_ASSESSMENT":
                try:
                    request_id = int(str(existing.logical_key).rsplit(":", 1)[-1])
                except Exception:
                    request_id = 0
                request = db.session.get(HiringRequest, request_id) if request_id else None
                if request is not None:
                    return _hr_step(operation, request, idempotency_key)
            if existing.kind == "GOAL_VERIFICATION":
                return _goal_verification_step(operation, idempotency_key)
            if existing.kind == "REPORT":
                if existing.logical_key == "engineering_delivery:v1":
                    return _deterministic_engineering_delivery(operation, idempotency_key)
                return _report_step(operation, idempotency_key)
            if existing.kind in {"MEETING_RESULT", "MEETING_STEP"}:
                try:
                    meeting_id = int(str(existing.logical_key).split(":", 2)[1])
                except Exception:
                    meeting_id = 0
                meeting = db.session.get(Meeting, meeting_id) if meeting_id else None
                if meeting is not None:
                    if existing.kind == "MEETING_RESULT":
                        return _consume_meeting(operation, meeting, idempotency_key)
                    return _advance_meeting(operation, meeting, idempotency_key)
            if existing.kind == "ORCHESTRATION":
                return _run_orchestration_step(operation, idempotency_key)
        return _existing_result(existing)
    if operation.status != "RUNNING":
        raise ValueError("Operation is not running")
    if not ensure_full_execution_authority(operation):
        raise OperationBudgetRequired(
            operation.approved_budget_twd,
            actual_cost(operation),
            Decimal((operation.founder_report_json or {}).get("additional_budget_twd") or "0.0001"),
        )
    recovered = _recover_open_step(operation)
    if recovered:
        return recovered
    meeting = Meeting.query.filter_by(operation_id=operation.id).order_by(
        Meeting.id.desc()
    ).first()
    if meeting:
        consumed = any(
            (step.result_json or {}).get("meeting_id") == meeting.id
            for step in operation.steps if step.kind == "MEETING_RESULT"
        )
        if meeting.status == "RUNNING":
            return _advance_meeting(operation, meeting, idempotency_key)
        if meeting.status in {"ENDED", "TERMINATED_BY_FOUNDER"} and not consumed:
            return _consume_meeting(operation, meeting, idempotency_key)
        delivery_works = [work for work in operation.works if work.work_type != "MANAGEMENT"]
        delivery_terminal = (
            all(work.state in {"ACCEPTED", "CANCELLED"} for work in delivery_works)
            if delivery_works
            else all(task.status in {"DONE", "CANCELLED"} for task in operation.tasks)
        )
        if meeting.status == "PLANNED" and delivery_terminal:
            config = (
                (operation.plan_json or {}).get("operation", {}).get("meeting_config")
                or default_meeting_config((operation.plan_json or {}).get("operation") or {})
            )
            trigger = config.get("trigger") or "NEVER"
            if trigger == "ON_MATERIAL_CONFLICT" and not _material_conflict_detected(operation):
                meeting_service.skip(
                    meeting,
                    "All approved specialist work completed without a persisted material conflict."
                )
                return {"kind": "MEETING_SKIPPED", "status": "SKIPPED_ZERO_COST", "meeting_id": meeting.id}
            meeting_service.start_auto(meeting)
            return {"kind": "MEETING_STARTED", "status": "RUNNING", "meeting_id": meeting.id}
    hiring = HiringRequest.query.filter_by(
        operation_id=operation.id
    ).order_by(HiringRequest.id.desc()).first()
    if hiring:
        if hiring.status in {"REQUESTED", "HR_REVIEW"}:
            return _hr_step(operation, hiring, idempotency_key)
        if hiring.status == "FOUNDER_REVIEW":
            reconciled = _reconcile_hiring_founder_review(operation, hiring)
            if reconciled is not None:
                return {
                    "kind": "HR_ASSESSMENT",
                    "status": reconciled["status"],
                    "hiring_request_id": hiring.id,
                    **{key: value for key, value in reconciled.items() if key != "status"},
                }
            wait_for_founder(
                operation,
                f"HR recommendation for {hiring.role_needed} requires Founder review.",
            )
            raise ValueError("Founder hiring decision required")
    multi = __import__(
        "eason_one.services.multi_agent",
        fromlist=["blocked_by_dependencies", "current_plan", "enabled", "run_parallel_wave", "select_wave"],
    )
    if multi.enabled(operation):
        if not multi.current_plan(operation):
            return _run_orchestration_step(operation, idempotency_key)

        # Project-scoped Founder authority really changes what the whole company
        # may lawfully do, so it freezes the Project. Execution-scoped Founder
        # exceptions remain branch-local and are handled only after healthy
        # READY/VERIFYING siblings have progressed.
        governance_service = __import__(
            "eason_one.services.governance",
            fromlist=["project_blocking_gate", "blocks_work"],
        )
        project_gate = governance_service.project_blocking_gate(operation.project)
        if project_gate is not None:
            return {
                "kind": "FOUNDER_AUTHORITY",
                "status": "PROJECT_WAITING_FOUNDER",
                "project_id": operation.project_id,
                "escalation_id": project_gate.id,
                "authority_type": project_gate.escalation_type,
            }

        # Reviews are executable company work too. A branch-specific authority
        # gate on some other Work may not stall this independent verification.
        work_runtime = __import__(
            "eason_one.services.work_runtime",
            fromlist=["task_for_work_state", "work_for_task"],
        )
        review_task = work_runtime.task_for_work_state(operation, "VERIFYING")
        if review_task:
            review_work = work_runtime.work_for_task(review_task)
            if not governance_service.blocks_work(review_work):
                return _run_review_step(operation, idempotency_key, review_task)

        # Healthy sibling branches always get first chance to progress. This is
        # the key multi-Employee rule: one WAITING Work cannot become an
        # Operation-wide scheduler mutex unless Governance says the authority is
        # Project-scoped.
        wave = multi.select_wave(operation)
        if len(wave) > 1:
            return multi.run_parallel_wave(operation, wave, idempotency_key)
        if len(wave) == 1:
            return _run_task_step(operation, idempotency_key, wave[0])

        blocked_task = work_runtime.task_for_work_state(operation, "WAITING")
        if blocked_task:
            work = work_runtime.work_for_task(blocked_task)
            waits = [row for row in work.wait_conditions if row.state == "OPEN"] if work else []
            if work and governance_service.blocks_work(work):
                escalation = next((
                    row for row in __import__(
                        "eason_one.models", fromlist=["Escalation"]
                    ).Escalation.query.filter_by(
                        project_id=operation.project_id, work_id=work.id, state="OPEN"
                    ).order_by(__import__(
                        "eason_one.models", fromlist=["Escalation"]
                    ).Escalation.id).all()
                    if governance_service.is_founder_type(row.escalation_type)
                ), None)
                return {
                    "kind": "FOUNDER_AUTHORITY",
                    "status": "WORK_WAITING_FOUNDER",
                    "work_id": work.id,
                    "task_id": blocked_task.id,
                    "escalation_id": getattr(escalation, "id", None),
                    "authority_type": getattr(escalation, "escalation_type", None),
                }
            if work and any(row.condition_type == "RECONCILIATION" for row in waits):
                __import__("eason_one.services.operation_kernel",fromlist=["transition"]).transition(
                    operation,"WAITING_INPUT","INTERNAL_RECONCILIATION_REQUIRED",
                    stage="RECOVERY",actor_type="RUNTIME",commit=False,
                    payload={"work_id":work.id,"task_id":blocked_task.id},
                )
                operation.waiting_reason="Internal provider-effect reconciliation is required before this Work can resume."
                db.session.commit()
                return {"kind":"RECOVERY","status":"RECONCILIATION_REQUIRED","work_id":work.id,"task_id":blocked_task.id}
            return _decision_step(operation, idempotency_key, blocked_task)

        dependency_blocked = multi.blocked_by_dependencies(operation)
        if dependency_blocked:
            __import__("eason_one.services.operation_kernel",fromlist=["transition"]).transition(
                operation,"WAITING_INPUT","ORCHESTRATION_INTERNAL_WAIT",
                stage="RECOVERY",actor_type="RUNTIME",commit=False,
                payload={"task_ids":[task.id for task in dependency_blocked]},
            )
            operation.waiting_reason=(
                "The approved dependency graph is waiting on upstream Work. "
                "No Founder decision has been requested."
            )
            db.session.commit()
            return {
                "kind": "ORCHESTRATION", "status": "WAITING_INTERNAL",
                "task_ids": [task.id for task in dependency_blocked],
            }
    else:
        work_runtime = __import__(
            "eason_one.services.work_runtime",
            fromlist=[
                "delivery_works", "dependencies_satisfied", "dependency_blocked_tasks",
                "sync_task_projection", "task_for_work", "task_for_work_state", "work_for_task",
            ],
        )
        vnext_delivery = work_runtime.delivery_works(operation)
        if vnext_delivery:
            review_task = work_runtime.task_for_work_state(operation, "VERIFYING")
            if review_task:
                return _run_review_step(operation, idempotency_key, review_task)

            blocked_task = work_runtime.task_for_work_state(operation, "WAITING")
            if blocked_task:
                work = work_runtime.work_for_task(blocked_task)
                waits = [row for row in work.wait_conditions if row.state == "OPEN"] if work else []
                if work and any(row.condition_type == "RECONCILIATION" for row in waits):
                    __import__(
                        "eason_one.services.operation_kernel", fromlist=["transition"]
                    ).transition(
                        operation, "WAITING_INPUT", "INTERNAL_RECONCILIATION_REQUIRED",
                        stage="RECOVERY", actor_type="RUNTIME", commit=False,
                        payload={"work_id": work.id, "task_id": blocked_task.id},
                    )
                    operation.waiting_reason = (
                        "Internal provider-effect reconciliation is required before this Work can resume."
                    )
                    db.session.commit()
                    return {
                        "kind": "RECOVERY", "status": "RECONCILIATION_REQUIRED",
                        "work_id": work.id, "task_id": blocked_task.id,
                    }
                return _decision_step(operation, idempotency_key, blocked_task)

            executable = []
            for work in sorted(vnext_delivery, key=lambda row: row.id):
                if work.state not in {"READY", "EXECUTING"}:
                    continue
                if not work_runtime.dependencies_satisfied(work):
                    continue
                task = work_runtime.task_for_work(work)
                if task:
                    work_runtime.sync_task_projection(work, task)
                    executable.append(task)
            if executable:
                return _run_task_step(operation, idempotency_key, executable[0])

            dependency_blocked = work_runtime.dependency_blocked_tasks(operation)
            if dependency_blocked:
                __import__(
                    "eason_one.services.operation_kernel", fromlist=["transition"]
                ).transition(
                    operation, "WAITING_INPUT", "WORK_DEPENDENCY_INTERNAL_WAIT",
                    stage="RECOVERY", actor_type="RUNTIME", commit=False,
                    payload={"task_ids": [task.id for task in dependency_blocked]},
                )
                operation.waiting_reason = (
                    "Approved Work is waiting on upstream Work dependencies. "
                    "No Founder decision has been requested."
                )
                db.session.commit()
                return {
                    "kind": "WORK", "status": "WAITING_INTERNAL",
                    "task_ids": [task.id for task in dependency_blocked],
                }
        else:
            # Pre-vNext Operations preserve the proven serial contract exactly.
            current = next((item for item in operation.tasks if item.status not in {"DONE", "CANCELLED"}), None)
            if current:
                if current.status in {"ASSIGNED", "WORKING"}:
                    return _run_task_step(operation, idempotency_key, current)
                if current.status == "REVIEW":
                    return _run_review_step(operation, idempotency_key, current)
                if current.status == "BLOCKED":
                    return _decision_step(operation, idempotency_key, current)
                raise ValueError(f"Task #{current.id} is in unsupported workflow state {current.status}")
    if _meeting_required_before_final(operation):
        meeting = _start_planned_final_meeting(operation)
        return {
            "kind": "MEETING_CREATED", "status": "RUNNING",
            "meeting_id": meeting.id,
        }
    allowed, _ = completion_guard(operation)
    if not allowed:
        return _decision_step(operation, idempotency_key)
    if _codex_only_operation(operation):
        return _deterministic_engineering_delivery(operation, idempotency_key)
    verification_step, _ = _current_goal_verification(operation)
    if not verification_step:
        return _goal_verification_step(operation, idempotency_key)
    verification = (verification_step.result_json or {}).get("verification") or {}
    if verification.get("overall_status") != "SATISFIED":
        return _decision_step(operation, idempotency_key)
    return _report_step(operation, idempotency_key)


def wait_for_founder(
    operation, reason, additional_budget=None, decision_kind="FOUNDER_AUTHORITY"
):
    """Compatibility boundary for legacy Operation callers.

    vNext Founder authority is owned by services.governance. Generic Operation
    recovery/evidence/hiring strings are internal waits and cannot become Founder
    attention merely because old code called this function. Historical Missions
    retain the old projection for audit compatibility.
    """
    project = operation.project if operation else None
    contract = __import__(
        "eason_one.services.project_contract", fromlist=["is_vnext_governed"]
    )
    if project and contract.is_vnext_governed(project):
        kind = str(decision_kind or "").strip().upper()
        if kind == "BUDGET_AUTHORIZATION":
            try:
                exact = Decimal(str(additional_budget))
            except (InvalidOperation, TypeError, ValueError):
                exact = Decimal("0")
            if exact <= 0:
                pause_for_internal_runtime_recovery(
                    operation,
                    str(reason) + " Exact positive Project budget shortfall is missing; no Founder gate was created.",
                    condition_type="RECONCILIATION",
                )
                return operation
            governance_owner = __import__(
                "eason_one.services.governance",
                fromlist=["request_budget_gate", "FounderAuthorityPreviouslyRejected"],
            )
            try:
                escalation = governance_owner.request_budget_gate(
                    project=project, additional_twd=exact, reason=reason, operation=operation,
                    created_by_employee_id=operation.proposed_by_employee_id,
                )
            except governance_owner.FounderAuthorityPreviouslyRejected as exc:
                # Founder-negative budget authority is canonical Governance truth.
                # Legacy Operation compatibility may request the gate, but it may
                # not independently rewrite Project/Work lifecycle state after the
                # same authority has already been rejected.
                runtime = __import__(
                    "eason_one.services.work_runtime", fromlist=["ensure_management_work"]
                )
                management_work = runtime.ensure_management_work(operation)
                governance_owner.apply_rejected_budget_boundary(
                    project=project,
                    work=management_work,
                    decision_id=exc.decision_id,
                )
                db.session.commit()
                return operation
            __import__(
                "eason_one.services.operation_kernel", fromlist=["transition"]
            ).transition(
                operation, "WAITING_APPROVAL", "FOUNDER_ACTION_REQUIRED",
                stage="FOUNDER_GATE", commit=False,
                payload={"decision_kind": kind, "reason": reason, "governance_escalation_id": escalation.id},
            )
            operation.waiting_reason = reason
            approved = __import__(
                "eason_one.services.governance", fromlist=["approve_option"]
            ).approve_option(escalation)
            operation.founder_report_json = {
                "decision_kind": kind,
                "headline": "I need one exact Project authority decision.",
                "summary": reason,
                "governance_escalation_id": escalation.id,
                "additional_budget_exact_twd": approved.get("additional_budget_twd"),
                "additional_budget_twd": approved.get("additional_budget_twd"),
                "scope": "PROJECT",
            }
            db.session.commit()
            return operation

        # No other legacy caller supplies the exact deadline/scope/constraint or
        # exact-action payload required by canonical Governance. Keep these
        # software/management conditions inside bounded recovery instead.
        pause_for_internal_runtime_recovery(
            operation,
            str(reason) + f" Legacy decision kind {kind or 'UNKNOWN'} did not carry a precise Founder-only authority payload.",
            condition_type="RECONCILIATION",
        )
        return operation

    # Historical compatibility path.
    if operation.project_id:
        __import__(
            "eason_one.services.escalations", fromlist=["open_escalation"]
        ).open_escalation(
            project_id=operation.project_id,
            operation_id=operation.id,
            escalation_type=decision_kind,
            reason=reason,
            created_by_employee_id=operation.proposed_by_employee_id,
            options=["APPROVE", "MODIFY", "REJECT"],
            recommendation=(
                "Authorize only if the requested change remains aligned with the Founder goal."
            ),
        )
    __import__("eason_one.services.operation_kernel",fromlist=["transition"]).transition(
        operation,"WAITING_APPROVAL","FOUNDER_ACTION_REQUIRED",stage="FOUNDER_GATE",commit=False,
        payload={"decision_kind":decision_kind,"reason":reason},
    )
    operation.waiting_reason = reason
    approved = Decimal(operation.approved_budget_twd)
    actual = actual_cost(operation)
    remaining = approved - actual
    is_budget = decision_kind == "BUDGET_AUTHORIZATION"

    report = {
        "decision_kind": decision_kind,
        "headline": "I need one decision.",
        "summary": reason,
        "next_move": (
            "Approve additional authority, modify the boundary, or stop."
            if is_budget else
            "Approve, modify, or reject this governed decision."
        ),
        "approved_twd": str(approved),
        "actual_twd": str(actual),
        "remaining_twd": str(remaining),
        "next_action": reason,
    }
    event = {
        "kind": decision_kind,
        "status": "PENDING",
        "reason": reason,
        "choices": ["APPROVE", "MODIFY", "REJECT"],
    }
    if is_budget:
        if additional_budget is None:
            raise ValueError("HISTORICAL_BUDGET_GATE_REQUIRES_EXACT_AMOUNT")
        exact_additional = Decimal(additional_budget)
        if exact_additional <= 0:
            raise ValueError("HISTORICAL_BUDGET_GATE_REQUIRES_EXACT_POSITIVE_AMOUNT")
        authorized_additional = budget_authorization_amount(exact_additional)
        resulting_total = approved + authorized_additional
        budget_fields = {
            "maximum_additional_authorization_twd": _budget_text(authorized_additional),
            "additional_budget_exact_twd": str(exact_additional),
            "additional_budget_twd": _budget_text(authorized_additional),
            "resulting_authorized_twd": _budget_text(resulting_total),
        }
        report.update(budget_fields)
        event.update({
            "authorized_twd": str(approved),
            "spent_twd": str(actual),
            "additional_required_twd": _budget_text(authorized_additional),
            "additional_required_exact_twd": str(exact_additional),
            "resulting_authorized_twd": _budget_text(resulting_total),
        })
    operation.founder_report_json = report

    memory = dict(operation.memory_json or {})
    events = list(memory.get("founder_attention_events") or [])
    duplicate = bool(
        events and events[-1].get("status") == "PENDING"
        and events[-1].get("kind") == decision_kind
        and events[-1].get("reason") == reason
    )
    if not duplicate:
        events.append(event)
    memory["founder_attention_events"] = events
    operation.memory_json = memory
    db.session.commit()
    return operation


def recover_multi_agent_preprovider_gate(operation):
    """Recover the known Phase-1 parallel pre-provider race without Founder input.

    The failed Phase-1 path could mark a branch AMBIGUOUS after the runtime had
    only crossed its local step boundary, even though no provider invocation had
    happened.  A sibling branch then observed the Operation-wide Founder gate and
    was rejected as unauthorized.  Recovery is deliberately narrow: every blocked
    Task must have only known pre-provider evidence and zero provider/cost evidence.
    Any genuinely ambiguous or paid provider attempt is left for Founder review.
    """
    kernel = __import__(
        "eason_one.services.operation_kernel",
        fromlist=["authoritative_status", "enqueue", "append_event"],
    )
    if kernel.authoritative_status(operation) != "WAITING_APPROVAL":
        return False
    memory = dict(operation.memory_json or {})
    if not memory.get("multi_agent_enabled"):
        return False
    if (operation.waiting_reason or "") != "Provider-call truth is ambiguous; recovery is required before continuing.":
        return False

    blocked = [task for task in operation.tasks if task.status == "BLOCKED"]
    if not blocked:
        return False
    safe_errors = {
        "Formal Mission execution cannot use MockProvider",
        "Operation is not authorized for execution",
        "Operation is not authorized for a provider call",
    }
    recovered_steps = []
    for task in blocked:
        step = OperationStep.query.filter_by(
            operation_id=operation.id, task_id=task.id, kind="TASK"
        ).order_by(OperationStep.id.desc()).first()
        if not step:
            return False
        run = db.session.get(AgentRun, step.agent_run_id) if step.agent_run_id else None
        if run:
            if Decimal(str(run.real_cost or 0)) > 0:
                return False
            if run.provider_request_id or run.provider_response_id:
                return False
            if run.failure_reason != "AUTHORITY_BLOCKED" and (step.error_text or "") not in safe_errors:
                return False
        elif (step.error_text or "") not in safe_errors:
            return False
        recovered_steps.append(step)

    for task in blocked:
        task.status = "ASSIGNED"
    for step in recovered_steps:
        step.status = "FAILED_PRE_PROVIDER_RECOVERED"
        step.finished_at = step.finished_at or now()

    attention = list(memory.get("founder_attention_events") or [])
    for index in range(len(attention) - 1, -1, -1):
        item = attention[index]
        if item.get("status") == "PENDING":
            attention[index] = dict(
                item, status="RESOLVED", resolution="AUTO_PRE_PROVIDER_RECOVERY",
                resolution_reason=(
                    "No provider invocation occurred; Phase-1 parallel pre-provider "
                    "execution race was repaired deterministically."
                ),
            )
            break
    memory["founder_attention_events"] = attention
    memory["multi_agent_current_wave"] = None
    memory.pop("multi_agent_deferred_attention", None)
    memory["multi_agent_preprovider_recovery"] = {
        "task_ids": [task.id for task in blocked],
        "step_ids": [step.id for step in recovered_steps],
    }
    operation.memory_json = memory
    operation.founder_report_json = None
    operation.waiting_reason = None
    kernel.enqueue(
        operation, actor_type="SYSTEM",
        reason="Recovered a proven zero-provider Phase-1 parallel pre-provider race.",
    )
    kernel.append_event(
        operation, "MULTI_AGENT_PRE_PROVIDER_GATE_RECOVERED",
        from_status=kernel.authoritative_status(operation),
        to_status=kernel.authoritative_status(operation),
        stage=operation.current_stage, actor_type="SYSTEM",
        payload={"task_ids": [task.id for task in blocked]},
    )
    db.session.commit()
    return True


def recover_project_budget_authority(operation):
    """Repair a stale Operation-level budget gate covered by Project authority.

    This is intentionally narrow: only a persisted BUDGET_AUTHORIZATION gate is
    eligible, the Project must already have enough remaining Founder authority,
    and zero-cost transient Codex-readiness blocks may be reset for retry while
    preserving the failed Run as audit evidence.
    """
    if operation.status != "WAITING_FOR_FOUNDER" or not operation.project:
        return False
    report = dict(operation.founder_report_json or {})
    if report.get("decision_kind") != "BUDGET_AUTHORIZATION":
        return False
    try:
        additional = Decimal(str(
            report.get("additional_budget_exact_twd")
            or report.get("additional_budget_twd")
            or "0"
        ))
    except (InvalidOperation, TypeError):
        return False
    if additional <= 0:
        return False

    kernel = __import__(
        "eason_one.services.operation_kernel",
        fromlist=["budget_snapshot", "enqueue", "append_event"],
    )
    snapshot = kernel.budget_snapshot(operation)
    required_available = Decimal(snapshot["available"]) + additional
    delegated = _delegate_project_authority(operation, required_available)
    if delegated is None:
        return False

    recovered_tasks = []
    for task in operation.tasks:
        if task.status != "BLOCKED" or not task.assigned_employee or task.assigned_employee.slug != "engineer":
            continue
        run = AgentRun.query.filter_by(
            operation_id=operation.id, task_id=task.id, purpose="TASK_EXECUTION"
        ).order_by(AgentRun.id.desc()).first()
        if not run or run.status != "FAILED" or Decimal(str(run.real_cost or 0)) > 0:
            continue
        text = " ".join([str(run.failure_reason or ""), str(run.error_text or "")]).casefold()
        if "codex runtime is not ready" not in text and "engineering_runtime_blocked" not in text:
            continue
        task.status = "ASSIGNED"
        recovered_tasks.append(task.id)

    memory = dict(operation.memory_json or {})
    events = list(memory.get("founder_attention_events") or [])
    for index in range(len(events) - 1, -1, -1):
        item = events[index]
        if item.get("status") == "PENDING" and item.get("kind") == "BUDGET_AUTHORIZATION":
            events[index] = dict(
                item, status="RESOLVED", resolution="AUTO_PROJECT_AUTHORITY",
                resolution_reason="Existing Project authority covered the internal Operation shortfall.",
            )
            break
    memory["founder_attention_events"] = events
    memory["project_budget_gate_recovered"] = {
        "delegated_twd": str(delegated),
        "recovered_task_ids": recovered_tasks,
    }
    operation.memory_json = memory
    operation.founder_report_json = None
    operation.waiting_reason = None
    kernel.enqueue(
        operation, actor_type="SYSTEM",
        reason="Existing Project authority covered the internal Operation budget shortfall.",
    )
    kernel.append_event(
        operation, "PROJECT_BUDGET_GATE_RECOVERED",
        from_status=kernel.authoritative_status(operation),
        to_status=kernel.authoritative_status(operation),
        stage=operation.current_stage, actor_type="SYSTEM",
        payload={"delegated_twd": str(delegated), "recovered_task_ids": recovered_tasks},
    )
    db.session.commit()
    return True


def recover_budget_governance(operation):
    report = operation.founder_report_json or {}
    if report.get("decision_kind") == "BUDGET_AUTHORIZATION":
        return operation
    step = OperationStep.query.filter_by(
        operation_id=operation.id, status="AMBIGUOUS"
    ).order_by(OperationStep.id.desc()).first()
    prefix = (
        "operation budget is exhausted; maximum additional authorization "
        "required TWD "
    )
    if not step or not (step.error_text or "").startswith(prefix):
        return operation
    try:
        additional = Decimal(step.error_text[len(prefix):].strip())
    except InvalidOperation:
        return operation
    step.status = "BLOCKED"
    step.error_text = "Additional Founder budget authorization is required."
    step.finished_at = step.finished_at or now()
    wait_for_founder(
        operation,
        "The next bounded execution step exceeds the remaining authorized "
        "Operation budget.",
        additional_budget=additional,
        decision_kind="BUDGET_AUTHORIZATION",
    )
    return operation


def reconcile_legacy_budget_event(operation):
    """Resolve only the proven pre-ceiling residue created by one prior grant.

    v0.20 Projects use the immutable Founder Contract + amendment ledger. This
    legacy precision repair is therefore forbidden from mutating their Project
    budget authority.
    """
    from . import core_v018 as _core_v018
    if _core_v018.is_v018_operation(operation):
        return False
    from ..models import KnowledgeItem
    legacy_audit = KnowledgeItem.query.filter(
        KnowledgeItem.project_id == operation.project_id,
        KnowledgeItem.kind == "DECISION",
        KnowledgeItem.title == f"Founder approve: {operation.title}",
        KnowledgeItem.source_ref.is_(None),
    ).order_by(KnowledgeItem.id.desc()).first()
    if legacy_audit and len((operation.memory_json or {}).get(
        "founder_attention_events") or []) >= 2:
        legacy_audit.source_ref = "operation-budget-governance"
        db.session.commit()
    if operation.status != "WAITING_FOR_FOUNDER":
        return False
    memory = dict(operation.memory_json or {})
    events = list(memory.get("founder_attention_events") or [])
    if len(events) < 2:
        return False
    prior, pending = events[-2], events[-1]
    if not (
        prior.get("kind") == pending.get("kind") == "BUDGET_AUTHORIZATION"
        and prior.get("status") == "RESOLVED"
        and prior.get("resolution") in {
            "APPROVE", "RECONCILED_LEGACY_PRECISION"}
        and pending.get("status") == "PENDING"
        and Decimal(str(prior.get("spent_twd", 0)))
            == Decimal(str(pending.get("spent_twd", 0)))
    ):
        return False
    if prior.get("resolution") == "RECONCILED_LEGACY_PRECISION":
        corrected_total = Decimal(str(
            prior.get("reconciled_authorized_twd")
            or operation.approved_budget_twd))
        original_estimate = corrected_total
    else:
        prior_authorized = Decimal(str(prior["authorized_twd"]))
        prior_exact = Decimal(str(
            prior.get("additional_required_exact_twd")
            or prior.get("additional_required_twd")))
        corrected_total = (
            prior_authorized + budget_authorization_amount(prior_exact))
        original_estimate = prior_authorized + prior_exact
    current = Decimal(operation.approved_budget_twd)
    if not (
        original_estimate <= corrected_total
        and Decimal("0") <= corrected_total - current <= BUDGET_QUANTUM
    ):
        return False
    operation.approved_budget_twd = corrected_total
    if operation.project:
        operation.project.real_budget_limit = corrected_total
    events[-1] = dict(
        pending, status="RESOLVED", resolution="RECONCILED_LEGACY_PRECISION",
        reconciled_authorized_twd=_budget_text(corrected_total),
        reconciliation_reason=(
            "Prior Founder authorization covers the unchanged bounded estimate "
            "under the corrected upward precision policy."))
    memory["founder_attention_events"] = events
    operation.memory_json = memory
    operation.founder_report_json = None
    operation.hard_cost_cap_twd = corrected_total
    __import__("eason_one.services.operation_kernel",fromlist=["enqueue"]).enqueue(
        operation,actor_type="SYSTEM",reason="Legacy budget precision was reconciled."
    )
    operation.waiting_reason = None
    db.session.commit()
    return True


def recover_legacy_preflight_failure(operation):
    report = operation.founder_report_json or {}
    if not (
        operation.status == "WAITING_FOR_FOUNDER"
        and report.get("summary")
          == "Provider-call truth is ambiguous; recovery is required before continuing."
    ):
        return False
    step = OperationStep.query.filter_by(
        operation_id=operation.id, status="AMBIGUOUS",
        agent_run_id=None).order_by(OperationStep.id.desc()).first()
    if not step:
        return False
    step.status = "BLOCKED"
    step.error_text = (
        "Provider configuration unavailable before provider invocation.")
    __import__("eason_one.services.operation_kernel",fromlist=["enqueue"]).enqueue(
        operation,actor_type="SYSTEM",reason="Legacy preflight failure was repaired before provider invocation."
    )
    operation.waiting_reason = None
    operation.founder_report_json = None
    db.session.commit()
    return True


def resume(operation):
    core = __import__(
        "eason_one.services.core_v018",
        fromlist=["is_v018_operation", "is_dormant_legacy_project_operation"],
    )
    if core.is_v018_operation(operation):
        __import__(
            "eason_one.services.company_runtime", fromlist=["resume_after_founder"]
        ).resume_after_founder(operation, resolution="FOUNDER_RESUMED")
        return operation
    if core.is_dormant_legacy_project_operation(operation):
        raise ValueError("This pre-v0.18 Project Operation is historical and cannot be resumed through the legacy runtime.")
    kernel=__import__("eason_one.services.operation_kernel",fromlist=["authoritative_status","enqueue"])
    if kernel.authoritative_status(operation) not in {"WAITING_APPROVAL","WAITING_INPUT"}:
        raise ValueError("Operation is not waiting or paused")
    kernel.enqueue(operation,actor_type="FOUNDER",reason="Founder resumed the saved checkpoint.")
    operation.waiting_reason = None
    db.session.commit()
    return operation


def pause(operation):
    core = __import__(
        "eason_one.services.core_v018",
        fromlist=["is_v018_operation", "is_dormant_legacy_project_operation"],
    )
    if core.is_v018_operation(operation):
        __import__(
            "eason_one.services.company_runtime", fromlist=["pause_operation"]
        ).pause_operation(operation, reason="Founder paused execution at a durable Work checkpoint.")
        return operation
    if core.is_dormant_legacy_project_operation(operation):
        raise ValueError("This pre-v0.18 Project Operation is historical and cannot be paused through the legacy runtime.")
    kernel=__import__("eason_one.services.operation_kernel",fromlist=["authoritative_status","transition"])
    if kernel.authoritative_status(operation) not in {"QUEUED","RUNNING","VERIFYING"}:
        raise ValueError("Operation is not running")
    kernel.transition(operation,"WAITING_INPUT","FOUNDER_PAUSED",stage="PAUSED",actor_type="FOUNDER",commit=False)
    operation.waiting_reason="Founder paused execution at a durable checkpoint."
    db.session.commit()
    return operation


def stop(operation):
    core = __import__(
        "eason_one.services.core_v018",
        fromlist=["is_v018_operation", "is_dormant_legacy_project_operation"],
    )
    if core.is_v018_operation(operation):
        __import__(
            "eason_one.services.company_runtime", fromlist=["cancel_operation"]
        ).cancel_operation(operation, reason="Founder cancelled the approved Work. No further execution is authorized.")
        return operation
    if core.is_dormant_legacy_project_operation(operation):
        raise ValueError("This pre-v0.18 Project Operation is historical and cannot be changed through the legacy runtime.")
    kernel=__import__("eason_one.services.operation_kernel",fromlist=["authoritative_status","transition"])
    if kernel.authoritative_status(operation) in {"COMPLETED","FAILED","CANCELLED"}:
        return operation
    kernel.transition(operation,"CANCELLED","FOUNDER_CANCELLED",stage="CANCELLED",actor_type="FOUNDER",commit=False,force=True)
    operation.waiting_reason="Founder cancelled the Operation. No further execution is authorized."
    if operation.project and operation.project.origin == "CEO_OPERATION":
        active_siblings = [row for row in Operation.query.filter_by(project_id=operation.project_id).all()
            if row.id != operation.id and kernel.authoritative_status(row) not in {"COMPLETED","FAILED","CANCELLED"}]
        if not active_siblings:
            operation.project.status="CANCELLED"
            operation.project.current_state_summary="Founder cancelled the governing Operation."
    db.session.commit()
    return operation


def telemetry(operation):
    runs = AgentRun.query.filter_by(operation_id=operation.id)
    meetings = Meeting.query.filter_by(operation_id=operation.id)
    return {
        "goal_status": operation.status,
        "provider_calls": runs.count(),
        "input_tokens": db.session.query(func.coalesce(
            func.sum(AgentRun.input_tokens), 0
        )).filter_by(operation_id=operation.id).scalar(),
        "output_tokens": db.session.query(func.coalesce(
            func.sum(AgentRun.output_tokens), 0
        )).filter_by(operation_id=operation.id).scalar(),
        "actual_cost_twd": str(actual_cost(operation)),
        "meetings_used": meetings.count(),
        "employees_used": db.session.query(
            func.count(func.distinct(AgentRun.employee_id))
        ).filter(AgentRun.operation_id == operation.id).scalar(),
        "reviews_used": runs.filter_by(purpose="TASK_REVIEW").count(),
        "blocked_count": Task.query.filter_by(
            operation_id=operation.id, status="BLOCKED"
        ).count(),
    }


def state(operation):
    done = sum(task.status == "DONE" for task in operation.tasks)
    active_task = next((task for task in operation.tasks if task.status in {
        "ASSIGNED", "WORKING", "REVIEW", "BLOCKED"
    }), None)
    meeting = Meeting.query.filter_by(
        operation_id=operation.id, status="RUNNING"
    ).order_by(Meeting.id.desc()).first()
    return {
        "operation_id": operation.id, "status": operation.status,
        "done": done, "total": len(operation.tasks),
        "actual_cost_twd": str(actual_cost(operation)),
        "remaining_budget_twd": str(remaining_budget(operation)),
        "continue_allowed": operation.status == "RUNNING",
        "current_step": (
            f"Meeting: {meeting.title}" if meeting
            else f"{active_task.assigned_employee.name}: {active_task.title}"
            if active_task and active_task.assigned_employee
            else "CEO final synthesis"
        ),
    }
