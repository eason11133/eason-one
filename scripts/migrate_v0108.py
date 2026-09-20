"""V0.10.8 deterministic UI/runtime consistency repair.

No Provider call is made and no audit record is deleted. The migration:
- collapses repeated open Mission shells with the same normalized title and
  materially equivalent objective into one canonical Mission;
- preserves every superseded record and all linked Runs/Tasks for audit;
- clears stale in-memory runtime labels on every non-running Operation so the
  server-rendered card and the polling endpoint cannot disagree.
"""
from __future__ import annotations

from collections import defaultdict
from difflib import SequenceMatcher
import os
import re
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from eason_one import create_app
from eason_one.extensions import db
from eason_one.models import AgentRun, Operation, Task, now

OPEN = {"PLANNED", "RUNNING", "WAITING_FOR_FOUNDER", "PAUSED"}


def _norm(value: str | None) -> str:
    text = (value or "").casefold()
    text = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", text)
    return " ".join(text.split())


def _execution_run_count(operation_id: int) -> int:
    return AgentRun.query.filter(
        AgentRun.operation_id == operation_id,
        AgentRun.purpose != "CEO_FOUNDER_REQUEST",
    ).count()


def _task_count(operation_id: int) -> int:
    return Task.query.filter_by(operation_id=operation_id).count()


def _equivalent(a: Operation, b: Operation) -> bool:
    if _norm(a.title) != _norm(b.title):
        return False
    ao, bo = _norm(a.objective), _norm(b.objective)
    if not ao or not bo:
        return True
    return SequenceMatcher(None, ao, bo).ratio() >= 0.72


def _canonical(group: list[Operation]) -> Operation:
    # Keep the Mission with real execution history first, then materialized
    # Tasks, then the newest record. Superseding never deletes older history.
    return max(
        group,
        key=lambda op: (
            _execution_run_count(op.id) > 0,
            _task_count(op.id) > 0,
            op.id,
        ),
    )


def _runtime_state(operation: Operation) -> str:
    if operation.status == "WAITING_FOR_FOUNDER":
        report = operation.founder_report_json or {}
        return "WAITING_FOR_BUDGET" if report.get("decision_kind") == "BUDGET_AUTHORIZATION" else "NEEDS_FOUNDER"
    if operation.status == "PAUSED":
        return "PAUSED"
    if operation.status == "PLANNED":
        return "PLANNED"
    if operation.status == "SUPERSEDED":
        return "SUPERSEDED"
    if operation.status == "COMPLETED":
        return "COMPLETED"
    if operation.status == "FAILED":
        return "FAILED"
    if operation.status == "TERMINATED_BY_FOUNDER":
        return "PAUSED"
    return operation.status


def migrate() -> dict:
    summary = {"superseded": [], "runtime_normalized": [], "paused_running": []}

    # The installer requires the local server to be stopped. Any persisted
    # RUNNING label therefore has no authoritative in-process worker and is
    # paused before the UI is rendered. No Provider call is made.
    for operation in Operation.query.filter_by(status="RUNNING").order_by(Operation.id).all():
        operation.status = "PAUSED"
        operation.waiting_reason = (
            "Paused by the V0.10.8 restart-safety repair because the server was stopped during installation. "
            "Founder may resume after reviewing the current state."
        )
        memory = dict(operation.memory_json or {})
        runtime = dict(memory.get("runtime") or {})
        runtime.update({"state": "PAUSED", "worker_alive": False})
        memory["runtime"] = runtime
        operation.memory_json = memory
        summary["paused_running"].append(operation.id)
    db.session.commit()

    rows = Operation.query.filter(Operation.status.in_(OPEN)).order_by(Operation.id).all()
    by_title: dict[str, list[Operation]] = defaultdict(list)
    for operation in rows:
        key = _norm(operation.title)
        if key:
            by_title[key].append(operation)

    for group in by_title.values():
        if len(group) < 2:
            continue
        # Some legitimate Missions can share a title. Collapse only connected
        # equivalence clusters by objective similarity.
        pending = list(group)
        while pending:
            seed = pending.pop(0)
            cluster = [seed]
            rest = []
            for candidate in pending:
                if any(_equivalent(candidate, member) for member in cluster):
                    cluster.append(candidate)
                else:
                    rest.append(candidate)
            pending = rest
            if len(cluster) < 2:
                continue
            canonical = _canonical(cluster)
            for duplicate in cluster:
                if duplicate.id == canonical.id:
                    continue
                duplicate.status = "SUPERSEDED"
                duplicate.ended_at = duplicate.ended_at or now()
                duplicate.waiting_reason = (
                    f"Superseded by Operation #{canonical.id}; repeated Mission shell retained for audit."
                )
                memory = dict(duplicate.memory_json or {})
                runtime = dict(memory.get("runtime") or {})
                runtime.update({
                    "state": "SUPERSEDED",
                    "worker_alive": False,
                    "heartbeat_at": runtime.get("heartbeat_at"),
                })
                memory["runtime"] = runtime
                memory["superseded_by_operation_id"] = canonical.id
                memory["superseded_reason"] = "DUPLICATE_MISSION_REPAIR_V0108"
                duplicate.memory_json = memory
                summary["superseded"].append((duplicate.id, canonical.id))
    db.session.commit()

    # No server worker can be alive while the installer has port 5000 stopped.
    # Persist one status-derived runtime label for every non-running row.
    rows = Operation.query.filter(Operation.status != "RUNNING").order_by(Operation.id).all()
    for operation in rows:
        memory = dict(operation.memory_json or {})
        runtime = dict(memory.get("runtime") or {})
        authoritative = _runtime_state(operation)
        changed = runtime.get("state") != authoritative or runtime.get("worker_alive") is not False
        runtime["state"] = authoritative
        runtime["worker_alive"] = False
        memory["runtime"] = runtime
        operation.memory_json = memory
        if changed:
            summary["runtime_normalized"].append((operation.id, authoritative))
    db.session.commit()
    return summary


def main() -> int:
    app = create_app()
    with app.app_context():
        summary = migrate()
        print("V0.10.8 data consistency repair complete.")
        for operation_id in summary["paused_running"]:
            print(f"  Paused stale RUNNING Operation #{operation_id}; no Provider call was made.")
        for duplicate, canonical in summary["superseded"]:
            print(f"  Superseded duplicate Operation #{duplicate} -> #{canonical}")
        for operation_id, state in summary["runtime_normalized"]:
            print(f"  Normalized Operation #{operation_id} runtime state -> {state}")
        db.session.remove()
        db.engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
