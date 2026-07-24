from decimal import Decimal
import pytest

from eason_one.extensions import db
from eason_one.models import (
    AgentRun, ContributionEvent, CostEvent, Employee, FounderFeedbackEvent,
    KnowledgeItem, Meeting, MeetingMessage, Project, Task,
)
from eason_one.services import meetings
from eason_one.services.ceo import operating_context
from eason_one.services.projects import classify, create_project


def people():
    return (
        Employee.query.filter_by(slug="ceo").one(),
        Employee.query.filter_by(slug="researcher").one(),
        Employee.query.filter_by(slug="critic").one(),
    )


def planned(participants=None, **limits):
    ceo, researcher, critic = people()
    participants = participants or [ceo, researcher, critic]
    return meetings.create(
        "Beauty pilot review", "Decide the next bounded action",
        "Positions, evidence, risks, and next action", ceo, participants,
        max_rounds=limits.get("max_rounds", 3),
        token_limit=limits.get("token_limit", 30000),
        real_cost_limit_twd=limits.get("real_cost_limit_twd", 100),
    )


def test_project_hygiene_filters_ceo_context_and_preserves_history(ctx):
    ceo, _, _ = people()
    live = create_project("Live", "Operate", ceo, status="ACTIVE")
    smoke = create_project("Smoke", "Test only", ceo, status="ACTIVE", environment="SMOKE")
    archived = create_project("Old", "Historical", ceo, status="ACTIVE", environment="ARCHIVED")
    run = AgentRun(
        employee_id=ceo.id, project_id=smoke.id, model_config_id=ceo.current_model.id,
        purpose="HISTORICAL", user_request="x", system_prompt_snapshot="x",
        context_snapshot="x", provider_key_snapshot="mock",
        model_name_snapshot="deterministic-mock", input_price_snapshot=0,
        output_price_snapshot=0, currency_snapshot="TWD",
    )
    db.session.add(run); db.session.commit()
    text = operating_context()
    assert live.name in text
    assert smoke.name not in text and archived.name not in text
    classify(smoke, "ARCHIVED")
    assert db.session.get(AgentRun, run.id).project_id == smoke.id


def test_existing_project_intake_creates_no_work_or_knowledge(client, ctx):
    ceo, _, _ = people()
    response = client.post("/projects/existing", data={
        "name": "Committed Beauty Work", "objective": "Launch responsibly",
        "current_state_summary": "Packaging is selected.", "status": "ACTIVE",
        "priority": "HIGH", "owner_id": ceo.id,
        "known_constraints": "No unverified claims", "next_milestone": "Founder review",
    })
    assert response.status_code == 302
    project = Project.query.filter_by(name="Committed Beauty Work").one()
    assert (project.environment, project.origin) == ("LIVE", "EXISTING")
    assert Task.query.filter_by(project_id=project.id).count() == 0
    assert KnowledgeItem.query.filter_by(project_id=project.id).count() == 0
    assert "Packaging is selected." in operating_context()


def test_meeting_lifecycle_refresh_is_read_only_and_terminal_blocks_calls(client, ctx, monkeypatch):
    meeting = planned()
    assert len(meeting.participants) == 3
    meetings.start(meeting)
    before = (MeetingMessage.query.count(), AgentRun.query.count())
    assert client.get(f"/meetings/{meeting.id}").status_code == 200
    assert client.get(f"/meetings/{meeting.id}").status_code == 200
    assert (MeetingMessage.query.count(), AgentRun.query.count()) == before
    meetings.stop(meeting, "Founder ended discussion")
    called = {"n": 0}
    monkeypatch.setattr("eason_one.services.execution.get_provider",
                        lambda key: called.__setitem__("n", called["n"] + 1))
    with pytest.raises(ValueError, match="not ACTIVE"):
        meetings.advance(meeting)
    assert called["n"] == 0
    assert meeting.status == "TERMINATED_BY_FOUNDER"


def test_only_valid_active_employees_may_participate(ctx):
    ceo, researcher, _ = people()
    invalid = Employee(name="Not persisted", slug="not-persisted")
    with pytest.raises(ValueError, match="valid persisted"):
        meetings.create("x", "y", "z", ceo, [invalid])
    researcher.active = False
    db.session.commit()
    with pytest.raises(ValueError, match="active"):
        meetings.create("x", "y", "z", ceo, [researcher])


def test_founder_join_intervention_commands_and_compact_context(ctx):
    meeting = planned()
    fact = KnowledgeItem(kind="FACT", title="Company constraint", content="No fabricated evidence",
                         founder_approved=True)
    db.session.add(fact)
    meetings.start(meeting)
    assert meeting.founder_joined_at is None
    meetings.join_founder(meeting)
    meetings.intervene(meeting, "Do not assume retailer demand.")
    meetings.command(meeting, "FOCUS", "Resolve packaging risk.")
    meetings.command(meeting, "REQUEST_EVIDENCE", "State evidence availability.")
    for round_number in range(1, 4):
        db.session.add(MeetingMessage(
            meeting_id=meeting.id, speaker_type="SYSTEM", round_number=round_number,
            message_type="SYSTEM", content=f"round-{round_number}-material",
        ))
    meeting.current_round = 3
    meeting.current_summary_json = {"disagreement": "Packaging risk remains."}
    db.session.commit()
    context = meetings.compact_context(meeting, people()[1])
    assert "Do not assume retailer demand." in context
    assert "Resolve packaging risk." in context
    assert "Packaging risk remains." in context
    assert "round-3-material" in context
    assert "round-1-material" not in context
    assert f"#{fact.id} [FACT] Company constraint" in context


def test_mock_meeting_round_usage_feedback_and_no_authoritative_mutation(ctx):
    meeting = planned()
    meetings.start(meeting)
    meetings.advance(meeting)
    assert meeting.current_round == 1
    messages = MeetingMessage.query.filter_by(
        meeting_id=meeting.id, speaker_type="EMPLOYEE").all()
    assert len(messages) == 3
    tokens, cost = meetings.usage(meeting)
    assert tokens > 0 and cost == Decimal("0")
    contributions = ContributionEvent.query.count()
    knowledge = KnowledgeItem.query.count()
    row = meetings.feedback(messages[0], "KEY_INSIGHT", "Useful distinction")
    assert row.employee_id == messages[0].employee_id
    assert FounderFeedbackEvent.query.count() == 1
    assert ContributionEvent.query.count() == contributions
    assert KnowledgeItem.query.count() == knowledge


def test_meeting_usage_aggregates_linked_actual_cost_events(ctx):
    meeting = planned()
    ceo, _, _ = people()
    run = AgentRun(
        employee_id=ceo.id, meeting_id=meeting.id, model_config_id=ceo.current_model.id,
        purpose="MEETING_TEST", user_request="x", system_prompt_snapshot="x",
        context_snapshot="x", input_tokens=12, output_tokens=8,
        provider_key_snapshot="mock", model_name_snapshot="deterministic-mock",
        input_price_snapshot=0, output_price_snapshot=0, currency_snapshot="TWD",
    )
    db.session.add(run); db.session.flush()
    db.session.add(CostEvent(
        company_id=meeting.company_id, employee_id=ceo.id, agent_run_id=run.id,
        category="MODEL_USAGE", description="linked actual", real_cost_delta=Decimal("1.250000"),
    ))
    db.session.commit()
    assert meetings.usage(meeting) == (20, Decimal("1.250000"))


@pytest.mark.parametrize("limit_kind", ["rounds", "tokens", "cost"])
def test_meeting_limits_stop_before_provider(ctx, monkeypatch, limit_kind):
    ceo, _, _ = people()
    meeting = planned([ceo], max_rounds=1, token_limit=30000)
    meetings.start(meeting)
    if limit_kind == "rounds":
        meeting.current_round = 1
    elif limit_kind == "tokens":
        meeting.token_limit = 1
    else:
        ceo.current_model.input_price_per_million = 1
        meeting.real_cost_limit_twd = 0
    db.session.commit()
    called = {"n": 0}
    monkeypatch.setattr("eason_one.services.execution.get_provider",
                        lambda key: called.__setitem__("n", called["n"] + 1))
    with pytest.raises(ValueError):
        meetings.advance(meeting)
    assert called["n"] == 0
    assert AgentRun.query.filter_by(meeting_id=meeting.id).count() == 0
    assert CostEvent.query.count() == 0


def test_inactive_participant_model_stops_before_provider(ctx, monkeypatch):
    ceo, _, _ = people()
    meeting = planned([ceo])
    meetings.start(meeting)
    ceo.current_model.active = False
    db.session.commit()
    called = {"n": 0}
    monkeypatch.setattr("eason_one.services.execution.get_provider",
                        lambda key: called.__setitem__("n", called["n"] + 1))
    with pytest.raises(ValueError, match="inactive"):
        meetings.advance(meeting)
    assert called["n"] == 0


def test_end_synthesis_persists_minutes_and_creates_no_brain_item(ctx):
    meeting = planned()
    meetings.start(meeting)
    meetings.advance(meeting)
    instructions = meeting.chair.system_instructions
    knowledge = KnowledgeItem.query.count()
    meetings.end_and_synthesize(meeting)
    meeting_id = meeting.id
    db.session.remove()
    restored = db.session.get(Meeting, meeting_id)
    assert restored.status == "ENDED"
    assert restored.minutes_json["participants"]
    assert "agreements" in restored.minutes_json
    assert KnowledgeItem.query.count() == knowledge
    assert restored.chair.system_instructions == instructions
    with pytest.raises(ValueError, match="not ACTIVE"):
        meetings.advance(restored)


def test_meeting_ui_exposes_room_controls_and_audit(client, ctx):
    meeting = planned()
    meetings.start(meeting)
    meetings.advance(meeting)
    page = client.get(f"/meetings/{meeting.id}").get_data(as_text=True)
    assert "LIVE SUMMARY" in page
    assert "TRANSCRIPT" in page
    assert "FOUNDER CONTROL" in page
    assert "ACTUAL COST" not in page  # room header reports exact TWD/tokens directly
    lobby = client.get("/meetings").get_data(as_text=True)
    assert "Meeting Lobby" in lobby and "ACTUAL COST" in lobby
