"""Evidence-backed Persistent Employee evolution for Eason One.

This module is intentionally deterministic.  It does not ask an LLM to decide
who is "better" and it does not turn model prose into performance truth.
Accepted Work / Artifact / Verification records are the durable learning basis.
The resulting profile is allowed to influence future staffing only as a
bounded tie-breaker after role/capability authority has already matched.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from ..extensions import db
from ..models import AgentRun, Employee, EmployeeLearningRecord, ExternalEffectAttempt, Work

OUTCOME_TYPES = frozenset({"WORK_EXPERIENCE", "REVIEW_EXPERIENCE"})
CANONICAL_BASIS = "CANONICAL_WORK_ACCEPTANCE"
SCHEMA = "EMPLOYEE_EVOLUTION_V1_3"


def _dt(value):
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _capability_from_work(work: Work | None) -> str | None:
    if work is None:
        return None
    control = dict(work.runtime_control_json or {})
    team = dict(control.get("team_formation") or {})
    required = str(team.get("required_capability") or "").strip().upper()
    if required:
        return required
    staffing = dict(control.get("staffing_requirements") or {})
    declared = [str(value).strip().upper() for value in (staffing.get("required_capabilities") or []) if str(value).strip()]
    return declared[0] if len(declared) == 1 else None


def record_capability(record: EmployeeLearningRecord) -> str | None:
    evidence = dict(record.evidence_json or {})
    value = str(evidence.get("capability") or "").strip().upper()
    if value:
        return value
    work = db.session.get(Work, record.work_id) if record.work_id else None
    return _capability_from_work(work)


def canonical_records(employee_id: int) -> list[EmployeeLearningRecord]:
    return (
        EmployeeLearningRecord.query.filter(
            EmployeeLearningRecord.employee_id == employee_id,
            EmployeeLearningRecord.validated.is_(True),
            EmployeeLearningRecord.learning_type.in_(tuple(OUTCOME_TYPES)),
            EmployeeLearningRecord.validation_basis == CANONICAL_BASIS,
        )
        .order_by(EmployeeLearningRecord.created_at.desc(), EmployeeLearningRecord.id.desc())
        .all()
    )


def probation_status(employee: Employee) -> dict[str, Any]:
    """Read-only evidence projection for a Persistent Employee's probation."""
    target = int(employee.probation_target_assignments or 0)
    owner_records = [row for row in canonical_records(employee.id) if row.learning_type == "WORK_EXPERIENCE"]
    completed = len(owner_records)
    return {
        "schema": "EMPLOYEE_PROBATION_EVOLUTION_V1",
        "employee_id": employee.id,
        "employment_status": employee.employment_status,
        "target_assignments": target,
        "accepted_owner_assignments": completed,
        "remaining_assignments": max(0, target - completed) if target > 0 else None,
        "transitioned": False,
        "truth_note": "Probation completion is evidence-backed employment state only; it grants no new authority or capability.",
    }


def reconcile_probation(employee: Employee) -> dict[str, Any]:
    """Promote a probationary Persistent Employee only from accepted owner work.

    Hiring establishes a probation target; canonical accepted Work proves it.
    Completion changes employment lifecycle state only. It does not mint a new
    capability, budget, model authority, salary, or organizational rank.
    """
    snapshot = probation_status(employee)
    target = int(snapshot["target_assignments"] or 0)
    completed = int(snapshot["accepted_owner_assignments"] or 0)
    owner_records = [row for row in canonical_records(employee.id) if row.learning_type == "WORK_EXPERIENCE"]
    changed = False
    if employee.employment_status == "PROBATION" and target > 0 and completed >= target:
        employee.employment_status = "ACTIVE"
        changed = True
        __import__(
            "eason_one.services.company_events", fromlist=["emit"]
        ).emit(
            "EMPLOYEE_PROBATION_COMPLETED",
            actor_type="RUNTIME",
            actor_id=employee.id,
            project_id=getattr(owner_records[0], "project_id", None) if owner_records else None,
            work_id=getattr(owner_records[0], "work_id", None) if owner_records else None,
            correlation_id=f"employee:{employee.id}",
            payload={
                "employee_id": employee.id,
                "accepted_owner_assignments": completed,
                "probation_target_assignments": target,
                "canonical_learning_record_ids": [row.id for row in owner_records[:target]],
                "new_employment_status": "ACTIVE",
                "authority_change": False,
                "capability_change": False,
                "budget_change": False,
            },
        )
    return {
        "schema": "EMPLOYEE_PROBATION_EVOLUTION_V1",
        "employee_id": employee.id,
        "employment_status": employee.employment_status,
        "target_assignments": target,
        "accepted_owner_assignments": completed,
        "remaining_assignments": max(0, target - completed) if target > 0 else None,
        "transitioned": changed,
        "truth_note": "Probation completion is evidence-backed employment state only; it grants no new authority or capability.",
    }


def capability_profile(employee: Employee, capability: str) -> dict[str, Any]:
    """Return current outcome-backed experience for one capability.

    ``score`` is not authority and cannot create a capability.  Team formation
    may use it only after the employee independently satisfies the required
    capability through the governed roster/role contract.
    """
    capability = str(capability or "").strip().upper()
    matched = [row for row in canonical_records(employee.id) if record_capability(row) == capability]
    owner = sum(row.learning_type == "WORK_EXPERIENCE" for row in matched)
    review = sum(row.learning_type == "REVIEW_EXPERIENCE" for row in matched)
    recovery_burden = 0
    for row in matched:
        attempts = list((row.evidence_json or {}).get("execution_attempt_ids") or [])
        recovery_burden += max(0, len(attempts) - 1)
    # Accepted owner experience has the strongest weight. Independent review is
    # useful evidence too. Recovery burden never erases accepted outcomes, but
    # distinguishes equally-qualified employees who needed materially more
    # attempts to reach those accepted outcomes.
    score = owner * 4 + review * 2 - recovery_burden
    if owner >= 4 and recovery_burden <= 1:
        depth = "DEEP"
    elif owner or review:
        depth = "PROVEN"
    else:
        depth = "NEW"
    latest = max((_dt(row.created_at) for row in matched if row.created_at), default=None)
    return {
        "schema": SCHEMA,
        "employee_id": employee.id,
        "capability": capability,
        "owner_acceptances": owner,
        "review_acceptances": review,
        "accepted_evidence": len(matched),
        "recovery_burden": recovery_burden,
        "score": score,
        "depth": depth,
        "record_ids": [row.id for row in matched],
        "last_accepted_at": latest,
    }


def employee_profile(employee: Employee) -> dict[str, Any]:
    """Readable evolution profile for Founder / research surfaces."""
    role_caps = set(__import__(
        "eason_one.services.team_formation", fromlist=["employee_capabilities"]
    ).employee_capabilities(employee))
    learned_caps = {cap for row in canonical_records(employee.id) if (cap := record_capability(row))}
    capabilities = sorted(role_caps | learned_caps)
    profiles = []
    for capability in capabilities:
        row = capability_profile(employee, capability)
        row["authorized_capability"] = capability in role_caps
        row["historical_only"] = capability not in role_caps and row["accepted_evidence"] > 0
        profiles.append(row)
    profiles.sort(
        key=lambda row: (
            bool(row["authorized_capability"]),
            row["accepted_evidence"],
            row["score"],
            row["capability"],
        ),
        reverse=True,
    )
    total_owner = sum(row["owner_acceptances"] for row in profiles)
    total_review = sum(row["review_acceptances"] for row in profiles)
    influence = staffing_influence_history(employee, limit=20)
    changed = [row for row in influence if row.get("behavior_changed")]
    probation = probation_status(employee)
    return {
        "schema": "EMPLOYEE_EVOLUTION_V1_3",
        "employee_id": employee.id,
        "capabilities": profiles,
        "authorized_capabilities": sorted(role_caps),
        "historical_evidence_capabilities": sorted(learned_caps - role_caps),
        "owner_acceptances": total_owner,
        "review_acceptances": total_review,
        "canonical_record_count": len(canonical_records(employee.id)),
        "probation": probation,
        "staffing_influence_count": sum(bool(row.get("experience_influenced_selection")) for row in influence),
        "behavior_change_count": len(changed),
        "staffing_influence": influence[:8],
        "behavioral_effect": (
            f"Outcome-backed experience changed {len(changed)} persisted staffing decision(s)."
            if changed else
            "Outcome-backed experience is eligible to influence future staffing tie-breaks."
            if any(row["accepted_evidence"] for row in profiles)
            else "No accepted Work experience has influenced future staffing yet."
        ),
    }



def staffing_influence_history(employee: Employee, *, limit: int = 20) -> list[dict[str, Any]]:
    """Return persisted staffing decisions where outcome-backed evolution mattered.

    This is a read projection over ``Work.runtime_control_json``.  It does not
    invent a causal claim: a row is marked behavioral only when Team Formation
    persisted both ``experience_influenced_selection`` and a counterfactual
    baseline showing that the same eligible roster would otherwise select a
    different Employee without outcome-backed learning.
    """
    rows: list[dict[str, Any]] = []
    for work in Work.query.order_by(Work.id.desc()).all():
        control = dict(work.runtime_control_json or {})
        team = dict(control.get("team_formation") or {})
        evidence = dict(team.get("selection_evidence") or {})
        if int(evidence.get("selected_employee_id") or 0) != int(employee.id):
            continue
        counterfactual = dict(evidence.get("counterfactual_without_learning") or {})
        behavior_changed = bool(
            evidence.get("experience_influenced_selection")
            and counterfactual.get("employee_id")
            and int(counterfactual.get("employee_id")) != int(employee.id)
        )
        rows.append({
            "work_id": work.id,
            "project_id": work.project_id,
            "work_title": work.title,
            "required_capability": evidence.get("required_capability") or team.get("required_capability"),
            "decision_basis": evidence.get("decision_basis"),
            "reason": evidence.get("reason"),
            "experience_influenced_selection": bool(evidence.get("experience_influenced_selection")),
            "behavior_changed": behavior_changed,
            "counterfactual_employee_id": counterfactual.get("employee_id"),
            "counterfactual_employee_name": counterfactual.get("employee_name"),
            "selected_experience_score": evidence.get("selected_experience_score"),
            "market_contract": evidence.get("market_contract"),
        })
        if len(rows) >= max(1, int(limit)):
            break
    return rows


def company_behavioral_evolution_summary() -> dict[str, Any]:
    """Research-safe aggregate of persisted learning-driven behavior changes."""
    decisions = []
    for work in Work.query.order_by(Work.id.desc()).all():
        team = dict((work.runtime_control_json or {}).get("team_formation") or {})
        evidence = dict(team.get("selection_evidence") or {})
        if not evidence:
            continue
        counterfactual = dict(evidence.get("counterfactual_without_learning") or {})
        changed = bool(
            evidence.get("experience_influenced_selection")
            and counterfactual.get("employee_id")
            and int(counterfactual.get("employee_id")) != int(evidence.get("selected_employee_id") or 0)
        )
        decisions.append({
            "work_id": work.id,
            "project_id": work.project_id,
            "work_title": work.title,
            "selected_employee_id": evidence.get("selected_employee_id"),
            "selected_employee_name": evidence.get("selected_employee_name"),
            "decision_basis": evidence.get("decision_basis"),
            "experience_influenced_selection": bool(evidence.get("experience_influenced_selection")),
            "behavior_changed": changed,
            "counterfactual": counterfactual,
            "reason": evidence.get("reason"),
        })
    changed = [row for row in decisions if row["behavior_changed"]]
    return {
        "schema": "COMPANY_BEHAVIORAL_EVOLUTION_V1",
        "staffing_decisions_with_evidence": len(decisions),
        "experience_influenced_decisions": sum(row["experience_influenced_selection"] for row in decisions),
        "counterfactual_behavior_changes": len(changed),
        "examples": changed[:8],
        "truth_note": (
            "A behavioral change is counted only when persisted Team Formation evidence records "
            "an experience-driven selection and a different no-learning counterfactual among the same eligible roster."
        ),
    }


def learning_consumption_summary(*, limit: int = 20) -> dict[str, Any]:
    """Return persisted evidence that canonical Employee memory reached later Runs.

    A row counts only when runtime persisted exact canonical learning record ids
    in ``AgentRun.context_composition_json``. This proves consumption of prior
    accepted outcomes, not that the later outcome improved because of them.
    Causal outcome claims remain separate (for example staffing counterfactuals).
    """
    rows: list[dict[str, Any]] = []
    consumed_ids: set[int] = set()
    canonical_ids = {
        row.id for row in EmployeeLearningRecord.query.filter(
            EmployeeLearningRecord.validated.is_(True),
            EmployeeLearningRecord.validation_basis == CANONICAL_BASIS,
            EmployeeLearningRecord.learning_type.in_(tuple(OUTCOME_TYPES)),
        ).all()
    }
    dispatched_run_ids = {
        int(value) for (value,) in db.session.query(ExternalEffectAttempt.execution_id).filter(
            ExternalEffectAttempt.dispatched_at.isnot(None)
        ).all()
    }
    employee_cache: dict[int, Employee | None] = {}
    execution_runs = 0
    review_runs = 0
    total_consumption_runs = 0
    for run in AgentRun.query.order_by(AgentRun.id.desc()).all():
        # Metadata staged before provider/tool dispatch is not consumption. The
        # durable ExternalEffect dispatch boundary proves the governed prompt
        # actually crossed into the model/tool execution lifecycle.
        if run.id not in dispatched_run_ids:
            continue
        composition = dict(run.context_composition_json or {})
        memory = dict(composition.get("persistent_employee_memory") or {})
        record_ids = []
        for value in memory.get("experience_record_ids") or []:
            try:
                record_ids.append(int(value))
            except (TypeError, ValueError):
                continue
        if not record_ids or memory.get("validation_basis") != CANONICAL_BASIS:
            continue
        ordered_ids = [value for value in record_ids if value in canonical_ids]
        if not ordered_ids:
            continue
        consumed_ids.update(ordered_ids)
        total_consumption_runs += 1
        if run.purpose == "TASK_EXECUTION":
            execution_runs += 1
        elif run.purpose == "TASK_REVIEW":
            review_runs += 1
        employee = None
        if run.employee_id:
            if run.employee_id not in employee_cache:
                employee_cache[run.employee_id] = db.session.get(Employee, run.employee_id)
            employee = employee_cache[run.employee_id]
        if len(rows) < max(1, int(limit)):
            rows.append({
                "run_id": run.id,
                "purpose": run.purpose,
                "status": run.status,
                "employee_id": run.employee_id,
                "employee_name": employee.name if employee else None,
                "project_id": run.project_id,
                "task_id": run.task_id,
                "work_id": run.work_id,
                "required_capability": memory.get("required_capability"),
                "experience_record_ids": ordered_ids,
                "matched_records": len(ordered_ids),
                "provider": run.provider_key_snapshot,
                "model": run.model_name_snapshot,
            })
    return {
        "schema": "COMPANY_LEARNING_CONSUMPTION_V1",
        "runs_with_canonical_memory": total_consumption_runs,
        "task_execution_runs": execution_runs,
        "task_review_runs": review_runs,
        "unique_learning_records_consumed": len(consumed_ids),
        "examples": rows[:8],
        "truth_note": (
            "A consumption row means exact canonical accepted-Work memory record ids were persisted in a later Run and its durable ExternalEffect crossed the dispatch boundary. "
            "It proves the company sent prior experience into governed execution, not that the later outcome improved because of it."
        ),
    }

def selection_profile(employee: Employee, capability: str, *, direct: int, specialist: int, active_workload: int) -> dict[str, Any]:
    evolution = capability_profile(employee, capability)
    return {
        "employee_id": employee.id,
        "employee_name": employee.name,
        "capability": capability,
        "direct_role_match": bool(direct),
        "specialist": bool(specialist),
        "experience_score": evolution["score"],
        "accepted_evidence": evolution["accepted_evidence"],
        "accepted_owner_experience": evolution["owner_acceptances"],
        "accepted_review_experience": evolution["review_acceptances"],
        "recovery_burden": evolution["recovery_burden"],
        "experience_record_ids": evolution["record_ids"],
        "active_workload": int(active_workload),
        "depth": evolution["depth"],
    }


def selection_reason(selected: dict[str, Any], candidate_count: int) -> str:
    if selected.get("accepted_owner_experience"):
        experience = f"{selected['accepted_owner_experience']} accepted owner outcome(s)"
    elif selected.get("accepted_review_experience"):
        experience = f"{selected['accepted_review_experience']} accepted review outcome(s)"
    else:
        experience = "no prior accepted outcome for this capability"
    role = "direct specialist" if selected.get("specialist") else "capable roster member"
    return (
        f"Selected from {candidate_count} eligible employee(s): {role}; {experience}; "
        f"current active workload {selected.get('active_workload', 0)}."
    )
