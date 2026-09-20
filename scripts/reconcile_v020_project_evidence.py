from __future__ import annotations

import argparse
from pathlib import Path

from eason_one import create_app
from eason_one.extensions import db
from eason_one.models import Escalation, Operation, Project, now


def sqlite_uri(path: Path) -> str:
    return "sqlite:///" + path.resolve().as_posix()


def _is_delegated(operation: Operation | None) -> bool:
    if operation is None:
        return False
    return (operation.memory_json or {}).get("authority_source") == "PROJECT_DELEGATED_CEO"


def _is_stale_recovery_gate(row: Escalation) -> bool:
    reason = " ".join(str(row.reason or "").casefold().split())
    if row.escalation_type == "BUDGET_AUTHORIZATION":
        return True
    if row.escalation_type != "PROJECT_AUTHORITY":
        return False
    if _is_delegated(row.operation):
        return True
    return reason in {
        "founder authority is required before the company can continue.",
        "founder authority is required before company execution may continue.",
    }


def reconcile_project(project: Project) -> dict:
    outcome = __import__(
        "eason_one.services.project_outcome",
        fromlist=["refresh_deterministic_http_evidence", "evaluate"],
    )

    # Refresh/reuse zero-Token deterministic proof first. This must not create
    # authority or paid AgentRuns.
    refresh = outcome.refresh_deterministic_http_evidence(project)
    evaluation = outcome.evaluate(project)

    resolved: list[int] = []
    for row in Escalation.query.filter_by(project_id=project.id, state="OPEN").order_by(Escalation.id).all():
        if not _is_stale_recovery_gate(row):
            continue
        row.state = "RESOLVED"
        row.resolved_at = now()
        row.resolution = "SUPERSEDED_BY_CURRENT_PROJECT_EVIDENCE_REEVALUATION"
        resolved.append(row.id)

    if resolved and project.status == "BLOCKED":
        project.status = "ACTIVE"
        project.current_state_summary = (
            "Recovered deterministic evidence is being re-evaluated before any additional paid Work."
        )
        project.next_milestone = (
            "Evaluate the current accepted Artifact + deterministic Project proof before planning continuation."
        )

    db.session.commit()
    return {
        "project_id": project.id,
        "refresh_status": refresh.get("status"),
        "new_proofs": refresh.get("new_proofs") or 0,
        "resolved_stale_recovery_gates": resolved,
        "project_status": project.status,
        "overall_status_before_semantic_review": evaluation.get("overall_status"),
        "missing_criteria": [
            row.get("criterion")
            for row in (evaluation.get("criteria") or [])
            if row.get("status") != "SATISFIED"
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Retire stale recovery/budget gates after deterministic evidence recovery so "
            "the v0.20 kernel can review current Project evidence before buying more Work."
        )
    )
    parser.add_argument("db", nargs="?", default="instance/eason_one.db")
    parser.add_argument("--project", type=int, required=True)
    args = parser.parse_args()
    db_path = Path(args.db)
    if not db_path.exists():
        raise SystemExit(f"Database does not exist: {db_path}")

    app = create_app({
        "TESTING": False,
        "AUTO_START_COMPANY_RUNTIME": False,
        "AUTO_START_OPERATION_RUNTIME": False,
        "SQLALCHEMY_DATABASE_URI": sqlite_uri(db_path),
    })
    with app.app_context():
        project = db.session.get(Project, args.project)
        if not project:
            raise SystemExit(f"Project #{args.project} does not exist")
        result = reconcile_project(project)
        print(result)
        db.session.remove()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
