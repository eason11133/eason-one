"""Authoritative V0.12 Operation kernel.

This module owns routing metadata, lifecycle transitions, durable checkpoints,
worker leases, and pre-provider budget reservations. LLM output may propose a
plan, but only this deterministic layer may change authoritative execution
state or consume authority.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING
import re
from typing import Any

from sqlalchemy import func, text
from sqlalchemy.exc import IntegrityError

from ..extensions import db
from ..models import AgentRun, Company, CostEvent, CostReservation, Operation, OperationEvent, Project, Work, now

ROUTES = {
    "DIRECT_RESPONSE",
    "DETERMINISTIC_ACTION",
    "SINGLE_WORKER",
    "SHORT_MEETING",
    "FULL_PROJECT",
}

STATUSES = {
    "CREATED",
    "ROUTED",
    "WAITING_APPROVAL",
    "QUEUED",
    "RUNNING",
    "WAITING_INPUT",
    "VERIFYING",
    "COMPLETED",
    "FAILED",
    "CANCELLED",
}

TERMINAL = {"COMPLETED", "FAILED", "CANCELLED"}

ALLOWED_TRANSITIONS = {
    "CREATED": {"ROUTED", "CANCELLED"},
    "ROUTED": {"WAITING_APPROVAL", "QUEUED", "COMPLETED", "CANCELLED"},
    "WAITING_APPROVAL": {"QUEUED", "CANCELLED", "FAILED"},
    "QUEUED": {"RUNNING", "WAITING_APPROVAL", "CANCELLED", "FAILED"},
    "RUNNING": {
        "QUEUED", "WAITING_INPUT", "WAITING_APPROVAL", "VERIFYING",
        "COMPLETED", "FAILED", "CANCELLED",
    },
    "WAITING_INPUT": {"QUEUED", "WAITING_APPROVAL", "CANCELLED", "FAILED"},
    "VERIFYING": {"QUEUED", "WAITING_INPUT", "WAITING_APPROVAL", "COMPLETED", "FAILED", "CANCELLED"},
    "COMPLETED": set(),
    "FAILED": set(),
    "CANCELLED": set(),
}

LEGACY_TO_KERNEL = {
    "PLANNED": "WAITING_APPROVAL",
    "WAITING_FOR_FOUNDER": "WAITING_APPROVAL",
    "RUNNING": "RUNNING",
    "PAUSED": "WAITING_INPUT",
    "COMPLETED": "COMPLETED",
    "FAILED": "FAILED",
    "TERMINATED_BY_FOUNDER": "CANCELLED",
    "SUPERSEDED": "CANCELLED",
}

KERNEL_TO_LEGACY = {
    "CREATED": "PLANNED",
    "ROUTED": "PLANNED",
    "WAITING_APPROVAL": "WAITING_FOR_FOUNDER",
    "QUEUED": "RUNNING",
    "RUNNING": "RUNNING",
    "WAITING_INPUT": "PAUSED",
    "VERIFYING": "RUNNING",
    "COMPLETED": "COMPLETED",
    "FAILED": "FAILED",
    "CANCELLED": "TERMINATED_BY_FOUNDER",
}

MONEY_QUANTUM = Decimal("0.000001")
LEASE_SECONDS = 90


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def money(value: Any) -> Decimal:
    return Decimal(str(value or 0)).quantize(MONEY_QUANTUM, rounding=ROUND_CEILING)


def authoritative_status(operation: Operation) -> str:
    value = (getattr(operation, "kernel_status", None) or "").upper()
    legacy = (operation.status or "").upper()
    # Legacy constructors still exist in old tests and repair utilities.  A
    # freshly defaulted CREATED value must not erase an explicitly persisted
    # later legacy state.
    if value == "CREATED" and legacy not in {"", "PLANNED"}:
        return LEGACY_TO_KERNEL.get(legacy, value)
    if value in STATUSES:
        return value
    return LEGACY_TO_KERNEL.get(legacy, "CREATED")


def legacy_projection(status: str, operation: Operation | None = None) -> str:
    # The legacy UI distinguishes the initial proposal (PLANNED) from later
    # Founder gates (WAITING_FOR_FOUNDER).  The kernel intentionally models
    # both as WAITING_APPROVAL, so preserve that compatibility distinction
    # deterministically from persisted approval authority.
    if status == "WAITING_APPROVAL" and operation is not None and operation.approved_at is None:
        return "PLANNED"
    return KERNEL_TO_LEGACY[status]


def _next_sequence(operation_id: int) -> int:
    value = db.session.query(func.coalesce(func.max(OperationEvent.sequence), 0)).filter(
        OperationEvent.operation_id == operation_id
    ).scalar()
    return int(value or 0) + 1


def append_event(
    operation: Operation,
    event_type: str,
    *,
    from_status: str | None = None,
    to_status: str | None = None,
    stage: str | None = None,
    actor_type: str = "SYSTEM",
    actor_ref: str | None = None,
    idempotency_key: str | None = None,
    payload: dict | None = None,
) -> OperationEvent:
    if idempotency_key:
        existing = OperationEvent.query.filter_by(
            operation_id=operation.id, idempotency_key=idempotency_key
        ).first()
        if existing:
            return existing
    event = OperationEvent(
        operation_id=operation.id,
        sequence=_next_sequence(operation.id),
        event_type=event_type,
        from_status=from_status,
        to_status=to_status,
        stage=stage or operation.current_stage,
        actor_type=actor_type,
        actor_ref=actor_ref,
        idempotency_key=idempotency_key,
        payload_json=payload or None,
    )
    db.session.add(event)
    return event


def bootstrap(operation: Operation, *, commit: bool = True) -> Operation:
    """Populate V0.12 authority fields for a legacy Operation exactly once."""
    status = authoritative_status(operation)
    operation.kernel_status = status
    if not operation.route_type or operation.route_type not in ROUTES:
        operation.route_type = "FULL_PROJECT" if len(operation.tasks) > 1 else "SINGLE_WORKER"
    operation.current_stage = operation.current_stage or status
    operation.hard_cost_cap_twd = money(operation.hard_cost_cap_twd or operation.approved_budget_twd)
    operation.estimated_cost_twd = money(operation.estimated_cost_twd or 0)
    operation.reserved_cost_twd = active_reserved(operation)
    operation.max_calls = 12 if operation.max_calls is None else int(operation.max_calls)
    operation.max_revisions = 2 if operation.max_revisions is None else int(operation.max_revisions)
    operation.max_messages = 24 if operation.max_messages is None else int(operation.max_messages)
    operation.max_elapsed_seconds = 3600 if operation.max_elapsed_seconds is None else int(operation.max_elapsed_seconds)
    operation.state_version = int(operation.state_version or 0)
    if not OperationEvent.query.filter_by(operation_id=operation.id).first():
        append_event(
            operation,
            "OPERATION_IMPORTED",
            to_status=status,
            stage=operation.current_stage,
            idempotency_key="kernel-bootstrap",
            payload={"legacy_status": operation.status, "route_type": operation.route_type},
        )
    operation.status = legacy_projection(status, operation)
    if commit:
        db.session.commit()
    return operation


def transition(
    operation: Operation,
    to_status: str,
    event_type: str,
    *,
    stage: str | None = None,
    actor_type: str = "SYSTEM",
    actor_ref: str | None = None,
    idempotency_key: str | None = None,
    payload: dict | None = None,
    force: bool = False,
    commit: bool = True,
) -> Operation:
    to_status = (to_status or "").upper()
    if to_status not in STATUSES:
        raise ValueError(f"Unknown Operation status: {to_status}")
    current = authoritative_status(operation)
    if current == to_status:
        append_event(
            operation,
            event_type,
            from_status=current,
            to_status=to_status,
            stage=stage or operation.current_stage,
            actor_type=actor_type,
            actor_ref=actor_ref,
            idempotency_key=idempotency_key,
            payload=payload,
        )
        operation.status = legacy_projection(to_status, operation)
        operation.current_stage = stage or operation.current_stage
        if to_status in TERMINAL:
            __import__("eason_one.services.ceo_learning", fromlist=["capture_operation_episode"]).capture_operation_episode(operation, commit=False)
        if commit:
            db.session.commit()
        return operation
    if not force and to_status not in ALLOWED_TRANSITIONS[current]:
        raise ValueError(f"Invalid Operation transition: {current} -> {to_status}")
    append_event(
        operation,
        event_type,
        from_status=current,
        to_status=to_status,
        stage=stage or operation.current_stage,
        actor_type=actor_type,
        actor_ref=actor_ref,
        idempotency_key=idempotency_key,
        payload=payload,
    )
    operation.kernel_status = to_status
    operation.status = legacy_projection(to_status, operation)
    operation.current_stage = stage or to_status
    operation.state_version = int(operation.state_version or 0) + 1
    operation.updated_at = now()
    if to_status in TERMINAL:
        operation.ended_at = operation.ended_at or now()
        operation.lease_owner = None
        operation.lease_expires_at = None
        __import__("eason_one.services.ceo_learning", fromlist=["capture_operation_episode"]).capture_operation_episode(operation, commit=False)
    if commit:
        db.session.commit()
    return operation


def set_route(
    operation: Operation,
    route_type: str,
    reason: str,
    *,
    actor_type: str = "ROUTER",
    commit: bool = True,
) -> Operation:
    route_type = (route_type or "").upper()
    if route_type not in ROUTES:
        raise ValueError(f"Unknown route type: {route_type}")
    operation.route_type = route_type
    operation.route_reason = (reason or "").strip()
    current = authoritative_status(operation)
    if current == "CREATED":
        transition(
            operation,
            "ROUTED",
            "ROUTE_SELECTED",
            stage="ROUTING",
            actor_type=actor_type,
            payload={"route_type": route_type, "reason": operation.route_reason},
            commit=False,
        )
    else:
        append_event(
            operation,
            "ROUTE_UPDATED",
            from_status=current,
            to_status=current,
            stage=operation.current_stage,
            actor_type=actor_type,
            payload={"route_type": route_type, "reason": operation.route_reason},
        )
    if commit:
        db.session.commit()
    return operation


def actual_spend(operation: Operation) -> Decimal:
    value = db.session.query(func.coalesce(func.sum(CostEvent.real_cost_delta), 0)).filter(
        CostEvent.operation_id == operation.id
    ).scalar()
    return money(value)


def _stage_group(stage: str | None) -> str:
    value = (stage or "").upper()
    if value in {"CHAIR_ROUTER", "MEETING_CONTRIBUTION", "MEETING_SYNTHESIS"} or value.startswith("MEETING_"):
        return "MEETING"
    return value


def stage_spend(operation: Operation, stage: str) -> Decimal:
    group = _stage_group(stage)
    query = db.session.query(func.coalesce(func.sum(CostEvent.real_cost_delta), 0)).filter(
        CostEvent.operation_id == operation.id
    )
    if group == "MEETING":
        query = query.filter(CostEvent.stage.in_([
            "CHAIR_ROUTER", "MEETING_CONTRIBUTION", "MEETING_SYNTHESIS",
        ]))
    else:
        query = query.filter(CostEvent.stage == group)
    return money(query.scalar())


def stage_reserved(operation: Operation, stage: str) -> Decimal:
    group = _stage_group(stage)
    query = db.session.query(func.coalesce(func.sum(CostReservation.estimated_twd), 0)).filter(
        CostReservation.operation_id == operation.id,
        CostReservation.status.in_(["RESERVED", "AMBIGUOUS"]),
    )
    if group == "MEETING":
        query = query.filter(CostReservation.stage.in_([
            "CHAIR_ROUTER", "MEETING_CONTRIBUTION", "MEETING_SYNTHESIS",
        ]))
    else:
        query = query.filter(CostReservation.stage == group)
    return money(query.scalar())


def active_reserved(operation: Operation) -> Decimal:
    value = db.session.query(func.coalesce(func.sum(CostReservation.estimated_twd), 0)).filter(
        CostReservation.operation_id == operation.id,
        CostReservation.status.in_(["RESERVED", "AMBIGUOUS"]),
    ).scalar()
    return money(value)


def budget_snapshot(operation: Operation, *, stage: str | None = None) -> dict[str, Decimal | int]:
    actual = actual_spend(operation)
    reserved = active_reserved(operation)
    hard_cap = money(operation.hard_cost_cap_twd or operation.approved_budget_twd)
    operation.actual_cost_twd = actual
    operation.reserved_cost_twd = reserved
    memory = dict(operation.memory_json or {})
    completion_reserve = money(memory.get("completion_reserve_twd") or 0)
    report_reserve = money(memory.get("report_reserve_twd") or 0)
    return {
        "estimated": money(operation.estimated_cost_twd),
        "hard_cap": hard_cap,
        "actual": actual,
        "reserved": reserved,
        "available": max(Decimal("0"), hard_cap - actual - reserved),
        "completion_reserve": completion_reserve,
        "report_reserve": report_reserve,
        "spendable_before_completion": max(Decimal("0"), hard_cap - actual - reserved - completion_reserve),
        "stage_actual": stage_spend(operation, stage) if stage else Decimal("0"),
        "stage_reserved": stage_reserved(operation, stage) if stage else Decimal("0"),
        "call_count": int(operation.call_count or 0),
        "max_calls": int(operation.max_calls or 0),
        "revision_count": int(operation.revision_count or 0),
        "max_revisions": int(operation.max_revisions or 0),
    }


def _elapsed_seconds(operation: Operation) -> int:
    started = None
    checkpoint = dict(operation.checkpoint_json or {})
    raw = checkpoint.get("runtime_started_at")
    if raw:
        try:
            started = datetime.fromisoformat(str(raw))
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            started = None
    started = started or operation.approved_at or operation.created_at
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return max(0, int((utcnow() - started).total_seconds()))


def _vnext_work_for_run(agent_run_id: int | None) -> Work | None:
    """Resolve the authoritative vNext Work for one execution attempt.

    Operation-level call/stage caps are legacy orchestration allocations. A
    Company Core vNext Execution is governed by its Work plus the Project hard
    envelope, so the reservation layer must know when those legacy caps are no
    longer authoritative. Merely having a Work row is not enough: migrated
    legacy executions may also have Work adapters and must retain their legacy
    Operation limits.
    """
    if not agent_run_id:
        return None
    run = db.session.get(AgentRun, int(agent_run_id))
    if not run or not run.work_id or not run.operation_id:
        return None
    operation = db.session.get(Operation, int(run.operation_id))
    if not operation or (operation.memory_json or {}).get("runtime_semantics") not in {"WORK_VNEXT", "WORK_CORE_V018"}:
        return None
    work = db.session.get(Work, int(run.work_id))
    if not work or int(work.operation_id or 0) != int(operation.id):
        return None
    return work


def _assert_vnext_dispatch_state(operation: Operation, work: Work) -> None:
    """Validate process state without resurrecting Operation lifecycle authority.

    A completed/failed Mission may still need paid *Project management* work:
    independent Project outcome review or a bounded continuation plan. Those
    calls remain governed by the active Work and immutable Project budget.
    Delivery Work, however, may not execute after its Mission is terminal.
    """
    project = getattr(work, "project", None)
    if operation.approved_at is None:
        raise ValueError("vNext Work has no approved Operation lineage")
    if project is None or project.status in {"COMPLETED", "CANCELLED", "REVIEW"}:
        raise ValueError("Project is not authorized for a provider call")
    if work.state not in {"READY", "EXECUTING", "VERIFYING"}:
        raise ValueError("Work is not authorized for a provider call")

    status = authoritative_status(operation)
    if status in {"QUEUED", "RUNNING", "VERIFYING"}:
        return
    if status in {"COMPLETED", "FAILED"} and work.work_type == "MANAGEMENT":
        return
    raise ValueError("Operation lifecycle does not permit this Work provider call")


def _work_actual_spend(work: Work) -> Decimal:
    value = db.session.query(func.coalesce(func.sum(CostEvent.real_cost_delta), 0)).filter(
        CostEvent.work_id == work.id
    ).scalar()
    return money(value)


def _work_active_reserved(work: Work) -> Decimal:
    value = (
        db.session.query(func.coalesce(func.sum(CostReservation.estimated_twd), 0))
        .join(AgentRun, CostReservation.agent_run_id == AgentRun.id)
        .filter(
            AgentRun.work_id == work.id,
            CostReservation.status.in_(["RESERVED", "AMBIGUOUS"]),
        )
        .scalar()
    )
    return money(value)


def _vnext_call_safety_ceiling(operation: Operation) -> int:
    """Bound runaway execution without treating an old plan estimate as authority.

    The old max_calls value priced only the happy path and therefore made the
    first legitimate retry/management decision look like a Founder problem.
    vNext keeps a deterministic safety ceiling, but derives enough headroom for
    Work retries, management/replan, verification and final delivery.
    """
    works = list(getattr(operation, "works", ()) or ())
    delivery = [work for work in works if work.work_type != "MANAGEMENT"]
    retry_slots = sum(max(0, int(work.retry_limit or 0)) for work in delivery)
    derived = 6 + (len(delivery) * 2) + retry_slots
    return max(int(operation.max_calls or 0), derived)


def assert_runtime_limits(
    operation: Operation,
    *,
    stage: str,
    estimate_twd: Decimal,
    agent_run_id: int | None = None,
) -> None:
    estimate = money(estimate_twd)
    snapshot = budget_snapshot(operation, stage=stage)
    work = _vnext_work_for_run(agent_run_id)
    vnext = work is not None
    if vnext:
        _assert_vnext_dispatch_state(operation, work)
    elif authoritative_status(operation) not in {"QUEUED", "RUNNING", "VERIFYING"}:
        raise ValueError("Operation is not authorized for a provider call")
    # Legacy single-call/stage caps were planning estimates, not Founder
    # authority.  They remain enforced for legacy Operations.  Once an
    # Execution belongs to a vNext Work, Project/Work resource truth governs
    # the call instead; stale estimates may not veto valid work.
    if (
        not vnext
        and operation.single_call_cost_cap_twd is not None
        and estimate > money(operation.single_call_cost_cap_twd)
    ):
        raise ValueError("Provider call exceeds the Operation single-call hard cap")
    call_ceiling = _vnext_call_safety_ceiling(operation) if vnext else int(operation.max_calls or 0)
    if call_ceiling and int(operation.call_count or 0) >= call_ceiling:
        raise ValueError("Operation maximum provider-call count reached")
    # ``max_elapsed_seconds`` is a legacy Operation wall-clock planning guard.
    # A durable vNext Project may legitimately pause across process restarts,
    # provider reconfiguration, Founder absence, or Company-owned recovery and
    # then resume Work hours/days later.  Its age is not execution authority.
    # Keep the wall-clock veto for legacy Operations only; vNext provider calls
    # are governed by current Project/Work authority, budget, retry and effect
    # truth instead of the Mission's original runtime start timestamp.
    if (
        not vnext
        and int(operation.max_elapsed_seconds or 0)
        and _elapsed_seconds(operation) >= int(operation.max_elapsed_seconds)
    ):
        raise ValueError("Operation maximum elapsed time reached")
    if not vnext and operation.stage_cost_cap_twd is not None:
        stage_cap = money(operation.stage_cost_cap_twd)
        if money(snapshot["stage_actual"]) + estimate > stage_cap:
            raise ValueError("Provider call exceeds the current-stage hard cap")
    memory = dict(operation.memory_json or {})
    stage_caps = dict(memory.get("stage_budget_caps") or {})
    stage_group = _stage_group(stage)
    if not vnext and stage_group in stage_caps:
        stage_cap = money(stage_caps[stage_group])
        if money(snapshot["stage_actual"]) + money(snapshot["stage_reserved"]) + estimate > stage_cap:
            raise ValueError(f"Provider call exceeds the {stage_group} stage budget cap")
    if vnext:
        # The reservation caller already owns the Company/Project write lock.
        # Count all in-flight Project/Work reservations here so concurrent
        # Employee calls cannot each pass a stale settled-cost snapshot.
        __import__(
            "eason_one.services.work_budget", fromlist=["ensure"]
        ).ensure(work, estimate, include_reservations=True)
    if stage == "CEO_OPERATION_REPORT":
        required_completion_reserve = Decimal("0")
    elif stage == "GOAL_VERIFICATION":
        required_completion_reserve = money(memory.get("report_reserve_twd") or 0)
    else:
        required_completion_reserve = money(memory.get("completion_reserve_twd") or 0)
    if (
        not vnext
        and money(snapshot["actual"]) + money(snapshot["reserved"]) + estimate
        + required_completion_reserve > money(snapshot["hard_cap"])
    ):
        raise ValueError("Provider call exceeds remaining Operation authority because funds are reserved for verification and final delivery")


def reserve_provider_call(
    operation: Operation,
    *,
    stage: str,
    estimate_twd: Decimal,
    idempotency_key: str,
    agent_run_id: int | None = None,
    expires_seconds: int = 900,
) -> CostReservation:
    """Atomically reserve authority before a paid provider call.

    SQLite is the supported V0.12 runtime. ``BEGIN IMMEDIATE`` takes the
    database write reservation before the budget is re-read, so concurrent
    workers cannot both pass the same remaining-budget check. The row lock is
    retained for databases that support ``SELECT .. FOR UPDATE``.
    """
    estimate = money(estimate_twd)
    operation_id = int(operation.id)
    try:
        # The reservation owns the commit boundary already. Finish any caller
        # transaction before taking SQLite's write reservation so the budget
        # check cannot run in a weaker deferred transaction.
        if db.session().in_transaction():
            db.session.commit()
        bind = db.session.get_bind()
        if bind.dialect.name == "sqlite":
            db.session.execute(text("BEGIN IMMEDIATE"))

        # One paid dispatch reservation owns Company -> Project -> Operation
        # authority in that lock order. SQLite's BEGIN IMMEDIATE serializes the
        # same boundary globally; row-locking databases use the explicit rows so
        # parallel Employee workers cannot both spend the same remaining TWD.
        run = db.session.get(AgentRun, int(agent_run_id)) if agent_run_id else None
        vnext_work = db.session.get(Work, int(run.work_id)) if run and run.work_id else None
        company = db.session.query(Company).order_by(Company.id).with_for_update().first()
        if company is None:
            raise ValueError("Company budget authority is unavailable")
        if vnext_work is not None and vnext_work.project_id:
            db.session.query(Project).filter(Project.id == vnext_work.project_id).with_for_update().one()
        operation = (
            db.session.query(Operation)
            .filter(Operation.id == operation_id)
            .with_for_update()
            .one()
        )
        existing = CostReservation.query.filter_by(
            operation_id=operation.id, idempotency_key=idempotency_key
        ).first()
        if existing:
            db.session.commit()
            return existing
        assert_runtime_limits(
            operation,
            stage=stage,
            estimate_twd=estimate,
            agent_run_id=agent_run_id,
        )
        reservation = CostReservation(
            operation_id=operation.id,
            agent_run_id=agent_run_id,
            stage=stage,
            idempotency_key=idempotency_key,
            estimated_twd=estimate,
            status="RESERVED",
            expires_at=utcnow() + timedelta(seconds=expires_seconds),
        )
        db.session.add(reservation)
        operation.call_count = int(operation.call_count or 0) + 1
        append_event(
            operation,
            "PROVIDER_CALL_RESERVED",
            from_status=authoritative_status(operation),
            to_status=authoritative_status(operation),
            stage=stage,
            idempotency_key=f"reserve-event:{idempotency_key}",
            payload={"estimated_twd": str(estimate), "agent_run_id": agent_run_id},
        )
        db.session.commit()
        return reservation
    except IntegrityError:
        db.session.rollback()
        existing = CostReservation.query.filter_by(
            operation_id=operation_id, idempotency_key=idempotency_key
        ).one_or_none()
        if existing is not None:
            return existing
        raise
    except Exception:
        db.session.rollback()
        raise


def resolve_reservation(
    reservation: CostReservation,
    *,
    actual_twd: Decimal | None = None,
    status: str = "CONSUMED",
    note: str | None = None,
) -> CostReservation:
    """Resolve one provider reservation under the same write lock as its event.

    Parallel Employee Runs can finish at nearly the same moment. On SQLite,
    taking the write reservation before reading the next OperationEvent sequence
    prevents two workers from manufacturing the same append-only event number.
    """
    reservation_id = int(reservation.id)
    status = status.upper()
    if status not in {"CONSUMED", "RELEASED", "AMBIGUOUS"}:
        raise ValueError("Unknown reservation resolution")
    if db.session().in_transaction():
        db.session.commit()
    bind = db.session.get_bind()
    if bind.dialect.name == "sqlite":
        db.session.execute(text("BEGIN IMMEDIATE"))
    reservation = (
        db.session.query(CostReservation)
        .filter(CostReservation.id == reservation_id)
        .with_for_update()
        .one()
    )
    if reservation.status not in {"RESERVED", "AMBIGUOUS"}:
        db.session.commit()
        return reservation
    reservation.status = status
    reservation.actual_twd = money(actual_twd) if actual_twd is not None else None
    reservation.resolved_at = None if status == "AMBIGUOUS" else now()
    reservation.resolution_note = note
    operation = reservation.operation
    operation.reserved_cost_twd = active_reserved(operation)
    append_event(
        operation,
        "PROVIDER_CALL_" + status,
        from_status=authoritative_status(operation),
        to_status=authoritative_status(operation),
        stage=reservation.stage,
        idempotency_key=f"reservation-resolution:{reservation.id}:{status}",
        payload={
            "reservation_id": reservation.id,
            "estimated_twd": str(reservation.estimated_twd),
            "actual_twd": str(reservation.actual_twd) if reservation.actual_twd is not None else None,
            "note": note,
        },
    )
    db.session.commit()
    return reservation


def checkpoint(operation: Operation, *, stage: str, data: dict, event_type: str = "CHECKPOINT_SAVED") -> Operation:
    current = dict(operation.checkpoint_json or {})
    current.update(data)
    current["stage"] = stage
    current["saved_at"] = utcnow().isoformat()
    operation.checkpoint_json = current
    operation.current_stage = stage
    append_event(
        operation,
        event_type,
        from_status=authoritative_status(operation),
        to_status=authoritative_status(operation),
        stage=stage,
        payload=data,
    )
    db.session.commit()
    return operation


def acquire_lease(operation: Operation, worker_id: str, *, seconds: int = LEASE_SECONDS) -> bool:
    """Atomically claim one Operation for one runtime worker."""
    operation_id = int(operation.id)
    current = utcnow()
    try:
        if db.session().in_transaction():
            db.session.commit()
        bind = db.session.get_bind()
        if bind.dialect.name == "sqlite":
            db.session.execute(text("BEGIN IMMEDIATE"))
        operation = (
            db.session.query(Operation)
            .filter(Operation.id == operation_id)
            .with_for_update()
            .one()
        )
        expires = operation.lease_expires_at
        if expires and expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if operation.lease_owner and expires and expires > current and operation.lease_owner != worker_id:
            db.session.rollback()
            return False
        operation.lease_owner = worker_id
        operation.lease_expires_at = current + timedelta(seconds=seconds)
        operation.attempt_count = int(operation.attempt_count or 0) + 1
        append_event(
            operation,
            "WORKER_LEASE_ACQUIRED",
            from_status=authoritative_status(operation),
            to_status=authoritative_status(operation),
            stage=operation.current_stage,
            idempotency_key=f"lease:{worker_id}:{operation.attempt_count}",
            payload={"worker_id": worker_id, "expires_at": operation.lease_expires_at.isoformat()},
        )
        db.session.commit()
        return True
    except Exception:
        db.session.rollback()
        raise


def renew_lease(operation: Operation, worker_id: str, *, seconds: int = LEASE_SECONDS) -> bool:
    operation_id = int(operation.id)
    updated = db.session.query(Operation).filter(
        Operation.id == operation_id, Operation.lease_owner == worker_id
    ).update(
        {Operation.lease_expires_at: utcnow() + timedelta(seconds=seconds)},
        synchronize_session=False,
    )
    db.session.commit()
    return updated == 1


def release_lease(operation: Operation, worker_id: str | None = None) -> None:
    filters = [Operation.id == int(operation.id)]
    if worker_id is not None:
        filters.append(Operation.lease_owner == worker_id)
    db.session.query(Operation).filter(*filters).update(
        {Operation.lease_owner: None, Operation.lease_expires_at: None},
        synchronize_session=False,
    )
    db.session.commit()


def reconcile_expired_reservations() -> int:
    """Hold expired unknown calls as ambiguous until Founder reconciliation."""
    current = utcnow()
    rows = CostReservation.query.filter(
        CostReservation.status == "RESERVED",
        CostReservation.expires_at.is_not(None),
        CostReservation.expires_at <= current,
    ).all()
    for reservation in rows:
        reservation.status = "AMBIGUOUS"
        reservation.resolution_note = (
            "Reservation expired without exact provider usage. Authority remains held "
            "until the call is reconciled or explicitly released."
        )
        append_event(
            reservation.operation,
            "PROVIDER_CALL_RESERVATION_EXPIRED",
            from_status=authoritative_status(reservation.operation),
            to_status=authoritative_status(reservation.operation),
            stage=reservation.stage,
            idempotency_key=f"reservation-expired:{reservation.id}",
            payload={"reservation_id": reservation.id, "estimated_twd": str(reservation.estimated_twd)},
        )
    if rows:
        db.session.commit()
    return len(rows)


def enqueue(operation: Operation, *, actor_type: str = "SYSTEM", reason: str | None = None) -> Operation:
    current = authoritative_status(operation)
    if current == "WAITING_APPROVAL":
        transition(
            operation,
            "QUEUED",
            "AUTHORITY_GRANTED",
            stage="QUEUED",
            actor_type=actor_type,
            payload={"reason": reason},
        )
    elif current == "WAITING_INPUT":
        transition(
            operation,
            "QUEUED",
            "OPERATION_RESUMED",
            stage="QUEUED",
            actor_type=actor_type,
            payload={"reason": reason},
        )
    elif current == "ROUTED":
        transition(operation, "QUEUED", "OPERATION_QUEUED", stage="QUEUED", actor_type=actor_type)
    elif current == "RUNNING":
        transition(operation, "QUEUED", "OPERATION_REQUEUED", stage="QUEUED", actor_type=actor_type)
    elif current != "QUEUED":
        raise ValueError(f"Operation cannot be queued from {current}")
    return operation


def recover_stale_operations() -> int:
    """Return abandoned in-process work to the durable queue after restart."""
    recovered = 0
    current = utcnow()
    for operation in Operation.query.filter(Operation.kernel_status.in_(["RUNNING", "VERIFYING"])).all():
        # v0.18 and retired v0.17 Work-first rows never enter the legacy queue.
        if __import__(
            "eason_one.services.core_v018", fromlist=["bypass_legacy_operation_runtime"]
        ).bypass_legacy_operation_runtime(operation):
            continue
        expires = operation.lease_expires_at
        if expires and expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if not operation.lease_owner or not expires or expires <= current:
            transition(
                operation,
                "QUEUED",
                "STALE_WORKER_RECOVERED",
                stage="QUEUED",
                payload={"prior_lease_owner": operation.lease_owner},
                force=True,
                commit=False,
            )
            operation.lease_owner = None
            operation.lease_expires_at = None
            recovered += 1
    if recovered:
        db.session.commit()
    return recovered


def normalize_target_mentions(text: str) -> list[str]:
    return [item.casefold() for item in re.findall(r"@([\w-]+)", text or "")]


def route_command(text: str, explicit_mode: str | None = None) -> dict[str, Any]:
    """Choose the smallest sufficient path before any model call."""
    value = " ".join((text or "").strip().casefold().split())
    mentions = normalize_target_mentions(value)
    explicit = (explicit_mode or "AUTO").strip().upper()

    if explicit in ROUTES:
        return {"route_type": explicit, "reason": "Founder selected the execution path explicitly.", "mentions": mentions}

    meeting_terms = (
        "open a meeting", "short meeting", "discuss together", "debate", "cross-role",
        "開會", "短會", "一起討論", "辯論", "跨角色",
    )
    project_terms = (
        "full project", "multi-day", "multiple deliverables", "several tasks", "project plan",
        "完整專案", "多天", "多個產物", "多階段", "多個任務",
    )
    deterministic_terms = (
        "cancel operation", "stop mission", "pause mission", "resume mission", "archive mission",
        "update status", "show cost", "create record", "取消任務", "停止任務", "暫停任務",
        "恢復任務", "更新狀態", "顯示成本", "建立紀錄",
    )
    status_terms = (
        "status", "current", "what changed", "cost", "spent", "budget", "who is working",
        "公司現況", "目前", "現在", "花多少", "成本", "誰在做", "進度",
    )
    work_terms = (
        "build", "implement", "fix", "research", "investigate", "analyze", "compare", "design",
        "create", "prepare", "deliver", "develop", "repair", "solve", "improve", "achieve",
        "修正", "實作", "研究", "調查", "分析", "比較", "設計", "建立", "新增", "加入", "加上", "準備", "交付",
        "開發", "完成", "改善", "解決",
    )
    delegation_terms = (
        "you are responsible for",
        "not only producing recommendations",
        "organize the necessary employees",
        "take ownership",
        "own the outcome",
        "carry this through",
        "concrete improvement",
        "verified improvement",
        "verified result",
        "由你負責",
        "不要只給建議",
        "自行安排必要",
        "負責完成",
    )
    imperative_execution = value.startswith((
        "make ", "improve ", "repair ", "solve ", "complete ", "achieve ",
        "organize ", "deliver ", "build ", "implement ", "fix ",
        "改善", "修好", "解決", "完成", "交付", "實作",
    ))

    if any(term in value for term in deterministic_terms):
        return {"route_type": "DETERMINISTIC_ACTION", "reason": "The request is an authoritative state action that does not require a model.", "mentions": mentions}
    if any(term in value for term in meeting_terms):
        return {"route_type": "SHORT_MEETING", "reason": "The Founder explicitly requested cross-role discussion.", "mentions": mentions}
    if __import__(
        "eason_one.services.research_department", fromlist=["requests_multi_ai_research"]
    ).requests_multi_ai_research(value):
        return {
            "route_type": "FULL_PROJECT",
            "reason": "The Founder explicitly requested independent multiple-AI research plus accountable department synthesis.",
            "mentions": mentions,
        }
    if len(mentions) >= 2:
        return {"route_type": "SHORT_MEETING", "reason": "The Founder explicitly addressed multiple Employees without requesting independent research branches.", "mentions": mentions}
    if any(term in value for term in project_terms):
        return {"route_type": "FULL_PROJECT", "reason": "The request explicitly requires multiple stages, tasks, or deliverables.", "mentions": mentions}
    if mentions:
        return {"route_type": "SINGLE_WORKER", "reason": "The Founder explicitly addressed one Employee, so one bounded specialist path is sufficient.", "mentions": mentions}
    shared_work_intent = __import__(
        "eason_one.services.ceo_intent", fromlist=["requests_governed_work"]
    ).requests_governed_work(value)
    if imperative_execution or shared_work_intent or any(term in value for term in delegation_terms) or any(term in value for term in work_terms):
        return {"route_type": "AUTO_DELEGATION", "reason": "The Founder delegated an outcome. The persistent CEO must choose the smallest sufficient team, Meeting gate, and workflow from History and current capability.", "mentions": mentions}
    if any(term in value for term in status_terms):
        return {"route_type": "DIRECT_RESPONSE", "reason": "Persisted state is sufficient; no execution object or provider call is required.", "mentions": mentions}
    return {"route_type": "DIRECT_RESPONSE", "reason": "The request asks for judgment or explanation but does not yet require company execution.", "mentions": mentions}


def execute_deterministic_action(text: str) -> dict[str, Any]:
    """Execute a narrow Founder command without a model call."""
    value=" ".join((text or "").strip().casefold().split())
    match=re.search(r"(?:operation|mission|任務|專案)\s*#?\s*(\d+)",value)
    operation=db.session.get(Operation,int(match.group(1))) if match else None
    if operation is None:
        core=__import__(
            "eason_one.services.core_v018",
            fromlist=["is_v018_operation","is_dormant_legacy_project_operation"],
        )
        operation=next((row for row in Operation.query.order_by(Operation.updated_at.desc(),Operation.id.desc()).all()
            if (core.is_v018_operation(row) and row.status not in TERMINAL)
            or (not core.is_dormant_legacy_project_operation(row) and not core.is_v018_operation(row) and authoritative_status(row) not in TERMINAL)),None)
    elif __import__(
        "eason_one.services.core_v018", fromlist=["is_dormant_legacy_project_operation"]
    ).is_dormant_legacy_project_operation(operation):
        raise ValueError("That pre-v0.18 Project Operation is historical and cannot receive runtime commands.")
    if not operation:
        raise ValueError("No active Operation is available for that deterministic action")
    service=__import__("eason_one.services.operations",fromlist=["pause","resume","stop"])
    if any(term in value for term in ("cancel","stop","terminate","取消","停止","終止")):
        service.stop(operation); action="CANCELLED"
    elif any(term in value for term in ("pause","暫停")):
        service.pause(operation); action="PAUSED"
    elif any(term in value for term in ("resume","continue","恢復","繼續")):
        service.resume(operation); action="RESUMED"
    elif any(term in value for term in ("cost","spent","budget","成本","花多少","預算")):
        action="COST_SNAPSHOT"
    else:
        raise ValueError("The deterministic command is not one of pause, resume, cancel, or cost status")
    snapshot=public_snapshot(operation)
    return {
        "action":action,"operation_id":operation.id,"title":operation.title,
        "status":snapshot["status"],"stage":snapshot["stage"],"budget":snapshot["budget"],
    }


def public_snapshot(operation: Operation) -> dict[str, Any]:
    core=__import__(
        "eason_one.services.core_v018",
        fromlist=["is_v018_operation","is_retired_work_vnext","is_dormant_legacy_project_operation"],
    )
    if core.is_v018_operation(operation):
        runtime=__import__("eason_one.services.company_runtime",fromlist=["runtime_snapshot"]).runtime_snapshot(operation)
        local_cost=Decimal(db.session.query(func.coalesce(func.sum(CostEvent.real_cost_delta),0)).filter(CostEvent.operation_id==operation.id).scalar() or 0)
        project_limit = Decimal("0")
        if operation.project:
            contracts = __import__(
                "eason_one.services.project_contract",
                fromlist=["is_vnext_governed", "assert_authority_ledger"],
            )
            if contracts.is_vnext_governed(operation.project):
                authority = contracts.assert_authority_ledger(operation.project)
                raw = authority.get("effective_budget_limit_twd")
                project_limit = Decimal(str(raw)) if raw not in (None, "") else Decimal("0")
            else:
                project_limit = Decimal(getattr(operation.project,"real_budget_limit",0) or 0)
        return {
            "status": runtime.get("status") or operation.status,
            "legacy_status": operation.status,
            "route_type": operation.route_type,
            "route_reason": operation.route_reason,
            "stage": runtime.get("stage") or "WORK_CORE_V018",
            "state_version": int(operation.state_version or 0),
            "lease_owner": None,
            "lease_expires_at": None,
            "checkpoint": {"runtime_semantics":"WORK_CORE_V018","active_work":runtime.get("active_work")},
            "budget": {"authorized":str(project_limit),"actual":str(local_cost),"reserved":"0","remaining":str(max(Decimal("0"),project_limit-local_cost))},
            "latest_event": None,
        }
    if core.is_retired_work_vnext(operation) or core.is_dormant_legacy_project_operation(operation):
        return {"status":"RETIRED","legacy_status":operation.status,"route_type":operation.route_type,"route_reason":operation.route_reason,"stage":"HISTORICAL","state_version":int(operation.state_version or 0),"lease_owner":None,"lease_expires_at":None,"checkpoint":{},"budget":{"authorized":"0","actual":"0","reserved":"0","remaining":"0"},"latest_event":None}
    bootstrap(operation, commit=False)
    budget = budget_snapshot(operation)
    latest = OperationEvent.query.filter_by(operation_id=operation.id).order_by(OperationEvent.sequence.desc()).first()
    return {
        "status": authoritative_status(operation),
        "legacy_status": operation.status,
        "route_type": operation.route_type,
        "route_reason": operation.route_reason,
        "stage": operation.current_stage,
        "state_version": int(operation.state_version or 0),
        "lease_owner": operation.lease_owner,
        "lease_expires_at": operation.lease_expires_at.isoformat() if operation.lease_expires_at else None,
        "checkpoint": operation.checkpoint_json or {},
        "budget": {key: str(value) if isinstance(value, Decimal) else value for key, value in budget.items()},
        "latest_event": {
            "sequence": latest.sequence,
            "event_type": latest.event_type,
            "to_status": latest.to_status,
            "stage": latest.stage,
            "created_at": latest.created_at.isoformat() if latest and latest.created_at else None,
        } if latest else None,
    }
