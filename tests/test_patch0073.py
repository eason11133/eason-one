import json
from decimal import Decimal

import pytest

from eason_one.extensions import db
from eason_one.models import AgentRun, CostEvent, Employee, MeetingStep, now
from eason_one.providers import ProviderResult
from eason_one.services import meetings
from eason_one.services.company import get_company


CONTROL_138=("Define normalization as format/labeling only; log every artifact edit and require "
  "blind audit of 2/10 packages for added narrative content")
BENCHMARK_003={
  "relation":"QUALIFY",
  "core_point":"Fixed rubric helps, but true false-positive risk is founders/team quietly upgrading raw artifacts (editing, phrasing, framing) during 'normalization'.",
  "controls":[
    CONTROL_138,
    "Have applicants self-report post-use whether admissions officer engagement changed due to package vs artifacts alone",
    "Pre-commit acceptance criteria in writing before any prospect is recruited, not adjusted after seeing early results",
  ],
  "risk":"If staff subtly improve narrative quality while 'reconstructing,' payment and use will validate consulting demand, not evidence-reconstruction demand.",
  "evidence_ids":[],
}


def people():
    return [Employee.query.filter_by(slug=slug).one() for slug in ("ceo","critic")]


def running():
    employees=people()
    meeting=meetings.create("Probe","Mission once","Which probe?",employees[0],employees,profile="ECONOMY")
    meetings.start_auto(meeting)
    return meeting


def paid_provider(monkeypatch,response):
    for employee in people():
        employee.current_model.provider_key="anthropic"
        employee.current_model.input_price_per_million=Decimal("10")
        employee.current_model.output_price_per_million=Decimal("20")
    db.session.commit()
    class Provider:
        calls=0
        def complete(self,*args,**kwargs):
            self.calls+=1
            data={"position":"Run the bounded probe.","actions":["Precommit the rubric"],
              "risk":"Narrative leakage.","evidence_ids":[],"confidence":.6} if self.calls==1 else response
            return ProviderResult(json.dumps(data),100,30,response_id=f"r-{self.calls}",
              status="completed",stop_reason="end_turn")
    provider=Provider()
    monkeypatch.setattr("eason_one.services.execution.get_provider",lambda key:provider)
    return provider


def test_benchmark_003_is_valid_with_exact_warning(ctx):
    assert len(CONTROL_138)==138
    meeting=running()
    validation=meetings._validate_response(json.dumps(BENCHMARK_003),meeting)
    assert validation.status=="VALID_WITH_WARNINGS"
    assert validation.warnings==[{"code":"COMPACTNESS_TARGET_EXCEEDED",
      "field":"controls[0]","target":120,"actual":138}]
    assert len(validation.data["controls"])==3


@pytest.mark.parametrize("mutation",[
  lambda d:d.update(relation="UNKNOWN"),
  lambda d:d.update(controls=d["controls"]+["fourth"]),
  lambda d:d.update(evidence_ids=[999999]),
  lambda d:d.update(core_point="x"*601),
])
def test_hard_semantic_and_acceptance_failures_remain_invalid(ctx,mutation):
    meeting=running(); data=json.loads(json.dumps(BENCHMARK_003)); mutation(data)
    with pytest.raises(meetings.StructuredValidationError):
        meetings._validate_response(json.dumps(data),meeting)
    with pytest.raises(meetings.StructuredValidationError):
        meetings._validate_response('{"relation":',meeting)


def test_slight_position_and_risk_overflow_warn_but_runaway_fails(ctx):
    meeting=running()
    data={"position":"p"*181,"actions":["a"*121],"risk":"r"*181,
      "evidence_ids":[],"confidence":.5}
    result=meetings._validate_round1(json.dumps(data),meeting)
    assert result.status=="VALID_WITH_WARNINGS"
    assert {w["field"] for w in result.warnings}=={"position","actions[0]","risk"}
    data["position"]="p"*601
    with pytest.raises(meetings.StructuredValidationError):
        meetings._validate_round1(json.dumps(data),meeting)


def test_two_paid_calls_with_benchmark_warning_end_normally(ctx,monkeypatch):
    meeting=running(); provider=paid_provider(monkeypatch,BENCHMARK_003)
    for index in range(5):
        meetings.next_step_idempotent(meeting,f"warn-{index}")
        if meeting.status=="ENDED": break
    assert meeting.status=="ENDED"
    assert provider.calls==2
    assert AgentRun.query.filter_by(meeting_id=meeting.id,purpose="CHAIR_ROUTER").count()==0
    assert AgentRun.query.filter_by(meeting_id=meeting.id,purpose="MEETING_SYNTHESIS").count()==0
    critic=AgentRun.query.filter_by(meeting_id=meeting.id,employee_id=people()[1].id).one()
    assert critic.structured_validation_status=="VALID_WITH_WARNINGS"
    assert critic.structured_validation_warnings_json[0]["actual"]==138
    assert meeting.minutes_json["responses"][0]["warning_count"]==1
    assert meeting.minutes_json["responses"][0]["controls"][0]==CONTROL_138
    assert meeting.current_summary_json["qualification"]
    assert meeting.current_summary_json["key_disagreement"]=="No explicit material disagreement presented."


def failed_fixture(raw,reason="STRUCTURED_OUTPUT_INVALID",stop="end_turn"):
    meeting=running()
    meetings.next_step(meeting)  # zero-cost CEO contribution
    critic=people()[1]; model=critic.current_model
    run=AgentRun(employee_id=critic.id,meeting_id=meeting.id,model_config_id=model.id,
      purpose="MEETING_CONTRIBUTION",user_request="existing",system_prompt_snapshot="historical",
      context_snapshot="historical",provider_key_snapshot="anthropic",
      model_name_snapshot=model.model_name,input_price_snapshot=Decimal("10"),
      output_price_snapshot=Decimal("20"),currency_snapshot="TWD",currency="TWD",
      raw_output=raw,status="FAILED",failure_reason=reason,provider_stop_reason=stop,
      input_tokens=1491,output_tokens=261,effective_max_output_tokens=512,
      real_cost=Decimal("0.180936"),finished_at=now())
    db.session.add(run); db.session.flush()
    db.session.add(CostEvent(company_id=get_company().id,employee_id=critic.id,
      agent_run_id=run.id,category="MODEL",description="historical paid response",
      real_cost_delta=run.real_cost,currency="TWD"))
    step=MeetingStep(meeting_id=meeting.id,logical_key="ROUND_1_CRITIC_CONTRIBUTION",
      kind="CONTRIBUTION",round_number=1,employee_id=critic.id,status="PAID_FAILED",
      agent_run_id=run.id,error_text="historical strict compactness failure",finished_at=now())
    db.session.add(step); db.session.flush()
    meeting.status="PAUSED"
    meeting.paid_failure_json={"step_id":step.id,"run_id":run.id,"reason":reason,
      "message":"historical failure","employee":"Critic","provider":"anthropic","model":model.model_name,
      "input_tokens":1491,"output_tokens":261,"cost_twd":"0.180936",
      "provider_stop_reason":stop,"retry_estimate_twd":"1","retry_output_cap":512}
    db.session.commit()
    return meeting,run,step


def test_founder_salvages_existing_paid_response_at_zero_cost(ctx):
    meeting,run,step=failed_fixture(json.dumps(BENCHMARK_003))
    calls_before=AgentRun.query.count(); costs_before=CostEvent.query.count()
    original=(run.status,run.failure_reason,run.error_text)
    assert meetings.salvage_preview(meeting)["eligible"] is True
    meetings.accept_existing_response(meeting)
    assert AgentRun.query.count()==calls_before
    assert CostEvent.query.count()==costs_before
    assert (run.status,run.failure_reason,run.error_text)==original
    recovery=MeetingStep.query.filter_by(meeting_id=meeting.id,kind="LOCAL_RECOVERY").one()
    assert recovery.result_json["provider_calls"]==0
    assert recovery.result_json["additional_cost_twd"]=="0"
    assert step.status=="SUCCEEDED"
    meetings.next_step(meeting); meetings.next_step(meeting)
    assert meeting.status=="ENDED"
    assert meeting.minutes_json["responses"][0]["controls"][0]==CONTROL_138


@pytest.mark.parametrize("raw,reason,stop",[
  ('{"relation":',"STRUCTURED_OUTPUT_INVALID","end_turn"),
  (json.dumps(BENCHMARK_003),"OUTPUT_TRUNCATED","max_tokens"),
  (json.dumps({**BENCHMARK_003,"evidence_ids":[999]}),"STRUCTURED_OUTPUT_INVALID","end_turn"),
  (json.dumps({**BENCHMARK_003,"controls":["x"*401]}),"STRUCTURED_OUTPUT_INVALID","end_turn"),
])
def test_local_salvage_fails_closed(ctx,raw,reason,stop):
    meeting,_,_=failed_fixture(raw,reason,stop)
    assert meetings.salvage_preview(meeting) is None
    with pytest.raises((ValueError,meetings.StructuredValidationError)):
        meetings.accept_existing_response(meeting)
    assert meeting.status=="PAUSED"
    assert MeetingStep.query.filter_by(meeting_id=meeting.id,kind="LOCAL_RECOVERY").count()==0


def test_salvage_ui_and_structured_warning_audit(ctx,client):
    meeting,run,_=failed_fixture(json.dumps(BENCHMARK_003))
    room=client.get(f"/meetings/{meeting.id}").get_data(as_text=True)
    assert "ACCEPT EXISTING RESPONSE" in room and "0 API CALLS" in room
    run.structured_validation_status="VALID_WITH_WARNINGS"
    run.structured_validation_warnings_json=[{"code":"COMPACTNESS_TARGET_EXCEEDED",
      "field":"controls[0]","target":120,"actual":138}]
    db.session.commit()
    audit=client.get(f"/runs/{run.id}").get_data(as_text=True)
    assert "VALID_WITH_WARNINGS" in audit
    assert "controls[0]" in audit and "target 120 / actual 138" in audit


def test_active_valid_response_is_locally_recoverable_and_idempotent(ctx):
    valid={**BENCHMARK_003,"controls":["Use a fixed rubric"]}
    meeting,_,_=failed_fixture(json.dumps(valid))
    assert meetings.salvage_preview(meeting)=={"eligible":True,"warning_count":0}
    before=(AgentRun.query.count(),CostEvent.query.count())
    meetings.accept_existing_response(meeting)
    assert (AgentRun.query.count(),CostEvent.query.count())==before
    with pytest.raises(ValueError,match="no paid failed step"):
        meetings.accept_existing_response(meeting)
    assert MeetingStep.query.filter_by(meeting_id=meeting.id,kind="LOCAL_RECOVERY").count()==1


def test_full_terminated_historical_recovery_lifecycle(ctx,client):
    meeting,run,step=failed_fixture(json.dumps(BENCHMARK_003))
    calls_before=AgentRun.query.count(); costs_before=CostEvent.query.count()
    usage_before=meetings.usage(meeting)
    meetings.stop(meeting,"Founder stopped after the historical paid failure.")
    assert meeting.status=="TERMINATED_BY_FOUNDER"
    page=client.get(f"/meetings/{meeting.id}").get_data(as_text=True)
    assert "RECOVER EXISTING INTELLIGENCE" in page
    for invalid_action in ("AUTHORIZE ONE RETRY","RESUME","END MEETING","CONTINUE MEETING"):
        assert invalid_action not in page
    meetings.accept_existing_response(meeting)
    assert meeting.status=="TERMINATED_BY_FOUNDER"
    assert meeting.termination_reason=="Founder stopped after the historical paid failure."
    assert AgentRun.query.count()==calls_before
    assert CostEvent.query.count()==costs_before
    assert meetings.usage(meeting)==usage_before
    assert run.status=="FAILED" and run.failure_reason=="STRUCTURED_OUTPUT_INVALID"
    assert step.status=="SUCCEEDED"
    assert meeting.minutes_json["recovery"]["additional_provider_calls"]==0
    assert meeting.minutes_json["recovery"]["additional_cost_twd"]=="0"
    assert meeting.minutes_json["recovery"]["provider"]=="anthropic"
    assert meeting.current_summary_json["next_step"]=="Meeting remains terminated. Recovered result is available for review."
    with pytest.raises(ValueError):
        meetings.accept_existing_response(meeting)
    recovered=client.get(f"/meetings/{meeting.id}").get_data(as_text=True)
    assert "RECOVERED MEETING RESULT" in recovered
    assert "Original status:" in recovered and "TERMINATED BY FOUNDER" in recovered
    assert "LOCAL · 0 API CALLS" in recovered
    assert "Historical provider:" in recovered
    assert CONTROL_138 in recovered
    assert "Waiting for the next bounded event." not in recovered


def test_recovery_shape_uses_immutable_schema_not_message_count(ctx):
    meeting,run,step=failed_fixture(json.dumps(BENCHMARK_003))
    step.contribution_shape="ROUND1_RESPONSE"
    run.response_schema_snapshot_json={"name":"meeting_same_round_response","schema":{}}
    # Mutable transcript count is deliberately made misleading.
    from eason_one.models import MeetingMessage
    MeetingMessage.query.filter_by(meeting_id=meeting.id,speaker_type="EMPLOYEE").delete()
    db.session.commit()
    validation,shape=meetings._revalidate_failed_contribution(meeting,step,run)
    assert shape=="ROUND1_RESPONSE"
    assert validation.status=="VALID_WITH_WARNINGS"


@pytest.mark.parametrize("value",["","   "])
def test_empty_and_whitespace_items_are_consistently_hard_invalid(ctx,value):
    meeting=running()
    round1={"position":"p","actions":[value],"risk":"r","evidence_ids":[],"confidence":.5}
    response={**BENCHMARK_003,"controls":[value]}
    later={"has_material_contribution":True,"type":"MATERIAL_REFINEMENT",
      "core_point":"x","controls":[value],"risk":"r","evidence_ids":[],"target_message_ids":[]}
    for validator,payload in [
      (lambda text:meetings._validate_round1(text,meeting),round1),
      (lambda text:meetings._validate_response(text,meeting),response),
      (lambda text:meetings._validate_later(text,meeting,2),later),
    ]:
        with pytest.raises(meetings.StructuredValidationError):
            validator(json.dumps(payload))


def test_ended_meeting_renders_founder_result_and_terminal_status(ctx,client):
    meeting=running()
    for index in range(5):
        meetings.next_step_idempotent(meeting,f"result-{index}")
        if meeting.status=="ENDED": break
    before=AgentRun.query.count()
    page=client.get(f"/meetings/{meeting.id}").get_data(as_text=True)
    assert "MEETING RESULT" in page
    for label in ("DECISION / POSITION","ACTIONS","CONTROLS","KEY RISK","COST","TOKENS","CALLS","COPYABLE MINUTES"):
        assert label in page
    assert "Meeting complete." in page
    assert "Waiting for the next bounded event." not in page
    for invalid_action in ("START MEETING","CONTINUE MEETING","RESUME","AUTHORIZE ONE RETRY","END MEETING"):
        assert invalid_action not in page
    assert AgentRun.query.count()==before
    assert meeting.minutes_json["provider_calls"]==2
