import json
from decimal import Decimal
from types import SimpleNamespace

from eason_one.extensions import db
from eason_one.models import AgentRun, Employee, KnowledgeItem, Operation, OperationStep
from eason_one.providers import ProviderResult
from eason_one.services import operations
from eason_one.services.ceo import founder_request


def _planned_operation():
    ceo = Employee.query.filter_by(slug="ceo").one()
    worker = Employee.query.filter_by(slug="researcher").one()
    reviewer = Employee.query.filter_by(slug="research-director").one()
    return operations.propose_operation(ceo, {
        "mode": "OPERATION_PLAN",
        "executive_response": "Approve bounded work.",
        "operation": {
            "title": "Learning Evidence Reconstruction",
            "objective": "Reconstruct decision-useful learning evidence.",
            "project_id": None,
            "budget_twd": 2,
            "tasks": [{
                "title": "Reconstruct evidence",
                "objective": "Produce a reviewable evidence packet.",
                "assignee_employee_id": worker.id,
                "reviewer_employee_id": reviewer.id,
                "acceptance_criteria": ["Evidence is reviewable"],
            }],
            "meeting_policy": "REQUIRED_ON_MATERIAL_CONFLICT",
            "completion_criteria": ["Evidence passes review"],
        },
    })


def test_headquarters_fills_viewport_and_uses_one_ceo_line(client):
    page = client.get("/headquarters").get_data(as_text=True)
    secondary = client.get("/headquarters/missions").get_data(as_text=True)
    css = open("eason_one/static/headquarters.css", encoding="utf-8").read()
    script = open("eason_one/static/headquarters.js", encoding="utf-8").read()
    assert 'class="hq-shell"' in page
    assert 'class="ceo-dock"' not in page
    assert page.count('name="request"') == 1
    assert 'class="ceo-dock"' in secondary
    assert '.hq-main-shell' in css and 'grid-column:2/-1' in css.replace(' ', '')
    assert 'data-command-dock' in script


def test_headquarters_retires_legacy_layout_constraint():
    css = open("eason_one/static/headquarters.css", encoding="utf-8").read()
    assert '.hq-main-shell' in css
    assert 'min-width:0' in css.replace(' ', '')
    assert 'width:100%' in css.replace(' ', '')


def _legacy_follow_up_run():
    ceo = Employee.query.filter_by(slug="ceo").one()
    payload = json.loads(json.dumps(_planned_operation().plan_json))
    Operation.query.delete()
    payload["mode"] = "OPERATION_FOLLOW_UP"
    run = AgentRun(
        employee_id=ceo.id,
        model_config_id=ceo.current_model.id,
        purpose="CEO_FOUNDER_REQUEST",
        user_request="Continue current work",
        system_prompt_snapshot="system",
        context_snapshot="context",
        raw_output=json.dumps(payload),
        parsed_output_json=None,
        status="SUCCEEDED",
        provider_key_snapshot="mock",
        model_name_snapshot="mock",
        input_price_snapshot=0,
        output_price_snapshot=0,
        currency_snapshot="TWD",
        error_text=(
            "CEO plan validation failed: Only OPERATION_PLAN may define an operation"
        ),
    )
    db.session.add(run)
    db.session.commit()
    return run


def test_legacy_structured_follow_up_reconstructs_pending_decision_once(client, ctx):
    run = _legacy_follow_up_run()
    first = client.get("/headquarters").get_data(as_text=True)
    assert Operation.query.count() == 1
    operation = Operation.query.one()
    assert operation.status == "PLANNED"
    assert operation.memory_json["legacy_source_agent_run_id"] == run.id
    assert run.parsed_output_json["mode"] == "OPERATION_PLAN"
    mission_page = client.get(f"/headquarters/missions/{operation.id}").get_data(as_text=True)
    assert "FOUNDER" in mission_page and "GATE" in mission_page
    for label in ("REJECT", "MODIFY", "APPROVE & CONTINUE"):
        assert label in mission_page
    client.get("/headquarters")
    assert Operation.query.count() == 1
    assert "MISSION CONTROL" in first


def test_legacy_approval_survives_reload_without_reconstruction(client, ctx):
    run = _legacy_follow_up_run()
    client.get("/headquarters")
    operation = Operation.query.one()
    client.post(f"/operations/{operation.id}/founder-decision", data={"action": "APPROVE"})
    reloaded = client.get(f"/headquarters/missions/{operation.id}").get_data(as_text=True)
    db.session.refresh(operation)
    assert operation.status == "RUNNING"
    assert Operation.query.count() == 1
    assert KnowledgeItem.query.filter_by(kind="DECISION").count() == 1
    assert "WORK PAUSED AT THE POINT OF AUTHORITY" not in reloaded
    assert operation.memory_json["legacy_source_agent_run_id"] == run.id


def test_budget_guard_persists_new_human_founder_decision(client, ctx, monkeypatch):
    operation = _planned_operation()
    operations.approve(operation)
    model = operation.proposed_by.current_model
    model.input_price_per_million = 1000
    model.output_price_per_million = 1000
    operation.approved_budget_twd = Decimal("0.0001")
    db.session.commit()
    calls = []
    monkeypatch.setattr("eason_one.services.execution.get_provider", lambda *args: calls.append(args))

    try:
        operations.next_step(operation, "budget-regression")
    except ValueError as exc:
        assert "operation budget" in str(exc)
    db.session.refresh(operation)
    step = OperationStep.query.filter_by(operation_id=operation.id).one()
    assert calls == []
    assert step.status == "BLOCKED"
    assert operation.status == "WAITING_FOR_FOUNDER"
    assert operation.founder_report_json["decision_kind"] == "BUDGET_AUTHORIZATION"
    event = operation.memory_json["founder_attention_events"][-1]
    assert event["kind"] == "BUDGET_AUTHORIZATION"
    assert event["status"] == "PENDING"

    for _ in range(2):
        page = client.get(f"/headquarters/missions/{operation.id}").get_data(as_text=True)
        assert "BUDGET AUTHORIZATION" in page
        assert "APPROVE & CONTINUE" in page
        assert "Additional" in page
    assert Operation.query.count() == 1

    client.post(f"/operations/{operation.id}/founder-decision", data={"action": "APPROVE"})
    db.session.refresh(operation)
    assert operation.status == "RUNNING"
    assert operation.memory_json["founder_attention_events"][-1]["status"] == "RESOLVED"
    page = client.get(f"/headquarters/missions/{operation.id}").get_data(as_text=True)
    assert "WORK PAUSED AT THE POINT OF AUTHORITY" not in page


def test_fractional_budget_authorization_resolves_same_step_without_micro_loop(client, ctx, monkeypatch):
    operation = _planned_operation()
    operations.approve(operation)
    operation.approved_budget_twd = Decimal("0.0604")
    operation.project.real_budget_limit = operation.approved_budget_twd
    db.session.commit()
    brain_before = KnowledgeItem.query.count()
    provider_calls = []
    monkeypatch.setattr(
        "eason_one.services.execution.estimate_execution",
        lambda *args, **kwargs: SimpleNamespace(real_cost=Decimal("0.7384354786")),
    )

    class Provider:
        def complete(self, *args, **kwargs):
            provider_calls.append(1)
            return ProviderResult(
                json.dumps({"result_summary": "Evidence reconstructed.", "knowledge_proposals": []}),
                0,
                0,
                response_id="mock-budget",
            )

    monkeypatch.setattr("eason_one.services.execution.get_provider", lambda _: Provider())
    try:
        operations.next_step(operation, "fractional-budget-block")
    except ValueError as exc:
        assert "operation budget" in str(exc)
    else:
        raise AssertionError("The insufficient budget must block execution")
    assert provider_calls == []
    report = operation.founder_report_json
    assert report["additional_budget_exact_twd"] == "0.6780354786"
    assert report["additional_budget_twd"] == "0.6781"
    assert report["resulting_authorized_twd"] == "0.7385"
    page = client.get(f"/headquarters/missions/{operation.id}").get_data(as_text=True)
    assert "Authorized" in page and "Additional" in page and "Resulting total" in page
    assert "0.6780354786" not in page

    response = client.post(f"/operations/{operation.id}/founder-decision", data={"action": "APPROVE"})
    assert response.status_code == 302
    db.session.refresh(operation)
    assert operation.status == "RUNNING"
    assert operation.approved_budget_twd == Decimal("0.7385")
    assert operation.project.real_budget_limit == Decimal("0.7385")
    assert operation.founder_report_json is None
    assert operation.memory_json["founder_attention_events"][-1]["status"] == "RESOLVED"
    assert len(operation.memory_json["founder_attention_events"]) == 1
    assert len(operation.memory_json["founder_decisions"]) == 1
    assert Operation.query.count() == 1
    assert KnowledgeItem.query.count() == brain_before

    result = operations.next_step(operation, "fractional-budget-resume")
    assert result["kind"] == "TASK"
    assert provider_calls == [1]
    assert len(operation.memory_json["founder_attention_events"]) == 1


def test_planned_operation_renders_real_founder_decision_controls(client, ctx):
    operation = _planned_operation()
    page = client.get(f"/headquarters/missions/{operation.id}").get_data(as_text=True)
    assert "WORK PAUSED AT THE POINT OF AUTHORITY" in page
    assert operation.objective in page
    assert f'/operations/{operation.id}/founder-decision' in page
    assert page.index("REJECT") < page.index("MODIFY") < page.index("APPROVE & CONTINUE")
    assert "MISSION SPINE" in page


def test_operation_page_uses_mission_room_and_scoped_drawers(client, ctx):
    operation = _planned_operation()
    operations.approve(operation)
    page = client.get(f"/headquarters/missions/{operation.id}").get_data(as_text=True)
    assert 'hq-mission-page' in page
    assert "MISSION SPINE" in page
    assert "EXECUTION FLOOR" in page
    assert "ARTIFACT BAY" in page
    assert "ACCEPTANCE CONTRACT" in page
    assert 'data-context-drawer' in page


def test_headquarters_navigation_replaces_legacy_sidebar(client):
    page = client.get("/headquarters").get_data(as_text=True)
    assert 'class="hq-nav"' in page
    for path in (
        "/headquarters/missions",
        "/headquarters/people",
        "/headquarters/meetings",
        "/headquarters/results",
        "/headquarters/memory",
        "/headquarters/finance",
        "/headquarters/system",
    ):
        assert f'href="{path}"' in page
    for path in ("/command", "/work", "/team", "/company"):
        assert f'href="{path}"' not in page


def test_founder_approve_is_authoritative_persistent_and_clears_attention(client, ctx):
    operation = _planned_operation()
    response = client.post(f"/operations/{operation.id}/founder-decision", data={"action": "APPROVE"})
    assert response.status_code == 302
    db.session.refresh(operation)
    assert operation.status == "RUNNING" and operation.approved_at
    assert len(operation.tasks) == 1
    decision = operation.memory_json["founder_decisions"][-1]
    assert decision["authority"] == "FOUNDER"
    assert decision["action"] == "APPROVE"
    brain = KnowledgeItem.query.filter_by(kind="DECISION").one()
    assert brain.founder_approved and brain.project_id == operation.project_id
    page = client.get(f"/headquarters/missions/{operation.id}").get_data(as_text=True)
    assert "WORK PAUSED AT THE POINT OF AUTHORITY" not in page


def test_founder_modify_persists_corrected_constraints_used_downstream(client, ctx):
    operation = _planned_operation()
    response = client.post(
        f"/operations/{operation.id}/founder-decision",
        data={
            "action": "MODIFY",
            "reason": "Narrow the evidence gate.",
            "objective": "Validate reconstructed evidence with two checks.",
            "budget_twd": "3.25",
            "completion_criteria": "Two checks pass\nDecision memo is reviewable",
        },
    )
    assert response.status_code == 302
    db.session.refresh(operation)
    plan = operation.plan_json["operation"]
    assert operation.status == "RUNNING"
    assert operation.objective == "Validate reconstructed evidence with two checks."
    assert str(operation.approved_budget_twd) == "3.2500"
    assert plan["completion_criteria"] == ["Two checks pass", "Decision memo is reviewable"]
    assert operation.project.objective == operation.objective
    record = operation.memory_json["founder_decisions"][-1]
    assert record["action"] == "MODIFY"
    assert record["changes"]["completion_criteria"] == plan["completion_criteria"]
    assert KnowledgeItem.query.filter_by(kind="DECISION").count() == 1


def test_founder_reject_persists_and_cannot_execute(client, ctx):
    operation = _planned_operation()
    response = client.post(
        f"/operations/{operation.id}/founder-decision",
        data={"action": "REJECT", "reason": "Evidence scope is not approved."},
    )
    assert response.status_code == 302
    db.session.refresh(operation)
    assert operation.status == "TERMINATED_BY_FOUNDER"
    assert operation.tasks == []
    assert operation.memory_json["founder_decisions"][-1]["action"] == "REJECT"
    assert KnowledgeItem.query.filter_by(kind="DECISION").one().content == "Evidence scope is not approved."
    page = client.get(f"/headquarters/missions/{operation.id}").get_data(as_text=True)
    assert "WORK PAUSED AT THE POINT OF AUTHORITY" not in page


def test_waiting_approve_persists_additional_authority_and_resumes(client, ctx):
    operation = _planned_operation()
    operations.approve(operation)
    operations.wait_for_founder(operation, "Additional verification authority is required.", "0.75")
    before = operation.approved_budget_twd
    response = client.post(f"/operations/{operation.id}/founder-decision", data={"action": "APPROVE"})
    assert response.status_code == 302
    db.session.refresh(operation)
    assert operation.status == "RUNNING"
    assert operation.approved_budget_twd == before + Decimal("0.75")
    assert operation.project.real_budget_limit == operation.approved_budget_twd


def test_truncated_response_is_failure_not_authority(client, ctx, monkeypatch):
    ceo = Employee.query.filter_by(slug="ceo").one()
    partial = '{"mode":"OPERATION_PLAN","executive_response":"partial"'

    class Truncated:
        def complete(self, *args, **kwargs):
            return ProviderResult(
                partial,
                100,
                50,
                status="incomplete",
                incomplete_reason="max_output_tokens",
                response_id="partial",
            )

    monkeypatch.setattr("eason_one.services.execution.get_provider", lambda _: Truncated())
    before_brain = KnowledgeItem.query.count()
    run, created = founder_request(ceo, "Continue Learning Evidence Reconstruction after approval.")
    assert created is None
    assert run.status == "FAILED" and run.failure_reason == "OUTPUT_TRUNCATED"
    assert Operation.query.count() == 0
    assert KnowledgeItem.query.count() == before_brain
    page = client.get("/headquarters/attention").get_data(as_text=True)
    assert "OUTPUT TRUNCATED" in page
    assert "RUNTIME FAILURES" in page
    assert "GOVERNANCE GATES" in page
