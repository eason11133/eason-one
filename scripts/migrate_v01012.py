from __future__ import annotations

import argparse
from pathlib import Path
import sys


def migrate() -> dict:
    from eason_one.extensions import db
    from eason_one.models import AgentRun, Operation, now
    from eason_one.services.engineering_runtime import archive_validation_mission

    repaired_live = []
    archived = []
    for operation in Operation.query.order_by(Operation.id).all():
        memory = dict(operation.memory_json or {})
        runtime = dict(memory.get("runtime") or {})
        live = dict(runtime.get("live_execution") or {})
        latest_codex = (
            AgentRun.query.filter(
                AgentRun.operation_id == operation.id,
                AgentRun.provider_key_snapshot == "codex",
                AgentRun.status == "SUCCEEDED",
                AgentRun.structured_validation_status == "PASSED",
            )
            .order_by(AgentRun.id.desc())
            .first()
        )
        if latest_codex and not live.get("run_id"):
            codex = ((latest_codex.parsed_output_json or {}).get("codex") or {})
            started = latest_codex.started_at.isoformat() if latest_codex.started_at else None
            finished = latest_codex.finished_at.isoformat() if latest_codex.finished_at else now().isoformat()
            live.update({
                "state": "COMPLETED",
                "phase": "EVIDENCE_PERSISTED",
                "detail": (
                    f"Historical Codex Run #{latest_codex.id} completed before live event streaming was enabled. "
                    "The authoritative Job Spec, structured output, tests, and audit evidence remain available."
                ),
                "current_action": None,
                "run_id": latest_codex.id,
                "task_id": latest_codex.task_id,
                "started_at": started,
                "last_activity_at": finished,
                "finished_at": finished,
                "counters": {
                    "commands_started": 0,
                    "commands_completed": 0,
                    "tests_started": len(codex.get("tests") or []),
                    "tests_completed": len(codex.get("tests") or []),
                    "files_changed": codex.get("changed_files") or [],
                    "tests": codex.get("tests") or [],
                    "acceptance": codex.get("acceptance") or [],
                    "risks": codex.get("risks") or [],
                },
                "events": [{
                    "at": finished,
                    "kind": "MIGRATION",
                    "phase": "EVIDENCE_PERSISTED",
                    "detail": f"Reconstructed completion summary for Codex Run #{latest_codex.id}; no fabricated intermediate events were added.",
                }],
            })
            runtime["live_execution"] = live
            runtime["active_task_id"] = None
            runtime["heartbeat_at"] = finished
            memory["runtime"] = runtime
            operation.memory_json = memory
            repaired_live.append(operation.id)

        # The reliability assessment was explicitly created as an internal test
        # Mission. Once the WSL2 Engineer/Codex step succeeds, archive it instead
        # of launching Critic/Claude or pretending the test is a product project.
        report = operation.founder_report_json or {}
        is_validation_case = (
            operation.title.strip().casefold() == "ceo direct line reliability assessment"
            and report.get("decision_kind") == "ENGINEERING_STEP_REVIEW"
            and latest_codex is not None
        )
        if is_validation_case:
            result = archive_validation_mission(
                operation,
                reason=(
                    "Founder archived this internal runtime-validation Mission after the "
                    "Founder → CEO → Engineer → WSL2 Codex chain completed one authoritative step. "
                    "Critic, Meeting, and further paid Provider work were intentionally cancelled."
                ),
                auto=True,
            )
            archived.append(result["operation_id"])

    db.session.commit()
    return {"live_repaired": repaired_live, "archived": archived}


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
        db.session.remove()
        db.engine.dispose()
    print("V0.10.12 live execution/archive migration complete. No Provider or Codex call was made.")
    print(f"  Historical live summaries repaired: {result['live_repaired'] or 'none'}")
    print(f"  Runtime validation Missions archived: {result['archived'] or 'none'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
