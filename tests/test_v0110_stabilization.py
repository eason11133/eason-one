from decimal import Decimal
from pathlib import Path

from eason_one.extensions import db
from eason_one.models import AgentRun, Employee, Operation, Project, Task
from eason_one.services.headquarters import headquarters_snapshot, mission_snapshot, missions_snapshot
from eason_one.services.stabilization import REAL_WORK, SYSTEM_VALIDATION, choose_focus, operation_kind


def _operation(title, status="PLANNED", *, project=None):
    ceo = Employee.query.filter_by(slug="ceo").one()
    project = project or Project.query.filter_by(environment="LIVE").first()
    if project is None:
        project = Project(
            name="V0.11 real project", objective="Test real company work",
            environment="LIVE", owner_employee_id=ceo.id,
        )
        db.session.add(project); db.session.flush()
    row = Operation(
        title=title,
        objective=f"Objective for {title}",
        project_id=project.id,
        proposed_by_employee_id=ceo.id,
        status=status,
        plan_json={"completion_criteria": ["Done truthfully"]},
        approved_budget_twd=Decimal("0"),
        actual_cost_twd=Decimal("0"),
        memory_json={},
    )
    db.session.add(row); db.session.flush()
    return row


def test_real_work_and_validation_are_separate(ctx):
    real = _operation("Build a real Founder dashboard")
    validation = _operation("Eason One Mission Room Live Execution Monitor Read-Only Validation")
    db.session.commit()
    assert operation_kind(real) == REAL_WORK
    assert operation_kind(validation) == SYSTEM_VALIDATION
    real_view = missions_snapshot("real")
    validation_view = missions_snapshot("validation")
    assert [row["operation"].id for row in real_view["missions"]] == [real.id]
    assert [row["operation"].id for row in validation_view["missions"]] == [validation.id]


def test_running_real_mission_beats_old_waiting_real_mission(ctx):
    old = _operation("Old Founder decision", "WAITING_FOR_FOUNDER")
    running = _operation("Current real execution", "RUNNING")
    db.session.commit()
    snapshot = headquarters_snapshot()
    assert snapshot["focus"]["operation"].id == running.id
    assert snapshot["focus"]["operation"].id != old.id


def test_validation_never_becomes_hq_current_mission(ctx):
    real = _operation("Real current Mission", "WAITING_FOR_FOUNDER")
    validation = _operation("Runtime validation smoke test", "RUNNING")
    db.session.commit()
    snapshot = headquarters_snapshot()
    assert snapshot["focus"]["operation"].id == real.id
    assert all(row["operation"].id != validation.id for row in snapshot["missions"])


def test_global_runtime_endpoint_survives_navigation(client, ctx):
    operation = _operation("Real background run", "RUNNING")
    engineer = Employee.query.filter_by(slug="engineer").one()
    model = engineer.current_model
    task = Task(
        project_id=operation.project_id, operation_id=operation.id,
        title="Bounded engineering", objective="Work in background", status="WORKING",
        assigned_employee_id=engineer.id,
    )
    db.session.add(task); db.session.flush()
    run = AgentRun(
        employee_id=engineer.id, project_id=operation.project_id, task_id=task.id,
        operation_id=operation.id, model_config_id=model.id, purpose="TASK_EXECUTION",
        user_request=task.objective, system_prompt_snapshot="x", context_snapshot="x",
        status="RUNNING", provider_key_snapshot=model.provider_key,
        model_name_snapshot=model.model_name, input_price_snapshot=0,
        output_price_snapshot=0, currency_snapshot="TWD", currency="TWD",
    )
    db.session.add(run); db.session.commit()
    before = run.status
    assert client.get("/headquarters").status_code == 200
    assert client.get("/headquarters/people").status_code == 200
    payload = client.get("/api/headquarters/runtime-focus").get_json()
    assert payload["active"] is True
    assert payload["operation_id"] == operation.id
    assert payload["href"].endswith(str(operation.id))
    assert db.session.get(AgentRun, run.id).status == before


def test_mission_page_is_compact_and_technical_details_are_collapsed(client, ctx):
    operation = _operation("Compact real Mission", "PAUSED")
    db.session.commit()
    text = client.get(f"/headquarters/missions/{operation.id}").get_data(as_text=True)
    assert "LIVE EXECUTION MONITOR" in text
    assert "OPEN TECHNICAL AUDIT DETAILS" in text
    assert "SHOW TECHNICAL LIVE EVENT LOG" in text
    assert "JOB SPEC PREPARED" not in text
    assert "11 ·" not in text


def test_runner_updates_clocks_before_signature_guard(ctx):
    script = Path("eason_one/static/operation-runner.js").read_text(encoding="utf-8")
    elapsed = script.index("setText(elapsedLabel")
    guard = script.index("if (signature === lastSignature) return;")
    assert elapsed < guard


def test_read_only_codex_contract_uses_writable_session_and_delta_restore(ctx):
    source = Path("eason_one/services/codex_connector.py").read_text(encoding="utf-8")
    assert 'sandbox = "workspace-write"' in source
    assert "_snapshot_repository(repo) if read_only" in source
    assert "_restore_repository(repo, repository_before" in source
    assert '"--ask-for-approval", "never"' not in source
    assert "at most 12 commands" in source


def test_partial_validation_run_can_surface_as_artifact(ctx):
    operation = _operation("Runtime validation smoke test", "PAUSED")
    engineer = Employee.query.filter_by(slug="engineer").one()
    task = Task(
        project_id=operation.project_id, operation_id=operation.id,
        title="Inspect", objective="Inspect", status="REVIEW",
        assigned_employee_id=engineer.id, result_summary="Useful partial result",
    )
    db.session.add(task); db.session.flush()
    model = engineer.current_model
    run = AgentRun(
        employee_id=engineer.id, project_id=operation.project_id, task_id=task.id,
        operation_id=operation.id, model_config_id=model.id, purpose="TASK_EXECUTION",
        user_request="Inspect", system_prompt_snapshot="x", context_snapshot="ROLE\nInspect",
        raw_output="{}", parsed_output_json={"result_summary":"Useful partial result","codex":{"changed_files":[],"tests":[],"acceptance":[],"risks":[]}},
        status="FAILED", resolution_status="VALIDATION_INFRASTRUCTURE_FAILURE",
        provider_key_snapshot="codex", model_name_snapshot="codex-cli",
        input_price_snapshot=0, output_price_snapshot=0,
        currency_snapshot="TWD", currency="TWD",
    )
    db.session.add(run); db.session.commit()
    artifacts = mission_snapshot(operation)["artifacts"]
    assert artifacts and artifacts[0]["status"] == "PARTIAL RESULT"

def test_full_founder_engineer_codex_validation_delivery_chain(ctx):
    from tests.test_v01010_truthful_engineering import _operation_with_repair
    from eason_one.services.engineering_runtime import prepare_engineering_repair, archive_validation_mission
    from eason_one.services.operation_runtime import run_until_gate

    operation, engineer_task, critic_task = _operation_with_repair()
    prepare_engineering_repair(operation, activate=True)
    paused = run_until_gate(operation.id, max_steps=8)
    assert paused["state"] == "PAUSED"
    db.session.expire_all()
    operation = db.session.get(Operation, operation.id)
    engineer_task = db.session.get(Task, engineer_task.id)
    assert engineer_task.status == "DONE"
    run = AgentRun.query.filter_by(operation_id=operation.id, task_id=engineer_task.id).one()
    assert run.provider_key_snapshot == "codex"
    assert run.structured_validation_status == "PASSED"
    delivered = archive_validation_mission(operation, reason="Founder accepted the bounded evidence.")
    db.session.expire_all()
    operation = db.session.get(Operation, operation.id)
    critic_task = db.session.get(Task, critic_task.id)
    assert delivered["archive_kind"] == "RUNTIME_VALIDATION_ARCHIVE"
    assert operation.status == "COMPLETED"
    assert critic_task.status == "CANCELLED"
    snapshot = mission_snapshot(operation)
    assert snapshot["mission"]["is_archived"] is True
    assert snapshot["artifacts"] and snapshot["artifacts"][0]["run"].id == run.id
