"""Bounded deterministic runtime for the Eason One CEO operating layer.

This runtime begins *future-only*.  On its first start it checkpoints the latest
already-persisted CompanyEvent instead of replaying historical Projects.  Later
cycles classify only new durable CompanyEvents, record deterministic Work/
Project reviews, apply at most one bounded CEO management/staffing effect, then
persist a fresh CompanyPlan projection. Strategic provider/Work execution stays
owned by Company Kernel and may consume only a current durable CEO receipt.

No function in this module calls a provider, creates Work, hires an Employee,
changes Project TWD authority, or bypasses Governance.  Company Kernel remains
the execution owner; this is a bounded management observer/allocator above it.
"""
from __future__ import annotations

from sqlalchemy import func

from ..extensions import db
from ..models import CompanyEvent, Employee, Project, Work
from . import ceo_learning, ceo_management, ceo_operating, ceo_review
from .company_events import emit


CHECKPOINT_EVENT_TYPE = "CEO_RUNTIME_CHECKPOINT"
WORK_REVIEW_EVENT_TYPE = "CEO_WORK_REVIEW_RECORDED"
PROJECT_REVIEW_EVENT_TYPE = "CEO_PROJECT_REVIEW_RECORDED"

_INTERNAL_EVENT_TYPES = {
    CHECKPOINT_EVENT_TYPE,
    WORK_REVIEW_EVENT_TYPE,
    PROJECT_REVIEW_EVENT_TYPE,
    ceo_management.PLAN_EVENT_TYPE,
    ceo_management.ACTION_RECEIPT_EVENT_TYPE,
    ceo_learning.PROJECT_CLOSURE_EVENT_TYPE,
    "CEO_PORTFOLIO_WORK_ASSIGNED",
    "CEO_PORTFOLIO_WORK_REALLOCATED",
}


def _latest_source_event_id() -> int:
    query = db.session.query(func.coalesce(func.max(CompanyEvent.id), 0))
    if _INTERNAL_EVENT_TYPES:
        query = query.filter(~CompanyEvent.event_type.in_(tuple(_INTERNAL_EVENT_TYPES)))
    return int(query.scalar() or 0)


def latest_checkpoint() -> dict | None:
    row = CompanyEvent.query.filter_by(event_type=CHECKPOINT_EVENT_TYPE).order_by(
        CompanyEvent.id.desc()
    ).first()
    if row is None:
        return None
    payload = dict(row.payload_json or {})
    return {
        "event_id": row.id,
        "source_event_id": int(payload.get("source_event_id") or 0),
        "mode": payload.get("mode"),
        "processed": int(payload.get("processed") or 0),
        "material_reviews": int(payload.get("material_reviews") or 0),
        "applied_actions": int(payload.get("applied_actions") or 0),
        "closures_captured": int(payload.get("closures_captured") or 0),
    }


def _write_checkpoint(
    source_event_id: int,
    *,
    mode: str,
    processed: int = 0,
    material_reviews: int = 0,
    applied_actions: int = 0,
    closures_captured: int = 0,
    commit: bool = True,
) -> dict:
    row = emit(
        CHECKPOINT_EVENT_TYPE,
        actor_type="RUNTIME",
        correlation_id="ceo:runtime-checkpoint",
        payload={
            "source_event_id": int(source_event_id),
            "mode": mode,
            "processed": int(processed),
            "material_reviews": int(material_reviews),
            "applied_actions": int(applied_actions),
            "closures_captured": int(closures_captured),
            "provider_calls": 0,
            "historical_replay": False,
        },
        commit=False,
    )
    if commit:
        db.session.commit()
    return {
        "event_id": row.id,
        "source_event_id": int(source_event_id),
        "mode": mode,
        "processed": int(processed),
        "material_reviews": int(material_reviews),
        "applied_actions": int(applied_actions),
        "closures_captured": int(closures_captured),
    }


def ensure_cutover(*, commit: bool = True) -> dict:
    """Establish a future-only cursor without replaying pre-cutover events.

    Cutover also persists one read-only CompanyPlan projection from *current*
    durable truth.  That gives the CEO a restart-safe operating baseline without
    replaying historical events or applying any management effect.
    """
    existing = latest_checkpoint()
    if existing is not None:
        return {"status": "EXISTING", **existing}
    cutoff = _latest_source_event_id()
    snapshot = ceo_operating.company_operating_snapshot()
    review = ceo_management.portfolio_review(snapshot)
    ceo_management.persist_company_plan(
        ceo_management.company_plan(snapshot, review),
        commit=False,
    )
    checkpoint = _write_checkpoint(cutoff, mode="FUTURE_ONLY_CUTOVER", commit=False)
    if commit:
        db.session.commit()
    return {"status": "CUTOVER_ESTABLISHED", **checkpoint}


def _source_events_after(cursor: int, *, max_events: int) -> list[CompanyEvent]:
    return (
        CompanyEvent.query.filter(CompanyEvent.id > int(cursor))
        .filter(~CompanyEvent.event_type.in_(tuple(_INTERNAL_EVENT_TYPES)))
        .order_by(CompanyEvent.id)
        .limit(max(1, int(max_events)))
        .all()
    )


def _review_correlation(trigger: dict) -> str:
    return f"ceo-review:{trigger['dedupe_key']}"


def _existing_review(trigger: dict) -> CompanyEvent | None:
    return CompanyEvent.query.filter(
        CompanyEvent.event_type.in_((WORK_REVIEW_EVENT_TYPE, PROJECT_REVIEW_EVENT_TYPE)),
        CompanyEvent.correlation_id == _review_correlation(trigger),
    ).order_by(CompanyEvent.id.desc()).first()


def _record_review(event: CompanyEvent, trigger: dict, review: dict) -> CompanyEvent:
    event_type = (
        WORK_REVIEW_EVENT_TYPE
        if review.get("review_type") == "WORK_REVIEW"
        else PROJECT_REVIEW_EVENT_TYPE
    )
    return emit(
        event_type,
        actor_type="EMPLOYEE",
        actor_id=getattr(
            Employee.query.filter_by(slug="ceo", active=True).first(),
            "id",
            None,
        ),
        project_id=event.project_id,
        work_id=event.work_id,
        causation_id=event.id,
        correlation_id=_review_correlation(trigger),
        payload={
            "trigger": trigger,
            "review": review,
            "provider_calls": 0,
            "domain_truth_mutated": False,
        },
        commit=False,
    )


def _evaluate_event(event: CompanyEvent) -> tuple[dict, dict | None]:
    trigger = ceo_review.classify_event(event)
    if _existing_review(trigger) is not None:
        return trigger, None
    disposition = trigger["disposition"]
    if disposition == "WORK_REVIEW" and event.work_id:
        work = db.session.get(Work, event.work_id)
        if work is not None:
            return trigger, ceo_review.work_review(work)
    if disposition in {"PROJECT_REVIEW", "ESCALATION_RECHECK"} and event.project_id:
        project = db.session.get(Project, event.project_id)
        if project is None:
            return trigger, None
        if str(project.status or "").upper() in {"PAUSED", *ceo_operating.TERMINAL_PROJECT_STATUSES}:
            return trigger, None
        return trigger, ceo_review.project_review(project)
    return trigger, None


def _apply_one_safe_management_action(reviews: list[tuple[CompanyEvent, dict, dict]]) -> dict | None:
    """Apply at most one current project-level CEO management effect per cycle."""
    for _event, _trigger, review in reviews:
        intent = ceo_management.project_review_to_management_intent(review)
        if intent is None:
            continue
        return ceo_management.execute_management_intent(intent, commit=False)
    return None


def _apply_one_safe_portfolio_action(snapshot: dict, review: dict) -> dict | None:
    """Apply at most one current, governed CEO staffing effect.

    Proven resource contention is resolved before filling unrelated unassigned
    Work so the CEO never amplifies an already-overcommitted Employee.  Every
    effect still revalidates current truth inside the canonical executor.
    """
    proposals = list(review.get("reallocation_decisions") or []) + list(
        review.get("allocation_decisions") or []
    )
    for proposal in proposals:
        intent = ceo_management.proposal_to_action_intent(
            proposal,
            basis_revision=str(snapshot.get("revision") or ""),
        )
        result = ceo_management.execute_action_intent(intent, commit=False)
        if result.get("status") in {"APPLIED", "ALREADY_APPLIED"}:
            return result
        # A stale/blocked proposal is not an error and must not cause the CEO to
        # cascade through lower-ranked moves from one stale portfolio snapshot.
        return result
    return None


def process_pending_events(*, max_events: int = 8, commit: bool = True) -> dict:
    """Process one bounded future-only CEO management batch.

    The batch is deterministic and provider-free.  Review/audit records,
    canonical staffing effect, CompanyPlan and cursor settle in one transaction
    when ``commit`` is true.  Restart replay therefore either sees the durable
    receipt/checkpoint or safely retries an uncommitted batch.
    """
    checkpoint = latest_checkpoint()
    if checkpoint is None:
        return ensure_cutover(commit=commit)

    events = _source_events_after(checkpoint["source_event_id"], max_events=max_events)
    if not events:
        return {
            "status": "IDLE",
            "source_event_id": checkpoint["source_event_id"],
            "processed": 0,
            "material_reviews": 0,
            "applied_actions": 0,
            "closures_captured": 0,
        }

    reviews: list[tuple[CompanyEvent, dict, dict]] = []
    for event in events:
        trigger, review = _evaluate_event(event)
        if review is not None:
            reviews.append((event, trigger, review))

    applied = None
    # One cycle commits at most one governed CEO effect. Material Project-level
    # Project-level management truth (including REPLAN / REQUEST_VERIFICATION
    # authorization) takes precedence over portfolio staffing. The CEO commits
    # only a Decision/receipt here; Company Kernel owns later provider/Work effects.
    if reviews:
        applied = _apply_one_safe_management_action(reviews)
        if applied is None:
            pre_snapshot = ceo_operating.company_operating_snapshot()
            pre_portfolio = ceo_management.portfolio_review(pre_snapshot)
            applied = _apply_one_safe_portfolio_action(pre_snapshot, pre_portfolio)

    for event, trigger, review in reviews:
        _record_review(event, trigger, review)

    closures = []
    for event in events:
        captured = ceo_learning.capture_project_closure_from_event(event, commit=False)
        if captured and captured.get("status") == "CAPTURED":
            closures.append(captured)

    if reviews or closures:
        fresh_snapshot = ceo_operating.company_operating_snapshot()
        fresh_review = ceo_management.portfolio_review(fresh_snapshot)
        ceo_management.persist_company_plan(
            ceo_management.company_plan(fresh_snapshot, fresh_review),
            commit=False,
        )

    last_source_event_id = int(events[-1].id)
    checkpoint_row = _write_checkpoint(
        last_source_event_id,
        mode="FUTURE_ONLY_ADVANCE",
        processed=len(events),
        material_reviews=len(reviews),
        applied_actions=1 if applied and applied.get("status") == "APPLIED" else 0,
        closures_captured=len(closures),
        commit=False,
    )
    if commit:
        db.session.commit()
    return {
        "status": "PROCESSED",
        "source_event_id": last_source_event_id,
        "processed": len(events),
        "material_reviews": len(reviews),
        "applied_actions": 1 if applied and applied.get("status") == "APPLIED" else 0,
        "closures_captured": len(closures),
        "closure_results": closures,
        "action_result": applied,
        "checkpoint_event_id": checkpoint_row["event_id"],
    }
