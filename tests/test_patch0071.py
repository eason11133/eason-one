import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from eason_one.extensions import db
from eason_one.models import AgentRun, Employee, Meeting, MeetingMessage, MeetingStep
from eason_one.providers import ProviderResult
from eason_one.services import meetings
from eason_one.services.brain import add_knowledge
from eason_one.services.projects import create_project


def people():
    return [
        Employee.query.filter_by(slug=slug).one()
        for slug in ("ceo", "researcher", "critic")
    ]


def running(participants=None, profile="ECONOMY", project=None):
    participants = participants or people()
    meeting = meetings.create("Conversation", "Reach a supported recommendation",
                              "Evidence and disagreement", participants[0], participants,
                              project=project, profile=profile)
    meetings.start_auto(meeting)
    return meeting


class SlowRouter:
    def __init__(self):
        self.calls = 0
        self.lock = threading.Lock()

    def complete(self, *args):
        with self.lock:
            self.calls += 1
        time.sleep(.15)
        return ProviderResult(json.dumps({
            "continue_meeting": True, "next_speakers": ["ceo", "researcher"],
            "reason": "small useful set", "founder_input_required": False,
            "founder_question": None,
        }), 5, 2)


def concurrent_posts(app, meeting_id, keys):
    barrier = threading.Barrier(len(keys))

    def post(key):
        with app.test_client() as client:
            barrier.wait()
            response = client.post(
                f"/meetings/{meeting_id}/next-step",
                headers={"Idempotency-Key": key},
            )
            return response.status_code

    with ThreadPoolExecutor(max_workers=len(keys)) as pool:
        return list(pool.map(post, keys))


def test_concurrent_first_creation_claim_invokes_provider_at_most_once(app, monkeypatch):
    with app.app_context():
        meeting = running()
        meeting_id = meeting.id
    provider = SlowRouter()
    monkeypatch.setattr("eason_one.services.execution.get_provider", lambda key: provider)
    statuses = concurrent_posts(app, meeting_id, ["parallel-a", "parallel-b"])
    with app.app_context():
        assert provider.calls == 1
        assert AgentRun.query.filter_by(meeting_id=meeting_id, purpose="CHAIR_ROUTER").count() == 1
        assert MeetingStep.query.filter_by(
            meeting_id=meeting_id, logical_key="ROUTER_BEFORE_ROUND_1").count() == 1
    assert all(status in {200, 409} for status in statuses)


def test_concurrent_failed_retry_claim_invokes_provider_at_most_once(app, monkeypatch):
    with app.app_context():
        meeting = running()
        meeting_id = meeting.id

    class Invalid:
        def complete(self, *args):
            return ProviderResult(json.dumps({
                "continue_meeting": True, "next_speakers": ["invalid"],
                "reason": "bad", "founder_input_required": False,
                "founder_question": None,
            }), 5, 2)

    monkeypatch.setattr("eason_one.services.execution.get_provider", lambda key: Invalid())
    with app.test_client() as client:
        assert client.post(f"/meetings/{meeting_id}/next-step",
                           headers={"Idempotency-Key": "initial-failure"}).status_code == 409
    provider = SlowRouter()
    monkeypatch.setattr("eason_one.services.execution.get_provider", lambda key: provider)
    concurrent_posts(app, meeting_id, ["retry-a", "retry-b"])
    with app.app_context():
        assert provider.calls == 0
        step = MeetingStep.query.filter_by(
            meeting_id=meeting_id, logical_key="ROUTER_BEFORE_ROUND_1").one()
        assert step.status == "FAILED"
        assert db.session.get(Meeting,meeting_id).status == "PAUSED"


def test_later_speakers_receive_bounded_validated_same_round_context(ctx, monkeypatch):
    meeting = running()
    meeting.routing_json = {"round": 1, "speakers": ["ceo", "researcher", "critic"]}
    db.session.commit()
    seen = {}

    class ConversationProvider:
        def complete(self, model, system, user, context, max_tokens, response_schema=None):
            employee = context.split("EMPLOYEE\nNAME: ", 1)[1].split("\n", 1)[0]
            seen[employee] = context
            if "ROUND1" in system:
                text = " " * 2000 + json.dumps({
                    "position": "Evidence is insufficient.", "actions": ["Run a live proof"],
                    "risk": "False confidence", "evidence_ids": [], "confidence": .4,
                }) + " " * 2000
            else:
                text = json.dumps({
                    "relation": "QUALIFY", "core_point": f"{employee} responds to prior evidence.",
                    "controls": ["Validate first"], "risk": "Validation remains missing.",
                    "evidence_ids": [],
                })
            return ProviderResult(text, 5, 2)

    monkeypatch.setattr("eason_one.services.execution.get_provider",
                        lambda key: ConversationProvider())
    meetings.next_step(meeting)
    meetings.next_step(meeting)
    meetings.next_step(meeting)
    assert "Evidence is insufficient." in seen["Researcher"]
    assert "Researcher responds to prior evidence." in seen["Critic"]
    assert " " * 100 not in seen["Researcher"]
    assert len(seen["Critic"]) < 5000
    assert "PRIOR VALIDATED CONTRIBUTIONS" in seen["Critic"]


def test_founder_input_resume_requires_new_intervention_and_reroutes(ctx, monkeypatch):
    meeting = running()
    calls = []

    class Router:
        def complete(self, model, system, user, context, max_tokens, response_schema=None):
            calls.append(user)
            waiting = "Founder intervention" not in user
            return ProviderResult(json.dumps({
                "continue_meeting": not waiting,
                "next_speakers": [] if waiting else ["researcher"],
                "reason": "Founder input needed" if waiting else "Use Founder direction",
                "founder_input_required": waiting,
                "founder_question": "Which outcome matters?" if waiting else None,
            }), 5, 2)

    monkeypatch.setattr("eason_one.services.execution.get_provider", lambda key: Router())
    meetings.next_step(meeting)
    assert meeting.status == "WAITING_FOR_FOUNDER"
    with pytest.raises(ValueError, match="new Founder intervention"):
        meetings.resume(meeting)
    meetings.join_founder(meeting)
    intervention = meetings.intervene(meeting, "Prioritize evidence sufficiency.")
    meetings.resume(meeting)
    meetings.next_step(meeting)
    assert meeting.status == "RUNNING"
    assert meeting.routing_json["speakers"] == ["researcher"]
    keys = [step.logical_key for step in MeetingStep.query.filter_by(kind="CHAIR_ROUTER").all()]
    assert any(str(intervention.id) in key for key in keys)
    assert len(calls) == 2


def test_autorun_state_returns_compact_event_and_live_brief(ctx):
    meeting = running([people()[0], people()[1]])
    meetings.next_step(meeting)
    state = meetings.next_step(meeting)
    assert state["latest_event"]["speaker"] == "Researcher"
    assert state["latest_event"]["contribution"]["relation"] == "AGREE"
    assert state["live_brief"]["agreement"]
    assert "messages" not in state and "transcript" not in state


def test_running_room_without_loop_is_explicitly_detached(ctx, client):
    meeting = running([people()[0]])
    before = AgentRun.query.count()
    page = client.get(f"/meetings/{meeting.id}").get_data(as_text=True)
    assert "RUNNING / LOCAL RUNNER NOT ATTACHED" in page
    assert "CONTINUE MEETING" in page
    assert AgentRun.query.count() == before


def test_evidence_ids_must_be_effective_and_visible(ctx):
    ceo = people()[0]
    project_a = create_project("A", "A", ceo, status="ACTIVE")
    project_b = create_project("B", "B", ceo, status="ACTIVE")
    meeting = running([ceo], project=project_a)
    valid = add_knowledge("FACT", "Visible", "yes", project_id=project_a.id, founder_approved=True)
    unrelated = add_knowledge("FACT", "Other", "no", project_id=project_b.id, founder_approved=True)
    historical = add_knowledge("HYPOTHESIS", "Old", "maybe", project_id=project_a.id, founder_approved=True)
    add_knowledge("KILLED", "Killed", "no", project_id=project_a.id,
                  target_knowledge_id=historical.id, founder_approved=True)
    assert meetings._parse_round1(json.dumps({
        "position": "x", "actions": [], "risk": "r",
        "evidence_ids": [valid.id], "confidence": .5,
    }), meeting)
    for bad in (999999, unrelated.id, historical.id):
        with pytest.raises(ValueError, match="invisible evidence"):
            meetings._parse_round1(json.dumps({
                "position": "x", "actions": [], "risk": "r",
                "evidence_ids": [bad], "confidence": .5,
            }), meeting)


def test_cross_meeting_target_message_is_rejected(ctx):
    employee = people()[0]
    first = running([employee])
    other = running([employee])
    run = AgentRun(employee_id=employee.id, meeting_id=other.id,
                   model_config_id=employee.current_model.id, purpose="MEETING_CONTRIBUTION",
                   user_request="x", system_prompt_snapshot="x", context_snapshot="x",
                   provider_key_snapshot="mock", model_name_snapshot="mock",
                   input_price_snapshot=0, output_price_snapshot=0, currency_snapshot="TWD",
                   status="SUCCEEDED")
    db.session.add(run); db.session.flush()
    message = MeetingMessage(meeting_id=other.id, employee_id=employee.id,
                             speaker_type="EMPLOYEE", round_number=1,
                             message_type="POSITION", content="{}", agent_run_id=run.id)
    db.session.add(message); db.session.commit()
    with pytest.raises(ValueError, match="cross-Meeting"):
        meetings._parse_later(json.dumps({
            "has_material_contribution": True, "type": "MATERIAL_REFINEMENT",
            "core_point": "x", "controls": [], "risk": "r",
            "evidence_ids": [], "target_message_ids": [message.id],
        }), first, 2)


def test_two_person_economy_happy_path_is_exactly_two_calls(ctx):
    meeting = running([people()[0], people()[1]])
    for index in range(10):
        meetings.next_step_idempotent(meeting, f"economy-two-{index}")
        if meeting.status == "ENDED":
            break
    runs = AgentRun.query.filter_by(meeting_id=meeting.id).all()
    assert meeting.status == "ENDED"
    assert len(runs) == 2
    assert {run.purpose for run in runs} == {"MEETING_CONTRIBUTION"}
    assert meeting.minutes_json and meeting.minutes_json["agreement"]


def test_three_person_economy_has_one_router_and_no_synthesis(ctx):
    meeting = running()
    for index in range(12):
        meetings.next_step_idempotent(meeting, f"economy-three-{index}")
        if meeting.status == "ENDED":
            break
    assert AgentRun.query.filter_by(meeting_id=meeting.id, purpose="CHAIR_ROUTER").count() == 1
    assert AgentRun.query.filter_by(meeting_id=meeting.id, purpose="MEETING_SYNTHESIS").count() == 0
    assert AgentRun.query.filter_by(meeting_id=meeting.id).count() == 3
    assert meeting.minutes_json["participants_who_spoke"] == ["CEO", "Researcher"]


@pytest.mark.parametrize("profile", ["STANDARD", "DEEP"])
def test_standard_and_deep_retain_bounded_model_synthesis(ctx, profile):
    meeting = running([people()[0], people()[1]], profile=profile)
    for index in range(15):
        meetings.next_step_idempotent(meeting, f"{profile}-{index}")
        if meeting.status == "ENDED":
            break
    assert meeting.status == "ENDED"
    assert AgentRun.query.filter_by(
        meeting_id=meeting.id, purpose="MEETING_SYNTHESIS").count() == 1
    assert all(run.effective_max_output_tokens <= run.model_config.max_output_tokens
               for run in AgentRun.query.filter_by(meeting_id=meeting.id))


def test_live_brief_uses_only_explicit_relations(ctx):
    meeting = running([people()[0]])
    db.session.add(MeetingMessage(meeting_id=meeting.id, employee_id=people()[0].id,
      speaker_type="EMPLOYEE", round_number=1, message_type="POSITION",
      content=json.dumps({"position":"One view","actions":[],"risk":"Gap","evidence_ids":[],"confidence":.5})))
    db.session.commit()
    brief = meetings._compact_live_brief(meeting)
    assert brief["agreement"] == "No explicit shared agreement established."
    db.session.add(MeetingMessage(meeting_id=meeting.id, employee_id=people()[0].id,
      speaker_type="EMPLOYEE", round_number=1, message_type="ADD_INFORMATION",
      content=json.dumps({"relation":"ADD_INFORMATION","core_point":"New fact","controls":[],"evidence_ids":[],"risk":"Gap"})))
    db.session.commit()
    assert meetings._compact_live_brief(meeting)["key_disagreement"] == "No explicit material disagreement presented."
    db.session.add(MeetingMessage(meeting_id=meeting.id, employee_id=people()[0].id,
      speaker_type="EMPLOYEE", round_number=1, message_type="AGREE",
      content=json.dumps({"relation":"AGREE","core_point":"Explicit agreement","controls":[],"evidence_ids":[],"risk":""})))
    db.session.commit()
    assert meetings._compact_live_brief(meeting)["agreement"] == "Explicit agreement"
