"""Truthful Engineer -> Codex recovery and audit helpers.

This module keeps one narrow rule authoritative:
formal Mission work may never treat MockProvider output as Employee evidence,
and a repaired Engineering Mission resumes from the earliest unfinished
Engineer Task exactly once before pausing for Founder review.
"""
from __future__ import annotations

import os
from datetime import timezone

from ..extensions import db
from ..models import AgentRun, Employee, EmployeeModelHistory, Operation, OperationStep, Task, now

INVALID_FORMAL_MOCK = "INVALID_FORMAL_MOCK"
ENGINEERING_REPAIR_GATE = "ENGINEERING_RUNTIME_REPAIR"
ENGINEERING_STEP_REVIEW_GATE = "ENGINEERING_STEP_REVIEW"


def run_is_invalidated(run: AgentRun | None) -> bool:
    if not run:
        return False
    return bool(
        run.status == "INVALIDATED"
        or run.resolution_status == INVALID_FORMAL_MOCK
        or (
            run.provider_key_snapshot == "mock"
            and run.purpose == "TASK_EXECUTION"
            and run.operation_id is not None
        )
    )


def run_is_authoritative_success(run: AgentRun | None) -> bool:
    return bool(run and run.status == "SUCCEEDED" and not run_is_invalidated(run))


def effective_run_status(run: AgentRun) -> str:
    return "INVALIDATED" if run_is_invalidated(run) else run.status


def codex_readiness() -> dict:
    descriptor = __import__(
        "eason_one.services.codex_connector",
        fromlist=["codex_runtime_descriptor"],
    ).codex_runtime_descriptor()
    default_repo = os.getenv("EASON_ONE_CODEX_DEFAULT_REPO")
    raw_allowed = os.getenv("EASON_ONE_CODEX_ALLOWED_REPOS", "")
    allowed = [item for item in raw_allowed.split(os.pathsep) if item.strip()]
    return {
        "ready": bool(descriptor.get("ready")),
        "transport": descriptor.get("transport"),
        "path": descriptor.get("display_path") or descriptor.get("path"),
        "linux_path": descriptor.get("path") if descriptor.get("transport") == "wsl" else None,
        "distro": descriptor.get("distro"),
        "version": descriptor.get("version"),
        "login": descriptor.get("login"),
        "error": descriptor.get("error"),
        "default_repo": default_repo,
        "allowed_repos": allowed,
        "max_concurrent_jobs": 1,
        "formal_mock_fallback": "DISABLED",
        "dependency_install": "FOUNDER APPROVAL REQUIRED",
        "database_migration": "FOUNDER APPROVAL REQUIRED",
        "deployment": "FOUNDER APPROVAL REQUIRED",
    }


def _valid_codex_success(task: Task) -> AgentRun | None:
    return (
        AgentRun.query.filter(
            AgentRun.task_id == task.id,
            AgentRun.status == "SUCCEEDED",
            AgentRun.provider_key_snapshot == "codex",
            AgentRun.structured_validation_status == "PASSED",
        )
        .order_by(AgentRun.id.desc())
        .first()
    )


def earliest_engineer_repair_task(operation: Operation) -> Task | None:
    for task in operation.tasks:
        if not task.assigned_employee or task.assigned_employee.slug != "engineer":
            continue
        if task.status in {"CANCELLED"}:
            continue
        if task.status == "DONE" and _valid_codex_success(task):
            continue
        return task
    return None


def invalidate_formal_mock_run(run: AgentRun) -> None:
    if not (
        run.provider_key_snapshot == "mock"
        and run.purpose == "TASK_EXECUTION"
        and run.operation_id is not None
    ):
        return
    run.status = "INVALIDATED"
    run.resolution_status = INVALID_FORMAL_MOCK
    run.structured_validation_status = "INVALIDATED"
    run.resolution_note = (
        "Formal Mission output was invalidated because MockProvider cannot produce "
        "authoritative Employee evidence. Audit is retained; contribution, success, "
        "Artifact, acceptance, and dependency credit are denied."
    )
    run.resolved_at = run.resolved_at or now()
    task = db.session.get(Task, run.task_id) if run.task_id else None
    if task and not _valid_codex_success(task):
        task.status = "ASSIGNED"
        task.result_summary = None
        task.completed_at = None
    for step in OperationStep.query.filter_by(agent_run_id=run.id).all():
        step.status = "INVALIDATED"
        step.error_text = run.resolution_note
        step.finished_at = step.finished_at or now()


def ensure_codex_model_history(engineer: Employee, codex_model) -> None:
    open_rows = (
        EmployeeModelHistory.query.filter_by(employee_id=engineer.id, ended_at=None)
        .order_by(EmployeeModelHistory.started_at.desc())
        .all()
    )
    active_codex = next((row for row in open_rows if row.model_config_id == codex_model.id), None)
    for row in open_rows:
        if row is active_codex:
            continue
        row.ended_at = now()
        if row.model_config and row.model_config.provider_key == "mock":
            row.reason = (
                (row.reason + " · " if row.reason else "")
                + "RETIRED / SIMULATION ONLY; formal Mission fallback disabled."
            )
    if not active_codex:
        db.session.add(EmployeeModelHistory(
            employee_id=engineer.id,
            model_config_id=codex_model.id,
            reason=(
                "Founder-approved Engineering runtime: Engineer reports to CEO and "
                "uses the signed-in local Codex CLI. One concurrent bounded tool job; "
                "formal Mock fallback disabled."
            ),
            started_at=now(),
        ))


def prepare_engineering_repair(operation: Operation, *, activate: bool = False) -> Task:
    """Set one deterministic Engineer/Codex cursor without launching Codex."""
    if __import__(
        "eason_one.services.core_v018", fromlist=["bypass_legacy_operation_runtime"]
    ).bypass_legacy_operation_runtime(operation):
        if __import__(
            "eason_one.services.core_v018", fromlist=["is_v018_operation"]
        ).is_v018_operation(operation):
            __import__("eason_one.services.company_runtime", fromlist=["wake_company_runtime"]).wake_company_runtime()
        raise ValueError("Work-first Projects are not recovered by the legacy Engineer repair cursor.")
    target = earliest_engineer_repair_task(operation)
    if not target:
        raise ValueError("No unfinished Engineer Task is available for Codex recovery")

    # A prior paid downstream task remains audit evidence, but it cannot remain
    # the visible/active cursor while its prerequisite Engineer evidence is absent.
    target_index = list(operation.tasks).index(target)
    for index, task in enumerate(operation.tasks):
        if task.assigned_employee and task.assigned_employee.slug == "engineer":
            task.reviewer_employee_id = None
        if index < target_index:
            continue
        if task.id == target.id:
            task.status = "ASSIGNED"
            task.result_summary = None
            task.completed_at = None
        elif task.status in {"WORKING", "REVIEW", "BLOCKED", "FAILED"}:
            task.status = "ASSIGNED"
            task.result_summary = None
            task.completed_at = None

    memory = dict(operation.memory_json or {})
    runtime = dict(memory.get("runtime") or {})
    engineering = dict(memory.get("engineering") or {})
    engineering.update({
        "connector": "codex-cli",
        "resume_task_id": target.id,
        "stop_after_current_step": True,
        "codex_retry_limit": min(int(engineering.get("codex_retry_limit", 1) or 1), 1),
        "max_concurrent_jobs": 1,
        "formal_mock_fallback": False,
    })
    runtime.update({
        "state": "STARTING" if activate else "PAUSED",
        "worker_alive": False,
        "active_task_id": target.id,
        "last_error": None if activate else operation.waiting_reason,
    })
    memory["engineering"] = engineering
    memory["runtime"] = runtime
    operation.memory_json = memory
    if activate:
        kernel=__import__("eason_one.services.operation_kernel",fromlist=["authoritative_status","enqueue","transition"])
        status=kernel.authoritative_status(operation)
        if status in {"WAITING_INPUT","WAITING_APPROVAL"}:
            kernel.enqueue(operation,actor_type="FOUNDER",reason="Founder activated the bounded Engineer repair.")
        elif status not in {"QUEUED","RUNNING"}:
            kernel.transition(operation,"QUEUED","ENGINEERING_REPAIR_QUEUED",stage="QUEUED",force=True,commit=False)
        operation.waiting_reason = None
        operation.founder_report_json = None
    db.session.commit()
    return target


def pause_after_engineer_step(operation: Operation, *, task: Task, run: AgentRun | None) -> None:
    actual = __import__(
        "eason_one.services.operations", fromlist=["actual_cost"]
    ).actual_cost(operation)
    __import__("eason_one.services.operation_kernel",fromlist=["transition"]).transition(
        operation,"WAITING_INPUT","ENGINEER_STEP_CHECKPOINT",stage="FOUNDER_REVIEW",commit=False,force=True,
        payload={"task_id":task.id,"agent_run_id":getattr(run,"id",None)},
    )
    operation.waiting_reason = (
        f"Engineer completed '{task.title}' through Codex. The Mission stopped after "
        "this bounded step; no Critic, Meeting, or other paid Provider was started."
    )
    operation.founder_report_json = {
        "decision_kind": ENGINEERING_STEP_REVIEW_GATE,
        "headline": "Engineer Codex step completed.",
        "summary": operation.waiting_reason,
        "next_move": "Review the Codex evidence, then explicitly resume the next Task or stop the test Mission.",
        "approved_twd": str(operation.approved_budget_twd),
        "actual_twd": str(actual),
        "remaining_twd": str(operation.approved_budget_twd - actual),
        "additional_budget_twd": "0",
        "resulting_authorized_twd": str(operation.approved_budget_twd),
        "engineer_task_id": task.id,
        "codex_run_id": getattr(run, "id", None),
    }
    memory = dict(operation.memory_json or {})
    engineering = dict(memory.get("engineering") or {})
    engineering["stop_after_current_step"] = False
    engineering["last_codex_run_id"] = getattr(run, "id", None)
    runtime = dict(memory.get("runtime") or {})
    runtime.update({
        "state": "PAUSED",
        "worker_alive": False,
        "active_task_id": None,
        "last_result": {
            "kind": "ENGINEER_CODEX_STEP_COMPLETE",
            "task_id": task.id,
            "run_id": getattr(run, "id", None),
        },
        "last_error": None,
    })
    memory["engineering"] = engineering
    memory["runtime"] = runtime
    operation.memory_json = memory
    db.session.commit()

VALIDATION_ARCHIVE = "RUNTIME_VALIDATION_ARCHIVE"


def archive_validation_mission(
    operation: Operation,
    *,
    reason: str | None = None,
    auto: bool = False,
) -> dict:
    """Close a bounded runtime-validation Mission without fabricating delivery.

    The completed Engineer/Codex evidence remains authoritative. Unstarted
    downstream Tasks are cancelled, never marked DONE, and no Provider is
    called. The Mission becomes a terminal validation archive with an explicit
    Founder-readable record in memory_json and founder_report_json.
    """
    live_run = AgentRun.query.filter(
        AgentRun.operation_id == operation.id,
        AgentRun.status.in_(["CREATED", "RUNNING"]),
    ).order_by(AgentRun.id.desc()).first()
    if live_run:
        raise ValueError(
            f"Run #{live_run.id} is still active. Pause or stop the current step before archiving."
        )
    if operation.status == "RUNNING":
        raise ValueError("A RUNNING Mission cannot be archived.")

    valid_runs = (
        AgentRun.query.filter(
            AgentRun.operation_id == operation.id,
            AgentRun.provider_key_snapshot == "codex",
            AgentRun.status == "SUCCEEDED",
            AgentRun.structured_validation_status == "PASSED",
        )
        .order_by(AgentRun.id)
        .all()
    )
    if not valid_runs:
        raise ValueError(
            "This validation Mission has no successful Engineer/Codex evidence to archive."
        )

    completed_task_ids: list[int] = []
    cancelled_task_ids: list[int] = []
    for task in operation.tasks:
        if task.status == "DONE" and _valid_codex_success(task):
            completed_task_ids.append(task.id)
            continue
        if task.status not in {"DONE", "CANCELLED"}:
            task.status = "CANCELLED"
            task.completed_at = None
            task.result_summary = task.result_summary or (
                "Cancelled when the Founder archived this bounded runtime-validation Mission."
            )
        if task.status == "CANCELLED":
            cancelled_task_ids.append(task.id)

    archive_reason = reason or (
        "Founder archived this Mission after the Engineer/WSL2 Codex execution chain "
        "was validated. Downstream Critic, Meeting, and paid Provider steps were intentionally not run."
    )
    archived_at = now()
    __import__("eason_one.services.operation_kernel",fromlist=["transition"]).transition(
        operation,"COMPLETED","VALIDATION_ARCHIVED",stage="COMPLETED",commit=False,force=True,
        payload={"auto":bool(auto),"codex_run_ids":[run.id for run in valid_runs]},
    )
    operation.ended_at = archived_at
    operation.waiting_reason = archive_reason
    operation.founder_report_json = {
        "decision_kind": VALIDATION_ARCHIVE,
        "headline": "Runtime validation archived.",
        "summary": archive_reason,
        "next_move": "Use the retained evidence to improve Eason One; do not continue this test Mission.",
        "validation_case": True,
        "auto_archived_by_migration": bool(auto),
        "completed_task_ids": completed_task_ids,
        "cancelled_task_ids": cancelled_task_ids,
        "codex_run_ids": [run.id for run in valid_runs],
        "actual_twd": str(operation.actual_cost_twd or 0),
        "approved_twd": str(operation.approved_budget_twd),
    }
    memory = dict(operation.memory_json or {})
    archive = dict(memory.get("archive") or {})
    archive.update({
        "kind": VALIDATION_ARCHIVE,
        "archived_at": archived_at.isoformat(),
        "reason": archive_reason,
        "completed_task_ids": completed_task_ids,
        "cancelled_task_ids": cancelled_task_ids,
        "codex_run_ids": [run.id for run in valid_runs],
    })
    runtime = dict(memory.get("runtime") or {})
    live = dict(runtime.get("live_execution") or {})
    live.update({
        "state": "ARCHIVED",
        "phase": "ARCHIVED",
        "detail": "Runtime validation evidence was archived; no further Employee or Provider work will start.",
        "current_action": None,
        "last_activity_at": archived_at.isoformat(),
        "finished_at": archived_at.isoformat(),
        "run_id": valid_runs[-1].id,
        "task_id": completed_task_ids[-1] if completed_task_ids else None,
    })
    events = list(live.get("events") or [])
    events.append({
        "at": archived_at.isoformat(),
        "kind": "ARCHIVE",
        "phase": "ARCHIVED",
        "detail": archive_reason,
    })
    live["events"] = events[-50:]
    runtime.update({
        "state": "ARCHIVED",
        "worker_alive": False,
        "active_task_id": None,
        "heartbeat_at": archived_at.isoformat(),
        "finished_at": archived_at.isoformat(),
        "last_error": None,
        "live_execution": live,
    })
    memory["archive"] = archive
    memory["runtime"] = runtime
    operation.memory_json = memory

    existing = OperationStep.query.filter_by(
        operation_id=operation.id,
        logical_key="runtime-validation-archive",
    ).first()
    if not existing:
        db.session.add(OperationStep(
            operation_id=operation.id,
            idempotency_key=f"archive-validation:{operation.id}",
            logical_key="runtime-validation-archive",
            kind="ARCHIVE",
            status="SUCCEEDED",
            result_json={
                "kind": VALIDATION_ARCHIVE,
                "completed_task_ids": completed_task_ids,
                "cancelled_task_ids": cancelled_task_ids,
                "codex_run_ids": [run.id for run in valid_runs],
            },
            finished_at=archived_at,
        ))
    db.session.commit()
    return {
        "operation_id": operation.id,
        "status": operation.status,
        "archive_kind": VALIDATION_ARCHIVE,
        "completed_task_ids": completed_task_ids,
        "cancelled_task_ids": cancelled_task_ids,
        "codex_run_ids": [run.id for run in valid_runs],
    }
