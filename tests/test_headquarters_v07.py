from decimal import Decimal
import json

from eason_one.extensions import db
from eason_one.models import (
    AgentRun,
    Employee,
    MeetingMessage,
    ModelConfig,
    ResearchRecord,
)
from eason_one.services import meetings
from eason_one.services.headquarters import _briefing_composition


def _meeting(status="PLANNED"):
    ceo = Employee.query.filter_by(slug="ceo").one()
    critic = Employee.query.filter_by(slug="critic").one()
    meeting = meetings.create(
        "Prompt reliability review",
        "Decide whether the current execution path is reliable for Founder dogfooding.",
        "Identify the highest-risk failure and the smallest verifiable correction.",
        ceo,
        [critic],
        profile="ECONOMY",
        real_cost_limit_twd=Decimal("2"),
    )
    if status == "RUNNING":
        meetings.start_auto(meeting)
    return meeting, ceo, critic


def _run(employee, meeting, status, *, retry_of=None, failure_reason=None):
    model = employee.current_model
    row = AgentRun(
        employee_id=employee.id,
        meeting_id=meeting.id,
        model_config_id=model.id,
        purpose="MEETING_CONTRIBUTION",
        user_request="Contribute",
        system_prompt_snapshot="precise prompt",
        context_snapshot="bounded context",
        raw_output="{}",
        status=status,
        provider_response_id=f"resp-{status}-{retry_of.id if retry_of else 0}",
        provider_stop_reason="max_tokens" if failure_reason else "end_turn",
        failure_reason=failure_reason,
        prompt_version="meeting-round1-v2.0",
        prompt_hash="a" * 64,
        context_hash="b" * 64,
        output_hash="c" * 64,
        retry_of_run_id=retry_of.id if retry_of else None,
        input_tokens=100,
        output_tokens=50,
        effective_max_output_tokens=512,
        real_cost=Decimal("0.1"),
        provider_key_snapshot=model.provider_key,
        model_name_snapshot=model.model_name,
        input_price_snapshot=model.input_price_per_million,
        output_price_snapshot=model.output_price_per_million,
        currency_snapshot=model.currency,
    )
    db.session.add(row)
    db.session.commit()
    return row


def test_research_ledger_renders_and_exports(client, ctx):
    page = client.get("/headquarters/research").get_data(as_text=True)
    assert "LIVING RESEARCH LEDGER" in page
    assert "EXPORT FULL JSON" in page
    assert "CAPTURE A RESEARCH RECORD" in page
    export = client.get("/headquarters/research/export.json")
    assert export.status_code == 200
    payload = export.get_json()
    assert payload["schema"] == "eason-one-research-export-v1"
    csv_export = client.get("/headquarters/research/runs.csv")
    assert csv_export.status_code == 200
    assert "prompt_version" in csv_export.get_data(as_text=True).splitlines()[0]


def test_founder_can_capture_research_record(client, ctx):
    response = client.post(
        "/headquarters/research/records",
        data={
            "record_type": "FIX",
            "status": "VALIDATED",
            "title": "Meeting Start became idempotent",
            "summary": "Repeated Start now attaches to the existing runner instead of creating a false failure.",
            "version_label": "Headquarters V0.7",
            "source_ref": "TEST-V07",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    record = ResearchRecord.query.one()
    assert record.record_type == "FIX"
    assert record.founder_approved is True
    assert "Meeting Start became idempotent" in response.get_data(as_text=True)


def test_employee_dossier_exposes_governed_ai_core_change(client, ctx):
    employee = Employee.query.filter_by(slug="critic").one()
    page = client.get(f"/headquarters/employees/{employee.id}#ai-core").get_data(as_text=True)
    assert "Change AI Core" in page
    assert f'action="/employees/{employee.id}/model"' in page
    assert "Identity stays. Engine changes." in page
    assert "AI CORE HISTORY" in page


def test_auto_start_is_idempotent(ctx):
    meeting, _, _ = _meeting()
    first = meetings.start_auto(meeting)
    started_at = first.started_at
    count = MeetingMessage.query.filter_by(
        meeting_id=meeting.id,
        message_type="SYSTEM",
    ).filter(MeetingMessage.content.like("Autonomous Meeting started%" )).count()
    second = meetings.start_auto(meeting)
    assert second.status == "RUNNING"
    assert second.started_at == started_at
    assert MeetingMessage.query.filter_by(
        meeting_id=meeting.id,
        message_type="SYSTEM",
    ).filter(MeetingMessage.content.like("Autonomous Meeting started%" )).count() == count == 1


def test_cross_round_context_contains_structured_state_and_message_ids(ctx):
    meeting, ceo, critic = _meeting("RUNNING")
    message = MeetingMessage(
        meeting_id=meeting.id,
        employee_id=ceo.id,
        speaker_type="EMPLOYEE",
        round_number=1,
        message_type="POSITION",
        content=json.dumps(
            {
                "position": "Truncated output must not mutate authoritative state.",
                "actions": ["Add an acceptance test"],
                "risk": "A paid response can be unusable.",
                "evidence_ids": [],
                "confidence": 0.9,
            }
        ),
        validation_status="VALID",
    )
    db.session.add(message)
    db.session.commit()
    packet = meetings.compact_context(meeting, critic, target_round=2)
    assert "STRUCTURED MEETING STATE" in packet
    assert f"MEETING_MESSAGE_ID: {message.id}" in packet
    assert "Truncated output must not mutate authoritative state" in packet
    assert "Do not repeat accepted points" in packet


def test_precise_prompt_compiler_and_compact_repair_contract(ctx):
    _, _, critic = _meeting()
    prompt, version = meetings._compile_contribution_prompt(
        critic, "MEETING_CONTRIBUTION_RESPONSE", 1, repair=True
    )
    for heading in (
        "PROMPT VERSION",
        "ROLE",
        "CURRENT TURN",
        "OUTPUT CONTRACT",
        "COMPLETION STANDARD",
        "PROHIBITED",
        "RECOVERY MODE",
    ):
        assert heading in prompt
    assert "No essay" in prompt
    assert "No chain-of-thought" in prompt
    assert version == meetings.PROMPT_VERSIONS["repair"]


def test_call_breakdown_separates_success_failure_and_retry(ctx):
    meeting, ceo, critic = _meeting("RUNNING")
    first = _run(ceo, meeting, "SUCCEEDED")
    failed = _run(critic, meeting, "FAILED", failure_reason="OUTPUT_TRUNCATED")
    _run(critic, meeting, "SUCCEEDED", retry_of=failed)
    breakdown = meetings.call_breakdown(meeting)
    assert breakdown == {
        "total": 3,
        "successful": 2,
        "failed": 1,
        "retries": 1,
        "truncated": 1,
    }


def test_planned_preview_remains_spatial_not_live_tiles(client, ctx):
    meeting, _, _ = _meeting()
    page = client.get(f"/headquarters/meetings/{meeting.id}").get_data(as_text=True)
    assert "PREPARED ROOM" in page
    assert 'class="meeting-space participants-' in page
    assert 'class="hq-participant-seat preview-person' in page
    planned_section = page.split("PREPARED ROOM", 1)[1].split("ROOM BRIEF", 1)[0]
    assert "No validated contribution yet." not in planned_section


def test_run_audit_exposes_prompt_hash_and_retry_lineage(client, ctx):
    meeting, ceo, critic = _meeting("RUNNING")
    failed = _run(critic, meeting, "FAILED", failure_reason="OUTPUT_TRUNCATED")
    retry = _run(critic, meeting, "SUCCEEDED", retry_of=failed)
    page = client.get(f"/headquarters/system/runs/{retry.id}").get_data(as_text=True)
    assert "Prompt version" in page
    assert "Prompt hash" in page
    assert f"Retry of Run #{failed.id}" in page
    assert "Prompt and context audit" in page


def test_status_report_composition_avoids_duplicate_support_cards(ctx):
    focus = {
        "next_result": "A tested demo",
    }
    employee = {"state": "WORKING"}
    composition = _briefing_composition(
        focus=focus,
        employees=[employee],
        meetings=[],
        governance=[],
        reliability=[],
        latest_result=None,
        activity=[{"kind": "TASK"}],
        ceo_answer={"kind": "STATUS_REPORT", "summary": "Validated company report"},
        ceo_query="status",
    )
    kinds = [block["kind"] for block in composition["blocks"]]
    assert kinds[:2] == ["ceo_report", "mission"]
    assert "next_result" not in kinds
    assert "people" not in kinds
    assert "changes" not in kinds
