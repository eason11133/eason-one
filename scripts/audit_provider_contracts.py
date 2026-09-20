#!/usr/bin/env python3
"""Offline provider-contract audit for the First Usable Project release path.

No real provider request is made. Fake SDK/HTTP transports assert the exact
request shapes and the shared retry/error normalization Eason One depends on.
"""
from __future__ import annotations

import io
import importlib.util
import json
import os
from pathlib import Path
from types import ModuleType, SimpleNamespace
import sys
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "eason_one"


def _load_runtime_modules():
    # Avoid importing eason_one.__init__ (and therefore Flask) so this audit is
    # runnable in a clean release/installer environment.
    pkg = ModuleType("eason_one")
    pkg.__path__ = [str(PKG)]
    sys.modules["eason_one"] = pkg
    for name in ("provider_protocol", "providers"):
        spec = importlib.util.spec_from_file_location(f"eason_one.{name}", PKG / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[f"eason_one.{name}"] = module
        assert spec.loader is not None
        spec.loader.exec_module(module)
    return sys.modules["eason_one.provider_protocol"], sys.modules["eason_one.providers"]


protocol, providers = _load_runtime_modules()
SCHEMA = {
    "name": "task_execution",
    "schema": {
        "type": "object",
        "properties": {"result_summary": {"type": "string"}},
        "required": ["result_summary"],
        "additionalProperties": False,
    },
}
MODEL = SimpleNamespace(model_name="audit-model")


class _FakeResponses:
    def __init__(self, owner): self.owner = owner
    def create(self, **kwargs):
        self.owner.last_responses_kwargs = kwargs
        usage = SimpleNamespace(input_tokens=100, output_tokens=40)
        return SimpleNamespace(
            output_text='{"result_summary":"ok"}', usage=usage, id="resp_audit",
            _request_id="req_openai", status="completed", output=[], incomplete_details=None,
        )


class _FakeChatCompletions:
    def __init__(self, owner): self.owner = owner
    def create(self, **kwargs):
        self.owner.last_chat_kwargs = kwargs
        usage = SimpleNamespace(prompt_tokens=100, completion_tokens=40)
        choice = SimpleNamespace(
            message=SimpleNamespace(content='{"result_summary":"ok"}'),
            finish_reason=self.owner.chat_finish_reason,
        )
        return SimpleNamespace(
            choices=[choice], usage=usage, id="chat_audit", _request_id="req_pplx",
            citations=["https://example.com/source"],
            search_results=[{"url":"https://example.com/source","title":"Source","snippet":"Evidence"}],
        )


class _FakeOpenAIClient:
    instances = []
    default_chat_finish_reason = "stop"
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.chat_finish_reason = type(self).default_chat_finish_reason
        self.last_responses_kwargs = None
        self.last_chat_kwargs = None
        self.responses = _FakeResponses(self)
        self.chat = SimpleNamespace(completions=_FakeChatCompletions(self))
        type(self).instances.append(self)


fake_openai = ModuleType("openai")
fake_openai.OpenAI = _FakeOpenAIClient
sys.modules["openai"] = fake_openai


class _FakeAnthropicMessages:
    def __init__(self, owner): self.owner = owner
    def create(self, **kwargs):
        self.owner.last_kwargs = kwargs
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text='{"result_summary":"ok"}')],
            usage=SimpleNamespace(input_tokens=100, output_tokens=40),
            stop_reason=self.owner.stop_reason,
            id="msg_audit", _request_id="req_anthropic",
        )


class _FakeAnthropicClient:
    instances = []
    default_stop_reason = "end_turn"
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.last_kwargs = None
        self.stop_reason = type(self).default_stop_reason
        self.messages = _FakeAnthropicMessages(self)
        type(self).instances.append(self)


fake_anthropic = ModuleType("anthropic")
fake_anthropic.Anthropic = _FakeAnthropicClient
fake_anthropic.transform_schema = lambda schema: schema
sys.modules["anthropic"] = fake_anthropic


def check_protocol_matrix():
    assert protocol.classify_http_failure(SimpleNamespace(status_code=400)) == ("PERMANENT", 400)
    assert protocol.classify_http_failure(SimpleNamespace(status_code=401)) == ("PERMANENT", 401)
    assert protocol.classify_http_failure(SimpleNamespace(status_code=422)) == ("PERMANENT", 422)
    assert protocol.classify_http_failure(SimpleNamespace(status_code=408)) == ("TRANSIENT", 408)
    assert protocol.classify_http_failure(SimpleNamespace(status_code=409)) == ("TRANSIENT", 409)
    assert protocol.classify_http_failure(SimpleNamespace(status_code=429)) == ("TRANSIENT", 429)
    assert protocol.classify_http_failure(SimpleNamespace(status_code=529)) == ("TRANSIENT", 529)
    assert protocol.classify_http_failure(RuntimeError("connection lost")) == (None, None)
    exc = SimpleNamespace(response=SimpleNamespace(headers={"retry-after": "7"}))
    assert protocol.retry_after_seconds(exc) == 7
    for value in ("length", "MAX_TOKENS", "max_output_tokens", "max-tokens-reached"):
        assert protocol.canonical_incomplete_reason(value) == "max_output_tokens", value


def check_openai_contract():
    os.environ["OPENAI_API_KEY"] = "audit"
    os.environ["EASON_ONE_WEB_SEARCH_COUNTRY"] = "TW"
    os.environ["EASON_ONE_WEB_SEARCH_TIMEZONE"] = "Asia/Taipei"
    providers.OpenAIProvider().complete(
        MODEL, "system", "user", "context", 1200,
        response_schema=SCHEMA, tool_mode="web_search",
    )
    client = _FakeOpenAIClient.instances[-1]
    assert client.kwargs.get("max_retries") == 0
    kwargs = client.last_responses_kwargs
    assert kwargs["tools"] == [{"type":"web_search","user_location":{"type":"approximate","country":"TW","timezone":"Asia/Taipei"}}]
    assert kwargs["max_tool_calls"] == 1 and kwargs["parallel_tool_calls"] is False
    fmt = kwargs["text"]["format"]
    assert fmt["type"] == "json_schema" and fmt["name"] == "task_execution" and fmt["strict"] is True


def check_anthropic_contract():
    os.environ["ANTHROPIC_API_KEY"] = "audit"
    providers.AnthropicProvider().complete(MODEL, "system", "user", "context", 1200, response_schema=SCHEMA)
    client = _FakeAnthropicClient.instances[-1]
    assert client.kwargs.get("max_retries") == 0
    assert client.last_kwargs["output_config"]["format"]["type"] == "json_schema"
    _FakeAnthropicClient.default_stop_reason = "max_tokens"
    result = providers.AnthropicProvider().complete(MODEL, "system", "user", "context", 1200, response_schema=SCHEMA)
    assert result.incomplete_reason == "max_output_tokens"
    _FakeAnthropicClient.default_stop_reason = "end_turn"


def check_perplexity_contract():
    os.environ["PERPLEXITY_API_KEY"] = "audit"
    result = providers.PerplexityProvider().complete(MODEL, "system", "user", "context", 2000, response_schema=SCHEMA)
    client = _FakeOpenAIClient.instances[-1]
    assert client.kwargs.get("base_url") == "https://api.perplexity.ai"
    assert client.kwargs.get("max_retries") == 0
    fmt = client.last_chat_kwargs["response_format"]
    # Current Sonar Chat Completions contract: json_schema + schema. Do not mix
    # in the Agent API's separate named-schema requirement.
    assert fmt == {"type":"json_schema","json_schema":{"schema":SCHEMA["schema"]}}
    assert result.sources and result.sources[0]["url"] == "https://example.com/source"
    _FakeOpenAIClient.default_chat_finish_reason = "length"
    truncated = providers.PerplexityProvider().complete(MODEL, "system", "user", "context", 2000, response_schema=SCHEMA)
    assert truncated.incomplete_reason == "max_output_tokens"
    _FakeOpenAIClient.default_chat_finish_reason = "stop"


class _FakeHTTPResponse:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode()
        self.headers = {"x-request-id": "req_gemini"}
    def read(self): return self.payload
    def __enter__(self): return self
    def __exit__(self, *args): return False


def check_gemini_contract():
    os.environ["GEMINI_API_KEY"] = "audit"
    captured = {}
    original = urllib.request.urlopen
    def ok(request, timeout=None):
        captured["body"] = json.loads(request.data.decode())
        return _FakeHTTPResponse({
            "responseId":"gem_audit",
            "candidates":[{"content":{"parts":[{"text": '{"result_summary":"ok"}'}]},"finishReason":"STOP"}],
            "usageMetadata":{"promptTokenCount":100,"candidatesTokenCount":40},
        })
    urllib.request.urlopen = ok
    try:
        providers.GeminiProvider().complete(MODEL, "system", "user", "context", 1200, response_schema=SCHEMA)
        generation = captured["body"]["generationConfig"]
        assert generation["responseMimeType"] == "application/json"
        assert generation["responseJsonSchema"] == SCHEMA["schema"]

        def reject(request, timeout=None):
            raise urllib.error.HTTPError(
                request.full_url, 429, "rate limited", {"Retry-After":"3"}, io.BytesIO(b'{"error":"rate limited"}')
            )
        urllib.request.urlopen = reject
        try:
            providers.GeminiProvider().complete(MODEL, "system", "user", "context", 1200, response_schema=SCHEMA)
            raise AssertionError("Gemini 429 did not raise")
        except protocol.ProviderHTTPError as exc:
            assert exc.status_code == 429
            assert protocol.classify_http_failure(exc) == ("TRANSIENT", 429)
            assert protocol.retry_after_seconds(exc) == 3
    finally:
        urllib.request.urlopen = original


def check_runtime_source_contracts():
    execution = (PKG / "services" / "execution.py").read_text(encoding="utf-8")
    work = (PKG / "services" / "work_execution.py").read_text(encoding="utf-8")
    task = (PKG / "services" / "task_execution.py").read_text(encoding="utf-8")
    kernel = (PKG / "services" / "company_kernel.py").read_text(encoding="utf-8")
    workforce = (PKG / "services" / "workforce.py").read_text(encoding="utf-8")
    assert 'PROVIDER_TRANSIENT_REJECTED' in execution
    assert 'external.mark_rejected(effect,exc)' in execution
    assert 'status="RELEASED"' in execution
    assert '"PROVIDER_TRANSIENT_REJECTED"' in work
    assert 'retry_after_seconds' in work
    assert 'run.failure_reason not in {"OUTPUT_TRUNCATED", "PROVIDER_TRANSIENT_REJECTED"}' in work
    assert '2400 if live_research else 1600' in task
    assert 'Provider rejected the Project outcome-review request' in kernel
    assert 'Provider rejected the continuation-planning request' in kernel
    assert 'retry_after_seconds' in kernel
    assert 'getattr(last, "failure_reason", None) == "PROVIDER_REQUEST_REJECTED"' in workforce
    assert 'retry_after_seconds' in workforce


if __name__ == "__main__":
    check_protocol_matrix()
    check_openai_contract()
    check_anthropic_contract()
    check_perplexity_contract()
    check_gemini_contract()
    check_runtime_source_contracts()
    print("PROVIDER_CONTRACT_AUDIT_PASS")
    print(" - OpenAI Responses structured output + TW hosted Web Search")
    print(" - Anthropic output_config JSON Schema + canonical max-token stop")
    print(" - Perplexity Sonar Chat Completions json_schema + observed source lineage")
    print(" - Gemini structured JSON + status-preserving HTTP errors")
    print(" - HTTP permanent/transient/ambiguous matrix + Retry-After")
    print(" - transient/truncation retry stays on the same provider before fallback")
