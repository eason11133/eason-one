"""Work-scoped Founder/company escalation records.

Opening an Escalation does not itself freeze a whole Project or Operation.
Callers decide whether the affected Work should wait while independent Work can
continue.
"""
from __future__ import annotations

from ..extensions import db
from ..models import Escalation
from .company_events import correlation_for_work, emit


def open_escalation(*, project_id: int, escalation_type: str, reason: str, work_id: int | None = None, operation_id: int | None = None, created_by_employee_id: int | None = None, options: list | None = None, recommendation: str | None = None) -> Escalation:
    # Generic Company escalation storage may not mint Founder authority for a
    # governed vNext Project. Exact Founder questions must pass through the
    # canonical Governance owner so scope/options/identity are frozen.
    governance = __import__(
        "eason_one.services.governance", fromlist=["is_founder_type"]
    )
    if governance.is_founder_type(escalation_type):
        project = db.session.get(__import__("eason_one.models", fromlist=["Project"]).Project, project_id)
        contracts = __import__(
            "eason_one.services.project_contract", fromlist=["is_vnext_governed"]
        )
        if project and contracts.is_vnext_governed(project):
            raise ValueError("V020_FOUNDER_AUTHORITY_MUST_USE_CANONICAL_GOVERNANCE")
    existing = Escalation.query.filter_by(
        project_id=project_id,
        work_id=work_id,
        operation_id=operation_id,
        escalation_type=escalation_type,
        state="OPEN",
    ).first()
    if existing:
        return existing
    project = db.session.get(
        __import__("eason_one.models", fromlist=["Project"]).Project, project_id
    )
    if project is not None and __import__(
        "eason_one.services.work_runtime", fromlist=["project_is_terminal"]
    ).project_is_terminal(project):
        raise ValueError(
            f"PROJECT_TERMINAL_ESCALATION_CREATION_FORBIDDEN:{str(project.status or '').upper()}"
        )
    row = Escalation(
        project_id=project_id,
        work_id=work_id,
        operation_id=operation_id,
        escalation_type=escalation_type,
        state="OPEN",
        reason=reason,
        options_json=options or None,
        recommendation=recommendation,
        created_by_employee_id=created_by_employee_id,
    )
    db.session.add(row)
    db.session.flush()
    emit(
        "ESCALATION_OPENED",
        actor_type="EMPLOYEE" if created_by_employee_id else "RUNTIME",
        actor_id=created_by_employee_id,
        project_id=project_id,
        work_id=work_id,
        correlation_id=correlation_for_work(work_id) if work_id else f"project:{project_id}",
        payload={"escalation_id": row.id, "type": escalation_type, "reason": reason},
    )
    return row
