"""Founder-facing read models for the Eason One Headquarters UI.

This module is deliberately read-only.  It translates the existing governed
runtime into product-facing scenes without inventing state or invoking a model.
The Headquarters UI can therefore be rendered safely on every GET request.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import func, or_

from ..extensions import db
from ..models import (
    AgentRun,
    ContributionEvent,
    CostEvent,
    Employee,
    EmployeeLearningRecord,
    EmployeeModelHistory,
    FounderFeedbackEvent,
    KnowledgeItem,
    Meeting,
    MeetingMessage,
    MeetingParticipant,
    ModelConfig,
    TalentTemplate,
    HiringRequest,
    Operation,
    OperationEvent,
    OperationStep,
    Project,
    Task,
    WorkMessage,
)
from . import command as command_service
from . import current_company
from . import meetings as meeting_service
from .company import get_company
from .contributions import meaningful_total
from .stabilization import (
    REAL_WORK, SYSTEM_VALIDATION, choose_focus, global_runtime_snapshot, operation_kind,
)

ACTIVE_TASK_STATUSES = {"ASSIGNED", "WORKING", "REVIEW", "BLOCKED"}
OPEN_MEETING_STATUSES = {"PLANNED", "RUNNING", "PAUSED", "WAITING_FOR_FOUNDER"}
TERMINAL_OPERATION_STATUSES = {"COMPLETED", "FAILED", "TERMINATED_BY_FOUNDER", "SUPERSEDED"}
TERMINAL_PROJECT_STATUSES = {"COMPLETED", "FAILED", "CANCELLED"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _initials(name: str) -> str:
    parts = [part for part in (name or "?").replace("-", " ").split() if part]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return f"{parts[0][0]}{parts[-1][0]}".upper()


def _short(text: str | None, limit: int = 160) -> str:
    value = " ".join((text or "").split())
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)].rstrip() + "…"


def _dt(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _age_label(value: datetime | None) -> str:
    value = _dt(value)
    if value is None:
        return "No update yet"
    seconds = max(0, int((_now() - value).total_seconds()))
    if seconds < 60:
        return "Just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    return f"{days}d ago"


def _money(value: Any) -> Decimal:
    return Decimal(value or 0)


def _task_is_live(task: Task) -> bool:
    return bool(
        task.status in ACTIVE_TASK_STATUSES
        and (task.operation_id is None or (task.operation and task.operation.status == "RUNNING"))
    )


def _task_authoritative_success(task: Task) -> bool:
    if task.status != "DONE":
        return False
    if task.operation_id is None:
        return bool(task.result_summary)
    run = (
        AgentRun.query.filter(
            AgentRun.task_id == task.id,
            AgentRun.status == "SUCCEEDED",
            AgentRun.structured_validation_status == "PASSED",
        )
        .order_by(AgentRun.id.desc())
        .first()
    )
    if not run:
        return False
    invalidated = __import__(
        "eason_one.services.engineering_runtime",
        fromlist=["run_is_invalidated"],
    ).run_is_invalidated(run)
    return not invalidated


def _validation_archive(operation: Operation) -> dict[str, Any]:
    memory = dict(operation.memory_json or {})
    archive = dict(memory.get("archive") or {})
    report = operation.founder_report_json or {}
    if report.get("decision_kind") == "RUNTIME_VALIDATION_ARCHIVE":
        archive = archive or {
            "kind": "RUNTIME_VALIDATION_ARCHIVE",
            "reason": report.get("summary"),
            "codex_run_ids": report.get("codex_run_ids") or [],
        }
    return archive


def _employee_current_task(employee: Employee) -> Task | None:
    """Project Work truth projected to the legacy Task-shaped Founder card."""
    models = __import__(
        "eason_one.models", fromlist=["Work", "WorkAssignment"]
    )
    run = (
        AgentRun.query.join(models.Work, AgentRun.work_id == models.Work.id)
        .join(Project, models.Work.project_id == Project.id)
        .join(
            models.WorkAssignment,
            (models.WorkAssignment.work_id == models.Work.id)
            & (models.WorkAssignment.employee_id == employee.id)
            & (models.WorkAssignment.ended_at.is_(None)),
        )
        .filter(
            AgentRun.employee_id == employee.id,
            AgentRun.status.in_(["CREATED", "RUNNING"]),
            Project.environment == "LIVE",
            Project.status.in_(["PLANNING", "ACTIVE", "BLOCKED", "REVIEW"]),
            models.Work.state.in_(["EXECUTING", "VERIFYING"]),
        )
        .order_by(AgentRun.id.desc()).first()
    )
    if run and run.work_id:
        task = Task.query.filter_by(work_id=run.work_id).order_by(Task.id).first()
        if task:
            return task
    return (
        Task.query.join(Project)
        .filter(
            Task.assigned_employee_id == employee.id,
            Task.work_id.is_(None),
            Task.operation_id.is_(None),
            Project.environment == "LIVE",
            Project.status.in_(["PLANNING", "ACTIVE", "BLOCKED", "REVIEW"]),
            Task.status.in_(ACTIVE_TASK_STATUSES),
        )
        .order_by(Task.updated_at.desc(), Task.id.desc()).first()
    )


def employee_presence(employee: Employee) -> dict[str, Any]:
    current_task = _employee_current_task(employee)
    live_meeting = (
        Meeting.query.join(MeetingParticipant)
        .outerjoin(Project, Meeting.project_id == Project.id)
        .filter(
            MeetingParticipant.employee_id == employee.id,
            MeetingParticipant.removed_at.is_(None),
            Meeting.status == "RUNNING",
            or_(Meeting.project_id.is_(None), ~Project.status.in_(tuple(TERMINAL_PROJECT_STATUSES))),
        )
        .order_by(Meeting.updated_at.desc() if hasattr(Meeting, "updated_at") else Meeting.id.desc())
        .first()
    )
    status = command_service.employee_status(employee)
    if status == "IN MEETING":
        state = "IN_MEETING"
    elif current_task:
        state = current_task.status
    elif employee.active:
        state = "AVAILABLE"
    else:
        state = "OFFLINE"
    return {
        "employee": employee,
        "initials": _initials(employee.name),
        "state": state,
        "state_label": state.replace("_", " "),
        "task": current_task,
        "project": current_task.project if current_task else None,
        "meeting": live_meeting,
        "department": employee.department.name if employee.department else "CEO Office",
        "position": employee.position.name if employee.position else "Employee",
        "model": employee.current_model,
        "last_update": _age_label(
            current_task.updated_at if current_task else employee.updated_at
        ),
    }


def _operation_stage(operation: Operation, tasks: list[Task]) -> str:
    if operation.status == "PLANNED":
        return "CONTRACT"
    if operation.status in {"WAITING_FOR_FOUNDER", "PAUSED"}:
        return "ATTENTION"
    if operation.status == "COMPLETED":
        return "DELIVERY"
    if operation.status in {"FAILED", "TERMINATED_BY_FOUNDER"}:
        return "INCIDENT"
    if not tasks:
        return "PLANNING"
    if all(task.status == "DONE" for task in tasks):
        return "INTEGRATION"
    if any(task.status == "REVIEW" for task in tasks):
        return "REVIEW"
    if any(task.status == "BLOCKED" for task in tasks):
        return "BLOCKED"
    return "EXECUTION"


def _mission_spine(operation: Operation, tasks: list[Task]) -> list[dict[str, Any]]:
    stage_order = [
        ("CONTRACT", "Mission contract"),
        ("PLANNING", "CEO plan"),
        ("EXECUTION", "Specialist work"),
        ("REVIEW", "Independent review"),
        ("INTEGRATION", "CEO integration"),
        ("DELIVERY", "Founder delivery"),
    ]
    current = _operation_stage(operation, tasks)
    normalized_current = {
        "ATTENTION": "CONTRACT" if operation.status == "WAITING_FOR_FOUNDER" else "EXECUTION",
        "BLOCKED": "EXECUTION",
        "INCIDENT": "EXECUTION",
    }.get(current, current)
    current_index = next(
        (index for index, (key, _) in enumerate(stage_order) if key == normalized_current),
        0,
    )
    rows: list[dict[str, Any]] = []
    for index, (key, label) in enumerate(stage_order):
        if operation.status == "COMPLETED":
            status = "DONE"
        elif index < current_index:
            status = "DONE"
        elif index == current_index:
            status = "ACTIVE"
        else:
            status = "UPCOMING"
        rows.append({"key": key, "label": label, "status": status})
    return rows


def _operation_progress(operation: Operation, tasks: list[Task]) -> int:
    """Measure the whole governed delivery loop, not only Task completion."""
    if operation.status == "PLANNED":
        return 0
    if operation.status == "COMPLETED":
        return 100
    if operation.status in {"FAILED", "TERMINATED_BY_FOUNDER", "SUPERSEDED"}:
        return min(99, int(operation.actual_cost_twd or 0))

    # A Mission paused at a Founder gate before the first Employee Run has not
    # made delivery progress. Task materialization is governance preparation,
    # not completed work, and must not display a misleading 8%.
    if operation.status in {"WAITING_FOR_FOUNDER", "PAUSED"}:
        execution_runs = AgentRun.query.filter(
            AgentRun.operation_id == operation.id,
            AgentRun.purpose != "CEO_FOUNDER_REQUEST",
        ).count()
        if not any(task.status == "DONE" for task in tasks):
            return 0

    plan = (operation.plan_json or {}).get("operation") or {}
    meeting_config = plan.get("meeting_config") or {}
    meeting_required = meeting_config.get("trigger") in {
        "ON_MATERIAL_CONFLICT", "BEFORE_FINAL_REPORT"
    }
    task_weight = 70 if meeting_required else 80
    progress = 5.0  # Founder approved the contract.
    fractions = {
        "TODO": 0.0, "ASSIGNED": 0.05, "WORKING": 0.35,
        "REVIEW": 0.75, "BLOCKED": 0.60, "DONE": 1.0,
        "CANCELLED": 1.0, "FAILED": 0.50,
    }
    effective_tasks = [task for task in tasks if task.status != "CANCELLED"]
    if effective_tasks:
        def fraction(task: Task) -> float:
            if task.status == "DONE" and not _task_authoritative_success(task):
                return 0.0
            return fractions.get(task.status, 0.0)
        progress += task_weight * (
            sum(fraction(task) for task in effective_tasks) / len(effective_tasks)
        )

    if meeting_required:
        meetings = Meeting.query.filter_by(operation_id=operation.id).all()
        if any(item.status in {"ENDED", "TERMINATED_BY_FOUNDER"} for item in meetings):
            progress += 10
        elif any(item.status in {"RUNNING", "PAUSED", "WAITING_FOR_FOUNDER"} for item in meetings):
            progress += 5

    verification = OperationStep.query.filter_by(
        operation_id=operation.id, kind="GOAL_VERIFICATION", status="SUCCEEDED"
    ).order_by(OperationStep.id.desc()).first()
    if verification and (verification.result_json or {}).get("overall_status") == "SATISFIED":
        progress += 10

    return max(0, min(99, round(progress)))


def _operation_event_text(step: OperationStep | None, operation: Operation, progress: int) -> str | None:
    if step is None:
        return None
    result = step.result_json or {}
    task = step.task
    if step.error_text:
        return f"{step.kind.replace('_', ' ').title()} paused: {_short(step.error_text, 180)}"
    if step.kind == "TASK" and task:
        employee = task.assigned_employee.name if task.assigned_employee else "Employee"
        return f"{employee} finished {task.title}; independent review is next. Progress {progress}%."
    if step.kind == "REVIEW" and task:
        reviewer = task.reviewer.name if task.reviewer else "Reviewer"
        decision = result.get("decision", "reviewed")
        verb = {"ACCEPT": "accepted", "REVISE": "requested revisions to", "BLOCK": "blocked"}.get(decision, "reviewed")
        return f"{reviewer} {verb} {task.title}. Progress {progress}%."
    if step.kind == "MEETING_STEP":
        meeting = Meeting.query.filter_by(operation_id=operation.id).order_by(Meeting.id.desc()).first()
        return (
            f"Internal Meeting advanced to round {meeting.current_round}/{meeting.max_rounds}. "
            f"Progress {progress}%." if meeting else f"Internal Meeting advanced. Progress {progress}%."
        )
    if step.kind == "MEETING_AUTO_RETRY":
        return "A failed Meeting contribution was compressed and rescheduled within the approved one-retry limit."
    if step.kind == "MEETING_RESULT":
        return f"The latest Meeting summary was incorporated into the Operation. Progress {progress}%."
    if step.kind == "GOAL_VERIFICATION":
        return f"Completion criteria were verified as {result.get('overall_status', 'reviewed')}. Progress {progress}%."
    if step.kind == "REPORT":
        return "The CEO completed the Founder report and closed the Operation."
    return f"{step.kind.replace('_', ' ').title()} {step.status.lower()}. Progress {progress}%."


def _next_step_preview(operation: Operation, tasks: list[Task], active_task: Task | None) -> dict[str, Any] | None:
    if _validation_archive(operation):
        return None
    next_task = next((
        task for task in tasks
        if task.status in {"ASSIGNED", "TODO", "BLOCKED", "FAILED", "REVIEW"}
        and (active_task is None or task.id != active_task.id)
    ), None)
    if not next_task:
        return None
    employee = next_task.assigned_employee
    model = employee.current_model if employee else None
    provider = model.provider_key if model else None
    paid = bool(
        model and provider not in {"codex", "mock"}
        and ((model.input_price_per_million or 0) > 0 or (model.output_price_per_million or 0) > 0)
    )
    estimate = Decimal("0")
    if model and paid:
        try:
            estimate = __import__(
                "eason_one.services.costs", fromlist=["estimate_execution"]
            ).estimate_execution(
                model,
                employee.system_instructions if employee else "",
                "\n".join([
                    operation.objective or "",
                    next_task.acceptance_criteria or "",
                    next_task.required_output or "",
                ]),
                next_task.objective or next_task.title,
                max_output_tokens=model.max_output_tokens,
            ).real_cost
        except Exception:
            estimate = Decimal("0")
    return {
        "task": next_task,
        "employee": employee,
        "model": model,
        "provider": provider,
        "paid": paid,
        "estimate_twd": estimate,
        "requires_founder": bool(paid),
    }


def mission_view(operation: Operation, classification: str | None = None) -> dict[str, Any]:
    tasks = list(operation.tasks)
    project = operation.project
    archive = _validation_archive(operation)
    kind = operation_kind(operation)
    effective_tasks = [task for task in tasks if task.status != "CANCELLED"]
    done = sum(_task_authoritative_success(task) for task in effective_tasks)
    total = len(effective_tasks)
    report = operation.founder_report_json or {}
    core = __import__(
        "eason_one.services.core_v018",
        fromlist=["is_v018_operation", "is_retired_work_vnext", "is_dormant_legacy_project_operation"],
    )
    is_v018 = core.is_v018_operation(operation)
    retired_v017 = core.is_retired_work_vnext(operation)
    dormant_legacy = core.is_dormant_legacy_project_operation(operation)

    execution_cost = _money(
        db.session.query(func.coalesce(func.sum(CostEvent.real_cost_delta), 0))
        .filter(CostEvent.operation_id == operation.id)
        .scalar()
    )
    planning_cost = _money(
        db.session.query(func.coalesce(func.sum(AgentRun.real_cost), 0))
        .filter(
            AgentRun.operation_id == operation.id,
            AgentRun.purpose == "CEO_FOUNDER_REQUEST",
        )
        .scalar()
    )

    if is_v018:
        runtime = __import__(
            "eason_one.services.company_runtime", fromlist=["runtime_snapshot"]
        ).runtime_snapshot(operation)
        done = int(runtime.get("done") or 0)
        total = int(runtime.get("total") or 0)
        kernel_status = operation.status
        authority_integrity_error = None
        if project:
            contract_read = __import__(
                "eason_one.services.project_contract", fromlist=["read_projection"]
            ).read_projection(project)
            terms = contract_read.get("terms") or {}
            authority_integrity_error = contract_read.get("integrity_error")
            # A broken governed Contract is fail-closed: the Founder may inspect
            # the Mission, but no legacy/mutable budget is projected as authority.
            project_limit = _money(terms.get("budget_limit_twd") or 0)
        else:
            project_limit = _money(operation.approved_budget_twd)
        kernel_budget = {
            "hard_cap": project_limit,
            "actual": execution_cost,
            "reserved": Decimal("0"),
            "available": max(Decimal("0"), project_limit - execution_cost),
            "completion_reserve": Decimal("0"),
            "spendable_before_completion": max(Decimal("0"), project_limit - execution_cost),
            "estimated": execution_cost,
        }
        active_work = runtime.get("active_work") or {}
        active_task = next((task for task in tasks if getattr(task, "work_id", None) == active_work.get("id")), None)
        kernel_event_count = 0
    elif retired_v017 or dormant_legacy:
        runtime = {"worker_alive": False, "retired": True}
        kernel_status = "RETIRED"
        kernel_budget = {
            "hard_cap": Decimal("0"), "actual": execution_cost, "reserved": Decimal("0"),
            "available": Decimal("0"), "completion_reserve": Decimal("0"),
            "spendable_before_completion": Decimal("0"), "estimated": execution_cost,
        }
        active_task = None
        kernel_event_count = OperationEvent.query.filter_by(operation_id=operation.id).count()
    else:
        kernel=__import__("eason_one.services.operation_kernel",fromlist=["authoritative_status","budget_snapshot"])
        kernel_status=kernel.authoritative_status(operation)
        kernel_budget=kernel.budget_snapshot(operation)
        kernel_event_count=OperationEvent.query.filter_by(operation_id=operation.id).count()
        runtime = __import__(
            "eason_one.services.operation_runtime", fromlist=["runtime_snapshot"]
        ).runtime_snapshot(operation)
        active_task = None
        active_task_id = (runtime.get("active_task") or {}).get("id")
        if runtime.get("worker_alive") and active_task_id:
            active_task = next((task for task in tasks if task.id == int(active_task_id)), None)
    team_ids = {
        employee_id
        for task in tasks
        for employee_id in (task.assigned_employee_id, task.reviewer_employee_id)
        if employee_id
    }
    team = [employee_presence(db.session.get(Employee, employee_id)) for employee_id in team_ids]
    team.sort(key=lambda row: (row["department"], row["employee"].id))
    plan = (operation.plan_json or {}).get("operation") or {}
    acceptance = plan.get("completion_criteria") or []
    event = next(
        (
            item
            for item in current_company.unresolved_governance()
            if item.get("operation") and item["operation"].id == operation.id
        ),
        None,
    )
    if event is None and operation.status == "PLANNED":
        event = {
            "key": f"operation:{operation.id}:initial-approval",
            "type": "OPERATION_APPROVAL",
            "operation": operation,
            "event": {"kind": "OPERATION_APPROVAL", "status": "PENDING"},
            "budget": {
                "authorized": _money(operation.approved_budget_twd),
                "spent": execution_cost,
                "remaining": _money(operation.approved_budget_twd) - execution_cost,
                "additional": Decimal("0"),
                "resulting": _money(operation.approved_budget_twd),
            },
            "href": f"/headquarters/missions/{operation.id}",
        }
    if classification is None and is_v018:
        truth = __import__("eason_one.services.company_truth", fromlist=["project_snapshot"]).project_snapshot(project)
        classification = (
            "TERMINAL" if project and project.status in {"COMPLETED", "CANCELLED", "FAILED"}
            else "WAITING_FOR_FOUNDER" if truth.get("open_founder_escalations") or __import__("eason_one.services.company_runtime", fromlist=["has_founder_gate"]).has_founder_gate(operation)
            else "BLOCKED" if truth.get("state") in {"WAITING", "FAILED"}
            else "ACTIVE"
        )
    classification = classification or (
        "TERMINAL"
        if kernel_status in {"COMPLETED","FAILED","CANCELLED","RETIRED"}
        else "WAITING_FOR_FOUNDER"
        if event
        else "BLOCKED"
        if kernel_status in {"WAITING_APPROVAL", "WAITING_INPUT"}
           or any(task.status == "BLOCKED" for task in tasks)
        else "ACTIVE"
    )
    budget_memory = dict(operation.memory_json or {})
    execution_estimate = _money(
        budget_memory.get("execution_budget_estimate_twd")
        or operation.approved_budget_twd
    )
    model_proposed_budget = _money(
        budget_memory.get("model_proposed_budget_twd")
        or operation.approved_budget_twd
    )
    latest_run = AgentRun.query.filter_by(operation_id=operation.id).order_by(
        AgentRun.id.desc()
    ).first()
    planning_run = AgentRun.query.filter(
        AgentRun.operation_id == operation.id,
        AgentRun.purpose == "CEO_FOUNDER_REQUEST",
    ).order_by(AgentRun.id.desc()).first()
    execution_run = AgentRun.query.filter(
        AgentRun.operation_id == operation.id,
        AgentRun.purpose != "CEO_FOUNDER_REQUEST",
        AgentRun.status.in_(["CREATED", "RUNNING"]),
    ).order_by(AgentRun.id.desc()).first()
    latest_execution_run = AgentRun.query.filter(
        AgentRun.operation_id == operation.id,
        AgentRun.purpose != "CEO_FOUNDER_REQUEST",
    ).order_by(AgentRun.id.desc()).first()
    next_step = _next_step_preview(operation, tasks, active_task)
    return {
        "operation": operation,
        "project": project,
        "classification": classification,
        "kind": kind,
        "is_real_work": kind == REAL_WORK,
        "is_validation": kind == SYSTEM_VALIDATION,
        "stage": (
            "WORK_CORE_V018" if is_v018 else
            "HISTORICAL" if dormant_legacy or retired_v017 else
            _operation_stage(operation, tasks)
        ),
        "spine": _mission_spine(operation, tasks),
        "tasks": tasks,
        "active_task": active_task,
        "done": done,
        "total": total,
        "progress": (100 if project and project.status == "COMPLETED" else (round(done / total * 100) if total else 0) if is_v018 else _operation_progress(operation, tasks)),
        "show_progress_percent": bool(
            (project and project.status == "COMPLETED")
            or (not is_v018 and not dormant_legacy and not retired_v017)
        ),
        "team": team,
        "budget": _money(kernel_budget["hard_cap"]),
        "spent": _money(kernel_budget["actual"]),
        "reserved": _money(kernel_budget["reserved"]),
        "remaining": _money(kernel_budget["available"]),
        "completion_reserve": _money(kernel_budget.get("completion_reserve") or 0),
        "spendable_before_completion": _money(kernel_budget.get("spendable_before_completion") or 0),
        "estimated": _money(kernel_budget["estimated"]),
        "kernel_status": kernel_status,
        "route_type": operation.route_type,
        "route_reason": operation.route_reason,
        "current_stage": (
            "WORKING" if (runtime.get("worker_alive") or runtime.get("active")) else
            "FOUNDER_DECISION" if event else
            "BLOCKED" if classification == "BLOCKED" else
            "READY" if kernel_status in {"QUEUED", "RUNNING"} else
            kernel_status
        ),
        "kernel_event_count": kernel_event_count,
        "runtime_truth": runtime,
        "limits": {
            "calls": int(operation.call_count or 0), "max_calls": int(operation.max_calls or 0),
            "revisions": int(operation.revision_count or 0), "max_revisions": int(operation.max_revisions or 0),
            "messages": int(operation.max_messages or 0), "elapsed_seconds": int(operation.max_elapsed_seconds or 0),
        },
        "planning_cost": planning_cost,
        "linked_total_cost": planning_cost + execution_cost,
        "execution_estimate": execution_estimate,
        "model_proposed_budget": model_proposed_budget,
        "founder_declared_budget_cap": (
            _money(budget_memory.get("founder_declared_budget_cap_twd"))
            if budget_memory.get("founder_declared_budget_cap_twd") is not None else None
        ),
        "stage_budget_caps": dict(budget_memory.get("stage_budget_caps") or {}),
        "ceo_policy_version": budget_memory.get("ceo_policy_version") or "ceo-policy-v1.0",
        "training_episode": budget_memory.get("ceo_training_episode"),
        "latest_run": latest_run,
        "planning_run": planning_run,
        "execution_run": execution_run,
        "latest_execution_run": latest_execution_run,
        "acceptance": acceptance,
        "archive": archive or None,
        "is_archived": bool(archive),
        "next_step": next_step,
        "governance": event,
        "authority_integrity_error": authority_integrity_error if is_v018 else None,
        "summary": _short(
            operation.waiting_reason
            or (operation.founder_report_json or {}).get("summary")
            or (project.current_state_summary if project else None)
            or operation.objective,
            220,
        ),
        "next_result": _short(
            (active_task.required_output if active_task else None)
            or ((next_step or {}).get("task").required_output if (next_step or {}).get("task") else None)
            or (project.next_milestone if project else None)
            or ("Archived runtime validation evidence" if archive else "CEO integration and Founder-ready delivery"),
            100,
        ),
        "updated_label": _age_label(operation.updated_at),
    }


def _focus_mission(
    operation_rows: list[dict[str, Any]],
    *,
    preferred_operation_id: int | None = None,
    include_validation: bool = False,
) -> dict[str, Any] | None:
    return choose_focus(
        operation_rows,
        preferred_operation_id=preferred_operation_id,
        include_validation=include_validation,
    )


def _meeting_view(meeting: Meeting) -> dict[str, Any]:
    # Query participants directly instead of relying on a possibly stale
    # relationship collection in the current SQLAlchemy identity map. This
    # matters when a Meeting is created and rendered in the same request/test
    # lifecycle after participants are inserted.
    participant_rows = (
        MeetingParticipant.query
        .filter_by(meeting_id=meeting.id)
        .filter(MeetingParticipant.removed_at.is_(None))
        .order_by(MeetingParticipant.id)
        .all()
    )
    participants = [
        participant.employee
        for participant in participant_rows
        if participant.employee is not None
    ]
    recent_messages = (
        MeetingMessage.query.filter_by(meeting_id=meeting.id)
        .order_by(MeetingMessage.id.desc())
        .limit(4)
        .all()
    )
    recent_messages.reverse()
    summary = meeting.current_summary_json or meeting.minutes_json or {}
    tokens, cost = meeting_service.usage(meeting)
    terminal_project = bool(
        meeting.project is not None
        and str(meeting.project.status or "").upper() in TERMINAL_PROJECT_STATUSES
    )
    historical = bool(
        terminal_project
        or (
            meeting.operation
            and __import__(
                "eason_one.services.core_v018", fromlist=["is_dormant_legacy_project_operation"]
            ).is_dormant_legacy_project_operation(meeting.operation)
        )
    )
    return {
        "meeting": meeting,
        "participants": [
            {"employee": employee, "initials": _initials(employee.name)}
            for employee in participants
        ],
        "recent_messages": recent_messages,
        "summary": summary,
        "tokens": tokens,
        "cost": cost,
        "calls": meeting_service.confirmed_provider_calls(meeting),
        "is_live": (not historical) and meeting.status in {"RUNNING", "PAUSED", "WAITING_FOR_FOUNDER"},
        "historical": historical,
    }




def _reliability_groups(failures: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Combine failure types into one understandable row per workflow."""
    grouped: dict[str, dict[str, Any]] = {}
    for item in failures:
        workflow = item.get("workflow") or "UNKNOWN_WORKFLOW"
        row = grouped.setdefault(workflow, {
            "workflow": workflow,
            "count": 0,
            "cost": Decimal("0"),
            "types": [],
            "latest": item.get("latest"),
            "blocking": False,
            "runs": [],
        })
        row["count"] += int(item.get("count") or 0)
        row["cost"] += _money(item.get("cost"))
        row["types"].append({
            "name": item.get("failure_type") or "EXECUTION_FAILED",
            "count": int(item.get("count") or 0),
            "impact": item.get("impact") or "The workflow ended without a usable result.",
        })
        row["blocking"] = row["blocking"] or bool(item.get("blocking"))
        row["runs"].extend(item.get("runs") or [])
        latest = item.get("latest")
        if latest and (not row["latest"] or latest.started_at > row["latest"].started_at):
            row["latest"] = latest
    rows = list(grouped.values())
    for row in rows:
        row["state_label"] = "BLOCKING CURRENT WORK" if row["blocking"] else "HISTORICAL · NOT BLOCKING"
        row["summary"] = (
            "Recovery is required before the linked work can continue."
            if row["blocking"]
            else "Recorded for reliability work; the current Mission is still able to continue."
        )
    rows.sort(key=lambda row: (not row["blocking"], -(row["latest"].id if row["latest"] else 0)))
    return rows


def _arrival_briefing(*, focus, employees, meetings, governance, reliability, latest_result, activity):
    """Choose one Founder briefing composition from authoritative company state.

    The home surface is intentionally not a fixed dashboard.  The most urgent
    condition controls both the message and the visual hierarchy: governance,
    blocking incident, live Meeting, delivery, ordinary operation, or idle.
    """
    active_people = [row for row in employees if row["state"] in ACTIVE_TASK_STATUSES | {"IN_MEETING"}]
    blocking = [row for row in reliability if row["blocking"]]
    running_meeting = next((row for row in meetings if row["meeting"].status == "RUNNING"), None)
    delivery_ready = bool(
        latest_result
        and (
            latest_result.get("status") in {"REVIEW", "DELIVERED"}
            or (not focus and latest_result.get("status") == "DONE")
        )
    )

    if governance:
        mode = "decision"
        headline = "Your authority is needed before this work can continue."
    elif blocking:
        mode = "incident"
        headline = "A live runtime incident is blocking company work."
    elif running_meeting:
        mode = "meeting"
        headline = "A Meeting is live and available for Founder observation."
    elif delivery_ready:
        mode = "delivery"
        headline = "A reviewed result is ready for Founder attention."
    elif focus:
        mode = "operating"
        headline = "The company is advancing the priority Mission."
    else:
        mode = "idle"
        headline = "The company is ready for your next Mission."

    updates = []
    for item in activity[:3]:
        updates.append({
            "kind": item["kind"],
            "title": item["title"],
            "detail": item["detail"],
            "age": item["age"],
            "href": item["href"],
        })
    return {
        "mode": mode,
        "headline": headline,
        "mission": focus,
        "primary_decision": governance[0] if governance else None,
        "primary_incident": blocking[0] if blocking else None,
        "live_meeting": running_meeting,
        "working_people": active_people,
        "working_count": len(active_people),
        "meeting_count": sum(row["meeting"].status == "RUNNING" for row in meetings),
        "founder_action_count": len(governance),
        "blocking_incident_count": len(blocking),
        "historical_reliability_count": sum(not row["blocking"] for row in reliability),
        "latest_result": latest_result,
        "updates": updates,
        "next_result": focus["next_result"] if focus else "Define the next Mission contract",
    }




def _coerce_iso_datetime(value: Any) -> datetime | None:
    """Parse a browser-session visit timestamp without trusting it as authority."""
    if isinstance(value, datetime):
        return _dt(value)
    if not value:
        return None
    try:
        return _dt(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    except (TypeError, ValueError):
        return None


def route_ceo_intent(query: str, explicit_mode: str | None = None) -> dict[str, str]:
    """Select the smallest sufficient Founder command path before any paid call."""
    explicit=(explicit_mode or "AUTO").strip().upper()
    kernel=__import__("eason_one.services.operation_kernel",fromlist=["route_command"])
    kernel_explicit={
        "BRIEF":"DIRECT_RESPONSE",
        "ADVISE":"DIRECT_RESPONSE",
        "ACT":None,
    }.get(explicit, explicit if explicit in kernel.ROUTES else None)
    routed=kernel.route_command(query,kernel_explicit)
    route_type=routed["route_type"]
    text=" ".join((query or "").lower().split())
    requests_work=__import__(
      "eason_one.services.ceo_intent",fromlist=["requests_governed_work"]
    ).requests_governed_work(text)
    brief_terms=(
        "status", "brief", "report", "what changed", "since last", "who is",
        "who's", "working now", "current mission", "current task", "blocked",
        "pending", "cost", "spent", "budget", "meeting status", "公司現況",
        "公司狀態", "目前", "現在誰", "花多少", "成本", "卡住", "匯報",
    )
    if explicit=="BRIEF" or (
        route_type=="DIRECT_RESPONSE" and any(term in text for term in brief_terms)
        and not requests_work
    ):
        legacy_route="BRIEF"
    elif route_type=="DIRECT_RESPONSE":
        legacy_route="ADVISE"
    else:
        legacy_route="ACT"
    return {
        "route":legacy_route,
        "route_type":route_type,
        "reason":routed["reason"],
        "mentions":routed.get("mentions") or [],
    }


def _office_brief(*, focus, employees, meetings, governance, reliability,
                  latest_result, activity, last_visit_at=None) -> dict[str, Any]:
    """Build the instant CEO-office briefing entirely from persisted state."""
    last_visit=_coerce_iso_datetime(last_visit_at)
    blocking=[row for row in reliability if row["blocking"]]
    live=next((row for row in meetings if row["meeting"].status=="RUNNING"),None)
    working=[row for row in employees if row["state"] in ACTIVE_TASK_STATUSES | {"IN_MEETING"}]
    changes=[]
    for item in activity:
        at=_dt(item.get("at"))
        if last_visit is None or (at and at > last_visit):
            changes.append(item)
    first_visit=last_visit is None
    if first_visit:
        changes=activity[:3]
    else:
        changes=changes[:5]

    attention=[]
    for item in governance[:3]:
        operation=item.get("operation")
        attention.append({
            "kind":"DECISION",
            "title":operation.title if operation else "Governed proposal",
            "detail":item.get("type", "FOUNDER_DECISION").replace("_", " "),
            "href":item.get("href") or "/headquarters/attention",
            "severity":"warning",
        })
    for row in blocking[:3]:
        attention.append({
            "kind":"INCIDENT",
            "title":row["workflow"].replace("_", " "),
            "detail":row["summary"],
            "href":"/headquarters/attention",
            "severity":"danger",
        })

    if blocking:
        recommendation={
            "title":f"Resolve {blocking[0]['workflow'].replace('_',' ').title()} before more autonomous work.",
            "reason":blocking[0]["summary"],
            "action":"INSPECT RECOVERY",
            "href":"/headquarters/attention",
            "kind":"INCIDENT",
        }
    elif governance:
        item=governance[0]
        operation=item.get("operation")
        recommendation={
            "title":f"Review {operation.title if operation else 'the pending governed proposal'}.",
            "reason":"Company work is waiting for Founder authority.",
            "action":"OPEN DECISION",
            "href":item.get("href") or "/headquarters/attention",
            "kind":"DECISION",
        }
    elif live:
        recommendation={
            "title":f"Observe {live['meeting'].title} while the decision is still forming.",
            "reason":live["summary"].get("open_question") or live["meeting"].purpose,
            "action":"ENTER MEETING",
            "href":f"/headquarters/meetings/{live['meeting'].id}",
            "kind":"MEETING",
        }
    elif latest_result and latest_result.get("status") in {"REVIEW", "DELIVERED"}:
        recommendation={
            "title":f"Review the delivered result: {latest_result['title']}.",
            "reason":latest_result["summary"],
            "action":"OPEN RESULT",
            "href":latest_result["href"],
            "kind":"RESULT",
        }
    elif focus:
        recommendation={
            "title":f"Keep {focus['operation'].title} moving toward its next result.",
            "reason":focus["next_result"],
            "action":"OPEN MISSION",
            "href":f"/headquarters/missions/{focus['operation'].id}",
            "kind":"MISSION",
        }
    else:
        recommendation={
            "title":"Define the first real dogfooding Mission.",
            "reason":"No active Mission is currently advancing toward the September delivery goal.",
            "action":"TALK TO CEO",
            "href":"#ceo-desk",
            "kind":"IDLE",
        }

    state_label=(
        "FOUNDER ATTENTION" if governance else
        "RUNTIME INCIDENT" if blocking else
        "LIVE MEETING" if live else
        "OPERATING" if focus else
        "AVAILABLE"
    )
    return {
        "state_label":state_label,
        "first_visit":first_visit,
        "last_visit_at":last_visit,
        "since_label":"Latest authoritative changes" if first_visit else "Since your last visit",
        "changes":changes,
        "attention":attention,
        "recommendation":recommendation,
        "focus":focus,
        "working":working[:6],
        "working_count":len(working),
        "live_meeting":live,
        "latest_result":latest_result,
        "metrics":{
            "decisions":len(governance),
            "incidents":len(blocking),
            "working":len(working),
            "meetings":sum(row["meeting"].status=="RUNNING" for row in meetings),
        },
    }

def status_report_answer(run: AgentRun) -> dict[str, Any]:
    """Translate a validated CEO status Run into a Founder-facing report.

    Every factual row is rendered from the deterministic status_report payload;
    the model-authored text is used only as the executive summary.
    """
    payload=run.parsed_output_json or {}
    report=payload.get("status_report") or {}
    mission=report.get("priority_mission")
    workers=report.get("employees_working") or []
    incidents=report.get("blocking_incidents") or []
    actions=report.get("founder_actions_required") or []
    sections=[
        {
            "label":"Priority Mission",
            "value":mission.get("title") if mission else "No active Mission",
            "detail":(
                f"{mission.get('status')} · "
                f"{mission.get('done',0)}/{mission.get('total',0)} Tasks"
            ) if mission else "No Mission is currently advancing.",
        },
        {
            "label":"Current Mission stage",
            "value":mission.get("stage") if mission else "NO ACTIVE MISSION",
            "detail":"Derived from persisted Mission and Task state.",
        },
        {
            "label":"Employees working now",
            "value":str(len(workers)),
            "detail":", ".join(
                f"{item['name']} — {item['current_task']}" for item in workers
            ) or "No Employee is on active work.",
        },
        {
            "label":"Blocking incidents",
            "value":str(len(incidents)),
            "detail":"; ".join(
                f"{item['workflow']}: {item['failure_type']}" for item in incidents
            ) or "No blocking runtime incident.",
        },
        {
            "label":"Founder actions required",
            "value":str(len(actions)),
            "detail":"; ".join(
                f"{item['type']}: {item['title']}" for item in actions
            ) or "No Founder action is required.",
        },
        {
            "label":"Next expected result",
            "value":report.get("next_expected_result") or "Not scheduled",
            "detail":"Source: persisted company state.",
        },
    ]
    return {
        "kind":"STATUS_REPORT",
        "title":"Company status",
        "summary":payload.get("executive_response") or "The status report was validated.",
        "sections":sections,
        "status_report":report,
        "run_id":run.id,
        "href":(
            f"/headquarters/missions/{mission['operation_id']}"
            if mission and mission.get("operation_id") else None
        ),
    }


def _briefing_composition(*, focus, employees, meetings, governance, reliability,
                          latest_result, activity, ceo_answer=None, ceo_query=None):
    """Compose the Founder surface from ranked, variable-size briefing blocks.

    This is intentionally not a set of page templates.  Each current condition
    contributes a block with a priority and footprint.  The highest-value facts
    determine the visual order and geometry for this render.
    """
    active_people=[row for row in employees
      if row["state"] in ACTIVE_TASK_STATUSES | {"IN_MEETING"}]
    blocking=[row for row in reliability if row["blocking"]]
    live=next((row for row in meetings if row["meeting"].status=="RUNNING"),None)
    blocks=[]

    def add(kind,priority,width,**data):
        blocks.append({"kind":kind,"priority":priority,"width":width,**data})

    status_report_mode=bool(ceo_answer and ceo_answer.get("kind")=="STATUS_REPORT")
    if ceo_answer:
        add("ceo_report",130,8,answer=ceo_answer,query=ceo_query)
    if governance:
        add("decision",120,8,item=governance[0],count=len(governance))
    if blocking:
        add("incident",115,8,item=blocking[0],count=len(blocking))
    if live:
        add("meeting",110,9,item=live)
    if latest_result and latest_result.get("status") in {"REVIEW","DELIVERED"}:
        add("result",105,8,item=latest_result)
    if focus:
        mission_width=4 if ceo_answer else 8
        add("mission",90,mission_width,item=focus)
        # A deterministic CEO status report already includes the next result and
        # active people.  Do not render the same facts again as equally loud
        # cards; the Mission remains as the one secondary source of truth.
        if not status_report_mode:
            add("next_result",66,4,value=focus["next_result"],mission=focus)
    elif not ceo_answer:
        add("idle",80,8)
    if active_people and not status_report_mode:
        add("people",72,4,people=active_people[:6],count=len(active_people))
    if activity and not status_report_mode:
        add("changes",46,8,items=activity[:4])
    historical=sum(not row["blocking"] for row in reliability)
    if historical:
        add("reliability",30,4,count=historical)

    blocks.sort(key=lambda row:(-row["priority"],row["kind"]))
    if blocks:
        top=blocks[0]["kind"]
    else:
        top="idle"
    layout={
        "ceo_report":"report-first",
        "decision":"authority-first",
        "incident":"incident-first",
        "meeting":"meeting-first",
        "result":"delivery-first",
        "mission":"mission-first",
    }.get(top,"quiet")

    # Fill the first row without forcing every state into the same geometry.
    if len(blocks)>1:
        first,second=blocks[0],blocks[1]
        if first["width"]+second["width"]>12:
            if first["kind"] in {"ceo_report","decision","incident","result"}:
                first["width"],second["width"]=8,4
            elif first["kind"]=="meeting":
                first["width"],second["width"]=9,3
            else:
                first["width"],second["width"]=8,4
    return {
        "layout":layout,
        "lead_kind":top,
        "blocks":blocks,
        "headline":{
            "ceo_report":"The CEO has returned with a validated report.",
            "decision":"Founder authority is the next constraint.",
            "incident":"Live work is blocked by a runtime incident.",
            "meeting":"A live Meeting is the most important company event.",
            "result":"A reviewed result is ready.",
            "mission":"The company is advancing its current Mission.",
        }.get(top,"The company is ready for the next Founder instruction."),
    }


def _activity_feed(limit: int = 8, *, include_validation: bool = False) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    tasks = (
        Task.query.join(Project).filter(Project.environment == "LIVE")
        .order_by(Task.updated_at.desc()).limit(limit * 2).all()
    )
    for task in tasks:
        operation = task.operation
        if operation and __import__(
            "eason_one.services.core_v018", fromlist=["is_dormant_legacy_project_operation"]
        ).is_dormant_legacy_project_operation(operation):
            continue
        if operation and not include_validation and operation_kind(operation) != REAL_WORK:
            continue
        events.append({
            "at": task.updated_at, "kind": "TASK", "title": task.title,
            "detail": f"{task.status.replace('_', ' ')} · {task.assigned_employee.name if task.assigned_employee else 'Unassigned'}",
            "href": f"/headquarters/missions/{task.operation_id}" if task.operation_id else "/headquarters/missions",
        })
        if len(events) >= limit:
            break
    for meeting in Meeting.query.order_by(Meeting.created_at.desc()).limit(limit).all():
        if meeting.operation and __import__(
            "eason_one.services.core_v018", fromlist=["is_dormant_legacy_project_operation"]
        ).is_dormant_legacy_project_operation(meeting.operation):
            continue
        if meeting.operation and not include_validation and operation_kind(meeting.operation) != REAL_WORK:
            continue
        at = meeting.ended_at or meeting.started_at or meeting.created_at
        events.append({"at": at, "kind": "MEETING", "title": meeting.title,
                       "detail": meeting.status.replace("_", " "),
                       "href": f"/headquarters/meetings/{meeting.id}"})
    for operation in Operation.query.order_by(Operation.updated_at.desc()).limit(limit * 2).all():
        if __import__(
            "eason_one.services.core_v018", fromlist=["is_dormant_legacy_project_operation"]
        ).is_dormant_legacy_project_operation(operation):
            continue
        if not include_validation and operation_kind(operation) != REAL_WORK:
            continue
        events.append({"at": operation.updated_at, "kind": "MISSION", "title": operation.title,
                       "detail": operation.status.replace("_", " "),
                       "href": f"/headquarters/missions/{operation.id}"})
    events.sort(key=lambda item: _dt(item["at"]) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    for event in events[:limit]:
        event["age"] = _age_label(event["at"])
    return events[:limit]


def _result_feed(limit: int = 6, *, include_validation: bool = False) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    tasks = (
        Task.query.join(Project).filter(Project.environment == "LIVE", Task.status.in_(["DONE", "REVIEW"]))
        .order_by(Task.updated_at.desc()).limit(limit * 2).all()
    )
    for task in tasks:
        if task.operation and not include_validation and operation_kind(task.operation) != REAL_WORK:
            continue
        results.append({
            "kind": "TASK_RESULT", "title": task.title,
            "summary": _short(task.result_summary or task.required_output or task.objective, 150),
            "project": task.project, "employee": task.assigned_employee, "reviewer": task.reviewer,
            "status": task.status, "updated": task.updated_at,
            "href": f"/headquarters/missions/{task.operation_id}" if task.operation_id else "/headquarters/missions",
        })
        if len(results) >= limit:
            break
    operations = (Operation.query.filter_by(status="COMPLETED")
                  .order_by(Operation.ended_at.desc(), Operation.updated_at.desc())
                  .limit(limit * 2).all())
    for operation in operations:
        if not include_validation and operation_kind(operation) != REAL_WORK:
            continue
        results.append({
            "kind": "MISSION_RESULT", "title": operation.title,
            "summary": _short((operation.founder_report_json or {}).get("summary") or operation.objective, 150),
            "project": operation.project, "employee": operation.proposed_by, "reviewer": None,
            "status": "DELIVERED", "updated": operation.ended_at or operation.updated_at,
            "href": f"/headquarters/missions/{operation.id}",
        })
    results.sort(key=lambda item: _dt(item["updated"]) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    for result in results[:limit]:
        result["age"] = _age_label(result["updated"])
    return results[:limit]


def headquarters_snapshot(ceo_answer=None, ceo_query=None, last_visit_at=None, preferred_operation_id=None) -> dict[str, Any]:
    company = get_company()
    if not company:
        return {"company": None}
    # Preserve one narrowly-scoped legacy recovery path while the retired
    # Command interface is no longer rendered. No provider call is made.
    if not Operation.query.count():
        command_service._recover_legacy_pending_operation()
    authoritative = current_company.projection()
    missions = [
        mission_view(row["operation"], row["classification"])
        for row in authoritative.get("real_operations", authoritative["operations"])
    ]
    validation_missions = [
        mission_view(row["operation"], row["classification"])
        for row in authoritative.get("validation_operations", [])
    ]
    focus = _focus_mission(missions, preferred_operation_id=preferred_operation_id)
    employee_rows = [employee_presence(employee) for employee in Employee.query.filter_by(active=True).order_by(Employee.id)]
    departments: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for employee in employee_rows:
        departments[employee["department"]].append(employee)
    open_meetings = [
        row
        for meeting in Meeting.query.filter(Meeting.status.in_(OPEN_MEETING_STATUSES))
        .order_by(Meeting.created_at.desc())
        .limit(12)
        .all()
        if not (row := _meeting_view(meeting)).get("historical")
    ][:6]
    live_meeting = next((row for row in open_meetings if row.get("is_live")), None)
    result_feed = _result_feed()
    latest_result = result_feed[0] if result_feed else None
    failures = authoritative["failures"]
    reliability = _reliability_groups(failures)
    activity = _activity_feed()
    arrival = _arrival_briefing(
        focus=focus, employees=employee_rows, meetings=open_meetings,
        governance=authoritative.get("real_governance", authoritative["governance"]), reliability=reliability,
        latest_result=latest_result, activity=activity,
    )
    briefing = _briefing_composition(
        focus=focus, employees=employee_rows, meetings=open_meetings,
        governance=authoritative.get("real_governance", authoritative["governance"]), reliability=reliability,
        latest_result=latest_result, activity=activity, ceo_answer=ceo_answer,
        ceo_query=ceo_query,
    )
    office = _office_brief(
        focus=focus, employees=employee_rows, meetings=open_meetings,
        governance=authoritative.get("real_governance", authoritative["governance"]), reliability=reliability,
        latest_result=latest_result, activity=activity, last_visit_at=last_visit_at,
    )
    return {
        "company": company,
        "authoritative": authoritative,
        "ceo": Employee.query.filter_by(slug="ceo").first(),
        "ceo_state": authoritative["ceo_state"],
        "missions": missions,
        "validation_missions": validation_missions,
        "focus": focus,
        "employees": employee_rows,
        "departments": dict(departments),
        "meetings": open_meetings,
        "live_meeting": live_meeting,
        "attention": authoritative.get("real_governance", authoritative["governance"]),
        "failures": failures,
        "reliability": reliability,
        "blocking_failures": [row for row in reliability if row["blocking"]],
        "arrival": arrival,
        "briefing": briefing,
        "office": office,
        "activity": activity,
        "results": result_feed,
        "latest_result": latest_result,
        "ceo_dialogue": ceo_dialogue_view(),
        "global_runtime": global_runtime_snapshot(),
        "live_operation": operation_live_snapshot(focus["operation"]) if focus else None,
        "metrics": {
            "active_missions": sum(row["classification"] == "ACTIVE" for row in missions),
            "blocked_missions": sum(row["classification"] == "BLOCKED" for row in missions),
            "working_employees": sum(row["state"] in {"WORKING", "REVIEW", "ASSIGNED", "IN_MEETING"} for row in employee_rows),
            "attention": len(authoritative.get("real_governance", authoritative["governance"])),
            "incidents": sum(row["blocking"] for row in reliability),
            "reliability": len(reliability),
            "results_ready": len(result_feed),
            "spent": authoritative["spent"],
            "remaining": authoritative["remaining"],
            "validation_missions": len(validation_missions),
        },
    }


def mission_snapshot(operation: Operation) -> dict[str, Any]:
    """Render the Mission first screen without loading the entire audit ledger."""
    mission = mission_view(operation)
    tasks = mission["tasks"]
    task_ids = [task.id for task in tasks]

    meeting_filter = Meeting.operation_id == operation.id
    if operation.project_id is not None:
        meeting_filter = meeting_filter | (
            Meeting.operation_id.is_(None) & (Meeting.project_id == operation.project_id)
        )
    meeting_rows = (
        Meeting.query.filter(meeting_filter)
        .order_by(Meeting.created_at.desc())
        .limit(4)
        .all()
    )
    meetings = [_meeting_view(meeting) for meeting in meeting_rows]
    runs = (
        AgentRun.query.filter_by(operation_id=operation.id)
        .order_by(AgentRun.id.desc())
        .limit(12)
        .all()
    )
    steps = (
        OperationStep.query.filter_by(operation_id=operation.id)
        .order_by(OperationStep.id.desc())
        .limit(24)
        .all()
    )
    steps.reverse()
    kernel_events=(
        OperationEvent.query.filter_by(operation_id=operation.id)
        .order_by(OperationEvent.sequence.desc()).limit(30).all()
    )
    kernel_events.reverse()

    contribution_filter = ContributionEvent.related_task_id.in_(task_ids or [-1])
    if operation.project_id is not None:
        contribution_filter = contribution_filter | (ContributionEvent.project_id == operation.project_id)
    contributions = (
        db.session.query(ContributionEvent, Employee)
        .join(Employee, ContributionEvent.employee_id == Employee.id)
        .filter(contribution_filter)
        .order_by(ContributionEvent.created_at.desc())
        .limit(10)
        .all()
    )
    knowledge = (
        KnowledgeItem.query.filter_by(project_id=operation.project_id, founder_approved=True)
        .order_by(KnowledgeItem.created_at.desc())
        .limit(6)
        .all()
        if operation.project_id else []
    )

    task_runs: dict[int, AgentRun] = {}
    if task_ids:
        candidate_runs = (
            AgentRun.query.filter(AgentRun.task_id.in_(task_ids))
            .order_by(AgentRun.id.desc())
            .all()
        )
        for run in candidate_runs:
            if run.task_id in task_runs:
                continue
            invalidated = __import__(
                "eason_one.services.engineering_runtime",
                fromlist=["run_is_invalidated"],
            ).run_is_invalidated(run)
            if invalidated:
                continue
            if (
                run.status == "SUCCEEDED" and run.structured_validation_status == "PASSED"
            ) or run.resolution_status in {
                "PARTIAL_RESULT", "VALIDATION_INFRASTRUCTURE_FAILURE"
            }:
                task_runs[run.task_id] = run

    runtime = mission.get("runtime_truth") or __import__(
        "eason_one.services.operation_runtime", fromlist=["runtime_snapshot"]
    ).runtime_snapshot(operation)
    live_task_id = (runtime.get("active_task") or {}).get("id") if runtime.get("worker_alive") else None
    task_display_status = {}
    for task in tasks:
        if task.id == live_task_id:
            task_display_status[task.id] = "WORKING"
        elif task.status == "BLOCKED":
            task_display_status[task.id] = "BLOCKED"
        elif task.status in {"ASSIGNED", "WORKING", "REVIEW"}:
            # A persisted Task label is not proof that a worker exists now.
            task_display_status[task.id] = "READY" if mission["kernel_status"] in {"QUEUED", "RUNNING", "VERIFYING"} else task.status
        else:
            task_display_status[task.id] = task.status

    artifacts = []
    for task in tasks:
        run = task_runs.get(task.id)
        is_partial = bool(run and run.resolution_status in {
            "PARTIAL_RESULT", "VALIDATION_INFRASTRUCTURE_FAILURE"
        })
        authoritative_success = _task_authoritative_success(task)
        task_result_available = bool(
            task.status == "DONE" and (task.result_summary or "").strip()
        )
        if not authoritative_success and not is_partial and not task_result_available:
            continue
        parsed = dict(run.parsed_output_json or {}) if run else {}
        codex = dict(parsed.get("codex") or {})
        context = dict(run.context_composition_json or {}) if run else {}
        result_value = (
            parsed.get("result_summary")
            or parsed.get("result")
            or task.result_summary
            or (run.raw_output if run and run.raw_output and not run.raw_output.lstrip().startswith("{") else None)
        )
        summary = _short(str(result_value) if result_value is not None else None, 320)
        if not summary:
            # A successful schema row without an actual result is audit evidence,
            # not a Founder-facing Artifact.
            continue
        artifact_signals = bool(
            codex.get("changed_files")
            or codex.get("tests")
            or codex.get("acceptance")
            or parsed.get("artifact")
            or parsed.get("artifacts")
        )
        if is_partial:
            truth_status = "PARTIAL RESULT"
        elif authoritative_success and artifact_signals:
            truth_status = "VALIDATED ARTIFACT"
        elif authoritative_success:
            truth_status = "VALIDATED RESULT"
        else:
            truth_status = "UNVERIFIED RESULT"
        artifacts.append({
            "task": task,
            "run": run,
            "title": task.required_output or task.title,
            "summary": summary,
            "owner": task.assigned_employee,
            "reviewer": task.reviewer,
            "status": truth_status,
            "kind": "ARTIFACT" if artifact_signals else "TASK_RESULT",
            "verified": bool(authoritative_success),
            "provider": run.provider_key_snapshot if run else None,
            "model": run.model_name_snapshot if run else None,
            "job_spec": run.context_snapshot if run and run.provider_key_snapshot == "codex" else None,
            "changed_files": codex.get("changed_files") or [],
            "tests": codex.get("tests") or [],
            "acceptance": codex.get("acceptance") or [],
            "risks": codex.get("risks") or [],
            "needs_founder": codex.get("needs_founder") if codex else None,
            "founder_reason": codex.get("founder_reason") if codex else None,
            "stdout_tail": context.get("stdout_tail") if run else None,
            "stderr_tail": context.get("stderr_tail") if run else None,
            "elapsed_ms": context.get("elapsed_ms") if run else None,
            "partial": is_partial,
        })

    company = get_company()
    company_spent = _money(
        db.session.query(func.coalesce(func.sum(CostEvent.real_cost_delta), 0))
        .filter(CostEvent.company_id == company.id)
        .scalar()
    )
    return {
        "company": company,
        "company_spent": company_spent,
        "mission": mission,
        "runtime": runtime,
        "global_runtime": global_runtime_snapshot(),
        "meetings": meetings,
        "runs": runs,
        "steps": steps,
        "kernel_events": kernel_events,
        "contributions": contributions,
        "knowledge": knowledge,
        "artifacts": artifacts,
        "task_display_status": task_display_status,
        "activity": ([
            {
                "kind": "STATE",
                "title": f"#{event.sequence} · {event.event_type.replace('_',' ')}",
                "detail": _short(str(event.payload_json or ""),160),
                "at": event.created_at,
                "age": _age_label(event.created_at),
            } for event in reversed(kernel_events[-12:])
        ] + [
            {
                "kind": "STEP",
                "title": f"{step.kind.replace('_', ' ')} · {step.status}",
                "detail": _short(step.error_text or str(step.result_json or ""), 160),
                "at": step.finished_at or step.created_at,
                "age": _age_label(step.finished_at or step.created_at),
            } for step in reversed(steps[-8:])
        ]),
    }


def employee_snapshot(employee: Employee) -> dict[str, Any]:
    presence = employee_presence(employee)
    tasks = Task.query.filter_by(assigned_employee_id=employee.id).order_by(Task.updated_at.desc()).all()
    active_tasks = [task for task in tasks if _task_is_live(task)]
    runs = AgentRun.query.filter_by(employee_id=employee.id).order_by(AgentRun.started_at.desc()).all()
    model_history = EmployeeModelHistory.query.filter_by(employee_id=employee.id).order_by(
        EmployeeModelHistory.started_at.desc()
    ).all()
    learning = EmployeeLearningRecord.query.filter_by(employee_id=employee.id).order_by(
        EmployeeLearningRecord.created_at.desc()
    ).all()
    contributions = ContributionEvent.query.filter_by(employee_id=employee.id).order_by(
        ContributionEvent.created_at.desc()
    ).all()
    meeting_participations = (
        db.session.query(MeetingParticipant, Meeting)
        .join(Meeting, MeetingParticipant.meeting_id == Meeting.id)
        .filter(MeetingParticipant.employee_id == employee.id)
        .order_by(Meeting.created_at.desc())
        .all()
    )
    messages = MeetingMessage.query.filter_by(employee_id=employee.id).order_by(
        MeetingMessage.created_at.desc()
    ).limit(30).all()
    feedback = FounderFeedbackEvent.query.filter_by(employee_id=employee.id).order_by(
        FounderFeedbackEvent.created_at.desc()
    ).all()
    cost = _money(
        db.session.query(func.coalesce(func.sum(CostEvent.real_cost_delta), 0))
        .filter(CostEvent.employee_id == employee.id)
        .scalar()
    )
    engineering_runtime = __import__(
        "eason_one.services.engineering_runtime",
        fromlist=["effective_run_status", "run_is_authoritative_success", "codex_readiness"],
    )
    successful_runs = sum(engineering_runtime.run_is_authoritative_success(run) for run in runs)
    failed_runs = sum(run.status == "FAILED" for run in runs)
    run_audit = [{
        "run": run,
        "effective_status": engineering_runtime.effective_run_status(run),
        "resolution": run.resolution_note or run.failure_reason or run.structured_validation_status,
        "authoritative_success": engineering_runtime.run_is_authoritative_success(run),
    } for run in runs]
    total_tokens = sum((run.input_tokens or 0) + (run.output_tokens or 0) for run in runs)
    projects = []
    seen_projects: set[int] = set()
    for task in tasks:
        if task.project and task.project.id not in seen_projects:
            seen_projects.add(task.project.id)
            projects.append(task.project)
    history_events: list[dict[str, Any]] = []
    for task in tasks:
        history_events.append({
            "at": task.updated_at,
            "kind": "TASK",
            "title": task.title,
            "detail": f"{task.status} · {task.project.name if task.project else 'No project'}",
        })
    for row in run_audit:
        run = row["run"]
        history_events.append({
            "at": run.finished_at or run.started_at,
            "kind": "RUN",
            "title": f"Run #{run.id} · {run.purpose}",
            "detail": (
                f"{row['effective_status']} · {run.provider_key_snapshot} / {run.model_name_snapshot}"
                + (f" · {row['resolution']}" if row["resolution"] else "")
            ),
        })
    for record in learning:
        history_events.append({
            "at": record.created_at,
            "kind": "LEARNING",
            "title": record.title,
            "detail": _short(record.content, 150),
        })
    for item in model_history:
        history_events.append({
            "at": item.started_at,
            "kind": "AI_CORE",
            "title": item.model_config.label if item.model_config else "AI Core removed",
            "detail": item.reason or "Model assignment changed",
        })
    for contribution in contributions:
        history_events.append({
            "at": contribution.created_at,
            "kind": "CONTRIBUTION",
            "title": contribution.event_type.replace("_", " "),
            "detail": contribution.reason,
        })
    history_events.sort(
        key=lambda event: _dt(event["at"]) or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    for event in history_events:
        event["age"] = _age_label(event["at"])
    manager = employee.manager
    reports = Employee.query.filter_by(manager_id=employee.id, active=True).order_by(Employee.name).all()
    evolution = __import__(
        "eason_one.services.employee_evolution", fromlist=["employee_profile"]
    ).employee_profile(employee)
    market_profile = __import__(
        "eason_one.services.market", fromlist=["employee_market_profile"]
    ).employee_market_profile(employee)
    return {
        "company": get_company(),
        "employee": employee,
        "presence": presence,
        "initials": _initials(employee.name),
        "manager": manager,
        "reports": reports,
        "tasks": tasks,
        "active_tasks": active_tasks,
        "projects": projects,
        "runs": runs,
        "run_audit": run_audit,
        "model_history": model_history,
        "codex_policy": engineering_runtime.codex_readiness() if (
            employee.current_model and employee.current_model.provider_key == "codex"
        ) else None,
        "model_configs": ModelConfig.query.filter_by(active=True, archived=False).order_by(
            ModelConfig.provider_key, ModelConfig.label
        ).all(),
        "learning": learning,
        "evolution": evolution,
        "contributions": contributions,
        "meaningful_contribution": meaningful_total(employee.id),
        "meeting_participations": meeting_participations,
        "messages": messages,
        "feedback": feedback,
        "history": history_events[:80],
        "metrics": {
            "completed_tasks": sum(task.status == "DONE" for task in tasks),
            "active_tasks": len(active_tasks),
            "successful_runs": successful_runs,
            "failed_runs": failed_runs,
            "tokens": total_tokens,
            "real_cost": cost,
            "meetings": len(meeting_participations),
            "feedback": len(feedback),
            "salary": _money(employee.salary_credits_per_week),
        },
        "economy": {
            **market_profile,
            "salary": _money(employee.salary_credits_per_week),
            "overtime": Decimal("0"),
        },
        "work_policy": {
            "type": "FLEXIBLE / ON CALL",
            "regular_hours": "Founder-defined runtime policy pending",
            "current_session": presence["state_label"],
            "last_checkpoint": presence["last_update"],
        },
    }


def results_snapshot() -> dict[str, Any]:
    return {"company": get_company(), "results": _result_feed(50)}


def attention_snapshot() -> dict[str, Any]:
    view = current_company.projection()
    reliability = _reliability_groups(view["failures"])
    return {
        "company": get_company(),
        "governance": view["governance"],
        "failures": view["failures"],
        "reliability": reliability,
        "blocking": [row for row in reliability if row["blocking"]],
        "historical": [row for row in reliability if not row["blocking"]],
    }


def mobile_snapshot() -> dict[str, Any]:
    hq = headquarters_snapshot()
    if not hq.get("company"):
        return hq
    return {
        "company": hq["company"],
        "ceo": hq["ceo"],
        "ceo_state": hq["ceo_state"],
        "focus": hq["focus"],
        "missions": [row for row in hq["missions"] if row["classification"] != "TERMINAL"][:6],
        "employees": hq["employees"],
        "live_meeting": hq["live_meeting"],
        "meetings": hq["meetings"][:5],
        "activity": hq["activity"][:10],
        "attention": hq["attention"],
        "failures": hq["failures"],
        "latest_result": hq["latest_result"],
    }


def local_ceo_answer(query: str, mobile: bool = False, snapshot=None) -> dict[str, Any]:
    """Return a source-grounded local CEO briefing without invoking a provider."""
    snapshot = snapshot or headquarters_snapshot()
    text = (query or "").strip()
    lowered = text.lower()
    if not snapshot.get("company"):
        return {"title": "Company unavailable", "summary": "Seed Eason One before requesting a briefing.", "facts": []}

    employee_match = next(
        (
            row
            for row in snapshot["employees"]
            if row["employee"].name.lower() in lowered
            or row["employee"].slug.lower() in lowered
        ),
        None,
    )
    mission_match = next(
        (
            row
            for row in snapshot["missions"]
            if row["operation"].title.lower() in lowered
            or (row["project"] and row["project"].name.lower() in lowered)
        ),
        None,
    )

    if employee_match:
        task = employee_match["task"]
        facts = [
            f"Status: {employee_match['state_label']}",
            f"Department: {employee_match['department']}",
            f"Current work: {task.title if task else 'No active Task'}",
            f"Last authoritative update: {employee_match['last_update']}",
        ]
        if task and task.project:
            facts.append(f"Mission: {task.project.name}")
        return {
            "title": f"{employee_match['employee'].name} briefing",
            "summary": f"{employee_match['employee'].name} is {employee_match['state_label'].lower()}. "
            + (f"Current work is {task.title}." if task else "No active assignment is recorded."),
            "facts": facts,
            "href": f"/headquarters/employees/{employee_match['employee'].id}",
        }

    if mission_match:
        active_task = mission_match["active_task"]
        facts = [
            f"Stage: {mission_match['stage'].replace('_', ' ')}",
            f"Progress: {mission_match['done']} of {mission_match['total']} Tasks done",
            f"Status: {mission_match['classification'].replace('_', ' ')}",
            f"Next result: {mission_match['next_result']}",
        ]
        if active_task:
            facts.append(
                f"Current Task: {active_task.title} — {active_task.assigned_employee.name if active_task.assigned_employee else 'Unassigned'}"
            )
        return {
            "title": f"{mission_match['operation'].title} briefing",
            "summary": mission_match["summary"],
            "facts": facts,
            "href": f"/headquarters/missions/{mission_match['operation'].id}",
        }

    if any(word in lowered for word in ("meeting", "會議", "討論")):
        live = snapshot["live_meeting"]
        if not live:
            return {
                "title": "Meeting briefing",
                "summary": "No live Meeting is currently recorded.",
                "facts": [f"{len(snapshot['meetings'])} planned or paused Meetings remain visible."],
                "href": "/headquarters/meetings",
            }
        names = ", ".join(item["employee"].name for item in live["participants"])
        summary = live["summary"]
        brief = (
            summary.get("executive_summary")
            or summary.get("current_summary")
            or live["meeting"].agenda
        )
        return {
            "title": f"Live Meeting · {live['meeting'].title}",
            "summary": _short(brief, 240),
            "facts": [
                f"Participants: {names}",
                f"Round: {live['meeting'].current_round} / {live['meeting'].max_rounds}",
                f"Status: {live['meeting'].status.replace('_', ' ')}",
            ],
            "href": f"/headquarters/meetings/{live['meeting'].id}",
        }

    if any(word in lowered for word in ("who", "employee", "staff", "誰", "員工", "上班")):
        active = [
            row for row in snapshot["employees"] if row["state"] in ACTIVE_TASK_STATUSES | {"IN_MEETING"}
        ]
        facts = [
            f"{row['employee'].name}: {row['state_label']}"
            + (f" — {row['task'].title}" if row["task"] else "")
            for row in active
        ] or ["No Employee has an active persisted assignment."]
        return {
            "title": "Workforce briefing",
            "summary": f"{len(active)} Employees are actively assigned or in a Meeting.",
            "facts": facts[:8],
            "href": "/headquarters/people",
        }

    if any(word in lowered for word in ("attention", "decision", "需要我", "決定", "批准", "卡住", "blocked")):
        facts = [
            f"{row['type'].replace('_', ' ')} · {row.get('operation').title if row.get('operation') else 'Founder proposal'}"
            for row in snapshot["attention"]
        ]
        facts += [
            f"Failure · {row['workflow']} ({row['count']} occurrences)"
            for row in snapshot["failures"]
        ]
        return {
            "title": "Founder attention",
            "summary": "Nothing needs Founder action." if not facts else f"{len(facts)} items need review.",
            "facts": facts or ["CEO can continue without Founder intervention."],
            "href": "/headquarters/attention",
        }

    focus = snapshot["focus"]
    facts = [
        f"CEO state: {snapshot['ceo_state'].replace('_', ' ')}",
        f"Active Missions: {snapshot['metrics']['active_missions']}",
        f"Working Employees: {snapshot['metrics']['working_employees']}",
        f"Founder attention: {snapshot['metrics']['attention']}",
    ]
    if focus:
        facts.extend([
            f"Priority Mission: {focus['operation'].title}",
            f"Current stage: {focus['stage'].replace('_', ' ')}",
            f"Next result: {focus['next_result']}",
        ])
    return {
        "title": "CEO headquarters briefing",
        "summary": (
            f"Eason One is {snapshot['ceo_state'].replace('_', ' ').lower()}. "
            + (
                f"The current priority is {focus['operation'].title}."
                if focus
                else "No active Mission is recorded."
            )
        ),
        "facts": facts,
        "href": f"/headquarters/missions/{focus['operation'].id}" if focus else "/headquarters",
    }


def missions_snapshot(view: str = "real") -> dict[str, Any]:
    """Founder-facing Mission registry with real work separated from validation."""
    authoritative = current_company.projection()
    real_missions = [
        mission_view(row["operation"], row["classification"])
        for row in authoritative.get("real_operations", authoritative["operations"])
    ]
    validation_missions = [
        mission_view(row["operation"], row["classification"])
        for row in authoritative.get("validation_operations", [])
    ]
    selected = validation_missions if view == "validation" else real_missions
    groups = {"needs_founder": [], "active": [], "blocked": [], "completed": []}
    for mission in selected:
        if mission["classification"] == "WAITING_FOR_FOUNDER":
            groups["needs_founder"].append(mission)
        elif mission["classification"] == "BLOCKED":
            groups["blocked"].append(mission)
        elif mission["classification"] == "TERMINAL":
            groups["completed"].append(mission)
        else:
            groups["active"].append(mission)
    for rows in groups.values():
        rows.sort(key=lambda row: (row["operation"].updated_at, row["operation"].id), reverse=True)
    return {
        "company": get_company(),
        "view": view,
        "missions": selected,
        "real_missions": real_missions,
        "validation_missions": validation_missions,
        "groups": groups,
        "focus": _focus_mission(selected, include_validation=(view == "validation")),
        "global_runtime": global_runtime_snapshot(),
        "metrics": {
            "active": len(groups["active"]),
            "needs_founder": len(groups["needs_founder"]),
            "blocked": len(groups["blocked"]),
            "completed": len(groups["completed"]),
            "real_total": len(real_missions),
            "validation_total": len(validation_missions),
        },
    }


def people_snapshot() -> dict[str, Any]:
    employees = [
        employee_presence(employee)
        for employee in Employee.query.filter_by(active=True).order_by(Employee.id).all()
    ]
    departments: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in employees:
        departments[row["department"]].append(row)
    talent_source = __import__("eason_one.services.workforce", fromlist=["find_founder_talent_source"]).find_founder_talent_source()
    return {
        "company": get_company(),
        "employees": employees,
        "departments": dict(departments),
        "hr": {
            "director": Employee.query.filter_by(slug="hr-director").first(),
            "talent_count": TalentTemplate.query.filter_by(active_in_pool=True).count(),
            "open_requests": HiringRequest.query.filter(HiringRequest.status.in_(["REQUESTED", "HR_REVIEW", "FOUNDER_REVIEW", "SYSTEM_RECOVERY"])).count(),
            "source": str(talent_source) if talent_source else None,
            "source_ready": bool(talent_source),
        },
        "metrics": {
            "total": len(employees),
            "working": sum(
                row["state"] in {"ASSIGNED", "WORKING", "REVIEW", "BLOCKED", "IN_MEETING"}
                for row in employees
            ),
            "available": sum(row["state"] == "AVAILABLE" for row in employees),
            "departments": len(departments),
        },
    }


def talent_snapshot(query: str = "", department: str = "") -> dict[str, Any]:
    service = __import__("eason_one.services.workforce", fromlist=["find_founder_talent_source", "assessment_authorization"])
    source = service.find_founder_talent_source()
    candidates = TalentTemplate.query.filter_by(active_in_pool=True)
    if query:
        like = f"%{query}%"
        from sqlalchemy import String, cast, or_
        candidates = candidates.filter(or_(
            TalentTemplate.name.ilike(like),
            TalentTemplate.role_title.ilike(like),
            cast(TalentTemplate.skills_json, String).ilike(like),
        ))
    if department:
        candidates = candidates.filter(TalentTemplate.department_hint.ilike(f"%{department}%"))
    hr = Employee.query.filter_by(slug="hr-director").first()
    return {
        "company": get_company(),
        "hr": hr,
        "source": str(source) if source else None,
        "source_ready": bool(source),
        "candidates": candidates.order_by(TalentTemplate.role_title).all(),
        "requests": HiringRequest.query.order_by(HiringRequest.updated_at.desc()).all(),
        "authorization": service.assessment_authorization(hr),
        "metrics": {
            "talent": TalentTemplate.query.filter_by(active_in_pool=True).count(),
            "open_requests": HiringRequest.query.filter(HiringRequest.status.in_(["REQUESTED", "HR_REVIEW", "FOUNDER_REVIEW", "SYSTEM_RECOVERY"])).count(),
            "active_employees": Employee.query.filter_by(active=True).count(),
        },
    }


def meetings_snapshot() -> dict[str, Any]:
    meetings = [
        _meeting_view(meeting)
        for meeting in Meeting.query.order_by(Meeting.created_at.desc()).all()
    ]
    groups = {"live": [], "planned": [], "completed": []}
    for row in meetings:
        status = row["meeting"].status
        if row.get("historical"):
            groups["completed"].append(row)
        elif status in {"RUNNING", "PAUSED", "WAITING_FOR_FOUNDER", "ACTIVE"}:
            groups["live"].append(row)
        elif status in {"ENDED", "TERMINATED_BY_FOUNDER", "FAILED"}:
            groups["completed"].append(row)
        else:
            groups["planned"].append(row)
    projects = [
        project for project in Project.query.filter_by(environment="LIVE").order_by(Project.updated_at.desc()).all()
        if str(project.status or "").upper() not in TERMINAL_PROJECT_STATUSES
        and not __import__(
            "eason_one.services.project_company", fromlist=["_project_is_dormant_history"]
        )._project_is_dormant_history(project.id)
    ]
    return {
        "company": get_company(),
        "meetings": meetings,
        "groups": groups,
        "employees": Employee.query.filter_by(active=True).order_by(Employee.name).all(),
        "projects": projects,
        "profiles": meeting_service.PROFILES,
    }


def meeting_snapshot(meeting: Meeting) -> dict[str, Any]:
    row = _meeting_view(meeting)
    messages = MeetingMessage.query.filter_by(meeting_id=meeting.id).order_by(MeetingMessage.id).all()
    display = []
    latest_by_employee: dict[int, dict[str, Any]] = {}
    for message in messages:
        parsed = None
        if message.speaker_type == "EMPLOYEE":
            try:
                import json
                parsed = json.loads(message.content)
            except Exception:
                parsed = None
        item = {"message": message, "parsed": parsed}
        display.append(item)
        if message.employee_id:
            latest_by_employee[message.employee_id] = item

    # Live rooms keep at most four visible tiles.  Recently speaking Employees
    # are ordered first; silent participants remain in their original roster
    # order and stay rendered in the background so the live runner can promote
    # them immediately when they speak.
    spoken_tiles = []
    silent_tiles = []
    for order, participant in enumerate(row["participants"]):
        employee_id = participant["employee"].id
        latest = latest_by_employee.get(employee_id)
        tile = {
            **participant,
            "latest": latest,
            "last_message_id": latest["message"].id if latest else None,
            "roster_order": order,
        }
        (spoken_tiles if latest else silent_tiles).append(tile)
    spoken_tiles.sort(key=lambda item: item["last_message_id"], reverse=True)
    live_tiles = spoken_tiles + silent_tiles
    latest_employee_message = next(
        (item for item in reversed(display) if item["message"].employee_id),
        None,
    )

    runs = AgentRun.query.filter_by(meeting_id=meeting.id).order_by(AgentRun.id.desc()).all()
    return {
        "company": get_company(),
        "meeting": meeting,
        "row": row,
        "display": display,
        "messages": messages,
        "recent": display[-10:],
        "live_tiles": live_tiles,
        "active_speaker_id": latest_employee_message["message"].employee_id if latest_employee_message else None,
        "runs": runs,
        "attempts": len(runs),
        "call_breakdown": meeting_service.call_breakdown(meeting),
        "salvage": meeting_service.salvage_preview(meeting),
        "primary_status": meeting_service.primary_status_text(meeting),
        "result": meeting_service.result_view(meeting),
        "feedback_signals": sorted(meeting_service.SIGNALS),
    }


def memory_snapshot() -> dict[str, Any]:
    source_items = KnowledgeItem.query.order_by(KnowledgeItem.created_at.desc()).all()
    projects = {project.id: project for project in Project.query.all()}
    employees = {employee.id: employee for employee in Employee.query.all()}
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    seen: set[tuple[Any, ...]] = set()
    items = []
    duplicates = 0
    for item in source_items:
        # The audit rows remain in the database. The Founder view collapses exact
        # duplicates so smoke-test retries do not look like separate knowledge.
        key = (
            item.kind,
            item.project_id,
            " ".join((item.title or "").split()).casefold(),
            " ".join((item.content or "").split()).casefold(),
            bool(item.founder_approved),
        )
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        items.append(item)
        groups[item.kind].append({
            "item": item,
            "project": projects.get(item.project_id),
            "origin": employees.get(item.origin_employee_id),
        })
    preferred = ["FACT", "HYPOTHESIS", "EVIDENCE", "DECISION", "CORRECTION", "KILLED", "LESSON", "CONSTRAINT"]
    ordered_groups = []
    for kind in preferred + sorted(set(groups) - set(preferred)):
        if groups.get(kind):
            ordered_groups.append((kind, groups[kind]))
    return {
        "company": get_company(),
        "items": items,
        "groups": ordered_groups,
        "duplicates_hidden": duplicates,
        "metrics": {
            "total": len(items),
            "approved": sum(bool(item.founder_approved) for item in items),
            "projects": len({item.project_id for item in items if item.project_id}),
            "types": len(groups),
        },
    }


def system_runs_snapshot(limit: int = 100) -> dict[str, Any]:
    view = current_company.projection()
    runs = AgentRun.query.order_by(AgentRun.started_at.desc()).limit(limit).all()
    employees = {employee.id: employee for employee in Employee.query.all()}
    operations = {operation.id: operation for operation in Operation.query.all()}
    rows = []
    for run in runs:
        rows.append({
            "run": run,
            "employee": employees.get(run.employee_id),
            "operation": operations.get(run.operation_id),
            "age": _age_label(run.finished_at or run.started_at),
        })
    return {
        "company": get_company(),
        "runs": rows,
        "failures": view["failures"],
        "metrics": {
            "total": len(rows),
            "failed": sum(row["run"].status == "FAILED" for row in rows),
            "running": sum(row["run"].status in {"CREATED", "RUNNING"} for row in rows),
            "succeeded": sum(row["run"].status == "SUCCEEDED" for row in rows),
        },
    }


def run_snapshot(run: AgentRun) -> dict[str, Any]:
    duration_ms = None
    if run.started_at and run.finished_at:
        duration_ms = max(0, round((run.finished_at - run.started_at).total_seconds() * 1000, 2))
    cost_scope = (
        "PLANNING"
        if run.purpose == "CEO_FOUNDER_REQUEST" and run.operation_id
        else "EXECUTION"
        if run.operation_id
        else "COMPANY"
    )
    return {
        "company": get_company(),
        "run": run,
        "duration_ms": duration_ms,
        "cost_scope": cost_scope,
        "employee": db.session.get(Employee, run.employee_id),
        "project": db.session.get(Project, run.project_id) if run.project_id else None,
        "task": db.session.get(Task, run.task_id) if run.task_id else None,
        "meeting": db.session.get(Meeting, run.meeting_id) if run.meeting_id else None,
        "operation": db.session.get(Operation, run.operation_id) if run.operation_id else None,
    }


def finance_snapshot() -> dict[str, Any]:
    company = get_company()
    events = CostEvent.query.order_by(CostEvent.created_at.desc(), CostEvent.id.desc()).limit(80).all()
    run_ids = {row.agent_run_id for row in events if row.agent_run_id}
    employee_ids = {row.employee_id for row in events if row.employee_id}
    project_ids = {row.project_id for row in events if row.project_id}
    operation_ids = {row.operation_id for row in events if row.operation_id}
    runs = {row.id: row for row in AgentRun.query.filter(AgentRun.id.in_(run_ids)).all()} if run_ids else {}
    employees = {row.id: row for row in Employee.query.filter(Employee.id.in_(employee_ids)).all()} if employee_ids else {}
    projects = {row.id: row for row in Project.query.filter(Project.id.in_(project_ids)).all()} if project_ids else {}
    operations = {row.id: row for row in Operation.query.filter(Operation.id.in_(operation_ids)).all()} if operation_ids else {}
    event_rows = []
    for event in events:
        run = runs.get(event.agent_run_id)
        cost_truth = (
            __import__("eason_one.services.costs", fromlist=["cost_truth_for_run"]).cost_truth_for_run(run)
            if run else {"kind": "LOCAL_LEDGER", "label": "Local cost ledger", "provider_billed_twd": None}
        )
        event_rows.append({
            "event": event,
            "employee": employees.get(event.employee_id),
            "project": projects.get(event.project_id),
            "operation": operations.get(event.operation_id),
            "run": run,
            "cost_truth": cost_truth,
            "age": _age_label(event.created_at),
        })
    # Finance only needs envelope truth. Avoid building the complete operational
    # projection (runtime, brain, reliability, staffing) for a ledger page.
    terminal = {"COMPLETED", "FAILED", "TERMINATED_BY_FOUNDER", "SUPERSEDED", "CANCELLED"}
    operation_rows = Operation.query.order_by(Operation.updated_at.desc()).all()
    current_operations = [row for row in operation_rows if row.status not in terminal]
    selected_operations = current_operations + [row for row in operation_rows if row.status in terminal][:8]
    selected_ids = [row.id for row in selected_operations]
    spend_by_operation = dict(db.session.query(
        CostEvent.operation_id, func.coalesce(func.sum(CostEvent.real_cost_delta), 0)
    ).filter(CostEvent.operation_id.in_(selected_ids)).group_by(CostEvent.operation_id).all()) if selected_ids else {}
    missions = []
    for operation in selected_operations:
        is_terminal = operation.status in terminal or bool(operation.project and operation.project.status in {"COMPLETED", "CANCELLED", "FAILED"})
        classification = "TERMINAL" if is_terminal else (
            "WAITING_FOR_FOUNDER" if operation.status in {"PLANNED", "WAITING_FOR_FOUNDER"}
            else "BLOCKED" if operation.status == "PAUSED" else "ACTIVE"
        )
        missions.append({
            "operation": operation,
            "project": operation.project,
            "classification": classification,
            "spent": _money(spend_by_operation.get(operation.id, 0)),
            "budget": _money(operation.approved_budget_twd),
        })
    company_spent = __import__("eason_one.services.company", fromlist=["spent"]).spent()
    company_remaining = __import__("eason_one.services.company", fromlist=["remaining"]).remaining()
    payroll = db.session.query(func.coalesce(func.sum(Employee.salary_credits_per_week), 0)).filter(Employee.active.is_(True)).scalar()
    return {
        "company": company,
        "spent": _money(company_spent),
        "remaining": _money(company_remaining),
        "limit": _money(company.real_budget_limit if company else 0),
        "missions": missions,
        "events": event_rows,
        "payroll_credits": _money(payroll),
        "codex_plan_runs": AgentRun.query.filter(AgentRun.provider_key_snapshot == "codex").count(),
        "provider_billing_available": False,
        "metrics": {
            "events": len(event_rows),
            "active_mission_budgets": sum((row["budget"] for row in missions if row["classification"] != "TERMINAL"), Decimal(0)),
            "active_mission_spend": sum((row["spent"] for row in missions if row["classification"] != "TERMINAL"), Decimal(0)),
        },
    }


def system_snapshot() -> dict[str, Any]:
    company = get_company()
    provider_rows = current_company.provider_health()
    failures = current_company.failure_groups()
    models = []
    for health in provider_rows:
        model = health["model"]
        models.append({
            **health,
            "assigned": Employee.query.filter_by(current_model_config_id=model.id, active=True).count(),
            "runs": AgentRun.query.filter_by(model_config_id=model.id).count(),
        })
    latest = AgentRun.query.order_by(AgentRun.started_at.desc(), AgentRun.id.desc()).limit(8).all()
    return {
        "company": company,
        "ceo_state": "SYSTEM_FAILURE" if any(row.get("blocking") for row in failures) else "AVAILABLE",
        "providers": models,
        "failures": failures,
        "reliability": _reliability_groups(failures),
        "latest_runs": latest,
        "metrics": {
            "models": len(models),
            "healthy": sum(row["healthy"] for row in models),
            "failures": sum(bool(row.get("blocking")) for row in failures),
            "reliability": len(_reliability_groups(failures)),
            "runs": AgentRun.query.count(),
        },
    }


def models_snapshot() -> dict[str, Any]:
    view = current_company.projection()
    health_by_id = {row["model"].id: row for row in view["provider_health"]}
    rows = []
    for model in ModelConfig.query.order_by(ModelConfig.archived, ModelConfig.active.desc(), ModelConfig.created_at.desc()):
        health = health_by_id.get(model.id) or {
            "healthy": False,
            "summary": "Inactive or archived configuration.",
            "recovery": None,
            "technical_detail": None,
        }
        rows.append({
            "model": model,
            "health": health,
            "employees": Employee.query.filter_by(current_model_config_id=model.id).order_by(Employee.name).all(),
            "runs": AgentRun.query.filter_by(model_config_id=model.id).count(),
        })
    return {
        "company": view["company"],
        "rows": rows,
        "metrics": {
            "total": len(rows),
            "active": sum(row["model"].active and not row["model"].archived for row in rows),
            "assigned": sum(bool(row["employees"]) for row in rows),
        },
    }


def ceo_dialogue_view(limit: int = 8) -> list[dict[str, Any]]:
    rows = (
        WorkMessage.query.filter(
            WorkMessage.message_type.in_({"FOUNDER_TO_CEO", "CEO_TO_FOUNDER"})
        )
        .order_by(WorkMessage.id.desc())
        .limit(limit)
        .all()
    )
    rows.reverse()
    rendered=[]
    for row in rows:
        run=db.session.get(AgentRun,row.agent_run_id) if row.agent_run_id else None
        operation=db.session.get(Operation,run.operation_id) if run and run.operation_id else None
        action_href=None
        action_label=None
        if operation and operation_kind(operation)==REAL_WORK:
            action_href=f"/headquarters/missions/{operation.id}"
            action_label=(
                "REVIEW PROPOSAL →" if operation.status=="PLANNED"
                else "OPEN MISSION →"
            )
        rendered.append({
            "id": row.id,
            "role": "CEO" if row.message_type == "CEO_TO_FOUNDER" else "FOUNDER",
            "content": row.content,
            "run_id": row.agent_run_id,
            "project_id": row.project_id,
            "task_id": row.task_id,
            "action_href":action_href,
            "action_label":action_label,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        })
    return rendered


def operation_live_snapshot(operation: Operation) -> dict[str, Any]:
    mission = mission_view(operation)
    meeting = (
        Meeting.query.filter_by(operation_id=operation.id)
        .order_by(Meeting.id.desc())
        .first()
    )
    meeting_view = _meeting_view(meeting) if meeting else None
    latest_step = (
        OperationStep.query.filter_by(operation_id=operation.id)
        .order_by(OperationStep.id.desc())
        .first()
    )
    workers = []
    seen = set()
    project_terminal = bool(
        operation.project is not None
        and str(operation.project.status or "").upper() in {"COMPLETED", "CANCELLED", "FAILED"}
    )
    company_runtime = __import__(
        "eason_one.services.company_runtime", fromlist=["is_work_vnext", "runtime_snapshot"]
    )
    is_vnext = company_runtime.is_work_vnext(operation)
    runtime = (
        company_runtime.runtime_snapshot(operation)
        if is_vnext
        else __import__(
            "eason_one.services.operation_runtime", fromlist=["runtime_snapshot"]
        ).runtime_snapshot(operation)
    )
    runtime_task_id = (runtime.get("active_task") or {}).get("id")
    active_work_id = (runtime.get("active_work") or {}).get("id")
    if is_vnext and not project_terminal:
        work_runtime = __import__(
            "eason_one.services.work_runtime", fromlist=["active_assignment"]
        )
        for work in sorted(operation.works, key=lambda row: row.id):
            if work.work_type == "MANAGEMENT" or work.state not in {"READY", "EXECUTING", "VERIFYING", "WAITING"}:
                continue
            assignment = work_runtime.active_assignment(work)
            employee = getattr(assignment, "employee", None)
            if not employee:
                continue
            seen.add(employee.id)
            control = dict(work.runtime_control_json or {})
            staffing = dict(control.get("team_formation") or {})
            required = list((control.get("staffing_requirements") or {}).get("required_capabilities") or [])
            open_gate = next((
                dict(row) for row in control.get("gates") or []
                if row.get("state") == "OPEN"
            ), {})
            visible_status = "LIVE" if work.state == "EXECUTING" else work.state
            if work.state == "READY" and not staffing.get("state") and required:
                visible_status = "MATCHING"
            workers.append({
                "id": employee.id,
                "work_id": work.id,
                "name": employee.name,
                "task": work.title,
                "status": visible_status,
                "role": employee.position.name if employee.position else "Employee",
                "required_capability": required[0] if required else None,
                "staffing_state": staffing.get("state") or ("MATCHING" if required else None),
                "wait_reason": open_gate.get("reason") if work.state == "WAITING" else None,
            })
    elif not project_terminal:
        for task in mission["tasks"]:
            employee = task.assigned_employee
            is_runtime_task = bool(runtime.get("worker_alive") and task.id == runtime_task_id)
            if not employee or employee.id in seen or not is_runtime_task:
                continue
            seen.add(employee.id)
            workers.append({
                "id": employee.id,
                "name": employee.name,
                "task": task.title,
                "status": "WORKING" if is_runtime_task else task.status,
                "role": employee.position.name if employee.position else "Employee",
                "wait_reason": None,
            })
    if meeting and not project_terminal:
        for participant in meeting.participants:
            employee = participant.employee
            if employee and employee.id not in seen and participant.removed_at is None:
                seen.add(employee.id)
                workers.append({
                    "id": employee.id,
                    "name": employee.name,
                    "task": meeting.title,
                    "status": "IN_MEETING" if meeting.status == "RUNNING" else meeting.status,
                    "role": employee.position.name if employee.position else "Employee",
                })
    report = operation.founder_report_json or {}
    progress = mission["progress"]
    meeting_tokens, meeting_cost = meeting_service.usage(meeting) if meeting else (0, Decimal("0"))
    meeting_config = ((operation.plan_json or {}).get("operation") or {}).get("meeting_config") or {}
    return {
        "operation_id": operation.id,
        "is_vnext": bool(is_vnext),
        "title": operation.title,
        "status": operation.status,
        "kernel_status": runtime.get("kernel_status"),
        "route_type": operation.route_type,
        "route_reason": operation.route_reason,
        "current_stage": operation.current_stage,
        "events_count": runtime.get("events_count",0),
        "budget_authority": runtime.get("budget"),
        "can_resume": False if is_vnext else bool(runtime.get("can_resume")),
        "can_archive": bool(runtime.get("can_archive")),
        "archived": bool(runtime.get("archived")),
        "resume_label": (
            "RESUME ENGINEER → CODEX"
            if operation.status == "PAUSED" and (operation.founder_report_json or {}).get("decision_kind") == "ENGINEERING_RUNTIME_REPAIR"
            else "RESUME CEO"
        ),
        "archive_label": "ARCHIVED VALIDATION" if runtime.get("archived") else "ARCHIVE VALIDATION MISSION",
        "stage": mission["stage"],
        "progress": progress,
        "done": mission["done"],
        "total": mission["total"],
        "budget": str(mission["budget"]),
        "spent": str(mission["spent"]),
        "remaining": str(mission["remaining"]),
        "company_spent": str(current_company.spent()),
        "current_task": runtime.get("active_work") if is_vnext else runtime.get("active_task"),
        "next_task": None if is_vnext else runtime.get("next_task"),
        "next_step": (
            {
                "task_id": mission["next_step"]["task"].id,
                "task": mission["next_step"]["task"].title,
                "employee": mission["next_step"]["employee"].name if mission["next_step"].get("employee") else None,
                "provider": mission["next_step"].get("provider"),
                "model": mission["next_step"]["model"].model_name if mission["next_step"].get("model") else None,
                "paid": mission["next_step"].get("paid"),
                "estimate_twd": str(mission["next_step"].get("estimate_twd") or 0),
                "requires_founder": mission["next_step"].get("requires_founder"),
            }
            if mission.get("next_step") else None
        ),
        "workers": workers,
        "meeting": (
            {
                "id": meeting.id,
                "title": meeting.title,
                "status": meeting.status,
                "round": meeting.current_round,
                "max_rounds": meeting.max_rounds,
                "token_limit": meeting.token_limit,
                "budget_twd": str(meeting.real_cost_limit_twd),
                "spent_twd": str(meeting_cost),
                "tokens_used": meeting_tokens,
                "retry_limit": int(meeting_config.get("retry_limit", 0)),
                "summary": meeting.current_summary_json or {},
                "result": meeting_service.result_view(meeting),
            }
            if meeting else None
        ),
        "latest_event": (
            {
                "kind": latest_step.kind,
                "status": latest_step.status,
                "detail": _operation_event_text(latest_step, operation, progress),
            }
            if latest_step else None
        ),
        "waiting_reason": operation.waiting_reason,
        "budget_request": (
            {
                "additional_twd": report.get("additional_budget_twd"),
                "resulting_authorized_twd": report.get("resulting_authorized_twd"),
                "reason": report.get("summary") or operation.waiting_reason,
            }
            if report.get("decision_kind") == "BUDGET_AUTHORIZATION"
            else None
        ),
        "founder_report": report if operation.status == "COMPLETED" else None,
        "continue_allowed": operation.status == "RUNNING",
        "runtime": runtime,
        "href": (
            f"/headquarters/projects/{operation.project_id}"
            if operation.project_id else f"/headquarters/missions/{operation.id}"
        ),
        "project_id": operation.project_id,
        "meeting_href": f"/headquarters/meetings/{meeting.id}" if meeting else None,
    }
