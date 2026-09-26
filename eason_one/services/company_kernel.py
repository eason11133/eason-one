"""Eason One v0.20 Work-first Company Kernel.

This module owns vNext business progression. The process/thread shell may call
``advance_once`` repeatedly, but it may not invent status transitions itself.

Governing chain:
Founder Project Contract -> Work -> Execution -> Artifact/Verification ->
Project outcome -> bounded continuation/recovery -> Result Ready.

Operation/Mission is an audit/sequencing envelope. Its completion criteria are
never consulted for final Project acceptance.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json

from flask import current_app

from ..extensions import db
from ..models import AgentRun, Artifact, ArtifactVersion, CompanyEvent, Employee, Escalation, HiringRequest, Meeting, Operation, Project, VerificationRecord, Work, WorkDependency, now
from . import work_runtime
from .company_events import emit

ACTIVE_PROJECT_STATES = {"ACTIVE", "PLANNING", "BLOCKED", "REVIEW"}
PROJECT_TERMINAL = {"COMPLETED", "CANCELLED"}
MISSION_TERMINAL = {"COMPLETED", "FAILED", "CANCELLED", "TERMINATED_BY_FOUNDER", "SUPERSEDED"}
DELIVERY_TERMINAL = {"ACCEPTED", "ABANDONED", "CANCELLED"}


_SYSTEM_RECOVERY_PROTOCOL = "HOST_PROOF_REVIEW_HANDOFF_V2"
_SYSTEM_RECOVERY_PROTOCOL_GATE_PREFIX = "PROJECT_REPEATED_FAILURE_PROTOCOL:"
_WORK_RUNTIME_EXCEPTION_GATE_PREFIX = "WORK_RUNTIME_EXCEPTION:"
_WORK_RUNTIME_EXCEPTION_EXHAUSTED_PREFIX = "WORK_RUNTIME_EXCEPTION_EXHAUSTED:"

_CEO_RUNTIME_CHECKPOINT_EVENT = "CEO_RUNTIME_CHECKPOINT"
_CEO_ACTION_RECEIPT_EVENT = "CEO_ACTION_RECEIPT"
_CEO_MANAGEMENT_REVIEW_REQUIRED_EVENT = "CEO_PROJECT_MANAGEMENT_REVIEW_REQUIRED"
_CEO_RECOMMENDATION_ACTION = {
    "REPLAN": "AUTHORIZE_PROJECT_REPLAN",
    "REQUEST_VERIFICATION": "REQUEST_PROJECT_VERIFICATION",
    "READY_FOR_OUTCOME_CHECK": "REQUEST_PROJECT_OUTCOME_CHECK",
    "QUIESCE": "DOCUMENT_QUIESCENCE",
    "RECOMMEND_STOP": "RECOMMEND_PROJECT_STOP",
    "ESCALATE": "SURFACE_FOUNDER_ESCALATION",
}


def _ceo_management_cutover_active() -> bool:
    return CompanyEvent.query.filter_by(event_type=_CEO_RUNTIME_CHECKPOINT_EVENT).first() is not None


def _ceo_management_receipt(project: Project, review: dict) -> dict | None:
    action_type = _CEO_RECOMMENDATION_ACTION.get(str(review.get("recommendation") or ""))
    if not action_type:
        return None
    expected_basis = str(review.get("basis_revision") or "")
    expected_learning = list((review.get("advisory_learning") or {}).get("validated_learning_ids") or [])
    rows = CompanyEvent.query.filter_by(
        event_type=_CEO_ACTION_RECEIPT_EVENT, project_id=project.id
    ).order_by(CompanyEvent.id.desc()).limit(24).all()
    for row in rows:
        payload = dict(row.payload_json or {})
        if (
            payload.get("status") == "APPLIED"
            and payload.get("action_type") == action_type
            and str(payload.get("basis_revision") or "") == expected_basis
            and list(payload.get("advisory_learning_ids") or []) == expected_learning
        ):
            return {**payload, "receipt_event_id": row.id}
    return None


def _ensure_ceo_management_clearance(project: Project, expected_recommendation: str) -> dict:
    """Require a current CEO decision before strategic Kernel work post-cutover.

    Pre-cutover Projects preserve legacy autonomous sequencing.  After the CEO
    future-only checkpoint exists, strategy/verification judgement is separated
    from execution: Kernel emits one deduped review-required fact and waits for a
    matching CEO Decision receipt.  The receipt authorizes only the existing
    canonical Kernel path; it never grants new Founder authority.
    """
    if not _ceo_management_cutover_active():
        return {"authorized": True, "mode": "LEGACY_PRE_CEO_CUTOVER", "decision_id": None}
    review = __import__(
        "eason_one.services.ceo_review", fromlist=["project_review"]
    ).project_review(project)
    recommendation = str(review.get("recommendation") or "")
    if recommendation != str(expected_recommendation or ""):
        return {
            "authorized": False,
            "mode": "CEO_REVIEW_MISMATCH",
            "review": review,
            "reason": f"CURRENT_CEO_RECOMMENDATION:{recommendation or 'NONE'}",
        }
    receipt = _ceo_management_receipt(project, review)
    if receipt is not None:
        return {
            "authorized": True,
            "mode": "CEO_DECISION_RECEIPT",
            "review": review,
            "decision_id": receipt.get("decision_id"),
            "receipt": receipt,
        }
    action_type = _CEO_RECOMMENDATION_ACTION.get(recommendation)
    if not action_type:
        return {"authorized": False, "mode": "CEO_ACTION_UNSUPPORTED", "review": review}
    basis = str(review.get("basis_revision") or "")
    learning_ids = list((review.get("advisory_learning") or {}).get("validated_learning_ids") or [])
    fingerprint = hashlib.sha256(json.dumps({
        "project_id": project.id,
        "recommendation": recommendation,
        "action_type": action_type,
        "basis_revision": basis,
        "advisory_learning_ids": learning_ids,
    }, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[:20]
    correlation = f"ceo-management-required:{project.id}:{fingerprint}"
    existing = CompanyEvent.query.filter_by(
        event_type=_CEO_MANAGEMENT_REVIEW_REQUIRED_EVENT,
        project_id=project.id,
        correlation_id=correlation,
    ).first()
    if existing is None:
        emit(
            _CEO_MANAGEMENT_REVIEW_REQUIRED_EVENT,
            actor_type="RUNTIME",
            project_id=project.id,
            correlation_id=correlation,
            payload={
                "recommendation": recommendation,
                "action_type": action_type,
                "basis_revision": basis,
                "advisory_learning_ids": learning_ids,
                "provider_calls": 0,
                "new_work_created": False,
                "founder_authority_changed": False,
            },
            commit=False,
        )
        db.session.commit()
    return {
        "authorized": False,
        "mode": "CEO_DECISION_REQUIRED",
        "review": review,
        "reason": "CURRENT_CEO_MANAGEMENT_DECISION_REQUIRED",
    }


def _close_result_ready_after_ceo_review(project: Project, evaluation: dict, outcome) -> dict | None:
    """Cross the Result Ready boundary only after current CEO judgement.

    The CEO receipt requests the deterministic outcome gate; it never grants
    Project lifecycle authority. ``project_outcome.close_result_ready`` still
    recomputes current Contract/evidence/authority and is the only function
    allowed to project REVIEW / Result Ready here.
    """
    clearance = _ensure_ceo_management_clearance(project, "READY_FOR_OUTCOME_CHECK")
    if not clearance.get("authorized"):
        return None
    return outcome.close_result_ready(
        project,
        evaluation,
        management_decision_id=clearance.get("decision_id"),
    )


def _work_runtime_failure_signature(exc: Exception) -> str:
    normalized = " ".join(str(exc or "").strip().split())
    basis = f"{type(exc).__name__}|{normalized}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:20]


def _system_recovery_protocol_issue_code(signature: str | None) -> str | None:
    value = str(signature or "").strip()
    return f"{_SYSTEM_RECOVERY_PROTOCOL_GATE_PREFIX}{value}" if value else None


def _is_legacy_host_proof_recovery_gate(gate: dict) -> bool:
    """Recognize only the old unscoped repeated-failure recovery gate.

    Before scoped recovery issue codes existed, this exact Project-level loop
    stopper opened an unscoped SYSTEM_RECOVERY gate. Compatibility may resolve
    that historical gate only when it is the sole SYSTEM_RECOVERY gate on the
    management Work and its persisted reason matches the old bounded-failure
    protocol. Other unscoped system conditions remain untouched.
    """
    if str((gate or {}).get("condition_type") or "").upper() != "SYSTEM_RECOVERY":
        return False
    if str((gate or {}).get("issue_code") or "").strip():
        return False
    text = " ".join(str((gate or {}).get("reason") or "").casefold().split())
    return (
        "same bounded project failure repeated twice" in text
        or "repeated identical recovery failed twice" in text
    )


def _latest_rejected_delivery_with_host_proof(project: Project):
    """Find a repeated-review failure caused by missing host-proof visibility.

    This is intentionally narrow: the latest ArtifactVersion must be rejected,
    deterministic host verification for that exact version must have PASSED, and
    independent review may be UNPROVEN but must contain no semantic FAILED row.
    It does not claim the Work succeeded; it only proves that a system protocol
    upgrade is allowed one bounded retry without asking the Founder.
    """
    works = (
        Work.query.filter(
            Work.project_id == project.id,
            Work.work_type != "MANAGEMENT",
            Work.state == "ABANDONED",
        )
        .order_by(Work.id.desc())
        .all()
    )
    for work in works:
        version = (
            ArtifactVersion.query.join(Artifact)
            .filter(Artifact.work_id == work.id)
            .order_by(ArtifactVersion.id.desc())
            .first()
        )
        if not version or version.status != "REJECTED":
            continue
        host = (
            VerificationRecord.query.filter_by(
                work_id=work.id, artifact_version_id=version.id,
                method="HOST_ENGINEERING_VALIDATION", status="PASSED",
            )
            .order_by(VerificationRecord.id.desc())
            .first()
        )
        review = (
            VerificationRecord.query.filter_by(
                work_id=work.id, artifact_version_id=version.id,
                method="INDEPENDENT_REVIEW",
            )
            .order_by(VerificationRecord.id.desc())
            .first()
        )
        outcome = (
            VerificationRecord.query.filter_by(
                work_id=work.id, artifact_version_id=version.id,
                method="INDEPENDENT_REVIEW_OUTCOME", status="FAILED",
            )
            .order_by(VerificationRecord.id.desc())
            .first()
        )
        if not host or not review or not outcome:
            continue
        host_details = dict(host.details_json or {})
        frozen_contract = dict((work.runtime_control_json or {}).get("acceptance_contract") or {})
        if (
            frozen_contract.get("contract_hash")
            and host_details.get("acceptance_contract_hash") != frozen_contract.get("contract_hash")
        ):
            continue
        if host_details.get("artifact_content_hash") not in {None, version.content_hash}:
            continue
        review_results = dict((review.details_json or {}).get("criterion_results") or {})
        if not review_results:
            continue
        statuses = {str((row or {}).get("status") or "UNPROVEN") for row in review_results.values()}
        if "FAILED" in statuses or not statuses.issubset({"UNPROVEN", "PASSED"}):
            continue
        return {
            "work_id": work.id,
            "artifact_version_id": version.id,
            "host_verification_id": host.id,
            "review_verification_id": review.id,
        }
    return None


def _reconcile_repaired_system_recovery(project: Project, operation: Operation, management: Work) -> bool:
    system_gates = [
        dict(row) for row in work_runtime.open_gates(management)
        if str(row.get("condition_type") or "").upper() == "SYSTEM_RECOVERY"
    ]
    if not system_gates:
        return False
    candidate = _latest_rejected_delivery_with_host_proof(project)
    if not candidate:
        return False
    signature = str((operation.memory_json or {}).get("continuation_failure_signature") or "")
    issue_code = _system_recovery_protocol_issue_code(signature)
    if not issue_code:
        return False
    exact_gate = next(
        (row for row in system_gates if str(row.get("issue_code") or "") == issue_code),
        None,
    )
    legacy_gate = None
    if exact_gate is None and len(system_gates) == 1 and _is_legacy_host_proof_recovery_gate(system_gates[0]):
        legacy_gate = system_gates[0]
    if exact_gate is None and legacy_gate is None:
        # Never let a repaired host-proof protocol clear an unrelated provider,
        # staffing, internal-exhaustion, or reconciliation recovery gate.
        return False
    control = dict(management.runtime_control_json or {})
    prior = dict(control.get("system_recovery_protocol_override") or {})
    if prior.get("protocol") == _SYSTEM_RECOVERY_PROTOCOL and prior.get("failure_signature") == signature:
        return False
    resolved = work_runtime.resolve_waits(
        management, "SYSTEM_RECOVERY",
        issue_code=(issue_code if exact_gate is not None else None),
        note=(
            "System recovery resumed after the host-proof/reviewer evidence handoff protocol was upgraded. "
            "This is one bounded Company retry, not new Founder authority."
        ),
    )
    if not resolved:
        return False
    control = dict(management.runtime_control_json or {})
    control["system_recovery_protocol_override"] = {
        "protocol": _SYSTEM_RECOVERY_PROTOCOL,
        "failure_signature": signature,
        "consumed": False,
        "source_work_id": candidate["work_id"],
        "source_artifact_version_id": candidate["artifact_version_id"],
        "source_host_verification_id": candidate["host_verification_id"],
        "created_at": now().isoformat(),
    }
    management.runtime_control_json = control
    if work_runtime.project_can_activate(project):
        project.status = "ACTIVE"
        project.current_state_summary = (
            "Company recovery resumed after repairing the durable host-proof to independent-review evidence handoff."
        )
        project.next_milestone = "Run one bounded recovery using the repaired verification protocol."
    emit(
        "PROJECT_SYSTEM_RECOVERY_PROTOCOL_REPAIRED", actor_type="RUNTIME",
        project_id=project.id, work_id=management.id, correlation_id=f"project:{project.id}",
        payload={"protocol": _SYSTEM_RECOVERY_PROTOCOL, "failure_signature": signature, **candidate},
    )
    return True


def _consume_system_recovery_protocol_override(management: Work, signature: str | None) -> bool:
    if not signature:
        return False
    control = dict(management.runtime_control_json or {})
    override = dict(control.get("system_recovery_protocol_override") or {})
    if (
        override.get("protocol") != _SYSTEM_RECOVERY_PROTOCOL
        or override.get("failure_signature") != signature
        or override.get("consumed") is True
    ):
        return False
    override["consumed"] = True
    override["consumed_at"] = now().isoformat()
    control["system_recovery_protocol_override"] = override
    management.runtime_control_json = control
    emit(
        "PROJECT_SYSTEM_RECOVERY_PROTOCOL_OVERRIDE_CONSUMED", actor_type="RUNTIME",
        project_id=management.project_id, work_id=management.id,
        correlation_id=f"project:{management.project_id}",
        payload={"protocol": _SYSTEM_RECOVERY_PROTOCOL, "failure_signature": signature},
    )
    return True


def is_kernel_operation(operation: Operation | None) -> bool:
    return __import__(
        "eason_one.services.core_v018", fromlist=["is_v018_operation"]
    ).is_v018_operation(operation)


def _compat_running(operation: Operation) -> None:
    if operation.status not in {"CANCELLED", "TERMINATED_BY_FOUNDER", "SUPERSEDED"}:
        operation.status = "RUNNING"
        operation.kernel_status = "RUNNING"
        operation.current_stage = "COMPANY_KERNEL_V020"
        operation.waiting_reason = None
        operation.lease_owner = None
        operation.lease_expires_at = None
        operation.ended_at = None


def _management_work(operation: Operation) -> Work:
    """Return a nonterminal management/control Work for Project-level actions.

    Historical v0.18 closure may have ACCEPTED/ABANDONED the original management
    Work. Reusing a terminal Work for new Project continuation would collapse
    Work and Execution truth, so v0.20 creates a fresh durable control Work.
    """
    rows = Work.query.filter_by(operation_id=operation.id, work_type="MANAGEMENT").order_by(Work.id.desc()).all()
    active = next((row for row in rows if row.state not in work_runtime.TERMINAL_WORK_STATES), None)
    if active:
        return active
    if not rows:
        return work_runtime.ensure_management_work(operation)
    return work_runtime.create_work(
        project_id=operation.project_id, operation_id=operation.id,
        title=f"Project control: {operation.title}",
        purpose="Evaluate durable Project evidence and sequence bounded continuation inside the immutable Founder Project Contract.",
        owner_employee_id=operation.proposed_by_employee_id,
        created_by_employee_id=operation.proposed_by_employee_id,
        expected_output="Project-level evidence review or bounded continuation plan",
        acceptance_criteria="Project control action is durably recorded without changing Founder authority.",
        priority="HIGH", work_type="MANAGEMENT",
        resource_ceiling_twd=operation.approved_budget_twd, state="EXECUTING",
        reason="v0.20 Project control Work after historical management Work became terminal.",
    )


def _ensure_serial_dependencies(operation: Operation) -> None:
    """Preserve serial ordering only for genuinely single-worker Operations.

    v0.20 previously chained every delivery Work here, which silently disabled
    the proven multi-Employee orchestration already present in Eason One.
    Multi-Employee Operations now wait for a Work-topology plan instead.
    """
    multi = __import__("eason_one.services.multi_agent", fromlist=["enabled"])
    if multi.enabled(operation):
        return
    delivery = [row for row in sorted(operation.works, key=lambda x: x.id) if row.work_type != "MANAGEMENT"]
    if len(delivery) < 2 or any(row.dependency_edges for row in delivery):
        return
    WorkDependency = __import__("eason_one.models", fromlist=["WorkDependency"]).WorkDependency
    for previous, current in zip(delivery, delivery[1:]):
        db.session.add(WorkDependency(work_id=current.id, depends_on_work_id=previous.id))


def adopt_operation(operation: Operation, *, commit=True) -> list[int]:
    if not is_kernel_operation(operation) or not operation.project_id:
        return []
    if operation.project.status == "PAUSED":
        # Startup adoption may materialize Work, bind Meetings and reconcile
        # staffing.  None of those durable mutations are allowed through an
        # explicit Founder pause.  Resume will make the existing operation
        # eligible for normal adoption without rebuilding the Project.
        return []
    __import__("eason_one.services.project_contract", fromlist=["get"]).get(operation.project)
    if not operation.works:
        plan = __import__("eason_one.services.operations", fromlist=["validate_plan"]).validate_plan(operation.plan_json)
        work_runtime.materialize_operation_works(operation, plan["operation"])
        db.session.flush()
        db.session.expire(operation, ["works", "tasks"])
    management = _management_work(operation)
    for meeting in Meeting.query.filter_by(operation_id=operation.id).all():
        if meeting.related_work_id != management.id:
            meeting.related_work_id = management.id
    _ensure_serial_dependencies(operation)
    team = __import__(
        "eason_one.services.team_formation", fromlist=["reconcile_work"]
    )
    for work in operation.works:
        if work.work_type != "MANAGEMENT" and work.state == "READY":
            team.reconcile_work(work)
    if operation.status not in MISSION_TERMINAL:
        governance = __import__(
            "eason_one.services.governance", fromlist=["attention"]
        )
        any_work_gate = any(work_runtime.has_open_gate(row) for row in operation.works)
        if not governance.attention(operation.project) and not any_work_gate:
            _compat_running(operation)
    # Adoption is process/restart maintenance, not a Project lifecycle writer.
    # Preserve REVIEW/BLOCKED/PLANNING exactly as durable business truth; only
    # refresh explanatory text when the Project was already ACTIVE.
    if operation.project.status == "ACTIVE":
        operation.project.current_state_summary = "Company Kernel is executing approved Work inside the Founder Project Contract."
    if commit:
        db.session.commit()
    return [row.id for row in operation.works]


def _operation_adoption_issue_code(operation: Operation) -> str:
    return f"OPERATION_ADOPTION_RECOVERY:{int(operation.id)}"


def _is_legacy_adoption_reconciliation_gate(gate: dict) -> bool:
    if str((gate or {}).get("condition_type") or "").upper() != "RECONCILIATION":
        return False
    if str((gate or {}).get("issue_code") or "").strip():
        return False
    reason = " ".join(str((gate or {}).get("reason") or "").casefold().split())
    return reason.startswith("company kernel adoption failed closed:")


def _resolve_repaired_adoption_gate(operation: Operation) -> bool:
    """Clear only the adoption gate owned by this exact approved Operation.

    Adoption is deterministic restart maintenance. If the same approved Operation
    now materializes successfully after a prior fail-closed startup, the old
    adoption reconciliation is obsolete. Other Project/Work reconciliation gates
    are independent truth and must remain untouched.
    """
    management = _management_work(operation)
    gates = [
        dict(row) for row in work_runtime.open_gates(management)
        if str(row.get("condition_type") or "").upper() == "RECONCILIATION"
    ]
    if not gates:
        return False
    issue = _operation_adoption_issue_code(operation)
    exact = next((row for row in gates if str(row.get("issue_code") or "") == issue), None)
    legacy = None
    if exact is None and len(gates) == 1 and _is_legacy_adoption_reconciliation_gate(gates[0]):
        legacy = gates[0]
    if exact is None and legacy is None:
        return False
    resolved = work_runtime.resolve_waits(
        management, "RECONCILIATION",
        issue_code=(issue if exact is not None else None),
        note=(
            "The same approved Operation now adopts cleanly after the prior fail-closed startup. "
            "Only its adoption reconciliation gate is retired."
        ),
    )
    if not resolved:
        return False
    project = operation.project
    if project is not None and work_runtime.project_can_activate(project):
        project.status = "ACTIVE"
        project.current_state_summary = (
            "Company Kernel repaired the prior startup-adoption fault and resumed the approved Mission."
        )
        project.next_milestone = operation.objective
    emit(
        "PROJECT_ADOPTION_RECOVERY_RESOLVED", actor_type="RUNTIME",
        project_id=operation.project_id, work_id=management.id,
        correlation_id=f"project:{operation.project_id}",
        payload={
            "operation_id": operation.id,
            "issue_code": issue,
            "legacy_unscoped_gate": bool(legacy is not None),
            "provider_call": False,
        },
    )
    return True


def adopt_approved_operations() -> list[int]:
    adopted = []
    dirty = False
    for operation in Operation.query.filter(Operation.approved_at.isnot(None)).order_by(Operation.id).all():
        if not is_kernel_operation(operation) or not operation.project:
            continue
        if operation.project.status in PROJECT_TERMINAL | {"PAUSED"}:
            continue
        if operation.status in MISSION_TERMINAL and operation.works:
            continue
        try:
            adopt_operation(operation, commit=False)
            _resolve_repaired_adoption_gate(operation)
        except ValueError as exc:
            # Startup adoption must fail closed *and visibly*. A corrupt/missing
            # governing Contract or invalid Work materialization is Company
            # recovery truth, not a silent scheduler skip and not Founder authority.
            project = operation.project
            project.status = "BLOCKED"
            project.current_state_summary = f"Company Kernel adoption failed closed: {exc}"
            project.next_milestone = "Repair/reconcile durable Project runtime truth before execution resumes."
            reason = f"Company Kernel adoption failed closed: {exc}"
            try:
                management = _management_work(operation)
                work_runtime.open_wait(
                    management, "RECONCILIATION", reason,
                    issue_code=_operation_adoption_issue_code(operation),
                )
            except Exception:
                # Project status/event still fail closed if even management Work
                # reconstruction is impossible; the startup transaction must
                # nevertheless persist that integrity failure.
                pass
            emit(
                "PROJECT_ADOPTION_FAILED_CLOSED", actor_type="RUNTIME",
                project_id=project.id, correlation_id=f"project:{project.id}",
                payload={"operation_id": operation.id, "reason": str(exc)[:1200]},
            )
            dirty = True
            continue
        adopted.append(operation.id)
        dirty = True
    if dirty:
        db.session.commit()
    return adopted


def _project_system_block(project: Project, reason: str, *, work=None, wait_type="SYSTEM_RECOVERY", issue_code=None) -> dict:
    """Block company execution without manufacturing Founder authority."""
    if work is not None:
        wait_kwargs = {}
        if issue_code:
            wait_kwargs["issue_code"] = str(issue_code)
        work_runtime.open_wait(work, wait_type, reason, **wait_kwargs)
    project.status = "BLOCKED"
    project.current_state_summary = str(reason or "Company recovery is required.")[:1400]
    project.next_milestone = "Company runtime must reconcile/recover inside the existing Founder Project Contract."
    emit(
        "PROJECT_INTERNAL_RECOVERY_REQUIRED", actor_type="RUNTIME",
        project_id=project.id, work_id=getattr(work, "id", None),
        correlation_id=f"project:{project.id}",
        payload={"reason": str(reason or "")[:1200], "wait_type": wait_type},
    )
    db.session.commit()
    return {
        "status": "RECONCILIATION_REQUIRED" if wait_type == "RECONCILIATION" else "SYSTEM_RECOVERY_REQUIRED",
        "project_id": project.id,
    }


def _open_project_gate(project: Project, reason: str, *, operation=None, work=None,
                       escalation_type: str, authority_payload: dict) -> dict:
    """Open only one precise gate owned by canonical Founder Governance."""
    governance = __import__(
        "eason_one.services.governance", fromlist=["open_gate"]
    )
    escalation = governance.open_gate(
        project=project, escalation_type=escalation_type, reason=reason,
        operation=operation, work=work,
        created_by_employee_id=getattr(getattr(project, "owner", None), "id", None),
        authority_payload=authority_payload,
        recommendation="Approve only the exact frozen authority change if intended.",
    )
    db.session.commit()
    return {"status": "NEEDS_FOUNDER", "project_id": project.id, "escalation_id": escalation.id}


def _budget_gate_from_run(project: Project, run, reason: str, *, operation=None, work=None) -> dict:
    authority = dict(((getattr(run, "context_composition_json", None) or {}).get("authority_failure") or {}))
    try:
        amount = Decimal(str(authority.get("additional_twd") or 0))
    except Exception:
        amount = Decimal("0")
    if str(authority.get("scope") or "PROJECT").upper() != "PROJECT" or amount <= 0:
        return _project_system_block(
            project,
            str(reason) + " Exact positive Project budget shortfall was not proven; no Founder gate was created.",
            work=work, wait_type="RECONCILIATION",
        )
    try:
        return _open_project_gate(
            project, reason, operation=operation, work=work,
            escalation_type="BUDGET_AUTHORIZATION",
            authority_payload={"additional_budget_twd": str(amount), "scope": "PROJECT"},
        )
    except __import__(
        "eason_one.services.governance", fromlist=["FounderAuthorityPreviouslyRejected"]
    ).FounderAuthorityPreviouslyRejected as exc:
        __import__(
            "eason_one.services.governance", fromlist=["apply_rejected_budget_boundary"]
        ).apply_rejected_budget_boundary(
            project=project, work=work, decision_id=exc.decision_id
        )
        db.session.commit()
        return {"status": "AUTHORITY_EXHAUSTED", "project_id": project.id, "decision_id": exc.decision_id}


def _topology_operation() -> Operation | None:
    """Return one governed multi-Employee Operation that still needs topology.

    Topology planning is management work, not Founder orchestration. It may only
    choose dependencies/parallelism among already-approved Work and assignees.
    """
    multi = __import__(
        "eason_one.services.multi_agent", fromlist=["enabled", "current_plan"]
    )
    for operation in Operation.query.filter(Operation.approved_at.isnot(None)).order_by(Operation.id).all():
        if not is_kernel_operation(operation) or not operation.project:
            continue
        if operation.status in MISSION_TERMINAL or operation.project.status not in ACTIVE_PROJECT_STATES:
            continue
        if not multi.enabled(operation) or multi.current_plan(operation):
            continue
        if work_runtime.project_hard_blockers(operation.project):
            continue
        return operation
    return None


def _unresolved_orchestration_attempt(operation: Operation, *, after_run_id: int = 0) -> AgentRun | None:
    """Return topology planning effect truth that forbids another paid call.

    A prior valid topology result is reusable only when no later orchestration
    attempt has unresolved post-dispatch truth.  This mirrors Work/Project
    recovery semantics: an older success can satisfy the business need, but it
    cannot erase a newer call that may also have spent money.
    """
    effects = __import__(
        "eason_one.models", fromlist=["ExternalEffectAttempt"]
    ).ExternalEffectAttempt
    rows = (
        AgentRun.query.filter_by(operation_id=operation.id, purpose="ORCHESTRATION_PLAN")
        .filter(AgentRun.id > int(after_run_id or 0))
        .order_by(AgentRun.id.asc()).all()
    )
    for run in rows:
        if run.status == "RUNNING" or str(run.outcome or "") == "FAILED_AMBIGUOUS":
            return run
        effect = (
            effects.query.filter_by(execution_id=run.id)
            .order_by(effects.id.desc()).first()
        )
        if effect is not None and str(effect.state or "").upper() in {
            "DISPATCHING", "RESPONSE_RECEIVED", "AMBIGUOUS_POST_DISPATCH",
        }:
            return run
    return None


def _block_unresolved_orchestration(operation: Operation, run: AgentRun) -> dict:
    management = work_runtime.ensure_management_work(operation)
    reason = (
        getattr(run, "error_text", None)
        or f"Topology planning Agent Run #{run.id} has unresolved provider-dispatch truth; automatic replay is forbidden."
    )
    issue_code = f"ORCHESTRATION_RUN_{run.id}_EFFECT_UNRESOLVED"
    work_runtime.open_wait(
        management, "RECONCILIATION", reason, issue_code=issue_code
    )
    if operation.project and operation.project.status not in PROJECT_TERMINAL:
        operation.project.status = "BLOCKED"
        operation.project.current_state_summary = (
            "Company topology planning is paused until one unresolved provider effect is reconciled; no duplicate planning call will be made."
        )
    db.session.commit()
    return {
        "status": "TOPOLOGY_RECONCILIATION_REQUIRED",
        "operation_id": operation.id,
        "run_id": run.id,
        "provider_replayed": False,
    }


def _plan_work_topology(operation: Operation) -> dict:
    """Reuse the existing bounded multi-agent planner as v0.20 Work topology.

    The planner cannot create/remove/reassign Work. Its only authority is to
    persist WorkDependency edges and bounded parallelism for approved Works.
    """
    multi = __import__(
        "eason_one.services.multi_agent",
        fromlist=["call_orchestrator", "conservative_fallback", "current_plan", "persist_plan", "validate_payload"],
    )
    existing = multi.current_plan(operation)
    if existing:
        return {"status": "TOPOLOGY_EXISTS", "operation_id": operation.id, "strategy": existing.get("strategy")}

    # Crash-safe reuse: if the provider result was already durably stored but
    # process death happened before dependency projection, consume that result
    # instead of paying for the same planning call again.
    prior = AgentRun.query.filter_by(
        operation_id=operation.id, purpose="ORCHESTRATION_PLAN", status="SUCCEEDED"
    ).order_by(AgentRun.id.desc()).first()
    if prior and prior.parsed_output_json:
        unresolved_after = _unresolved_orchestration_attempt(operation, after_run_id=prior.id)
        if unresolved_after is not None:
            return _block_unresolved_orchestration(operation, unresolved_after)
        try:
            plan = multi.validate_payload(operation, dict(prior.parsed_output_json))
            multi.persist_plan(operation, plan, planner_run_id=prior.id)
            return {
                "status": "TOPOLOGY_PLANNED", "operation_id": operation.id,
                "strategy": plan.get("strategy"), "max_parallelism": plan.get("max_parallelism"),
                "planner_run_id": prior.id, "reused": True,
            }
        except Exception:
            db.session.rollback()
            operation = db.session.get(Operation, operation.id)

    unresolved = _unresolved_orchestration_attempt(operation)
    if unresolved is not None:
        return _block_unresolved_orchestration(operation, unresolved)

    # Crash-safe failure projection: normal same-process behavior falls back to a
    # deterministic dependency graph after one known/safe orchestrator failure.
    # A restart after that failure was durably recorded must not purchase a second
    # topology call merely because the caller died before persisting the fallback.
    latest_known_failure = (
        AgentRun.query.filter_by(operation_id=operation.id, purpose="ORCHESTRATION_PLAN", status="FAILED")
        .order_by(AgentRun.id.desc()).first()
    )
    if latest_known_failure is not None and str(latest_known_failure.outcome or "") in {"FAILED_SAFE", "FAILED_KNOWN"}:
        reason = (
            latest_known_failure.error_text
            or latest_known_failure.failure_reason
            or "A prior durable topology attempt failed without ambiguous external effect."
        )
        plan = multi.conservative_fallback(operation, reason)
        multi.persist_plan(operation, plan, planner_run_id=None)
        return {
            "status": "TOPOLOGY_CONSERVATIVE_FALLBACK", "operation_id": operation.id,
            "strategy": plan.get("strategy"), "max_parallelism": plan.get("max_parallelism"),
            "error": reason, "source_failed_run_id": latest_known_failure.id,
            "provider_replayed": False,
        }

    run = None
    try:
        run = multi.call_orchestrator(operation)
        if run.status == "SUCCEEDED" and run.parsed_output_json:
            plan = multi.validate_payload(operation, dict(run.parsed_output_json))
            multi.persist_plan(operation, plan, planner_run_id=run.id)
            return {
                "status": "TOPOLOGY_PLANNED", "operation_id": operation.id,
                "strategy": plan.get("strategy"), "max_parallelism": plan.get("max_parallelism"),
                "planner_run_id": run.id, "reused": False,
            }
        reason = getattr(run, "error_text", None) or getattr(run, "failure_reason", None) or "Topology planner did not return a usable result."
    except Exception as exc:
        db.session.rollback()
        operation = db.session.get(Operation, operation.id)
        reason = str(exc)

    # Failure of the optional topology model must not collapse the Company back
    # into a fake single-agent queue.  Use only explicit approved dependency
    # signals; never interpret Task order or a provisional CEO owner as dataflow.
    plan = multi.conservative_fallback(operation, reason)
    multi.persist_plan(operation, plan, planner_run_id=None)
    return {
        "status": "TOPOLOGY_CONSERVATIVE_FALLBACK", "operation_id": operation.id,
        "strategy": plan.get("strategy"), "max_parallelism": plan.get("max_parallelism"), "error": reason,
    }


def _work_owner_id(work: Work) -> int | None:
    assignment = work_runtime.active_assignment(work)
    return getattr(assignment, "employee_id", None)


def _dispatch_actor_id(work: Work) -> int | None:
    """Return the Persistent Employee who will perform the *next* action.

    READY/EXECUTING Work is performed by its accountable assignee. VERIFYING
    Work is different: semantic review is performed by the frozen independent
    reviewer, not by the Artifact producer. Parallel-wave ownership must use
    the actual next actor or the scheduler can accidentally run two actions for
    the same Critic/Reviewer concurrently while believing they belong to two
    different Employees.
    """
    if work.state == "VERIFYING":
        control = dict(getattr(work, "runtime_control_json", None) or {})
        frozen = dict(control.get("acceptance_contract") or {})
        if frozen:
            reviewer_id = frozen.get("reviewer_employee_id")
            return int(reviewer_id) if reviewer_id is not None else None
    return _work_owner_id(work)


def _eligible_works(max_parallelism: int = 4) -> list[Work]:
    """Select a bounded wave of independent governed Work.

    Different Persistent Employees may work concurrently. One Employee never
    owns two Works in the same wave, and repository-mutating Engineer/Codex Work
    remains single-writer globally. WorkDependency is the dependency truth.
    """
    multi = __import__(
        "eason_one.services.multi_agent", fromlist=["enabled", "current_plan"]
    )
    candidates = Work.query.filter(
        Work.state.in_(["VERIFYING", "READY", "EXECUTING"])
    ).order_by(Work.id).all()
    # Close verification quickly while still allowing unrelated employees to
    # progress in the same company tick.
    candidates.sort(key=lambda row: (0 if row.state == "VERIFYING" else 1, row.id))
    selected: list[Work] = []
    owners: set[int] = set()
    engineer_selected = False
    per_operation: dict[int, int] = {}
    max_parallelism = max(1, min(int(max_parallelism or 1), 4))
    running_employee_ids = {
        int(employee_id) for (employee_id,) in db.session.query(AgentRun.employee_id).filter(
            AgentRun.status == "RUNNING"
        ).distinct().all()
    }

    for work in candidates:
        if len(selected) >= max_parallelism:
            break
        if work.work_type == "MANAGEMENT" or not is_kernel_operation(work.operation):
            continue
        operation = work.operation
        if multi.enabled(operation) and not multi.current_plan(operation):
            # Topology is management authority. Delivery cannot race ahead of it.
            continue
        if not work.project or work.project.status not in ACTIVE_PROJECT_STATES or work.project.status == "REVIEW":
            continue
        if work_runtime.has_open_gate(work):
            continue
        team = __import__(
            "eason_one.services.team_formation", fromlist=["execution_ready"]
        )
        if not team.execution_ready(work):
            continue
        if work_runtime.project_hard_blockers(work.project, include_founder=False):
            continue
        governance = __import__("eason_one.services.governance", fromlist=["blocks_work"])
        if governance.blocks_work(work):
            continue
        if work.state in {"READY", "EXECUTING"} and not work_runtime.dependencies_satisfied(work):
            continue
        if AgentRun.query.filter_by(work_id=work.id, status="RUNNING").count():
            continue

        owner_id = _dispatch_actor_id(work)
        if owner_id is not None and owner_id in running_employee_ids:
            continue
        if owner_id is not None and work.work_type != "MANAGEMENT":
            capacity = __import__(
                "eason_one.services.ceo_operating", fromlist=["employee_capacity_view"]
            ).employee_capacity_view(owner_id)
            if capacity.get("assignment_conflict") or (
                capacity.get("active_project_id") is not None
                and int(capacity["active_project_id"]) != int(work.project_id)
            ):
                continue
        if owner_id is not None and owner_id in owners:
            continue
        owner = db.session.get(Employee, owner_id) if owner_id else None
        is_engineer = bool(owner and owner.slug == "engineer")
        if is_engineer and engineer_selected:
            continue

        plan = multi.current_plan(operation) if multi.enabled(operation) else None
        operation_cap = int((plan or {}).get("max_parallelism") or 1)
        if per_operation.get(operation.id, 0) >= operation_cap:
            continue

        selected.append(work)
        per_operation[operation.id] = per_operation.get(operation.id, 0) + 1
        if owner_id is not None:
            owners.add(owner_id)
        if is_engineer:
            engineer_selected = True
    return selected


def _eligible_work() -> Work | None:
    rows = _eligible_works(max_parallelism=1)
    return rows[0] if rows else None


def _dispatch_work(work: Work) -> dict:
    work_id = work.id
    try:
        result = (
            __import__("eason_one.services.work_execution", fromlist=["review_work"]).review_work(work)
            if work.state == "VERIFYING"
            else __import__("eason_one.services.work_execution", fromlist=["execute_work"]).execute_work(work)
        )
    except Exception as exc:
        db.session.rollback()
        work = db.session.get(Work, work_id)
        if not work or work.state in work_runtime.TERMINAL_WORK_STATES:
            return {"status": "RUNTIME_EXCEPTION", "work_id": work_id}
        failure_signature = _work_runtime_failure_signature(exc)
        event_model = __import__("eason_one.models", fromlist=["CompanyEvent"]).CompanyEvent
        prior = 0
        history = event_model.query.filter_by(
            event_type="WORK_RUNTIME_EXCEPTION", work_id=work.id
        ).all()
        total_prior = len(history)
        for row in history:
            payload = dict(getattr(row, "payload_json", None) or {})
            prior_signature = str(payload.get("failure_signature") or "")
            if not prior_signature:
                legacy_error = str(payload.get("error") or "")
                legacy_type = str(payload.get("error_type") or "Exception")
                normalized = " ".join(legacy_error.strip().split())
                prior_signature = hashlib.sha256(
                    f"{legacy_type}|{normalized}".encode("utf-8")
                ).hexdigest()[:20]
            if prior_signature == failure_signature:
                prior += 1
        if prior < 3 and total_prior < 12:
            delay = [5, 15, 30][prior]
            issue_code = f"{_WORK_RUNTIME_EXCEPTION_GATE_PREFIX}{failure_signature}"
            work_runtime.open_wait(
                work, "INTERNAL_RECOVERY",
                f"Company Kernel fault: {type(exc).__name__}: {exc}",
                retry_after=now() + timedelta(seconds=delay),
                issue_code=issue_code,
            )
            status = "RETRY_SCHEDULED"
        else:
            if prior >= 3:
                issue_code = f"{_WORK_RUNTIME_EXCEPTION_EXHAUSTED_PREFIX}{failure_signature}"
                prefix = "Company Kernel bounded recovery exhausted after three retries for the same internal failure. "
            else:
                issue_code = f"WORK_RUNTIME_EXCEPTION_GLOBAL_EXHAUSTED:{int(work.id)}"
                prefix = "Company Kernel reached the global internal-exception anomaly ceiling across distinct failures. "
            work_runtime.open_wait(
                work, "SYSTEM_RECOVERY",
                (
                    prefix
                    + f"Repair/reconcile this exact runtime path before resuming: {type(exc).__name__}: {exc}"
                ),
                issue_code=issue_code,
            )
            if work.project is not None:
                work.project.status = "BLOCKED"
                work.project.current_state_summary = (
                    "Company hit a repeated internal runtime fault and stopped automatic retry without abandoning the Work."
                )
            status = "SYSTEM_RECOVERY_REQUIRED"
        emit(
            "WORK_RUNTIME_EXCEPTION", actor_type="RUNTIME", project_id=work.project_id,
            work_id=work.id, correlation_id=f"work:{work.id}",
            payload={
                "error_type": type(exc).__name__, "error": str(exc)[:1200],
                "failure_signature": failure_signature, "attempt": prior + 1,
                "issue_code": issue_code,
                "work_abandoned": False,
            },
        )
        db.session.commit()
        result = {"status": status, "work_id": work.id}
    if result.get("status") == "ACCEPTED" and work.project:
        work.project.current_state_summary = f"{work.title} is accepted. Company Kernel is continuing the Project."
        db.session.commit()
    return result


def _dispatch_work_thread(app, work_id: int) -> dict:
    with app.app_context():
        try:
            work = db.session.get(Work, work_id)
            if not work:
                return {"status": "MISSING", "work_id": work_id}
            return _dispatch_work(work)
        finally:
            db.session.remove()


def _dispatch_work_wave(works: list[Work]) -> dict:
    """Run a bounded concurrent wave across independent Persistent Employees."""
    if len(works) < 2:
        raise ValueError("Work wave requires at least two eligible Works")
    app = current_app._get_current_object()
    work_ids = [work.id for work in works]
    project_ids = sorted({work.project_id for work in works})
    emit(
        "COMPANY_WORK_WAVE_STARTED", actor_type="RUNTIME",
        correlation_id=f"work-wave:{min(work_ids)}",
        payload={"work_ids": work_ids, "project_ids": project_ids, "parallelism": len(work_ids)},
    )
    db.session.commit()
    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=len(work_ids), thread_name_prefix="eason-work") as pool:
        futures = {pool.submit(_dispatch_work_thread, app, work_id): work_id for work_id in work_ids}
        for future in as_completed(futures):
            work_id = futures[future]
            try:
                results.append(future.result())
            except Exception as exc:
                results.append({"status": "RUNTIME_EXCEPTION", "work_id": work_id, "error": str(exc)})
    db.session.expire_all()
    emit(
        "COMPANY_WORK_WAVE_COMPLETED", actor_type="RUNTIME",
        correlation_id=f"work-wave:{min(work_ids)}",
        payload={
            "work_ids": work_ids,
            "results": [{"work_id": row.get("work_id"), "status": row.get("status")} for row in results],
        },
    )
    db.session.commit()
    results.sort(key=lambda row: int(row.get("work_id") or 0))
    return {"status": "WORK_WAVE_COMPLETED", "work_ids": work_ids, "results": results}


def _hiring_request_due() -> HiringRequest | None:
    """Return one current Project staffing request that Company HR can advance.

    Retry/backoff is reconstructed from request-tagged AgentRuns, so process
    restart cannot turn a failed HR call into an immediate paid retry loop.
    """
    workforce = __import__(
        "eason_one.services.workforce",
        fromlist=["assessment_retry_state"],
    )
    rows = (
        HiringRequest.query
        .filter(HiringRequest.status.in_(["REQUESTED", "HR_REVIEW"]))
        .order_by(HiringRequest.id)
        .all()
    )
    current = now()
    for request in rows:
        project = db.session.get(Project, request.project_id) if request.project_id else None
        operation = db.session.get(Operation, request.operation_id) if request.operation_id else None
        if project is not None:
            if project.environment != "LIVE" or project.status not in {"ACTIVE", "PLANNING", "BLOCKED"}:
                continue
            if operation is not None and not is_kernel_operation(operation):
                continue
            # Founder/project-level authority or management recovery owns the
            # pause. HR must not spend through that global boundary.
            if work_runtime.project_hard_blockers(project):
                continue
        retry = workforce.assessment_retry_state(request, current=current)
        if retry["state"] in {
            "BACKOFF", "IN_PROGRESS", "RECONCILIATION", "SYSTEM_RECOVERY", "EXHAUSTED"
        }:
            continue
        return request
    return None


def _hiring_recovery_work(request: HiringRequest):
    workforce = __import__(
        "eason_one.services.workforce", fromlist=["source_work_for_request"]
    )
    source = workforce.source_work_for_request(request)
    if source is not None:
        return source
    operation = db.session.get(Operation, request.operation_id) if request.operation_id else None
    return _management_work(operation) if operation is not None else None


def _advance_hiring(request: HiringRequest) -> dict:
    """Advance one governed staffing request without unbounded provider spend."""
    request_id = request.id
    workforce = __import__(
        "eason_one.services.workforce",
        fromlist=["assess_request", "assessment_retry_state"],
    )
    try:
        run = workforce.assess_request(request)
    except Exception as exc:
        db.session.rollback()
        request = db.session.get(HiringRequest, request_id)
        retry = workforce.assessment_retry_state(request, current=now()) if request else {
            "state": "EXHAUSTED", "attempts": 0, "last_run": None, "retry_after": None
        }
        last_run = retry.get("last_run")
        project = db.session.get(Project, request.project_id) if request and request.project_id else None
        operation = db.session.get(Operation, request.operation_id) if request and request.operation_id else None
        recovery_work = _hiring_recovery_work(request) if request else None

        # If the paid HR model response itself succeeded, this exception came
        # from deterministic local projection/validation (placement, model
        # policy, schema-derived staffing truth, etc.). Replaying the provider
        # cannot repair local code/config truth and would create an endless
        # scheduler loop around the same paid response. Keep the exact response,
        # stop only this staffing branch, and route it to Company system recovery.
        if request is not None and last_run is not None and last_run.status == "SUCCEEDED":
            request.status = "SYSTEM_RECOVERY"
            if recovery_work is not None:
                work_runtime.open_wait(
                    recovery_work, "SYSTEM_RECOVERY",
                    f"HiringRequest #{request_id} has a paid HR result that could not be projected into lawful staffing truth; repair local staffing/configuration without replaying the provider.",
                    issue_code=f"HIRING_REQUEST_{request_id}_LOCAL_PROJECTION",
                )
            emit(
                "HIRING_ASSESSMENT_LOCAL_PROJECTION_RECOVERY_REQUIRED", actor_type="RUNTIME",
                project_id=getattr(request, "project_id", None),
                work_id=getattr(recovery_work, "id", None),
                correlation_id=(f"project:{request.project_id}" if request.project_id else f"hiring:{request_id}"),
                payload={
                    "hiring_request_id": request_id,
                    "hr_run_id": last_run.id,
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:1200],
                    "provider_replayed": False,
                },
            )
            db.session.commit()
            return {"status": "HIRING_SYSTEM_RECOVERY_REQUIRED", "hiring_request_id": request_id}

        # A proven positive Project budget shortfall is Founder authority, not
        # a retryable HR/provider failure. Reuse the canonical budget gate.
        if (project is not None and last_run is not None
                and getattr(last_run, "failure_reason", None) == "FOUNDER_BUDGET_EXTENSION_REQUIRED"):
            result = _budget_gate_from_run(
                project, last_run,
                last_run.error_text or "HR assessment requires additional Project budget.",
                operation=operation, work=recovery_work,
            )
            result.update({"hiring_request_id": request_id, "hr_run_id": last_run.id})
            return result

        if request is not None and retry.get("state") == "RECONCILIATION":
            request.status = "SYSTEM_RECOVERY"
            if recovery_work is not None:
                work_runtime.open_wait(
                    recovery_work, "RECONCILIATION",
                    f"HiringRequest #{request_id} HR provider attempt has ambiguous post-dispatch truth; reconcile before retry.",
                    issue_code=f"HIRING_REQUEST_{request_id}_HR_ASSESSMENT",
                )
            emit(
                "HIRING_ASSESSMENT_RECONCILIATION_REQUIRED", actor_type="RUNTIME",
                project_id=getattr(request, "project_id", None),
                work_id=getattr(recovery_work, "id", None),
                correlation_id=(f"project:{request.project_id}" if request.project_id else f"hiring:{request_id}"),
                payload={"hiring_request_id": request_id, "hr_run_id": getattr(last_run, "id", None)},
            )
            db.session.commit()
            return {"status": "HIRING_RECONCILIATION_REQUIRED", "hiring_request_id": request_id}

        if request is not None and retry.get("state") == "SYSTEM_RECOVERY":
            request.status = "SYSTEM_RECOVERY"
            if recovery_work is not None:
                work_runtime.open_wait(
                    recovery_work, "SYSTEM_RECOVERY",
                    f"HiringRequest #{request_id} provider request was definitively rejected; repair provider/configuration before retry.",
                    issue_code=f"HIRING_REQUEST_{request_id}_PROVIDER_RECOVERY",
                )
            emit(
                "HIRING_ASSESSMENT_SYSTEM_RECOVERY_REQUIRED", actor_type="RUNTIME",
                project_id=getattr(request, "project_id", None),
                work_id=getattr(recovery_work, "id", None),
                correlation_id=(f"project:{request.project_id}" if request.project_id else f"hiring:{request_id}"),
                payload={"hiring_request_id": request_id, "hr_run_id": getattr(last_run, "id", None), "failure_reason": getattr(last_run, "failure_reason", None)},
            )
            db.session.commit()
            return {"status": "HIRING_SYSTEM_RECOVERY_REQUIRED", "hiring_request_id": request_id}

        if request is not None and retry.get("state") == "EXHAUSTED":
            request.status = "SYSTEM_RECOVERY"
            if recovery_work is not None:
                work_runtime.open_wait(
                    recovery_work, "SYSTEM_RECOVERY",
                    f"HiringRequest #{request_id} exhausted bounded HR assessment attempts; repair provider/system execution before resuming staffing.",
                    issue_code=f"HIRING_REQUEST_{request_id}_RECOVERY_EXHAUSTED",
                )
            emit(
                "HIRING_ASSESSMENT_RECOVERY_EXHAUSTED", actor_type="RUNTIME",
                project_id=getattr(request, "project_id", None),
                work_id=getattr(recovery_work, "id", None),
                correlation_id=(f"project:{request.project_id}" if request.project_id else f"hiring:{request_id}"),
                payload={"hiring_request_id": request_id, "attempts": retry.get("attempts", 0), "hr_run_id": getattr(last_run, "id", None)},
            )
            db.session.commit()
            return {"status": "HIRING_SYSTEM_RECOVERY_REQUIRED", "hiring_request_id": request_id}

        # Safe/known failures remain company-owned and are retried only after a
        # bounded durable backoff. No Founder question is manufactured.
        emit(
            "HIRING_ASSESSMENT_RETRY_SCHEDULED", actor_type="RUNTIME",
            project_id=getattr(request, "project_id", None),
            work_id=getattr(recovery_work, "id", None),
            correlation_id=(f"project:{request.project_id}" if request and request.project_id else f"hiring:{request_id}"),
            payload={
                "hiring_request_id": request_id,
                "attempt": retry.get("attempts", 0),
                "retry_after": retry.get("retry_after").isoformat() if retry.get("retry_after") else None,
                "hr_run_id": getattr(last_run, "id", None),
                "error_type": type(exc).__name__,
                "error": str(exc)[:1200],
            },
        )
        db.session.commit()
        return {
            "status": "HIRING_RETRY_SCHEDULED",
            "hiring_request_id": request_id,
            "attempt": retry.get("attempts", 0),
            "retry_after": retry.get("retry_after").isoformat() if retry.get("retry_after") else None,
        }

    db.session.expire_all()
    request = db.session.get(HiringRequest, request_id)
    if request and request.status == "HIRED":
        return {
            "status": "EMPLOYEE_HIRED",
            "hiring_request_id": request.id,
            "employee_id": request.created_employee_id,
            "hr_run_id": getattr(run, "id", None),
        }
    if request and request.status == "FOUNDER_REVIEW":
        return {
            "status": "HIRING_FOUNDER_REVIEW",
            "hiring_request_id": request.id,
            "hr_run_id": getattr(run, "id", None),
        }
    return {
        "status": "HIRING_ASSESSED",
        "hiring_request_id": request_id,
        "request_status": getattr(request, "status", None),
        "hr_run_id": getattr(run, "id", None),
    }


def _delivery(operation: Operation) -> list[Work]:
    return [row for row in sorted(operation.works, key=lambda x: x.id) if row.work_type != "MANAGEMENT"]


def _mission_terminal(operation: Operation) -> bool:
    rows = _delivery(operation)
    return bool(rows) and all(row.state in DELIVERY_TERMINAL for row in rows)


def _mission_has_failure(operation: Operation) -> bool:
    # A bounded Mission succeeds only when every required delivery Work is
    # ACCEPTED. CANCELLED is terminal history, but it is not delivery success
    # (for example Founder declined extra budget for that exact Work).
    return any(row.state in {"ABANDONED", "CANCELLED"} for row in _delivery(operation))


def _meeting_gate(operation: Operation) -> dict | None:
    # Meeting coordination has a current v0.20 owner. Normal Company Kernel
    # progression must never execute business functions from the legacy runtime.
    # The coordination owner decides whether the approved trigger is due; an
    # ON_MATERIAL_CONFLICT room may legitimately run before all delivery closes.
    coordination = __import__(
        "eason_one.services.meeting_coordination",
        fromlist=["advance_meeting_gate", "operation_meeting"],
    )
    meeting = coordination.operation_meeting(operation)
    if meeting is None:
        return None
    return coordination.advance_meeting_gate(operation)


def _mark_mission_failed(operation: Operation) -> dict:
    failed = next((row for row in _delivery(operation) if row.state in {"ABANDONED", "CANCELLED"}), None)
    for row in _delivery(operation):
        if row.id == getattr(failed, "id", None) or row.state in DELIVERY_TERMINAL:
            continue
        work_runtime.transition(
            row, "CANCELLED", actor_type="RUNTIME",
            reason="Cancelled because an earlier Work in this bounded Mission failed; CEO will replan at Project level.",
        )
        work_runtime.sync_task_projection(row)
    operation.status = "FAILED"
    operation.kernel_status = "FAILED"
    operation.current_stage = "MISSION_FAILED_PROJECT_CONTINUES"
    operation.ended_at = operation.ended_at or now()
    operation.waiting_reason = None
    operation.lease_owner = None
    operation.lease_expires_at = None
    reason = "Bounded Mission recovery exhausted. This is failure evidence, not Project failure."
    failure_mode = "GENERAL"
    if failed:
        run = AgentRun.query.filter_by(work_id=failed.id).order_by(AgentRun.id.desc()).first()
        if getattr(run, "resolution_status", None) == "EVIDENCE_REVIEW_EXHAUSTED":
            failure_mode = "EVIDENCE_ONLY"
            reason = getattr(run, "resolution_note", None) or reason
        else:
            reason = (
                getattr(run, "resolution_note", None)
                or getattr(run, "error_text", None)
                or getattr(run, "failure_reason", None)
                or reason
            )
    memory = dict(operation.memory_json or {})
    memory["continuation_failure_mode"] = failure_mode
    memory["continuation_failure_reason"] = str(reason)[:4000]
    memory["continuation_failed_work_id"] = getattr(failed, "id", None)
    operation.memory_json = memory
    if operation.project and operation.project.status not in PROJECT_TERMINAL:
        if work_runtime.project_can_activate(operation.project):
            operation.project.status = "ACTIVE"
            operation.project.current_state_summary = (
                "A bounded Mission exhausted semantic evidence review; company continuation must preserve the implementation and reconcile evidence only."
                if failure_mode == "EVIDENCE_ONLY"
                else "A bounded Mission failed; CEO is replanning within the existing Founder Project Contract."
            )
            operation.project.next_milestone = (
                "Collect/reconcile missing evidence without replaying the implementation."
                if failure_mode == "EVIDENCE_ONLY"
                else "Choose a different bounded approach without expanding Founder authority."
            )
    emit(
        "MISSION_FAILED", actor_type="RUNTIME", project_id=operation.project_id,
        work_id=getattr(failed, "id", None), correlation_id=f"project:{operation.project_id}",
        payload={
            "operation_id": operation.id, "reason": str(reason)[:1200],
            "project_failed": False, "failure_mode": failure_mode,
            "failed_work_id": getattr(failed, "id", None),
        },
    )
    db.session.commit()
    return {
        "status": "MISSION_FAILED", "operation_id": operation.id,
        "work_id": getattr(failed, "id", None), "failure_mode": failure_mode,
    }


def _mark_mission_complete(operation: Operation) -> dict:
    if operation.status != "COMPLETED":
        operation.status = "COMPLETED"
        operation.kernel_status = "COMPLETED"
        operation.current_stage = "MISSION_EVIDENCE_ACCEPTED"
        operation.ended_at = operation.ended_at or now()
        operation.waiting_reason = None
        emit(
            "MISSION_COMPLETED", actor_type="RUNTIME", project_id=operation.project_id,
            correlation_id=f"project:{operation.project_id}",
            payload={
                "operation_id": operation.id,
                "accepted_work_ids": [row.id for row in _delivery(operation) if row.state == "ACCEPTED"],
                "project_completed": False,
            },
        )
        db.session.commit()
    return {"status": "MISSION_COMPLETED", "operation_id": operation.id}


def _latest_operation(project: Project) -> Operation | None:
    # A pending delegated continuation can become stale before approval when
    # accepted evidence or Founder Project authority changes. Such a Mission is
    # retained as SUPERSEDED audit history but is no longer current sequencing
    # truth; Project advancement must continue from the latest non-superseded
    # Mission instead of repeatedly trying to revive the obsolete plan.
    return (
        Operation.query.filter_by(project_id=project.id)
        .filter(Operation.status != "SUPERSEDED")
        .order_by(Operation.id.desc())
        .first()
    )


def _has_active_delivery(project: Project) -> bool:
    """Whether any delivery branch can still make progress inside this Mission.

    A READY/WAITING downstream Work whose required upstream branch is already
    ABANDONED/CANCELLED is not active progress.  Treating it as active used to
    deadlock Project continuation forever after bounded recovery exhausted: no
    Work was runnable, yet Project sequencing refused to close the failed
    Mission because blocked descendants still existed.
    """
    rows = Work.query.filter(
        Work.project_id == project.id,
        Work.work_type != "MANAGEMENT",
        Work.state.in_(["PROPOSED", "READY", "EXECUTING", "WAITING", "VERIFYING"]),
    ).order_by(Work.id).all()
    for work in rows:
        if work.state in {"PROPOSED", "EXECUTING", "VERIFYING"}:
            return True
        if work.state == "READY":
            # READY is real progress only if its dependency graph is currently
            # satisfiable. Another READY/EXECUTING upstream branch is counted by
            # its own row; a failed upstream must not keep this Project alive.
            edges = WorkDependency.query.filter_by(work_id=work.id).all()
            failed_dependency = False
            for edge in edges:
                upstream = db.session.get(Work, edge.depends_on_work_id)
                if upstream is None or upstream.state in {"ABANDONED", "CANCELLED"}:
                    failed_dependency = True
                    break
            if not failed_dependency:
                return True
        if work.state == "WAITING":
            gates = work_runtime.open_gates(work)
            if not gates:
                return True
            # Internal retry/reconciliation/Founder authority is still a live
            # branch. A pure dependency wait is live only while at least one
            # governed upstream dependency can still satisfy the gate. vNext
            # DEPENDENCY waits are sometimes opened without one target_work_id,
            # so the durable WorkDependency graph is authoritative here.
            non_dependency = [gate for gate in gates if gate.get("condition_type") != "DEPENDENCY"]
            if non_dependency:
                return True
            edges = WorkDependency.query.filter_by(work_id=work.id).all()
            if not edges:
                # No durable dependency edge means this is an orphaned wait that
                # recovery owns; do not let it masquerade as delivery progress.
                continue
            upstream_rows = [db.session.get(Work, edge.depends_on_work_id) for edge in edges]
            if any(
                upstream is not None and upstream.state not in {"ABANDONED", "CANCELLED"}
                for upstream in upstream_rows
            ):
                return True
    return False




def _matching_attempts(project_id: int, purpose: str, key: str, value: str) -> list[AgentRun]:
    rows = AgentRun.query.filter_by(project_id=project_id, purpose=purpose).order_by(AgentRun.id).all()
    return [row for row in rows if (row.context_composition_json or {}).get(key) == value]


_PROJECT_MANAGEMENT_PROVIDER_FAILURES = {"PROVIDER_REQUEST_REJECTED", "PROVIDER_PREFLIGHT_FAILED"}


def _project_management_recovery_candidate(employee, operation, failed_run, *, purpose: str, fingerprint_key: str, fingerprint: str):
    """Return one untried lawful model for a replay-safe Project management fault.

    Project outcome review and continuation planning are Company management calls.
    A definitive/pre-dispatch provider fault may use another governed ModelConfig,
    but ambiguous external effects, Founder authority failures, and already-tried
    models never become a back door for additional spend.
    """
    if failed_run is None or str(getattr(failed_run, "failure_reason", "") or "") not in _PROJECT_MANAGEMENT_PROVIDER_FAILURES:
        return None
    if str(getattr(failed_run, "outcome", "") or "") not in {"FAILED_SAFE", "FAILED_KNOWN"}:
        return None
    replay_ok, _reason = __import__(
        "eason_one.services.external_effects", fromlist=["retry_authorized"]
    ).retry_authorized(failed_run)
    if not replay_ok:
        return None
    attempts = _matching_attempts(
        int(getattr(failed_run, "project_id", 0) or 0), purpose, fingerprint_key, fingerprint
    )
    # The per-fingerprint Project-management cap is authority over spend, not
    # merely a timer/recovery concern. Enforce it at the candidate selector too
    # so normal sequencing can never discover a newly configured fourth model
    # and dispatch a fourth paid attempt after three durable attempts already
    # exist for the same exact management input.
    if len(attempts) >= 3:
        return None
    excluded = {
        int(row.model_config_id) for row in attempts if getattr(row, "model_config_id", None) is not None
    }
    return __import__(
        "eason_one.services.execution_policy", fromlist=["select_retry_model"]
    ).select_retry_model(
        employee, failed_run, operation, purpose, exclude_model_ids=excluded
    )


def _project_management_provider_issue_code(purpose: str, run: AgentRun) -> str:
    return f"PROJECT_MANAGEMENT_PROVIDER_RECOVERY:{str(purpose or '').upper()}:{int(run.id)}"


def _project_management_reconciliation_issue_code(purpose: str, run: AgentRun) -> str:
    """Exact owner for one unresolved Project-management external effect.

    Outcome review and continuation planning share the same MANAGEMENT Work.
    A generic RECONCILIATION gate would therefore let one settled effect keep an
    unrelated management fault blocked forever or invite broad cleanup. Scope the
    gate to purpose + durable AgentRun instead.
    """
    return f"PROJECT_MANAGEMENT_RECONCILIATION:{str(purpose or '').upper()}:{int(run.id)}"


def _project_management_retry_issue_code(purpose: str, fingerprint: str, *, exhausted: bool = False) -> str:
    """Stable owner for one exact Project-management retry/recovery gate.

    Management Work can carry several independent recovery conditions at once.
    Timer maintenance therefore needs an issue identity narrower than merely
    INTERNAL_RECOVERY/SYSTEM_RECOVERY or one due retry could retire another
    Project-management fault on the same Work.
    """
    normalized_purpose = str(purpose or "PROJECT_MANAGEMENT").upper()
    digest = hashlib.sha256(
        f"{normalized_purpose}|{str(fingerprint or '')}".encode("utf-8")
    ).hexdigest()[:20]
    prefix = "PROJECT_MANAGEMENT_RETRY_EXHAUSTED" if exhausted else "PROJECT_MANAGEMENT_RETRY"
    return f"{prefix}:{normalized_purpose}:{digest}"


def _project_result_proof_reconciliation_issue_code(project: Project) -> str:
    """Exact owner for a transient Result Ready proof read/validation fault."""
    return f"PROJECT_RESULT_PROOF_RECONCILIATION:{int(project.id)}"


def _project_outcome_evaluation_reconciliation_issue_code(project: Project, operation: Operation) -> str:
    """Exact owner for one Project outcome-evaluation invariant fault."""
    return f"PROJECT_OUTCOME_EVALUATION_RECONCILIATION:{int(project.id)}:{int(operation.id)}"


def _unresolved_project_attempt(project_id: int, *, purpose: str, fingerprint_key: str, fingerprint: str) -> AgentRun | None:
    """Return unresolved exact management effect truth that forbids another call.

    A previous successful plan/review does not erase the financial/effect truth
    of a later RUNNING or post-dispatch ambiguous attempt. Company management
    must finish/reconcile that attempt before projecting output or spending again.
    """
    rows = _matching_attempts(project_id, purpose, fingerprint_key, fingerprint)
    effects = __import__("eason_one.models", fromlist=["ExternalEffectAttempt"]).ExternalEffectAttempt
    for row in rows:
        if row.status == "RUNNING" or str(row.outcome or "") == "FAILED_AMBIGUOUS":
            return row
        effect = (
            effects.query.filter_by(execution_id=row.id)
            .order_by(effects.id.desc()).first()
        )
        if effect is not None and str(effect.state or "").upper() in {
            "DISPATCHING", "RESPONSE_RECEIVED", "AMBIGUOUS_POST_DISPATCH",
        }:
            return row
    return None


def _reusable_successful_project_attempt(project: Project, *, purpose: str, fingerprint_key: str, fingerprint: str) -> AgentRun | None:
    """Newest durable paid success for the same exact Project decision input.

    Company-level planning/review calls can succeed at the provider boundary and
    then lose the process before their deterministic projection is persisted. A
    restart must continue from that already-paid result instead of purchasing the
    same management decision again. Historical later failures do not hide an
    earlier exact success.

    Founder scope/constraint/deadline changes fail closed. New Runs carry an
    execution-terms hash; older Runs are reusable only when no non-budget Project
    amendment happened after they began.
    """
    contracts = __import__(
        "eason_one.services.project_contract",
        fromlist=["execution_terms_hash", "execution_terms_changed_after"],
    )
    current_terms_hash = contracts.execution_terms_hash(project)
    rows = _matching_attempts(project.id, purpose, fingerprint_key, fingerprint)
    if _unresolved_project_attempt(
        project.id, purpose=purpose, fingerprint_key=fingerprint_key, fingerprint=fingerprint
    ) is not None:
        return None
    for run in reversed(rows):
        if run.status != "SUCCEEDED":
            continue
        context = dict(run.context_composition_json or {})
        snap = str(context.get("project_execution_terms_hash") or "")
        if snap:
            if snap != current_terms_hash:
                continue
        elif contracts.execution_terms_changed_after(project, run.started_at):
            continue
        return run
    return None


def _project_attempt_budget_available(project_id: int, *, purpose: str, fingerprint_key: str, fingerprint: str, max_attempts: int = 3) -> bool:
    """Whether one more paid management attempt is still inside the durable cap.

    Compact truncation recovery is still a real provider call. It therefore
    consumes the same per-evidence attempt budget as ordinary retries instead of
    bypassing the cap inside one kernel tick.
    """
    return len(_matching_attempts(project_id, purpose, fingerprint_key, fingerprint)) < int(max_attempts)


def _bounded_project_retry(project: Project, operation: Operation, management: Work, *, purpose: str, fingerprint_key: str, fingerprint: str, reason: str) -> dict:
    attempts = _matching_attempts(project.id, purpose, fingerprint_key, fingerprint)
    failed = [row for row in attempts if row.status != "SUCCEEDED"]
    count = len(failed)
    if count < 3:
        delay = [10, 30, 120][max(0, count - 1)]
        if failed:
            rejection = dict((failed[-1].context_composition_json or {}).get("provider_rejection") or {})
            try:
                provider_delay = int(rejection.get("retry_after_seconds")) if rejection.get("retry_after_seconds") is not None else 0
            except (TypeError, ValueError):
                provider_delay = 0
            delay = max(delay, min(120, max(0, provider_delay)))
        work_runtime.open_wait(
            management, "INTERNAL_RECOVERY",
            f"{purpose} internal retry {count}/3: {reason}",
            retry_after=now() + timedelta(seconds=delay),
            issue_code=_project_management_retry_issue_code(purpose, fingerprint),
        )
        if work_runtime.project_can_activate(project):
            project.status = "ACTIVE"
            project.current_state_summary = f"Internal {purpose} recovery is scheduled; Founder authority is unchanged."
        db.session.commit()
        return {"status": "INTERNAL_RETRY_SCHEDULED", "purpose": purpose, "attempt": count, "max_attempts": 3}
    work_runtime.open_wait(
        management, "SYSTEM_RECOVERY",
        f"{purpose} exhausted 3 bounded attempts for the same evidence fingerprint: {reason}",
        issue_code=_project_management_retry_issue_code(purpose, fingerprint, exhausted=True),
    )
    project.status = "BLOCKED"
    project.current_state_summary = f"Internal {purpose} recovery is exhausted. This is a system fault, not Project failure or a Founder authority decision."
    project.next_milestone = "Repair provider/system execution, then resume from durable Project evidence."
    emit(
        "PROJECT_SYSTEM_RECOVERY_REQUIRED", actor_type="RUNTIME", project_id=project.id,
        work_id=management.id, correlation_id=f"project:{project.id}",
        payload={"purpose": purpose, "fingerprint": fingerprint, "attempts": count, "reason": reason[:1200]},
    )
    db.session.commit()
    return {"status": "SYSTEM_RECOVERY_REQUIRED", "purpose": purpose, "attempts": count}

def _review_input_hash(project: Project) -> str:
    return __import__(
        "eason_one.services.project_outcome", fromlist=["review_input_hash"]
    ).review_input_hash(project)


def _run_project_outcome_review(
    project: Project, operation: Operation, *, management_decision_id: int | None = None
) -> dict:
    outcome = __import__("eason_one.services.project_outcome", fromlist=["evidence_packet"])
    contract = __import__(
        "eason_one.services.project_contract", fromlist=["governing_terms"]
    ).governing_terms(project)
    input_hash = _review_input_hash(project)
    reusable_review = _reusable_successful_project_attempt(
        project, purpose="PROJECT_OUTCOME_REVIEW",
        fingerprint_key="project_outcome_input_hash", fingerprint=input_hash,
    )
    reused_review = reusable_review is not None

    management = _management_work(operation)
    unresolved_review = _unresolved_project_attempt(
        project.id, purpose="PROJECT_OUTCOME_REVIEW",
        fingerprint_key="project_outcome_input_hash", fingerprint=input_hash,
    )
    if unresolved_review is not None:
        if unresolved_review.status == "RUNNING":
            return {"status": "PROJECT_REVIEW_IN_PROGRESS", "run_id": unresolved_review.id}
        reason = unresolved_review.error_text or (
            "Project outcome review has unresolved post-dispatch truth; reconcile before replay."
        )
        work_runtime.open_wait(
            management, "RECONCILIATION", reason,
            issue_code=_project_management_reconciliation_issue_code("PROJECT_OUTCOME_REVIEW", unresolved_review),
        )
        project.status = "BLOCKED"
        db.session.commit()
        return {"status": "RECONCILIATION_REQUIRED", "run_id": unresolved_review.id}
    ceo = project.owner if project.owner and project.owner.active else Employee.query.filter_by(slug="ceo", active=True).first()
    reviewer = __import__(
        "eason_one.services.project_outcome", fromlist=["select_outcome_reviewer"]
    ).select_outcome_reviewer(project, fallback_ceo=ceo)
    if not reviewer:
        return _project_system_block(
            project,
            "No independent non-producer Project outcome reviewer is available; Company staffing/review ownership must be repaired before Result Ready.",
            work=management,
            issue_code=f"PROJECT_OUTCOME_REVIEWER_UNAVAILABLE:{int(project.id)}",
        )
    packet = outcome.evidence_packet(project)
    criterion_rows = __import__(
        "eason_one.services.project_outcome", fromlist=["criterion_records"]
    ).criterion_records(contract)
    criteria = [row["text"] for row in criterion_rows]
    criterion_ids = [row["id"] for row in criterion_rows]
    execution = __import__("eason_one.services.execution", fromlist=["execute"])
    review_schema = __import__("eason_one.schemas", fromlist=["PROJECT_OUTCOME_REVIEW_SCHEMA"]).PROJECT_OUTCOME_REVIEW_SCHEMA
    review_prompt = (
        reviewer.system_instructions
        + "\nPROJECT_OUTCOME_REVIEW_V1\n"
          "You are a semantic evidence reviewer, not the source of Project authority. "
          "Use only the immutable Founder success criteria and accepted Work evidence supplied. "
          "Return exactly one row for every supplied criterion_id (P1..P8), in the supplied order. "
          "Do not repeat or paraphrase Founder criterion text; criterion_id is the only review address. "
          "SATISFIED requires direct accepted evidence. NOT_SATISFIED requires evidence of a gap. "
          "Otherwise use INSUFFICIENT_EVIDENCE. Never invent completion and never redefine criteria."
    )
    review_output_limit = min(
        max(2400, 400 * max(1, len(criterion_ids))),
        int(reviewer.current_model.max_output_tokens),
    )

    def _execute_project_review(*, prompt: str, prompt_version: str, retry_of_run=None, recovery=None, model_override=None):
        composition = {
            "project_outcome_input_hash": input_hash,
            "contract_hash": contract.get("governing_contract_hash") or contract.get("contract_hash"),
            "ceo_management_decision_id": management_decision_id,
        }
        if recovery:
            composition["company_recovery"] = dict(recovery)
        return execution.execute(
            reviewer,
            "PROJECT_OUTCOME_REVIEW",
            f"Evaluate Project #{project.id} against the immutable Founder Project Contract.",
            project=project,
            operation=operation,
            work=management,
            context_override=packet,
            context_composition=composition,
            system_prompt_override=prompt,
            response_schema=review_schema,
            max_output_tokens_override=review_output_limit,
            prompt_version=prompt_version,
            retry_of_run=retry_of_run,
            model_override=model_override,
        )

    run = reusable_review
    if run is None:
        resume_from = None
        resume_model = None
        prior_attempts = _matching_attempts(
            project.id, "PROJECT_OUTCOME_REVIEW", "project_outcome_input_hash", input_hash
        )
        latest_failed = next((row for row in reversed(prior_attempts) if row.status != "SUCCEEDED"), None)
        if latest_failed is not None and latest_failed.failure_reason in _PROJECT_MANAGEMENT_PROVIDER_FAILURES:
            resume_model = _project_management_recovery_candidate(
                reviewer, operation, latest_failed, purpose="PROJECT_OUTCOME_REVIEW",
                fingerprint_key="project_outcome_input_hash", fingerprint=input_hash,
            )
            if resume_model is None:
                return _project_system_block(
                    project,
                    (latest_failed.error_text or latest_failed.failure_reason or "Project outcome review provider recovery is unavailable")
                    + " No lawful untried reviewer ModelConfig is currently available; Company will resume automatically only after configuration exposes one.",
                    work=management, wait_type="SYSTEM_RECOVERY",
                    issue_code=_project_management_provider_issue_code("PROJECT_OUTCOME_REVIEW", latest_failed),
                )
            resume_from = latest_failed
        run = _execute_project_review(
            prompt=review_prompt,
            prompt_version=(
                "project-outcome-review-v3-provider-recovery" if resume_from is not None
                else "project-outcome-review-v3"
            ),
            retry_of_run=resume_from,
            model_override=resume_model,
            recovery=(
                {
                    "reason": resume_from.failure_reason,
                    "prior_run_id": resume_from.id,
                    "policy": "DIFFERENT_GOVERNED_UNTRIED_MODEL",
                    "selected_model_config_id": resume_model.id,
                    "selected_provider": resume_model.provider_key,
                    "max_attempts": 3,
                }
                if resume_from is not None else None
            ),
        )
    if (run.status != "SUCCEEDED" and run.failure_reason == "OUTPUT_TRUNCATED"
        and _project_attempt_budget_available(
            project.id, purpose="PROJECT_OUTCOME_REVIEW",
            fingerprint_key="project_outcome_input_hash", fingerprint=input_hash,
        )):
        prior = run
        compact_prompt = review_prompt + (
            "\nPROJECT_OUTCOME_REVIEW_TRUNCATION_RECOVERY\n"
            "The previous review hit the configured output ceiling. Return the same criterion-id coverage in compact form. "
            "Use one short evidence sentence per criterion, the smallest sufficient work_ids list, and a concise overall summary. "
            "Do not restate criterion text or accepted Artifact prose. Preserve every criterion_id, status, and evidence lineage."
        )
        run = _execute_project_review(
            prompt=compact_prompt,
            prompt_version="project-outcome-review-v3-truncation-recovery",
            retry_of_run=prior,
            recovery={
                "reason": "OUTPUT_TRUNCATED",
                "prior_run_id": prior.id,
                "policy": "ONE_SAME_PROVIDER_COMPACT_RETRY",
                "max_attempts": 2,
            },
        )
    if run.status != "SUCCEEDED":
        reason = run.error_text or run.failure_reason or "Project outcome evidence review failed."
        if run.outcome == "FAILED_AMBIGUOUS":
            work_runtime.open_wait(
                management, "RECONCILIATION", reason,
                issue_code=_project_management_reconciliation_issue_code("PROJECT_OUTCOME_REVIEW", run),
            )
            project.status = "BLOCKED"
            db.session.commit()
            return {"status": "RECONCILIATION_REQUIRED", "run_id": run.id}
        if run.failure_reason == "FOUNDER_BUDGET_EXTENSION_REQUIRED":
            return _budget_gate_from_run(project, run, reason, operation=operation, work=management)
        if run.failure_reason == "AUTHORITY_BLOCKED":
            return _project_system_block(
                project, reason + " Runtime did not identify a precise Founder-only authority type.",
                work=management, wait_type="RECONCILIATION",
            )
        if run.failure_reason in _PROJECT_MANAGEMENT_PROVIDER_FAILURES:
            if not _project_attempt_budget_available(
                project.id, purpose="PROJECT_OUTCOME_REVIEW",
                fingerprint_key="project_outcome_input_hash", fingerprint=input_hash,
            ):
                result = _bounded_project_retry(
                    project, operation, management, purpose="PROJECT_OUTCOME_REVIEW",
                    fingerprint_key="project_outcome_input_hash", fingerprint=input_hash, reason=reason,
                )
                result["run_id"] = run.id
                return result
            candidate = _project_management_recovery_candidate(
                reviewer, operation, run, purpose="PROJECT_OUTCOME_REVIEW",
                fingerprint_key="project_outcome_input_hash", fingerprint=input_hash,
            )
            if candidate is not None:
                run.resolution_status = "PROJECT_REVIEW_PROVIDER_RETRY_ALLOWED"
                run.resolution_note = (
                    f"Company may retry Project review with untried governed {candidate.provider_key}/{candidate.model_name}; Founder authority is unchanged."
                )
                db.session.commit()
                result = _bounded_project_retry(
                    project, operation, management, purpose="PROJECT_OUTCOME_REVIEW",
                    fingerprint_key="project_outcome_input_hash", fingerprint=input_hash, reason=reason,
                )
                result.update({
                    "run_id": run.id, "next_model_config_id": candidate.id,
                    "next_provider": candidate.provider_key,
                })
                return result
            return _project_system_block(
                project, reason + " No lawful untried reviewer ModelConfig is currently available; repair provider/configuration before replay.",
                work=management, wait_type="SYSTEM_RECOVERY",
                issue_code=_project_management_provider_issue_code("PROJECT_OUTCOME_REVIEW", run),
            )
        run.resolution_status = "PROJECT_REVIEW_RETRY_ALLOWED"
        run.resolution_note = "Project remains active; review failure is not outcome authority."
        db.session.commit()
        result = _bounded_project_retry(
            project, operation, management, purpose="PROJECT_OUTCOME_REVIEW",
            fingerprint_key="project_outcome_input_hash", fingerprint=input_hash, reason=reason,
        )
        result["run_id"] = run.id
        return result
    try:
        payload = dict(run.parsed_output_json or json.loads(run.raw_output or "{}"))
        rows = payload.get("criteria") or []
        expected = list(criterion_ids)
        if [str(row.get("criterion_id") or "") for row in rows] != expected:
            raise ValueError("Project outcome review criterion id coverage/order changed Founder authority")
        accepted_work_ids = {
            row.id for row in __import__(
                "eason_one.services.project_outcome", fromlist=["accepted_delivery"]
            ).accepted_delivery(project)
        }
        for row in rows:
            if row.get("status") not in {"SATISFIED", "NOT_SATISFIED", "INSUFFICIENT_EVIDENCE"}:
                raise ValueError("Invalid Project outcome criterion status")
            cited_work_ids = [int(value) for value in (row.get("work_ids") or [])]
            if any(value not in accepted_work_ids for value in cited_work_ids):
                raise ValueError("Project outcome review cited Work that is not durably accepted")
            if row.get("status") == "SATISFIED" and not cited_work_ids:
                raise ValueError("SATISFIED Project criterion requires accepted Work lineage")
        run.parsed_output_json = payload
        run.structured_validation_status = "PASSED"
        run.structured_validation_errors_json = []
        db.session.commit()
    except Exception as exc:
        run.status = "FAILED"
        run.outcome = "FAILED_KNOWN"
        run.failure_reason = "STRUCTURED_OUTPUT_INVALID"
        run.failure_stage = "POSTPROCESS"
        run.error_text = f"Project outcome review validation failed: {exc}"
        db.session.commit()
        result = _bounded_project_retry(
            project, operation, management, purpose="PROJECT_OUTCOME_REVIEW",
            fingerprint_key="project_outcome_input_hash", fingerprint=input_hash, reason=run.error_text,
        )
        result["run_id"] = run.id
        return result
    return {"status": "REVIEW_REUSED" if reused_review else "PROJECT_REVIEWED", "run_id": run.id}


def _deadline_exceeded(project: Project) -> bool:
    contract = __import__(
        "eason_one.services.project_contract", fromlist=["is_vnext_governed", "governing_terms"]
    )
    if contract.is_vnext_governed(project):
        raw = contract.governing_terms(project).get("deadline")
        if not raw:
            return False
        deadline = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    else:
        deadline = project.deadline
        if not deadline:
            return False
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    current = now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current > deadline


def _continuation_exists(project: Project, evidence_hash: str) -> Operation | None:
    for operation in Operation.query.filter_by(project_id=project.id).order_by(Operation.id.desc()).all():
        if str(operation.status or "").upper() == "SUPERSEDED":
            continue
        memory = dict(operation.memory_json or {})
        if memory.get("continuation_evidence_hash") == evidence_hash and memory.get("authority_source") == "PROJECT_DELEGATED_CEO":
            return operation
    return None


def _accepted_evidence_identity(project: Project) -> list[dict]:
    """Exact current accepted evidence identity for retry/continuation fingerprints.

    Work ids alone are not enough: the same durable Work may acquire a newer
    accepted ArtifactVersion during crash/reconciliation repair.  Carry the
    exact ArtifactVersion/content/frozen acceptance Contract so old retry debt
    and an old continuation plan can never be reused against new evidence.
    """
    rows = []
    works = (
        Work.query.filter(
            Work.project_id == project.id,
            Work.work_type != "MANAGEMENT",
            Work.state == "ACCEPTED",
        )
        .order_by(Work.id)
        .all()
    )
    for work in works:
        version = (
            ArtifactVersion.query.join(Artifact)
            .filter(Artifact.work_id == work.id)
            .order_by(ArtifactVersion.id.desc())
            .first()
        )
        control = dict(work.runtime_control_json or {})
        contract = dict(control.get("acceptance_contract") or {})
        rows.append({
            "work_id": int(work.id),
            "artifact_version_id": int(version.id) if version is not None else None,
            "artifact_status": str(getattr(version, "status", None) or ""),
            "artifact_content_hash": str(getattr(version, "content_hash", None) or ""),
            "acceptance_contract_hash": str(contract.get("contract_hash") or ""),
        })
    return rows


def _accepted_evidence_snapshot_hash(project: Project) -> str:
    payload = json.dumps(
        _accepted_evidence_identity(project), ensure_ascii=False, sort_keys=True
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _continuation_evidence_hash(
    project: Project, prior_operation: Operation, evaluation: dict, *,
    failure_reason: str | None = None, failure_mode: str | None = None,
) -> str:
    """Canonical identity of the exact Project truth used to plan continuation.

    This is shared by planning and pending delegated-approval revalidation. A
    paid CEO plan may be reused only while the same Founder Contract, accepted
    ArtifactVersion/content/acceptance Contract, outcome gaps and bounded-Mission
    failure evidence remain current.
    """
    contract = __import__(
        "eason_one.services.project_contract", fromlist=["governing_terms"]
    ).governing_terms(project)
    missing = [
        row for row in (evaluation.get("criteria") or [])
        if row.get("status") != "SATISFIED"
    ]
    evidence_only = str(failure_mode or "").upper() == "EVIDENCE_ONLY"
    basis = json.dumps({
        "contract_hash": contract.get("governing_contract_hash") or contract.get("contract_hash"),
        "accepted_work_ids": evaluation.get("accepted_work_ids") or [],
        "accepted_evidence": _accepted_evidence_identity(project),
        "missing": missing,
        "failed_operation_id": prior_operation.id if failure_reason else None,
        "failure_reason": failure_reason,
        "failure_mode": "EVIDENCE_ONLY" if evidence_only else "GENERAL",
    }, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def _is_delegated_continuation(operation: Operation) -> bool:
    """Return whether delegated authority is an outcome-evidence continuation.

    PROJECT_DELEGATED_CEO also governs ordinary internal Project Missions. Only
    continuation Missions carry an evidence snapshot that can become stale and
    therefore require the additional continuation-currentness proof.
    """
    memory = dict(getattr(operation, "memory_json", None) or {})
    if memory.get("authority_source") != "PROJECT_DELEGATED_CEO":
        return False
    if any(memory.get(key) not in (None, "") for key in (
        "continuation_evidence_hash",
        "continuation_source_run_id",
        "continuation_mode",
        "project_contract_hash",
    )):
        return True
    route_reason = str(getattr(operation, "route_reason", None) or "")
    return route_reason.startswith(
        "PROJECT_DELEGATED_CEO: bounded continuation inside immutable Founder Project Contract."
    )


def _delegated_continuation_currentness(operation: Operation) -> dict:
    """Re-prove that one unapproved delegated Mission still matches Company Truth.

    The probe is deterministic and provider-free. It reconstructs the same
    continuation fingerprint from current durable evidence using the persisted
    planning Run lineage. Missing lineage is reconciliation; a proven Contract
    or evidence mismatch means the old plan is safely superseded.
    """
    memory = dict(getattr(operation, "memory_json", None) or {})
    if memory.get("authority_source") != "PROJECT_DELEGATED_CEO":
        return {"current": False, "kind": "RECONCILIATION", "reason": "Delegated continuation authority metadata is missing."}
    project = db.session.get(Project, getattr(operation, "project_id", None))
    if project is None:
        return {"current": False, "kind": "RECONCILIATION", "reason": "Delegated continuation references a missing Project."}
    expected_hash = str(memory.get("continuation_evidence_hash") or "").strip()
    source_run_id = memory.get("continuation_source_run_id")
    expected_contract = str(memory.get("project_contract_hash") or "").strip()
    if not expected_hash or not source_run_id or not expected_contract:
        return {
            "current": False, "kind": "RECONCILIATION",
            "reason": "Delegated continuation lacks exact planning lineage needed to prove current evidence authority.",
        }
    run = db.session.get(AgentRun, source_run_id)
    prior = db.session.get(Operation, getattr(run, "operation_id", None)) if run is not None else None
    if run is None or prior is None or int(getattr(run, "project_id", 0) or 0) != int(project.id):
        return {
            "current": False, "kind": "RECONCILIATION",
            "reason": "Delegated continuation planning Run lineage is incomplete; automatic approval is fail-closed.",
        }
    try:
        terms = __import__(
            "eason_one.services.project_contract", fromlist=["governing_terms"]
        ).governing_terms(project)
        current_contract = str(
            terms.get("governing_contract_hash") or terms.get("contract_hash") or ""
        )
        if current_contract != expected_contract:
            return {
                "current": False, "kind": "STALE_CONTINUATION",
                "reason": "Founder Project Contract changed after this delegated continuation was planned.",
                "expected_project_contract_hash": expected_contract,
                "current_project_contract_hash": current_contract,
            }
        evaluation = __import__(
            "eason_one.services.project_outcome", fromlist=["evaluate"]
        ).evaluate(project)
        run_context = dict(getattr(run, "context_composition_json", None) or {})
        failure_reason = str(run_context.get("continuation_failure_reason") or "").strip() or None
        failure_mode = str(
            run_context.get("continuation_mode")
            or memory.get("continuation_mode")
            or "GENERAL"
        )
        current_hash = _continuation_evidence_hash(
            project, prior, evaluation,
            failure_reason=failure_reason, failure_mode=failure_mode,
        )
    except ValueError as exc:
        return {
            "current": False, "kind": "RECONCILIATION",
            "reason": f"Current Project evidence could not be deterministically revalidated: {exc}",
        }
    if current_hash != expected_hash:
        return {
            "current": False, "kind": "STALE_CONTINUATION",
            "reason": "Accepted Project evidence changed after this delegated continuation was planned.",
            "expected_continuation_evidence_hash": expected_hash,
            "current_continuation_evidence_hash": current_hash,
        }
    return {
        "current": True, "kind": "CURRENT",
        "continuation_evidence_hash": current_hash,
        "accepted_evidence_snapshot_hash": _accepted_evidence_snapshot_hash(project),
        "project_contract_hash": expected_contract,
    }


def _normalized_failure_signature(project: Project, evaluation: dict, failure_reason: str | None) -> str | None:
    if not failure_reason:
        return None
    missing = [
        {"criterion": row.get("criterion"), "status": row.get("status")}
        for row in (evaluation.get("criteria") or [])
        if row.get("status") != "SATISFIED"
    ]
    reason = " ".join(str(failure_reason or "").casefold().split())[:1200]
    payload = json.dumps({
        "project_id": project.id,
        "accepted_work_ids": evaluation.get("accepted_work_ids") or [],
        "accepted_evidence": _accepted_evidence_identity(project),
        "missing": missing,
        "failure_reason": reason,
    }, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _repeated_failure_count(project: Project, signature: str | None) -> int:
    if not signature:
        return 0
    count = 0
    for row in Operation.query.filter_by(project_id=project.id).order_by(Operation.id).all():
        memory = dict(row.memory_json or {})
        if (
            memory.get("authority_source") == "PROJECT_DELEGATED_CEO"
            and memory.get("continuation_failure_signature") == signature
            and row.status == "FAILED"
        ):
            count += 1
    return count


_CONTINUATION_RUNTIME_READINESS_PREFIXES = (
    "ENGINEERING_TOOL_UNAVAILABLE:",
    "ENGINEERING_REPOSITORY_UNAVAILABLE:",
    "RESEARCH_TOOL_UNAVAILABLE:",
    "STAFFING_RUNTIME_UNAVAILABLE:",
    "MODEL_POLICY_BLOCKED:",
)


_CONTINUATION_APPROVAL_RECOVERY_PREFIX = "CONTINUATION_APPROVAL_RECOVERY:"
_CONTINUATION_APPROVAL_RECONCILIATION_PREFIX = "CONTINUATION_APPROVAL_RECONCILIATION:"


def _continuation_approval_issue_code(operation: Operation) -> str:
    return f"{_CONTINUATION_APPROVAL_RECOVERY_PREFIX}{int(operation.id)}"


def _continuation_approval_reconciliation_issue_code(operation: Operation) -> str:
    return f"{_CONTINUATION_APPROVAL_RECONCILIATION_PREFIX}{int(operation.id)}"


def _continuation_approval_wait_type(reason: str) -> str:
    """Classify exact execution-core readiness failures as Company recovery.

    Missing tools/providers are not ambiguous business truth and do not create
    Founder authority. They are recoverable Company/runtime conditions. Other
    approval inconsistencies remain RECONCILIATION until an owning invariant
    proves a more specific class.
    """
    text = str(reason or "")
    return (
        "SYSTEM_RECOVERY"
        if text.startswith(_CONTINUATION_RUNTIME_READINESS_PREFIXES)
        else "RECONCILIATION"
    )


def _plan_continuation(
    project: Project, prior_operation: Operation, evaluation: dict, *,
    failure_reason: str | None = None, failure_mode: str | None = None,
    management_decision_id: int | None = None,
) -> dict:
    contract_mod = __import__("eason_one.services.project_contract", fromlist=["governing_terms", "assert_internal_authority"])
    contract = contract_mod.governing_terms(project)
    failure_signature = _normalized_failure_signature(project, evaluation, failure_reason)
    repeated_failures = _repeated_failure_count(project, failure_signature)
    management = _management_work(prior_operation)
    protocol_retry = (
        repeated_failures >= 2
        and _consume_system_recovery_protocol_override(management, failure_signature)
    )
    if failure_signature and repeated_failures >= 2 and not protocol_retry:
        work_runtime.open_wait(
            management, "SYSTEM_RECOVERY",
            "The same bounded Project failure repeated twice without new accepted evidence. "
            "Company Kernel stopped the loop instead of spending more Founder budget.",
            issue_code=_system_recovery_protocol_issue_code(failure_signature),
        )
        project.status = "BLOCKED"
        project.current_state_summary = (
            "Repeated identical recovery failed twice. This is a system/recovery problem, not a request for more Founder authority."
        )
        project.next_milestone = "Repair the execution/verification path, then resume from existing durable evidence."
        emit(
            "PROJECT_REPEATED_FAILURE_LOOP_STOPPED", actor_type="RUNTIME",
            project_id=project.id, work_id=management.id,
            correlation_id=f"project:{project.id}",
            payload={
                "failure_signature": failure_signature,
                "repeated_failures": repeated_failures,
                "failure_reason": str(failure_reason or "")[:1200],
            },
        )
        db.session.commit()
        return {
            "status": "SYSTEM_RECOVERY_REQUIRED", "project_id": project.id,
            "failure_signature": failure_signature, "repeated_failures": repeated_failures,
        }
    if _deadline_exceeded(project):
        return _project_system_block(
            project,
            "The Founder Project Contract deadline has passed, but runtime has no exact replacement deadline proposal. "
            "CEO may not silently extend it and Founder may not be asked to approve a blank date.",
            work=_management_work(prior_operation), wait_type="RECONCILIATION",
        )
    remaining = __import__("eason_one.services.operations", fromlist=["project_remaining_authority"]).project_remaining_authority(project)

    missing = [row for row in evaluation.get("criteria") or [] if row.get("status") != "SATISFIED"]
    evidence_only = str(failure_mode or "").upper() == "EVIDENCE_ONLY"
    evidence_hash = _continuation_evidence_hash(
        project, prior_operation, evaluation,
        failure_reason=failure_reason, failure_mode=failure_mode,
    )
    existing = _continuation_exists(project, evidence_hash)
    if existing:
        if existing.approved_at is None:
            try:
                __import__("eason_one.services.operations", fromlist=["approve"]).approve(
                    existing, authority_source="PROJECT_DELEGATED_CEO"
                )
            except ValueError as exc:
                reason = str(exc)
                wait_type = _continuation_approval_wait_type(reason)
                return _project_system_block(
                    project, reason + " Delegated continuation approval could not prove an exact Founder authority delta.",
                    work=_management_work(existing), wait_type=wait_type,
                    issue_code=(
                        _continuation_approval_issue_code(existing)
                        if wait_type == "SYSTEM_RECOVERY"
                        else _continuation_approval_reconciliation_issue_code(existing)
                    ),
                )
        return {"status": "CONTINUATION_EXISTS", "operation_id": existing.id, "project_id": project.id}

    ceo = project.owner if project.owner and project.owner.active else Employee.query.filter_by(slug="ceo", active=True).first()
    management = _management_work(prior_operation)
    if not ceo:
        return _project_system_block(
            project, "No active CEO is available to sequence Project continuation.", work=management,
            issue_code=f"PROJECT_CEO_UNAVAILABLE:{int(project.id)}",
        )

    packet = __import__("eason_one.services.project_outcome", fromlist=["evidence_packet"]).evidence_packet(project)
    context = (
        packet
        + "\n\nCURRENT OUTCOME EVALUATION\n" + json.dumps(evaluation, ensure_ascii=False)
        + ("\n\nFAILED MISSION EVIDENCE\n" + failure_reason if failure_reason else "")
        + (
            "\n\nCONTINUATION MODE\nEVIDENCE_ONLY: the prior implementation and host proof are durable evidence. "
            "Do not replay implementation or widen repository changes; plan only read-only evidence collection/reconciliation."
            if evidence_only else ""
        )
        + "\n\nORGANIZATION\n" + __import__("eason_one.services.ceo", fromlist=["operating_context"]).operating_context()
    )
    execution = __import__("eason_one.services.execution", fromlist=["execute"])
    response_schema = __import__("eason_one.schemas", fromlist=["CEO_EXECUTION_SCHEMA"]).CEO_EXECUTION_SCHEMA
    base_prompt = (
        ceo.system_instructions
        + "\nPROJECT_CONTINUATION_V1\n"
          "Return only an OPERATION_PLAN using the required schema. The Project already exists. "
          f"Set operation.project_id exactly to {project.id}; project must be null. "
          "Plan the smallest useful next Mission that addresses currently unsatisfied Founder Project criteria using the accepted Company evidence already produced. "
          "Do not change Project objective, success criteria, constraints, deadline, or budget authority. "
          "Do not create Founder approval work for internal sequencing. Preserve Company semantics: use the smallest sufficient set of single-purpose Tasks, "
          "declare exactly one accountable required_capability per Task, split materially different specialties into separate Tasks, and let Runtime establish dependency/handoff topology from the resulting Work. "
          "Use RESEARCH only when fresh external facts are materially required; use CRITICAL_REVIEW for independent evidence/claim review; use SOFTWARE_ENGINEERING only when repository/code work is actually required. "
          "For every writable SOFTWARE_ENGINEERING Task, set write_scope to CODEX_WRITE_SCOPE_V1 with the smallest exact repository-relative file paths the Task may create or modify. For explicitly read-only engineering, set write_scope to null and state the no-write contract explicitly. Every non-engineering Task must use null write_scope. "
          "Do not collapse continuation into a default Engineer-only Mission. If the active roster lacks a required delivery capability, keep that required_capability explicit and assign the nearest accountable manager/CEO placeholder; Company Team Formation owns governed staffing before execution. "
          "Meetings are conditional coordination tools for material cross-role conflict, not mandatory ceremony. Every continuation action must remain inside the existing Founder Project authority."
          + (
              " CONTINUATION MODE IS EVIDENCE_ONLY. The implementation must not be replayed or modified. "
              "Every Task must be read-only evidence reconciliation using existing Artifacts, VerificationRecords, logs, and accepted Company Truth."
              if evidence_only else ""
          )
    )
    composition = {
        "contract_hash": contract.get("governing_contract_hash") or contract.get("contract_hash"),
        "continuation_evidence_hash": evidence_hash,
        "accepted_evidence_snapshot_hash": _accepted_evidence_snapshot_hash(project),
        "remaining_project_authority_twd": str(remaining) if remaining is not None else None,
        "continuation_mode": "EVIDENCE_ONLY" if evidence_only else "GENERAL",
        "continuation_failure_reason": failure_reason,
        "ceo_management_decision_id": management_decision_id,
    }

    def _execute_continuation(*, prompt: str, prompt_version: str, retry_of_run=None, recovery=None, model_override=None):
        local_composition = dict(composition)
        if recovery:
            local_composition["company_recovery"] = dict(recovery)
        return execution.execute(
            ceo,
            "CEO_PROJECT_CONTINUATION",
            f"Create the next smallest bounded Mission for Project #{project.id} without changing Founder authority.",
            project=project,
            operation=prior_operation,
            work=management,
            context_override=context[:22000],
            context_composition=local_composition,
            system_prompt_override=prompt,
            response_schema=response_schema,
            # Continuation planning emits the same large authority-bearing schema
            # as first-use Founder planning. Do not keep a hidden 2600-token cap
            # that can turn an otherwise valid plan into Founder-visible failure.
            max_output_tokens_override=int(ceo.current_model.max_output_tokens),
            prompt_version=prompt_version,
            retry_of_run=retry_of_run,
            model_override=model_override,
        )

    unresolved_continuation = _unresolved_project_attempt(
        project.id, purpose="CEO_PROJECT_CONTINUATION",
        fingerprint_key="continuation_evidence_hash", fingerprint=evidence_hash,
    )
    if unresolved_continuation is not None:
        if unresolved_continuation.status == "RUNNING":
            return {"status": "CONTINUATION_PLANNING_IN_PROGRESS", "run_id": unresolved_continuation.id}
        reason = unresolved_continuation.error_text or (
            "Project continuation planning has unresolved post-dispatch truth; reconcile before replay."
        )
        work_runtime.open_wait(
            management, "RECONCILIATION", reason,
            issue_code=_project_management_reconciliation_issue_code("CEO_PROJECT_CONTINUATION", unresolved_continuation),
        )
        project.status = "BLOCKED"
        db.session.commit()
        return {"status": "RECONCILIATION_REQUIRED", "run_id": unresolved_continuation.id}

    reusable_continuation = _reusable_successful_project_attempt(
        project, purpose="CEO_PROJECT_CONTINUATION",
        fingerprint_key="continuation_evidence_hash", fingerprint=evidence_hash,
    )
    reused_continuation = reusable_continuation is not None
    run = reusable_continuation
    if run is None:
        resume_from = None
        resume_model = None
        prior_attempts = _matching_attempts(
            project.id, "CEO_PROJECT_CONTINUATION", "continuation_evidence_hash", evidence_hash
        )
        latest_failed = next((row for row in reversed(prior_attempts) if row.status != "SUCCEEDED"), None)
        if latest_failed is not None and latest_failed.failure_reason in _PROJECT_MANAGEMENT_PROVIDER_FAILURES:
            resume_model = _project_management_recovery_candidate(
                ceo, prior_operation, latest_failed, purpose="CEO_PROJECT_CONTINUATION",
                fingerprint_key="continuation_evidence_hash", fingerprint=evidence_hash,
            )
            if resume_model is None:
                return _project_system_block(
                    project,
                    (latest_failed.error_text or latest_failed.failure_reason or "Project continuation provider recovery is unavailable")
                    + " No lawful untried CEO ModelConfig is currently available; Company will resume automatically only after configuration exposes one.",
                    work=management, wait_type="SYSTEM_RECOVERY",
                    issue_code=_project_management_provider_issue_code("CEO_PROJECT_CONTINUATION", latest_failed),
                )
            resume_from = latest_failed
        run = _execute_continuation(
            prompt=base_prompt,
            prompt_version=(
                "project-continuation-v2-provider-recovery" if resume_from is not None
                else "project-continuation-v2"
            ),
            retry_of_run=resume_from,
            model_override=resume_model,
            recovery=(
                {
                    "reason": resume_from.failure_reason,
                    "prior_run_id": resume_from.id,
                    "policy": "DIFFERENT_GOVERNED_UNTRIED_MODEL",
                    "selected_model_config_id": resume_model.id,
                    "selected_provider": resume_model.provider_key,
                    "max_attempts": 3,
                }
                if resume_from is not None else None
            ),
        )
    if (run.status != "SUCCEEDED" and run.failure_reason == "OUTPUT_TRUNCATED"
        and _project_attempt_budget_available(
            project.id, purpose="CEO_PROJECT_CONTINUATION",
            fingerprint_key="continuation_evidence_hash", fingerprint=evidence_hash,
        )):
        compact_prompt = base_prompt + (
            "\nPROJECT_CONTINUATION_TRUNCATION_RECOVERY\n"
            "The immediately prior continuation plan hit the configured model output ceiling. "
            "Return the same complete authority-preserving OPERATION_PLAN in materially more compact form. "
            "Use the shortest sufficient strings, 1-4 single-purpose Tasks unless genuinely more are required, "
            "and one concise testable sentence per acceptance criterion. Do not repeat accepted evidence in prose. "
            "Preserve every required schema field, required_capability, reviewer decision, meeting gate, budget estimate, "
            "and exact engineering write_scope. Never omit authority fields to save tokens."
        )
        prior = run
        run = _execute_continuation(
            prompt=compact_prompt,
            prompt_version="project-continuation-v2-truncation-recovery",
            retry_of_run=prior,
            recovery={
                "reason": "OUTPUT_TRUNCATED",
                "prior_run_id": prior.id,
                "policy": "ONE_SAME_PROVIDER_COMPACT_RETRY",
                "max_attempts": 2,
            },
        )
    if run.status != "SUCCEEDED":
        reason = run.error_text or run.failure_reason or "CEO continuation planning failed."
        if run.outcome == "FAILED_AMBIGUOUS":
            work_runtime.open_wait(
                management, "RECONCILIATION", reason,
                issue_code=_project_management_reconciliation_issue_code("CEO_PROJECT_CONTINUATION", run),
            )
            project.status = "BLOCKED"
            db.session.commit()
            return {"status": "RECONCILIATION_REQUIRED", "run_id": run.id}
        if run.failure_reason == "FOUNDER_BUDGET_EXTENSION_REQUIRED":
            return _budget_gate_from_run(project, run, reason, operation=prior_operation, work=management)
        if run.failure_reason == "AUTHORITY_BLOCKED":
            return _project_system_block(
                project, reason + " Runtime did not identify a precise Founder-only authority type.",
                work=management, wait_type="RECONCILIATION",
            )
        if run.failure_reason in _PROJECT_MANAGEMENT_PROVIDER_FAILURES:
            if not _project_attempt_budget_available(
                project.id, purpose="CEO_PROJECT_CONTINUATION",
                fingerprint_key="continuation_evidence_hash", fingerprint=evidence_hash,
            ):
                result = _bounded_project_retry(
                    project, prior_operation, management, purpose="CEO_PROJECT_CONTINUATION",
                    fingerprint_key="continuation_evidence_hash", fingerprint=evidence_hash, reason=reason,
                )
                result["run_id"] = run.id
                return result
            candidate = _project_management_recovery_candidate(
                ceo, prior_operation, run, purpose="CEO_PROJECT_CONTINUATION",
                fingerprint_key="continuation_evidence_hash", fingerprint=evidence_hash,
            )
            if candidate is not None:
                run.resolution_status = "PROJECT_CONTINUATION_PROVIDER_RETRY_ALLOWED"
                run.resolution_note = (
                    f"Company may retry continuation planning with untried governed {candidate.provider_key}/{candidate.model_name}; Founder authority is unchanged."
                )
                db.session.commit()
                result = _bounded_project_retry(
                    project, prior_operation, management, purpose="CEO_PROJECT_CONTINUATION",
                    fingerprint_key="continuation_evidence_hash", fingerprint=evidence_hash, reason=reason,
                )
                result.update({
                    "run_id": run.id, "next_model_config_id": candidate.id,
                    "next_provider": candidate.provider_key,
                })
                return result
            return _project_system_block(
                project, reason + " No lawful untried CEO ModelConfig is currently available; repair provider/configuration before replay.",
                work=management, wait_type="SYSTEM_RECOVERY",
                issue_code=_project_management_provider_issue_code("CEO_PROJECT_CONTINUATION", run),
            )
        run.resolution_status = "PROJECT_CONTINUATION_RETRY_ALLOWED"
        run.resolution_note = "Planning failure does not change Project outcome."
        db.session.commit()
        result = _bounded_project_retry(
            project, prior_operation, management, purpose="CEO_PROJECT_CONTINUATION",
            fingerprint_key="continuation_evidence_hash", fingerprint=evidence_hash, reason=reason,
        )
        result["run_id"] = run.id
        return result

    try:
        raw = dict(run.parsed_output_json or json.loads(run.raw_output or "{}"))
        raw["mode"] = "OPERATION_PLAN"
        raw["project"] = None
        raw["project_id"] = project.id
        operation_spec = dict(raw.get("operation") or {})
        operation_spec["project_id"] = project.id
        if evidence_only:
            narrowed = []
            for item in list(operation_spec.get("tasks") or []):
                item = dict(item)
                item["objective"] = (
                    "READ-ONLY EVIDENCE RECONCILIATION; do not modify repository files. "
                    + str(item.get("objective") or "")
                )[:420]
                criteria = list(item.get("acceptance_criteria") or [])
                if not any("do not modify" in str(row).casefold() for row in criteria):
                    criteria.append(
                        "No repository file changes; use only existing durable Artifact/Verification evidence."
                    )
                item["acceptance_criteria"] = criteria[:8]
                # Evidence-only continuation may remove write authority, never
                # inherit or preserve a previous model-proposed repository scope.
                item["write_scope"] = None
                narrowed.append(item)
            operation_spec["tasks"] = narrowed
        raw["operation"] = operation_spec
        plan = __import__("eason_one.services.ceo", fromlist=["validate_plan"]).validate_plan(raw)
        run.parsed_output_json = plan
        run.structured_validation_status = "PASSED"
        run.structured_validation_errors_json = []
        db.session.commit()
    except Exception as exc:
        run.status = "FAILED"
        run.outcome = "FAILED_KNOWN"
        run.failure_reason = "STRUCTURED_OUTPUT_INVALID"
        run.failure_stage = "POSTPROCESS"
        run.error_text = f"Delegated continuation validation failed: {exc}"
        db.session.commit()
        result = _bounded_project_retry(
            project, prior_operation, management, purpose="CEO_PROJECT_CONTINUATION",
            fingerprint_key="continuation_evidence_hash", fingerprint=evidence_hash, reason=run.error_text,
        )
        result["run_id"] = run.id
        return result

    try:
        operation = __import__("eason_one.services.operations", fromlist=["propose_operation"]).propose_operation(
            ceo,
            plan,
            route_type=None,
            route_reason="PROJECT_DELEGATED_CEO: bounded continuation inside immutable Founder Project Contract.",
            founder_request=None,
            authority_source="PROJECT_DELEGATED_CEO",
        )
        memory = dict(operation.memory_json or {})
        memory["authority_source"] = "PROJECT_DELEGATED_CEO"
        memory["continuation_evidence_hash"] = evidence_hash
        memory["continuation_accepted_evidence_hash"] = _accepted_evidence_snapshot_hash(project)
        memory["continuation_source_run_id"] = run.id
        memory["continuation_failure_signature"] = failure_signature
        memory["continuation_mode"] = "EVIDENCE_ONLY" if evidence_only else "GENERAL"
        memory["project_contract_hash"] = contract.get("governing_contract_hash") or contract.get("contract_hash")
        memory["ceo_management_decision_id"] = management_decision_id
        operation.memory_json = memory
        db.session.commit()
        __import__("eason_one.services.operations", fromlist=["approve"]).approve(
            operation, authority_source="PROJECT_DELEGATED_CEO"
        )
    except ValueError as exc:
        reason = str(exc)
        # If the validated continuation plan itself proves a precise priced
        # shortfall, freeze that exact Project amount. Never classify by words.
        try:
            estimate = __import__(
                "eason_one.services.operations", fromlist=["execution_budget_estimate"]
            ).execution_budget_estimate(plan, include_orchestration=False)[0]
            remaining_now = __import__(
                "eason_one.services.operations", fromlist=["project_remaining_authority"]
            ).project_remaining_authority(project)
            shortfall = Decimal(estimate) - Decimal(remaining_now or 0)
        except Exception:
            shortfall = Decimal("0")
        if shortfall > 0:
            try:
                return _open_project_gate(
                    project, reason, operation=locals().get("operation", prior_operation),
                    work=management, escalation_type="BUDGET_AUTHORIZATION",
                    authority_payload={"additional_budget_twd": str(shortfall), "scope": "PROJECT"},
                )
            except __import__(
                "eason_one.services.governance", fromlist=["FounderAuthorityPreviouslyRejected"]
            ).FounderAuthorityPreviouslyRejected as rejected:
                __import__(
                    "eason_one.services.governance", fromlist=["apply_rejected_budget_boundary"]
                ).apply_rejected_budget_boundary(
                    project=project, work=management, decision_id=rejected.decision_id
                )
                db.session.commit()
                return {
                    "status": "AUTHORITY_EXHAUSTED", "project_id": project.id,
                    "decision_id": rejected.decision_id,
                }
        wait_type = _continuation_approval_wait_type(reason)
        return _project_system_block(
            project, reason + " No exact Founder authority delta was deterministically proven.",
            work=management, wait_type=wait_type,
            issue_code=(
                _continuation_approval_issue_code(operation)
                if wait_type == "SYSTEM_RECOVERY" and locals().get("operation") is not None
                else _continuation_approval_reconciliation_issue_code(operation)
                if locals().get("operation") is not None else None
            ),
        )

    emit(
        "PROJECT_CONTINUATION_CREATED", actor_type="EMPLOYEE", actor_id=ceo.id,
        project_id=project.id, correlation_id=f"project:{project.id}",
        payload={
            "operation_id": operation.id,
            "source_run_id": run.id,
            "authority_source": "PROJECT_DELEGATED_CEO",
            "project_contract_hash": contract.get("governing_contract_hash") or contract.get("contract_hash"),
            "continuation_evidence_hash": evidence_hash,
        },
    )
    if work_runtime.project_can_activate(project):
        project.status = "ACTIVE"
        project.current_state_summary = (
            "CEO created a read-only evidence reconciliation Mission; prior implementation will not be replayed."
            if evidence_only
            else "CEO created the next bounded Mission inside the existing Founder Project Contract."
        )
        project.next_milestone = operation.objective
    db.session.commit()
    return {
        "status": "CONTINUATION_CREATED", "project_id": project.id,
        "operation_id": operation.id, "run_id": run.id,
        "reused_planning_run": bool(reused_continuation),
    }


def _supersede_stale_delegated_continuation(
    project: Project, operation: Operation, *, reason: str, currentness: dict | None = None,
) -> dict:
    """Retire one unapproved stale delegated Mission without provider work.

    The paid planning Run and Operation remain immutable audit history. Only the
    obsolete pending authority projection is retired; current Project sequencing
    will recompute continuation from durable evidence on the next Company tick.
    """
    if operation.approved_at is not None:
        return {"status": "CONTINUATION_ALREADY_APPROVED", "operation_id": operation.id, "project_id": project.id}
    memory = dict(operation.memory_json or {})
    if memory.get("authority_source") != "PROJECT_DELEGATED_CEO":
        return {"status": "CONTINUATION_NOT_DELEGATED", "operation_id": operation.id, "project_id": project.id}
    management = _management_work(operation)
    for condition, issue in (
        ("SYSTEM_RECOVERY", _continuation_approval_issue_code(operation)),
        ("RECONCILIATION", _continuation_approval_reconciliation_issue_code(operation)),
    ):
        work_runtime.resolve_waits(
            management, condition, issue_code=issue,
            note="Obsolete delegated continuation approval gate retired with the superseded plan.",
        )
    if management.state not in work_runtime.TERMINAL_WORK_STATES:
        work_runtime.transition(
            management, "CANCELLED", actor_type="RUNTIME",
            reason="Pending delegated continuation was superseded because its planning evidence/Founder authority is no longer current.",
        )
    operation.status = "SUPERSEDED"
    operation.kernel_status = "SUPERSEDED"
    operation.current_stage = "SUPERSEDED_STALE_CONTINUATION"
    operation.waiting_reason = str(reason)[:1400]
    operation.ended_at = operation.ended_at or now()
    operation.lease_owner = None
    operation.lease_expires_at = None
    memory["superseded_reason"] = str(reason)[:4000]
    memory["superseded_at"] = now().isoformat()
    if currentness:
        memory["superseded_currentness"] = dict(currentness)
    operation.memory_json = memory
    if work_runtime.project_can_activate(project):
        project.status = "ACTIVE"
        project.current_state_summary = (
            "A stale unapproved CEO-delegated Mission was retired because its evidence/Founder authority basis changed. "
            "Company sequencing will replan from current durable truth without replaying the obsolete Mission."
        )
        project.next_milestone = "Recompute the smallest continuation from current accepted evidence and the current Founder Project Contract."
    emit(
        "PROJECT_CONTINUATION_STALE_PLAN_SUPERSEDED", actor_type="RUNTIME",
        project_id=project.id, work_id=management.id, correlation_id=f"project:{project.id}",
        payload={
            "operation_id": operation.id,
            "planning_run_id": memory.get("continuation_source_run_id"),
            "reason": str(reason)[:1400],
            "currentness": dict(currentness or {}),
            "provider_call": False,
            "operation_approved": False,
        },
    )
    db.session.commit()
    return {
        "status": "CONTINUATION_STALE_PLAN_SUPERSEDED",
        "project_id": project.id, "operation_id": operation.id,
    }


def _resume_ready_delegated_continuation(project: Project, operation: Operation) -> dict | None:
    """Approve one already-planned delegated Mission after its scoped readiness gate cleared.

    The expensive CEO planning result is reused. This function never creates a
    second continuation proposal and never bypasses a remaining Work/Founder gate.
    """
    memory = dict(operation.memory_json or {})
    if memory.get("authority_source") != "PROJECT_DELEGATED_CEO" or operation.approved_at is not None:
        return None
    management = _management_work(operation)
    if work_runtime.has_open_gate(management):
        return None
    operations_mod = __import__(
        "eason_one.services.operations", fromlist=["approve", "delegated_approval_readiness"]
    )
    readiness = operations_mod.delegated_approval_readiness(operation)
    if not readiness.get("ready"):
        kind = str(readiness.get("kind") or "RECONCILIATION")
        reason = str(readiness.get("reason") or "Delegated continuation approval is not ready.")
        if kind == "STALE_CONTINUATION":
            return _supersede_stale_delegated_continuation(
                project, operation, reason=reason, currentness=readiness,
            )
        if kind == "BUDGET_AUTHORIZATION":
            amount = Decimal(str(readiness.get("additional_budget_twd") or 0))
            if amount > 0:
                try:
                    return _open_project_gate(
                        project, reason, operation=operation, work=management,
                        escalation_type="BUDGET_AUTHORIZATION",
                        authority_payload={"additional_budget_twd": str(amount), "scope": "PROJECT"},
                    )
                except __import__(
                    "eason_one.services.governance", fromlist=["FounderAuthorityPreviouslyRejected"]
                ).FounderAuthorityPreviouslyRejected as rejected:
                    __import__(
                        "eason_one.services.governance", fromlist=["apply_rejected_budget_boundary"]
                    ).apply_rejected_budget_boundary(
                        project=project, work=management, decision_id=rejected.decision_id
                    )
                    db.session.commit()
                    return {
                        "status": "AUTHORITY_EXHAUSTED", "project_id": project.id,
                        "decision_id": rejected.decision_id,
                    }
        return _project_system_block(
            project, reason, work=management,
            wait_type="SYSTEM_RECOVERY" if kind == "SYSTEM_RECOVERY" else "RECONCILIATION",
            issue_code=(
                _continuation_approval_issue_code(operation)
                if kind == "SYSTEM_RECOVERY"
                else _continuation_approval_reconciliation_issue_code(operation)
            ),
        )
    try:
        operations_mod.approve(operation, authority_source="PROJECT_DELEGATED_CEO")
    except ValueError as exc:
        # Readiness can change between the maintenance probe and this bounded
        # business tick. Re-probe structured truth rather than classify a stale
        # exception string or manufacturing Founder authority by guesswork.
        fresh = operations_mod.delegated_approval_readiness(operation)
        reason = str(fresh.get("reason") or exc)
        kind = str(fresh.get("kind") or "RECONCILIATION")
        if kind == "BUDGET_AUTHORIZATION":
            amount = Decimal(str(fresh.get("additional_budget_twd") or 0))
            if amount > 0:
                try:
                    return _open_project_gate(
                        project, reason, operation=operation, work=management,
                        escalation_type="BUDGET_AUTHORIZATION",
                        authority_payload={"additional_budget_twd": str(amount), "scope": "PROJECT"},
                    )
                except __import__(
                    "eason_one.services.governance", fromlist=["FounderAuthorityPreviouslyRejected"]
                ).FounderAuthorityPreviouslyRejected as rejected:
                    __import__(
                        "eason_one.services.governance", fromlist=["apply_rejected_budget_boundary"]
                    ).apply_rejected_budget_boundary(
                        project=project, work=management, decision_id=rejected.decision_id
                    )
                    db.session.commit()
                    return {
                        "status": "AUTHORITY_EXHAUSTED", "project_id": project.id,
                        "decision_id": rejected.decision_id,
                    }
        wait_type = "SYSTEM_RECOVERY" if kind == "SYSTEM_RECOVERY" else "RECONCILIATION"
        return _project_system_block(
            project, reason, work=management, wait_type=wait_type,
            issue_code=(
                _continuation_approval_issue_code(operation)
                if wait_type == "SYSTEM_RECOVERY"
                else _continuation_approval_reconciliation_issue_code(operation)
            ),
        )
    emit(
        "PROJECT_CONTINUATION_APPROVAL_RESUMED", actor_type="RUNTIME",
        project_id=project.id, work_id=management.id, correlation_id=f"project:{project.id}",
        payload={
            "operation_id": operation.id,
            "planning_run_id": memory.get("continuation_source_run_id"),
            "reused_existing_plan": True,
            "provider_call": False,
        },
    )
    if work_runtime.project_can_activate(project):
        project.status = "ACTIVE"
        project.current_state_summary = (
            "Company readiness recovered and the existing CEO-delegated continuation Mission was approved without replanning."
        )
        project.next_milestone = operation.objective
    db.session.commit()
    return {
        "status": "CONTINUATION_APPROVED_AFTER_RECOVERY",
        "project_id": project.id, "operation_id": operation.id,
    }


def _reconcile_authority_exhausted_outcome(project: Project, operation: Operation, management: Work) -> dict:
    """Use only existing/local evidence after Founder rejects more budget.

    AUTHORITY_EXHAUSTED is a spending boundary, not a ban on deterministic
    reconciliation. The Company may reuse accepted Artifacts and run zero-cost
    host verification, but it may not buy another model review or implementation
    attempt. If those existing proofs already satisfy the Project Contract, close
    Result Ready; otherwise remain honestly BLOCKED inside the rejected cap.
    """
    outcome = __import__(
        "eason_one.services.project_outcome",
        fromlist=["evaluate", "refresh_deterministic_http_evidence", "close_result_ready"],
    )
    try:
        evaluation = outcome.evaluate(project)
    except ValueError as exc:
        project.status = "BLOCKED"
        project.current_state_summary = f"Authority is exhausted and existing evidence cannot be reconciled: {exc}"
        project.next_milestone = "Founder authority remains unchanged; no paid continuation may start."
        db.session.commit()
        return {"status": "AUTHORITY_EXHAUSTED", "project_id": project.id, "reason": str(exc)}

    if evaluation.get("accepted_work_ids"):
        refresh = outcome.refresh_deterministic_http_evidence(project)
        if refresh.get("new_proofs"):
            evaluation = outcome.evaluate(project)

    # A blocked, unchanged Project is quiescent truth, not a productive
    # scheduler action. Persist the evaluated evidence basis once so it cannot
    # starve later Projects on every Company tick.
    evaluation_hash = hashlib.sha256(json.dumps(
        evaluation, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()
    control = dict(management.runtime_control_json or {})
    previous_hash = control.get("authority_exhausted_evaluation_hash")
    if (
        project.status == "BLOCKED"
        and previous_hash == evaluation_hash
        and evaluation.get("overall_status") != "SATISFIED"
    ):
        return None

    if evaluation.get("overall_status") == "SATISFIED":
        work_runtime.resolve_waits(
            management, "AUTHORITY_EXHAUSTED",
            note="Existing accepted evidence satisfied the Project Contract without additional Founder budget.",
        )
        # The rejected-budget wait has served its purpose: it blocked new paid
        # continuation. Once existing evidence independently satisfies the
        # Contract, keeping the Project lifecycle at BLOCKED would manufacture
        # an unclassified blocker and prevent the CEO from making the narrow
        # READY_FOR_OUTCOME_CHECK judgement. Do not reactivate across any other
        # whole-Project hard blocker; the shared runtime predicate remains the
        # lifecycle authority for ACTIVE projection. Clearing this stale wait
        # does not restore budget authority or authorize any provider work.
        if not work_runtime.project_can_activate(project):
            db.session.commit()
            return None
        project.status = "ACTIVE"
        project.current_state_summary = (
            "Existing accepted evidence now satisfies the Project Contract without additional Founder budget; "
            "the deterministic Result Ready gate awaits current CEO outcome-check judgement."
        )
        project.next_milestone = "CEO outcome-check judgement, then deterministic Result Ready validation."
        # Persist the zero-cost authority reconciliation before asking the CEO;
        # a pending/replayed review-required fact must never leave lifecycle
        # cleanup floating in the session until some unrelated later commit.
        db.session.commit()
        ready = _close_result_ready_after_ceo_review(project, evaluation, outcome)
        if ready is None:
            return None
        emit(
            "PROJECT_CLOSED_INSIDE_REJECTED_BUDGET", actor_type="RUNTIME",
            project_id=project.id, work_id=management.id, correlation_id=f"project:{project.id}",
            payload={
                "authority_granted": False,
                "paid_continuation_started": False,
                "accepted_work_ids": evaluation.get("accepted_work_ids") or [],
                "contract_hash": evaluation.get("contract_hash"),
            },
        )
        db.session.commit()
        return ready

    project.status = "BLOCKED"
    project.current_state_summary = (
        "Founder declined additional Project budget. Existing accepted/local evidence is insufficient "
        "to satisfy every Project success criterion, so the Company is stopped inside the current cap."
    )
    project.next_milestone = "No paid continuation is authorized; Founder may reconsider budget later or cancel the Project."
    control["authority_exhausted_evaluation_hash"] = evaluation_hash
    management.runtime_control_json = control
    db.session.commit()
    return {
        "status": "AUTHORITY_EXHAUSTED", "project_id": project.id,
        "evaluation": evaluation, "paid_continuation_started": False,
    }


def _advance_project(project: Project) -> dict | None:
    if project.status in PROJECT_TERMINAL or project.status == "PAUSED":
        return None
    if project.status == "REVIEW":
        outcome = __import__(
            "eason_one.services.project_outcome", fromlist=["result_ready_proof"]
        )
        try:
            if outcome.result_ready_proof(project):
                return None
        except Exception as exc:
            operation = _latest_operation(project)
            management = _management_work(operation) if operation and operation.works else None
            return _project_system_block(
                project,
                f"REVIEW state could not validate current Project Result proof: {exc}",
                work=management,
                wait_type="RECONCILIATION",
                issue_code=_project_result_proof_reconciliation_issue_code(project),
            )

        # A REVIEW Project with no current proof is not a Founder decision gate.
        # Result Ready was a projection of Company evidence; if that evidence
        # changes during reconciliation, the Company must withdraw the stale
        # projection and resume outcome verification/continuation automatically
        # inside the same immutable Founder Contract.  Only inability to recover
        # the governing runtime lineage is a system block.
        operation = _latest_operation(project)
        if not operation or not is_kernel_operation(operation):
            management = _management_work(operation) if operation and operation.works else None
            return _project_system_block(
                project,
                "Stale Project Result proof cannot be reconciled because no current v0.20 Company Mission lineage is available.",
                work=management,
                wait_type="RECONCILIATION",
            )
        project.status = "ACTIVE"
        project.current_state_summary = (
            "The prior Result Ready proof is no longer current because its accepted evidence basis changed. "
            "Company outcome reconciliation resumed automatically inside the existing Founder Project Contract."
        )
        project.next_milestone = "Re-verify current accepted evidence and continue only if the Founder Project Contract is not yet satisfied."
        emit(
            "PROJECT_RESULT_RECONCILIATION_STARTED", actor_type="RUNTIME",
            project_id=project.id, correlation_id=f"project:{project.id}",
            payload={"reason": "STALE_PROJECT_RESULT_PROOF", "operation_id": operation.id},
        )
        db.session.commit()
        # Deliberately fall through in the same Company tick. The ordinary
        # terminal-Mission outcome path below owns deterministic host refresh,
        # independent semantic review and bounded continuation; do not invent a
        # second recovery workflow here.
    governance = __import__(
        "eason_one.services.governance", fromlist=["attention"]
    )
    if governance.attention(project):
        # Only canonical Founder authority can freeze whole-Project advancement.
        # Generic/internal Escalation rows are audit/recovery evidence and must
        # be reconciled by their Work owner rather than deadlocking the Project.
        return None
    if _has_active_delivery(project):
        return None
    operation = _latest_operation(project)
    if not operation or not is_kernel_operation(operation):
        return None
    if operation.approved_at is None:
        return _resume_ready_delegated_continuation(project, operation)
    management = _management_work(operation)
    if work_runtime.has_open_gate(management):
        repaired = _reconcile_repaired_system_recovery(project, operation, management)
        if repaired:
            db.session.commit()
        remaining_gates = work_runtime.open_gates(management)
        if remaining_gates:
            # A rejected budget gate stops *spending*, not deterministic outcome
            # reconciliation. All other management gates continue to block here.
            if all(row.get("condition_type") == "AUTHORITY_EXHAUSTED" for row in remaining_gates):
                return _reconcile_authority_exhausted_outcome(project, operation, management)
            return None

    failure_reason = None
    failure_mode = None
    if _mission_has_failure(operation):
        # advance_once reaches Project sequencing only after no delivery Work is
        # runnable. Therefore one terminal failed branch is enough to close this
        # bounded Mission and cancel descendants that can no longer satisfy their
        # dependency contract; waiting for every blocked descendant to become
        # terminal would deadlock continuation forever.
        if operation.status != "FAILED":
            mission_result = _mark_mission_failed(operation)
            return mission_result
        memory = dict(operation.memory_json or {})
        failure_mode = str(memory.get("continuation_failure_mode") or "GENERAL")
        failure_reason = str(memory.get("continuation_failure_reason") or "").strip() or None
        if not failure_reason:
            failed = next((row for row in _delivery(operation) if row.state in {"ABANDONED", "CANCELLED"}), None)
            run = AgentRun.query.filter_by(work_id=getattr(failed, "id", None)).order_by(AgentRun.id.desc()).first() if failed else None
            failure_reason = (
                getattr(run, "resolution_note", None)
                or getattr(run, "error_text", None)
                or getattr(run, "failure_reason", None)
                or "Prior bounded Mission failed."
            )
    elif _mission_terminal(operation):
        meeting_result = _meeting_gate(operation)
        if meeting_result is not None:
            return {"status": "MEETING", "operation_id": operation.id, **meeting_result}
        if operation.status != "COMPLETED":
            return _mark_mission_complete(operation)
    else:
        return None

    outcome = __import__("eason_one.services.project_outcome", fromlist=[
        "evaluate", "needs_semantic_review", "close_result_ready",
        "mark_continuation_needed", "refresh_deterministic_http_evidence",
    ])
    try:
        evaluation = outcome.evaluate(project)
    except ValueError as exc:
        return _project_system_block(
            project, str(exc), work=_management_work(operation), wait_type="RECONCILIATION",
            issue_code=_project_outcome_evaluation_reconciliation_issue_code(project, operation),
        )

    if evaluation["overall_status"] == "SATISFIED":
        return _close_result_ready_after_ceo_review(project, evaluation, outcome)

    # Missing deterministic HTTP proof is a local verification problem, not a
    # reason to buy another Engineer/Codex Mission.  Re-check the already
    # accepted repository artifact over a real isolated loopback HTTP server
    # before any paid Project continuation is planned.
    refreshed_host_proof = False
    if evaluation.get("accepted_work_ids"):
        refresh = outcome.refresh_deterministic_http_evidence(project)
        if refresh.get("new_proofs"):
            refreshed_host_proof = True
            evaluation = outcome.evaluate(project)
            if evaluation["overall_status"] == "SATISFIED":
                return _close_result_ready_after_ceo_review(project, evaluation, outcome)

    if evaluation.get("accepted_work_ids") and outcome.needs_semantic_review(project):
        clearance = _ensure_ceo_management_clearance(project, "REQUEST_VERIFICATION")
        if not clearance.get("authorized"):
            return None
        # A failed bounded Mission is evidence about that Mission, not permission
        # to skip Project-level review of evidence that already exists. This is
        # especially important after deterministic host proof is recovered out of
        # band (for example during restart reconciliation): review the current
        # evidence packet before buying another Engineer/Codex continuation.
        review = _run_project_outcome_review(
            project, operation,
            management_decision_id=clearance.get("decision_id"),
        )
        if review.get("status") not in {"PROJECT_REVIEWED", "REVIEW_REUSED"}:
            return review
        evaluation = outcome.evaluate(project)
        if evaluation["overall_status"] == "SATISFIED":
            return _close_result_ready_after_ceo_review(project, evaluation, outcome)

    outcome.mark_continuation_needed(project, evaluation, reason=(
        "A bounded Mission failed; CEO is using the failure evidence to choose another approach inside Founder authority."
        if failure_reason else None
    ))
    current_review = __import__(
        "eason_one.services.ceo_review", fromlist=["project_review"]
    ).project_review(project) if _ceo_management_cutover_active() else None
    if current_review is not None:
        current_recommendation = str(current_review.get("recommendation") or "")
        if current_recommendation in {"QUIESCE", "ESCALATE", "RECOMMEND_STOP"}:
            _ensure_ceo_management_clearance(project, current_recommendation)
            return None
        if current_recommendation != "REPLAN":
            _ensure_ceo_management_clearance(project, current_recommendation)
            return None
    clearance = _ensure_ceo_management_clearance(project, "REPLAN")
    if not clearance.get("authorized"):
        return None
    return _plan_continuation(
        project, operation, evaluation,
        failure_reason=failure_reason, failure_mode=failure_mode,
        management_decision_id=clearance.get("decision_id"),
    )


def advance_once() -> dict:
    # Team formation is Company management truth and must settle before topology
    # or execution. It can reassign not-yet-started Work to an existing
    # Persistent Employee or create one explicit capability-gap HiringRequest.
    team = __import__(
        "eason_one.services.team_formation", fromlist=["reconcile_pending_team"]
    ).reconcile_pending_team()
    if team is not None:
        return {"kind": "TEAM_FORMATION", **team}

    # Multi-Employee topology must exist before any branch executes. This reuses
    # the already-proven bounded orchestrator instead of reinventing an agent graph.
    topology = _topology_operation()
    if topology is not None:
        return {"kind": "ORCHESTRATION", **_plan_work_topology(topology)}

    works = _eligible_works(max_parallelism=4)
    if len(works) > 1:
        return {"kind": "WORK_WAVE", **_dispatch_work_wave(works)}
    if len(works) == 1:
        return {"kind": "WORK", **_dispatch_work(works[0])}

    # Reuse the existing HR/Hiring subsystem as current company work. Staffing
    # does not preempt runnable delivery, but when a capability request is the
    # next company-owned action the v0.20 kernel advances it instead of falling
    # back to the retired Operation decision loop.
    hiring = _hiring_request_due()
    if hiring is not None:
        return {"kind": "HIRING", **_advance_hiring(hiring)}

    # When no Work is immediately runnable, give an already-approved Company
    # Meeting one bounded coordination step. This lets real cross-role conflict
    # affect a later retry/handoff instead of waiting until the entire Mission is
    # already over. Independent runnable sibling Work always keeps priority.
    projects = Project.query.filter(
        Project.environment == "LIVE",
        Project.status.in_(["ACTIVE", "PLANNING", "BLOCKED", "REVIEW"]),
    ).order_by(Project.id).all()
    for project in projects:
        operation = _latest_operation(project)
        if not operation or operation.approved_at is None or not is_kernel_operation(operation):
            continue
        meeting_result = _meeting_gate(operation)
        if meeting_result is not None:
            return {"kind": "MEETING", "operation_id": operation.id, **meeting_result}

    # Advance Project-level sequencing only when no Work/Meeting action is due.
    for project in projects:
        result = _advance_project(project)
        if result is not None:
            return {"kind": "PROJECT", **result}
    return {"kind": "IDLE"}


def advance_batch(max_steps: int = 8) -> dict:
    """Run a productive batch so small Projects do not sleep between stages."""
    rows = []
    max_steps = max(1, min(int(max_steps or 1), 16))
    for _ in range(max_steps):
        result = advance_once()
        rows.append(result)
        if result.get("kind") == "IDLE":
            break
        # Founder attention or reconciliation on one Work/Project is not a
        # process-wide stop signal. The next bounded step may belong to another
        # independent Employee or Project. Eligibility/ Governance decide what
        # is lawful to run; the batch loop must not recreate the old single-agent
        # "one blocked branch stops the company" behavior.
    last = rows[-1] if rows else {"kind": "IDLE"}
    return {"kind": last.get("kind", "IDLE"), "last": last, "steps": rows, "productive_steps": sum(row.get("kind") != "IDLE" for row in rows)}
