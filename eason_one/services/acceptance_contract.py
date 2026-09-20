"""Frozen Work acceptance contracts and proof ownership.

Work is the governing business truth.  Task remains a compatibility adapter.
The acceptance contract is frozen into Work.runtime_control_json before the
approved Work becomes executable, so retries/restarts cannot silently move the
goalposts.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Iterable

from .text_normalization import clean_rows, clean_text

CONTRACT_VERSION = "WORK_ACCEPTANCE_V1"

_REPO_REGRESSION_MARKERS = (
    "full test suite", "entire test suite", "complete test suite",
    "repository test suite", "repository-wide test", "repository wide test",
    "all tests", "all existing tests", "existing tests must pass",
    "regression suite", "full regression", "repository pytest", "pytest -q",
    "完整測試", "全套測試", "全部測試", "所有測試", "回歸測試", "不破壞既有測試",
)
_DB_MARKERS = ("database", "db", "sqlite", "資料庫")
_NO_EFFECT_MARKERS = (
    "no ", "without ", "must not", "does not", "do not", "read-only", "read only",
    "不", "無", "不得", "不能", "唯讀", "只讀", "不寫", "不修改",
)
_HTTP_RE = re.compile(r"\b(GET|POST|PUT|PATCH|DELETE)\s+(/[A-Za-z0-9_./<>:-]+)", re.IGNORECASE)
_HTTP_RESPONSE_MARKERS = (
    "http 200", "http status", "response json", "json response",
    "response contains", "response includes", "application version",
    "service=", "status=", "回應 json", "回傳 http", "回應包含",
    "回傳包含", "目前 application version",
)
_EXACT_REPLACEMENT_PATTERNS = (
    re.compile(r"\bfrom\s+[\"“](?P<old>.*?)[\"”]\s+to\s+[\"“](?P<new>.*?)[\"”]", re.I | re.S),
    re.compile(r"\bfrom\s+`(?P<old>.*?)`\s+to\s+`(?P<new>.*?)`", re.I | re.S),
    re.compile(r"\bchange\s*:\s*[\"“](?P<old>.*?)[\"”]\s+to\s*:\s*[\"“](?P<new>.*?)[\"”]", re.I | re.S),
    re.compile(r"\bchange\s*:\s*`(?P<old>.*?)`\s+to\s*:\s*`(?P<new>.*?)`", re.I | re.S),
)


def criteria_rows(value: str | Iterable[str] | None) -> list[str]:
    return clean_rows(value)


def norm(value: Any) -> str:
    return " ".join(clean_text(value, multiline=False).casefold().split()).strip(" .")


def _hash_json(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def criteria_hash(rows: list[str]) -> str:
    return _hash_json(rows)


def _has_exact_replacement(text: str) -> bool:
    for pattern in _EXACT_REPLACEMENT_PATTERNS:
        for match in pattern.finditer(text or ""):
            old = " ".join(match.group("old").split())
            new = " ".join(match.group("new").split())
            if old and new and old != new:
                return True
    return False


def classify_criterion(criterion: str, *, task_context: str = "") -> str:
    """Return the only proof-owner class currently authorized for this criterion.

    Keep deterministic classes intentionally narrow.  Unknown claims become
    independent-review requirements rather than being guessed into host proof.
    """
    text = str(criterion or "")
    lowered = text.casefold()

    if _HTTP_RE.search(text):
        return "HOST_HTTP_CONTRACT"

    # A response-only criterion often relies on the endpoint frozen in the
    # surrounding Work title/objective (for example C2 says only that JSON must
    # contain service/status/version).  If there is exactly one endpoint in the
    # complete Work context, this is still deterministic HTTP truth and must not
    # be turned into a paid semantic review merely because the model split the
    # method/path into C1.
    context_matches = {(m.group(1).upper(), m.group(2)) for m in _HTTP_RE.finditer(task_context or "")}
    non_health = {row for row in context_matches if row[1] != "/api/healthz"}
    one_primary_http = len(non_health) == 1 or (not non_health and len(context_matches) == 1)
    if one_primary_http and any(marker in lowered for marker in _HTTP_RESPONSE_MARKERS):
        return "HOST_HTTP_CONTRACT"

    if _has_exact_replacement(text):
        return "HOST_EXACT_REPLACEMENT"

    if any(marker in lowered for marker in _REPO_REGRESSION_MARKERS):
        return "HOST_REPOSITORY_REGRESSION"

    if (
        any(marker in lowered for marker in _DB_MARKERS)
        and any(marker in lowered for marker in _NO_EFFECT_MARKERS)
    ):
        # A no-DB-side-effect criterion can borrow only one unambiguous primary
        # endpoint. Never guess which endpoint owns a side-effect assertion.
        explicit = {(m.group(1).upper(), m.group(2)) for m in _HTTP_RE.finditer(text)}
        context_matches = {(m.group(1).upper(), m.group(2)) for m in _HTTP_RE.finditer(task_context or "")}
        non_health = {row for row in context_matches if row[1] != "/api/healthz"}
        if len(explicit) == 1 or len(non_health) == 1 or (not non_health and len(context_matches) == 1):
            return "HOST_DATABASE_NO_EFFECT"

    return "INDEPENDENT_REVIEW"


def build(
    *,
    title: str,
    objective: str,
    criteria: str | Iterable[str] | None,
    reviewer_employee_id: int | None = None,
    owner_employee_id: int | None = None,
    project_execution_terms_hash: str | None = None,
) -> dict:
    rows = criteria_rows(criteria)
    context = "\n".join(filter(None, [title or "", objective or "", *rows]))
    items: list[dict] = []
    for index, criterion in enumerate(rows, 1):
        proof_owner = classify_criterion(criterion, task_context=context)
        item = {
            "id": f"C{index}",
            "text": criterion,
            "normalized": norm(criterion),
            "proof_owner": proof_owner,
            "verifier_employee_id": (
                int(reviewer_employee_id)
                if proof_owner == "INDEPENDENT_REVIEW" and reviewer_employee_id is not None
                else None
            ),
        }
        items.append(item)

    contract_body = {
        "version": CONTRACT_VERSION,
        "criteria_hash": criteria_hash(rows),
        "producer_employee_id_at_freeze": int(owner_employee_id) if owner_employee_id is not None else None,
        "reviewer_employee_id": int(reviewer_employee_id) if reviewer_employee_id is not None else None,
        "project_execution_terms_hash": project_execution_terms_hash,
        "criteria": items,
    }
    contract_body["contract_hash"] = _hash_json(contract_body)
    return contract_body


def host_criteria(contract: dict) -> list[dict]:
    return [
        item for item in (contract.get("criteria") or [])
        if item.get("proof_owner") != "INDEPENDENT_REVIEW"
    ]


def semantic_criteria(contract: dict) -> list[dict]:
    return [
        item for item in (contract.get("criteria") or [])
        if item.get("proof_owner") == "INDEPENDENT_REVIEW"
    ]


def ensure_for_work(work, *, task=None) -> dict:
    """Return the frozen contract, creating it only before first execution.

    Once frozen, Work acceptance text and semantic reviewer identity may not be
    silently changed.  Task acceptance text is a compatibility projection and
    is corrected from Work rather than allowed to become authority.
    """
    control = dict(getattr(work, "runtime_control_json", None) or {})
    frozen = dict(control.get("acceptance_contract") or {})
    rows = criteria_rows(getattr(work, "acceptance_criteria", None))
    if not rows:
        raise ValueError(
            "WORK_ACCEPTANCE_CRITERIA_MISSING: executable Work has no Founder-approved acceptance criterion."
        )

    if task is not None and getattr(task, "acceptance_criteria", None) != getattr(work, "acceptance_criteria", None):
        task.acceptance_criteria = getattr(work, "acceptance_criteria", None)

    if frozen:
        if frozen.get("criteria_hash") != criteria_hash(rows):
            # Historical contracts may contain transport-only U+FFFC/U+FFFD
            # characters from provider truncation. Installing the sanitizer must
            # not retroactively declare those immutable contracts tampered. Allow
            # only the exact case where removing forbidden control noise makes
            # frozen text equal to the current Work projection; never allow a
            # semantic wording change.
            frozen_clean = clean_rows([
                item.get("text") for item in (frozen.get("criteria") or [])
                if isinstance(item, dict)
            ])
            if criteria_hash(frozen_clean) != criteria_hash(rows):
                raise ValueError(
                    "WORK_ACCEPTANCE_CONTRACT_CHANGED: Founder-approved Work acceptance text changed after authority was frozen."
                )
        semantic = semantic_criteria(frozen)
        frozen_reviewer = frozen.get("reviewer_employee_id")
        if semantic and task is not None and getattr(task, "reviewer_employee_id", None) != frozen_reviewer:
            raise ValueError(
                "WORK_ACCEPTANCE_REVIEWER_CHANGED: semantic verification ownership changed after approval."
            )
        frozen_terms_hash = frozen.get("project_execution_terms_hash")
        project = getattr(work, "project", None)
        if frozen_terms_hash and project is not None:
            current_terms_hash = __import__(
                "eason_one.services.project_contract", fromlist=["execution_terms_hash"]
            ).execution_terms_hash(project)
            if str(current_terms_hash) != str(frozen_terms_hash):
                raise ValueError(
                    "WORK_PROJECT_TERMS_CHANGED: Founder Project objective/success/constraints/deadline changed after this Work was approved; replan instead of executing stale Work."
                )
        return frozen

    reviewer_id = getattr(task, "reviewer_employee_id", None) if task is not None else None
    owner_id = getattr(task, "assigned_employee_id", None) if task is not None else None
    if owner_id is None:
        assignment = next(
            (row for row in getattr(work, "assignments", []) if getattr(row, "ended_at", None) is None),
            None,
        )
        owner_id = getattr(assignment, "employee_id", None)

    project = getattr(work, "project", None)
    project_terms_hash = None
    if project is not None:
        contracts = __import__(
            "eason_one.services.project_contract", fromlist=["is_vnext_governed", "execution_terms_hash"]
        )
        if contracts.is_vnext_governed(project):
            project_terms_hash = contracts.execution_terms_hash(project)
    frozen = build(
        title=getattr(work, "title", "") or "",
        objective=getattr(work, "purpose", "") or "",
        criteria=rows,
        reviewer_employee_id=reviewer_id,
        owner_employee_id=owner_id,
        project_execution_terms_hash=project_terms_hash,
    )
    control["acceptance_contract"] = frozen
    work.runtime_control_json = control
    try:
        events = __import__(
            "eason_one.services.company_events", fromlist=["emit", "correlation_for_work"]
        )
        events.emit(
            "WORK_ACCEPTANCE_CONTRACT_FROZEN",
            actor_type="RUNTIME",
            project_id=getattr(work, "project_id", None),
            work_id=getattr(work, "id", None),
            correlation_id=events.correlation_for_work(getattr(work, "id", None)),
            payload={
                "version": frozen.get("version"),
                "contract_hash": frozen.get("contract_hash"),
                "criteria_hash": frozen.get("criteria_hash"),
                "criterion_owners": {
                    item.get("id"): item.get("proof_owner")
                    for item in (frozen.get("criteria") or [])
                },
                "reviewer_employee_id": frozen.get("reviewer_employee_id"),
            },
        )
    except Exception:
        # Contract truth must not depend on the observation stream being healthy.
        # The owning transaction still persists runtime_control_json.
        pass
    return frozen
