"""Deterministic CEO portfolio and CompanyPlan projections.

Slice 3 begins at the company-management boundary, but this module remains
*judgement first*.  It may persist a CEO plan projection for restart/audit, but
it never calls a provider, spends Project budget, creates Work, hires an
Employee, changes Project lifecycle, or bypasses the existing Company Kernel.

Effectful changes are represented as typed intents. Existing-roster staffing
still goes only through Team Formation. Project-level QUIESCE/ESCALATE/STOP and
REPLAN/REQUEST_VERIFICATION/READY_FOR_OUTCOME_CHECK decisions commit durable
receipts; these Kernel-facing actions only authorize or request the existing
Company Kernel path on the next tick. The CEO layer never calls a provider,
creates Work, declares Result Ready, or completes a Project directly.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
from typing import Any

from ..extensions import db
from ..models import CompanyEvent, Decision, Employee, Escalation, Project, Work, WorkAssignment, now
from . import ceo_learning, ceo_operating
from .company_events import emit


SCHEMA = "CEO_COMPANY_PLAN_V1"
PLAN_EVENT_TYPE = "CEO_COMPANY_PLAN_PROJECTION_REVISED"
ACTION_RECEIPT_EVENT_TYPE = "CEO_ACTION_RECEIPT"
MANAGEMENT_ACTION_TYPES = {
    "DOCUMENT_QUIESCENCE",
    "SURFACE_FOUNDER_ESCALATION",
    "RECOMMEND_PROJECT_STOP",
    "AUTHORIZE_PROJECT_REPLAN",
    "REQUEST_PROJECT_VERIFICATION",
    "REQUEST_PROJECT_OUTCOME_CHECK",
}

PORTFOLIO_STATES = {
    "READY",
    "IN_PROGRESS",
    "EXPECTED_WAIT",
    "QUIESCENT",
    "NEEDS_FOUNDER",
    "VERIFYING",
    "RESULT_READY",
    "PAUSED",
    "TERMINAL",
}

# Strategic priority is Founder truth.  It is only a deterministic tie-breaker
# *within* an operating attention class and is never overwritten by this layer.
_PRIORITY_RANK = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}

# Founder attention and operating resource focus are intentionally separate.
# A Result Ready / Needs Founder Project should be visible first without being
# mistaken for a place where the CEO should manufacture more delivery Work.
_ATTENTION_RANK = {
    "NEEDS_FOUNDER": 0,
    "RESULT_READY": 1,
    "VERIFYING": 2,
    "IN_PROGRESS": 3,
    "READY": 4,
    "EXPECTED_WAIT": 5,
    "QUIESCENT": 6,
    "PAUSED": 7,
    "TERMINAL": 8,
}
_EXECUTABLE_STATES = {"READY", "IN_PROGRESS", "VERIFYING"}


def _jsonable(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    return value


def _stable_hash(value: Any) -> str:
    raw = json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _project_sort_key(row: dict) -> tuple:
    state = str(row.get("operating_state") or "").upper()
    priority = str(row.get("founder_priority") or "MEDIUM").upper()
    return (
        _ATTENTION_RANK.get(state, 99),
        _PRIORITY_RANK.get(priority, 9),
        int(row.get("project_id") or 0),
    )


def _operating_focus_key(row: dict) -> tuple:
    # Do not interrupt in-progress/verification work merely because another
    # Project became READY.  Founder priority still breaks ties inside the same
    # operating phase.
    state_rank = {"VERIFYING": 0, "IN_PROGRESS": 1, "READY": 2}
    priority = str(row.get("founder_priority") or "MEDIUM").upper()
    return (
        state_rank.get(str(row.get("operating_state") or "").upper(), 9),
        _PRIORITY_RANK.get(priority, 9),
        int(row.get("project_id") or 0),
    )


def _operating_reason(row: dict) -> str:
    state = str(row.get("operating_state") or "").upper()
    primary = dict((row.get("blockers") or {}).get("primary") or {})
    if state == "NEEDS_FOUNDER":
        return primary.get("reason") or "A material next step requires Founder authority."
    if state == "RESULT_READY":
        return "Verified Project outcome is ready; ordinary delivery resources must not restart."
    if state == "QUIESCENT":
        return primary.get("reason") or "No current meaningful executable path is proven."
    if state == "EXPECTED_WAIT":
        return primary.get("reason") or "A declared dependency/recovery path owns the current wait."
    if state == "VERIFYING":
        return "Current delivery is under verification."
    if state == "IN_PROGRESS":
        return "Current delivery has a lawful active responsibility."
    if state == "READY":
        return "A lawful next delivery step is ready."
    if state == "PAUSED":
        return "Founder/Project lifecycle pause fences new execution."
    if state == "TERMINAL":
        return "Project lifecycle is terminal."
    return "Current operating meaning is not classified."


def _available_employee_ids(snapshot: dict) -> set[int]:
    return {
        int(row["employee_id"])
        for row in (snapshot.get("employees") or [])
        if row.get("employee_id") is not None and row.get("capacity") == "AVAILABLE"
    }


def _available_candidate_for_work(work: Work, available_ids: set[int]) -> tuple[Employee | None, str | None]:
    """Return a safe existing-roster candidate without mutating staffing truth.

    Automatic CEO allocation is deliberately stricter than legacy inference: an
    effect proposal requires one *declared* governed capability.  Weak text
    inference may inform human/CEO reasoning but cannot authorize reassignment.
    """
    team = __import__(
        "eason_one.services.team_formation",
        fromlist=["declared_capabilities", "ranked_existing_employees_for_work"],
    )
    declared = list(team.declared_capabilities(work))
    if len(declared) != 1:
        return None, None
    capability = declared[0]
    for row in team.ranked_existing_employees_for_work(work, capability):
        employee = row[5]
        if int(employee.id) in available_ids:
            return employee, capability
    return None, capability


def _unassigned_ready_work(project_ids: set[int]) -> list[Work]:
    if not project_ids:
        return []
    active_assignment_work_ids = {
        int(value)
        for (value,) in db.session.query(WorkAssignment.work_id)
        .filter(WorkAssignment.ended_at.is_(None)).all()
    }
    return [
        work for work in Work.query.filter(
            Work.project_id.in_(project_ids),
            Work.work_type != "MANAGEMENT",
            Work.state == "READY",
        ).order_by(Work.id).all()
        if int(work.id) not in active_assignment_work_ids
    ]


def _allocation_proposals(snapshot: dict, project_views: dict[int, dict]) -> list[dict]:
    """Propose only provably safe existing-roster allocation opportunities.

    These are *not* applied here.  A proposal proves merely that a governed Work
    already exists, has one declared capability, and an active Persistent
    Employee with that capability is currently available in the CEO projection.
    """
    available_ids = _available_employee_ids(snapshot)
    focus_ids = {
        project_id for project_id, row in project_views.items()
        if str(row.get("operating_state") or "").upper() in _EXECUTABLE_STATES
    }
    proposals: list[dict] = []
    for work in _unassigned_ready_work(focus_ids):
        employee, capability = _available_candidate_for_work(work, available_ids)
        if employee is None:
            continue
        proposals.append({
            "decision_key": f"assign:work:{work.id}:employee:{employee.id}",
            "kind": "ASSIGN_EXISTING_EMPLOYEE",
            "project_id": work.project_id,
            "work_id": work.id,
            "employee_id": employee.id,
            "employee_name": employee.name,
            "required_capability": capability,
            "reason": (
                f"Ready Work #{work.id} has no active owner; existing Employee {employee.name} "
                f"is available and already satisfies declared capability {capability}."
            ),
            "effect_authorized": False,
            "authority_note": (
                "PortfolioReview is judgement only. Apply through an explicit canonical staffing action; "
                "do not mutate WorkAssignment from this projection."
            ),
        })
    return proposals


def _staffing_findings(snapshot: dict, project_views: dict[int, dict]) -> list[dict]:
    available_ids = _available_employee_ids(snapshot)
    findings: list[dict] = []
    seen_work_ids: set[int] = set()
    for project_id, row in project_views.items():
        for blocker in (row.get("blockers") or {}).get("all") or []:
            if blocker.get("type") != "CAPABILITY" or not blocker.get("work_id"):
                continue
            work_id = int(blocker["work_id"])
            if work_id in seen_work_ids:
                continue
            seen_work_ids.add(work_id)
            work = db.session.get(Work, work_id)
            if work is None:
                continue
            employee, capability = _available_candidate_for_work(work, available_ids)
            findings.append({
                "project_id": project_id,
                "work_id": work_id,
                "required_capability": capability,
                "recommendation": "USE_EXISTING_EMPLOYEE" if employee else "GOVERNED_STAFFING_REQUIRED",
                "employee_id": getattr(employee, "id", None),
                "employee_name": getattr(employee, "name", None),
                "reason": (
                    f"Available existing Employee {employee.name} satisfies {capability}."
                    if employee and capability
                    else "No currently available existing Employee is proven to satisfy the declared capability."
                ),
                # This finding never authorizes a HiringRequest. New persistent
                # staff remain owned by the existing governed Team Formation/HR path.
                "creates_employee": False,
            })
    return findings


def _resource_contention_findings(snapshot: dict) -> list[dict]:
    """Surface real Employee overcommitment without inventing a reassignment.

    A previously quiescent responsibility can become executable again after the
    Employee has legitimately been allocated elsewhere.  That is a portfolio
    conflict, not permission to silently end either durable WorkAssignment.
    The CEO therefore records a management finding and fails closed until a
    later governed reallocation path can choose which responsibility moves.
    """
    findings: list[dict] = []
    for row in snapshot.get("employees") or []:
        if str(row.get("capacity") or "").upper() != "OVERCOMMITTED":
            continue
        responsibilities = list(row.get("active_responsibilities") or [])
        project_ids = sorted({
            int(item["project_id"])
            for item in responsibilities
            if item.get("project_id") is not None
        })
        findings.append({
            "kind": "RESOURCE_CONTENTION",
            "employee_id": row.get("employee_id"),
            "employee_name": row.get("name"),
            "project_ids": project_ids,
            "active_responsibilities": responsibilities,
            "recommendation": "PORTFOLIO_REALLOCATION_REQUIRED",
            "reason": (
                f"Employee {row.get('name') or row.get('employee_id')} has "
                f"{len(responsibilities)} simultaneously active responsibilities. "
                "No automatic displacement is authorized from a read projection."
            ),
            "effect_authorized": False,
        })
    return findings


def _contention_reallocation_proposals(snapshot: dict, project_views: dict[int, dict]) -> list[dict]:
    """Propose one bounded owner move per provably overcommitted Employee.

    The proposal never ends an assignment itself.  It only identifies a READY,
    not-yet-executed responsibility that can move to the current best AVAILABLE
    existing Employee with the same declared capability.  EXECUTING/VERIFYING
    responsibilities are preserved so portfolio management cannot interrupt or
    transfer an in-flight provider effect merely to improve utilization.
    """
    available_ids = _available_employee_ids(snapshot)
    proposals: list[dict] = []
    team = __import__(
        "eason_one.services.team_formation",
        fromlist=["declared_capabilities", "ranked_existing_employees"],
    )
    runtime = __import__("eason_one.services.work_runtime", fromlist=["active_assignment"])

    for employee_row in snapshot.get("employees") or []:
        if str(employee_row.get("capacity") or "").upper() != "OVERCOMMITTED":
            continue
        source_employee_id = int(employee_row.get("employee_id") or 0)
        if not source_employee_id:
            continue
        movable: list[tuple] = []
        for responsibility in employee_row.get("active_responsibilities") or []:
            work_id = int(responsibility.get("work_id") or 0)
            work = db.session.get(Work, work_id) if work_id else None
            if work is None or work.work_type == "MANAGEMENT" or str(work.state or "").upper() != "READY":
                continue
            assignment = runtime.active_assignment(work)
            if assignment is None or int(assignment.employee_id) != source_employee_id:
                continue
            project_view = project_views.get(int(work.project_id)) or {}
            if str(project_view.get("operating_state") or "").upper() not in _EXECUTABLE_STATES:
                continue
            declared = list(team.declared_capabilities(work))
            if len(declared) != 1:
                continue
            capability = declared[0]
            replacement, _ = _available_candidate_for_work(work, available_ids)
            if replacement is None or int(replacement.id) == source_employee_id:
                continue
            founder_priority = str(project_view.get("founder_priority") or "MEDIUM").upper()
            started_at = str(responsibility.get("started_at") or "")
            # Larger priority rank means strategically lower priority.  Moving a
            # later READY assignment preserves older context when priorities tie.
            move_key = (
                _PRIORITY_RANK.get(founder_priority, 9),
                started_at,
                int(work.id),
            )
            movable.append((move_key, work, replacement, capability, assignment, project_view))

        if not movable:
            continue
        _, work, replacement, capability, assignment, project_view = max(movable, key=lambda row: row[0])
        proposals.append({
            "decision_key": (
                f"reallocate:work:{work.id}:from:{source_employee_id}:to:{replacement.id}"
            ),
            "kind": "REALLOCATE_EXISTING_EMPLOYEE",
            "project_id": int(work.project_id),
            "work_id": int(work.id),
            "from_employee_id": source_employee_id,
            "employee_id": int(replacement.id),
            "employee_name": replacement.name,
            "required_capability": capability,
            "prior_assignment_id": int(assignment.id),
            "founder_priority": project_view.get("founder_priority"),
            "reason": (
                f"Employee {employee_row.get('name') or source_employee_id} is overcommitted. "
                f"READY Work #{work.id} can move to available Employee {replacement.name} "
                f"with the same governed capability {capability}, preserving in-progress responsibilities."
            ),
            "effect_authorized": False,
            "authority_note": (
                "Apply only through the canonical CEO resource-contention reallocation seam. "
                "No in-flight execution, Project authority, Work scope, or prior assignment history may be changed."
            ),
        })
    return proposals


def portfolio_review(snapshot: dict | None = None) -> dict:
    """Create a deterministic company-level management judgement.

    Strategic priority remains Founder truth.  This review only distinguishes
    what needs attention from what can actually consume operating resources now.
    """
    snapshot = snapshot or ceo_operating.company_operating_snapshot()
    all_project_rows = [dict(row) for row in (snapshot.get("projects") or [])]
    for row in all_project_rows:
        state = str(row.get("operating_state") or "").upper()
        if state not in PORTFOLIO_STATES:
            raise ValueError(f"UNMAPPED_PORTFOLIO_STATE:{state or 'EMPTY'}")
    historical_terminal_project_ids = sorted(
        int(row["project_id"])
        for row in all_project_rows
        if str(row.get("operating_state") or "").upper() == "TERMINAL"
    )
    # PortfolioReview is current operating judgement. An audit caller may pass
    # include_terminal=True, but terminal history must still never compete for
    # priority, Founder attention, staffing, risk, or resource allocation.
    project_rows = [
        row for row in all_project_rows
        if str(row.get("operating_state") or "").upper() != "TERMINAL"
    ]
    project_rows.sort(key=_project_sort_key)
    by_id = {int(row["project_id"]): row for row in project_rows}
    current_project_ids = set(by_id)

    order = [{
        "project_id": row["project_id"],
        "name": row.get("name"),
        "founder_priority": row.get("founder_priority"),
        "operating_state": row.get("operating_state"),
        "resource_executable": str(row.get("operating_state") or "").upper() in _EXECUTABLE_STATES,
        "reason": _operating_reason(row),
    } for row in project_rows]

    operating_focus = [
        row for row in project_rows
        if str(row.get("operating_state") or "").upper() in _EXECUTABLE_STATES
    ]
    operating_focus.sort(key=_operating_focus_key)
    operating_focus_rows = [{
        "project_id": row["project_id"],
        "name": row.get("name"),
        "founder_priority": row.get("founder_priority"),
        "operating_state": row.get("operating_state"),
        "next_milestone": row.get("next_milestone"),
    } for row in operating_focus]

    quiescence = []
    for row in project_rows:
        if str(row.get("operating_state") or "").upper() != "QUIESCENT":
            continue
        primary = dict((row.get("blockers") or {}).get("primary") or {})
        resume_conditions = ["material Project truth changes and ProjectReview finds a legal next objective"]
        if primary.get("type") == "EVIDENCE" and primary.get("actionable_by") == "NONE":
            resume_conditions = [
                "new persisted evidence changes the exact missing-evidence basis",
                "Founder changes the relevant Project constraint or Contract",
            ]
        quiescence.append({
            "project_id": row["project_id"],
            "reason": primary.get("reason") or _operating_reason(row),
            "blocker_type": primary.get("type"),
            "basis": primary.get("basis"),
            "resume_conditions": resume_conditions,
        })

    advisory_learning = ceo_learning.active_policy_advisory()
    advisory_ids = list(advisory_learning.get("validated_learning_ids") or [])
    allocation_decisions = _allocation_proposals(snapshot, by_id)
    reallocation_decisions = _contention_reallocation_proposals(snapshot, by_id)
    for proposal in allocation_decisions + reallocation_decisions:
        proposal["advisory_learning_ids"] = list(advisory_ids)
    resource_contentions = _resource_contention_findings(snapshot)
    risks = []
    for row in project_rows:
        for blocker in (row.get("blockers") or {}).get("all") or []:
            kind = blocker.get("type")
            if kind == "EVIDENCE":
                risk = "EVIDENCE"
            elif kind == "EXECUTION":
                risk = "EXECUTION"
            elif kind in {"RESOURCE", "AUTHORITY"} and any(
                token in str(blocker.get("reason") or "").casefold()
                for token in ("budget", "cost", "spend", "fund")
            ):
                risk = "COST"
            else:
                risk = "DELIVERY"
            risks.append({
                "project_id": row["project_id"],
                "risk": risk,
                "blocker_type": kind,
                "reason": blocker.get("reason"),
                "basis": blocker.get("basis"),
            })
    for finding in resource_contentions:
        risks.append({
            "project_id": None,
            "employee_id": finding.get("employee_id"),
            "project_ids": list(finding.get("project_ids") or []),
            "risk": "DELIVERY",
            "blocker_type": "RESOURCE_CONTENTION",
            "reason": finding.get("reason"),
            "basis": {
                "active_responsibility_work_ids": [
                    item.get("work_id") for item in finding.get("active_responsibilities") or []
                ],
                "auto_reassignment_authorized": False,
            },
        })

    return {
        "review_type": "PORTFOLIO_REVIEW",
        "basis_revision": snapshot.get("revision"),
        "project_operating_order": order,
        "operating_focus": operating_focus_rows,
        "allocation_decisions": allocation_decisions,
        "reallocation_decisions": reallocation_decisions,
        "staffing_decisions": _staffing_findings(snapshot, by_id) + resource_contentions,
        "resource_contentions": resource_contentions,
        "founder_escalations": [
            row for row in (snapshot.get("founder_decisions") or [])
            if int(row.get("project_id") or 0) in current_project_ids
        ],
        "quiescence_decisions": quiescence,
        "historical_terminal_project_ids": historical_terminal_project_ids,
        "terminal_history_has_operating_effect": False,
        "advisory_learning": advisory_learning,
        "risks": risks,
        "expected_company_outcome": (
            "Use available capacity only on Projects with a proven executable next objective; "
            "preserve strategic priority for blocked/quiescent Projects without manufacturing busywork."
        ),
        "confidence": "HIGH",
    }


def company_plan(snapshot: dict | None = None, review: dict | None = None) -> dict:
    """Build a stable CEO CompanyPlan projection from current truth + review."""
    snapshot = snapshot or ceo_operating.company_operating_snapshot()
    review = review or portfolio_review(snapshot)
    employees = list(snapshot.get("employees") or [])
    all_project_rows = list(snapshot.get("projects") or [])
    project_rows = [
        row for row in all_project_rows
        if str(row.get("operating_state") or "").upper() != "TERMINAL"
    ]
    current_project_ids = {int(row["project_id"]) for row in project_rows}

    semantic = {
        "founder_priorities": [{
            "project_id": row["project_id"],
            "priority": row.get("founder_priority"),
        } for row in sorted(project_rows, key=lambda row: int(row["project_id"]))],
        "operating_focus": list(review.get("operating_focus") or []),
        "project_operating_states": [{
            "project_id": row["project_id"],
            "operating_state": row.get("operating_state"),
            "founder_priority": row.get("founder_priority"),
        } for row in sorted(project_rows, key=lambda row: int(row["project_id"]))],
        "employee_allocations": [{
            "employee_id": row["employee_id"],
            "capacity": row.get("capacity"),
            "primary_responsibility": row.get("primary_responsibility"),
            "retained_responsibilities": list(row.get("retained_responsibilities") or []),
        } for row in employees],
        "pending_founder_dependencies": [
            row for row in (snapshot.get("founder_decisions") or [])
            if int(row.get("project_id") or 0) in current_project_ids
        ],
        "quiescent_projects": list(review.get("quiescence_decisions") or []),
        "results_ready": [
            row for row in (snapshot.get("results_ready") or [])
            if int(row.get("project_id") or 0) in current_project_ids
        ],
        "terminal_history_has_operating_effect": False,
        "material_risks": list(review.get("risks") or []),
        "proposed_allocations": list(review.get("allocation_decisions") or []),
        "proposed_reallocations": list(review.get("reallocation_decisions") or []),
        "staffing_findings": list(review.get("staffing_decisions") or []),
        "resource_contentions": list(review.get("resource_contentions") or []),
        "advisory_learning": {
            "baseline_version": (review.get("advisory_learning") or {}).get("baseline_version"),
            "validated_learning_ids": list(
                (review.get("advisory_learning") or {}).get("validated_learning_ids") or []
            ),
            "authority_effect": False,
            "budget_effect": False,
            "capability_effect": False,
            "current_fact_effect": False,
        },
    }
    plan_hash = _stable_hash(semantic)
    return {
        "schema": SCHEMA,
        "basis_revision": snapshot.get("revision"),
        "plan_hash": plan_hash,
        "revision": f"plan:{plan_hash[:16]}",
        **semantic,
        "truth_note": (
            "CompanyPlan is a CEO operating projection. Current durable Company truth, Founder directives, "
            "Project Contract and Governance always override this projection."
        ),
    }


def latest_company_plan() -> dict | None:
    row = (
        CompanyEvent.query.filter_by(event_type=PLAN_EVENT_TYPE)
        .order_by(CompanyEvent.id.desc()).first()
    )
    if row is None:
        return None
    payload = dict(row.payload_json or {})
    plan = payload.get("plan")
    return dict(plan) if isinstance(plan, dict) else None


def persist_company_plan(plan: dict | None = None, *, commit: bool = True) -> dict:
    """Persist a deduped CEO plan projection without changing Company domain truth."""
    plan = dict(plan or company_plan())
    if plan.get("schema") != SCHEMA or not plan.get("plan_hash"):
        raise ValueError("INVALID_CEO_COMPANY_PLAN")
    previous = latest_company_plan()
    if previous and previous.get("plan_hash") == plan.get("plan_hash"):
        return {"status": "UNCHANGED", "plan": previous, "event_id": None}

    ceo = Employee.query.filter_by(slug="ceo", active=True).first()
    event = emit(
        PLAN_EVENT_TYPE,
        actor_type="EMPLOYEE" if ceo else "SYSTEM",
        actor_id=getattr(ceo, "id", None),
        correlation_id="ceo:company-plan",
        payload={
            "schema": SCHEMA,
            "plan_hash": plan["plan_hash"],
            "basis_revision": plan.get("basis_revision"),
            "plan": plan,
            "domain_truth_mutated": False,
        },
        commit=False,
    )
    if commit:
        db.session.commit()
    return {"status": "PERSISTED", "plan": plan, "event_id": event.id}


def proposal_to_action_intent(proposal: dict, *, basis_revision: str) -> dict:
    """Translate one portfolio proposal into a typed, still-non-effectful intent."""
    kind = str(proposal.get("kind") or "")
    if kind == "ASSIGN_EXISTING_EMPLOYEE":
        payload = {
            "action_type": "ASSIGN_EXISTING_EMPLOYEE",
            "project_id": int(proposal["project_id"]),
            "work_id": int(proposal["work_id"]),
            "employee_id": int(proposal["employee_id"]),
            "required_capability": proposal.get("required_capability"),
            "basis_revision": basis_revision,
            "advisory_learning_ids": list(proposal.get("advisory_learning_ids") or []),
            "preconditions": [
                "PROJECT_NOT_PAUSED_OR_TERMINAL",
                "WORK_READY_AND_UNASSIGNED",
                "EMPLOYEE_AVAILABLE",
                "DECLARED_CAPABILITY_MATCHES",
                "NO_FOUNDER_AUTHORITY_BLOCKS_WORK",
            ],
        }
        authority_basis = "EXISTING_ROSTER_WITHIN_ALREADY_APPROVED_WORK"
    elif kind == "REALLOCATE_EXISTING_EMPLOYEE":
        payload = {
            "action_type": "REALLOCATE_EXISTING_EMPLOYEE",
            "project_id": int(proposal["project_id"]),
            "work_id": int(proposal["work_id"]),
            "from_employee_id": int(proposal["from_employee_id"]),
            "employee_id": int(proposal["employee_id"]),
            "required_capability": proposal.get("required_capability"),
            "basis_revision": basis_revision,
            "advisory_learning_ids": list(proposal.get("advisory_learning_ids") or []),
            "preconditions": [
                "PROJECT_NOT_PAUSED_OR_TERMINAL",
                "WORK_READY_NOT_EXECUTED",
                "CURRENT_OWNER_OVERCOMMITTED",
                "REPLACEMENT_EMPLOYEE_AVAILABLE",
                "DECLARED_CAPABILITY_MATCHES",
                "NO_FOUNDER_AUTHORITY_BLOCKS_WORK",
            ],
        }
        authority_basis = "RESOURCE_CONTENTION_EXISTING_ROSTER_REALLOCATION"
    else:
        raise ValueError("UNSUPPORTED_CEO_ACTION_PROPOSAL")
    return {
        "intent_id": f"ceo-intent:{_stable_hash(payload)[:24]}",
        **payload,
        "authority_basis": authority_basis,
        "effect_authorized": False,
        "status": "PROPOSED",
    }


def validate_action_intent(intent: dict) -> dict:
    """Re-read current truth before a bounded CEO staffing effect may act."""
    action_type = str(intent.get("action_type") or "")
    if action_type not in {"ASSIGN_EXISTING_EMPLOYEE", "REALLOCATE_EXISTING_EMPLOYEE"}:
        return {**intent, "status": "UNSUPPORTED_ACTION", "effect_authorized": False}
    project = db.session.get(Project, int(intent.get("project_id") or 0))
    work = db.session.get(Work, int(intent.get("work_id") or 0))
    employee = db.session.get(Employee, int(intent.get("employee_id") or 0))
    if project is None or work is None or employee is None or work.project_id != project.id:
        return {**intent, "status": "STALE", "effect_authorized": False, "reason": "SUBJECT_NOT_CURRENT"}
    ceo = Employee.query.filter_by(slug="ceo", active=True).first()
    if ceo is None:
        return {**intent, "status": "UNAUTHORIZED", "effect_authorized": False, "reason": "ACTIVE_CEO_MISSING"}
    team = __import__(
        "eason_one.services.team_formation",
        fromlist=[
            "validate_existing_employee_for_ready_work",
            "validate_existing_employee_reallocation_for_ready_work",
        ],
    )
    if action_type == "ASSIGN_EXISTING_EMPLOYEE":
        validation = team.validate_existing_employee_for_ready_work(
            work,
            employee.id,
            expected_capability=str(intent.get("required_capability") or ""),
            assigned_by_employee_id=ceo.id,
        )
    else:
        validation = team.validate_existing_employee_reallocation_for_ready_work(
            work,
            from_employee_id=int(intent.get("from_employee_id") or 0),
            to_employee_id=employee.id,
            expected_capability=str(intent.get("required_capability") or ""),
            assigned_by_employee_id=ceo.id,
        )
    if validation["status"] == "ALREADY_APPLIED":
        return {**intent, **validation, "effect_authorized": False}
    if validation["status"] != "VALIDATED":
        return {
            **intent,
            "status": validation["status"],
            "effect_authorized": False,
            "reason": validation["reason"],
        }
    return {
        **intent,
        "status": "VALIDATED",
        "effect_authorized": True,
        "reason": validation["reason"],
    }


def _stage_ceo_action_decision(validated: dict, *, ceo: Employee, reason: str) -> Decision:
    """Stage one first-class CEO Decision in the same transaction as its effect.

    A Decision is not an effect receipt.  It records why the CEO chose a bounded
    organizational action; the later receipt proves whether that action actually
    settled.  If the effect fails, transaction rollback removes this staged row
    so history never claims a committed decision whose canonical effect was not
    durably applied.
    """
    action_type = str(validated.get("action_type") or "")
    work_id = int(validated.get("work_id") or 0)
    employee_id = int(validated.get("employee_id") or 0)
    from_employee_id = validated.get("from_employee_id")
    if action_type == "REALLOCATE_EXISTING_EMPLOYEE":
        question = f"How should proven resource contention around Work #{work_id} be resolved?"
        decision_text = f"REALLOCATE EMP-{int(from_employee_id)} -> EMP-{employee_id}"
    else:
        question = f"Which existing Employee should own already-approved Work #{work_id}?"
        decision_text = f"ASSIGN EMP-{employee_id}"
    basis = {
        "schema": "CEO_ACTION_DECISION_V1",
        "intent_id": validated.get("intent_id"),
        "action_type": action_type,
        "authority_basis": validated.get("authority_basis"),
        "authority_validation": validated.get("reason"),
        "basis_revision": validated.get("basis_revision"),
        "required_capability": validated.get("required_capability"),
        "preconditions": list(validated.get("preconditions") or []),
        "advisory_learning_ids": list(validated.get("advisory_learning_ids") or []),
        "advisory_learning_has_authority_effect": False,
        "provider_execution_authorized": False,
        "kernel_execution_authorized": action_type in {"AUTHORIZE_PROJECT_REPLAN", "REQUEST_PROJECT_VERIFICATION"},
        "project_twd_authority_changed": False,
    }
    row = Decision(
        project_id=int(validated["project_id"]),
        work_id=work_id,
        proposed_by_employee_id=ceo.id,
        decided_by_employee_id=ceo.id,
        question=question,
        decision=decision_text,
        rationale=reason,
        state="COMMITTED",
        authority_basis=json.dumps(basis, ensure_ascii=False, sort_keys=True),
        committed_at=now(),
    )
    db.session.add(row)
    db.session.flush()
    return row


def action_receipt(intent_id: str) -> dict | None:
    """Return one durable CEO action receipt, if this intent already settled."""
    row = CompanyEvent.query.filter_by(
        event_type=ACTION_RECEIPT_EVENT_TYPE,
        correlation_id=str(intent_id or ""),
    ).order_by(CompanyEvent.id.desc()).first()
    if row is None:
        return None
    payload = dict(row.payload_json or {})
    return {**payload, "receipt_event_id": row.id, "replayed_from_receipt": True}


def _project_id_from_ref(value: str | None) -> int:
    text = str(value or "")
    if not text.startswith("project:"):
        raise ValueError("CEO_PROJECT_REVIEW_PROJECT_REF_REQUIRED")
    return int(text.split(":", 1)[1])


def _resume_conditions_for_review(review: dict) -> list[str]:
    primary = dict(review.get("primary_blocker") or {})
    if primary.get("type") == "EVIDENCE" and primary.get("actionable_by") == "NONE":
        return [
            "new persisted evidence changes the exact missing-evidence basis",
            "Founder changes the relevant Project constraint or Contract",
        ]
    if primary.get("actionable_by") == "FOUNDER":
        return [
            "the current Founder authority gate is resolved or invalidated",
            "the governing Project Contract materially changes",
        ]
    return ["material Project truth changes and ProjectReview finds a legal next objective"]


def project_review_to_management_intent(review: dict) -> dict | None:
    """Translate a material ProjectReview judgement into one typed management intent.

    The intent remains non-effectful until current Project truth and Governance
    are revalidated. CONTINUE remains judgement-only; material QUIESCE, Founder
    escalation, stop recommendation, replan, verification, and deterministic
    outcome-check requests use the governed management seam.
    """
    if str(review.get("review_type") or "") != "PROJECT_REVIEW":
        raise ValueError("CEO_PROJECT_REVIEW_REQUIRED")
    recommendation = str(review.get("recommendation") or "")
    action_type = {
        "QUIESCE": "DOCUMENT_QUIESCENCE",
        "ESCALATE": "SURFACE_FOUNDER_ESCALATION",
        "RECOMMEND_STOP": "RECOMMEND_PROJECT_STOP",
        "REPLAN": "AUTHORIZE_PROJECT_REPLAN",
        "REQUEST_VERIFICATION": "REQUEST_PROJECT_VERIFICATION",
        "READY_FOR_OUTCOME_CHECK": "REQUEST_PROJECT_OUTCOME_CHECK",
    }.get(recommendation)
    if action_type is None:
        return None
    project_id = _project_id_from_ref(review.get("project_ref"))
    # Once a current Result Ready proof already exists, READY_FOR_OUTCOME_CHECK
    # is descriptive Project truth for Founder review, not another CEO effect.
    # Do not mint a second Decision/receipt merely because PROJECT_RESULT_READY
    # itself triggered a ProjectReview.
    if action_type == "REQUEST_PROJECT_OUTCOME_CHECK":
        project = db.session.get(Project, project_id)
        if project is None:
            return None
        current_view = ceo_operating.project_operating_view(project)
        if current_view.get("result_state") == "RESULT_READY":
            return None
    primary = dict(review.get("primary_blocker") or {})
    advisory = dict(review.get("advisory_learning") or {})
    payload = {
        "action_type": action_type,
        "project_id": project_id,
        "basis_revision": str(review.get("basis_revision") or ""),
        "recommendation": recommendation,
        "diagnosis": str(review.get("diagnosis") or ""),
        "primary_blocker": primary,
        "resume_conditions": _resume_conditions_for_review(review),
        "advisory_learning_ids": list(advisory.get("validated_learning_ids") or []),
        "preconditions": [
            "PROJECT_NOT_PAUSED_OR_TERMINAL",
            "PROJECT_REVIEW_BASIS_STILL_CURRENT",
            "CURRENT_RECOMMENDATION_MATCHES_INTENT",
            "ADVISORY_LEARNING_DOES_NOT_GRANT_AUTHORITY",
        ],
    }
    if action_type == "DOCUMENT_QUIESCENCE":
        authority_basis = "CEO_OPERATING_AUTHORITY_SEMANTIC_QUIESCENCE_ONLY"
        payload["expected_effect"] = (
            "Project remains active; current no-work condition becomes auditable CEO quiescence truth."
        )
    elif action_type == "SURFACE_FOUNDER_ESCALATION":
        authority_basis = "EXISTING_CANONICAL_FOUNDER_GOVERNANCE_GATE"
        payload["expected_effect"] = (
            "Existing current Founder authority gate is surfaced without creating a duplicate question."
        )
    elif action_type == "AUTHORIZE_PROJECT_REPLAN":
        authority_basis = "CEO_OPERATING_AUTHORITY_WITHIN_IMMUTABLE_PROJECT_CONTRACT"
        payload["expected_effect"] = (
            "Company Kernel may plan one current bounded continuation inside unchanged Founder authority; "
            "the CEO does not create Work or call a provider."
        )
    elif action_type == "REQUEST_PROJECT_VERIFICATION":
        authority_basis = "CEO_OPERATING_AUTHORITY_REQUEST_INDEPENDENT_PROJECT_VERIFICATION"
        payload["expected_effect"] = (
            "Company Kernel may run the canonical independent Project outcome review over current accepted evidence; "
            "producer Work is not replayed."
        )
    elif action_type == "REQUEST_PROJECT_OUTCOME_CHECK":
        authority_basis = "CEO_OPERATING_AUTHORITY_REQUEST_DETERMINISTIC_PROJECT_OUTCOME_CHECK"
        payload["expected_effect"] = (
            "Company Kernel may run the existing deterministic current-Contract outcome gate. "
            "The CEO cannot declare Result Ready or complete the Project."
        )
    else:
        authority_basis = "CEO_STOP_RECOMMENDATION_REQUIRES_FOUNDER_PROJECT_CANCEL_GATE"
        payload["expected_effect"] = (
            "CEO stop recommendation opens or reuses canonical PROJECT_CANCEL Founder governance; "
            "the CEO does not terminate the Project."
        )
    return {
        "intent_id": f"ceo-intent:{_stable_hash(payload)[:24]}",
        **payload,
        "authority_basis": authority_basis,
        "effect_authorized": False,
        "status": "PROPOSED",
    }


def _management_action_for_recommendation(recommendation: str) -> str | None:
    return {
        "QUIESCE": "DOCUMENT_QUIESCENCE",
        "ESCALATE": "SURFACE_FOUNDER_ESCALATION",
        "RECOMMEND_STOP": "RECOMMEND_PROJECT_STOP",
        "REPLAN": "AUTHORIZE_PROJECT_REPLAN",
        "REQUEST_VERIFICATION": "REQUEST_PROJECT_VERIFICATION",
        "READY_FOR_OUTCOME_CHECK": "REQUEST_PROJECT_OUTCOME_CHECK",
    }.get(str(recommendation or ""))


def validate_management_intent(intent: dict) -> dict:
    """Revalidate current truth + Governance before committing a management effect."""
    action_type = str(intent.get("action_type") or "")
    if action_type not in MANAGEMENT_ACTION_TYPES:
        return {**intent, "status": "UNSUPPORTED_ACTION", "effect_authorized": False}
    project = db.session.get(Project, int(intent.get("project_id") or 0))
    if project is None:
        return {**intent, "status": "STALE", "effect_authorized": False, "reason": "PROJECT_NOT_CURRENT"}
    status = str(project.status or "").upper()
    if status == "PAUSED" or status in ceo_operating.TERMINAL_PROJECT_STATUSES:
        return {**intent, "status": "STALE", "effect_authorized": False, "reason": f"PROJECT_{status}"}

    review_api = __import__("eason_one.services.ceo_review", fromlist=["project_review"])
    fresh = review_api.project_review(project)
    if str(fresh.get("basis_revision") or "") != str(intent.get("basis_revision") or ""):
        return {**intent, "status": "STALE", "effect_authorized": False, "reason": "PROJECT_REVIEW_BASIS_CHANGED"}
    expected_action = _management_action_for_recommendation(fresh.get("recommendation"))
    if expected_action != action_type:
        return {**intent, "status": "STALE", "effect_authorized": False, "reason": "PROJECT_RECOMMENDATION_CHANGED"}
    fresh_learning_ids = list((fresh.get("advisory_learning") or {}).get("validated_learning_ids") or [])
    if fresh_learning_ids != list(intent.get("advisory_learning_ids") or []):
        return {**intent, "status": "STALE", "effect_authorized": False, "reason": "ADVISORY_LEARNING_BASIS_CHANGED"}

    primary = dict(fresh.get("primary_blocker") or {})
    governance = __import__(
        "eason_one.services.governance", fromlist=["is_founder_type", "current_gate"]
    )
    result = {**intent, "current_review": fresh}
    if action_type == "DOCUMENT_QUIESCENCE":
        # Quiescence is semantic management truth, never a Project lifecycle
        # mutation.  A Founder-owned unresolved blocker may also leave a Project
        # operationally quiescent, but that case is represented by ESCALATE and
        # therefore cannot enter this branch.
        if str(primary.get("actionable_by") or "") == "FOUNDER":
            return {**result, "status": "BLOCKED_GOVERNANCE", "effect_authorized": False, "reason": "FOUNDER_AUTHORITY_REQUIRES_ESCALATION"}
        return {**result, "status": "VALIDATED", "effect_authorized": True, "reason": "CURRENT_QUIESCENCE_CONFIRMED"}

    if action_type == "SURFACE_FOUNDER_ESCALATION":
        basis = str(primary.get("basis") or "")
        if primary.get("type") != "AUTHORITY" or primary.get("actionable_by") != "FOUNDER" or not basis.startswith("escalation:"):
            return {**result, "status": "BLOCKED_GOVERNANCE", "effect_authorized": False, "reason": "CANONICAL_FOUNDER_GATE_MISSING"}
        escalation_id = int(basis.split(":", 1)[1])
        escalation = db.session.get(Escalation, escalation_id)
        current = governance.current_gate(project)
        if (
            escalation is None
            or escalation.state != "OPEN"
            or not governance.is_founder_type(escalation.escalation_type)
            or current is None
            or int(current.id) != escalation_id
        ):
            return {**result, "status": "STALE", "effect_authorized": False, "reason": "FOUNDER_GATE_NOT_CURRENT"}
        return {
            **result,
            "status": "VALIDATED",
            "effect_authorized": True,
            "reason": "CURRENT_CANONICAL_FOUNDER_GATE_CONFIRMED",
            "escalation_id": escalation_id,
            "escalation_type": escalation.escalation_type,
        }

    if action_type in {
        "AUTHORIZE_PROJECT_REPLAN",
        "REQUEST_PROJECT_VERIFICATION",
        "REQUEST_PROJECT_OUTCOME_CHECK",
    }:
        if __import__(
            "eason_one.services.governance", fromlist=["attention"]
        ).attention(project):
            return {**result, "status": "BLOCKED_GOVERNANCE", "effect_authorized": False, "reason": "CURRENT_FOUNDER_AUTHORITY_GATE_BLOCKS_INTERNAL_MANAGEMENT"}
        if action_type == "REQUEST_PROJECT_OUTCOME_CHECK":
            current_view = ceo_operating.project_operating_view(project)
            if current_view.get("result_state") == "RESULT_READY":
                return {
                    **result,
                    "status": "STALE",
                    "effect_authorized": False,
                    "reason": "PROJECT_ALREADY_RESULT_READY",
                }
            try:
                evaluation = __import__(
                    "eason_one.services.project_outcome", fromlist=["evaluate"]
                ).evaluate(project)
            except ValueError as exc:
                return {
                    **result,
                    "status": "BLOCKED_GOVERNANCE",
                    "effect_authorized": False,
                    "reason": f"PROJECT_OUTCOME_CHECK_NOT_PROVABLE:{exc}",
                }
            if evaluation.get("overall_status") != "SATISFIED":
                return {
                    **result,
                    "status": "STALE",
                    "effect_authorized": False,
                    "reason": "PROJECT_OUTCOME_NO_LONGER_SATISFIED",
                }
        return {
            **result,
            "status": "VALIDATED",
            "effect_authorized": True,
            "reason": (
                "CURRENT_REPLAN_WITHIN_FOUNDER_CONTRACT_CONFIRMED"
                if action_type == "AUTHORIZE_PROJECT_REPLAN"
                else "CURRENT_PROJECT_VERIFICATION_REQUEST_CONFIRMED"
                if action_type == "REQUEST_PROJECT_VERIFICATION"
                else "CURRENT_DETERMINISTIC_PROJECT_OUTCOME_CHECK_CONFIRMED"
            ),
        }

    # Stop recommendation is intentionally narrower than an ordinary REPLAN:
    # ProjectReview produces it only for authoritative contradiction + no
    # executable delivery.  The CEO still lacks termination authority; Governance
    # owns the PROJECT_CANCEL question and Founder owns its resolution.
    if not (
        primary.get("type") == "EVIDENCE"
        and primary.get("basis") == "project_contract:contradicted"
        and primary.get("source") == "PROJECT_OUTCOME"
        and str(fresh.get("evidence_state") or "") == "CONTRADICTED"
    ):
        return {**result, "status": "BLOCKED_GOVERNANCE", "effect_authorized": False, "reason": "STOP_RECOMMENDATION_NOT_MATERIAL_CURRENT_TRUTH"}
    return {**result, "status": "VALIDATED", "effect_authorized": True, "reason": "MATERIAL_STOP_RECOMMENDATION_REQUIRES_FOUNDER"}


def _stage_ceo_management_decision(validated: dict, *, ceo: Employee) -> Decision:
    action_type = str(validated.get("action_type") or "")
    project_id = int(validated.get("project_id") or 0)
    primary = dict(validated.get("primary_blocker") or {})
    if action_type == "DOCUMENT_QUIESCENCE":
        question = f"Should Project #{project_id} remain active but quiescent under current truth?"
        selected = "QUIESCE"
    elif action_type == "SURFACE_FOUNDER_ESCALATION":
        question = f"Does Project #{project_id} currently require the existing Founder authority decision?"
        selected = f"ESCALATE VIA FOUNDER GATE #{int(validated.get('escalation_id') or 0)}"
    elif action_type == "AUTHORIZE_PROJECT_REPLAN":
        question = f"Should Project #{project_id} change its bounded continuation strategy under the current Founder Contract?"
        selected = "REPLAN"
    elif action_type == "REQUEST_PROJECT_VERIFICATION":
        question = f"Should Project #{project_id} run independent Project outcome verification over current accepted evidence?"
        selected = "REQUEST_VERIFICATION"
    elif action_type == "REQUEST_PROJECT_OUTCOME_CHECK":
        question = f"Should Project #{project_id} run the deterministic Project outcome gate against current accepted proof?"
        selected = "READY_FOR_OUTCOME_CHECK"
    else:
        question = f"Should Project #{project_id} be recommended to the Founder for termination?"
        selected = "RECOMMEND_STOP"
    basis = {
        "schema": "CEO_MANAGEMENT_DECISION_V1",
        "intent_id": validated.get("intent_id"),
        "action_type": action_type,
        "authority_basis": validated.get("authority_basis"),
        "authority_validation": validated.get("reason"),
        "basis_revision": validated.get("basis_revision"),
        "primary_blocker": primary,
        "resume_conditions": list(validated.get("resume_conditions") or []),
        "expected_effect": validated.get("expected_effect"),
        "advisory_learning_ids": list(validated.get("advisory_learning_ids") or []),
        "advisory_learning_has_authority_effect": False,
        "project_lifecycle_mutation_authorized": False,
        "project_termination_authorized": False,
        "project_result_ready_authorized": False,
        "project_completion_authorized": False,
        "provider_execution_authorized": False,
        "project_twd_authority_changed": False,
    }
    row = Decision(
        project_id=project_id,
        proposed_by_employee_id=ceo.id,
        decided_by_employee_id=ceo.id,
        question=question,
        decision=selected,
        rationale=str(validated.get("diagnosis") or validated.get("reason") or ""),
        state="COMMITTED",
        authority_basis=json.dumps(basis, ensure_ascii=False, sort_keys=True),
        committed_at=now(),
    )
    db.session.add(row)
    db.session.flush()
    return row


def execute_management_intent(intent: dict, *, commit: bool = True) -> dict:
    """Commit one bounded CEO management decision + durable effect receipt atomically."""
    intent_id = str(intent.get("intent_id") or "").strip()
    if not intent_id:
        raise ValueError("CEO_ACTION_INTENT_ID_REQUIRED")
    settled = action_receipt(intent_id)
    if settled is not None:
        return settled
    validated = validate_management_intent(intent)
    if not validated.get("effect_authorized"):
        return validated
    project = db.session.get(Project, int(validated["project_id"]))
    ceo = Employee.query.filter_by(slug="ceo", active=True).first()
    if project is None or ceo is None:
        return {**validated, "status": "STALE", "effect_authorized": False, "reason": "SUBJECT_NOT_CURRENT"}

    governance = __import__(
        "eason_one.services.governance", fromlist=["open_gate", "identity_for"]
    )
    action_type = str(validated["action_type"])
    try:
        decision = _stage_ceo_management_decision(validated, ceo=ceo)
        escalation_id = validated.get("escalation_id")
        governance_effect = "CURRENT_TRUTH_REVALIDATED"
        if action_type == "DOCUMENT_QUIESCENCE":
            semantic_effect = "QUIESCENCE_DOCUMENTED"
        elif action_type == "SURFACE_FOUNDER_ESCALATION":
            semantic_effect = "EXISTING_FOUNDER_GATE_SURFACED"
            governance_effect = "EXISTING_FOUNDER_GATE_REUSED"
        elif action_type == "AUTHORIZE_PROJECT_REPLAN":
            semantic_effect = "KERNEL_PROJECT_REPLAN_AUTHORIZED"
            governance_effect = "CURRENT_FOUNDER_CONTRACT_REVALIDATED"
        elif action_type == "REQUEST_PROJECT_VERIFICATION":
            semantic_effect = "KERNEL_PROJECT_VERIFICATION_REQUESTED"
            governance_effect = "CURRENT_ACCEPTED_EVIDENCE_REVALIDATED"
        elif action_type == "REQUEST_PROJECT_OUTCOME_CHECK":
            semantic_effect = "KERNEL_PROJECT_OUTCOME_CHECK_REQUESTED"
            governance_effect = "CURRENT_PROJECT_OUTCOME_BASIS_REVALIDATED"
        else:
            gate = governance.open_gate(
                project=project,
                escalation_type="PROJECT_CANCEL",
                reason=(
                    f"CEO recommends stopping Project #{project.id}: "
                    f"{validated.get('diagnosis') or 'current authoritative evidence contradicts the Project Contract.'}"
                ),
                created_by_employee_id=ceo.id,
                authority_payload={},
                recommendation=(
                    "Cancel this Project only if the Founder accepts the CEO recommendation under current evidence."
                ),
            )
            escalation_id = int(gate.id)
            semantic_effect = "STOP_RECOMMENDATION_ROUTED_TO_FOUNDER"
            governance_effect = "PROJECT_CANCEL_GATE_OPENED_OR_REUSED"

        receipt_payload = {
            "intent_id": intent_id,
            "action_type": action_type,
            "status": "APPLIED",
            "project_id": project.id,
            "decision_id": decision.id,
            "escalation_id": escalation_id,
            "authority_basis": validated.get("authority_basis"),
            "authority_validation": validated.get("reason"),
            "basis_revision": validated.get("basis_revision"),
            "governance_effect": governance_effect,
            "semantic_effect": semantic_effect,
            "resume_conditions": list(validated.get("resume_conditions") or []),
            "advisory_learning_ids": list(validated.get("advisory_learning_ids") or []),
            "advisory_learning_has_authority_effect": False,
            "project_paused": False,
            "project_terminated": False,
            "project_result_ready_declared": False,
            "project_completed": False,
            "provider_execution_created": False,
            "kernel_execution_authorized": action_type in {
                "AUTHORIZE_PROJECT_REPLAN",
                "REQUEST_PROJECT_VERIFICATION",
                "REQUEST_PROJECT_OUTCOME_CHECK",
            },
            "kernel_outcome_check_requested": action_type == "REQUEST_PROJECT_OUTCOME_CHECK",
            "project_twd_authority_changed": False,
            "new_work_created": False,
            "new_employee_created": False,
        }
        receipt = emit(
            ACTION_RECEIPT_EVENT_TYPE,
            actor_type="EMPLOYEE",
            actor_id=ceo.id,
            project_id=project.id,
            decision_id=decision.id,
            correlation_id=intent_id,
            payload=receipt_payload,
            commit=False,
        )
        if commit:
            db.session.commit()
        return {**receipt_payload, "receipt_event_id": receipt.id, "replayed_from_receipt": False}
    except ValueError as exc:
        db.session.rollback()
        return {
            **validated,
            "status": "FAILED_CLOSED",
            "effect_authorized": False,
            "reason": str(exc),
        }


def execute_action_intent(intent: dict, *, commit: bool = True) -> dict:
    """Apply one validated CEO staffing effect through canonical Company services.

    Receipt and staffing mutation share one transaction.  Replaying one
    ``intent_id`` therefore cannot duplicate assignment/reassignment or internal
    Market generations after restart.
    """
    intent_id = str(intent.get("intent_id") or "").strip()
    if not intent_id:
        raise ValueError("CEO_ACTION_INTENT_ID_REQUIRED")
    settled = action_receipt(intent_id)
    if settled is not None:
        return settled

    validated = validate_action_intent(intent)
    if validated.get("status") == "ALREADY_APPLIED":
        return validated
    if not validated.get("effect_authorized"):
        return validated
    if validated.get("action_type") not in {"ASSIGN_EXISTING_EMPLOYEE", "REALLOCATE_EXISTING_EMPLOYEE"}:
        return {**validated, "status": "UNSUPPORTED_ACTION", "effect_authorized": False}

    project = db.session.get(Project, int(validated["project_id"]))
    work = db.session.get(Work, int(validated["work_id"]))
    employee = db.session.get(Employee, int(validated["employee_id"]))
    ceo = Employee.query.filter_by(slug="ceo", active=True).first()
    if project is None or work is None or employee is None or ceo is None:
        return {**validated, "status": "STALE", "effect_authorized": False, "reason": "SUBJECT_NOT_CURRENT"}

    team = __import__(
        "eason_one.services.team_formation",
        fromlist=[
            "assign_existing_employee_for_ready_work",
            "reassign_existing_employee_for_ready_work",
        ],
    )
    action_type = str(validated["action_type"])
    try:
        if action_type == "ASSIGN_EXISTING_EMPLOYEE":
            reason = (
                f"CEO portfolio assigned existing {validated['required_capability']} capacity "
                f"to already-approved Work #{work.id}; no new Work, Employee, TWD authority or provider call was created."
            )
        else:
            from_employee_id = int(validated["from_employee_id"])
            reason = (
                f"CEO portfolio resolved Employee overcommitment by moving READY Work #{work.id} "
                f"from EMP-{from_employee_id} to available EMP-{employee.id}; in-flight execution was not transferred."
            )
        decision = _stage_ceo_action_decision(validated, ceo=ceo, reason=reason)
        if action_type == "ASSIGN_EXISTING_EMPLOYEE":
            applied = team.assign_existing_employee_for_ready_work(
                work,
                employee.id,
                assigned_by_employee_id=ceo.id,
                reason=reason,
                expected_capability=str(validated["required_capability"]),
                intent_id=intent_id,
            )
        else:
            from_employee_id = int(validated["from_employee_id"])
            applied = team.reassign_existing_employee_for_ready_work(
                work,
                from_employee_id=from_employee_id,
                to_employee_id=employee.id,
                assigned_by_employee_id=ceo.id,
                reason=reason,
                expected_capability=str(validated["required_capability"]),
                intent_id=intent_id,
            )
        receipt_payload = {
            "intent_id": intent_id,
            "action_type": action_type,
            "status": applied["status"],
            "project_id": project.id,
            "work_id": work.id,
            "from_employee_id": applied.get("from_employee_id"),
            "employee_id": employee.id,
            "required_capability": validated["required_capability"],
            "assignment_id": applied.get("assignment_id"),
            "decision_id": decision.id,
            "authority_basis": validated.get("authority_basis"),
            "authority_validation": validated.get("reason"),
            "provider_execution_created": False,
            "provider_execution_transferred": False,
            "project_twd_authority_changed": False,
            "new_work_created": False,
            "new_employee_created": False,
            "prior_assignment_history_erased": False,
        }
        receipt = emit(
            ACTION_RECEIPT_EVENT_TYPE,
            actor_type="EMPLOYEE",
            actor_id=ceo.id,
            project_id=project.id,
            work_id=work.id,
            decision_id=decision.id,
            correlation_id=intent_id,
            payload=receipt_payload,
            commit=False,
        )
        if commit:
            db.session.commit()
        return {**receipt_payload, "receipt_event_id": receipt.id, "replayed_from_receipt": False}
    except ValueError as exc:
        db.session.rollback()
        return {
            **validated,
            "status": "FAILED_CLOSED",
            "effect_authorized": False,
            "reason": str(exc),
        }
