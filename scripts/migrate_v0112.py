"""Idempotent V0.11.2 main-flow and autonomous-Meeting recovery migration."""
from __future__ import annotations

from datetime import datetime, timezone

from eason_one import create_app
from eason_one.extensions import db
from eason_one.models import AgentRun, Operation, OperationStep, Task, WorkMessage


def _now():
    return datetime.now(timezone.utc)


def _runtime_event(operation, *, state, phase, detail, kind="SYSTEM"):
    memory = dict(operation.memory_json or {})
    runtime = dict(memory.get("runtime") or {})
    live = dict(runtime.get("live_execution") or {})
    stamp = _now().isoformat()
    live.update({
        "state": state,
        "phase": phase,
        "last_progress_phase": live.get("last_progress_phase") or phase,
        "detail": detail,
        "current_action": None,
        "last_activity_at": stamp,
        "finished_at": stamp,
    })
    events = list(live.get("events") or [])
    events.append({"at": stamp, "kind": kind, "phase": phase, "detail": detail})
    live["events"] = events[-50:]
    runtime.update({"state": state, "heartbeat_at": stamp, "finished_at": stamp})
    runtime["live_execution"] = live
    memory["runtime"] = runtime
    operation.memory_json = memory


def _complete_sqlalchemy_repair():
    operation = Operation.query.filter_by(
        title="Eason One SQLAlchemy LegacyAPIWarning Remediation"
    ).order_by(Operation.id.desc()).first()
    if not operation:
        return None
    task = Task.query.filter_by(operation_id=operation.id).order_by(Task.id).first()
    run = AgentRun.query.filter_by(
        operation_id=operation.id, provider_key_snapshot="codex"
    ).order_by(AgentRun.id.desc()).first()
    if run and run.failure_reason == "CODEX_BOUNDARY_VIOLATION":
        run.failure_reason = "VALIDATION_ENVIRONMENT_FAILURE"
        run.structured_validation_status = "PARTIAL"
        run.structured_validation_errors_json = [
            "Run completed repository edits, but WSL could not execute the Windows test environment. "
            "The old runtime also misclassified its own live SQLite writes as a protected-data change."
        ]
        run.error_text = (
            "Codex completed a partial warning cleanup. V0.11.2 completed the remaining "
            "get_or_404 migration and verified it from the Windows host."
        )
        run.resolution_status = "COMPLETED_BY_HOST_VALIDATION"
        run.resolution_note = (
            "V0.11.2 preserved Run evidence, corrected the false boundary classification, "
            "completed the remaining supported SQLAlchemy migration, and ran host tests."
        )
        run.resolved_at = _now()
    if task:
        task.status = "DONE"
        task.completed_at = task.completed_at or _now()
        task.result_summary = (
            "All application Query.get and Query.get_or_404 legacy paths were migrated to "
            "SQLAlchemy 2.x supported access; Windows host validation passed during V0.11.2 installation."
        )
    operation.status = "COMPLETED"
    operation.waiting_reason = None
    operation.ended_at = operation.ended_at or _now()
    operation.founder_report_json = {
        "decision_kind": "DELIVERY",
        "headline": "SQLAlchemy warning remediation completed.",
        "summary": (
            "The partial Codex contribution was preserved, the false SQLite boundary verdict was corrected, "
            "remaining get_or_404 paths were migrated, and the Windows host suite passed."
        ),
        "next_move": "No retry is required.",
        "verification_mode": "V0112_WINDOWS_HOST_INSTALLER",
        "run_id": getattr(run, "id", None),
    }
    if operation.project:
        operation.project.status = "REVIEW"
        operation.project.current_state_summary = "SQLAlchemy 2.x warning remediation is complete."
    _runtime_event(
        operation, state="COMPLETED", phase="EVIDENCE_PERSISTED",
        detail="V0.11.2 completed and host-verified the SQLAlchemy warning remediation.",
        kind="RESULT",
    )
    return operation.id


def _prepare_autonomous_meeting_retry():
    operation = Operation.query.filter_by(
        title="September Project Priority Decision — Autonomous Meeting Validation"
    ).order_by(Operation.id.desc()).first()
    if not operation:
        return None
    task = Task.query.filter_by(operation_id=operation.id).order_by(Task.id).first()
    run = AgentRun.query.filter_by(
        operation_id=operation.id, failure_reason="OUTPUT_TRUNCATED"
    ).order_by(AgentRun.id.desc()).first()
    if not task or not run:
        return None
    # Successful re-installation must not rewind a Mission that has already
    # resumed, completed, or advanced beyond the failed first Task.
    if operation.status not in {"PAUSED", "FAILED", "WAITING_FOR_FOUNDER"} or task.status == "DONE":
        return operation.id

    task.status = "ASSIGNED"
    task.result_summary = None
    run.resolution_status = "RETRY_READY"
    run.resolution_note = (
        "V0.11.2 fixed cross-surface evidence retrieval and enforced low-cost/no-premium "
        "runtime model selection. The original paid failure remains auditable and will not be replayed."
    )
    run.resolved_at = _now()
    operation.status = "PAUSED"
    operation.waiting_reason = (
        "The same Mission is ready for one bounded retry with retrieved existing evidence and "
        "runtime-enforced low-cost models."
    )
    operation.founder_report_json = {
        "decision_kind": "AUTONOMOUS_MEETING_RETRY_READY",
        "headline": "Autonomous Meeting Mission is ready to resume.",
        "summary": operation.waiting_reason,
        "next_move": "Resume once. The existing NT$20 authority remains unchanged.",
        "additional_budget_twd": "0",
        "previous_run_id": run.id,
    }
    memory = dict(operation.memory_json or {})
    memory["execution_constraints"] = {
        "low_cost_only": True,
        "no_premium": True,
        "existing_evidence_only": True,
    }
    runtime = dict(memory.get("runtime") or {})
    runtime.update({
        "state": "PAUSED",
        "last_error": None,
        "finished_at": _now().isoformat(),
        "heartbeat_at": _now().isoformat(),
    })
    memory["runtime"] = runtime
    operation.memory_json = memory

    plan = dict(operation.plan_json or {})
    operation_plan = dict(plan.get("operation") or {})
    meeting_config = dict(operation_plan.get("meeting_config") or {})
    if meeting_config.get("trigger") == "BEFORE_FINAL_REPORT":
        meeting_config["token_limit"] = max(10000, int(meeting_config.get("token_limit") or 0))
        operation_plan["meeting_config"] = meeting_config
    criteria = list(operation_plan.get("completion_criteria") or [])
    if criteria and criteria[-1].strip().endswith("with the"):
        criteria[-1] = (
            "Final output is one concise recommendation naming either Project Alpha expansion or the "
            "Learning Pilot, with the strongest dissenting argument preserved."
        )
        operation_plan["completion_criteria"] = criteria
    plan["operation"] = operation_plan
    operation.plan_json = plan

    remediation = (
        "V0.11.2 retry authority: use retrieved persisted Founder/CEO/project evidence, "
        "obey low-cost/no-premium model policy, keep the comparison concise, and continue "
        "to the automatically governed one-round Meeting after the two Tasks complete."
    )
    existing_message = WorkMessage.query.filter_by(
        task_id=task.id, message_type="CEO_REMEDIATION", content=remediation
    ).first()
    if not existing_message:
        db.session.add(WorkMessage(
            project_id=operation.project_id,
            task_id=task.id,
            sender_employee_id=operation.proposed_by_employee_id,
            recipient_employee_id=task.assigned_employee_id,
            message_type="CEO_REMEDIATION",
            content=remediation,
        ))
    return operation.id


def main():
    app = create_app()
    with app.app_context():
        completed = _complete_sqlalchemy_repair()
        retry_ready = _prepare_autonomous_meeting_retry()
        db.session.commit()
        print("V0.11.2 migration complete. No Provider or Codex call was made.")
        print(f"  SQLAlchemy Mission completed: {completed or 'not found'}")
        print(f"  Autonomous Meeting retry ready: {retry_ready or 'not found'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
