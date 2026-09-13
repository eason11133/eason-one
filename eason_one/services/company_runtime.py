"""Thin process shell for the Eason One v0.20 Company Kernel.

Business semantics live in company_kernel.py. This module only owns process
lifecycle, wake/sleep, restart maintenance, and Founder operator controls.
Legacy runtime code is migration/read compatibility and is never called from
the normal tick path.
"""
from __future__ import annotations

import threading
import traceback

from flask import current_app

from ..extensions import db
from ..models import AgentRun, Escalation, Operation, Project, Work, now
from . import work_runtime
from . import company_kernel
from . import runtime_recovery

_LOCK = threading.Lock()
_THREAD: threading.Thread | None = None
_STOP = threading.Event()
_WAKE = threading.Event()
_HEALTH_LOCK = threading.Lock()
_HEALTH = {
    "thread_alive": False,
    "started_at": None,
    "last_tick_at": None,
    "last_kind": None,
    "last_error": None,
    "consecutive_errors": 0,
}


def is_work_vnext(operation: Operation | None) -> bool:
    return company_kernel.is_kernel_operation(operation)


def adopt_operation(operation: Operation, *, commit: bool = True) -> list[int]:
    return company_kernel.adopt_operation(operation, commit=commit)


def adopt_approved_operations() -> list[int]:
    return company_kernel.adopt_approved_operations()


def _eligible_work() -> Work | None:
    """Compatibility hook for older diagnostics; Company Kernel owns selection."""
    return company_kernel._eligible_work()


def migrate_latest_v017_candidate() -> dict | None:
    """One-time compatibility cutover only; never a normal scheduler path."""
    result = __import__(
        "eason_one.services.legacy_company_runtime_v018",
        fromlist=["migrate_latest_v017_candidate"],
    ).migrate_latest_v017_candidate()
    if result and result.get("migrated") and result.get("operation_id"):
        operation = db.session.get(Operation, result["operation_id"])
        if operation:
            company_kernel.adopt_operation(operation)
    return result


def _health_update(**changes) -> None:
    with _HEALTH_LOCK:
        _HEALTH.update(changes)


def runtime_health() -> dict:
    with _HEALTH_LOCK:
        result = dict(_HEALTH)
    thread = _THREAD
    result["thread_alive"] = bool(thread and thread.is_alive())
    result["owner"] = "COMPANY_KERNEL_V020"
    return result


def _runtime_error(app, phase: str, exc: Exception) -> None:
    app.logger.error("company runtime %s failed: %s\n%s", phase, exc, traceback.format_exc())
    with _HEALTH_LOCK:
        _HEALTH["last_tick_at"] = now().isoformat()
        _HEALTH["last_kind"] = "ERROR"
        _HEALTH["last_error"] = f"{phase}: {type(exc).__name__}: {exc}"
        _HEALTH["consecutive_errors"] = int(_HEALTH.get("consecutive_errors") or 0) + 1


def _maintenance(app, phase: str, fn) -> None:
    try:
        fn()
    except Exception as exc:
        db.session.rollback()
        app.logger.error("company runtime maintenance %s failed: %s", phase, exc)


def tick() -> dict:
    """Run one bounded business step through the sole v0.20 governing kernel."""
    return company_kernel.advance_batch(max_steps=8)


def _loop(app, process_start_cutoff) -> None:
    _health_update(
        thread_alive=True, started_at=now().isoformat(), last_error=None,
        consecutive_errors=0,
    )
    with app.app_context():
        _maintenance(app, "adopt-approved", company_kernel.adopt_approved_operations)
        _maintenance(app, "reconcile-known-platform-faults", runtime_recovery.reconcile_known_platform_faults)
        _maintenance(
            app, "recover-interrupted-founder-requests",
            lambda: runtime_recovery.recover_interrupted_founder_requests(before=process_start_cutoff),
        )
        _maintenance(
            app, "recover-interrupted-meeting-steps",
            lambda: runtime_recovery.recover_interrupted_meeting_steps(before=process_start_cutoff),
        )
        _maintenance(
            app, "recover-interrupted-standalone-runs",
            lambda: runtime_recovery.recover_interrupted_standalone_runs(before=process_start_cutoff),
        )
        _maintenance(app, "reconcile-provider-settlement", runtime_recovery.reconcile_interrupted_provider_settlement)
        _maintenance(app, "resolve-settled-orchestration-reconciliation", runtime_recovery.resolve_settled_orchestration_reconciliation)
        _maintenance(app, "resolve-settled-work-effect-reconciliation", runtime_recovery.resolve_settled_work_effect_reconciliation)
        _maintenance(app, "resolve-work-integrity-recovery", runtime_recovery.resolve_repaired_work_integrity_waits)
        _maintenance(app, "resolve-settled-project-management-reconciliation", runtime_recovery.resolve_settled_project_management_reconciliation)
        _maintenance(app, "resolve-project-result-proof-reconciliation", runtime_recovery.resolve_project_result_proof_reconciliation)
        _maintenance(app, "resolve-project-outcome-evaluation-reconciliation", runtime_recovery.resolve_project_outcome_evaluation_reconciliation)
        _maintenance(app, "resolve-project-ceo-recovery", runtime_recovery.resolve_safe_project_ceo_recovery)
        _maintenance(app, "resolve-project-outcome-reviewer-recovery", runtime_recovery.resolve_safe_project_outcome_reviewer_recovery)
        _maintenance(app, "resolve-safe-system-recovery", runtime_recovery.resolve_safe_system_recovery_waits)
        _maintenance(app, "resolve-safe-project-management-recovery", runtime_recovery.resolve_safe_project_management_system_recovery)
        _maintenance(app, "resolve-safe-continuation-approval-recovery", runtime_recovery.resolve_safe_continuation_approval_system_recovery)
        _maintenance(app, "resolve-safe-hiring-recovery", runtime_recovery.resolve_safe_hiring_system_recovery)
        _maintenance(app, "recover-stale-work", runtime_recovery.recover_stale_work)
        _maintenance(
            app, "ceo-future-only-cutover",
            lambda: __import__(
                "eason_one.services.ceo_runtime", fromlist=["ensure_cutover"]
            ).ensure_cutover(),
        )
        try:
            db.session.remove()
        except Exception:
            pass

    while not _STOP.is_set():
        try:
            with app.app_context():
                _maintenance(app, "resolve-internal-waits", runtime_recovery.resolve_internal_waits)
                _maintenance(app, "reconcile-provider-settlement", runtime_recovery.reconcile_interrupted_provider_settlement)
                _maintenance(app, "resolve-settled-orchestration-reconciliation", runtime_recovery.resolve_settled_orchestration_reconciliation)
                _maintenance(app, "resolve-settled-work-effect-reconciliation", runtime_recovery.resolve_settled_work_effect_reconciliation)
                _maintenance(app, "resolve-work-integrity-recovery", runtime_recovery.resolve_repaired_work_integrity_waits)
                _maintenance(app, "resolve-settled-project-management-reconciliation", runtime_recovery.resolve_settled_project_management_reconciliation)
                _maintenance(app, "resolve-project-result-proof-reconciliation", runtime_recovery.resolve_project_result_proof_reconciliation)
                _maintenance(app, "resolve-project-outcome-evaluation-reconciliation", runtime_recovery.resolve_project_outcome_evaluation_reconciliation)
                _maintenance(app, "resolve-project-ceo-recovery", runtime_recovery.resolve_safe_project_ceo_recovery)
                _maintenance(app, "resolve-project-outcome-reviewer-recovery", runtime_recovery.resolve_safe_project_outcome_reviewer_recovery)
                _maintenance(app, "resolve-safe-system-recovery", runtime_recovery.resolve_safe_system_recovery_waits)
                _maintenance(app, "resolve-safe-project-management-recovery", runtime_recovery.resolve_safe_project_management_system_recovery)
                _maintenance(app, "resolve-safe-continuation-approval-recovery", runtime_recovery.resolve_safe_continuation_approval_system_recovery)
                _maintenance(app, "resolve-safe-hiring-recovery", runtime_recovery.resolve_safe_hiring_system_recovery)
                _maintenance(app, "repair-orphaned-waits", runtime_recovery.repair_orphaned_waiting_work)
                result = tick()
                _maintenance(
                    app, "ceo-operating-cycle",
                    lambda: __import__(
                        "eason_one.services.ceo_runtime", fromlist=["process_pending_events"]
                    ).process_pending_events(max_events=8),
                )
                _health_update(
                    last_tick_at=now().isoformat(),
                    last_kind=(result.get("last") or {}).get("status") or result.get("kind"),
                    last_error=None,
                    consecutive_errors=0,
                )
                if not result.get("productive_steps"):
                    _maintenance(app, "recover-stale-work", runtime_recovery.recover_stale_work)
                db.session.remove()
        except Exception as exc:
            with app.app_context():
                db.session.rollback()
                _runtime_error(app, "runtime-loop", exc)
                try:
                    db.session.remove()
                except Exception:
                    pass
        _WAKE.wait(timeout=2.0)
        _WAKE.clear()
    _health_update(thread_alive=False)


def start_company_runtime(app) -> bool:
    global _THREAD
    with _LOCK:
        if _THREAD and _THREAD.is_alive():
            return False
        _STOP.clear()
        _WAKE.clear()
        process_start_cutoff = now()
        _THREAD = threading.Thread(
            target=_loop, args=(app, process_start_cutoff), name="eason-one-company-runtime", daemon=True,
        )
        _THREAD.start()
        return True


def ensure_company_runtime(app) -> bool:
    started = False
    if not _THREAD or not _THREAD.is_alive():
        started = start_company_runtime(app)
    _WAKE.set()
    return started


def runtime_snapshot(operation: Operation) -> dict:
    works = sorted(operation.works, key=lambda row: row.id)
    delivery = [work for work in works if work.work_type != "MANAGEMENT"]
    management = next((work for work in works if work.work_type == "MANAGEMENT"), None)
    project_terminal = bool(
        operation.project is not None
        and str(operation.project.status or "").upper() in {"COMPLETED", "CANCELLED", "FAILED"}
    )
    active_runs = [] if project_terminal else (
        AgentRun.query.filter_by(operation_id=operation.id, status="RUNNING")
        .order_by(AgentRun.id.desc()).all()
    )
    active_work_ids = {run.work_id for run in active_runs if run.work_id}
    active_works = [
        work for work in delivery
        if work.id in active_work_ids or work.state in {"EXECUTING", "VERIFYING"}
    ]
    active_works.sort(key=lambda row: row.id)
    active_work = active_works[0] if active_works else None
    active_rows = []
    for work in active_works:
        assignment = work_runtime.active_assignment(work)
        employee = getattr(assignment, "employee", None) if assignment else None
        active_rows.append({
            "id": work.id, "title": work.title, "state": work.state,
            "employee_id": getattr(employee, "id", None),
            "employee": getattr(employee, "name", None),
            "employee_slug": getattr(employee, "slug", None),
        })
    return {
        "owner": "COMPANY_KERNEL_V020" if is_work_vnext(operation) else "LEGACY_OPERATION_RUNTIME",
        "work_vnext": is_work_vnext(operation),
        "operation_id": operation.id,
        "project_id": operation.project_id,
        "active": bool(active_runs or active_works),
        "active_work": (
            {"id": active_work.id, "title": active_work.title, "state": active_work.state}
            if active_work else None
        ),
        "active_works": active_rows,
        "active_employee_ids": sorted({row["employee_id"] for row in active_rows if row["employee_id"] is not None}),
        "active_execution_id": getattr(active_runs[0], "id", None) if active_runs else None,
        "active_execution_ids": [run.id for run in active_runs],
        "management_gate": ([] if project_terminal else work_runtime.open_gates(management)) if management else [],
        "work_counts": {
            state: sum(work.state == state for work in delivery)
            for state in ["PROPOSED", "READY", "EXECUTING", "WAITING", "VERIFYING", "ACCEPTED", "ABANDONED", "CANCELLED"]
        },
        "done": sum(work.state == "ACCEPTED" for work in delivery),
        "total": len(delivery),
    }


def has_founder_gate(operation: Operation | None) -> bool:
    """Whether canonical Founder Governance currently owns an authority question.

    Work waits such as RECONCILIATION/BUDGET and generic Escalations are company
    execution state, not Founder authority.  Manual Founder pause is also a
    control state, not an authority question.
    """
    if not is_work_vnext(operation) or not operation.project_id:
        return False
    governance = __import__(
        "eason_one.services.governance", fromlist=["attention", "EXECUTION_SCOPED_TYPES", "normalize_type"]
    )
    for gate in governance.attention(operation.project_id):
        kind = governance.normalize_type(gate.escalation_type)
        if kind in governance.EXECUTION_SCOPED_TYPES:
            if gate.operation_id not in {None, operation.id}:
                continue
        return True
    return False


def pause_operation(operation: Operation, *, reason: str = "Founder paused company execution.") -> int:
    if not is_work_vnext(operation):
        raise ValueError("Operation is not owned by the Work-first Company Kernel")
    paused = 0
    for work in list(operation.works):
        if work.state in work_runtime.TERMINAL_WORK_STATES:
            continue
        # Manual pause is an explicit Founder control, not a request for new
        # authority. Keep it separate from canonical Governance waits.
        if not work_runtime.has_open_gate(work, "FOUNDER_PAUSE"):
            work_runtime.open_wait(work, "FOUNDER_PAUSE", reason)
            paused += 1
    if operation.project and operation.project.status not in {"COMPLETED", "CANCELLED"}:
        operation.project.status = "BLOCKED"
        operation.project.current_state_summary = "Founder paused execution. No new Work will start until resumed."
    operation.waiting_reason = reason
    db.session.commit()
    return paused


def pause_project(project: Project, *, reason: str = "Founder paused Project execution.") -> int:
    """Pause one Founder Project without cancelling or rewriting durable history.

    PAUSED is non-terminal. Existing Work/Artifact/Evidence/Cost truth remains
    intact. Every non-terminal Work receives an exact FOUNDER_PAUSE gate so
    alternate scheduler/recovery paths cannot treat the pause as ordinary
    BLOCKED work. Deterministic settlement may still persist already-observed
    truth, but project_can_activate() forbids silent reactivation.
    """
    if project.status in {"COMPLETED", "CANCELLED"}:
        raise ValueError("Terminal Project cannot be paused")
    issue = f"PROJECT_PAUSE:{int(project.id)}"
    paused = 0
    for work in Work.query.filter_by(project_id=project.id).order_by(Work.id).all():
        if work.state in work_runtime.TERMINAL_WORK_STATES or work.state == "PROPOSED":
            # PROPOSED Work has not entered the runnable state machine yet.
            # Project.status=PAUSED is sufficient authority to keep it inert,
            # and forcing PROPOSED -> WAITING would violate Work transitions.
            continue
        if not any(str(row.get("issue_code") or "") == issue for row in work_runtime.open_gates(work, "FOUNDER_PAUSE")):
            work_runtime.open_wait(
                work, "FOUNDER_PAUSE", reason,
                issue_code=issue,
                resume_state=(work.state if work.state in {"READY", "EXECUTING", "VERIFYING"} else None),
            )
            paused += 1
    for operation in Operation.query.filter_by(project_id=project.id).all():
        if operation.kernel_status not in {"COMPLETED", "FAILED", "CANCELLED"}:
            operation.waiting_reason = reason
    project.status = "PAUSED"
    project.current_state_summary = "Founder paused this Project. Durable company state is preserved; no new paid Work may start until Resume."
    project.next_milestone = "Explicit Founder Resume restores ordinary Company sequencing from the durable checkpoint."
    db.session.commit()
    return paused


def resume_project(project: Project, *, resolution: str = "RESUMED") -> int:
    """Resume one PAUSED Project from its existing durable checkpoint."""
    if project.status != "PAUSED":
        return 0
    issue = f"PROJECT_PAUSE:{int(project.id)}"
    resolved = 0
    for work in Work.query.filter_by(project_id=project.id).order_by(Work.id).all():
        resolved += work_runtime.resolve_waits(
            work, "FOUNDER_PAUSE", issue_code=issue,
            note=f"Founder resumed Project: {resolution}.",
        )
    for operation in Operation.query.filter_by(project_id=project.id).all():
        if operation.waiting_reason == "Founder paused Project execution." or str(operation.waiting_reason or "").startswith("Founder paused"):
            operation.waiting_reason = None
    # Clear PAUSED before asking whether another canonical blocker still owns
    # the Project. Resume never clears those unrelated gates.
    project.status = "BLOCKED"
    if work_runtime.project_can_activate(project):
        project.status = "ACTIVE"
        project.current_state_summary = "Founder resumed this Project; Company sequencing will continue from durable Work/Artifact/Evidence truth."
    else:
        project.current_state_summary = "Founder resumed this Project, but another current authority/recovery blocker still owns the next step."
    db.session.commit()
    wake_company_runtime()
    return resolved


def cancel_operation(operation: Operation, *, reason: str = "Founder cancelled the approved work.") -> int:
    if not is_work_vnext(operation):
        raise ValueError("Operation is not owned by the Work-first Company Kernel")
    cancelled = 0
    for work in list(operation.works):
        if work.state in work_runtime.TERMINAL_WORK_STATES:
            continue
        work_runtime.transition(work, "CANCELLED", actor_type="FOUNDER", reason=reason)
        work_runtime.sync_task_projection(work)
        work_runtime.resolve_waits(work, note="Cancelled by Founder.")
        cancelled += 1
    governance = __import__(
        "eason_one.services.governance", fromlist=["is_founder_type"]
    )
    # Mission Stop cannot resolve Founder Project authority.  It may clean up
    # operation-local internal/recovery escalations only.
    for escalation in Escalation.query.filter_by(operation_id=operation.id, state="OPEN").all():
        if governance.is_founder_type(escalation.escalation_type):
            continue
        escalation.state = "RESOLVED"
        escalation.resolved_at = now()
        escalation.resolution = "MISSION_CANCELLED"
    operation.status = "CANCELLED"
    operation.kernel_status = "CANCELLED"
    operation.current_stage = "CANCELLED"
    operation.waiting_reason = reason
    operation.ended_at = operation.ended_at or now()
    operation.lease_owner = None
    operation.lease_expires_at = None

    # Mission Stop and Project Cancel are distinct authority semantics. Stopping
    # any vNext Mission never cancels the Founder Project Contract implicitly.
    if operation.project and operation.project.status not in {"COMPLETED", "CANCELLED", "REVIEW"}:
        if work_runtime.project_can_activate(operation.project):
            operation.project.status = "ACTIVE"
            operation.project.current_state_summary = (
                "Founder stopped one Mission; the Project Contract remains active and company management must replan or await explicit Project Cancel."
            )
        else:
            operation.project.status = "BLOCKED"
    db.session.commit()
    return cancelled


def resume_after_founder(operation: Operation, *, resolution: str = "RESUMED") -> int:
    """Resume only an explicit manual Founder pause.

    Canonical Founder authority gates are resolved exclusively by
    governance.resolve_gate().  This function deliberately does not resolve
    BUDGET/RECONCILIATION waits or mutate any Escalation row.
    """
    if not is_work_vnext(operation):
        raise ValueError("Operation is not owned by the Work-first Company Kernel")
    resolved = 0
    for work in list(operation.works):
        resolved += work_runtime.resolve_waits(
            work, "FOUNDER_PAUSE", note=f"Founder resumed manual pause: {resolution}."
        )
    operation.waiting_reason = None
    # Do not make a Project ACTIVE while another canonical Founder gate or
    # company recovery wait still blocks it. Project lifecycle truth belongs to
    # those owning resolvers.
    if operation.project and operation.project.status == "BLOCKED":
        if work_runtime.project_can_activate(operation.project):
            operation.project.status = "ACTIVE"
            operation.project.current_state_summary = "Founder resumed the manually paused Mission; company execution may continue."
    db.session.commit()
    wake_company_runtime()
    return resolved


def wake_company_runtime(app=None) -> None:
    """Wake the v0.20 process owner without depending on an HTTP route.

    Initial Founder approval, delegated continuation, recovery and other lawful
    service-layer transitions may all be the thing that makes Company Work
    runnable. In a production app context, make sure the process thread exists
    before setting the wake event. TESTING keeps background execution explicit
    so deterministic tests are not raced by a daemon thread.
    """
    resolved_app = app
    if resolved_app is None:
        try:
            resolved_app = current_app._get_current_object()
        except RuntimeError:
            resolved_app = None
    if resolved_app is not None:
        auto_start = resolved_app.config.get(
            "AUTO_START_COMPANY_RUNTIME",
            resolved_app.config.get("AUTO_START_OPERATION_RUNTIME", not resolved_app.testing),
        )
        if auto_start:
            ensure_company_runtime(resolved_app)
            return
    _WAKE.set()


def stop_company_runtime(timeout: float = 2.0) -> None:
    global _THREAD
    _STOP.set()
    _WAKE.set()
    thread = _THREAD
    if thread and thread.is_alive():
        thread.join(timeout=timeout)
    _THREAD = None
