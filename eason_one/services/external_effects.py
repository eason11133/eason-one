"""External provider/tool effect ledger for replay authority."""
from __future__ import annotations

import hashlib

from ..extensions import db
from ..models import ExternalEffectAttempt, now
from .company_events import correlation_for_work, emit

EFFECT_KINDS = {"MODEL_INFERENCE", "LOCAL_REVERSIBLE"}


def _fingerprint(provider: str, system_prompt: str, user_request: str, context: str) -> str:
    payload = "\n".join([provider, system_prompt, user_request, context])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _effect_kind(effect: ExternalEffectAttempt) -> str | None:
    kind = str(getattr(effect, "effect_kind", "") or "").strip().upper()
    if kind in EFFECT_KINDS:
        return kind
    # Legacy prefixed keys from an earlier staged build remain readable, but
    # untyped historical effects are conservative and never guessed replayable.
    key = str(getattr(effect, "idempotency_key", "") or "")
    prefix = key.split(":", 1)[0]
    return prefix if prefix in EFFECT_KINDS else None


def prepare(run, *, provider: str, system_prompt: str, user_request: str, context: str,
            estimated_cost_twd, effect_kind: str, reservation=None) -> ExternalEffectAttempt:
    effect_kind = str(effect_kind or "").strip().upper()
    if effect_kind not in EFFECT_KINDS:
        raise ValueError(f"External effect kind {effect_kind!r} has no governed replay policy")
    logical = f"work:{run.work_id}:purpose:{run.purpose}" if run.work_id else f"execution:{run.id}"
    effect = ExternalEffectAttempt(
        execution_id=run.id,
        work_id=run.work_id,
        operation_id=run.operation_id,
        provider=provider,
        effect_kind=effect_kind,
        request_fingerprint=_fingerprint(provider, system_prompt, user_request, context),
        idempotency_key=f"{effect_kind}:{logical}",
        state="RESERVED" if reservation is not None else "PREPARED",
        estimated_cost_twd=estimated_cost_twd or 0,
        cost_reservation_id=getattr(reservation, "id", None),
    )
    db.session.add(effect)
    db.session.flush()
    return effect


def retry_authorized(run) -> tuple[bool, str]:
    effect = (ExternalEffectAttempt.query.filter_by(execution_id=run.id)
              .order_by(ExternalEffectAttempt.id.desc()).first())
    if effect is None:
        return True, "NO_EXTERNAL_EFFECT_PREPARED"
    kind = _effect_kind(effect)
    if kind not in EFFECT_KINDS:
        return False, "UNKNOWN_EFFECT_KIND"
    if effect.state == "AMBIGUOUS_POST_DISPATCH":
        return False, "AMBIGUOUS_POST_DISPATCH"
    if effect.state in {"DISPATCHING", "RESPONSE_RECEIVED"}:
        return False, f"NONTERMINAL_EXTERNAL_EFFECT:{effect.state}"
    if effect.state == "FAILED_PRE_DISPATCH":
        return True, "FAILED_PRE_DISPATCH"
    if effect.state == "REJECTED_POST_DISPATCH":
        return True, "REJECTED_POST_DISPATCH"
    if effect.state in {"PREPARED", "RESERVED"}:
        return True, "NOT_DISPATCHED"
    if effect.state in {"PERSISTED", "SETTLED"}:
        if kind == "MODEL_INFERENCE":
            if str(getattr(run, "status", "") or "").upper() == "SUCCEEDED":
                return False, "MODEL_INFERENCE_SUCCESS_ALREADY_PAID"
            return True, "MODEL_INFERENCE_SETTLED"
        composition = dict(getattr(run, "context_composition_json", None) or {})
        repository_delta = list(composition.get("repository_delta") or [])
        # A successful local write is precisely the case where replay must NOT
        # be inferred from status=SUCCEEDED. A fresh attempt is authorized only
        # after that exact delta is proven restored (for example after semantic
        # rejection or a newly discovered Founder authority boundary).
        if run.status == "SUCCEEDED" and not repository_delta:
            return True, "LOCAL_REVERSIBLE_READ_ONLY_SUCCEEDED"
        if composition.get("completed_write_restore_verified") is True:
            return True, "LOCAL_REVERSIBLE_COMPLETED_WRITE_RESTORE_VERIFIED"
        if composition.get("failed_write_restore_verified") is True:
            return True, "LOCAL_REVERSIBLE_RESTORE_VERIFIED"
        if composition.get("read_only_restore_verified") is True:
            return True, "LOCAL_REVERSIBLE_READ_ONLY_RESTORE_VERIFIED"
        if (composition.get("restart_recovery") or {}).get("restore_verified") is True:
            return True, "LOCAL_REVERSIBLE_RESTART_RESTORE_VERIFIED"
        return False, "LOCAL_REVERSIBLE_NOT_PROVEN_RESTORED"
    return False, f"UNPROVEN_REPLAY_STATE:{effect.state}"


def mark_pre_dispatch_failure(effect, error):
    effect.state = "FAILED_PRE_DISPATCH"; effect.error_text = str(error)

def mark_dispatching(effect):
    if effect.state not in {"PREPARED", "RESERVED"}: raise ValueError(f"Cannot dispatch ExternalEffect from {effect.state}")
    effect.state = "DISPATCHING"; effect.dispatched_at = now()

def mark_response(effect, result):
    effect.state = "RESPONSE_RECEIVED"; effect.response_received_at = now()
    effect.provider_request_id = getattr(result, "request_id", None); effect.provider_response_id = getattr(result, "response_id", None)

def mark_persisted(effect):
    effect.state = "PERSISTED"; effect.persisted_at = now()

def mark_settled(effect, *, actual_cost_twd=None):
    effect.state = "SETTLED"; effect.actual_cost_twd = actual_cost_twd; effect.settled_at = now()

def mark_rejected(effect, error):
    effect.state = "REJECTED_POST_DISPATCH"
    effect.response_received_at = now()
    effect.error_text = str(error)

def mark_ambiguous(effect, error):
    effect.state = "AMBIGUOUS_POST_DISPATCH"; effect.error_text = str(error)

def emit_execution_event(run, event_type: str, *, payload=None):
    emit(event_type, actor_type="EMPLOYEE", actor_id=run.employee_id, project_id=run.project_id,
         work_id=run.work_id, execution_id=run.id,
         correlation_id=correlation_for_work(run.work_id) if run.work_id else f"execution:{run.id}", payload=payload)
