"""Persistent bounded Meeting state machine.

A Meeting is an explicit escalation object.  It is visible from approval,
retains its state after restart, and may not exceed its round/message/cost
limits.  Legacy status values remain a UI projection only.
"""
from __future__ import annotations

from sqlalchemy import func

from ..extensions import db
from ..models import Meeting, MeetingEvent, MeetingMessage, now

STATUSES = {
    "PLANNED", "WAITING_FOR_INPUTS", "READY", "ACTIVE",
    "SYNTHESIZING", "COMPLETED", "FAILED", "CANCELLED",
}
TERMINAL = {"COMPLETED", "FAILED", "CANCELLED"}
ALLOWED = {
    "PLANNED": {"WAITING_FOR_INPUTS", "READY", "CANCELLED"},
    "WAITING_FOR_INPUTS": {"READY", "ACTIVE", "CANCELLED", "FAILED"},
    "READY": {"ACTIVE", "WAITING_FOR_INPUTS", "CANCELLED", "FAILED"},
    "ACTIVE": {"WAITING_FOR_INPUTS", "SYNTHESIZING", "COMPLETED", "CANCELLED", "FAILED"},
    "SYNTHESIZING": {"COMPLETED", "WAITING_FOR_INPUTS", "FAILED", "CANCELLED"},
    "COMPLETED": set(), "FAILED": set(), "CANCELLED": set(),
}
LEGACY_TO_KERNEL = {
    "PLANNED": "READY", "ACTIVE": "ACTIVE", "RUNNING": "ACTIVE",
    "PAUSED": "WAITING_FOR_INPUTS", "WAITING_FOR_FOUNDER": "WAITING_FOR_INPUTS",
    "ENDED": "COMPLETED", "TERMINATED_BY_FOUNDER": "CANCELLED",
}
KERNEL_TO_LEGACY = {
    "PLANNED": "PLANNED", "WAITING_FOR_INPUTS": "PAUSED", "READY": "PLANNED",
    "ACTIVE": "RUNNING", "SYNTHESIZING": "ACTIVE", "COMPLETED": "ENDED",
    "FAILED": "FAILED", "CANCELLED": "TERMINATED_BY_FOUNDER",
}


def status(meeting: Meeting) -> str:
    value=(meeting.kernel_status or "").upper()
    legacy=(meeting.status or "").upper()
    if value=="PLANNED" and legacy not in {"", "PLANNED"}:
        return LEGACY_TO_KERNEL.get(legacy,value)
    if value in STATUSES:
        return value
    return LEGACY_TO_KERNEL.get(legacy, "PLANNED")


def _next(meeting_id: int) -> int:
    value=db.session.query(func.coalesce(func.max(MeetingEvent.sequence),0)).filter_by(meeting_id=meeting_id).scalar()
    return int(value or 0)+1


def append_event(meeting: Meeting,event_type: str,*,from_status=None,to_status=None,stage=None,actor_type="SYSTEM",payload=None):
    row=MeetingEvent(
        meeting_id=meeting.id,sequence=_next(meeting.id),event_type=event_type,
        from_status=from_status,to_status=to_status,stage=stage or meeting.current_stage,
        actor_type=actor_type,payload_json=payload or None,
    )
    db.session.add(row)
    return row


def bootstrap(meeting: Meeting,*,commit=True):
    current=status(meeting)
    meeting.kernel_status=current
    meeting.current_stage=meeting.current_stage or current
    meeting.max_messages=max(2,int(meeting.max_messages or (meeting.max_rounds*meeting.max_speakers_per_round+2)))
    meeting.message_count=MeetingMessage.query.filter_by(meeting_id=meeting.id).count()
    meeting.state_version=int(meeting.state_version or 0)
    if not MeetingEvent.query.filter_by(meeting_id=meeting.id).first():
        append_event(meeting,"MEETING_IMPORTED",to_status=current,payload={"legacy_status":meeting.status})
    if commit: db.session.commit()
    return meeting


def transition(meeting: Meeting,to_status: str,event_type: str,*,stage=None,actor_type="SYSTEM",legacy_status=None,payload=None,force=False,commit=True):
    to_status=(to_status or "").upper()
    if to_status not in STATUSES: raise ValueError(f"Unknown Meeting status: {to_status}")
    current=status(meeting)
    if current != to_status and not force and to_status not in ALLOWED[current]:
        raise ValueError(f"Invalid Meeting transition: {current} -> {to_status}")
    append_event(meeting,event_type,from_status=current,to_status=to_status,stage=stage,actor_type=actor_type,payload=payload)
    meeting.kernel_status=to_status
    meeting.current_stage=stage or to_status
    meeting.status=legacy_status or KERNEL_TO_LEGACY[to_status]
    meeting.state_version=int(meeting.state_version or 0)+1
    meeting.message_count=MeetingMessage.query.filter_by(meeting_id=meeting.id).count()
    if to_status=="ACTIVE": meeting.started_at=meeting.started_at or now()
    if to_status in TERMINAL: meeting.ended_at=meeting.ended_at or now()
    if commit: db.session.commit()
    return meeting


def ensure_message_capacity(meeting: Meeting,additional: int=1):
    current=MeetingMessage.query.filter_by(meeting_id=meeting.id).count()
    operation_limit=int(meeting.operation.max_messages) if meeting.operation else int(meeting.max_messages)
    hard_limit=min(int(meeting.max_messages),operation_limit)
    if current+int(additional)>hard_limit:
        raise ValueError(f"Meeting message limit would be exceeded ({current}/{hard_limit})")
    return True
