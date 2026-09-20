import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from eason_one.extensions import db
from eason_one.models import AgentRun, Employee, KnowledgeItem, Operation, Project, Proposal, Task, now
from eason_one.providers import ProviderResult
from eason_one.schemas import CEO_EXECUTION_SCHEMA
from eason_one.services.ceo import founder_request
from eason_one.services.operations import approve, ensure_budget, project_remaining_authority, recover_project_budget_authority
from eason_one.services.project_company import home_snapshot, project_snapshot, results_snapshot



def _assert_openai_strict_object_requirements(node, path="$"):
    if isinstance(node, dict):
        if node.get("type") == "object":
            properties = node.get("properties", {})
            required = node.get("required")
            assert isinstance(required, list), f"{path}: object schema must declare required"
            assert set(required) == set(properties), (
                f"{path}: strict structured output requires every property to be required; "
                f"properties={sorted(properties)} required={sorted(required)}"
            )
            assert node.get("additionalProperties") is False, (
                f"{path}: strict structured output requires additionalProperties=false"
            )
        for key, value in node.items():
            _assert_openai_strict_object_requirements(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            _assert_openai_strict_object_requirements(value, f"{path}[{index}]")


def test_v013_ceo_execution_schema_is_openai_strict_compatible():
    _assert_openai_strict_object_requirements(CEO_EXECUTION_SCHEMA["schema"])
    project_object = CEO_EXECUTION_SCHEMA["schema"]["properties"]["project"]["anyOf"][0]
    assert set(project_object["required"]) == set(project_object["properties"])


def _provider(payload):
    class Provider:
        def complete(self, *args, **kwargs):
            return ProviderResult(
                json.dumps(payload), 240, 180,
                request_id="req-v013", response_id="resp-v013",
            )
    return Provider()


def _new_project_payload(researcher):
    return {
        "mode": "OPERATION_PLAN",
        "executive_response": "I will start the Project with one bounded evidence step, then keep work and results attached to the durable Project.",
        "project": {
            "name": "Project-first Demo",
            "objective": "Deliver a usable small demo without Founder micromanagement.",
            "priority": "HIGH",
            "success_criteria": ["A usable demo exists", "The result is reviewable from the Project"],
            "constraints": ["Do not contact external parties", "Do not remove People or Meetings"],
            "deadline": "2026-08-10T18:00:00+08:00",
        },
        "project_id": None,
        "tasks": [],
        "operation": {
            "title": "Establish the first verified Project move",
            "objective": "Produce the first evidence needed to move the Project forward.",
            "project_id": None,
            "budget_twd": 8,
            "tasks": [{
                "title": "Produce bounded evidence",
                "objective": "Return one concrete Project result.",
                "assignee_employee_id": researcher.id,
                "reviewer_employee_id": None,
                "acceptance_criteria": ["A concrete result is persisted."],
            }],
            "meeting_policy": "NEVER",
            "meeting_config": {
                "trigger": "NEVER",
                "participant_employee_ids": [],
                "max_rounds": 1,
                "max_speakers_per_round": 1,
                "contribution_output_cap": 192,
                "token_limit": 6000,
                "budget_twd": 0,
                "retry_limit": 0,
            },
            "completion_criteria": ["The first bounded Project result is complete."],
        },
    }


def test_v013_founder_navigation_is_project_first_and_preserves_people_meetings(ctx):
    template = Path("eason_one/templates/hq_base.html").read_text(encoding="utf-8")
    if "headquarters-v015-founder.css" in template:
        assert "('/headquarters','HQ','⌂')" in template
        assert "('/headquarters/projects','Projects','□')" in template
        assert "('/headquarters/people','People','P')" in template
        assert "('/headquarters/meetings','Meetings','◎')" in template
        assert "('/headquarters/truth','Company Truth','↺')" in template
        assert "('/headquarters/finance','Finance','₵')" in template
        assert "('/headquarters/ceo-office','CEO Office'" not in template
        assert template.index("Projects','□") < template.index("People','P")
    elif "headquarters-v014.css" in template:
        assert "('/headquarters','Home','⌂')" in template
        assert "('/headquarters/ceo-office','CEO Office','◈')" in template
        assert "('/headquarters/projects','Projects','□')" in template
        assert "('/headquarters/people','People','P')" in template
        assert "('/headquarters/meetings','Meetings','◎')" in template
        assert "('/headquarters/results','History','↺')" in template
        assert template.index("CEO Office','◈") < template.index("Projects','□")
        assert template.index("Projects','□") < template.index("People','P")
    else:
        assert "('/headquarters','HQ','⌂')" in template
        assert "('/headquarters/projects','Projects','◇')" in template
        assert "('/headquarters/people','People','P')" in template
        assert "('/headquarters/meetings','Meetings','◎')" in template
        assert "('/headquarters/results','Results','R')" in template
        assert "('/headquarters/missions','Mission Control','M')" in template
        assert template.index("Projects','◇") < template.index("People','P")
        assert template.index("People','P") < template.index("Meetings','◎")
    assert "headquarters-v013.css" in template


def test_v013_hq_and_project_surfaces_render_without_mission_dashboard_mental_model(client):
    home = client.get("/headquarters")
    assert home.status_code == 200
    text = home.get_data(as_text=True)
    if "COMPANY · FOUNDER RE-ENTRY" in text:
        assert "CURRENT COMPANY SITUATION" in text
        assert "Where the real outcomes are now" in text
        assert "data-command-open" in text
    elif "HQ · FOUNDER CONTROL" in text:
        assert "NEEDS YOU" in text
        assert "HAPPENING NOW" in text
        assert "ACTIVE PROJECTS" in text
        assert "Talk to CEO" in text
    elif "Founder / HQ Home" in text:
        assert "Active Projects" in text
        assert "Needs Founder" in text
        assert "Talk to CEO" in text
    else:
        assert "YOUR COMPANY TODAY" in text
        assert "CEO · DIRECT LINE" in text
        assert "The work that actually matters." in text
    assert "CURRENT MISSION" not in text

    projects = client.get("/headquarters/projects")
    assert projects.status_code == 200
    text = projects.get_data(as_text=True)
    if "Real outcomes the company is responsible for." in text:
        assert "Give the company an outcome, not a Task." in text
        assert "Project Health" not in text
        assert "Next Milestones" not in text
    elif "What the company is trying to accomplish." in text:
        assert "Founder-facing outcomes" in text
        assert "Project Health" not in text
        assert "Next Milestones" not in text
    elif "All company work, organized by outcomes." in text:
        assert "Project Health" in text
        assert "Next Milestones" in text
    else:
        assert "Your durable company work." in text
        assert "You should never need to create a Mission manually." in text

    results = client.get("/headquarters/results")
    assert results.status_code == 200
    result_text = results.get_data(as_text=True)
    assert "What actually happened." in result_text or "What the company actually made." in result_text


def test_v013_new_project_contract_materializes_durable_project_and_keeps_first_mission_bounded(ctx, monkeypatch):
    ceo = Employee.query.filter_by(slug="ceo").one()
    researcher = Employee.query.filter_by(slug="researcher").one()
    payload = _new_project_payload(researcher)
    monkeypatch.setattr("eason_one.services.execution.get_provider", lambda _: _provider(payload))

    run, operation = founder_request(
        ceo,
        "Create a Project that delivers a usable small demo. Project budget hard cap NT$50. "
        "Do not contact external parties. You manage the work and only ask me for consequential decisions.",
    )
    assert run.status == "SUCCEEDED"
    assert operation is not None
    memory = operation.memory_json
    assert memory["new_project_spec"]["name"] == "Project-first Demo"
    assert Decimal(memory["project_authorized_budget_twd"]) == Decimal("50.0000")
    assert Decimal(operation.hard_cost_cap_twd) < Decimal("50")
    assert Decimal(operation.hard_cost_cap_twd) >= Decimal(operation.estimated_cost_twd)

    approve(operation)
    db.session.refresh(operation)
    project = db.session.get(Project, operation.project_id)
    assert project is not None
    assert project.name == "Project-first Demo"
    assert project.origin == "CEO_PROJECT"
    assert Decimal(project.real_budget_limit) == Decimal("50.0000")
    assert "Do not contact external parties" in (project.known_constraints or "")
    assert project.deadline is not None
    success = KnowledgeItem.query.filter_by(
        project_id=project.id, kind="DECISION", title="Project success criteria"
    ).one()
    assert success.founder_approved is True
    assert "A usable demo exists" in success.content
    assert project_remaining_authority(project) <= Decimal("50.0000")


def test_v013_project_result_truth_separates_unverified_result_from_validated_artifact(ctx):
    ceo = Employee.query.filter_by(slug="ceo").one()
    researcher = Employee.query.filter_by(slug="researcher").one()
    project = Project(
        name="Result Truth Project", objective="Keep Project Results honest.",
        status="ACTIVE", priority="HIGH", environment="LIVE", origin="TEST",
        owner_employee_id=ceo.id, real_budget_limit=Decimal("10"),
    )
    db.session.add(project); db.session.flush()
    task = Task(
        project_id=project.id, title="Produce a result", objective="Do work",
        status="DONE", assigned_employee_id=researcher.id, created_by_employee_id=ceo.id,
        required_output="Result", acceptance_criteria="Concrete result exists",
        result_summary="A real persisted result exists.",
    )
    db.session.add(task); db.session.commit()

    rows = project_snapshot(project)["results"]
    assert len(rows) == 1
    assert rows[0]["kind"] == "TASK_RESULT"
    assert rows[0]["verification"] == "UNVERIFIED"
    assert rows[0]["href"].endswith(f"/headquarters/projects/{project.id}#results")

    model = researcher.current_model
    run = AgentRun(
        employee_id=researcher.id, project_id=project.id, task_id=task.id,
        model_config_id=model.id, purpose="TASK_EXECUTION", user_request="Do work",
        system_prompt_snapshot="system", context_snapshot="context",
        raw_output=json.dumps({"result_summary": "A real persisted result exists.", "changed_files": ["demo.py"], "tests": ["pytest"]}),
        parsed_output_json={"result_summary": "A real persisted result exists.", "changed_files": ["demo.py"], "tests": ["pytest"]},
        status="SUCCEEDED", structured_validation_status="PASSED",
        provider_key_snapshot=model.provider_key, model_name_snapshot=model.model_name,
        input_price_snapshot=model.input_price_per_million,
        output_price_snapshot=model.output_price_per_million,
        currency_snapshot=model.currency,
    )
    db.session.add(run); db.session.commit()
    rows = results_snapshot()["results"]
    result = next(row for row in rows if row["project"].id == project.id)
    assert result["kind"] == "ARTIFACT"
    assert result["verification"] == "VALIDATED"
    assert result["files"] == ["demo.py"]
    assert result["checks"] == ["pytest"]


def test_v013_truthful_people_now_requires_live_agent_run(ctx):
    ceo = Employee.query.filter_by(slug="ceo").one()
    researcher = Employee.query.filter_by(slug="researcher").one()
    project = Project(
        name="Presence Project", objective="Truthful presence", status="ACTIVE",
        priority="HIGH", environment="LIVE", origin="TEST", owner_employee_id=ceo.id,
    )
    db.session.add(project); db.session.flush()
    task = Task(
        project_id=project.id, title="Stale working label", objective="Should not look live",
        status="WORKING", assigned_employee_id=researcher.id, created_by_employee_id=ceo.id,
    )
    db.session.add(task); db.session.commit()
    snapshot = home_snapshot()
    assert all(row["employee"].id != researcher.id for row in snapshot["working"])
    card = next(row for row in snapshot["projects"] if row["project"].id == project.id)
    assert card["state"] != "WORKING"


def test_v013_secondary_ui_is_visually_demoted_and_people_meetings_are_not_rewritten(ctx):
    missions = Path("eason_one/templates/hq_missions.html").read_text(encoding="utf-8")
    system = Path("eason_one/templates/hq_system.html").read_text(encoding="utf-8")
    finance = Path("eason_one/templates/hq_finance.html").read_text(encoding="utf-8")
    memory = Path("eason_one/templates/hq_memory.html").read_text(encoding="utf-8")
    assert "ADVANCED · MISSION CONTROL" in missions
    assert "The engine room stays out of your way." in system
    assert "Authority first. Spend second." in finance or "Every cost remains attributable." in finance
    assert "What the company is allowed to remember." in memory
    # V0.13 intentionally leaves the successful People/Meetings page templates untouched.
    assert Path("eason_one/templates/hq_people.html").exists()
    assert Path("eason_one/templates/hq_meetings.html").exists()


def test_v013_project_scoped_ceo_cannot_silently_switch_or_duplicate_project(ctx, monkeypatch):
    ceo = Employee.query.filter_by(slug="ceo").one()
    researcher = Employee.query.filter_by(slug="researcher").one()
    project = Project(
        name="Scoped Project", objective="Keep CEO work attached here.",
        status="ACTIVE", priority="HIGH", environment="LIVE", origin="TEST",
        owner_employee_id=ceo.id, real_budget_limit=Decimal("20"),
    )
    db.session.add(project); db.session.commit()
    payload = _new_project_payload(researcher)
    payload["project"]["name"] = "Wrong Duplicate Project"
    payload["operation"]["project_id"] = None
    monkeypatch.setattr("eason_one.services.execution.get_provider", lambda _: _provider(payload))

    run, operation = founder_request(
        ceo,
        "Continue this Project and produce the next verified result within existing authority.",
        project_id=project.id,
    )
    assert run.status == "SUCCEEDED"
    assert operation is not None
    assert operation.project_id == project.id
    assert operation.memory_json.get("new_project_spec") is None
    assert Project.query.filter_by(name="Wrong Duplicate Project").count() == 0


def test_v013_founder_can_complete_review_project_without_model_call(client, ctx):
    ceo = Employee.query.filter_by(slug="ceo").one()
    researcher = Employee.query.filter_by(slug="researcher").one()
    project = Project(
        name="Acceptable Project", objective="Deliver something real.",
        status="REVIEW", priority="HIGH", environment="LIVE", origin="TEST",
        owner_employee_id=ceo.id, real_budget_limit=Decimal("10"),
    )
    db.session.add(project); db.session.flush()
    task = Task(
        project_id=project.id, title="Delivered result", objective="Deliver it",
        status="DONE", assigned_employee_id=researcher.id, created_by_employee_id=ceo.id,
        result_summary="A concrete Project result is ready for Founder acceptance.",
    )
    db.session.add(task); db.session.flush()
    model = researcher.current_model
    run = AgentRun(
        employee_id=researcher.id, project_id=project.id, task_id=task.id,
        model_config_id=model.id, purpose="TASK_EXECUTION", user_request="Deliver it",
        system_prompt_snapshot="system", context_snapshot="context",
        raw_output=json.dumps({"result_summary": task.result_summary, "tests": ["deterministic-check"]}),
        parsed_output_json={"result_summary": task.result_summary, "tests": ["deterministic-check"]},
        status="SUCCEEDED", structured_validation_status="PASSED",
        provider_key_snapshot=model.provider_key, model_name_snapshot=model.model_name,
        input_price_snapshot=model.input_price_per_million,
        output_price_snapshot=model.output_price_per_million,
        currency_snapshot=model.currency,
    )
    db.session.add(run); db.session.commit()

    response = client.post(f"/headquarters/projects/{project.id}/complete", follow_redirects=False)
    assert response.status_code == 302
    db.session.refresh(project)
    assert project.status == "COMPLETED"
    assert project.next_milestone is None
    decision = KnowledgeItem.query.filter_by(
        project_id=project.id, kind="DECISION", title="Founder accepted Project result"
    ).one()
    assert decision.founder_approved is True


def test_v013_host_verification_rejects_wrong_exact_replacement(app, monkeypatch, tmp_path):
    monkeypatch.setenv("EASON_ONE_TEST_HOST_VALIDATION", "1")
    repo = tmp_path / "repo"
    target = repo / "eason_one" / "templates" / "headquarters.html"
    target.parent.mkdir(parents=True)
    target.write_text("What should the company move forward with?", encoding="utf-8")
    project = SimpleNamespace(
        objective=(
            'Change the Eason One HQ homepage heading from “What should the company move forward?” '
            'to “What should we move forward on?” and verify the template.'
        )
    )
    task = SimpleNamespace(
        project=project, operation=None, title="HQ heading micro-fix",
        objective="Apply only the requested heading correction.",
        acceptance_criteria="The new heading exists and the old heading no longer exists.",
    )
    payload = {"changed_files": ["eason_one/templates/headquarters.html"]}
    with app.app_context():
        result = __import__(
            "eason_one.services.host_validation", fromlist=["validate_codex_result"]
        ).validate_codex_result(
            task, repo, payload,
            actual_changed_files=["eason_one/templates/headquarters.html"],
        )
    assert result["attempted"] is True
    assert result["success"] is False
    assert result["failure_reason"] == "EXACT_ACCEPTANCE_MISMATCH"
    exact = result["exact_replacements"][0]
    assert exact["new"] == "What should we move forward on?"
    assert exact["status"] == "FAILED"


def test_v013_host_verification_accepts_exact_replacement(app, monkeypatch, tmp_path):
    monkeypatch.setenv("EASON_ONE_TEST_HOST_VALIDATION", "1")
    repo = tmp_path / "repo"
    target = repo / "eason_one" / "templates" / "headquarters.html"
    target.parent.mkdir(parents=True)
    target.write_text("What should we move forward on?", encoding="utf-8")
    project = SimpleNamespace(
        objective=(
            'Change the Eason One HQ homepage heading from “What should the company move forward?” '
            'to “What should we move forward on?” and verify the template.'
        )
    )
    task = SimpleNamespace(
        project=project, operation=None, title="HQ heading micro-fix",
        objective="Apply only the requested heading correction.",
        acceptance_criteria="The new heading exists and the old heading no longer exists.",
    )
    payload = {"changed_files": ["eason_one/templates/headquarters.html"]}
    with app.app_context():
        result = __import__(
            "eason_one.services.host_validation", fromlist=["validate_codex_result"]
        ).validate_codex_result(
            task, repo, payload,
            actual_changed_files=["eason_one/templates/headquarters.html"],
        )
    assert result["success"] is True
    assert result["repository_delta_match"] is True
    assert result["exact_replacements"][0]["status"] == "PASSED"


def test_v013_codex_claim_without_host_verification_is_not_validated_or_acceptable(client, ctx):
    ceo = Employee.query.filter_by(slug="ceo").one()
    engineer = Employee.query.filter_by(slug="engineer").one()
    project = Project(
        name="False Validation Regression",
        objective='Change heading from “old exact text” to “new exact text”.',
        status="REVIEW", priority="HIGH", environment="LIVE", origin="TEST",
        owner_employee_id=ceo.id, real_budget_limit=Decimal("3"),
    )
    db.session.add(project); db.session.flush()
    task = Task(
        project_id=project.id, title="Exact text change", objective="Change the heading",
        status="DONE", assigned_employee_id=engineer.id, created_by_employee_id=ceo.id,
        result_summary="Codex claimed the heading was changed.",
    )
    db.session.add(task); db.session.flush()
    model = engineer.current_model
    run = AgentRun(
        employee_id=engineer.id, project_id=project.id, task_id=task.id,
        model_config_id=model.id, purpose="TASK_EXECUTION", user_request=task.objective,
        system_prompt_snapshot="system", context_snapshot="context",
        context_composition_json={
            "read_only": False,
            "host_validation": {"attempted": False, "success": True},
        },
        raw_output=json.dumps({"result_summary": task.result_summary}),
        parsed_output_json={
            "result_summary": task.result_summary,
            "knowledge_proposals": [],
            "codex": {
                "changed_files": ["eason_one/templates/headquarters.html"],
                "tests": [{"command": "claimed test", "status": "PASSED", "detail": "claim"}],
                "acceptance": [{"criterion": "Change the heading", "status": "PASSED", "evidence": "claim"}],
                "risks": [], "needs_founder": False, "founder_reason": None,
            },
        },
        status="SUCCEEDED", structured_validation_status="PASSED",
        provider_key_snapshot="codex", model_name_snapshot=model.model_name,
        input_price_snapshot=0, output_price_snapshot=0, currency_snapshot="TWD",
    )
    db.session.add(run); db.session.commit()

    snapshot = project_snapshot(project)
    assert snapshot["results"][0]["verification"] == "UNVERIFIED"
    assert snapshot["results"][0]["href"].endswith(f"/headquarters/system/runs/{run.id}")
    assert snapshot["card"]["state"] == "BLOCKED"
    assert snapshot["card"]["progress"] <= 95

    response = client.post(f"/headquarters/projects/{project.id}/complete", follow_redirects=False)
    assert response.status_code == 302
    db.session.refresh(project)
    assert project.status == "REVIEW"


def test_v013_wsl_codex_readiness_recovers_from_cold_start_timeout(ctx, monkeypatch):
    """A transient WSL cold start must be retried before blocking Engineering."""
    from flask import current_app
    from eason_one.services import codex_connector

    monkeypatch.setitem(current_app.config, "CODEX_REAL_READINESS_PROBE", True)
    monkeypatch.setenv("EASON_ONE_CODEX_TRANSPORT", "wsl")
    monkeypatch.setenv("EASON_ONE_WSL_PATH", r"C:\WINDOWS\system32\wsl.exe")
    monkeypatch.setenv("EASON_ONE_CODEX_WSL_DISTRO", "Ubuntu")
    monkeypatch.setenv("EASON_ONE_CODEX_WSL_PATH", "/home/eason/.local/bin/codex")
    monkeypatch.setenv("EASON_ONE_CODEX_READINESS_TIMEOUT_SECONDS", "30")
    monkeypatch.setattr(codex_connector.time, "sleep", lambda *_: None)
    codex_connector._READINESS_CACHE.update({"at": 0.0, "value": None})

    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs.get("timeout")))
        if len(calls) == 1:
            raise codex_connector.subprocess.TimeoutExpired(command, kwargs.get("timeout"))
        if command[-1] == "true":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if command[-1] == "--version":
            return SimpleNamespace(returncode=0, stdout="codex-cli 0.146.0\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="Logged in using ChatGPT\n", stderr="")

    monkeypatch.setattr(codex_connector.subprocess, "run", fake_run)
    descriptor = codex_connector.codex_runtime_descriptor(force_probe=True)

    assert descriptor["ready"] is True
    assert descriptor["version"] == "codex-cli 0.146.0"
    assert len(calls) == 4
    assert calls[0][0][-1] == "true"
    assert calls[1][0][-1] == "true"
    assert calls[2][0][-1] == "--version"
    assert calls[3][0][-2:] == ["login", "status"]
    assert all(timeout == 30 for _, timeout in calls)


def _budget_test_operation(ceo, project, *, approved="0.0001", status="RUNNING", kernel_status="RUNNING"):
    operation = Operation(
        title="Project authority allocation regression",
        objective="Continue bounded work inside existing Project authority.",
        project_id=project.id, proposed_by_employee_id=ceo.id,
        status=status, kernel_status=kernel_status, route_type="SINGLE_WORKER",
        current_stage="EXECUTION",
        plan_json={
            "mode": "OPERATION_PLAN",
            "executive_response": "Bounded work.",
            "operation": {
                "title": "Project authority allocation regression",
                "objective": "Continue bounded work inside existing Project authority.",
                "project_id": project.id,
                "budget_twd": approved,
                "tasks": [],
                "meeting_policy": "NEVER",
                "meeting_config": {
                    "trigger": "NEVER", "participant_employee_ids": [],
                    "max_rounds": 1, "max_speakers_per_round": 1,
                    "contribution_output_cap": 192, "token_limit": 6000,
                    "budget_twd": 0, "retry_limit": 0,
                },
                "completion_criteria": ["Bounded work is complete."],
            },
        },
        approved_budget_twd=Decimal(approved), estimated_cost_twd=Decimal("0.0001"),
        hard_cost_cap_twd=Decimal(approved), stage_cost_cap_twd=Decimal(approved),
        single_call_cost_cap_twd=Decimal(approved), approved_at=now(),
        memory_json={"founder_attention_events": []},
    )
    db.session.add(operation); db.session.commit()
    return operation


def test_v013_project_authority_autodelegates_internal_operation_shortfall(ctx, monkeypatch):
    ceo = Employee.query.filter_by(slug="ceo").one()
    project = Project(
        name="Project Budget Envelope", objective="Keep Founder budget at Project scope.",
        status="ACTIVE", priority="HIGH", environment="LIVE", origin="TEST",
        owner_employee_id=ceo.id, real_budget_limit=Decimal("3"),
    )
    db.session.add(project); db.session.commit()
    operation = _budget_test_operation(ceo, project)

    def snapshot(op):
        hard = Decimal(op.hard_cost_cap_twd)
        actual = Decimal("0.0001")
        return {
            "hard_cap": hard, "actual": actual, "reserved": Decimal("0"),
            "available": max(Decimal("0"), hard - actual),
            "completion_reserve": Decimal("0"),
            "spendable_before_completion": max(Decimal("0"), hard - actual),
        }

    monkeypatch.setattr("eason_one.services.operation_kernel.budget_snapshot", snapshot)
    assert ensure_budget(operation, Decimal("0.50"), company_checked=True) is True
    db.session.refresh(operation); db.session.refresh(project)
    assert Decimal(operation.approved_budget_twd) == Decimal("0.5001")
    assert Decimal(operation.hard_cost_cap_twd) == Decimal("0.5001")
    assert Decimal(project.real_budget_limit) == Decimal("3.0000")
    assert operation.memory_json["project_authority_delegations"][-1]["delegated_twd"] == "0.5000"


def test_v013_stale_budget_gate_recovers_from_existing_project_authority(ctx):
    ceo = Employee.query.filter_by(slug="ceo").one()
    engineer = Employee.query.filter_by(slug="engineer").one()
    project = Project(
        name="Recovered Project Budget Gate", objective="Retry Engineering without Founder reauthorization.",
        status="ACTIVE", priority="HIGH", environment="LIVE", origin="TEST",
        owner_employee_id=ceo.id, real_budget_limit=Decimal("3"),
    )
    db.session.add(project); db.session.commit()
    operation = _budget_test_operation(
        ceo, project, status="WAITING_FOR_FOUNDER", kernel_status="WAITING_APPROVAL"
    )
    operation.founder_report_json = {
        "decision_kind": "BUDGET_AUTHORIZATION",
        "summary": "The next bounded execution step exceeds the remaining authorized Operation budget.",
        "additional_budget_exact_twd": "0.5000",
        "additional_budget_twd": "0.5",
    }
    operation.waiting_reason = operation.founder_report_json["summary"]
    operation.memory_json = {
        "founder_attention_events": [{
            "kind": "BUDGET_AUTHORIZATION", "status": "PENDING",
            "reason": operation.waiting_reason, "choices": ["APPROVE", "MODIFY", "REJECT"],
        }]
    }
    task = Task(
        project_id=project.id, operation_id=operation.id,
        title="Retry Codex after readiness repair", objective="Apply bounded code change.",
        status="BLOCKED", priority="HIGH", created_by_employee_id=ceo.id,
        assigned_employee_id=engineer.id, reviewer_employee_id=engineer.id,
        required_output="Operation result", acceptance_criteria="Exact change verified",
    )
    db.session.add(task); db.session.flush()
    model = engineer.current_model
    run = AgentRun(
        employee_id=engineer.id, project_id=project.id, operation_id=operation.id, task_id=task.id,
        model_config_id=model.id, purpose="TASK_EXECUTION", user_request=task.objective,
        system_prompt_snapshot="system", context_snapshot="context",
        status="FAILED", failure_reason="ENGINEERING_RUNTIME_BLOCKED",
        structured_validation_status="FAILED",
        structured_validation_errors_json=["Codex runtime is not ready"],
        error_text="Codex runtime is not ready: wsl.exe --version timed out after 12 seconds",
        real_cost=0, provider_key_snapshot="codex", model_name_snapshot=model.model_name,
        input_price_snapshot=0, output_price_snapshot=0, currency_snapshot="TWD",
    )
    db.session.add(run); db.session.commit()

    assert recover_project_budget_authority(operation) is True
    db.session.refresh(operation); db.session.refresh(project); db.session.refresh(task)
    assert operation.status == "RUNNING"
    assert operation.founder_report_json is None
    assert operation.waiting_reason is None
    assert task.status == "ASSIGNED"
    assert Decimal(project.real_budget_limit) == Decimal("3.0000")
    assert Decimal(operation.approved_budget_twd) == Decimal("0.5001")
    attention = operation.memory_json["founder_attention_events"][-1]
    assert attention["status"] == "RESOLVED"
    assert attention["resolution"] == "AUTO_PROJECT_AUTHORITY"


def test_v013_founder_operation_budget_change_never_rewrites_project_hard_cap(ctx):
    from eason_one.services import founder_decisions
    ceo = Employee.query.filter_by(slug="ceo").one()
    project = Project(
        name="Stable Project Authority", objective="Keep Project cap stable.",
        status="ACTIVE", priority="HIGH", environment="LIVE", origin="TEST",
        owner_employee_id=ceo.id, real_budget_limit=Decimal("3"),
    )
    db.session.add(project); db.session.commit()
    operation = _budget_test_operation(
        ceo, project, approved="0.1000", status="WAITING_FOR_FOUNDER", kernel_status="WAITING_APPROVAL"
    )
    operation.founder_report_json = {
        "decision_kind": "BUDGET_AUTHORIZATION",
        "additional_budget_twd": "0.2",
        "additional_budget_exact_twd": "0.2000",
    }
    db.session.commit()

    founder_decisions.decide(operation, "APPROVE")
    db.session.refresh(operation); db.session.refresh(project)
    assert Decimal(operation.approved_budget_twd) == Decimal("0.3000")
    assert Decimal(project.real_budget_limit) == Decimal("3.0000")


def test_v013_completed_operation_supersedes_stale_founder_gate_and_project_can_complete(client, ctx):
    """A recovered Operation must not leave an immortal Needs You gate behind."""
    ceo = Employee.query.filter_by(slug="ceo").one()
    engineer = Employee.query.filter_by(slug="engineer").one()
    project = Project(
        name="Completed Gate Reconciliation",
        objective="Finish verified Engineering without stale Founder attention.",
        status="REVIEW", priority="HIGH", environment="LIVE", origin="TEST",
        owner_employee_id=ceo.id, real_budget_limit=Decimal("3"),
        current_state_summary="Engineering delivery is ready for Founder acceptance.",
    )
    db.session.add(project); db.session.flush()
    operation = Operation(
        title="Completed engineering execution",
        objective="Apply and verify one bounded change.",
        project_id=project.id, proposed_by_employee_id=ceo.id,
        status="COMPLETED", kernel_status="COMPLETED", route_type="SINGLE_WORKER",
        current_stage="COMPLETED",
        plan_json={
            "mode": "OPERATION_PLAN", "executive_response": "Done.",
            "operation": {
                "title": "Completed engineering execution",
                "objective": "Apply and verify one bounded change.",
                "project_id": project.id, "budget_twd": "1",
                "tasks": [], "meeting_policy": "NEVER",
                "meeting_config": {
                    "trigger": "NEVER", "participant_employee_ids": [],
                    "max_rounds": 1, "max_speakers_per_round": 1,
                    "contribution_output_cap": 192, "token_limit": 6000,
                    "budget_twd": 0, "retry_limit": 0,
                },
                "completion_criteria": ["The exact change is independently verified."],
            },
        },
        approved_budget_twd=Decimal("1"), estimated_cost_twd=Decimal("0.2"),
        hard_cost_cap_twd=Decimal("1"), stage_cost_cap_twd=Decimal("1"),
        single_call_cost_cap_twd=Decimal("1"), approved_at=now(), ended_at=now(),
        founder_report_json={
            "decision_kind": "DELIVERY",
            "headline": "Engineering Mission completed.",
            "summary": "Independent host verification passed.",
        },
        memory_json={
            "founder_attention_events": [{
                "kind": "BUDGET_AUTHORIZATION", "status": "PENDING",
                "reason": "Historical internal budget gate.",
                "choices": ["APPROVE", "MODIFY", "REJECT"],
            }]
        },
    )
    db.session.add(operation); db.session.flush()
    task = Task(
        project_id=project.id, operation_id=operation.id,
        title="Verified bounded change", objective="Apply exact text replacement.",
        status="DONE", priority="HIGH", created_by_employee_id=ceo.id,
        assigned_employee_id=engineer.id, reviewer_employee_id=engineer.id,
        required_output="Operation result", acceptance_criteria="Exact text verified",
        result_summary="The requested exact change is present and independently verified.",
        completed_at=now(),
    )
    db.session.add(task); db.session.flush()
    model = engineer.current_model
    run = AgentRun(
        employee_id=engineer.id, project_id=project.id, operation_id=operation.id,
        task_id=task.id, model_config_id=model.id, purpose="TASK_EXECUTION",
        user_request=task.objective, system_prompt_snapshot="system",
        context_snapshot="context",
        context_composition_json={
            "read_only": False,
            "host_validation": {
                "attempted": True, "success": True,
                "repository_delta_match": True,
            },
            "repository_delta": ["eason_one/templates/headquarters.html"],
        },
        raw_output=json.dumps({"result_summary": task.result_summary}),
        parsed_output_json={
            "result_summary": task.result_summary,
            "knowledge_proposals": [],
            "codex": {
                "changed_files": ["eason_one/templates/headquarters.html"],
                "tests": [{"command": "host exact check", "status": "PASSED", "detail": "exact"}],
                "acceptance": [{"criterion": "Exact text verified", "status": "PASSED", "evidence": "host"}],
                "risks": [], "needs_founder": False, "founder_reason": None,
            },
        },
        status="SUCCEEDED", structured_validation_status="PASSED",
        provider_key_snapshot="codex", model_name_snapshot=model.model_name,
        input_price_snapshot=0, output_price_snapshot=0, currency_snapshot="TWD",
    )
    db.session.add(run); db.session.commit()

    snapshot = project_snapshot(project)
    db.session.refresh(operation)
    assert snapshot["attention"] == []
    assert snapshot["card"]["state"] == "RESULT_READY"
    event = operation.memory_json["founder_attention_events"][0]
    assert event["status"] == "RESOLVED"
    assert event["resolution"] == "SUPERSEDED_BY_COMPLETED_OPERATION"

    response = client.post(
        f"/headquarters/projects/{project.id}/complete", follow_redirects=False
    )
    assert response.status_code == 302
    db.session.refresh(project)
    assert project.status == "COMPLETED"


def test_v013_verified_project_supersedes_stale_pending_project_plan_proposal(client, ctx):
    """A legacy pending Proposal cannot block Founder acceptance after verified delivery."""
    ceo = Employee.query.filter_by(slug="ceo").one()
    engineer = Employee.query.filter_by(slug="engineer").one()
    project = Project(
        name="Verified Proposal Reconciliation",
        objective="Finish one verified delivery without a stale proposal gate.",
        status="REVIEW", priority="HIGH", environment="LIVE", origin="TEST",
        owner_employee_id=ceo.id, real_budget_limit=Decimal("3"),
        current_state_summary="Engineering delivery is ready for Founder acceptance.",
    )
    db.session.add(project); db.session.flush()
    operation = Operation(
        title="Verified execution", objective="Apply one bounded change.",
        project_id=project.id, proposed_by_employee_id=ceo.id,
        status="COMPLETED", kernel_status="COMPLETED", route_type="SINGLE_WORKER",
        current_stage="COMPLETED",
        plan_json={
            "mode": "OPERATION_PLAN", "executive_response": "Done.",
            "operation": {
                "title": "Verified execution", "objective": "Apply one bounded change.",
                "project_id": project.id, "budget_twd": "1", "tasks": [],
                "meeting_policy": "NEVER",
                "meeting_config": {
                    "trigger": "NEVER", "participant_employee_ids": [],
                    "max_rounds": 1, "max_speakers_per_round": 1,
                    "contribution_output_cap": 192, "token_limit": 6000,
                    "budget_twd": 0, "retry_limit": 0,
                },
                "completion_criteria": ["The exact change is independently verified."],
            },
        },
        approved_budget_twd=Decimal("1"), estimated_cost_twd=Decimal("0.2"),
        hard_cost_cap_twd=Decimal("1"), stage_cost_cap_twd=Decimal("1"),
        single_call_cost_cap_twd=Decimal("1"), approved_at=now(), ended_at=now(),
        founder_report_json={"decision_kind": "DELIVERY", "summary": "Verified."},
        memory_json={"founder_attention_events": []},
    )
    db.session.add(operation); db.session.flush()
    task = Task(
        project_id=project.id, operation_id=operation.id,
        title="Verified change", objective="Apply exact replacement.",
        status="DONE", priority="HIGH", created_by_employee_id=ceo.id,
        assigned_employee_id=engineer.id, reviewer_employee_id=engineer.id,
        required_output="Operation result", acceptance_criteria="Exact text verified",
        result_summary="The exact change is present and independently verified.",
        completed_at=now(),
    )
    db.session.add(task); db.session.flush()
    model = engineer.current_model
    run = AgentRun(
        employee_id=engineer.id, project_id=project.id, operation_id=operation.id,
        task_id=task.id, model_config_id=model.id, purpose="TASK_EXECUTION",
        user_request=task.objective, system_prompt_snapshot="system",
        context_snapshot="context",
        context_composition_json={
            "read_only": False,
            "host_validation": {
                "attempted": True, "success": True, "repository_delta_match": True,
            },
            "repository_delta": ["eason_one/templates/headquarters.html"],
        },
        raw_output=json.dumps({"result_summary": task.result_summary}),
        parsed_output_json={
            "result_summary": task.result_summary, "knowledge_proposals": [],
            "codex": {
                "changed_files": ["eason_one/templates/headquarters.html"],
                "tests": [{"command": "host exact check", "status": "PASSED", "detail": "exact"}],
                "acceptance": [{"criterion": "Exact text verified", "status": "PASSED", "evidence": "host"}],
                "risks": [], "needs_founder": False, "founder_reason": None,
            },
        },
        status="SUCCEEDED", structured_validation_status="PASSED",
        provider_key_snapshot="codex", model_name_snapshot=model.model_name,
        input_price_snapshot=0, output_price_snapshot=0, currency_snapshot="TWD",
    )
    db.session.add(run); db.session.flush()
    proposal = Proposal(
        project_id=project.id, agent_run_id=run.id,
        proposed_by_employee_id=ceo.id, status="PENDING",
        payload_json={"type": "PROJECT_PLAN", "plan": {"mode": "PROJECT_ACTION"}},
    )
    db.session.add(proposal); db.session.commit()

    snapshot = project_snapshot(project)
    db.session.refresh(proposal)
    assert proposal.status == "SUPERSEDED"
    assert snapshot["attention"] == []
    assert snapshot["card"]["state"] == "RESULT_READY"

    response = client.post(
        f"/headquarters/projects/{project.id}/complete", follow_redirects=False
    )
    assert response.status_code == 302
    db.session.refresh(project)
    assert project.status == "COMPLETED"


def test_v013_experience_layer_one_static_contract():
    root = Path(__file__).resolve().parents[1]
    hq = (root / "eason_one" / "templates" / "headquarters.html").read_text(encoding="utf-8")
    project = (root / "eason_one" / "templates" / "hq_project.html").read_text(encoding="utf-8")
    js = (root / "eason_one" / "static" / "headquarters.js").read_text(encoding="utf-8")
    css = (root / "eason_one" / "static" / "headquarters-v013.css").read_text(encoding="utf-8")

    if "v016-company-page" in hq:
        assert "COMPANY · FOUNDER RE-ENTRY" in hq
        assert "CURRENT COMPANY SITUATION" in hq
        assert "PROJECT · OUTCOME SPACE" in project
        assert "CURRENT FRONTIER" in project
        assert "ARTIFACTS & EVIDENCE" in project
        assert "Advanced · runtime records" in project
    elif "v015-hq-page" in hq:
        assert "HQ · FOUNDER CONTROL" in hq
        assert "NEEDS YOU" in hq
        assert "HAPPENING NOW" in hq
        assert "ACTIVE PROJECTS" in hq
        assert "ARTIFACTS & EVIDENCE" in project
        assert "Advanced · Runtime records" in project
    elif "v014-home-page" in hq:
        assert "Founder / HQ Home" in hq
        assert "Active Projects" in hq
        assert "Needs Founder" in hq
        assert "Talk to CEO" in hq
        assert "Current Work Items" in project
        assert "Artifacts & Evidence" in project
        assert "Founder Attention" in project
    else:
        assert "What should we move forward on?" in hq
        assert "data-project-live-root" in project
        assert "data-project-now-body" in project
        assert "Project complete." in project
    assert "showThinking('Reading the company state and your intent…')" in js
    assert "data.ceoThinkingBubble" not in js  # do not regress to an invalid property access
    assert "dataset.ceoThinkingBubble" in js
    assert "/api/headquarters/runtime-focus" in js
    assert "SYNCING RESULT" in js
    assert ".dialogue-message.is-thinking" in css
    assert "v013-live-runtime-worker" in css
