"""Deterministic execution-policy enforcement for approved Missions.

The CEO's natural-language authority contract is not advisory.  Runtime model
selection must obey explicit low-cost / no-premium constraints without mutating
an Employee's persistent ModelConfig assignment.
"""
from __future__ import annotations

from decimal import Decimal
import json
import os

from flask import current_app

from ..extensions import db
from ..models import ModelConfig

_CREDENTIALS = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "perplexity": "PERPLEXITY_API_KEY",
}
_PREMIUM_MARKERS = ("sonnet", "opus", "premium", "ultra", "max")

# Founder cost policy: Anthropic/Claude is reserved for the independent Critic
# and the persistent Claude Researcher in formal Company runtime. Other generic
# Employees keep their identity/model preference, but paid execution is routed
# to another configured real provider. A dedicated Researcher is handled by the
# provider-family hard boundary below and is never cross-routed.
_CLAUDE_RESERVED_EMPLOYEE_SLUGS = {"critic", "claude-researcher"}


def _claude_allowed_for_employee(employee) -> bool:
    return str(getattr(employee, "slug", "") or "").strip().casefold() in _CLAUDE_RESERVED_EMPLOYEE_SLUGS


def _dedicated_provider(employee) -> str | None:
    return __import__(
        "eason_one.services.research_department", fromlist=["provider_family"]
    ).provider_family(employee)


def _research_provider_scope(employee, constraints: dict) -> set[str]:
    """Provider allowlist that applies only to Research Department execution.

    Founder phrases such as ``只用 Claude 做研究`` should constrain the
    specialist/Director/retry path without accidentally forbidding unrelated
    downstream Engineering or Critic work in the same Project.
    """
    slug = str(getattr(employee, "slug", "") or "").strip().casefold()
    if slug not in {
        "researcher", "research-director", "openai-researcher",
        "claude-researcher", "gemini-researcher", "perplexity-researcher",
    }:
        return set()
    return {
        str(value or "").strip().casefold()
        for value in (constraints.get("research_allowed_providers") or [])
        if str(value or "").strip()
    }


def _same_provider_candidates(provider: str, constraints: dict, *, web_search: bool = False) -> list[ModelConfig]:
    return [
        model for model in ModelConfig.query.filter_by(active=True, archived=False).all()
        if model.provider_key == provider
        and _runtime_eligible(model, web_search=web_search)
        and not (constraints.get("no_premium") and _is_premium(model))
        and not (constraints.get("low_cost_only") and _is_premium(model))
    ]


def _critical_review_staffing_request(request) -> bool:
    if request is None:
        return False
    text = " ".join([
        str(getattr(request, "role_needed", "") or ""),
        json.dumps(getattr(request, "capabilities_json", None) or [], ensure_ascii=False),
        json.dumps(getattr(request, "responsibilities_json", None) or [], ensure_ascii=False),
    ]).casefold()
    return "critical_review" in text or "critical review" in text or "critic" in text


def compile_text_constraints(text) -> dict:
    """Compile execution restrictions from one authoritative text source.

    This helper is intentionally reusable by Operation creation so Founder and
    frozen Project constraints become durable runtime policy instead of being
    rediscovered from whatever wording the CEO happens to put in a later plan.
    Boolean restrictions only narrow execution. Provider rules are compiled by
    the Research Department parser, which preserves negation/only semantics.
    """
    value = str(text or "").casefold()
    result = {
        "low_cost_only": any(phrase in value for phrase in (
            "low-cost", "low cost", "lowest-cost", "economy model",
            "低成本", "最低成本", "便宜模型", "低價模型", "低价模型",
        )),
        "no_premium": any(phrase in value for phrase in (
            "no premium", "without premium", "禁止高價", "不用高價",
            "禁止高价", "不用高价", "不要高價", "不要高价",
        )),
        "existing_evidence_only": any(phrase in value for phrase in (
            "existing evidence only", "only existing", "use existing eason one evidence",
            "using only eason one evidence", "no extensive new research",
            "只用現有證據", "只用现有证据", "只用既有證據", "只用既有证据",
            "不要新增研究", "不做新研究",
        )),
    }
    provider = __import__(
        "eason_one.services.research_department", fromlist=["execution_provider_constraints"]
    ).execution_provider_constraints(value)
    result.update({key: list(rows) for key, rows in provider.items() if rows})
    return result


def constraint_rows_from_execution_constraints(constraints: dict) -> list[str]:
    """Human-readable Project Contract rows for durable execution authority."""
    constraints = dict(constraints or {})
    department = __import__(
        "eason_one.services.research_department", fromlist=["LABEL_BY_PROVIDER"]
    )
    labels = dict(getattr(department, "LABEL_BY_PROVIDER", {}) or {})
    rows: list[str] = []
    for provider in constraints.get("excluded_providers") or []:
        provider = str(provider or "").strip().casefold()
        if provider:
            rows.append(f"Do not use provider: {labels.get(provider, provider)}.")
    allowed = [
        str(provider or "").strip().casefold()
        for provider in (constraints.get("research_allowed_providers") or [])
        if str(provider or "").strip()
    ]
    if allowed:
        rows.append(
            "Research providers only: "
            + ", ".join(labels.get(provider, provider) for provider in allowed)
            + "."
        )
    if constraints.get("no_premium"):
        rows.append("No premium models.")
    if constraints.get("low_cost_only"):
        rows.append("Low-cost execution only.")
    if constraints.get("existing_evidence_only"):
        rows.append("Use existing evidence only; do not perform new external research.")
    return rows


def _merge_constraint_maps(*maps: dict) -> dict:
    """Intersect/union narrowing policy from Mission + current Project authority."""
    excluded: set[str] = set()
    allowlists: list[set[str]] = []
    result = {}
    conflict = False
    for mapping in maps:
        mapping = dict(mapping or {})
        conflict = bool(conflict or mapping.get("constraint_conflict"))
        excluded.update(
            str(value or "").strip().casefold()
            for value in (mapping.get("excluded_providers") or [])
            if str(value or "").strip()
        )
        allowed = {
            str(value or "").strip().casefold()
            for value in (mapping.get("research_allowed_providers") or [])
            if str(value or "").strip()
        }
        if allowed:
            allowlists.append(allowed)
        for key in ("low_cost_only", "no_premium", "existing_evidence_only"):
            if mapping.get(key):
                result[key] = True
    if excluded:
        result["excluded_providers"] = sorted(excluded)
    if allowlists:
        allowed = set.intersection(*allowlists) - excluded
        if allowed:
            result["research_allowed_providers"] = sorted(allowed)
        else:
            conflict = True
            result["research_allowed_providers"] = []
    if conflict:
        result["constraint_conflict"] = True
    return result


def execution_constraints(operation) -> dict:
    if operation is None:
        return {}
    memory = dict(operation.memory_json or {})
    existing = dict(memory.get("execution_constraints") or {})
    plan = operation.plan_json or {}
    derived = compile_text_constraints(json.dumps(plan, ensure_ascii=False))

    # New Operations persist the authority envelope in memory. For historical
    # Operations, recover provider hints from the plan only when no durable
    # provider authority was stored. Boolean restrictions always narrow.
    mission = dict(existing)
    for key in ("low_cost_only", "no_premium", "existing_evidence_only"):
        if derived.get(key):
            mission[key] = True
    if "excluded_providers" not in existing and derived.get("excluded_providers"):
        mission["excluded_providers"] = list(derived["excluded_providers"] or [])
    if "research_allowed_providers" not in existing and derived.get("research_allowed_providers"):
        mission["research_allowed_providers"] = list(derived["research_allowed_providers"] or [])

    # Project Contract amendments are live Founder authority. An already-open
    # Mission may keep a narrower restriction, but it may never continue under
    # authority that the current Project Contract has since revoked.
    project = getattr(operation, "project", None)
    if project is not None:
        contract = __import__(
            "eason_one.services.project_contract", fromlist=["is_vnext_governed", "governing_terms"]
        )
        if contract.is_vnext_governed(project):
            terms = contract.governing_terms(project)
            project_policy = compile_text_constraints(" ; ".join(terms.get("constraints") or []))
            return _merge_constraint_maps(project_policy, mission)
    return _merge_constraint_maps(mission)


def _is_premium(model: ModelConfig) -> bool:
    text = f"{model.label} {model.model_name}".casefold()
    return any(marker in text for marker in _PREMIUM_MARKERS)


def _configured(model: ModelConfig) -> bool:
    credential = _CREDENTIALS.get(model.provider_key)
    return not credential or bool(os.getenv(credential))


def _runtime_eligible(model: ModelConfig, *, web_search: bool = False) -> bool:
    """Return whether a ModelConfig is safe to dispatch in formal Company runtime.

    Persistent Employee bindings may point at mock, Codex, or a provider that is
    not configured on this host.  Those bindings remain identity/preferences,
    not execution authority.  Formal runtime must select a configured paid model
    with explicit pricing; live research additionally requires a priced hosted
    search request.
    """
    if model is None or not model.active or model.archived:
        return False
    if model.provider_key not in _CREDENTIALS or not _configured(model):
        return False
    if Decimal(getattr(model, "input_price_per_million", 0) or 0) <= 0:
        return False
    if Decimal(getattr(model, "output_price_per_million", 0) or 0) <= 0:
        return False
    if int(getattr(model, "max_output_tokens", 0) or 0) <= 0:
        return False
    if web_search:
        if model.provider_key not in {"openai", "perplexity"}:
            return False
        if Decimal(getattr(model, "request_price_per_call", 0) or 0) <= 0:
            return False
    return True


def _score(model: ModelConfig) -> Decimal:
    # Output is usually the expensive side; weight it conservatively.
    return Decimal(model.input_price_per_million or 0) + (
        Decimal(model.output_price_per_million or 0) * Decimal("2")
    )


def select_execution_model(employee, operation=None, purpose=None, *, require_real=False):
    # current_model_config_id is the durable binding truth.  SQLAlchemy may keep
    # a previously-loaded relationship object cached after the FK is reassigned
    # in the same session, so resolve by FK before enforcing provider-family
    # boundaries.  Otherwise a Claude Researcher could momentarily look like it
    # still owns the old Anthropic model after being (incorrectly) rebound.
    model_id = getattr(employee, "current_model_config_id", None)
    current = db.session.get(ModelConfig, model_id) if model_id is not None else getattr(employee, "current_model", None)
    if current is None:
        raise ValueError("Employee has no execution model configured")
    if getattr(employee, "slug", None) == "engineer" or current.provider_key == "codex" and getattr(employee, "slug", None) == "engineer":
        return current

    testing = bool(current_app.config.get("TESTING"))
    constraints = execution_constraints(operation)
    if constraints.get("constraint_conflict"):
        raise ValueError("FOUNDER_PROVIDER_CONSTRAINT_CONFLICT: current Project and Mission provider authority have no permitted intersection.")
    excluded_providers = {str(value or "").strip().casefold() for value in (constraints.get("excluded_providers") or []) if str(value or "").strip()}
    research_allowed = _research_provider_scope(employee, constraints)
    formal = bool((operation is not None or require_real) and not testing)
    claude_allowed = _claude_allowed_for_employee(employee)
    dedicated_provider = _dedicated_provider(employee)

    # Provider-specialized Research Employees are persistent identities, not a
    # decorative label over a generic routing pool. Formal execution may upgrade
    # the exact model version inside the same provider family, but it may never
    # silently turn the Claude Researcher into GPT (or vice versa).
    if dedicated_provider:
        if dedicated_provider in excluded_providers:
            raise ValueError(
                f"FOUNDER_PROVIDER_EXCLUDED: provider {dedicated_provider} is forbidden by the approved Founder constraint."
            )
        if research_allowed and dedicated_provider not in research_allowed:
            raise ValueError(
                f"FOUNDER_RESEARCH_PROVIDER_NOT_ALLOWED: provider {dedicated_provider} is outside the approved Research provider allowlist."
            )
        if not formal:
            if current.provider_key != dedicated_provider:
                raise ValueError(
                    f"RESEARCH_SPECIALIZATION_MISMATCH: {employee.slug} requires provider {dedicated_provider}."
                )
            return current
        candidates = _same_provider_candidates(dedicated_provider, constraints)
        if not candidates:
            raise ValueError(
                f"DEDICATED_PROVIDER_UNAVAILABLE: {employee.name} requires a configured {dedicated_provider} ModelConfig."
            )
        if current in candidates:
            return current
        return sorted(candidates, key=lambda model: (_score(model), model.id))[0]

    # Reserve Claude for the independent Critic in formal runtime. If Claude is
    # configured, Critic gets first claim on it; everyone else is routed away
    # from Anthropic even when their persistent binding still points there.
    if formal and claude_allowed:
        claude_candidates = [
            model for model in ModelConfig.query.filter_by(active=True, archived=False).all()
            if model.provider_key == "anthropic"
            and model.provider_key not in excluded_providers
            and (not research_allowed or model.provider_key in research_allowed)
            and _runtime_eligible(model)
            and not (constraints.get("no_premium") and _is_premium(model))
            and not (constraints.get("low_cost_only") and _is_premium(model))
        ]
        if claude_candidates:
            if current in claude_candidates:
                return current
            return sorted(claude_candidates, key=lambda model: (_score(model), model.id))[0]

    current_forbidden = bool(
        current.provider_key in excluded_providers
        or (research_allowed and current.provider_key not in research_allowed)
        or (formal and not _runtime_eligible(current))
        or (formal and current.provider_key == "codex")
        or (formal and not claude_allowed and current.provider_key == "anthropic")
        or (constraints.get("no_premium") and _is_premium(current))
        or (constraints.get("low_cost_only") and _is_premium(current))
    )
    if not current_forbidden:
        return current

    candidates = [
        model for model in ModelConfig.query.filter_by(active=True, archived=False).all()
        if _runtime_eligible(model)
        and model.provider_key not in excluded_providers
        and (not research_allowed or model.provider_key in research_allowed)
        and not (formal and not claude_allowed and model.provider_key == "anthropic")
        and not (constraints.get("no_premium") and _is_premium(model))
        and not (constraints.get("low_cost_only") and _is_premium(model))
    ]
    if not candidates:
        rule = "low-cost/no-premium" if constraints else "formal Company runtime"
        raise ValueError(
            f"MODEL_POLICY_BLOCKED: no configured real ModelConfig satisfies the {rule} constraint."
        )
    return sorted(candidates, key=lambda model: (_score(model), model.id))[0]


def select_staffing_model(request, proposed_model=None):
    """Apply the same Founder cost policy to new Persistent Employee staffing.

    Claude may be assigned only to CRITICAL_REVIEW/Critic staffing. Other hires
    receive the cheapest configured non-Anthropic real provider, preventing HR
    from creating an expensive default binding that runtime would immediately
    override anyway.
    """
    allow_claude = _critical_review_staffing_request(request)
    operation = None
    if getattr(request, "operation_id", None):
        Operation = __import__("eason_one.models", fromlist=["Operation"]).Operation
        operation = db.session.get(Operation, request.operation_id)
    constraints = execution_constraints(operation)
    if constraints.get("constraint_conflict"):
        return None
    excluded_providers = {
        str(value or "").strip().casefold()
        for value in (constraints.get("excluded_providers") or [])
        if str(value or "").strip()
    }
    request_text = " ".join([
        str(getattr(request, "role_needed", "") or ""),
        " ".join(str(value or "") for value in (getattr(request, "capabilities_json", None) or [])),
    ]).casefold()
    research_allowed = {
        str(value or "").strip().casefold()
        for value in (constraints.get("research_allowed_providers") or [])
        if str(value or "").strip()
    } if "research" in request_text else set()

    def eligible(model):
        if not _runtime_eligible(model):
            return False
        provider = str(model.provider_key or "").casefold()
        if provider in excluded_providers:
            return False
        if research_allowed and provider not in research_allowed:
            return False
        if constraints.get("no_premium") and _is_premium(model):
            return False
        if constraints.get("low_cost_only") and _is_premium(model):
            return False
        if not allow_claude and provider == "anthropic":
            return False
        return True

    if (
        proposed_model is not None
        and eligible(proposed_model)
    ):
        return proposed_model
    candidates = [
        model for model in ModelConfig.query.filter_by(active=True, archived=False).all()
        if eligible(model)
    ]
    if not candidates:
        return None
    if allow_claude:
        claude = [model for model in candidates if model.provider_key == "anthropic"]
        if claude:
            return sorted(claude, key=lambda model: (_score(model), model.id))[0]
    return sorted(candidates, key=lambda model: (_score(model), model.id))[0]



def select_research_model(employee, operation=None, exclude_run=None, *, exclude_model_ids=None):
    """Select a bounded live-web research core without rebinding Employee identity.

    Commodity web-search execution may come from OpenAI hosted Web Search or
    Perplexity/Sonar. Eason One still owns Work authority, budget, source lineage,
    verification, and Employee identity around the provider call. Tests may use Mock.
    """
    current = getattr(employee, "current_model", None)
    if current is None:
        return None
    excluded_model_ids = {
        int(value) for value in (exclude_model_ids or []) if value is not None
    }
    if (
        current_app.config.get("TESTING")
        and current.provider_key == "mock"
        and int(current.id) not in excluded_model_ids
    ):
        return current
    constraints = execution_constraints(operation)
    if constraints.get("constraint_conflict"):
        return None
    excluded_providers = {str(value or "").strip().casefold() for value in (constraints.get("excluded_providers") or []) if str(value or "").strip()}
    research_allowed = _research_provider_scope(employee, constraints)
    dedicated_provider = _dedicated_provider(employee)
    if dedicated_provider in excluded_providers:
        return None
    if dedicated_provider and research_allowed and dedicated_provider not in research_allowed:
        return None
    if dedicated_provider and dedicated_provider not in {"openai", "perplexity"}:
        # Anthropic/Gemini specialists currently contribute an independent model
        # perspective. Their provider adapter does not claim live web-search
        # evidence, so Task Runtime must not silently route them through another
        # provider merely to obtain sources.
        return None

    # A Persistent Employee's configured research core is the first choice when
    # it is a healthy, priced live-web provider.  Cost-based selection is a
    # fallback, not authority to silently ignore the Founder/Employee binding.
    current_allowed = bool(
        _runtime_eligible(current, web_search=True)
        and current.provider_key not in excluded_providers
        and (not research_allowed or current.provider_key in research_allowed)
        and (not dedicated_provider or current.provider_key == dedicated_provider)
        and not (constraints.get("no_premium") and _is_premium(current))
        and not (constraints.get("low_cost_only") and _is_premium(current))
        and int(current.id) not in excluded_model_ids
        and not (
            exclude_run is not None
            and current.provider_key == getattr(exclude_run, "provider_key_snapshot", None)
            and current.model_name == getattr(exclude_run, "model_name_snapshot", None)
        )
    )
    if current_allowed:
        return current

    candidates=[]
    for model in ModelConfig.query.filter_by(active=True, archived=False).all():
        if int(model.id) in excluded_model_ids:
            continue
        if not _runtime_eligible(model, web_search=True):
            continue
        if model.provider_key in excluded_providers:
            continue
        if research_allowed and model.provider_key not in research_allowed:
            continue
        if dedicated_provider and model.provider_key != dedicated_provider:
            continue
        if constraints.get("no_premium") and _is_premium(model):
            continue
        if constraints.get("low_cost_only") and _is_premium(model):
            continue
        if (
            exclude_run is not None
            and model.provider_key == getattr(exclude_run,"provider_key_snapshot",None)
            and model.model_name == getattr(exclude_run,"model_name_snapshot",None)
        ):
            continue
        candidates.append(model)
    if not candidates:
        return None
    provider_family_fault = _provider_family_retry_fault(exclude_run)
    failed_provider = str(getattr(exclude_run, "provider_key_snapshot", "") or "")
    return sorted(
        candidates,
        key=lambda model: (
            # When the last attempt proved a provider-family boundary fault, a
            # generic Researcher should prefer another lawful live-search provider
            # before another model behind the same broken boundary. Dedicated
            # provider-family Researchers remain constrained above.
            1 if provider_family_fault and not dedicated_provider and model.provider_key == failed_provider else 0,
            Decimal(getattr(model, "request_price_per_call", 0) or 0),
            _score(model),
            model.id,
        ),
    )[0]

def model_override_authorized(employee, model, operation=None, *, web_search: bool = False) -> bool:
    """Defense-in-depth check for caller-supplied ModelConfig overrides.

    Callers may preselect a review/research/retry model, but no caller owns
    authority to bypass the Founder/Project provider envelope. Formal runtime
    additionally requires the same configured/priced execution safety as normal
    selection. Test/simulation mode keeps real-provider fixture setup lightweight
    while still enforcing all authority semantics.
    """
    if model is None or not getattr(model, "active", False) or getattr(model, "archived", False):
        return False
    testing = bool(current_app.config.get("TESTING"))
    if model.provider_key == "mock":
        return testing
    constraints = execution_constraints(operation)
    if constraints.get("constraint_conflict"):
        return False
    excluded = {
        str(value or "").strip().casefold()
        for value in (constraints.get("excluded_providers") or [])
        if str(value or "").strip()
    }
    research_allowed = _research_provider_scope(employee, constraints)
    provider = str(getattr(model, "provider_key", "") or "").strip().casefold()
    if provider in excluded:
        return False
    if research_allowed and provider not in research_allowed:
        return False
    dedicated = _dedicated_provider(employee)
    if dedicated and provider != dedicated:
        return False
    if constraints.get("no_premium") and _is_premium(model):
        return False
    if constraints.get("low_cost_only") and _is_premium(model):
        return False
    if web_search and provider not in {"openai", "perplexity"}:
        return False
    formal = bool(operation is not None and not testing)
    if formal and not _claude_allowed_for_employee(employee) and provider == "anthropic":
        return False
    if formal and not _runtime_eligible(model, web_search=web_search):
        return False
    return True


def retry_model_eligible(employee, model, operation=None, *, web_search: bool = False) -> bool:
    """Return whether a retry candidate is as dispatch-safe as a first attempt.

    Recovery is not a weaker authority path. A stale/malformed ModelConfig with
    missing prices, output limits or credentials may not become eligible merely
    because the Company is retrying after a failure.
    """
    if model is None:
        return False
    testing = bool(current_app.config.get("TESTING"))
    dedicated_provider = _dedicated_provider(employee)
    if model.provider_key == "mock":
        # Mock is useful for generic isolated test fixtures, but it is never a
        # provider-family substitute for a persistent dedicated Researcher.
        # Keeping this boundary in TESTING prevents the test harness from
        # masking the exact production invariant we are trying to prove.
        return bool(testing and not dedicated_provider)
    if not _runtime_eligible(model, web_search=web_search):
        return False
    constraints = execution_constraints(operation)
    if constraints.get("constraint_conflict"):
        return False
    excluded_providers = {str(value or "").strip().casefold() for value in (constraints.get("excluded_providers") or []) if str(value or "").strip()}
    research_allowed = _research_provider_scope(employee, constraints)
    if model.provider_key in excluded_providers:
        return False
    if research_allowed and model.provider_key not in research_allowed:
        return False
    if dedicated_provider and model.provider_key != dedicated_provider:
        return False
    if constraints.get("no_premium") and _is_premium(model):
        return False
    if constraints.get("low_cost_only") and _is_premium(model):
        return False
    formal = bool(operation is not None and not testing)
    if formal and not _claude_allowed_for_employee(employee) and model.provider_key == "anthropic":
        return False
    return True


def _provider_family_retry_fault(failed_run) -> bool:
    """Whether recovery should prefer a different provider family first.

    A definitive provider rejection already proves the request did not produce a
    usable completion.  Some PRE_DISPATCH failures are equally provider-family
    specific (for example a missing provider credential/configuration).  In
    both cases, retrying another model behind the same broken provider boundary
    is usually wasted work for a generic Employee.  Dedicated provider-family
    Employees remain constrained by ``retry_model_eligible``.
    """
    if failed_run is None:
        return False
    reason = str(getattr(failed_run, "failure_reason", "") or "")
    if reason == "PROVIDER_REQUEST_REJECTED":
        return True
    if reason != "PROVIDER_PREFLIGHT_FAILED":
        return False
    error = str(getattr(failed_run, "error_text", "") or "").casefold()
    provider_boundary_markers = (
        "provider is not configured",
        "set openai_api_key",
        "set anthropic_api_key",
        "set gemini_api_key",
        "set perplexity_api_key",
        "unsupported provider",
        "web research requires an explicit request_price_per_call",
    )
    return any(marker in error for marker in provider_boundary_markers)


def select_retry_model(employee, failed_run, operation=None, purpose=None, *, exclude_model_ids=None):
    """Choose a governed alternate intelligence after a safe pre-dispatch failure.

    The retry path uses the same runtime-eligibility and Founder constraints as
    first-attempt execution; it never turns recovery into a back door for an
    unpriced, unconfigured, wrong-provider or zero-output ModelConfig.
    """
    excluded_model_ids = {
        int(value) for value in (exclude_model_ids or []) if value is not None
    }
    candidates = []
    for model in ModelConfig.query.filter_by(active=True, archived=False).all():
        if int(model.id) in excluded_model_ids:
            continue
        if not retry_model_eligible(employee, model, operation):
            continue
        if (
            failed_run
            and model.provider_key == failed_run.provider_key_snapshot
            and model.model_name == failed_run.model_name_snapshot
        ):
            continue
        candidates.append(model)
    if not candidates:
        return None
    failed_was_real = bool(failed_run and failed_run.provider_key_snapshot != "mock")
    provider_family_fault = _provider_family_retry_fault(failed_run)
    failed_provider = str(getattr(failed_run, "provider_key_snapshot", "") or "")
    return sorted(
        candidates,
        key=lambda model: (
            # A definitive provider rejection often reflects provider-family
            # configuration/auth/schema trouble. Missing provider configuration
            # is the same kind of boundary failure. Generic Employees should
            # therefore try a different governed provider before another model
            # behind the same failing boundary. Dedicated Research Employees
            # remain constrained to their provider family by
            # retry_model_eligible().
            1 if provider_family_fault and model.provider_key == failed_provider else 0,
            1 if failed_was_real and model.provider_key == "mock" else 0,
            _score(model),
            model.id,
        ),
    )[0]


def policy_audit(employee, selected_model, operation=None) -> dict:
    assigned = getattr(employee, "current_model", None)
    return {
        "employee_id": getattr(employee, "id", None),
        "assigned_model_id": getattr(assigned, "id", None),
        "selected_model_id": getattr(selected_model, "id", None),
        "selected_provider": getattr(selected_model, "provider_key", None),
        "selected_model": getattr(selected_model, "model_name", None),
        "override_applied": bool(assigned and assigned.id != selected_model.id),
        "constraints": execution_constraints(operation),
        "dedicated_provider_family": _dedicated_provider(employee),
    }
