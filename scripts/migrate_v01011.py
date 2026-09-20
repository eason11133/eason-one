"""V0.10.11 WSL2 Codex transport migration.

No Provider or Codex execution is performed. The migration preserves failed
Windows-native Codex Runs as audit evidence, returns affected Engineering Tasks
to ASSIGNED, and prepares one bounded WSL2 Engineer recovery step.
"""
from __future__ import annotations

from decimal import Decimal

from eason_one import create_app
from eason_one.extensions import db
from eason_one.models import AgentRun, Employee, EmployeeModelHistory, Operation, Task
from eason_one.services.engineering_runtime import (
    ENGINEERING_REPAIR_GATE,
    prepare_engineering_repair,
)
from eason_one.services.operations import actual_cost

OPEN = {"PLANNED", "RUNNING", "PAUSED", "WAITING_FOR_FOUNDER"}
WINDOWS_SANDBOX_MARKERS = (
    "codex-windows-sandbox-setup.exe",
    "CreateProcessAsUserW failed",
    "Windows error 2",
    "windows sandbox failed",
)


def _is_windows_sandbox_failure(run: AgentRun) -> bool:
    if run.provider_key_snapshot != "codex" or run.status != "FAILED":
        return False
    text = " ".join([
        run.error_text or "",
        run.failure_reason or "",
        str(run.structured_validation_errors_json or ""),
        str((run.context_composition_json or {}).get("stderr_tail") or ""),
    ])
    return any(marker.casefold() in text.casefold() for marker in WINDOWS_SANDBOX_MARKERS)


def migrate() -> dict:
    summary = {
        "windows_failures_retained": [],
        "missions_repaired": [],
        "history_updated": False,
        "costs_reconciled": [],
    }
    engineer = Employee.query.filter_by(slug="engineer").first()
    if not engineer:
        raise RuntimeError("Engineer is required")

    active_history = (
        EmployeeModelHistory.query.filter_by(employee_id=engineer.id, ended_at=None)
        .order_by(EmployeeModelHistory.started_at.desc())
        .first()
    )
    if active_history:
        note = (
            "WSL2 transport selected because the native Windows Codex sandbox could "
            "not spawn child processes. Engineer remains accountable; Codex runs in "
            "Ubuntu through wsl.exe with the approved Windows repository mapped to /mnt."
        )
        if note not in (active_history.reason or ""):
            active_history.reason = ((active_history.reason + " · ") if active_history.reason else "") + note
        summary["history_updated"] = True

    affected_operations: set[int] = set()
    for run in AgentRun.query.order_by(AgentRun.id).all():
        if not _is_windows_sandbox_failure(run):
            continue
        summary["windows_failures_retained"].append(run.id)
        if run.operation_id:
            affected_operations.add(run.operation_id)
        task = db.session.get(Task, run.task_id) if run.task_id else None
        later_authoritative_success = None
        if task:
            later_authoritative_success = AgentRun.query.filter(
                AgentRun.operation_id == run.operation_id,
                AgentRun.task_id == task.id,
                AgentRun.provider_key_snapshot == "codex",
                AgentRun.status == "SUCCEEDED",
                AgentRun.structured_validation_status == "PASSED",
            ).order_by(AgentRun.id.desc()).first()
        if task and not later_authoritative_success:
            task.status = "ASSIGNED"
            task.result_summary = None
            task.completed_at = None
        if not run.resolution_note:
            run.resolution_note = (
                "Retained as failed Windows-native Codex transport evidence. Replaced by "
                "a WSL2 Codex recovery; no API cost was incurred by this failed local run."
            )
        run.resolution_status = run.resolution_status or "REPLACED_BY_WSL2_TRANSPORT"

    db.session.commit()

    for operation in Operation.query.filter(Operation.status.in_(OPEN)).order_by(Operation.id).all():
        cost = actual_cost(operation)
        operation.actual_cost_twd = cost
        summary["costs_reconciled"].append((operation.id, str(cost)))
        memory = dict(operation.memory_json or {})
        engineering = dict(memory.get("engineering") or {})
        should_repair = operation.id in affected_operations or bool(
            engineering.get("resume_task_id") and operation.status in {"PAUSED", "WAITING_FOR_FOUNDER"}
        )
        # A later authoritative WSL2 Codex success supersedes an earlier native
        # Windows sandbox failure. Re-running this idempotent migration during a
        # newer installer must never rewind a completed Engineer step back to a
        # repair cursor or reopen paid downstream work.
        authoritative_codex_success = AgentRun.query.filter(
            AgentRun.operation_id == operation.id,
            AgentRun.provider_key_snapshot == "codex",
            AgentRun.status == "SUCCEEDED",
            AgentRun.structured_validation_status == "PASSED",
        ).order_by(AgentRun.id.desc()).first()
        if authoritative_codex_success:
            should_repair = False
        if not should_repair:
            continue
        engineering.update({
            "connector": "codex-cli-wsl2",
            "transport": "wsl",
            "wsl_distro": "Ubuntu",
            "wsl_codex_path": "/home/eason/.local/bin/codex",
            "stop_after_current_step": True,
            "formal_mock_fallback": False,
            "max_concurrent_jobs": 1,
        })
        memory["engineering"] = engineering
        operation.memory_json = memory
        operation.status = "PAUSED"
        operation.waiting_reason = (
            "WSL2 Codex recovery is ready. Resume launches the earliest unfinished "
            "Engineer Task once through Ubuntu, then pauses before any Critic or paid Provider."
        )
        operation.founder_report_json = {
            "decision_kind": ENGINEERING_REPAIR_GATE,
            "headline": "Engineer → WSL2 Codex recovery ready.",
            "summary": operation.waiting_reason,
            "next_move": "Resume one bounded Engineer/WSL2 Codex step or stop this test Mission.",
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
        # prepare_engineering_repair preserves the memory envelope; assert WSL2 transport.
        memory = dict(operation.memory_json or {})
        engineering = dict(memory.get("engineering") or {})
        engineering.update({
            "connector": "codex-cli-wsl2",
            "transport": "wsl",
            "wsl_distro": "Ubuntu",
            "wsl_codex_path": "/home/eason/.local/bin/codex",
        })
        memory["engineering"] = engineering
        operation.memory_json = memory
        summary["missions_repaired"].append((operation.id, target.id))

    db.session.commit()
    return summary


def main() -> int:
    app = create_app()
    with app.app_context():
        summary = migrate()
        print("V0.10.11 WSL2 Codex migration complete. No Provider or Codex execution was made.")
        print(f"  Windows-native Codex failures retained: {summary['windows_failures_retained'] or 'none'}")
        print(f"  Engineer model history updated: {summary['history_updated']}")
        for operation_id, task_id in summary["missions_repaired"]:
            print(f"  Mission #{operation_id} WSL2 repair cursor -> Engineer Task #{task_id}; one-step stop enabled.")
        for operation_id, cost in summary["costs_reconciled"]:
            print(f"  Mission #{operation_id} execution spend reconciled: NT$ {cost}")
        db.session.remove()
        db.engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
