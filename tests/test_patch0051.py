import json
from decimal import Decimal

import pytest

from eason_one.extensions import db
from eason_one.models import AgentRun, CostEvent, Employee, MeetingMessage, Project, Task
from eason_one.providers import ProviderResult
from eason_one.services import meetings
from eason_one.services.costs import estimate_execution
from eason_one.services.projects import create_project


def employees():
    return [
        Employee.query.filter_by(slug=slug).one()
        for slug in ("ceo", "researcher", "critic")
    ]


def active_meeting(participants=None, **limits):
    people = participants or employees()
    meeting = meetings.create(
        "Gate review", "Prove bounded safe execution", "Position, evidence, risk",
        people[0], people, max_rounds=limits.get("max_rounds", 3),
        token_limit=limits.get("token_limit", 50000),
        real_cost_limit_twd=limits.get("real_cost_limit_twd", 1000),
    )
    meetings.start(meeting)
    return meeting


class PartialRoundProvider:
    def __init__(self):
        self.calls = []
        self.failed_once = False

    def complete(self, model, system_prompt, user_prompt, context, max_output_tokens, response_schema=None):
        employee_name = context.split("EMPLOYEE\nNAME: ", 1)[1].split("\n", 1)[0]
        self.calls.append(employee_name)
        if employee_name == "Researcher" and not self.failed_once:
            self.failed_once = True
            return ProviderResult("", 20, 5, response_id="failed-billable",
                                  status="incomplete", incomplete_reason="max_output_tokens")
        return ProviderResult(f"{employee_name} contribution", 20, 5,
                              response_id=f"ok-{employee_name}", status="completed")


def test_partial_round_retry_skips_success_and_completes_missing_only(ctx, monkeypatch):
    meeting = active_meeting()
    provider = PartialRoundProvider()
    monkeypatch.setattr("eason_one.services.execution.get_provider", lambda key: provider)

    with pytest.raises(ValueError, match="incomplete"):
        meetings.advance(meeting)

    assert provider.calls == ["CEO", "Researcher"]
    assert meeting.current_round == 0
    ceo_messages = MeetingMessage.query.filter_by(
        meeting_id=meeting.id, round_number=1, employee_id=employees()[0].id,
        speaker_type="EMPLOYEE").all()
    assert len(ceo_messages) == 1
    ceo_run_id = ceo_messages[0].agent_run_id
    assert db.session.get(AgentRun, ceo_run_id).status == "SUCCEEDED"
    assert CostEvent.query.filter_by(agent_run_id=ceo_run_id).count() == 1
    failed_b = AgentRun.query.filter_by(
        meeting_id=meeting.id, employee_id=employees()[1].id, status="FAILED").one()
    assert CostEvent.query.filter_by(agent_run_id=failed_b.id).count() == 1

    meetings.advance(meeting)
    assert provider.calls == ["CEO", "Researcher", "Researcher", "Critic"]
    assert meeting.current_round == 1
    assert MeetingMessage.query.filter_by(
        meeting_id=meeting.id, round_number=1, employee_id=employees()[0].id,
        speaker_type="EMPLOYEE").count() == 1
    assert AgentRun.query.filter_by(
        meeting_id=meeting.id, employee_id=employees()[0].id,
        purpose="MEETING_CONTRIBUTION", status="SUCCEEDED").count() == 1
    assert CostEvent.query.filter_by(agent_run_id=ceo_run_id).count() == 1


def test_form_retry_after_partial_round_does_not_rebill_success(client, ctx, monkeypatch):
    meeting = active_meeting()
    provider = PartialRoundProvider()
    monkeypatch.setattr("eason_one.services.execution.get_provider", lambda key: provider)
    client.post(f"/meetings/{meeting.id}/advance")
    assert provider.calls == ["CEO", "Researcher"]
    client.post(f"/meetings/{meeting.id}/advance")
    assert provider.calls == ["CEO", "Researcher", "Researcher", "Critic"]
    assert db.session.get(type(meeting), meeting.id).current_round == 1


@pytest.mark.parametrize("ceiling", ["cost", "tokens"])
def test_meeting_limit_uses_full_execution_frame_before_provider(ctx, monkeypatch, ceiling):
    ceo = employees()[0]
    ceo.system_instructions = "SYSTEM-GOVERNANCE " * 1200
    ceo.current_model.input_price_per_million = Decimal("1000")
    meeting = active_meeting([ceo], token_limit=100000, real_cost_limit_twd=100000)
    context = meetings.compact_context(meeting, ceo)
    prompt = ceo.system_instructions + "\nMEETING_CONTRIBUTION\nBe compact. Follow the round protocol. Do not mutate authoritative company state."
    request = "Contribute to round 1"
    full = estimate_execution(ceo.current_model, prompt, context, request)
    context_only = estimate_execution(ceo.current_model, "", context, request)
    assert full.input_tokens > context_only.input_tokens
    if ceiling == "cost":
        meeting.real_cost_limit_twd = (full.real_cost + context_only.real_cost) / 2
    else:
        meeting.token_limit = (
            full.input_tokens + full.output_tokens +
            context_only.input_tokens + context_only.output_tokens
        ) // 2
    db.session.commit()
    calls = {"count": 0}
    monkeypatch.setattr(
        "eason_one.services.execution.get_provider",
        lambda key: calls.__setitem__("count", calls["count"] + 1),
    )
    with pytest.raises(ValueError, match="cost" if ceiling == "cost" else "token"):
        meetings.advance(meeting)
    assert calls["count"] == 0
    assert AgentRun.query.filter_by(meeting_id=meeting.id).count() == 0


def test_execute_and_meeting_share_canonical_estimate(ctx, monkeypatch):
    ceo = employees()[0]
    meeting = active_meeting([ceo])
    context = meetings.compact_context(meeting, ceo)
    prompt = ceo.system_instructions + "\nMEETING_CONTRIBUTION\nBe compact. Follow the round protocol. Do not mutate authoritative company state."
    request = "Contribute to round 1"
    estimate = estimate_execution(ceo.current_model, prompt, context, request)
    captured = {}

    def capture_budget(value):
        captured["value"] = value

    monkeypatch.setattr("eason_one.services.execution.ensure_budget", capture_budget)
    meetings.advance(meeting)
    assert captured["value"] == estimate.real_cost


def test_ceo_dashboard_shows_only_live_project_work(client, ctx):
    ceo, researcher, _ = employees()
    live = create_project("Visible Live", "live", ceo, status="ACTIVE", environment="LIVE")
    smoke = create_project("Hidden Smoke", "smoke", ceo, status="ACTIVE", environment="SMOKE")
    archived = create_project("Hidden Archive", "old", ceo, status="ACTIVE", environment="ARCHIVED")
    db.session.add_all([
        Task(project_id=live.id, title="Live blocked", objective="x", status="BLOCKED",
             assigned_employee_id=researcher.id),
        Task(project_id=smoke.id, title="Smoke blocked", objective="x", status="BLOCKED",
             assigned_employee_id=researcher.id),
        Task(project_id=archived.id, title="Archived done", objective="x", status="DONE",
             assigned_employee_id=researcher.id),
    ])
    db.session.commit()
    page = client.get("/command").get_data(as_text=True)
    assert "Visible Live" in page
    assert "Hidden Smoke" not in page
    assert "Hidden Archive" not in page
    assert "Founder decision required" in page
    assert "Live blocked" in page
    assert "Archived done" not in page


def test_structured_synthesis_populates_minutes_from_validated_output(ctx):
    meeting = active_meeting()
    meetings.advance(meeting)
    meetings.intervene(meeting, "Founder intervention that must be recorded.") if meeting.founder_joined_at else meetings.join_founder(meeting)
    if not MeetingMessage.query.filter_by(meeting_id=meeting.id, message_type="FOUNDER_INTERVENTION").first():
        meetings.intervene(meeting, "Founder intervention that must be recorded.")
    meetings.end_and_synthesize(meeting)
    assert meeting.minutes_json["agreements"] == ["Use the smallest bounded next action."]
    assert meeting.minutes_json["evidence_referenced"] == [
        "Only evidence recorded in the Meeting context was considered."
    ]
    assert meeting.minutes_json["founder_interventions"] == [
        "Founder intervention that must be recorded."
    ]
    assert meeting.minutes_json["token_usage"] > 0
    assert meeting.minutes_json["real_cost_twd"] == "0"


class InvalidSynthesisProvider:
    def complete(self, model, system_prompt, user_prompt, context, max_output_tokens, response_schema=None):
        return ProviderResult(json.dumps({"agreements": ["misleading partial"]}), 30, 10,
                              response_id="billable-invalid", status="completed")


def test_invalid_synthesis_keeps_cost_and_meeting_recoverable(ctx, monkeypatch):
    ceo = employees()[0]
    meeting = active_meeting([ceo])
    provider = InvalidSynthesisProvider()
    monkeypatch.setattr("eason_one.services.execution.get_provider", lambda key: provider)
    with pytest.raises(ValueError, match="validation failed"):
        meetings.end_and_synthesize(meeting)
    run = AgentRun.query.filter_by(meeting_id=meeting.id, purpose="MEETING_SYNTHESIS").one()
    assert run.status == "FAILED"
    assert CostEvent.query.filter_by(agent_run_id=run.id).count() == 1
    assert meeting.status == "ACTIVE"
    assert meeting.minutes_json is None
