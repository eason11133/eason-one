import json
from decimal import Decimal

import pytest

from eason_one.extensions import db
from eason_one.models import (
    AgentRun, CostEvent, Employee, EmployeeModelHistory, FounderFeedbackEvent,
    Meeting, MeetingMessage, MeetingStep, ModelConfig,
)
from eason_one.providers import ProviderResult
from eason_one.schemas import MEETING_LATER_SCHEMA, MEETING_ROUND1_SCHEMA
from eason_one.services import meetings
from eason_one.services.execution import execute
from eason_one.services.model_configs import (
    archive as archive_model, create as create_model, delete as delete_model,
    edit as edit_model,
)


def people():
    return [
        Employee.query.filter_by(slug=slug).one()
        for slug in ("ceo", "researcher", "critic")
    ]


def auto_meeting(participants=None, profile="ECONOMY", **overrides):
    participants = participants or people()
    meeting = meetings.create(
        "Lean strategy", "Decide whether evidence supports action",
        "Evidence, disagreement, and recommendation", participants[0], participants,
        profile=profile, **overrides,
    )
    meetings.start_auto(meeting)
    return meeting


def run_to_terminal(meeting, limit=20):
    states = []
    for index in range(limit):
        state = meetings.next_step_idempotent(meeting, f"step-{index}")
        states.append(state)
        if meeting.status != "RUNNING":
            break
    return states


def test_profiles_are_conservative_and_do_not_change_employee_models(ctx):
    employee = people()[0]
    model_id = employee.current_model.id
    meeting = auto_meeting([employee], "ECONOMY")
    assert (meeting.max_rounds, meeting.max_speakers_per_round) == (2, 2)
    assert (meeting.contribution_output_cap, meeting.router_output_cap,
            meeting.synthesis_output_cap) == (512, 192, 512)
    assert meeting.real_cost_limit_twd == Decimal("1.0000")
    assert employee.current_model.id == model_id


def test_output_override_reaches_provider_estimator_and_audit(ctx, monkeypatch):
    employee = people()[0]
    employee.current_model.max_output_tokens = 4096
    db.session.commit()
    received = {}

    class Provider:
        def complete(self, model, system, user, context, max_tokens, response_schema=None):
            received["max_tokens"] = max_tokens
            return ProviderResult("ok", 2, 1)

    monkeypatch.setattr("eason_one.services.execution.get_provider", lambda key: Provider())
    monkeypatch.setattr("eason_one.services.execution.ensure_budget",
                        lambda estimate: received.setdefault("estimate", estimate))
    run = execute(employee, "TEST", "x", max_output_tokens_override=512)
    assert received["max_tokens"] == 512
    assert run.effective_max_output_tokens == 512
    assert received["estimate"] < Decimal("1")
    assert employee.current_model.max_output_tokens == 4096


@pytest.mark.parametrize("override", [0, -1, 1201])
def test_output_override_must_be_positive_and_within_model_max(ctx, monkeypatch, override):
    employee = people()[0]
    employee.current_model.max_output_tokens = 1200
    calls = []
    monkeypatch.setattr("eason_one.services.execution.get_provider", lambda key: calls.append(key))
    with pytest.raises(ValueError, match="override"):
        execute(employee, "TEST", "x", max_output_tokens_override=override)
    assert calls == [] and AgentRun.query.count() == 0


def test_structured_contribution_contracts_are_aggressively_bounded():
    first = MEETING_ROUND1_SCHEMA["schema"]["properties"]
    later = MEETING_LATER_SCHEMA["schema"]["properties"]
    assert first["position"]["maxLength"] == 180
    assert first["actions"]["maxItems"] == 3
    assert first["actions"]["items"]["maxLength"] == 120
    assert first["risk"]["maxLength"] == 180
    assert later["core_point"]["maxLength"] == 180
    assert later["type"]["enum"] == [
        "DISAGREEMENT", "COUNTEREVIDENCE", "NEW_INFORMATION", "MATERIAL_REFINEMENT"
    ]


def test_one_start_autonomously_routes_subset_and_ends(ctx):
    meeting = auto_meeting()
    states = run_to_terminal(meeting)
    assert meeting.status == "ENDED"
    assert meeting.current_round == 1 < meeting.max_rounds
    contributions = AgentRun.query.filter_by(
        meeting_id=meeting.id, purpose="MEETING_CONTRIBUTION").all()
    assert len(contributions) == 2  # router selected a lean subset from three invitees
    assert {run.employee.slug for run in contributions} == {"ceo", "researcher"}
    assert all(run.effective_max_output_tokens == 512 for run in contributions)
    assert AgentRun.query.filter_by(meeting_id=meeting.id, purpose="CHAIR_ROUTER").count() == 1
    assert AgentRun.query.filter_by(meeting_id=meeting.id, purpose="MEETING_SYNTHESIS").count() == 0
    assert len(AgentRun.query.filter_by(meeting_id=meeting.id).all()) == 3
    assert states[-1]["status"] == "ENDED"


def test_router_invalid_employee_slug_is_rejected(ctx, monkeypatch):
    meeting = auto_meeting()

    class Provider:
        def complete(self, *args):
            return ProviderResult(json.dumps({
                "continue_meeting": True, "next_speakers": ["outsider"],
                "reason": "bad", "founder_input_required": False,
                "founder_question": None,
            }), 5, 2)

    monkeypatch.setattr("eason_one.services.execution.get_provider", lambda key: Provider())
    with pytest.raises(ValueError, match="invalid Employee slug"):
        meetings.next_step(meeting)
    assert meeting.current_round == 0
    assert MeetingStep.query.filter_by(kind="CHAIR_ROUTER", status="FAILED").count() == 1


def test_no_material_later_round_triggers_early_synthesis(ctx):
    employee = people()[0]
    meeting = auto_meeting([employee], "DEEP")
    meeting.current_round = 1
    meeting.routing_json = {"round": 2, "speakers": [employee.slug]}
    db.session.commit()
    meetings.next_step(meeting)  # structured no-material contribution
    meetings.next_step(meeting)  # deterministic round close, no provider
    assert meeting.routing_json["ready_for_synthesis"] is True
    meetings.next_step(meeting)
    assert meeting.status == "ENDED" and meeting.current_round == 2 < meeting.max_rounds


def test_duplicate_http_idempotency_key_cannot_duplicate_paid_step(ctx):
    meeting = auto_meeting()
    first = meetings.next_step_idempotent(meeting, "same-browser-request")
    calls = AgentRun.query.filter_by(meeting_id=meeting.id).count()
    second = meetings.next_step_idempotent(meeting, "same-browser-request")
    assert second == first
    assert AgentRun.query.filter_by(meeting_id=meeting.id).count() == calls == 1


def test_pause_stop_and_refresh_paths_make_no_provider_calls(ctx, client, monkeypatch):
    meeting = auto_meeting()
    calls = []
    monkeypatch.setattr("eason_one.services.execution.get_provider", lambda key: calls.append(key))
    meetings.pause(meeting)
    assert meetings.next_step(meeting)["status"] == "PAUSED"
    assert client.get(f"/meetings/{meeting.id}").status_code == 200
    assert calls == []
    meetings.resume(meeting)
    meetings.stop(meeting)
    assert meetings.next_step(meeting)["status"] == "TERMINATED_BY_FOUNDER"
    assert calls == []


def test_hard_ceiling_exposes_blocked_before_call_details(ctx, client, monkeypatch):
    employee = people()[0]
    meeting = auto_meeting([employee], token_limit=1)
    calls = []
    monkeypatch.setattr("eason_one.services.execution.get_provider", lambda key: calls.append(key))
    response = client.post(
        f"/meetings/{meeting.id}/next-step",
        headers={"Idempotency-Key": "blocked-1"},
    )
    assert response.status_code == 409 and calls == []
    page = client.get(f"/meetings/{meeting.id}").get_data(as_text=True)
    assert "BLOCKED BEFORE API CALL" in page
    assert "No provider call was made." in page
    assert employee.name in page and employee.current_model.label in page


def test_live_brief_is_compact_and_protocol_is_not_evidence(ctx):
    meeting = auto_meeting([people()[0]])
    meetings.next_step(meeting)
    meetings.next_step(meeting)  # closes first round deterministically
    brief = meeting.current_summary_json
    assert len(brief["agreement"]) <= 180
    assert brief["evidence"] == "No qualifying evidence presented."
    assert "Participants must distinguish" not in brief["evidence"]


def test_planner_and_room_use_founder_simple_controls(ctx, client):
    planner = client.get("/meetings").get_data(as_text=True)
    assert 'type="checkbox" name="participant_ids"' in planner
    assert "participant-card" in planner and "Local Mock" in planner
    for label in ("MAX ROUNDS", "TOKEN CEILING", "TWD CEILING",
                  "CONTRIBUTION OUTPUT CAP", "ROUTER OUTPUT CAP", "SYNTHESIS OUTPUT CAP"):
        assert label in planner
    meeting = meetings.create("Simple room", "Question", "Question", people()[0], people())
    room = client.get(f"/meetings/{meeting.id}").get_data(as_text=True)
    assert "START MEETING" in room and "JOIN" in room
    assert '<details class="panel advanced-controls">' in room
    assert "<summary>TRANSCRIPT</summary>" in room
    assert "Focus Discussion" not in room and "Request Evidence" not in room


def test_compact_bubble_hides_raw_json_and_feedback_is_zero_call(ctx, client, monkeypatch):
    meeting = auto_meeting([people()[0]])
    meetings.next_step(meeting)
    message = MeetingMessage.query.filter_by(
        meeting_id=meeting.id, speaker_type="EMPLOYEE").one()
    page = client.get(f"/meetings/{meeting.id}").get_data(as_text=True)
    assert "Readiness remains unproven." in page
    assert '{"position":' not in page
    calls = []
    monkeypatch.setattr("eason_one.services.execution.get_provider", lambda key: calls.append(key))
    client.post(f"/meeting-messages/{message.id}/feedback",
                data={"signal": "KEY_INSIGHT", "note": "Useful"})
    assert calls == [] and FounderFeedbackEvent.query.one().message_id == message.id


def test_model_edit_preserves_historical_run_snapshot(ctx):
    employee = people()[0]
    run = execute(employee, "TEST", "before edit")
    model = employee.current_model
    original = (run.model_name_snapshot, run.input_price_snapshot,
                run.output_price_snapshot, run.currency_snapshot)
    edit_model(model, "Edited Mock", "mock-edited", "1", "2", "TWD", 2000)
    assert (run.model_name_snapshot, run.input_price_snapshot,
            run.output_price_snapshot, run.currency_snapshot) == original
    assert model.label == "Edited Mock" and model.max_output_tokens == 2000


def test_invalid_model_edits_are_rejected(ctx):
    model = create_model("Real", "openai", "model", "1", "1", "TWD", 100)
    with pytest.raises(ValueError, match="greater than zero"):
        edit_model(model, "Real", "model", "0", "1", "TWD", 100)
    with pytest.raises(ValueError, match="currency"):
        edit_model(model, "Real", "model", "1", "1", "USD", 100)


def test_archived_model_cannot_assign_or_execute_and_preserves_audit(ctx):
    employee = people()[0]
    run = execute(employee, "TEST", "historical")
    model = employee.current_model
    archive_model(model)
    assert run.model_name_snapshot
    with pytest.raises(ValueError, match="archived"):
        execute(employee, "TEST", "blocked")
    other = people()[1]
    from eason_one.services.employees import change_model
    with pytest.raises(ValueError, match="archived"):
        change_model(other, model)
    assert AgentRun.query.count() == 1


def test_unused_model_deletes_but_assigned_or_used_models_do_not(ctx):
    unused = create_model("Unused", "mock", "unused", "0", "0", "TWD", 100)
    unused_id = unused.id
    delete_model(unused)
    assert db.session.get(ModelConfig, unused_id) is None

    assigned = create_model("Assigned", "mock", "assigned", "0", "0", "TWD", 100)
    people()[0].current_model = assigned
    db.session.commit()
    with pytest.raises(ValueError, match="Assigned"):
        delete_model(assigned)

    used = create_model("Used", "mock", "used", "0", "0", "TWD", 100)
    people()[1].current_model = used
    db.session.commit()
    execute(people()[1], "TEST", "use model")
    people()[1].current_model = ModelConfig.query.filter_by(label="Local Mock").one()
    db.session.commit()
    with pytest.raises(ValueError, match="Historically used"):
        delete_model(used)


def test_model_management_ui_separates_archived_and_shows_counts(ctx, client):
    model = create_model("Archive Me", "mock", "archive-me", "0", "0", "TWD", 100)
    archive_model(model)
    page = client.get("/models").get_data(as_text=True)
    assert "Archived Models" in page and "Archive Me" in page
    assert "Historical runs:" in page and "Assigned:" in page
