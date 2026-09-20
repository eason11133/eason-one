"""Provider-protocol normalization shared by all Eason One model adapters.

This module intentionally contains no Flask/SQLAlchemy imports.  Provider SDKs
have different exception and stop-reason shapes; Company Runtime should see one
small deterministic contract instead of re-learning each SDK at every callsite.
"""
from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from types import SimpleNamespace

PERMANENT_HTTP_REJECTIONS = frozenset({400, 401, 403, 404, 422})
TRANSIENT_HTTP_REJECTIONS = frozenset({408, 409, 429})

_TRUNCATION_REASONS = frozenset({
    "length",
    "max_tokens",
    "max_token",
    "max_output_tokens",
    "max_tokens_reached",
    "max_output_tokens_reached",
    "model_context_window_exceeded",
})


class ProviderHTTPError(RuntimeError):
    """HTTP provider failure that preserves status/headers for durable policy."""

    def __init__(self, message: str, *, status_code: int, headers=None, request_id=None):
        super().__init__(message)
        self.status_code = int(status_code)
        self.request_id = request_id
        self.response = SimpleNamespace(
            status_code=self.status_code,
            headers=dict(headers or {}),
        )


def canonical_incomplete_reason(reason):
    """Normalize provider-specific output-limit stop reasons.

    Raw stop_reason remains persisted separately on AgentRun; this canonical
    value exists only so bounded recovery can react consistently.
    """
    if reason is None:
        return None
    raw = str(reason).strip()
    normalized = raw.lower().replace("-", "_").replace(" ", "_")
    if normalized in _TRUNCATION_REASONS:
        return "max_output_tokens"
    return raw


def http_status(exc):
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    try:
        return int(status)
    except (TypeError, ValueError):
        return None


def classify_http_failure(exc):
    """Return (classification, status) for a provider HTTP response.

    PERMANENT means the provider definitively rejected a request that requires a
    request/configuration repair before retry. TRANSIENT means the provider
    returned a retryable HTTP response and no model completion was accepted.
    None means there is no trustworthy HTTP response (for example a connection
    drop/timeout), so post-dispatch effect truth remains ambiguous.
    """
    status = http_status(exc)
    if status is None:
        return None, None
    if status in PERMANENT_HTTP_REJECTIONS:
        return "PERMANENT", status
    if status in TRANSIENT_HTTP_REJECTIONS or status >= 500:
        return "TRANSIENT", status
    return None, status


def retry_after_seconds(exc, *, cap_seconds: int = 60):
    """Read Retry-After without trusting unbounded provider delays."""
    headers = getattr(getattr(exc, "response", None), "headers", None) or {}
    value = None
    try:
        value = headers.get("retry-after") or headers.get("Retry-After")
    except AttributeError:
        return None
    if value is None:
        return None
    text = str(value).strip()
    try:
        seconds = float(text)
    except ValueError:
        try:
            when = parsedate_to_datetime(text)
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            seconds = (when - datetime.now(timezone.utc)).total_seconds()
        except Exception:
            return None
    return max(0, min(int(cap_seconds), int(round(seconds))))
