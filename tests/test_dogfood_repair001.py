import json
from decimal import Decimal

from eason_one.extensions import db
from eason_one.models import (
    AgentRun, ContributionEvent, CostEvent, Employee, KnowledgeItem, Meeting,
    MeetingStep, ModelConfig, Operation, Project, Proposal, Task,
)
from eason_one.providers import ProviderResult
from eason_one.seed import ensure_hr
from eason_one.services import command
from eason_one.services.brain import current
from eason_one.services.ceo import founder_request
from eason_one.services.ceo_context import compose
from eason_one.services.contributions import meaningful_total
from eason_one.services import workforce


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


def test_simple_and_structured_founder_requests_use_distinct_caps(
    ctx, monkeypatch
):
    ceo=_ceo()
    ceo.current_model.max_output_tokens=4096
    db.session.commit()
    seen=[]
    class Provider:
        def complete(self, model, system, user, context, maximum, schema=None):
            seen.append(maximum)
            payload=({"mode":"STATUS_QUERY","executive_response":"All clear.",
              "project":None,"project_id":None,"tasks":[],"operation":None}
              if "status" in user.lower() else _operation_plan())
            return ProviderResult(json.dumps(payload),20,20)
    monkeypatch.setattr("eason_one.services.execution.get_provider",lambda _:Provider())
    founder_request(ceo,"Give me company status")
    founder_request(ceo,"Prepare a realistic multi-task learning operation")
    assert seen==[768,2048]


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


def test_unresolved_failure_survives_unrelated_success_until_acknowledged(
    client, ctx
):
    ceo=_ceo()
    failed=AgentRun(employee_id=ceo.id,model_config_id=ceo.current_model.id,
      purpose="CEO_FOUNDER_REQUEST",user_request="Create plan",
      system_prompt_snapshot="s",context_snapshot="c",raw_output='{"partial":',
      status="FAILED",failure_reason="OUTPUT_TRUNCATED",real_cost=Decimal(".10"),
      provider_key_snapshot="openai",model_name_snapshot="paid",
      input_price_snapshot=1,output_price_snapshot=1,currency_snapshot="TWD")
    success=AgentRun(employee_id=ceo.id,model_config_id=ceo.current_model.id,
      purpose="CEO_FOUNDER_REQUEST",user_request="Company status",
      system_prompt_snapshot="s",context_snapshot="c",raw_output="{}",
      parsed_output_json={"mode":"STATUS_QUERY","executive_response":"All clear"},
      status="SUCCEEDED",real_cost=0,provider_key_snapshot="mock",
      model_name_snapshot="mock",input_price_snapshot=0,
      output_price_snapshot=0,currency_snapshot="TWD")
    db.session.add_all([failed,success]); db.session.commit()
    snapshot=command.snapshot()
    assert snapshot["report"]["summary"]=="All clear"
    assert snapshot["unresolved_attention"]==[failed]
    response=client.post(f"/command/failures/{failed.id}/acknowledge")
    assert response.status_code==302
    assert failed.status=="FAILED" and failed.raw_output=='{"partial":'
    assert failed.resolution_status=="ACKNOWLEDGED"
    snapshot=command.snapshot()
    assert snapshot["report"]["summary"]=="All clear"
    assert snapshot["unresolved_attention"]==[]


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


def test_context_keeps_only_target_project_or_active_operation_history(ctx):
    ceo=_ceo()
    target=Project(name="Target Learning",objective="Target",
      owner_employee_id=ceo.id,environment="LIVE")
    unrelated=Project(name="Other Live",objective="Other",
      owner_employee_id=ceo.id,environment="LIVE")
    db.session.add_all([target,unrelated]); db.session.flush()
    for project,text in ((target,"TARGET MEMORY"),(unrelated,"UNRELATED MEMORY")):
        db.session.add(AgentRun(employee_id=ceo.id,project_id=project.id,
          model_config_id=ceo.current_model.id,purpose="CEO_FOUNDER_REQUEST",
          user_request=text,system_prompt_snapshot="s",context_snapshot="c",
          raw_output="{}",parsed_output_json={"executive_response":text},
          status="SUCCEEDED",provider_key_snapshot="mock",
          model_name_snapshot="mock",input_price_snapshot=0,
          output_price_snapshot=0,currency_snapshot="TWD"))
    db.session.commit()
    text=compose(ceo,founder_request="Continue Target Learning",
      project=target).text
    assert "TARGET MEMORY" in text and "UNRELATED MEMORY" not in text


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
    client.get(response.location)
    assert Meeting.query.count()==1 and AgentRun.query.count()==0


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


def test_two_field_hr_request_is_accepted_without_staffing_schema(
    client, ctx
):
    response=client.post("/team/hr/requests",data={
      "role_needed":"Product quality / QA",
      "problem":"Identify failures before the Founder encounters them.",
    })
    assert response.status_code==302
    request=__import__(
      "eason_one.models",fromlist=["HiringRequest"]).HiringRequest.query.one()
    assert request.role_needed=="Product quality / QA"
    assert request.hr_agent_run_id is not None
    assert Employee.query.filter_by(hiring_request_id=request.id).count()==0


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


def test_non_live_pending_proposal_is_history_not_current_authority(
    client, ctx
):
    ceo=_ceo()
    legacy=Project(name="Beauty Legacy",objective="Old",
      owner_employee_id=ceo.id,environment="SMOKE")
    db.session.add(legacy); db.session.flush()
    run=AgentRun(employee_id=ceo.id,project_id=legacy.id,
      model_config_id=ceo.current_model.id,purpose="TASK_EXECUTION",
      user_request="Old",system_prompt_snapshot="s",context_snapshot="c",
      status="SUCCEEDED",provider_key_snapshot="mock",model_name_snapshot="mock",
      input_price_snapshot=0,output_price_snapshot=0,currency_snapshot="TWD")
    db.session.add(run); db.session.flush()
    db.session.add(Proposal(project_id=legacy.id,agent_run_id=run.id,
      proposed_by_employee_id=ceo.id,payload_json={
        "type":"KNOWLEDGE","title":"Legacy Beauty Proposal"},
      status="PENDING"))
    db.session.commit()
    page=client.get("/inbox").get_data(as_text=True)
    primary=page.split("HISTORY",1)[0]
    assert "Legacy Beauty Proposal" not in primary
    assert "Legacy Beauty Proposal" in page.split("HISTORY",1)[1]


def test_contribution_counts_completed_and_recovered_work_not_failures(ctx):
    ceo=_ceo()
    db.session.add(ContributionEvent(employee_id=ceo.id,scope="COMPANY",
      event_type="TASK_ACCEPTED",value=1,reason="Accepted Task",
      related_task_id=None,related_reference="task:1",status="FINAL"))
    succeeded=AgentRun(employee_id=ceo.id,model_config_id=ceo.current_model.id,
      purpose="MEETING_CONTRIBUTION",user_request="Contribute",
      system_prompt_snapshot="s",context_snapshot="c",raw_output="{}",
      status="SUCCEEDED",provider_key_snapshot="mock",model_name_snapshot="mock",
      input_price_snapshot=0,output_price_snapshot=0,currency_snapshot="TWD")
    failed=AgentRun(employee_id=ceo.id,model_config_id=ceo.current_model.id,
      purpose="CEO_FOUNDER_REQUEST",user_request="Failed",
      system_prompt_snapshot="s",context_snapshot="c",raw_output="",
      status="FAILED",real_cost=1,provider_key_snapshot="openai",
      model_name_snapshot="paid",input_price_snapshot=1,
      output_price_snapshot=1,currency_snapshot="TWD")
    db.session.add_all([succeeded,failed]); db.session.commit()
    assert meaningful_total(ceo.id)==2


def test_shared_founder_ui_scale_is_defined():
    css=open("eason_one/static/app.css",encoding="utf-8").read()
    for token in (
      "--font-meta:11px","--font-label:13px","--font-body:15px",
      "--font-h3:18px","--font-h2:22px","--font-h1:30px",
      "--layout-max:1180px","--reading-max:760px","--section-gap:44px",
    ):
        assert token in css


def test_legacy_luna_upgrade_preserves_historical_run_and_bootstraps_hr(
    ctx, monkeypatch
):
    ceo=_ceo()
    luna=ModelConfig(label="OpenAI GPT-5.6 Luna",provider_key="openai",
      model_name="gpt-5.6-luna",input_price_per_million=1,
      output_price_per_million=2,currency="TWD",max_output_tokens=512)
    claude=ModelConfig(label="Claude",provider_key="anthropic",
      model_name="claude",input_price_per_million=1,
      output_price_per_million=2,currency="TWD",max_output_tokens=4096)
    db.session.add_all([luna,claude]); db.session.flush()
    historical=AgentRun(employee_id=ceo.id,model_config_id=luna.id,
      purpose="CEO_FOUNDER_REQUEST",user_request="Legacy plan",
      system_prompt_snapshot="s",context_snapshot="c",raw_output='{"partial":',
      status="FAILED",failure_reason="OUTPUT_TRUNCATED",
      effective_max_output_tokens=512,real_cost=Decimal("0.144966"),
      provider_key_snapshot="openai",model_name_snapshot="gpt-5.6-luna",
      input_price_snapshot=1,output_price_snapshot=2,currency_snapshot="TWD")
    db.session.add(historical); db.session.commit()
    assert luna.max_output_tokens==512
    from eason_one import _upgrade_v1_database
    _upgrade_v1_database()
    db.session.refresh(luna)
    db.session.refresh(historical)
    assert luna.max_output_tokens==4096
    assert historical.status=="FAILED"
    assert historical.effective_max_output_tokens==512
    assert historical.real_cost==Decimal("0.144966")
    hr=ensure_hr()
    assert hr.active and hr.current_model.provider_key!="mock"
    assert hr.current_model.max_output_tokens>=workforce.HR_ASSESSMENT_OUTPUT_CAP
    seen=[]
    original=workforce.estimate_execution
    def capture(model,system,context,user,maximum,schema):
        seen.append(maximum)
        return original(model,system,context,user,maximum,schema)
    monkeypatch.setattr(workforce,"estimate_execution",capture)
    assert workforce.assessment_authorization(hr) is not None
    assert seen==[workforce.HR_ASSESSMENT_OUTPUT_CAP]


def test_hr_execution_uses_same_1536_contract(ctx, monkeypatch):
    real=ModelConfig(label="Compatible real",provider_key="openai",
      model_name="compatible",input_price_per_million=1,
      output_price_per_million=1,currency="TWD",max_output_tokens=4096)
    db.session.add(real); db.session.commit()
    hr=ensure_hr()
    assert hr.current_model==real
    item=workforce.request_hire(requested_by_type="FOUNDER",
      role_needed="QA",problem="Find failures",why_now="Now",
      responsibilities=["Test"],capabilities=["QA"],urgency="MEDIUM",
      use_frequency="OCCASIONAL")
    from eason_one.providers import MockProvider
    monkeypatch.setattr(
      "eason_one.services.execution.get_provider",lambda _:MockProvider())
    run=workforce.assess_request(item)
    assert run.effective_max_output_tokens==1536


def test_single_live_project_is_not_implicit_target_context(ctx, monkeypatch):
    ceo=_ceo()
    project=Project(name="WildOne",objective="Existing direction",
      owner_employee_id=ceo.id,environment="LIVE",status="ACTIVE")
    db.session.add(project); db.session.commit()
    contexts=[]
    class Provider:
        def complete(self,model,system,user,context,maximum,schema=None):
            contexts.append(context)
            return ProviderResult(json.dumps({
              "mode":"STATUS_QUERY","executive_response":"Response",
              "project":None,"project_id":None,"tasks":[],"operation":None}),1,1)
    monkeypatch.setattr("eason_one.services.execution.get_provider",lambda _:Provider())
    founder_request(ceo,"Evaluate a new Learning Evidence business direction.")
    founder_request(ceo,"How is WildOne going?")
    founder_request(ceo,"Give me company status.")
    assert "Project #"+str(project.id) not in contexts[0]
    assert "Project #"+str(project.id) in contexts[1]
    assert "Project #"+str(project.id) not in contexts[2]
