from decimal import Decimal
from types import SimpleNamespace

import pytest

from eason_one.extensions import db
from eason_one.models import AgentRun, CostEvent, Employee
from eason_one.providers import AnthropicProvider, OpenAIProvider
from eason_one.schemas import MEETING_SYNTHESIS_SCHEMA
from eason_one.services import meetings
from eason_one.services.costs import estimate_execution
from eason_one.services.execution import execute
from eason_one.services.model_configs import create as create_model


def anthropic_employee():
    employee = Employee.query.filter_by(slug="ceo").one()
    model = create_model("Anthropic gate", "anthropic", "claude-gate-test",
                         "1000", "2000", "TWD", 500)
    employee.current_model = model
    db.session.commit()
    return employee


def test_real_sdk_clients_disable_hidden_retries(monkeypatch):
    openai_args = {}
    anthropic_args = {}

    openai_response = SimpleNamespace(
        output_text="ok", usage=SimpleNamespace(input_tokens=1, output_tokens=1),
        id="openai-id", _request_id="openai-request", status="completed",
        output=[], incomplete_details=None,
    )

    class OpenAIClient:
        def __init__(self, **kwargs):
            openai_args.update(kwargs)
            self.responses = SimpleNamespace(create=lambda **kwargs: openai_response)

    anthropic_response = SimpleNamespace(
        id="anthropic-id", _request_id="anthropic-request", stop_reason="end_turn",
        content=[SimpleNamespace(type="text", text="ok")],
        usage=SimpleNamespace(input_tokens=1, output_tokens=1,
                              cache_creation_input_tokens=0, cache_read_input_tokens=0),
    )

    class AnthropicClient:
        def __init__(self, **kwargs):
            anthropic_args.update(kwargs)
            self.messages = SimpleNamespace(create=lambda **kwargs: anthropic_response)

    monkeypatch.setattr("openai.OpenAI", OpenAIClient)
    monkeypatch.setattr("anthropic.Anthropic", AnthropicClient)
    monkeypatch.setenv("OPENAI_API_KEY", "fake-openai")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-anthropic")
    model = SimpleNamespace(model_name="exact-model")
    OpenAIProvider().complete(model, "s", "u", "c", 10)
    AnthropicProvider().complete(model, "s", "u", "c", 10)
    assert openai_args == {"api_key": "fake-openai", "max_retries": 0}
    assert anthropic_args == {"api_key": "fake-anthropic", "max_retries": 0}


def test_schema_aware_execution_estimate_is_canonical(ctx):
    employee = anthropic_employee()
    without = estimate_execution(employee.current_model, "s", "c", "u")
    with_schema = estimate_execution(
        employee.current_model, "s", "c", "u",
        response_schema=MEETING_SYNTHESIS_SCHEMA,
    )
    assert with_schema.input_tokens > without.input_tokens
    assert with_schema.real_cost > without.real_cost
    assert "RESPONSE SCHEMA:" in with_schema.framed_text


@pytest.mark.parametrize("ceiling", ["cost", "token"])
def test_schema_aware_meeting_ceiling_rejects_before_provider(ctx, monkeypatch, ceiling):
    employee = anthropic_employee()
    meeting = meetings.create("Schema gate", "Bound synthesis", "Synthesize",
                              employee, [employee], token_limit=100000,
                              real_cost_limit_twd=100000)
    meetings.start(meeting)
    context = meetings.compact_context(meeting, employee)
    prompt = employee.system_instructions + (
        "\nMEETING_SYNTHESIS\nSummarize agreements, disagreements, evidence, "
        "unresolved issues, actions, and Founder decisions required.")
    request = "Produce final Meeting synthesis"
    without = estimate_execution(employee.current_model, prompt, context, request)
    with_schema = estimate_execution(
        employee.current_model, prompt, context, request,
        response_schema=MEETING_SYNTHESIS_SCHEMA,
    )
    if ceiling == "cost":
        meeting.real_cost_limit_twd = (without.real_cost + with_schema.real_cost) / 2
    else:
        meeting.token_limit = (
            without.input_tokens + without.output_tokens +
            with_schema.input_tokens + with_schema.output_tokens
        ) // 2
    db.session.commit()
    calls = []
    monkeypatch.setattr("eason_one.services.execution.get_provider",
                        lambda key: calls.append(key))
    with pytest.raises(ValueError, match=ceiling):
        meetings.end_and_synthesize(meeting)
    assert calls == []
    assert AgentRun.query.filter_by(meeting_id=meeting.id).count() == 0


def test_execute_company_gate_uses_schema_aware_estimate(ctx, monkeypatch):
    employee = anthropic_employee()
    schema_estimate = estimate_execution(
        employee.current_model, employee.system_instructions, "", "request",
        response_schema=MEETING_SYNTHESIS_SCHEMA,
    )
    captured = {}
    monkeypatch.setattr("eason_one.services.execution.ensure_budget",
                        lambda value: captured.setdefault("estimate", value))

    class Provider:
        def complete(self, *args):
            return SimpleNamespace(
                text="{}", input_tokens=1, output_tokens=1, response_id="r",
                request_id="q", status="completed", refusal=None,
                incomplete_reason=None, cache_creation_input_tokens=0,
                cache_read_input_tokens=0,
            )

    monkeypatch.setattr("eason_one.services.execution.get_provider", lambda key: Provider())
    execute(employee, "TEST", "request", context_override="",
            response_schema=MEETING_SYNTHESIS_SCHEMA)
    assert captured["estimate"] == schema_estimate.real_cost


def test_zero_cache_usage_records_one_exact_cost(ctx, monkeypatch):
    employee = anthropic_employee()

    class Provider:
        def complete(self, *args):
            return SimpleNamespace(
                text="ok", input_tokens=20, output_tokens=5, response_id="r",
                request_id="q", status="completed", refusal=None,
                incomplete_reason=None, cache_creation_input_tokens=0,
                cache_read_input_tokens=0,
            )

    monkeypatch.setattr("eason_one.services.execution.get_provider", lambda key: Provider())
    run = execute(employee, "TEST", "request")
    assert run.status == "SUCCEEDED" and run.real_cost == Decimal("0.030000")
    assert CostEvent.query.filter_by(agent_run_id=run.id).count() == 1


def test_cache_usage_is_auditable_but_has_no_understated_cost_event(ctx, client, monkeypatch):
    employee = anthropic_employee()

    class Provider:
        def complete(self, *args):
            return SimpleNamespace(
                text="ok", input_tokens=20, output_tokens=5, response_id="r",
                request_id="q", status="completed", refusal=None,
                incomplete_reason=None, cache_creation_input_tokens=7,
                cache_read_input_tokens=3,
            )

    monkeypatch.setattr("eason_one.services.execution.get_provider", lambda key: Provider())
    run = execute(employee, "TEST", "request")
    assert run.status == "FAILED" and run.real_cost is None
    assert (run.cache_creation_input_tokens, run.cache_read_input_tokens) == (7, 3)
    assert CostEvent.query.filter_by(agent_run_id=run.id).count() == 0
    page = client.get(f"/runs/{run.id}").get_data(as_text=True)
    assert "UNRECONCILED" in page
    assert "7 creation / 3 read" in page
