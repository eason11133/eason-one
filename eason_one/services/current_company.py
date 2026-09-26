import os
from collections import defaultdict
from decimal import Decimal

from sqlalchemy import func, or_

from ..extensions import db
from ..models import (
    AgentRun, CostEvent, Employee, Escalation, KnowledgeItem, Meeting, ModelConfig,
    Operation, Project, Proposal, Task, Work,
)
from .brain import current
from .company import get_company, remaining, spent
from .stabilization import REAL_WORK, SYSTEM_VALIDATION, operation_kind

ACTIVE_TASKS = {"ASSIGNED", "WORKING", "REVIEW", "BLOCKED"}
OPERATING_PROJECT_STATUSES = {"PLANNING", "ACTIVE", "BLOCKED", "REVIEW"}
TERMINAL_PROJECT_STATUSES = {"COMPLETED", "CANCELLED", "FAILED"}
TERMINAL_OPERATIONS = {"COMPLETED", "FAILED", "TERMINATED_BY_FOUNDER", "SUPERSEDED"}
PROVIDER_CREDENTIALS = {
    "openai": ("OPENAI_API_KEY", "Configure the OpenAI API credential."),
    "anthropic": ("ANTHROPIC_API_KEY", "Configure the Anthropic API credential."),
    "gemini": ("GEMINI_API_KEY", "Configure the Gemini API credential."),
    "perplexity": ("PERPLEXITY_API_KEY", "Configure the Perplexity API credential."),
}


def provider_health():
    rows = []
    for model in ModelConfig.query.filter_by(active=True, archived=False):
        required = PROVIDER_CREDENTIALS.get(model.provider_key)
        if model.provider_key == "codex":
            descriptor = __import__(
                "eason_one.services.codex_connector",
                fromlist=["codex_runtime_descriptor"],
            ).codex_runtime_descriptor()
            configured = bool(descriptor.get("ready"))
            transport = (descriptor.get("transport") or "unknown").upper()
            summary = (
                f"Codex CLI ready through {transport}"
                if configured else
                f"Codex CLI is unavailable through {transport}."
            )
            recovery = None if configured else (
                descriptor.get("error")
                or "Install/sign in to Codex CLI in WSL2 or configure the selected transport."
            )
            detail = descriptor.get("display_path") or descriptor.get("path")
        elif model.provider_key == "mock":
            configured = True
            summary = "Simulation only — formal Mission fallback disabled."
            recovery = None
            detail = "TESTING / SIMULATION"
        else:
            configured = not required or bool(os.getenv(required[0]))
            if configured and model.provider_key == "perplexity" and Decimal(getattr(model,"request_price_per_call",0) or 0) <= 0:
                configured = False
                summary = "Perplexity credential exists, but governed request pricing is missing."
                recovery = "Set Request fee / call in Company currency before using Sonar for formal research."
                detail = "request_price_per_call"
            else:
                summary = "Ready" if configured else "Provider credential is unavailable."
                recovery = None if configured else required[1]
                detail = None if configured else required[0]
        research_capable = model.provider_key in {"openai", "perplexity"}
        research_ready = bool(
            research_capable
            and configured
            and Decimal(getattr(model, "request_price_per_call", 0) or 0) > 0
        )
        research_recovery = None
        if research_capable and configured and not research_ready:
            research_recovery = (
                "Set Search/request reserve per call in Company currency before formal live-web research."
            )
        rows.append({
            "model": model, "healthy": configured,
            "summary": summary,
            "recovery": recovery,
            "technical_detail": detail,
            "research_capable": research_capable,
            "research_ready": research_ready,
            "research_recovery": research_recovery,
        })
    return rows


def _budget(operation, event=None):
    actual = Decimal(db.session.query(func.coalesce(
        func.sum(CostEvent.real_cost_delta), 0
    )).filter_by(operation_id=operation.id).scalar())
    authorized = Decimal(operation.approved_budget_twd)
    additional = Decimal(str((event or {}).get(
        "additional_required_twd", "0")))
    return {
        "authorized": authorized, "spent": actual,
        "remaining": authorized - actual, "additional": additional,
        "resulting": authorized + additional,
    }


def unresolved_governance():
    rows = []
    # Company Core vNext Escalation is the first-class Founder-decision truth.
    # Legacy operation.memory events remain below for backward compatibility.
    governance = __import__(
        "eason_one.services.governance", fromlist=["attention"]
    )
    for escalation in governance.attention():
        operation = escalation.operation
        core = __import__(
            "eason_one.services.core_v018",
            fromlist=["is_dormant_legacy_project_operation"],
        )
        if operation is not None and core.is_dormant_legacy_project_operation(operation):
            # Historical pre-v0.18 authority is audit data, not live Founder work.
            continue
        if operation is None and escalation.project_id:
            project_ops = Operation.query.filter(
                Operation.project_id == escalation.project_id
            ).all()
            if project_ops and not any(
                __import__(
                    "eason_one.services.core_v018", fromlist=["is_v018_declared_operation"]
                ).is_v018_declared_operation(row)
                for row in project_ops
            ):
                continue
        rows.append({
            "key": f"escalation:{escalation.id}",
            "type": escalation.escalation_type,
            "operation": operation,
            "event": {
                "kind": escalation.escalation_type,
                "status": "PENDING",
                "reason": escalation.reason,
                "summary": escalation.reason,
            },
            "budget": _budget(operation, None) if operation else None,
            "href": (f"/operations/{operation.id}" if operation else f"/headquarters/projects/{escalation.project_id}"),
            "escalation": escalation,
        })
    for operation in Operation.query.order_by(Operation.updated_at.desc()):
        if __import__(
            "eason_one.services.core_v018", fromlist=["bypass_legacy_operation_runtime"]
        ).bypass_legacy_operation_runtime(operation):
            # Approved vNext Founder authority is represented by first-class
            # Work Escalations. Historical proposal attention must not leak back
            # into the live Founder queue.
            continue
        events = (operation.memory_json or {}).get("founder_attention_events") or []
        for index, event in enumerate(events):
            if event.get("status") == "PENDING":
                if any(
                    row.get("operation") is operation
                    and row.get("type") == (event.get("kind") or "FOUNDER_AUTHORITY")
                    and (row.get("event") or {}).get("reason") == event.get("reason")
                    for row in rows
                ):
                    continue
                rows.append({
                    "key": f"operation:{operation.id}:{index}",
                    "type": event.get("kind") or "FOUNDER_AUTHORITY",
                    "operation": operation, "event": event,
                    "budget": _budget(operation, event),
                    "href": f"/operations/{operation.id}",
                })
    for proposal in Proposal.query.filter_by(status="PENDING"):
        if not __import__(
            "eason_one.services.proposal_authority", fromlist=["is_initial_project_proposal"]
        ).is_initial_project_proposal(proposal):
            continue
        project = db.session.get(Project, proposal.project_id) if proposal.project_id else None
        if project and project.environment == "LIVE":
            project_ops = Operation.query.filter(Operation.project_id == project.id).all()
            core = __import__(
                "eason_one.services.core_v018", fromlist=["is_v018_declared_operation"]
            )
            if project_ops and not any(core.is_v018_declared_operation(row) for row in project_ops):
                # A pre-v0.18 Project is historical after cutover. Its leftover
                # proposal cannot become current Founder authority.
                continue
        if project and project.environment == "LIVE" and project.status in {"REVIEW", "COMPLETED"}:
            # A pending legacy Project-plan proposal is not live Founder authority
            # once Founder-approved execution has already delivered the Project.
            # project_company reconciles the row to SUPERSEDED on Founder reads;
            # this guard prevents stale Proposal state from leaking into global HQ.
            approved_operation = Operation.query.filter(
                Operation.project_id == project.id, Operation.approved_at.isnot(None)
            ).first()
            if approved_operation:
                continue
        if not project or project.environment == "LIVE":
            rows.append({
                "key": f"proposal:{proposal.id}", "type": "PROPOSAL",
                "proposal": proposal, "href": f"/inbox#{proposal.id}",
            })
    return rows


def _run_blocks_current_work(run):
    """Return whether an unresolved Run still blocks an active runtime object.

    Historical unresolved rows remain auditable, but they must not put the CEO or
    Founder UI into emergency mode after the associated Mission/Meeting has moved
    on.  A failure is blocking only when its linked Task, Operation, or Meeting is
    currently paused/blocked/failed in a way that requires recovery.
    """
    if run.project_id:
        project = db.session.get(Project, run.project_id)
        if project is not None and str(project.status or "").upper() in TERMINAL_PROJECT_STATUSES:
            return False
    if run.work_id:
        work = db.session.get(Work, run.work_id)
        if work is not None:
            if work.project is not None and str(work.project.status or "").upper() in TERMINAL_PROJECT_STATUSES:
                return False
            return work.state == "WAITING"
    if run.task_id:
        task = db.session.get(Task, run.task_id)
        # Only a true legacy Task may own current blocking truth. Work-backed
        # Tasks are projections and cannot override their Work.
        if task and task.work_id is None and task.status == "BLOCKED":
            return True
    if run.operation_id:
        operation = db.session.get(Operation, run.operation_id)
        if operation and operation.status in {"WAITING_FOR_FOUNDER", "PAUSED"}:
            return True
    if run.meeting_id:
        meeting = db.session.get(Meeting, run.meeting_id)
        if meeting and meeting.status in {"PAUSED", "WAITING_FOR_FOUNDER", "FAILED"}:
            return True
    return False


def _failure_impact(failure_type):
    return {
        "OUTPUT_TRUNCATED": "The model response hit its output limit before a usable result was produced.",
        "STRUCTURED_OUTPUT_INVALID": "The response completed, but it did not satisfy the required structured schema.",
        "PROVIDER_ERROR": "The provider call did not complete successfully.",
        "EXECUTION_FAILED": "The workflow ended without a usable result.",
    }.get(failure_type, "The workflow ended without a usable result.")


def failure_groups():
    groups = defaultdict(list)
    failures = AgentRun.query.filter(
        AgentRun.status == "FAILED", AgentRun.resolution_status.is_(None)
    ).order_by(AgentRun.started_at.desc()).all()
    project_ids = {run.project_id for run in failures if run.project_id}
    work_ids = {run.work_id for run in failures if run.work_id}
    task_ids = {run.task_id for run in failures if run.task_id}
    operation_ids = {run.operation_id for run in failures if run.operation_id}
    meeting_ids = {run.meeting_id for run in failures if run.meeting_id}
    projects = {row.id: row for row in Project.query.filter(Project.id.in_(project_ids)).all()} if project_ids else {}
    works = {row.id: row for row in Work.query.filter(Work.id.in_(work_ids)).all()} if work_ids else {}
    tasks = {row.id: row for row in Task.query.filter(Task.id.in_(task_ids)).all()} if task_ids else {}
    operations = {row.id: row for row in Operation.query.filter(Operation.id.in_(operation_ids)).all()} if operation_ids else {}
    meetings = {row.id: row for row in Meeting.query.filter(Meeting.id.in_(meeting_ids)).all()} if meeting_ids else {}

    def blocks(run):
        project = projects.get(run.project_id)
        if project is not None and str(project.status or "").upper() in TERMINAL_PROJECT_STATUSES:
            return False
        work = works.get(run.work_id)
        if work is not None:
            work_project = projects.get(work.project_id)
            if work_project is not None and str(work_project.status or "").upper() in TERMINAL_PROJECT_STATUSES:
                return False
            return work.state == "WAITING"
        task = tasks.get(run.task_id)
        if task and task.work_id is None and task.status == "BLOCKED":
            return True
        operation = operations.get(run.operation_id)
        if operation and operation.status in {"WAITING_FOR_FOUNDER", "PAUSED"}:
            return True
        meeting = meetings.get(run.meeting_id)
        return bool(meeting and meeting.status in {"PAUSED", "WAITING_FOR_FOUNDER", "FAILED"})

    for run in failures:
        key = (run.failure_reason or "EXECUTION_FAILED", run.purpose)
        groups[key].append(run)
    rows = []
    for key, runs in groups.items():
        blocking = any(blocks(run) for run in runs)
        rows.append({
            "failure_type": key[0], "workflow": key[1], "runs": runs,
            "count": len(runs), "latest": runs[0],
            "cost": sum((Decimal(run.real_cost or 0) for run in runs), Decimal(0)),
            "impact": _failure_impact(key[0]),
            "blocking": blocking,
            "state_label": "BLOCKING CURRENT WORK" if blocking else "HISTORICAL RELIABILITY ISSUE",
        })
    return rows


def brain_projection():
    effective = current(None)
    all_effective = []
    for project in Project.query.all():
        all_effective.extend(current(project.id))
    unique = {item.id: item for item in all_effective}
    by_scope = {
        "company": [item for item in effective if item.project_id is None],
        "project": [item for item in unique.values() if item.project_id is not None],
        "operation": [],
    }
    by_kind = defaultdict(list)
    for item in unique.values():
        by_kind[item.kind].append(item)
    return {"by_scope": by_scope, "by_kind": dict(by_kind),
            "effective": list(unique.values())}


def projection():
    # Pure read model. Recovery/reconciliation belongs to runtime/control paths,
    # never to a Founder GET request.
    governance = unresolved_governance()
    governance_by_operation = {
        row["operation"].id: row for row in governance if row.get("operation")
    }
    operations = []
    kernel = __import__(
        "eason_one.services.operation_kernel", fromlist=["authoritative_status"]
    )
    core = __import__(
        "eason_one.services.core_v018",
        fromlist=["is_v018_operation", "is_retired_work_vnext", "is_dormant_legacy_project_operation"],
    )
    for operation in Operation.query.order_by(Operation.updated_at.desc()):
        tasks = list(operation.tasks)
        is_vnext = core.is_v018_operation(operation)
        retired_work_vnext = core.is_retired_work_vnext(operation)
        dormant_legacy = core.is_dormant_legacy_project_operation(operation)
        if is_vnext:
            runtime = __import__(
                "eason_one.services.company_runtime", fromlist=["runtime_snapshot"]
            ).runtime_snapshot(operation)
            state = operation.status  # compatibility display only; Work drives classification below
            active_work = runtime.get("active_work") or {}
            active = next((task for task in tasks if task.work_id == active_work.get("id")), None)
        elif retired_work_vnext or dormant_legacy:
            # Any approved pre-v0.18 LIVE Project row is audit history. Legacy
            # status strings may remain for evidence but cannot describe today.
            runtime = {"worker_alive": False, "retired": True}
            state = "RETIRED"
            active = None
        else:
            state = kernel.authoritative_status(operation)
            runtime = __import__(
                "eason_one.services.operation_runtime", fromlist=["runtime_snapshot"]
            ).runtime_snapshot(operation)
            active_id = (runtime.get("active_task") or {}).get("id") if runtime.get("worker_alive") else None
            active = next((task for task in tasks if task.id == active_id), None)
        project_terminal = bool(
            operation.project is not None
            and str(operation.project.status or "").upper() in TERMINAL_PROJECT_STATUSES
        )
        if project_terminal or retired_work_vnext or dormant_legacy or state in {"COMPLETED", "FAILED", "CANCELLED"}:
            classification = "TERMINAL"
        elif is_vnext:
            governance_owner = __import__(
                "eason_one.services.governance", fromlist=["project_blocking_gate"]
            )
            project_gate = governance_owner.project_blocking_gate(operation.project) if operation.project else None
            active_delivery = any(
                work.work_type != "MANAGEMENT" and work.state in {"READY", "EXECUTING", "VERIFYING"}
                for work in operation.works
            )
            waiting_delivery = any(
                work.work_type != "MANAGEMENT" and work.state == "WAITING"
                for work in operation.works
            )
            if operation.status == "PLANNED" or project_gate is not None:
                classification = "WAITING_FOR_FOUNDER"
            elif active_delivery:
                # One execution-scoped Founder exception must not make the whole
                # multi-Employee Mission look stopped while sibling Work proceeds.
                classification = "ACTIVE"
            elif operation.id in governance_by_operation:
                classification = "WAITING_FOR_FOUNDER"
            elif waiting_delivery:
                classification = "BLOCKED"
            else:
                classification = "ACTIVE" if state in {"CREATED", "ROUTED", "QUEUED", "RUNNING", "VERIFYING"} else "BLOCKED"
        elif operation.status == "PLANNED" or operation.id in governance_by_operation:
            # Legacy Mission projection keeps its historical single-state model.
            classification = "WAITING_FOR_FOUNDER"
        elif state in {"WAITING_APPROVAL", "WAITING_INPUT"} or any(
            task.status == "BLOCKED" for task in tasks
        ):
            classification = "BLOCKED"
        elif state in {"CREATED", "ROUTED", "QUEUED", "RUNNING", "VERIFYING"}:
            classification = "ACTIVE"
        else:
            classification = "BLOCKED"
        kind = operation_kind(operation)
        delivery_works = [work for work in operation.works if work.work_type != "MANAGEMENT"] if is_vnext else []
        operations.append({
            "operation": operation, "classification": classification,
            "kind": kind,
            "tasks": tasks, "current_task": active,
            "active_works": (runtime.get("active_works") or []) if is_vnext else [],
            "done": (sum(work.state == "ACCEPTED" for work in delivery_works) if is_vnext
                     else sum(task.status == "DONE" for task in tasks)),
            "total": (len(delivery_works) if is_vnext else len(tasks)),
            "budget": _budget(operation, (
                governance_by_operation.get(operation.id) or {}).get("event")),
        })
    failures = failure_groups()
    brain = brain_projection()
    live = Project.query.filter_by(environment="LIVE").order_by(
        Project.updated_at.desc()).all()
    legacy = Project.query.filter(Project.environment != "LIVE").order_by(
        Project.updated_at.desc()).all()
    real_operations = [row for row in operations if row["kind"] == REAL_WORK]
    validation_operations = [row for row in operations if row["kind"] == SYSTEM_VALIDATION]
    real_operation_ids = {row["operation"].id for row in real_operations}
    real_governance = [
        row for row in governance
        if not row.get("operation") or row["operation"].id in real_operation_ids
    ]
    for failure in failures:
        operation_id = failure["latest"].operation_id if failure.get("latest") else None
        if operation_id and operation_id not in real_operation_ids:
            failure["blocking"] = False
            failure["state_label"] = "SYSTEM VALIDATION HISTORY"
    if any(row.get("blocking") for row in failures):
        ceo_state = "SYSTEM_FAILURE"
    elif real_governance or any(row["classification"] == "WAITING_FOR_FOUNDER" for row in real_operations):
        ceo_state = "FOUNDER_ATTENTION"
    elif any(row["classification"] in {"ACTIVE", "BLOCKED"} for row in real_operations):
        ceo_state = "WORKING"
    else:
        ceo_state = "AVAILABLE"
    # Work owns vNext activity; Task is only its display/execution adapter.
    vnext_tasks = Task.query.join(Work, Task.work_id == Work.id).join(
        Project, Work.project_id == Project.id
    ).filter(
        Project.environment == "LIVE",
        Project.status.in_(OPERATING_PROJECT_STATUSES),
        Work.state.in_(["READY", "EXECUTING", "WAITING", "VERIFYING"]),
    ).all()
    legacy_tasks = Task.query.join(Project).outerjoin(
        Operation, Task.operation_id == Operation.id
    ).filter(
        Project.environment == "LIVE", Project.status.in_(OPERATING_PROJECT_STATUSES), Task.work_id.is_(None),
        Task.status.in_(ACTIVE_TASKS),
        or_(Task.operation_id.is_(None), Operation.status == "RUNNING"),
    ).all()
    core = __import__(
        "eason_one.services.core_v018", fromlist=["is_dormant_legacy_project_operation"]
    )
    active_tasks = vnext_tasks + [
        task for task in legacy_tasks
        if (task.operation_id is None or operation_kind(task.operation) == REAL_WORK)
        and not (task.operation and core.is_dormant_legacy_project_operation(task.operation))
    ]
    return {
        "company": get_company(), "operations": operations,
        "real_operations": real_operations,
        "validation_operations": validation_operations,
        "governance": governance, "real_governance": real_governance,
        "failures": failures, "brain": brain,
        "live_projects": live, "legacy_projects": legacy,
        "provider_health": provider_health(), "ceo_state": ceo_state,
        "spent": spent(), "remaining": remaining(),
        "active_tasks": active_tasks,
        "active_employees": Employee.query.filter_by(active=True).count(),
    }


def local_briefing(view=None):
    view = view or projection()
    facts = len(view["brain"]["by_kind"].get("FACT", []))
    hypotheses = len(view["brain"]["by_kind"].get("HYPOTHESIS", []))
    rows = view.get("real_operations", view["operations"])
    active = sum(row["classification"] == "ACTIVE" for row in rows)
    blocked = sum(row["classification"] == "BLOCKED" for row in rows)
    return (
        f"Persisted company status: {active} active Operations; {blocked} blocked; "
        f"{len(view.get('real_governance', view['governance']))} Founder decisions pending; "
        f"{sum(bool(row.get('blocking')) for row in view['failures'])} blocking failure groups; "
        f"{sum(not bool(row.get('blocking')) for row in view['failures'])} historical reliability groups; "
        f"{len(view['active_tasks'])} current LIVE Tasks. "
        f"AI spend is NT$ {view['spent']}; remaining company budget is "
        f"NT$ {view['remaining']}. Company Brain contains {facts} effective "
        f"FACT records and {hypotheses} effective HYPOTHESIS records."
    )
