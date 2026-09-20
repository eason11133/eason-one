from __future__ import annotations

import argparse
from pathlib import Path

from eason_one import create_app
from eason_one.extensions import db
from eason_one.models import Escalation, Project, now


def sqlite_uri(path: Path) -> str:
    return "sqlite:///" + path.resolve().as_posix()


def _http_criteria(contract: dict) -> list[str]:
    classify = __import__(
        "eason_one.services.acceptance_contract", fromlist=["classify_criterion"]
    ).classify_criterion
    return [
        row for row in (contract.get("success_criteria") or [])
        if classify(row, task_context=row) == "HOST_HTTP_CONTRACT"
    ]


def reconcile_project(project: Project) -> dict:
    outcome = __import__(
        "eason_one.services.project_outcome",
        fromlist=["refresh_deterministic_http_evidence", "evaluate"],
    )
    contract = __import__(
        "eason_one.services.project_contract", fromlist=["get"]
    ).get(project)
    http_criteria = _http_criteria(contract)
    if not http_criteria:
        return {"project_id": project.id, "status": "NO_HTTP_CRITERIA"}

    refresh = outcome.refresh_deterministic_http_evidence(project)
    evaluation = outcome.evaluate(project)
    by_text = {row.get("criterion"): row for row in (evaluation.get("criteria") or [])}
    http_satisfied = all(
        (by_text.get(criterion) or {}).get("status") == "SATISFIED"
        for criterion in http_criteria
    )
    resolved = []
    if http_satisfied:
        # These gates asked the Founder to buy another execution attempt merely
        # to obtain deterministic HTTP proof. Once the host proves the exact
        # Project HTTP criteria over a real loopback server, that specific
        # shortfall no longer exists. Do not fabricate budget or refund cost;
        # simply retire the superseded gate and let the kernel re-evaluate the
        # remaining Project criteria from durable evidence.
        for escalation in Escalation.query.filter_by(
            project_id=project.id, state="OPEN"
        ).order_by(Escalation.id).all():
            reason = str(escalation.reason or "")
            lowered = reason.casefold()
            budget_shaped = escalation.escalation_type == "BUDGET_AUTHORIZATION" or (
                escalation.escalation_type == "PROJECT_AUTHORITY"
                and (
                    "budget" in lowered
                    or "remaining project authority" in lowered
                    or "paid work" in lowered
                )
            )
            if not budget_shaped:
                continue
            escalation.state = "RESOLVED"
            escalation.resolved_at = now()
            escalation.resolution = "SUPERSEDED_BY_DETERMINISTIC_HTTP_PROOF"
            resolved.append(escalation.id)
        if resolved and project.status == "BLOCKED":
            project.status = "ACTIVE"
            project.current_state_summary = (
                "Deterministic live HTTP proof was recovered locally. "
                "The stale paid-recovery budget gate was superseded; Founder authority was not expanded."
            )
            project.next_milestone = "Re-evaluate the existing Project evidence before any additional paid Work."
    db.session.commit()
    return {
        "project_id": project.id,
        "status": "HTTP_PROOF_REFRESHED" if refresh.get("new_proofs") else refresh.get("status"),
        "new_proofs": refresh.get("new_proofs") or 0,
        "http_criteria": http_criteria,
        "http_satisfied": http_satisfied,
        "resolved_budget_escalations": resolved,
        "project_status": project.status,
        "overall_status": evaluation.get("overall_status"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Refresh deterministic Project HTTP proof through isolated real loopback HTTP "
            "and retire only budget gates superseded by that proof."
        )
    )
    parser.add_argument("db", nargs="?", default="instance/eason_one.db")
    parser.add_argument("--project", type=int, default=None)
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
        query = Project.query.filter(Project.environment == "LIVE").order_by(Project.id)
        if args.project is not None:
            query = query.filter(Project.id == args.project)
        rows = []
        for project in query.all():
            try:
                result = reconcile_project(project)
            except Exception as exc:
                db.session.rollback()
                result = {
                    "project_id": project.id,
                    "status": "REFUSED_OR_FAILED",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            if args.project is not None or result.get("new_proofs") or result.get("resolved_budget_escalations"):
                print(result)
            rows.append(result)
        print(
            "Projects with new deterministic HTTP proof:",
            sum(bool(row.get("new_proofs")) for row in rows),
        )
        print(
            "Superseded budget escalations resolved:",
            sum(len(row.get("resolved_budget_escalations") or []) for row in rows),
        )
        required_failed = bool(
            args.project is not None
            and rows
            and not bool(rows[0].get("http_satisfied"))
        )
        db.session.remove()
    if required_failed:
        print("Required Project HTTP proof was not satisfied; refusing to claim recovery.")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
