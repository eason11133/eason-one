"""Persistent Employee experience grounded in canonical company outcomes.

This is not free-form self-reflection and it is not a second Company Brain.
A normal Employee earns durable experience only when canonical Work reaches
ACCEPTED with an accepted ArtifactVersion and authoritative verification.
Future Work may retrieve that experience as bounded context, while authority,
Project truth, and learner/company decisions remain owned by their normal
subsystems.
"""
from __future__ import annotations

import json
import re
from typing import Iterable

from ..extensions import db
from ..models import (
    AgentRun,
    ArtifactVersion,
    ContributionEvent,
    Employee,
    EmployeeLearningRecord,
    FounderFeedbackEvent,
    Task,
    VerificationRecord,
    Work,
)

_MEMORY_TYPES = {"WORK_EXPERIENCE", "REVIEW_EXPERIENCE"}
_CANONICAL_BASIS = "CANONICAL_WORK_ACCEPTANCE"
_EXECUTION_MEMORY_SCHEMA = "PERSISTENT_EMPLOYEE_EXECUTION_MEMORY_V1"
_STOPWORDS = {
    "about", "accepted", "acceptance", "artifact", "company", "eason",
    "employee", "founder", "mission", "operation", "output", "project",
    "required", "result", "task", "verification", "work", "with", "from",
}


def _terms(text: str | None) -> set[str]:
    return {
        token for token in re.findall(
            r"[A-Za-z][A-Za-z0-9_\-]{3,}|[\u4e00-\u9fff]{2,}",
            (text or "").casefold(),
        )
        if token not in _STOPWORDS
    }


def _task_for_work(work: Work) -> Task | None:
    return Task.query.filter_by(work_id=work.id).order_by(Task.id).first()


def _verification_rows(work: Work, version: ArtifactVersion) -> list[VerificationRecord]:
    return VerificationRecord.query.filter_by(
        work_id=work.id, artifact_version_id=version.id
    ).order_by(VerificationRecord.id).all()


def _attempt_rows(work: Work) -> list[AgentRun]:
    return AgentRun.query.filter_by(work_id=work.id).order_by(AgentRun.id).all()


def _proof_snapshot(rows: Iterable[VerificationRecord]) -> list[dict]:
    result = []
    for row in rows:
        result.append({
            "verification_record_id": row.id,
            "method": row.method,
            "status": row.status,
            "verifier_employee_id": row.verifier_employee_id,
            "agent_run_id": row.agent_run_id,
        })
    return result


def _experience_content(work: Work, version: ArtifactVersion, proofs: list[VerificationRecord], attempts: list[AgentRun], *, responsibility: str) -> str:
    passed_methods = sorted({row.method for row in proofs if row.status == "PASSED"})
    prior_nonpass = sum(row.status in {"FAILED", "UNPROVEN"} for row in proofs)
    lines = [
        f"Accepted {responsibility.lower()} experience on Work #{work.id}: {work.title}.",
        f"Purpose: {work.purpose}",
        f"Expected output: {work.expected_output or '-'}",
        f"Authoritative proof: {', '.join(passed_methods) or 'ACCEPTED WORK STATE'}.",
        f"Accepted ArtifactVersion #{version.id} content hash {version.content_hash}.",
    ]
    if len(attempts) > 1:
        lines.append(f"This Work required {len(attempts)} execution attempts before accepted outcome.")
    if prior_nonpass:
        lines.append(f"The accepted outcome followed {prior_nonpass} failed/unproven verification record(s); prior attempts must not be treated as accepted precedent.")
    return " ".join(lines)


def _create_contribution(
    employee: Employee,
    work: Work,
    version: ArtifactVersion,
    proof: VerificationRecord,
    *,
    responsibility: str,
) -> ContributionEvent:
    """Persist one idempotent outcome-backed contribution fact.

    Contribution is not awarded for provider activity or prose generation.  It
    exists only because the exact Work/ArtifactVersion crossed canonical
    acceptance.  This keeps Contribution, Employee experience and verification
    anchored to the same durable truth without inventing a second scoring
    system.
    """
    event_type = (
        "CANONICAL_WORK_ACCEPTED"
        if responsibility == "OWNER"
        else "CANONICAL_REVIEW_ACCEPTED"
    )
    reference = (
        f"CANONICAL_ACCEPTANCE:WORK:{work.id}:VERSION:{version.id}:"
        f"EMPLOYEE:{employee.id}:ROLE:{responsibility.replace(' ', '_')}"
    )
    existing = ContributionEvent.query.filter_by(
        employee_id=employee.id,
        project_id=work.project_id,
        scope="PROJECT",
        event_type=event_type,
        related_reference=reference,
    ).first()
    if existing:
        return existing
    task = _task_for_work(work)
    row = ContributionEvent(
        employee_id=employee.id,
        project_id=work.project_id,
        scope="PROJECT",
        event_type=event_type,
        value=1,
        reason=(
            f"Outcome-backed {responsibility.lower()} contribution on Work #{work.id}; "
            f"ArtifactVersion #{version.id} passed VerificationRecord #{proof.id}."
        ),
        related_task_id=getattr(task, "id", None),
        related_reference=reference,
        status="CONFIRMED",
    )
    db.session.add(row)
    db.session.flush()
    return row


def _create_record(
    employee: Employee,
    work: Work,
    version: ArtifactVersion,
    acceptance: VerificationRecord,
    *,
    learning_type: str,
    responsibility: str,
    proofs: list[VerificationRecord],
    attempts: list[AgentRun],
) -> EmployeeLearningRecord:
    existing = EmployeeLearningRecord.query.filter_by(
        employee_id=employee.id,
        work_id=work.id,
        artifact_version_id=version.id,
        learning_type=learning_type,
    ).first()
    if existing:
        return existing
    task = _task_for_work(work)
    record = EmployeeLearningRecord(
        employee_id=employee.id,
        project_id=work.project_id,
        task_id=getattr(task, "id", None),
        work_id=work.id,
        artifact_version_id=version.id,
        verification_record_id=acceptance.id,
        learning_type=learning_type,
        validation_basis="CANONICAL_WORK_ACCEPTANCE",
        title=(f"{responsibility.title()} experience — {work.title}")[:180],
        content=_experience_content(work, version, proofs, attempts, responsibility=responsibility),
        source_ref=f"WORK:{work.id}/ARTIFACT_VERSION:{version.id}/ACCEPTANCE:{acceptance.id}",
        evidence_json={
            "schema": "employee-experience-v1",
            "capability": (
                str(((work.runtime_control_json or {}).get("team_formation") or {}).get("required_capability") or "").strip().upper()
                or (lambda values: values[0] if len(values) == 1 else None)([
                    str(value).strip().upper()
                    for value in (((work.runtime_control_json or {}).get("staffing_requirements") or {}).get("required_capabilities") or [])
                    if str(value).strip()
                ])
            ),
            "project_id": work.project_id,
            "operation_id": work.operation_id,
            "work_id": work.id,
            "work_type": work.work_type,
            "work_title": work.title,
            "work_purpose": work.purpose,
            "expected_output": work.expected_output,
            "artifact_version_id": version.id,
            "artifact_content_hash": version.content_hash,
            "producing_execution_id": version.execution_id,
            "producer_employee_id": version.producer_employee_id,
            "responsibility": responsibility,
            "acceptance_record_id": acceptance.id,
            "proof_records": _proof_snapshot(proofs),
            "execution_attempt_ids": [row.id for row in attempts],
        },
        validated=True,
    )
    db.session.add(record)
    db.session.flush()
    return record


def capture_accepted_work_experience(work: Work, version: ArtifactVersion, acceptance: VerificationRecord) -> list[EmployeeLearningRecord]:
    """Stage idempotent producer/reviewer experience for one accepted outcome.

    Caller owns the transaction so experience and Work acceptance commit or
    roll back together. No model call is used to invent a lesson.
    """
    if work.state != "ACCEPTED" or version.status != "ACCEPTED" or acceptance.status != "PASSED":
        return []
    proofs = _verification_rows(work, version)
    attempts = _attempt_rows(work)
    created: list[EmployeeLearningRecord] = []
    producer = db.session.get(Employee, version.producer_employee_id) if version.producer_employee_id else None
    if producer and producer.active:
        created.append(_create_record(
            producer, work, version, acceptance,
            learning_type="WORK_EXPERIENCE", responsibility="OWNER",
            proofs=proofs, attempts=attempts,
        ))
        _create_contribution(
            producer, work, version, acceptance, responsibility="OWNER"
        )
    reviewer_ids = sorted({
        row.verifier_employee_id for row in proofs
        if row.method == "INDEPENDENT_REVIEW" and row.status == "PASSED" and row.verifier_employee_id
    })
    for reviewer_id in reviewer_ids:
        if producer and reviewer_id == producer.id:
            continue
        reviewer = db.session.get(Employee, reviewer_id)
        if reviewer and reviewer.active:
            created.append(_create_record(
                reviewer, work, version, acceptance,
                learning_type="REVIEW_EXPERIENCE", responsibility="INDEPENDENT REVIEWER",
                proofs=proofs, attempts=attempts,
            ))
            review_proof = next(
                row for row in proofs
                if row.verifier_employee_id == reviewer_id
                and row.method == "INDEPENDENT_REVIEW"
                and row.status == "PASSED"
            )
            _create_contribution(
                reviewer, work, version, review_proof,
                responsibility="INDEPENDENT REVIEWER",
            )
    # Accepted owner evidence can complete a delegated hire's probation. This is
    # an employment-state transition only and is staged in the same transaction
    # as the canonical acceptance evidence. Read surfaces never trigger it.
    if producer and producer.active:
        __import__(
            "eason_one.services.employee_evolution", fromlist=["reconcile_probation"]
        ).reconcile_probation(producer)
    return created


def relevant_experience(employee: Employee, project=None, task=None, *, capability: str | None = None, limit: int = 6) -> tuple[str, dict]:
    """Return bounded, outcome-backed experience relevant to the current Work."""
    query = EmployeeLearningRecord.query.filter(
        EmployeeLearningRecord.employee_id == employee.id,
        EmployeeLearningRecord.validated.is_(True),
        EmployeeLearningRecord.learning_type.in_(tuple(_MEMORY_TYPES)),
        EmployeeLearningRecord.validation_basis == _CANONICAL_BASIS,
    ).order_by(EmployeeLearningRecord.created_at.desc(), EmployeeLearningRecord.id.desc())
    candidates = query.limit(40).all()
    if not candidates:
        return "", {"experience_record_ids": [], "matched_records": 0}

    task_text = " ".join(filter(None, [
        getattr(task, "title", None), getattr(task, "objective", None),
        getattr(task, "required_output", None), getattr(task, "acceptance_criteria", None),
        getattr(project, "name", None),
    ]))
    wanted = _terms(task_text)
    required_capability = str(capability or "").strip().upper() or None
    ranked = []
    for row in candidates:
        evidence = row.evidence_json or {}
        evidence_capability = str(evidence.get("capability") or "").strip().upper() or None
        if required_capability and evidence_capability != required_capability:
            continue
        haystack = " ".join([
            row.title or "", row.content or "",
            str(evidence.get("work_type") or ""), str(evidence.get("work_title") or ""),
            str(evidence.get("work_purpose") or ""), str(evidence.get("expected_output") or ""),
        ])
        overlap = len(wanted.intersection(_terms(haystack))) if wanted else 0
        same_project = bool(project and row.project_id == project.id)
        # Relevant cross-project experience is preferred; current-project history
        # remains useful but does not monopolize the bounded memory window.
        score = overlap * 4 + (1 if same_project else 2)
        ranked.append((score, row.id, row))
    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    selected = [row for _, _, row in ranked[:limit]]
    if not selected:
        return "", {"experience_record_ids": [], "matched_records": 0}

    lines = ["PERSISTENT EMPLOYEE EXPERIENCE — outcome-backed, not authority"]
    for row in reversed(selected):
        evidence = row.evidence_json or {}
        proof_methods = sorted({
            item.get("method") for item in (evidence.get("proof_records") or [])
            if item.get("status") == "PASSED" and item.get("method")
        })
        lines.extend([
            f"- EXPERIENCE #{row.id} · {row.learning_type} · Project #{row.project_id or '-'} · Work #{row.work_id or '-'}",
            f"  {row.title}: {row.content}",
            f"  Proof: {', '.join(proof_methods) or row.validation_basis or 'validated'}; source {row.source_ref or '-'}",
        ])
    return "\n".join(lines), {
        "experience_record_ids": [row.id for row in selected],
        "matched_records": len(selected),
        "query_terms": sorted(wanted)[:20],
        "required_capability": required_capability,
    }



def execution_learning_context(
    employee: Employee,
    project=None,
    task=None,
    *,
    purpose: str = "TASK_EXECUTION",
    capability: str | None = None,
    limit: int = 6,
) -> tuple[str, dict]:
    """Return canonical Persistent Employee memory for one governed model call.

    This is the consumption boundary that turns accepted cross-Project outcomes
    into future behavior.  Only canonical accepted-Work records are eligible.
    The context is advisory precedent: it cannot prove a current fact, grant a
    capability, expand Founder authority/budget, or replace current verification.
    """
    text, metadata = relevant_experience(
        employee, project, task, capability=capability, limit=limit
    )
    metadata = dict(metadata or {})
    record_ids = [int(value) for value in (metadata.get("experience_record_ids") or [])]
    envelope = {
        "schema": _EXECUTION_MEMORY_SCHEMA,
        "purpose": str(purpose or "TASK_EXECUTION").strip().upper(),
        "employee_id": employee.id,
        "project_id": getattr(project, "id", None),
        "task_id": getattr(task, "id", None),
        "experience_record_ids": record_ids,
        "matched_records": int(metadata.get("matched_records") or 0),
        "query_terms": list(metadata.get("query_terms") or []),
        "required_capability": str(capability or metadata.get("required_capability") or "").strip().upper() or None,
        "validation_basis": _CANONICAL_BASIS,
        "authority_effect": False,
        "capability_effect": False,
        "budget_effect": False,
        "current_fact_effect": False,
    }
    if not text or not record_ids:
        return "", envelope
    boundary = (
        "\nLEARNING CONSUMPTION RULE — prior accepted outcomes are precedent, not current truth. "
        "Reuse relevant patterns only when they fit the current governed Work. Current Project Contract, "
        "acceptance criteria, provider-observed evidence, and VerificationRecords always outrank memory. "
        "For research, historical experience is never fresh web evidence. For review, an older accepted result "
        "never proves the current ArtifactVersion."
    )
    return text + boundary, envelope

def backfill_existing_experience(*, commit: bool = False) -> int:
    """Attach existing accepted vNext outcomes to their Persistent Employees.

    This is deterministic compatibility adoption, not model-generated learning.
    It lets an upgraded company retain career history that predates this memory
    projection without replaying the underlying Work.
    """
    from ..models import Artifact
    accepted = (
        db.session.query(Work, ArtifactVersion)
        .join(Artifact, Artifact.work_id == Work.id)
        .join(ArtifactVersion, ArtifactVersion.artifact_id == Artifact.id)
        .filter(
            Work.state == "ACCEPTED",
            ArtifactVersion.status == "ACCEPTED",
        )
        .order_by(Work.id, ArtifactVersion.id)
        .all()
    )
    count = 0
    for work, version in accepted:
        acceptance = (
            VerificationRecord.query.filter_by(
                work_id=work.id, artifact_version_id=version.id, status="PASSED",
                method="ACCEPTANCE_CONTRACT",
            ).order_by(VerificationRecord.id.desc()).first()
            or VerificationRecord.query.filter_by(
                work_id=work.id, artifact_version_id=version.id, status="PASSED"
            ).order_by(VerificationRecord.id.desc()).first()
        )
        if acceptance is None:
            continue
        before = EmployeeLearningRecord.query.filter_by(
            work_id=work.id, artifact_version_id=version.id
        ).count()
        capture_accepted_work_experience(work, version, acceptance)
        after = EmployeeLearningRecord.query.filter_by(
            work_id=work.id, artifact_version_id=version.id
        ).count()
        count += max(0, after - before)
    if commit:
        db.session.commit()
    return count



def founder_feedback_context(employee: Employee, *, limit: int = 5) -> tuple[str, dict]:
    """Return direct Founder signals attached to this Persistent Employee.

    Feedback remains an observation with exact provenance. It is not converted
    into a capability score, promotion entitlement, or autonomous authority.
    """
    rows = (
        FounderFeedbackEvent.query.filter_by(employee_id=employee.id)
        .order_by(FounderFeedbackEvent.created_at.desc(), FounderFeedbackEvent.id.desc())
        .limit(limit).all()
    )
    if not rows:
        return "", {"founder_feedback_ids": [], "signals": {}}
    counts: dict[str, int] = {}
    lines = ["DIRECT FOUNDER FEEDBACK — provenance, not a capability score"]
    for row in reversed(rows):
        counts[row.signal] = counts.get(row.signal, 0) + 1
        lines.append(
            f"- FEEDBACK #{row.id} · {row.signal} · Meeting #{row.meeting_id} / Message #{row.message_id}: "
            f"{row.note or 'No Founder note supplied.'}"
        )
    return "\n".join(lines), {
        "founder_feedback_ids": [row.id for row in rows],
        "signals": counts,
    }

def roster_experience_summary(employee: Employee, *, limit: int = 3) -> str:
    """Compact career signal for CEO team formation.

    This exposes only proven past outcomes, never inferred capability scores.
    The CEO may use it as evidence when selecting a team but retains the normal
    Project/Work planning authority.
    """
    rows = (
        EmployeeLearningRecord.query.filter(
            EmployeeLearningRecord.employee_id == employee.id,
            EmployeeLearningRecord.validated.is_(True),
            EmployeeLearningRecord.learning_type.in_(tuple(_MEMORY_TYPES)),
        )
        .order_by(EmployeeLearningRecord.created_at.desc(), EmployeeLearningRecord.id.desc())
        .limit(20).all()
    )
    feedback_rows = (
        FounderFeedbackEvent.query.filter_by(employee_id=employee.id)
        .order_by(FounderFeedbackEvent.created_at.desc(), FounderFeedbackEvent.id.desc())
        .limit(20).all()
    )
    feedback_counts: dict[str, int] = {}
    for item in feedback_rows:
        feedback_counts[item.signal] = feedback_counts.get(item.signal, 0) + 1
    feedback_text = ", ".join(f"{key} {value}" for key, value in sorted(feedback_counts.items())) or "none"
    if not rows:
        return f"accepted experience: none recorded yet; Founder signals: {feedback_text}"
    project_ids = {row.project_id for row in rows if row.project_id is not None}
    owner_count = sum(row.learning_type == "WORK_EXPERIENCE" for row in rows)
    review_count = sum(row.learning_type == "REVIEW_EXPERIENCE" for row in rows)
    recent = "; ".join(row.title for row in rows[:limit])
    return (
        f"accepted experience: owner {owner_count}; independent review {review_count}; "
        f"projects represented {len(project_ids)}; recent: {recent}; Founder signals: {feedback_text}"
    )
