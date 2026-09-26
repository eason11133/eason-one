"""Deterministic CEO operating read model for EASON ONE.

This module is the first layer of the CEO operating system.  It does not plan,
spend, schedule, mutate Project truth, or call a provider.  It converts durable
Company Core truth into the semantic operating view the CEO can reason over:

- CompanyOperatingSnapshot: what the company is doing now
- SemanticProgress: whether something materially changed
- BlockerAssessment: why a Project cannot make its next meaningful move

The renderer, Jarvis, and LLM-based CEO reasoning are deliberately downstream.
Current durable Company truth always wins over memory, prose, or model output.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime
from decimal import Decimal
import hashlib
import json
from typing import Any

from sqlalchemy import func

from ..extensions import db
from ..models import (
    Artifact,
    ArtifactVersion,
    AgentRun,
    CompanyEvent,
    CostEvent,
    Employee,
    Escalation,
    Project,
    Work,
    WorkAssignment,
)


OPERATING_PROJECT_STATUSES = {"PLANNING", "ACTIVE", "BLOCKED", "REVIEW", "PAUSED"}
TERMINAL_PROJECT_STATUSES = {"COMPLETED", "CANCELLED", "FAILED"}
ACTIVE_WORK_STATES = {"READY", "EXECUTING", "WAITING", "VERIFYING"}

BLOCKER_TYPES = {
    "EXECUTION",
    "DEPENDENCY",
    "EVIDENCE",
    "CAPABILITY",
    "RESOURCE",
    "AUTHORITY",
    "STRATEGY",
    "EXTERNAL",
}

EMPLOYEE_CAPACITY_STATES = {
    "AVAILABLE",
    "PRIMARY_ASSIGNED",
    "WAITING",
    "OVERCOMMITTED",
    "UNAVAILABLE",
}

MEANINGFUL_PROGRESS_TYPES = {
    "MILESTONE_ADVANCED",
    "BLOCKER_RESOLVED",
    "EVIDENCE_ADVANCED",
    "ARTIFACT_ADVANCED",
    "VERIFICATION_ADVANCED",
    "RESULT_ADVANCED",
}

# Existing CompanyEvent names that have durable, business-significant meaning.
# This is intentionally conservative.  Provider calls, retries, scheduler ticks,
# and plain Artifact submission are activity, not CEO-level progress.
_PROGRESS_EVENT_MAP = {
    "WORK_ACCEPTED": "MILESTONE_ADVANCED",
    "ARTIFACT_ACCEPTED": "ARTIFACT_ADVANCED",
    "ARTIFACT_REJECTED": "VERIFICATION_ADVANCED",
    "WORK_EVIDENCE_REVIEW_EXHAUSTED": "VERIFICATION_ADVANCED",
    "PROJECT_HOST_HTTP_REVERIFIED": "EVIDENCE_ADVANCED",
    "PROJECT_OUTCOME_READY": "RESULT_ADVANCED",
    "PROJECT_RESULT_READY": "RESULT_ADVANCED",
    "FOUNDER_PROJECT_COMPLETED": "RESULT_ADVANCED",
}

_GATE_TO_BLOCKER = {
    "DEPENDENCY": "DEPENDENCY",
    "CAPABILITY_GAP": "CAPABILITY",
    "FOUNDER_DECISION": "AUTHORITY",
    "AUTHORITY_EXHAUSTED": "AUTHORITY",
    "SYSTEM_RECOVERY": "EXECUTION",
    "RECONCILIATION": "EXECUTION",
    "INTERNAL_RECOVERY": "EXECUTION",
    "RETRY_BACKOFF": "EXECUTION",
    # These waits mean implementation truth exists but sufficient proof does
    # not.  The blocker is evidence/verification, not a reason to replay the
    # implementation.
    "EVIDENCE_REVIEW_RETRY": "EVIDENCE",
    "HOST_PROOF_RETRY": "EVIDENCE",
}


def _iso(value) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _money(value) -> Decimal:
    return Decimal(value or 0)


def _jsonable(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    return value


def _stable_hash(payload: dict) -> str:
    body = json.dumps(_jsonable(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _latest_event_id() -> int:
    return int(db.session.query(func.coalesce(func.max(CompanyEvent.id), 0)).scalar() or 0)


def _governed_project(project: Project) -> bool:
    try:
        return bool(__import__(
            "eason_one.services.project_contract", fromlist=["is_vnext_governed"]
        ).is_vnext_governed(project))
    except Exception:
        return False


def _contract_view(project: Project) -> dict:
    if not _governed_project(project):
        return {
            "objective": project.objective,
            "success_criteria": [],
            "constraints": [],
            "deadline": _iso(project.deadline),
            "budget_limit_twd": str(_money(project.real_budget_limit)),
            "governing_contract_hash": None,
            "integrity_error": "LEGACY_OR_UNGOVERNED_PROJECT",
        }
    try:
        projection = __import__(
            "eason_one.services.project_contract", fromlist=["read_projection"]
        ).read_projection(project)
        terms = dict(projection.get("terms") or {})
        return {
            "objective": terms.get("objective") or project.objective,
            "success_criteria": list(terms.get("success_criteria") or []),
            "constraints": list(terms.get("constraints") or []),
            "deadline": _iso(terms.get("deadline") or project.deadline),
            "budget_limit_twd": str(_money(terms.get("budget_limit_twd") or project.real_budget_limit)),
            "governing_contract_hash": terms.get("governing_contract_hash") or projection.get("governing_contract_hash"),
            "integrity_error": projection.get("integrity_error"),
        }
    except Exception as exc:
        # Read models fail closed; the CEO may surface the integrity problem but
        # cannot invent a replacement Contract.
        return {
            "objective": project.objective,
            "success_criteria": [],
            "constraints": [],
            "deadline": _iso(project.deadline),
            "budget_limit_twd": str(_money(project.real_budget_limit)),
            "governing_contract_hash": None,
            "integrity_error": f"CONTRACT_READ_FAILED:{type(exc).__name__}",
        }


def _project_cost(project_id: int) -> Decimal:
    return _money(db.session.query(func.coalesce(func.sum(CostEvent.real_cost_delta), 0)).filter(
        CostEvent.project_id == project_id
    ).scalar())


def _active_assignments(project: Project) -> list[dict]:
    # Terminal Project responsibility is retained in WorkAssignment history but
    # is not current operating responsibility.  Do not require mutation of the
    # historical assignment row merely to release CEO capacity/read semantics.
    if str(project.status or "").upper() in TERMINAL_PROJECT_STATUSES:
        return []
    rows = (
        WorkAssignment.query.join(Work, WorkAssignment.work_id == Work.id)
        .filter(
            Work.project_id == project.id,
            WorkAssignment.ended_at.is_(None),
            Work.state.in_(ACTIVE_WORK_STATES),
        )
        .order_by(WorkAssignment.id).all()
    )
    return [{
        "assignment_id": row.id,
        "work_id": row.work_id,
        "work_title": row.work.title,
        "work_state": row.work.state,
        "work_type": row.work.work_type,
        "employee_id": row.employee_id,
        "employee_name": row.employee.name if row.employee else None,
        "responsibility": row.responsibility,
        "reason": row.reason,
    } for row in rows]


def employee_capacity_view(employee: Employee | int) -> dict:
    """Project durable Work responsibility into a CEO staffing view.

    Provider/model identity is intentionally absent.  Capacity describes the
    persistent Employee and current Company responsibility only.
    """
    if isinstance(employee, int):
        employee = db.session.get(Employee, employee)
    if employee is None:
        raise ValueError("EMPLOYEE_NOT_FOUND")
    if not employee.active or str(employee.employment_status or "ACTIVE").upper() != "ACTIVE":
        return {
            "employee_id": employee.id,
            "name": employee.name,
            "slug": employee.slug,
            "capacity": "UNAVAILABLE",
            "primary_responsibility": None,
            "active_responsibilities": [],
            "retained_responsibilities": [],
            "active_project_id": None,
            "active_project_ids": [],
            "current_responsibility": None,
            "queued_responsibilities": [],
            "assignment_conflict": False,
        }

    assignments = (
        WorkAssignment.query.join(Work, WorkAssignment.work_id == Work.id)
        .join(Project, Work.project_id == Project.id)
        .filter(
            WorkAssignment.employee_id == employee.id,
            WorkAssignment.ended_at.is_(None),
            Work.state.in_(ACTIVE_WORK_STATES),
            Project.environment == "LIVE",
            ~Project.status.in_(tuple(TERMINAL_PROJECT_STATUSES | {"PAUSED"})),
        )
        .order_by(WorkAssignment.started_at, WorkAssignment.id).all()
    )

    # A durable assignment can remain accountable history while consuming no
    # present operating capacity.  The canonical example is the exact
    # MISSING_EVIDENCE:<basis> gate: runtime has proved that the Work must stay
    # quiescent until *new persisted evidence* appears, so pretending its owner
    # is still busy would make the CEO unable to use genuinely free capacity.
    #
    # This is deliberately narrower than treating every WAITING Work as free.
    # Retry/recovery/dependency waits can become runnable without a new CEO
    # decision and therefore remain capacity-consuming WAITING responsibility.
    runtime = __import__("eason_one.services.work_runtime", fromlist=["open_gates"])

    def assignment_view(row):
        return {
            "assignment_id": row.id,
            "project_id": row.work.project_id,
            "project_name": row.work.project.name if row.work.project else None,
            "work_id": row.work_id,
            "work_title": row.work.title,
            "work_state": row.work.state,
            "responsibility": row.responsibility,
            "started_at": _iso(row.started_at),
        }

    def is_quiescent_retained(row) -> bool:
        if str(row.work.state or "").upper() != "WAITING":
            return False
        gates = list(runtime.open_gates(row.work))
        return any(
            str(gate.get("condition_type") or "").upper() == "DEPENDENCY"
            and str(gate.get("issue_code") or "").startswith("MISSING_EVIDENCE:")
            for gate in gates
        )

    active_rows = [row for row in assignments if not is_quiescent_retained(row)]
    retained_rows = [row for row in assignments if is_quiescent_retained(row)]
    responsibilities = [assignment_view(row) for row in active_rows]
    retained = [assignment_view(row) for row in retained_rows]

    running_work_ids = {
        int(work_id) for (work_id,) in db.session.query(AgentRun.work_id).filter(
            AgentRun.employee_id == employee.id,
            AgentRun.status == "RUNNING",
            AgentRun.work_id.isnot(None),
        ).all()
    }
    state_rank = {"EXECUTING": 0, "VERIFYING": 1, "WAITING": 2, "READY": 3}
    responsibilities.sort(key=lambda row: (
        0 if row["work_id"] in running_work_ids else 1,
        state_rank.get(str(row["work_state"] or "").upper(), 9),
        row["assignment_id"],
    ))
    project_ids = sorted({int(row["project_id"]) for row in responsibilities})
    conflict = len(project_ids) > 1

    if not responsibilities:
        capacity = "AVAILABLE"
        primary = None
    elif conflict:
        capacity = "OVERCOMMITTED"
        primary = responsibilities[0]
    else:
        primary = responsibilities[0]
        capacity = "WAITING" if primary["work_state"] == "WAITING" else "PRIMARY_ASSIGNED"
    assert capacity in EMPLOYEE_CAPACITY_STATES
    return {
        "employee_id": employee.id,
        "name": employee.name,
        "slug": employee.slug,
        "capacity": capacity,
        "primary_responsibility": primary,
        "active_responsibilities": responsibilities,
        "retained_responsibilities": retained,
        "active_project_id": project_ids[0] if len(project_ids) == 1 else None,
        "active_project_ids": project_ids,
        "current_responsibility": primary,
        "queued_responsibilities": responsibilities[1:] if primary else [],
        "assignment_conflict": conflict,
    }


def semantic_progress(project: Project | int, *, limit: int = 12) -> dict:
    """Return conservative, replay-safe meaningful progress from durable events."""
    if isinstance(project, int):
        project = db.session.get(Project, project)
    if project is None:
        raise ValueError("PROJECT_NOT_FOUND")

    events = (
        CompanyEvent.query.filter(CompanyEvent.project_id == project.id)
        .filter(CompanyEvent.event_type.in_(tuple(_PROGRESS_EVENT_MAP)))
        .order_by(CompanyEvent.id.desc()).limit(limit).all()
    )
    rows = []
    seen = set()
    for event in events:
        progress_type = _PROGRESS_EVENT_MAP.get(event.event_type)
        if progress_type not in MEANINGFUL_PROGRESS_TYPES:
            continue
        # CompanyEvent id is append-only and authoritative.  The semantic key is
        # exposed so callers can dedupe projections across restart without ever
        # inventing a second progress fact.
        semantic_key = f"event:{event.id}:{progress_type}"
        if semantic_key in seen:
            continue
        seen.add(semantic_key)
        rows.append({
            "event_id": event.id,
            "semantic_key": semantic_key,
            "progress_type": progress_type,
            "event_type": event.event_type,
            "project_id": event.project_id,
            "work_id": event.work_id,
            "artifact_id": event.artifact_id,
            "decision_id": event.decision_id,
            "occurred_at": _iso(event.created_at),
            "basis_refs": [
                ref for ref in (
                    f"work:{event.work_id}" if event.work_id else None,
                    f"artifact:{event.artifact_id}" if event.artifact_id else None,
                    f"decision:{event.decision_id}" if event.decision_id else None,
                    f"execution:{event.execution_id}" if event.execution_id else None,
                ) if ref
            ],
            "payload": dict(event.payload_json or {}),
        })
    rows.sort(key=lambda row: row["event_id"])
    latest = rows[-1] if rows else None
    counts = Counter(row["progress_type"] for row in rows)
    return {
        "project_id": project.id,
        "latest": latest,
        "last_meaningful_progress_at": latest.get("occurred_at") if latest else None,
        "events": rows,
        "counts": dict(counts),
    }


def contract_evidence_view(project: Project | int) -> dict:
    """Project Contract evidence state, never a substitute for claim-level proof."""
    if isinstance(project, int):
        project = db.session.get(Project, project)
    if project is None:
        raise ValueError("PROJECT_NOT_FOUND")
    if not _governed_project(project):
        return {
            "state": "NOT_PROVEN",
            "overall_status": "UNAVAILABLE",
            "criteria": [],
            "integrity_error": "LEGACY_OR_UNGOVERNED_PROJECT",
        }
    try:
        evaluation = __import__(
            "eason_one.services.project_outcome", fromlist=["evaluate"]
        ).evaluate(project)
    except Exception as exc:
        return {
            "state": "NOT_PROVEN",
            "overall_status": "UNAVAILABLE",
            "criteria": [],
            "integrity_error": f"PROJECT_OUTCOME_EVALUATION_FAILED:{type(exc).__name__}",
        }

    criteria = list(evaluation.get("criteria") or [])
    statuses = [str(row.get("status") or "UNPROVEN").upper() for row in criteria]
    if statuses and all(status == "SATISFIED" for status in statuses):
        state = "SUPPORTED"
    elif any(status == "NOT_SATISFIED" for status in statuses):
        state = "CONTRADICTED"
    elif any(status == "SATISFIED" for status in statuses):
        state = "PARTIALLY_SUPPORTED"
    else:
        state = "NOT_PROVEN"
    return {
        "state": state,
        "overall_status": evaluation.get("overall_status"),
        "criteria": [{
            "criterion_id": row.get("criterion_id"),
            "criterion": row.get("criterion"),
            "status": row.get("status"),
            "method": row.get("method"),
            "work_ids": list(row.get("work_ids") or []),
        } for row in criteria],
        "integrity_error": None,
    }


def _gate_resolution(kind: str, gate: dict) -> tuple[str, str]:
    """Return (actionable_by, resolution_state) for one durable gate."""
    if kind == "DEPENDENCY":
        return "RUNTIME", "EXPECTED_WAIT"
    if kind == "CAPABILITY_GAP":
        return "CEO", "RESOLVING"
    if kind in {"FOUNDER_DECISION", "AUTHORITY_EXHAUSTED"}:
        return "FOUNDER", "NEEDS_FOUNDER"
    if kind in {"EVIDENCE_REVIEW_RETRY", "HOST_PROOF_RETRY"}:
        return "RUNTIME", "EXPECTED_WAIT" if gate.get("retry_after") else "RESOLVING"
    if kind in {"SYSTEM_RECOVERY", "RECONCILIATION", "INTERNAL_RECOVERY", "RETRY_BACKOFF"}:
        return "RUNTIME", "EXPECTED_WAIT" if gate.get("retry_after") else "RESOLVING"
    return "NONE", "ACTIVE"


def _resource_secondary(reason: str) -> bool:
    text = " ".join(str(reason or "").lower().split())
    return any(token in text for token in ("budget", "cost", "fund", "resource", "spend"))


def classify_project_blockers(project: Project | int, *, evidence_view: dict | None = None) -> dict:
    """Classify current durable Project blockers without inventing a strategy.

    The classifier is intentionally deterministic.  STRATEGY is reserved for a
    later CEO ProjectReview because current persistence can prove gates and
    missing evidence, but not safely infer that the chosen plan is conceptually
    wrong from activity alone.
    """
    if isinstance(project, int):
        project = db.session.get(Project, project)
    if project is None:
        raise ValueError("PROJECT_NOT_FOUND")

    status = str(project.status or "").upper()
    if status == "PAUSED":
        return {"primary": None, "secondary": [], "all": [], "classification_error": None}
    if status in TERMINAL_PROJECT_STATUSES:
        return {"primary": None, "secondary": [], "all": [], "classification_error": None}

    runtime = __import__("eason_one.services.work_runtime", fromlist=["project_open_gates"])
    gates = list(runtime.project_open_gates(project))
    assessments: list[dict[str, Any]] = []

    def add(blocker_type: str, *, basis: str, reason: str, actionable_by: str,
            resolution_state: str, work_id=None, source="GATE"):
        if blocker_type not in BLOCKER_TYPES:
            raise ValueError(f"UNKNOWN_BLOCKER_TYPE:{blocker_type}")
        key = (blocker_type, work_id, basis)
        if any((row["type"], row.get("work_id"), row.get("basis")) == key for row in assessments):
            return
        assessments.append({
            "type": blocker_type,
            "basis": basis,
            "reason": reason,
            "actionable_by": actionable_by,
            "resolution_state": resolution_state,
            "work_id": work_id,
            "source": source,
        })

    for gate in gates:
        kind = str(gate.get("condition_type") or "").upper()
        if kind == "FOUNDER_PAUSE":
            # Project lifecycle already owns Pause.  Pause is not a blocker.
            continue
        issue_code = str(gate.get("issue_code") or "")
        # MISSING_EVIDENCE is a very specific dependency-shaped wait.  Runtime
        # has already proved that there is no persisted evidence basis to
        # consume and will only reopen it when the evidence-basis hash changes.
        # At CEO level this is therefore an EVIDENCE blocker with no useful
        # autonomous action, not a generic dependency that should keep an
        # Employee reserved or trigger another research attempt.
        if kind == "DEPENDENCY" and issue_code.startswith("MISSING_EVIDENCE:"):
            blocker_type = "EVIDENCE"
            actionable_by, resolution_state = "NONE", "QUIESCENT"
        else:
            blocker_type = _GATE_TO_BLOCKER.get(kind)
            actionable_by = resolution_state = None
        if blocker_type is None:
            continue
        if actionable_by is None:
            actionable_by, resolution_state = _gate_resolution(kind, gate)
        add(
            blocker_type,
            basis=f"gate:{gate.get('id') or kind}",
            reason=str(gate.get("reason") or kind),
            actionable_by=actionable_by,
            resolution_state=resolution_state,
            work_id=gate.get("work_id"),
        )
        if kind == "AUTHORITY_EXHAUSTED" and _resource_secondary(gate.get("reason")):
            add(
                "RESOURCE",
                basis=f"gate:{gate.get('id') or kind}:resource",
                reason=str(gate.get("reason") or "Project resource authority is exhausted."),
                actionable_by="FOUNDER",
                resolution_state="NEEDS_FOUNDER",
                work_id=gate.get("work_id"),
            )

    governance = __import__(
        "eason_one.services.governance", fromlist=["is_founder_type"]
    )
    open_escalations = Escalation.query.filter_by(project_id=project.id, state="OPEN").order_by(Escalation.id).all()
    for escalation in open_escalations:
        if governance.is_founder_type(escalation.escalation_type):
            add(
                "AUTHORITY",
                basis=f"escalation:{escalation.id}",
                reason=escalation.reason,
                actionable_by="FOUNDER",
                resolution_state="NEEDS_FOUNDER",
                work_id=escalation.work_id,
                source="ESCALATION",
            )

    works = Work.query.filter_by(project_id=project.id).order_by(Work.id).all()
    delivery = [row for row in works if row.work_type != "MANAGEMENT"]
    executable = [row for row in delivery if row.state in {"READY", "EXECUTING", "VERIFYING"}]

    # An exhausted semantic proof attempt is evidence insufficiency, not an
    # implementation failure and must never imply replay of the producer Work.
    # Exhaustion alone does *not* prove that no new evidence basis exists.  The
    # exact MISSING_EVIDENCE:<basis> gate above owns that stronger quiescence
    # statement.  Until that gate exists, CEO ProjectReview may replan toward a
    # different evidence path, but it may not replay the abandoned producer.
    exhausted_evidence_work_ids = {
        int(row.work_id) for row in CompanyEvent.query.filter_by(
            project_id=project.id, event_type="WORK_EVIDENCE_REVIEW_EXHAUSTED"
        ).all() if row.work_id is not None
    }
    for work in delivery:
        if work.state != "ABANDONED":
            continue
        if work.id in exhausted_evidence_work_ids:
            add(
                "EVIDENCE",
                basis=f"work:{work.id}:evidence_review_exhausted",
                reason=(
                    "Independent review exhausted without sufficient semantic evidence; "
                    "implementation replay is not authorized, but CEO may replan a distinct evidence path."
                ),
                actionable_by="CEO",
                resolution_state="ACTIVE",
                work_id=work.id,
                source="WORK_OUTCOME",
            )
        elif not executable:
            add(
                "EXECUTION",
                basis=f"work:{work.id}:abandoned",
                reason="The latest bounded delivery branch is abandoned and no current delivery branch is executable.",
                actionable_by="CEO",
                resolution_state="ACTIVE",
                work_id=work.id,
                source="WORK_OUTCOME",
            )

    evidence_view = evidence_view or contract_evidence_view(project)
    evidence_state = str(evidence_view.get("state") or "NOT_PROVEN")
    all_delivery_terminal = bool(delivery) and all(
        row.state in {"ACCEPTED", "ABANDONED", "CANCELLED"} for row in delivery
    )
    if (
        evidence_state in {"NOT_PROVEN", "PARTIALLY_SUPPORTED"}
        and all_delivery_terminal
        and not executable
        and not any(row["type"] in {"AUTHORITY", "EXECUTION"} for row in assessments)
    ):
        # Do not infer "no new basis exists" from terminal Work alone.  If the
        # runtime has a canonical missing-evidence gate it is already represented
        # as QUIESCENT above.  Otherwise expose the evidence gap as CEO-actionable
        # so ProjectReview can REPLAN without replaying old Work.
        if not any(
            row["type"] == "EVIDENCE" and row.get("resolution_state") == "QUIESCENT"
            for row in assessments
        ):
            add(
                "EVIDENCE",
                basis="project_contract:insufficient_evidence",
                reason=(
                    "Current accepted Company evidence does not yet prove the Project Contract, "
                    "and no existing delivery Work is executable; CEO must replan or establish an exact quiescent evidence basis."
                ),
                actionable_by="CEO",
                resolution_state="ACTIVE",
                source="PROJECT_OUTCOME",
            )
    elif evidence_state == "CONTRADICTED" and all_delivery_terminal and not executable:
        add(
            "EVIDENCE",
            basis="project_contract:contradicted",
            reason="Current authoritative Project evidence contradicts at least one Founder success criterion.",
            actionable_by="CEO",
            resolution_state="ACTIVE",
            source="PROJECT_OUTCOME",
        )

    # Dependency waits are already represented by their gates.  If a legacy
    # Work is waiting without a readable gate, expose an integrity gap rather
    # than guessing that it is evidence, strategy, or execution failure.
    classification_error = None
    if status == "BLOCKED" and not assessments:
        classification_error = "UNCLASSIFIED_PROJECT_BLOCKED_STATE"

    def rank(row):
        # Primary means the blocker most directly preventing the next meaningful
        # Project transition, not a permanent severity hierarchy.  Existing
        # project-scoped authority/evidence facts outrank lower-level recovery
        # only when they are actually present.
        resolution_rank = {
            "NEEDS_FOUNDER": 0,
            "QUIESCENT": 1,
            "ACTIVE": 2,
            "RESOLVING": 3,
            "EXPECTED_WAIT": 4,
        }.get(row.get("resolution_state"), 9)
        source_rank = {"PROJECT_OUTCOME": 0, "ESCALATION": 1, "GATE": 2, "WORK_OUTCOME": 3}.get(row.get("source"), 8)
        return (resolution_rank, source_rank, str(row.get("basis")))

    assessments.sort(key=rank)
    primary = assessments[0] if assessments else None
    secondary = assessments[1:] if len(assessments) > 1 else []
    return {
        "primary": primary,
        "secondary": secondary,
        "all": assessments,
        "classification_error": classification_error,
    }


def _result_ready(project: Project) -> bool:
    if project.status != "REVIEW":
        return False
    try:
        return bool(__import__(
            "eason_one.services.project_outcome", fromlist=["result_ready_proof"]
        ).result_ready_proof(project))
    except Exception:
        return False


def _operating_state(project: Project, works: list[Work], blockers: dict, result_ready: bool) -> str:
    status = str(project.status or "").upper()
    if status == "PAUSED":
        return "PAUSED"
    if status in TERMINAL_PROJECT_STATUSES:
        return "TERMINAL"
    if result_ready:
        return "RESULT_READY"
    primary = blockers.get("primary")
    if primary:
        if primary.get("resolution_state") == "NEEDS_FOUNDER":
            return "NEEDS_FOUNDER"
        if primary.get("resolution_state") == "QUIESCENT":
            return "QUIESCENT"
    delivery = [row for row in works if row.work_type != "MANAGEMENT"]
    if any(row.state == "EXECUTING" for row in delivery):
        return "IN_PROGRESS"
    if any(row.state == "VERIFYING" for row in delivery):
        return "VERIFYING"
    if any(row.state == "READY" for row in delivery):
        return "READY"
    if primary and primary.get("resolution_state") == "EXPECTED_WAIT":
        return "EXPECTED_WAIT"
    if any(row.state == "WAITING" for row in delivery):
        return "EXPECTED_WAIT"
    return "QUIESCENT" if delivery else "READY"


def project_operating_view(project: Project | int) -> dict:
    if isinstance(project, int):
        project = db.session.get(Project, project)
    if project is None:
        raise ValueError("PROJECT_NOT_FOUND")

    contract = _contract_view(project)
    works = Work.query.filter_by(project_id=project.id).order_by(Work.id).all()
    progress = semantic_progress(project)
    evidence = contract_evidence_view(project)
    blockers = classify_project_blockers(project, evidence_view=evidence)
    result_ready = _result_ready(project)
    spent = _project_cost(project.id)
    budget = _money(contract.get("budget_limit_twd") or project.real_budget_limit)
    remaining = max(Decimal("0"), budget - spent)
    assignments = _active_assignments(project)

    latest_meaningful = progress.get("latest")
    return {
        "project_id": project.id,
        "name": project.name,
        "lifecycle": project.status,
        "operating_state": _operating_state(project, works, blockers, result_ready),
        "founder_priority": project.priority,
        # Slice 1 never invents a CEO portfolio priority.  Until PortfolioReview
        # exists, operating priority mirrors the durable Founder priority.
        "operating_priority": project.priority,
        "goal": contract.get("objective") or project.objective,
        "success_criteria": list(contract.get("success_criteria") or []),
        "constraints": list(contract.get("constraints") or []),
        "deadline": contract.get("deadline"),
        # current_state_summary is narrative meaning, not a durable milestone.
        # Do not relabel it as a milestone merely to fill the projection.
        "current_meaning": project.current_state_summary,
        "current_milestone": None,
        "next_milestone": project.next_milestone,
        "current_responsibilities": assignments,
        "progress": progress,
        "blockers": blockers,
        "contract_evidence": evidence,
        "verification_state": (
            "RESULT_VERIFIED" if result_ready
            else "VERIFYING" if any(row.state == "VERIFYING" for row in works)
            else "NOT_PENDING"
        ),
        "budget": {
            "limit_twd": str(budget),
            "spent_twd": str(spent),
            "remaining_twd": str(remaining),
        },
        "founder_dependency": next((
            row for row in blockers.get("all") or [] if row.get("actionable_by") == "FOUNDER"
        ), None),
        "latest_meaningful_change": latest_meaningful,
        "last_meaningful_progress_at": progress.get("last_meaningful_progress_at"),
        "result_state": "RESULT_READY" if result_ready else "NOT_READY",
        "contract_integrity_error": contract.get("integrity_error"),
    }


def project_basis_revision(project: Project | int, *, view: dict | None = None) -> str:
    """Stable revision for one Project operating view.

    Project reviews must not become stale merely because an unrelated Project
    emitted a CompanyEvent.  This fingerprint therefore scopes the basis to the
    Project's own durable projection and latest Project event.
    """
    if isinstance(project, int):
        project = db.session.get(Project, project)
    if project is None:
        raise ValueError("PROJECT_NOT_FOUND")
    if view is None:
        view = project_operating_view(project)
    latest_project_event_id = int(
        db.session.query(func.coalesce(func.max(CompanyEvent.id), 0))
        .filter(
            CompanyEvent.project_id == project.id,
            ~CompanyEvent.event_type.like("CEO_%"),
        ).scalar() or 0
    )
    body = {
        "project_id": project.id,
        "source_event_id": latest_project_event_id,
        "view": view,
    }
    return f"project:{project.id}:event:{latest_project_event_id}:state:{_stable_hash(body)[:16]}"


def _open_founder_decisions(project_ids: list[int]) -> list[dict]:
    if not project_ids:
        return []
    governance = __import__(
        "eason_one.services.governance", fromlist=["is_founder_type"]
    )
    rows = Escalation.query.filter(
        Escalation.project_id.in_(project_ids), Escalation.state == "OPEN"
    ).order_by(Escalation.id).all()
    result = []
    for row in rows:
        if not governance.is_founder_type(row.escalation_type):
            continue
        result.append({
            "escalation_id": row.id,
            "project_id": row.project_id,
            "work_id": row.work_id,
            "type": row.escalation_type,
            "reason": row.reason,
            "recommendation": row.recommendation,
            "created_at": _iso(row.created_at),
        })
    return result


def company_operating_snapshot(*, include_terminal: bool = False) -> dict:
    """Build an immutable CEO-facing snapshot from current durable Company truth."""
    query = Project.query.filter(Project.environment == "LIVE")
    if not include_terminal:
        query = query.filter(Project.status.in_(OPERATING_PROJECT_STATUSES))
    projects = query.order_by(Project.id).all()
    project_views = [project_operating_view(project) for project in projects]

    employees = Employee.query.filter_by(active=True).order_by(Employee.id).all()
    employee_views = [employee_capacity_view(employee) for employee in employees]

    company_api = __import__(
        "eason_one.services.company", fromlist=["get_company", "spent", "remaining"]
    )
    company = company_api.get_company()
    company_resources = {
        "currency": company.currency,
        "budget_limit": str(_money(company.real_budget_limit)),
        "spent": str(_money(company_api.spent())),
        "remaining": str(_money(company_api.remaining())),
    }
    founder_decisions = _open_founder_decisions([project.id for project in projects])
    results_ready = [
        {"project_id": row["project_id"], "name": row["name"]}
        for row in project_views if row["result_state"] == "RESULT_READY"
    ]
    recent_progress = []
    for row in project_views:
        latest = row.get("latest_meaningful_change")
        if latest:
            recent_progress.append({"project_id": row["project_id"], **latest})
    recent_progress.sort(key=lambda row: row.get("event_id") or 0, reverse=True)
    recent_progress = recent_progress[:12]

    revision_body = {
        "source_event_id": _latest_event_id(),
        "projects": project_views,
        "employees": employee_views,
        "founder_decisions": founder_decisions,
        "results_ready": results_ready,
        "company_resources": company_resources,
    }
    revision_hash = _stable_hash(revision_body)
    return {
        "revision": f"event:{revision_body['source_event_id']}:state:{revision_hash[:16]}",
        "source_event_id": revision_body["source_event_id"],
        "observed_at": _iso(__import__("eason_one.models", fromlist=["now"]).now()),
        "projects": project_views,
        "employees": employee_views,
        "founder_decisions": founder_decisions,
        "results_ready": results_ready,
        "company_resources": company_resources,
        "recent_meaningful_changes": recent_progress,
    }
