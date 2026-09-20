"""Work-first Company Runtime for Eason One v0.17.

One durable business spine drives approved company work:
Project -> Work -> Execution -> Artifact/Verification -> Founder outcome.
Operation is retained as the proposal/audit envelope during migration; its
kernel/lease/Task scheduler does not decide whether approved vNext Work runs.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import threading
import time
import traceback

from ..extensions import db
from ..models import (
    AgentRun, Artifact, ArtifactVersion, CompanyEvent, CostEvent, Escalation,
    ExternalEffectAttempt, Meeting, MeetingEvent, Operation, Project, WaitCondition, Work, WorkMessage, now,
)
from ..schemas import CEO_CLOSURE_SCHEMA
from . import artifacts, work_runtime
from .company_events import emit
from .execution import execute

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

_ACTIVE_PROJECT_STATES = {"ACTIVE", "PLANNING", "BLOCKED"}
_HARD_WAITS = {"FOUNDER_DECISION", "BUDGET", "RECONCILIATION", "VERIFICATION"}


def is_work_vnext(operation: Operation | None) -> bool:
    # Compatibility name retained for callers. v0.18 Company Runtime itself
    # schedules only the explicit WORK_CORE_V018 lane.
    return __import__(
        "eason_one.services.core_v018", fromlist=["is_v018_operation"]
    ).is_v018_operation(operation)


def _compat_running(operation: Operation) -> None:
    """Project Work state is authoritative; keep legacy Operation as projection."""
    if operation.status not in {"COMPLETED", "FAILED", "TERMINATED_BY_FOUNDER", "SUPERSEDED"}:
        operation.status = "RUNNING"
        operation.kernel_status = "RUNNING"
        operation.current_stage = "WORK_RUNTIME"
        operation.waiting_reason = None
        operation.lease_owner = None
        operation.lease_expires_at = None


def _compat_complete(operation: Operation) -> None:
    operation.status = "COMPLETED"
    operation.kernel_status = "COMPLETED"
    operation.current_stage = "COMPLETED"
    operation.ended_at = operation.ended_at or now()
    operation.lease_owner = None
    operation.lease_expires_at = None
    operation.waiting_reason = None


def _ensure_serial_dependencies(operation: Operation) -> None:
    """Use CEO plan order as the safe default without an extra orchestration call."""
    delivery = [row for row in sorted(operation.works, key=lambda x: x.id) if row.work_type != "MANAGEMENT"]
    if len(delivery) < 2:
        return
    if any(row.dependency_edges for row in delivery):
        return
    for previous, current in zip(delivery, delivery[1:]):
        db.session.add(__import__("eason_one.models", fromlist=["WorkDependency"]).WorkDependency(
            work_id=current.id, depends_on_work_id=previous.id,
        ))


def adopt_operation(operation: Operation, *, commit: bool = True) -> list[int]:
    if not is_work_vnext(operation):
        return []
    if not operation.project_id:
        return []
    if not operation.works:
        plan = __import__("eason_one.services.operations", fromlist=["validate_plan"]).validate_plan(operation.plan_json)
        work_runtime.materialize_operation_works(operation, plan["operation"])
        db.session.flush()
        db.session.expire(operation, ["works", "tasks"])
    management = work_runtime.ensure_management_work(operation)
    # Meetings are coordination work inside the approved Project. They inherit
    # the management Work authority/budget envelope instead of falling back to
    # the legacy Operation kernel.
    for meeting in Meeting.query.filter_by(operation_id=operation.id).all():
        if meeting.related_work_id != management.id:
            meeting.related_work_id = management.id
    _ensure_serial_dependencies(operation)
    _compat_running(operation)
    if operation.project and operation.project.status not in {"COMPLETED", "CANCELLED", "FAILED"}:
        operation.project.status = "ACTIVE"
        operation.project.current_state_summary = "CEO is executing the approved Work."
    if commit:
        db.session.commit()
    return [work.id for work in operation.works]


def adopt_approved_operations() -> list[int]:
    adopted = []
    for operation in Operation.query.filter(Operation.approved_at.isnot(None)).order_by(Operation.id).all():
        if not is_work_vnext(operation):
            continue
        if operation.project and operation.project.status in {"COMPLETED", "CANCELLED", "FAILED"}:
            continue
        adopt_operation(operation, commit=False)
        adopted.append(operation.id)
    if adopted:
        db.session.commit()
    return adopted


def migrate_latest_v017_candidate() -> dict | None:
    """Move at most the latest safe retired WORK_VNEXT Project onto v0.18.

    Historical Projects stay dormant. This exists for the currently-live Project
    that was already Founder-approved before the Core Cutover (for example the
    user's Project #17). A row with ambiguous post-dispatch truth is never
    migrated or replayed automatically.
    """
    core = __import__(
        "eason_one.services.core_v018",
        fromlist=["LEGACY_WORK_RUNTIME_SEMANTICS", "RUNTIME_SEMANTICS", "stamp_v018"],
    )
    # The cutover is a one-time adoption, not a restart-time sweep. Once any
    # approved v0.18 Operation exists, older WORK_VNEXT rows are history and
    # must never be awakened by a later process/reloader restart.
    already_cut_over = next((
        row for row in Operation.query.filter(Operation.approved_at.isnot(None)).order_by(Operation.id.desc()).all()
        if (row.memory_json or {}).get("runtime_semantics") == core.RUNTIME_SEMANTICS
    ), None)
    if already_cut_over is not None:
        return None

    candidates = [
        operation for operation in Operation.query.filter(Operation.approved_at.isnot(None)).order_by(Operation.id.desc()).all()
        if (operation.memory_json or {}).get("runtime_semantics") == core.LEGACY_WORK_RUNTIME_SEMANTICS
        and operation.project
        and operation.project.environment == "LIVE"
        and operation.project.status not in {"COMPLETED", "CANCELLED"}
        and operation.status not in {"COMPLETED", "CANCELLED", "TERMINATED_BY_FOUNDER", "SUPERSEDED"}
    ]
    if not candidates:
        return None
    operation = candidates[0]
    works = list(sorted(operation.works, key=lambda row: row.id))
    if not works:
        return None

    ambiguous = (
        AgentRun.query.filter(
            AgentRun.work_id.in_([work.id for work in works]),
            AgentRun.outcome == "FAILED_AMBIGUOUS",
        ).order_by(AgentRun.id.desc()).first()
    )
    running = (
        AgentRun.query.filter(
            AgentRun.work_id.in_([work.id for work in works]),
            AgentRun.status == "RUNNING",
        ).order_by(AgentRun.id.desc()).first()
    )
    if ambiguous or running:
        memory = dict(operation.memory_json or {})
        memory["v018_migration_blocked"] = {
            "reason": "AMBIGUOUS_EXTERNAL_EFFECT" if ambiguous else "RUNNING_EXECUTION_REQUIRES_RECONCILIATION",
            "run_id": getattr(ambiguous or running, "id", None),
        }
        operation.memory_json = memory
        db.session.commit()
        return {"migrated": False, "operation_id": operation.id, "project_id": operation.project_id, "reason": memory["v018_migration_blocked"]["reason"]}

    # Capture legacy wait rows before the semantics switch. They are copied into
    # Work.runtime_control_json only when they represent a real current gate.
    wait_rows = {
        work.id: list(WaitCondition.query.filter_by(work_id=work.id, state="OPEN").order_by(WaitCondition.id).all())
        for work in works
    }
    core.stamp_v018(operation)
    memory = dict(operation.memory_json or {})
    memory["migrated_from_runtime_semantics"] = core.LEGACY_WORK_RUNTIME_SEMANTICS
    memory["v018_migrated_at"] = now().isoformat()
    operation.memory_json = memory

    hard_gate_types = {"FOUNDER_DECISION", "BUDGET", "RECONCILIATION", "VERIFICATION", "DEPENDENCY"}
    reopened = []
    for work in works:
        # Start with a clean v0.18 gate container. Historical WaitCondition rows
        # remain auditable below but no longer govern this Work.
        work.runtime_control_json = {"version": "WORK_CORE_V018", "gates": [], "next_gate_id": 1}

        if work.state == "ABANDONED":
            latest = AgentRun.query.filter_by(work_id=work.id).order_by(AgentRun.id.desc()).first()
            if work.work_type == "MANAGEMENT":
                work.state = "EXECUTING"
                work.abandoned_at = None
                reopened.append(work.id)
            elif latest and latest.status == "FAILED" and latest.outcome in {"FAILED_SAFE", "FAILED_KNOWN"}:
                work.state = "READY"
                work.abandoned_at = None
                work.accepted_at = None
                work.cancelled_at = None
                latest.resolution_status = latest.resolution_status or "V018_SAFE_MIGRATION_RETRY"
                latest.resolution_note = latest.resolution_note or "Safe known v0.17 failure reopened by v0.18 Core Cutover."
                reopened.append(work.id)
            else:
                # A terminal Work without safe failure evidence cannot be replayed.
                db.session.rollback()
                return {"migrated": False, "operation_id": operation.id, "project_id": operation.project_id, "reason": "TERMINAL_WORK_NOT_PROVEN_SAFE"}

        imported_hard_gate = False
        resume_hint = None
        for wait in wait_rows.get(work.id, []):
            if wait.condition_type in hard_gate_types and work.state not in work_runtime.TERMINAL_WORK_STATES:
                work_runtime.open_wait(
                    work, wait.condition_type, wait.reason or "Migrated v0.17 Work gate.",
                    target_work_id=wait.target_work_id, retry_after=wait.retry_after,
                    resume_state=wait.resume_state,
                )
                imported_hard_gate = True
            else:
                # A gate attached to already terminal Work is stale audit history,
                # not authority to resurrect an accepted/cancelled commitment.
                resume_hint = resume_hint or wait.resume_state
            wait.state = "RESOLVED"
            wait.resolved_at = now()
            wait.resolution_note = "Retired by v0.18 Core Cutover; governing state moved onto Work."

        if work.state == "WAITING" and not imported_hard_gate:
            target = resume_hint if resume_hint in {"READY", "EXECUTING", "VERIFYING"} else (
                "EXECUTING" if work.work_type == "MANAGEMENT" else "READY"
            )
            work_runtime.transition(
                work, target, actor_type="RUNTIME",
                reason="v0.18 retired the legacy wait/recovery scheduler and restored the Work-owned checkpoint.",
            )
        work_runtime.sync_task_projection(work)

    operation.status = "RUNNING"
    operation.kernel_status = "RUNNING"  # compatibility projection only
    operation.current_stage = "WORK_CORE_V018"
    operation.waiting_reason = None
    operation.ended_at = None
    operation.founder_report_json = None
    operation.lease_owner = None
    operation.lease_expires_at = None
    operation.project.status = "BLOCKED" if has_founder_gate(operation) else "ACTIVE"
    operation.project.current_state_summary = (
        "v0.18 Core Cutover restored the approved Project to its durable Work state."
    )
    operation.project.next_milestone = "Company Runtime continues the approved Work without reviving historical Projects."
    emit(
        "WORK_CORE_V018_MIGRATED", actor_type="RUNTIME", project_id=operation.project_id,
        correlation_id=f"project:{operation.project_id}",
        payload={"operation_id": operation.id, "reopened_work_ids": reopened},
    )
    db.session.commit()
    adopt_operation(operation)
    return {"migrated": True, "operation_id": operation.id, "project_id": operation.project_id, "reopened_work_ids": reopened}


def _repair_orphaned_waiting_work() -> int:
    """A vNext Work may not remain WAITING without a durable condition."""
    repaired = 0
    for work in Work.query.filter_by(state="WAITING").order_by(Work.id).all():
        if not work.operation or not is_work_vnext(work.operation):
            continue
        if work_runtime.primary_gate(work):
            continue
        if work.work_type == "MANAGEMENT":
            resume = "EXECUTING"
        else:
            latest_artifact = (
                ArtifactVersion.query.join(Artifact)
                .filter(Artifact.work_id == work.id)
                .order_by(ArtifactVersion.id.desc())
                .first()
            )
            # Resume from durable Work/Artifact truth. The compatibility Task
            # projection is never allowed to select a v0.18 scheduler state.
            resume = "VERIFYING" if latest_artifact and latest_artifact.status == "SUBMITTED" else "READY"
        work_runtime.transition(
            work, resume, actor_type="RUNTIME",
            reason="Recovered orphaned WAITING Work with no open durable condition.",
        )
        work_runtime.sync_task_projection(work)
        repaired += 1
    if repaired:
        db.session.commit()
    return repaired


def _resolve_internal_waits() -> int:
    """Resolve only v0.18 Work-owned dependency/retry gates that are due."""
    resolved = 0
    current = now()
    for work in Work.query.filter_by(state="WAITING").order_by(Work.id).all():
        if not work.operation or not is_work_vnext(work.operation):
            continue
        for gate in list(work_runtime.open_gates(work)):
            kind = gate.get("condition_type")
            if kind == "DEPENDENCY" and work_runtime.dependencies_satisfied(work):
                resolved += work_runtime.resolve_waits(
                    work, "DEPENDENCY", note="Upstream Work is accepted."
                )
                break
            if kind in {"INTERNAL_RECOVERY", "RETRY_BACKOFF"} and work_runtime.retry_due(gate, current):
                resolved += work_runtime.resolve_waits(
                    work, kind, note="Bounded internal retry is due."
                )
                break
    if resolved:
        db.session.commit()
    return resolved


def _recover_stale_work() -> int:
    """Recover process-local loss from durable Execution/effect truth.

    Generic provider work is safely replayable only before the durable dispatch
    boundary. Codex persists a pre-dispatch governed-source snapshot; after a
    restart it is replayed only when Git/protected boundaries prove rollback safe.
    """
    changed = 0
    for work in Work.query.filter(Work.state.in_(["EXECUTING", "VERIFYING"])).order_by(Work.id).all():
        operation = work.operation
        if not is_work_vnext(operation):
            continue
        live = AgentRun.query.filter_by(work_id=work.id, status="RUNNING").order_by(AgentRun.id.desc()).first()
        if not live:
            continue
        effect = ExternalEffectAttempt.query.filter_by(execution_id=live.id).order_by(ExternalEffectAttempt.id.desc()).first()
        state = getattr(effect, "state", None)
        if live.provider_key_snapshot == "codex":
            codex = __import__("eason_one.services.codex_connector", fromlist=["reconcile_interrupted_run", "cleanup_recovery_snapshot"])
            result = codex.reconcile_interrupted_run(live)
            if result.get("safe"):
                work_runtime.open_wait(
                    work, "INTERNAL_RECOVERY", result.get("reason") or live.error_text,
                    retry_after=now(),
                )
                db.session.commit()
                codex.cleanup_recovery_snapshot(live)
            else:
                live.status = "FAILED"
                live.outcome = "FAILED_AMBIGUOUS"
                live.failure_reason = "CODEX_PROCESS_RESTART_RECONCILIATION"
                live.failure_stage = "CODEX_TOOL"
                live.error_text = result.get("reason") or (
                    "The process restarted while Codex owned the repository and automatic source rollback could not be proven safe."
                )
                live.finished_at = now()
                if effect is not None:
                    __import__("eason_one.services.external_effects", fromlist=["mark_ambiguous"]).mark_ambiguous(
                        effect, live.error_text
                    )
                work_runtime.open_wait(work, "RECONCILIATION", live.error_text)
            changed += 1
        elif state in {None, "PREPARED", "RESERVED", "FAILED_PRE_DISPATCH"}:
            live.status = "FAILED"
            live.outcome = "FAILED_SAFE"
            live.failure_reason = "PROCESS_RESTART_BEFORE_DISPATCH"
            live.failure_stage = "PRE_DISPATCH"
            live.error_text = "Process restarted before an external dispatch was durably observed."
            live.finished_at = now()
            if work.state != "WAITING":
                work_runtime.open_wait(
                    work, "INTERNAL_RECOVERY", live.error_text,
                    retry_after=now(),
                )
            changed += 1
        else:
            live.status = "FAILED"
            live.outcome = "FAILED_AMBIGUOUS"
            live.failure_reason = "PROCESS_RESTART_AFTER_DISPATCH"
            live.failure_stage = "POST_DISPATCH"
            live.error_text = "Process restarted after external dispatch; effect reconciliation is required before replay."
            live.finished_at = now()
            work_runtime.open_wait(work, "RECONCILIATION", live.error_text)
            changed += 1
    if changed:
        db.session.commit()
    return changed


def _is_validation_scope_fault(run: AgentRun | None) -> bool:
    """Prove that a failed Engineer run was vetoed only by unrelated repo tests.

    This is intentionally strict. Automatic recovery is allowed only when the
    task-specific HTTP contract already passed, every other host check passed,
    the only failed check was the repository-wide regression suite, and Codex's
    write delta was restored after the failed validation. That makes replay a
    safe local side effect rather than a guessed retry.
    """
    if not run or run.failure_reason != "HOST_VALIDATION_FAILED":
        return False
    context = dict(run.context_composition_json or {})
    host = dict(context.get("host_validation") or {})
    checks = [row for row in (host.get("checks") or []) if isinstance(row, dict)]
    failed = [row for row in checks if row.get("status") == "FAILED"]
    http_passed = any(
        row.get("kind") == "HTTP_CONTRACT" and row.get("status") == "PASSED"
        for row in checks
    )
    regression_only = bool(failed) and all(
        row.get("kind") == "REGRESSION_SUITE" for row in failed
    )
    # codex_connector restores failed write deltas before committing failure.
    # Require that proof before allowing a fresh tool execution.
    reverted = "failed_write_delta_reverted" in context
    return http_passed and regression_only and reverted


def _reconcile_validation_scope_faults() -> list[int]:
    """Recover v0.18 Work falsely abandoned by the old verification policy.

    No Provider/Codex call happens here. We only reopen a Work when durable host
    evidence proves its endpoint contract passed and an unrelated historical
    repository suite was the sole veto. The next normal Work tick may then make
    one fresh bounded Engineer attempt under the corrected acceptance policy.
    """
    recovered: list[int] = []
    for work in Work.query.filter_by(state="ABANDONED").order_by(Work.id).all():
        if work.work_type == "MANAGEMENT" or not is_work_vnext(work.operation):
            continue
        control = dict(work.runtime_control_json or {})
        if control.get("validation_scope_recovery_used"):
            continue
        latest = (
            AgentRun.query.filter_by(work_id=work.id, purpose="TASK_EXECUTION")
            .order_by(AgentRun.id.desc()).first()
        )
        if not _is_validation_scope_fault(latest):
            continue

        # Mark every matching platform-caused attempt so those attempts do not
        # consume the Work's genuine bounded retry allowance.
        poisoned = []
        for run in AgentRun.query.filter_by(work_id=work.id, purpose="TASK_EXECUTION").all():
            if _is_validation_scope_fault(run):
                run.resolution_status = "SYSTEM_VALIDATION_SCOPE_FAULT"
                run.resolved_at = now()
                run.resolution_note = (
                    "v0.18 host validation used repository-wide regression outside "
                    "the approved Work acceptance scope; failed write delta was reverted."
                )
                poisoned.append(run.id)

        operation = work.operation
        project = work.project
        work.state = "READY"
        work.abandoned_at = None
        work.accepted_at = None
        work.cancelled_at = None
        control["validation_scope_recovery_used"] = True
        control["validation_scope_recovery"] = {
            "at": now().isoformat(),
            "source_run_id": latest.id,
            "platform_fault_run_ids": poisoned,
            "reason": "UNRELATED_REPOSITORY_REGRESSION_VETO",
        }
        work.runtime_control_json = control
        work_runtime.sync_task_projection(work)

        operation.status = "RUNNING"
        operation.kernel_status = "RUNNING"
        operation.current_stage = "WORK_CORE_V018"
        operation.waiting_reason = None
        operation.ended_at = None
        operation.founder_report_json = None
        operation.lease_owner = None
        operation.lease_expires_at = None

        if project:
            project.status = "ACTIVE"
            project.current_state_summary = (
                f"{work.title} was reopened after Eason One reconciled an invalid "
                "repository-wide verification veto."
            )
            project.next_milestone = "Engineer will retry the same approved Work under task-scoped host acceptance."

        for management in [row for row in operation.works if row.work_type == "MANAGEMENT"]:
            if management.state == "ABANDONED":
                management.state = "EXECUTING"
                management.abandoned_at = None
                management.accepted_at = None
                management.cancelled_at = None
                work_runtime.sync_task_projection(management)

        emit(
            "WORK_SYSTEM_RECONCILED", actor_type="RUNTIME",
            project_id=work.project_id, work_id=work.id,
            correlation_id=f"work:{work.id}",
            payload={
                "reason": "UNRELATED_REPOSITORY_REGRESSION_VETO",
                "source_run_id": latest.id,
                "reopened_state": "READY",
                "new_provider_call": False,
            },
        )
        if project:
            emit(
                "PROJECT_RECOVERED", actor_type="RUNTIME",
                project_id=project.id, work_id=work.id,
                correlation_id=f"project:{project.id}",
                payload={"reason": "SYSTEM_VALIDATION_SCOPE_FAULT"},
            )
        recovered.append(work.id)

    if recovered:
        db.session.commit()
    return recovered




def _reconcile_superseded_codex_verification_gates() -> list[int]:
    """Remove Founder gates created only by Codex's WSL verification limits.

    Codex is a bounded implementation tool. Windows host validation is the
    deterministic acceptance authority for Engineering Work. If the latest
    successful Engineer execution already has authoritative host PASS evidence
    and Codex asked for Founder only because its WSL sandbox could not run the
    verification environment, the Founder gate is a platform-authority defect.

    No external call happens here and no new Codex attempt is created. The same
    durable successful execution is allowed to continue into Artifact acceptance.
    """
    reconciled: list[int] = []
    work_execution = __import__(
        "eason_one.services.work_execution",
        fromlist=["codex_founder_request_is_verification_only"],
    )
    for work in Work.query.filter_by(state="WAITING").order_by(Work.id).all():
        if work.work_type == "MANAGEMENT" or not is_work_vnext(work.operation):
            continue
        if not work_runtime.has_open_gate(work, "FOUNDER_DECISION"):
            continue
        latest = (
            AgentRun.query.filter_by(work_id=work.id, purpose="TASK_EXECUTION")
            .order_by(AgentRun.id.desc()).first()
        )
        if not latest or latest.status != "SUCCEEDED" or latest.provider_key_snapshot != "codex":
            continue
        codex = dict((latest.parsed_output_json or {}).get("codex") or {})
        if not work_execution.codex_founder_request_is_verification_only(latest, codex):
            continue

        resolved = work_runtime.resolve_waits(
            work, "FOUNDER_DECISION",
            note=(
                "Eason One reconciled an invalid Founder gate: Codex could not run "
                "verification inside WSL, while authoritative Windows host acceptance had already passed."
            ),
        )
        if not resolved:
            continue

        for escalation in Escalation.query.filter_by(
            work_id=work.id, state="OPEN", escalation_type="CODEX_RISK_APPROVAL"
        ).all():
            escalation.state = "RESOLVED"
            escalation.resolved_at = now()
            escalation.resolution = "SYSTEM_HOST_VERIFICATION_SUPERSEDED_CODEX_GATE"

        context = dict(latest.context_composition_json or {})
        context["codex_founder_gate_reconciled"] = {
            "at": now().isoformat(),
            "reason": codex.get("founder_reason"),
            "basis": "WINDOWS_HOST_ACCEPTANCE_PASSED",
        }
        latest.context_composition_json = context
        latest.resolution_status = latest.resolution_status or "SYSTEM_HOST_VERIFICATION_SUPERSEDED_CODEX_GATE"
        latest.resolution_note = latest.resolution_note or (
            "WSL verification limitation was internal; Windows host acceptance already passed."
        )

        operation = work.operation
        operation.status = "RUNNING"
        operation.kernel_status = "RUNNING"
        operation.current_stage = "WORK_CORE_V018"
        operation.waiting_reason = None
        if operation.project and operation.project.status == "BLOCKED":
            operation.project.status = "ACTIVE"
        if operation.project:
            operation.project.current_state_summary = (
                f"{work.title} is continuing from the already-successful Engineer execution; "
                "an internal WSL verification limitation did not require Founder authority."
            )
            operation.project.next_milestone = (
                "Persist the already-proven Artifact and continue CEO closure without another Codex call."
            )

        emit(
            "WORK_SYSTEM_RECONCILED", actor_type="RUNTIME",
            project_id=work.project_id, work_id=work.id,
            correlation_id=f"work:{work.id}",
            payload={
                "reason": "CODEX_WSL_VERIFICATION_GATE_SUPERSEDED_BY_HOST",
                "source_run_id": latest.id,
                "new_provider_call": False,
                "host_validation_success": True,
            },
        )
        reconciled.append(work.id)

    if reconciled:
        db.session.commit()
    return reconciled


def _eligible_work() -> Work | None:
    # Verification first so finished work does not sit behind new execution.
    candidates = Work.query.filter(Work.state.in_(["VERIFYING", "READY", "EXECUTING"])).order_by(Work.id).all()
    for work in candidates:
        if work.work_type == "MANAGEMENT":
            continue
        operation = work.operation
        if not is_work_vnext(operation):
            continue
        if not work.project or work.project.status not in _ACTIVE_PROJECT_STATES:
            continue
        if int(work_runtime.has_open_gate(work)):
            continue
        if work.state in {"READY", "EXECUTING"} and not work_runtime.dependencies_satisfied(work):
            continue
        if AgentRun.query.filter_by(work_id=work.id, status="RUNNING").count():
            continue
        return work
    return None


def _failed_delivery(operation: Operation) -> Work | None:
    return next((
        work for work in sorted(operation.works, key=lambda row: row.id)
        if work.work_type != "MANAGEMENT" and work.state == "ABANDONED"
    ), None)


def _finalize_failed_operation(operation: Operation, *, reason: str | None = None) -> dict:
    """End a Project truthfully when bounded internal recovery is exhausted."""
    failed = _failed_delivery(operation)
    ceo = operation.proposed_by
    if reason is None and failed is not None:
        last = AgentRun.query.filter_by(work_id=failed.id).order_by(AgentRun.id.desc()).first()
        reason = getattr(last, "error_text", None) or getattr(last, "failure_reason", None)
    reason = reason or "The company exhausted bounded internal recovery before the approved outcome could be verified."
    operation.status = "FAILED"
    operation.kernel_status = "FAILED"
    operation.current_stage = "FAILED"
    operation.waiting_reason = None
    operation.ended_at = operation.ended_at or now()
    operation.lease_owner = None
    operation.lease_expires_at = None
    for management in [work for work in operation.works if work.work_type == "MANAGEMENT"]:
        if management.state not in work_runtime.TERMINAL_WORK_STATES:
            work_runtime.transition(
                management, "ABANDONED", actor_type="RUNTIME",
                reason="Project closed after bounded delivery recovery was exhausted.",
            )
            work_runtime.sync_task_projection(management)
    if operation.project:
        operation.project.status = "FAILED"
        operation.project.current_state_summary = reason[:1200]
        operation.project.next_milestone = "Founder may revise scope or start a new Project after reviewing the failure evidence."
    operation.founder_report_json = {
        "headline": "CEO could not complete this Project.",
        "summary": reason[:1600],
        "result": "No verified completion was claimed. Bounded automatic recovery was exhausted.",
        "next_move": "Review the failure evidence; change authority/scope only if you want another attempt.",
        "cost_twd": str(_project_spent(operation.project_id)) if operation.project_id else "0",
        "authorized_twd": str(operation.approved_budget_twd),
        "verification": "NOT_SATISFIED",
    }
    content = reason[:1600] + "\n\nResult: No verified completion was claimed."
    if ceo and not WorkMessage.query.filter_by(
        project_id=operation.project_id, message_type="CEO_TO_FOUNDER", content=content
    ).first():
        db.session.add(WorkMessage(
            project_id=operation.project_id, sender_employee_id=ceo.id,
            message_type="CEO_TO_FOUNDER", content=content,
        ))
    if operation.project_id and not CompanyEvent.query.filter_by(
        event_type="PROJECT_FAILED", project_id=operation.project_id
    ).first():
        emit(
            "PROJECT_FAILED", actor_type="EMPLOYEE" if ceo else "RUNTIME",
            actor_id=getattr(ceo, "id", None), project_id=operation.project_id,
            work_id=getattr(failed, "id", None), correlation_id=f"project:{operation.project_id}",
            payload={"operation_id": operation.id, "reason": reason[:1200]},
        )
    db.session.commit()
    return {"status": "PROJECT_FAILED", "operation_id": operation.id, "work_id": getattr(failed, "id", None)}


def _delivery_closed(operation: Operation) -> bool:
    rows = [work for work in operation.works if work.work_type != "MANAGEMENT"]
    return bool(rows) and all(work.state in {"ACCEPTED", "CANCELLED"} for work in rows)


def _meeting_config(operation: Operation) -> dict:
    ops = __import__("eason_one.services.operations", fromlist=["default_meeting_config"])
    spec = ((operation.plan_json or {}).get("operation") or {})
    config = dict(spec.get("meeting_config") or ops.default_meeting_config(spec))
    defaults = ops.default_meeting_config(spec)
    for key, value in defaults.items():
        config.setdefault(key, value)
    return config


def _operation_meeting(operation: Operation) -> Meeting | None:
    return Meeting.query.filter_by(operation_id=operation.id).order_by(Meeting.id).first()


def _meeting_terminal(meeting: Meeting | None) -> bool:
    if meeting is None:
        return True
    kernel = __import__("eason_one.services.meeting_kernel", fromlist=["status", "TERMINAL"])
    return kernel.status(meeting) in kernel.TERMINAL


def _meeting_failed_for_internal_recovery(meeting: Meeting | None) -> bool:
    if not meeting or not _meeting_terminal(meeting):
        return False
    recovery = ((meeting.minutes_json or {}).get("recovery") or {})
    return recovery.get("kind") == "INTERNAL_REPLAN_REQUIRED"


def _meeting_runtime_retries(meeting: Meeting) -> int:
    return MeetingEvent.query.filter_by(meeting_id=meeting.id, event_type="MEETING_RUNTIME_RETRY").count()


def _advance_meeting_gate(operation: Operation) -> dict | None:
    """Advance at most one bounded Meeting action after delivery is accepted.

    Meetings are Company coordination, not a Founder-driven runner. Ordinary
    provider/local failures receive bounded automatic retry. Only an explicit
    router question may leave the room WAITING_FOR_FOUNDER.
    """
    meeting = _operation_meeting(operation)
    if meeting is None or not _delivery_closed(operation):
        return None
    meeting_service = __import__("eason_one.services.meetings", fromlist=[
        "start_auto", "skip", "next_step", "retry_paid_step", "close_for_internal_recovery"
    ])
    meeting_kernel = __import__("eason_one.services.meeting_kernel", fromlist=["status", "append_event", "TERMINAL"])
    status = meeting_kernel.status(meeting)
    if status in meeting_kernel.TERMINAL:
        return None

    config = _meeting_config(operation)
    trigger = str(config.get("trigger") or "ON_MATERIAL_CONFLICT").upper()
    if status in {"PLANNED", "READY"}:
        if trigger == "NEVER":
            meeting_service.skip(meeting, "Approved Meeting policy does not require a coordination call.")
            return {"status": "MEETING_SKIPPED", "meeting_id": meeting.id}
        if trigger == "ON_MATERIAL_CONFLICT":
            ops = __import__("eason_one.services.operations", fromlist=["_material_conflict_detected"])
            if not ops._material_conflict_detected(operation):
                meeting_service.skip(meeting, "Accepted specialist work contains no persisted material conflict.")
                return {"status": "MEETING_SKIPPED", "meeting_id": meeting.id}
        meeting_service.start_auto(meeting)
        return {"status": "MEETING_STARTED", "meeting_id": meeting.id}

    if meeting.status == "WAITING_FOR_FOUNDER":
        latest = __import__("eason_one.models", fromlist=["MeetingMessage"]).MeetingMessage.query.filter_by(
            meeting_id=meeting.id, message_type="FOUNDER_INTERVENTION"
        ).order_by(__import__("eason_one.models", fromlist=["MeetingMessage"]).MeetingMessage.id.desc()).first()
        waited_at = int((meeting.routing_json or {}).get("founder_message_id_at_wait") or 0)
        if latest and latest.id > waited_at:
            # The Founder decision itself is the authority event. Do not demand
            # a second Resume click after the answer has already been supplied.
            meeting_service.resume(meeting)
            return {"status": "MEETING_RESUMED", "meeting_id": meeting.id}
        return {"status": "NEEDS_FOUNDER", "meeting_id": meeting.id}

    if meeting.status == "PAUSED":
        retry_limit = max(0, int(config.get("retry_limit") or 1))
        retries = _meeting_runtime_retries(meeting)
        if retries >= retry_limit:
            reason = "Meeting exhausted bounded automatic recovery; no reliable coordination outcome was committed."
            meeting_service.close_for_internal_recovery(meeting, reason)
            return {"status": "MEETING_RECOVERY_EXHAUSTED", "meeting_id": meeting.id}
        meeting_kernel.append_event(
            meeting, "MEETING_RUNTIME_RETRY", actor_type="RUNTIME",
            payload={"attempt": retries + 1, "limit": retry_limit},
        )
        db.session.commit()
        if meeting.paid_failure_json:
            meeting_service.retry_paid_step(meeting, actor_type="RUNTIME")
        else:
            meeting_service.start_auto(meeting)
        return {"status": "MEETING_RETRY", "meeting_id": meeting.id, "attempt": retries + 1}

    if meeting.status == "RUNNING":
        try:
            meeting_service.next_step(meeting)
        except Exception as exc:
            # Meeting code persists PAUSED + failure evidence before raising.
            db.session.rollback()
            meeting = db.session.get(Meeting, meeting.id)
            if meeting and meeting.status not in {"PAUSED", "WAITING_FOR_FOUNDER", "ENDED", "TERMINATED_BY_FOUNDER"}:
                meeting_service.close_for_internal_recovery(
                    meeting, f"Meeting runtime failed without a recoverable checkpoint: {type(exc).__name__}: {exc}"
                )
            return {"status": "MEETING_STEP_FAILED", "meeting_id": getattr(meeting, "id", None)}
        return {"status": "MEETING_ADVANCED", "meeting_id": meeting.id}
    return {"status": "MEETING_WAITING", "meeting_id": meeting.id, "meeting_status": meeting.status}


def _project_spent(project_id: int) -> Decimal:
    from sqlalchemy import func
    return Decimal(
        db.session.query(func.coalesce(func.sum(CostEvent.real_cost_delta), 0))
        .filter(CostEvent.project_id == project_id).scalar() or 0
    )


def _closure_packet(operation: Operation) -> str:
    rows = []
    for work in sorted((row for row in operation.works if row.work_type != "MANAGEMENT"), key=lambda x: x.id):
        version = (
            ArtifactVersion.query.join(Artifact)
            .filter(Artifact.work_id == work.id, ArtifactVersion.status == "ACCEPTED")
            .order_by(ArtifactVersion.id.desc()).first()
        )
        run = (
            version.execution if version and version.execution else
            AgentRun.query.filter_by(work_id=work.id, status="SUCCEEDED")
            .order_by(AgentRun.id.desc()).first()
        )
        parsed = dict(getattr(run, "parsed_output_json", None) or {})
        codex = dict(parsed.get("codex") or {})
        host_validation = dict((getattr(run, "context_composition_json", None) or {}).get("host_validation") or {})
        rows.append({
            "work_id": work.id,
            "title": work.title,
            "purpose": work.purpose,
            "acceptance_criteria": work.acceptance_criteria,
            "state": work.state,
            "artifact": (version.content_text if version else None),
            "artifact_hash": (version.content_hash if version else None),
            "execution": ({
                "run_id": run.id,
                "provider": run.provider_key_snapshot,
                "changed_files": codex.get("changed_files") or [],
                "codex_tests": codex.get("tests") or [],
                "codex_acceptance": codex.get("acceptance") or [],
                "host_validation": host_validation,
            } if run else None),
        })
    criteria = list(((operation.plan_json or {}).get("operation") or {}).get("completion_criteria") or [])
    meeting = _operation_meeting(operation)
    meeting_evidence = None
    if meeting and _meeting_terminal(meeting):
        meeting_evidence = {
            "meeting_id": meeting.id,
            "status": meeting.status,
            "summary": meeting.current_summary_json or {},
            "minutes": meeting.minutes_json or {},
            "termination_reason": meeting.termination_reason,
        }
    packet = {
        "project": {
            "id": operation.project_id,
            "name": operation.project.name if operation.project else operation.title,
            "objective": operation.project.objective if operation.project else operation.objective,
        },
        "mission": {"id": operation.id, "title": operation.title, "objective": operation.objective},
        "completion_criteria": criteria,
        "accepted_work": rows,
        "meeting_evidence": meeting_evidence,
        "spent_twd": str(_project_spent(operation.project_id)),
    }
    return json.dumps(packet, ensure_ascii=False, separators=(",", ":"))


def _closure_exists(operation: Operation) -> bool:
    management = Work.query.filter_by(operation_id=operation.id, work_type="MANAGEMENT").first()
    if not management:
        return False
    return bool(
        ArtifactVersion.query.join(Artifact)
        .filter(
            Artifact.work_id == management.id,
            Artifact.artifact_type == "FOUNDER_REPORT",
            ArtifactVersion.status == "ACCEPTED",
        ).first()
    )


def _engineering_fast_close(operation: Operation) -> tuple[dict, dict, AgentRun | None] | None:
    """Close one verified Engineer delivery without another paid CEO call.

    The Engineer Work is already ACCEPTED only after bounded Codex + host
    verification. Formatting that evidence for the Founder is deterministic.
    """
    delivery = [work for work in operation.works if work.work_type != "MANAGEMENT"]
    if len(delivery) != 1 or delivery[0].state != "ACCEPTED":
        return None
    meeting = _operation_meeting(operation)
    if meeting and _meeting_terminal(meeting):
        minutes = meeting.minutes_json or {}
        # A real coordination Meeting may materially qualify the Engineer
        # result. Preserve that evidence by using the CEO closure synthesis.
        if int(minutes.get("provider_calls") or 0) > 0 or (minutes.get("founder_interventions") or []):
            return None
    assignment = work_runtime.active_assignment(delivery[0])
    if not assignment or not assignment.employee or assignment.employee.slug != "engineer":
        return None
    version = (
        ArtifactVersion.query.join(Artifact)
        .filter(Artifact.work_id == delivery[0].id, ArtifactVersion.status == "ACCEPTED")
        .order_by(ArtifactVersion.id.desc()).first()
    )
    if not version:
        return None
    run = version.execution or (
        AgentRun.query.filter_by(work_id=delivery[0].id, status="SUCCEEDED")
        .order_by(AgentRun.id.desc()).first()
    )
    parsed = dict(getattr(run, "parsed_output_json", None) or {})
    codex = dict(parsed.get("codex") or {})
    host_validation = dict((getattr(run, "context_composition_json", None) or {}).get("host_validation") or {})
    summary = (version.content_text or parsed.get("result_summary") or "Verified engineering work completed.").strip()
    findings = []
    files = [str(row) for row in (codex.get("changed_files") or [])]
    if files:
        findings.append("Changed: " + ", ".join(files[:12]))
    checks = [row for row in (codex.get("tests") or []) if isinstance(row, dict)]
    for row in checks[:6]:
        findings.append(f"Check: {row.get('command') or 'validation'} — {row.get('status') or 'recorded'}")
    risks = [str(row) for row in (codex.get("risks") or [])]
    report = {
        "executive_summary": summary[:1600],
        "result": summary[:4000],
        "key_findings": findings,
        "disagreements_or_risks": risks,
        "unresolved_questions": [],
        "founder_decisions_required": [],
        "recommended_next_actions": ["Review the delivered engineering result and continue only if a new goal is needed."],
    }
    criteria = list(((operation.plan_json or {}).get("operation") or {}).get("completion_criteria") or [])
    def _norm(value):
        return " ".join(str(value or "").casefold().split()).strip(" .")
    acceptance = {
        _norm(row.get("criterion")): row
        for row in (codex.get("acceptance") or []) if isinstance(row, dict)
    }
    host_criteria = {
        _norm(row.get("criterion")): row
        for row in (host_validation.get("criterion_results") or {}).values()
        if isinstance(row, dict) and row.get("criterion")
    }
    proofs = {}
    for criterion in criteria:
        key = _norm(criterion)
        codex_row = acceptance.get(key)
        host_row = host_criteria.get(key)
        if codex_row and codex_row.get("status") == "PASSED":
            proofs[key] = ("CODEX_PLUS_HOST", codex_row)
        elif host_row and host_row.get("status") == "PASSED":
            proofs[key] = ("HOST", host_row)
        else:
            # Do not fabricate Project-level certainty merely to save a CEO
            # synthesis call. Fast close requires persisted criterion evidence.
            return None
    verification = {
        "overall_status": "SATISFIED",
        "criteria": [{
            "criterion": str(criterion),
            "status": "SATISFIED",
            "evidence": [
                proofs[_norm(criterion)][1].get("evidence")
                or f"Accepted Work #{delivery[0].id} / ArtifactVersion #{version.id}"
            ],
            "reason": (
                "Windows host verification directly proved this approved criterion."
                if proofs[_norm(criterion)][0] == "HOST"
                else "Codex recorded criterion success and the bounded Engineer result was independently accepted on the host."
            ),
        } for criterion in criteria],
        "summary": "Verified Engineer delivery satisfies the approved bounded outcome.",
        "recommended_action": "Deliver the verified result to the Founder.",
    }
    return verification, report, run


_CLOSURE_REPORT_FAILURES = {
    "OUTPUT_TRUNCATED",
    "PROVIDER_INCOMPLETE",
    "STRUCTURED_OUTPUT_INVALID",
    "PROVIDER_ERROR",
}


def _accepted_delivery_closure_fallback(operation: Operation, *, reason: str) -> tuple[dict, dict, AgentRun | None] | None:
    """Compile Founder delivery truth from already-accepted Work.

    CEO narrative generation is a presentation step. Once every governed
    delivery Work owns an ACCEPTED Artifact, an incomplete CEO prose response
    cannot retroactively turn the delivered company outcome into FAILED. This
    fallback adds no new semantic claim: it reports the accepted Work/Artifact
    evidence already present in Company Truth.
    """
    delivery = [
        work for work in sorted(operation.works, key=lambda row: row.id)
        if work.work_type != "MANAGEMENT"
    ]
    if not delivery or any(work.state != "ACCEPTED" for work in delivery):
        return None

    accepted_rows = []
    source_run = None
    for work in delivery:
        version = (
            ArtifactVersion.query.join(Artifact)
            .filter(
                Artifact.work_id == work.id,
                ArtifactVersion.status == "ACCEPTED",
            )
            .order_by(ArtifactVersion.id.desc()).first()
        )
        if not version:
            return None
        run = version.execution or (
            AgentRun.query.filter_by(work_id=work.id, status="SUCCEEDED")
            .order_by(AgentRun.id.desc()).first()
        )
        source_run = source_run or run
        text = (version.content_text or "").strip()
        accepted_rows.append({
            "work": work,
            "version": version,
            "run": run,
            "summary": text[:1800] or f"Accepted ArtifactVersion #{version.id}",
        })

    criteria = list(((operation.plan_json or {}).get("operation") or {}).get("completion_criteria") or [])
    evidence_labels = [
        f"Accepted Work #{row['work'].id} / ArtifactVersion #{row['version'].id}"
        for row in accepted_rows
    ]
    verification = {
        "overall_status": "SATISFIED",
        "criteria": [{
            "criterion": str(criterion),
            "status": "SATISFIED",
            "evidence": list(evidence_labels),
            "reason": (
                "All Founder-approved delivery Work reached ACCEPTED with persisted Artifact evidence. "
                "CEO narrative generation is non-authoritative and cannot revoke those accepted outcomes."
            ),
        } for criterion in criteria],
        "summary": (
            f"{len(accepted_rows)} approved delivery Work item(s) are accepted with persisted artifacts. "
            "The CEO narrative response failed, so Eason One compiled this closure deterministically from accepted Company Truth."
        ),
        "recommended_action": "Present the accepted delivery evidence to the Founder for review.",
        "verification_mode": "ACCEPTED_WORK_DETERMINISTIC_FALLBACK",
    }
    result_lines = [
        f"{row['work'].title}: {row['summary']}" for row in accepted_rows
    ]
    report = {
        "executive_summary": (
            f"Delivery is ready for Founder review. {len(accepted_rows)} approved Work item(s) completed and were accepted. "
            "The CEO report generator was unavailable/truncated, but that reporting fault did not invalidate the accepted outcome."
        ),
        "result": "\n\n".join(result_lines)[:5000],
        "key_findings": evidence_labels,
        "disagreements_or_risks": [
            "CEO closure narrative fallback used because: " + str(reason or "closure report generation failed")[:700]
        ],
        "unresolved_questions": [],
        "founder_decisions_required": [],
        "recommended_next_actions": [
            "Review the accepted artifacts. Start a new Project only if additional scope is desired."
        ],
        "closure_mode": "DETERMINISTIC_FROM_ACCEPTED_WORK",
    }
    return verification, report, source_run


def _persist_founder_result(operation: Operation, management: Work, ceo, verification: dict, report: dict, source_run: AgentRun | None) -> dict:
    artifact = Artifact.query.filter_by(
        work_id=management.id, artifact_type="FOUNDER_REPORT"
    ).order_by(Artifact.id).first()
    if not artifact:
        artifact = Artifact(
            project_id=operation.project_id, work_id=management.id,
            artifact_type="FOUNDER_REPORT", title=f"Founder outcome: {operation.project.name}",
        )
        db.session.add(artifact); db.session.flush()
    content = json.dumps(report, ensure_ascii=False)
    version_no = (
        db.session.query(db.func.max(ArtifactVersion.version))
        .filter_by(artifact_id=artifact.id).scalar() or 0
    ) + 1
    version = ArtifactVersion(
        artifact_id=artifact.id, version=version_no, producer_employee_id=ceo.id,
        execution_id=getattr(source_run, "id", None), status="SUBMITTED",
        content_text=content,
        content_hash=hashlib.sha256((content + "\nLOCATION:").encode("utf-8")).hexdigest(),
    )
    db.session.add(version); db.session.flush()
    emit(
        "ARTIFACT_SUBMITTED", actor_type="EMPLOYEE", actor_id=ceo.id,
        project_id=operation.project_id, work_id=management.id,
        execution_id=getattr(source_run, "id", None), artifact_id=artifact.id,
        correlation_id=f"work:{management.id}",
        payload={"artifact_version_id": version.id, "version": version.version, "type": "FOUNDER_REPORT"},
    )
    artifacts.verify_and_accept(
        management, version, method="CEO_CLOSURE_VERIFICATION",
        verifier_employee_id=ceo.id, agent_run_id=getattr(source_run, "id", None),
        details={"verification": verification},
    )
    operation.founder_report_json = {
        "headline": "Boss, it's complete.",
        "summary": report["executive_summary"],
        "result": report["result"],
        "next_move": (report.get("recommended_next_actions") or ["Review the completed result."])[0],
        "cost_twd": str(_project_spent(operation.project_id)),
        "authorized_twd": str(operation.approved_budget_twd),
        "artifact_version_id": version.id,
        "verification": "SATISFIED",
    }
    _compat_complete(operation)
    operation.project.status = "REVIEW"
    operation.project.current_state_summary = report["executive_summary"]
    operation.project.next_milestone = "Founder review of the delivered outcome."
    if not WorkMessage.query.filter_by(
        project_id=operation.project_id, message_type="CEO_TO_FOUNDER",
        content=report["executive_summary"] + "\n\nResult: " + report["result"],
    ).first():
        db.session.add(WorkMessage(
            project_id=operation.project_id, sender_employee_id=ceo.id,
            agent_run_id=getattr(source_run, "id", None), message_type="CEO_TO_FOUNDER",
            content=report["executive_summary"] + "\n\nResult: " + report["result"],
        ))
    if not CompanyEvent.query.filter_by(
        event_type="PROJECT_OUTCOME_READY", project_id=operation.project_id, artifact_id=artifact.id
    ).first():
        emit(
            "PROJECT_OUTCOME_READY", actor_type="EMPLOYEE", actor_id=ceo.id,
            project_id=operation.project_id, work_id=management.id,
            execution_id=getattr(source_run, "id", None), artifact_id=artifact.id,
            correlation_id=f"project:{operation.project_id}",
            payload={"operation_id": operation.id, "verification": "SATISFIED"},
        )
    db.session.commit()
    return {
        "status": "RESULT_READY", "operation_id": operation.id,
        "run_id": getattr(source_run, "id", None), "artifact_version_id": version.id,
    }


def _persist_accepted_delivery_closure_fallback(
    operation: Operation, management: Work, *, reason: str, failed_run: AgentRun | None = None
) -> dict | None:
    fallback = _accepted_delivery_closure_fallback(operation, reason=reason)
    if fallback is None:
        return None
    verification, report, source_run = fallback
    if failed_run is not None and failed_run.resolution_status is None:
        failed_run.resolution_status = "SYSTEM_CLOSURE_REPORT_NON_AUTHORITATIVE"
        failed_run.resolution_note = (
            "Accepted delivery truth remained valid; CEO narrative generation could not revoke accepted Work/Artifact evidence."
        )
    result = _persist_founder_result(operation, management, operation.proposed_by, verification, report, source_run)
    operation.founder_report_json = {
        **(operation.founder_report_json or {}),
        "closure_mode": "DETERMINISTIC_FROM_ACCEPTED_WORK",
        "closure_reporting_fault": str(reason or "")[:1200],
    }
    emit(
        "CEO_CLOSURE_REPORT_FALLBACK", actor_type="RUNTIME",
        project_id=operation.project_id, work_id=management.id,
        execution_id=getattr(failed_run, "id", None),
        correlation_id=f"project:{operation.project_id}",
        payload={
            "reason": str(reason or "")[:1200],
            "accepted_delivery_count": len([w for w in operation.works if w.work_type != "MANAGEMENT" and w.state == "ACCEPTED"]),
            "new_provider_call": False,
        },
    )
    db.session.commit()
    return result


def _schedule_closure_recovery(operation: Operation, management: Work, run: AgentRun, reason: str) -> dict:
    if run.outcome == "FAILED_AMBIGUOUS":
        work_runtime.open_wait(management, "RECONCILIATION", reason)
        operation.project.current_state_summary = "CEO closure has an ambiguous external effect that cannot be replayed safely."
        db.session.commit()
        return {"status": "RECONCILIATION_REQUIRED", "operation_id": operation.id, "run_id": run.id}

    # A closure-report formatting/provider failure is not delivery authority.
    # If all governed delivery Work is already ACCEPTED, preserve that company
    # truth and deterministically present the accepted evidence instead of
    # spending more merely to regenerate prose.
    if _delivery_closed(operation) and run.failure_reason in _CLOSURE_REPORT_FAILURES:
        fallback = _persist_accepted_delivery_closure_fallback(
            operation, management, reason=reason, failed_run=run
        )
        if fallback is not None:
            return fallback

    attempts = AgentRun.query.filter_by(work_id=management.id, purpose="CEO_PROJECT_CLOSURE").count()
    max_attempts = max(1, int(management.retry_limit or 0) + 1)
    if attempts < max_attempts:
        delay = min(30, 5 * (2 ** max(0, attempts - 1)))
        retry_after = now() + __import__("datetime", fromlist=["timedelta"]).timedelta(seconds=delay)
        work_runtime.open_wait(management, "INTERNAL_RECOVERY", reason, retry_after=retry_after)
        operation.project.current_state_summary = "CEO is recovering the final outcome step automatically."
        db.session.commit()
        return {"status": "CLOSURE_RETRY_SCHEDULED", "operation_id": operation.id, "run_id": run.id}

    # Even after bounded narrative retries, accepted delivery remains accepted.
    # A reporting failure cannot call _finalize_failed_operation unless delivery
    # itself is not closed/accepted.
    if _delivery_closed(operation):
        fallback = _persist_accepted_delivery_closure_fallback(
            operation, management, reason=reason + " Bounded CEO closure retries were exhausted.", failed_run=run
        )
        if fallback is not None:
            return fallback
    return _finalize_failed_operation(operation, reason=reason + " Bounded CEO closure retries were exhausted.")


def _close_operation(operation: Operation) -> dict:
    if _closure_exists(operation):
        _compat_complete(operation)
        if operation.project and operation.project.status not in {"COMPLETED", "CANCELLED"}:
            operation.project.status = "REVIEW"
        db.session.commit()
        return {"status": "RESULT_READY", "operation_id": operation.id}

    management = work_runtime.ensure_management_work(operation)
    ceo = operation.proposed_by
    fast = _engineering_fast_close(operation)
    if fast:
        verification, report, source_run = fast
        return _persist_founder_result(operation, management, ceo, verification, report, source_run)
    packet = _closure_packet(operation)
    # A successful closure response is durable evidence. If the process died
    # after the provider returned but before the Founder artifact committed,
    # reuse that response instead of paying for the same CEO synthesis twice.
    run = (
        AgentRun.query.filter_by(
            work_id=management.id, purpose="CEO_PROJECT_CLOSURE", status="SUCCEEDED"
        ).order_by(AgentRun.id.desc()).first()
    )
    if run is None:
        run = execute(
            ceo,
            "CEO_PROJECT_CLOSURE",
            f"Verify and report the approved outcome for Project #{operation.project_id}.",
            project=operation.project,
            operation=operation,
            work=management,
            context_override="COMPANY OUTCOME EVIDENCE\n" + packet,
            context_composition={"source": "accepted_work_artifacts", "chars": len(packet)},
            system_prompt_override=(
                ceo.system_instructions
                + "\nCEO_PROJECT_CLOSURE\nUse only the supplied durable evidence. In one call, "
                  "(1) verify every completion criterion and (2) produce the Founder-ready result. "
                  "Return strict JSON. Never mark SATISFIED without direct evidence."
            ),
            response_schema=CEO_CLOSURE_SCHEMA,
            max_output_tokens_override=min(1400, ceo.current_model.max_output_tokens),
            prompt_version="ceo-project-closure-v1",
        )
    if run.status != "SUCCEEDED":
        reason = run.error_text or run.failure_reason or "CEO closure execution failed."
        if run.failure_reason in {"FOUNDER_BUDGET_EXTENSION_REQUIRED", "AUTHORITY_BLOCKED"}:
            work_runtime.open_wait(management, "FOUNDER_DECISION", reason)
            if not Escalation.query.filter_by(work_id=management.id, state="OPEN", escalation_type="BUDGET_AUTHORIZATION").first():
                __import__("eason_one.services.escalations", fromlist=["open_escalation"]).open_escalation(
                    project_id=operation.project_id, work_id=management.id,
                    operation_id=operation.id, escalation_type="BUDGET_AUTHORIZATION",
                    reason=reason, created_by_employee_id=ceo.id,
                )
        else:
            return _schedule_closure_recovery(operation, management, run, reason)
        operation.project.current_state_summary = "CEO needs additional Founder authority for the final outcome step."
        db.session.commit()
        return {"status": "NEEDS_FOUNDER", "operation_id": operation.id, "run_id": run.id}

    try:
        payload = json.loads(run.raw_output or "{}")
        verification = payload["verification"]
        report = payload["report"]
        if verification.get("overall_status") not in {"SATISFIED", "NOT_SATISFIED", "INSUFFICIENT_EVIDENCE"}:
            raise ValueError("Invalid closure verification status")
        run.parsed_output_json = payload
        run.structured_validation_status = "PASSED"
        run.structured_validation_errors_json = []
    except Exception as exc:
        run.status = "FAILED"
        run.outcome = "FAILED_KNOWN"
        run.failure_reason = "STRUCTURED_OUTPUT_INVALID"
        run.failure_stage = "POSTPROCESS"
        run.error_text = f"CEO closure validation failed: {exc}"
        db.session.commit()
        return _schedule_closure_recovery(operation, management, run, run.error_text)

    if verification["overall_status"] != "SATISFIED":
        # This is an internal company gap, not automatically a Founder gate.
        management.state = "EXECUTING"
        operation.project.current_state_summary = verification.get("summary") or "CEO found an outcome gap."
        db.session.commit()
        return _finalize_failed_operation(
            operation,
            reason=(verification.get("summary") or "CEO verification found that the approved outcome is not satisfied.")
            + " " + (verification.get("recommended_action") or "A new bounded Project/Work plan is required."),
        )

    return _persist_founder_result(operation, management, ceo, verification, report, run)



def _reconcile_closure_reporting_faults() -> list[int]:
    """Recover Projects incorrectly failed by non-authoritative CEO prose faults.

    This is intentionally conservative: every delivery Work must already be
    ACCEPTED with an accepted Artifact, and the latest CEO closure failure must
    be a known report-generation failure. No Provider/Codex call is made.
    """
    recovered: list[int] = []
    for operation in Operation.query.filter_by(status="FAILED").order_by(Operation.id).all():
        if not is_work_vnext(operation) or not _delivery_closed(operation):
            continue
        delivery = [w for w in operation.works if w.work_type != "MANAGEMENT"]
        if not delivery or any(w.state != "ACCEPTED" for w in delivery):
            continue
        management = Work.query.filter_by(operation_id=operation.id, work_type="MANAGEMENT").first()
        if not management:
            continue
        failed_run = (
            AgentRun.query.filter_by(work_id=management.id, purpose="CEO_PROJECT_CLOSURE")
            .order_by(AgentRun.id.desc()).first()
        )
        if not failed_run or failed_run.failure_reason not in _CLOSURE_REPORT_FAILURES:
            continue
        prepared = _accepted_delivery_closure_fallback(
            operation, reason=failed_run.error_text or failed_run.failure_reason or "CEO closure report generation failed."
        )
        if prepared is None:
            continue

        if management.state == "ABANDONED":
            management.state = "EXECUTING"
            management.abandoned_at = None
            management.accepted_at = None
            management.cancelled_at = None
            work_runtime.sync_task_projection(management)
        operation.status = "RUNNING"
        operation.kernel_status = "RUNNING"
        operation.current_stage = "WORK_CORE_V018"
        operation.waiting_reason = None
        operation.ended_at = None
        operation.lease_owner = None
        operation.lease_expires_at = None
        if operation.project:
            operation.project.status = "ACTIVE"
            operation.project.current_state_summary = (
                "Accepted delivery remains valid. Eason One is recovering a non-authoritative CEO closure-report failure without new provider work."
            )
            operation.project.next_milestone = "Compile Founder delivery evidence from already-accepted Work and Artifacts."

        for row in AgentRun.query.filter_by(work_id=management.id, purpose="CEO_PROJECT_CLOSURE").all():
            if row.status == "FAILED" and row.failure_reason in _CLOSURE_REPORT_FAILURES and row.resolution_status is None:
                row.resolution_status = "SYSTEM_CLOSURE_REPORT_NON_AUTHORITATIVE"
                row.resolution_note = "Accepted delivery truth was preserved; CEO report generation was presentation-only."

        emit(
            "PROJECT_RECOVERED", actor_type="RUNTIME",
            project_id=operation.project_id, work_id=management.id,
            execution_id=failed_run.id, correlation_id=f"project:{operation.project_id}",
            payload={
                "reason": "CEO_CLOSURE_REPORT_NON_AUTHORITATIVE",
                "new_provider_call": False,
                "accepted_delivery_count": len(delivery),
            },
        )
        db.session.flush()
        verification, report, source_run = prepared
        _persist_founder_result(operation, management, operation.proposed_by, verification, report, source_run)
        operation.founder_report_json = {
            **(operation.founder_report_json or {}),
            "closure_mode": "DETERMINISTIC_FROM_ACCEPTED_WORK",
            "closure_reporting_fault": (failed_run.error_text or failed_run.failure_reason or "")[:1200],
        }
        db.session.commit()
        recovered.append(operation.id)
    return recovered


def _next_failed_operation() -> Operation | None:
    for operation in Operation.query.filter(Operation.approved_at.isnot(None)).order_by(Operation.id).all():
        if not is_work_vnext(operation) or operation.status in {"COMPLETED", "FAILED", "CANCELLED", "TERMINATED_BY_FOUNDER"}:
            continue
        if _failed_delivery(operation):
            return operation
    return None


def _next_meeting_failed_operation() -> Operation | None:
    for operation in Operation.query.filter(Operation.approved_at.isnot(None)).order_by(Operation.id).all():
        if not is_work_vnext(operation) or operation.status in {"COMPLETED", "FAILED", "CANCELLED", "TERMINATED_BY_FOUNDER"}:
            continue
        if _meeting_failed_for_internal_recovery(_operation_meeting(operation)):
            return operation
    return None


def _next_closable_operation() -> Operation | None:
    rows = Operation.query.filter(Operation.approved_at.isnot(None)).order_by(Operation.id).all()
    for operation in rows:
        if not is_work_vnext(operation):
            continue
        if operation.status == "COMPLETED":
            continue
        if not operation.project or operation.project.status not in _ACTIVE_PROJECT_STATES:
            continue
        if _delivery_closed(operation):
            meeting = _operation_meeting(operation)
            if meeting is not None and not _meeting_terminal(meeting):
                continue
            if _meeting_failed_for_internal_recovery(meeting):
                continue
            management = Work.query.filter_by(operation_id=operation.id, work_type="MANAGEMENT").first()
            if management and int(work_runtime.has_open_gate(management)):
                continue
            return operation
    return None


def _record_runtime_exception(work_id: int, error: Exception) -> dict:
    """Turn a runtime bug into durable bounded recovery instead of a hot loop."""
    db.session.rollback()
    work = db.session.get(Work, work_id)
    if not work or work.state in work_runtime.TERMINAL_WORK_STATES:
        return {"status": "RUNTIME_EXCEPTION", "work_id": work_id}

    # If process-local execution was left RUNNING, use effect truth to decide
    # whether replay is safe before scheduling anything else.
    live = AgentRun.query.filter_by(work_id=work.id, status="RUNNING").order_by(AgentRun.id.desc()).first()
    if live:
        _recover_stale_work()
        db.session.commit()
        return {"status": "RECOVERY_CLASSIFIED", "work_id": work.id, "run_id": live.id}

    prior = CompanyEvent.query.filter_by(event_type="WORK_RUNTIME_EXCEPTION", work_id=work.id).count()
    delay = [5, 15, 30][min(prior, 2)]
    retry_after = now() + __import__("datetime", fromlist=["timedelta"]).timedelta(seconds=delay) if prior < 3 else None
    reason = f"Company Runtime error while advancing Work #{work.id}: {type(error).__name__}: {error}"
    if retry_after is not None:
        work_runtime.open_wait(work, "INTERNAL_RECOVERY", reason, retry_after=retry_after)
    else:
        work_runtime.transition(work, "ABANDONED", actor_type="RUNTIME", reason=reason + " Bounded runtime recovery was exhausted.")
    emit(
        "WORK_RUNTIME_EXCEPTION", actor_type="RUNTIME", project_id=work.project_id,
        work_id=work.id, correlation_id=f"work:{work.id}",
        payload={"error_type": type(error).__name__, "error": str(error)[:1200], "attempt": prior + 1,
                 "retry_scheduled": retry_after is not None},
    )
    if work.project:
        work.project.current_state_summary = (
            "CEO is recovering an internal execution fault automatically."
            if retry_after else "Bounded runtime recovery was exhausted; the Project will close as failed without claiming completion."
        )
    db.session.commit()
    return {"status": "RETRY_SCHEDULED" if retry_after else "FAILED_INTERNAL", "work_id": work.id}


def _health_update(**values) -> None:
    with _HEALTH_LOCK:
        _HEALTH.update(values)


def runtime_health() -> dict:
    with _HEALTH_LOCK:
        data = dict(_HEALTH)
    thread = _THREAD
    data["thread_alive"] = bool(thread and thread.is_alive())
    return data


def _runtime_error(app, where: str, error: Exception) -> None:
    """Make scheduler failures observable without turning them into Founder work."""
    try:
        db.session.rollback()
    except Exception:
        pass
    message = f"{where}: {type(error).__name__}: {error}"
    _health_update(
        last_tick_at=now().isoformat(),
        last_error=message[:2000],
        consecutive_errors=int(runtime_health().get("consecutive_errors") or 0) + 1,
    )
    try:
        app.logger.error("Company Runtime %s\n%s", message, traceback.format_exc())
    except Exception:
        print(f"[company-runtime] {message}", flush=True)
        traceback.print_exc()


def _maintenance(app, name: str, fn) -> int:
    """Run isolated maintenance without letting return-shape differences fault the runtime.

    Maintenance helpers report work in a few truthful shapes: integer counts,
    migration dictionaries, or identifier collections (for example startup
    adoption returns a list of adopted Operation ids).  The scheduler only
    needs a count for observability, so normalize those shapes here instead of
    forcing every helper through ``int(...)``.
    """
    try:
        value = fn()
        if isinstance(value, dict):
            return int(bool(value.get("migrated")))
        if isinstance(value, (list, tuple, set, frozenset)):
            return len(value)
        if value is None:
            return 0
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, (int, float)):
            return int(value)
        # A new maintenance helper must choose one of the supported shapes.
        # Treat an unexpected return as an implementation fault rather than
        # silently manufacturing scheduler activity.
        raise TypeError(
            f"Unsupported maintenance result from {name}: {type(value).__name__}"
        )
    except Exception as exc:
        _runtime_error(app, f"maintenance:{name}", exc)
        try:
            db.session.remove()
        except Exception:
            pass
        return 0


def _dispatch_once() -> dict | None:
    """Advance one eligible Work before optional compatibility maintenance."""
    work = _eligible_work()
    if not work:
        return None
    work_id = work.id
    try:
        result = (
            __import__("eason_one.services.work_execution", fromlist=["review_work"]).review_work(work)
            if work.state == "VERIFYING"
            else __import__("eason_one.services.work_execution", fromlist=["execute_work"]).execute_work(work)
        )
    except Exception as exc:
        result = _record_runtime_exception(work_id, exc)
    project = db.session.get(Project, work.project_id)
    if project and result.get("status") == "ACCEPTED":
        project.current_state_summary = f"{work.title} completed. CEO is continuing the Project."
        db.session.commit()
    return {"kind": "WORK", **result}


def tick() -> dict:
    # Fresh approved Work gets first claim on the runtime. Compatibility repair
    # is deliberately not allowed to starve dispatch. Retry/dependency waits are
    # resolved in the loop as isolated maintenance before this function runs.
    dispatched = _dispatch_once()
    if dispatched is not None:
        return dispatched
    # Accepted delivery may require a bounded company Meeting before closure.
    for candidate in Operation.query.filter(Operation.approved_at.isnot(None)).order_by(Operation.id).all():
        if not is_work_vnext(candidate) or candidate.status in {"COMPLETED", "FAILED", "CANCELLED", "TERMINATED_BY_FOUNDER"}:
            continue
        if not _delivery_closed(candidate):
            continue
        meeting_result = _advance_meeting_gate(candidate)
        if meeting_result is not None:
            if meeting_result.get("status") == "NEEDS_FOUNDER" and candidate.project:
                candidate.project.current_state_summary = "CEO needs a specific Founder decision from the Project Meeting."
                db.session.commit()
            return {"kind": "MEETING", "operation_id": candidate.id, **meeting_result}
    failed_operation = _next_failed_operation()
    if failed_operation:
        return {"kind": "FAILURE", **_finalize_failed_operation(failed_operation)}
    meeting_failed = _next_meeting_failed_operation()
    if meeting_failed:
        return {"kind": "FAILURE", **_finalize_failed_operation(
            meeting_failed, reason="The required Project Meeting exhausted bounded internal recovery; completion was not claimed."
        )}
    operation = _next_closable_operation()
    if operation:
        try:
            return {"kind": "CLOSURE", **_close_operation(operation)}
        except Exception as exc:
            management = work_runtime.ensure_management_work(operation)
            result = _record_runtime_exception(management.id, exc)
            return {"kind": "CLOSURE", **result}
    return {"kind": "IDLE"}


def _loop(app) -> None:
    _health_update(
        thread_alive=True, started_at=now().isoformat(), last_error=None,
        consecutive_errors=0,
    )
    # Startup adoption/recovery is best-effort. A stale legacy row must never
    # prevent a newly approved Work from reaching an Employee.
    with app.app_context():
        _maintenance(app, "adopt-approved-v018", adopt_approved_operations)
        _maintenance(app, "reconcile-validation-scope", _reconcile_validation_scope_faults)
        _maintenance(app, "reconcile-codex-verification-gates", _reconcile_superseded_codex_verification_gates)
        _maintenance(app, "reconcile-closure-reporting", _reconcile_closure_reporting_faults)
        _maintenance(app, "recover-stale-v018", _recover_stale_work)
        try:
            db.session.remove()
        except Exception:
            pass

    while not _STOP.is_set():
        try:
            with app.app_context():
                # Only wait resolution that can make Work immediately schedulable
                # happens before dispatch, and each repair is isolated.
                _maintenance(app, "resolve-internal-waits", _resolve_internal_waits)
                _maintenance(app, "repair-orphaned-waits", _repair_orphaned_waiting_work)
                _maintenance(app, "reconcile-validation-scope", _reconcile_validation_scope_faults)
                _maintenance(app, "reconcile-codex-verification-gates", _reconcile_superseded_codex_verification_gates)
                _maintenance(app, "reconcile-closure-reporting", _reconcile_closure_reporting_faults)

                did_work = False
                for _ in range(4):
                    result = tick()
                    _health_update(
                        last_tick_at=now().isoformat(),
                        last_kind=result.get("kind"),
                        last_error=None,
                        consecutive_errors=0,
                    )
                    if result.get("kind") == "IDLE":
                        break
                    did_work = True

                # Only v0.18 recovery runs here. Retired v0.17 WaitCondition and
                # OperationKernel maintenance are intentionally outside the
                # governing lane and cannot wake historical Projects.
                if not did_work:
                    _maintenance(app, "recover-stale-v018", _recover_stale_work)
                db.session.remove()
        except Exception as exc:
            with app.app_context():
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
        _THREAD = threading.Thread(
            target=_loop, args=(app,), name="eason-one-company-runtime", daemon=True,
        )
        _THREAD.start()
        return True


def ensure_company_runtime(app) -> bool:
    """Ensure the process-owned scheduler exists; approval may call this safely."""
    thread = _THREAD
    started = False
    if not thread or not thread.is_alive():
        started = start_company_runtime(app)
    _WAKE.set()
    return started



def runtime_snapshot(operation: Operation) -> dict:
    works = [work for work in sorted(operation.works, key=lambda row: row.id)]
    delivery = [work for work in works if work.work_type != "MANAGEMENT"]
    active_run = (
        AgentRun.query.filter_by(operation_id=operation.id, status="RUNNING")
        .order_by(AgentRun.id.desc()).first()
    )
    active_work = db.session.get(Work, active_run.work_id) if active_run and active_run.work_id else None
    if not active_work:
        active_work = next((work for work in delivery if work.state in {"EXECUTING", "VERIFYING"}), None)
    return {
        "owner": "COMPANY_RUNTIME" if is_work_vnext(operation) else "LEGACY_OPERATION_RUNTIME",
        "work_vnext": is_work_vnext(operation),
        "operation_id": operation.id,
        "active": bool(active_run or active_work),
        "active_work": (
            {"id": active_work.id, "title": active_work.title, "state": active_work.state}
            if active_work else None
        ),
        "active_execution_id": getattr(active_run, "id", None),
        "work_counts": {
            state: sum(work.state == state for work in delivery)
            for state in ["READY", "EXECUTING", "WAITING", "VERIFYING", "ACCEPTED"]
        },
        "done": sum(work.state == "ACCEPTED" for work in delivery),
        "total": len(delivery),
    }


def has_founder_gate(operation: Operation | None) -> bool:
    if not is_work_vnext(operation):
        return False
    if Escalation.query.filter_by(operation_id=operation.id, state="OPEN").count():
        return True
    return any(
        gate.get("condition_type") in {"FOUNDER_DECISION", "BUDGET"}
        for work in operation.works
        for gate in work_runtime.open_gates(work)
    )


def pause_operation(operation: Operation, *, reason: str = "Founder paused company execution.") -> int:
    """Pause vNext Work without handing scheduling back to OperationKernel."""
    if not is_work_vnext(operation):
        raise ValueError("Operation is not owned by the Work-first runtime")
    paused = 0
    for work in list(operation.works):
        if work.work_type == "MANAGEMENT" or work.state in work_runtime.TERMINAL_WORK_STATES:
            continue
        if not work_runtime.has_open_gate(work, "FOUNDER_DECISION"):
            work_runtime.open_wait(work, "FOUNDER_DECISION", reason)
            paused += 1
    if operation.project and operation.project.status not in {"COMPLETED", "CANCELLED", "FAILED"}:
        operation.project.status = "BLOCKED"
        operation.project.current_state_summary = "Founder paused execution. No new Work will start until resumed."
    operation.waiting_reason = reason
    db.session.commit()
    return paused


def cancel_operation(operation: Operation, *, reason: str = "Founder cancelled the approved work.") -> int:
    """Cancel every nonterminal vNext Work and make cancellation durable."""
    if not is_work_vnext(operation):
        raise ValueError("Operation is not owned by the Work-first runtime")
    cancelled = 0
    for work in list(operation.works):
        if work.state in work_runtime.TERMINAL_WORK_STATES:
            continue
        work_runtime.transition(work, "CANCELLED", actor_type="FOUNDER", reason=reason)
        work_runtime.sync_task_projection(work)
        work_runtime.resolve_waits(work, note="Cancelled by Founder.")
        cancelled += 1
    for escalation in Escalation.query.filter_by(operation_id=operation.id, state="OPEN").all():
        escalation.state = "RESOLVED"
        escalation.resolved_at = now()
        escalation.resolution = "REJECTED"
    operation.status = "CANCELLED"
    operation.kernel_status = "CANCELLED"
    operation.current_stage = "CANCELLED"
    operation.waiting_reason = reason
    operation.ended_at = operation.ended_at or now()
    operation.lease_owner = None
    operation.lease_expires_at = None
    if operation.project and operation.project.status not in {"COMPLETED", "FAILED"}:
        operation.project.status = "CANCELLED"
        operation.project.current_state_summary = reason
    db.session.commit()
    return cancelled


def resume_after_founder(operation: Operation, *, resolution: str = "APPROVED") -> int:
    """Resolve only explicit Founder-owned Work gates, then wake Company Runtime."""
    if not is_work_vnext(operation):
        raise ValueError("Operation is not owned by the Work-first runtime")
    resolved = 0
    for work in list(operation.works):
        resolved += work_runtime.resolve_waits(
            work, "FOUNDER_DECISION", note=f"Founder decision: {resolution}."
        )
        resolved += work_runtime.resolve_waits(
            work, "BUDGET", note=f"Founder decision: {resolution}."
        )
    for escalation in Escalation.query.filter_by(operation_id=operation.id, state="OPEN").all():
        escalation.state = "RESOLVED"
        escalation.resolved_at = now()
        escalation.resolution = resolution
    if operation.project and operation.project.status == "BLOCKED":
        operation.project.status = "ACTIVE"
    operation.waiting_reason = None
    operation.founder_report_json = None
    _compat_running(operation)
    db.session.commit()
    wake_company_runtime()
    return resolved


def wake_company_runtime(app=None) -> None:
    if app is not None:
        ensure_company_runtime(app)
    else:
        _WAKE.set()


def stop_company_runtime(timeout: float = 2.0) -> None:
    global _THREAD
    _STOP.set()
    _WAKE.set()
    thread = _THREAD
    if thread and thread.is_alive():
        thread.join(timeout=timeout)
    _THREAD = None
