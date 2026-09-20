"""V0.10.10 truthful Engineering repair migration.

No Provider or real Codex call is made. The migration only repairs persisted
identity, task cursor, run validity, and read-model inputs.
"""
from __future__ import annotations

from decimal import Decimal

from eason_one import create_app
from eason_one.extensions import db
from eason_one.models import AgentRun, Employee, Operation, Task
from eason_one.services.codex_connector import ensure_codex_model_config
from eason_one.services.engineering_runtime import (
    ENGINEERING_REPAIR_GATE,
    ensure_codex_model_history,
    invalidate_formal_mock_run,
    prepare_engineering_repair,
)
from eason_one.services.operations import actual_cost

OPEN = {"PLANNED", "RUNNING", "PAUSED", "WAITING_FOR_FOUNDER"}


def migrate() -> dict:
    summary = {
        "invalidated_runs": [],
        "history_repaired": False,
        "missions_repaired": [],
        "stale_task_states_cleared": [],
        "costs_reconciled": [],
        "engineering_director_inactive": False,
    }
    ceo = Employee.query.filter_by(slug="ceo").first()
    engineer = Employee.query.filter_by(slug="engineer").first()
    if not ceo or not engineer:
        raise RuntimeError("CEO and Engineer are required")
    director = Employee.query.filter_by(slug="engineering-director").first()
    if director:
        director.active = False
        director.employment_status = "INACTIVE"
        director.current_task = None
        summary["engineering_director_inactive"] = True
    codex = ensure_codex_model_config()
    engineer.manager_id = ceo.id
    engineer.current_model_config_id = codex.id
    engineer.role_description = (
        "Translate approved Engineering Tasks into precise bounded Codex Job Specs; "
        "invoke the signed-in local Codex tool; inspect diffs, tests, risks, and "
        "acceptance; retry within approved limits; report directly to the CEO."
    )
    ensure_codex_model_history(engineer, codex)
    summary["history_repaired"] = True

    for run in AgentRun.query.filter(
        AgentRun.provider_key_snapshot == "mock",
        AgentRun.purpose == "TASK_EXECUTION",
        AgentRun.operation_id.isnot(None),
    ).order_by(AgentRun.id).all():
        invalidate_formal_mock_run(run)
        summary["invalidated_runs"].append(run.id)

    # A non-running Mission cannot truthfully leave an Employee visibly WORKING.
    for task in Task.query.filter(Task.status.in_(["WORKING", "REVIEW"])).all():
        if task.operation and task.operation.status != "RUNNING":
            task.status = "ASSIGNED"
            task.result_summary = None if task.assigned_employee and task.assigned_employee.slug == "engineer" else task.result_summary
            summary["stale_task_states_cleared"].append(task.id)

    db.session.commit()

    for operation in Operation.query.filter(Operation.status.in_(OPEN)).order_by(Operation.id).all():
        cost = actual_cost(operation)
        operation.actual_cost_twd = cost
        summary["costs_reconciled"].append((operation.id, str(cost)))
        formal_mock = AgentRun.query.filter_by(
            operation_id=operation.id,
            resolution_status="INVALID_FORMAL_MOCK",
        ).first()
        report = operation.founder_report_json or {}
        needs_repair = bool(
            formal_mock
            or report.get("decision_kind") == ENGINEERING_REPAIR_GATE
        )
        authoritative_codex_success = AgentRun.query.filter(
            AgentRun.operation_id == operation.id,
            AgentRun.provider_key_snapshot == "codex",
            AgentRun.status == "SUCCEEDED",
            AgentRun.structured_validation_status == "PASSED",
        ).order_by(AgentRun.id.desc()).first()
        # Once a real Engineer/Codex result has passed validation, an older
        # invalid Mock record is audit history only. Idempotent migration must
        # not rewind the completed step into a repair cursor.
        if authoritative_codex_success:
            needs_repair = False
        if not needs_repair:
            continue
        operation.status = "PAUSED"
        operation.waiting_reason = (
            "Engineering recovery is ready. Resume starts the earliest unfinished "
            "Engineer Task through Codex exactly once, then pauses before any Critic "
            "or other paid Provider call."
        )
        operation.founder_report_json = {
            "decision_kind": ENGINEERING_REPAIR_GATE,
            "headline": "Engineer → Codex recovery ready.",
            "summary": operation.waiting_reason,
            "next_move": "Resume one bounded Engineer/Codex step or stop this test Mission.",
            "approved_twd": str(operation.approved_budget_twd),
            "actual_twd": str(cost),
            "remaining_twd": str(Decimal(operation.approved_budget_twd) - cost),
            "additional_budget_twd": "0",
            "resulting_authorized_twd": str(operation.approved_budget_twd),
        }
        try:
            target = prepare_engineering_repair(operation, activate=False)
        except ValueError:
            continue
        summary["missions_repaired"].append((operation.id, target.id))

    db.session.commit()
    return summary


def main() -> int:
    app = create_app()
    with app.app_context():
        summary = migrate()
        print("V0.10.10 truthful Engineering migration complete. No Provider or Codex call was made.")
        print(f"  Formal Mock Runs invalidated: {summary['invalidated_runs'] or 'none'}")
        print(f"  Engineer Codex history repaired: {summary['history_repaired']}")
        print(f"  Engineering Director inactive: {summary['engineering_director_inactive']}")
        for operation_id, task_id in summary["missions_repaired"]:
            print(f"  Mission #{operation_id} repair cursor -> Engineer Task #{task_id}; one-step stop enabled.")
        if summary["stale_task_states_cleared"]:
            print(f"  Cleared stale WORKING/REVIEW Tasks: {summary['stale_task_states_cleared']}")
        for operation_id, cost in summary["costs_reconciled"]:
            print(f"  Mission #{operation_id} execution spend reconciled: NT$ {cost}")
        db.session.remove()
        db.engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
