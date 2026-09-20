"""Verify/migrate Company Core vNext Batch A against an explicit SQLite DB.

This script never calls a model provider.  ``--dry-run`` works on a temporary
copy so the real runtime database is untouched.  ``--apply`` opens the explicit
database through the normal app startup path, which performs the same additive
migration/backfill used by Eason One at launch.
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import tempfile
from pathlib import Path


def _table_count(path: Path, table: str):
    con = sqlite3.connect(path)
    try:
        found = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        return con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] if found else None
    finally:
        con.close()


def _before(path: Path):
    return {
        name: _table_count(path, name)
        for name in ("project", "task", "agent_run", "company_event", "artifact", "decision")
    }


def _run(path: Path):
    from eason_one import create_app
    from eason_one.models import (
        AgentRun, Artifact, ArtifactVersion, CompanyEvent, Decision, Project, Task, Work,
    )

    uri = f"sqlite:///{path.resolve().as_posix()}"
    app = create_app({
        "TESTING": True,
        "ALLOW_MOCK_PROVIDER": True,
        "SQLALCHEMY_DATABASE_URI": uri,
    })
    with app.app_context():
        from eason_one.extensions import db
        after = {
            "project": Project.query.count(),
            "task": Task.query.count(),
            "agent_run": AgentRun.query.count(),
            "work": Work.query.count(),
            "company_event": CompanyEvent.query.count(),
            "artifact": Artifact.query.count(),
            "artifact_version": ArtifactVersion.query.count(),
            "decision": Decision.query.count(),
            "task_without_work": Task.query.filter(
                Task.project_id.isnot(None), Task.work_id.is_(None)
            ).count(),
            "task_run_without_work": AgentRun.query.filter(
                AgentRun.task_id.isnot(None), AgentRun.work_id.is_(None)
            ).count(),
        }
        # Force ORM compilation against the new same-name tables.  This catches
        # the exact v0.15 collision where app startup succeeded but Artifact or
        # Decision queries later failed on missing vNext columns.
        Artifact.query.limit(1).all()
        Decision.query.limit(1).all()
        db.session.remove()
    return after


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True, type=Path)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    source = args.database.resolve()
    if not source.exists():
        raise SystemExit(f"Database does not exist: {source}")

    before = _before(source)
    temp_dir = None
    target = source
    if args.dry_run:
        temp_dir = Path(tempfile.mkdtemp(prefix="eason-one-batch-a-"))
        target = temp_dir / source.name
        shutil.copy2(source, target)

    try:
        first = _run(target)
        second = _run(target)
        # Migration is additive: core historical row counts may never shrink.
        for key in ("project", "task", "agent_run"):
            if before.get(key) is not None and first[key] != before[key]:
                raise RuntimeError(
                    f"Historical {key} count changed: before={before[key]} after={first[key]}"
                )
        if first != second:
            raise RuntimeError(f"Migration is not idempotent: first={first} second={second}")
        if first["task_without_work"] != 0:
            raise RuntimeError(
                f"{first['task_without_work']} Project Tasks remain outside Work truth"
            )
        if first["task_run_without_work"] != 0:
            raise RuntimeError(
                f"{first['task_run_without_work']} task-linked Executions remain outside Work truth"
            )
        label = "DRY-RUN PASS" if args.dry_run else "LIVE MIGRATION PASS"
        print(label)
        print("before:", before)
        print("after:", first)
        return 0
    finally:
        if temp_dir is not None:
            shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
