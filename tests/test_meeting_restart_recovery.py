import json
from datetime import timedelta

import pytest

from eason_one.extensions import db
from eason_one.models import AgentRun, Employee, ExternalEffectAttempt, Meeting, MeetingMessage, MeetingStep, now
from eason_one.providers import ProviderResult
from eason_one.services import meetings, runtime_recovery


def _people():
    return [
        Employee.query.filter_by(slug=slug).one()
        for slug in ("ceo", "researcher")
    ]


def _running(profile="ECONOMY"):
    people = _people()
    meeting = meetings.create(
        "Restart recovery",
        "Reach one bounded recommendation",
        "Resolve the current issue",
        people[0],
        people,
        profile=profile,
    )
    meetings.start_auto(meeting)
    return meeting


class ContributionProvider:
    def __init__(self):
        self.calls = 0

    def complete(self, model, system, user, context, max_tokens, response_schema=None, tool_mode=None):
        self.calls += 1
        return ProviderResult(
            json.dumps({
                "position": "Use the persisted evidence before spending again.",
                "actions": ["Resume local materialization"],
                "risk": "Duplicate provider spend",
                "evidence_ids": [],
                "confidence": 0.9,
            }),
            20,
            10,
            response_id=f"response-{self.calls}",
            request_id=f"request-{self.calls}",
        )


def test_paid_meeting_contribution_is_materialized_after_restart_without_second_provider_call(ctx, monkeypatch):
    meeting = _running()
    provider = ContributionProvider()
    monkeypatch.setattr("eason_one.services.execution.get_provider", lambda key: provider)

    original = meetings._commit_contribution_run

    def crash_after_paid_response(*args, **kwargs):
        raise SystemExit("simulated process death after execute() committed")

    monkeypatch.setattr(meetings, "_commit_contribution_run", crash_after_paid_response)
    with pytest.raises(SystemExit):
        meetings.next_step(meeting)

    step = MeetingStep.query.filter_by(meeting_id=meeting.id, kind="CONTRIBUTION").one()
    run = AgentRun.query.filter_by(meeting_id=meeting.id, purpose="MEETING_CONTRIBUTION").one()
    assert provider.calls == 1
    assert step.status == "RUNNING"
    assert run.status == "SUCCEEDED"
    assert (run.context_composition_json or {})["meeting_step"]["id"] == step.id
    assert MeetingMessage.query.filter_by(meeting_id=meeting.id, agent_run_id=run.id).count() == 0

    monkeypatch.setattr(meetings, "_commit_contribution_run", original)
    recovered = runtime_recovery.recover_interrupted_meeting_steps(
        before=now() + timedelta(seconds=1)
    )

    db.session.expire_all()
    step = db.session.get(MeetingStep, step.id)
    assert recovered == 1
    assert provider.calls == 1
    assert step.status == "SUCCEEDED"
    assert step.agent_run_id == run.id
    assert MeetingMessage.query.filter_by(meeting_id=meeting.id, agent_run_id=run.id).count() == 1


class ProcessDeathDuringDispatch:
    def __init__(self):
        self.calls = 0

    def complete(self, *args, **kwargs):
        self.calls += 1
        # BaseException deliberately models a process death. execute() must not
        # convert this into a safe ordinary Exception path.
        raise SystemExit("simulated process death while provider request is in flight")


def test_restart_after_meeting_dispatch_never_blind_replays_unknown_provider_effect(ctx, monkeypatch):
    meeting = _running()
    provider = ProcessDeathDuringDispatch()
    monkeypatch.setattr("eason_one.services.execution.get_provider", lambda key: provider)

    with pytest.raises(SystemExit):
        meetings.next_step(meeting)

    step = MeetingStep.query.filter_by(meeting_id=meeting.id, kind="CONTRIBUTION").one()
    run = AgentRun.query.filter_by(meeting_id=meeting.id, purpose="MEETING_CONTRIBUTION").one()
    effect = ExternalEffectAttempt.query.filter_by(execution_id=run.id).one()
    assert provider.calls == 1
    assert step.status == "RUNNING"
    assert run.status == "RUNNING"
    assert effect.state == "DISPATCHING"

    recovered = runtime_recovery.recover_interrupted_meeting_steps(
        before=now() + timedelta(seconds=1)
    )

    db.session.expire_all()
    step = db.session.get(MeetingStep, step.id)
    run = db.session.get(AgentRun, run.id)
    effect = ExternalEffectAttempt.query.filter_by(execution_id=run.id).one()
    meeting = db.session.get(Meeting, meeting.id)
    assert recovered == 1
    assert provider.calls == 1
    assert step.status == "AMBIGUOUS"
    assert run.outcome == "FAILED_AMBIGUOUS"
    assert effect.state == "AMBIGUOUS_POST_DISPATCH"
    assert meeting.kernel_status == "CANCELLED"
    assert (meeting.minutes_json or {}).get("recovery", {}).get("kind") == "INTERNAL_REPLAN_REQUIRED"


class SynthesisProvider:
    def __init__(self):
        self.calls = 0

    def complete(self, model, system, user, context, max_tokens, response_schema=None, tool_mode=None):
        self.calls += 1
        return ProviderResult(
            json.dumps({
                "agreements": ["Use the accepted evidence."],
                "disagreements": [],
                "evidence_referenced": [],
                "actions": ["Continue with the verified recommendation."],
                "founder_decisions_required": [],
                "rejected_or_unresolved": [],
            }),
            20,
            10,
            response_id=f"synthesis-{self.calls}",
            request_id=f"synthesis-request-{self.calls}",
        )


def test_paid_meeting_synthesis_is_closed_locally_after_restart(ctx, monkeypatch):
    meeting = _running(profile="STANDARD")
    meeting.routing_json = {"ready_for_synthesis": True, "reason": "Enough evidence."}
    db.session.commit()
    provider = SynthesisProvider()
    monkeypatch.setattr("eason_one.services.execution.get_provider", lambda key: provider)

    original = meetings._commit_synthesis_run
    monkeypatch.setattr(
        meetings,
        "_commit_synthesis_run",
        lambda *args, **kwargs: (_ for _ in ()).throw(SystemExit("crash before Meeting close")),
    )
    with pytest.raises(SystemExit):
        meetings.next_step(meeting)

    step = MeetingStep.query.filter_by(meeting_id=meeting.id, kind="FINAL_SYNTHESIS").one()
    run = AgentRun.query.filter_by(meeting_id=meeting.id, purpose="MEETING_SYNTHESIS").one()
    assert provider.calls == 1
    assert run.status == "SUCCEEDED"
    assert step.status == "RUNNING"

    monkeypatch.setattr(meetings, "_commit_synthesis_run", original)
    recovered = runtime_recovery.recover_interrupted_meeting_steps(
        before=now() + timedelta(seconds=1)
    )

    db.session.expire_all()
    meeting = db.session.get(Meeting, meeting.id)
    step = db.session.get(MeetingStep, step.id)
    assert recovered == 1
    assert provider.calls == 1
    assert step.status == "SUCCEEDED"
    assert meeting.kernel_status == "COMPLETED"
    assert meeting.status == "ENDED"
    assert (meeting.minutes_json or {}).get("agreements") == ["Use the accepted evidence."]
