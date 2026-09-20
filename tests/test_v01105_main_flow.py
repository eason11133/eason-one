import json
from decimal import Decimal

from eason_one.extensions import db
from eason_one.models import AgentRun, Employee, Operation, Project, WorkMessage
from eason_one.services.ceo import founder_request
from eason_one.services.headquarters import route_ceo_intent
from eason_one.services.operation_runtime import run_until_gate
from eason_one.services.stabilization import REAL_WORK, SYSTEM_VALIDATION, operation_kind


def _validation_operation(title="Eason One Mission Room Live Execution Monitor Read-Only Validation"):
    ceo=Employee.query.filter_by(slug="ceo").one()
    project=Project.query.filter_by(environment="LIVE").first()
    if project is None:
        project=Project(name="Main flow test project",objective="Exercise real work",status="ACTIVE",priority="HIGH",environment="LIVE",origin="TEST",owner_employee_id=ceo.id)
        db.session.add(project); db.session.flush()
    row=Operation(
        title=title, objective="System validation history", project_id=project.id,
        proposed_by_employee_id=ceo.id, status="WAITING_FOR_FOUNDER",
        plan_json={"completion_criteria":["Evidence"]},
        approved_budget_twd=Decimal("0"), actual_cost_twd=Decimal("0"),
        memory_json={"mission_kind":SYSTEM_VALIDATION},
    )
    db.session.add(row); db.session.commit()
    return row


def _engineering_plan(engineer_id):
    return {
        "mode":"OPERATION_PLAN",
        "executive_response":"I recommend one bounded Engineer operation using local Codex, with no Meeting or paid reviewer.",
        "operation":{
            "title":"Remove SQLAlchemy Query.get legacy warnings",
            "objective":"Replace only legacy Query.get usage with Session.get and preserve behavior.",
            "project_id":None,
            "budget_twd":"0.0001",
            "tasks":[{
                "title":"Remove Query.get warnings",
                "objective":"Make the bounded repository change, run affected tests, then run the full suite.",
                "assignee_employee_id":engineer_id,
                "reviewer_employee_id":engineer_id,
                "acceptance_criteria":["Existing tests pass", "Query.get warning is eliminated"],
            }],
            "meeting_policy":"NEVER",
            "meeting_config":{
                "trigger":"NEVER", "participant_employee_ids":[],
                "max_rounds":1, "max_speakers_per_round":1,
                "contribution_output_cap":256, "token_limit":6000,
                "budget_twd":0, "retry_limit":0,
            },
            "completion_criteria":["Engineer result and test evidence are persisted", "CEO delivery is available"],
        },
    }


def _fake_ceo_execute(monkeypatch, captured):
    def fake_execute(employee, purpose, user_request, **kwargs):
        assert purpose=="CEO_FOUNDER_REQUEST"
        captured["context"]=kwargs.get("context_override") or ""
        plan=_engineering_plan(Employee.query.filter_by(slug="engineer").one().id)
        model=employee.current_model
        run=AgentRun(
            employee_id=employee.id, model_config_id=model.id,
            purpose=purpose, user_request=user_request,
            system_prompt_snapshot=kwargs.get("system_prompt_override") or "test",
            context_snapshot=captured["context"], raw_output=json.dumps(plan),
            status="SUCCEEDED", provider_key_snapshot=model.provider_key,
            model_name_snapshot=model.model_name,
            input_price_snapshot=model.input_price_per_million,
            output_price_snapshot=model.output_price_per_million,
            currency_snapshot=model.currency, currency=model.currency,
            input_tokens=100, output_tokens=100, real_cost=Decimal("0"),
        )
        db.session.add(run); db.session.commit()
        return run
    monkeypatch.setattr("eason_one.services.ceo.execute",fake_execute)


def test_new_work_ignores_validation_and_unrelated_missions(ctx, monkeypatch):
    validation=_validation_operation()
    ceo=Employee.query.filter_by(slug="ceo").one()
    project=Project.query.filter_by(environment="LIVE").first()
    unrelated=Operation(
        title="Unrelated real Mission", objective="Do something else", project_id=project.id,
        proposed_by_employee_id=ceo.id, status="PAUSED",
        plan_json={"completion_criteria":["Other"]}, approved_budget_twd=0,
        actual_cost_twd=0, memory_json={"mission_kind":REAL_WORK},
    )
    db.session.add(unrelated); db.session.commit()
    captured={}
    _fake_ceo_execute(monkeypatch,captured)

    run, operation=founder_request(ceo,"Fix the SQLAlchemy Query.get warnings as one bounded engineering task.")

    assert run.status=="SUCCEEDED"
    assert operation.status=="PLANNED"
    assert operation_kind(operation)==REAL_WORK
    assert validation.title not in captured["context"]
    assert unrelated.title not in captured["context"]
    assert operation.id not in {validation.id,unrelated.id}


def test_ceo_proposal_survives_refresh_with_action_link(client, ctx, monkeypatch):
    _validation_operation()
    captured={}
    _fake_ceo_execute(monkeypatch,captured)

    response=client.post("/headquarters/ceo/execute",json={
        "request":"Fix the SQLAlchemy Query.get warnings as one bounded engineering task.",
        "mode":"ACT",
    })
    assert response.status_code==200
    payload=response.get_json()
    operation_id=payload["answer"]["proposal"]["operation_id"]
    assert payload["proposal_type"]=="OPERATION"

    page=client.get("/headquarters").get_data(as_text=True)
    assert "I recommend one bounded Engineer operation" in page
    assert f'/headquarters/missions/{operation_id}' in page
    assert "REVIEW PROPOSAL →" in page
    rows=WorkMessage.query.order_by(WorkMessage.id).all()
    assert [row.message_type for row in rows[-2:]]==["FOUNDER_TO_CEO","CEO_TO_FOUNDER"]
    assert rows[-1].agent_run_id==payload["run_id"]


def test_founder_ceo_engineer_codex_delivery_chain(ctx, monkeypatch):
    _validation_operation()
    captured={}
    _fake_ceo_execute(monkeypatch,captured)
    ceo=Employee.query.filter_by(slug="ceo").one()
    run, operation=founder_request(ceo,"Fix the SQLAlchemy Query.get warnings as one bounded engineering task.")

    from eason_one.services.operations import approve
    approve(operation)
    result=run_until_gate(operation.id,max_steps=12)
    db.session.expire_all()
    operation=db.session.get(Operation,operation.id)
    execution_runs=AgentRun.query.filter_by(operation_id=operation.id,purpose="TASK_EXECUTION").all()

    assert result["state"] in {"COMPLETED","PAUSED"}
    assert execution_runs
    assert execution_runs[0].provider_key_snapshot=="codex"
    assert execution_runs[0].structured_validation_status=="PASSED"
    assert operation_kind(operation)==REAL_WORK
    assert operation.status in {"COMPLETED","PAUSED"}
    assert operation.tasks[0].result_summary


def test_exact_warning_cleanup_request_routes_to_governed_work(ctx):
    routed=route_ceo_intent(
        "Remove the current SQLAlchemy LegacyAPIWarning caused by Query.get() in Eason One. "
        "This is a bounded real Engineering task."
    )
    assert routed["route"]=="ACT"
