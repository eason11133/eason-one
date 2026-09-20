"""Research Department policy for persistent model-specialized Researchers.

The Department is intentionally organizational rather than an agent graph:

CEO -> Research Director -> persistent provider-family Researchers

Each specialist is a durable Employee with its own Employee ID, memory, EC
compensation and outcome history.  The provider/model remains an execution core;
exact model versions may change without creating a new Employee identity.

This module contains deterministic organization/routing helpers only.  It does
not create Founder authority, call a provider, or mutate Project/Work scope.
"""
from __future__ import annotations

from decimal import Decimal
import re

from ..models import Employee, Work

POLICY_VERSION = "RESEARCH_DEPARTMENT_V1"

SPECIALISTS = (
    {"provider": "openai", "slug": "openai-researcher", "name": "OpenAI Researcher", "label": "OpenAI"},
    {"provider": "anthropic", "slug": "claude-researcher", "name": "Claude Researcher", "label": "Claude"},
    {"provider": "gemini", "slug": "gemini-researcher", "name": "Gemini Researcher", "label": "Gemini"},
    {"provider": "perplexity", "slug": "perplexity-researcher", "name": "Perplexity Researcher", "label": "Perplexity"},
)
PROVIDER_BY_SLUG = {row["slug"]: row["provider"] for row in SPECIALISTS}
SPEC_BY_PROVIDER = {row["provider"]: row for row in SPECIALISTS}
LABEL_BY_PROVIDER = {row["provider"]: row["label"] for row in SPECIALISTS}

_PROVIDER_MARKERS = {
    "openai": ("openai", "gpt", "chatgpt"),
    "anthropic": ("anthropic", "claude", "克勞德"),
    "gemini": ("gemini", "google ai", "google model", "谷歌 ai"),
    "perplexity": ("perplexity", "sonar"),
}
_MULTI_MARKERS = (
    "multi-ai", "multi ai", "multiple ai", "multiple ais", "multiple models",
    "different ai", "different ais", "different models", "several ai", "several models",
    "independent ai", "independent models", "more than one ai", "more than one model",
    "多個ai", "多個 ai", "多ai", "多 ai", "多模型", "多個模型", "不同ai", "不同 ai",
    "不同模型", "各個ai", "各個 ai", "各種ai", "各種 ai", "各模型", "四個ai", "四個 ai",
    "三個ai", "三個 ai", "兩個ai", "兩個 ai", "多家ai", "多家 ai",
)
_RESEARCH_MARKERS = (
    "research", "investigate", "evidence", "study", "market research", "deep research",
    "研究", "調查", "查資料", "找資料", "資料蒐集", "市場研究",
)
_HEAD_DELEGATION_MARKERS = (
    "research director decide", "research director choose", "head of research choose",
    "research department choose", "research department decide", "let research choose",
    "delegate to research director", "研究主管決定", "研究主管挑", "研究主管選",
    "研究部門主管決定", "研究部門主管挑", "研究部門主管選", "讓研究主管安排",
    "叫研究主管安排", "研究部門自己挑", "研究部門自己選", "讓研究部門安排",
)
_SYNTHESIS_MARKERS = (
    "synthes", "integrat", "reconcile", "consolidat", "combine findings", "compare findings",
    "department conclusion", "final research brief", "cross-model conclusion",
    "整合", "綜合", "彙整", "總結各", "比較各", "統整", "研究部門結論",
)
_MEETING_MARKERS = ("meeting", "debate", "discuss together", "開會", "辯論", "一起討論")


def _text(*values) -> str:
    return " ".join(" ".join(str(value or "").casefold().split()) for value in values)


def provider_family(employee: Employee | None) -> str | None:
    return PROVIDER_BY_SLUG.get(str(getattr(employee, "slug", "") or "").strip().casefold())


def is_specialist(employee: Employee | None) -> bool:
    return provider_family(employee) is not None


def specialist_employees(*, active_only: bool = True) -> list[Employee]:
    query = Employee.query.filter(Employee.slug.in_(list(PROVIDER_BY_SLUG)))
    if active_only:
        query = query.filter_by(active=True)
    by_slug = {row.slug: row for row in query.order_by(Employee.id).all()}
    return [by_slug[row["slug"]] for row in SPECIALISTS if row["slug"] in by_slug]


_CONSTRAINT_SPLIT_RE = re.compile(
    r"(?:[,，;；。!?！？]+|\bbut\b|\binstead\b|\bonly\s+use\b|\buse\s+only\b|但是|但|而是|不過|不过|只要|只用|改用)",
    flags=re.IGNORECASE,
)
_NEGATIVE_PROVIDER_MARKERS = (
    "do not use", "don't use", "dont use", "do not", "don't", "dont",
    "without", "exclude", "excluding", "except", "anything but", "not ", "no ",
    "不要用", "不要叫", "不要找", "不要", "不用", "別用", "别用",
    "別叫", "别叫", "排除", "禁止", "不准", "不是", "除了",
)


def _providers_in_text(value: str) -> list[str]:
    found = []
    for row in SPECIALISTS:
        provider = row["provider"]
        if any(marker in value for marker in _PROVIDER_MARKERS[provider]):
            found.append(provider)
    return found


def provider_constraints(text: str) -> dict[str, list[str]]:
    """Return deterministic include/exclude provider intent.

    Provider names are not enough: Founder negation is authority.  Split common
    contrast clauses so requests such as ``不要 OpenAI，只要 Claude`` cannot be
    inverted by a later keyword-count shortcut.  The last explicit clause wins
    when a provider is mentioned more than once.
    """
    value = _text(text)
    states: dict[str, str] = {}
    for clause in (part.strip() for part in _CONSTRAINT_SPLIT_RE.split(value)):
        if not clause:
            continue
        providers = _providers_in_text(clause)
        if not providers:
            continue
        negative = any(marker in clause for marker in _NEGATIVE_PROVIDER_MARKERS)
        state = "EXCLUDE" if negative else "INCLUDE"
        for provider in providers:
            states[provider] = state
    return {
        "included": [row["provider"] for row in SPECIALISTS if states.get(row["provider"]) == "INCLUDE"],
        "excluded": [row["provider"] for row in SPECIALISTS if states.get(row["provider"]) == "EXCLUDE"],
    }


def requested_providers(text: str) -> list[str]:
    return provider_constraints(text)["included"]

_ONLY_MARKERS = ("only", "only use", "use only", "只要", "只用", "只叫", "只能用", "僅用", "仅用")

def exclusive_requested_providers(text: str) -> list[str]:
    """Return an explicit research-provider allowlist when Founder says *only*.

    This is narrower than ``requested_providers``: naming Claude and GPT for a
    multi-AI comparison selects those branches, while wording such as ``只用
    Claude`` also forbids the Research Director/retry path from silently adding
    another provider family during the research stage.
    """
    value = _text(text)
    if not any(marker in value for marker in _ONLY_MARKERS):
        return []
    included = requested_providers(value)
    return included


def excluded_providers(text: str) -> list[str]:
    return provider_constraints(text)["excluded"]


def execution_provider_constraints(text: str) -> dict[str, list[str]]:
    """Compile provider authority that must survive planning into Runtime.

    Exclusions are global provider prohibitions. A Research allowlist is narrower:
    it applies to Research Department execution when Founder says ``only`` or
    explicitly names a multi-AI roster, preventing Director synthesis/retry from
    silently introducing an unrequested research provider.
    """
    constraints = provider_constraints(text)
    allowed = exclusive_requested_providers(text)
    if not allowed and len(constraints["included"]) >= 2 and requests_multi_ai_research(text):
        allowed = list(constraints["included"])
    excluded = list(constraints["excluded"])
    allowed = [provider for provider in allowed if provider not in set(excluded)]
    return {
        "excluded_providers": excluded,
        "research_allowed_providers": allowed,
    }


def requests_research(text: str) -> bool:
    value = _text(text)
    return any(marker in value for marker in _RESEARCH_MARKERS)


def requests_multi_ai_research(text: str) -> bool:
    value = _text(text)
    if not requests_research(value):
        return False
    constraints = provider_constraints(value)
    return len(constraints["included"]) >= 2 or any(marker in value for marker in _MULTI_MARKERS)


def requests_head_delegation(text: str) -> bool:
    value = _text(text)
    return requests_research(value) and any(marker in value for marker in _HEAD_DELEGATION_MARKERS)


def explicit_meeting_request(text: str) -> bool:
    value = _text(text)
    return any(marker in value for marker in _MEETING_MARKERS)


def is_synthesis_text(*values) -> bool:
    value = _text(*values)
    return any(marker in value for marker in _SYNTHESIS_MARKERS)


def _task_for_work(work: Work | None):
    if work is None:
        return None
    # Task deliberately does not expose a SQLAlchemy backref on Work.  Use the
    # canonical compatibility resolver so Department routing sees the same
    # Task metadata as the execution runtime.
    from . import work_runtime
    return work_runtime.task_for_work(work)


def is_synthesis_work(work: Work | None) -> bool:
    if work is None:
        return False
    task = _task_for_work(work)
    return is_synthesis_text(
        getattr(work, "title", None), getattr(work, "purpose", None), getattr(work, "expected_output", None),
        getattr(task, "title", None), getattr(task, "objective", None), getattr(task, "required_output", None),
    )


def _model_operating_cost(employee: Employee) -> Decimal:
    model = getattr(employee, "current_model", None)
    if model is None:
        return Decimal("999999")
    return (
        Decimal(getattr(model, "input_price_per_million", 0) or 0)
        + Decimal(getattr(model, "output_price_per_million", 0) or 0) * Decimal("2")
        + Decimal(getattr(model, "request_price_per_call", 0) or 0)
    )


def select_department_specialist(work: Work) -> tuple[Employee | None, dict]:
    """Choose one persistent specialist for a Research Director-owned Work.

    Explicit provider intent wins. Otherwise the Department prefers a sourced
    search-capable provider for ordinary external research, then accepted
    experience, lower active workload, lower configured operating cost and a
    stable Employee id.  This is staffing only; it cannot expand Work authority.
    """
    employees = specialist_employees(active_only=True)
    if not employees:
        return None, {"policy": POLICY_VERSION, "reason": "No active model-specialized Researcher exists."}

    task = _task_for_work(work)
    text = _text(
        getattr(work, "title", None), getattr(work, "purpose", None), getattr(work, "expected_output", None),
        getattr(task, "title", None), getattr(task, "objective", None), getattr(task, "acceptance_criteria", None),
    )
    constraints = provider_constraints(text)
    explicit = constraints["included"]
    durable = __import__(
        "eason_one.services.execution_policy", fromlist=["execution_constraints"]
    ).execution_constraints(getattr(work, "operation", None))
    if durable.get("constraint_conflict"):
        return None, {
            "policy": POLICY_VERSION,
            "decision_basis": "FOUNDER_PROVIDER_CONSTRAINT_CONFLICT",
            "reason": "Current Project and Mission Research provider authority have no permitted intersection.",
        }
    excluded = set(constraints["excluded"]) | {
        str(value or "").strip().casefold()
        for value in (durable.get("excluded_providers") or [])
        if str(value or "").strip()
    }
    allowed = {
        str(value or "").strip().casefold()
        for value in (durable.get("research_allowed_providers") or [])
        if str(value or "").strip()
    }
    if explicit:
        wanted = explicit[0]
        if wanted in excluded or (allowed and wanted not in allowed):
            return None, {
                "policy": POLICY_VERSION,
                "decision_basis": "FOUNDER_PROVIDER_CONSTRAINT_CONFLICT",
                "requested_provider": wanted,
                "excluded_providers": sorted(excluded),
                "research_allowed_providers": sorted(allowed),
                "reason": "The approved Work conflicts with durable Founder/Project provider authority and must not be guessed.",
            }
        chosen = next((row for row in employees if provider_family(row) == wanted), None)
        if chosen is not None:
            return chosen, {
                "policy": POLICY_VERSION,
                "decision_basis": "EXPLICIT_PROVIDER_SPECIALIZATION",
                "requested_provider": wanted,
                "excluded_providers": sorted(excluded),
                "research_allowed_providers": sorted(allowed),
                "selected_provider": wanted,
                "reason": f"The approved Work explicitly names {LABEL_BY_PROVIDER[wanted]}; Research Director delegated to that persistent specialist.",
            }

    eligible_employees = [
        row for row in employees
        if provider_family(row) not in excluded
        and (not allowed or provider_family(row) in allowed)
    ]
    if not eligible_employees:
        return None, {
            "policy": POLICY_VERSION,
            "decision_basis": "FOUNDER_PROVIDER_EXCLUSIONS_EXHAUSTED",
            "excluded_providers": sorted(excluded),
            "reason": "Founder provider exclusions leave no eligible Research Department specialist.",
        }

    formation = __import__("eason_one.services.team_formation", fromlist=["_active_workload"])
    evolution = __import__("eason_one.services.employee_evolution", fromlist=["selection_profile"])
    rows = []
    for employee in eligible_employees:
        family = provider_family(employee)
        workload = formation._active_workload(employee.id)
        profile = evolution.selection_profile(
            employee, "RESEARCH", direct=1, specialist=1, active_workload=workload
        )
        # Ordinary research benefits from at least one live-source capable core.
        source_fit = 1 if family in {"openai", "perplexity"} else 0
        rank = (
            source_fit,
            int(profile.get("experience_score") or 0),
            -int(workload),
            -_model_operating_cost(employee),
            -employee.id,
        )
        rows.append((rank, employee, profile))
    rows.sort(key=lambda row: row[0], reverse=True)
    _, chosen, profile = rows[0]
    return chosen, {
        "policy": POLICY_VERSION,
        "decision_basis": "DEPARTMENT_HEAD_DELEGATION",
        "requested_provider": None,
        "excluded_providers": sorted(excluded),
        "research_allowed_providers": sorted(allowed),
        "selected_provider": provider_family(chosen),
        "selected_experience_score": int(profile.get("experience_score") or 0),
        "selected_learning_record_ids": list(profile.get("experience_record_ids") or []),
        "active_workload": int(profile.get("active_workload") or 0),
        "reason": (
            "Research Director selected an already-capable persistent specialist using source-fit, "
            "accepted outcome experience, current workload, configured operating cost and a stable tie-break."
        ),
    }


def _clean_criteria(values, extra: str) -> list[str]:
    rows = []
    for value in list(values or []) + [extra]:
        text = " ".join(str(value or "").split())
        if text and text not in rows:
            rows.append(text[:280])
        if len(rows) >= 8:
            break
    return rows or [extra[:280]]


def _task(provider: str, employee: Employee, seed_task: dict) -> dict:
    label = LABEL_BY_PROVIDER[provider]
    seed_title = " ".join(str(seed_task.get("title") or "Approved research").split())
    seed_objective = " ".join(str(seed_task.get("objective") or seed_title).split())
    source_rule = (
        "Use provider-observed sources when the governed runtime supplies live search; preserve source lineage."
        if provider in {"openai", "perplexity"}
        else
        "This provider branch may be model-perspective only in the current runtime; never fabricate live sources or citations."
    )
    return {
        "title": f"Independent {label} research — {seed_title}"[:180],
        "objective": (
            f"Independently execute the approved research objective as the {label} specialist: {seed_objective}. "
            "Do not consult sibling Researcher conclusions before submitting your own perspective."
        ),
        "assignee_employee_id": employee.id,
        "reviewer_employee_id": None,
        "required_capabilities": ["RESEARCH"],
        "acceptance_criteria": _clean_criteria(seed_task.get("acceptance_criteria"), source_rule),
        "write_scope": seed_task.get("write_scope"),
    }


def _director_synthesis_task(director: Employee, seed_task: dict) -> dict:
    seed_title = " ".join(str(seed_task.get("title") or "Approved research").split())
    seed_objective = " ".join(str(seed_task.get("objective") or seed_title).split())
    criterion = (
        "Use every accepted specialist output, distinguish sourced evidence from model-only reasoning, "
        "preserve material disagreements, and expose unresolved uncertainty."
    )
    return {
        "title": f"Synthesize Research Department findings — {seed_title}"[:180],
        "objective": (
            f"Reconcile all accepted independent Researcher outputs for the approved research objective: {seed_objective}. "
            "This is the canonical Research Department output for downstream approved Work."
        ),
        "assignee_employee_id": director.id,
        "reviewer_employee_id": None,
        "required_capabilities": ["RESEARCH"],
        "acceptance_criteria": _clean_criteria(seed_task.get("acceptance_criteria"), criterion),
        "write_scope": seed_task.get("write_scope"),
    }


def _research_seed_tasks(tasks: list[dict]) -> list[dict]:
    return [
        item for item in tasks
        if isinstance(item, dict)
        and [str(value or "").strip().upper() for value in (item.get("required_capabilities") or [])] == ["RESEARCH"]
        and not is_synthesis_text(item.get("title"), item.get("objective"))
    ]


def normalize_ceo_plan(payload: dict, founder_request: str) -> tuple[dict, list[str]]:
    """Compile Research Department intent without deleting approved CEO work.

    Multi-AI expansion replaces exactly the single research seed Task in-place
    with persistent specialist branches plus one Director synthesis. Every other
    CEO-planned Task is preserved byte-for-structure so Research -> Strategy ->
    Delivery projects cannot silently collapse into research-only projects.
    """
    if not isinstance(payload, dict) or payload.get("mode") != "OPERATION_PLAN":
        return payload, []
    operation = payload.get("operation")
    if not isinstance(operation, dict) or not requests_research(founder_request):
        return payload, []

    notes = []
    active = specialist_employees(active_only=True)
    by_provider = {provider_family(row): row for row in active}
    director = Employee.query.filter_by(slug="research-director", active=True).first()
    if director is None:
        return payload, []

    constraints = provider_constraints(founder_request)
    requested = list(constraints["included"])
    excluded = set(constraints["excluded"])
    if set(requested) & excluded:
        raise ValueError("RESEARCH_DEPARTMENT_PROVIDER_CONFLICT: Founder provider intent is contradictory; Company must not guess.")

    tasks = list(operation.get("tasks") or [])

    if requests_multi_ai_research(founder_request):
        unavailable = [provider for provider in requested if provider not in by_provider]
        if unavailable:
            labels = ", ".join(LABEL_BY_PROVIDER[provider] for provider in unavailable)
            raise ValueError(
                f"RESEARCH_DEPARTMENT_PROVIDER_UNAVAILABLE: requested provider-specialized Researcher unavailable: {labels}."
            )

        # Explicit two-or-more provider names are an exact roster. Generic
        # multi-AI uses every active specialist except Founder-excluded families.
        if len(requested) >= 2:
            selected = [provider for provider in requested if provider not in excluded]
        else:
            selected = [provider for provider in requested if provider not in excluded]
            for row in SPECIALISTS:
                provider = row["provider"]
                if provider in by_provider and provider not in excluded and provider not in selected:
                    selected.append(provider)
        selected = selected[:4]
        if len(selected) < 2:
            raise ValueError(
                "RESEARCH_DEPARTMENT_MULTI_AI_UNAVAILABLE: Founder constraints leave fewer than two eligible Researcher Employees."
            )

        seeds = _research_seed_tasks(tasks)
        if len(seeds) != 1:
            raise ValueError(
                "RESEARCH_DEPARTMENT_RESEARCH_SCOPE_AMBIGUOUS: multi-AI research requires exactly one CEO-planned research seed Task; "
                "Company must regenerate the plan rather than delete, merge, or guess across multiple approved research scopes."
            )
        seed = seeds[0]
        branch_tasks = [_task(provider, by_provider[provider], seed) for provider in selected]
        director_task = _director_synthesis_task(director, seed)
        expanded = []
        for item in tasks:
            if item is seed:
                expanded.extend(branch_tasks)
                expanded.append(director_task)
            else:
                expanded.append(item)
        if len(expanded) > 12:
            raise ValueError(
                "RESEARCH_DEPARTMENT_TASK_LIMIT: safe multi-AI expansion would exceed the governed 12-Task operation limit."
            )
        operation["tasks"] = expanded

        if not explicit_meeting_request(founder_request):
            operation["meeting_policy"] = "NEVER"
            config = dict(operation.get("meeting_config") or {})
            config.update({
                "trigger": "NEVER", "participant_employee_ids": [], "max_rounds": 1,
                "max_speakers_per_round": 1, "contribution_output_cap": 192,
                "token_limit": 6000, "budget_twd": 0, "retry_limit": 0,
            })
            operation["meeting_config"] = config

        # Keep every original completion criterion. Add Department requirements
        # rather than replacing Project semantics with a research-only finish line.
        completion = list(operation.get("completion_criteria") or [])
        for criterion in (
            "Every selected specialist Research Work is accepted.",
            "The Research Director synthesis is accepted and preserves material disagreements.",
        ):
            if criterion not in completion:
                completion.append(criterion)
        operation["completion_criteria"] = completion[:8]

        names = ", ".join(by_provider[provider].name for provider in selected)
        payload["executive_response"] = (
            f"I’ll run the approved research stage through {names} independently, have the Research Director reconcile the results, "
            "and then continue the rest of the CEO-planned Project without dropping downstream Work."
        )
        notes.append("Research Department multi-AI organization compiled without deleting downstream CEO-planned Tasks")
        return payload, notes

    # Single-provider and Department-delegation requests preserve Founder
    # exclusions inside the durable Task objective so later Team Formation sees
    # the same constraint instead of silently choosing a forbidden provider.
    research_task = next(iter(_research_seed_tasks(tasks)), None)
    if research_task is None:
        return payload, notes
    if excluded:
        labels = ", ".join(LABEL_BY_PROVIDER[provider] for provider in sorted(excluded))
        suffix = f" Founder provider constraint: do not use {labels}."
        if suffix.strip() not in str(research_task.get("objective") or ""):
            research_task["objective"] = (str(research_task.get("objective") or "").rstrip() + suffix).strip()

    if requested:
        wanted = requested[0]
        employee = by_provider.get(wanted)
        if employee is None:
            raise ValueError(
                f"RESEARCH_DEPARTMENT_PROVIDER_UNAVAILABLE: no active {LABEL_BY_PROVIDER[wanted]} Researcher is configured."
            )
        research_task["assignee_employee_id"] = employee.id
        research_task["reviewer_employee_id"] = None
        notes.append(f"Explicit {LABEL_BY_PROVIDER[wanted]} Researcher selection preserved as Employee accountability")
        return payload, notes

    if requests_head_delegation(founder_request) or excluded:
        research_task["assignee_employee_id"] = director.id
        research_task["reviewer_employee_id"] = None
        notes.append("Research Director delegation requested; Team Formation will choose one eligible persistent department specialist")
    return payload, notes

