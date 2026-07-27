import json
import hashlib
from decimal import Decimal, InvalidOperation

from sqlalchemy import func

from ..extensions import db
from ..models import (
    AgentRun, CostEvent, Employee, HiringRequest, Meeting, Operation,
    OperationStep, Project, Task, WorkMessage, now,
)
from ..schemas import (
    CEO_DECISION_SCHEMA, GOAL_VERIFICATION_SCHEMA, SYNTHESIS_SCHEMA,
)
from .company import remaining as company_remaining
from .execution import execute
from . import meetings as meeting_service
from .reviews import DECISIONS, run_review
from .task_execution import run_task
from .tasks import transition

PLAN_FIELDS = {"mode", "executive_response", "operation"}
OPERATION_FIELDS = {
    "title", "objective", "project_id", "budget_twd", "tasks",
    "meeting_policy", "completion_criteria",
}
TASK_FIELDS = {
    "title", "objective", "assignee_employee_id", "reviewer_employee_id",
    "acceptance_criteria",
}
TERMINAL = {"COMPLETED", "FAILED", "TERMINATED_BY_FOUNDER"}
OPEN_STEP = {"PROVIDER_CALL_STARTED", "AMBIGUOUS"}
GOAL_STATUSES = {
    "SATISFIED", "NOT_SATISFIED", "INSUFFICIENT_EVIDENCE",
}
GOAL_EVIDENCE_BUDGET = 16000


def normalize_meeting_policy(value):
    text = (value or "").strip().upper()
    if text in {"NEVER", "NONE", "NO MEETINGS"} or "NEVER" in text:
        return "NEVER"
    if text == "AUTO":
        return "AUTO"
    return "REQUIRED_ON_MATERIAL_CONFLICT"


def validate_plan(payload):
    if not isinstance(payload, dict) or set(payload) != PLAN_FIELDS:
        raise ValueError("Invalid OPERATION_PLAN fields")
    if payload.get("mode") != "OPERATION_PLAN":
        raise ValueError("Operation mode must be OPERATION_PLAN")
    if not isinstance(payload.get("executive_response"), str) or not payload[
        "executive_response"
    ].strip():
        raise ValueError("Executive response is required")
    operation = payload.get("operation")
    if not isinstance(operation, dict) or set(operation) != OPERATION_FIELDS:
        raise ValueError("Invalid operation fields")
    for field in ("title", "objective", "meeting_policy"):
        if not isinstance(operation[field], str) or not operation[field].strip():
            raise ValueError(f"Operation {field} is required")
    try:
        budget = Decimal(str(operation["budget_twd"]))
    except (InvalidOperation, TypeError):
        raise ValueError("Operation budget must be numeric")
    if budget <= 0:
        raise ValueError("Operation budget must be positive")
    if operation["project_id"] is not None and not db.session.get(
        Project, operation["project_id"]
    ):
        raise ValueError("Operation references an unknown Project")
    tasks = operation["tasks"]
    if not isinstance(tasks, list) or not 1 <= len(tasks) <= 12:
        raise ValueError("Operation requires 1–12 tasks")
    for item in tasks:
        if not isinstance(item, dict) or set(item) != TASK_FIELDS:
            raise ValueError("Invalid operation task fields")
        if not item["title"].strip() or not item["objective"].strip():
            raise ValueError("Task title and objective are required")
        if not isinstance(item["acceptance_criteria"], list) or not all(
            isinstance(value, str) and value.strip()
            for value in item["acceptance_criteria"]
        ):
            raise ValueError("Task acceptance criteria are required")
        assignee = db.session.get(Employee, item["assignee_employee_id"])
        reviewer = db.session.get(Employee, item["reviewer_employee_id"])
        if not assignee or not assignee.active:
            raise ValueError("Unknown or inactive assignee")
        if not reviewer or not reviewer.active:
            raise ValueError("Unknown or inactive reviewer")
    criteria = operation["completion_criteria"]
    if not isinstance(criteria, list) or not 1 <= len(criteria) <= 12 or not all(
        isinstance(value, str) and value.strip() for value in criteria
    ):
        raise ValueError(
            "Operation requires between 1 and 12 completion criteria"
        )
    return payload


def propose_operation(ceo, payload):
    plan = validate_plan(payload)
    plan["operation"]["meeting_policy"] = normalize_meeting_policy(
        plan["operation"]["meeting_policy"]
    )
    data = plan["operation"]
    operation = Operation(
        title=data["title"], objective=data["objective"],
        project_id=data["project_id"], proposed_by_employee_id=ceo.id,
        status="PLANNED", plan_json=plan,
        approved_budget_twd=Decimal(str(data["budget_twd"])),
        memory_json={"meeting_results": [], "decisions": [], "hiring": []},
    )
    db.session.add(operation)
    db.session.commit()
    return operation


def approve(operation):
    if operation.status == "RUNNING":
        return operation
    if operation.status != "PLANNED":
        raise ValueError("Only a planned operation may be approved")
    plan = validate_plan(operation.plan_json)
    data = plan["operation"]
    if operation.project_id:
        project = db.session.get(Project, operation.project_id)
    else:
        project = Project(
            name=data["title"], objective=data["objective"], status="ACTIVE",
            priority="HIGH", environment="LIVE", origin="CEO_OPERATION",
            owner_employee_id=operation.proposed_by_employee_id,
            real_budget_limit=operation.approved_budget_twd,
            current_state_summary="CEO operation approved and ready.",
            next_milestone=data["completion_criteria"][0],
        )
        db.session.add(project)
        db.session.flush()
        operation.project_id = project.id
    if not operation.tasks:
        for item in data["tasks"]:
            db.session.add(Task(
                project_id=project.id, operation_id=operation.id,
                title=item["title"], objective=item["objective"],
                status="ASSIGNED", priority="HIGH",
                created_by_employee_id=operation.proposed_by_employee_id,
                assigned_employee_id=item["assignee_employee_id"],
                reviewer_employee_id=item["reviewer_employee_id"],
                required_output="Operation result",
                acceptance_criteria="\n".join(item["acceptance_criteria"]),
            ))
    operation.status = "RUNNING"
    operation.approved_at = operation.approved_at or now()
    project.current_state_summary = "CEO is managing the approved operation."
    db.session.commit()
    return operation


def actual_cost(operation):
    value = db.session.query(func.coalesce(
        func.sum(CostEvent.real_cost_delta), 0
    )).filter_by(operation_id=operation.id).scalar()
    operation.actual_cost_twd = Decimal(value)
    return Decimal(value)


def remaining_budget(operation):
    return Decimal(operation.approved_budget_twd) - actual_cost(operation)


def ensure_budget(operation, estimate=Decimal("0"), company_checked=False):
    estimate = Decimal(estimate)
    if operation.status != "RUNNING":
        raise ValueError("Operation is not running")
    if not company_checked and (
        company_remaining() <= 0 or estimate > company_remaining()
    ):
        raise ValueError("Company real budget is exhausted")
    remaining = remaining_budget(operation)
    if remaining <= 0 or estimate > remaining:
        additional = max(Decimal("0"), estimate - max(remaining, Decimal("0")))
        raise ValueError(
            f"operation budget is exhausted; maximum additional authorization "
            f"required TWD {additional}"
        )
    return True


def completion_guard(operation):
    reasons = []
    statuses = {task.status for task in operation.tasks}
    if "BLOCKED" in statuses:
        reasons.append("Blocked Task remains unresolved")
    unfinished = statuses - {"DONE", "CANCELLED"}
    if unfinished:
        reasons.append("Required Task or Review remains unfinished")
    if operation.status == "WAITING_FOR_FOUNDER":
        reasons.append("Founder decision remains unresolved")
    meetings = Meeting.query.filter_by(operation_id=operation.id).all()
    if any(meeting.status not in {
        "ENDED", "TERMINATED_BY_FOUNDER"
    } for meeting in meetings):
        reasons.append("Required Meeting remains unresolved")
    requests = HiringRequest.query.filter_by(operation_id=operation.id).all()
    if any(item.status in {
        "REQUESTED", "HR_REVIEW", "FOUNDER_REVIEW"
    } for item in requests):
        reasons.append("Hiring decision remains unresolved")
    criteria = operation.plan_json["operation"].get("completion_criteria") or []
    if not criteria:
        reasons.append("Completion criteria are missing")
    return not reasons, reasons


def _compact_value(value, string_limit=700, list_limit=12):
    if isinstance(value, str):
        return value if len(value) <= string_limit else value[:string_limit - 1] + "…"
    if isinstance(value, list):
        return [
            _compact_value(item, string_limit, list_limit)
            for item in value[-list_limit:]
        ]
    if isinstance(value, dict):
        return {
            key: _compact_value(item, string_limit, list_limit)
            for key, item in value.items()
        }
    return value


def goal_evidence_packet(operation):
    completion_criteria = list(
        operation.plan_json["operation"].get("completion_criteria") or []
    )
    if not 1 <= len(completion_criteria) <= 12:
        raise ValueError(
            "Goal Verification requires between 1 and 12 approved "
            "completion criteria"
        )
    reviews = AgentRun.query.filter_by(
        operation_id=operation.id, purpose="TASK_REVIEW", status="SUCCEEDED"
    ).order_by(AgentRun.id.desc()).all()
    latest_reviews = {}
    for run in reviews:
        if run.task_id not in latest_reviews:
            latest_reviews[run.task_id] = run
    memory = operation.memory_json or {}
    from .brain import current
    brain = current(operation.project_id)
    packet = {
        "founder_objective": operation.objective,
        "completion_criteria": completion_criteria,
        "operation": {
            "id": operation.id,
            "status": operation.status,
            "approved_budget_twd": str(operation.approved_budget_twd),
        },
        "completed_tasks": [{
            "task_id": task.id,
            "title": task.title,
            "objective": task.objective,
            "status": task.status,
            "result": task.result_summary,
            "acceptance_criteria": task.acceptance_criteria,
        } for task in operation.tasks if task.status in {"DONE", "CANCELLED"}],
        "latest_reviews": [{
            "task_id": task_id,
            "decision": (run.parsed_output_json or {}).get("decision"),
            "summary": (run.parsed_output_json or {}).get("summary"),
            "issues": (run.parsed_output_json or {}).get("issues") or [],
            "required_changes": (
                (run.parsed_output_json or {}).get("required_changes") or []
            ),
        } for task_id, run in sorted(latest_reviews.items())],
        "meeting_results": (memory.get("meeting_results") or [])[-4:],
        "ceo_decisions": [
            decision for decision in (memory.get("decisions") or [])
            if decision.get("action") != "COMPLETE"
        ][-6:],
        "company_brain_evidence": [{
            "id": item.id, "kind": item.kind, "title": item.title,
            "content": item.content,
        } for item in brain[-12:]],
    }
    supporting = {
        key: value for key, value in packet.items()
        if key != "completion_criteria"
    }
    packet = _compact_value(supporting)
    packet["completion_criteria"] = completion_criteria
    serialized = json.dumps(
        packet, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    if len(serialized) > GOAL_EVIDENCE_BUDGET:
        packet = _compact_value(
            supporting, string_limit=250, list_limit=8
        )
        packet["completion_criteria"] = completion_criteria
        serialized = json.dumps(
            packet, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        )
    if len(serialized) > GOAL_EVIDENCE_BUDGET:
        packet = _compact_value(
            supporting, string_limit=80, list_limit=4
        )
        packet["completion_criteria"] = completion_criteria
        serialized = json.dumps(
            packet, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        )
    if len(serialized) > GOAL_EVIDENCE_BUDGET:
        raise ValueError("Goal verification evidence exceeds bounded context")
    digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    return packet, serialized, digest


def _validate_goal_verification(payload, criteria):
    required = {"overall_status", "criteria", "summary", "recommended_action"}
    if not isinstance(payload, dict) or set(payload) != required:
        raise ValueError("Invalid Goal Verification fields")
    if payload["overall_status"] not in GOAL_STATUSES:
        raise ValueError("Invalid Goal Verification status")
    if not isinstance(payload["summary"], str) or not payload["summary"].strip():
        raise ValueError("Goal Verification summary is required")
    if (
        not isinstance(payload["recommended_action"], str)
        or not payload["recommended_action"].strip()
    ):
        raise ValueError("Goal Verification recommendation is required")
    rows = payload["criteria"]
    if not isinstance(rows, list) or len(rows) != len(criteria):
        raise ValueError("Goal Verification must assess every criterion")
    seen = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != {
            "criterion", "status", "evidence", "reason",
        }:
            raise ValueError("Invalid Goal Verification criterion fields")
        if row["status"] not in GOAL_STATUSES:
            raise ValueError("Invalid criterion status")
        if (
            not isinstance(row["evidence"], list)
            or not all(isinstance(item, str) for item in row["evidence"])
            or not isinstance(row["reason"], str)
            or not row["reason"].strip()
        ):
            raise ValueError("Invalid Goal Verification criterion evidence")
        seen.append(row["criterion"])
    if seen != list(criteria):
        raise ValueError("Goal Verification criteria do not match approval")
    if payload["overall_status"] == "SATISFIED" and any(
        row["status"] != "SATISFIED" for row in rows
    ):
        raise ValueError("Satisfied verification contains an unsatisfied criterion")
    return payload


def _persist_goal_verification(operation, payload, digest):
    memory = dict(operation.memory_json or {})
    memory["goal_verification"] = {
        **payload, "evidence_digest": digest,
    }
    operation.memory_json = memory
    db.session.commit()


def _current_goal_verification(operation):
    _, _, digest = goal_evidence_packet(operation)
    step = OperationStep.query.filter_by(
        operation_id=operation.id,
        logical_key=f"goal_verification:{digest}",
        kind="GOAL_VERIFICATION", status="SUCCEEDED",
    ).first()
    return step, digest


def _logical_key(operation, kind, task=None, subject=None):
    if task:
        suffix = "review" if kind == "REVIEW" else "execute"
        attempts = OperationStep.query.filter_by(
            operation_id=operation.id, kind=kind, task_id=task.id
        ).count()
        return f"task:{task.id}:{suffix}:{attempts + 1}"
    if subject is not None:
        return f"{kind.lower()}:{subject}"
    count = OperationStep.query.filter_by(
        operation_id=operation.id, kind=kind
    ).count()
    return f"{kind.lower()}:{count + 1}"


def _claim(operation, request_key, logical_key, kind, task=None):
    existing = OperationStep.query.filter_by(
        operation_id=operation.id, idempotency_key=request_key
    ).first()
    if not existing:
        existing = OperationStep.query.filter_by(
            operation_id=operation.id, logical_key=logical_key
        ).first()
    if existing:
        return existing, False
    step = OperationStep(
        operation_id=operation.id, idempotency_key=request_key,
        logical_key=logical_key, kind=kind,
        task_id=getattr(task, "id", None), status="CLAIMED",
    )
    db.session.add(step)
    db.session.commit()
    return step, True


def _provider_boundary(step):
    step.status = "PROVIDER_CALL_STARTED"
    step.provider_started_at = now()
    db.session.commit()


def _finish(step, run, result):
    step.agent_run_id = getattr(run, "id", None)
    step.status = "SUCCEEDED"
    step.result_json = result
    step.finished_at = now()
    db.session.commit()
    return result


def _existing_result(step):
    if step.result_json is not None:
        return step.result_json
    return {"kind": step.kind, "status": step.status}


def _mark_paid_or_ambiguous(operation, step, run, error):
    step.agent_run_id = getattr(run, "id", None)
    paid = bool(run and run.real_cost and Decimal(run.real_cost) > 0)
    step.status = "PAID_FAILED" if paid else "AMBIGUOUS"
    step.error_text = str(error)
    step.finished_at = now()
    wait_for_founder(
        operation,
        ("A paid internal step failed. Founder recovery authority is required."
         if paid else
         "Provider-call truth is ambiguous; recovery is required before continuing."),
    )
    db.session.commit()


def _materialize_task_run(task, run):
    payload = run.parsed_output_json or json.loads(run.raw_output)
    task.result_summary = payload["result_summary"]
    if task.status == "ASSIGNED":
        task.status = "WORKING"
    if task.status == "WORKING":
        task.status = "REVIEW"
    db.session.commit()


def _materialize_review_run(task, run):
    payload = run.parsed_output_json or json.loads(run.raw_output)
    target = DECISIONS[payload["decision"]]
    task.status = target
    if target == "DONE":
        task.completed_at = now()
    db.session.commit()


def _recover_open_step(operation):
    step = OperationStep.query.filter(
        OperationStep.operation_id == operation.id,
        OperationStep.status.in_(OPEN_STEP),
    ).order_by(OperationStep.id).first()
    if not step:
        return None
    run = db.session.get(AgentRun, step.agent_run_id) if step.agent_run_id else None
    if run and run.status == "SUCCEEDED" and (
        run.parsed_output_json or run.raw_output
    ):
        task = db.session.get(Task, step.task_id) if step.task_id else None
        if step.kind == "TASK" and task:
            _materialize_task_run(task, run)
        elif step.kind == "REVIEW" and task:
            _materialize_review_run(task, run)
        elif step.kind == "GOAL_VERIFICATION":
            payload = run.parsed_output_json or json.loads(run.raw_output)
            criteria = (
                operation.plan_json["operation"].get("completion_criteria") or []
            )
            payload = _validate_goal_verification(payload, criteria)
            digest = step.logical_key.split(":", 1)[1]
            _persist_goal_verification(operation, payload, digest)
            return _finish(step, run, {
                "kind": "GOAL_VERIFICATION",
                "overall_status": payload["overall_status"],
                "verification": payload,
                "evidence_digest": digest,
                "agent_run_id": run.id,
                "status": "RECOVERED",
            })
        elif step.kind == "DECISION":
            decision = run.parsed_output_json or json.loads(run.raw_output)
            action = decision.get("action")
            plan = decision.get("task_plan") or {}
            if action == "CREATE_TASK":
                task = create_remediation_task(
                    operation, plan, agent_run_id=run.id
                )
                result = {
                    "kind": "DECISION", "action": action,
                    "task_id": task.id, "agent_run_id": run.id,
                    "status": "RECOVERED",
                }
            elif action == "REASSIGN_TASK":
                task = db.session.get(Task, plan.get("task_id"))
                if not task:
                    raise ValueError(
                        "Recovered REASSIGN_TASK references an unknown Task"
                    )
                reassign_task(
                    operation, task,
                    _active_employee(plan.get("assignee_employee_id"), "Assignee"),
                    _active_employee(plan.get("reviewer_employee_id"), "Reviewer"),
                    plan.get("reason") or decision.get("reason"),
                    agent_run_id=run.id,
                )
                result = {
                    "kind": "DECISION", "action": action,
                    "task_id": task.id,
                    "assignee_employee_id": task.assigned_employee_id,
                    "reviewer_employee_id": task.reviewer_employee_id,
                    "agent_run_id": run.id, "status": "RECOVERED",
                }
            else:
                step.status = "AMBIGUOUS"
                wait_for_founder(
                    operation,
                    "A paid CEO decision requires Founder recovery review.",
                )
                raise ValueError("Operation recovery requires Founder attention")
            memory = dict(operation.memory_json or {})
            decisions = list(memory.get("decisions") or [])
            if decision not in decisions:
                decisions.append(decision)
                memory["decisions"] = decisions
                operation.memory_json = memory
            return _finish(step, run, result)
        else:
            step.status = "AMBIGUOUS"
            wait_for_founder(
                operation, "A paid result exists but requires Founder recovery review."
            )
            raise ValueError("Operation recovery requires Founder attention")
        return _finish(step, run, {
            "kind": step.kind, "status": "RECOVERED",
            "task_id": getattr(task, "id", None), "agent_run_id": run.id,
        })
    step.status = "PAID_FAILED" if (
        run and run.real_cost and Decimal(run.real_cost) > 0
    ) else "AMBIGUOUS"
    wait_for_founder(
        operation,
        "Operation provider-call recovery is required; automatic replay is blocked.",
    )
    db.session.commit()
    raise ValueError("Operation recovery requires Founder attention")


def _run_task_step(operation, request_key, task):
    logical = _logical_key(operation, "TASK", task)
    step, created = _claim(operation, request_key, logical, "TASK", task)
    if not created:
        if step.status in OPEN_STEP:
            return _recover_open_step(operation)
        return _existing_result(step)
    if task.status == "ASSIGNED":
        transition(task, "WORKING")
    _provider_boundary(step)
    run = None
    try:
        run = run_task(task)
        step.agent_run_id = run.id
        db.session.commit()
        if run.status != "SUCCEEDED" or not run.parsed_output_json:
            raise ValueError(run.error_text or "Task execution failed")
        transition(task, "REVIEW")
        return _finish(step, run, {
            "kind": "TASK", "status": "SUCCEEDED", "task_id": task.id,
            "agent_run_id": run.id,
        })
    except Exception as exc:
        run = run or AgentRun.query.filter_by(
            operation_id=operation.id, task_id=task.id,
            purpose="TASK_EXECUTION"
        ).order_by(AgentRun.id.desc()).first()
        _mark_paid_or_ambiguous(operation, step, run, exc)
        raise


def _run_review_step(operation, request_key, task):
    logical = _logical_key(operation, "REVIEW", task)
    step, created = _claim(operation, request_key, logical, "REVIEW", task)
    if not created:
        if step.status in OPEN_STEP:
            return _recover_open_step(operation)
        return _existing_result(step)
    _provider_boundary(step)
    run = None
    try:
        run = run_review(task)
        step.agent_run_id = run.id
        db.session.commit()
        if run.status != "SUCCEEDED" or not run.parsed_output_json:
            raise ValueError(run.error_text or "Review execution failed")
        return _finish(step, run, {
            "kind": "REVIEW", "status": "SUCCEEDED", "task_id": task.id,
            "decision": run.parsed_output_json["decision"],
            "agent_run_id": run.id,
        })
    except Exception as exc:
        run = run or AgentRun.query.filter_by(
            operation_id=operation.id, task_id=task.id,
            purpose="TASK_REVIEW"
        ).order_by(AgentRun.id.desc()).first()
        _mark_paid_or_ambiguous(operation, step, run, exc)
        raise


def create_meeting(operation, reason, participants, budget_twd):
    budget = Decimal(budget_twd)
    ensure_budget(operation, budget)
    if not reason.strip():
        raise ValueError("CEO Meeting reason is required")
    ceo = db.session.get(Employee, operation.proposed_by_employee_id)
    meeting = meeting_service.create(
        title=f"{operation.title} — Internal Review",
        purpose=reason, agenda=reason, chair=ceo,
        participants=participants, project=operation.project,
        max_rounds=2, token_limit=6000,
        real_cost_limit_twd=budget, profile="ECONOMY",
    )
    meeting.created_by = "CEO"
    meeting.operation_id = operation.id
    db.session.commit()
    return meeting


def create_meeting_for_conflict(operation, task, reason, budget_twd):
    employees = []
    for employee in (
        db.session.get(Employee, operation.proposed_by_employee_id),
        task.assigned_employee, task.reviewer,
    ):
        if employee and employee.id not in {item.id for item in employees}:
            employees.append(employee)
    meeting = create_meeting(operation, reason, employees, budget_twd)
    meeting_service.start_auto(meeting)
    return meeting


def _consume_meeting(operation, meeting, request_key):
    logical = _logical_key(operation, "MEETING_RESULT", subject=meeting.id)
    step, created = _claim(
        operation, request_key, logical, "MEETING_RESULT"
    )
    if not created:
        return _existing_result(step)
    result = meeting_service.result_view(meeting)
    if not result:
        raise ValueError("Meeting has no usable result")
    memory = dict(operation.memory_json or {})
    outcomes = list(memory.get("meeting_results") or [])
    outcomes.append({"meeting_id": meeting.id, "result": result})
    memory["meeting_results"] = outcomes
    operation.memory_json = memory
    db.session.commit()
    return _finish(step, None, {
        "kind": "MEETING_RESULT", "status": "CONSUMED",
        "meeting_id": meeting.id,
    })


def _active_employee(employee_id, label):
    employee = db.session.get(Employee, employee_id)
    if not employee or not employee.active:
        raise ValueError(f"{label} must be an active Employee")
    return employee


def create_remediation_task(operation, plan, agent_run_id=None):
    if agent_run_id:
        audit = WorkMessage.query.filter_by(
            agent_run_id=agent_run_id, message_type="CEO_REMEDIATION"
        ).first()
        if audit and audit.task_id:
            return db.session.get(Task, audit.task_id)
    if operation.status != "RUNNING" or not operation.approved_at:
        raise ValueError("Operation must be Founder-approved and running")
    if Task.query.filter_by(operation_id=operation.id).count() >= 20:
        wait_for_founder(
            operation,
            "Operation reached the deterministic ceiling of 20 total Tasks.",
        )
        db.session.commit()
        raise ValueError("Operation cannot exceed 20 total Tasks")
    required = {
        "title", "objective", "assignee_employee_id", "reviewer_employee_id",
        "acceptance_criteria", "reason",
    }
    if not isinstance(plan, dict) or not required.issubset(plan):
        raise ValueError("CREATE_TASK requires a complete task_plan")
    if not all(
        isinstance(plan.get(field), str) and plan[field].strip()
        for field in ("title", "objective", "reason")
    ):
        raise ValueError("Remediation title, objective, and reason are required")
    criteria = plan["acceptance_criteria"]
    if not isinstance(criteria, list) or not criteria or not all(
        isinstance(item, str) and item.strip() for item in criteria
    ):
        raise ValueError("Remediation acceptance criteria are required")
    assignee = _active_employee(plan["assignee_employee_id"], "Assignee")
    reviewer = _active_employee(plan["reviewer_employee_id"], "Reviewer")
    blocked = next(
        (item for item in operation.tasks if item.status == "BLOCKED"), None
    )
    verification = (operation.memory_json or {}).get("goal_verification") or {}
    verification_requires_work = verification.get(
        "overall_status"
    ) in {"NOT_SATISFIED", "INSUFFICIENT_EVIDENCE"}
    if not blocked and not HiringRequest.query.filter_by(
        operation_id=operation.id, status="HIRED"
    ).first() and not verification_requires_work:
        raise ValueError(
            "Remediation Task requires blocked work, an approved capability "
            "change, or an unsatisfied Goal Verification"
        )
    task = Task(
        operation_id=operation.id, project_id=operation.project_id,
        parent_task_id=getattr(blocked, "id", None),
        title=plan["title"].strip(), objective=plan["objective"].strip(),
        status="ASSIGNED", priority="HIGH",
        created_by_employee_id=operation.proposed_by_employee_id,
        assigned_employee_id=assignee.id, reviewer_employee_id=reviewer.id,
        required_output="Remediation result",
        acceptance_criteria="\n".join(item.strip() for item in criteria),
    )
    db.session.add(task)
    db.session.flush()
    db.session.add(WorkMessage(
        project_id=operation.project_id, task_id=task.id,
        sender_employee_id=operation.proposed_by_employee_id,
        recipient_employee_id=assignee.id,
        message_type="CEO_REMEDIATION",
        content=plan["reason"].strip(),
        agent_run_id=agent_run_id,
    ))
    db.session.commit()
    return task


def reassign_task(
    operation, task, assignee, reviewer, reason, agent_run_id=None
):
    if agent_run_id:
        audit = WorkMessage.query.filter_by(
            agent_run_id=agent_run_id, message_type="CEO_REASSIGNMENT"
        ).first()
        if audit:
            return task
    if operation.status != "RUNNING" or not operation.approved_at:
        raise ValueError("Operation must be Founder-approved and running")
    if task.operation_id != operation.id or task.project_id != operation.project_id:
        raise ValueError("Task is outside the approved Operation")
    if task.status not in {"ASSIGNED", "WORKING", "BLOCKED"}:
        raise ValueError(f"Task in {task.status} cannot be reassigned")
    assignee = _active_employee(getattr(assignee, "id", assignee), "Assignee")
    reviewer = _active_employee(getattr(reviewer, "id", reviewer), "Reviewer")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("Reassignment audit reason is required")
    previous_assignee_id = task.assigned_employee_id
    previous_reviewer_id = task.reviewer_employee_id
    task.assigned_employee_id = assignee.id
    task.reviewer_employee_id = reviewer.id
    if task.status == "BLOCKED":
        task.status = "ASSIGNED"
    db.session.add(WorkMessage(
        project_id=operation.project_id, task_id=task.id,
        sender_employee_id=operation.proposed_by_employee_id,
        recipient_employee_id=assignee.id,
        message_type="CEO_REASSIGNMENT",
        content=(
            f"{reason.strip()} Previous assignee #{previous_assignee_id}; "
            f"previous reviewer #{previous_reviewer_id}; new assignee "
            f"#{assignee.id}; new reviewer #{reviewer.id}."
        ),
        agent_run_id=agent_run_id,
    ))
    db.session.commit()
    return task


def _advance_meeting(operation, meeting, request_key):
    sequence = OperationStep.query.filter_by(
        operation_id=operation.id, kind="MEETING_STEP"
    ).count() + 1
    logical = _logical_key(
        operation, "MEETING_STEP", subject=f"{meeting.id}:{sequence}"
    )
    step, created = _claim(
        operation, request_key, logical, "MEETING_STEP"
    )
    if not created:
        return _existing_result(step)
    _provider_boundary(step)
    try:
        result = meeting_service.next_step_idempotent(meeting, logical)
        run = AgentRun.query.filter_by(
            meeting_id=meeting.id
        ).order_by(AgentRun.id.desc()).first()
        if meeting.paid_failure_json:
            step.agent_run_id = getattr(run, "id", None)
            step.status = "PAID_FAILED"
            wait_for_founder(
                operation,
                "A paid Meeting step failed. Founder recovery authority is required.",
            )
            db.session.commit()
            raise ValueError("Paid Meeting step failed")
        return _finish(step, run, {
            "kind": "MEETING_STEP", "meeting_id": meeting.id,
            "meeting_status": meeting.status, "status": "SUCCEEDED",
        })
    except Exception as exc:
        if step.status != "PAID_FAILED":
            run = AgentRun.query.filter_by(
                meeting_id=meeting.id
            ).order_by(AgentRun.id.desc()).first()
            _mark_paid_or_ambiguous(operation, step, run, exc)
        raise


def _decision_step(operation, request_key, blocked_task=None):
    from .ceo_context import compose
    iteration = OperationStep.query.filter_by(
        operation_id=operation.id, kind="DECISION"
    ).count() + 1
    logical = _logical_key(operation, "DECISION", subject=iteration)
    step, created = _claim(
        operation, request_key, logical, "DECISION", blocked_task
    )
    if not created:
        return _existing_result(step)
    ceo = db.session.get(Employee, operation.proposed_by_employee_id)
    composed = compose(
        ceo, founder_request="Choose the next internal operation action.",
        operation=operation, project=operation.project,
    )
    _provider_boundary(step)
    run = None
    try:
        run = execute(
            ceo, "CEO_OPERATION_DECISION",
            f"Decide operation #{operation.id} next action",
            project=operation.project, operation=operation,
            context_override=composed.text,
            context_composition=composed.composition,
            system_prompt_override=(
                ceo.system_instructions
                + "\nCEO_OPERATION_DECISION\nReturn one strict bounded internal decision."
            ),
            response_schema=CEO_DECISION_SCHEMA,
        )
        step.agent_run_id = run.id
        db.session.commit()
        if run.status != "SUCCEEDED":
            raise ValueError(run.error_text or "CEO decision failed")
        decision = json.loads(run.raw_output)
        run.parsed_output_json = decision
        action = decision["action"]
        persist_decision = True
        if action == "MEETING":
            if normalize_meeting_policy(
                operation.plan_json["operation"]["meeting_policy"]
            ) == "NEVER":
                raise ValueError("Approved Meeting policy forbids a CEO Meeting")
            if not blocked_task:
                blocked_task = next(
                    (item for item in operation.tasks if item.status == "BLOCKED"),
                    None,
                )
            details = decision.get("meeting") or {}
            meeting = create_meeting_for_conflict(
                operation, blocked_task,
                details.get("question") or decision["reason"],
                Decimal(str(details.get("budget_twd") or "0.5")),
            )
            result = {
                "kind": "DECISION", "action": action,
                "meeting_id": meeting.id, "agent_run_id": run.id,
            }
        elif action == "HIRING_REQUEST":
            from .workforce import request_hire
            need = decision.get("hiring_need") or {}
            request = request_hire(
                requester=ceo, requested_by_type="EMPLOYEE",
                operation=operation, project=operation.project, **need,
            )
            result = {
                "kind": "DECISION", "action": action,
                "hiring_request_id": request.id, "agent_run_id": run.id,
            }
        elif action == "FOUNDER":
            wait_for_founder(
                operation,
                decision.get("founder_request") or decision["reason"],
            )
            result = {
                "kind": "DECISION", "action": action,
                "agent_run_id": run.id,
            }
        elif action == "CONTINUE":
            target = db.session.get(Task, decision.get("next_task_id")) if (
                decision.get("next_task_id")
            ) else blocked_task
            if target and target.status == "BLOCKED":
                target.status = "WORKING"
            result = {
                "kind": "DECISION", "action": action,
                "next_task_id": getattr(target, "id", None),
                "agent_run_id": run.id,
            }
        elif action == "CREATE_TASK":
            task = create_remediation_task(
                operation, decision.get("task_plan") or {},
                agent_run_id=run.id,
            )
            result = {
                "kind": "DECISION", "action": action,
                "task_id": task.id, "agent_run_id": run.id,
            }
        elif action == "REASSIGN_TASK":
            plan = decision.get("task_plan") or {}
            task = db.session.get(Task, plan.get("task_id"))
            if not task:
                raise ValueError("REASSIGN_TASK references an unknown Task")
            assignee = _active_employee(
                plan.get("assignee_employee_id"), "Assignee"
            )
            reviewer = _active_employee(
                plan.get("reviewer_employee_id"), "Reviewer"
            )
            reassign_task(
                operation, task, assignee, reviewer,
                plan.get("reason") or decision["reason"],
                agent_run_id=run.id,
            )
            result = {
                "kind": "DECISION", "action": action,
                "task_id": task.id, "assignee_employee_id": assignee.id,
                "reviewer_employee_id": reviewer.id,
                "agent_run_id": run.id,
            }
        elif action == "COMPLETE":
            allowed, reasons = completion_guard(operation)
            if not allowed:
                raise ValueError(
                    "CEO cannot complete operation: " + "; ".join(reasons)
                )
            verification_step, _ = _current_goal_verification(operation)
            verification = (
                (verification_step.result_json or {}).get("verification")
                if verification_step else None
            )
            if (
                not verification
                or verification.get("overall_status") != "SATISFIED"
            ):
                persist_decision = False
                result = {
                    "kind": "DECISION", "action": "COMPLETE_REJECTED",
                    "requested_action": "COMPLETE",
                    "reason": (
                        "Current Goal Verification is not SATISFIED; "
                        "the Operation remains in remediation."
                    ),
                    "agent_run_id": run.id,
                }
            else:
                result = {
                    "kind": "DECISION", "action": action,
                    "agent_run_id": run.id,
                }
        else:
            raise ValueError("Unknown CEO decision action")
        if persist_decision:
            memory = dict(operation.memory_json or {})
            decisions = list(memory.get("decisions") or [])
            decisions.append(decision)
            memory["decisions"] = decisions
            operation.memory_json = memory
        db.session.commit()
        return _finish(step, run, result)
    except Exception as exc:
        run = run or AgentRun.query.filter_by(
            operation_id=operation.id, purpose="CEO_OPERATION_DECISION"
        ).order_by(AgentRun.id.desc()).first()
        _mark_paid_or_ambiguous(operation, step, run, exc)
        raise


def _hr_step(operation, request, request_key):
    from .workforce import assess_request
    logical = _logical_key(
        operation, "HR_ASSESSMENT", subject=request.id
    )
    step, created = _claim(
        operation, request_key, logical, "HR_ASSESSMENT"
    )
    if not created:
        return _existing_result(step)
    _provider_boundary(step)
    try:
        run = assess_request(request)
        step.agent_run_id = run.id
        wait_for_founder(
            operation,
            f"HR recommendation for {request.role_needed} requires Founder review.",
        )
        return _finish(step, run, {
            "kind": "HR_ASSESSMENT", "status": "FOUNDER_REVIEW",
            "hiring_request_id": request.id, "agent_run_id": run.id,
        })
    except Exception as exc:
        run = AgentRun.query.filter_by(
            operation_id=operation.id, purpose="HR_ASSESSMENT"
        ).order_by(AgentRun.id.desc()).first()
        _mark_paid_or_ambiguous(operation, step, run, exc)
        raise


def _goal_verification_step(operation, request_key):
    allowed, reasons = completion_guard(operation)
    if not allowed:
        raise ValueError(
            "Workflow is not ready for Goal Verification: " + "; ".join(reasons)
        )
    packet, serialized, digest = goal_evidence_packet(operation)
    logical = f"goal_verification:{digest}"
    step, created = _claim(
        operation, request_key, logical, "GOAL_VERIFICATION"
    )
    if not created:
        if step.status in OPEN_STEP:
            return _recover_open_step(operation)
        return _existing_result(step)
    ceo = db.session.get(Employee, operation.proposed_by_employee_id)
    _provider_boundary(step)
    run = None
    try:
        run = execute(
            ceo, "GOAL_VERIFICATION",
            f"Verify achievement of Operation #{operation.id}",
            project=operation.project, operation=operation,
            context_override="GOAL VERIFICATION EVIDENCE\n" + serialized,
            context_composition={
                "goal_verification_evidence": {
                    "characters": len(serialized),
                    "budget": GOAL_EVIDENCE_BUDGET,
                    "completed_tasks": len(packet["completed_tasks"]),
                    "latest_reviews": len(packet["latest_reviews"]),
                    "meeting_results": len(packet["meeting_results"]),
                    "brain_items": len(packet["company_brain_evidence"]),
                    "evidence_digest": digest,
                },
            },
            system_prompt_override=(
                ceo.system_instructions
                + "\nGOAL_VERIFICATION\nIndependently assess whether the "
                "Founder objective and every completion criterion are supported "
                "by the supplied persisted evidence. Return only strict JSON."
            ),
            response_schema=GOAL_VERIFICATION_SCHEMA,
        )
        step.agent_run_id = run.id
        db.session.commit()
        if run.status != "SUCCEEDED":
            raise ValueError(run.error_text or "Goal Verification failed")
        payload = _validate_goal_verification(
            json.loads(run.raw_output), packet["completion_criteria"]
        )
        run.parsed_output_json = payload
        _persist_goal_verification(operation, payload, digest)
        return _finish(step, run, {
            "kind": "GOAL_VERIFICATION",
            "overall_status": payload["overall_status"],
            "verification": payload,
            "evidence_digest": digest,
            "agent_run_id": run.id,
        })
    except Exception as exc:
        run = run or AgentRun.query.filter_by(
            operation_id=operation.id, purpose="GOAL_VERIFICATION"
        ).order_by(AgentRun.id.desc()).first()
        _mark_paid_or_ambiguous(operation, step, run, exc)
        raise


def _report_step(operation, request_key):
    from .ceo_context import compose
    allowed, reasons = completion_guard(operation)
    if not allowed:
        return _decision_step(
            operation, request_key,
            next((task for task in operation.tasks if task.status == "BLOCKED"),
                 None),
        )
    verification_step, _ = _current_goal_verification(operation)
    verification = (
        (verification_step.result_json or {}).get("verification")
        if verification_step else None
    )
    if not verification or verification.get("overall_status") != "SATISFIED":
        raise ValueError(
            "Operation cannot complete without a current SATISFIED "
            "Goal Verification"
        )
    logical = _logical_key(operation, "REPORT", subject="final")
    step, created = _claim(
        operation, request_key, logical, "REPORT"
    )
    if not created:
        return _existing_result(step)
    ceo = db.session.get(Employee, operation.proposed_by_employee_id)
    composed = compose(
        ceo, founder_request="Produce the final Founder operation report.",
        operation=operation, project=operation.project,
    )
    _provider_boundary(step)
    run = None
    try:
        run = execute(
            ceo, "CEO_OPERATION_REPORT",
            f"Report completion of operation #{operation.id}",
            project=operation.project, operation=operation,
            context_override=composed.text,
            context_composition=composed.composition,
            system_prompt_override=(
                ceo.system_instructions
                + "\nCEO_PROJECT_SYNTHESIS\nReturn only strict JSON briefing fields."
            ),
            response_schema=SYNTHESIS_SCHEMA,
        )
        step.agent_run_id = run.id
        db.session.commit()
        if run.status != "SUCCEEDED":
            raise ValueError(run.error_text or "CEO report failed")
        payload = json.loads(run.raw_output)
        operation.status = "COMPLETED"
        operation.ended_at = now()
        operation.founder_report_json = {
            "headline": "Boss, it's complete.",
            "summary": payload["executive_summary"],
            "result": payload["result"],
            "next_move": (payload["recommended_next_actions"] or [
                "Review the completed result."
            ])[0],
            "cost_twd": str(actual_cost(operation)),
            "authorized_twd": str(operation.approved_budget_twd),
        }
        operation.project.status = "REVIEW"
        operation.project.current_state_summary = payload["executive_summary"]
        return _finish(step, run, {
            "kind": "REPORT", "status": "COMPLETED",
            "agent_run_id": run.id,
        })
    except Exception as exc:
        run = run or AgentRun.query.filter_by(
            operation_id=operation.id, purpose="CEO_OPERATION_REPORT"
        ).order_by(AgentRun.id.desc()).first()
        _mark_paid_or_ambiguous(operation, step, run, exc)
        raise


def next_step(operation, idempotency_key):
    existing = OperationStep.query.filter_by(
        operation_id=operation.id, idempotency_key=idempotency_key
    ).first()
    if existing:
        if existing.status in OPEN_STEP:
            return _recover_open_step(operation)
        return _existing_result(existing)
    if operation.status != "RUNNING":
        raise ValueError("Operation is not running")
    recovered = _recover_open_step(operation)
    if recovered:
        return recovered
    meeting = Meeting.query.filter_by(operation_id=operation.id).order_by(
        Meeting.id.desc()
    ).first()
    if meeting:
        consumed = any(
            (step.result_json or {}).get("meeting_id") == meeting.id
            for step in operation.steps if step.kind == "MEETING_RESULT"
        )
        if meeting.status == "RUNNING":
            return _advance_meeting(operation, meeting, idempotency_key)
        if meeting.status in {"ENDED", "TERMINATED_BY_FOUNDER"} and not consumed:
            return _consume_meeting(operation, meeting, idempotency_key)
    hiring = HiringRequest.query.filter_by(
        operation_id=operation.id
    ).order_by(HiringRequest.id.desc()).first()
    if hiring:
        if hiring.status in {"REQUESTED", "HR_REVIEW"}:
            return _hr_step(operation, hiring, idempotency_key)
        if hiring.status == "FOUNDER_REVIEW":
            wait_for_founder(
                operation,
                f"HR recommendation for {hiring.role_needed} requires Founder review.",
            )
            raise ValueError("Founder hiring decision required")
    task = next((item for item in operation.tasks if item.status in {
        "ASSIGNED", "WORKING"
    }), None)
    if task:
        return _run_task_step(operation, idempotency_key, task)
    task = next((item for item in operation.tasks if item.status == "REVIEW"), None)
    if task:
        return _run_review_step(operation, idempotency_key, task)
    blocked = next(
        (item for item in operation.tasks if item.status == "BLOCKED"), None
    )
    if blocked:
        return _decision_step(operation, idempotency_key, blocked)
    allowed, _ = completion_guard(operation)
    if not allowed:
        return _decision_step(operation, idempotency_key)
    verification_step, _ = _current_goal_verification(operation)
    if not verification_step:
        return _goal_verification_step(operation, idempotency_key)
    verification = (verification_step.result_json or {}).get("verification") or {}
    if verification.get("overall_status") != "SATISFIED":
        return _decision_step(operation, idempotency_key)
    return _report_step(operation, idempotency_key)


def wait_for_founder(operation, reason, additional_budget=None):
    operation.status = "WAITING_FOR_FOUNDER"
    operation.waiting_reason = reason
    approved = Decimal(operation.approved_budget_twd)
    actual = actual_cost(operation)
    remaining = approved - actual
    if additional_budget is None:
        additional_budget = Decimal("0.0001")
    operation.founder_report_json = {
        "headline": "I need one decision.",
        "summary": reason,
        "next_move": "Approve additional authority, discuss, or stop.",
        "approved_twd": str(approved),
        "actual_twd": str(actual),
        "remaining_twd": str(remaining),
        "next_action": reason,
        "maximum_additional_authorization_twd": str(
            Decimal(additional_budget)
        ),
        "additional_budget_twd": str(Decimal(additional_budget)),
    }
    db.session.commit()
    return operation


def resume(operation):
    if operation.status not in {"WAITING_FOR_FOUNDER", "PAUSED"}:
        raise ValueError("Operation is not waiting or paused")
    operation.status = "RUNNING"
    operation.waiting_reason = None
    db.session.commit()
    return operation


def pause(operation):
    if operation.status != "RUNNING":
        raise ValueError("Operation is not running")
    operation.status = "PAUSED"
    db.session.commit()
    return operation


def stop(operation):
    if operation.status in TERMINAL:
        return operation
    operation.status = "TERMINATED_BY_FOUNDER"
    operation.ended_at = now()
    db.session.commit()
    return operation


def telemetry(operation):
    runs = AgentRun.query.filter_by(operation_id=operation.id)
    meetings = Meeting.query.filter_by(operation_id=operation.id)
    return {
        "goal_status": operation.status,
        "provider_calls": runs.count(),
        "input_tokens": db.session.query(func.coalesce(
            func.sum(AgentRun.input_tokens), 0
        )).filter_by(operation_id=operation.id).scalar(),
        "output_tokens": db.session.query(func.coalesce(
            func.sum(AgentRun.output_tokens), 0
        )).filter_by(operation_id=operation.id).scalar(),
        "actual_cost_twd": str(actual_cost(operation)),
        "meetings_used": meetings.count(),
        "employees_used": db.session.query(
            func.count(func.distinct(AgentRun.employee_id))
        ).filter(AgentRun.operation_id == operation.id).scalar(),
        "reviews_used": runs.filter_by(purpose="TASK_REVIEW").count(),
        "blocked_count": Task.query.filter_by(
            operation_id=operation.id, status="BLOCKED"
        ).count(),
    }


def state(operation):
    done = sum(task.status == "DONE" for task in operation.tasks)
    active_task = next((task for task in operation.tasks if task.status in {
        "ASSIGNED", "WORKING", "REVIEW", "BLOCKED"
    }), None)
    meeting = Meeting.query.filter_by(
        operation_id=operation.id, status="RUNNING"
    ).order_by(Meeting.id.desc()).first()
    return {
        "operation_id": operation.id, "status": operation.status,
        "done": done, "total": len(operation.tasks),
        "actual_cost_twd": str(actual_cost(operation)),
        "remaining_budget_twd": str(remaining_budget(operation)),
        "continue_allowed": operation.status == "RUNNING",
        "current_step": (
            f"Meeting: {meeting.title}" if meeting
            else f"{active_task.assigned_employee.name}: {active_task.title}"
            if active_task and active_task.assigned_employee
            else "CEO final synthesis"
        ),
    }
