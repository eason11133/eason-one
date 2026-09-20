"""Durable local Operation runtime for Eason One V0.12.

Threads are only execution helpers. The database owns queue state, worker lease,
checkpoint, events, counters, and budget authority, so a page change or process
restart cannot erase the authoritative run.
"""
from __future__ import annotations

import threading
import time
import uuid
from datetime import datetime, timezone

from flask import current_app

from ..extensions import db
from ..models import AgentRun, Meeting, Operation, OperationEvent, Task
from . import operation_kernel as kernel

_WORKERS: dict[int, threading.Thread] = {}
_LOCK = threading.RLock()
MAX_RUNTIME_STEPS = 48


def _owned_by_company_runtime(operation: Operation | None) -> bool:
    """Work-first rows never run in the retired Operation worker."""
    return __import__(
        "eason_one.services.core_v018", fromlist=["bypass_legacy_operation_runtime"]
    ).bypass_legacy_operation_runtime(operation)


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _seconds_since(value: str | None) -> int | None:
    parsed = _parse_iso(value)
    if not parsed:
        return None
    return max(0, int((datetime.now(timezone.utc) - parsed).total_seconds()))


def _lease_alive(operation: Operation) -> bool:
    if not operation.lease_owner or not operation.lease_expires_at:
        return False
    expires = operation.lease_expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return expires > datetime.now(timezone.utc)


def _sync_legacy_mutation(operation: Operation) -> None:
    """Import one remaining legacy direct assignment into the kernel event log."""
    authoritative = kernel.authoritative_status(operation)
    expected = kernel.legacy_projection(authoritative, operation)
    if operation.status == expected:
        return
    # PLANNED and WAITING_FOR_FOUNDER are compatible projections of the
    # same kernel WAITING_APPROVAL state; approved_at decides which one is
    # correct without manufacturing another transition.
    if authoritative == "WAITING_APPROVAL" and operation.status in {"PLANNED", "WAITING_FOR_FOUNDER"}:
        operation.status = expected
        db.session.commit()
        return
    mapped = kernel.LEGACY_TO_KERNEL.get(operation.status)
    if not mapped:
        operation.status = expected
        db.session.commit()
        return
    kernel.transition(
        operation,
        mapped,
        "LEGACY_STATE_RECONCILED",
        stage=mapped,
        force=True,
        payload={"legacy_status": operation.status},
    )


def runtime_snapshot(operation: Operation) -> dict:
    core = __import__(
        "eason_one.services.core_v018",
        fromlist=["is_v018_operation", "is_dormant_legacy_project_operation"],
    )
    if core.is_v018_operation(operation):
        return __import__(
            "eason_one.services.company_runtime", fromlist=["runtime_snapshot"]
        ).runtime_snapshot(operation)
    if core.is_dormant_legacy_project_operation(operation):
        return {
            "state": "HISTORICAL",
            "kernel_status": "RETIRED",
            "gate_kind": None,
            "worker_alive": False,
            "active_task": None,
            "next_task": None,
            "latest_run": None,
            "execution_run": None,
            "latest_execution_run": None,
            "planning_run": None,
            "live_execution": {"activity_state": "HISTORICAL"},
            "public": __import__(
                "eason_one.services.operation_kernel", fromlist=["public_snapshot"]
            ).public_snapshot(operation),
            "historical": True,
        }
    kernel.bootstrap(operation, commit=False)
    _sync_legacy_mutation(operation)
    status = kernel.authoritative_status(operation)
    checkpoint = dict(operation.checkpoint_json or {})
    thread = _WORKERS.get(operation.id)
    thread_alive = bool(thread and thread.is_alive())
    worker_alive = bool(status == "RUNNING" and (_lease_alive(operation) or thread_alive))

    latest_execution_run = (
        AgentRun.query.filter(
            AgentRun.operation_id == operation.id,
            AgentRun.purpose != "CEO_FOUNDER_REQUEST",
        ).order_by(AgentRun.id.desc()).first()
    )
    current_execution_run = (
        AgentRun.query.filter(
            AgentRun.operation_id == operation.id,
            AgentRun.purpose != "CEO_FOUNDER_REQUEST",
            AgentRun.status.in_(["CREATED", "RUNNING"]),
        ).order_by(AgentRun.id.desc()).first()
    )

    active_task = None
    active_task_id = checkpoint.get("active_task_id")
    if active_task_id:
        candidate = db.session.get(Task, int(active_task_id))
        if candidate and candidate.operation_id == operation.id:
            active_task = candidate
    if active_task is None and current_execution_run and current_execution_run.task_id:
        candidate = db.session.get(Task, current_execution_run.task_id)
        if candidate and candidate.operation_id == operation.id:
            active_task = candidate
    if active_task is None and (worker_alive or status == "VERIFYING"):
        # A persisted Task status is not proof that an Employee is working now.
        # Only a live lease/thread or an active execution Run may project a
        # current Task into the Founder UI.
        active_task = next((task for task in operation.tasks if task.status in {
            "ASSIGNED", "WORKING", "REVIEW"
        }), None)
    if not worker_alive and status != "VERIFYING":
        active_task = None

    next_task = None
    for task in operation.tasks:
        if task.status in {"ASSIGNED", "TODO", "BLOCKED", "FAILED", "REVIEW"}:
            if active_task and task.id == active_task.id:
                continue
            next_task = task
            break

    planning_run = (
        AgentRun.query.filter(
            AgentRun.operation_id == operation.id,
            AgentRun.purpose == "CEO_FOUNDER_REQUEST",
        ).order_by(AgentRun.id.desc()).first()
    )
    report = operation.founder_report_json or {}
    gate_kind = report.get("decision_kind") if status == "WAITING_APPROVAL" else None

    ui_state = {
        "CREATED": "CREATED",
        "ROUTED": "ROUTED",
        "WAITING_APPROVAL": "WAITING_FOR_BUDGET" if gate_kind == "BUDGET_AUTHORIZATION" else "NEEDS_FOUNDER",
        "QUEUED": "READY",
        "RUNNING": "WORKING" if worker_alive else "READY",
        "WAITING_INPUT": "PAUSED",
        "VERIFYING": "VERIFYING",
        "COMPLETED": "COMPLETED",
        "FAILED": "FAILED",
        "CANCELLED": "CANCELLED",
    }[status]

    started_at = checkpoint.get("runtime_started_at")
    last_activity_at = checkpoint.get("last_activity_at") or checkpoint.get("saved_at")
    elapsed_seconds = _seconds_since(started_at)
    last_activity_seconds = _seconds_since(last_activity_at)
    activity_state = "IDLE"
    if worker_alive:
        if last_activity_seconds is None or last_activity_seconds <= 30:
            activity_state = "ACTIVE"
        elif last_activity_seconds <= 180:
            activity_state = "QUIET"
        else:
            activity_state = "STALE"

    def run_view(run: AgentRun | None) -> dict | None:
        if not run:
            return None
        return {
            "id": run.id,
            "purpose": run.purpose,
            "status": run.status,
            "employee": run.employee.name if run.employee else None,
            "cost_twd": str(run.real_cost or 0),
            "provider": run.provider_key_snapshot,
            "model": run.model_name_snapshot,
            "started_at": run.started_at.isoformat() if run.started_at else None,
            "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        }

    def task_view(task: Task | None) -> dict | None:
        if not task:
            return None
        employee = task.assigned_employee
        model = employee.current_model if employee else None
        return {
            "id": task.id,
            "title": task.title,
            "status": task.status,
            "employee": employee.name if employee else None,
            "provider": model.provider_key if model else None,
            "model": model.model_name if model else None,
            "paid_provider": bool(
                model and model.provider_key not in {"codex", "mock"}
                and ((model.input_price_per_million or 0) > 0 or (model.output_price_per_million or 0) > 0)
            ),
        }

    archive = dict((operation.memory_json or {}).get("archive") or {})
    events_count = OperationEvent.query.filter_by(operation_id=operation.id).count()
    public = kernel.public_snapshot(operation)
    return {
        "state": ui_state,
        "kernel_status": status,
        "gate_kind": gate_kind,
        "worker_alive": worker_alive,
        "started_at": started_at,
        "heartbeat_at": last_activity_at,
        "finished_at": checkpoint.get("runtime_finished_at"),
        "last_result": checkpoint.get("last_result"),
        "last_error": checkpoint.get("last_error"),
        "step_count": int(checkpoint.get("step_count") or 0),
        "active_task": task_view(active_task),
        "next_task": task_view(next_task),
        "latest_run": run_view(current_execution_run or latest_execution_run),
        "execution_run": run_view(current_execution_run),
        "latest_execution_run": run_view(latest_execution_run),
        "planning_run": run_view(planning_run),
        "live_execution": {
            "started_at": started_at,
            "last_activity_at": last_activity_at,
            "elapsed_seconds": elapsed_seconds,
            "last_activity_seconds": last_activity_seconds,
            "activity_state": activity_state,
            "lease_owner": operation.lease_owner,
            "lease_expires_at": operation.lease_expires_at.isoformat() if operation.lease_expires_at else None,
        },
        "events_count": events_count,
        "kernel": public,
        "archived": bool(archive or report.get("decision_kind") == "RUNTIME_VALIDATION_ARCHIVE"),
        "archive": archive or None,
        "can_resume": status == "WAITING_INPUT" and not archive,
        "can_archive": bool(
            status in {"WAITING_INPUT", "WAITING_APPROVAL", "FAILED"}
            and not worker_alive and not archive
        ),
    }


def run_until_gate(operation_id: int, *, max_steps: int = MAX_RUNTIME_STEPS) -> dict:
    worker_id = uuid.uuid4().hex[:12]
    operation = db.session.get(Operation, operation_id)
    if not operation:
        raise ValueError("Operation not found")
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
        return runtime.runtime_snapshot(operation)
    if core.is_dormant_legacy_project_operation(operation):
        return runtime_snapshot(operation)
    kernel.bootstrap(operation)
    status = kernel.authoritative_status(operation)
    if status not in {"QUEUED", "RUNNING"}:
        return runtime_snapshot(operation)
    if not kernel.acquire_lease(operation, worker_id):
        return runtime_snapshot(operation)
    if kernel.authoritative_status(operation) == "QUEUED":
        kernel.transition(
            operation,
            "RUNNING",
            "WORKER_STARTED",
            stage="EXECUTION",
            actor_type="RUNTIME",
            actor_ref=worker_id,
        )

    checkpoint = dict(operation.checkpoint_json or {})
    start_index = int(checkpoint.get("step_count") or 0)
    kernel.checkpoint(
        operation,
        stage="EXECUTION",
        data={
            "runtime_started_at": checkpoint.get("runtime_started_at") or datetime.now(timezone.utc).isoformat(),
            "runtime_finished_at": None,
            "last_error": None,
            "worker_id": worker_id,
        },
        event_type="RUNTIME_CHECKPOINT_STARTED",
    )

    for offset in range(max_steps):
        db.session.expire_all()
        operation = db.session.get(Operation, operation_id)
        if not operation:
            break
        _sync_legacy_mutation(operation)
        if kernel.authoritative_status(operation) != "RUNNING":
            break
        if operation.lease_owner != worker_id:
            break
        kernel.renew_lease(operation, worker_id)
        step_number = start_index + offset + 1
        request_key = f"runtime:{operation_id}:step:{step_number}"
        try:
            result = __import__(
                "eason_one.services.operations", fromlist=["next_step"]
            ).next_step(operation, request_key)
            db.session.expire_all()
            operation = db.session.get(Operation, operation_id)
            _sync_legacy_mutation(operation)
            active_task_id = result.get("task_id") if isinstance(result, dict) else None
            kernel.checkpoint(
                operation,
                stage=operation.current_stage or "EXECUTION",
                data={
                    "step_count": step_number,
                    "active_task_id": active_task_id,
                    "last_result": result,
                    "last_error": None,
                    "last_activity_at": datetime.now(timezone.utc).isoformat(),
                },
                event_type="RUNTIME_STEP_COMMITTED",
            )
            engineering = dict((operation.memory_json or {}).get("engineering") or {})
            if (
                kernel.authoritative_status(operation) == "RUNNING"
                and engineering.get("stop_after_current_step")
                and isinstance(result, dict)
                and result.get("kind") == "TASK"
                and result.get("status") == "SUCCEEDED"
            ):
                task = db.session.get(Task, result.get("task_id"))
                run = db.session.get(AgentRun, result.get("agent_run_id"))
                if task and task.assigned_employee and task.assigned_employee.slug == "engineer":
                    __import__(
                        "eason_one.services.engineering_runtime",
                        fromlist=["pause_after_engineer_step"],
                    ).pause_after_engineer_step(operation, task=task, run=run)
                    operation = db.session.get(Operation, operation_id)
                    _sync_legacy_mutation(operation)
        except Exception as exc:
            db.session.rollback()
            operation = db.session.get(Operation, operation_id)
            if operation and kernel.authoritative_status(operation) == "RUNNING":
                operations = __import__(
                    "eason_one.services.operations",
                    fromlist=["is_vnext_operation", "pause_for_internal_runtime_recovery", "wait_for_founder"],
                )
                if operations.is_vnext_operation(operation):
                    operations.pause_for_internal_runtime_recovery(
                        operation,
                        "The durable runtime stopped before the next governed step: " + str(exc),
                    )
                else:
                    operations.wait_for_founder(
                        operation,
                        "The durable runtime stopped before the next governed step: " + str(exc),
                        decision_kind="RUNTIME_RECOVERY",
                    )
            operation = db.session.get(Operation, operation_id)
            if operation:
                kernel.checkpoint(
                    operation,
                    stage=(
                        "FOUNDER_GATE"
                        if kernel.authoritative_status(operation) == "WAITING_APPROVAL"
                        else "RECOVERY"
                        if kernel.authoritative_status(operation) == "WAITING_INPUT"
                        else "FAILED"
                    ),
                    data={
                        "step_count": start_index + offset,
                        "last_error": str(exc),
                        "last_activity_at": datetime.now(timezone.utc).isoformat(),
                    },
                    event_type="RUNTIME_STEP_FAILED",
                )
            break

        if kernel.authoritative_status(operation) != "RUNNING":
            break
        time.sleep(0.01)
    else:
        operation = db.session.get(Operation, operation_id)
        if operation and kernel.authoritative_status(operation) == "RUNNING":
            operations = __import__(
                "eason_one.services.operations",
                fromlist=["is_vnext_operation", "pause_for_internal_runtime_recovery", "wait_for_founder"],
            )
            if operations.is_vnext_operation(operation):
                operations.pause_for_internal_runtime_recovery(
                    operation,
                    f"The durable runtime reached its {max_steps}-step internal safety ceiling.",
                )
            else:
                operations.wait_for_founder(
                    operation,
                    f"The durable runtime reached its {max_steps}-step safety ceiling.",
                    decision_kind="RUNTIME_RECOVERY",
                )

    operation = db.session.get(Operation, operation_id)
    if operation:
        checkpoint = dict(operation.checkpoint_json or {})
        kernel.checkpoint(
            operation,
            stage=operation.current_stage or kernel.authoritative_status(operation),
            data={
                "runtime_finished_at": datetime.now(timezone.utc).isoformat(),
                "last_activity_at": datetime.now(timezone.utc).isoformat(),
                "step_count": int(checkpoint.get("step_count") or start_index),
            },
            event_type="RUNTIME_STOPPED_AT_DURABLE_STATE",
        )
        kernel.release_lease(operation, worker_id)
        return runtime_snapshot(operation)
    return {"state": "MISSING", "worker_alive": False}


def _thread_target(app, operation_id: int) -> None:
    try:
        with app.app_context():
            run_until_gate(operation_id)
    finally:
        with _LOCK:
            _WORKERS.pop(operation_id, None)
        try:
            with app.app_context():
                db.session.remove()
        except Exception:
            pass


def start_background(operation_id: int) -> dict:
    """Attach one helper thread to a DB-queued Operation."""
    app = current_app._get_current_object()
    operation = db.session.get(Operation, operation_id)
    if not operation:
        raise ValueError("Operation not found")
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
        return runtime.runtime_snapshot(operation)
    if core.is_dormant_legacy_project_operation(operation):
        return runtime_snapshot(operation)
    kernel.bootstrap(operation)
    status = kernel.authoritative_status(operation)
    if status == "RUNNING" and _lease_alive(operation):
        return runtime_snapshot(operation)
    if status not in {"QUEUED", "RUNNING"}:
        return runtime_snapshot(operation)
    with _LOCK:
        existing = _WORKERS.get(operation_id)
        if existing and existing.is_alive():
            return runtime_snapshot(operation)
        thread = threading.Thread(
            target=_thread_target,
            args=(app, operation_id),
            name=f"eason-one-operation-{operation_id}",
            daemon=True,
        )
        _WORKERS[operation_id] = thread
        thread.start()
    return runtime_snapshot(operation)

def resume_queued_operations() -> list[int]:
    """Attach runtime workers to durable, already-authorized queued Operations.

    Restart recovery is not complete when RUNNING merely becomes QUEUED.  In a
    normal production process the company must continue without the Founder
    opening a page or pressing Resume.  WAITING_INPUT / WAITING_APPROVAL remain
    untouched because those states still require their deterministic resolver.
    """
    operation_ids = []
    for operation in Operation.query.filter_by(kernel_status="QUEUED").order_by(Operation.id).all():
        if operation.approved_at is None:
            continue
        if _owned_by_company_runtime(operation):
            continue
        project = operation.project
        if project is not None and project.status in {"COMPLETED", "CANCELLED"}:
            continue
        operation_ids.append(operation.id)

    for operation_id in operation_ids:
        start_background(operation_id)
    return operation_ids

