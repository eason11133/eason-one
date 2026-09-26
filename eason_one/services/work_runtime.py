"""Company Core v0.18 Work runtime.

Work is the authoritative organizational commitment.  Task remains a temporary
legacy adapter used by existing execution/review/UI paths during cutover.
"""
from __future__ import annotations

from decimal import Decimal
from datetime import datetime
from types import SimpleNamespace
from typing import Iterable

from ..extensions import db
from ..models import (
    Employee,
    Project,
    Task,
    WaitCondition,
    Work,
    WorkAssignment,
    WorkDependency,
    now,
)
from .company_events import correlation_for_work, emit

WORK_STATES = {
    "PROPOSED", "READY", "EXECUTING", "WAITING", "VERIFYING",
    "ACCEPTED", "ABANDONED", "CANCELLED",
}
TERMINAL_WORK_STATES = {"ACCEPTED", "ABANDONED", "CANCELLED"}
TERMINAL_PROJECT_STATES = {"COMPLETED", "CANCELLED"}

# Project lifecycle writers must not infer "ACTIVE" merely because the local
# wait they just handled disappeared. These Work-owned gates represent durable
# whole-Project blockers whose owning subsystem must resolve them first.
PROJECT_HARD_WAIT_TYPES = {
    # FOUNDER_DECISION is intentionally not globally hard here. Canonical
    # Governance distinguishes Project-scoped authority (global) from exact
    # execution exceptions (only that Work).
    "FOUNDER_PAUSE",
    "AUTHORITY_EXHAUSTED",
    "SYSTEM_RECOVERY",
    "RECONCILIATION",
}

# Every vNext Work wait must have a named owner and an exit policy. Historical
# WaitCondition labels remain readable, but new WORK_CORE_V018 code cannot add a
# generic/ownerless wait such as BUDGET or VERIFICATION and leave the company
# permanently WAITING.
VNEXT_WAIT_OWNERS = {
    "DEPENDENCY": {"owner": "RUNTIME_DEPENDENCY", "exit": "upstream Work ACCEPTED"},
    "INTERNAL_RECOVERY": {"owner": "RUNTIME_RETRY", "exit": "retry_after or bounded exhaustion"},
    "RETRY_BACKOFF": {"owner": "RUNTIME_RETRY", "exit": "retry_after or bounded exhaustion"},
    "EVIDENCE_REVIEW_RETRY": {"owner": "RUNTIME_EVIDENCE_REVIEW", "exit": "retry_after or evidence-review exhaustion"},
    "HOST_PROOF_RETRY": {"owner": "RUNTIME_HOST_PROOF", "exit": "retry_after or reconciliation"},
    "CAPABILITY_GAP": {"owner": "WORKFORCE", "exit": "existing staff reassignment or governed HiringRequest materialization"},
    "FOUNDER_DECISION": {"owner": "CANONICAL_GOVERNANCE", "exit": "Founder Decision / gate invalidation"},
    "FOUNDER_PAUSE": {"owner": "FOUNDER_CONTROL", "exit": "explicit Resume"},
    "AUTHORITY_EXHAUSTED": {"owner": "PROJECT_KERNEL", "exit": "zero-cost evidence closure or explicit Founder reconsideration/cancel"},
    "SYSTEM_RECOVERY": {"owner": "PLATFORM_RECOVERY", "exit": "proven protocol repair or operator/system repair"},
    "RECONCILIATION": {"owner": "PLATFORM_INTEGRITY", "exit": "durable ambiguity/integrity reconciliation"},
}

_ALLOWED_TRANSITIONS = {
    "PROPOSED": {"READY", "CANCELLED"},
    "READY": {"EXECUTING", "WAITING", "CANCELLED"},
    "EXECUTING": {"WAITING", "VERIFYING", "ACCEPTED", "ABANDONED", "CANCELLED"},
    "WAITING": {"READY", "EXECUTING", "VERIFYING", "ABANDONED", "CANCELLED"},
    "VERIFYING": {"EXECUTING", "WAITING", "ACCEPTED", "ABANDONED", "CANCELLED"},
    "ACCEPTED": set(),
    "ABANDONED": set(),
    "CANCELLED": set(),
}


def active_assignment(work: Work) -> WorkAssignment | None:
    return WorkAssignment.query.filter_by(work_id=work.id, ended_at=None).order_by(WorkAssignment.id.desc()).first()


def task_for_work(work: Work) -> Task | None:
    return Task.query.filter_by(work_id=work.id).order_by(Task.id).first()


def work_for_task(task: Task | None) -> Work | None:
    if not task:
        return None
    if task.work_id:
        return db.session.get(Work, task.work_id)
    return None


def _is_v018_work(work: Work | None) -> bool:
    if not work or not work.operation:
        return False
    return __import__(
        "eason_one.services.core_v018", fromlist=["is_v018_operation"]
    ).is_v018_operation(work.operation)


def _control(work: Work) -> dict:
    data = dict(work.runtime_control_json or {})
    data.setdefault("version", "WORK_CORE_V018")
    data.setdefault("gates", [])
    return data


def open_gates(work: Work, condition_type: str | None = None) -> list[dict]:
    """Return v0.18 Work-owned gates, or legacy waits for historical rows."""
    if _is_v018_work(work):
        rows = [dict(row) for row in _control(work).get("gates") or [] if row.get("state") == "OPEN"]
        if condition_type:
            rows = [row for row in rows if row.get("condition_type") == condition_type]
        return rows
    query = WaitCondition.query.filter_by(work_id=work.id, state="OPEN")
    if condition_type:
        query = query.filter_by(condition_type=condition_type)
    return [{
        "id": row.id, "condition_type": row.condition_type, "state": row.state,
        "reason": row.reason, "target_work_id": row.target_work_id,
        "retry_after": row.retry_after.isoformat() if row.retry_after else None,
        "resume_state": row.resume_state,
    } for row in query.order_by(WaitCondition.id).all()]


def primary_gate(work: Work):
    rows = open_gates(work)
    return SimpleNamespace(**rows[0]) if rows else None


def has_open_gate(work: Work, condition_type: str | None = None) -> bool:
    return bool(open_gates(work, condition_type))


def project_open_gates(project, condition_types: set[str] | None = None) -> list[dict]:
    """Return Work-owned open gates across one Project with Work provenance."""
    project_id = getattr(project, "id", project)
    if not project_id:
        return []
    rows: list[dict] = []
    for work in Work.query.filter_by(project_id=int(project_id)).order_by(Work.id).all():
        for gate in open_gates(work):
            kind = str(gate.get("condition_type") or "")
            if condition_types is not None and kind not in condition_types:
                continue
            rows.append({**dict(gate), "work_id": work.id, "work_type": work.work_type})
    return rows


def project_hard_blockers(project, *, include_founder: bool = True) -> list[dict]:
    """Return only blockers that lawfully freeze *all* Project execution.

    Multi-Employee Projects need two different wait scopes. A delivery Work may
    reconcile its own provider/effect history while unrelated authorized Work
    continues. Project-control recovery, rejected Project budget and an explicit
    Founder pause are genuinely global and therefore remain hard blockers.

    This scope distinction is intentionally derived from the owning Work rather
    than the wait label alone. Otherwise one Employee's local reconciliation
    silently turns into a company-wide scheduler mutex.
    """
    rows: list[dict] = []
    for row in project_open_gates(project, PROJECT_HARD_WAIT_TYPES):
        kind = str(row.get("condition_type") or "")
        work_type = str(row.get("work_type") or "")
        if kind == "FOUNDER_PAUSE":
            rows.append(row)
            continue
        if work_type == "MANAGEMENT" and kind in {
            "AUTHORITY_EXHAUSTED", "SYSTEM_RECOVERY", "RECONCILIATION",
        }:
            rows.append(row)
    if include_founder:
        gate = __import__(
            "eason_one.services.governance", fromlist=["project_blocking_gate"]
        ).project_blocking_gate(project)
        if gate is not None:
            rows.insert(0, {
                "condition_type": "FOUNDER_DECISION",
                "work_id": getattr(gate, "work_id", None),
                "escalation_id": gate.id,
                "reason": gate.reason,
                "state": "OPEN",
                "authority_scope": "PROJECT",
            })
    return rows


def project_is_terminal(project) -> bool:
    return str(getattr(project, "status", "") or "").upper() in TERMINAL_PROJECT_STATES


def project_execution_fenced(project) -> bool:
    status = str(getattr(project, "status", "") or "").upper()
    return status == "PAUSED" or status in TERMINAL_PROJECT_STATES


def _assert_project_work_mutable(work: Work, *, action: str) -> None:
    project = getattr(work, "project", None)
    if project is not None and project_is_terminal(project):
        raise ValueError(
            f"PROJECT_TERMINAL_WORK_MUTATION_FORBIDDEN:{action}:{str(project.status or '').upper()}"
        )


def project_can_activate(project) -> bool:
    # PAUSED and terminal lifecycle states are hard execution fences. Recovery
    # may still reconcile already-observed provider/cost truth elsewhere, but it
    # must never project a terminal Project back to ACTIVE or wake new Work.
    if project_execution_fenced(project):
        return False
    return not project_hard_blockers(project)


def retry_due(gate: dict, current=None) -> bool:
    value = gate.get("retry_after") if gate else None
    if not value:
        return False
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return False
    current = current or now()
    if parsed.tzinfo is None and getattr(current, "tzinfo", None) is not None:
        parsed = parsed.replace(tzinfo=current.tzinfo)
    return parsed <= current


_TASK_STATUS_BY_WORK_STATE = {
    "PROPOSED": "TODO",
    "READY": "ASSIGNED",
    "EXECUTING": "WORKING",
    "WAITING": "BLOCKED",
    "VERIFYING": "REVIEW",
    "ACCEPTED": "DONE",
    "ABANDONED": "FAILED",
    "CANCELLED": "CANCELLED",
}


def sync_task_projection(work: Work, task: Task | None = None) -> Task | None:
    """Project authoritative Work state into the temporary legacy Task adapter.

    During Company Core cutover, Work owns organizational execution truth.  Task
    still feeds older execution/review helpers, so it may mirror Work, but a stale
    Task status must never veto a valid Work transition or make the scheduler pick
    a different business state.  This helper intentionally performs a one-way
    projection and never changes Work.
    """
    task = task or task_for_work(work)
    if not task:
        return None
    desired = _TASK_STATUS_BY_WORK_STATE.get(work.state)
    if desired and task.status != desired:
        task.status = desired
        if desired == "DONE":
            task.completed_at = task.completed_at or now()
    return task


def delivery_works(operation) -> list[Work]:
    return [work for work in operation.works if work.work_type != "MANAGEMENT"]


def task_for_work_state(operation, *states: str) -> Task | None:
    """Return the first legacy Task adapter whose authoritative Work is in states."""
    wanted = set(states)
    for work in sorted(delivery_works(operation), key=lambda row: row.id):
        if work.state not in wanted:
            continue
        task = task_for_work(work)
        if task:
            sync_task_projection(work, task)
            return task
    return None


def dependency_blocked_tasks(operation) -> list[Task]:
    """Return READY/EXECUTING Work adapters blocked only by Work dependencies."""
    rows: list[Task] = []
    for work in sorted(delivery_works(operation), key=lambda row: row.id):
        if work.state not in {"READY", "EXECUTING"}:
            continue
        if dependencies_satisfied(work):
            continue
        task = task_for_work(work)
        if task:
            rows.append(task)
    return rows


def create_work(
    *,
    project_id: int,
    operation_id: int | None,
    title: str,
    purpose: str,
    owner_employee_id: int,
    created_by_employee_id: int | None,
    expected_output: str | None = None,
    acceptance_criteria: str | None = None,
    priority: str = "HIGH",
    work_type: str = "DELIVERY",
    resource_ceiling_twd=None,
    parent_work_id: int | None = None,
    state: str = "READY",
    reason: str | None = None,
) -> Work:
    if state not in WORK_STATES:
        raise ValueError(f"Unsupported Work state {state}")
    project = db.session.get(Project, project_id)
    if project is not None and project_is_terminal(project):
        raise ValueError(
            f"PROJECT_TERMINAL_WORK_CREATION_FORBIDDEN:{str(project.status or '').upper()}"
        )
    if not db.session.get(Employee, owner_employee_id):
        raise ValueError("Work owner Employee does not exist")
    work = Work(
        project_id=project_id,
        operation_id=operation_id,
        parent_work_id=parent_work_id,
        title=title[:180],
        purpose=purpose,
        expected_output=expected_output,
        acceptance_criteria=acceptance_criteria,
        state=state,
        work_type=work_type,
        priority=priority,
        created_by_employee_id=created_by_employee_id,
        resource_ceiling_twd=(None if resource_ceiling_twd is None else Decimal(str(resource_ceiling_twd))),
    )
    db.session.add(work)
    db.session.flush()
    assignment = WorkAssignment(
        work_id=work.id,
        employee_id=owner_employee_id,
        responsibility="OWNER",
        assigned_by_employee_id=created_by_employee_id,
        reason=reason or "Initial approved Work assignment.",
    )
    db.session.add(assignment)
    emit(
        "WORK_CREATED",
        actor_type="EMPLOYEE" if created_by_employee_id else "RUNTIME",
        actor_id=created_by_employee_id,
        project_id=project_id,
        work_id=work.id,
        correlation_id=correlation_for_work(work.id),
        payload={"title": work.title, "state": work.state, "work_type": work.work_type},
    )
    emit(
        "WORK_ASSIGNED",
        actor_type="EMPLOYEE" if created_by_employee_id else "RUNTIME",
        actor_id=created_by_employee_id,
        project_id=project_id,
        work_id=work.id,
        correlation_id=correlation_for_work(work.id),
        payload={"employee_id": owner_employee_id, "responsibility": "OWNER"},
    )
    return work


def ensure_management_work(operation) -> Work:
    """Return the durable Work that owns operation-level management Executions.

    CEO orchestration, replan, goal-verification and final reporting are real
    company work with cost/provenance.  They must not exist as ownerless AgentRuns.
    The management Work is excluded from delivery-readiness checks until final
    reporting, then accepted with the Founder report artifact.
    """
    existing = Work.query.filter_by(
        operation_id=operation.id, work_type="MANAGEMENT"
    ).order_by(Work.id).first()
    if existing:
        return existing
    if not operation.project_id:
        raise ValueError("Operation must have a Project before management Work can exist")
    owner_id = operation.proposed_by_employee_id
    return create_work(
        project_id=operation.project_id,
        operation_id=operation.id,
        title=f"Manage and close: {operation.title}",
        purpose=(
            "Organize the approved Work, make bounded management decisions, "
            "verify the Founder goal, and deliver the final company outcome."
        ),
        owner_employee_id=owner_id,
        created_by_employee_id=owner_id,
        expected_output="Founder-ready operation outcome and closure evidence",
        acceptance_criteria=(
            "Required delivery Work is accepted; Goal Verification is SATISFIED; "
            "the final Founder report is persisted."
        ),
        priority="HIGH",
        work_type="MANAGEMENT",
        resource_ceiling_twd=operation.approved_budget_twd,
        state="EXECUTING",
        reason="Runtime management Work for the Founder-approved Operation.",
    )


def _planned_expected_output(item: dict) -> str:
    """Compile the approved Task contract into a Founder-meaningful output label."""
    scope = dict(item.get("write_scope") or {}) if isinstance(item, dict) else {}
    paths = [str(value) for value in (scope.get("paths") or []) if str(value or "").strip()]
    if len(paths) == 1:
        return paths[0]
    if paths:
        return "Approved repository outputs: " + ", ".join(paths)
    capabilities = {str(value or "").upper() for value in (item.get("required_capabilities") or [])}
    if "SOFTWARE_ENGINEERING" in capabilities:
        connector = __import__(
            "eason_one.services.codex_connector", fromlist=["plan_task_is_read_only"]
        )
        if connector.plan_task_is_read_only(item):
            return "Read-only engineering analysis and verification evidence"
    return "Operation result"


def materialize_operation_works(operation, plan_data: dict) -> list[Work]:
    """Create vNext Work + legacy Task pairs exactly once for an approved Operation."""
    existing = Work.query.filter_by(operation_id=operation.id).order_by(Work.id).all()
    if existing:
        return existing
    project_id = operation.project_id
    if not project_id:
        raise ValueError("Operation must have a Project before Work materialization")
    works: list[Work] = []
    tasks: list[Task] = []
    for item in plan_data["tasks"]:
        expected_output = _planned_expected_output(item)
        work = create_work(
            project_id=project_id,
            operation_id=operation.id,
            title=item["title"],
            purpose=item["objective"],
            owner_employee_id=item["assignee_employee_id"],
            created_by_employee_id=operation.proposed_by_employee_id,
            expected_output=expected_output,
            acceptance_criteria="\n".join(item["acceptance_criteria"]),
            priority="HIGH",
            resource_ceiling_twd=operation.approved_budget_twd,
        )
        control = dict(work.runtime_control_json or {})
        control["staffing_requirements"] = {
            "version": "TEAM_FORMATION_V1",
            "required_capabilities": list(item.get("required_capabilities") or []),
            "declared_by": "CEO_OPERATION_PLAN",
        }
        if item.get("write_scope") is not None:
            connector = __import__(
                "eason_one.services.codex_connector", fromlist=["freeze_write_scope"]
            )
            control["codex_write_scope"] = connector.freeze_write_scope(
                item["write_scope"],
                source="APPROVED_OPERATION_PLAN",
                authority_ref=f"operation:{operation.id}:plan",
            )
            control["deliverable_targets"] = {
                "version": "DELIVERABLE_TARGETS_V1",
                "kind": "REPOSITORY_PATHS",
                "paths": list(item["write_scope"].get("paths") or []),
                "source": "APPROVED_OPERATION_PLAN",
            }
        work.runtime_control_json = control
        task = Task(
            project_id=project_id,
            operation_id=operation.id,
            work_id=work.id,
            title=item["title"],
            objective=item["objective"],
            status="ASSIGNED",
            priority="HIGH",
            created_by_employee_id=operation.proposed_by_employee_id,
            assigned_employee_id=item["assignee_employee_id"],
            reviewer_employee_id=item.get("reviewer_employee_id"),
            required_output=expected_output,
            acceptance_criteria="\n".join(item["acceptance_criteria"]),
        )
        db.session.add(task)
        db.session.flush()
        __import__(
            "eason_one.services.acceptance_contract", fromlist=["ensure_for_work"]
        ).ensure_for_work(work, task=task)
        assignee = db.session.get(Employee, item["assignee_employee_id"])
        if assignee and assignee.slug == "engineer":
            __import__(
                "eason_one.services.codex_connector", fromlist=["ensure_execution_boundary"]
            ).ensure_execution_boundary(work, task)
        works.append(work)
        tasks.append(task)
    return works


def reopen_abandoned(work: Work, target: str, *, reason: str, actor_type: str = "RUNTIME") -> Work:
    """Explicitly reopen terminal ABANDONED Work for a proven platform repair.

    Normal Work transitions deliberately make ABANDONED terminal. Recovery code
    may reopen it only through this auditable exception so terminal resurrection
    can never happen as a silent field assignment. CANCELLED/ACCEPTED Work are
    never eligible for this repair path.
    """
    _assert_project_work_mutable(work, action="REOPEN_ABANDONED")
    if work.state != "ABANDONED":
        raise ValueError("WORK_REOPEN_REQUIRES_ABANDONED_STATE")
    if target not in {"READY", "EXECUTING", "VERIFYING"}:
        raise ValueError("WORK_REOPEN_TARGET_INVALID")
    previous = work.state
    work.state = target
    work.abandoned_at = None
    work.accepted_at = None
    work.cancelled_at = None
    # A prior ABANDONED transition may have truthfully voided an unpaid EC
    # contract. Proven platform repair reopens Work through this function only,
    # so create a new immutable contract generation instead of mutating the
    # historical VOIDED record back to ACTIVE.
    __import__(
        "eason_one.services.market",
        fromlist=["reissue_voided_contract_after_system_reopen"],
    ).reissue_voided_contract_after_system_reopen(work, reason=reason)
    emit(
        "WORK_SYSTEM_REOPENED",
        actor_type=actor_type,
        project_id=work.project_id,
        work_id=work.id,
        correlation_id=correlation_for_work(work.id),
        payload={"from": previous, "to": target, "reason": reason},
    )
    if _is_v018_work(work):
        sync_task_projection(work)
    return work


def restore_durable_accepted(work: Work, *, reason: str, actor_type: str = "RUNTIME") -> Work:
    """Restore terminal ACCEPTED truth after restart proved the exact evidence.

    WAITING -> ACCEPTED is intentionally not a normal lifecycle transition.  A
    restart may use this audited exception only after its caller has revalidated
    the frozen acceptance Contract, the latest ACCEPTED ArtifactVersion, and an
    exact PASSED VerificationRecord.  This prevents stale WAITING projection
    from making already-paid/accepted Work executable again without weakening
    the ordinary state machine.
    """
    _assert_project_work_mutable(work, action="RESTORE_DURABLE_ACCEPTED")
    if work.state != "WAITING":
        raise ValueError("WORK_DURABLE_ACCEPTANCE_RESTORE_REQUIRES_WAITING")
    if not _is_v018_work(work):
        raise ValueError("WORK_DURABLE_ACCEPTANCE_RESTORE_REQUIRES_VNEXT")
    previous = work.state
    work.state = "ACCEPTED"
    if work.accepted_at is None:
        work.accepted_at = now()
    work.abandoned_at = None
    work.cancelled_at = None
    emit(
        "WORK_DURABLE_ACCEPTANCE_RESTORED",
        actor_type=actor_type,
        project_id=work.project_id,
        work_id=work.id,
        correlation_id=correlation_for_work(work.id),
        payload={"from": previous, "to": "ACCEPTED", "reason": reason},
    )
    sync_task_projection(work)
    return work


def transition(work: Work, target: str, *, actor_type: str = "RUNTIME", actor_id: int | None = None, reason: str | None = None) -> Work:
    if target not in WORK_STATES:
        raise ValueError(f"Unsupported Work state {target}")
    current = work.state
    if current == target:
        return work
    _assert_project_work_mutable(work, action=f"TRANSITION_{current}_TO_{target}")
    if target not in _ALLOWED_TRANSITIONS.get(current, set()):
        raise ValueError(f"Illegal Work transition {current} -> {target}")
    work.state = target
    if target == "ACCEPTED":
        work.accepted_at = now()
    elif target == "ABANDONED":
        work.abandoned_at = now()
    elif target == "CANCELLED":
        work.cancelled_at = now()
    if target in {"ABANDONED", "CANCELLED"}:
        # Economic authority follows Work truth. A terminal failed/cancelled Work
        # must not leave an ACTIVE internal-market contract that appears payable.
        # The market service itself safely no-ops on a historical DB whose
        # additive market tables have not been created yet; other failures are
        # intentionally not swallowed because ghost payable contracts are truth
        # corruption, not a cosmetic read-model problem.
        __import__(
            "eason_one.services.market", fromlist=["retire_unsettled_contract"]
        ).retire_unsettled_contract(
            work, reason=reason or f"Work transitioned to {target}", terminal_state=target
        )
    event_type = {
        "READY": "WORK_READY",
        "EXECUTING": "WORK_STARTED",
        "WAITING": "WORK_WAITING",
        "VERIFYING": "WORK_VERIFYING",
        "ACCEPTED": "WORK_ACCEPTED",
        "ABANDONED": "WORK_ABANDONED",
        "CANCELLED": "WORK_CANCELLED",
    }.get(target, "WORK_STATE_CHANGED")
    emit(
        event_type,
        actor_type=actor_type,
        actor_id=actor_id,
        project_id=work.project_id,
        work_id=work.id,
        correlation_id=correlation_for_work(work.id),
        payload={"from": current, "to": target, "reason": reason},
    )
    # Keep the legacy Task as a one-way compatibility projection. This never
    # reads Task state back into Work and therefore cannot influence scheduling.
    if _is_v018_work(work):
        sync_task_projection(work)
    return work


def open_wait(
    work: Work,
    condition_type: str,
    reason: str,
    *,
    target_work_id: int | None = None,
    retry_after=None,
    resume_state: str | None = None,
    gate_key: str | None = None,
    issue_code: str | None = None,
):
    """Open a durable Work gate.

    WORK_CORE_V018 stores the gate on Work itself. The WaitCondition table is
    retained only for historical/pre-v0.18 compatibility and is not governing
    for the new scheduler.
    """
    _assert_project_work_mutable(work, action=f"OPEN_WAIT_{condition_type}")
    if _is_v018_work(work):
        if condition_type not in VNEXT_WAIT_OWNERS:
            raise ValueError(f"VNEXT_WAIT_TYPE_HAS_NO_OWNER:{condition_type}")
        data = _control(work)
        gates = [dict(row) for row in data.get("gates") or []]
        if condition_type == "FOUNDER_DECISION" and gate_key:
            identity_field, identity_value = "gate_key", str(gate_key)
        else:
            if condition_type == "RECONCILIATION" and not issue_code:
                # Integrity incidents always need an exact durable identity even
                # when an older caller did not supply one explicitly.
                import hashlib
                normalized = " ".join(str(reason or "").split())
                issue_code = "INCIDENT_" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
            # issue_code is a cross-subsystem exact owner, not a RECONCILIATION-
            # only feature. Two INTERNAL_RECOVERY/SYSTEM_RECOVERY gates on the
            # same Work must coexist instead of overwriting each other.
            if issue_code:
                identity_field, identity_value = "issue_code", str(issue_code)
            else:
                identity_field, identity_value = None, None
        existing = next((
            row for row in gates
            if row.get("state") == "OPEN"
            and row.get("condition_type") == condition_type
            and (identity_field is None or row.get(identity_field) == identity_value)
        ), None)
        prior_state = work.state
        checkpoint = resume_state or (
            prior_state if prior_state in {"READY", "EXECUTING", "VERIFYING"} else None
        )
        if existing is None:
            sequence = int(data.get("next_gate_id") or 1)
            existing = {
                "id": f"work:{work.id}:gate:{sequence}",
                "condition_type": condition_type,
                "state": "OPEN",
                "reason": reason,
                "target_work_id": target_work_id,
                "retry_after": retry_after.isoformat() if hasattr(retry_after, "isoformat") else retry_after,
                "resume_state": checkpoint,
                "opened_at": now().isoformat(),
                "owner": VNEXT_WAIT_OWNERS[condition_type]["owner"],
                "exit_policy": VNEXT_WAIT_OWNERS[condition_type]["exit"],
            }
            if gate_key:
                existing["gate_key"] = str(gate_key)
            if issue_code:
                existing["issue_code"] = str(issue_code)
            gates.append(existing)
            data["next_gate_id"] = sequence + 1
        else:
            existing["reason"] = reason or existing.get("reason")
            if target_work_id is not None:
                existing["target_work_id"] = target_work_id
            if retry_after is not None:
                existing["retry_after"] = retry_after.isoformat() if hasattr(retry_after, "isoformat") else retry_after
            if resume_state and not existing.get("resume_state"):
                existing["resume_state"] = resume_state
            if gate_key:
                existing["gate_key"] = str(gate_key)
            if issue_code:
                existing["issue_code"] = str(issue_code)
        if work.state != "WAITING" and work.state not in TERMINAL_WORK_STATES:
            transition(work, "WAITING", reason=reason)
        data["gates"] = gates
        work.runtime_control_json = data
        return SimpleNamespace(**existing)

    existing = WaitCondition.query.filter_by(
        work_id=work.id, condition_type=condition_type, state="OPEN"
    ).first()
    if existing:
        existing.reason = reason or existing.reason
        if target_work_id is not None:
            existing.target_work_id = target_work_id
        if retry_after is not None:
            existing.retry_after = retry_after
        if resume_state and not existing.resume_state:
            existing.resume_state = resume_state
        return existing

    prior_state = work.state
    checkpoint = resume_state or (
        prior_state if prior_state in {"READY", "EXECUTING", "VERIFYING"} else None
    )
    if work.state != "WAITING" and work.state not in TERMINAL_WORK_STATES:
        transition(work, "WAITING", reason=reason)
    condition = WaitCondition(
        work_id=work.id,
        condition_type=condition_type,
        state="OPEN",
        target_work_id=target_work_id,
        reason=reason,
        resume_state=checkpoint,
        retry_after=retry_after,
    )
    db.session.add(condition)
    db.session.flush()
    return condition


def resolve_waits(
    work: Work, condition_type: str | None = None, *, note: str | None = None,
    gate_key: str | None = None, issue_code: str | None = None,
) -> int:
    if _is_v018_work(work):
        data = _control(work)
        gates = [dict(row) for row in data.get("gates") or []]
        resolved = []
        for row in gates:
            if row.get("state") != "OPEN":
                continue
            if condition_type and row.get("condition_type") != condition_type:
                continue
            if gate_key is not None and row.get("gate_key") != str(gate_key):
                continue
            if issue_code is not None and row.get("issue_code") != str(issue_code):
                continue
            # Generic compatibility resolution may clean historical unkeyed
            # waits, but can never clear a new exact gate owned by another
            # subsystem. Exact issue identity applies to every vNext wait type.
            if gate_key is None and condition_type == "FOUNDER_DECISION" and row.get("gate_key"):
                continue
            if issue_code is None and condition_type is not None and row.get("issue_code"):
                continue
            row["state"] = "RESOLVED"
            row["resolved_at"] = now().isoformat()
            row["resolution_note"] = note
            resolved.append(row)
        if not resolved:
            return 0
        data["gates"] = gates
        work.runtime_control_json = data
        remaining = [row for row in gates if row.get("state") == "OPEN"]
        if not remaining and work.state == "WAITING" and not project_is_terminal(getattr(work, "project", None)):
            checkpoints = {
                row.get("resume_state") for row in resolved
                if row.get("resume_state") in {"READY", "EXECUTING", "VERIFYING"}
            }
            if work.work_type == "MANAGEMENT":
                target = "EXECUTING"
            elif "VERIFYING" in checkpoints:
                target = "VERIFYING"
            elif "EXECUTING" in checkpoints:
                target = "EXECUTING"
            else:
                target = "READY"
            transition(work, target, reason=note or "Work gates resolved")
        return len(resolved)

    query = WaitCondition.query.filter_by(work_id=work.id, state="OPEN")
    if condition_type:
        query = query.filter_by(condition_type=condition_type)
    rows = query.all()
    for row in rows:
        row.state = "RESOLVED"
        row.resolved_at = now()
        row.resolution_note = note
    if rows:
        db.session.flush()
        remaining = WaitCondition.query.filter_by(work_id=work.id, state="OPEN").count()
        if remaining == 0 and work.state == "WAITING" and not project_is_terminal(getattr(work, "project", None)):
            checkpoints = {
                row.resume_state for row in rows
                if row.resume_state in {"READY", "EXECUTING", "VERIFYING"}
            }
            if work.work_type == "MANAGEMENT":
                target = "EXECUTING"
            elif "VERIFYING" in checkpoints:
                target = "VERIFYING"
            elif "EXECUTING" in checkpoints:
                target = "EXECUTING"
            else:
                target = "READY"
            transition(work, target, reason=note or "Wait conditions resolved")
    return len(rows)


def reassign(work: Work, employee_id: int, *, assigned_by_employee_id: int | None, reason: str) -> WorkAssignment:
    _assert_project_work_mutable(work, action="REASSIGN")
    employee = db.session.get(Employee, employee_id)
    if not employee or not employee.active:
        raise ValueError("Replacement Employee is not active")
    if work.work_type != "MANAGEMENT":
        capacity = __import__(
            "eason_one.services.ceo_operating", fromlist=["employee_capacity_view"]
        ).employee_capacity_view(employee)
        if capacity.get("assignment_conflict") or (
            capacity.get("active_project_id") is not None
            and int(capacity["active_project_id"]) != int(work.project_id)
        ):
            raise ValueError(
                f"EMPLOYEE_ACTIVE_PROJECT_CONFLICT:EMP-{employee.id}:PROJECT-{capacity.get('active_project_id')}"
            )

    # Reassignment is company authority, not a loophole around the approved Work
    # capability contract.  Enforce only explicit governed capability truth here;
    # legacy Works without a declaration keep compatibility until management
    # replans them through Team Formation.
    team = __import__(
        "eason_one.services.team_formation",
        fromlist=["declared_capabilities", "employee_capabilities", "CANONICAL_DELIVERY_CAPABILITIES"],
    )
    declared = team.declared_capabilities(work)
    if len(declared) > 1:
        raise ValueError("WORK_REASSIGNMENT_REQUIRES_SINGLE_GOVERNED_CAPABILITY")
    if len(declared) == 1:
        capability = declared[0]
        if capability not in team.CANONICAL_DELIVERY_CAPABILITIES:
            raise ValueError("WORK_REASSIGNMENT_UNKNOWN_GOVERNED_CAPABILITY")
        if capability not in team.employee_capabilities(employee):
            raise ValueError(
                f"WORK_REASSIGNMENT_CAPABILITY_MISMATCH:{capability}:EMP-{employee.id}"
            )

    previous = active_assignment(work)
    if previous and previous.employee_id == employee_id:
        return previous
    if previous:
        previous.ended_at = now()
    assignment = WorkAssignment(
        work_id=work.id,
        employee_id=employee_id,
        responsibility="OWNER",
        assigned_by_employee_id=assigned_by_employee_id,
        reason=reason,
    )
    db.session.add(assignment)
    task = task_for_work(work)
    if task:
        task.assigned_employee_id = employee_id

    # If an internal labor contract already exists, rotate the unpaid economic
    # authority to the new legitimate assignee.  Initial Team Formation awards
    # before this call, so a seller that already equals the replacement is an
    # idempotent no-op rather than a duplicate market generation.
    __import__(
        "eason_one.services.market", fromlist=["rotate_contract_for_reassignment"]
    ).rotate_contract_for_reassignment(
        work,
        previous_employee_id=getattr(previous, "employee_id", None),
        new_employee_id=employee_id,
        reason=reason,
        assigned_by_employee_id=assigned_by_employee_id,
    )
    emit(
        "WORK_REASSIGNED",
        actor_type="EMPLOYEE" if assigned_by_employee_id else "RUNTIME",
        actor_id=assigned_by_employee_id,
        project_id=work.project_id,
        work_id=work.id,
        correlation_id=correlation_for_work(work.id),
        payload={"from_employee_id": getattr(previous, "employee_id", None), "to_employee_id": employee_id, "reason": reason},
    )
    return assignment


def set_dependencies_from_task_plan(operation, plan_rows: Iterable[dict]) -> None:
    task_by_id = {task.id: task for task in operation.tasks}
    for row in plan_rows:
        task = task_by_id.get(int(row["task_id"]))
        if not task or not task.work_id:
            continue
        WorkDependency.query.filter_by(work_id=task.work_id).delete(synchronize_session=False)
        for dependency_task_id in row.get("depends_on_task_ids") or []:
            dependency_task = task_by_id.get(int(dependency_task_id))
            if dependency_task and dependency_task.work_id:
                db.session.add(WorkDependency(work_id=task.work_id, depends_on_work_id=dependency_task.work_id))


def dependencies_satisfied(work: Work) -> bool:
    edges = WorkDependency.query.filter_by(work_id=work.id).all()
    for edge in edges:
        upstream=db.session.get(Work,edge.depends_on_work_id)
        if not upstream or upstream.state!="ACCEPTED":
            return False
    return True


def backfill_legacy_work_spine() -> int:
    """Idempotently import legacy Tasks into the vNext Work spine.

    This is compatibility migration, not a claim that historical Task state was
    perfect.  It preserves the best known organizational state and links prior
    AgentRun/CostEvent evidence to the imported Work so future analysis has one
    durable parent.
    """
    from ..models import AgentRun, CostEvent

    mapping={
        "TODO":"READY",
        "ASSIGNED":"READY",
        "WORKING":"EXECUTING",
        "REVIEW":"VERIFYING",
        "DONE":"ACCEPTED",
        "BLOCKED":"WAITING",
        "FAILED":"WAITING",
        "CANCELLED":"CANCELLED",
    }
    imported=0
    for task in Task.query.filter(Task.work_id.is_(None)).order_by(Task.id).all():
        state=mapping.get(task.status,"WAITING")
        work=Work(
            project_id=task.project_id,
            operation_id=task.operation_id,
            title=task.title,
            purpose=task.objective,
            expected_output=task.required_output,
            acceptance_criteria=task.acceptance_criteria,
            state=state,
            work_type="DELIVERY",
            priority=task.priority or "MEDIUM",
            created_by_employee_id=task.created_by_employee_id,
            resource_ceiling_twd=task.budget_credits,
            accepted_at=task.completed_at if state=="ACCEPTED" else None,
            cancelled_at=task.completed_at if state=="CANCELLED" else None,
        )
        db.session.add(work); db.session.flush()
        task.work_id=work.id
        if task.assigned_employee_id:
            db.session.add(WorkAssignment(
                work_id=work.id,employee_id=task.assigned_employee_id,
                responsibility="OWNER",assigned_by_employee_id=task.created_by_employee_id,
                reason="Imported from legacy Task assignment during Company Core v0.18 migration.",
            ))
        if state=="WAITING":
            db.session.add(WaitCondition(
                work_id=work.id,condition_type="LEGACY_STATE",state="OPEN",
                reason=f"Imported legacy Task state {task.status}; management/reconciliation must establish the next valid action.",
                resume_state="READY",
            ))
        emit(
            "WORK_IMPORTED",actor_type="SYSTEM",project_id=task.project_id,work_id=work.id,
            correlation_id=correlation_for_work(work.id),
            payload={"legacy_task_id":task.id,"legacy_status":task.status,"state":state},
        )
        AgentRun.query.filter_by(task_id=task.id,work_id=None).update({"work_id":work.id},synchronize_session=False)
        CostEvent.query.filter_by(task_id=task.id,work_id=None).update({"work_id":work.id},synchronize_session=False)
        imported+=1
    db.session.commit()

    # Reconstruct dependency edges for already-planned active Operations.
    from ..models import Operation
    for operation in Operation.query.order_by(Operation.id).all():
        plan=dict(operation.memory_json or {}).get("multi_agent_orchestration") or {}
        rows=plan.get("tasks") or []
        if rows:
            set_dependencies_from_task_plan(operation,rows)
    db.session.commit()
    return imported
