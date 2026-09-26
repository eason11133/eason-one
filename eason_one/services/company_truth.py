"""Deterministic Company Core vNext read model.

This module is intentionally boring: no LLM decides what the company is doing.
It projects Founder/observability state from durable Work, Execution, Artifact,
Meeting, Escalation and cost truth.  UI code may format this projection but must
not author competing business state.
"""
from __future__ import annotations

from collections import Counter
from decimal import Decimal

from sqlalchemy import func

from ..extensions import db
from ..models import (
    AgentRun,
    Artifact,
    ArtifactVersion,
    CostEvent,
    Employee,
    Escalation,
    Meeting,
    Project,
    WaitCondition,
    Work,
    WorkAssignment,
    WorkDependency,
)

MEETING_ACTIVE = {
    "PLANNED", "WAITING_FOR_INPUTS", "READY", "RUNNING", "ACTIVE",
    "SYNTHESIZING", "PAUSED", "WAITING_FOR_FOUNDER",
}
EXECUTION_ACTIVE = {"RUNNING"}
DELIVERY_TERMINAL = {"ACCEPTED", "CANCELLED"}
DELIVERY_FAILED = {"ABANDONED"}


def _wait_state(open_waits):
    """Project current state comes from current gates, never stale failed history."""
    if not open_waits:
        return None
    priority = {
        "AUTHORITY_EXHAUSTED": ("BLOCKED", 0),
        "SYSTEM_RECOVERY": ("RECOVERING", 1),
        "RECONCILIATION": ("RECOVERING", 2),
        "INTERNAL_RECOVERY": ("RECOVERING", 3),
        "EVIDENCE_REVIEW_RETRY": ("VERIFYING", 4),
        "HOST_PROOF_RETRY": ("VERIFYING", 5),
        "FOUNDER_PAUSE": ("PAUSED", 6),
    }
    ranked = []
    for gate in open_waits:
        kind = gate.get("condition_type", "") if isinstance(gate, dict) else getattr(gate, "condition_type", "")
        state, rank = priority.get(kind, ("WAITING", 20))
        ranked.append((rank, state, gate))
    _, state, gate = min(ranked, key=lambda row: row[0])
    reason = gate.get("reason") if isinstance(gate, dict) else gate.reason
    return state, reason


def project_snapshot(project: Project | int) -> dict:
    if isinstance(project, int):
        project = db.session.get(Project, project)
    if not project:
        raise ValueError("Project does not exist")

    works = Work.query.filter_by(project_id=project.id).order_by(Work.id).all()
    delivery = [row for row in works if row.work_type != "MANAGEMENT"]
    management = [row for row in works if row.work_type == "MANAGEMENT"]
    counts = Counter(row.state for row in delivery)
    operation_ids = [int(row.operation_id) for row in works if row.operation_id is not None]
    current_operation_id = max(operation_ids) if operation_ids else None
    current_delivery = [
        row for row in delivery
        if current_operation_id is None or row.operation_id == current_operation_id
    ]

    work_ids = [row.id for row in works]
    latest_run_by_work = {}
    if work_ids:
        for run in AgentRun.query.filter(AgentRun.work_id.in_(work_ids)).order_by(AgentRun.id.desc()).all():
            latest_run_by_work.setdefault(run.work_id, run)
    missing_evidence_ids = {
        work_id for work_id, run in latest_run_by_work.items()
        if run.status == "FAILED" and run.failure_reason == "MISSING_EVIDENCE"
    }
    # A crash/restart may leave the Work row EXECUTING after the durable Run has
    # already failed safe. Preserve that evidence as Founder-facing quiescence
    # even if a recovery pass has temporarily lost the matching WaitCondition.
    blocked_by_missing = set(missing_evidence_ids)
    if blocked_by_missing:
        dependencies = WorkDependency.query.filter(
            WorkDependency.work_id.in_(work_ids), WorkDependency.depends_on_work_id.in_(work_ids)
        ).all()
        changed = True
        while changed:
            changed = False
            for edge in dependencies:
                if edge.depends_on_work_id in blocked_by_missing and edge.work_id not in blocked_by_missing:
                    blocked_by_missing.add(edge.work_id)
                    changed = True
    work_runtime = __import__("eason_one.services.work_runtime", fromlist=["open_gates"])
    # Read every durable gate, not merely the first gate per Work. A stale older
    # retry gate must not hide a newer AUTHORITY_EXHAUSTED/RECONCILIATION truth
    # from the Founder surface.
    open_waits = [
        {**dict(gate), "work_id": row.id}
        for row in works for gate in work_runtime.open_gates(row)
    ]
    # Project-scoped hard waits are current Company truth. They must outrank
    # stale ABANDONED delivery history; otherwise an exhausted old attempt can
    # incorrectly mask a newer Founder authority / platform-integrity blocker.
    hard_waits = work_runtime.project_hard_blockers(project, include_founder=False)
    escalations = Escalation.query.filter_by(
        project_id=project.id, state="OPEN"
    ).order_by(Escalation.id).all()
    governance = __import__(
        "eason_one.services.governance",
        fromlist=["is_founder_type", "normalize_type", "PROJECT_SCOPED_TYPES", "EXECUTION_SCOPED_TYPES"],
    )
    founder_escalations = [
        row for row in escalations if governance.is_founder_type(row.escalation_type)
    ]
    project_founder_escalations = [
        row for row in founder_escalations
        if governance.normalize_type(row.escalation_type) in governance.PROJECT_SCOPED_TYPES
    ]
    work_founder_escalations = [
        row for row in founder_escalations
        if governance.normalize_type(row.escalation_type) in governance.EXECUTION_SCOPED_TYPES
    ]
    internal_escalations = [
        row for row in escalations if not governance.is_founder_type(row.escalation_type)
    ]
    core = __import__("eason_one.services.core_v018", fromlist=["is_v018_operation"])
    active_executions = []
    for run in AgentRun.query.filter(
        AgentRun.project_id == project.id,
        AgentRun.status.in_(EXECUTION_ACTIVE),
    ).order_by(AgentRun.id).all():
        work = db.session.get(Work, run.work_id) if run.work_id else None
        operation = work.operation if work else None
        if operation is None and run.operation_id:
            operation = db.session.get(__import__("eason_one.models", fromlist=["Operation"]).Operation, run.operation_id)
        if core.is_v018_operation(operation):
            active_executions.append(run)
    meetings = []
    for row in Meeting.query.filter(
        Meeting.project_id == project.id,
        Meeting.status.in_(MEETING_ACTIVE),
    ).order_by(Meeting.id).all():
        operation = db.session.get(__import__("eason_one.models", fromlist=["Operation"]).Operation, row.operation_id) if row.operation_id else None
        if core.is_v018_operation(operation):
            meetings.append(row)
    accepted_versions = (
        ArtifactVersion.query.join(Artifact)
        .filter(
            Artifact.project_id == project.id,
            ArtifactVersion.status == "ACCEPTED",
        )
        .order_by(ArtifactVersion.id).all()
    )
    spent = Decimal(
        db.session.query(func.coalesce(func.sum(CostEvent.real_cost_delta), 0))
        .filter(CostEvent.project_id == project.id).scalar() or 0
    )

    total = len(delivery)
    accepted = counts.get("ACCEPTED", 0)
    cancelled = counts.get("CANCELLED", 0)
    closed = accepted + cancelled
    result_ready_proof = None
    result_ready_error = None
    if project.status == "REVIEW":
        try:
            result_ready_proof = __import__(
                "eason_one.services.project_outcome", fromlist=["result_ready_proof"]
            ).result_ready_proof(project)
        except Exception as exc:
            # Founder read state must fail closed when the Contract/authority
            # proof cannot even be evaluated. Runtime recovery owns repair.
            result_ready_error = str(exc)
    terminal_projection = str(project.status or "").upper() in {"COMPLETED", "CANCELLED", "FAILED"}
    historical_residuals = {
        "open_wait_count": len(open_waits),
        "open_escalation_count": len(escalations),
        "active_execution_count": len(active_executions),
        "open_meeting_count": len(meetings),
    } if terminal_projection else {}
    if terminal_projection:
        # Keep every underlying row as immutable history, but terminal Project
        # read models must not project those residual rows as present-tense
        # company attention, execution, or meeting activity.
        open_waits = []
        hard_waits = []
        escalations = []
        founder_escalations = []
        project_founder_escalations = []
        work_founder_escalations = []
        internal_escalations = []
        active_executions = []
        meetings = []

    progress = 100 if project.status == "COMPLETED" or result_ready_proof else (
        round(closed / total * 100) if total else 0
    )

    if project.status == "COMPLETED":
        state = "COMPLETED"
        reason = "Project is formally closed."
    elif project.status == "CANCELLED":
        state = "CANCELLED"
        reason = project.current_state_summary or "Founder cancelled the Project."
    elif project.status == "REVIEW" and result_ready_proof:
        state = "RESULT_READY"
        reason = "Founder Project Contract is satisfied by a current Contract-bound Project Result proof."
    elif project.status == "REVIEW":
        state = "RECOVERING"
        reason = (
            "Project REVIEW has no current Contract-bound Project Result proof; Company recovery must reconcile this state before Founder acceptance."
            + (f" {result_ready_error}" if result_ready_error else "")
        )
    elif project.status == "PAUSED":
        state = "PAUSED"
        reason = project.current_state_summary or "Founder paused this Project; durable state is preserved until explicit Resume."
    elif project_founder_escalations:
        state = "NEEDS_YOU"
        reason = project_founder_escalations[0].reason
    elif any(row.status in {"RUNNING", "ACTIVE"} for row in meetings):
        state = "MEETING"
        reason = "A real Project Meeting is active."
    elif hard_waits:
        # These are globally-scoped Work-owned blockers (for example a
        # management AUTHORITY_EXHAUSTED wait after Founder rejects more
        # budget). Historical failed attempts remain audit evidence only.
        state, reason = _wait_state(hard_waits)
    elif missing_evidence_ids and current_delivery and all(
        row.id in blocked_by_missing or row.state in DELIVERY_TERMINAL | DELIVERY_FAILED
        for row in current_delivery
    ):
        state = "WAITING"
        reason = "BLOCKED_MISSING_EVIDENCE: current Work and its dependent path have no new verifiable evidence."
    elif any(row.state == "ABANDONED" for row in current_delivery) and not __import__(
        "eason_one.services.company_kernel", fromlist=["_has_active_delivery"]
    )._has_active_delivery(project):
        # A long-lived CEO management Run must not make the Founder surface say
        # WORKING when the current delivery branch has exhausted bounded recovery.
        # Conversely, an independent READY/runnable sibling still counts as real
        # delivery progress even between provider calls.
        state = "RECOVERING"
        failed = next(row for row in current_delivery if row.state == "ABANDONED")
        reason = (
            project.current_state_summary
            or f"{failed.title} exhausted bounded recovery; Company Runtime owns branch recovery or Project continuation."
        )
    elif active_executions:
        state = "WORKING"
        employee_count = len({int(run.employee_id) for run in active_executions if run.employee_id is not None})
        work_count = len({int(run.work_id) for run in active_executions if run.work_id is not None})
        if work_founder_escalations:
            reason = (
                f"{employee_count or len(active_executions)} employee(s) are working across "
                f"{work_count or len(active_executions)} Work item(s); "
                f"{len(work_founder_escalations)} Work-specific Founder decision(s) are also pending."
            )
        else:
            run = active_executions[0]
            work = db.session.get(Work, run.work_id) if run.work_id else None
            reason = f"Employee #{run.employee_id} is executing {work.title if work else run.purpose}."
    elif project.status == "FAILED":
        # Legacy terminal truth may still exist in history, but v0.20 Company
        # Kernel never derives Project failure from an abandoned Work/Mission.
        state = "FAILED"
        reason = project.current_state_summary or "Legacy Project failure is recorded."
    elif work_founder_escalations:
        # A Work-scoped Founder exception stops only its exact branch. If no
        # sibling Work is currently executing, the Founder surface should show
        # that attention is now the next company action without implying that
        # the Project Contract itself is blocked.
        state = "NEEDS_YOU"
        reason = work_founder_escalations[0].reason
    elif internal_escalations:
        state = "RECOVERING"
        reason = internal_escalations[0].reason
    elif open_waits:
        state, reason = _wait_state(open_waits)
    elif project.status == "BLOCKED":
        state = "BLOCKED"
        reason = project.current_state_summary or "Project execution is blocked by durable Company truth."
    elif delivery and all(row.state in (DELIVERY_TERMINAL | DELIVERY_FAILED) for row in delivery):
        # Abandoned bounded attempts are historical failure evidence, not an
        # eternal current-state veto. If a newer accepted/cancelled bounded move
        # exists, management/outcome truth owns the present state.
        latest_delivery = max(delivery, key=lambda row: row.id)
        if latest_delivery.state == "ABANDONED":
            state = "RECOVERING"
            reason = project.current_state_summary or "The latest bounded Mission failed; Project-level continuation/recovery is required."
        else:
            management_accepted = bool(management) and all(
                row.state == "ACCEPTED" for row in management
            )
            if management_accepted and result_ready_proof:
                state = "RESULT_READY"
                reason = "Required company Work is accepted and the current Project Result proof is ready for Founder review."
            else:
                state = "VERIFYING"
                reason = "Current accepted delivery evidence exists; company management is verifying and closing the outcome."
    elif any(row.state == "VERIFYING" for row in delivery):
        state = "VERIFYING"
        reason = "Company Work is under verification."
    elif any(row.state in {"READY", "EXECUTING"} for row in delivery):
        state = "READY" if not active_executions else "WORKING"
        reason = "Company Work is ready for deterministic scheduling."
    elif works:
        state = "IDLE"
        reason = "No Work is currently executable."
    else:
        state = "LEGACY"
        reason = "This Project has not yet been materialized into the vNext Work spine."

    return {
        "project_id": project.id,
        "project_status": project.status,
        "state": state,
        "reason": reason,
        "work_total": total,
        "work_accepted": accepted,
        "work_cancelled": cancelled,
        "work_counts": dict(counts),
        "management_work_ids": [row.id for row in management],
        "open_waits": open_waits,
        "open_escalations": escalations,
        "open_founder_escalations": founder_escalations,
        "open_project_founder_escalations": project_founder_escalations,
        "open_work_founder_escalations": work_founder_escalations,
        "open_internal_escalations": internal_escalations,
        "active_executions": active_executions,
        "open_meetings": meetings,
        "historical_terminal_residuals": historical_residuals,
        "accepted_artifacts": accepted_versions,
        "progress": progress,
        "spent_twd": spent,
        "has_vnext_truth": bool(works),
    }


def employee_activity(employee: Employee | int) -> dict:
    if isinstance(employee, int):
        employee = db.session.get(Employee, employee)
    if not employee:
        raise ValueError("Employee does not exist")

    core = __import__("eason_one.services.core_v018", fromlist=["is_v018_operation"])
    for meeting in Meeting.query.filter(Meeting.status.in_(MEETING_ACTIVE)).order_by(Meeting.id.desc()).all():
        project = db.session.get(Project, meeting.project_id) if meeting.project_id else None
        if project is not None and str(project.status or "").upper() in {"PAUSED", "COMPLETED", "CANCELLED", "FAILED"}:
            # Pause preserves the durable Meeting checkpoint, so the Meeting row
            # may still carry an active status.  That preserved history must not
            # project an employee as currently working while the Project-wide
            # activation fence is closed.
            continue
        operation = db.session.get(__import__("eason_one.models", fromlist=["Operation"]).Operation, meeting.operation_id) if meeting.operation_id else None
        if not core.is_v018_operation(operation):
            continue
        participant = next(
            (row for row in meeting.participants if row.employee_id == employee.id and row.removed_at is None),
            None,
        )
        if participant:
            return {
                "employee_id": employee.id,
                "activity_type": "IN_MEETING",
                "meeting_id": meeting.id,
                "project_id": meeting.project_id,
                "work_id": meeting.related_work_id,
                "reason": meeting.title,
                "source": "WORK_CORE_V018",
            }

    runs = AgentRun.query.filter_by(employee_id=employee.id, status="RUNNING").order_by(AgentRun.id.desc()).all()
    for run in runs:
        project = db.session.get(Project, run.project_id) if run.project_id else None
        if project is not None and str(project.status or "").upper() in {"PAUSED", "COMPLETED", "CANCELLED", "FAILED"}:
            # A RUNNING row can be the durable pre-pause/pre-crash checkpoint.
            # Settlement recovery may classify it, but Founder read models must
            # not describe it as live execution until explicit Resume.
            continue
        work = db.session.get(Work, run.work_id) if run.work_id else None
        operation = work.operation if work else (
            db.session.get(__import__("eason_one.models", fromlist=["Operation"]).Operation, run.operation_id)
            if run.operation_id else None
        )
        if not core.is_v018_operation(operation):
            continue
        return {
            "employee_id": employee.id,
            "activity_type": "FOCUSED_WORK",
            "project_id": run.project_id,
            "work_id": run.work_id,
            "execution_id": run.id,
            "reason": work.title if work else run.purpose,
            "source": "WORK_CORE_V018",
        }

    assignments = (
        WorkAssignment.query.join(Work)
        .filter(
            WorkAssignment.employee_id == employee.id,
            WorkAssignment.ended_at.is_(None),
            Work.state == "WAITING",
        )
        .order_by(WorkAssignment.id.desc()).all()
    )
    work_runtime = __import__("eason_one.services.work_runtime", fromlist=["primary_gate"])
    for assignment in assignments:
        project = db.session.get(Project, assignment.work.project_id)
        if project is not None and str(project.status or "").upper() in {"PAUSED", "COMPLETED", "CANCELLED", "FAILED"}:
            continue
        if not core.is_v018_operation(assignment.work.operation):
            continue
        gate = work_runtime.primary_gate(assignment.work)
        return {
            "employee_id": employee.id,
            "activity_type": "WAITING",
            "project_id": assignment.work.project_id,
            "work_id": assignment.work_id,
            "reason": getattr(gate, "reason", None) or "Assigned Work is waiting.",
            "source": "WORK_CORE_V018",
        }

    return {
        "employee_id": employee.id,
        "activity_type": "AVAILABLE",
        "project_id": None,
        "work_id": None,
        "reason": "No active v0.18 Execution, Meeting, or waiting assigned Work.",
        "source": "WORK_CORE_V018",
    }


def accepted_artifact_rows(project_id: int, limit: int = 8) -> list[dict]:
    versions = (
        ArtifactVersion.query.join(Artifact)
        .filter(
            Artifact.project_id == project_id,
            ArtifactVersion.status == "ACCEPTED",
        )
        .order_by(ArtifactVersion.accepted_at.desc(), ArtifactVersion.id.desc())
        .limit(limit).all()
    )
    rows = []
    for version in versions:
        artifact = version.artifact
        run = version.execution
        rows.append({
            "artifact": artifact,
            "version": version,
            "work": artifact.work,
            "run": run,
            "employee": version.producer,
            "title": artifact.title,
            "summary": version.content_text or version.content_location or "Accepted artifact",
            "updated": version.accepted_at or version.created_at,
        })
    return rows
