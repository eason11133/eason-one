"""Apply the one explicit Founder-authorized Project #21 Codex write scope."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from eason_one import create_app
from eason_one.extensions import db
from eason_one.models import AgentRun, Project, Work
from eason_one.services.codex_connector import freeze_write_scope


APPROVED_PATH = "trial_company_output/first_company_trial.html"


def sqlite_uri(path: Path) -> str:
    return "sqlite:///" + path.resolve().as_posix()


def apply_project21_scope() -> dict:
    project = db.session.get(Project, 21)
    work = db.session.get(Work, 64)
    if not project or project.name != "First Company Trial — Taiwan AI Opportunity Brief":
        raise ValueError("PROJECT_21_RELEASE_IDENTITY_MISMATCH")
    if not work or work.project_id != project.id:
        raise ValueError("PROJECT_21_ENGINEERING_WORK_MISMATCH")
    if AgentRun.query.filter_by(work_id=work.id, provider_key_snapshot="codex").count():
        raise ValueError("PROJECT_21_CODEX_SCOPE_CANNOT_CHANGE_AFTER_EXECUTION")
    control = dict(work.runtime_control_json or {})
    expected = freeze_write_scope(
        {"version": "CODEX_WRITE_SCOPE_V1", "paths": [APPROVED_PATH]},
        source="FOUNDER_RELEASE_MIGRATION_PROJECT_21",
        authority_ref="architecture-reliability-20260902:project:21:work:64",
    )
    current = control.get("codex_write_scope")
    if current and current != expected:
        raise ValueError("PROJECT_21_CODEX_WRITE_SCOPE_CONFLICT")
    control["codex_write_scope"] = expected
    boundary = dict(control.get("codex_execution_boundary") or {})
    if boundary:
        boundary["version"] = "CODEX_EXECUTION_BOUNDARY_V2"
        boundary["allowed_paths"] = [APPROVED_PATH]
        boundary["max_changed_files"] = 1
        body = {key: value for key, value in boundary.items() if key != "boundary_hash"}
        boundary["boundary_hash"] = hashlib.sha256(json.dumps(
            body, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")).hexdigest()
        control["codex_execution_boundary"] = boundary
    work.runtime_control_json = control
    db.session.commit()
    return {"project_id": project.id, "work_id": work.id, "paths": [APPROVED_PATH]}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("database")
    args = parser.parse_args()
    app = create_app({
        "AUTO_START_COMPANY_RUNTIME": False,
        "AUTO_START_OPERATION_RUNTIME": False,
        "SQLALCHEMY_DATABASE_URI": sqlite_uri(Path(args.database)),
    })
    with app.app_context():
        print(json.dumps(apply_project21_scope(), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
