"""V0.11.0 Stabilization Reset migration.

No Provider or Codex call is made.  The migration separates real company work
from system-validation history, repairs the known Run #63 validation
infrastructure misclassification when its own evidence proves no job-caused
change, and clears stale employee/task presence outside running Missions.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


def _payload(run) -> dict:
    parsed = dict(run.parsed_output_json or {})
    if parsed.get("codex"):
        return dict(parsed["codex"])
    raw = (run.raw_output or "").strip()
    try:
        value = json.loads(raw)
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def migrate() -> dict:
    from eason_one.extensions import db
    from eason_one.models import AgentRun, Operation, Task, now
    from eason_one.services.stabilization import (
        REAL_WORK, SYSTEM_VALIDATION, operation_kind, stamp_kind,
    )

    summary = {
        "real": [], "validation": [], "partial_runs": [],
        "stale_tasks_cleared": [],
    }
    operations = Operation.query.order_by(Operation.id).all()
    for operation in operations:
        kind = operation_kind(operation)
        stamp_kind(
            operation, kind,
            reason=(
                "Internal smoke/runtime validation; excluded from real company work."
                if kind == SYSTEM_VALIDATION else
                "Founder/project work; included in the real Mission registry."
            ),
        )
        summary["validation" if kind == SYSTEM_VALIDATION else "real"].append(operation.id)

        if operation.status != "RUNNING":
            for task in operation.tasks:
                if task.status not in {"WORKING", "REVIEW"}:
                    continue
                has_result = bool(task.result_summary)
                if not has_result:
                    latest = (AgentRun.query.filter_by(task_id=task.id)
                              .order_by(AgentRun.id.desc()).first())
                    has_result = bool(latest and (_payload(latest).get("summary") or latest.parsed_output_json))
                next_status = "REVIEW" if has_result else "ASSIGNED"
                if task.status != next_status:
                    task.status = next_status
                    summary["stale_tasks_cleared"].append(task.id)

    for run in AgentRun.query.filter_by(
        provider_key_snapshot="codex", failure_reason="CODEX_BOUNDARY_VIOLATION"
    ).order_by(AgentRun.id).all():
        operation = db.session.get(Operation, run.operation_id) if run.operation_id else None
        if not operation or operation_kind(operation) != SYSTEM_VALIDATION:
            continue
        payload = _payload(run)
        summary_text = str(payload.get("summary") or run.raw_output or "")
        changed = payload.get("changed_files")
        no_job_delta = (
            changed == []
            or "no job-caused" in summary_text.casefold()
            or "no job caused" in summary_text.casefold()
        )
        if not no_job_delta:
            continue
        if payload and not (run.parsed_output_json or {}).get("codex"):
            run.parsed_output_json = {
                "result_summary": payload.get("summary") or "Useful validation evidence was preserved.",
                "knowledge_proposals": [],
                "codex": payload,
            }
        run.resolution_status = "VALIDATION_INFRASTRUCTURE_FAILURE"
        run.resolution_note = (
            "The validation harness treated pre-existing repository dirt and/or the "
            "read-only Codex initialization limitation as a new boundary violation. "
            "The Run's own before/after evidence reports no job-caused repository change; "
            "useful findings are retained as a partial validation Artifact."
        )
        run.resolved_at = run.resolved_at or now()
        run.structured_validation_status = "PARTIAL"
        run.structured_validation_warnings_json = [
            "Useful evidence exists; the old boundary validator was not authoritative for this Run."
        ]
        task = db.session.get(Task, run.task_id) if run.task_id else None
        if task:
            task.status = "REVIEW"
            task.result_summary = payload.get("summary") or task.result_summary
        operation.status = "PAUSED"
        operation.waiting_reason = (
            "System validation produced a partial result. No retry or paid Provider step is required."
        )
        report = dict(operation.founder_report_json or {})
        report.update({
            "decision_kind": "SYSTEM_VALIDATION_REVIEW",
            "summary": payload.get("summary") or "Partial validation evidence is ready for review.",
            "requires_paid_provider": False,
        })
        operation.founder_report_json = report
        memory = dict(operation.memory_json or {})
        runtime = dict(memory.get("runtime") or {})
        runtime.update({"state": "PAUSED", "active_task_id": None, "worker_alive": False})
        memory["runtime"] = runtime
        operation.memory_json = memory
        summary["partial_runs"].append(run.id)

    db.session.commit()
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", default=".")
    target = Path(parser.parse_args().target).resolve()
    sys.path.insert(0, str(target))
    from eason_one import create_app
    from eason_one.extensions import db

    app = create_app({"AUTO_START_OPERATION_RUNTIME": False})
    with app.app_context():
        result = migrate()
        db.session.remove(); db.engine.dispose()
    print("V0.11.0 stabilization migration complete. No Provider or Codex call was made.")
    print(f"  Real Missions: {result['real'] or 'none'}")
    print(f"  System validation Missions: {result['validation'] or 'none'}")
    print(f"  Partial validation Runs repaired: {result['partial_runs'] or 'none'}")
    print(f"  Stale Task presence cleared: {result['stale_tasks_cleared'] or 'none'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
