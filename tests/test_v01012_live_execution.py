from decimal import Decimal

from eason_one.extensions import db
from eason_one.models import AgentRun, Employee, ModelConfig, Operation, Task
from eason_one.services.codex_connector import _summarize_codex_event
from eason_one.services.engineering_runtime import (
    VALIDATION_ARCHIVE,
    archive_validation_mission,
    prepare_engineering_repair,
)
from eason_one.services.headquarters import mission_snapshot, mission_view
from eason_one.services.operation_runtime import run_until_gate, runtime_snapshot
from tests.test_v01010_truthful_engineering import _operation_with_repair


def _complete_engineer_step():
    operation, engineer_task, critic_task = _operation_with_repair()
    prepare_engineering_repair(operation, activate=True)
    result = run_until_gate(operation.id, max_steps=8)
    db.session.expire_all()
    return (
        db.session.get(Operation, operation.id),
        db.session.get(Task, engineer_task.id),
        db.session.get(Task, critic_task.id),
        result,
    )


def test_live_execution_monitor_records_real_phases_and_artifact(ctx):
    operation, engineer_task, _, result = _complete_engineer_step()
    assert result["state"] == "PAUSED"
    runtime = runtime_snapshot(operation)
    live = runtime["live_execution"]
    assert live["state"] == "COMPLETED"
    assert live["phase"] == "EVIDENCE_PERSISTED"
    assert live["run_id"]
    assert live["task_id"] == engineer_task.id
    assert live["last_activity_seconds"] is not None
    kinds = {row["kind"] for row in live["events"]}
    assert {"ENGINEER", "TEST", "VALIDATION", "RESULT"}.issubset(kinds)
    counters = live["counters"]
    assert counters["tests"]
    snapshot = mission_snapshot(operation)
    artifact = snapshot["artifacts"][0]
    assert artifact["run"].provider_key_snapshot == "codex"
    assert artifact["job_spec"].startswith("ROLE")
    assert artifact["tests"][0]["status"] == "PASSED"
    assert artifact["acceptance"][0]["status"] == "PASSED"


def test_paused_engineer_review_has_no_current_employee_and_previews_next(ctx):
    operation, _, critic_task, _ = _complete_engineer_step()
    state = runtime_snapshot(operation)
    assert state["active_task"] is None
    assert state["execution_run"] is None
    assert state["next_task"]["id"] == critic_task.id
    assert state["can_resume"] is False
    assert state["can_archive"] is True


def test_archive_validation_mission_is_terminal_and_cancels_downstream(client, ctx):
    operation, engineer_task, critic_task, _ = _complete_engineer_step()
    result = archive_validation_mission(operation, reason="Founder completed the bounded validation.")
    db.session.expire_all()
    operation = db.session.get(Operation, operation.id)
    engineer_task = db.session.get(Task, engineer_task.id)
    critic_task = db.session.get(Task, critic_task.id)
    assert result["archive_kind"] == VALIDATION_ARCHIVE
    assert operation.status == "COMPLETED"
    assert operation.founder_report_json["decision_kind"] == VALIDATION_ARCHIVE
    assert engineer_task.status == "DONE"
    assert critic_task.status == "CANCELLED"
    state = runtime_snapshot(operation)
    assert state["state"] == "ARCHIVED"
    assert state["archived"] is True
    assert state["active_task"] is None
    assert state["next_task"] is None
    assert state["can_resume"] is False
    before = AgentRun.query.filter_by(operation_id=operation.id).count()
    response = client.post(f"/operations/{operation.id}/runtime/start", headers={"Accept": "application/json"})
    assert response.status_code == 409
    assert "archived" in response.get_json()["error"].lower()
    assert AgentRun.query.filter_by(operation_id=operation.id).count() == before
    page = client.get(f"/headquarters/missions/{operation.id}")
    text = page.get_data(as_text=True)
    assert page.status_code == 200
    assert "RUNTIME VALIDATION ARCHIVE" in text
    assert "No Employee, Critic, Meeting, or Provider can resume this Mission." in text
    assert "RESUME ENGINEER" not in text
    assert "Engineer → Codex Job Spec" in text


def test_archive_route_is_founder_control(client, ctx):
    operation, _, _, _ = _complete_engineer_step()
    response = client.post(
        f"/operations/{operation.id}/archive-validation",
        data={"reason": "Archive after evidence review."},
        follow_redirects=False,
    )
    assert response.status_code == 302
    operation = db.session.get(Operation, operation.id)
    assert operation.status == "COMPLETED"
    assert operation.founder_report_json["summary"] == "Archive after evidence review."


def test_v01012_migration_archives_named_runtime_validation(ctx):
    from scripts.migrate_v01012 import migrate

    operation, _, critic_task, _ = _complete_engineer_step()
    operation.title = "CEO Direct Line Reliability Assessment"
    db.session.commit()
    result = migrate()
    db.session.expire_all()
    operation = db.session.get(Operation, operation.id)
    critic_task = db.session.get(Task, critic_task.id)
    assert operation.id in result["archived"]
    assert operation.status == "COMPLETED"
    assert operation.founder_report_json["decision_kind"] == VALIDATION_ARCHIVE
    assert critic_task.status == "CANCELLED"


def test_next_paid_step_preview_names_employee_model_and_estimate(ctx):
    operation, _, critic_task, _ = _complete_engineer_step()
    critic = db.session.get(Employee, critic_task.assigned_employee_id)
    paid = ModelConfig(
        label="Paid Critic",
        provider_key="anthropic",
        model_name="claude-test",
        input_price_per_million=Decimal("100"),
        output_price_per_million=Decimal("200"),
        currency="TWD",
        max_output_tokens=800,
        active=True,
        archived=False,
    )
    db.session.add(paid); db.session.flush()
    critic.current_model_config_id = paid.id
    db.session.commit()
    view = mission_view(operation)
    preview = view["next_step"]
    assert preview["task"].id == critic_task.id
    assert preview["employee"].id == critic.id
    assert preview["provider"] == "anthropic"
    assert preview["paid"] is True
    assert preview["requires_founder"] is True
    assert preview["estimate_twd"] > 0


def test_codex_json_events_map_to_founder_visible_phases():
    command = _summarize_codex_event({
        "type": "item.started",
        "item": {"type": "command_execution", "command": "python -m pytest tests/test_runtime.py"},
    })
    assert command["phase"] == "RUNNING_TESTS"
    assert command["kind"] == "TEST"
    file_change = _summarize_codex_event({
        "type": "item.completed",
        "item": {"type": "file_change", "changes": [{"path": "eason_one/a.py"}]},
    })
    assert file_change["phase"] == "APPLYING_CHANGES"
    assert file_change["counters"]["files_changed"] == ["eason_one/a.py"]
    turn = _summarize_codex_event({
        "type": "turn.completed",
        "usage": {"input_tokens": 10, "output_tokens": 20},
    })
    assert turn["phase"] == "VALIDATING_OUTPUT"
    assert turn["counters"]["output_tokens"] == 20


def test_prior_wsl_migration_does_not_rewind_later_success(ctx):
    from eason_one.models import now
    from scripts.migrate_v01010 import migrate as migrate_v01010
    from scripts.migrate_v01011 import migrate as migrate_v01011
    from scripts.migrate_v01012 import migrate as migrate_v01012

    operation, engineer_task, _, _ = _complete_engineer_step()
    operation.title = "CEO Direct Line Reliability Assessment"
    successful = AgentRun.query.filter_by(
        operation_id=operation.id,
        provider_key_snapshot="codex",
        status="SUCCEEDED",
    ).order_by(AgentRun.id.desc()).first()
    assert successful is not None
    failed_native = AgentRun(
        employee_id=successful.employee_id,
        task_id=engineer_task.id,
        operation_id=operation.id,
        model_config_id=successful.model_config_id,
        purpose="TASK_EXECUTION",
        user_request="Earlier Windows-native attempt",
        system_prompt_snapshot="Earlier Windows-native attempt",
        context_snapshot="Earlier Windows-native attempt",
        status="FAILED",
        provider_key_snapshot="codex",
        model_name_snapshot="codex-cli",
        input_price_snapshot=Decimal("0"),
        output_price_snapshot=Decimal("0"),
        currency_snapshot="TWD",
        real_cost=Decimal("0"),
        currency="TWD",
        error_text="windows sandbox failed: CreateProcessAsUserW failed: 2",
        failure_reason="CODEX_WINDOWS_SANDBOX_UNAVAILABLE",
        started_at=now(),
        finished_at=now(),
    )
    db.session.add(failed_native)
    db.session.commit()

    migrate_v01010()
    migrate_v01011()
    db.session.expire_all()
    operation = db.session.get(Operation, operation.id)
    engineer_task = db.session.get(Task, engineer_task.id)
    assert operation.founder_report_json["decision_kind"] == "ENGINEERING_STEP_REVIEW"
    assert engineer_task.status == "DONE"

    result = migrate_v01012()
    db.session.expire_all()
    operation = db.session.get(Operation, operation.id)
    assert operation.id in result["archived"]
    assert operation.status == "COMPLETED"
    assert operation.founder_report_json["decision_kind"] == VALIDATION_ARCHIVE
