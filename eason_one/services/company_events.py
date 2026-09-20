"""Company-level append-only event contract for Company Core vNext.

Domain kernels (OperationEvent / MeetingEvent) keep their detailed state-machine
history.  CompanyEvent records business-significant organizational facts that
Founder surfaces, replay, and later behavior/contribution analytics can consume
without inventing their own truth.
"""
from __future__ import annotations

from typing import Any

from ..extensions import db
from ..models import CompanyEvent


def emit(
    event_type: str,
    *,
    actor_type: str = "SYSTEM",
    actor_id: int | None = None,
    project_id: int | None = None,
    work_id: int | None = None,
    execution_id: int | None = None,
    meeting_id: int | None = None,
    artifact_id: int | None = None,
    decision_id: int | None = None,
    causation_id: int | None = None,
    correlation_id: str | None = None,
    payload: dict[str, Any] | None = None,
    commit: bool = False,
) -> CompanyEvent:
    event = CompanyEvent(
        event_type=event_type,
        actor_type=actor_type,
        actor_id=actor_id,
        project_id=project_id,
        work_id=work_id,
        execution_id=execution_id,
        meeting_id=meeting_id,
        artifact_id=artifact_id,
        decision_id=decision_id,
        causation_id=causation_id,
        correlation_id=correlation_id,
        payload_json=payload or None,
        schema_version=1,
    )
    db.session.add(event)
    db.session.flush()
    if commit:
        db.session.commit()
    return event


def correlation_for_work(work_id: int) -> str:
    return f"work:{work_id}"
