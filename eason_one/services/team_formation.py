"""Work-first team formation for governed Eason One Projects.

This layer does not implement an agent framework.  It turns already-approved
Project/Work truth into organizational staffing truth:

Work requirement -> current roster capability match -> WorkAssignment
                 -> explicit HiringRequest only when the Company has a real gap.

The capability vocabulary is deliberately small and operational.  It can be
expanded without changing Work/Employee identity or execution authority.
"""
from __future__ import annotations

from collections import defaultdict

from ..extensions import db
from ..models import AgentRun, Employee, EmployeeLearningRecord, HiringRequest, Project, Work
from . import work_runtime
from .company_events import emit

VERSION = "TEAM_FORMATION_V1"

# Strong role/capability signals only.  Unknown/mixed work stays with the CEO's
# approved assignee instead of inventing a staffing problem from weak text.
_CAPABILITY_RULES = (
    ("SOFTWARE_ENGINEERING", ("repository", "code", "coding", "api", "backend", "frontend", "bug", "implement", "software", "database", "migration", "deploy")),
    ("RESEARCH", ("research", "investigate", "evidence", "sources", "market", "compare", "benchmark", "literature", "findings", "study")),
    ("CRITICAL_REVIEW", ("critic", "critique", "audit", "adversarial", "review", "verify", "verification", "risk review", "challenge")),
    ("PRODUCT_STRATEGY", ("product strategy", "requirements", "roadmap", "user story", "product brief", "prioritize", "prioritization")),
    ("PRODUCT_DESIGN", ("ui", "ux", "interface", "wireframe", "prototype", "visual design", "interaction design")),
    ("MARKETING", ("marketing", "campaign", "growth", "acquisition", "positioning", "go-to-market", "social media", "seo")),
    ("FINANCE", ("financial", "finance", "forecast", "cash flow", "runway", "accounting", "valuation")),
    ("LEGAL_COMPLIANCE", ("legal", "contract", "compliance", "privacy", "terms", "regulation", "license review")),
    ("OPERATIONS", ("operations", "vendor", "process", "workflow", "rollout", "launch operations", "support operations")),
    ("CONTENT", ("copywriting", "content", "article", "script", "editorial", "copy")),
)

_ROLE_CAPABILITIES = {
    "ceo": {"EXECUTIVE_MANAGEMENT", "PRODUCT_STRATEGY", "OPERATIONS"},
    "research-director": {"RESEARCH", "CRITICAL_REVIEW"},
    "researcher": {"RESEARCH"},
    "openai-researcher": {"RESEARCH"},
    "claude-researcher": {"RESEARCH"},
    "gemini-researcher": {"RESEARCH"},
    "perplexity-researcher": {"RESEARCH"},
    "engineer": {"SOFTWARE_ENGINEERING"},
    "critic": {"CRITICAL_REVIEW"},
    "hr-director": {"WORKFORCE", "OPERATIONS"},
}

_ROLE_FOR_CAPABILITY = {
    "SOFTWARE_ENGINEERING": "Software Engineer",
    "RESEARCH": "Researcher",
    "CRITICAL_REVIEW": "Critic / Independent Reviewer",
    "PRODUCT_STRATEGY": "Product Strategist",
    "PRODUCT_DESIGN": "Product Designer",
    "MARKETING": "Marketing Specialist",
    "FINANCE": "Finance Specialist",
    "LEGAL_COMPLIANCE": "Legal / Compliance Specialist",
    "OPERATIONS": "Operations Specialist",
    "CONTENT": "Content Specialist",
}

CANONICAL_DELIVERY_CAPABILITIES = frozenset(_ROLE_FOR_CAPABILITY)


def _text(work: Work) -> str:
    return " ".join(
        str(value or "").casefold()
        for value in (work.title, work.purpose, work.expected_output, work.acceptance_criteria)
    )



def declared_capabilities(work: Work) -> list[str]:
    requirements = dict((work.runtime_control_json or {}).get("staffing_requirements") or {})
    return [
        str(value).strip().upper()
        for value in (requirements.get("required_capabilities") or [])
        if str(value).strip()
    ]

def infer_primary_capability(work: Work) -> tuple[str | None, str | None]:
    """Return the declared primary capability, with conservative legacy fallback."""
    declared = declared_capabilities(work)
    if declared:
        # One Work has one accountable specialization. Materially different
        # capabilities must be separate Tasks/Works so ownership, budget,
        # dependency, handoff, review and recovery remain explicit company truth.
        if len(declared) != 1:
            return None, "invalid multi-capability Work; management replan required"
        return declared[0], "declared by approved CEO Operation plan"
    text = _text(work)
    scores = []
    for capability, markers in _CAPABILITY_RULES:
        hits = [marker for marker in markers if marker in text]
        if hits:
            scores.append((len(hits), capability, hits))
    if not scores:
        return None, None
    scores.sort(reverse=True)
    if len(scores) > 1 and scores[0][0] == scores[1][0]:
        return None, None
    score, capability, hits = scores[0]
    return capability, f"matched work markers: {', '.join(hits[:4])}"


def employee_capabilities(employee: Employee) -> set[str]:
    values = set(_ROLE_CAPABILITIES.get(employee.slug, set()))
    haystack = " ".join(
        str(value or "").casefold()
        for value in (
            employee.position.name if employee.position else "",
            employee.role_description,
        )
    )
    for capability, markers in _CAPABILITY_RULES:
        canonical_words = capability.replace("_", " ").casefold()
        if canonical_words in haystack or capability.casefold() in haystack or any(marker in haystack for marker in markers):
            values.add(capability)
    return values


def _active_workload(employee_id: int) -> int:
    Assignment = __import__("eason_one.models", fromlist=["WorkAssignment"]).WorkAssignment
    return int(
        db.session.query(db.func.count(db.distinct(Work.project_id)))
        .select_from(Assignment)
        .join(Work, Assignment.work_id == Work.id)
        .join(Project, Work.project_id == Project.id)
        .filter(
            Assignment.employee_id == employee_id,
            Assignment.ended_at.is_(None),
            Project.environment == "LIVE",
            Project.status.in_(["PLANNING", "ACTIVE", "BLOCKED", "REVIEW"]),
            Work.state.in_(["READY", "EXECUTING", "VERIFYING", "WAITING"]),
        )
        .scalar() or 0
    )


def _capacity_eligible_for_work(employee: Employee, work: Work) -> bool:
    """An ordinary Employee may queue Work only inside one active Project."""
    view = __import__(
        "eason_one.services.ceo_operating", fromlist=["employee_capacity_view"]
    ).employee_capacity_view(employee)
    if view.get("capacity") == "AVAILABLE":
        return True
    return bool(
        not view.get("assignment_conflict")
        and int(view.get("active_project_id") or 0) == int(work.project_id)
    )


def _experience_count(employee_id: int, capability: str) -> int:
    """Compatibility score backed only by accepted Work experience."""
    employee = db.session.get(Employee, employee_id)
    if employee is None:
        return 0
    evolution = __import__(
        "eason_one.services.employee_evolution", fromlist=["capability_profile"]
    )
    return int(evolution.capability_profile(employee, capability)["score"])


def ranked_existing_employees(capability: str, *, exclude_employee_id: int | None = None) -> list[tuple]:
    """Return deterministic eligible roster ranking for one capability.

    Role/capability truth stays primary. Accepted cross-Project experience is a
    bounded tie-breaker, followed by current workload. This is the first place
    where Persistent Employee learning is allowed to change future behavior.
    """
    evolution = __import__(
        "eason_one.services.employee_evolution", fromlist=["selection_profile"]
    )
    candidates = []
    for employee in Employee.query.filter_by(active=True).order_by(Employee.id).all():
        if employee.id == exclude_employee_id:
            continue
        # CEO capability informs management/team design, but CEO is not a
        # catch-all delivery specialist. Missing delivery capability should
        # become an explicit staffing gap instead of centralizing work in CEO.
        if employee.slug == "ceo":
            continue
        if capability not in employee_capabilities(employee):
            continue
        direct = 1 if capability in _ROLE_CAPABILITIES.get(employee.slug, set()) else 0
        level = int(getattr(getattr(employee, "position", None), "level", 1) or 1)
        specialist = 1 if level < 3 and not employee.slug.endswith("-director") else 0
        workload = _active_workload(employee.id)
        profile = evolution.selection_profile(
            employee, capability, direct=direct, specialist=specialist, active_workload=workload
        )
        # The first two terms preserve organizational role truth. Evolution can
        # change selection only among otherwise equivalent eligible employees.
        rank = (direct, specialist, int(profile["experience_score"]), -workload, -employee.id)
        candidates.append((*rank, employee, profile))
    candidates.sort(key=lambda row: row[:5], reverse=True)
    return candidates


def best_existing_employee(capability: str, *, exclude_employee_id: int | None = None) -> Employee | None:
    candidates = ranked_existing_employees(capability, exclude_employee_id=exclude_employee_id)
    return candidates[0][5] if candidates else None


def _candidate_allowed_for_work(employee: Employee, work: Work, capability: str) -> bool:
    """Reject staffing choices that Runtime would deterministically forbid.

    This matters most for provider-specialized Research Employees: assigning a
    forbidden provider and discovering the constraint only at dispatch time is
    avoidable company churn. Generic employees remain routable through normal
    execution policy.
    """
    if str(capability or "").upper() != "RESEARCH":
        return True
    department = __import__(
        "eason_one.services.research_department", fromlist=["provider_family"]
    )
    provider = department.provider_family(employee)
    constraints = __import__(
        "eason_one.services.execution_policy", fromlist=["execution_constraints"]
    ).execution_constraints(getattr(work, "operation", None))
    if constraints.get("constraint_conflict"):
        return False
    if provider is None:
        return True
    excluded = {
        str(value or "").strip().casefold()
        for value in (constraints.get("excluded_providers") or [])
        if str(value or "").strip()
    }
    allowed = {
        str(value or "").strip().casefold()
        for value in (constraints.get("research_allowed_providers") or [])
        if str(value or "").strip()
    }
    if provider in excluded:
        return False
    if allowed and provider not in allowed:
        return False
    return True


def _ranked_for_work(work: Work, capability: str, *, exclude_employee_id: int | None = None) -> list[tuple]:
    return [
        row for row in ranked_existing_employees(capability, exclude_employee_id=exclude_employee_id)
        if _candidate_allowed_for_work(row[5], work, capability)
    ]


def ranked_existing_employees_for_work(
    work: Work, capability: str, *, exclude_employee_id: int | None = None
) -> list[tuple]:
    """Public governed roster ranking after Work-specific execution constraints."""
    return _ranked_for_work(
        work,
        str(capability or "").strip().upper(),
        exclude_employee_id=exclude_employee_id,
    )


def staffing_selection_evidence(
    capability: str,
    selected: Employee,
    *,
    exclude_employee_id: int | None = None,
    preserved_owner: bool = False,
    market_contract: dict | None = None,
) -> dict:
    evolution = __import__(
        "eason_one.services.employee_evolution", fromlist=["selection_reason"]
    )
    ranked = ranked_existing_employees(capability, exclude_employee_id=exclude_employee_id)
    profiles = [dict(row[6]) for row in ranked[:6]]
    selected_profile = next((row for row in profiles if row["employee_id"] == selected.id), None)
    if selected_profile is None:
        direct = 1 if capability in _ROLE_CAPABILITIES.get(selected.slug, set()) else 0
        level = int(getattr(getattr(selected, "position", None), "level", 1) or 1)
        specialist = 1 if level < 3 and not selected.slug.endswith("-director") else 0
        selected_profile = __import__(
            "eason_one.services.employee_evolution", fromlist=["selection_profile"]
        ).selection_profile(
            selected, capability, direct=direct, specialist=specialist,
            active_workload=_active_workload(selected.id),
        )
        profiles.insert(0, selected_profile)

    influenced = False
    decision_basis = "APPROVED_OWNER_CAPABLE" if preserved_owner else "ONLY_ELIGIBLE_EMPLOYEE"
    selected_row = next((row for row in ranked if row[5].id == selected.id), None) if ranked else None
    # Counterfactual ranking removes only outcome-backed experience while
    # preserving the exact same governed capability/role/workload roster.  This
    # lets Research surfaces prove when learning actually changed behavior
    # instead of merely showing that learning records exist.
    baseline_without_learning = None
    if ranked:
        baseline_without_learning = max(
            ranked, key=lambda row: (row[0], row[1], row[3], row[4])
        )
    if not preserved_owner and ranked:
        if selected_row is not None and selected_row is ranked[0]:
            if len(ranked) >= 2:
                first, second = ranked[0], ranked[1]
                if first[0] != second[0] or first[1] != second[1]:
                    decision_basis = "ROLE_SPECIALIZATION"
                elif first[2] != second[2]:
                    decision_basis = "OUTCOME_BACKED_EXPERIENCE"
                    influenced = True
                elif first[3] != second[3]:
                    decision_basis = "CURRENT_WORKLOAD"
                else:
                    decision_basis = "DETERMINISTIC_TIEBREAK"
            else:
                decision_basis = "ONLY_ELIGIBLE_EMPLOYEE"

    if market_contract and not preserved_owner:
        decision_basis = "EASON_MARKET_V1"
    reason = (
        "Approved owner already satisfies the required capability; no reassignment was needed. "
        + evolution.selection_reason(selected_profile, max(1, len(ranked)))
        if preserved_owner else evolution.selection_reason(selected_profile, max(1, len(ranked)))
    )
    if market_contract and not preserved_owner:
        reason = (
            f"Internal Eason Market contract #{market_contract.get('contract_id')} awarded the Work to "
            f"{selected.name} at {market_contract.get('agreed_ec')} EC after capability/role/outcome-history qualification. "
            + reason
        )
    counterfactual = None
    if baseline_without_learning is not None:
        counterfactual = {
            "policy": "SAME_ELIGIBLE_ROSTER_WITHOUT_OUTCOME_EXPERIENCE",
            "employee_id": baseline_without_learning[5].id,
            "employee_name": baseline_without_learning[5].name,
            "direct_role_match": bool(baseline_without_learning[0]),
            "specialist": bool(baseline_without_learning[1]),
            "active_workload": int(baseline_without_learning[6].get("active_workload") or 0),
        }
    behavior_changed = bool(
        influenced
        and counterfactual
        and int(counterfactual["employee_id"]) != int(selected.id)
    )
    return {
        "schema": "STAFFING_SELECTION_EVIDENCE_V1_1",
        "required_capability": capability,
        "selected_employee_id": selected.id,
        "selected_employee_name": selected.name,
        "selected_experience_score": int((selected_profile or {}).get("experience_score") or 0),
        "decision_basis": decision_basis,
        "reason": reason,
        "experience_influenced_selection": influenced,
        "behavior_changed_vs_no_learning": behavior_changed,
        "counterfactual_without_learning": counterfactual,
        "market_contract": market_contract,
        "candidates": profiles,
    }


def _team_control(work: Work) -> dict:
    return dict((work.runtime_control_json or {}).get("team_formation") or {})


def _write_control(work: Work, payload: dict) -> None:
    control = dict(work.runtime_control_json or {})
    control["team_formation"] = {"version": VERSION, **payload}
    work.runtime_control_json = control


def execution_ready(work: Work) -> bool:
    """True when team formation has either matched or intentionally deferred.

    Unknown/mixed work is explicitly preserved as CEO-approved assignment; it
    is not a staffing blocker. Strongly inferred work must be reconciled first.
    """
    declared = declared_capabilities(work)
    if len(declared) > 1:
        return False
    capability, _ = infer_primary_capability(work)
    if capability is None:
        return True
    state = _team_control(work).get("state")
    return state in {"MATCHED", "REASSIGNED", "MATCH_PRESERVED"}


def validate_existing_employee_for_ready_work(
    work: Work,
    employee_id: int,
    *,
    expected_capability: str,
    assigned_by_employee_id: int,
) -> dict:
    """Return the canonical non-mutating authority check for CEO allocation."""
    if work.work_type == "MANAGEMENT" or str(work.state or "").upper() != "READY":
        return {"status": "STALE", "reason": "WORK_NOT_READY_DELIVERY"}
    project = work.project
    if project is None or str(project.environment or "").upper() != "LIVE":
        return {"status": "STALE", "reason": "PROJECT_NOT_LIVE"}
    if not work_runtime.project_can_activate(project):
        return {"status": "BLOCKED_BY_GOVERNANCE", "reason": "PROJECT_NOT_ACTIVATABLE"}
    if AgentRun.query.filter_by(work_id=work.id).count():
        return {"status": "STALE", "reason": "EXECUTION_ALREADY_BEGUN"}
    governance = __import__("eason_one.services.governance", fromlist=["blocks_work"])
    if governance.blocks_work(work):
        return {"status": "BLOCKED_BY_GOVERNANCE", "reason": "WORK_BLOCKED_BY_GOVERNANCE"}

    ceo = db.session.get(Employee, int(assigned_by_employee_id or 0))
    if ceo is None or not ceo.active or ceo.slug != "ceo":
        return {"status": "UNAUTHORIZED", "reason": "REQUIRES_ACTIVE_CEO"}
    employee = db.session.get(Employee, int(employee_id or 0))
    if employee is None or not employee.active:
        return {"status": "STALE", "reason": "EMPLOYEE_NOT_ACTIVE"}

    declared = declared_capabilities(work)
    expected = str(expected_capability or "").strip().upper()
    if len(declared) != 1 or declared[0] != expected:
        return {"status": "STALE", "reason": "CAPABILITY_BASIS_CHANGED"}
    capability = declared[0]
    if capability not in CANONICAL_DELIVERY_CAPABILITIES:
        return {"status": "STALE", "reason": "UNKNOWN_CAPABILITY"}
    if capability not in employee_capabilities(employee) or not _candidate_allowed_for_work(employee, work, capability):
        return {"status": "STALE", "reason": "EMPLOYEE_NOT_CAPABILITY_ELIGIBLE"}

    active = work_runtime.active_assignment(work)
    if active is not None:
        if int(active.employee_id) == int(employee.id):
            return {
                "status": "ALREADY_APPLIED",
                "reason": "CURRENT_OWNER_ALREADY_MATCHES",
                "employee_id": employee.id,
                "assignment_id": active.id,
                "required_capability": capability,
            }
        return {"status": "STALE", "reason": "WORK_ALREADY_ASSIGNED"}

    operating = __import__("eason_one.services.ceo_operating", fromlist=["employee_capacity_view"])
    ranked = _ranked_for_work(work, capability)
    available = [
        row for row in ranked
        if _capacity_eligible_for_work(row[5], work)
    ]
    if not available:
        return {"status": "STALE", "reason": "NO_AVAILABLE_ELIGIBLE_EMPLOYEE"}
    selected = available[0][5]
    if int(selected.id) != int(employee.id):
        return {"status": "STALE", "reason": "SELECTION_BASIS_CHANGED"}
    return {
        "status": "VALIDATED",
        "reason": "CANONICAL_TEAM_FORMATION_EXISTING_ROSTER_AUTHORITY",
        "employee_id": employee.id,
        "required_capability": capability,
    }


def assign_existing_employee_for_ready_work(
    work: Work,
    employee_id: int,
    *,
    assigned_by_employee_id: int,
    reason: str,
    expected_capability: str,
    intent_id: str,
) -> dict:
    """Apply one bounded CEO portfolio assignment through Team Formation.

    This is the canonical authority seam for Slice 3 existing-roster
    allocation.  It is deliberately narrower than generic reassignment:

    * the Work must already exist and be READY;
    * no execution may have begun;
    * the Project must remain activatable under current Governance;
    * the Work must declare exactly one canonical capability;
    * the requested Employee must be the current best *available* eligible
      roster member for that capability; and
    * no existing owner may be displaced by this initial-allocation path.

    The function never creates Work, Employee, Project authority, TWD budget or
    provider execution.  It only materializes an already-approved Work's
    organizational owner and internal EC contract using existing Company
    services.  The caller owns the transaction commit so assignment, audit and
    CEO receipt can settle atomically.
    """
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("CEO_STAFFING_REASON_REQUIRED")
    if not isinstance(intent_id, str) or not intent_id.strip():
        raise ValueError("CEO_STAFFING_INTENT_ID_REQUIRED")
    validation = validate_existing_employee_for_ready_work(
        work,
        employee_id,
        expected_capability=expected_capability,
        assigned_by_employee_id=assigned_by_employee_id,
    )
    if validation["status"] == "ALREADY_APPLIED":
        return {
            **validation,
            "work_id": work.id,
        }
    if validation["status"] != "VALIDATED":
        raise ValueError(f"CEO_STAFFING_{validation['reason']}")
    project = work.project
    ceo = db.session.get(Employee, int(assigned_by_employee_id))
    employee = db.session.get(Employee, int(employee_id))
    capability = str(validation["required_capability"])
    ranked = _ranked_for_work(work, capability)

    market = __import__(
        "eason_one.services.market",
        fromlist=["award_internal_work", "contract_evidence", "contract_for_work"],
    )
    market.award_internal_work(
        work,
        capability,
        ranked,
        opened_by_employee_id=ceo.id,
        preferred_employee_id=employee.id,
        selection_basis_override="CEO_PORTFOLIO_AVAILABLE_CAPACITY",
    )
    assignment = work_runtime.reassign(
        work,
        employee.id,
        assigned_by_employee_id=ceo.id,
        reason=reason.strip(),
    )
    market_contract = market.contract_evidence(market.contract_for_work(work.id))
    evidence = staffing_selection_evidence(
        capability,
        employee,
        market_contract=market_contract,
    )
    _write_control(work, {
        "state": "REASSIGNED",
        "required_capability": capability,
        "basis": "CEO portfolio allocated available existing capacity to already-approved READY Work.",
        "employee_id": employee.id,
        "selection_evidence": evidence,
        "allocation_source": "CEO_PORTFOLIO",
        "ceo_intent_id": intent_id,
    })
    emit(
        "CEO_PORTFOLIO_WORK_ASSIGNED",
        actor_type="EMPLOYEE",
        actor_id=ceo.id,
        project_id=work.project_id,
        work_id=work.id,
        correlation_id=intent_id,
        payload={
            "intent_id": intent_id,
            "employee_id": employee.id,
            "required_capability": capability,
            "assignment_id": assignment.id,
            "market_contract_id": (market_contract or {}).get("contract_id"),
            "provider_execution_created": False,
            "project_twd_authority_changed": False,
            "new_employee_created": False,
        },
    )
    return {
        "status": "APPLIED",
        "work_id": work.id,
        "employee_id": employee.id,
        "assignment_id": assignment.id,
        "required_capability": capability,
        "market_contract": market_contract,
    }


def validate_existing_employee_reallocation_for_ready_work(
    work: Work,
    *,
    from_employee_id: int,
    to_employee_id: int,
    expected_capability: str,
    assigned_by_employee_id: int,
) -> dict:
    """Validate a bounded CEO reallocation that resolves real overcommitment.

    This seam is intentionally narrower than generic ``work_runtime.reassign``.
    The CEO may move only an already-governed READY delivery Work whose current
    owner is provably OVERCOMMITTED, before any provider Execution has begun.
    The replacement must be the current best AVAILABLE existing-roster Employee
    for the same declared capability.  No Project scope, TWD authority, Work,
    Employee, or provider execution may be created by this decision.
    """
    if work.work_type == "MANAGEMENT" or str(work.state or "").upper() != "READY":
        return {"status": "STALE", "reason": "WORK_NOT_READY_DELIVERY"}
    project = work.project
    if project is None or str(project.environment or "").upper() != "LIVE":
        return {"status": "STALE", "reason": "PROJECT_NOT_LIVE"}
    if not work_runtime.project_can_activate(project):
        return {"status": "BLOCKED_BY_GOVERNANCE", "reason": "PROJECT_NOT_ACTIVATABLE"}
    if AgentRun.query.filter_by(work_id=work.id).count():
        return {"status": "STALE", "reason": "EXECUTION_ALREADY_BEGUN"}
    governance = __import__("eason_one.services.governance", fromlist=["blocks_work"])
    if governance.blocks_work(work):
        return {"status": "BLOCKED_BY_GOVERNANCE", "reason": "WORK_BLOCKED_BY_GOVERNANCE"}

    ceo = db.session.get(Employee, int(assigned_by_employee_id or 0))
    if ceo is None or not ceo.active or ceo.slug != "ceo":
        return {"status": "UNAUTHORIZED", "reason": "REQUIRES_ACTIVE_CEO"}
    previous_employee = db.session.get(Employee, int(from_employee_id or 0))
    replacement = db.session.get(Employee, int(to_employee_id or 0))
    if previous_employee is None or replacement is None or not replacement.active:
        return {"status": "STALE", "reason": "EMPLOYEE_NOT_ACTIVE"}
    if int(previous_employee.id) == int(replacement.id):
        return {"status": "ALREADY_APPLIED", "reason": "CURRENT_OWNER_ALREADY_MATCHES"}

    assignment = work_runtime.active_assignment(work)
    if assignment is None:
        return {"status": "STALE", "reason": "WORK_NO_LONGER_ASSIGNED"}
    if int(assignment.employee_id) == int(replacement.id):
        return {
            "status": "ALREADY_APPLIED",
            "reason": "CURRENT_OWNER_ALREADY_MATCHES",
            "employee_id": replacement.id,
            "assignment_id": assignment.id,
        }
    if int(assignment.employee_id) != int(previous_employee.id):
        return {"status": "STALE", "reason": "CURRENT_OWNER_CHANGED"}

    operating = __import__("eason_one.services.ceo_operating", fromlist=["employee_capacity_view"])
    if str(operating.employee_capacity_view(previous_employee).get("capacity") or "").upper() != "OVERCOMMITTED":
        return {"status": "STALE", "reason": "SOURCE_EMPLOYEE_NOT_OVERCOMMITTED"}
    if str(operating.employee_capacity_view(replacement).get("capacity") or "").upper() != "AVAILABLE":
        return {"status": "STALE", "reason": "REPLACEMENT_EMPLOYEE_NOT_AVAILABLE"}

    declared = declared_capabilities(work)
    expected = str(expected_capability or "").strip().upper()
    if len(declared) != 1 or declared[0] != expected:
        return {"status": "STALE", "reason": "CAPABILITY_BASIS_CHANGED"}
    capability = declared[0]
    if capability not in CANONICAL_DELIVERY_CAPABILITIES:
        return {"status": "STALE", "reason": "UNKNOWN_CAPABILITY"}
    if capability not in employee_capabilities(replacement) or not _candidate_allowed_for_work(replacement, work, capability):
        return {"status": "STALE", "reason": "EMPLOYEE_NOT_CAPABILITY_ELIGIBLE"}

    ranked = _ranked_for_work(work, capability, exclude_employee_id=previous_employee.id)
    available = [
        row for row in ranked
        if str(operating.employee_capacity_view(row[5]).get("capacity") or "").upper() == "AVAILABLE"
    ]
    if not available:
        return {"status": "STALE", "reason": "NO_AVAILABLE_ELIGIBLE_REPLACEMENT"}
    selected = available[0][5]
    if int(selected.id) != int(replacement.id):
        return {"status": "STALE", "reason": "REPLACEMENT_SELECTION_BASIS_CHANGED"}
    return {
        "status": "VALIDATED",
        "reason": "CANONICAL_TEAM_FORMATION_RESOURCE_CONTENTION_REALLOCATION",
        "from_employee_id": previous_employee.id,
        "employee_id": replacement.id,
        "assignment_id": assignment.id,
        "required_capability": capability,
    }


def reassign_existing_employee_for_ready_work(
    work: Work,
    *,
    from_employee_id: int,
    to_employee_id: int,
    assigned_by_employee_id: int,
    reason: str,
    expected_capability: str,
    intent_id: str,
) -> dict:
    """Resolve one proven CEO resource contention through canonical reassignment.

    The prior WorkAssignment and prior internal Market generation remain durable
    history. ``work_runtime.reassign`` ends only the current assignment and the
    Market service supersedes only an unpaid ACTIVE contract.  Because this
    authority seam rejects Work with any AgentRun, it cannot transfer an in-flight
    provider execution or cause paid execution replay.
    """
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("CEO_REALLOCATION_REASON_REQUIRED")
    if not isinstance(intent_id, str) or not intent_id.strip():
        raise ValueError("CEO_REALLOCATION_INTENT_ID_REQUIRED")
    validation = validate_existing_employee_reallocation_for_ready_work(
        work,
        from_employee_id=from_employee_id,
        to_employee_id=to_employee_id,
        expected_capability=expected_capability,
        assigned_by_employee_id=assigned_by_employee_id,
    )
    if validation["status"] == "ALREADY_APPLIED":
        return {**validation, "work_id": work.id}
    if validation["status"] != "VALIDATED":
        raise ValueError(f"CEO_REALLOCATION_{validation['reason']}")

    ceo = db.session.get(Employee, int(assigned_by_employee_id))
    replacement = db.session.get(Employee, int(to_employee_id))
    capability = str(validation["required_capability"])
    assignment = work_runtime.reassign(
        work,
        replacement.id,
        assigned_by_employee_id=ceo.id,
        reason=reason.strip(),
    )
    market = __import__(
        "eason_one.services.market",
        fromlist=["contract_evidence", "contract_for_work"],
    )
    market_contract = market.contract_evidence(market.contract_for_work(work.id))
    evidence = staffing_selection_evidence(
        capability,
        replacement,
        exclude_employee_id=int(from_employee_id),
        market_contract=market_contract,
    )
    _write_control(work, {
        "state": "REASSIGNED",
        "required_capability": capability,
        "basis": "CEO portfolio resolved proven Employee overcommitment without interrupting in-flight execution.",
        "employee_id": replacement.id,
        "selection_evidence": evidence,
        "allocation_source": "CEO_PORTFOLIO_REALLOCATION",
        "ceo_intent_id": intent_id,
        "reallocated_from_employee_id": int(from_employee_id),
    })
    emit(
        "CEO_PORTFOLIO_WORK_REALLOCATED",
        actor_type="EMPLOYEE",
        actor_id=ceo.id,
        project_id=work.project_id,
        work_id=work.id,
        correlation_id=intent_id,
        payload={
            "intent_id": intent_id,
            "from_employee_id": int(from_employee_id),
            "employee_id": replacement.id,
            "required_capability": capability,
            "assignment_id": assignment.id,
            "market_contract_id": (market_contract or {}).get("contract_id"),
            "provider_execution_created": False,
            "provider_execution_transferred": False,
            "project_twd_authority_changed": False,
            "new_employee_created": False,
            "prior_assignment_history_erased": False,
        },
    )
    return {
        "status": "APPLIED",
        "work_id": work.id,
        "from_employee_id": int(from_employee_id),
        "employee_id": replacement.id,
        "assignment_id": assignment.id,
        "required_capability": capability,
        "market_contract": market_contract,
    }


def _existing_open_request(work: Work, capability: str) -> HiringRequest | None:
    control = _team_control(work)
    request_id = control.get("hiring_request_id")
    request = db.session.get(HiringRequest, request_id) if request_id else None
    if request and request.status not in {"REJECTED"}:
        return request
    rows = HiringRequest.query.filter_by(project_id=work.project_id).filter(
        HiringRequest.status.in_(["REQUESTED", "HR_REVIEW", "FOUNDER_REVIEW", "HIRED", "SYSTEM_RECOVERY"])
    ).order_by(HiringRequest.id.desc()).all()
    for row in rows:
        if capability in set(row.capabilities_json or []):
            return row
    return None


def _request_capability_gap(work: Work, capability: str, basis: str | None) -> HiringRequest:
    existing = _existing_open_request(work, capability)
    if existing:
        _write_control(work, {
            "state": "STAFFING_REQUESTED",
            "required_capability": capability,
            "basis": basis,
            "hiring_request_id": existing.id,
        })
        work_runtime.open_wait(
            work, "CAPABILITY_GAP",
            f"Company lacks {capability}; HiringRequest #{existing.id} is the staffing owner.",
            issue_code=f"HIRING_REQUEST_{existing.id}",
        )
        return existing

    ceo = Employee.query.filter_by(slug="ceo", active=True).first()
    if not ceo:
        raise ValueError("TEAM_FORMATION_REQUIRES_ACTIVE_CEO")
    workforce = __import__("eason_one.services.workforce", fromlist=["request_hire"])
    request = workforce.request_hire(
        requested_by_type="EMPLOYEE",
        requester=ceo,
        role_needed=_ROLE_FOR_CAPABILITY.get(capability, capability.replace("_", " ").title()),
        problem=f"Project Work #{work.id} requires {capability}, but no active Persistent Employee currently covers that capability.",
        why_now=f"Work #{work.id} cannot truthfully execute with the current roster. {basis or ''}".strip(),
        responsibilities=[work.purpose, work.expected_output or "Deliver the governed Work output."],
        capabilities=[capability],
        urgency="HIGH" if work.priority in {"HIGH", "CRITICAL"} else "MEDIUM",
        use_frequency="PROJECT_NEED",
        operation=work.operation,
        project=work.project,
        source_work=work,
    )
    _write_control(work, {
        "state": "STAFFING_REQUESTED",
        "required_capability": capability,
        "basis": basis,
        "hiring_request_id": request.id,
    })
    emit(
        "WORK_CAPABILITY_GAP_IDENTIFIED", actor_type="EMPLOYEE", actor_id=ceo.id,
        project_id=work.project_id, work_id=work.id, correlation_id=f"work:{work.id}",
        payload={"required_capability": capability, "hiring_request_id": request.id, "basis": basis},
    )
    return request


def reconcile_work(work: Work) -> dict | None:
    if work.work_type == "MANAGEMENT" or work.state not in {"READY", "WAITING"}:
        return None
    control = _team_control(work)
    request = db.session.get(HiringRequest, control.get("hiring_request_id")) if control.get("hiring_request_id") else None
    if control.get("state") == "STAFFING_REPLAN_REQUIRED":
        recommendation = str((getattr(request, "hr_assessment_json", None) or {}).get("recommendation") or "")
        if request is None or request.status != "ASSESSMENT_COMPLETE" or recommendation != "USE EXISTING STAFF":
            return None
        capability, basis = infer_primary_capability(work)
        if not capability:
            return None
        workforce = __import__(
            "eason_one.services.workforce", fromlist=["reconcile_invalid_use_existing_staff"]
        )
        repair = workforce.reconcile_invalid_use_existing_staff(request, capability)
        employee = (repair or {}).get("employee")
        if employee is None:
            return None
        ceo = Employee.query.filter_by(slug="ceo", active=True).first()
        work_runtime.reassign(
            work, employee.id, assigned_by_employee_id=getattr(ceo, "id", None),
            reason=f"Deterministic staffing reconciliation assigned {capability} to {employee.name}.",
        )
        work_runtime.resolve_waits(
            work, "CAPABILITY_GAP",
            note=f"Persistent Employee {employee.name} now satisfies {capability}.",
        )
        work_runtime.resolve_waits(
            work, "RECONCILIATION",
            issue_code=f"HIRING_REQUEST_{request.id}_NOT_MATERIALIZED",
            note=(
                "HR recommended USE EXISTING STAFF, but roster truth had no matching capability; "
                "Company deterministically materialized or selected a valid Persistent Employee."
            ),
        )
        _write_control(work, {
            "state": "REASSIGNED", "required_capability": capability,
            "basis": basis, "employee_id": employee.id,
            "hiring_request_id": request.id,
            "staffing_outcome": (repair or {}).get("status"),
        })
        emit(
            "WORK_STAFFING_RECONCILED", actor_type="RUNTIME",
            project_id=work.project_id, work_id=work.id, correlation_id=f"work:{work.id}",
            payload={
                "hiring_request_id": request.id, "required_capability": capability,
                "employee_id": employee.id, "repair_status": (repair or {}).get("status"),
            },
        )
        return {
            "status": "WORK_REASSIGNED_AFTER_STAFFING_RECONCILIATION",
            "work_id": work.id, "employee_id": employee.id, "capability": capability,
        }
    if control.get("state") == "STAFFING_REQUESTED" and request is not None:
        if request.status in {"REQUESTED", "HR_REVIEW", "FOUNDER_REVIEW", "SYSTEM_RECOVERY"}:
            # HR / Founder / recovery owns the next action. Returning another
            # Team Formation action here would starve the scheduler and prevent
            # the existing HiringRequest from ever advancing.
            return None
        if request.status in {"ASSESSMENT_COMPLETE", "REJECTED"}:
            recommendation = str((request.hr_assessment_json or {}).get("recommendation") or request.status)
            wait_type = "AUTHORITY_EXHAUSTED" if request.status == "REJECTED" else "RECONCILIATION"
            issue = f"HIRING_REQUEST_{request.id}_NOT_MATERIALIZED"
            work_runtime.open_wait(
                work, wait_type,
                f"Staffing request #{request.id} did not materialize the required capability ({recommendation}); Company management must replan this Work.",
                issue_code=issue,
            )
            _write_control(work, {
                **control, "state": "STAFFING_REPLAN_REQUIRED",
                "hiring_request_id": request.id, "staffing_outcome": recommendation,
            })
            emit(
                "WORK_STAFFING_REPLAN_REQUIRED", actor_type="RUNTIME",
                project_id=work.project_id, work_id=work.id, correlation_id=f"work:{work.id}",
                payload={"hiring_request_id": request.id, "outcome": recommendation, "issue_code": issue},
            )
            return {"status": "STAFFING_REPLAN_REQUIRED", "work_id": work.id, "hiring_request_id": request.id}
    if AgentRun.query.filter_by(work_id=work.id).count():
        # Never rewrite responsibility after execution has begun. Management can
        # create a later Work/reassignment explicitly from actual evidence.
        return None

    declared = declared_capabilities(work)
    if len(declared) == 1 and declared[0] not in CANONICAL_DELIVERY_CAPABILITIES:
        issue = f"WORK_{work.id}_UNKNOWN_CAPABILITY_REPLAN"
        work_runtime.open_wait(
            work, "RECONCILIATION",
            f"Unknown accountable capability {declared[0]!r}; Company management must replan this Work using the canonical delivery capability vocabulary before execution.",
            issue_code=issue,
        )
        _write_control(work, {
            "state": "STAFFING_REPLAN_REQUIRED",
            "required_capabilities": declared,
            "basis": "unknown capability is outside the canonical delivery vocabulary",
            "issue_code": issue,
        })
        emit(
            "WORK_STAFFING_REPLAN_REQUIRED", actor_type="RUNTIME",
            project_id=work.project_id, work_id=work.id, correlation_id=f"work:{work.id}",
            payload={"required_capabilities": declared, "issue_code": issue},
        )
        return {"status": "STAFFING_REPLAN_REQUIRED", "work_id": work.id, "required_capabilities": declared}
    if len(declared) > 1:
        issue = f"WORK_{work.id}_MULTI_CAPABILITY_REPLAN"
        work_runtime.open_wait(
            work, "RECONCILIATION",
            "One Work cannot truthfully have multiple accountable specialties. Company management must split this approved legacy Work into single-capability Works before execution.",
            issue_code=issue,
        )
        _write_control(work, {
            "state": "STAFFING_REPLAN_REQUIRED",
            "required_capabilities": declared,
            "basis": "legacy multi-capability Work violates one-accountable-capability ownership",
            "issue_code": issue,
        })
        emit(
            "WORK_STAFFING_REPLAN_REQUIRED", actor_type="RUNTIME",
            project_id=work.project_id, work_id=work.id, correlation_id=f"work:{work.id}",
            payload={"required_capabilities": declared, "issue_code": issue},
        )
        return {"status": "STAFFING_REPLAN_REQUIRED", "work_id": work.id, "required_capabilities": declared}

    capability, basis = infer_primary_capability(work)
    assignment = work_runtime.active_assignment(work)
    owner = assignment.employee if assignment else None
    if (
        control.get("state") in {"MATCHED", "REASSIGNED", "MATCH_PRESERVED"}
        and control.get("employee_id") == getattr(owner, "id", None)
        and control.get("required_capability") == capability
    ):
        return None
    if capability is None:
        _write_control(work, {
            "state": "MATCH_PRESERVED",
            "required_capability": None,
            "basis": "No single strong capability signal; preserve approved assignee.",
            "employee_id": getattr(owner, "id", None),
        })
        return {"status": "TEAM_MATCH_PRESERVED", "work_id": work.id}

    # Research Department V1: a Work intentionally assigned to the Department
    # Head is a manager-delegation request unless the Head owns the synthesis
    # itself.  This keeps one persistent accountable Employee per provider
    # family without turning models into transient pseudo-employees.
    if (
        owner and owner.slug == "research-director" and capability == "RESEARCH"
        and not __import__(
            "eason_one.services.research_department", fromlist=["is_synthesis_work"]
        ).is_synthesis_work(work)
    ):
        department = __import__(
            "eason_one.services.research_department",
            fromlist=["select_department_specialist", "specialist_employees"],
        )
        candidate, delegation = department.select_department_specialist(work)
        if (
            candidate is not None
            and candidate.id != owner.id
            and _capacity_eligible_for_work(candidate, work)
        ):
            specialist_ids = {
                employee.id for employee in department.specialist_employees(active_only=True)
            }
            ranked = [
                row for row in _ranked_for_work(work, capability)
                if row[5].id in specialist_ids and _capacity_eligible_for_work(row[5], work)
            ]
            ceo = Employee.query.filter_by(slug="ceo", active=True).first()
            market_award = __import__(
                "eason_one.services.market", fromlist=["award_internal_work"]
            ).award_internal_work(
                work, capability, ranked,
                opened_by_employee_id=getattr(owner, "id", None),
                preferred_employee_id=candidate.id,
            ) if ranked else None
            work_runtime.reassign(
                work, candidate.id, assigned_by_employee_id=owner.id,
                reason=(
                    f"Research Director delegated approved Research Work to {candidate.name} "
                    "under the persistent Research Department policy."
                ),
            )
            work_runtime.resolve_waits(
                work, "CAPABILITY_GAP",
                note=f"Research Department specialist {candidate.name} satisfies RESEARCH.",
            )
            market_contract = (
                __import__(
                    "eason_one.services.market", fromlist=["contract_evidence"]
                ).contract_evidence(market_award.get("contract"))
                if market_award else None
            )
            evidence = {
                "schema": "RESEARCH_DEPARTMENT_DELEGATION_V1",
                "required_capability": capability,
                "manager_employee_id": owner.id,
                "manager_employee_name": owner.name,
                "selected_employee_id": candidate.id,
                "selected_employee_name": candidate.name,
                **dict(delegation or {}),
                "market_contract": market_contract,
            }
            _write_control(work, {
                "state": "REASSIGNED", "required_capability": capability,
                "basis": basis, "employee_id": candidate.id,
                "department_manager_id": owner.id,
                "selection_evidence": evidence,
            })
            emit(
                "RESEARCH_DEPARTMENT_WORK_DELEGATED",
                actor_type="EMPLOYEE", actor_id=owner.id,
                project_id=work.project_id, work_id=work.id, correlation_id=f"work:{work.id}",
                payload={
                    "required_capability": capability,
                    "manager_employee_id": owner.id,
                    "employee_id": candidate.id,
                    "provider_family": delegation.get("selected_provider"),
                    "market_contract_id": getattr((market_award or {}).get("contract"), "id", None),
                },
            )
            return {
                "status": "RESEARCH_DEPARTMENT_DELEGATED",
                "work_id": work.id, "employee_id": candidate.id,
                "manager_employee_id": owner.id, "capability": capability,
            }

    if (
        owner and owner.slug != "ceo"
        and capability in employee_capabilities(owner)
        and _capacity_eligible_for_work(owner, work)
    ):
        ranked = _ranked_for_work(work, capability)
        ceo = Employee.query.filter_by(slug="ceo", active=True).first()
        market_award = __import__(
            "eason_one.services.market", fromlist=["award_internal_work"]
        ).award_internal_work(
            work, capability, ranked,
            opened_by_employee_id=getattr(ceo, "id", None),
            preferred_employee_id=owner.id,
        ) if ranked else None
        market_contract = (
            __import__("eason_one.services.market", fromlist=["contract_evidence"]).contract_evidence(
                market_award.get("contract")
            ) if market_award else None
        )
        evidence = staffing_selection_evidence(
            capability, owner, preserved_owner=True, market_contract=market_contract
        )
        _write_control(work, {
            "state": "MATCHED", "required_capability": capability,
            "basis": basis, "employee_id": owner.id,
            "selection_evidence": evidence,
        })
        return {"status": "TEAM_MATCHED", "work_id": work.id, "employee_id": owner.id, "capability": capability}

    ranked = [
        row for row in _ranked_for_work(work, capability, exclude_employee_id=getattr(owner, "id", None))
        if _capacity_eligible_for_work(row[5], work)
    ]
    candidate = ranked[0][5] if ranked else None
    market_award = None
    if candidate:
        ceo = Employee.query.filter_by(slug="ceo", active=True).first()
        market_award = __import__(
            "eason_one.services.market", fromlist=["award_internal_work"]
        ).award_internal_work(
            work, capability, ranked, opened_by_employee_id=getattr(ceo, "id", None)
        )
        if market_award and market_award.get("employee") is not None:
            candidate = market_award["employee"]
        work_runtime.reassign(
            work, candidate.id,
            assigned_by_employee_id=getattr(ceo, "id", None),
            reason=f"Company team formation matched Work to existing {capability} capability before execution.",
        )
        work_runtime.resolve_waits(
            work, "CAPABILITY_GAP",
            note=f"Existing Persistent Employee {candidate.name} satisfies {capability}.",
        )
        _write_control(work, {
            "state": "REASSIGNED", "required_capability": capability,
            "basis": basis, "employee_id": candidate.id,
            "selection_evidence": staffing_selection_evidence(
                capability, candidate, exclude_employee_id=getattr(owner, "id", None),
                market_contract=(
                    __import__("eason_one.services.market", fromlist=["contract_evidence"]).contract_evidence(
                        market_award.get("contract")
                    ) if market_award else None
                ),
            ),
        })
        emit(
            "WORK_TEAM_FORMED", actor_type="EMPLOYEE", actor_id=getattr(ceo, "id", None),
            project_id=work.project_id, work_id=work.id, correlation_id=f"work:{work.id}",
            payload={
                "required_capability": capability,
                "employee_id": candidate.id,
                "previous_employee_id": getattr(owner, "id", None),
                "market_contract_id": getattr((market_award or {}).get("contract"), "id", None),
            },
        )
        return {"status": "WORK_REASSIGNED_FOR_CAPABILITY", "work_id": work.id, "employee_id": candidate.id, "capability": capability}

    request = _request_capability_gap(work, capability, basis)
    return {"status": "CAPABILITY_GAP", "work_id": work.id, "hiring_request_id": request.id, "capability": capability}


def reconcile_pending_team() -> dict | None:
    """Reconcile one not-yet-executed Work so Company Kernel stays bounded."""
    rows = Work.query.filter(
        Work.work_type != "MANAGEMENT",
        Work.state.in_(["READY", "WAITING"]),
    ).order_by(Work.id).all()
    for work in rows:
        if not work.project or work.project.environment != "LIVE":
            continue
        if work.project.status not in {"ACTIVE", "PLANNING", "BLOCKED"}:
            # Team formation changes durable assignment/market truth.  It is
            # therefore execution, not a harmless read-side reconciliation,
            # and must remain completely inert for PAUSED/REVIEW/terminal
            # Projects until ordinary Project sequencing is lawful again.
            continue
        if __import__(
            "eason_one.services.work_runtime", fromlist=["project_hard_blockers"]
        ).project_hard_blockers(work.project):
            continue
        if not work.operation or work.operation.approved_at is None:
            continue
        if not __import__("eason_one.services.company_kernel", fromlist=["is_kernel_operation"]).is_kernel_operation(work.operation):
            continue
        result = reconcile_work(work)
        if result:
            db.session.commit()
            return result
    return None
