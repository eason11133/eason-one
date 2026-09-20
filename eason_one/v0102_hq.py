"""V0.10.3 Headquarters visual shell and compatibility bridge.

The existing Headquarters template may be a Jinja fragment without its own
<head> or <body>.  Therefore this module does not edit that template.  It
injects the V0.10 assets into the *rendered* Headquarters HTML response, after
the application's normal base template has produced the final document.

Non-trivial Founder requests reuse Eason One's authoritative
services.ceo.founder_request() path so the Provider Run, token usage,
cost, latency, failures, and governed output remain persisted by the existing
runtime rather than by a second ad-hoc provider stack.
"""
from __future__ import annotations

import importlib
import json
import re
from typing import Any, Callable

from flask import Blueprint, current_app, jsonify, request, url_for

bp = Blueprint("hq_v010", __name__)

CSS_MARKER = "headquarters-v0103.css"
JS_MARKER = "headquarters-v0103.js"
HQ_MARKERS = ("ceo briefing", "talk to your ceo")


def _import_attr(module_name: str, attr_name: str) -> Any:
    module = importlib.import_module(module_name)
    return getattr(module, attr_name)


def _resolve_employee_model() -> Any:
    candidates = (
        ("eason_one.models", "Employee"),
        ("eason_one.models.employee", "Employee"),
    )
    last_error: Exception | None = None
    for module_name, attr_name in candidates:
        try:
            return _import_attr(module_name, attr_name)
        except (ImportError, AttributeError) as exc:
            last_error = exc
    raise RuntimeError("Employee model could not be resolved") from last_error


def _resolve_founder_request() -> Callable[..., Any]:
    candidates = (
        ("eason_one.services.ceo", "founder_request"),
    )
    last_error: Exception | None = None
    for module_name, attr_name in candidates:
        try:
            fn = _import_attr(module_name, attr_name)
            if callable(fn):
                return fn
        except (ImportError, AttributeError) as exc:
            last_error = exc
    raise RuntimeError("founder_request() could not be resolved") from last_error


def _first_attr(obj: Any, names: tuple[str, ...]) -> Any:
    if obj is None:
        return None
    for name in names:
        try:
            value = getattr(obj, name)
        except (AttributeError, TypeError):
            continue
        if value is not None and value != "":
            return value
    return None


def _json_like(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text or text[:1] not in ("{", "["):
        return value
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return value


def _extract_executive_response(run: Any, created: Any) -> str:
    candidate = _first_attr(
        run,
        (
            "executive_response",
            "output_text",
            "response_text",
            "raw_output",
            "output",
            "content",
            "result",
            "response_json",
            "parsed_output",
            "output_json",
            "provider_response",
            "response_body",
            "completion",
        ),
    )
    if candidate is None:
        candidate = _first_attr(
            created,
            (
                "executive_response",
                "summary",
                "description",
                "objective",
                "title",
                "name",
            ),
        )

    candidate = _json_like(candidate)
    if isinstance(candidate, dict):
        for key in (
            "executive_response",
            "response",
            "message",
            "summary",
            "plan",
            "content",
        ):
            value = candidate.get(key)
            if value:
                if isinstance(value, str):
                    return value.strip()
                return json.dumps(value, ensure_ascii=False, indent=2)
        return json.dumps(candidate, ensure_ascii=False, indent=2)
    if isinstance(candidate, list):
        return json.dumps(candidate, ensure_ascii=False, indent=2)
    if candidate is not None:
        text = str(candidate).strip()
        if text:
            return text

    status = str(_first_attr(run, ("status",)) or "UNKNOWN").upper()
    return f"CEO Run finished with status {status}. Open Runs for the complete persisted output."


def _serialize_entity(entity: Any) -> dict[str, Any] | None:
    if entity is None:
        return None
    payload: dict[str, Any] = {}
    for source, target in (
        ("id", "id"),
        ("slug", "slug"),
        ("title", "title"),
        ("name", "name"),
        ("status", "status"),
        ("objective", "objective"),
    ):
        value = _first_attr(entity, (source,))
        if value is not None:
            payload[target] = value
    return payload or None


def _insert_before_closing_tag(html: str, tag: str, payload: str) -> tuple[str, bool]:
    match = re.search(rf"</{re.escape(tag)}\s*>", html, flags=re.IGNORECASE)
    if not match:
        return html, False
    return html[: match.start()] + payload + html[match.start() :], True


@bp.after_app_request
def inject_v010_headquarters_assets(response):
    """Inject assets into final HQ HTML; never mutate the Jinja fragment itself."""
    try:
        if response.status_code != 200 or response.direct_passthrough:
            return response
        if response.mimetype != "text/html":
            return response

        html = response.get_data(as_text=True)
        lowered = html.lower()
        # Scope the transformation to the CEO Headquarters page.  This avoids
        # touching Mission Room, People, Meetings, or unrelated HTML routes.
        if not all(marker in lowered for marker in HQ_MARKERS):
            return response

        css_url = url_for("static", filename=CSS_MARKER)
        js_url = url_for("static", filename=JS_MARKER)
        changed = False

        if CSS_MARKER not in html:
            css_tag = f'  <link rel="stylesheet" href="{css_url}?v=0.10.3">\n'
            html, inserted = _insert_before_closing_tag(html, "head", css_tag)
            if not inserted:
                # A rendered fragment is still legal input for the browser. A
                # stylesheet link at the beginning is more robust than failing
                # installation because the source template has no </head>.
                html = css_tag + html
            changed = True

        if JS_MARKER not in html:
            js_tag = f'  <script src="{js_url}?v=0.10.3"></script>\n'
            html, inserted = _insert_before_closing_tag(html, "body", js_tag)
            if not inserted:
                html = html + "\n" + js_tag
            changed = True

        if changed:
            response.set_data(html)
        return response
    except Exception:
        # Asset decoration must never turn a working Headquarters response into
        # a 500.  The install-time render verification catches real failures.
        current_app.logger.exception("V0.10.3 Headquarters asset injection failed")
        return response


@bp.post("/headquarters/v0102/ceo")
def ceo_direct_line_v010():
    payload = request.get_json(silent=True) if request.is_json else None
    payload = payload or request.form or {}
    prompt = str(
        payload.get("prompt")
        or payload.get("message")
        or payload.get("request")
        or payload.get("founder_request")
        or ""
    ).strip()

    if not prompt:
        return jsonify({"ok": False, "failure_code": "EMPTY_FOUNDER_REQUEST"}), 400
    if len(prompt) > 12000:
        return jsonify({"ok": False, "failure_code": "FOUNDER_REQUEST_TOO_LARGE"}), 413

    try:
        Employee = _resolve_employee_model()
        founder_request = _resolve_founder_request()
        ceo = Employee.query.filter_by(slug="ceo").first()
        if ceo is None:
            return jsonify({"ok": False, "failure_code": "CEO_NOT_FOUND"}), 404

        # Authoritative existing runtime: this call creates and persists the Run.
        result = founder_request(ceo, prompt)
        if isinstance(result, tuple):
            run = result[0] if len(result) > 0 else None
            created = result[1] if len(result) > 1 else None
        else:
            run, created = result, None

        if run is None:
            raise RuntimeError("founder_request() returned no Run")

        response_text = _extract_executive_response(run, created)
        run_status = str(_first_attr(run, ("status",)) or "UNKNOWN").upper()
        failure_reason = _first_attr(
            run,
            ("failure_reason", "failure_code", "error_code", "error"),
        )

        body = {
            "ok": run_status not in {"FAILED", "ERROR"},
            "executive_response": response_text,
            "run": {
                "id": _first_attr(run, ("id", "run_id")),
                "status": run_status,
                "provider": _first_attr(run, ("provider", "provider_name")),
                "model": _first_attr(run, ("model", "model_name", "model_id")),
                "input_tokens": _first_attr(run, ("input_tokens", "prompt_tokens")),
                "output_tokens": _first_attr(run, ("output_tokens", "completion_tokens")),
                "latency_ms": _first_attr(run, ("latency_ms", "duration_ms")),
                "cost": _first_attr(run, ("actual_cost", "estimated_cost", "cost")),
                "failure_reason": failure_reason,
            },
            "created": _serialize_entity(created),
        }
        status_code = 200 if body["ok"] else 422
        return jsonify(body), status_code
    except Exception as exc:  # Runtime boundary: report, do not mutate authority here.
        current_app.logger.exception("V0.10.3 compatibility CEO Direct Line failed")
        return (
            jsonify(
                {
                    "ok": False,
                    "failure_code": "CEO_RUNTIME_FAILURE",
                    "error": str(exc),
                }
            ),
            500,
        )
