import json
import os
from decimal import Decimal
from types import SimpleNamespace

import pytest

from eason_one.extensions import db
from eason_one.models import AgentRun, CostEvent, Employee, MeetingMessage, ModelConfig
from eason_one.providers import AnthropicProvider, anthropic_compatible_schema
from eason_one.schemas import (
    CEO_SCHEMA, MEETING_RESPONSE_SCHEMA, MEETING_ROUND1_SCHEMA, MEETING_SYNTHESIS_SCHEMA,
    REVIEW_SCHEMA, SYNTHESIS_SCHEMA, TASK_EXECUTION_SCHEMA,
)
from eason_one.services import meetings
from eason_one.services.ceo import founder_request
from eason_one.services.execution import execute
from eason_one.services.model_configs import create as create_model


def anthropic_model(**overrides):
    values = {
        "label": "Claude Test", "provider_key": "anthropic",
        "model_name": "claude-test-exact", "input_price": "10",
        "output_price": "30", "currency": "TWD", "max_output_tokens": 500,
    }
    values.update(overrides)
    return create_model(**values)


def assign(employee=None, **overrides):
    employee = employee or Employee.query.filter_by(slug="ceo").one()
    employee.current_model = anthropic_model(**overrides)
    db.session.commit()
    return employee


def message(text="provider text", stop_reason="end_turn", input_tokens=12,
            output_tokens=4, cache_creation=0, cache_read=0):
    return SimpleNamespace(
        id="msg_anthropic_123", _request_id="req_anthropic_456",
        content=[
            SimpleNamespace(type="text", text=text),
            SimpleNamespace(type="tool_use", name="not-enabled", input={"ignored": True}),
        ],
        stop_reason=stop_reason,
        usage=SimpleNamespace(
            input_tokens=input_tokens, output_tokens=output_tokens,
            cache_creation_input_tokens=cache_creation,
            cache_read_input_tokens=cache_read,
        ),
    )


def fake_sdk(monkeypatch, response=None, error=None):
    captured = {}

    class Messages:
        def create(self, **kwargs):
            captured["kwargs"] = kwargs
            if error:
                raise error
            return response or message()

    class Client:
        def __init__(self, **kwargs):
            captured["client"] = kwargs
            self.messages = Messages()

    monkeypatch.setattr("anthropic.Anthropic", Client)
    return captured


def test_dependency_and_model_service_accept_anthropic(ctx):
    assert "anthropic>=0.117.0,<1" in open("pyproject.toml", encoding="utf-8").read()
    model = anthropic_model()
    assert model.provider_key == "anthropic" and model.model_name == "claude-test-exact"


@pytest.mark.parametrize(("field", "value"), [
    ("input_price", "0"), ("output_price", "0"),
])
def test_anthropic_zero_price_rejected(ctx, field, value):
    with pytest.raises(ValueError, match="greater than zero"):
        anthropic_model(**{field: value})


def test_anthropic_wrong_currency_rejected(ctx):
    with pytest.raises(ValueError, match="currency"):
        anthropic_model(currency="USD")


def test_inactive_anthropic_model_rejected_before_provider(ctx, monkeypatch):
    employee = assign()
    employee.current_model.active = False
    db.session.commit()
    called = []
    monkeypatch.setattr("eason_one.services.execution.get_provider", lambda key: called.append(key))
    with pytest.raises(ValueError, match="inactive"):
        execute(employee, "TEST", "x")
    assert called == [] and AgentRun.query.count() == 0


def test_messages_api_exact_mapping_and_environment_key(ctx, monkeypatch):
    model = anthropic_model(model_name="claude-sonnet-exact-id", max_output_tokens=777)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "environment-only-test-key")
    captured = fake_sdk(monkeypatch)
    result = AnthropicProvider().complete(model, "SYSTEM RULES", "FOUNDER REQUEST", "EFFECTIVE CONTEXT", 777)
    kwargs = captured["kwargs"]
    assert captured["client"] == {"api_key": "environment-only-test-key", "max_retries": 0}
    assert kwargs["model"] == "claude-sonnet-exact-id"
    assert kwargs["max_tokens"] == 777
    assert kwargs["system"] == "SYSTEM RULES"
    assert kwargs["messages"] == [{"role": "user", "content": "EFFECTIVE CONTEXT\n\nUSER REQUEST:\nFOUNDER REQUEST"}]
    assert "system" not in kwargs["messages"][0]
    assert result.text == "provider text"  # unrelated tool block was not concatenated


def test_anthropic_key_is_required_from_environment(ctx, monkeypatch):
    model = anthropic_model()
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    called = []
    monkeypatch.setattr("anthropic.Anthropic", lambda **kwargs: called.append(kwargs))
    with pytest.raises(KeyError, match="ANTHROPIC_API_KEY"):
        AnthropicProvider().complete(model, "s", "u", "c", 10)
    assert called == []


@pytest.mark.parametrize("contract", [
    CEO_SCHEMA, REVIEW_SCHEMA, SYNTHESIS_SCHEMA, TASK_EXECUTION_SCHEMA,
    MEETING_SYNTHESIS_SCHEMA, MEETING_ROUND1_SCHEMA, MEETING_RESPONSE_SCHEMA,
])
def test_all_structured_contracts_convert_for_anthropic(ctx, monkeypatch, contract):
    model = anthropic_model()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
    captured = fake_sdk(monkeypatch)
    original = json.loads(json.dumps(contract["schema"]))
    AnthropicProvider().complete(model, "s", "u", "c", 10, contract)
    sent = captured["kwargs"]["output_config"]["format"]
    assert sent["type"] == "json_schema"
    assert sent["schema"]["type"] == original["type"]
    assert sent["schema"]["required"] == original["required"]
    assert sent["schema"]["properties"].keys() == original["properties"].keys()
    assert contract["schema"] == original  # adapter never weakens the application contract


def test_schema_adapter_removes_only_provider_unsupported_assertions():
    source = {"type": "object", "required": ["items"], "additionalProperties": False,
              "properties": {"items": {"type": "array", "maxItems": 2,
                                       "items": {"type": "string", "maxLength": 5}}}}
    converted = anthropic_compatible_schema(source)
    assert converted["required"] == ["items"]
    assert converted["additionalProperties"] is False
    assert converted["properties"]["items"]["items"]["type"] == "string"
    assert "maxItems" not in converted["properties"]["items"]
    assert "maxLength" not in converted["properties"]["items"]["items"]
    assert "maxItems: 2" in converted["properties"]["items"]["description"]
    assert "maxLength: 5" in converted["properties"]["items"]["items"]["description"]
    assert source["properties"]["items"]["maxItems"] == 2


def test_application_validation_still_rejects_invalid_anthropic_content(ctx, monkeypatch):
    employee = assign()
    invalid = json.dumps({"mode": "NOT_ALLOWED", "executive_response": "x"})
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
    fake_sdk(monkeypatch, message(invalid))
    run, proposal = founder_request(employee, "status")
    assert proposal is None
    assert "Invalid CEO request mode" in run.error_text
    assert run.parsed_output_json is None


def test_response_ids_usage_and_normal_completion_persist(ctx, monkeypatch):
    employee = assign()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
    fake_sdk(monkeypatch, message("ok", input_tokens=19, output_tokens=7))
    run = execute(employee, "TEST", "request")
    assert run.status == "SUCCEEDED"
    assert run.provider_response_id == "msg_anthropic_123"
    assert run.provider_request_id == "req_anthropic_456"
    assert (run.input_tokens, run.output_tokens) == (19, 7)
    assert (run.cache_creation_input_tokens, run.cache_read_input_tokens) == (0, 0)
    assert CostEvent.query.filter_by(agent_run_id=run.id).count() == 1


@pytest.mark.parametrize(("stop_reason", "text", "error_fragment"), [
    ("max_tokens", "partial", "response incomplete: max_tokens"),
    ("refusal", "I cannot comply", "refusal: I cannot comply"),
])
def test_billable_anthropic_non_normal_ledgers_exactly_once(
        ctx, monkeypatch, stop_reason, text, error_fragment):
    employee = assign()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
    fake_sdk(monkeypatch, message(text, stop_reason, input_tokens=20, output_tokens=5))
    run = execute(employee, "TEST", "request")
    assert run.status == "FAILED" and error_fragment in run.error_text
    assert (run.input_tokens, run.output_tokens) == (20, 5)
    assert run.real_cost == Decimal("0.000350")
    assert CostEvent.query.filter_by(agent_run_id=run.id).count() == 1


def test_transport_exception_creates_no_fake_cost(ctx, monkeypatch):
    employee = assign()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
    fake_sdk(monkeypatch, error=RuntimeError("transport unavailable"))
    with pytest.raises(RuntimeError, match="transport unavailable"):
        execute(employee, "TEST", "request")
    run = AgentRun.query.one()
    assert run.status == "FAILED"
    assert CostEvent.query.filter_by(agent_run_id=run.id).count() == 0


def test_unexpected_cache_usage_is_explicit_and_not_claimed_success(ctx, monkeypatch):
    employee = assign()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
    fake_sdk(monkeypatch, message("ok", cache_creation=3, cache_read=2))
    run = execute(employee, "TEST", "request")
    assert run.status == "FAILED"
    assert "exact cache-cost reconciliation is not enabled" in run.error_text
    assert (run.cache_creation_input_tokens, run.cache_read_input_tokens) == (3, 2)
    assert run.real_cost is None
    assert CostEvent.query.filter_by(agent_run_id=run.id).count() == 0


def test_anthropic_run_snapshots_are_immutable(ctx, monkeypatch):
    employee = assign(model_name="claude-original", input_price="11", output_price="33")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
    fake_sdk(monkeypatch)
    run = execute(employee, "TEST", "request")
    employee.current_model.model_name = "claude-changed"
    employee.current_model.input_price_per_million = 99
    employee.current_model.provider_key = "mock"
    db.session.commit()
    assert run.provider_key_snapshot == "anthropic"
    assert run.model_name_snapshot == "claude-original"
    assert run.input_price_snapshot == Decimal("11.0000")
    assert run.output_price_snapshot == Decimal("33.0000")
    assert run.currency_snapshot == "TWD"


def test_anthropic_participant_flows_through_meeting_ledger(ctx, monkeypatch):
    employee = assign(Employee.query.filter_by(slug="researcher").one())
    meeting = meetings.create("Claude review", "Test provider neutrality", "One bounded position",
                              employee, [employee], token_limit=10000, real_cost_limit_twd=10)
    meetings.start(meeting)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
    fake_sdk(monkeypatch, message("Anthropic participant contribution", input_tokens=25, output_tokens=6))
    meetings.advance(meeting)
    linked = MeetingMessage.query.filter_by(
        meeting_id=meeting.id, employee_id=employee.id, round_number=1).one()
    run = db.session.get(AgentRun, linked.agent_run_id)
    assert run.provider_key_snapshot == "anthropic"
    assert meetings.usage(meeting) == (31, run.real_cost)


def test_meeting_ceiling_blocks_anthropic_before_invocation(ctx, monkeypatch):
    employee = assign()
    meeting = meetings.create("Ceiling", "Reject before provider", "Bounded",
                              employee, [employee], token_limit=1, real_cost_limit_twd=10)
    meetings.start(meeting)
    called = []
    monkeypatch.setattr("eason_one.services.execution.get_provider", lambda key: called.append(key))
    with pytest.raises(ValueError, match="token limit"):
        meetings.advance(meeting)
    assert called == [] and AgentRun.query.filter_by(meeting_id=meeting.id).count() == 0
