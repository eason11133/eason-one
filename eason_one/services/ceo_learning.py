"""Founder-governed CEO learning and outcome history.

The CEO may learn from persisted Mission episodes, but learning never mutates
Founder authority or active policy silently. Terminal Missions create an
immutable research episode plus an unvalidated CEO learning proposal. Only
validated EmployeeLearningRecord rows are injected into future CEO context.
"""
from __future__ import annotations

from decimal import Decimal
import json
import re
from typing import Any

from sqlalchemy import func

from ..extensions import db
from ..models import (
    AgentRun, ContributionEvent, CostEvent, Employee, EmployeeLearningRecord,
    Meeting, Operation, OperationEvent, ResearchRecord, Task, WorkMessage, now,
)

BASELINE_POLICY = {
    "version": "ceo-policy-v1.0",
    "principles": [
        "Own the Founder objective and completion criteria, not merely Task completion.",
        "Use the smallest sufficient organization; do not add managers or reviewers by default.",
        "Escalate to a Meeting only for material conflict, cross-specialty dependency, or explicit Founder request.",
        "Preserve a completion reserve for verification and final delivery before discretionary work.",
        "For repository work, delegate to Engineer, who invokes Codex and validates diff/tests inside the approved boundary.",
        "Use relevant persisted History with provenance; never treat model output as authoritative fact automatically.",
        "Stay inside Founder-approved cost, time, tool, and risk boundaries. Ask only when authority is genuinely required.",
    ],
}

CEO_POLICY_LEARNING_TYPE = "CEO_POLICY_LEARNING"
CEO_POLICY_PROMOTION_BASIS = "FOUNDER_PROMOTED_OUTCOME_LEARNING"
_CEO_POLICY_CANDIDATE_TYPES = {
    "CEO_POLICY_CANDIDATE",
    "CEO_DECISION_OUTCOME_CANDIDATE",
}
_OBSERVABLE_CEO_DECISION_OUTCOMES = {
    "OBSERVED_ACCEPTED",
    "OBSERVED_NONACCEPTED",
    "OBSERVED_PROJECT_TERMINAL_CONTEXT",
    "OBSERVED_FOUNDER_GATE_STATE",
    "OBSERVED_CONTINUATION_LINEAGE",
    "OBSERVED_VERIFICATION_LINEAGE",
    "OBSERVED_RESULT_READY",
    "OBSERVED_NO_DOWNSTREAM_EFFECT",
}


def _superseded_by_learning_id(record: EmployeeLearningRecord) -> int | None:
    value = (record.evidence_json or {}).get("superseded_by_learning_id")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _active_ceo_policy_record(record: EmployeeLearningRecord) -> bool:
    """Return True only for Founder-approved advisory CEO policy memory.

    Older v0.12 policy candidates remain compatible after explicit Founder
    validation.  New Project-closure decision-outcome candidates are *never*
    injected directly: observing an outcome is not the same thing as proving a
    causal, reusable policy.
    """
    if not record.validated or _superseded_by_learning_id(record) is not None:
        return False
    if (
        record.learning_type == "CEO_POLICY_CANDIDATE"
        and record.validation_basis == "FOUNDER"
    ):
        return True
    return (
        record.learning_type == CEO_POLICY_LEARNING_TYPE
        and record.validation_basis == CEO_POLICY_PROMOTION_BASIS
    )


def _money(value: Any) -> Decimal:
    return Decimal(str(value or 0))


def _terms(text: str | None) -> set[str]:
    return {
        token for token in re.findall(r"[A-Za-z0-9_\-]{4,}|[\u4e00-\u9fff]{2,}", (text or "").casefold())
        if token not in {"mission", "operation", "project", "founder", "eason", "company"}
    }


def active_policy_context(ceo: Employee, *, limit: int = 8) -> tuple[str, dict]:
    candidates = (
        EmployeeLearningRecord.query.filter_by(employee_id=ceo.id, validated=True)
        .order_by(EmployeeLearningRecord.created_at.desc(), EmployeeLearningRecord.id.desc())
        .limit(max(40, int(limit) * 6)).all()
    )
    rows = [row for row in candidates if _active_ceo_policy_record(row)][:max(1, int(limit))]
    lines = [f"ACTIVE CEO POLICY {BASELINE_POLICY['version']}"]
    lines.extend(f"- {item}" for item in BASELINE_POLICY["principles"])
    if rows:
        lines.append("FOUNDER-VALIDATED CEO LEARNINGS")
        for row in reversed(rows):
            lines.append(f"- LEARNING #{row.id} {row.title}: {row.content}")
        lines.append(
            "LEARNING BOUNDARY — these learnings are advisory precedent only. "
            "Current durable Company truth, Founder directives, Project Contract, "
            "Evidence, Governance and Verification always outrank memory."
        )
    return "\n".join(lines), {
        "baseline_version": BASELINE_POLICY["version"],
        "validated_learning_ids": [row.id for row in rows],
        "authority_effect": False,
        "budget_effect": False,
        "current_fact_effect": False,
    }


def active_policy_advisory(ceo: Employee | None = None, *, limit: int = 8) -> dict:
    """Return the promoted CEO policy context as bounded advisory judgement input.

    This is the only structured retrieval seam used by CEO reviews/portfolio
    judgement.  It deliberately carries explicit *non-authority* metadata so a
    promoted learning can influence reasoning provenance without becoming a
    current fact, budget grant, capability claim, Founder directive, or
    Governance permission.
    """
    ceo = ceo or Employee.query.filter_by(slug="ceo", active=True).first()
    if ceo is None:
        return {
            "context": f"ACTIVE CEO POLICY {BASELINE_POLICY['version']}",
            "baseline_version": BASELINE_POLICY["version"],
            "validated_learning_ids": [],
            "authority_effect": False,
            "budget_effect": False,
            "capability_effect": False,
            "current_fact_effect": False,
            "precedence": (
                "Current durable Company truth, Founder directives, Project Contract, "
                "Evidence, Governance and Verification outrank advisory memory."
            ),
        }
    context, metadata = active_policy_context(ceo, limit=limit)
    return {
        "context": context,
        **metadata,
        "capability_effect": False,
        "precedence": (
            "Current durable Company truth, Founder directives, Project Contract, "
            "Evidence, Governance and Verification outrank advisory memory."
        ),
    }


def similar_episode_context(founder_request: str | None, *, limit: int = 4) -> tuple[str, dict]:
    request_terms = _terms(founder_request)
    candidates = (
        ResearchRecord.query.filter_by(record_type="EXPERIMENT")
        .filter(ResearchRecord.record_key.like("CEO-EPISODE-%"))
        .order_by(ResearchRecord.created_at.desc(), ResearchRecord.id.desc())
        .limit(30).all()
    )
    ranked = []
    for row in candidates:
        haystack = " ".join([row.title or "", row.summary or "", json.dumps(row.metadata_json or {}, ensure_ascii=False)])
        overlap = len(request_terms.intersection(_terms(haystack))) if request_terms else 0
        accepted = bool((row.metadata_json or {}).get("founder_accepted"))
        ranked.append((overlap, accepted, row.id, row))
    ranked.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
    selected = [row for overlap, _, _, row in ranked if overlap > 0][:limit]
    lines = []
    for row in reversed(selected):
        meta = row.metadata_json or {}
        lines.append(
            f"CEO EPISODE {row.record_key}: {row.summary}\n"
            f"Route: {meta.get('route_type', '-')} | Cost: TWD {meta.get('actual_cost_twd', '0')} "
            f"| Calls: {meta.get('provider_calls', 0)} | Result: {meta.get('terminal_status', '-')}"
        )
    return "\n\n".join(lines), {"episode_keys": [row.record_key for row in selected]}


def _task_rows(operation: Operation) -> list[dict]:
    rows = []
    for task in operation.tasks:
        runs = AgentRun.query.filter_by(operation_id=operation.id, task_id=task.id).order_by(AgentRun.id).all()
        rows.append({
            "task_id": task.id,
            "title": task.title,
            "status": task.status,
            "assignee_employee_id": task.assigned_employee_id,
            "reviewer_employee_id": task.reviewer_employee_id,
            "result_summary": task.result_summary,
            "run_ids": [run.id for run in runs],
            "provider_calls": sum(1 for run in runs if run.provider_response_id or run.real_cost),
            "cost_twd": str(sum((_money(run.real_cost) for run in runs), Decimal("0"))),
        })
    return rows


def _meeting_rows(operation: Operation) -> list[dict]:
    rows = []
    for meeting in Meeting.query.filter_by(operation_id=operation.id).order_by(Meeting.id).all():
        rows.append({
            "meeting_id": meeting.id,
            "status": meeting.status,
            "kernel_status": meeting.kernel_status,
            "profile": meeting.execution_profile,
            "rounds": meeting.current_round,
            "message_count": meeting.message_count,
            "budget_twd": str(meeting.real_cost_limit_twd or 0),
            "minutes": meeting.minutes_json,
            "termination_reason": meeting.termination_reason,
        })
    return rows


def _lesson_text(operation: Operation, episode: dict) -> str:
    tasks = episode["tasks"]
    meetings = episode["meetings"]
    review_runs = AgentRun.query.filter_by(operation_id=operation.id, purpose="TASK_REVIEW").count()
    revisions = int(operation.revision_count or 0)
    lessons = [
        f"Mission #{operation.id} used route {operation.route_type} and ended {episode['terminal_status']} at TWD {episode['actual_cost_twd']}.",
    ]
    if review_runs > 1 or revisions > 1:
        lessons.append(
            "Repeated review/revision occurred. Future similar work should require a reviewer only when risk or uncertainty justifies the extra call, with a deterministic revision ceiling."
        )
    if meetings:
        completed = any(row["kernel_status"] == "COMPLETED" for row in meetings)
        lessons.append(
            "The Meeting escalation completed and should be compared against Artifact quality and cost."
            if completed else
            "A planned Meeting did not complete; future routing must either start it after inputs are ready or deterministically skip/cancel it with an explicit reason."
        )
    if all(task["status"] == "DONE" for task in tasks) and episode["terminal_status"] != "COMPLETED":
        lessons.append(
            "Task completion did not equal objective completion. Preserve verification and final-delivery authority before discretionary work."
        )
    if not lessons[1:]:
        lessons.append("Retain this episode as outcome-linked evidence; do not generalize a new policy until the Founder validates the lesson.")
    return " ".join(lessons)


def capture_operation_episode(operation: Operation, *, commit: bool = True) -> ResearchRecord | None:
    """Capture one idempotent terminal real-work CEO training episode."""
    kind = __import__(
        "eason_one.services.stabilization", fromlist=["operation_kind"]
    ).operation_kind(operation)
    if kind != "REAL_WORK":
        return None
    key = f"CEO-EPISODE-{operation.id}"
    existing = ResearchRecord.query.filter_by(record_key=key).first()
    if existing:
        return existing
    terminal_status = (
        __import__("eason_one.services.operation_kernel", fromlist=["authoritative_status"])
        .authoritative_status(operation)
    )
    if terminal_status not in {"COMPLETED", "FAILED", "CANCELLED"}:
        return None
    actual = db.session.query(func.coalesce(func.sum(CostEvent.real_cost_delta), 0)).filter_by(
        operation_id=operation.id
    ).scalar()
    provider_calls = AgentRun.query.filter(
        AgentRun.operation_id == operation.id,
        AgentRun.purpose != "CEO_FOUNDER_REQUEST",
        (AgentRun.provider_response_id.isnot(None)) | (AgentRun.real_cost.isnot(None)),
    ).count()
    events = OperationEvent.query.filter_by(operation_id=operation.id).order_by(OperationEvent.sequence).all()
    founder_actions = [
        {
            "event_type": event.event_type,
            "actor_ref": event.actor_ref,
            "payload": event.payload_json,
        }
        for event in events if event.actor_type == "FOUNDER"
    ]
    task_ids = [task.id for task in operation.tasks]
    contribution_query = ContributionEvent.query
    if task_ids:
        contribution_query = contribution_query.filter(ContributionEvent.related_task_id.in_(task_ids))
    elif operation.project_id:
        contribution_query = contribution_query.filter_by(project_id=operation.project_id)
    else:
        contribution_query = contribution_query.filter(ContributionEvent.id == -1)
    contributions = [
        {
            "event_id": row.id,
            "employee_id": row.employee_id,
            "task_id": row.related_task_id,
            "event_type": row.event_type,
            "status": row.status,
            "value": str(row.value),
            "reason": row.reason,
            "related_reference": row.related_reference,
        }
        for row in contribution_query.order_by(ContributionEvent.id).all()
    ]
    report = operation.founder_report_json or {}
    episode = {
        "schema": "ceo-training-episode-v1",
        "operation_id": operation.id,
        "project_id": operation.project_id,
        "objective": operation.objective,
        "route_type": operation.route_type,
        "route_reason": operation.route_reason,
        "terminal_status": terminal_status,
        "approved_budget_twd": str(operation.approved_budget_twd or 0),
        "actual_cost_twd": str(actual or 0),
        "provider_calls": provider_calls,
        "revision_count": int(operation.revision_count or 0),
        "tasks": _task_rows(operation),
        "meetings": _meeting_rows(operation),
        "founder_actions": founder_actions,
        "contribution_events": contributions,
        "founder_report": report,
        "founder_accepted": bool(report.get("founder_accepted") or report.get("accepted_at")),
        "policy_version": BASELINE_POLICY["version"],
    }
    summary = _lesson_text(operation, episode)
    record = ResearchRecord(
        record_key=key,
        record_type="EXPERIMENT",
        title=f"CEO training episode — Mission #{operation.id}: {operation.title}"[:220],
        summary=summary,
        status="CLOSED",
        version_label="v0.12.2",
        project_id=operation.project_id,
        operation_id=operation.id,
        source_ref=f"Operation #{operation.id}; authoritative events and cost ledger",
        metadata_json=episode,
        founder_approved=False,
    )
    db.session.add(record)
    memory = dict(operation.memory_json or {})
    memory["ceo_training_episode"] = {"record_key": key, "summary": summary, "policy_version": BASELINE_POLICY["version"]}
    operation.memory_json = memory
    ceo = db.session.get(Employee, operation.proposed_by_employee_id)
    if ceo:
        proposal = EmployeeLearningRecord(
            employee_id=ceo.id,
            project_id=operation.project_id,
            learning_type="CEO_POLICY_CANDIDATE",
            title=f"CEO policy candidate from Mission #{operation.id}"[:180],
            content=summary,
            source_ref=key,
            validated=False,
        )
        db.session.add(proposal)
    if commit:
        db.session.commit()
    return record


def validate_learning(record: EmployeeLearningRecord, *, commit: bool = True) -> EmployeeLearningRecord:
    record.validated = True
    record.validation_basis = "FOUNDER"
    if commit:
        db.session.commit()
    return record


def promote_ceo_learning(
    record: EmployeeLearningRecord,
    *,
    scope: str,
    policy_statement: str | None = None,
    founder_authorized: bool = False,
    commit: bool = True,
) -> EmployeeLearningRecord:
    """Promote one outcome-backed CEO candidate into advisory policy memory.

    Promotion is intentionally explicit and Founder-gated.  A Project closure
    may stage a factual candidate automatically, but it can never promote that
    candidate into reusable CEO policy by itself.  The promoted row is a new
    immutable memory fact so the source candidate/history remains intact.
    """
    if not founder_authorized:
        raise ValueError("FOUNDER_AUTHORIZATION_REQUIRED_FOR_CEO_LEARNING_PROMOTION")
    scope = str(scope or "").strip().upper()
    if scope not in {"ROLE", "COMPANY"}:
        raise ValueError("CEO_POLICY_SCOPE_MUST_BE_ROLE_OR_COMPANY")
    ceo = Employee.query.filter_by(slug="ceo", active=True).first()
    if ceo is None or int(record.employee_id) != int(ceo.id):
        raise ValueError("CEO_POLICY_PROMOTION_REQUIRES_ACTIVE_CEO_CANDIDATE")
    if record.learning_type not in _CEO_POLICY_CANDIDATE_TYPES:
        raise ValueError("UNSUPPORTED_CEO_POLICY_CANDIDATE")

    source_evidence = dict(record.evidence_json or {})
    if record.learning_type == "CEO_DECISION_OUTCOME_CANDIDATE":
        if source_evidence.get("causal_claim") is not False:
            raise ValueError("CEO_DECISION_OUTCOME_CANDIDATE_MUST_REMAIN_NONCAUSAL")
        if source_evidence.get("outcome") not in _OBSERVABLE_CEO_DECISION_OUTCOMES:
            raise ValueError("CEO_DECISION_OUTCOME_NOT_OBSERVABLE")
        if not source_evidence.get("observed_basis_hash"):
            raise ValueError("CEO_DECISION_OUTCOME_BASIS_REQUIRED")
        if not str(policy_statement or "").strip():
            raise ValueError("CEO_DECISION_OUTCOME_POLICY_STATEMENT_REQUIRED")

    statement = str(policy_statement or record.content or "").strip()
    if not statement:
        raise ValueError("CEO_POLICY_STATEMENT_REQUIRED")
    source_ref = f"CEO_POLICY_PROMOTION:{record.id}:{scope}"
    existing = EmployeeLearningRecord.query.filter_by(
        employee_id=ceo.id,
        learning_type=CEO_POLICY_LEARNING_TYPE,
        source_ref=source_ref,
    ).first()
    if existing is not None:
        return existing

    promoted = EmployeeLearningRecord(
        employee_id=ceo.id,
        project_id=record.project_id,
        task_id=record.task_id,
        work_id=record.work_id,
        artifact_version_id=record.artifact_version_id,
        verification_record_id=record.verification_record_id,
        learning_type=CEO_POLICY_LEARNING_TYPE,
        validation_basis=CEO_POLICY_PROMOTION_BASIS,
        title=(f"CEO {scope.lower()} learning — {record.title}")[:180],
        content=statement,
        source_ref=source_ref,
        evidence_json={
            "schema": "ceo-promoted-policy-learning-v1",
            "scope": scope,
            "source_learning_id": record.id,
            "source_learning_type": record.learning_type,
            "source_validation_basis": record.validation_basis,
            "source_ref": record.source_ref,
            "source_evidence": source_evidence,
            "founder_authorized": True,
            "authority_effect": False,
            "budget_effect": False,
            "capability_effect": False,
            "current_fact_effect": False,
        },
        validated=True,
    )
    db.session.add(promoted)
    db.session.flush()
    if commit:
        db.session.commit()
    return promoted


def supersede_ceo_policy_learning(
    record: EmployeeLearningRecord,
    replacement: EmployeeLearningRecord,
    *,
    reason: str,
    founder_authorized: bool = False,
    commit: bool = True,
) -> EmployeeLearningRecord:
    """Retire advisory CEO memory without rewriting its historical content.

    Supersession is Founder-gated and cycle-safe.  The old row is retained for
    audit but excluded from normal active-policy retrieval.
    """
    if not founder_authorized:
        raise ValueError("FOUNDER_AUTHORIZATION_REQUIRED_FOR_CEO_LEARNING_SUPERSESSION")
    if int(record.id or 0) == int(replacement.id or 0):
        raise ValueError("CEO_POLICY_CANNOT_SUPERSEDE_ITSELF")
    if int(record.employee_id) != int(replacement.employee_id):
        raise ValueError("CEO_POLICY_SUPERSESSION_EMPLOYEE_MISMATCH")
    if not _active_ceo_policy_record(record) or not _active_ceo_policy_record(replacement):
        raise ValueError("CEO_POLICY_SUPERSESSION_REQUIRES_ACTIVE_POLICY_ROWS")
    if not str(reason or "").strip():
        raise ValueError("CEO_POLICY_SUPERSESSION_REASON_REQUIRED")

    # If replacement already points (directly or indirectly) back to record,
    # record -> replacement would create an advisory-memory cycle.
    seen: set[int] = set()
    cursor = replacement
    while cursor is not None and cursor.id not in seen:
        if int(cursor.id) == int(record.id):
            raise ValueError("CEO_POLICY_SUPERSESSION_CYCLE")
        seen.add(int(cursor.id))
        next_id = _superseded_by_learning_id(cursor)
        cursor = db.session.get(EmployeeLearningRecord, next_id) if next_id else None

    evidence = dict(record.evidence_json or {})
    evidence.update({
        "superseded_by_learning_id": replacement.id,
        "supersession_reason": str(reason).strip(),
        "superseded_at": now().isoformat(),
    })
    record.evidence_json = evidence
    if commit:
        db.session.commit()
    return record

# ---------------------------------------------------------------------------
# Company Core v0.20 Project closure / outcome-backed learning projection
# ---------------------------------------------------------------------------
#
# The legacy functions above preserve the older Operation/Mission learning
# contract.  The functions below operate on current Project/Work truth only.
# They intentionally do not call a provider and do not auto-promote free-form
# policy.  Accepted Work experience remains owned by employee_memory /
# employee_evolution; Project closure simply captures authoritative facts and
# direct CEO action outcomes without rewriting history.

PROJECT_CLOSURE_EVENT_TYPE = "CEO_PROJECT_CLOSURE_CAPTURED"
PROJECT_CLOSURE_SCHEMA = "CEO_PROJECT_CLOSURE_V1"
_TERMINAL_PROJECT_STATUSES = frozenset({"COMPLETED", "CANCELLED", "FAILED"})
_TERMINAL_SOURCE_EVENTS = frozenset({
    "FOUNDER_PROJECT_COMPLETED",
    "PROJECT_FAILED",
    "PROJECT_CANCELLED_BY_FOUNDER",
    "PROJECT_CANCELLED",
})


def _closure_jsonable(value: Any):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _closure_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_closure_jsonable(item) for item in value]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _closure_hash(payload: dict[str, Any]) -> str:
    import hashlib
    body = json.dumps(
        _closure_jsonable(payload), ensure_ascii=False, sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _founder_completion_observation(project, *, source_event=None) -> dict[str, Any] | None:
    """Observe the canonical Founder completion authority without creating it.

    Project closure/learning is downstream evidence only. For COMPLETED Projects
    it records whether the terminal lifecycle is actually anchored to the
    first-class Founder Decision and its matching terminal source event. Legacy
    or manually-mutated rows remain observable but are explicitly noncanonical.
    """
    if str(project.status or "").upper() != "COMPLETED":
        return None
    from ..models import CompanyEvent, Decision
    decision = Decision.query.filter_by(
        legacy_source="PROJECT_RESULT_ACCEPTANCE", legacy_source_id=project.id
    ).first()
    try:
        basis = json.loads(getattr(decision, "authority_basis", None) or "{}") if decision else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        basis = {}
    if not isinstance(basis, dict):
        basis = {}

    event = None
    if (
        source_event is not None
        and source_event.event_type == "FOUNDER_PROJECT_COMPLETED"
        and getattr(source_event, "project_id", None) == project.id
        and decision is not None
        and getattr(source_event, "decision_id", None) == decision.id
    ):
        event = source_event
    elif decision is not None:
        event = CompanyEvent.query.filter_by(
            event_type="FOUNDER_PROJECT_COMPLETED",
            project_id=project.id,
            decision_id=decision.id,
        ).order_by(CompanyEvent.id).first()

    payload = dict(getattr(event, "payload_json", None) or {}) if event else {}
    expected = {
        "project_contract_hash": basis.get("governing_contract_hash"),
        "project_authority_hash": basis.get("budget_authority_hash"),
        "project_result_artifact_id": basis.get("project_result_artifact_id"),
        "project_result_artifact_version_id": basis.get("project_result_artifact_version_id"),
        "project_result_verification_id": basis.get("project_result_verification_id"),
    }
    canonical = bool(
        decision
        and decision.state == "COMMITTED"
        and decision.decision == "ACCEPT_AND_COMPLETE"
        and basis.get("version") == "FOUNDER_PROJECT_COMPLETION_V1"
        and basis.get("authority") == "FOUNDER"
        and event
        and all(payload.get(key) == value for key, value in expected.items())
    )
    return {
        "schema": "CEO_FOUNDER_COMPLETION_OBSERVATION_V1",
        "canonical": canonical,
        "completion_decision_id": getattr(decision, "id", None),
        "completion_event_id": getattr(event, "id", None),
        "decision_state": getattr(decision, "state", None),
        "decision_value": getattr(decision, "decision", None),
        "authority_basis_version": basis.get("version"),
        "project_contract_hash": basis.get("governing_contract_hash"),
        "project_authority_hash": basis.get("budget_authority_hash"),
        "project_result_artifact_id": basis.get("project_result_artifact_id"),
        "project_result_artifact_version_id": basis.get("project_result_artifact_version_id"),
        "project_result_verification_id": basis.get("project_result_verification_id"),
        "learning_authority_effect": False,
    }


def ceo_action_outcomes_for_project(project) -> list[dict[str, Any]]:
    """Project direct, observable outcomes for settled CEO action intents.

    This does *not* claim that a CEO decision was globally good or causally
    responsible for the final Project result.  It only joins a durable CEO
    action receipt to the current Work outcome created by that action.
    """
    from ..models import AgentRun, CompanyEvent, Decision, Escalation, Operation, VerificationRecord, Work
    management = __import__(
        "eason_one.services.ceo_management", fromlist=["ACTION_RECEIPT_EVENT_TYPE"]
    )
    rows = CompanyEvent.query.filter_by(
        event_type=management.ACTION_RECEIPT_EVENT_TYPE,
        project_id=project.id,
    ).order_by(CompanyEvent.id).all()
    results: list[dict[str, Any]] = []
    for row in rows:
        payload = dict(row.payload_json or {})
        work_id = payload.get("work_id")
        work = db.session.get(Work, int(work_id)) if work_id else None
        decision_id = payload.get("decision_id") or row.decision_id
        decision = db.session.get(Decision, int(decision_id)) if decision_id else None
        action_type = str(payload.get("action_type") or "")
        state = str(getattr(work, "state", "") or "").upper()
        observed = "NOT_YET_OBSERVABLE"
        observation_summary = None
        observation_details: dict[str, Any] = {}

        if work is not None:
            if state == "ACCEPTED":
                observed = "OBSERVED_ACCEPTED"
            elif state in {"ABANDONED", "CANCELLED"}:
                observed = "OBSERVED_NONACCEPTED"
            observation_summary = (
                f"Work #{work.id} is currently {state or 'UNKNOWN'} after the CEO action receipt."
            )
            observation_details = {"observed_work_state": state or None}
        elif action_type in {"SURFACE_FOUNDER_ESCALATION", "RECOMMEND_PROJECT_STOP"}:
            escalation_id = payload.get("escalation_id")
            escalation = db.session.get(Escalation, int(escalation_id)) if escalation_id else None
            if escalation is not None:
                observed = "OBSERVED_FOUNDER_GATE_STATE"
                observation_summary = (
                    f"Founder gate #{escalation.id} is {escalation.state}; resolution is "
                    f"{escalation.resolution or 'not recorded'}."
                )
                observation_details = {
                    "escalation_id": escalation.id,
                    "escalation_type": escalation.escalation_type,
                    "escalation_state": escalation.state,
                    "escalation_resolution": escalation.resolution,
                }
        elif action_type == "AUTHORIZE_PROJECT_REPLAN" and decision_id:
            linked = [
                op for op in Operation.query.filter_by(project_id=project.id).order_by(Operation.id).all()
                if int((op.memory_json or {}).get("ceo_management_decision_id") or 0) == int(decision_id)
            ]
            observed = "OBSERVED_CONTINUATION_LINEAGE" if linked else "OBSERVED_NO_DOWNSTREAM_EFFECT"
            observation_summary = (
                f"{len(linked)} continuation Mission(s) are durably linked to CEO Decision #{decision_id}."
                if linked
                else f"No continuation Mission is durably linked to CEO Decision #{decision_id} at Project closure."
            )
            observation_details = {
                "continuation_operation_ids": [op.id for op in linked],
                "continuation_operation_statuses": [
                    {"operation_id": op.id, "status": op.status, "kernel_status": op.kernel_status}
                    for op in linked
                ],
            }
        elif action_type == "REQUEST_PROJECT_VERIFICATION" and decision_id:
            linked_runs = [
                run for run in AgentRun.query.filter_by(
                    project_id=project.id, purpose="PROJECT_OUTCOME_REVIEW"
                ).order_by(AgentRun.id).all()
                if int((run.context_composition_json or {}).get("ceo_management_decision_id") or 0) == int(decision_id)
            ]
            run_ids = [run.id for run in linked_runs]
            verifications = (
                VerificationRecord.query.filter(VerificationRecord.agent_run_id.in_(run_ids))
                .order_by(VerificationRecord.id).all()
                if run_ids else []
            )
            observed = "OBSERVED_VERIFICATION_LINEAGE" if linked_runs else "OBSERVED_NO_DOWNSTREAM_EFFECT"
            observation_summary = (
                f"{len(linked_runs)} independent Project outcome review run(s) are durably linked to CEO Decision #{decision_id}."
                if linked_runs
                else f"No independent Project outcome review run is durably linked to CEO Decision #{decision_id} at Project closure."
            )
            observation_details = {
                "project_outcome_review_run_ids": run_ids,
                "project_outcome_review_runs": [
                    {"run_id": run.id, "status": run.status, "outcome": run.outcome}
                    for run in linked_runs
                ],
                "verification_record_ids": [verification.id for verification in verifications],
                "verification_statuses": [
                    {
                        "verification_id": verification.id,
                        "method": verification.method,
                        "status": verification.status,
                    }
                    for verification in verifications
                ],
            }
        elif action_type == "REQUEST_PROJECT_OUTCOME_CHECK" and decision_id:
            result_events = [
                event for event in CompanyEvent.query.filter_by(
                    project_id=project.id, event_type="PROJECT_RESULT_READY"
                ).order_by(CompanyEvent.id).all()
                if int((event.payload_json or {}).get("ceo_management_decision_id") or 0) == int(decision_id)
            ]
            observed = "OBSERVED_RESULT_READY" if result_events else "OBSERVED_NO_DOWNSTREAM_EFFECT"
            observation_summary = (
                f"The deterministic Result Ready gate produced {len(result_events)} Project Result Ready event(s) linked to CEO Decision #{decision_id}."
                if result_events
                else f"No Project Result Ready event is durably linked to CEO Decision #{decision_id} at Project closure."
            )
            observation_details = {
                "project_result_ready_event_ids": [event.id for event in result_events],
            }
        elif action_type == "DOCUMENT_QUIESCENCE":
            observed = "OBSERVED_PROJECT_TERMINAL_CONTEXT"
            observation_summary = (
                f"Project #{project.id} later reached terminal status {str(project.status or '').upper()} after the quiescence decision; causality is not inferred."
            )
        elif action_type:
            observed = "OBSERVED_PROJECT_TERMINAL_CONTEXT"
            observation_summary = (
                f"Project #{project.id} later reached terminal status {str(project.status or '').upper()} after CEO action {action_type}; causality is not inferred."
            )

        authority_guardrails = {
            "project_twd_authority_changed": bool(payload.get("project_twd_authority_changed")),
            "provider_execution_created_by_receipt": bool(payload.get("provider_execution_created")),
            "project_terminated_by_receipt": bool(payload.get("project_terminated")),
            "project_result_ready_declared_by_receipt": bool(payload.get("project_result_ready_declared")),
            "project_completed_by_receipt": bool(payload.get("project_completed")),
        }
        results.append({
            "intent_id": payload.get("intent_id") or row.correlation_id,
            "receipt_event_id": row.id,
            "decision_id": decision_id,
            "decision_selected": getattr(decision, "decision", None),
            "decision_rationale": getattr(decision, "rationale", None),
            "action_type": action_type or None,
            "work_id": work_id,
            "from_employee_id": payload.get("from_employee_id"),
            "employee_id": payload.get("employee_id"),
            "required_capability": payload.get("required_capability"),
            "receipt_status": payload.get("status"),
            "observed_work_state": state or None,
            "project_terminal_status": str(project.status or "").upper(),
            "observation_summary": observation_summary,
            "observation_details": observation_details,
            "authority_guardrails": authority_guardrails,
            "performance_assessment": {
                "score": None,
                "decision_basis_quality": "NOT_SCORED",
                "actual_outcome": observed,
                "cost_attribution": "NOT_ATTRIBUTED",
                "risk_attribution": "NOT_ATTRIBUTED",
            },
            "outcome": observed,
            "causal_claim": False,
        })
    return results


def project_closure_facts(project, *, source_event=None) -> dict[str, Any]:
    """Build deterministic terminal Project closure facts from durable truth."""
    from ..models import (
        Artifact,
        ArtifactVersion,
        CompanyEvent,
        ContributionEvent,
        CostEvent,
        Decision,
        EmployeeLearningRecord,
        VerificationRecord,
        Work,
        WorkAssignment,
    )
    status = str(project.status or "").upper()
    if status not in _TERMINAL_PROJECT_STATUSES:
        raise ValueError("PROJECT_CLOSURE_REQUIRES_TERMINAL_PROJECT")

    works = Work.query.filter_by(project_id=project.id).order_by(Work.id).all()
    work_ids = [row.id for row in works]
    accepted_ids = [row.id for row in works if row.state == "ACCEPTED"]
    unresolved_ids = [
        row.id for row in works
        if row.state in {"PROPOSED", "READY", "EXECUTING", "WAITING", "VERIFYING"}
    ]
    open_assignment_ids: list[int] = []
    if work_ids:
        open_assignment_ids = [
            int(value) for (value,) in db.session.query(WorkAssignment.id).filter(
                WorkAssignment.work_id.in_(work_ids),
                WorkAssignment.ended_at.is_(None),
            ).order_by(WorkAssignment.id).all()
        ]

    cost = db.session.query(func.coalesce(func.sum(CostEvent.real_cost_delta), 0)).filter(
        CostEvent.project_id == project.id
    ).scalar()
    decision_ids = [
        int(value) for (value,) in db.session.query(Decision.id).filter(
            Decision.project_id == project.id
        ).order_by(Decision.id).all()
    ]
    contribution_ids = [
        int(value) for (value,) in db.session.query(ContributionEvent.id).filter(
            ContributionEvent.project_id == project.id
        ).order_by(ContributionEvent.id).all()
    ]
    canonical_learning_ids = [
        int(value) for (value,) in db.session.query(EmployeeLearningRecord.id).filter(
            EmployeeLearningRecord.project_id == project.id,
            EmployeeLearningRecord.validated.is_(True),
            EmployeeLearningRecord.validation_basis == "CANONICAL_WORK_ACCEPTANCE",
            EmployeeLearningRecord.learning_type.in_(("WORK_EXPERIENCE", "REVIEW_EXPERIENCE")),
        ).order_by(EmployeeLearningRecord.id).all()
    ]

    accepted_versions: list[int] = []
    verification_ids: list[int] = []
    if work_ids:
        accepted_versions = [
            int(value) for (value,) in db.session.query(ArtifactVersion.id).join(
                Artifact, ArtifactVersion.artifact_id == Artifact.id,
            ).filter(
                Artifact.project_id == project.id,
                ArtifactVersion.status == "ACCEPTED",
            ).order_by(ArtifactVersion.id).all()
        ]
        verification_ids = [
            int(value) for (value,) in db.session.query(VerificationRecord.id).filter(
                VerificationRecord.work_id.in_(work_ids),
                VerificationRecord.status == "PASSED",
            ).order_by(VerificationRecord.id).all()
        ]

    result_proof = None
    if status == "COMPLETED":
        try:
            proof = __import__(
                "eason_one.services.project_outcome", fromlist=["result_ready_proof"]
            ).result_ready_proof(project)
        except Exception:
            proof = None
        if proof:
            result_proof = {
                "artifact_id": proof["artifact"].id,
                "artifact_version_id": proof["version"].id,
                "verification_id": proof["verification"].id,
                "contract_hash": proof.get("contract_hash"),
                "authority_hash": proof.get("authority_hash"),
                "outcome_basis_hash": proof.get("outcome_basis_hash"),
            }

    terminal_authority = _founder_completion_observation(
        project, source_event=source_event
    )
    source_event_id = getattr(source_event, "id", None)
    source_event_type = getattr(source_event, "event_type", None)
    facts = {
        "schema": PROJECT_CLOSURE_SCHEMA,
        "project_id": project.id,
        "terminal_status": status,
        "source_event_id": source_event_id,
        "source_event_type": source_event_type,
        "accepted_work_ids": accepted_ids,
        "unresolved_work_ids": unresolved_ids,
        "open_assignment_ids_retained_as_history": open_assignment_ids,
        "total_cost_twd": str(Decimal(str(cost or 0))),
        "decision_ids": decision_ids,
        "contribution_event_ids": contribution_ids,
        "canonical_employee_learning_record_ids": canonical_learning_ids,
        "accepted_artifact_version_ids": accepted_versions,
        "passed_verification_record_ids": verification_ids,
        "ceo_action_outcomes": ceo_action_outcomes_for_project(project),
        "result_proof": result_proof,
        "terminal_authority": terminal_authority,
        "provider_calls": 0,
        "domain_truth_mutated": False,
        "learning_policy": (
            "Closure captures authoritative outcomes only. It does not mint capability, "
            "rewrite Decision history, or auto-promote company policy. Accepted Work "
            "experience remains owned by canonical Employee memory/evolution."
        ),
    }
    facts["closure_hash"] = _closure_hash({
        key: value for key, value in facts.items()
        if key not in {"source_event_id", "source_event_type", "closure_hash"}
    })
    return facts


def _stage_ceo_action_outcome_candidates(project, facts: dict[str, Any]) -> list[int]:
    """Persist factual, unvalidated CEO decision-outcome candidates.

    A candidate records what happened after a settled CEO action.  It is not a
    promoted policy, capability claim, or causal conclusion.  Promotion remains
    a separate governed learning decision.
    """
    from ..models import Employee, EmployeeLearningRecord
    ceo = Employee.query.filter_by(slug="ceo", active=True).first()
    if ceo is None:
        return []
    ids: list[int] = []
    for outcome in facts.get("ceo_action_outcomes") or []:
        intent_id = str(outcome.get("intent_id") or "").strip()
        if not intent_id or outcome.get("outcome") == "NOT_YET_OBSERVABLE":
            continue
        source_ref = f"CEO_ACTION_OUTCOME:{intent_id}"
        row = EmployeeLearningRecord.query.filter_by(
            employee_id=ceo.id,
            learning_type="CEO_DECISION_OUTCOME_CANDIDATE",
            source_ref=source_ref,
        ).first()
        if row is None:
            work_id = outcome.get("work_id")
            observed = outcome.get("outcome")
            row = EmployeeLearningRecord(
                employee_id=ceo.id,
                project_id=project.id,
                work_id=int(work_id) if work_id else None,
                learning_type="CEO_DECISION_OUTCOME_CANDIDATE",
                validation_basis="CEO_ACTION_OUTCOME_OBSERVATION",
                title=(
                    f"CEO action outcome candidate — {outcome.get('action_type') or 'STAFFING'} "
                    f"#{work_id or outcome.get('decision_id') or '-'}"
                )[:180],
                content=(
                    f"CEO {outcome.get('action_type') or 'staffing'} action {intent_id} has factual closure observation "
                    f"{observed}. {outcome.get('observation_summary') or ''} This is factual outcome evidence only; "
                    "it does not prove causality, decision quality, or a reusable policy."
                ),
                source_ref=source_ref,
                evidence_json={
                    "schema": "ceo-decision-outcome-candidate-v1",
                    "scope": "ROLE",
                    "project_id": project.id,
                    "project_terminal_status": facts.get("terminal_status"),
                    "project_total_cost_twd": facts.get("total_cost_twd"),
                    "observed_basis_hash": facts.get("closure_hash"),
                    **dict(outcome),
                },
                validated=False,
            )
            db.session.add(row)
            db.session.flush()
        ids.append(int(row.id))
    return ids


def latest_project_closure(project_id: int) -> dict[str, Any] | None:
    from ..models import CompanyEvent
    row = CompanyEvent.query.filter_by(
        event_type=PROJECT_CLOSURE_EVENT_TYPE,
        project_id=int(project_id),
    ).order_by(CompanyEvent.id.desc()).first()
    if row is None:
        return None
    return {**dict(row.payload_json or {}), "closure_event_id": row.id}


def capture_project_closure(project, *, source_event=None, commit: bool = True) -> dict[str, Any]:
    """Persist one deduped Project closure projection for a terminal episode.

    This function is safe to retry.  It never changes Project lifecycle, Work,
    budget, authority, Employee capability, or provider execution.
    """
    from ..models import CompanyEvent, Employee
    status = str(project.status or "").upper()
    if status not in _TERMINAL_PROJECT_STATUSES:
        return {"status": "NOT_TERMINAL", "project_id": project.id}
    if source_event is not None and source_event.event_type not in _TERMINAL_SOURCE_EVENTS:
        return {"status": "IGNORED_SOURCE_EVENT", "project_id": project.id}

    # A terminal source event is the freeze point for one closure episode. Check
    # its durable correlation *before* rebuilding facts or staging candidates so
    # restart/replay cannot pull later DB mutations into an already-captured
    # completion and silently mint new learning evidence.
    source_id = getattr(source_event, "id", None)
    if source_id:
        correlation = f"ceo:project-closure:{project.id}:source:{source_id}"
        existing = CompanyEvent.query.filter_by(
            event_type=PROJECT_CLOSURE_EVENT_TYPE,
            correlation_id=correlation,
        ).order_by(CompanyEvent.id.desc()).first()
        if existing is not None:
            return {
                **dict(existing.payload_json or {}),
                "status": "EXISTING",
                "closure_event_id": existing.id,
            }

    facts = project_closure_facts(project, source_event=source_event)
    candidate_ids = _stage_ceo_action_outcome_candidates(project, facts)
    facts["ceo_candidate_learning_record_ids"] = candidate_ids
    facts["closure_hash"] = _closure_hash({
        key: value for key, value in facts.items()
        if key not in {"source_event_id", "source_event_type", "closure_hash"}
    })
    previous = latest_project_closure(project.id)
    if previous and previous.get("closure_hash") == facts.get("closure_hash"):
        return {"status": "UNCHANGED", **previous}

    ceo = Employee.query.filter_by(slug="ceo", active=True).first()
    correlation = (
        f"ceo:project-closure:{project.id}:source:{source_id}"
        if source_id else f"ceo:project-closure:{project.id}:{facts['closure_hash'][:16]}"
    )
    existing = CompanyEvent.query.filter_by(
        event_type=PROJECT_CLOSURE_EVENT_TYPE,
        correlation_id=correlation,
    ).order_by(CompanyEvent.id.desc()).first()
    if existing is not None:
        return {**dict(existing.payload_json or {}), "status": "EXISTING", "closure_event_id": existing.id}

    event = __import__(
        "eason_one.services.company_events", fromlist=["emit"]
    ).emit(
        PROJECT_CLOSURE_EVENT_TYPE,
        actor_type="EMPLOYEE" if ceo else "RUNTIME",
        actor_id=getattr(ceo, "id", None),
        project_id=project.id,
        causation_id=source_id,
        correlation_id=correlation,
        payload=facts,
        commit=False,
    )
    if commit:
        db.session.commit()
    return {"status": "CAPTURED", **facts, "closure_event_id": event.id}


def capture_project_closure_from_event(event, *, commit: bool = True) -> dict[str, Any] | None:
    """Future-only runtime hook: terminal source events may create closure."""
    from ..models import Project
    if event.event_type not in _TERMINAL_SOURCE_EVENTS or not event.project_id:
        return None
    project = db.session.get(Project, int(event.project_id))
    if project is None:
        return None
    return capture_project_closure(project, source_event=event, commit=commit)
