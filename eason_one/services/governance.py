"""Canonical Founder / Company Governance for Eason One Company Core v0.20.

This module owns *Founder authority questions*.  Runtime failures, Work waits,
legacy Operation projections, and generic Proposals are not Founder authority by
name.  A Founder gate exists only when a precise, durable authority boundary is
identified here.

The durable objects are intentionally existing first-class Company Core tables:
Escalation = the frozen authority question, Decision = the committed answer,
CompanyEvent = append-only observable history, Founder Project Contract = the
Project terms/authority ledger.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from typing import Any

from ..extensions import db
from ..models import Decision, Escalation, Operation, Project, Work, now
from .company_events import emit
from .text_normalization import clean_rows, clean_text

# Founder-only questions.  Generic strings such as FOUNDER_AUTHORITY,
# PROJECT_AUTHORITY or AUTHORITY_BLOCKED are deliberately absent: callers must
# identify the exact boundary instead of escalating an internal/system problem.
FOUNDER_ONLY_TYPES = {
    "BUDGET_AUTHORIZATION",
    "PROJECT_DEADLINE_CHANGE",
    "PROJECT_SCOPE_CHANGE",
    "PROJECT_CONSTRAINT_CHANGE",
    "PROJECT_CANCEL",
    "CODEX_RISK_APPROVAL",
    "EXTERNAL_EFFECT_AUTHORIZATION",
}

# These change or terminate the Founder Project Contract and therefore have
# Project scope even when one Work/Mission discovered the need. Work/Operation
# ids on Escalation are provenance only; they do not narrow the authority.
PROJECT_SCOPED_TYPES = {
    "BUDGET_AUTHORIZATION",
    "PROJECT_DEADLINE_CHANGE",
    "PROJECT_SCOPE_CHANGE",
    "PROJECT_CONSTRAINT_CHANGE",
    "PROJECT_CANCEL",
}

EXECUTION_SCOPED_TYPES = {
    "CODEX_RISK_APPROVAL",
    "EXTERNAL_EFFECT_AUTHORIZATION",
}


class FounderAuthorityPreviouslyRejected(ValueError):
    """The Founder already declined this authority under current Project truth.

    This is a negative authority receipt, not a transient runtime error. Company
    execution must replan/stop inside the existing Contract instead of reopening
    the same Founder question until governing authority materially changes.
    """

    def __init__(self, *, decision_id: int, authority_type: str):
        self.decision_id = int(decision_id)
        self.authority_type = normalize_type(authority_type)
        super().__init__(
            f"FOUNDER_AUTHORITY_PREVIOUSLY_REJECTED:{self.authority_type}:decision:{self.decision_id}"
        )

# Narrow compatibility rename. We do not map generic PROJECT_AUTHORITY because
# that would recreate the exact "authority-looking string => Founder" bug.
_COMPAT_TYPE = {
    "PROJECT_DEADLINE_AUTHORITY": "PROJECT_DEADLINE_CHANGE",
}


def normalize_type(value: str | None) -> str:
    normalized = str(value or "").strip().upper()
    return _COMPAT_TYPE.get(normalized, normalized)


def is_founder_type(value: str | None) -> bool:
    return normalize_type(value) in FOUNDER_ONLY_TYPES


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _normalize_reason(reason: str) -> str:
    return clean_text(reason, multiline=False).casefold()


def _as_project(project: Project | int) -> Project:
    if isinstance(project, int):
        project = db.session.get(Project, project)
    if not project:
        raise ValueError("PROJECT_NOT_FOUND")
    return project


def _as_positive_money(value, *, error: str) -> Decimal:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(error) from exc
    if amount <= 0:
        raise ValueError(error)
    return amount


def _option_list(options) -> list:
    if options is None:
        return []
    if isinstance(options, list):
        return options
    # Historical rows occasionally used a single object. Keep them readable.
    return [options]


def approve_option(escalation: Escalation) -> dict:
    """Return the exact payload frozen into APPROVE, never a newly guessed value."""
    for row in _option_list(escalation.options_json):
        if isinstance(row, dict) and str(row.get("action") or "").upper() == "APPROVE":
            return dict(row)
    return {}


def gate_identity(*, project_id: int, escalation_type: str, reason: str,
                  options=None, work_id: int | None = None) -> str:
    kind = normalize_type(escalation_type)
    # Authority identity is the exact frozen decision payload, not explanatory
    # prose. Equivalent retries with different error wording must reuse the same
    # Founder question instead of manufacturing duplicate authority.
    identity = {
        "project_id": int(project_id),
        "authority_type": kind,
        "options": _option_list(options),
    }
    # Only an execution-scoped exception is tied to one exact Work. Project
    # amendments stay Project-scoped even when a Work discovered the shortfall.
    if kind in EXECUTION_SCOPED_TYPES:
        identity["work_id"] = int(work_id or 0)
    return _hash(identity)


def identity_for(escalation: Escalation) -> str:
    return gate_identity(
        project_id=escalation.project_id,
        escalation_type=escalation.escalation_type,
        reason=escalation.reason,
        options=escalation.options_json,
        work_id=escalation.work_id,
    )


def _validate_gate(project: Project, kind: str, options, work: Work | None) -> None:
    if kind not in FOUNDER_ONLY_TYPES:
        raise ValueError(f"NOT_FOUNDER_AUTHORITY:{kind or 'UNKNOWN'}")

    # Result Ready is the Founder acceptance boundary. Terms/execution cannot
    # be changed after evidence has been declared ready merely to make it fit.
    # REVIEW still permits the explicit terminal choice to cancel the whole
    # Project; an already terminal COMPLETED/CANCELLED Project cannot be rewritten.
    if project.status == "COMPLETED":
        raise ValueError("PROJECT_ALREADY_COMPLETED")
    if project.status == "CANCELLED":
        raise ValueError("PROJECT_ALREADY_CANCELLED")
    if project.status == "REVIEW" and kind != "PROJECT_CANCEL":
        raise ValueError("PROJECT_RESULT_READY_AUTHORITY_LOCKED")

    approved = {}
    for row in _option_list(options):
        if isinstance(row, dict) and str(row.get("action") or "").upper() == "APPROVE":
            approved = row
            break

    if kind == "BUDGET_AUTHORIZATION":
        _as_positive_money(
            approved.get("additional_budget_twd"),
            error="FOUNDER_BUDGET_GATE_REQUIRES_EXACT_POSITIVE_AMOUNT",
        )
        if str(approved.get("scope") or "PROJECT").upper() != "PROJECT":
            raise ValueError("V020_BUDGET_AUTHORITY_MUST_BE_PROJECT_SCOPED")
    elif kind == "PROJECT_DEADLINE_CHANGE":
        if not approved.get("new_deadline"):
            raise ValueError("PROJECT_DEADLINE_GATE_REQUIRES_EXACT_DEADLINE")
    elif kind == "PROJECT_SCOPE_CHANGE":
        objective = clean_text(approved.get("objective"), multiline=False)
        criteria = clean_rows(approved.get("success_criteria"))
        if not objective and not criteria:
            raise ValueError("PROJECT_SCOPE_GATE_REQUIRES_EXACT_TERMS")
    elif kind == "PROJECT_CONSTRAINT_CHANGE":
        if "constraints" not in approved or not isinstance(approved.get("constraints"), list):
            raise ValueError("PROJECT_CONSTRAINT_GATE_REQUIRES_EXACT_CONSTRAINTS")
    elif kind in EXECUTION_SCOPED_TYPES:
        if not work or work.project_id != project.id:
            raise ValueError("EXECUTION_AUTHORITY_REQUIRES_EXACT_WORK")
        action = approved.get("requested_action")
        if not isinstance(action, dict) or not action:
            raise ValueError("EXECUTION_AUTHORITY_REQUIRES_EXACT_ACTION")
        supplied = approved.get("requested_action_hash")
        if supplied and supplied != _hash(action):
            raise ValueError("EXECUTION_AUTHORITY_ACTION_HASH_MISMATCH")


def _sanitize_authority_payload(kind: str, payload: dict | None) -> dict:
    """Clean transport noise without changing the meaning of exact authority.

    Execution exception payloads are hashed exact actions and are therefore left
    structurally untouched. Project amendment text is safe to normalize only for
    Unicode/control transport artifacts before it becomes immutable authority.
    """
    value = dict(payload or {})
    if kind == "PROJECT_SCOPE_CHANGE":
        if "objective" in value:
            value["objective"] = clean_text(value.get("objective"), multiline=False)
        if "success_criteria" in value:
            value["success_criteria"] = clean_rows(value.get("success_criteria"))
    elif kind == "PROJECT_CONSTRAINT_CHANGE":
        if "constraints" in value:
            value["constraints"] = clean_rows(value.get("constraints"))
    elif kind == "PROJECT_DEADLINE_CHANGE" and "new_deadline" in value:
        value["new_deadline"] = clean_text(value.get("new_deadline"), multiline=False)
    return value


def _default_options(kind: str, authority_payload: dict | None) -> list:
    payload = _sanitize_authority_payload(kind, authority_payload)
    payload["action"] = "APPROVE"
    if kind in EXECUTION_SCOPED_TYPES and isinstance(payload.get("requested_action"), dict):
        payload.setdefault("requested_action_hash", _hash(payload["requested_action"]))
    return [payload, {"action": "REJECT"}]


def _sanitize_options(kind: str, options) -> list:
    """Normalize Project amendment text before identity/freeze, including explicit options."""
    result = []
    for row in _option_list(options):
        if not isinstance(row, dict):
            result.append(row)
            continue
        action = str(row.get("action") or "").upper()
        if action == "APPROVE":
            cleaned = _sanitize_authority_payload(kind, row)
            cleaned["action"] = "APPROVE"
            if kind in EXECUTION_SCOPED_TYPES and isinstance(cleaned.get("requested_action"), dict):
                cleaned.setdefault("requested_action_hash", _hash(cleaned["requested_action"]))
            result.append(cleaned)
        else:
            # REJECT has no authority payload. Preserve only its explicit action
            # instead of letting arbitrary text become part of gate identity.
            result.append({"action": action or "REJECT"})
    return result


def _current_rejection(project: Project, *, kind: str, gate_identity_value: str) -> Decision | None:
    """Return a still-binding negative Founder authority decision.

    Exact execution exceptions are suppressed only for the same exact payload.
    Project budget rejection is stronger: while the governing Contract and
    budget-authority ledger are unchanged, it means the current cap remains the
    cap. Automated Company code may not keep asking with a slightly different
    shortfall amount until the Founder eventually accepts.
    """
    snapshot = _project_authority_snapshot(project)
    for decision in Decision.query.filter_by(project_id=project.id, state="COMMITTED").order_by(Decision.id.desc()).all():
        basis = _parse_basis(decision)
        if str(basis.get("action") or decision.decision or "").upper() != "REJECT":
            continue
        if normalize_type(basis.get("authority_type")) != kind:
            continue
        if basis.get("governing_contract_hash") != snapshot.get("governing_contract_hash"):
            continue
        if basis.get("budget_authority_hash") != snapshot.get("budget_authority_hash"):
            continue
        if kind == "BUDGET_AUTHORIZATION":
            return decision
        if basis.get("gate_identity") == gate_identity_value:
            return decision
    return None


def open_gate(*, project: Project | int, escalation_type: str, reason: str,
              work: Work | None = None, operation: Operation | None = None,
              created_by_employee_id: int | None = None, options=None,
              authority_payload: dict | None = None,
              recommendation: str | None = None,
              allow_founder_reconsideration: bool = False) -> Escalation:
    """Open/deduplicate one exact Founder authority question.

    Different exact payload => a different durable queued question.
    Same exact payload => idempotent reuse. Existing unanswered authority keeps
    Founder focus until it is resolved; newly discovered needs wait behind it.
    """
    project = _as_project(project)
    kind = normalize_type(escalation_type)
    options = _sanitize_options(kind, options) if options is not None else _default_options(kind, authority_payload)
    _validate_gate(project, kind, options, work)

    new_identity = gate_identity(
        project_id=project.id, escalation_type=kind, reason=reason,
        options=options, work_id=getattr(work, "id", None),
    )
    if not allow_founder_reconsideration:
        rejected = _current_rejection(project, kind=kind, gate_identity_value=new_identity)
        if rejected is not None:
            emit(
                "FOUNDER_GATE_REOPEN_SUPPRESSED", actor_type="RUNTIME",
                project_id=project.id, work_id=getattr(work, "id", None),
                decision_id=rejected.id, correlation_id=f"project:{project.id}",
                payload={
                    "authority_type": kind,
                    "rejected_decision_id": rejected.id,
                    "attempted_gate_identity": new_identity,
                    "reason": str(reason or "")[:1200],
                },
            )
            db.session.flush()
            raise FounderAuthorityPreviouslyRejected(
                decision_id=rejected.id, authority_type=kind
            )
    open_rows = Escalation.query.filter_by(project_id=project.id, state="OPEN").order_by(Escalation.id).all()
    for row in open_rows:
        if not is_founder_type(row.escalation_type):
            continue
        if identity_for(row) == new_identity:
            return row

    # UI may focus one Founder question at a time, but authority truth is a
    # durable FIFO queue. A newly discovered exact boundary must never silently
    # resolve or jump ahead of a different unanswered Founder question.
    # ``attention()`` exposes the oldest OPEN gate per Project; later needs remain
    # authoritative and surface only after earlier Founder questions are answered.
    queued_behind = [row.id for row in open_rows if is_founder_type(row.escalation_type)]

    row = Escalation(
        project_id=project.id,
        # Project authority remains project-scoped; ids are provenance only.
        work_id=getattr(work, "id", None),
        operation_id=getattr(operation, "id", None),
        escalation_type=kind,
        state="OPEN",
        reason=clean_text(reason, multiline=False),
        options_json=options,
        recommendation=recommendation,
        created_by_employee_id=created_by_employee_id,
    )
    db.session.add(row)
    db.session.flush()
    emit(
        "FOUNDER_GATE_OPENED", actor_type="EMPLOYEE" if created_by_employee_id else "RUNTIME",
        actor_id=created_by_employee_id, project_id=project.id,
        work_id=getattr(work, "id", None), correlation_id=f"project:{project.id}",
        payload={
            "escalation_id": row.id, "authority_type": kind,
            "gate_identity": new_identity,
            "authority_scope": "PROJECT" if kind in PROJECT_SCOPED_TYPES else "WORK",
            "provenance_operation_id": getattr(operation, "id", None),
            "provenance_work_id": getattr(work, "id", None),
            "queued_behind_escalation_ids": queued_behind,
        },
    )
    if kind in PROJECT_SCOPED_TYPES and project.status not in {"REVIEW", "COMPLETED", "CANCELLED"}:
        project.status = "BLOCKED"
        project.current_state_summary = str(reason or "Founder Project authority decision required.")[:1400]
        project.next_milestone = "Founder Project authority is required before Project-wide execution may continue."
    elif kind in EXECUTION_SCOPED_TYPES and project.status == "ACTIVE":
        # Keep the company alive: this exact Work waits, while unrelated Work
        # inside unchanged Project authority may continue. Founder attention is
        # still projected from the durable Escalation queue.
        project.current_state_summary = (
            "One Work requires an exact Founder execution decision; other authorized company Work may continue."
        )
    db.session.flush()
    return row


def current_gate(project: Project | int) -> Escalation | None:
    """Return the oldest *actionable* Founder authority question for the Project.

    Terminal Project rows remain immutable audit history.  An OPEN Escalation
    that survived an older completion/cancellation path must never reappear as
    current Founder attention merely because its historical row was preserved.
    """
    project = _as_project(project)
    if str(project.status or "").upper() in {"COMPLETED", "CANCELLED", "FAILED"}:
        return None
    rows = Escalation.query.filter_by(project_id=project.id, state="OPEN").order_by(Escalation.id.asc()).all()
    founder_rows = [row for row in rows if is_founder_type(row.escalation_type)]
    # A newer exact proposal of the same authority type replaces the actionable
    # proposal while preserving the older row as unresolved audit truth. Across
    # different authority types, Founder questions remain FIFO.
    latest_by_type = {}
    for row in founder_rows:
        latest_by_type[normalize_type(row.escalation_type)] = row
    return min(latest_by_type.values(), key=lambda row: row.id) if latest_by_type else None


def project_blocking_gate(project: Project | int) -> Escalation | None:
    """Return the oldest Project-scoped authority gate, if any.

    Execution-scoped exceptions (for example one Work's risky Codex action)
    block only that exact Work. They must not freeze unrelated Employees in the
    same Project merely because Founder attention is required for one branch.
    """
    project = _as_project(project)
    if str(project.status or "").upper() in {"COMPLETED", "CANCELLED", "FAILED"}:
        return None
    rows = Escalation.query.filter_by(project_id=project.id, state="OPEN").order_by(Escalation.id.asc()).all()
    return next(
        (row for row in rows if is_founder_type(row.escalation_type)
         and normalize_type(row.escalation_type) in PROJECT_SCOPED_TYPES),
        None,
    )


def blocks_work(work: Work | None) -> bool:
    """Return whether unresolved Founder authority blocks this exact Work.

    Project-scoped gates freeze new execution across the Project because budget,
    scope, constraints, deadline or cancellation authority may change what Work
    is lawful. Execution-scoped exceptions block only their exact Work. This is
    enforced centrally instead of relying on a best-effort UI/Wait projection.
    """
    if not work or not work.project_id:
        return False
    project = getattr(work, "project", None) or db.session.get(Project, work.project_id)
    if project is not None and str(project.status or "").upper() in {"COMPLETED", "CANCELLED", "FAILED"}:
        return False
    rows = Escalation.query.filter_by(project_id=work.project_id, state="OPEN").order_by(Escalation.id).all()
    for row in rows:
        kind = normalize_type(row.escalation_type)
        if kind in PROJECT_SCOPED_TYPES:
            return True
        if kind in EXECUTION_SCOPED_TYPES and row.work_id == work.id:
            return True
    return False


def attention(project: Project | int | None = None) -> list[Escalation]:
    """Return one focused Founder authority question per Project.

    Multiple different exact needs may remain durably OPEN as a FIFO queue;
    Founder attention shows only the oldest unanswered item. Resolving it reveals
    the next queued item without deleting or reordering authority truth.
    """
    query = Escalation.query.filter_by(state="OPEN")
    if project is not None:
        project = _as_project(project)
        query = query.filter_by(project_id=project.id)
    project_ids = sorted({
        row.project_id for row in query.order_by(Escalation.id.asc()).all()
        if is_founder_type(row.escalation_type)
    })
    result = [current_gate(project_id) for project_id in project_ids]
    return sorted((row for row in result if row is not None), key=lambda row: row.id)


def exact_action_payload(*, work: Work, authority_type: str, reason: str,
                         risks=None, extra: dict | None = None) -> dict:
    """Canonical execution exception payload used by gate and receipt."""
    payload = {
        "authority_type": normalize_type(authority_type),
        "project_id": work.project_id,
        "work_id": work.id,
        "operation_id": work.operation_id,
        "work_title": work.title,
        "reason": clean_text(reason, multiline=False),
        "risks": clean_rows(risks),
    }
    payload.update(dict(extra or {}))
    return payload


def _project_authority_snapshot(project: Project) -> dict:
    contract = __import__(
        "eason_one.services.project_contract", fromlist=["governing_terms", "effective_authority"]
    )
    terms = contract.governing_terms(project)
    authority = contract.effective_authority(project)
    return {
        "base_contract_hash": terms.get("base_contract_hash") or terms.get("contract_hash"),
        "governing_contract_hash": terms.get("governing_contract_hash") or terms.get("contract_hash"),
        "budget_authority_hash": authority.get("authority_hash"),
        "effective_budget_limit_twd": authority.get("effective_budget_limit_twd"),
        "effective_deadline": terms.get("deadline"),
    }


def _decision_basis(escalation: Escalation, *, action: str, approved_payload: dict,
                    resolved_payload: dict | None = None, founder_changes: dict | None = None,
                    applied_effects: dict | None = None, receipt: dict | None = None) -> dict:
    project = escalation.project or db.session.get(Project, escalation.project_id)
    snapshot = _project_authority_snapshot(project)
    return {
        "version": "FOUNDER_GOVERNANCE_DECISION_V2",
        "authority": "FOUNDER",
        "escalation_id": escalation.id,
        "authority_type": normalize_type(escalation.escalation_type),
        "authority_scope": "PROJECT" if normalize_type(escalation.escalation_type) in PROJECT_SCOPED_TYPES else "WORK",
        "gate_identity": identity_for(escalation),
        "action": action,
        # The original APPROVE option is immutable audit truth. APPROVE must
        # consume this payload byte-for-semantic-byte; MODIFY records a separate
        # resolved payload instead of rewriting what the gate originally asked.
        "frozen_approve_payload": approved_payload,
        "resolved_authority_payload": resolved_payload if resolved_payload is not None else approved_payload,
        "founder_changes": founder_changes or {},
        "applied_effects": applied_effects or {},
        "receipt": receipt,
        **snapshot,
    }


def _record_decision(escalation: Escalation, *, action: str, reason: str,
                     approved_payload: dict, resolved_payload: dict | None = None,
                     founder_changes: dict | None = None, applied_effects: dict | None = None,
                     receipt: dict | None = None) -> Decision:
    existing = Decision.query.filter_by(
        legacy_source="FOUNDER_ESCALATION", legacy_source_id=escalation.id
    ).first()
    if existing:
        return existing
    project = escalation.project or db.session.get(Project, escalation.project_id)
    basis = _decision_basis(
        escalation, action=action, approved_payload=approved_payload,
        resolved_payload=resolved_payload, founder_changes=founder_changes,
        applied_effects=applied_effects, receipt=receipt,
    )
    row = Decision(
        legacy_source="FOUNDER_ESCALATION", legacy_source_id=escalation.id,
        project_id=project.id, work_id=escalation.work_id,
        proposed_by_employee_id=escalation.created_by_employee_id,
        decided_by_employee_id=None,
        question=f"{normalize_type(escalation.escalation_type)}: {escalation.reason}",
        decision=action, rationale=reason,
        state="COMMITTED", authority_basis=_canonical(basis), committed_at=now(),
    )
    db.session.add(row)
    db.session.flush()
    emit(
        "FOUNDER_DECISION_COMMITTED", actor_type="FOUNDER",
        project_id=project.id, work_id=escalation.work_id,
        decision_id=row.id, correlation_id=f"project:{project.id}",
        payload={
            "escalation_id": escalation.id,
            "authority_type": normalize_type(escalation.escalation_type),
            "action": action,
            "gate_identity": identity_for(escalation),
            "governing_contract_hash": basis.get("governing_contract_hash"),
            "budget_authority_hash": basis.get("budget_authority_hash"),
        },
    )
    return row


def _parse_deadline(value: str):
    text = clean_text(value, multiline=False)
    if not text:
        raise ValueError("PROJECT_DEADLINE_GATE_REQUIRES_EXACT_DEADLINE")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("PROJECT_DEADLINE_INVALID") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _cancel_project(project: Project, *, reason: str) -> dict:
    work_runtime = __import__("eason_one.services.work_runtime", fromlist=["transition", "resolve_waits", "TERMINAL_WORK_STATES"])
    cancelled_work = []
    for work in Work.query.filter_by(project_id=project.id).order_by(Work.id).all():
        if work.state in work_runtime.TERMINAL_WORK_STATES:
            continue
        work_runtime.transition(work, "CANCELLED", actor_type="FOUNDER", reason=reason)
        work_runtime.resolve_waits(work, note="Project cancelled by Founder.")
        cancelled_work.append(work.id)
    for operation in Operation.query.filter_by(project_id=project.id).all():
        if operation.status in {"COMPLETED", "FAILED", "CANCELLED"}:
            continue
        operation.status = "CANCELLED"
        operation.kernel_status = "CANCELLED"
        operation.current_stage = "CANCELLED"
        operation.waiting_reason = reason
        operation.ended_at = operation.ended_at or now()
        operation.lease_owner = None
        operation.lease_expires_at = None
    project.status = "CANCELLED"
    project.current_state_summary = reason
    project.next_milestone = None
    emit(
        "PROJECT_CANCELLED_BY_FOUNDER", actor_type="FOUNDER", project_id=project.id,
        correlation_id=f"project:{project.id}", payload={"cancelled_work_ids": cancelled_work, "reason": reason},
    )
    return {"cancelled_work_ids": cancelled_work}


def _resolve_waits(escalation: Escalation, *, note: str) -> None:
    if escalation.work_id:
        work = db.session.get(Work, escalation.work_id)
        if work:
            runtime = __import__("eason_one.services.work_runtime", fromlist=["resolve_waits"])
            runtime.resolve_waits(
                work, "FOUNDER_DECISION", note=note,
                gate_key=f"escalation:{escalation.id}",
            )
            # Pre-keyed compatibility waits belong to the same exact Work but
            # cannot identify another Escalation. Generic resolution clears
            # only unkeyed rows and deliberately preserves every keyed gate.
            runtime.resolve_waits(work, "FOUNDER_DECISION", note=note)
            runtime.resolve_waits(work, "BUDGET", note=note)
    # Compatibility projection only. Escalation/Decision remain truth.
    if escalation.operation_id:
        operation = db.session.get(Operation, escalation.operation_id)
        if operation:
            operation.founder_report_json = None
            operation.waiting_reason = None


def apply_rejected_budget_boundary(*, project: Project, work: Work | None, decision_id: int, escalation_id: int | None = None) -> None:
    """Apply a durable negative budget decision to current execution truth.

    A delivery attempt that requires authority the Founder declined is no longer
    executable. Management instead owns one AUTHORITY_EXHAUSTED wait while it
    evaluates existing accepted evidence. This prevents the same Work from
    bouncing READY/EXECUTING and reopening budget authority.
    """
    runtime = __import__(
        "eason_one.services.work_runtime",
        fromlist=["open_wait", "transition", "sync_task_projection", "TERMINAL_WORK_STATES"],
    )
    management = work if work and work.work_type == "MANAGEMENT" else None
    if work and work.state not in runtime.TERMINAL_WORK_STATES:
        if work.work_type != "MANAGEMENT":
            runtime.transition(
                work, "CANCELLED", actor_type="FOUNDER",
                reason="Founder declined additional Project budget required by this bounded Work; "
                       "the exact attempt may not resume under the unchanged cap.",
            )
            runtime.sync_task_projection(work)
    if management is None and work and work.operation:
        management = runtime.ensure_management_work(work.operation)
    if management is None:
        operation = (
            Operation.query.filter_by(project_id=project.id)
            .filter(Operation.approved_at.isnot(None))
            .order_by(Operation.id.desc()).first()
        )
        if operation is not None:
            management = runtime.ensure_management_work(operation)
    if management is not None and management.state not in runtime.TERMINAL_WORK_STATES:
        runtime.open_wait(
            management, "AUTHORITY_EXHAUSTED",
            f"Founder rejected additional Project budget (Decision #{decision_id}); "
            "continue only if existing evidence can close inside the current cap.",
        )
    project.status = "BLOCKED"
    project.current_state_summary = (
        "Founder declined additional Project budget. Company may use existing accepted evidence, "
        "but may not resume the authority-dependent attempt or reopen the same budget question."
    )
    project.next_milestone = "Reconcile existing evidence inside the current Founder Project budget cap."
    emit(
        "FOUNDER_BUDGET_REJECTED_BOUNDARY", actor_type="FOUNDER",
        project_id=project.id, work_id=getattr(work, "id", None),
        correlation_id=f"project:{project.id}",
        payload={
            "escalation_id": escalation_id, "decision_id": int(decision_id),
            "authority_granted": False,
        },
    )


def resolve_gate(escalation: Escalation | int, action: str, *, reason: str | None = None,
                 changes: dict | None = None) -> Decision:
    """Commit one Founder decision and apply only its exact frozen authority."""
    if isinstance(escalation, int):
        escalation = db.session.get(Escalation, escalation)
    if not escalation:
        raise ValueError("FOUNDER_GATE_NOT_FOUND")
    if escalation.state != "OPEN":
        existing = Decision.query.filter_by(
            legacy_source="FOUNDER_ESCALATION", legacy_source_id=escalation.id
        ).first()
        if existing:
            return existing
        raise ValueError("FOUNDER_GATE_NOT_OPEN")
    kind = normalize_type(escalation.escalation_type)
    if kind not in FOUNDER_ONLY_TYPES:
        raise ValueError("NOT_FOUNDER_AUTHORITY")
    action = str(action or "").strip().upper()
    if action not in {"APPROVE", "REJECT", "MODIFY"}:
        raise ValueError("UNKNOWN_FOUNDER_DECISION")
    project = escalation.project or db.session.get(Project, escalation.project_id)
    if not project:
        raise ValueError("PROJECT_NOT_FOUND")
    current = current_gate(project)
    if kind != "PROJECT_CANCEL" and current is not None and current.id != escalation.id:
        raise ValueError("FOUNDER_GATE_NOT_CURRENT")

    frozen = approve_option(escalation)
    if not frozen and action in {"APPROVE", "MODIFY"}:
        raise ValueError("FOUNDER_GATE_HAS_NO_FROZEN_APPROVE_PAYLOAD")

    work = db.session.get(Work, escalation.work_id) if escalation.work_id else None
    # Authority is checked again at commit time. A gate opened while the Project
    # was ACTIVE cannot become a stale capability token after Result Ready or a
    # terminal transition. REJECT remains non-authorizing and may clear it.
    if action == "APPROVE":
        _validate_gate(project, kind, _option_list(escalation.options_json), work)

    founder_changes = dict(changes or {})
    if action == "APPROVE" and founder_changes:
        # APPROVE means approve the exact option that was frozen when the gate
        # opened. Callers are not allowed to smuggle new authority through an
        # otherwise innocent APPROVE request.
        raise ValueError("FOUNDER_APPROVE_MUST_CONSUME_FROZEN_PAYLOAD")
    if action == "REJECT" and founder_changes:
        raise ValueError("FOUNDER_REJECT_CANNOT_APPLY_AUTHORITY_CHANGES")
    if action == "MODIFY" and not founder_changes:
        raise ValueError("FOUNDER_MODIFY_REQUIRES_EXACT_CHANGES")

    payload = dict(frozen)
    if action == "MODIFY":
        payload.update(founder_changes)
        payload = _sanitize_authority_payload(kind, payload)
    payload.pop("action", None)

    # A modified proposal is a new exact authority payload and must satisfy the
    # same type-specific invariants as the original gate before any mutation is
    # applied. This keeps UI/service callers from bypassing validation.
    if action == "MODIFY":
        validation_payload = dict(payload)
        validation_payload["action"] = "APPROVE"
        _validate_gate(project, kind, [validation_payload, {"action": "REJECT"}], work)

    applied = {}
    receipt = None

    if action in {"APPROVE", "MODIFY"}:
        contracts = __import__(
            "eason_one.services.project_contract",
            fromlist=[
                "authorize_budget_extension", "authorize_deadline_change",
                "authorize_scope_change", "authorize_constraint_change",
            ],
        )
        if kind == "BUDGET_AUTHORIZATION":
            amount = _as_positive_money(
                payload.get("additional_budget_twd"),
                error="FOUNDER_BUDGET_GATE_REQUIRES_EXACT_POSITIVE_AMOUNT",
            )
            amendment = contracts.authorize_budget_extension(
                project, additional_twd=amount,
                reason=reason or escalation.reason,
                origin_employee_id=project.owner_employee_id,
                source_ref=f"project:{project.id}:founder_gate:{escalation.id}:budget",
            )
            applied = {"budget_amendment_hash": amendment.get("amendment_hash"), "additional_budget_twd": str(amount)}
        elif kind == "PROJECT_DEADLINE_CHANGE":
            amendment = contracts.authorize_deadline_change(
                project, new_deadline=_parse_deadline(payload.get("new_deadline")),
                reason=reason or escalation.reason,
                origin_employee_id=project.owner_employee_id,
                source_ref=f"project:{project.id}:founder_gate:{escalation.id}:deadline",
            )
            applied = {"deadline_amendment_hash": amendment.get("amendment_hash"), "new_deadline": amendment.get("new_deadline")}
        elif kind == "PROJECT_SCOPE_CHANGE":
            amendment = contracts.authorize_scope_change(
                project, objective=payload.get("objective"),
                success_criteria=payload.get("success_criteria"),
                reason=reason or escalation.reason,
                origin_employee_id=project.owner_employee_id,
                source_ref=f"project:{project.id}:founder_gate:{escalation.id}:scope",
            )
            applied = {"scope_amendment_hash": amendment.get("amendment_hash")}
        elif kind == "PROJECT_CONSTRAINT_CHANGE":
            amendment = contracts.authorize_constraint_change(
                project, constraints=payload.get("constraints"),
                reason=reason or escalation.reason,
                origin_employee_id=project.owner_employee_id,
                source_ref=f"project:{project.id}:founder_gate:{escalation.id}:constraints",
            )
            applied = {"constraint_amendment_hash": amendment.get("amendment_hash")}
        elif kind == "PROJECT_CANCEL":
            applied = _cancel_project(project, reason=reason or escalation.reason or "Founder cancelled the Project.")
        elif kind in EXECUTION_SCOPED_TYPES:
            requested_action = payload.get("requested_action")
            if not isinstance(requested_action, dict) or not requested_action:
                raise ValueError("EXECUTION_AUTHORITY_REQUIRES_EXACT_ACTION")
            action_hash = _hash(requested_action)
            if payload.get("requested_action_hash") and payload.get("requested_action_hash") != action_hash:
                raise ValueError("EXECUTION_AUTHORITY_ACTION_HASH_MISMATCH")
            receipt = {
                "version": "EXACT_ACTION_AUTHORITY_RECEIPT_V1",
                "authority_type": kind,
                "project_id": project.id,
                "work_id": escalation.work_id,
                "requested_action": requested_action,
                "requested_action_hash": action_hash,
            }
            applied = {"requested_action_hash": action_hash}

    decision = _record_decision(
        escalation, action=action, reason=reason or (
            "Founder approved the exact frozen authority proposal." if action == "APPROVE"
            else "Founder committed an exact modified authority proposal." if action == "MODIFY"
            else "Founder rejected the exact frozen authority proposal."
        ), approved_payload=frozen, resolved_payload=payload,
        founder_changes=founder_changes, applied_effects=applied, receipt=receipt,
    )
    escalation.state = "RESOLVED"
    escalation.resolved_at = now()
    escalation.resolution = action
    _resolve_waits(escalation, note=f"Founder decision {action} on {kind} gate #{escalation.id}.")

    if kind == "PROJECT_CANCEL" and action in {"APPROVE", "MODIFY"}:
        # Whole-Project cancellation makes every other unanswered authority
        # question moot. Retire them without fabricating Founder answers or
        # leaving terminal Projects with stale NEEDS YOU cards.
        for other in Escalation.query.filter_by(project_id=project.id, state="OPEN").order_by(Escalation.id).all():
            if other.id == escalation.id or not is_founder_type(other.escalation_type):
                continue
            invalidate_gate(
                other, resolution="PROJECT_CANCELLED",
                reason="Entire Project was cancelled by explicit Founder action; this queued authority question is no longer actionable.",
                actor_type="FOUNDER",
            )
    elif kind == "BUDGET_AUTHORIZATION" and action == "REJECT":
        apply_rejected_budget_boundary(
            project=project, work=work, decision_id=decision.id, escalation_id=escalation.id
        )
    elif kind != "PROJECT_CANCEL":
        # Project status projects that Founder attention is still queued even
        # when the next exact gate is execution-scoped. Work-level eligibility
        # remains branch-local, so unrelated siblings may still progress.
        project_gate = current_gate(project)
        if project_gate is not None:
            project.status = "BLOCKED"
            project.current_state_summary = project_gate.reason
            project.next_milestone = "Founder authority is required for the next queued exact boundary."
        elif project.status == "BLOCKED" and __import__(
            "eason_one.services.work_runtime", fromlist=["project_can_activate"]
        ).project_can_activate(project):
            project.status = "ACTIVE"
            project.current_state_summary = (
                "Founder approved the exact requested authority; company execution may continue."
                if action in {"APPROVE", "MODIFY"}
                else "Founder declined the requested authority; unaffected Work may continue inside existing Project authority."
            )
    db.session.commit()
    return decision


def invalidate_gate(escalation: Escalation | int, *, resolution: str, reason: str,
                    actor_type: str = "RUNTIME") -> Escalation:
    """Invalidate a Founder gate without fabricating a Founder Decision.

    This is reserved for cases where durable system evidence proves that the
    question itself was invalid/superseded (for example, a platform defect made
    Codex request Founder verification approval after authoritative host proof
    had already passed).  It never grants authority and never appends a Project
    Contract amendment.  All canonical Founder-gate state transitions still
    pass through this owner instead of direct Escalation mutation elsewhere.
    """
    if isinstance(escalation, int):
        escalation = db.session.get(Escalation, escalation)
    if not escalation:
        raise ValueError("FOUNDER_GATE_NOT_FOUND")
    if not is_founder_type(escalation.escalation_type):
        raise ValueError("NOT_FOUNDER_AUTHORITY")
    if escalation.state != "OPEN":
        return escalation
    resolution = str(resolution or "").strip().upper()
    if not resolution or resolution in {"APPROVE", "MODIFY"}:
        raise ValueError("FOUNDER_GATE_INVALIDATION_REQUIRES_NON_AUTHORIZING_RESOLUTION")

    escalation.state = "RESOLVED"
    escalation.resolved_at = now()
    escalation.resolution = resolution
    _resolve_waits(escalation, note=reason)
    emit(
        "FOUNDER_GATE_INVALIDATED", actor_type=actor_type,
        project_id=escalation.project_id, work_id=escalation.work_id,
        correlation_id=f"project:{escalation.project_id}",
        payload={
            "escalation_id": escalation.id,
            "authority_type": normalize_type(escalation.escalation_type),
            "resolution": resolution,
            "reason": str(reason or "")[:1200],
            "authority_granted": False,
        },
    )
    db.session.flush()
    return escalation


def _parse_basis(decision: Decision) -> dict:
    try:
        value = json.loads(decision.authority_basis or "{}")
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def approval_receipt(*, project: Project | int, work: Work | int,
                     authority_type: str, requested_action: dict) -> dict | None:
    project = _as_project(project)
    if isinstance(work, int):
        work = db.session.get(Work, work)
    if not work or work.project_id != project.id:
        return None
    kind = normalize_type(authority_type)
    action_hash = _hash(requested_action)
    # Bind receipt to current Project authority/terms. Scope or budget changes
    # invalidate an old execution exception instead of silently broadening it.
    snapshot = _project_authority_snapshot(project)
    for decision in Decision.query.filter_by(project_id=project.id, work_id=work.id, state="COMMITTED").order_by(Decision.id.desc()).all():
        basis = _parse_basis(decision)
        receipt = basis.get("receipt") or {}
        if not isinstance(receipt, dict):
            continue
        if receipt.get("authority_type") != kind:
            continue
        if receipt.get("requested_action_hash") != action_hash:
            continue
        if basis.get("governing_contract_hash") != snapshot.get("governing_contract_hash"):
            continue
        if basis.get("budget_authority_hash") != snapshot.get("budget_authority_hash"):
            continue
        return {
            **receipt,
            "decision_id": decision.id,
            "governing_contract_hash": basis.get("governing_contract_hash"),
            "budget_authority_hash": basis.get("budget_authority_hash"),
        }
    return None


def rejected_decision_for_gate(escalation: Escalation | int) -> Decision | None:
    """Return the still-binding Founder REJECT that makes this OPEN gate invalid."""
    if isinstance(escalation, int):
        escalation = db.session.get(Escalation, escalation)
    if not escalation or not escalation.project_id:
        return None
    project = escalation.project or db.session.get(Project, escalation.project_id)
    if not project:
        return None
    kind = normalize_type(escalation.escalation_type)
    if kind not in FOUNDER_ONLY_TYPES:
        return None
    return _current_rejection(
        project, kind=kind, gate_identity_value=identity_for(escalation)
    )


def request_budget_gate(*, project: Project | int, additional_twd, reason: str,
                        work: Work | None = None, operation: Operation | None = None,
                        created_by_employee_id: int | None = None,
                        allow_founder_reconsideration: bool = False) -> Escalation:
    amount = _as_positive_money(
        additional_twd, error="FOUNDER_BUDGET_GATE_REQUIRES_EXACT_POSITIVE_AMOUNT"
    )
    return open_gate(
        project=project, escalation_type="BUDGET_AUTHORIZATION", reason=reason,
        work=work, operation=operation, created_by_employee_id=created_by_employee_id,
        authority_payload={"additional_budget_twd": str(amount), "scope": "PROJECT"},
        recommendation=f"Authorize exactly the additional NT${amount} Project authority only if intended.",
        allow_founder_reconsideration=allow_founder_reconsideration,
    )


def latest_execution_receipt(*, project: Project | int, work: Work | int,
                             authority_type: str) -> dict | None:
    """Return latest still-valid exact-action receipt for one Work/type."""
    project = _as_project(project)
    if isinstance(work, int):
        work = db.session.get(Work, work)
    if not work or work.project_id != project.id:
        return None
    kind = normalize_type(authority_type)
    snapshot = _project_authority_snapshot(project)
    for decision in Decision.query.filter_by(project_id=project.id, work_id=work.id, state="COMMITTED").order_by(Decision.id.desc()).all():
        basis = _parse_basis(decision)
        receipt = basis.get("receipt") or {}
        if not isinstance(receipt, dict) or receipt.get("authority_type") != kind:
            continue
        if basis.get("governing_contract_hash") != snapshot.get("governing_contract_hash"):
            continue
        if basis.get("budget_authority_hash") != snapshot.get("budget_authority_hash"):
            continue
        return {
            **receipt,
            "decision_id": decision.id,
            "governing_contract_hash": basis.get("governing_contract_hash"),
            "budget_authority_hash": basis.get("budget_authority_hash"),
        }
    return None
