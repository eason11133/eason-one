"""V0.10.9 engineering runtime migration. Zero Provider/Codex calls."""
from __future__ import annotations

from decimal import Decimal
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from sqlalchemy import func, or_
from eason_one import create_app
from eason_one.extensions import db
from eason_one.models import AgentRun, Company, CostEvent, Employee, Operation, Task
from eason_one.seed import seed
from eason_one.services.codex_connector import ensure_codex_model_config
from eason_one.services.operations import _normalize_engineering_plan

OPEN = {"PLANNED", "RUNNING", "WAITING_FOR_FOUNDER", "PAUSED"}


def _spent(operation_id: int) -> Decimal:
    return Decimal((db.session.query(func.coalesce(func.sum(CostEvent.real_cost_delta), 0))
        .outerjoin(AgentRun, CostEvent.agent_run_id == AgentRun.id)
        .filter(CostEvent.operation_id == operation_id)
        .filter(or_(
            CostEvent.agent_run_id.is_(None),
            AgentRun.purpose != "CEO_FOUNDER_REQUEST",
        )).scalar()))


def _repair_cost(operation: Operation) -> None:
    actual = _spent(operation.id)
    operation.actual_cost_twd = actual
    report = dict(operation.founder_report_json or {})
    if report:
        report["actual_twd"] = str(actual)
        report["remaining_twd"] = str(Decimal(operation.approved_budget_twd) - actual)
        operation.founder_report_json = report


def migrate() -> dict:
    if not Company.query.first():
        seed()
    summary = {
        "director_inactivated": False,
        "engineer_codex_assigned": False,
        "plans_normalized": [],
        "mock_runs_invalidated": [],
        "tasks_reset": [],
        "missions_paused": [],
        "costs_reconciled": [],
    }
    ceo = Employee.query.filter_by(slug="ceo").first()
    engineer = Employee.query.filter_by(slug="engineer").first()
    director = Employee.query.filter_by(slug="engineering-director").first()
    codex = ensure_codex_model_config()

    if engineer:
        engineer.manager_id = getattr(ceo, "id", None)
        engineer.current_model_config_id = codex.id
        engineer.role_description = (
            "Translate approved Engineering Tasks into precise bounded Codex Job Specs; "
            "invoke the local Codex tool; inspect diffs, tests, risks, and acceptance; "
            "retry within approved limits; report directly to the CEO."
        )
        engineer.system_instructions = (
            "You are the accountable Engineer. Do not directly author repository code through a general chat model. "
            "Create a precise bounded Codex Job Spec, keep Codex inside the approved repository/scope, inspect its "
            "diff and test evidence, retry only within the approved limit, and escalate only destructive, secret, "
            "dependency, migration, deployment, scope, or usage-boundary changes."
        )
        summary["engineer_codex_assigned"] = True
    if director:
        director.active = False
        director.employment_status = "INACTIVE"
        summary["director_inactivated"] = True
    db.session.commit()

    for operation in Operation.query.filter(Operation.status.in_(OPEN)).order_by(Operation.id).all():
        before = repr(operation.plan_json)
        operation.plan_json = _normalize_engineering_plan(dict(operation.plan_json or {}))
        if repr(operation.plan_json) != before:
            summary["plans_normalized"].append(operation.id)
        for task in operation.tasks:
            if engineer and director and task.assigned_employee_id == director.id:
                task.assigned_employee_id = engineer.id
            if engineer and task.assigned_employee_id == engineer.id:
                task.reviewer_employee_id = engineer.id
        _repair_cost(operation)
        summary["costs_reconciled"].append((operation.id, str(operation.actual_cost_twd)))
    db.session.commit()

    # Formal Mission Mock output is audit evidence of a configuration defect,
    # never a valid Artifact or dependency for later paid work.
    mock_runs = AgentRun.query.filter(
        AgentRun.provider_key_snapshot == "mock",
        AgentRun.purpose == "TASK_EXECUTION",
        AgentRun.operation_id.isnot(None),
    ).order_by(AgentRun.id).all()
    affected_operations = set()
    for run in mock_runs:
        operation = db.session.get(Operation, run.operation_id)
        if not operation or operation.status not in OPEN:
            continue
        memory = operation.memory_json or {}
        if memory.get("simulation_mode"):
            continue
        run.resolution_status = "INVALID_FORMAL_MOCK"
        run.resolution_note = (
            "V0.10.9 invalidated this formal Mission output because MockProvider cannot produce authoritative work."
        )
        summary["mock_runs_invalidated"].append(run.id)
        task = db.session.get(Task, run.task_id) if run.task_id else None
        if task:
            later_real = AgentRun.query.filter(
                AgentRun.task_id == task.id,
                AgentRun.id > run.id,
                AgentRun.status == "SUCCEEDED",
                AgentRun.provider_key_snapshot != "mock",
            ).first()
            if not later_real:
                task.status = "ASSIGNED"
                task.result_summary = None
                task.completed_at = None
                summary["tasks_reset"].append(task.id)
        affected_operations.add(operation.id)
    db.session.commit()

    for operation_id in affected_operations:
        operation = db.session.get(Operation, operation_id)
        if not operation or operation.status not in OPEN:
            continue
        operation.status = "PAUSED"
        operation.waiting_reason = (
            "Engineering workflow repaired: formal Mock output was invalidated. "
            "Resume only after reviewing the Engineer → Codex configuration and existing paid failure evidence."
        )
        actual = _spent(operation.id)
        operation.actual_cost_twd = actual
        operation.founder_report_json = {
            "decision_kind": "ENGINEERING_RUNTIME_REPAIR",
            "headline": "Engineering workflow repaired.",
            "summary": operation.waiting_reason,
            "next_move": "Review current Tasks and resume within the existing authority, or stop the test Mission.",
            "approved_twd": str(operation.approved_budget_twd),
            "actual_twd": str(actual),
            "remaining_twd": str(Decimal(operation.approved_budget_twd) - actual),
            "additional_budget_twd": "0",
            "resulting_authorized_twd": str(operation.approved_budget_twd),
        }
        memory = dict(operation.memory_json or {})
        runtime = dict(memory.get("runtime") or {})
        runtime.update({"state": "PAUSED", "worker_alive": False, "last_error": operation.waiting_reason})
        memory["runtime"] = runtime
        memory["engineering"] = {
            **dict(memory.get("engineering") or {}),
            "connector": "codex-cli",
            "codex_retry_limit": 1,
            "max_changed_files": 20,
        }
        operation.memory_json = memory
        summary["missions_paused"].append(operation.id)
    db.session.commit()
    return summary


def main() -> int:
    app = create_app()
    with app.app_context():
        summary = migrate()
        print("V0.10.9 engineering runtime migration complete. No Provider or Codex call was made.")
        print(f"  Engineer assigned to Codex CLI: {summary['engineer_codex_assigned']}")
        print(f"  Engineering Director inactive: {summary['director_inactivated']}")
        for run_id in summary["mock_runs_invalidated"]:
            print(f"  Invalidated formal Mock Run #{run_id}; audit retained.")
        for task_id in summary["tasks_reset"]:
            print(f"  Reset Task #{task_id} to ASSIGNED for real Engineer/Codex execution.")
        for operation_id in summary["missions_paused"]:
            print(f"  Paused Mission #{operation_id} at ENGINEERING_RUNTIME_REPAIR gate; no spend.")
        for operation_id, actual in summary["costs_reconciled"]:
            print(f"  Reconciled Mission #{operation_id} execution spend: NT$ {actual}")
        db.session.remove()
        db.engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
