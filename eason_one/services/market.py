"""Eason Market V1: durable intra-company labor allocation and settlement.

This is intentionally smaller than the long-term multi-company Eason Market.
V1 makes an already-governed Work economically observable without weakening
Project authority:

Work demand -> eligible Persistent Employee offers -> deterministic award
           -> WorkAssignment -> accepted Artifact/Verification -> EC settlement
           -> wallet + outcome-backed reputation.

EC is internal labor capital, not TWD, provider billing, or legal tender.  The
Project's real-budget authority remains exclusively in the existing TWD ledger.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from sqlalchemy import func, inspect

from ..extensions import db
from ..models import (
    ArtifactVersion,
    Company,
    Employee,
    MarketContract,
    MarketLedgerEntry,
    MarketOffer,
    MarketOrder,
    VerificationRecord,
    Work,
    now,
)
from .company_events import emit

POLICY_VERSION = "INTERNAL_MARKET_V1_3"
_CENT = Decimal("0.01")


def _money(value) -> Decimal:
    return Decimal(value or 0).quantize(_CENT, rounding=ROUND_HALF_UP)


def _quote_breakdown(employee: Employee, profile: dict) -> dict:
    """Return a deterministic internal labor quote and its evidence-backed parts.

    V1.3 keeps compensation causality auditable: the persisted offer records a
    no-learning base quote and a bounded experience premium separately.  This
    lets the Employee Economy prove that accepted outcomes changed future EC
    compensation without pretending that EC changes capability or real TWD
    authority.
    """
    level = int(getattr(getattr(employee, "position", None), "level", 1) or 1)
    workload = int(profile.get("active_workload") or 0)
    salary = _money(employee.salary_credits_per_week)
    salary_reference = min(Decimal("10"), salary / Decimal("200")) if salary > 0 else Decimal("0")
    owner = Decimal(int(profile.get("accepted_owner_experience") or 0))
    review = Decimal(int(profile.get("accepted_review_experience") or 0))
    burden = Decimal(int(profile.get("recovery_burden") or 0))
    raw_premium = owner * Decimal("1.50") + review * Decimal("0.75") - burden * Decimal("0.25")
    experience_premium = max(Decimal("0"), min(Decimal("12"), raw_premium)).quantize(_CENT, rounding=ROUND_HALF_UP)
    base_quote = (
        Decimal("6")
        + Decimal(level * 2)
        + Decimal(workload) * Decimal("1.50")
        + salary_reference
    ).quantize(_CENT, rounding=ROUND_HALF_UP)
    total = max(Decimal("1"), base_quote + experience_premium).quantize(_CENT, rounding=ROUND_HALF_UP)
    return {
        "base_quote_ec": base_quote,
        "experience_premium_ec": experience_premium,
        "quote_ec": total,
        "accepted_owner_experience": int(owner),
        "accepted_review_experience": int(review),
        "recovery_burden": int(burden),
    }


def _quote_ec(employee: Employee, profile: dict) -> Decimal:
    return _quote_breakdown(employee, profile)["quote_ec"]


def _candidate_payload(row: tuple) -> dict:
    """Translate team_formation's ranked row into the market's stable envelope."""
    direct, specialist, experience_score, neg_workload, neg_employee_id, employee, profile = row
    workload = int(profile.get("active_workload") or max(0, -int(neg_workload)))
    quote = _quote_breakdown(employee, profile)
    return {
        "employee": employee,
        "employee_id": employee.id,
        "employee_name": employee.name,
        "direct": int(direct),
        "specialist": int(specialist),
        "experience_score": int(experience_score),
        "active_workload": workload,
        "quote_ec": quote["quote_ec"],
        "quote_breakdown": quote,
        "profile": dict(profile),
    }


def _selection_key(candidate: dict) -> tuple:
    # Authority/capability truth first, then proven outcome history.  Price is
    # allowed to break only that already-qualified tier; workload/id are stable
    # fallbacks. This prevents the market from buying an unqualified Employee.
    return (
        int(candidate["direct"]),
        int(candidate["specialist"]),
        int(candidate["experience_score"]),
        -candidate["quote_ec"],
        -int(candidate["active_workload"]),
        -int(candidate["employee_id"]),
    )


def _company() -> Company:
    company = Company.query.order_by(Company.id).first()
    if company is None:
        raise ValueError("EASON_MARKET_REQUIRES_COMPANY")
    return company


def _create_award_generation(
    work: Work,
    capability: str,
    ranked_candidates: list[tuple],
    *,
    generation: int,
    opened_by_employee_id: int | None = None,
    preferred_employee_id: int | None = None,
    supersedes_order_id: int | None = None,
    supersedes_contract_id: int | None = None,
    selection_basis_override: str | None = None,
) -> dict:
    """Persist exactly one immutable market generation from an eligible roster."""
    project = getattr(work, "project", None)
    if project is not None and __import__(
        "eason_one.services.work_runtime", fromlist=["project_is_terminal"]
    ).project_is_terminal(project):
        raise ValueError(
            f"PROJECT_TERMINAL_MARKET_AWARD_FORBIDDEN:{str(project.status or '').upper()}"
        )
    candidates = [_candidate_payload(row) for row in ranked_candidates]
    candidates.sort(key=_selection_key, reverse=True)
    preferred = next((row for row in candidates if row["employee_id"] == preferred_employee_id), None)
    if preferred_employee_id is not None and preferred is None:
        raise ValueError("MARKET_PREFERRED_EMPLOYEE_NOT_ELIGIBLE")
    selected = preferred or candidates[0]
    selection_basis = selection_basis_override or (
        "APPROVED_OWNER_PREFERENCE" if preferred is not None else "INTERNAL_MARKET_RANKING"
    )

    order = MarketOrder(
        company_id=_company().id,
        project_id=work.project_id,
        work_id=work.id,
        generation=int(generation),
        supersedes_order_id=supersedes_order_id,
        capability=str(capability).strip().upper(),
        buyer_type="COMPANY",
        currency="EC",
        status="OPEN",
        policy_version=POLICY_VERSION,
        opened_by_employee_id=opened_by_employee_id,
    )
    db.session.add(order)
    db.session.flush()
    emit(
        "MARKET_ORDER_OPENED",
        actor_type="EMPLOYEE" if opened_by_employee_id else "RUNTIME",
        actor_id=opened_by_employee_id,
        project_id=work.project_id,
        work_id=work.id,
        correlation_id=f"work:{work.id}",
        payload={
            "market_order_id": order.id,
            "generation": order.generation,
            "supersedes_order_id": supersedes_order_id,
            "capability": order.capability,
            "currency": "EC",
            "eligible_employee_count": len(candidates),
            "policy_version": POLICY_VERSION,
        },
    )

    selected_offer: MarketOffer | None = None
    for candidate in candidates:
        offer = MarketOffer(
            order_id=order.id,
            employee_id=candidate["employee_id"],
            quote_ec=candidate["quote_ec"],
            status="SELECTED" if candidate["employee_id"] == selected["employee_id"] else "NOT_SELECTED",
            rank_json={
                "direct_capability": candidate["direct"],
                "specialist": candidate["specialist"],
                "experience_score": candidate["experience_score"],
                "active_workload": candidate["active_workload"],
            },
            rationale_json={
                "policy": POLICY_VERSION,
                "market_generation": int(generation),
                "selection_order": [
                    "capability authority",
                    "specialist fit",
                    "accepted outcome experience",
                    "lower internal EC quote",
                    "lower active workload",
                    "stable employee id",
                ],
                "experience_record_ids": candidate["profile"].get("experience_record_ids") or [],
                "quote_is_internal_only": True,
                "quote_breakdown": {
                    key: (str(value) if isinstance(value, Decimal) else value)
                    for key, value in candidate["quote_breakdown"].items()
                },
                "selection_basis": selection_basis,
            },
        )
        db.session.add(offer)
        db.session.flush()
        if offer.status == "SELECTED":
            selected_offer = offer

    assert selected_offer is not None
    contract = MarketContract(
        order_id=order.id,
        work_id=work.id,
        generation=order.generation,
        supersedes_contract_id=supersedes_contract_id,
        seller_employee_id=selected["employee_id"],
        offer_id=selected_offer.id,
        agreed_ec=selected_offer.quote_ec,
        currency="EC",
        status="ACTIVE",
    )
    db.session.add(contract)
    db.session.flush()
    order.status = "CONTRACTED"
    emit(
        "MARKET_CONTRACT_AWARDED",
        actor_type="RUNTIME",
        project_id=work.project_id,
        work_id=work.id,
        correlation_id=f"work:{work.id}",
        payload={
            "market_order_id": order.id,
            "market_contract_id": contract.id,
            "generation": contract.generation,
            "supersedes_contract_id": supersedes_contract_id,
            "offer_id": selected_offer.id,
            "seller_employee_id": selected["employee_id"],
            "agreed_ec": str(_money(contract.agreed_ec)),
            "currency": "EC",
            "eligible_employee_count": len(candidates),
            "policy_version": POLICY_VERSION,
            "selection_basis": selection_basis,
            "real_twd_budget_unchanged": True,
        },
    )
    return {
        "order": order,
        "contract": contract,
        "offer": selected_offer,
        "employee": selected["employee"],
        "reused": False,
        "candidate_count": len(candidates),
    }


def award_internal_work(
    work: Work,
    capability: str,
    ranked_candidates: list[tuple],
    *,
    opened_by_employee_id: int | None = None,
    preferred_employee_id: int | None = None,
    selection_basis_override: str | None = None,
) -> dict | None:
    """Create/return the current idempotent internal-market award for governed Work.

    The caller supplies only Employees already proven eligible by Team Formation.
    This function cannot create capability or authority. Legitimate later
    reassignment is represented by a new immutable generation, never by mutating
    the historical seller.
    """
    if work.work_type == "MANAGEMENT" or not ranked_candidates:
        return None

    existing_contract = contract_for_work(work.id)
    if existing_contract is not None:
        employee = db.session.get(Employee, existing_contract.seller_employee_id)
        offer = db.session.get(MarketOffer, existing_contract.offer_id)
        return {
            "order": existing_contract.order,
            "contract": existing_contract,
            "offer": offer,
            "employee": employee,
            "reused": True,
        }

    return _create_award_generation(
        work,
        capability,
        ranked_candidates,
        generation=1,
        opened_by_employee_id=opened_by_employee_id,
        preferred_employee_id=preferred_employee_id,
        selection_basis_override=selection_basis_override,
    )


def _market_schema_available() -> bool:
    """Return whether the additive internal-market tables exist on this DB.

    Historical databases are valid before their first V1.3 app start.  Checking
    schema presence before an optional economic projection avoids putting the
    primary Work transaction into a failed SQL state merely because the new
    additive tables have not been created yet.
    """
    return bool(inspect(db.engine).has_table(MarketContract.__tablename__))


def contract_for_work(work_id: int) -> MarketContract | None:
    if not _market_schema_available():
        return None
    return (
        MarketContract.query.filter_by(work_id=work_id)
        .order_by(MarketContract.generation.desc(), MarketContract.id.desc())
        .first()
    )


def contract_history_for_work(work_id: int) -> list[MarketContract]:
    if not _market_schema_available():
        return []
    return (
        MarketContract.query.filter_by(work_id=work_id)
        .order_by(MarketContract.generation.desc(), MarketContract.id.desc())
        .all()
    )


def active_contract_for_work(work_id: int) -> MarketContract | None:
    if not _market_schema_available():
        return None
    return (
        MarketContract.query.filter_by(work_id=work_id, status="ACTIVE")
        .order_by(MarketContract.generation.desc(), MarketContract.id.desc())
        .first()
    )


def contract_evidence(contract: MarketContract | None) -> dict | None:
    if contract is None:
        return None
    order = contract.order
    offer = contract.offer
    return {
        "schema": "EASON_MARKET_CONTRACT_V1_3",
        "order_id": order.id if order else None,
        "generation": contract.generation,
        "supersedes_contract_id": contract.supersedes_contract_id,
        "contract_id": contract.id,
        "offer_id": contract.offer_id,
        "capability": order.capability if order else None,
        "seller_employee_id": contract.seller_employee_id,
        "seller_employee_name": contract.seller.name if contract.seller else None,
        # This envelope is persisted inside Work.runtime_control_json by Team
        # Formation, so every value must be JSON-native. SQLAlchemy Numeric
        # columns are Decimal and timezone columns are datetime; leaking either
        # here makes a later autoflush fail during unrelated queries.
        "agreed_ec": str(_money(contract.agreed_ec)),
        "currency": contract.currency,
        "status": contract.status,
        "offer_rank": dict(offer.rank_json or {}) if offer else {},
        "offer_rationale": dict(offer.rationale_json or {}) if offer else {},
        "artifact_version_id": contract.artifact_version_id,
        "verification_record_id": contract.verification_record_id,
        "settled_at": contract.settled_at.isoformat() if contract.settled_at else None,
        "superseded_at": contract.superseded_at.isoformat() if contract.superseded_at else None,
        "settlement_note": contract.settlement_note,
        "truth_note": "Internal EC labor contract only; Project TWD authority and provider billing remain separate.",
    }



def rotate_contract_for_reassignment(
    work: Work,
    *,
    previous_employee_id: int | None,
    new_employee_id: int,
    reason: str,
    assigned_by_employee_id: int | None = None,
) -> MarketContract | None:
    """Rotate unpaid internal labor authority after a legitimate Work reassignment.

    The old generation remains immutable audit history and receives no EC.  A
    new generation is created only when the replacement Employee independently
    satisfies the already-governed Work capability.  Settled/disputed/voided
    contracts are terminal and can never be rewritten into a new payable path.
    """
    if not _market_schema_available() or work.work_type == "MANAGEMENT":
        return None
    current = contract_for_work(work.id)
    if current is None:
        return None
    if current.status != "ACTIVE":
        return current
    if int(current.seller_employee_id) == int(new_employee_id):
        return current
    project = getattr(work, "project", None)
    if project is not None and __import__(
        "eason_one.services.work_runtime", fromlist=["project_is_terminal"]
    ).project_is_terminal(project):
        raise ValueError(
            f"PROJECT_TERMINAL_MARKET_ROTATION_FORBIDDEN:{str(project.status or '').upper()}"
        )

    capability = str(getattr(current.order, "capability", "") or "").strip().upper()
    if not capability:
        raise ValueError("MARKET_REASSIGNMENT_CAPABILITY_MISSING")
    team = __import__(
        "eason_one.services.team_formation",
        fromlist=["employee_capabilities", "ranked_existing_employees"],
    )
    replacement = db.session.get(Employee, int(new_employee_id))
    if replacement is None or not replacement.active or capability not in team.employee_capabilities(replacement):
        raise ValueError("MARKET_REASSIGNMENT_EMPLOYEE_NOT_CAPABILITY_ELIGIBLE")
    ranked = team.ranked_existing_employees(capability)
    if not any(row[5].id == replacement.id for row in ranked):
        raise ValueError("MARKET_REASSIGNMENT_EMPLOYEE_NOT_GOVERNED_ROSTER_ELIGIBLE")

    old_order = current.order
    current.status = "SUPERSEDED"
    current.superseded_at = now()
    current.settlement_note = (
        f"No EC paid. Governed Work reassigned from EMP-{current.seller_employee_id} "
        f"to EMP-{replacement.id}: {str(reason or 'company-owned reassignment')[:800]}"
    )
    if old_order:
        old_order.status = "SUPERSEDED"
    emit(
        "MARKET_CONTRACT_SUPERSEDED",
        actor_type="EMPLOYEE" if assigned_by_employee_id else "RUNTIME",
        actor_id=assigned_by_employee_id,
        project_id=work.project_id,
        work_id=work.id,
        correlation_id=f"work:{work.id}",
        payload={
            "market_contract_id": current.id,
            "generation": current.generation,
            "previous_employee_id": previous_employee_id or current.seller_employee_id,
            "new_employee_id": replacement.id,
            "reason": str(reason or "")[:800],
            "ec_paid": "0.00",
        },
    )
    award = _create_award_generation(
        work,
        capability,
        ranked,
        generation=int(current.generation or 1) + 1,
        opened_by_employee_id=assigned_by_employee_id,
        preferred_employee_id=replacement.id,
        supersedes_order_id=getattr(old_order, "id", None),
        supersedes_contract_id=current.id,
        selection_basis_override="GOVERNED_WORK_REASSIGNMENT",
    )
    return award["contract"]


def retire_unsettled_contract(work: Work, *, reason: str, terminal_state: str) -> MarketContract | None:
    """Retire internal labor authority when Work can no longer be delivered.

    No negative ledger entry is written because no EC payment was ever made.
    This prevents CANCELLED/ABANDONED Work from leaving a ghost ACTIVE contract
    on the Founder Market surface. Accepted/settled contracts are immutable.
    """
    contract = contract_for_work(work.id)
    if contract is None or contract.status == "SETTLED":
        return contract
    if contract.status in {"VOIDED", "DISPUTED"}:
        return contract
    contract.status = "VOIDED"
    contract.settlement_note = (
        f"No EC paid. Work became {str(terminal_state or '').upper()}: {str(reason or 'terminal Work state')[:900]}"
    )
    if contract.order:
        contract.order.status = "VOIDED"
    emit(
        "MARKET_CONTRACT_VOIDED", actor_type="RUNTIME",
        project_id=work.project_id, work_id=work.id, correlation_id=f"work:{work.id}",
        payload={
            "market_contract_id": contract.id,
            "seller_employee_id": contract.seller_employee_id,
            "terminal_work_state": str(terminal_state or "").upper(),
            "reason": str(reason or "")[:900],
            "ec_paid": "0.00",
        },
    )
    return contract


def reissue_voided_contract_after_system_reopen(work: Work, *, reason: str) -> MarketContract | None:
    """Create a new immutable payable generation after a proven platform reopen.

    ABANDONED correctly voids an unpaid EC contract.  When deterministic runtime
    later proves that abandonment came from an Eason One platform defect, the
    historical VOIDED contract must remain immutable while the same governed
    labor promise becomes payable again.  Reissue preserves seller/capability
    and the original EC quote; it does not rerun a market auction or change TWD
    authority.
    """
    if not _market_schema_available() or work.work_type == "MANAGEMENT":
        return None
    prior = contract_for_work(work.id)
    if prior is None or prior.status != "VOIDED":
        return prior
    project = getattr(work, "project", None)
    if project is not None and __import__(
        "eason_one.services.work_runtime", fromlist=["project_is_terminal"]
    ).project_is_terminal(project):
        raise ValueError(
            f"PROJECT_TERMINAL_MARKET_REISSUE_FORBIDDEN:{str(project.status or '').upper()}"
        )
    order = prior.order
    capability = str(getattr(order, "capability", "") or "").strip().upper()
    if not capability:
        raise ValueError("MARKET_SYSTEM_REOPEN_CAPABILITY_MISSING")
    seller = db.session.get(Employee, prior.seller_employee_id)
    if seller is None:
        raise ValueError("MARKET_SYSTEM_REOPEN_SELLER_MISSING")

    generation = int(prior.generation or 1) + 1
    new_order = MarketOrder(
        company_id=_company().id,
        project_id=work.project_id,
        work_id=work.id,
        generation=generation,
        supersedes_order_id=getattr(order, "id", None),
        capability=capability,
        buyer_type="COMPANY",
        currency="EC",
        status="OPEN",
        policy_version=POLICY_VERSION,
        opened_by_employee_id=None,
    )
    db.session.add(new_order)
    db.session.flush()
    old_offer = prior.offer
    old_rank = dict(getattr(old_offer, "rank_json", None) or {})
    old_rationale = dict(getattr(old_offer, "rationale_json", None) or {})
    old_rationale.update({
        "policy": POLICY_VERSION,
        "market_generation": generation,
        "selection_basis": "SYSTEM_PLATFORM_REOPEN_REISSUE",
        "reissued_from_contract_id": prior.id,
        "reissued_from_order_id": getattr(order, "id", None),
        "platform_reopen_reason": str(reason or "")[:800],
        "quote_preserved": True,
        "real_twd_budget_unchanged": True,
    })
    offer = MarketOffer(
        order_id=new_order.id,
        employee_id=prior.seller_employee_id,
        quote_ec=_money(prior.agreed_ec),
        status="SELECTED",
        rank_json=old_rank,
        rationale_json=old_rationale,
    )
    db.session.add(offer)
    db.session.flush()
    contract = MarketContract(
        order_id=new_order.id,
        work_id=work.id,
        generation=generation,
        supersedes_contract_id=prior.id,
        seller_employee_id=prior.seller_employee_id,
        offer_id=offer.id,
        agreed_ec=_money(prior.agreed_ec),
        currency="EC",
        status="ACTIVE",
    )
    db.session.add(contract)
    db.session.flush()
    new_order.status = "CONTRACTED"
    emit(
        "MARKET_CONTRACT_REISSUED_AFTER_SYSTEM_REOPEN",
        actor_type="RUNTIME",
        project_id=work.project_id, work_id=work.id, correlation_id=f"work:{work.id}",
        payload={
            "market_order_id": new_order.id,
            "market_contract_id": contract.id,
            "generation": generation,
            "supersedes_contract_id": prior.id,
            "seller_employee_id": prior.seller_employee_id,
            "agreed_ec": str(_money(prior.agreed_ec)),
            "currency": "EC",
            "reason": str(reason or "")[:800],
            "quote_preserved": True,
            "real_twd_budget_unchanged": True,
        },
    )
    return contract

def settle_accepted_work(
    work: Work,
    version: ArtifactVersion,
    acceptance: VerificationRecord,
) -> MarketContract | None:
    """Settle one awarded Work exactly once after authoritative acceptance."""
    contract = contract_for_work(work.id)
    if contract is None:
        return None
    if contract.status == "SETTLED":
        return contract
    # DISPUTED/VOIDED contracts never become payable later merely because a
    # different accepted Artifact appears. Economic truth is fail-closed.
    if contract.status != "ACTIVE":
        return contract
    if work.state != "ACCEPTED" or version.status != "ACCEPTED" or acceptance.status != "PASSED":
        return contract

    producer_id = version.producer_employee_id
    if producer_id != contract.seller_employee_id:
        contract.status = "DISPUTED"
        contract.artifact_version_id = version.id
        contract.verification_record_id = acceptance.id
        contract.settlement_note = (
            f"Accepted Artifact producer EMP-{producer_id or 'UNKNOWN'} does not match contracted "
            f"seller EMP-{contract.seller_employee_id}; no EC was paid."
        )
        if contract.order:
            contract.order.status = "DISPUTED"
        emit(
            "MARKET_SETTLEMENT_BLOCKED",
            actor_type="RUNTIME",
            project_id=work.project_id,
            work_id=work.id,
            artifact_id=version.artifact_id,
            correlation_id=f"work:{work.id}",
            payload={
                "market_contract_id": contract.id,
                "contracted_employee_id": contract.seller_employee_id,
                "producer_employee_id": producer_id,
                "reason": "ACCEPTED_ARTIFACT_PRODUCER_MISMATCH",
            },
        )
        return contract

    existing = MarketLedgerEntry.query.filter_by(
        contract_id=contract.id, entry_type="WORK_ACCEPTANCE_PAYMENT"
    ).first()
    if existing is None:
        db.session.add(MarketLedgerEntry(
            employee_id=contract.seller_employee_id,
            contract_id=contract.id,
            project_id=work.project_id,
            work_id=work.id,
            entry_type="WORK_ACCEPTANCE_PAYMENT",
            amount_ec=_money(contract.agreed_ec),
            reason=f"Accepted governed Work #{work.id} settled internal Eason Market contract #{contract.id}.",
            evidence_json={
                "artifact_version_id": version.id,
                "verification_record_id": acceptance.id,
                "artifact_content_hash": version.content_hash,
                "validation_basis": "CANONICAL_WORK_ACCEPTANCE",
            },
        ))
    contract.status = "SETTLED"
    contract.artifact_version_id = version.id
    contract.verification_record_id = acceptance.id
    contract.settled_at = now()
    contract.settlement_note = "Paid only after accepted Artifact + PASS Verification evidence."
    if contract.order:
        contract.order.status = "SETTLED"
    emit(
        "MARKET_CONTRACT_SETTLED",
        actor_type="RUNTIME",
        project_id=work.project_id,
        work_id=work.id,
        artifact_id=version.artifact_id,
        correlation_id=f"work:{work.id}",
        payload={
            "market_contract_id": contract.id,
            "seller_employee_id": contract.seller_employee_id,
            "amount_ec": str(_money(contract.agreed_ec)),
            "currency": "EC",
            "artifact_version_id": version.id,
            "verification_record_id": acceptance.id,
        },
    )
    return contract


def wallet_balance(employee_id: int) -> Decimal:
    value = db.session.query(func.coalesce(func.sum(MarketLedgerEntry.amount_ec), 0)).filter(
        MarketLedgerEntry.employee_id == employee_id
    ).scalar()
    return _money(value)


def employee_market_reputation(employee: Employee) -> dict:
    """Evidence-backed economic reputation; never a capability grant."""
    evolution = __import__(
        "eason_one.services.employee_evolution", fromlist=["employee_profile"]
    ).employee_profile(employee)
    contracts = MarketContract.query.filter_by(seller_employee_id=employee.id).all()
    settled = sum(row.status == "SETTLED" for row in contracts)
    disputed = sum(row.status == "DISPUTED" for row in contracts)
    accepted = int(evolution.get("owner_acceptances") or 0)
    reviewed = int(evolution.get("review_acceptances") or 0)
    recovery = sum(int(row.get("recovery_burden") or 0) for row in evolution.get("capabilities") or [])
    score = accepted * 4 + reviewed * 2 + settled * 2 - recovery - disputed * 6
    if accepted >= 4 and disputed == 0:
        band = "TRUSTED"
    elif accepted or reviewed or settled:
        band = "PROVEN"
    else:
        band = "NEW"
    return {
        "schema": "EMPLOYEE_MARKET_REPUTATION_V1",
        "score": score,
        "band": band,
        "accepted_owner_outcomes": accepted,
        "accepted_review_outcomes": reviewed,
        "settled_contracts": settled,
        "disputed_contracts": disputed,
        "recovery_burden": recovery,
        "truth_note": "Reputation summarizes persisted outcomes and settlement truth; it cannot create capability or authority.",
    }



def economic_evolution_summary() -> dict:
    """Return persisted evidence that accepted experience changed EC pricing.

    Only offers written with a quote breakdown are counted.  A positive premium
    is an economic consequence of canonical outcome history, not a capability
    grant and not a real-currency payment.
    """
    offers = MarketOffer.query.order_by(MarketOffer.id.desc()).all()
    rows = []
    for offer in offers:
        rationale = dict(offer.rationale_json or {})
        breakdown = dict(rationale.get("quote_breakdown") or {})
        if not breakdown:
            continue
        try:
            base = _money(breakdown.get("base_quote_ec"))
            premium = _money(breakdown.get("experience_premium_ec"))
            total = _money(breakdown.get("quote_ec") or offer.quote_ec)
        except Exception:
            continue
        rows.append({
            "offer_id": offer.id,
            "order_id": offer.order_id,
            "employee_id": offer.employee_id,
            "employee_name": offer.employee.name if offer.employee else None,
            "base_quote_ec": base,
            "experience_premium_ec": premium,
            "quote_ec": total,
            "status": offer.status,
            "experience_record_ids": list(rationale.get("experience_record_ids") or []),
        })
    changed = [row for row in rows if row["experience_premium_ec"] > 0]
    return {
        "schema": "ECONOMIC_EVOLUTION_EVIDENCE_V1",
        "offers_with_breakdown": len(rows),
        "offers_with_experience_premium": len(changed),
        "total_experience_premium_ec": _money(sum((row["experience_premium_ec"] for row in changed), Decimal("0"))),
        "examples": changed[:8],
        "truth_note": (
            "Premium evidence is derived from persisted internal EC offers. It does not alter capability, "
            "Founder Project TWD authority, provider billing, or legal compensation."
        ),
    }

def employee_market_profile(employee: Employee) -> dict:
    contracts = MarketContract.query.filter_by(seller_employee_id=employee.id).order_by(
        MarketContract.id.desc()
    ).all()
    entries = MarketLedgerEntry.query.filter_by(employee_id=employee.id).order_by(
        MarketLedgerEntry.id.desc()
    ).all()
    reputation = employee_market_reputation(employee)
    return {
        "active": True,
        "version": POLICY_VERSION,
        "wallet_state": "Internal Eason Market V1.3 active",
        "reputation": reputation,
        "balance": wallet_balance(employee.id),
        "earned": _money(sum((_money(row.amount_ec) for row in entries if _money(row.amount_ec) > 0), Decimal("0"))),
        "active_contracts": sum(row.status == "ACTIVE" for row in contracts),
        "settled_contracts": sum(row.status == "SETTLED" for row in contracts),
        "contracts": contracts[:10],
        "ledger": entries[:12],
        "boundary": "EC is internal labor capital. It does not spend or expand Founder-approved TWD authority.",
    }


def snapshot() -> dict:
    orders = MarketOrder.query.order_by(MarketOrder.id.desc()).limit(60).all()
    contracts = MarketContract.query.order_by(MarketContract.id.desc()).limit(60).all()
    settled = [row for row in contracts if row.status == "SETTLED"]
    volume = sum((_money(row.agreed_ec) for row in settled), Decimal("0"))
    order_rows = []
    for order in orders:
        contract = MarketContract.query.filter_by(order_id=order.id).first()
        offers = MarketOffer.query.filter_by(order_id=order.id).order_by(MarketOffer.id).all()
        work = order.work
        order_rows.append({
            "order": order,
            "project": order.project,
            "work": work,
            "offers": offers,
            "contract": contract,
            "contract_evidence": contract_evidence(contract),
        })
    return {
        "company": _company(),
        "version": POLICY_VERSION,
        "orders": order_rows,
        "metrics": {
            "orders": MarketOrder.query.count(),
            "open": MarketOrder.query.filter(MarketOrder.status.in_(["OPEN", "CONTRACTED"])).count(),
            "settled": MarketContract.query.filter_by(status="SETTLED").count(),
            "disputed": MarketContract.query.filter_by(status="DISPUTED").count(),
            "voided": MarketContract.query.filter_by(status="VOIDED").count(),
            "superseded": MarketContract.query.filter_by(status="SUPERSEDED").count(),
            "volume_ec": _money(volume),
            "participants": db.session.query(func.count(func.distinct(MarketOffer.employee_id))).scalar() or 0,
        },
        "mechanism": [
            "Only Employees already eligible for the Work capability can offer.",
            "Organizational fit and accepted outcome experience rank before price.",
            "Lower EC quote breaks ties inside an equally-qualified tier.",
            "No payment occurs until the contracted Employee produces an accepted Artifact with PASS Verification.",
            "Settlement updates the Employee EC wallet; canonical acceptance separately updates persistent capability/reputation evidence.",
            "Accepted owner/review outcomes create a bounded evidence-backed skill premium in future EC quotes; recovery burden reduces that premium without erasing accepted work.",
            "Every V1.3 offer persists its no-learning base quote and experience premium separately so the economic effect can be audited later.",
            "Legitimate pre-settlement Work reassignment supersedes the unpaid contract and opens a new generation for the replacement Employee; prior generations remain immutable audit history.",
        ],
        "boundary": [
            "Internal Market V1.3 is intra-company; cross-company contracting is not claimed yet.",
            "EC is an internal labor ledger, not TWD, legal tender, or provider billing.",
            "Market allocation cannot mint capability, bypass Founder Project authority, or increase a real budget cap.",
        ],
    }
