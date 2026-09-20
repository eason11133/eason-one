from __future__ import annotations

import argparse
from pathlib import Path

from eason_one import create_app
from eason_one.extensions import db
from eason_one.models import Operation
from eason_one.services.core_v018 import RUNTIME_SEMANTICS, LEGACY_WORK_RUNTIME_SEMANTICS


def sqlite_uri(path: Path) -> str:
    return "sqlite:///" + path.resolve().as_posix()


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply Eason One v0.18 schema/core cutover without starting runtimes.")
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
        rows = Operation.query.filter(Operation.approved_at.isnot(None)).order_by(Operation.id.desc()).all()
        v018 = [row for row in rows if (row.memory_json or {}).get("runtime_semantics") == RUNTIME_SEMANTICS]
        retired = [row for row in rows if (row.memory_json or {}).get("runtime_semantics") == LEGACY_WORK_RUNTIME_SEMANTICS]
        blocked = [row for row in retired if (row.memory_json or {}).get("v018_migration_blocked")]
        print(f"Database: {db_path.resolve()}")
        print(f"Approved v0.18 Operations: {len(v018)}")
        if v018:
            newest = v018[0]
            print(f"Current v0.18 Operation: #{newest.id} / Project #{newest.project_id}")
        print(f"Dormant historical WORK_VNEXT Operations: {len(retired)}")
        if blocked:
            newest = blocked[0]
            reason = (newest.memory_json or {}).get("v018_migration_blocked") or {}
            print(f"Migration blocked safely for Operation #{newest.id}: {reason.get('reason')}")
        db.session.remove()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
