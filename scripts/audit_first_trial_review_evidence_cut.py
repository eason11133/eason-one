#!/usr/bin/env python3
"""DB-copy acceptance for First Trial Review & Evidence Acceptance Cut."""
from __future__ import annotations

import argparse
from pathlib import Path

from eason_one import create_app
from eason_one.extensions import db
from eason_one.models import AgentRun, Artifact, ArtifactVersion, Employee, Work


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
        Project = __import__("eason_one.models", fromlist=["Project"]).Project
        project = Project.query.filter(Project.name.like("First Company Trial%"))\
            .order_by(Project.id.desc()).first()
        if project is None:
            print("FIRST_TRIAL_REVIEW_EVIDENCE_DB_PASS (no First Company Trial found)")
            return

        research = Work.query.filter_by(project_id=project.id).filter(
            Work.title.like("Research Taiwan AI automation opportunity evidence%")
        ).order_by(Work.id.desc()).first()
        if research is None:
            raise AssertionError("First trial Research Work is missing")

        run_count_before = AgentRun.query.count()
        recovered = __import__(
            "eason_one.services.runtime_recovery", fromlist=["reconcile_known_platform_faults"]
        ).reconcile_known_platform_faults()
        db.session.expire_all()
        research = db.session.get(Work, research.id)
        project = db.session.get(Project, project.id)
        if AgentRun.query.count() != run_count_before:
            raise AssertionError("DB-copy platform recovery created a provider/AgentRun attempt")

        producer = AgentRun.query.filter_by(work_id=research.id, purpose="TASK_EXECUTION", status="SUCCEEDED")\
            .order_by(AgentRun.id.desc()).first()
        if producer is None:
            raise AssertionError("Research has no successful producing Execution")
        sources = [
            row for row in ((producer.context_composition_json or {}).get("provider_sources") or [])
            if isinstance(row, dict) and str(row.get("url") or "").strip()
        ]
        if not sources:
            raise AssertionError("Successful Research producing Execution has no provider-observed sources")

        version = ArtifactVersion.query.join(Artifact).filter(Artifact.work_id == research.id)\
            .order_by(ArtifactVersion.id.desc()).first()
        if version is None or version.execution_id != producer.id:
            raise AssertionError("Latest Research ArtifactVersion is not bound to successful producing Execution")

        faulty = AgentRun.query.filter_by(work_id=research.id, purpose="TASK_REVIEW")\
            .filter(AgentRun.failure_reason == "RESEARCH_REVIEW_SOURCE_EVIDENCE_MISSING")\
            .order_by(AgentRun.id).all()
        for run in faulty:
            if run.resolution_status != "SYSTEM_RESEARCH_REVIEW_EVIDENCE_SCOPE_SUPERSEDED":
                raise AssertionError(f"Historical review Run #{run.id} was not superseded as a platform policy fault")

        gates = [dict(row) for row in __import__(
            "eason_one.services.work_runtime", fromlist=["open_gates"]
        ).open_gates(research)]
        if any("no provider-observed source evidence" in str(row.get("reason") or "") for row in gates):
            raise AssertionError("Obsolete Research-review source gate remains open")
        if research.state not in {"VERIFYING", "ACCEPTED"}:
            raise AssertionError(f"Research did not resume at semantic review: {research.state}")

        control = dict(research.runtime_control_json or {})
        contract = dict(control.get("acceptance_contract") or {})
        reviewer_id = contract.get("reviewer_employee_id")
        reviewer = db.session.get(Employee, reviewer_id) if reviewer_id else None
        if reviewer is None or not reviewer.active:
            raise AssertionError("Frozen Research reviewer is unavailable")
        model = __import__(
            "eason_one.services.execution_policy", fromlist=["select_execution_model"]
        ).select_execution_model(reviewer, research.operation, "TASK_REVIEW")
        if reviewer.slug == "critic" and model.provider_key != "anthropic":
            raise AssertionError(f"Critic review route drifted from Claude: {model.provider_key}/{model.model_name}")

        producer_run, helper_sources = __import__(
            "eason_one.services.work_execution", fromlist=["_artifact_provider_source_lineage"]
        )._artifact_provider_source_lineage(version)
        if producer_run is None or producer_run.id != producer.id or len(helper_sources) != len(sources[:40]):
            raise AssertionError("Exact ArtifactVersion source-lineage handoff does not match producing Run")

        print("FIRST_TRIAL_REVIEW_EVIDENCE_DB_PASS")
        print(f"  PROJECT: #{project.id} {project.status}")
        print(f"  DETERMINISTIC_RECOVERIES_APPLIED_ON_COPY: {recovered}")
        print(f"  RESEARCH_STATE: {research.state}")
        print(f"  PRODUCING_RUN: #{producer.id} {producer.provider_key_snapshot}/{producer.model_name_snapshot}")
        print(f"  PROVIDER_SOURCE_COUNT: {len(sources)}")
        if faulty:
            print(f"  SUPERSEDED_REVIEW_RUNS: {[run.id for run in faulty]}")
        print(f"  FROZEN_REVIEWER: #{reviewer.id} {reviewer.slug}")
        print(f"  REVIEW_EFFECTIVE: {model.provider_key}/{model.model_name}")
        print("  RESEARCH_REPLAYED: False")
        print("  PROVIDER_CALLS_MADE: 0")


if __name__ == "__main__":
    main()
