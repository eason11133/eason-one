from __future__ import annotations

import argparse
from pathlib import Path

from eason_one import create_app
from eason_one.extensions import db
from eason_one.models import Project


def sqlite_uri(path: Path) -> str:
    return "sqlite:///" + path.resolve().as_posix()


def reconcile_current_app(project_id: int | None = None) -> list[dict]:
    from eason_one.services.project_contract import reconcile_legacy_founder_project_cap

    query = Project.query.order_by(Project.id)
    if project_id is not None:
        query = query.filter(Project.id == project_id)
    results: list[dict] = []
    for project in query.all():
        try:
            result = reconcile_legacy_founder_project_cap(
                project, resolve_stale_budget_gate=True
            )
        except ValueError as exc:
            result = {
                "status": "RECONCILIATION_REFUSED",
                "project_id": project.id,
                "reason": str(exc),
            }
        results.append(result)
    db.session.commit()
    return results


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Reconcile only provable pre-v0.20 explicit Founder Project budget "
            "authority. No model/CEO-estimated budget is accepted as evidence."
        )
    )
    parser.add_argument("db", nargs="?", default="instance/eason_one.db")
    parser.add_argument("--project", type=int, default=None)
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
        results = reconcile_current_app(args.project)
        print(f"Database: {db_path.resolve()}")
        changed = [row for row in results if row.get("status") in {
            "PRE_FREEZE_CAP_RESTORED", "APPEND_ONLY_AUTHORITY_RESTORED"
        }]
        for row in results:
            if args.project is not None or row in changed:
                print(row)
        print(f"Provable historical Founder budget restorations: {len(changed)}")
        db.session.remove()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
