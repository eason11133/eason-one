"""V0.10.7 deterministic repair for existing local data.

No audit record is deleted and no Provider call is made. Open duplicate shells
are marked SUPERSEDED, every pre-existing RUNNING Operation is first moved to a
truthful non-running state because the server is stopped during installation,
and budget estimates are then recomputed where the stored plan is valid.

The important V0.10.7 invariant is simple: after a stopped-server migration,
zero Operations may still claim RUNNING. A malformed historical plan must not
bypass restart safety merely because its cost estimate cannot be calculated.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from eason_one import create_app
from eason_one.extensions import db
from eason_one.models import AgentRun, Operation, now
from eason_one.services.operations import (
    BUDGET_QUANTUM,
    execution_budget_estimate,
    plan_fingerprint,
    ensure_full_execution_authority,
)

OPEN = {"PLANNED", "RUNNING", "WAITING_FOR_FOUNDER", "PAUSED"}
RANK = {"RUNNING": 4, "WAITING_FOR_FOUNDER": 3, "PAUSED": 2, "PLANNED": 1}


def _execution_run_count(operation: Operation) -> int:
    return AgentRun.query.filter(
        AgentRun.operation_id == operation.id,
        AgentRun.purpose != "CEO_FOUNDER_REQUEST",
    ).count()


def _has_execution(operation: Operation) -> bool:
    return _execution_run_count(operation) > 0


def _runtime_memory(operation: Operation) -> tuple[dict, dict]:
    memory = dict(operation.memory_json or {})
    runtime = dict(memory.get("runtime") or {})
    memory["runtime"] = runtime
    return memory, runtime


def _pause_for_restart(operation: Operation, *, reason: str, invalid_plan: bool = False) -> None:
    """Move a stopped-server RUNNING shell to a truthful persisted state."""
    operation.status = "PAUSED"
    operation.waiting_reason = reason
    memory, runtime = _runtime_memory(operation)
    runtime.update({
        "state": "PAUSED",
        "last_error": reason if invalid_plan else None,
        "migration_pause": True,
        "worker_alive": False,
    })
    if invalid_plan:
        memory["migration_plan_review_required"] = True
    operation.memory_json = memory


def _mark_plan_review(operation: Operation, error: Exception) -> None:
    """Persist an invalid historical plan without pretending it can execute."""
    reason = (
        "V0.10.7 stopped this historical Mission before any Provider call because "
        f"its stored execution contract needs review: {error}"
    )
    operation.status = "WAITING_FOR_FOUNDER"
    operation.waiting_reason = reason
    memory, runtime = _runtime_memory(operation)
    runtime.update({
        "state": "NEEDS_FOUNDER",
        "last_error": str(error),
        "migration_pause": True,
        "worker_alive": False,
    })
    memory["migration_plan_review_required"] = True
    memory["migration_plan_error"] = str(error)
    operation.memory_json = memory
    operation.founder_report_json = {
        "decision_kind": "PLAN_REPAIR",
        "headline": "This historical Mission needs one contract review.",
        "summary": reason,
        "next_move": "Review, modify, supersede, or stop this Mission. No Provider call was made.",
        "next_action": reason,
    }


def migrate() -> dict:
    summary = {
        "superseded": [],
        "planned_estimates": [],
        "budget_gates": [],
        "paused_stale": [],
        "plan_reviews": [],
        "safety_holds": [],
        "skipped": [],
    }
    rows = Operation.query.filter(Operation.status.in_(OPEN)).order_by(Operation.id).all()

    # Preserve history but collapse repeated, unexecuted Mission shells.
    groups = defaultdict(list)
    for operation in rows:
        title_key = " ".join((operation.title or "").casefold().split())
        project_key = operation.project_id or 0
        try:
            fingerprint = plan_fingerprint(operation.plan_json)
        except Exception as exc:
            fingerprint = f"unfingerprinted:{operation.id}"
            summary["skipped"].append((operation.id, f"fingerprint: {exc}"))
        groups[("fingerprint", fingerprint)].append(operation)
        if title_key:
            groups[("title", project_key, title_key)].append(operation)

    superseded_ids = set()
    for key, group in groups.items():
        group = list({item.id: item for item in group}.values())
        if len(group) < 2:
            continue
        canonical = sorted(
            group,
            key=lambda item: (bool(_has_execution(item)), RANK.get(item.status, 0), item.id),
            reverse=True,
        )[0]
        for duplicate in group:
            if duplicate.id == canonical.id or duplicate.id in superseded_ids:
                continue
            if _has_execution(duplicate):
                continue
            duplicate.status = "SUPERSEDED"
            duplicate.ended_at = duplicate.ended_at or now()
            duplicate.waiting_reason = (
                f"Superseded by Operation #{canonical.id}; repeated open Mission ({key[0]} match)."
            )
            memory = dict(duplicate.memory_json or {})
            memory["superseded_by_operation_id"] = canonical.id
            memory["superseded_reason"] = "DUPLICATE_FOUNDER_PLAN"
            duplicate.memory_json = memory
            superseded_ids.add(duplicate.id)
            summary["superseded"].append((duplicate.id, canonical.id))
    db.session.commit()

    # The installer requires port 5000 to be stopped. Therefore no old worker
    # can still be authoritative. Pause EVERY surviving RUNNING row before any
    # plan validation or estimate. This fixes V0.10.6, where malformed plans
    # skipped the estimate branch and incorrectly remained RUNNING.
    for operation in Operation.query.filter_by(status="RUNNING").order_by(Operation.id).all():
        run_count = _execution_run_count(operation)
        reason = (
            "Paused by the V0.10.7 restart-safety repair after prior Employee execution. "
            "Founder may explicitly resume the server runtime after reviewing current state."
            if run_count
            else
            "Paused by the V0.10.7 restart-safety repair before the first Employee Run. "
            "The previous RUNNING label had no live server worker and no Provider call was made."
        )
        _pause_for_restart(operation, reason=reason)
        summary["paused_stale"].append(operation.id)
    db.session.commit()

    # Estimate valid open plans after restart safety has already made state
    # truthful. Invalid historical contracts are moved to a Founder review gate
    # instead of being left active or causing installation rollback.
    candidates = Operation.query.filter(
        Operation.status.in_(["PLANNED", "PAUSED", "WAITING_FOR_FOUNDER"])
    ).order_by(Operation.id).all()
    for operation in candidates:
        if operation.status == "SUPERSEDED":
            continue
        try:
            estimate, breakdown, _ = execution_budget_estimate(operation.plan_json)
        except Exception as exc:
            # Historical malformed plans are not an installer failure. They are
            # preserved, explicitly gated, and excluded from runtime dispatch.
            if operation.status in {"PAUSED", "WAITING_FOR_FOUNDER"}:
                _mark_plan_review(operation, exc)
                summary["plan_reviews"].append((operation.id, str(exc)))
                db.session.commit()
            else:
                summary["skipped"].append((operation.id, f"estimate: {exc}"))
            continue

        memory = dict(operation.memory_json or {})
        memory["execution_budget_estimate_twd"] = str(estimate)
        memory["execution_budget_breakdown"] = breakdown
        operation.memory_json = memory

        if operation.status == "PLANNED":
            proposed = Decimal(str(operation.approved_budget_twd or 0))
            if proposed < estimate:
                operation.approved_budget_twd = estimate
                plan = dict(operation.plan_json or {})
                data = dict(plan.get("operation") or {})
                data["budget_twd"] = str(estimate)
                plan["operation"] = data
                operation.plan_json = plan
            summary["planned_estimates"].append((operation.id, str(estimate)))
            db.session.commit()
            continue

        # A paused shell with no paid Employee Run can be moved directly to a
        # precise Founder budget gate. Already-paid Missions stay paused for an
        # explicit resume decision, preserving their prior execution history.
        if operation.status == "PAUSED" and not _has_execution(operation):
            approved_before = Decimal(str(operation.approved_budget_twd or 0))
            if approved_before < estimate:
                ensure_full_execution_authority(operation)
                summary["budget_gates"].append((
                    operation.id,
                    str(approved_before.quantize(BUDGET_QUANTUM)),
                    str(estimate),
                ))
        db.session.commit()

    # Fail-safe invariant: a stopped-server migration must never leave a row
    # claiming RUNNING, even if a future schema edge case bypasses a branch.
    for operation in Operation.query.filter_by(status="RUNNING").order_by(Operation.id).all():
        reason = (
            "V0.10.7 applied a final restart-safety hold because no live server worker "
            "can exist during installation. No Provider call was made."
        )
        _pause_for_restart(operation, reason=reason)
        summary["safety_holds"].append(operation.id)
    db.session.commit()
    return summary


def main() -> int:
    app = create_app()
    with app.app_context():
        summary = migrate()
        print("V0.10.7 data repair complete.")
        for duplicate, canonical in summary["superseded"]:
            print(f"  Superseded duplicate Operation #{duplicate} -> #{canonical}")
        for operation_id, estimate in summary["planned_estimates"]:
            print(f"  Planned Operation #{operation_id} system execution estimate: NT$ {estimate}")
        for operation_id, approved, estimate in summary["budget_gates"]:
            print(
                f"  Operation #{operation_id} moved to Founder budget gate: authorized NT$ {approved}; "
                f"system execution estimate NT$ {estimate}."
            )
        for operation_id in summary["paused_stale"]:
            print(
                f"  Paused pre-existing Operation #{operation_id} for restart safety; "
                "no automatic Provider call was made."
            )
        for operation_id, reason in summary["plan_reviews"]:
            print(f"  Operation #{operation_id} moved to PLAN_REPAIR gate: {reason}")
        for operation_id in summary["safety_holds"]:
            print(f"  Applied final non-running safety hold to Operation #{operation_id}.")
        for operation_id, reason in summary["skipped"]:
            print(f"  Review Operation #{operation_id}: {reason}")
        remaining_running = Operation.query.filter_by(status="RUNNING").count()
        if remaining_running:
            raise RuntimeError(
                f"V0.10.7 restart safety failed: {remaining_running} Operation(s) still RUNNING"
            )
        db.session.remove()
        db.engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
