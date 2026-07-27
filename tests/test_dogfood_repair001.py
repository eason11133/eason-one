import json
from decimal import Decimal

from eason_one.extensions import db
from eason_one.models import (
    AgentRun, CostEvent, Employee, KnowledgeItem, Meeting, ModelConfig,
    Operation, Project, Proposal, Task,
)
from eason_one.providers import ProviderResult
from eason_one.seed import ensure_hr
from eason_one.services import command
from eason_one.services.brain import current
from eason_one.services.ceo import founder_request
from eason_one.services.ceo_context import compose
from eason_one.services.contributions import meaningful_total


def _ceo():
    return Employee.query.filter_by(slug="ceo").one()


def _operation_plan():
    worker=Employee.query.filter_by(slug="researcher").one()
    reviewer=Employee.query.filter_by(slug="research-director").one()
    return {
      "mode":"OPERATION_PLAN","executive_response":"I prepared bounded work.",
      "operation":{"title":"Learning evidence","objective":"Validate learning.",
        "project_id":None,"budget_twd":10,
        "tasks":[{"title":"Collect evidence","objective":"Collect evidence.",
          "assignee_employee_id":worker.id,
          "reviewer_employee_id":reviewer.id,
          "acceptance_criteria":["Evidence is reviewable"]}],
        "meeting_policy":"REQUIRED_ON_MATERIAL_CONFLICT",
        "completion_criteria":["Evidence is accepted"]}}


def test_ceo_operation_plan_uses_purpose_aware_output_budget(ctx, monkeypatch):
    ceo=_ceo()
    ceo.current_model.max_output_tokens=4096
    db.session.commit()
    seen=[]
    class Provider:
        def complete(self, model, system, user, context, maximum, schema=None):
            seen.append(maximum)
            return ProviderResult(json.dumps(_operation_plan()),20,20)
    monkeypatch.setattr("eason_one.services.execution.get_provider",lambda _:Provider())
    run, operation=founder_request(ceo,"Prepare a learning evidence operation")
    assert run.status=="SUCCEEDED" and seen==[2048]
    assert operation.status=="PLANNED"


def test_truncated_paid_founder_request_is_preserved_without_ghost_state(
    ctx, monkeypatch
):
    ceo=_ceo()
    ceo.current_model.provider_key="openai"
    ceo.current_model.input_price_per_million=Decimal("10")
    ceo.current_model.output_price_per_million=Decimal("20")
    ceo.current_model.max_output_tokens=2048
    db.session.commit()
    partial='{"mode":"OPERATION_PLAN","executive_response":"Useful partial advice"'
    class Provider:
        def complete(self,*args):
            return ProviderResult(
              partial,100,40,response_id="resp-dogfood",
              request_id="req-dogfood",status="incomplete",
              incomplete_reason="max_output_tokens")
    monkeypatch.setattr("eason_one.services.execution.get_provider",lambda _:Provider())
    run, created=founder_request(ceo,"Plan learning evidence")
    assert created is None and Operation.query.count()==0 and Proposal.query.count()==0
    assert run.failure_reason=="OUTPUT_TRUNCATED" and run.raw_output==partial
    assert CostEvent.query.filter_by(agent_run_id=run.id).count()==1
    snapshot=command.snapshot()
    assert snapshot["executive_state"]=="UNRESOLVED_FAILURE"
    assert "Useful partial advice" in snapshot["report"]["summary"]
    assert command.work_snapshot()["unresolved_events"]==[run]


def test_non_live_history_is_not_current_context_or_attention(ctx):
    ceo=_ceo()
    legacy=Project(name="Beauty Consultation",objective="Old smoke test",
      owner_employee_id=ceo.id,status="ACTIVE",environment="SMOKE")
    db.session.add(legacy); db.session.flush()
    db.session.add(Task(project_id=legacy.id,title="Old beauty work",
      objective="Legacy",status="WORKING",assigned_employee_id=ceo.id))
    old=AgentRun(employee_id=ceo.id,project_id=legacy.id,
      model_config_id=ceo.current_model_config_id,purpose="CEO_FOUNDER_REQUEST",
      user_request="Beauty Consultation history",
      system_prompt_snapshot="s",context_snapshot="c",raw_output="{}",
      parsed_output_json={"executive_response":"Stale Beauty advice"},
      status="SUCCEEDED",provider_key_snapshot="mock",
      model_name_snapshot="mock",input_price_snapshot=0,
      output_price_snapshot=0,currency_snapshot="TWD")
    db.session.add(old); db.session.flush()
    db.session.add(Proposal(project_id=legacy.id,agent_run_id=old.id,
      proposed_by_employee_id=ceo.id,payload_json={"type":"KNOWLEDGE"},
      status="PENDING"))
    db.session.commit()
    context=compose(ceo,founder_request="Prepare Learning Evidence")
    assert "Beauty Consultation" not in context.text
    assert command.snapshot()["executive_state"]=="IDLE"
    assert command.employee_view(ceo)["current_work"] is None


def test_command_centers_latest_response_and_work_failure(client, ctx):
    ceo=_ceo()
    run=AgentRun(employee_id=ceo.id,model_config_id=ceo.current_model_config_id,
      purpose="CEO_FOUNDER_REQUEST",user_request="Status",
      system_prompt_snapshot="s",context_snapshot="c",raw_output="{}",
      parsed_output_json={"mode":"ADVISORY","executive_response":"Central answer"},
      status="SUCCEEDED",provider_key_snapshot="mock",model_name_snapshot="mock",
      input_price_snapshot=0,output_price_snapshot=0,currency_snapshot="TWD")
    db.session.add(run); db.session.commit()
    page=client.get("/command").get_data(as_text=True)
    assert "Central answer" in page
    assert "The company currently has no active work." not in page


def test_planned_meeting_redirects_to_visible_zero_call_state(client, ctx):
    ceo=_ceo()
    response=client.post("/meetings",data={
      "title":"Dogfood plan","mission_context":"Plan only",
      "meeting_question":"What next?","purpose":"Meeting mission","agenda":"",
      "participant_ids":[str(ceo.id)],"chair_employee_id":str(ceo.id),
      "project_id":"","execution_profile":"ECONOMY"})
    meeting=Meeting.query.one()
    assert response.location.endswith(f"/meetings/{meeting.id}")
    assert meeting.status=="PLANNED" and AgentRun.query.count()==0
    assert "PLANNED / READY TO START" in client.get("/meetings").get_data(as_text=True)


def test_hr_reuses_existing_real_model_and_ui_is_founder_readable(client, ctx):
    real=ModelConfig(label="Existing real model",provider_key="openai",
      model_name="existing",input_price_per_million=1,
      output_price_per_million=1,currency="TWD",max_output_tokens=2048)
    db.session.add(real); db.session.commit()
    hr=ensure_hr()
    assert hr.department.name=="Human Resources"
    assert hr.current_model==real
    page=client.get("/team/hr").get_data(as_text=True)
    assert "Capability or role needed" in page
    assert "Advanced staffing context" in page
    assert "Low — can wait" in page
    assert "No verified candidates are currently available." in page


def test_inbox_and_brain_scope_and_contribution_truth(client, ctx):
    ceo=_ceo()
    company=KnowledgeItem(kind="FACT",title="Company fact",content="Company",
      founder_approved=True)
    project=Project(name="Live",objective="Live",owner_employee_id=ceo.id,
      environment="LIVE")
    db.session.add_all([company,project]); db.session.flush()
    scoped=KnowledgeItem(kind="FACT",title="Project fact",content="Project",
      project_id=project.id,founder_approved=True)
    run=AgentRun(employee_id=ceo.id,model_config_id=ceo.current_model_config_id,
      purpose="CEO_PROJECT_SYNTHESIS",user_request="Synthesize",
      system_prompt_snapshot="s",context_snapshot="c",raw_output="{}",
      status="SUCCEEDED",provider_key_snapshot="mock",model_name_snapshot="mock",
      input_price_snapshot=0,output_price_snapshot=0,currency_snapshot="TWD")
    db.session.add_all([scoped,run]); db.session.flush()
    pending=Proposal(agent_run_id=run.id,proposed_by_employee_id=ceo.id,
      payload_json={"type":"KNOWLEDGE","title":"Pending"},status="PENDING")
    history=Proposal(agent_run_id=run.id,proposed_by_employee_id=ceo.id,
      payload_json={"type":"KNOWLEDGE","title":"Old"},status="APPROVED")
    db.session.add_all([pending,history]); db.session.commit()
    assert current()==[company]
    assert meaningful_total(ceo.id)==1
    inbox=client.get("/inbox").get_data(as_text=True)
    assert inbox.index("Pending") < inbox.index("HISTORY")
    assert "Advanced audit" in inbox
