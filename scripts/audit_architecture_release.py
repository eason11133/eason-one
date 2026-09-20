#!/usr/bin/env python3
"""Deterministic production-kernel acceptance from the live Project #21 state.

The source SQLite database is never opened for writes. Every external provider,
including Codex, is replaced at the transport boundary while Project/Work,
budget, artifacts, reviews, recovery and scheduler code remain production code.
"""
from __future__ import annotations

import argparse
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile

from eason_one import create_app
from eason_one.extensions import db
from eason_one.models import AgentRun, Artifact, CostEvent, Project, Work
from eason_one.providers import ProviderResult

CODEX_WORKSPACES: list[Path] = []


def sqlite_uri(path: Path) -> str:
    return "sqlite:///" + path.resolve().as_posix()


class DigitalTwinProvider:
    calls: list[str] = []

    def complete(self, model, system, user, context, cap, response_schema=None, tool_mode=None):
        name = (response_schema or {}).get("name", "")
        self.calls.append(name)
        if name == "task_review":
            criterion_ids = re.findall(r"^- (C\d+):", context, flags=re.MULTILINE)
            payload = {
                "decision": "ACCEPT",
                "summary": "Digital-twin independent review passed the exact frozen ArtifactVersion.",
                "issues": [], "required_changes": [],
                "criterion_results": [{
                    "criterion_id": value, "status": "PASSED",
                    "evidence": "Current immutable ArtifactVersion and bound lineage satisfy this criterion.",
                } for value in criterion_ids],
            }
        elif name == "task_execution":
            payload = {
                "result_summary": "Digital-twin Employee completed bounded Work from accepted upstream Company evidence.",
                "knowledge_proposals": [],
            }
        elif name == "project_outcome_review_v2":
            criterion_ids = list(dict.fromkeys(re.findall(r"\b(P[1-8])\b", context)))
            work_ids = list(dict.fromkeys(int(value) for value in re.findall(r"Work #(\d+)", context)))
            payload = {
                "criteria": [{
                    "criterion_id": value, "status": "SATISFIED",
                    "evidence": "Current accepted ArtifactVersions and traceable evidence satisfy the frozen criterion.",
                    "work_ids": work_ids[:16],
                } for value in criterion_ids],
                "summary": "All current Founder Project Contract criteria are satisfied by current accepted evidence.",
            }
        else:
            raise AssertionError(f"Unexpected digital-twin schema: {name!r}")
        text = json.dumps(payload, ensure_ascii=False)
        return ProviderResult(
            text, 100, 100, response_id=f"twin-response-{len(self.calls)}",
            request_id=f"twin-request-{len(self.calls)}",
        )


def fake_codex_runner(*, repo, prompt, read_only, schema):
    repo = Path(repo).resolve()
    CODEX_WORKSPACES.append(repo)
    if (repo / ".git").exists() or (repo / ".env").exists() or (repo / "instance").exists():
        raise AssertionError("Digital-twin Codex received protected live-repository state")
    allowed = re.findall(r"^- (trial_company_output/first_company_trial\.html)$", prompt, re.MULTILINE)
    if read_only or allowed != ["trial_company_output/first_company_trial.html"]:
        raise AssertionError("Digital-twin Codex did not receive the exact Founder-approved path")
    relative = allowed[0]
    target = Path(repo, *relative.split("/"))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "<!doctype html><meta charset='utf-8'><title>First Company Trial</title>"
        "<h1>Taiwan AI Opportunity Brief</h1><p>Digital-twin bounded artifact.</p>",
        encoding="utf-8",
    )
    payload = {
        "summary": "Created the one Founder-approved HTML artifact.",
        "changed_files": [relative],
        "tests": [{"command": "host validation", "status": "PASSED", "detail": "Exact file exists and is readable."}],
        "acceptance": [], "risks": [], "needs_founder": False, "founder_reason": None,
    }
    return {
        "returncode": 0, "stdout": json.dumps({"usage": {"input_tokens": 0, "output_tokens": 0}}) + "\n",
        "stderr": "", "final_output": json.dumps(payload), "elapsed_ms": 1.0,
        "command": ["fake-codex", "exec"],
    }


def app_for(database: Path, repo_mirror: Path):
    return create_app({
        "TESTING": True,
        "AUTO_START_COMPANY_RUNTIME": False,
        "AUTO_START_OPERATION_RUNTIME": False,
        "SQLALCHEMY_DATABASE_URI": sqlite_uri(database),
        "CODEX_RUNNER": fake_codex_runner,
        "EASON_ONE_CODEX_DEFAULT_REPO": str(repo_mirror),
    })


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", default="instance/eason_one.db")
    args = parser.parse_args()
    source = Path(args.database).resolve()
    if not source.is_file():
        raise SystemExit(f"Database not found: {source}")
    temp_root = Path(tempfile.mkdtemp(prefix="eason-one-architecture-twin-"))
    twin = temp_root / "project21.db"
    repo_mirror = temp_root / "repo"
    repo_mirror.mkdir(); (repo_mirror / ".git").mkdir()
    shutil.copy2(source, twin)
    os.environ["EASON_ONE_CODEX_ALLOWED_REPOS"] = str(repo_mirror)
    os.environ["EASON_ONE_CODEX_DEFAULT_REPO"] = str(repo_mirror)
    os.environ.update({key: "digital-twin-never-sent" for key in (
        "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "PERPLEXITY_API_KEY", "GEMINI_API_KEY",
    )})
    provider = DigitalTwinProvider()
    try:
        app = app_for(twin, repo_mirror)
        with app.app_context():
            project = db.session.get(Project, 21)
            if project is None:
                raise AssertionError("Project #21 is missing")
            research_execution_ids = [run.id for run in AgentRun.query.filter_by(work_id=61, purpose="TASK_EXECUTION").all()]
            if 198 not in research_execution_ids:
                raise AssertionError("Producing Research Run #198 is missing")
            # Redirect only the DB-copy Codex execution surface to an isolated
            # repository mirror. Authority/path content is unchanged.
            engineering = db.session.get(Work, 64)
            control = dict(engineering.runtime_control_json or {})
            connector = __import__(
                "eason_one.services.codex_connector", fromlist=["freeze_write_scope"]
            )
            control["codex_write_scope"] = connector.freeze_write_scope(
                {"version": "CODEX_WRITE_SCOPE_V1", "paths": [
                    "trial_company_output/first_company_trial.html"
                ]},
                source="FOUNDER_RELEASE_MIGRATION_PROJECT_21",
                authority_ref="architecture-reliability-20260902:project:21:work:64",
            )
            boundary = dict(control.get("codex_execution_boundary") or {})
            boundary["version"] = "CODEX_EXECUTION_BOUNDARY_V2"
            boundary["allowed_paths"] = ["trial_company_output/first_company_trial.html"]
            boundary["max_changed_files"] = 1
            boundary["repo_path"] = str(repo_mirror)
            body = {key: value for key, value in boundary.items() if key != "boundary_hash"}
            boundary["boundary_hash"] = hashlib.sha256(json.dumps(
                body, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")).hexdigest()
            control["codex_execution_boundary"] = boundary
            engineering.runtime_control_json = control
            db.session.commit()
            import eason_one.services.execution as execution
            execution.get_provider = lambda key: provider
            recovered = __import__(
                "eason_one.services.runtime_recovery", fromlist=["reconcile_known_platform_faults"]
            ).reconcile_known_platform_faults()
            if db.session.get(Work, 61).state != "VERIFYING":
                raise AssertionError("Research did not resume at exact-version review")

        # Recreate the Flask application after every kernel tick. This is the
        # restart matrix: durable SQLite truth, not process memory, drives every
        # subsequent transition.
        timeline = []
        for _ in range(3):
            app = app_for(twin, repo_mirror)
            with app.app_context():
                import eason_one.services.execution as execution
                execution.get_provider = lambda key: provider
                result = __import__(
                    "eason_one.services.company_kernel", fromlist=["advance_batch"]
                ).advance_batch(16)
                timeline.extend(row.get("kind") for row in result.get("steps", []))
                project = db.session.get(Project, 21)
                proof = __import__(
                    "eason_one.services.project_outcome", fromlist=["result_ready_proof"]
                ).result_ready_proof(project)
                if proof:
                    break
        else:
            with app.app_context():
                stuck_project = db.session.get(Project, 21)
                stuck_works = [(row.id, row.state) for row in Work.query.filter_by(project_id=21).order_by(Work.id)]
            raise AssertionError(
                "Project #21 did not reach Result Ready within 3 restarted productive batches; "
                f"project={stuck_project.status}; works={stuck_works}; timeline_tail={timeline[-32:]}"
            )

        with app.app_context():
            project = db.session.get(Project, 21)
            works = Work.query.filter_by(project_id=21).order_by(Work.id).all()
            if [(work.id, work.state) for work in works] != [
                (61, "ACCEPTED"), (62, "ACCEPTED"), (63, "ACCEPTED"),
                (64, "ACCEPTED"), (65, "ACCEPTED"),
            ]:
                raise AssertionError("Project #21 Work topology did not close deterministically")
            research_after = [run.id for run in AgentRun.query.filter_by(work_id=61, purpose="TASK_EXECUTION").all()]
            if research_after != research_execution_ids:
                raise AssertionError("Successful Research was replayed")
            result_artifact = Artifact.query.filter_by(project_id=21, artifact_type="PROJECT_RESULT").first()
            if result_artifact is None or not any(v.status == "ACCEPTED" for v in result_artifact.versions):
                raise AssertionError("Current immutable Project Result artifact is missing")
            spent = sum(Decimal(row.real_cost_delta or 0) for row in CostEvent.query.filter_by(project_id=21))
            if spent > Decimal(project.real_budget_limit):
                raise AssertionError("Project hard cap exceeded")
            if not (repo_mirror / "trial_company_output" / "first_company_trial.html").is_file():
                raise AssertionError("Fake Codex did not produce exact isolated output")
            if not CODEX_WORKSPACES or any(path == repo_mirror.resolve() for path in CODEX_WORKSPACES):
                raise AssertionError("Codex did not execute exclusively in a disposable isolated workspace")
            print("ARCHITECTURE_DIGITAL_TWIN_PASS")
            print(f"  SOURCE_DB: {source}")
            print(f"  RECOVERIES: {recovered}")
            print(f"  RESTARTED_KERNEL_TICKS: {len(timeline)}")
            print(f"  PROJECT_21_STATE: {project.status}")
            print("  WORK_STATES: " + ", ".join(f"#{w.id}={w.state}" for w in works))
            print("  RESEARCH_RUN_198_REPLAYED: False")
            print(f"  PROJECT_COST_TWD: {spent}")
            print(f"  PROJECT_CAP_TWD: {project.real_budget_limit}")
            print(f"  FAKE_PROVIDER_CALLS: {len(provider.calls)}")
            print("  REAL_PROVIDER_CALLS: 0")
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


if __name__ == "__main__":
    main()
