"""Founder-facing stabilization helpers.

This module deliberately contains no Provider calls.  It is the one place that
separates real company work from system-validation history and chooses the
single Mission that should be treated as current across Headquarters surfaces.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy import or_

from ..extensions import db
from ..models import AgentRun, Operation, Project, Task, Work

REAL_WORK = "REAL_WORK"
SYSTEM_VALIDATION = "SYSTEM_VALIDATION"

_VALIDATION_TITLES = {
    "CEO Direct Line Reliability Assessment",
    "Eason One Mission Room Live Execution Monitor Read-Only Validation",
}
_VALIDATION_MARKERS = (
    "runtime validation",
    "meeting validation",
    "autonomous meeting validation",
    "read-only validation",
    "read only validation",
    "smoke test",
    "system validation",
)


def operation_kind(operation: Operation | None) -> str:
    if operation is None:
        return REAL_WORK
    memory = dict(operation.memory_json or {})
    explicit = str(memory.get("mission_kind") or "").upper()
    if explicit == SYSTEM_VALIDATION:
        return SYSTEM_VALIDATION
    if memory.get("archive") or (operation.founder_report_json or {}).get("decision_kind") == "RUNTIME_VALIDATION_ARCHIVE":
        return SYSTEM_VALIDATION
    project = operation.project
    if project is not None and project.environment != "LIVE":
        return SYSTEM_VALIDATION
    title = " ".join((operation.title or "").split())
    objective = " ".join((operation.objective or "").split()).casefold()
    combined = f"{title} {objective}".casefold()
    # A known validation title is stronger evidence than an old incorrectly
    # stamped REAL_WORK flag. This is the exact stale Mission class reproduced
    # by the Founder after V0.12.1.
    if title in _VALIDATION_TITLES or any(marker in combined for marker in _VALIDATION_MARKERS):
        return SYSTEM_VALIDATION
    if explicit == REAL_WORK:
        return REAL_WORK
    return REAL_WORK


def stamp_kind(operation: Operation, kind: str, *, reason: str | None = None) -> bool:
    kind = kind.upper()
    if kind not in {REAL_WORK, SYSTEM_VALIDATION}:
        raise ValueError(f"Unsupported Mission kind: {kind}")
    memory = dict(operation.memory_json or {})
    changed = memory.get("mission_kind") != kind
    memory["mission_kind"] = kind
    if reason:
        memory["mission_kind_reason"] = reason
    operation.memory_json = memory
    return changed


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _run_operation(run: AgentRun | None) -> Operation | None:
    if not run:
        return None
    if run.operation_id:
        return db.session.get(Operation, run.operation_id)
    if run.work_id:
        work = db.session.get(Work, run.work_id)
        return work.operation if work else None
    return None


def active_execution_run() -> AgentRun | None:
    """Return only a live v0.18 execution; historical runs cannot steal focus."""
    candidates = (
        AgentRun.query.outerjoin(Project, AgentRun.project_id == Project.id)
        .filter(
            AgentRun.purpose != "CEO_FOUNDER_REQUEST",
            AgentRun.status.in_(["CREATED", "RUNNING"]),
            or_(
                AgentRun.project_id.is_(None),
                (Project.environment == "LIVE")
                & Project.status.notin_(["COMPLETED", "FAILED", "CANCELLED", "PAUSED"]),
            ),
        )
        .order_by(AgentRun.id.desc()).all()
    )
    core = __import__("eason_one.services.core_v018", fromlist=["is_v018_operation"])
    return next((run for run in candidates if core.is_v018_operation(_run_operation(run))), None)


def active_operation() -> Operation | None:
    run = active_execution_run()
    operation = _run_operation(run)
    if operation:
        return operation
    core = __import__("eason_one.services.core_v018", fromlist=["is_v018_operation"])
    active_works = (
        Work.query.join(Project, Work.project_id == Project.id)
        .filter(
            Project.environment == "LIVE",
            Project.status.notin_(["COMPLETED", "FAILED", "CANCELLED", "PAUSED"]),
            Work.state.in_(["EXECUTING", "VERIFYING"]),
        )
        .order_by(Work.updated_at.desc(), Work.id.desc()).all()
    )
    for work in active_works:
        if core.is_v018_operation(work.operation):
            return work.operation
    return None


def choose_focus(
    rows: Iterable[dict[str, Any]],
    *,
    preferred_operation_id: int | None = None,
    include_validation: bool = False,
) -> dict[str, Any] | None:
    rows = list(rows)
    if not include_validation:
        rows = [row for row in rows if row.get("kind", operation_kind(row.get("operation"))) == REAL_WORK]
    if not rows:
        return None
    current_rows = [row for row in rows if row.get("classification") != "TERMINAL"]
    # A terminal Mission remains registry/history truth but must never become the
    # company's current priority merely because no live Mission exists.
    if not current_rows:
        return None
    active = active_operation()
    if active:
        match = next((row for row in current_rows if row["operation"].id == active.id), None)
        if match:
            return match
    running = [
        row for row in current_rows
        if row["operation"].status == "RUNNING"
    ]
    if running:
        return max(running, key=lambda row: (row["operation"].updated_at, row["operation"].id))
    if preferred_operation_id:
        match = next((row for row in current_rows if row["operation"].id == preferred_operation_id), None)
        if match:
            return match
    priority = {
        "WAITING_FOR_FOUNDER": 0,
        "BLOCKED": 1,
        "ACTIVE": 2,
    }
    return min(
        current_rows,
        key=lambda row: (
            priority.get(row.get("classification"), 9),
            -int((row["operation"].updated_at or row["operation"].created_at).timestamp()),
            -row["operation"].id,
        ),
    )


def _founder_phase(raw_phase: str | None, work_state: str | None) -> tuple[str, str, str | None]:
    raw = str(raw_phase or "").upper()
    groups = {
        "INSPECT": {"READING_REPOSITORY", "ANALYZING_TASK", "INSPECT"},
        "PREPARE": {"JOB_SPEC_PREPARED", "STARTING_CODEX", "PREPARE"},
        "IMPLEMENT": {"CODEX_SESSION_STARTED", "RUNNING_COMMAND", "APPLYING_CHANGES", "IMPLEMENT"},
        "VERIFY": {"RUNNING_TESTS", "VALIDATING_OUTPUT", "VERIFY", "VERIFYING"},
        "DELIVER": {"SYNTHESIZING_RESULT", "EVIDENCE_PERSISTED", "DELIVER"},
    }
    phase = next((name for name, values in groups.items() if raw in values), None)
    if phase is None:
        phase = "VERIFY" if str(work_state or "").upper() == "VERIFYING" else "IMPLEMENT"
    labels = {"INSPECT": "Inspect", "PREPARE": "Prepare", "IMPLEMENT": "Implement", "VERIFY": "Verify", "DELIVER": "Deliver"}
    order = ["INSPECT", "PREPARE", "IMPLEMENT", "VERIFY", "DELIVER"]
    idx = order.index(phase)
    next_phase = labels[order[idx + 1]] if idx + 1 < len(order) else None
    return phase, labels[phase], next_phase


def global_runtime_snapshot(*, include_validation: bool = False) -> dict[str, Any]:
    company_runtime = __import__(
        "eason_one.services.company_runtime", fromlist=["runtime_health"]
    )
    health = company_runtime.runtime_health()
    run = active_execution_run()
    work = db.session.get(Work, run.work_id) if run and run.work_id else None
    operation = db.session.get(Operation, run.operation_id) if run and run.operation_id else active_operation()
    if not operation or (not include_validation and operation_kind(operation) != REAL_WORK):
        core = __import__("eason_one.services.core_v018", fromlist=["is_v018_operation"])
        queued = next((
            row for row in Work.query.join(Project, Work.project_id == Project.id)
            .filter(
                Project.environment == "LIVE",
                Project.status.notin_(["COMPLETED", "FAILED", "CANCELLED", "PAUSED"]),
                Work.state == "READY",
            )
            .order_by(Work.updated_at.desc(), Work.id.desc()).all()
            if core.is_v018_operation(row.operation)
        ), None)
        queued_payload = None
        if queued is not None:
            work_runtime = __import__(
                "eason_one.services.work_runtime", fromlist=["active_assignment"]
            )
            assignment = work_runtime.active_assignment(queued)
            employee = getattr(getattr(assignment, "employee", None), "name", None)
            project = db.session.get(Project, queued.project_id) if queued.project_id else None
            queued_payload = {
                "id": queued.id,
                "project_id": queued.project_id,
                "title": queued.title,
                "project_title": getattr(project, "name", None),
                "employee": employee,
                "status": queued.state,
                "href": f"/headquarters/projects/{queued.project_id}" if queued.project_id else "/headquarters/projects",
            }
        return {
            "active": False,
            "runtime_health": health,
            "queued_work": queued_payload,
        }
    if not work:
        work = next((row for row in operation.works if row.state in {"EXECUTING", "VERIFYING"}), None)
    project = db.session.get(Project, operation.project_id) if operation.project_id else None
    runtime = dict((operation.memory_json or {}).get("runtime") or {})
    live = dict(runtime.get("live_execution") or {})
    task = db.session.get(Task, run.task_id) if run and run.task_id else None
    employee = run.employee.name if run and run.employee else live.get("employee")
    is_vnext = __import__(
        "eason_one.services.core_v018", fromlist=["is_v018_operation"]
    ).is_v018_operation(operation)
    active = bool(run or (work and work.state in {"EXECUTING", "VERIFYING"}))
    phase, phase_label, next_phase = _founder_phase(live.get("phase"), getattr(work, "state", None))
    raw_action = live.get("current_action") or live.get("detail") or getattr(work, "purpose", None) or "Working on the approved outcome"
    return {
        "active": active,
        "operation_id": operation.id,
        "project_id": getattr(project, "id", None),
        "work_id": getattr(work, "id", None),
        "title": getattr(project, "name", None) or operation.title,
        "mission_title": operation.title,
        "kind": operation_kind(operation),
        "href": f"/headquarters/projects/{project.id}" if project else f"/headquarters/missions/{operation.id}",
        "status": getattr(work, "state", None) if is_vnext else operation.status,
        "run_id": run.id if run else live.get("run_id"),
        "employee": employee,
        "task": getattr(work, "title", None) or getattr(task, "title", None) or operation.objective,
        "phase": phase,
        "phase_label": phase_label,
        "next_phase": next_phase,
        "raw_phase": live.get("phase"),
        "current_action": raw_action,
        "started_at": getattr(run, "started_at", None).isoformat() if run and run.started_at else live.get("started_at"),
        "last_activity_at": live.get("last_activity_at") or runtime.get("heartbeat_at"),
        "updated_at": _iso(getattr(work, "updated_at", None) or operation.updated_at),
        "runtime_health": health,
    }

