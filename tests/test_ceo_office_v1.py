from datetime import timedelta
from decimal import Decimal

from eason_one.extensions import db
from eason_one.models import AgentRun, CostEvent, Employee, Operation, now
from eason_one.services import command, operations
from eason_one.services.company import get_company


def _operation():
    ceo=Employee.query.filter_by(slug="ceo").one()
    employees=Employee.query.filter(
      Employee.slug.in_(["researcher","critic","research-director"])
    ).order_by(Employee.id).all()
    plan={"mode":"OPERATION_PLAN","executive_response":"Persisted plan response.",
      "operation":{"title":"Commercial validation","objective":"Validate demand.",
        "project_id":None,"budget_twd":2,
        "tasks":[
          {"title":"Research evidence","objective":"Collect evidence.",
           "assignee_employee_id":employees[0].id,
           "reviewer_employee_id":employees[2].id,
           "acceptance_criteria":["Evidence exists"]},
          {"title":"Critique evidence","objective":"Challenge evidence.",
           "assignee_employee_id":employees[1].id,
           "reviewer_employee_id":employees[2].id,
           "acceptance_criteria":["Risks exist"]},
          {"title":"Synthesize evidence","objective":"Produce result.",
           "assignee_employee_id":employees[2].id,
           "reviewer_employee_id":ceo.id,
           "acceptance_criteria":["Result exists"]}],
        "meeting_policy":"REQUIRED_ON_MATERIAL_CONFLICT",
        "completion_criteria":["Demand is supported"]}}
    return operations.approve(operations.propose_operation(ceo,plan))


def _run(operation, employee, status="SUCCEEDED", started_at=None):
    run=AgentRun(
      employee_id=employee.id,operation_id=operation.id,
      project_id=operation.project_id,model_config_id=employee.current_model.id,
      purpose="TASK_EXECUTION",user_request="Persisted work",
      system_prompt_snapshot="system",context_snapshot="context",
      raw_output="{}",status=status,input_tokens=3000,output_tokens=1000,
      real_cost=Decimal("0.62"),provider_key_snapshot="mock",
      model_name_snapshot="mock",input_price_snapshot=0,
      output_price_snapshot=0,currency_snapshot="TWD",
      started_at=started_at or now())
    db.session.add(run); db.session.flush()
    return run


def test_idle_command_is_quiet_full_canvas_and_zero_provider_calls(
    client, ctx, monkeypatch
):
    calls=[]
    monkeypatch.setattr(
      "eason_one.services.execution.get_provider",
      lambda *args,**kwargs:calls.append((args,kwargs)))
    page=client.get("/command").get_data(as_text=True)
    assert calls==[]
    assert "ceo-canvas" in page and "ceo-composer" in page
    assert "Good evening" not in page
    assert "what would you like us to do" not in page
    assert "CEO output was preserved" not in page


def test_active_operation_briefing_uses_persisted_work_usage_and_cost(
    client, ctx, monkeypatch
):
    operation=_operation()
    statuses=["DONE","WORKING","ASSIGNED"]
    for task,status in zip(operation.tasks,statuses):
        task.status=status
    run=_run(operation,operation.tasks[0].assigned_employee)
    db.session.add(CostEvent(
      company_id=get_company().id,employee_id=run.employee_id,
      project_id=operation.project_id,operation_id=operation.id,
      agent_run_id=run.id,category="MODEL_USAGE",description="Persisted usage",
      real_cost_delta=Decimal("0.62"),internal_credits_delta=0))
    db.session.commit()
    calls=[]
    monkeypatch.setattr(
      "eason_one.services.execution.get_provider",
      lambda *args,**kwargs:calls.append((args,kwargs)))
    page=client.get("/command").get_data(as_text=True)
    assert calls==[]
    for value in (
      "Validate demand.","1 / 3","4,000","NT$ 0.62 / NT$ 2.00",
      "Research evidence","Critique evidence","Synthesize evidence",
      "DONE","WORKING","ASSIGNED","CURRENT EXECUTION"):
        assert value in page


def test_new_success_is_primary_and_old_failure_is_secondary(client, ctx):
    ceo=Employee.query.filter_by(slug="ceo").one()
    failed=AgentRun(
      employee_id=ceo.id,model_config_id=ceo.current_model.id,
      purpose="CEO_FOUNDER_REQUEST",user_request="Old request",
      system_prompt_snapshot="s",context_snapshot="c",status="FAILED",
      failure_reason="OUTPUT_TRUNCATED",real_cost=Decimal(".10"),
      provider_key_snapshot="mock",model_name_snapshot="mock",
      input_price_snapshot=0,output_price_snapshot=0,currency_snapshot="TWD",
      started_at=now()-timedelta(minutes=2))
    success=AgentRun(
      employee_id=ceo.id,model_config_id=ceo.current_model.id,
      purpose="CEO_FOUNDER_REQUEST",user_request="New request",
      system_prompt_snapshot="s",context_snapshot="c",status="SUCCEEDED",
      parsed_output_json={"mode":"STATUS_QUERY",
        "executive_response":"Persisted current CEO intelligence."},
      provider_key_snapshot="mock",model_name_snapshot="mock",
      input_price_snapshot=0,output_price_snapshot=0,currency_snapshot="TWD",
      started_at=now())
    db.session.add_all([failed,success]); db.session.commit()
    page=client.get("/command").get_data(as_text=True)
    assert page.index("Persisted current CEO intelligence.") < page.index(
      "PREVIOUS UNRESOLVED REQUEST")
    assert page.count("Persisted current CEO intelligence.")>=1
    assert "UNRESOLVED FAILURE" not in page


def test_completed_operation_renders_persisted_result_without_provider(
    client, ctx, monkeypatch
):
    operation=_operation()
    for task in operation.tasks:
        task.status="DONE"
    operation.status="COMPLETED"
    operation.memory_json={"goal_verification":{
      "overall_status":"SATISFIED","criteria":[]}}
    operation.founder_report_json={
      "headline":"Persisted completion headline.",
      "summary":"Persisted executive result.",
      "result":"Persisted evidence result."}
    db.session.commit()
    calls=[]
    monkeypatch.setattr(
      "eason_one.services.execution.get_provider",
      lambda *args,**kwargs:calls.append((args,kwargs)))
    page=client.get("/command").get_data(as_text=True)
    assert calls==[]
    for value in (
      "COMPLETED","Persisted completion headline.",
      "Persisted executive result.","Persisted evidence result.","SATISFIED"):
        assert value in page


def test_command_snapshot_is_deterministic(ctx, monkeypatch):
    monkeypatch.setattr(
      "eason_one.services.execution.get_provider",
      lambda *args,**kwargs:(_ for _ in ()).throw(
        AssertionError("provider must not be used")))
    result=command.snapshot()
    assert result["executive_state"]=="IDLE"
