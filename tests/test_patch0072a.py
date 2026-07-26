import copy
import json
from decimal import Decimal

import pytest

from eason_one.extensions import db
from eason_one.models import AgentRun, CostEvent, Employee
from eason_one.providers import ProviderResult, anthropic_compatible_schema
from eason_one.schemas import MEETING_RESPONSE_SCHEMA, MEETING_ROUND1_SCHEMA
from eason_one.services import meetings
from eason_one.services.costs import estimate_execution
from eason_one.services.execution import execute


def people():
    return [Employee.query.filter_by(slug=slug).one() for slug in ("ceo","critic")]


def running(max_speakers=2):
    employees=people()
    meeting=meetings.create("Probe review","Bounded learning experiment mission",
      "What probe should run next?",employees[0],employees,profile="ECONOMY",
      max_speakers_per_round=max_speakers)
    meetings.start_auto(meeting)
    return meeting


def test_anthropic_transform_preserves_constraints_as_descriptions_without_mutation():
    original=copy.deepcopy(MEETING_RESPONSE_SCHEMA["schema"])
    transformed=anthropic_compatible_schema(MEETING_RESPONSE_SCHEMA["schema"])
    assert MEETING_RESPONSE_SCHEMA["schema"]==original
    assert transformed is not original
    assert "maxLength: 180" in transformed["properties"]["core_point"]["description"]
    assert "maxItems: 3" in transformed["properties"]["controls"]["description"]
    assert "maxLength: 120" in transformed["properties"]["controls"]["items"]["description"]
    assert "maxLength" not in transformed["properties"]["core_point"]


def test_schema_specific_compact_protocols_are_single_and_bounded():
    first=meetings._contribution_protocol("MEETING_CONTRIBUTION_ROUND1")
    later=meetings._contribution_protocol("MEETING_CONTRIBUTION_RESPONSE")
    assert "<=180" in first and "<=3 actions" in first and "<=120" in first
    assert "<=180" in later and "<=3 controls" in later and "do not restate" in later
    assert first.count("EVIDENCE_ID")==1 and later.count("EVIDENCE_ID")==1


@pytest.mark.parametrize("reason",["max_tokens","model_context_window_exceeded","max_output_tokens"])
def test_output_limit_reasons_are_explicit_truncation(ctx,monkeypatch,reason):
    employee=people()[0]
    model=employee.current_model
    model.provider_key="anthropic" if reason!="max_output_tokens" else "openai"
    model.input_price_per_million=Decimal("10")
    model.output_price_per_million=Decimal("20")
    db.session.commit()
    class Limited:
        def complete(self,*args,**kwargs):
            return ProviderResult("",50,25,response_id="resp",request_id="req",
              status="incomplete",incomplete_reason=reason,stop_reason=reason)
    monkeypatch.setattr("eason_one.services.execution.get_provider",lambda key:Limited())
    run=execute(employee,"TEST","x",context_override="x",response_schema=MEETING_ROUND1_SCHEMA,
      max_output_tokens_override=512)
    assert (run.status,run.failure_reason,run.provider_stop_reason)==("FAILED","OUTPUT_TRUNCATED",reason)
    assert run.effective_max_output_tokens==512
    assert CostEvent.query.filter_by(agent_run_id=run.id).count()==1


def test_paid_retry_estimate_uses_historical_schema_and_cap(ctx,monkeypatch):
    meeting=running()
    employee=people()[0]
    model=employee.current_model
    model.provider_key="openai"
    model.input_price_per_million=Decimal("10")
    model.output_price_per_million=Decimal("20")
    db.session.commit()
    class Invalid:
        def complete(self,*args,**kwargs): return ProviderResult("{}",100,20)
    monkeypatch.setattr("eason_one.services.execution.get_provider",lambda key:Invalid())
    with pytest.raises(ValueError):
        meetings.next_step(meeting)
    run=AgentRun.query.filter_by(meeting_id=meeting.id).one()
    failure=meeting.paid_failure_json
    historical=type("Historical",(),{"input_price_per_million":run.input_price_snapshot,
      "output_price_per_million":run.output_price_snapshot,"max_output_tokens":run.effective_max_output_tokens})()
    expected=estimate_execution(historical,run.system_prompt_snapshot,run.context_snapshot,
      run.user_request,run.effective_max_output_tokens,run.response_schema_snapshot_json)
    without=estimate_execution(historical,run.system_prompt_snapshot,run.context_snapshot,
      run.user_request,run.effective_max_output_tokens,None)
    assert failure["retry_schema_included"] is True
    assert failure["retry_output_cap"]==512
    assert Decimal(failure["retry_estimate_twd"])==expected.real_cost
    assert expected.input_tokens>without.input_tokens


def test_economy_minutes_preserve_all_compact_value(ctx):
    meeting=running()
    for index in range(6):
        meetings.next_step_idempotent(meeting,f"m-{index}")
        if meeting.status=="ENDED": break
    first=meeting.minutes_json["positions"][0]
    response=meeting.minutes_json["responses"][0]
    assert set(first)=={"speaker","position","actions","risk","evidence_ids","confidence",
      "validation_status","warning_count"}
    assert set(response)=={"speaker","relation","core_point","controls","risk","evidence_ids",
      "validation_status","warning_count"}
    assert first["actions"] and first["risk"]
    assert response["controls"] and response["risk"]


@pytest.mark.parametrize("relation",["QUALIFY","ADD_INFORMATION"])
def test_non_disagreement_relations_stay_out_of_disagreement(ctx,relation):
    meeting=running()
    employee=people()[1]
    from eason_one.models import MeetingMessage
    db.session.add(MeetingMessage(meeting_id=meeting.id,employee_id=employee.id,
      speaker_type="EMPLOYEE",round_number=1,message_type=relation,
      content=json.dumps({"relation":relation,"core_point":"Refinement","controls":[],
        "risk":"Bounded risk","evidence_ids":[]})))
    db.session.commit()
    brief=meetings._compact_live_brief(meeting)
    assert brief["key_disagreement"]=="No explicit material disagreement presented."
    if relation=="QUALIFY": assert brief["qualification"]=="Refinement"


def test_direct_small_meeting_obeys_one_speaker_cap(ctx):
    meeting=running(max_speakers=1)
    meetings.next_step(meeting)
    assert meeting.routing_json["speakers"]==["ceo"]
    for _ in range(3):
        meetings.next_step(meeting)
        if meeting.status=="ENDED": break
    assert AgentRun.query.filter_by(meeting_id=meeting.id,purpose="MEETING_CONTRIBUTION").count()==1


def test_waiting_for_founder_rejects_start_and_requires_new_intervention(ctx):
    meeting=running()
    meeting.status="WAITING_FOR_FOUNDER"
    meeting.routing_json={"founder_message_id_at_wait":0}
    db.session.commit()
    with pytest.raises(ValueError,match="explicit resume"):
        meetings.start_auto(meeting)
    with pytest.raises(ValueError,match="new Founder intervention"):
        meetings.resume(meeting)
    meetings.join_founder(meeting)
    intervention=meetings.intervene(meeting,"Use the smallest safe probe.")
    meetings.resume(meeting)
    assert meeting.status=="RUNNING"
    assert meeting.routing_json["founder_resume_message_id"]==intervention.id


def test_speech_cards_and_live_payload_preserve_compact_fields(ctx,client):
    meeting=running()
    first=meetings.next_step(meeting)
    assert set(first["latest_event"]["contribution"]) >= {"position","actions","risk"}
    second=meetings.next_step(meeting)
    assert set(second["latest_event"]["contribution"]) >= {"relation","core_point","controls","risk"}
    page=client.get(f"/meetings/{meeting.id}?autorun=1").get_data(as_text=True)
    for label in ("POSITION","ACTIONS","RELATION","CORE POINT","CONTROLS","RISK"):
        assert label in page
    assert "updateSpeech" in page
    assert "VIEW MISSION CONTEXT" in page


def test_context_composition_audit_page_is_read_only(ctx,client):
    meeting=running()
    meetings.next_step(meeting)
    run=AgentRun.query.filter_by(meeting_id=meeting.id).one()
    before=AgentRun.query.count()
    page=client.get(f"/runs/{run.id}").get_data(as_text=True)
    assert "CONTEXT COMPOSITION" in page
    assert "Schema overhead estimate" in page
    assert "not exact provider tokenization" in page
    assert AgentRun.query.count()==before
