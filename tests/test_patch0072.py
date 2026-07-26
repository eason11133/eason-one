import json
from decimal import Decimal

import pytest

from eason_one.extensions import db
from eason_one.models import AgentRun, CostEvent, Employee, MeetingStep
from eason_one.providers import ProviderResult
from eason_one.schemas import MEETING_RESPONSE_SCHEMA, MEETING_ROUND1_SCHEMA
from eason_one.services import meetings
from eason_one.services.execution import execute


def ceo():
    return Employee.query.filter_by(slug="ceo").one()


def running(mission="Keep this unique mission token MISSION-ALPHA.", question="What is the bounded decision?"):
    employee=ceo()
    meeting=meetings.create("Compact decision",mission,question,employee,[employee],profile="ECONOMY")
    meetings.start_auto(meeting)
    return meeting


def test_canonical_context_has_one_mission_and_no_live_brief_reinjection(ctx):
    meeting=running()
    meeting.current_summary_json={"agreement":"DISPLAY-ONLY-LIVE-BRIEF"}
    db.session.commit()
    context,composition=meetings._context_packet(meeting,ceo(),1)
    assert context.count("MISSION-ALPHA")==1
    assert context.count("MISSION_CONTEXT")==1
    assert "MEETING_QUESTION\nWhat is the bounded decision?" in context
    assert "DISPLAY-ONLY-LIVE-BRIEF" not in context
    assert "PREVIOUS ROUND SUMMARY" not in context
    assert composition["total_chars"]==len(context)
    assert set(composition["components"]) >= {"mission","question","evidence","protocol"}


def test_evidence_and_message_namespaces_are_explicit(ctx):
    meeting=running()
    context,_=meetings._context_packet(meeting,ceo(),1)
    assert "AVAILABLE COMPANY BRAIN EVIDENCE\nNone. Return evidence_ids: []." in context
    assert "EVIDENCE_ID only for Company Brain evidence" in context
    assert "MEETING_MESSAGE_ID only for Meeting messages" in context
    assert "Message #" not in context


def test_compact_schema_contracts_and_economy_cap():
    first=MEETING_ROUND1_SCHEMA["schema"]["properties"]
    response=MEETING_RESPONSE_SCHEMA["schema"]["properties"]
    assert set(first)=={"position","actions","risk","evidence_ids","confidence"}
    assert first["position"]["maxLength"]==180
    assert first["actions"]["maxItems"]==3
    assert first["actions"]["items"]["maxLength"]==120
    assert set(response)=={"relation","core_point","controls","risk","evidence_ids"}
    assert response["core_point"]["maxLength"]==180
    assert response["controls"]["items"]["maxLength"]==120
    assert meetings.PROFILES["ECONOMY"]["contribution_cap"]==512


@pytest.mark.parametrize("reason",["max_tokens","max_output_tokens"])
def test_provider_truncation_maps_to_output_truncated_and_keeps_cost(ctx,monkeypatch,reason):
    employee=ceo()
    model=employee.current_model
    model.provider_key="anthropic" if reason=="max_tokens" else "openai"
    model.input_price_per_million=Decimal("10")
    model.output_price_per_million=Decimal("20")
    db.session.commit()

    class Truncated:
        def complete(self,*args,**kwargs):
            return ProviderResult("",100,50,response_id="resp-1",request_id="req-1",
              status="incomplete",incomplete_reason=reason,stop_reason=reason)

    monkeypatch.setattr("eason_one.services.execution.get_provider",lambda key:Truncated())
    run=execute(employee,"TEST","bounded",context_override="context")
    assert run.status=="FAILED"
    assert run.failure_reason=="OUTPUT_TRUNCATED"
    assert run.provider_stop_reason==reason
    assert (run.provider_response_id,run.provider_request_id)==("resp-1","req-1")
    assert run.real_cost>0
    assert CostEvent.query.filter_by(agent_run_id=run.id).count()==1


def test_context_composition_is_auditable_on_run(ctx):
    meeting=running()
    meetings.next_step(meeting)
    run=AgentRun.query.filter_by(meeting_id=meeting.id).one()
    assert run.context_composition_json["total_chars"]==len(run.context_snapshot)
    assert "mission" in run.context_composition_json["components"]


def test_billable_invalid_output_pauses_without_automatic_retry(ctx,monkeypatch):
    meeting=running()
    model=ceo().current_model
    model.provider_key="openai"
    model.input_price_per_million=Decimal("10")
    model.output_price_per_million=Decimal("20")
    db.session.commit()
    calls={"count":0}

    class Invalid:
        def complete(self,*args,**kwargs):
            calls["count"]+=1
            return ProviderResult('{"wrong":"shape"}',100,20,response_id=f"resp-{calls['count']}")

    monkeypatch.setattr("eason_one.services.execution.get_provider",lambda key:Invalid())
    with pytest.raises(ValueError,match="Invalid Round 1"):
        meetings.next_step_idempotent(meeting,"first")
    assert meeting.status=="PAUSED"
    assert meeting.paid_failure_json["reason"]=="STRUCTURED_OUTPUT_INVALID"
    assert MeetingStep.query.filter_by(kind="CONTRIBUTION").one().status=="PAID_FAILED"
    assert CostEvent.query.count()==1
    assert calls["count"]==1
    meetings.next_step_idempotent(meeting,"stale-auto")
    assert calls["count"]==1
    assert CostEvent.query.count()==1
    with pytest.raises(ValueError,match="explicit Founder retry"):
        meetings.resume(meeting)
    with pytest.raises(ValueError,match="explicit Founder retry"):
        meetings.start_auto(meeting)


def test_explicit_paid_retry_is_single_use(ctx,monkeypatch):
    meeting=running()
    model=ceo().current_model
    model.provider_key="openai"
    model.input_price_per_million=Decimal("10")
    model.output_price_per_million=Decimal("20")
    db.session.commit()

    class Sequence:
        calls=0
        def complete(self,*args,**kwargs):
            self.calls+=1
            if self.calls==1: return ProviderResult("{}",80,10)
            return ProviderResult(json.dumps({"position":"Proceed carefully.","actions":["Run a test"],
              "risk":"Evidence remains thin.","evidence_ids":[],"confidence":.5}),80,25)
    provider=Sequence()
    monkeypatch.setattr("eason_one.services.execution.get_provider",lambda key:provider)
    with pytest.raises(ValueError):
        meetings.next_step(meeting)
    meetings.retry_paid_step(meeting)
    with pytest.raises(ValueError,match="no paid failed step"):
        meetings.retry_paid_step(meeting)
    meetings.next_step(meeting)
    assert provider.calls==2
    assert CostEvent.query.count()==2
    assert MeetingStep.query.filter_by(kind="CONTRIBUTION").one().status=="SUCCEEDED"


def test_paid_failure_room_exposes_audit_and_explicit_retry(ctx,client):
    meeting=running()
    meeting.status="PAUSED"
    meeting.paid_failure_json={"reason":"OUTPUT_TRUNCATED","message":"cap reached","run_id":7,
      "employee":"CEO","provider":"openai","model":"gpt","input_tokens":10,"output_tokens":5,
      "cost_twd":"0.1","retry_estimate_twd":"0.2"}
    db.session.commit()
    page=client.get(f"/meetings/{meeting.id}").get_data(as_text=True)
    assert "PAID STEP FAILED" in page
    assert "AUTHORIZE ONE RETRY" in page
    assert "VIEW RUN" in page
