"""Project-first Founder read models for Eason One V0.13.

The Founder should reason about durable Projects, not runtime machinery.  This
module intentionally keeps the HQ and Project overview cheap: it reads Project,
active Run, Meeting, Result, Decision and Cost summaries only.  Mission/Task/Run
internals remain available in Mission Control and are loaded only when opened.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import func

from ..extensions import db
from ..models import (
    AgentRun, CompanyEvent, CostEvent, CostReservation, Employee, ExternalEffectAttempt, KnowledgeItem, Meeting, MeetingParticipant,
    Operation, Project, Proposal, Task, Work, WorkDependency, WorkMessage, WaitCondition,
)
from .company import get_company, remaining, spent
from .stabilization import REAL_WORK, operation_kind

PROJECT_OPEN = {"PLANNING", "ACTIVE", "BLOCKED", "REVIEW", "PARKED"}
PROJECT_TERMINAL = {"COMPLETED", "FAILED", "CANCELLED"}
MEETING_OPEN = {"PLANNED", "WAITING_FOR_INPUTS", "READY", "RUNNING", "SYNTHESIZING", "PAUSED", "WAITING_FOR_FOUNDER"}
RUN_LIVE = {"RUNNING"}
OP_TERMINAL = {"COMPLETED", "FAILED", "TERMINATED_BY_FOUNDER", "SUPERSEDED"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _dt(value):
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _age(value) -> str:
    value = _dt(value)
    if not value:
        return "No update yet"
    seconds = max(0, int((_now() - value).total_seconds()))
    if seconds < 60:
        return "Just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    return f"{hours // 24}d ago"


def _short(value: str | None, limit: int = 180) -> str:
    text = " ".join((value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _founder_state_detail(state: str, reason: str | None) -> str:
    """Translate durable engine truth without hiding its technical code."""
    text = " ".join(str(reason or "").split())
    if "BLOCKED_MISSING_EVIDENCE" in text or "persisted evidence is missing" in text.casefold():
        return (
            "目前缺少可驗證的既有證據，因此這個研究分支已停止；"
            "它會保持等待，直到出現新的證據來源或新的受治理決策。 "
            "Technical: BLOCKED_MISSING_EVIDENCE."
        )
    if not text:
        return str(state or "IDLE").replace("_", " ").title()
    return text


def _money(value: Any) -> Decimal:
    return Decimal(value or 0)


def _real_operations(project_id: int) -> list[Operation]:
    rows = Operation.query.filter_by(project_id=project_id).order_by(
        Operation.updated_at.desc(), Operation.id.desc()
    ).all()
    return [row for row in rows if operation_kind(row) == REAL_WORK]


def _project_is_dormant_history(project_id: int) -> bool:
    """Pre-v0.18 Project execution/proposals are audit history, not live work."""
    rows = _real_operations(project_id)
    if not rows:
        return False
    core = __import__(
        "eason_one.services.core_v018", fromlist=["is_v018_declared_operation"]
    )
    return not any(core.is_v018_declared_operation(row) for row in rows)


def _project_attention_map() -> dict[int, list[dict[str, Any]]]:
    rows = __import__(
        "eason_one.services.current_company", fromlist=["unresolved_governance"]
    ).unresolved_governance()
    mapped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        operation = row.get("operation")
        proposal = row.get("proposal")
        if operation is not None and operation.status in OP_TERMINAL:
            # A terminal Operation cannot still require live Founder authority.
            # Stale pending events remain auditable in Operation memory but must not
            # leak into the Founder attention read model.
            continue
        project_id = getattr(operation, "project_id", None) or getattr(proposal, "project_id", None)
        if not project_id:
            continue
        if _project_is_dormant_history(int(project_id)):
            # Pre-v0.18 authority remains auditable but is never actionable
            # Founder attention after the Core Cutover.
            continue
        event = row.get("event") or {}
        escalation = row.get("escalation")
        mapped[int(project_id)].append({
            **row,
            "escalation_id": getattr(escalation, "id", None),
            "title": getattr(operation, "title", None) or "Project decision",
            "reason": _short(
                event.get("question") or event.get("reason") or event.get("summary")
                or getattr(operation, "waiting_reason", None)
                or "Founder authority is required before the company can continue.",
                180,
            ),
            "project_href": f"/headquarters/projects/{project_id}#needs-you",
        })
    # Company Core vNext canonical Governance is the first-class
    # Founder-attention truth. It already enforces one current gate per Project.
    governance = __import__(
        "eason_one.services.governance", fromlist=["attention"]
    )
    for escalation in reversed(governance.attention()):
        if _project_is_dormant_history(int(escalation.project_id)):
            continue
        existing = mapped[int(escalation.project_id)]
        if any((item.get("escalation_id") == escalation.id) for item in existing):
            continue
        authority_type = governance.normalize_type(escalation.escalation_type)
        approve_payload = governance.approve_option(escalation)
        existing.append({
            "escalation_id": escalation.id,
            "operation": escalation.operation,
            "proposal": None,
            "event": {
                "kind": escalation.escalation_type,
                "status": escalation.state,
                "reason": escalation.reason,
            },
            "title": "Founder decision",
            "reason": _short(escalation.reason, 180),
            "project_href": f"/headquarters/projects/{escalation.project_id}#needs-you",
            # Canonical Project Governance is both the read and action surface.
            # The UI receives the exact frozen APPROVE payload for display only;
            # APPROVE itself submits no mutable terms and the resolver consumes
            # the Escalation's durable frozen payload.
            "governance_action": (
                f"/headquarters/projects/{escalation.project_id}"
                f"/governance/{escalation.id}"
            ),
            "authority_type": authority_type,
            "approve_payload": approve_payload,
            "supports_modify": authority_type in {
                "BUDGET_AUTHORIZATION",
                "PROJECT_DEADLINE_CHANGE",
                "PROJECT_SCOPE_CHANGE",
                "PROJECT_CONSTRAINT_CHANGE",
            },
        })

    # v0.20 Company Kernel permits only one current blocking Founder gate per
    # Project. If historical stale rows survived an earlier build, the Founder
    # surface must not multiply them into several simultaneous decisions. Keep
    # the newest first-class Escalation visible; older rows remain in audit data.
    for project_id, items in list(mapped.items()):
        escalation_items = [item for item in items if item.get("escalation_id") is not None]
        if len(escalation_items) <= 1:
            continue
        latest = max(escalation_items, key=lambda item: int(item.get("escalation_id") or 0))
        mapped[project_id] = [
            item for item in items
            if item.get("escalation_id") is None or item is latest
        ]
    return mapped


def _live_runs_by_project() -> dict[int, list[AgentRun]]:
    rows = AgentRun.query.filter(
        AgentRun.status.in_(RUN_LIVE),
        AgentRun.project_id.isnot(None),
        AgentRun.purpose != "CEO_FOUNDER_REQUEST",
    ).order_by(AgentRun.id.desc()).all()
    core = __import__("eason_one.services.core_v018", fromlist=["is_v018_operation"])
    mapped: dict[int, list[AgentRun]] = defaultdict(list)
    for row in rows:
        operation = db.session.get(Operation, row.operation_id) if row.operation_id else None
        if operation is None and row.work_id:
            work = db.session.get(Work, row.work_id)
            operation = work.operation if work else None
        if not core.is_v018_operation(operation):
            continue
        mapped[int(row.project_id)].append(row)
    return mapped


def _open_meetings_by_project() -> dict[int, list[Meeting]]:
    rows = Meeting.query.filter(
        Meeting.project_id.isnot(None), Meeting.status.in_(MEETING_OPEN)
    ).order_by(Meeting.created_at.desc()).all()
    core = __import__("eason_one.services.core_v018", fromlist=["is_v018_operation"])
    mapped: dict[int, list[Meeting]] = defaultdict(list)
    for row in rows:
        operation = db.session.get(Operation, row.operation_id) if row.operation_id else None
        if not core.is_v018_operation(operation):
            continue
        mapped[int(row.project_id)].append(row)
    return mapped


def _costs_by_project() -> dict[int, Decimal]:
    rows = db.session.query(
        CostEvent.project_id, func.coalesce(func.sum(CostEvent.real_cost_delta), 0)
    ).filter(CostEvent.project_id.isnot(None)).group_by(CostEvent.project_id).all()
    return {int(project_id): _money(value) for project_id, value in rows}


def _project_cost_breakdown(project: Project, card: dict[str, Any]) -> dict[str, Any]:
    """Founder-readable cost truth for one Project.

    This is a projection over persisted CostEvent / CostReservation records. It
    never estimates future provider spend and never mutates budget authority.
    """
    events = CostEvent.query.filter_by(project_id=project.id).order_by(CostEvent.id).all()
    employee_ids = sorted({row.employee_id for row in events if row.employee_id})
    employees = {
        row.id: row for row in Employee.query.filter(Employee.id.in_(employee_ids)).all()
    } if employee_ids else {}
    run_ids = sorted({row.agent_run_id for row in events if row.agent_run_id})
    runs = {
        row.id: row for row in AgentRun.query.filter(AgentRun.id.in_(run_ids)).all()
    } if run_ids else {}

    by_employee: dict[int | None, Decimal] = defaultdict(Decimal)
    by_provider: dict[str, Decimal] = defaultdict(Decimal)
    by_stage: dict[str, Decimal] = defaultdict(Decimal)
    for event in events:
        amount = _money(event.real_cost_delta)
        by_employee[event.employee_id] += amount
        run = runs.get(event.agent_run_id)
        provider = (
            str(run.provider_key_snapshot or "").strip()
            if run is not None else ""
        ) or "NON_PROVIDER"
        by_provider[provider] += amount
        by_stage[str(event.stage or event.category or "OTHER").upper()] += amount

    operation_ids = [row.id for row in _real_operations(project.id)]
    reservation_rows = []
    if operation_ids:
        reservation_rows = CostReservation.query.filter(
            CostReservation.operation_id.in_(operation_ids),
            CostReservation.status.in_(["RESERVED", "AMBIGUOUS"]),
        ).order_by(CostReservation.id).all()
    reserved = sum((_money(row.estimated_twd) for row in reservation_rows), Decimal("0"))
    spent_value = sum((_money(row.real_cost_delta) for row in events), Decimal("0"))
    budget_limit = card.get("budget_limit")
    remaining_value = card.get("budget_remaining")
    authority_used = None
    outside_execution_envelope = Decimal("0")
    if budget_limit is not None and remaining_value is not None:
        authority_used = max(Decimal("0"), _money(budget_limit) - _money(remaining_value))
        outside_execution_envelope = max(Decimal("0"), spent_value - authority_used)

    employee_rows = []
    for employee_id, amount in sorted(by_employee.items(), key=lambda item: (item[1], item[0] or 0), reverse=True):
        employee = employees.get(employee_id)
        employee_rows.append({
            "employee_id": employee_id,
            "employee_name": employee.name if employee else "Company / planning",
            "amount": amount,
        })
    provider_rows = [
        {"provider": provider, "amount": amount}
        for provider, amount in sorted(by_provider.items(), key=lambda item: (item[1], item[0]), reverse=True)
    ]
    stage_rows = [
        {"stage": stage, "amount": amount}
        for stage, amount in sorted(by_stage.items(), key=lambda item: (item[1], item[0]), reverse=True)
    ]
    return {
        "spent": spent_value,
        "observed_model_cost": spent_value,
        "reserved": reserved,
        "budget_limit": budget_limit,
        "remaining": remaining_value,
        "execution_authority_used": authority_used,
        "outside_execution_envelope": outside_execution_envelope,
        "event_count": len(events),
        "reservation_count": len(reservation_rows),
        "by_employee": employee_rows[:8],
        "by_provider": provider_rows[:8],
        "by_stage": stage_rows[:8],
        "truth_note": (
            "Observed local model cost is a billing ledger. Project execution authority is a separate Founder-approved envelope; "
            "pre-approval/planning cost may appear in observed cost without consuming that envelope. No unobserved provider billing is invented."
        ),
    }


def _task_counts(project_id: int) -> dict[str, int]:
    return {
        status: int(count)
        for status, count in db.session.query(Task.status, func.count(Task.id))
        .filter(Task.project_id == project_id)
        .group_by(Task.status).all()
    }


def _run_is_validated_result(run: AgentRun | None, files, checks, parsed: dict[str, Any]) -> bool:
    if not run or run.status != "SUCCEEDED" or run.structured_validation_status != "PASSED":
        return False
    if run.provider_key_snapshot == "codex":
        context = dict(run.context_composition_json or {})
        if context.get("read_only"):
            return bool(checks or parsed.get("result_summary") or parsed.get("result"))
        host = dict(context.get("host_validation") or {})
        return bool(
            files
            and host.get("attempted")
            and host.get("success")
            and host.get("repository_delta_match") is True
        )
    return bool(files or checks or parsed.get("result_summary") or parsed.get("result"))


def _project_result_view(version) -> dict[str, Any]:
    """Founder-safe projection of the durable PROJECT_RESULT payload.

    The accepted ArtifactVersion stores complete machine/audit lineage as JSON.
    Founder surfaces must show the delivered company outcome, not a truncated
    serialization of that internal record.
    """
    try:
        payload = json.loads(version.content_text or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        payload = {}
    founder = dict(payload.get("founder_summary") or {})
    criteria = list(payload.get("criterion_results") or founder.get("criterion_results") or [])
    accepted_outputs = list(payload.get("accepted_outputs") or [])
    team = []
    for row in accepted_outputs:
        name = str((row or {}).get("employee_name") or "").strip()
        if name and name not in team:
            team.append(name)
    satisfied = sum(1 for row in criteria if str((row or {}).get("status") or "").upper() == "SATISFIED")
    summary = " ".join(str(founder.get("summary") or founder.get("result") or "").split()).strip()
    if not summary:
        summary = (
            f"Verified Project outcome from {len(accepted_outputs)} accepted team output(s)"
            + (f" by {', '.join(team)}" if team else "")
            + "."
        )
    criterion_view = []
    for row in criteria[:8]:
        if not isinstance(row, dict):
            continue
        criterion_view.append({
            "criterion_id": str(row.get("criterion_id") or "").strip(),
            "criterion": str(row.get("criterion") or "").strip(),
            "status": str(row.get("status") or "UNPROVEN").strip().upper(),
            "evidence": _short(str(row.get("evidence") or ""), 220),
        })
    deliverables = []
    seen_deliverables = set()
    ArtifactVersion = __import__("eason_one.models", fromlist=["ArtifactVersion"]).ArtifactVersion
    artifact_service = __import__("eason_one.services.artifacts", fromlist=["deliverable_targets"])
    for output in accepted_outputs:
        version_id = (output or {}).get("artifact_version_id") if isinstance(output, dict) else None
        version = db.session.get(ArtifactVersion, version_id) if version_id else None
        if version is None:
            continue
        for item in artifact_service.deliverable_targets(version):
            key = (version.id, int(item["index"]))
            if key in seen_deliverables:
                continue
            seen_deliverables.add(key)
            deliverables.append({
                "artifact_version_id": version.id,
                "path": item["path"],
                "name": item["name"],
                "available": bool(item["available"]),
                "integrity": item.get("integrity"),
                "href": f"/headquarters/artifacts/{version.id}/deliverables/{item['index']}",
            })

    return {
        "summary": _short(summary, 360),
        "headline": str(founder.get("headline") or "Verified Project Outcome").strip()[:160],
        "result": _short(str(founder.get("result") or ""), 360),
        "criteria_satisfied": satisfied,
        "criteria_total": len(criteria),
        "criteria": criterion_view,
        "team_names": team[:8],
        "accepted_output_count": len(accepted_outputs),
        "cost_twd": founder.get("cost_twd"),
        "deliverables": deliverables,
    }


def _result_rows(project: Project, limit: int = 8) -> list[dict[str, Any]]:
    """Return durable results without confusing historical proof with current authority."""
    rows: list[dict[str, Any]] = []
    try:
        current_result_proof = __import__(
            "eason_one.services.project_outcome", fromlist=["result_ready_proof"]
        ).result_ready_proof(project)
    except Exception:
        # Founder read surfaces stay available when authority/result integrity is
        # broken. Company Truth will surface the Project as recovery/blocking.
        current_result_proof = None
    current_result_version_id = getattr((current_result_proof or {}).get("version"), "id", None)
    vnext_rows = __import__(
        "eason_one.services.company_truth", fromlist=["accepted_artifact_rows"]
    ).accepted_artifact_rows(project.id, limit=limit)
    seen_execution_ids = set()
    for item in vnext_rows:
        run = item["run"]
        version = item["version"]
        artifact_type = item["artifact"].artifact_type
        project_result = _project_result_view(version) if artifact_type == "PROJECT_RESULT" else None
        current_project_result = bool(
            artifact_type == "PROJECT_RESULT" and version.id == current_result_version_id
        )
        summary = project_result["summary"] if project_result else _short(item["summary"], 220)
        if project_result:
            deliverables = list(project_result.get("deliverables") or [])
        else:
            artifact_service = __import__(
                "eason_one.services.artifacts", fromlist=["deliverable_targets"]
            )
            deliverables = [{
                "artifact_version_id": version.id,
                "path": target["path"],
                "name": target["name"],
                "available": bool(target["available"]),
                "integrity": target.get("integrity"),
                "href": f"/headquarters/artifacts/{version.id}/deliverables/{target['index']}",
            } for target in artifact_service.deliverable_targets(version)]
        available_deliverables = [row for row in deliverables if row.get("available")]
        artifact_service = __import__(
            "eason_one.services.artifacts", fromlist=["readable_artifact"]
        )
        readable = artifact_service.readable_artifact(version)
        readable_href = f"/headquarters/artifacts/{version.id}" if readable.get("available") else None
        audit_href = f"/headquarters/system/runs/{run.id}" if run else None
        rows.append({
            "kind": artifact_type,
            "verification": (
                "VALIDATED" if artifact_type != "PROJECT_RESULT" or current_project_result
                else "SUPERSEDED"
            ),
            "current_project_result": current_project_result,
            "title": project_result["headline"] if project_result else item["title"],
            "summary": summary,
            "result_detail": project_result["result"] if project_result else None,
            "criteria_satisfied": project_result["criteria_satisfied"] if project_result else None,
            "criteria_total": project_result["criteria_total"] if project_result else None,
            "criteria": project_result["criteria"] if project_result else [],
            "team_names": project_result["team_names"] if project_result else [],
            "accepted_output_count": project_result["accepted_output_count"] if project_result else None,
            "cost_twd": project_result["cost_twd"] if project_result else None,
            "task": (db.session.get(Task, run.task_id) if run and run.task_id else None),
            "run": run,
            "employee": item["employee"],
            "files": [],
            "checks": [],
            "updated": item["updated"],
            "href": (
                available_deliverables[0]["href"]
                if available_deliverables else (readable_href or audit_href or f"/headquarters/projects/{project.id}#results")
            ),
            "audit_href": audit_href,
            "readable_href": readable_href,
            "readable": bool(readable_href),
            "deliverables": deliverables,
            "artifact_version_id": version.id,
        })
        if run:
            seen_execution_ids.add(run.id)
    tasks = Task.query.filter(
        Task.project_id == project.id,
        Task.status == "DONE",
        Task.result_summary.isnot(None),
    ).order_by(Task.completed_at.desc(), Task.updated_at.desc()).limit(limit * 2).all()
    for task in tasks:
        summary = (task.result_summary or "").strip()
        if not summary:
            continue
        run = AgentRun.query.filter_by(task_id=task.id).order_by(AgentRun.id.desc()).first()
        if run and run.id in seen_execution_ids:
            continue
        parsed = dict(run.parsed_output_json or {}) if run else {}
        codex = dict(parsed.get("codex") or {})
        files = codex.get("changed_files") or parsed.get("changed_files") or []
        checks = codex.get("tests") or parsed.get("tests") or []
        validated = _run_is_validated_result(run, files, checks, parsed)
        rows.append({
            "kind": "ARTIFACT" if files else "TASK_RESULT",
            "verification": "VALIDATED" if validated else "UNVERIFIED",
            "title": task.title,
            "summary": _short(summary, 220),
            "task": task,
            "run": run,
            "employee": task.assigned_employee,
            "files": files,
            "checks": checks,
            "updated": task.completed_at or task.updated_at,
            "href": f"/headquarters/projects/{project.id}#results",
            "audit_href": (f"/headquarters/system/runs/{run.id}" if run else None),
            "deliverables": [],
        })
        if len(rows) >= limit:
            break
    for operation in _real_operations(project.id):
        if operation.status != "COMPLETED":
            continue
        report = operation.founder_report_json or {}
        summary = (report.get("result") or report.get("summary") or "").strip()
        if not summary:
            continue
        rows.append({
            "kind": "PROJECT_DELIVERY",
            "verification": "VALIDATED" if report.get("verification") else "DELIVERED",
            "title": operation.title,
            "summary": _short(summary, 220),
            "task": None,
            "run": None,
            "employee": operation.proposed_by,
            "files": report.get("artifacts") or [],
            "checks": report.get("checks") or [],
            "updated": operation.ended_at or operation.updated_at,
            "href": f"/headquarters/projects/{project.id}#results",
            "audit_href": None,
            "deliverables": [],
        })
    rows.sort(
        key=lambda row: (
            1 if row.get("current_project_result") else 0,
            _dt(row["updated"]) or datetime.min.replace(tzinfo=timezone.utc),
        ),
        reverse=True,
    )
    for row in rows:
        row["age"] = _age(row["updated"])
    return rows[:limit]


def _project_team(project: Project, live_runs=None) -> list[dict[str, Any]]:
    ids: set[int] = {project.owner_employee_id}
    for task in Task.query.filter_by(project_id=project.id).all():
        if task.assigned_employee_id:
            ids.add(task.assigned_employee_id)
        if task.reviewer_employee_id:
            ids.add(task.reviewer_employee_id)
    from ..models import Work, WorkAssignment
    ids.update(
        employee_id for (employee_id,) in db.session.query(WorkAssignment.employee_id)
        .join(Work).filter(
            Work.project_id == project.id, WorkAssignment.ended_at.is_(None)
        ).all()
        if employee_id
    )
    meeting_ids = [row.id for row in Meeting.query.filter_by(project_id=project.id).all()]
    if meeting_ids:
        participants = MeetingParticipant.query.filter(
            MeetingParticipant.meeting_id.in_(meeting_ids),
            MeetingParticipant.removed_at.is_(None),
        ).all()
        ids.update(row.employee_id for row in participants)
    live_employee_ids = {row.employee_id for row in (live_runs or [])}
    employees = Employee.query.filter(Employee.id.in_(ids)).order_by(Employee.id).all() if ids else []
    return [{
        "employee": employee,
        "working": employee.id in live_employee_ids,
        "initials": "".join(part[0] for part in employee.name.split()[:2]).upper() or employee.name[:2].upper(),
    } for employee in employees]


def project_card(project: Project, *, live_runs=None, open_meetings=None, attention=None, cost=None, include_results: bool = True) -> dict[str, Any]:
    live_runs = list(live_runs or [])
    open_meetings = list(open_meetings or [])
    attention = list(attention or [])
    cost = _money(cost)
    counts = _task_counts(project.id)
    total = sum(counts.values())
    done = counts.get("DONE", 0) + counts.get("CANCELLED", 0)
    blocked = counts.get("BLOCKED", 0)
    real_ops = _real_operations(project.id)
    current_op = next((row for row in real_ops if row.status not in OP_TERMINAL), None)
    core = __import__(
        "eason_one.services.core_v018",
        fromlist=["is_v018_operation", "is_v018_declared_operation"],
    )
    has_v018 = any(core.is_v018_operation(row) for row in real_ops)
    has_v018_lane = any(core.is_v018_declared_operation(row) for row in real_ops)
    dormant_history = bool(real_ops) and not has_v018_lane
    terminal_projection = str(project.status or "").upper() in {"COMPLETED", "CANCELLED", "FAILED"}
    if dormant_history or terminal_projection:
        # Historical provider/budget/meeting rows remain queryable audit truth,
        # but neither retired history nor terminal Projects may surface them as
        # current Founder attention or live Company activity.
        attention = []
        live_runs = []
        open_meetings = []
    results = _result_rows(project, limit=3) if include_results else []
    latest_result = results[0] if results else None
    truth = __import__(
        "eason_one.services.company_truth", fromlist=["project_snapshot"]
    ).project_snapshot(project)
    contract_read = __import__(
        "eason_one.services.project_contract", fromlist=["read_projection"]
    ).read_projection(project)
    terms = contract_read.get("terms") or {}
    contract_integrity_error = contract_read.get("integrity_error")
    governed_budget = terms.get("budget_limit_twd")

    if truth["has_vnext_truth"]:
        total = truth["work_total"]
        done = truth["work_accepted"] + truth["work_cancelled"]
        blocked = truth["work_counts"].get("WAITING", 0)
        state = truth["state"]
        state_detail = _founder_state_detail(state, truth["reason"])
        progress = truth["progress"]
    else:
        if project.status == "COMPLETED":
            state = "COMPLETED"
            state_detail = "Project goal is closed."
        elif attention:
            state = "NEEDS_YOU"
            state_detail = attention[0]["reason"]
        elif live_runs:
            state = "WORKING"
            run = live_runs[0]
            task = db.session.get(Task, run.task_id) if run.task_id else None
            state_detail = f"{run.employee.name} is working on {task.title if task else run.purpose}."
        elif any(meeting.status == "RUNNING" for meeting in open_meetings):
            state = "MEETING"
            state_detail = f"{next(m.title for m in open_meetings if m.status == 'RUNNING')} is live."
        elif project.status == "REVIEW" and latest_result and latest_result.get("verification") == "VALIDATED":
            state = "RESULT_READY"
            state_detail = "The latest verified company delivery is ready in Project Results."
        elif project.status == "REVIEW":
            state = "BLOCKED"
            state_detail = "A delivery exists, but independent verification is incomplete or failed. It cannot be accepted yet."
        elif blocked or project.status == "BLOCKED":
            state = "BLOCKED"
            state_detail = "Work is blocked; open the Project for the specific reason."
        elif current_op and current_op.status == "PLANNED":
            state = "READY"
            state_detail = "CEO proposal is waiting for Founder authority."
        elif project.status == "PARKED":
            state = "PARKED"
            state_detail = "No company work is scheduled right now."
        else:
            state = "IDLE"
            state_detail = "No employee is executing this Project right now."
        if project.status == "COMPLETED":
            progress = 100
        elif project.status == "REVIEW" and (not latest_result or latest_result.get("verification") != "VALIDATED"):
            progress = min(95, round(done / total * 100) if total else 0)
        else:
            progress = round(done / total * 100) if total else 0
    if dormant_history:
        state = "HISTORICAL"
        state_detail = "Retired v0.17 Work-first record. It is audit history and cannot be scheduled by the v0.18 runtime."
    elif contract_integrity_error:
        # Read surfaces must remain available even when one governed Project
        # has invalid authority truth.  The Project itself remains fail-closed.
        state = "BLOCKED"
        state_detail = f"Governance integrity blocked: {contract_integrity_error}. Company execution is not authorized until Contract truth is repaired."

    return {
        "project": project,
        "href": f"/headquarters/projects/{project.id}",
        "state": state,
        "state_detail": state_detail,
        "progress": progress,
        # v0.18 does not manufacture a percentage from workflow phases. Until
        # formal closure, Founder surfaces show the auditable closed/total Work
        # count instead of a misleading 0%/45% style progress number.
        "show_progress_percent": bool(project.status == "COMPLETED" or (not has_v018 and not dormant_history)),
        "is_dormant_history": dormant_history,
        "done": done,
        "total": total,
        "blocked": blocked,
        "cost": cost,
        "budget_limit": _money(governed_budget) if governed_budget is not None else None,
        # Founder Project authority is the execution envelope. CEO_FOUNDER_REQUEST
        # planning cost remains Company cost but, by design, does not consume this
        # envelope. Keep the Founder surface on the same accounting definition as
        # work_budget.ensure()/operations.project_remaining_authority().
        "budget_remaining": (
            __import__("eason_one.services.operations", fromlist=["project_remaining_authority"])
            .project_remaining_authority(project)
            if governed_budget is not None and not contract_integrity_error else None
        ),
        "contract_integrity_error": contract_integrity_error,
        "live_runs": live_runs,
        "open_meetings": open_meetings,
        "attention": attention,
        "current_operation": current_op,
        "latest_result": latest_result,
        "current_direction": (
            "Retired pre-v0.18 Project record. No runtime is authorized to continue it."
            if dormant_history else _short(project.current_state_summary or terms.get("objective") or project.objective, 210)
        ),
        "next_step": (
            "Audit only. Create or approve a v0.18 Project to resume this outcome."
            if dormant_history else _short(project.next_milestone or "CEO has not recorded the next milestone yet.", 150)
        ),
        "updated_label": _age(project.updated_at),
    }


def _reconcile_superseded_project_proposals(project_id: int | None = None) -> list[int]:
    """Supersede legacy pending Project proposals after verified delivery exists.

    V0.13 executes Founder-approved work through governed Operations. A historical
    Proposal row must not remain an immortal Founder gate after that Project has
    already reached REVIEW/COMPLETED with an independently validated Result. The
    Proposal stays in the audit trail, but ceases to be actionable.
    """
    query = Proposal.query.filter_by(status="PENDING")
    if project_id is not None:
        query = query.filter_by(project_id=project_id)
    changed: list[int] = []
    for proposal in query.order_by(Proposal.id).all():
        if (proposal.payload_json or {}).get("type") != "PROJECT_PLAN":
            continue
        project = db.session.get(Project, proposal.project_id) if proposal.project_id else None
        if not project or project.environment != "LIVE" or project.status not in {"REVIEW", "COMPLETED"}:
            continue
        approved_operation = Operation.query.filter(
            Operation.project_id == project.id, Operation.approved_at.isnot(None)
        ).order_by(Operation.id.desc()).first()
        if not approved_operation:
            continue
        results = _result_rows(project, limit=3)
        if not any(row.get("verification") == "VALIDATED" for row in results):
            continue
        proposal.status = "SUPERSEDED"
        proposal.review_note = (
            "Superseded by Founder-approved Project execution and independently "
            "validated delivery; retained as historical proposal audit evidence."
        )
        proposal.reviewed_at = _now()
        changed.append(proposal.id)
    if changed:
        db.session.commit()
    return changed


def _project_cards(include_completed: bool = True, *, include_results: bool = True) -> list[dict[str, Any]]:
    projects = Project.query.filter_by(environment="LIVE").order_by(Project.updated_at.desc()).all()
    if not include_completed:
        projects = [row for row in projects if row.status not in PROJECT_TERMINAL]
    live = _live_runs_by_project()
    meetings = _open_meetings_by_project()
    attention = _project_attention_map()
    costs = _costs_by_project()
    return [project_card(
        project,
        live_runs=live.get(project.id),
        open_meetings=meetings.get(project.id),
        attention=attention.get(project.id),
        cost=costs.get(project.id),
        include_results=include_results,
    ) for project in projects]


def _company_activity(limit: int = 12, project_id: int | None = None) -> list[dict[str, Any]]:
    """Project/company activity from Company Truth, with legacy Task fallback only.

    Governed Projects emit CompanyEvent rows from Work/Artifact/Meeting/Result
    transitions. Founder surfaces must not reconstruct current activity from the
    compatibility Task projection because that can lag the authoritative Work.
    """
    rows: list[dict[str, Any]] = []
    event_query = CompanyEvent.query
    if project_id:
        event_query = event_query.filter(CompanyEvent.project_id == project_id)
    interesting = {
        "WORK_ASSIGNED", "WORK_REASSIGNED", "WORK_STARTED", "WORK_VERIFYING",
        "WORK_ACCEPTED", "WORK_WAITING", "WORK_ABANDONED",
        "COMPANY_WORK_WAVE_STARTED", "COMPANY_WORK_WAVE_COMPLETED",
        "ARTIFACT_SUBMITTED", "ARTIFACT_ACCEPTED", "ARTIFACT_REJECTED",
        "MEETING_CREATED", "MEETING_STARTED", "MEETING_COMPLETED",
        "EMPLOYEE_HIRED", "PROJECT_CONTINUATION_CREATED",
        "PROJECT_RESULT_READY", "FOUNDER_PROJECT_COMPLETED",
    }
    events = (event_query.filter(CompanyEvent.event_type.in_(interesting))
              .order_by(CompanyEvent.id.desc()).limit(limit * 5).all())
    for event in events:
        project = db.session.get(Project, event.project_id) if event.project_id else None
        if project is not None and project.environment != "LIVE":
            continue
        work = db.session.get(Work, event.work_id) if event.work_id else None
        actor = (db.session.get(Employee, event.actor_id)
                 if event.actor_type == "EMPLOYEE" and event.actor_id else None)
        payload = dict(event.payload_json or {})
        label = event.event_type.replace("_", " ").title()
        subject = work.title if work else (project.name if project else "Company")
        person = actor.name if actor else "Company"
        title_map = {
            "WORK_ASSIGNED": f"{person} assigned {subject}",
            "WORK_REASSIGNED": f"Company reassigned {subject}",
            "WORK_STARTED": f"{person} started {subject}",
            "WORK_VERIFYING": f"{subject} entered verification",
            "WORK_ACCEPTED": f"{subject} was accepted",
            "WORK_WAITING": f"{subject} is waiting",
            "WORK_ABANDONED": f"{subject} needs recovery",
            "COMPANY_WORK_WAVE_STARTED": "Independent Employee work started in parallel",
            "COMPANY_WORK_WAVE_COMPLETED": "Parallel Employee work wave completed",
            "ARTIFACT_SUBMITTED": f"{subject} produced a reviewable artifact",
            "ARTIFACT_ACCEPTED": f"Artifact from {subject} was accepted",
            "ARTIFACT_REJECTED": f"Artifact from {subject} was rejected",
            "MEETING_CREATED": "Company created a coordination Meeting",
            "MEETING_STARTED": "Company Meeting started",
            "MEETING_COMPLETED": "Company Meeting completed",
            "EMPLOYEE_HIRED": "Company added a Persistent Employee",
            "PROJECT_CONTINUATION_CREATED": f"Company sequenced the next move for {subject}",
            "PROJECT_RESULT_READY": f"{subject} reached Result Ready",
            "FOUNDER_PROJECT_COMPLETED": f"Founder completed {subject}",
        }
        detail = (payload.get("reason") or payload.get("title") or payload.get("summary")
                  or payload.get("required_capability") or label)
        rows.append({
            "kind": event.event_type.replace("PROJECT_", "").replace("WORK_", "WORK ").replace("_", " "),
            "title": title_map.get(event.event_type, f"{label} · {subject}"),
            "detail": _short(str(detail), 150),
            "project": project,
            "at": event.created_at,
            "href": (
                f"/headquarters/projects/{event.project_id}"
                if event.project_id else "/headquarters"
            ),
        })
        if len(rows) >= limit:
            break

    # Legacy Projects never emitted the v0.20 event contract. Keep their Task
    # history readable without letting it compete with governed Project truth.
    if len(rows) < limit:
        task_query = Task.query.join(Project).filter(Project.environment == "LIVE")
        if project_id:
            task_query = task_query.filter(Task.project_id == project_id)
        for task in task_query.order_by(Task.updated_at.desc()).limit(limit * 2).all():
            if task.work_id is not None or task.status not in {"DONE", "BLOCKED", "REVIEW"}:
                continue
            verb = {"DONE": "completed", "BLOCKED": "blocked", "REVIEW": "submitted for review"}.get(task.status, task.status.lower())
            person = task.assigned_employee.name if task.assigned_employee else "Company"
            rows.append({
                "kind": "LEGACY WORK",
                "title": f"{person} {verb} {task.title}",
                "detail": _short(task.result_summary or task.objective, 150),
                "project": task.project,
                "at": task.updated_at,
                "href": f"/headquarters/projects/{task.project_id}",
            })
            if len(rows) >= limit:
                break
    rows.sort(key=lambda row: _dt(row["at"]) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    for row in rows[:limit]:
        row["age"] = _age(row["at"])
    return rows[:limit]


def _working_people(project_id: int | None = None) -> list[dict[str, Any]]:
    """Durable current Employee ownership, not a millisecond AgentRun sample.

    A Work-first company remains active between provider calls. READY, VERIFYING
    and internally WAITING Work must stay visible so Headquarters cannot flash
    false idle while the Company Kernel owns the next step.
    """
    query = Work.query.join(Project).filter(
        Project.environment == "LIVE",
        Project.status.notin_(PROJECT_TERMINAL | {"PAUSED"}),
        Work.state.in_(["READY", "EXECUTING", "VERIFYING", "WAITING"]),
    )
    if project_id:
        query = query.filter(Work.project_id == project_id)
    core = __import__("eason_one.services.core_v018", fromlist=["is_v018_operation"])
    candidates = []
    rank = {"EXECUTING": 0, "VERIFYING": 1, "READY": 2, "WAITING": 3}
    for work in query.order_by(Work.updated_at.desc(), Work.id.desc()).all():
        if not core.is_v018_operation(work.operation):
            continue
        assignment = next((row for row in reversed(work.assignments) if row.ended_at is None), None)
        if not assignment or not assignment.employee or not assignment.employee.active:
            continue
        run = (AgentRun.query.filter_by(work_id=work.id, status="RUNNING")
               .order_by(AgentRun.id.desc()).first())
        # Management Work persists for the full Mission. It is not evidence that
        # the CEO is actively doing something between management executions.
        # Show management only while a durable Run is genuinely RUNNING.
        if work.work_type == "MANAGEMENT" and run is None:
            continue
        wait = (__import__("eason_one.services.work_runtime", fromlist=["primary_gate"])
                .primary_gate(work) if work.state == "WAITING" else None)
        candidates.append((rank.get(work.state, 9), work.updated_at, {
            "employee": assignment.employee,
            "task": db.session.get(Task, run.task_id) if run and run.task_id else None,
            "work": work,
            "project": work.project,
            "run": run,
            "status": "LIVE" if run else work.state,
            "wait": wait,
            "initials": "".join(part[0] for part in assignment.employee.name.split()[:2]).upper() or assignment.employee.name[:2].upper(),
        }))
    # One Founder-facing row per Employee. If someone owns several branches,
    # show the most active one first; Project detail still lists every Work.
    candidates.sort(key=lambda row: (row[0], -(_dt(row[1]) or datetime.min.replace(tzinfo=timezone.utc)).timestamp()))
    seen: set[int] = set()
    rows = []
    for _, __, row in candidates:
        if row["employee"].id in seen:
            continue
        seen.add(row["employee"].id)
        rows.append(row)
    return rows


_BLOCKING_WAIT_TYPES = {
    "AUTHORITY", "BUDGET", "DEPENDENCY", "FOUNDER_DECISION", "LEGACY_STATE",
    "AUTHORITY_EXHAUSTED", "SYSTEM_RECOVERY", "RECONCILIATION", "FOUNDER_PAUSE",
}


def _work_command_state(work: Work, wait: WaitCondition | None) -> str:
    """Translate durable Work state into the five Founder command lanes.

    This projection intentionally never infers Work state from an AgentRun. A
    failed attempt can be retried while its Work remains active or waiting;
    only the persisted Work lifecycle and its open WaitCondition own the label.
    """
    if work.state == "WAITING":
        return "BLOCKED" if wait and wait.condition_type in _BLOCKING_WAIT_TYPES else "WAITING"
    if work.state == "ABANDONED":
        return "FAILED"
    if work.state == "ACCEPTED":
        return "COMPLETED"
    if work.state == "CANCELLED":
        return "CANCELLED"
    return "ACTIVE"


def work_command_snapshot(project_id: int | None = None) -> dict[str, Any]:
    """Return a pure, authoritative Founder command view of persisted Work."""
    query = Work.query.join(Project).filter(Project.environment == "LIVE")
    if project_id is not None:
        query = query.filter(Work.project_id == project_id)
    else:
        # Global command lanes are present-tense operating truth. Terminal
        # Project Work remains visible on that Project's history page, but stale
        # READY/WAITING rows must not appear as company-wide ACTIVE/BLOCKED work.
        query = query.filter(Project.status.notin_(PROJECT_TERMINAL))
    works = query.order_by(Work.updated_at.desc(), Work.id.desc()).all()
    rows: list[dict[str, Any]] = []
    for work in works:
        assignments = sorted(work.assignments, key=lambda item: item.id)
        assignment = next(
            (item for item in reversed(assignments) if item.ended_at is None),
            assignments[-1] if assignments else None,
        )
        if not __import__(
            "eason_one.services.core_v018", fromlist=["is_v018_operation"]
        ).is_v018_operation(work.operation):
            continue
        wait = __import__(
            "eason_one.services.work_runtime", fromlist=["primary_gate"]
        ).primary_gate(work)
        lane = _work_command_state(work, wait)
        rows.append({
            "work": work,
            "state": lane,
            "project": work.project,
            "employee": assignment.employee if assignment else None,
            "wait": wait,
            "updated_label": _age(work.updated_at),
            "href": f"/headquarters/projects/{work.project_id}#work",
        })
    lanes = {
        state: [row for row in rows if row["state"] == state]
        for state in ("ACTIVE", "WAITING", "BLOCKED", "FAILED", "COMPLETED", "CANCELLED")
    }
    return {"rows": rows, "lanes": lanes, "counts": {key: len(value) for key, value in lanes.items()}}



_PULSE_FOUNDER_WAIT_TYPES = {"FOUNDER_DECISION", "BUDGET", "AUTHORITY", "FOUNDER_PAUSE"}
_PULSE_RECOVERY_WAIT_TYPES = {"SYSTEM_RECOVERY", "RECONCILIATION"}
_PULSE_ACTIVE_WORK_STATES = {"READY", "EXECUTING", "VERIFYING", "WAITING"}
_PULSE_EVENT_TYPES = {
    "EXECUTION_STARTED", "EXECUTION_SUCCEEDED", "EXECUTION_FAILED_SAFE",
    "EXECUTION_FAILED_KNOWN", "EXECUTION_FAILED_AMBIGUOUS",
    "WORK_ASSIGNED", "WORK_REASSIGNED", "WORK_STARTED", "WORK_VERIFYING",
    "WORK_ACCEPTED", "WORK_WAITING", "WORK_ABANDONED", "WORK_SYSTEM_RECONCILED",
    "PROJECT_RECOVERED", "PROJECT_CONTINUATION_CREATED", "PROJECT_RESULT_READY",
    "ARTIFACT_SUBMITTED", "ARTIFACT_ACCEPTED", "ARTIFACT_REJECTED",
    "MARKET_ORDER_OPENED", "MARKET_CONTRACT_AWARDED", "MARKET_CONTRACT_SETTLED",
    "MARKET_SETTLEMENT_BLOCKED",
}


def _pulse_age(value) -> str:
    value = _dt(value)
    if value is None:
        return "no timestamp"
    seconds = max(0, int((_now() - value).total_seconds()))
    if seconds < 60:
        return f"{seconds}s ago"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    return f"{hours // 24}d ago"


def _pulse_elapsed(value) -> str | None:
    value = _dt(value)
    if value is None:
        return None
    seconds = max(0, int((_now() - value).total_seconds()))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def _pulse_effect(run: AgentRun | None) -> ExternalEffectAttempt | None:
    if run is None:
        return None
    return (
        ExternalEffectAttempt.query.filter_by(execution_id=run.id)
        .order_by(ExternalEffectAttempt.id.desc()).first()
    )


def _pulse_effect_at(effect: ExternalEffectAttempt | None):
    if effect is None:
        return None
    return (
        effect.settled_at or effect.persisted_at or effect.response_received_at
        or effect.dispatched_at or effect.created_at
    )


def _pulse_effect_label(effect: ExternalEffectAttempt | None) -> str | None:
    if effect is None:
        return None
    return {
        "PREPARED": "Preparing provider request",
        "RESERVED": "Budget reserved · preparing provider",
        "DISPATCHING": "Provider request dispatched",
        "RESPONSE_RECEIVED": "Provider response received",
        "PERSISTED": "Provider response persisted",
        "SETTLED": "Provider call settled",
        "FAILED_PRE_DISPATCH": "Provider call failed before dispatch",
        "REJECTED_POST_DISPATCH": "Provider rejected the request",
        "AMBIGUOUS_POST_DISPATCH": "Provider result requires reconciliation",
    }.get(effect.state, str(effect.state or "Provider state").replace("_", " ").title())


def _pulse_work_status(work: Work, run: AgentRun | None, wait) -> str:
    kind = str(getattr(wait, "condition_type", "") or "")
    if kind in _PULSE_FOUNDER_WAIT_TYPES:
        return "NEEDS_YOU"
    if kind in _PULSE_RECOVERY_WAIT_TYPES:
        return "RECOVERING"
    if run is not None and run.status == "RUNNING":
        # A replacement attempt is real company activity, but Founder-facing
        # language should say recovery rather than pretending this is a clean
        # first execution. The original failed attempt remains available in
        # Run audit through retry_of_run_id / replacement_run_id.
        return "RECOVERING" if run.retry_of_run_id else "WORKING"
    if run is not None and run.status == "FAILED" and run.failure_reason == "MISSING_EVIDENCE":
        return "WAITING"
    if work.state in {"EXECUTING", "VERIFYING"}:
        return "WORKING"
    if work.state in {"READY", "WAITING"}:
        return "WAITING"
    return "IDLE"


def _pulse_task(work: Work) -> Task | None:
    return Task.query.filter_by(work_id=work.id).order_by(Task.id.desc()).first()


def _pulse_owner(work: Work) -> Employee | None:
    assignments = sorted(work.assignments, key=lambda row: row.id)
    assignment = next(
        (row for row in reversed(assignments) if row.ended_at is None),
        assignments[-1] if assignments else None,
    )
    return assignment.employee if assignment else None


def _pulse_downstream(work: Work) -> Work | None:
    edges = WorkDependency.query.filter_by(depends_on_work_id=work.id).order_by(WorkDependency.id).all()
    for edge in edges:
        candidate = edge.work
        if candidate is not None and candidate.state not in {"ACCEPTED", "CANCELLED"}:
            return candidate
    return None


def _pulse_next_step(work: Work, task: Task | None, status: str, wait) -> str:
    if status == "NEEDS_YOU":
        return _short(getattr(wait, "reason", None) or "Founder authority is required before the company can continue.", 150)
    if status == "RECOVERING":
        return "Runtime recovery owns the next action. Founder intervention is not required."
    if status == "WAITING" and wait is None:
        return "If new valid evidence appears, the company can continue; otherwise this Work remains safely paused."
    if work.state == "WAITING" and getattr(wait, "condition_type", None) == "DEPENDENCY":
        upstream = db.session.get(Work, getattr(wait, "target_work_id", None)) if getattr(wait, "target_work_id", None) else None
        return f"Waiting for {upstream.title} to be accepted." if upstream else _short(getattr(wait, "reason", None) or "Waiting on upstream Work.", 150)
    if work.state == "VERIFYING":
        reviewer = getattr(task, "reviewer", None) if task is not None else None
        if reviewer is not None:
            return f"Independent review by {reviewer.name}."
        return "Verification must finish before the Work can be accepted."
    reviewer = getattr(task, "reviewer", None) if task is not None else None
    if reviewer is not None and getattr(task, "assigned_employee_id", None) != getattr(task, "reviewer_employee_id", None):
        return f"After execution → {reviewer.name} review."
    downstream = _pulse_downstream(work)
    if downstream is not None:
        owner = _pulse_owner(downstream)
        return f"Then → {owner.name + ': ' if owner else ''}{downstream.title}."
    return "Then → Company Runtime continues Project closure."


def _pulse_work_detail(work: Work, run: AgentRun | None, effect: ExternalEffectAttempt | None, status: str, wait) -> str:
    if status == "NEEDS_YOU":
        return _short(getattr(wait, "reason", None) or "Founder authority is required.", 160)
    if status == "RECOVERING":
        if run is not None and run.status == "RUNNING" and run.retry_of_run_id:
            return f"Automatic recovery attempt {max(2, int(run.attempt_number or 2))} is running. No Founder action is required."
        return _short(getattr(wait, "reason", None) or "Company Runtime is reconciling a recoverable failure. No Founder action is required.", 160)
    if status == "WAITING" and run is not None and run.failure_reason == "MISSING_EVIDENCE":
        return _founder_state_detail(status, "BLOCKED_MISSING_EVIDENCE")
    if run is not None:
        return _pulse_effect_label(effect) or "Execution is running."
    if work.state == "VERIFYING":
        return "Execution is complete; persisted evidence is being verified."
    if work.state == "READY":
        return "Queued for Company Runtime dispatch; no Founder action is required."
    if work.state == "WAITING":
        return _short(_founder_state_detail(status, getattr(wait, "reason", None) or "Waiting on a durable company dependency."), 220)
    return str(work.state or "IDLE").replace("_", " ").title()


def _pulse_activity(project_ids: set[int], live_runs: list[AgentRun], limit: int = 8) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    # Current provider effect state is a durable ledger projection. It gives the
    # Founder a finer-grained pulse than waiting for a terminal Execution event.
    for run in live_runs:
        effect = _pulse_effect(run)
        if effect is None:
            continue
        actor = db.session.get(Employee, run.employee_id)
        work = db.session.get(Work, run.work_id) if run.work_id else None
        project = db.session.get(Project, run.project_id) if run.project_id else None
        rows.append({
            "kind": "PROVIDER",
            "title": f"{actor.name if actor else 'Employee'} · {_pulse_effect_label(effect)}",
            "detail": _short(
                f"{run.provider_key_snapshot}/{run.model_name_snapshot} · "
                f"{work.title if work else ('Founder request planning' if run.purpose == 'CEO_FOUNDER_REQUEST' else run.purpose)}",
                170,
            ),
            "at": _pulse_effect_at(effect) or run.started_at,
            "href": f"/headquarters/system/runs/{run.id}",
            "event_id": None,
        })

    events = CompanyEvent.query.filter(CompanyEvent.event_type.in_(_PULSE_EVENT_TYPES)).order_by(CompanyEvent.id.desc()).limit(80).all()
    live_run_ids = {run.id for run in live_runs}
    for event in events:
        if event.project_id not in project_ids and event.execution_id not in live_run_ids:
            # Keep very recent Founder-request execution events visible even
            # before a Project exists, but do not let old global audit noise in.
            run = db.session.get(AgentRun, event.execution_id) if event.execution_id else None
            if not (run is not None and run.purpose == "CEO_FOUNDER_REQUEST" and _pulse_age(event.created_at).endswith(("s ago", "m ago"))):
                continue
        run = db.session.get(AgentRun, event.execution_id) if event.execution_id else None
        work = db.session.get(Work, event.work_id) if event.work_id else None
        project = (
            db.session.get(Project, event.project_id) if event.project_id
            else (db.session.get(Project, run.project_id) if run is not None and run.project_id else None)
        )
        actor = db.session.get(Employee, event.actor_id) if event.actor_type == "EMPLOYEE" and event.actor_id else None
        payload = dict(event.payload_json or {})
        subject = work.title if work else (project.name if project else "Founder request")
        provider = (
            f"{run.provider_key_snapshot}/{run.model_name_snapshot}"
            if run is not None else None
        )
        titles = {
            "EXECUTION_STARTED": f"{actor.name if actor else 'Employee'} started {subject}",
            "EXECUTION_SUCCEEDED": f"{actor.name if actor else 'Employee'} finished {subject}",
            "EXECUTION_FAILED_SAFE": f"{actor.name if actor else 'Employee'} hit a safe failure",
            "EXECUTION_FAILED_KNOWN": f"{actor.name if actor else 'Employee'} hit a known provider failure",
            "EXECUTION_FAILED_AMBIGUOUS": f"{actor.name if actor else 'Employee'} needs runtime reconciliation",
            "WORK_ASSIGNED": f"{subject} received an accountable owner",
            "WORK_REASSIGNED": f"Company reassigned {subject}",
            "WORK_STARTED": f"{subject} entered execution",
            "WORK_VERIFYING": f"{subject} entered verification",
            "WORK_ACCEPTED": f"{subject} was accepted",
            "WORK_WAITING": f"{subject} is waiting",
            "WORK_ABANDONED": f"{subject} entered bounded recovery",
            "WORK_SYSTEM_RECONCILED": f"Runtime reconciled {subject}",
            "PROJECT_RECOVERED": f"Company recovered {subject}",
            "PROJECT_CONTINUATION_CREATED": f"Company sequenced the next move for {subject}",
            "PROJECT_RESULT_READY": f"{subject} reached Result Ready",
            "ARTIFACT_SUBMITTED": f"{subject} produced an Artifact",
            "ARTIFACT_ACCEPTED": f"Artifact from {subject} was accepted",
            "ARTIFACT_REJECTED": f"Artifact from {subject} was rejected",
            "MARKET_ORDER_OPENED": f"Eason Market opened demand for {subject}",
            "MARKET_CONTRACT_AWARDED": f"Eason Market awarded {subject}",
            "MARKET_CONTRACT_SETTLED": f"Eason Market settled {subject}",
            "MARKET_SETTLEMENT_BLOCKED": f"Eason Market blocked settlement for {subject}",
        }
        source_count = int(payload.get("provider_source_count") or 0)
        detail_bits = [provider]
        if source_count:
            detail_bits.append(f"{source_count} provider source{'s' if source_count != 1 else ''} observed")
        if event.event_type.startswith("EXECUTION_FAILED"):
            # Founder surfaces explain ownership, not transport/debug jargon.
            # The exact failure_reason/error_text remains available in Run audit.
            detail_bits.append(
                "Company Runtime owns retry or reconciliation; Founder action is only required if a governance gate appears."
            )
        elif payload.get("reason"):
            detail_bits.append(_short(str(payload["reason"]), 110))
        if not any(detail_bits):
            detail_bits.append(event.event_type.replace("_", " ").title())
        rows.append({
            "kind": "EXECUTION" if event.event_type.startswith("EXECUTION_") else "COMPANY",
            "title": titles.get(event.event_type, f"{event.event_type.replace('_', ' ').title()} · {subject}"),
            "detail": " · ".join(bit for bit in detail_bits if bit),
            "at": event.created_at,
            "href": f"/headquarters/system/runs/{run.id}" if run is not None else (f"/headquarters/projects/{project.id}" if project is not None else "/headquarters"),
            "event_id": event.id,
        })
    rows.sort(key=lambda row: _dt(row.get("at")) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    seen: set[tuple] = set()
    result = []
    for row in rows:
        key = (row.get("kind"), row.get("title"), row.get("event_id"), row.get("href"))
        if key in seen:
            continue
        seen.add(key)
        row["age"] = _pulse_age(row.get("at"))
        result.append(row)
        if len(result) >= limit:
            break
    return result


def company_pulse_snapshot() -> dict[str, Any]:
    """Truth-only live Founder pulse.

    This function is deliberately read-only. It never wakes a runtime, retries
    an Execution, creates a CompanyEvent, or constructs a provider. The browser
    may poll it frequently without changing Company Truth.
    """
    company = get_company()
    if not company:
        return {"company": None, "rows": [], "activity": [], "counts": {}}

    core = __import__("eason_one.services.core_v018", fromlist=["is_v018_operation"])
    live_runs = AgentRun.query.filter_by(status="RUNNING").order_by(AgentRun.id.desc()).all()
    project_ids: set[int] = set()
    for run in live_runs:
        work = db.session.get(Work, run.work_id) if run.work_id else None
        operation = work.operation if work is not None else (db.session.get(Operation, run.operation_id) if run.operation_id else None)
        if run.project_id and core.is_v018_operation(operation):
            project_ids.add(int(run.project_id))

    # If there is no provider call in flight, keep the most recently touched
    # v0.20 Project visible so queued, verifying and dependency-bound Work does
    # not disappear between calls. While a brand-new Founder request is being
    # planned, do not let old development Projects compete with that live CEO work.
    live_founder_request = any(
        run.purpose == "CEO_FOUNDER_REQUEST" and run.work_id is None
        for run in live_runs
    )
    if not project_ids and not live_founder_request:
        candidates = Work.query.join(Project).filter(
            Project.environment == "LIVE",
            Project.status.notin_(PROJECT_TERMINAL | {"PAUSED"}),
            Work.state.in_(list(_PULSE_ACTIVE_WORK_STATES)),
        ).order_by(Work.updated_at.desc(), Work.id.desc()).limit(120).all()
        current = next((work for work in candidates if core.is_v018_operation(work.operation)), None)
        if current is not None:
            project_ids.add(int(current.project_id))

    works: list[Work] = []
    if project_ids:
        works = (
            Work.query.join(Project).filter(
                Project.environment == "LIVE",
                Project.status.notin_(PROJECT_TERMINAL | {"PAUSED"}),
                Work.state.in_(list(_PULSE_ACTIVE_WORK_STATES)),
                Work.project_id.in_(project_ids),
            )
            .order_by(Work.updated_at.desc(), Work.id.desc()).all()
        )

    attention_map = _project_attention_map()
    work_rows: list[dict[str, Any]] = []
    rank = {"WORKING": 0, "RECOVERING": 1, "NEEDS_YOU": 2, "WAITING": 3, "IDLE": 4}
    for work in works:
        if not core.is_v018_operation(work.operation):
            continue
        run = AgentRun.query.filter_by(work_id=work.id, status="RUNNING").order_by(AgentRun.id.desc()).first()
        # Management Work exists for the full Mission and stays EXECUTING as an
        # ownership envelope. It earns a Founder pulse card only while a real
        # management AgentRun is RUNNING; otherwise that would be fake activity.
        if work.work_type == "MANAGEMENT" and run is None:
            continue
        owner = _pulse_owner(work)
        if owner is None or not owner.active:
            continue
        wait = __import__("eason_one.services.work_runtime", fromlist=["primary_gate"]).primary_gate(work)
        status = _pulse_work_status(work, run, wait)
        # Canonical Project Governance is a fallback only when the Work itself
        # does not expose the Founder gate in runtime_control_json.
        if status == "WAITING" and attention_map.get(work.project_id) and work.work_type == "MANAGEMENT":
            status = "NEEDS_YOU"
        task = _pulse_task(work)
        effect = _pulse_effect(run)
        at = (
            _pulse_effect_at(effect) if effect is not None else
            (run.started_at if run is not None else work.updated_at)
        )
        work_rows.append({
            "employee": owner,
            "project": work.project,
            "work": work,
            "run": run,
            "effect": effect,
            "status": status,
            "provider": f"{run.provider_key_snapshot} / {run.model_name_snapshot}" if run is not None else None,
            "elapsed": _pulse_elapsed(run.started_at) if run is not None else None,
            "last_activity": _pulse_age(at),
            "detail": _pulse_work_detail(work, run, effect, status, wait),
            "next_step": _pulse_next_step(work, task, status, wait),
            "href": f"/headquarters/projects/{work.project_id}#work",
            "audit_href": f"/headquarters/system/runs/{run.id}" if run is not None else None,
            "initials": "".join(part[0] for part in owner.name.split()[:2]).upper() or owner.name[:2].upper(),
            "sort_at": at,
        })

    # A Founder request can be genuinely running before its Project exists.
    # Keep that real CEO execution visible instead of claiming the company is idle.
    for run in live_runs:
        if run.purpose != "CEO_FOUNDER_REQUEST" or run.work_id is not None:
            continue
        owner = db.session.get(Employee, run.employee_id)
        if owner is None:
            continue
        effect = _pulse_effect(run)
        at = _pulse_effect_at(effect) if effect is not None else run.started_at
        work_rows.append({
            "employee": owner,
            "project": db.session.get(Project, run.project_id) if run.project_id else None,
            "work": None,
            "run": run,
            "effect": effect,
            "status": "WORKING",
            "provider": f"{run.provider_key_snapshot} / {run.model_name_snapshot}",
            "elapsed": _pulse_elapsed(run.started_at),
            "last_activity": _pulse_age(at),
            "detail": _pulse_effect_label(effect) or "CEO is compiling the Founder request into a Project proposal.",
            "next_step": "Then → Project proposal for Founder approval.",
            "href": "/headquarters#ceo-line",
            "audit_href": f"/headquarters/system/runs/{run.id}",
            "initials": "".join(part[0] for part in owner.name.split()[:2]).upper() or owner.name[:2].upper(),
            "sort_at": at,
            "title": "Plan Founder request",
            "request_preview": _short(run.user_request, 140),
        })

    work_rows.sort(key=lambda row: (
        rank.get(row["status"], 9),
        -(_dt(row.get("sort_at")) or datetime.min.replace(tzinfo=timezone.utc)).timestamp(),
    ))
    # One current card per persistent Employee. The card is the highest-priority
    # live responsibility; Project detail still exposes every Work branch.
    visible: list[dict[str, Any]] = []
    seen_employees: set[int] = set()
    for row in work_rows:
        employee_id = row["employee"].id
        if employee_id in seen_employees:
            continue
        seen_employees.add(employee_id)
        visible.append(row)
        if len(visible) >= 6:
            break

    active_employees = Employee.query.filter_by(active=True).count()
    counts = {status: sum(1 for row in visible if row["status"] == status) for status in ("WORKING", "WAITING", "RECOVERING", "NEEDS_YOU")}
    counts["IDLE"] = max(0, int(active_employees) - len({row["employee"].id for row in visible}))
    return {
        "company": company,
        "rows": visible,
        "activity": _pulse_activity(project_ids, live_runs, 8),
        "counts": counts,
        "project_ids": sorted(project_ids),
        "has_live_provider": any(row.get("run") is not None for row in visible),
        "synced_at": _now(),
    }


def home_snapshot() -> dict[str, Any]:
    # Founder read surfaces are pure projections. Runtime repair never happens on GET.
    company = get_company()
    if not company:
        return {"company": None}
    # Headquarters is a present-tense surface. Closed Projects stay available
    # in the registry/results pages and must not be fully projected on every HQ
    # refresh.
    projects = _project_cards(include_completed=False, include_results=False)
    # Preserve the historical registry contract used by diagnostics without
    # paying the cost of fully hydrating every closed Project on the HQ route.
    # The homepage renders active_projects only; these compact rows remain
    # available to read-model callers and link to the full Project audit page.
    closed_projects = Project.query.filter(
        Project.environment == "LIVE", Project.status.in_(PROJECT_TERMINAL | {"PAUSED"})
    ).order_by(Project.updated_at.desc()).all()
    projects.extend({
        "project": project,
        "state": "HISTORICAL",
        "state_detail": project.current_state_summary or project.status,
        "href": f"/headquarters/projects/{project.id}",
        "attention": [],
        "is_dormant_history": project.origin == "LEGACY",
        "contract_integrity_error": None,
    } for project in closed_projects)
    active = [row for row in projects if row["project"].status not in PROJECT_TERMINAL | {"PAUSED"} and not row.get("is_dormant_history")]
    attention = [item for row in active for item in row["attention"]]
    dialogue = __import__(
        "eason_one.services.ceo", fromlist=["recent_ceo_conversation"]
    ).recent_ceo_conversation(10)
    dialogue_rows = [{
        "role": "CEO" if row.message_type == "CEO_TO_FOUNDER" else "FOUNDER",
        "content": row.content,
        "run_id": row.agent_run_id,
        "project_id": row.project_id,
        "action_href": f"/headquarters/projects/{row.project_id}" if row.project_id else None,
        "action_label": "OPEN PROJECT →" if row.project_id else None,
    } for row in dialogue]
    working = _working_people()
    command = work_command_snapshot()
    company_owned_states = {"WORKING", "READY", "VERIFYING", "MEETING", "RECOVERING"}
    durable_working = any(row.get("state") in company_owned_states for row in active)
    return {
        "company": company,
        "ceo": Employee.query.filter_by(slug="ceo").first(),
        "ceo_dialogue": dialogue_rows,
        "projects": projects,
        "active_projects": active,
        "attention": attention,
        "activity": _company_activity(10),
        "pulse": company_pulse_snapshot(),
        "working": working,
        "work_command": command,
        "spent": spent(),
        "remaining": remaining(),
        "state": "NEEDS_YOU" if attention else "WORKING" if durable_working else "AVAILABLE",
        "durable_working": durable_working,
    }


def projects_snapshot() -> dict[str, Any]:
    company = get_company()
    if not company:
        return {"company": None, "ceo": None, "projects": [], "open": [], "current": [], "recently_completed": [], "archive": [], "completed": []}

    projects = Project.query.filter_by(environment="LIVE").order_by(Project.updated_at.desc()).all()
    open_projects = [row for row in projects if row.status not in PROJECT_TERMINAL | {"PAUSED"}]
    closed_projects = [row for row in projects if row.status in PROJECT_TERMINAL]
    paused_projects = [row for row in projects if row.status == "PAUSED"]

    current = [project_card(row, include_results=False) for row in open_projects]
    recent_projects = closed_projects[:6]
    recently_completed = [project_card(row, include_results=True) for row in recent_projects]

    def archive_card(project: Project) -> dict[str, Any]:
        dormant = _project_is_dormant_history(project.id)
        return {
            "project": project,
            "state": "HISTORICAL" if dormant else project.status,
            "href": f"/headquarters/projects/{project.id}",
            "updated_label": _age(project.updated_at),
            "is_dormant_history": dormant,
            "latest_result": None,
            "current_direction": project.next_milestone or project.objective,
        }

    archive = [archive_card(row) for row in closed_projects[6:] + paused_projects]
    dormant_recent = [row for row in recently_completed if row.get("is_dormant_history")]
    if dormant_recent:
        recently_completed = [row for row in recently_completed if not row.get("is_dormant_history")]
        archive = [archive_card(row["project"]) for row in dormant_recent] + archive
    rows = current + recently_completed + archive
    modern_closed = recently_completed + [row for row in archive if not row.get("is_dormant_history")]
    dormant = [row for row in archive if row.get("is_dormant_history")]
    # Keep the Founder registry useful: only a small recent set competes with
    # current company work. Older closed work and pre-v0.18 records stay fully
    # accessible in Development History instead of being deleted.
    archive.sort(
        key=lambda row: _dt(row["project"].updated_at) or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    return {
        "company": company,
        "ceo": Employee.query.filter_by(slug="ceo").first(),
        "projects": rows,
        "open": current,
        "current": current,
        "recently_completed": recently_completed,
        "archive": archive,
        # Compatibility for older templates/tests that still expect completed.
        "completed": modern_closed + dormant,
    }


_FOUNDER_PHASES = ("Plan", "Research", "Evidence", "Review", "Decision", "Delivery")


def _founder_phase_for_work(work: Work) -> str:
    """Project one Work into a semantic phase without inventing a percentage."""
    text = " ".join((work.title or "", work.purpose or "", work.expected_output or "")).casefold()
    if any(word in text for word in ("evidence", "proof", "source", "reconcile", "validate")):
        return "Evidence"
    if any(word in text for word in ("review", "critic", "audit", "quality")):
        return "Review"
    if any(word in text for word in ("research", "market", "competitor", "investigat")):
        return "Research"
    if any(word in text for word in ("decision", "strategy", "recommend")):
        return "Decision"
    if any(word in text for word in ("deliver", "report", "brief", "release", "output")):
        return "Delivery"
    return "Plan"


def _semantic_progress_projection(delivery_rows: list[dict], focus_work: dict | None) -> dict:
    current_phase = _founder_phase_for_work(focus_work["work"]) if focus_work else (
        "Delivery" if delivery_rows and all(row["work"].state == "ACCEPTED" for row in delivery_rows) else "Plan"
    )
    current_index = _FOUNDER_PHASES.index(current_phase)
    reached = {
        _founder_phase_for_work(row["work"])
        for row in delivery_rows
        if row["work"].state == "ACCEPTED"
    }
    phases = []
    for index, label in enumerate(_FOUNDER_PHASES):
        state = "CURRENT" if index == current_index else "DONE" if label in reached or index < current_index else "UPCOMING"
        phases.append({"label": label, "state": state})
    return {
        "phases": phases,
        "current_phase": current_phase,
        "show_percent": False,
        "basis": "CURRENT_EFFECTIVE_PLAN_PHASES",
    }


def _project_team_projection(
    current_rows: list[dict], *, focus_work_id: int | None, stage_by_id: dict[int, int]
) -> list[dict]:
    grouped: dict[int, list[dict]] = defaultdict(list)
    for row in current_rows:
        employee = row.get("employee")
        if employee is not None:
            grouped[int(employee.id)].append(row)
    status_rank = {"WORKING": 0, "RECOVERING": 1, "NEEDS_YOU": 2, "WAITING": 3}
    result = []
    focus_stage = stage_by_id.get(int(focus_work_id or 0), 0)
    next_stage = min(
        (stage for work_id, stage in stage_by_id.items() if stage > focus_stage),
        default=None,
    )
    for employee_id, rows in grouped.items():
        rows.sort(key=lambda row: (status_rank.get(row["founder_state"], 9), row["work"].id))
        current = rows[0]
        queued = [row for row in rows[1:] if row["work"].state in {"READY", "WAITING", "EXECUTING", "VERIFYING"}]
        member_stage = min(stage_by_id.get(row["work"].id, focus_stage) for row in rows)
        if current["founder_state"] == "WORKING":
            status = "WORKING"
        elif current["work"].id != focus_work_id and member_stage > focus_stage:
            status = "UP NEXT" if member_stage == next_stage else "QUEUED"
        elif current["founder_state"] in {"WAITING", "RECOVERING", "NEEDS_YOU"}:
            status = "WAITING"
        elif current["work"].state == "READY":
            status = "UP NEXT"
        else:
            status = "QUEUED"
        result.append({
            "employee": current["employee"],
            "status": status,
            "current_work": current["work"],
            "current_detail": current["founder_detail"],
            "next_work": queued[0]["work"] if queued else None,
            "work_count": len(rows),
        })
    result.sort(key=lambda row: ({"WORKING": 0, "WAITING": 1, "UP NEXT": 2, "QUEUED": 3}.get(row["status"], 9), row["employee"].id))
    return result


def project_snapshot(project: Project) -> dict[str, Any]:
    db.session.refresh(project)
    contract_read = __import__(
        "eason_one.services.project_contract", fromlist=["read_projection"]
    ).read_projection(project)
    governing_terms = dict(contract_read.get("terms") or {})
    live = _live_runs_by_project().get(project.id, [])
    meetings = _open_meetings_by_project().get(project.id, [])
    attention = _project_attention_map().get(project.id, [])
    cost = _costs_by_project().get(project.id, Decimal("0"))
    card = project_card(project, live_runs=live, open_meetings=meetings, attention=attention, cost=cost)
    if card.get("is_dormant_history"):
        attention = []
        live = []
        meetings = []
    operations = _real_operations(project.id)
    multi_agent = __import__(
        "eason_one.services.multi_agent", fromlist=["projection"]
    )
    orchestration = next(
        (view for row in operations if (view := multi_agent.projection(row)) is not None),
        None,
    )
    tasks = Task.query.filter_by(project_id=project.id).order_by(Task.updated_at.desc()).all()
    meetings_all = Meeting.query.filter_by(project_id=project.id).order_by(Meeting.created_at.desc()).all()
    results = _result_rows(project, 24)
    decisions = KnowledgeItem.query.filter(
        KnowledgeItem.project_id == project.id,
        KnowledgeItem.kind.in_(["DECISION", "CORRECTION", "FACT", "EVIDENCE"]),
    ).order_by(KnowledgeItem.created_at.desc()).limit(16).all()
    team = _project_team(project, live_runs=live)
    messages = WorkMessage.query.filter_by(project_id=project.id).order_by(WorkMessage.id.desc()).limit(10).all()
    messages.reverse()
    dialogue = [{
        "role": "CEO" if row.message_type == "CEO_TO_FOUNDER" else "FOUNDER" if row.message_type == "FOUNDER_TO_CEO" else "COMPANY",
        "content": row.content,
        "run_id": row.agent_run_id,
    } for row in messages]
    works = Work.query.filter_by(project_id=project.id).order_by(Work.id).all()
    work_ids = [work.id for work in works]
    runs_by_work: dict[int, list[AgentRun]] = defaultdict(list)
    if work_ids:
        for run in AgentRun.query.filter(AgentRun.work_id.in_(work_ids)).order_by(AgentRun.id.desc()).all():
            runs_by_work[int(run.work_id)].append(run)
    tasks_by_work: dict[int, Task] = {}
    if work_ids:
        for task in Task.query.filter(Task.work_id.in_(work_ids)).order_by(Task.id.desc()).all():
            tasks_by_work.setdefault(int(task.work_id), task)
    work_rows = []
    for work in works:
        assignment = next((row for row in reversed(work.assignments) if row.ended_at is None), None)
        wait = (
            None
            if card.get("is_dormant_history")
            else __import__(
                "eason_one.services.work_runtime", fromlist=["primary_gate"]
            ).primary_gate(work)
        )
        work_runs = runs_by_work.get(work.id, [])
        latest_run = work_runs[0] if work_runs else None
        latest_execution_run = next((run for run in work_runs if run.purpose == "TASK_EXECUTION"), None)
        latest_review_run = next((run for run in work_runs if run.purpose == "TASK_REVIEW"), None)
        running_run = next((run for run in work_runs if run.status == "RUNNING"), None)
        visible_run = running_run or latest_run
        task = tasks_by_work.get(work.id)
        reviewer = getattr(task, "reviewer", None) if task is not None else None
        status_run = running_run or latest_run
        founder_state = (
            "HISTORICAL"
            if card.get("is_dormant_history")
            else "DONE" if work.state == "ACCEPTED"
            else "CANCELLED" if work.state == "CANCELLED"
            else "FAILED" if work.state == "ABANDONED" and not wait
            else _pulse_work_status(work, status_run, wait)
        )
        effect = _pulse_effect(running_run) if running_run is not None else None
        if founder_state in {"WORKING", "WAITING", "RECOVERING", "NEEDS_YOU"}:
            founder_detail = _pulse_work_detail(work, status_run, effect, founder_state, wait)
            founder_next = _pulse_next_step(work, task, founder_state, wait)
        elif founder_state == "DONE":
            founder_detail = "Accepted company output is persisted and available to downstream Work."
            founder_next = "Downstream Work may consume this accepted evidence."
        elif founder_state == "FAILED":
            founder_detail = "Company Work is abandoned. Open audit only if the failure needs engineering investigation."
            founder_next = "A new governed continuation or Founder decision is required before this branch can move again."
        elif founder_state == "CANCELLED":
            founder_detail = "This Work was cancelled and will not execute."
            founder_next = "No next action for this branch."
        else:
            founder_detail = "Historical execution record."
            founder_next = "Audit only."
        staffing = dict((work.runtime_control_json or {}).get("team_formation") or {})
        requirements = dict((work.runtime_control_json or {}).get("staffing_requirements") or {})
        selection_evidence = dict(staffing.get("selection_evidence") or {})
        market_contract = __import__(
            "eason_one.services.market", fromlist=["contract_for_work", "contract_evidence"]
        )
        contract = market_contract.contract_for_work(work.id)
        market_evidence = market_contract.contract_evidence(contract)
        work_rows.append({
            "work": work,
            "employee": assignment.employee if assignment else None,
            "wait": wait,
            "latest_run": latest_run,
            "latest_execution_run": latest_execution_run,
            "latest_review_run": latest_review_run,
            "visible_run": visible_run,
            "task": task,
            "reviewer": reviewer,
            "founder_state": founder_state,
            "founder_detail": founder_detail,
            "founder_next": founder_next,
            "technical_reason": (
                "BLOCKED_MISSING_EVIDENCE"
                if latest_run is not None and latest_run.failure_reason == "MISSING_EVIDENCE"
                else (str(getattr(wait, "issue_code", "") or "").split(":")[0] or None)
            ),
            "retry_attempt": (int(running_run.attempt_number or 1) if running_run is not None and running_run.retry_of_run_id else None),
            "provider": (f"{visible_run.provider_key_snapshot} / {visible_run.model_name_snapshot}" if visible_run is not None else None),
            "execution_provider": (
                f"{latest_execution_run.provider_key_snapshot} / {latest_execution_run.model_name_snapshot}"
                if latest_execution_run is not None else None
            ),
            "review_provider": (
                f"{latest_review_run.provider_key_snapshot} / {latest_review_run.model_name_snapshot}"
                if latest_review_run is not None else None
            ),
            "active_run_purpose": getattr(running_run, "purpose", None),
            "is_management": work.work_type == "MANAGEMENT",
            "staffing": staffing,
            "selection_evidence": selection_evidence,
            "market_contract": market_evidence,
            "required_capabilities": list(requirements.get("required_capabilities") or []),
        })

    delivery_rows = [row for row in work_rows if not row["is_management"]]
    by_id = {row["work"].id: row for row in delivery_rows}
    dep_map: dict[int, list[int]] = defaultdict(list)
    if by_id:
        for edge in WorkDependency.query.filter(WorkDependency.work_id.in_(list(by_id))).order_by(WorkDependency.id).all():
            if edge.depends_on_work_id in by_id:
                dep_map[edge.work_id].append(edge.depends_on_work_id)
    stage_by_id: dict[int, int] = {}
    unresolved = set(by_id)
    while unresolved:
        advanced = False
        for work_id in sorted(list(unresolved)):
            deps = dep_map.get(work_id, [])
            if all(dep in stage_by_id for dep in deps):
                stage_by_id[work_id] = 0 if not deps else 1 + max(stage_by_id[dep] for dep in deps)
                unresolved.remove(work_id)
                advanced = True
        if not advanced:
            # Defensive fallback for corrupt/cyclic legacy topology: keep it
            # readable without inventing a dependency resolution.
            for work_id in sorted(unresolved):
                stage_by_id[work_id] = 0
            break
    current_states = {"NEEDS_YOU", "RECOVERING", "WORKING", "WAITING"}
    current_delivery_rows = [row for row in delivery_rows if row["founder_state"] in current_states]
    current_ids = {row["work"].id for row in current_delivery_rows}
    historical_delivery_rows = [row for row in delivery_rows if row["work"].id not in current_ids]
    handoff_stages = []
    for stage_index in sorted(set(stage_by_id.values())):
        nodes = []
        for work_id, stage in sorted(stage_by_id.items()):
            if stage != stage_index:
                continue
            row = by_id[work_id]
            if work_id not in current_ids:
                continue
            nodes.append({
                **row,
                "dependency_names": [by_id[dep]["work"].title for dep in dep_map.get(work_id, []) if dep in by_id],
            })
        if nodes:
            handoff_stages.append({"index": stage_index, "nodes": nodes})
    handoff = {
        "stages": handoff_stages,
        "node_count": len(current_delivery_rows),
        "edge_count": sum(
            1 for work_id, dependencies in dep_map.items() if work_id in current_ids
            for dependency in dependencies if dependency in current_ids
        ),
    }
    focus_rank = {"NEEDS_YOU": 0, "RECOVERING": 1, "WORKING": 2, "WAITING": 3}
    focus_work = next((row for row in sorted(
        current_delivery_rows,
        key=lambda row: (focus_rank.get(row.get("founder_state"), 7), row["work"].id),
    )), None)
    semantic_progress = _semantic_progress_projection(delivery_rows, focus_work)
    project_team = _project_team_projection(
        current_delivery_rows,
        focus_work_id=focus_work["work"].id if focus_work else None,
        stage_by_id=stage_by_id,
    )
    founder_blocker = None
    if focus_work and focus_work["founder_state"] in {"WAITING", "RECOVERING"}:
        founder_blocker = (
            "缺少可驗證的新證據。"
            if (
                focus_work.get("technical_reason") == "BLOCKED_MISSING_EVIDENCE"
                or "BLOCKED_MISSING_EVIDENCE" in str(focus_work.get("founder_detail") or "")
            )
            else focus_work["founder_detail"]
        )
    founder_next = focus_work["founder_next"] if focus_work else card.get("state_detail")
    if founder_blocker == "缺少可驗證的新證據。":
        sequence = [row["employee"].name for row in project_team]
        founder_next = "等待新的有效證據" + (" → " + " → ".join(sequence) if sequence else "")
    return {
        "company": get_company(),
        "ceo": Employee.query.filter_by(slug="ceo").first(),
        "project": project,
        "governing": {
            "objective": governing_terms.get("objective") or project.objective,
            "deadline": governing_terms.get("deadline"),
            "constraints": list(governing_terms.get("constraints") or []),
            "success_criteria": list(governing_terms.get("success_criteria") or []),
            "contract_hash": governing_terms.get("governing_contract_hash") or governing_terms.get("contract_hash"),
            "integrity_error": contract_read.get("integrity_error"),
        },
        "card": card,
        "attention": attention,
        "working": _working_people(project.id),
        "team": team,
        "meetings": meetings_all,
        "results": results,
        "decisions": decisions,
        "works": work_rows,
        "current_works": current_delivery_rows,
        "handoff": handoff,
        "previous_attempts": historical_delivery_rows,
        "focus_work": focus_work,
        "semantic_progress": semantic_progress,
        "project_team": project_team,
        "founder_blocker": founder_blocker,
        "founder_next": founder_next,
        "work_command": work_command_snapshot(project.id),
        "tasks": tasks,
        "operations": operations,
        "orchestration": orchestration,
        "activity": _company_activity(20, project.id),
        "dialogue": dialogue,
        "cost": cost,
        "cost_breakdown": _project_cost_breakdown(project, card),
    }


def results_snapshot() -> dict[str, Any]:
    company = get_company()
    result_rows = []
    if company:
        # Results is a bounded recent read model. Full Project/audit history is
        # still preserved and accessible from each Project and System Runs.
        Artifact = __import__("eason_one.models", fromlist=["Artifact"]).Artifact
        candidate_ids = {
            int(project_id) for (project_id,) in db.session.query(Artifact.project_id).distinct().all()
            if project_id is not None
        }
        candidate_ids.update(
            int(project_id) for (project_id,) in db.session.query(Task.project_id).filter(
                Task.status == "DONE", Task.result_summary.isnot(None)
            ).distinct().all() if project_id is not None
        )
        projects = (
            Project.query.filter(Project.environment == "LIVE", Project.id.in_(candidate_ids))
            .order_by(Project.updated_at.desc()).limit(16).all()
            if candidate_ids else []
        )
        for project in projects:
            for row in _result_rows(project, 16):
                row = dict(row)
                row["project"] = project
                result_rows.append(row)
    result_rows.sort(key=lambda row: _dt(row["updated"]) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    # Founder Results is one outcome per Project. Individual accepted Artifacts
    # remain reachable from Result detail and Project audit, but they do not
    # compete as peer outcomes on the first screen.
    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in result_rows:
        grouped[row["project"].id].append(row)
    outcomes = []
    for rows in grouped.values():
        primary = next((row for row in rows if row.get("current_project_result")), None)
        primary = primary or next((row for row in rows if row.get("kind") == "PROJECT_RESULT"), None)
        primary = primary or next((row for row in rows if row.get("readable") or row.get("deliverables")), None)
        primary = primary or next((row for row in rows if row.get("verification") == "VALIDATED"), None) or rows[0]
        primary = dict(primary)
        primary["supporting_results"] = len(rows) - 1
        primary["deliverable_count"] = sum(len(row.get("deliverables") or []) for row in rows)
        if not primary.get("team_names"):
            primary["team_names"] = list(dict.fromkeys(
                row["employee"].name for row in rows if row.get("employee")
            ))[:8]
        outcomes.append(primary)
    outcomes.sort(key=lambda row: _dt(row["updated"]) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return {"company": company, "results": outcomes[:24]}


def preview_snapshot() -> dict[str, Any]:
    """Tiny deterministic snapshot used before a paid CEO request."""
    cards = [
        row for row in _project_cards(include_completed=False, include_results=False)
        if not row.get("is_dormant_history")
    ]
    attention = [item for row in cards for item in row["attention"]]
    working = _working_people()
    return {
        "projects": len(cards),
        "attention": len(attention),
        "working": len(working),
        "project_names": [row["project"].name for row in cards[:5]],
        "next_project": cards[0]["project"].name if cards else None,
    }


def local_brief(query: str, project_id: int | str | None = None) -> dict[str, Any]:
    """Answer Founder status questions from persisted Project state with zero model calls."""
    text = " ".join((query or "").lower().split())
    cards = _project_cards(include_completed=True, include_results=False)
    match = None
    if project_id:
        try:
            resolved_id = int(project_id)
        except (TypeError, ValueError):
            resolved_id = None
        if resolved_id is not None:
            match = next((row for row in cards if row["project"].id == resolved_id), None)
    if match is None:
        match = next((row for row in cards if row["project"].name.lower() in text), None)
    if match:
        return {
            "kind": "PROJECT_BRIEF",
            "title": match["project"].name,
            "summary": match["state_detail"],
            "facts": [
                f"Project state: {match['state'].replace('_', ' ')}",
                f"Current direction: {match['current_direction']}",
                f"Next milestone: {match['next_step']}",
                f"Progress: {match['progress']}%",
                f"Cost: NT$ {match['cost']}",
            ],
            "href": match["href"],
        }
    if any(word in text for word in ("who", "employee", "staff", "誰", "員工", "working")):
        workers = _working_people()
        return {
            "kind": "WORKFORCE_BRIEF",
            "title": "People working now",
            "summary": f"{len(workers)} Employee Runs are live right now.",
            "facts": [
                f"{row['employee'].name}: {row['task'].title if row['task'] else row['run'].purpose}"
                + (f" · {row['project'].name}" if row['project'] else "")
                for row in workers[:8]
            ] or ["No Employee Run is live right now."],
            "href": "/headquarters/people",
        }
    if any(word in text for word in ("decision", "attention", "需要我", "決定", "批准", "blocked", "卡住")):
        attention = [item for row in cards for item in row["attention"]]
        return {
            "kind": "FOUNDER_ATTENTION",
            "title": "Needs you",
            "summary": "Nothing needs Founder authority." if not attention else f"{len(attention)} Project decisions need Founder authority.",
            "facts": [f"{item['title']}: {item['reason']}" for item in attention[:8]] or ["CEO may continue inside existing authority."],
            "href": "/headquarters#needs-you",
        }
    if any(word in text for word in ("cost", "spent", "budget", "成本", "花多少", "預算")):
        return {
            "kind": "COST_BRIEF",
            "title": "Company API budget",
            "summary": f"NT$ {spent()} has been recorded; NT$ {remaining()} remains in company authority.",
            "facts": [f"{row['project'].name}: NT$ {row['cost']}" for row in cards[:8]],
            "href": "/headquarters/finance",
        }
    open_cards = [row for row in cards if row["project"].status not in PROJECT_TERMINAL | {"PAUSED"} and not row.get("is_dormant_history")]
    return {
        "kind": "COMPANY_BRIEF",
        "title": "Company today",
        "summary": (
            f"{len(open_cards)} Projects are open. "
            + (f"{sum(bool(row['live_runs']) for row in open_cards)} have live Employee work." if open_cards else "No active Project is recorded.")
        ),
        "facts": [f"{row['project'].name}: {row['state'].replace('_', ' ')} — {row['state_detail']}" for row in open_cards[:6]] or ["No active Project is recorded."],
        "href": "/headquarters/projects",
    }
