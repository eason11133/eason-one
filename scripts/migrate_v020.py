from __future__ import annotations

import argparse
from pathlib import Path

from eason_one import create_app
from eason_one.extensions import db
from eason_one.models import CompanyEvent, Operation, Project, Work
from eason_one.services.company_events import emit
from eason_one.services.core_v018 import RUNTIME_SEMANTICS


def sqlite_uri(path: Path) -> str:
    return "sqlite:///" + path.resolve().as_posix()


def _legacy_runtime_failed_project(project: Project) -> CompanyEvent | None:
    events = CompanyEvent.query.filter_by(
        project_id=project.id, event_type="PROJECT_FAILED"
    ).order_by(CompanyEvent.id.desc()).all()
    for event in events:
        operation_id = (event.payload_json or {}).get("operation_id")
        operation = db.session.get(Operation, operation_id) if operation_id else None
        if operation and (operation.memory_json or {}).get("runtime_semantics") == RUNTIME_SEMANTICS:
            return event
    return None


def migrate_current_app() -> dict:
    """Apply the v0.20 cutover inside an existing Flask app context.

    Kept separate from CLI wiring so migration behavior can be exercised
    against a synthetic historical database without starting either runtime.
    """
    # Historical conversions are explicit here and nowhere in normal boot.
    from eason_one.services.work_runtime import backfill_legacy_work_spine
    from eason_one.services.legacy_v015 import import_legacy_v015_truth
    from eason_one.services.company_runtime import migrate_latest_v017_candidate
    backfill_legacy_work_spine()
    import_legacy_v015_truth()
    migrate_latest_v017_candidate()

    from eason_one.services.project_contract import (
        build as build_legacy_contract, freeze, get as get_contract,
        has_frozen_contract, reconcile_legacy_founder_project_cap,
    )
    from eason_one.services.project_outcome import evaluate

    v020_operations = []
    for operation in Operation.query.filter(Operation.approved_at.isnot(None)).order_by(Operation.id).all():
        memory = dict(operation.memory_json or {})
        if memory.get("runtime_semantics") != RUNTIME_SEMANTICS:
            continue
        memory["core_rebuild_version"] = "0.20.0"
        memory["core_cutover_version"] = "0.20.0"
        operation.memory_json = memory
        v020_operations.append(operation)

    frozen_projects = 0
    missing_contracts = []
    reopened_review = []
    reopened_legacy_failure = []
    restored_founder_budget = []
    project_ids = sorted({op.project_id for op in v020_operations if op.project_id})
    for project_id in project_ids:
        project = db.session.get(Project, project_id)
        if not project:
            continue
        try:
            budget_reconciliation = reconcile_legacy_founder_project_cap(
                project, resolve_stale_budget_gate=True
            )
            if budget_reconciliation.get("status") in {
                "PRE_FREEZE_CAP_RESTORED", "APPEND_ONLY_AUTHORITY_RESTORED"
            }:
                restored_founder_budget.append(budget_reconciliation)
            # Explicit migration is the one allowed conversion boundary from
            # historical Founder evidence / compatibility columns into the new
            # immutable Contract. Normal vNext readers deliberately fail closed
            # when a WORK_CORE_V018 Project lacks the ledger, so calling get()
            # before freeze would deadlock the migration itself.
            legacy_or_current = (
                get_contract(project)
                if has_frozen_contract(project)
                else build_legacy_contract(project)
            )
            freeze(
                project,
                success_criteria=legacy_or_current.get("success_criteria") or [],
                constraints=legacy_or_current.get("constraints") or [],
                origin_employee_id=project.owner_employee_id,
            )
            frozen_projects += 1
        except ValueError as exc:
            missing_contracts.append((project.id, str(exc)))
            project.status = "BLOCKED"
            project.current_state_summary = (
                "v0.20 migration could not prove the immutable Founder Project Contract: " + str(exc)
            )[:1200]
            continue

        if project.status == "REVIEW":
            result = evaluate(project)
            if result.get("overall_status") != "SATISFIED":
                project.status = "ACTIVE"
                project.current_state_summary = (
                    "v0.20 reopened this Project because historical Mission closure did not prove the full Founder Project Contract."
                )
                project.next_milestone = "Re-evaluate accepted evidence, then continue bounded Work if Project criteria remain unsatisfied."
                emit(
                    "V020_LEGACY_CLOSURE_REOPENED", actor_type="RUNTIME",
                    project_id=project.id, correlation_id=f"project:{project.id}",
                    payload={"previous_status": "REVIEW", "project_contract_hash": result.get("contract_hash")},
                )
                reopened_review.append(project.id)

        if project.status == "FAILED" and _legacy_runtime_failed_project(project):
            project.status = "ACTIVE"
            project.current_state_summary = (
                "v0.20 recovered Project authority: a bounded Mission failure remains evidence but no longer implies Project failure."
            )
            project.next_milestone = "CEO chooses the next bounded approach inside the existing Founder Project Contract."
            emit(
                "V020_LEGACY_PROJECT_FAILURE_REOPENED", actor_type="RUNTIME",
                project_id=project.id, correlation_id=f"project:{project.id}",
                payload={"previous_status": "FAILED", "reason": "legacy Mission failure propagation"},
            )
            reopened_legacy_failure.append(project.id)

    db.session.commit()
    return {
        "v020_operations": len(v020_operations),
        "frozen_projects": frozen_projects,
        "reopened_review": reopened_review,
        "reopened_legacy_failure": reopened_legacy_failure,
        "restored_founder_budget": restored_founder_budget,
        "missing_contracts": missing_contracts,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Apply Eason One v0.20 Project Contract / Company Kernel cutover once, with runtimes disabled."
    )
    parser.add_argument("db", nargs="?", default="instance/eason_one.db")
    args = parser.parse_args()
    db_path = Path(args.db)
    if not db_path.exists():
        raise SystemExit(f"Database does not exist: {db_path}")

    app = create_app({
        "TESTING": True,
        "AUTO_START_COMPANY_RUNTIME": False,
        "AUTO_START_OPERATION_RUNTIME": False,
        "SQLALCHEMY_DATABASE_URI": sqlite_uri(db_path),
    })
    with app.app_context():
        summary = migrate_current_app()
        print(f"Database: {db_path.resolve()}")
        print(f"v0.20 governed Operations: {summary['v020_operations']}")
        print(f"Frozen Founder Project Contracts: {summary['frozen_projects']}")
        print(f"Historical REVIEW Projects reopened for Project-level proof: {summary['reopened_review'] or 'none'}")
        print(f"Legacy Mission-propagated FAILED Projects reopened: {summary['reopened_legacy_failure'] or 'none'}")
        print(f"Historical explicit Founder Project budgets restored: {summary['restored_founder_budget'] or 'none'}")
        if summary["missing_contracts"]:
            print("Projects blocked for explicit contract reconciliation:")
            for project_id, reason in summary["missing_contracts"]:
                print(f"  Project #{project_id}: {reason}")
        db.session.remove()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
