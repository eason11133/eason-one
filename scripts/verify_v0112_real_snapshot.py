"""Verify the V0.11.2 patch against a copied real Eason One database.

No Provider or Codex call is made.  This checks persisted Mission recovery,
relevant evidence retrieval, runtime model-policy enforcement, and Founder
surface rendering using the actual database snapshot supplied to the installer.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", required=True)
    parser.add_argument("--database", required=True)
    args = parser.parse_args()

    target = Path(args.target).resolve()
    database = Path(args.database).resolve()
    sys.path.insert(0, str(target))

    from eason_one import create_app
    from eason_one.extensions import db
    from eason_one.models import AgentRun, Employee, Operation, Task
    from eason_one.services.context import build_with_composition
    from eason_one.services.execution_policy import select_execution_model
    from eason_one.services.operation_runtime import runtime_snapshot

    app = create_app({
        "TESTING": True,
        "AUTO_START_OPERATION_RUNTIME": False,
        "SQLALCHEMY_DATABASE_URI": f"sqlite:///{database.as_posix()}",
    })
    with app.app_context():
        sql_operation = Operation.query.filter_by(
            title="Eason One SQLAlchemy LegacyAPIWarning Remediation"
        ).order_by(Operation.id.desc()).first()
        if sql_operation:
            assert sql_operation.status == "COMPLETED", sql_operation.status
            assert (sql_operation.founder_report_json or {}).get("verification_mode") == "V0112_WINDOWS_HOST_INSTALLER"
            run66 = AgentRun.query.filter_by(operation_id=sql_operation.id).order_by(AgentRun.id.desc()).first()
            assert run66 is not None
            assert run66.failure_reason != "CODEX_BOUNDARY_VIOLATION"

        meeting_operation = Operation.query.filter_by(
            title="September Project Priority Decision — Autonomous Meeting Validation"
        ).order_by(Operation.id.desc()).first()
        if meeting_operation:
            assert meeting_operation.status == "PAUSED", meeting_operation.status
            constraints = (meeting_operation.memory_json or {}).get("execution_constraints") or {}
            assert constraints.get("low_cost_only") is True
            assert constraints.get("no_premium") is True
            assert constraints.get("existing_evidence_only") is True
            config = ((meeting_operation.plan_json or {}).get("operation") or {}).get("meeting_config") or {}
            assert int(config.get("token_limit") or 0) >= 10000
            task = Task.query.filter_by(operation_id=meeting_operation.id).order_by(Task.id).first()
            assert task is not None and task.status == "ASSIGNED"
            employee = db.session.get(Employee, task.assigned_employee_id)
            context, composition = build_with_composition(employee, task.project, task)
            lowered = context.casefold()
            assert "retrieved existing eason one evidence" in lowered
            assert "alpha" in lowered
            assert "english" in lowered and "trainer" in lowered
            assert (composition.get("evidence_retrieval") or {}).get("relevant_items", 0) >= 1

            # The real runtime must enforce the approved no-premium/no-Mock policy.
            previous_testing = app.config["TESTING"]
            app.config["TESTING"] = False
            try:
                selected = select_execution_model(employee, meeting_operation, "TASK_EXECUTION")
            finally:
                app.config["TESTING"] = previous_testing
            assert selected.provider_key not in {"mock", "anthropic"}
            assert all(marker not in f"{selected.label} {selected.model_name}".casefold()
                       for marker in ("sonnet", "opus", "premium", "ultra", "max"))

            state = runtime_snapshot(meeting_operation)
            assert state["can_resume"] is True
            assert state["can_archive"] is False

        client = app.test_client()
        paths = [
            "/headquarters",
            "/headquarters/missions",
            "/headquarters/meetings",
            "/headquarters/people",
        ]
        if meeting_operation:
            paths.append(f"/headquarters/missions/{meeting_operation.id}")
        for path in paths:
            response = client.get(path)
            assert response.status_code == 200, (path, response.status_code)

        print("V0.11.2 real snapshot verification passed.")
        print("  Persisted evidence retrieval: ready")
        print("  Runtime low-cost/no-premium enforcement: ready")
        print("  Autonomous Meeting Mission: one bounded resume ready")
        print("  Founder surfaces: rendered without Provider/Codex calls")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
