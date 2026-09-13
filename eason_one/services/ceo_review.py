"""Deterministic CEO trigger and review policy.

Slice 2 deliberately stops at judgement.  Review results contain no side effects;
Slice 3 is responsible for translating an approved intent through Governance and
canonical domain services.
"""
from __future__ import annotations

from typing import Any

from ..extensions import db
from ..models import Artifact, ArtifactVersion, CompanyEvent, Employee, Project, Work
from . import ceo_operating


TRIGGER_DISPOSITIONS = {
    "IGNORE",
    "DETERMINISTIC_ACTION",
    "WORK_REVIEW",
    "PROJECT_REVIEW",
    "ESCALATION_RECHECK",
}

WORK_REVIEW_OUTCOMES = {
    "ACCEPT_WORK",
    "CONTINUE_WORK",
    "REPAIR_WORK",
    "WAIT",
    "QUIESCE",
    "PROJECT_REVIEW_REQUIRED",
}

PROJECT_RECOMMENDATIONS = {
    "CONTINUE",
    "REPAIR",
    "REPLAN",
    "REALLOCATE",
    "REQUEST_VERIFICATION",
    "QUIESCE",
    "ESCALATE",
    "RECOMMEND_STOP",
    "READY_FOR_OUTCOME_CHECK",
}

_LOW_VALUE_EVENT_TYPES = {
    "WORK_CREATED",
    "WORK_ASSIGNED",
    "WORK_REASSIGNED",
    "WORK_READY",
    "WORK_STARTED",
    "COMPANY_WORK_WAVE_STARTED",
    "COMPANY_WORK_WAVE_COMPLETED",
    "RUNTIME_CHECKPOINT_STARTED",
    "RUNTIME_STEP_COMMITTED",
    "RUNTIME_STOPPED_AT_DURABLE_STATE",
    "EXECUTION_RETRIED",
    "WORK_EVIDENCE_REVIEW_RETRY_SCHEDULED",
    "WORK_SYSTEM_RECOVERY_AUTO_RESUMED",
    "PROJECT_MANAGEMENT_SYSTEM_RECOVERY_AUTO_RESUMED",
}

_PROJECT_REVIEW_EVENT_REASONS = {
    "WORK_ACCEPTED": "WORK_ACCEPTED",
    "WORK_ABANDONED": "WORK_FAILED_BOUNDEDLY",
    "ARTIFACT_ACCEPTED": "ARTIFACT_ACCEPTED",
    "ARTIFACT_REJECTED": "CRITIC_REJECTED",
    "WORK_EVIDENCE_REVIEW_EXHAUSTED": "EVIDENCE_REVIEW_EXHAUSTED",
    "PROJECT_HOST_HTTP_REVERIFIED": "MEANINGFUL_EVIDENCE_CHANGE",
    "FOUNDER_DECISION_COMMITTED": "FOUNDER_DIRECTIVE_CHANGED",
    "FOUNDER_BUDGET_REJECTED_BOUNDARY": "FOUNDER_DIRECTIVE_CHANGED",
    "WORK_STAFFING_REPLAN_REQUIRED": "CAPABILITY_OR_STAFFING_CHANGE",
    "WORK_CAPABILITY_GAP_IDENTIFIED": "CAPABILITY_OR_STAFFING_CHANGE",
    "PROJECT_REPEATED_FAILURE_LOOP_STOPPED": "NO_PROGRESS_OR_REPEAT_LOOP",
    "CEO_PROJECT_MANAGEMENT_REVIEW_REQUIRED": "KERNEL_MANAGEMENT_JUDGEMENT_REQUIRED",
}

_WORK_REVIEW_EVENT_REASONS = {
    "WORK_WAITING": "WORK_WAITING",
    "WORK_VERIFYING": "WORK_VERIFYING",
    "WORK_RUNTIME_EXCEPTION": "WORK_RUNTIME_EXCEPTION",
    "RUNTIME_STEP_FAILED": "WORK_RUNTIME_EXCEPTION",
}

_ESCALATION_RECHECK_EVENT_REASONS = {
    "FOUNDER_GATE_INVALIDATED": "DECISION_BASIS_CHANGED",
    "PROJECT_CONTINUATION_STALE_PLAN_SUPERSEDED": "DECISION_BASIS_CHANGED",
}

_TERMINAL_OR_ALREADY_OWNED_EVENTS = {
    "PROJECT_RESULT_READY",
    "PROJECT_OUTCOME_READY",
    "FOUNDER_PROJECT_COMPLETED",
    "PROJECT_CANCELLED_BY_FOUNDER",
}


def _project_for_event(event: CompanyEvent) -> Project | None:
    return db.session.get(Project, event.project_id) if event.project_id else None


def _event_reveals_overcommitment(event: CompanyEvent) -> bool:
    """Return True when an otherwise-low-value Work event exposes contention.

    A quiescent responsibility can lawfully become runnable again after its
    Employee has been allocated elsewhere.  WORK_READY/WORK_STARTED are usually
    runtime noise, but in that exact situation they materially change the CEO's
    portfolio truth and must refresh Project/Company management state.
    """
    if not event.work_id or str(event.event_type or "").upper() not in {
        "WORK_READY", "WORK_STARTED", "WORK_ASSIGNED", "WORK_REASSIGNED",
    }:
        return False
    work = db.session.get(Work, int(event.work_id))
    if work is None:
        return False
    runtime = __import__("eason_one.services.work_runtime", fromlist=["active_assignment"])
    assignment = runtime.active_assignment(work)
    if assignment is None:
        return False
    try:
        capacity = ceo_operating.employee_capacity_view(int(assignment.employee_id))
    except ValueError:
        return False
    return str(capacity.get("capacity") or "").upper() == "OVERCOMMITTED"


def classify_event(event: CompanyEvent | int) -> dict:
    """Return the cheapest legal CEO response to one durable CompanyEvent."""
    if isinstance(event, int):
        event = db.session.get(CompanyEvent, event)
    if event is None:
        raise ValueError("COMPANY_EVENT_NOT_FOUND")
    project = _project_for_event(event)
    event_type = str(event.event_type or "").upper()

    if project is not None:
        status = str(project.status or "").upper()
        if status == "PAUSED":
            # Pause is an activation fence.  Truth may still settle in the
            # background, but ordinary CEO reviews may not manufacture new Work.
            return _trigger(event, "IGNORE", "PROJECT_PAUSED")
        if status in ceo_operating.TERMINAL_PROJECT_STATUSES:
            return _trigger(event, "IGNORE", "PROJECT_TERMINAL")
        if ceo_operating.project_operating_view(project)["result_state"] == "RESULT_READY":
            if event_type not in {"FOUNDER_DECISION_COMMITTED", "FOUNDER_GATE_INVALIDATED"}:
                return _trigger(event, "IGNORE", "RESULT_ALREADY_READY")

    if _event_reveals_overcommitment(event):
        return _trigger(event, "PROJECT_REVIEW", "RESOURCE_CONTENTION")
    if event_type in _LOW_VALUE_EVENT_TYPES:
        return _trigger(event, "IGNORE", "RUNTIME_OR_ACTIVITY_EVENT")
    if event_type in _TERMINAL_OR_ALREADY_OWNED_EVENTS:
        return _trigger(event, "IGNORE", "DOMAIN_TRANSITION_ALREADY_OWNS_NEXT_STEP")
    if event_type in _ESCALATION_RECHECK_EVENT_REASONS:
        return _trigger(event, "ESCALATION_RECHECK", _ESCALATION_RECHECK_EVENT_REASONS[event_type])
    if event_type in _PROJECT_REVIEW_EVENT_REASONS:
        return _trigger(event, "PROJECT_REVIEW", _PROJECT_REVIEW_EVENT_REASONS[event_type])
    if event_type in _WORK_REVIEW_EVENT_REASONS:
        return _trigger(event, "WORK_REVIEW", _WORK_REVIEW_EVENT_REASONS[event_type])
    # Unknown events are ignored rather than sent to an LLM by default.  A new
    # domain event must be deliberately classified before it can spend CEO
    # reasoning budget.
    return _trigger(event, "IGNORE", "UNCLASSIFIED_EVENT_NO_CEO_SPEND")


def _trigger(event: CompanyEvent, disposition: str, reason: str) -> dict:
    if disposition not in TRIGGER_DISPOSITIONS:
        raise ValueError(f"UNKNOWN_TRIGGER_DISPOSITION:{disposition}")
    semantic_version = str((event.payload_json or {}).get("version") or event.id)
    return {
        "trigger_id": f"event:{event.id}",
        "event_ref": f"company_event:{event.id}",
        "project_ref": f"project:{event.project_id}" if event.project_id else None,
        "work_ref": f"work:{event.work_id}" if event.work_id else None,
        "disposition": disposition,
        "reason": reason,
        "subject_refs": [
            ref for ref in (
                f"project:{event.project_id}" if event.project_id else None,
                f"work:{event.work_id}" if event.work_id else None,
                f"artifact:{event.artifact_id}" if event.artifact_id else None,
                f"decision:{event.decision_id}" if event.decision_id else None,
            ) if ref
        ],
        "basis_event_id": event.id,
        "dedupe_key": f"{disposition}:{event_type_key(event)}:{semantic_version}",
    }


def event_type_key(event: CompanyEvent) -> str:
    return ":".join([
        str(event.event_type or "UNKNOWN").upper(),
        str(event.project_id or 0),
        str(event.work_id or 0),
        str(event.artifact_id or 0),
        str(event.decision_id or 0),
    ])


def _latest_rejected_version(work: Work) -> ArtifactVersion | None:
    return (
        ArtifactVersion.query.join(Artifact)
        .filter(Artifact.work_id == work.id, ArtifactVersion.status == "REJECTED")
        .order_by(ArtifactVersion.id.desc()).first()
    )


def work_review(work: Work | int) -> dict:
    """Judge one Work without changing it or calling a provider."""
    if isinstance(work, int):
        work = db.session.get(Work, work)
    if work is None:
        raise ValueError("WORK_NOT_FOUND")
    project = work.project or db.session.get(Project, work.project_id)
    if project is None:
        raise ValueError("PROJECT_NOT_FOUND")
    if project.status == "PAUSED":
        raise ValueError("PROJECT_REVIEW_FORBIDDEN_PAUSED")

    runtime = __import__("eason_one.services.work_runtime", fromlist=["open_gates"])
    gates = list(runtime.open_gates(work))
    gate_types = {str(row.get("condition_type") or "").upper() for row in gates}
    rejected = _latest_rejected_version(work)

    if work.state == "ACCEPTED":
        outcome = "ACCEPT_WORK"
        diagnosis = "Durable Work acceptance exists."
        blocker = None
    elif rejected is not None and work.state == "EXECUTING":
        outcome = "REPAIR_WORK"
        diagnosis = f"ArtifactVersion #{rejected.id} was rejected; the Work remains active for repair."
        blocker = None
    elif work.state == "WAITING" and gate_types and gate_types <= {"DEPENDENCY"}:
        outcome = "WAIT"
        diagnosis = "Work is waiting on a declared upstream dependency."
        blocker = "DEPENDENCY"
    elif work.state == "WAITING" and gate_types & {"EVIDENCE_REVIEW_RETRY", "HOST_PROOF_RETRY"}:
        outcome = "WAIT"
        diagnosis = "Evidence verification has a bounded runtime-owned wait; implementation replay is not authorized."
        blocker = "EVIDENCE"
    elif work.state == "WAITING" and gate_types & {"SYSTEM_RECOVERY", "RECONCILIATION", "INTERNAL_RECOVERY", "RETRY_BACKOFF"}:
        outcome = "WAIT"
        diagnosis = "Runtime recovery owns the current wait."
        blocker = "EXECUTION"
    elif work.state == "WAITING" and gate_types & {"FOUNDER_DECISION", "AUTHORITY_EXHAUSTED", "CAPABILITY_GAP"}:
        outcome = "PROJECT_REVIEW_REQUIRED"
        diagnosis = "The Work wait crosses a management/authority boundary and cannot be solved locally."
        blocker = (
            "AUTHORITY" if gate_types & {"FOUNDER_DECISION", "AUTHORITY_EXHAUSTED"}
            else "CAPABILITY"
        )
    elif work.state == "ABANDONED":
        outcome = "PROJECT_REVIEW_REQUIRED"
        diagnosis = "The bounded Work branch ended without acceptance; Project-level continuation policy owns the next move."
        blocker = None
    elif work.state == "CANCELLED":
        outcome = "PROJECT_REVIEW_REQUIRED"
        diagnosis = "The Work was cancelled; only Project truth can decide whether another objective is still needed."
        blocker = None
    elif work.state in {"READY", "EXECUTING", "VERIFYING"}:
        outcome = "CONTINUE_WORK"
        diagnosis = "The Work has a lawful local execution/verification path."
        blocker = None
    elif work.state == "PROPOSED":
        outcome = "WAIT"
        diagnosis = "The Work is proposed but not yet in the runnable state machine."
        blocker = None
    else:
        outcome = "PROJECT_REVIEW_REQUIRED"
        diagnosis = f"Unsupported Work state {work.state!r} requires Project-level fail-closed review."
        blocker = None

    if outcome not in WORK_REVIEW_OUTCOMES:
        raise AssertionError(outcome)
    return {
        "review_type": "WORK_REVIEW",
        "work_ref": f"work:{work.id}",
        "project_ref": f"project:{project.id}",
        "basis": {
            "work_state": work.state,
            "gate_types": sorted(gate_types),
            "latest_rejected_artifact_version_id": getattr(rejected, "id", None),
        },
        "outcome": outcome,
        "diagnosis": diagnosis,
        "blocker": blocker,
        "produced_value_refs": [
            f"artifact_version:{row.id}" for row in ArtifactVersion.query.join(Artifact)
            .filter(Artifact.work_id == work.id, ArtifactVersion.status == "ACCEPTED")
            .order_by(ArtifactVersion.id).all()
        ],
        "intended_actions": [],
        "expected_outcome": None,
        "confidence": "HIGH" if outcome != "PROJECT_REVIEW_REQUIRED" else "MEDIUM",
    }


def _advisory_learning() -> dict:
    learning = __import__(
        "eason_one.services.ceo_learning", fromlist=["active_policy_advisory"]
    )
    ceo = Employee.query.filter_by(slug="ceo", active=True).first()
    return learning.active_policy_advisory(ceo)


def project_review(project: Project | int) -> dict:
    """Deterministic first-pass Project judgement from Slice 1 semantics.

    Ambiguous strategic reasoning is deliberately *not* improvised here.  Clear
    durable facts produce safe recommendations; later CEO reasoning can enrich
    REPLAN/REALLOCATE decisions without weakening these gates.
    """
    if isinstance(project, int):
        project = db.session.get(Project, project)
    if project is None:
        raise ValueError("PROJECT_NOT_FOUND")
    if project.status == "PAUSED":
        raise ValueError("PROJECT_REVIEW_FORBIDDEN_PAUSED")
    if project.status in ceo_operating.TERMINAL_PROJECT_STATUSES:
        raise ValueError("PROJECT_REVIEW_FORBIDDEN_TERMINAL")

    view = ceo_operating.project_operating_view(project)
    blockers = view["blockers"]
    primary = blockers.get("primary")
    evidence_state = view["contract_evidence"]["state"]
    outcome_satisfied = view["contract_evidence"].get("overall_status") == "SATISFIED"
    works = Work.query.filter_by(project_id=project.id).order_by(Work.id).all()
    delivery = [row for row in works if row.work_type != "MANAGEMENT"]
    active_delivery = [
        row for row in delivery
        if row.state in {"PROPOSED", "READY", "EXECUTING", "WAITING", "VERIFYING"}
    ]
    rejected_active = [
        row for row in delivery
        if row.state == "EXECUTING" and _latest_rejected_version(row) is not None
    ]

    recommendation = None
    diagnosis = None
    next_objective = None
    health = None

    if view["result_state"] == "RESULT_READY":
        recommendation = "READY_FOR_OUTCOME_CHECK"
        health = "READY_FOR_OUTCOME_CHECK"
        diagnosis = "Current Contract-bound Project Result proof is ready; ordinary execution must not restart."
    elif blockers.get("classification_error"):
        recommendation = "QUIESCE"
        health = "BLOCKED"
        diagnosis = blockers["classification_error"] + ": fail closed until durable blocker truth is classified."
    elif rejected_active:
        recommendation = "REPAIR"
        health = "NEEDS_REPAIR"
        diagnosis = f"{len(rejected_active)} active Work item(s) contain a rejected ArtifactVersion and remain repairable."
        next_objective = "Repair the rejected current ArtifactVersion without changing the Project Contract."
    elif primary and primary["type"] == "AUTHORITY":
        recommendation = "ESCALATE"
        health = "BLOCKED"
        diagnosis = primary["reason"]
    elif outcome_satisfied and not active_delivery:
        # Project-level success proof outranks failed/abandoned historical
        # delivery branches once no delivery is still active.  The CEO may
        # request the deterministic current-Contract outcome gate, but cannot
        # declare Result Ready or complete the Project itself.
        recommendation = "READY_FOR_OUTCOME_CHECK"
        health = "READY_FOR_OUTCOME_CHECK"
        diagnosis = (
            "Current accepted Project evidence satisfies every Founder success criterion and no delivery Work remains active; "
            "the deterministic Result Ready gate should revalidate current Contract, authority and evidence."
        )
        next_objective = "Run the deterministic Project outcome gate; do not create more delivery Work."
    elif primary and primary["type"] == "EVIDENCE" and primary["resolution_state"] == "QUIESCENT":
        recommendation = "QUIESCE"
        health = "WAITING"
        diagnosis = primary["reason"]
    elif (
        primary
        and primary["type"] == "EVIDENCE"
        and evidence_state == "CONTRADICTED"
        and primary.get("basis") == "project_contract:contradicted"
        and primary.get("source") == "PROJECT_OUTCOME"
    ):
        # This blocker is emitted only after authoritative Project outcome
        # evidence contradicts a Founder success criterion *and* all delivery
        # Work is terminal with no executable branch.  That is materially
        # stronger than ordinary missing evidence, so the CEO may recommend
        # stopping.  It still may not terminate the Project itself.
        recommendation = "RECOMMEND_STOP"
        health = "AT_RISK"
        diagnosis = primary["reason"]
        next_objective = None
    elif (
        delivery
        and all(row.state == "ACCEPTED" for row in delivery)
        and evidence_state != "CONTRADICTED"
        and __import__(
            "eason_one.services.project_outcome", fromlist=["needs_semantic_review"]
        ).needs_semantic_review(project)
    ):
        recommendation = "REQUEST_VERIFICATION"
        health = "READY_FOR_VERIFICATION"
        diagnosis = (
            "All current delivery Work is durably accepted, but the Project Contract still lacks "
            "independent semantic outcome proof over that exact accepted evidence."
        )
        next_objective = (
            "Run the canonical independent Project outcome review over current accepted evidence; "
            "do not replay producer Work."
        )
    elif primary and primary["type"] == "EVIDENCE":
        recommendation = "REPLAN"
        health = "NEEDS_REPLAN"
        diagnosis = primary["reason"]
        next_objective = (
            "Choose a distinct decision-relevant evidence path from the current Contract gap; "
            "do not replay the exhausted implementation or evidence-review attempt."
        )
    elif evidence_state == "CONTRADICTED":
        recommendation = "REPLAN"
        health = "NEEDS_REPLAN"
        diagnosis = "Authoritative Project evidence contradicts at least one Founder success criterion."
        next_objective = "Re-evaluate the Project strategy against the contradicted Contract criterion before creating more Work."
    elif primary and primary["type"] == "CAPABILITY":
        recommendation = "REALLOCATE"
        health = "BLOCKED"
        diagnosis = primary["reason"]
        next_objective = "Resolve the demonstrated capability gap with existing Company capacity before considering new persistent staff."
    elif primary and primary["type"] == "EXECUTION":
        recommendation = "CONTINUE"
        health = "WAITING" if primary["resolution_state"] in {"EXPECTED_WAIT", "RESOLVING"} else "AT_RISK"
        diagnosis = primary["reason"]
        next_objective = "Let the owning runtime recovery path settle before management changes strategy."
    elif primary and primary["type"] == "DEPENDENCY":
        recommendation = "CONTINUE"
        health = "WAITING"
        diagnosis = primary["reason"]
        next_objective = "Wait for the declared upstream Work rather than creating duplicate Work."
    elif view["operating_state"] == "VERIFYING":
        recommendation = "CONTINUE"
        health = "READY_FOR_VERIFICATION"
        diagnosis = "Current Company Work is already under verification."
    elif delivery and all(row.state == "ACCEPTED" for row in delivery):
        recommendation = "READY_FOR_OUTCOME_CHECK"
        health = "READY_FOR_OUTCOME_CHECK"
        diagnosis = "All current delivery Work is accepted; Project Contract outcome should be checked against current proof."
    elif view["operating_state"] in {"READY", "IN_PROGRESS", "EXPECTED_WAIT"}:
        recommendation = "CONTINUE"
        health = "ON_TRACK" if view["operating_state"] != "EXPECTED_WAIT" else "WAITING"
        diagnosis = "A lawful current Work path exists and no material CEO-level blocker requires intervention."
    else:
        recommendation = "QUIESCE"
        health = "WAITING"
        diagnosis = "No current executable meaningful path is proven; fail closed instead of creating busywork."

    if recommendation not in PROJECT_RECOMMENDATIONS:
        raise AssertionError(recommendation)
    return {
        "review_type": "PROJECT_REVIEW",
        "project_ref": f"project:{project.id}",
        "basis_revision": ceo_operating.project_basis_revision(project, view=view),
        "health_assessment": health,
        "diagnosis": diagnosis,
        "primary_blocker": primary,
        "supporting_findings": blockers.get("secondary") or [],
        "recommendation": recommendation,
        "next_meaningful_objective": next_objective,
        "intended_actions": [],
        "expected_outcome": None,
        "founder_escalation": primary if recommendation == "ESCALATE" else None,
        "evidence_state": evidence_state,
        "advisory_learning": _advisory_learning(),
        "confidence": "HIGH" if recommendation in {
            "QUIESCE", "ESCALATE", "RECOMMEND_STOP", "REQUEST_VERIFICATION", "READY_FOR_OUTCOME_CHECK"
        } else "MEDIUM",
    }
