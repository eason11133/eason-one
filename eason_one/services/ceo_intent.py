"""Shared Founder-to-CEO intent guards.

The deterministic status path is deliberately narrow. A request that contains
status words but also asks for a plan, analysis, improvement, implementation,
or a bounded deliverable must enter the governed CEO Run path.
"""
from __future__ import annotations

import re


def normalize(text: str | None) -> str:
    return " ".join((text or "").lower().split())


_DIRECT_WORK_TERMS = (
    " build ", " implement ", " develop ", " improve ", " fix ",
    " remove ", " replace ", " refactor ", " upgrade ", " eliminate ",
    " solve ", " research ", " investigate ", " analyze ", " analyse ",
    " compare ", " assign ", " launch ", " design ", " deliver ",
    " complete ", " have the team ", " ask the team ",
    "建立", "新增", "加入", "加上", "實作", "開發", "改善", "修正", "解決", "研究", "調查",
    "分析", "比較", "指派", "讓團隊", "叫員工", "完成", "交付",
)

_PLAN_OBJECTS = (
    "plan", "steps", "step", "objective", "acceptance criteria", "risk",
    "task", "employees", "employee", "team", "budget", "deliverable",
    "implementation", "development", "reliability",
)

_PLAN_VERBS = (
    "prepare", "create", "draft", "propose", "define", "design", "review",
)

_CHINESE_PLAN_PATTERNS = (
    "準備計畫", "提出計畫", "制定計畫", "三步", "步驟", "目標", "驗收標準",
    "風險", "參與員工", "員工分工", "下一個開發任務",
)


def requests_governed_work(text: str | None) -> bool:
    """True when the Founder asks for judgment/work, not a narrow read-only fact."""
    value = normalize(text)
    if not value:
        return False

    padded = f" {value} "
    if any(term in padded for term in _DIRECT_WORK_TERMS):
        return True
    if any(term in value for term in _CHINESE_PLAN_PATTERNS):
        return True

    # Covers requests such as "prepare a concrete three-step plan" where the
    # verb and object are separated by modifiers, while avoiding a plain
    # "status of the current plan" lookup.
    verbs = "|".join(re.escape(item) for item in _PLAN_VERBS)
    objects = "|".join(re.escape(item) for item in _PLAN_OBJECTS)
    return re.search(rf"\b(?:{verbs})\b.{{0,100}}\b(?:{objects})\b", value) is not None


def is_strict_company_status_request(text: str | None) -> bool:
    """Recognize only status-only requests safe for deterministic handling."""
    value = normalize(text)
    if not value or requests_governed_work(value):
        return False

    company_terms = (
        "company status", "status report", "company doing", "company state",
        "eason one status", "status of eason one", "eason one's current",
        "whole company", "working right now", "founder attention",
        "current priority mission", "公司現況", "公司狀態", "公司在做什麼",
    )
    if any(term in value for term in company_terms):
        return True
    return (
        any(term in value for term in ("eason one", "the company", "company"))
        and any(term in value for term in ("status", "doing now", "current", "briefing", "report"))
    )
