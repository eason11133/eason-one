"""Founder-approved Project Contract and append-only authority/terms ledger.

The base Contract is immutable.  Founder-approved changes are append-only
amendments.  Compatibility Project columns are projections only; vNext company
behavior must read the validated ledger through ``governing_terms`` and
``effective_authority``.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import re

from ..extensions import db
from ..models import Escalation, KnowledgeItem, Operation, Project, now
from .text_normalization import clean_rows, clean_text

CONTRACT_VERSION = "PROJECT_CONTRACT_V1"
CONTRACT_TITLE = "Founder Project Contract"
AMENDMENT_TITLE = "Founder Project Contract Amendment"
AMENDMENT_VERSION = "PROJECT_CONTRACT_AMENDMENT_V1"
LEGACY_SUCCESS_TITLE = "Project success criteria"

SUPPORTED_AMENDMENTS = {
    "BUDGET_EXTENSION",
    "DEADLINE_CHANGE",
    "SCOPE_CHANGE",
    "CONSTRAINT_CHANGE",
}


def _rows(value) -> list[str]:
    return clean_rows(value)


def _deadline(project: Project):
    value = getattr(project, "deadline", None)
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _normalize_deadline(value):
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.isoformat()


def _deadline_dt(value):
    if value in (None, ""):
        return None
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _hash(body: dict) -> str:
    raw = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _authority_body(project_id: int, base_contract_hash: str, budget,
                    budget_amendment_hashes: list[str]) -> dict:
    body = {
        "project_id": project_id,
        "base_contract_hash": base_contract_hash,
        "effective_budget_limit_twd": budget,
        "amendment_hashes": list(budget_amendment_hashes),
    }
    body["authority_hash"] = _hash(body)
    return body


def _next_governing_hash(previous_hash: str, amendment_hash: str, amendment_type: str) -> str:
    return _hash({
        "previous_governing_contract_hash": previous_hash,
        "amendment_hash": amendment_hash,
        "amendment_type": amendment_type,
    })


def _legacy_success_criteria(project_id: int) -> list[str]:
    row = (
        KnowledgeItem.query.filter_by(
            project_id=project_id, title=LEGACY_SUCCESS_TITLE, founder_approved=True
        ).order_by(KnowledgeItem.id.desc()).first()
    )
    return _rows(getattr(row, "content", None))


def _base_row(project: Project):
    return (
        KnowledgeItem.query.filter_by(
            project_id=project.id, title=CONTRACT_TITLE, founder_approved=True
        ).order_by(KnowledgeItem.id).first()
    )


def has_frozen_contract(project: Project | int) -> bool:
    if isinstance(project, int):
        project = db.session.get(Project, project)
    return bool(project and _base_row(project))


def is_vnext_governed(project: Project | int) -> bool:
    """Whether missing/corrupt Contract truth must fail closed for this Project."""
    if isinstance(project, int):
        project = db.session.get(Project, project)
    if not project:
        return False
    if has_frozen_contract(project):
        return True
    # A WORK_CORE_V018 Operation is explicit durable cutover evidence.  Do not
    # infer vNext from mutable Project columns or dates.
    for operation in Operation.query.filter_by(project_id=project.id).all():
        if (operation.memory_json or {}).get("runtime_semantics") == "WORK_CORE_V018":
            return True
    return False


def build(project: Project, *, success_criteria=None, constraints=None) -> dict:
    criteria = _rows(success_criteria)
    if not criteria:
        criteria = _legacy_success_criteria(project.id)
    constraint_rows = _rows(constraints if constraints is not None else project.known_constraints)
    body = {
        "version": CONTRACT_VERSION,
        "project_id": project.id,
        "objective": clean_text(project.objective, multiline=False),
        "success_criteria": criteria,
        "constraints": constraint_rows,
        "budget_limit_twd": (
            str(Decimal(project.real_budget_limit))
            if project.real_budget_limit is not None else None
        ),
        "deadline": _deadline(project),
    }
    body["contract_hash"] = _hash(body)
    return body


def _validate_base(project: Project, row: KnowledgeItem) -> dict:
    try:
        body = json.loads(row.content)
    except Exception as exc:
        raise ValueError("PROJECT_CONTRACT_CORRUPT") from exc
    expected = _hash({k: v for k, v in body.items() if k != "contract_hash"})
    if body.get("contract_hash") != expected:
        raise ValueError("PROJECT_CONTRACT_HASH_MISMATCH")
    if int(body.get("project_id") or 0) != project.id:
        raise ValueError("PROJECT_CONTRACT_PROJECT_MISMATCH")
    if body.get("version") != CONTRACT_VERSION:
        raise ValueError("PROJECT_CONTRACT_VERSION_MISMATCH")
    return body


def freeze(project: Project, *, success_criteria=None, constraints=None,
           origin_employee_id=None) -> dict:
    """Persist the immutable Founder base Contract once."""
    existing = _base_row(project)
    candidate = build(project, success_criteria=success_criteria, constraints=constraints)
    if not candidate["success_criteria"]:
        raise ValueError("PROJECT_CONTRACT_MISSING_SUCCESS_CRITERIA")
    if existing:
        return _validate_base(project, existing)

    row = KnowledgeItem(
        project_id=project.id,
        kind="DECISION",
        title=CONTRACT_TITLE,
        content=json.dumps(candidate, ensure_ascii=False, sort_keys=True),
        rationale="Immutable Founder-approved Project outcome and authority boundary.",
        source_ref=f"project:{project.id}:founder_contract:v1",
        origin_employee_id=origin_employee_id,
        founder_approved=True,
    )
    db.session.add(row)
    db.session.flush()
    return candidate


def get(project: Project | int, *, require=True) -> dict | None:
    if isinstance(project, int):
        project = db.session.get(Project, project)
    if not project:
        if require:
            raise ValueError("PROJECT_NOT_FOUND")
        return None
    row = _base_row(project)
    if row:
        return _validate_base(project, row)

    # vNext may never silently fall back to mutable compatibility columns.
    if is_vnext_governed(project):
        if require:
            raise ValueError("PROJECT_CONTRACT_MISSING")
        return None

    # Historical compatibility only. This synthesis is not persisted and does
    # not let a Mission completion criterion become Project truth.
    legacy = build(project)
    if legacy["success_criteria"]:
        return legacy
    if require:
        raise ValueError("PROJECT_CONTRACT_MISSING")
    return None


def _amendment_rows(project: Project):
    return (
        KnowledgeItem.query.filter_by(
            project_id=project.id, title=AMENDMENT_TITLE, founder_approved=True
        ).order_by(KnowledgeItem.id).all()
    )


def _validated_chain(project: Project, base: dict) -> tuple[list[dict], dict]:
    current_budget = (
        Decimal(str(base.get("budget_limit_twd")))
        if base.get("budget_limit_twd") not in (None, "") else None
    )
    state = {
        "objective": clean_text(base.get("objective"), multiline=False),
        "success_criteria": _rows(base.get("success_criteria")),
        "constraints": _rows(base.get("constraints")),
        "deadline": base.get("deadline"),
        "budget": current_budget,
        "governing_hash": base.get("contract_hash"),
        "budget_hashes": [],
        "all_hashes": [],
    }
    current_authority = _authority_body(
        project.id, base.get("contract_hash"), base.get("budget_limit_twd"), []
    )
    result: list[dict] = []

    for row in _amendment_rows(project):
        try:
            body = json.loads(row.content)
        except Exception as exc:
            raise ValueError("PROJECT_CONTRACT_AMENDMENT_CORRUPT") from exc
        if body.get("version") != AMENDMENT_VERSION:
            raise ValueError("PROJECT_CONTRACT_AMENDMENT_VERSION_MISMATCH")
        if int(body.get("project_id") or 0) != project.id:
            raise ValueError("PROJECT_CONTRACT_AMENDMENT_PROJECT_MISMATCH")
        if body.get("base_contract_hash") != base.get("contract_hash"):
            raise ValueError("PROJECT_CONTRACT_AMENDMENT_BASE_MISMATCH")
        amendment_type = str(body.get("amendment_type") or "").upper()
        if amendment_type not in SUPPORTED_AMENDMENTS:
            raise ValueError("PROJECT_CONTRACT_AMENDMENT_TYPE_UNSUPPORTED")

        # New amendments carry a derived governing hash. Historical budget-only
        # v1 rows did not, so their original hash algorithm remains valid.
        if "governing_contract_hash" in body:
            hash_source = {
                k: v for k, v in body.items()
                if k not in {"amendment_hash", "governing_contract_hash"}
            }
        else:
            hash_source = {k: v for k, v in body.items() if k != "amendment_hash"}
        expected_hash = _hash(hash_source)
        if body.get("amendment_hash") != expected_hash:
            raise ValueError("PROJECT_CONTRACT_AMENDMENT_HASH_MISMATCH")

        previous_governing = body.get("previous_governing_contract_hash")
        if previous_governing not in (None, state["governing_hash"]):
            raise ValueError("PROJECT_CONTRACT_AMENDMENT_GOVERNING_CHAIN_MISMATCH")
        derived_governing = _next_governing_hash(
            state["governing_hash"], body["amendment_hash"], amendment_type
        )
        if body.get("governing_contract_hash") not in (None, derived_governing):
            raise ValueError("PROJECT_CONTRACT_AMENDMENT_GOVERNING_HASH_MISMATCH")

        if amendment_type == "BUDGET_EXTENSION":
            if body.get("previous_authority_hash") != current_authority.get("authority_hash"):
                raise ValueError("PROJECT_CONTRACT_AMENDMENT_AUTHORITY_CHAIN_MISMATCH")
            previous = Decimal(str(body.get("previous_budget_limit_twd")))
            new = Decimal(str(body.get("new_budget_limit_twd")))
            if state["budget"] is None or previous != state["budget"] or new <= previous:
                raise ValueError("PROJECT_CONTRACT_AMENDMENT_CHAIN_MISMATCH")
            state["budget"] = new
            state["budget_hashes"].append(body["amendment_hash"])
            current_authority = _authority_body(
                project.id, base.get("contract_hash"), str(new), state["budget_hashes"]
            )
        elif amendment_type == "DEADLINE_CHANGE":
            previous = body.get("previous_deadline")
            if previous != state["deadline"]:
                raise ValueError("PROJECT_DEADLINE_AMENDMENT_CHAIN_MISMATCH")
            new = _normalize_deadline(body.get("new_deadline"))
            if not new or new == previous:
                raise ValueError("PROJECT_DEADLINE_AMENDMENT_INVALID")
            state["deadline"] = new
        elif amendment_type == "SCOPE_CHANGE":
            if body.get("previous_objective") != state["objective"]:
                raise ValueError("PROJECT_SCOPE_AMENDMENT_OBJECTIVE_CHAIN_MISMATCH")
            if _rows(body.get("previous_success_criteria")) != state["success_criteria"]:
                raise ValueError("PROJECT_SCOPE_AMENDMENT_CRITERIA_CHAIN_MISMATCH")
            objective = clean_text(body.get("new_objective"), multiline=False)
            criteria = _rows(body.get("new_success_criteria"))
            if not objective or not criteria:
                raise ValueError("PROJECT_SCOPE_AMENDMENT_INVALID")
            state["objective"] = objective
            state["success_criteria"] = criteria
        elif amendment_type == "CONSTRAINT_CHANGE":
            if _rows(body.get("previous_constraints")) != state["constraints"]:
                raise ValueError("PROJECT_CONSTRAINT_AMENDMENT_CHAIN_MISMATCH")
            constraints = _rows(body.get("new_constraints"))
            # Empty replacement is valid: Founder may intentionally clear all.
            if not isinstance(body.get("new_constraints"), list):
                raise ValueError("PROJECT_CONSTRAINT_AMENDMENT_INVALID")
            state["constraints"] = constraints

        state["governing_hash"] = derived_governing
        state["all_hashes"].append(body["amendment_hash"])
        normalized = dict(body)
        normalized.setdefault("previous_governing_contract_hash", previous_governing or (
            base.get("contract_hash") if len(state["all_hashes"]) == 1 else result[-1].get("governing_contract_hash")
        ))
        normalized.setdefault("governing_contract_hash", derived_governing)
        result.append(normalized)

    return result, state


def amendments(project: Project | int) -> list[dict]:
    if isinstance(project, int):
        project = db.session.get(Project, project)
    if not project:
        raise ValueError("PROJECT_NOT_FOUND")
    base = get(project)
    chain, _ = _validated_chain(project, base)
    return chain


def read_projection(project: Project | int) -> dict:
    """Fault-contained read-only projection for Founder/organizational surfaces.

    Authority/execution paths must continue to call governing_terms()/
    effective_authority() directly so governed Projects fail closed.  This
    helper exists only so one historical or integrity-broken Project cannot
    crash unrelated read surfaces.  Governed Projects never fall back to
    mutable compatibility columns here.
    """
    if isinstance(project, int):
        project = db.session.get(Project, project)
    if not project:
        return {
            "project_id": None,
            "is_vnext_governed": False,
            "terms": None,
            "integrity_error": "PROJECT_NOT_FOUND",
        }
    governed = is_vnext_governed(project)
    try:
        terms = governing_terms(project, require=False)
    except ValueError as exc:
        return {
            "project_id": project.id,
            "is_vnext_governed": governed,
            "terms": None,
            "integrity_error": str(exc),
        }
    if governed and terms is None:
        return {
            "project_id": project.id,
            "is_vnext_governed": True,
            "terms": None,
            "integrity_error": "PROJECT_CONTRACT_MISSING",
        }
    return {
        "project_id": project.id,
        "is_vnext_governed": governed,
        "terms": terms,
        "integrity_error": None,
    }


def governing_terms(project: Project | int, *, require=True) -> dict | None:
    """Validated effective Founder Project terms after append-only amendments."""
    if isinstance(project, int):
        project = db.session.get(Project, project)
    if not project:
        if require:
            raise ValueError("PROJECT_NOT_FOUND")
        return None
    base = get(project, require=require)
    if base is None:
        return None
    chain, state = _validated_chain(project, base)
    return {
        "version": "PROJECT_GOVERNING_TERMS_V1",
        "project_id": project.id,
        "base_contract_hash": base.get("contract_hash"),
        "contract_hash": base.get("contract_hash"),  # compatibility alias
        "governing_contract_hash": state["governing_hash"],
        "objective": state["objective"],
        "success_criteria": list(state["success_criteria"]),
        "constraints": list(state["constraints"]),
        "budget_limit_twd": str(state["budget"]) if state["budget"] is not None else None,
        "deadline": state["deadline"],
        "amendment_hashes": list(state["all_hashes"]),
        "amendment_count": len(chain),
    }


def execution_terms_hash(project: Project | int) -> str:
    """Hash only Founder terms that define what Work is lawful to execute.

    Budget extensions are append-only resource authority and do not invalidate an
    already-approved bounded Work. Objective/success criteria/constraints/deadline
    changes do: an old Work may not silently continue under materially different
    Founder terms.
    """
    terms = governing_terms(project)
    body = {
        "version": "PROJECT_EXECUTION_TERMS_V1",
        "project_id": int(terms["project_id"]),
        "objective": terms.get("objective"),
        "success_criteria": list(terms.get("success_criteria") or []),
        "constraints": list(terms.get("constraints") or []),
        "deadline": terms.get("deadline"),
    }
    return _hash(body)


def execution_terms_changed_after(project: Project | int, moment) -> bool:
    """Whether Founder execution terms changed after one historical Run began.

    This exists for pre-snapshot Runs created before execution_terms_hash was
    persisted on every AgentRun.  Budget-only amendments do not stale already
    completed bounded Work.  Scope, constraint, or deadline changes do.
    """
    if isinstance(project, int):
        project = db.session.get(Project, project)
    if not project:
        raise ValueError("PROJECT_NOT_FOUND")
    chain = amendments(project)
    rows = _amendment_rows(project)
    if len(chain) != len(rows):
        raise ValueError("PROJECT_CONTRACT_AMENDMENT_CHAIN_LENGTH_MISMATCH")
    if moment is None:
        return any(str(item.get("amendment_type") or "").upper() != "BUDGET_EXTENSION" for item in chain)
    when = moment
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    for row, item in zip(rows, chain):
        if str(item.get("amendment_type") or "").upper() == "BUDGET_EXTENSION":
            continue
        created = getattr(row, "created_at", None)
        if created is None:
            # Missing amendment time is not authority to silently reuse old Work.
            return True
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if created > when:
            return True
    return False


def effective_authority(project: Project | int) -> dict:
    """Return the validated Founder budget authority ledger only."""
    if isinstance(project, int):
        project = db.session.get(Project, project)
    if not project:
        raise ValueError("PROJECT_NOT_FOUND")
    base = get(project)
    _, state = _validated_chain(project, base)
    return _authority_body(
        project.id, base.get("contract_hash"),
        str(state["budget"]) if state["budget"] is not None else None,
        state["budget_hashes"],
    )


def assert_authority_ledger(project: Project) -> dict:
    authority = effective_authority(project)
    effective = authority.get("effective_budget_limit_twd")
    persisted = project.real_budget_limit
    if effective is None:
        if persisted is not None:
            raise ValueError("PROJECT_BUDGET_LEDGER_MISMATCH")
    elif persisted is None or Decimal(str(persisted)) != Decimal(str(effective)):
        raise ValueError("PROJECT_BUDGET_LEDGER_MISMATCH")
    return authority


def assert_governing_projection(project: Project) -> dict:
    """Detect mutable compatibility columns drifting from governing ledger."""
    terms = governing_terms(project)
    assert_authority_ledger(project)
    if is_vnext_governed(project):
        if str(project.objective or "").strip() != terms["objective"]:
            raise ValueError("PROJECT_OBJECTIVE_PROJECTION_MISMATCH")
        if _rows(project.known_constraints) != terms["constraints"]:
            raise ValueError("PROJECT_CONSTRAINT_PROJECTION_MISMATCH")
        projected_deadline = _deadline(project)
        if projected_deadline != terms["deadline"]:
            raise ValueError("PROJECT_DEADLINE_PROJECTION_MISMATCH")
    return terms


def _assert_amendable(project: Project) -> None:
    if project.status not in {"PLANNING", "ACTIVE", "BLOCKED"}:
        raise ValueError("PROJECT_CONTRACT_NOT_AMENDABLE")


def _append(project: Project, body: dict, *, reason: str,
            origin_employee_id=None, source_ref=None) -> dict:
    body = dict(body)
    body["version"] = AMENDMENT_VERSION
    body["project_id"] = project.id
    body["base_contract_hash"] = get(project).get("contract_hash")
    terms = governing_terms(project)
    body["previous_governing_contract_hash"] = terms["governing_contract_hash"]
    hash_source = dict(body)
    body["amendment_hash"] = _hash(hash_source)
    body["governing_contract_hash"] = _next_governing_hash(
        terms["governing_contract_hash"], body["amendment_hash"], body["amendment_type"]
    )
    row = KnowledgeItem(
        project_id=project.id, kind="DECISION", title=AMENDMENT_TITLE,
        content=json.dumps(body, ensure_ascii=False, sort_keys=True),
        rationale=clean_text(reason or "Founder amended Project authority/terms.", multiline=False),
        source_ref=source_ref or f"project:{project.id}:contract_amendment:{body['amendment_type'].lower()}",
        origin_employee_id=origin_employee_id, founder_approved=True,
    )
    db.session.add(row)
    db.session.flush()
    # Re-read validates the entire append-only chain before caller proceeds.
    chain = amendments(project)
    if not chain or chain[-1]["amendment_hash"] != body["amendment_hash"]:
        raise ValueError("PROJECT_CONTRACT_AMENDMENT_PERSISTENCE_MISMATCH")
    return body


def authorize_budget_extension(project: Project, *, additional_twd, reason: str,
                               origin_employee_id=None, source_ref=None) -> dict:
    additional = Decimal(str(additional_twd or 0))
    if additional <= 0:
        raise ValueError("PROJECT_BUDGET_EXTENSION_INVALID")
    _assert_amendable(project)

    base = get(project)
    # Historical Projects may synthesize a legacy contract; freeze it before
    # appending new v0.20 Founder authority.
    if not has_frozen_contract(project):
        freeze(
            project, success_criteria=base.get("success_criteria") or [],
            constraints=base.get("constraints") or [], origin_employee_id=origin_employee_id,
        )
    authority = assert_authority_ledger(project)
    previous = Decimal(str(authority.get("effective_budget_limit_twd") or 0))
    new = previous + additional
    body = _append(project, {
        "amendment_type": "BUDGET_EXTENSION",
        "previous_budget_limit_twd": str(previous),
        "additional_budget_twd": str(additional),
        "new_budget_limit_twd": str(new),
        "reason": clean_text(reason or "Founder authorized additional Project budget.", multiline=False),
        "previous_authority_hash": authority.get("authority_hash"),
    }, reason=reason, origin_employee_id=origin_employee_id, source_ref=source_ref)
    project.real_budget_limit = new
    db.session.flush()
    effective = assert_authority_ledger(project)
    return {**body, "authority_hash": effective.get("authority_hash")}


def authorize_deadline_change(project: Project, *, new_deadline, reason: str,
                              origin_employee_id=None, source_ref=None) -> dict:
    _assert_amendable(project)
    if not has_frozen_contract(project):
        base = get(project)
        freeze(project, success_criteria=base.get("success_criteria") or [],
               constraints=base.get("constraints") or [], origin_employee_id=origin_employee_id)
    terms = governing_terms(project)
    normalized = _normalize_deadline(new_deadline)
    if not normalized or normalized == terms.get("deadline"):
        raise ValueError("PROJECT_DEADLINE_CHANGE_INVALID")
    body = _append(project, {
        "amendment_type": "DEADLINE_CHANGE",
        "previous_deadline": terms.get("deadline"),
        "new_deadline": normalized,
        "reason": clean_text(reason or "Founder changed the Project deadline.", multiline=False),
    }, reason=reason, origin_employee_id=origin_employee_id, source_ref=source_ref)
    project.deadline = _deadline_dt(normalized)
    db.session.flush()
    return body


def authorize_scope_change(project: Project, *, objective=None, success_criteria=None,
                           reason: str, origin_employee_id=None, source_ref=None) -> dict:
    _assert_amendable(project)
    if not has_frozen_contract(project):
        base = get(project)
        freeze(project, success_criteria=base.get("success_criteria") or [],
               constraints=base.get("constraints") or [], origin_employee_id=origin_employee_id)
    terms = governing_terms(project)
    new_objective = clean_text(objective or terms["objective"], multiline=False)
    new_criteria = _rows(success_criteria) if success_criteria is not None else list(terms["success_criteria"])
    if not new_objective or not new_criteria:
        raise ValueError("PROJECT_SCOPE_CHANGE_INVALID")
    if new_objective == terms["objective"] and new_criteria == terms["success_criteria"]:
        raise ValueError("PROJECT_SCOPE_CHANGE_NOOP")
    body = _append(project, {
        "amendment_type": "SCOPE_CHANGE",
        "previous_objective": terms["objective"],
        "previous_success_criteria": terms["success_criteria"],
        "new_objective": new_objective,
        "new_success_criteria": new_criteria,
        "reason": clean_text(reason or "Founder changed the Project scope.", multiline=False),
    }, reason=reason, origin_employee_id=origin_employee_id, source_ref=source_ref)
    project.objective = new_objective
    db.session.flush()
    return body


def authorize_constraint_change(project: Project, *, constraints, reason: str,
                                origin_employee_id=None, source_ref=None) -> dict:
    _assert_amendable(project)
    if constraints is None or not isinstance(constraints, (list, tuple, str)):
        raise ValueError("PROJECT_CONSTRAINT_CHANGE_INVALID")
    if not has_frozen_contract(project):
        base = get(project)
        freeze(project, success_criteria=base.get("success_criteria") or [],
               constraints=base.get("constraints") or [], origin_employee_id=origin_employee_id)
    terms = governing_terms(project)
    new_constraints = _rows(constraints)
    if new_constraints == terms["constraints"]:
        raise ValueError("PROJECT_CONSTRAINT_CHANGE_NOOP")
    body = _append(project, {
        "amendment_type": "CONSTRAINT_CHANGE",
        "previous_constraints": terms["constraints"],
        "new_constraints": new_constraints,
        "reason": clean_text(reason or "Founder changed the Project constraints.", multiline=False),
    }, reason=reason, origin_employee_id=origin_employee_id, source_ref=source_ref)
    project.known_constraints = "\n".join(new_constraints) if new_constraints else None
    db.session.flush()
    return body


def legacy_founder_project_cap_evidence(project: Project) -> dict | None:
    """Return explicit pre-v0.20 Founder Project-cap evidence, if provable."""
    rows = (
        Operation.query.filter_by(project_id=project.id)
        .filter(Operation.approved_at.isnot(None))
        .order_by(Operation.id.asc()).all()
    )
    for operation in rows:
        memory = dict(operation.memory_json or {})
        if not memory.get("new_project_spec"):
            continue
        if str(memory.get("authority_source") or "FOUNDER_APPROVAL").upper() != "FOUNDER_APPROVAL":
            continue
        raw = memory.get("founder_declared_budget_cap_twd")
        if raw in (None, ""):
            continue
        try:
            cap = Decimal(str(raw))
        except Exception:
            continue
        if cap <= 0:
            continue
        return {
            "operation_id": operation.id,
            "budget_limit_twd": cap,
            "source_ref": f"operation:{operation.id}:founder_declared_budget_cap_twd",
        }
    return None


def reconcile_legacy_founder_project_cap(project: Project, *,
                                         resolve_stale_budget_gate: bool = True) -> dict:
    """Restore a provable historical Founder cap without inventing authority."""
    evidence = legacy_founder_project_cap_evidence(project)
    if not evidence:
        return {"status": "NO_EXPLICIT_FOUNDER_CAP", "project_id": project.id}
    proven = Decimal(str(evidence["budget_limit_twd"]))
    persisted_base = _base_row(project)

    if persisted_base is None:
        current = Decimal(str(project.real_budget_limit or 0))
        if proven > current:
            project.real_budget_limit = proven
            db.session.flush()
            return {
                "status": "PRE_FREEZE_CAP_RESTORED", "project_id": project.id,
                "previous_budget_limit_twd": str(current),
                "effective_budget_limit_twd": str(proven), **evidence,
            }
        return {
            "status": "PRE_FREEZE_CAP_ALREADY_COVERED", "project_id": project.id,
            "effective_budget_limit_twd": str(current), **evidence,
        }

    authority = assert_authority_ledger(project)
    current = Decimal(str(authority.get("effective_budget_limit_twd") or 0))
    if proven <= current:
        return {
            "status": "AUTHORITY_ALREADY_COVERS_LEGACY_CAP", "project_id": project.id,
            "effective_budget_limit_twd": str(current), **evidence,
        }

    amendment = authorize_budget_extension(
        project, additional_twd=proven - current,
        reason=(
            "v0.20 migration reconciliation restored the pre-existing explicit "
            "Founder Project budget cap captured before execution; this is not "
            "new AI/system authority."
        ),
        origin_employee_id=project.owner_employee_id,
        source_ref=f"project:{project.id}:v020_legacy_founder_budget_reconciliation:operation:{evidence['operation_id']}",
    )

    resolved = []
    if resolve_stale_budget_gate:
        budget_gate_re = re.compile(
            r"needs\s+NT\$([0-9]+(?:\.[0-9]+)?)\s+more\s+"
            r"\(authorized\s+NT\$([0-9]+(?:\.[0-9]+)?),\s*spent\s+NT\$([0-9]+(?:\.[0-9]+)?)\)",
            flags=re.IGNORECASE,
        )
        for escalation in Escalation.query.filter_by(
            project_id=project.id, state="OPEN", escalation_type="BUDGET_AUTHORIZATION"
        ).all():
            match = budget_gate_re.search(str(escalation.reason or ""))
            if not match:
                continue
            additional = Decimal(match.group(1))
            old_authorized = Decimal(match.group(2))
            if old_authorized != current or proven < old_authorized + additional:
                continue
            __import__(
                "eason_one.services.governance", fromlist=["invalidate_gate"]
            ).invalidate_gate(
                escalation,
                resolution="MIGRATION_AUTHORITY_RESTORED",
                reason=(
                    "Explicit v0.20 migration proved this budget question was already "
                    "covered by durable pre-cutover Founder authority; no new authority was granted."
                ),
                actor_type="RUNTIME",
            )
            resolved.append(escalation.id)
        # Only Founder gates should block Project authority. Internal escalation
        # rows are not a reason to rewrite Project status here.
        runtime = __import__("eason_one.services.work_runtime", fromlist=["project_can_activate"])
        if runtime.project_can_activate(project) and project.status == "BLOCKED":
            project.status = "ACTIVE"
            project.current_state_summary = (
                "Restored the pre-existing Founder Project budget authority from "
                "durable pre-v0.20 approval evidence. Company Kernel may continue."
            )
            project.next_milestone = "Resume bounded Project recovery/continuation from durable evidence."

    db.session.flush()
    return {
        "status": "APPEND_ONLY_AUTHORITY_RESTORED", "project_id": project.id,
        "previous_budget_limit_twd": str(current),
        "effective_budget_limit_twd": str(proven),
        "resolved_budget_escalation_ids": resolved,
        "amendment_hash": amendment.get("amendment_hash"), **evidence,
    }


def assert_internal_authority(project: Project, *, requested_budget_twd) -> dict:
    contract = governing_terms(project)
    assert_authority_ledger(project)
    remaining = __import__(
        "eason_one.services.operations", fromlist=["project_remaining_authority"]
    ).project_remaining_authority(project)
    requested = Decimal(str(requested_budget_twd or 0))
    if requested <= 0:
        raise ValueError("INTERNAL_MISSION_BUDGET_INVALID")
    if remaining is not None and requested > Decimal(remaining):
        raise ValueError("PROJECT_BUDGET_AUTHORITY_EXCEEDED")
    if project.status in {"REVIEW", "COMPLETED", "CANCELLED"}:
        raise ValueError("PROJECT_CONTRACT_TERMINAL")
    return contract
