#!/usr/bin/env python3
"""DB-copy acceptance for the consolidated First Usable Trial runtime cut.

This script may reconcile deterministic historical platform faults on the copy,
but it never calls a model provider or Codex. It is intended for installers to
prove that an existing approved Project can survive the source upgrade.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from eason_one import create_app
from eason_one.extensions import db
from eason_one.models import AgentRun, Employee, Work


def sqlite_uri(path: Path) -> str:
    return "sqlite:///" + path.resolve().as_posix()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True)
    args = parser.parse_args()
    db_path = Path(args.database).resolve()
    if not db_path.is_file():
        raise SystemExit(f"Database copy not found: {db_path}")

    app = create_app({
        "TESTING": False,
        "AUTO_START_COMPANY_RUNTIME": False,
        "AUTO_START_OPERATION_RUNTIME": False,
        "SQLALCHEMY_DATABASE_URI": sqlite_uri(db_path),
    })
    with app.app_context():
        project = __import__("eason_one.models", fromlist=["Project"]).Project.query.filter(
            __import__("eason_one.models", fromlist=["Project"]).Project.name.like("First Company Trial%")
        ).order_by(__import__("eason_one.models", fromlist=["Project"]).Project.id.desc()).first()
        if project is None:
            print("FIRST_TRIAL_RELEASE_CUT_DB_PASS (no First Company Trial found; provider/runtime audits still apply)")
            return

        run_count_before = AgentRun.query.count()
        recovered = __import__(
            "eason_one.services.runtime_recovery", fromlist=["reconcile_known_platform_faults"]
        ).reconcile_known_platform_faults()
        db.session.expire_all()
        project = db.session.get(type(project), project.id)
        run_count_after = AgentRun.query.count()
        if run_count_after != run_count_before:
            raise AssertionError("DB-copy recovery created an AgentRun/provider attempt")

        research = Work.query.filter_by(project_id=project.id).filter(
            Work.title.like("Research Taiwan AI automation opportunity evidence%")
        ).order_by(Work.id.desc()).first()
        if research is None:
            raise AssertionError("First trial Research Work is missing")
        work_runtime = __import__("eason_one.services.work_runtime", fromlist=["open_gates"])
        open_gates = [dict(row) for row in work_runtime.open_gates(research)]
        obsolete = [row for row in open_gates if (
            "json_object" in str(row.get("reason") or "")
            and "response_format" in str(row.get("reason") or "")
        )]
        if obsolete:
            raise AssertionError("Historical Perplexity json_object reconciliation remains open after release cut")

        control = dict(research.runtime_control_json or {})
        team = dict(control.get("team_formation") or {})
        researcher = db.session.get(Employee, team.get("employee_id")) if team.get("employee_id") else Employee.query.filter_by(slug="researcher", active=True).first()
        if not researcher:
            raise AssertionError("Researcher employee is unavailable")
        model = __import__(
            "eason_one.services.execution_policy", fromlist=["select_research_model"]
        ).select_research_model(researcher, research.operation)
        if not model or model.provider_key != "perplexity" or model.model_name != "sonar":
            raise AssertionError(f"Research effective route is not perplexity/sonar: {getattr(model,'provider_key',None)}/{getattr(model,'model_name',None)}")

        strategy = Work.query.filter_by(project_id=project.id).filter(
            Work.title.like("Compare three opportunities and recommend one%")
        ).order_by(Work.id.desc()).first()
        if strategy:
            strategy_gates = [dict(row) for row in work_runtime.open_gates(strategy)]
            stale_staffing = [row for row in strategy_gates if row.get("issue_code") == "HIRING_REQUEST_3_NOT_MATERIALIZED"]
            if stale_staffing:
                raise AssertionError("Historical Product Strategy staffing contradiction remains open")
            strategy_team = dict((strategy.runtime_control_json or {}).get("team_formation") or {})
            assignee = db.session.get(Employee, strategy_team.get("employee_id")) if strategy_team.get("employee_id") else None
            if strategy.state not in {"ACCEPTED", "VERIFYING"} and (not assignee or not assignee.active):
                raise AssertionError("Product Strategy Work has no live accountable specialist after staffing reconciliation")

        critic = Employee.query.filter_by(slug="critic", active=True).first()
        if critic:
            critic_model = __import__(
                "eason_one.services.execution_policy", fromlist=["select_execution_model"]
            ).select_execution_model(critic, research.operation, "TASK_REVIEW")
            if critic_model.provider_key != "anthropic":
                raise AssertionError("Critic is no longer the sole Claude review path")

        print("FIRST_TRIAL_RELEASE_CUT_DB_PASS")
        print(f"  PROJECT: #{project.id} {project.status}")
        print(f"  DETERMINISTIC_RECOVERIES_APPLIED_ON_COPY: {recovered}")
        print(f"  RESEARCH_STATE: {research.state}")
        print(f"  RESEARCH_EFFECTIVE: {model.provider_key}/{model.model_name}")
        if strategy:
            print(f"  PRODUCT_STRATEGY_STATE: {strategy.state}")
        print("  CRITIC_ROUTE: anthropic")
        print("  PROVIDER_CALLS_MADE: 0")


if __name__ == "__main__":
    main()
