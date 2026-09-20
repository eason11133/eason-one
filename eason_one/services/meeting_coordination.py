"""Bounded v0.20 Meeting coordination for the Company Kernel.

Meeting progression is Company coordination truth, not Project outcome truth and
not Founder authority truth. A Meeting may wait for an explicit Founder message,
but it cannot amend the Project Contract, mint budget authority, or close/fail a
Project. Keeping this owner outside legacy_company_runtime_v018 prevents legacy
runtime semantics from re-entering the normal v0.20 tick path.
"""
from __future__ import annotations

from ..extensions import db
from ..models import Meeting, MeetingEvent, MeetingMessage


def operation_meeting(operation) -> Meeting | None:
    return Meeting.query.filter_by(operation_id=operation.id).order_by(Meeting.id).first()


def meeting_terminal(meeting: Meeting | None) -> bool:
    if meeting is None:
        return True
    kernel = __import__("eason_one.services.meeting_kernel", fromlist=["status", "TERMINAL"])
    return kernel.status(meeting) in kernel.TERMINAL


def _delivery_closed(operation) -> bool:
    rows = [work for work in operation.works if work.work_type != "MANAGEMENT"]
    return bool(rows) and all(work.state in {"ACCEPTED", "CANCELLED"} for work in rows)


def _material_conflict_detected(operation) -> bool:
    """Detect a persisted cross-role conflict from current Work truth.

    v0.20 cannot let stale Task state decide whether a real Company Meeting is
    needed. Independent review outcomes and explicit management decisions are
    durable evidence; legacy Task status is only a compatibility fallback when
    the Operation has no Work spine.
    """
    delivery = [work for work in operation.works if work.work_type != "MANAGEMENT"]
    if delivery:
        if any(work.state == "ABANDONED" for work in delivery):
            return True
    elif any(task.status in {"BLOCKED", "FAILED"} for task in operation.tasks):
        return True

    models = __import__("eason_one.models", fromlist=["AgentRun"])
    review_runs = models.AgentRun.query.filter_by(
        operation_id=operation.id, purpose="TASK_REVIEW", status="SUCCEEDED"
    ).order_by(models.AgentRun.id).all()
    for run in review_runs:
        decision = (run.parsed_output_json or {}).get("decision")
        if decision in {"REVISE", "BLOCK"}:
            return True

    decisions = (operation.memory_json or {}).get("decisions") or []
    return any(item.get("action") in {"MEETING", "REASSIGN_TASK"} for item in decisions)


def _record_result_once(operation, meeting: Meeting) -> dict | None:
    """Project a completed Meeting result into bounded Mission memory once.

    Downstream Work context already consumes ``meeting_results``. Recording the
    result here makes a real mid-Mission Meeting an input to subsequent Employee
    work without introducing a second orchestration/authority engine.
    """
    meeting_service = __import__("eason_one.services.meetings", fromlist=["result_view"])
    result = meeting_service.result_view(meeting)
    if not result:
        return None
    memory = dict(operation.memory_json or {})
    outcomes = list(memory.get("meeting_results") or [])
    if any(int(item.get("meeting_id") or 0) == int(meeting.id) for item in outcomes if isinstance(item, dict)):
        return None
    outcomes.append({"meeting_id": meeting.id, "result": result})
    memory["meeting_results"] = outcomes[-8:]
    operation.memory_json = memory
    emit = __import__("eason_one.services.company_events", fromlist=["emit"]).emit
    emit(
        "MEETING_RESULT_COMMITTED", actor_type="RUNTIME",
        project_id=operation.project_id, work_id=meeting.related_work_id,
        correlation_id=f"meeting:{meeting.id}",
        payload={"operation_id": operation.id, "meeting_id": meeting.id},
    )
    db.session.commit()
    return {"status": "MEETING_RESULT_COMMITTED", "meeting_id": meeting.id}


def _meeting_config(operation) -> dict:
    ops = __import__("eason_one.services.operations", fromlist=["default_meeting_config"])
    spec = ((operation.plan_json or {}).get("operation") or {})
    config = dict(spec.get("meeting_config") or ops.default_meeting_config(spec))
    defaults = ops.default_meeting_config(spec)
    for key, value in defaults.items():
        config.setdefault(key, value)
    return config


def _meeting_runtime_retries(meeting: Meeting) -> int:
    return MeetingEvent.query.filter_by(
        meeting_id=meeting.id, event_type="MEETING_RUNTIME_RETRY"
    ).count()


def requires_founder_input(meeting: Meeting | None) -> bool:
    """Return whether the Meeting is waiting on a concrete Founder message.

    The legacy WAITING_FOR_FOUNDER string is not sufficient evidence because
    historical code used it for provider/synthesis failures.  Current truth
    requires the explicit router wait marker plus a non-empty question and the
    matching latest state transition.
    """
    if meeting is None or meeting.status != "WAITING_FOR_FOUNDER":
        return False
    routing = dict(meeting.routing_json or {})
    question = str(routing.get("question") or "").strip()
    if routing.get("waiting_for_founder") is not True or not question:
        return False
    latest = (
        MeetingEvent.query.filter_by(meeting_id=meeting.id)
        .order_by(MeetingEvent.sequence.desc())
        .first()
    )
    return bool(
        latest
        and latest.event_type == "MEETING_FOUNDER_INPUT_REQUIRED"
        and latest.to_status == "WAITING_FOR_INPUTS"
    )


def advance_meeting_gate(operation) -> dict | None:
    """Advance at most one bounded Company coordination action.

    ``ON_MATERIAL_CONFLICT`` may run before every delivery Work is closed so a
    cross-role disagreement can actually influence the next retry/handoff.
    ``BEFORE_FINAL_REPORT`` remains a terminal-delivery gate. Ordinary provider
    failures receive bounded automatic recovery. Founder authority remains owned
    exclusively by services.governance.
    """
    meeting = operation_meeting(operation)
    if meeting is None:
        return None

    meeting_service = __import__(
        "eason_one.services.meetings",
        fromlist=[
            "start_auto", "skip", "next_step", "retry_paid_step",
            "close_for_internal_recovery",
        ],
    )
    meeting_kernel = __import__(
        "eason_one.services.meeting_kernel", fromlist=["status", "append_event", "TERMINAL"]
    )
    status = meeting_kernel.status(meeting)
    if status in meeting_kernel.TERMINAL:
        return _record_result_once(operation, meeting)

    config = _meeting_config(operation)
    trigger = str(config.get("trigger") or "ON_MATERIAL_CONFLICT").upper()
    delivery_closed = _delivery_closed(operation)
    if status in {"PLANNED", "READY"}:
        if trigger == "NEVER":
            meeting_service.skip(
                meeting, "Approved Meeting policy does not require a coordination call."
            )
            return {"status": "MEETING_SKIPPED", "meeting_id": meeting.id}
        if trigger == "BEFORE_FINAL_REPORT" and not delivery_closed:
            return None
        if trigger == "ON_MATERIAL_CONFLICT" and not _material_conflict_detected(operation):
            if not delivery_closed:
                # Keep the planned room dormant while independent Employees work.
                # It may become necessary after a later review/conflict event.
                return None
            meeting_service.skip(
                meeting, "Accepted specialist work contains no persisted material conflict."
            )
            return {"status": "MEETING_SKIPPED", "meeting_id": meeting.id}
        meeting_service.start_auto(meeting)
        return {"status": "MEETING_STARTED", "meeting_id": meeting.id}

    if meeting.status == "WAITING_FOR_FOUNDER":
        if not requires_founder_input(meeting):
            # A stale/legacy projection must never manufacture Founder attention.
            meeting_service.close_for_internal_recovery(
                meeting,
                "Meeting carried a non-canonical Founder-wait projection without an explicit question.",
            )
            return {"status": "MEETING_NONCANONICAL_WAIT_CLOSED", "meeting_id": meeting.id}
        latest = (
            MeetingMessage.query.filter_by(
                meeting_id=meeting.id, message_type="FOUNDER_INTERVENTION"
            )
            .order_by(MeetingMessage.id.desc())
            .first()
        )
        waited_at = int((meeting.routing_json or {}).get("founder_message_id_at_wait") or 0)
        if latest and latest.id > waited_at:
            # The message itself satisfies the requested conversational input;
            # do not manufacture a second approval/resume authority event.
            meeting_service.resume(meeting)
            return {"status": "MEETING_RESUMED", "meeting_id": meeting.id}
        return {"status": "NEEDS_FOUNDER", "meeting_id": meeting.id}

    if meeting.status == "PAUSED":
        retry_limit = max(0, int(config.get("retry_limit") or 1))
        retries = _meeting_runtime_retries(meeting)
        if retries >= retry_limit:
            reason = (
                "Meeting exhausted bounded automatic recovery; no reliable coordination "
                "outcome was committed."
            )
            meeting_service.close_for_internal_recovery(meeting, reason)
            return {"status": "MEETING_RECOVERY_EXHAUSTED", "meeting_id": meeting.id}
        meeting_kernel.append_event(
            meeting,
            "MEETING_RUNTIME_RETRY",
            actor_type="RUNTIME",
            payload={"attempt": retries + 1, "limit": retry_limit},
        )
        db.session.commit()
        if meeting.paid_failure_json:
            meeting_service.retry_paid_step(meeting, actor_type="RUNTIME")
        else:
            meeting_service.start_auto(meeting)
        return {"status": "MEETING_RETRY", "meeting_id": meeting.id, "attempt": retries + 1}

    if meeting.status == "RUNNING":
        try:
            meeting_service.next_step(meeting)
        except Exception as exc:
            # Meeting code persists PAUSED + failure evidence before raising.
            db.session.rollback()
            meeting = db.session.get(Meeting, meeting.id)
            if meeting and meeting.status not in {
                "PAUSED", "WAITING_FOR_FOUNDER", "ENDED", "TERMINATED_BY_FOUNDER"
            }:
                meeting_service.close_for_internal_recovery(
                    meeting,
                    "Meeting runtime failed without a recoverable checkpoint: "
                    f"{type(exc).__name__}: {exc}",
                )
            return {"status": "MEETING_STEP_FAILED", "meeting_id": getattr(meeting, "id", None)}
        return {"status": "MEETING_ADVANCED", "meeting_id": meeting.id}

    return {
        "status": "MEETING_WAITING",
        "meeting_id": meeting.id,
        "meeting_status": meeting.status,
    }
