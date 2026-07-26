import json
from decimal import Decimal

from sqlalchemy import func

from ..extensions import db
from ..models import AgentRun, CostEvent, HiringRequest, WorkMessage
from .brain import current
from .company import remaining

OPERATION_CONTEXT_BUDGET = 4000


def _bounded(text, budget):
    if len(text) <= budget:
        return text
    return text[:budget - 1] + "…"


def _latest_review(task):
    if not task or not task.operation_id:
        return None
    exact = AgentRun.query.filter_by(
        operation_id=task.operation_id, task_id=task.id,
        purpose="TASK_REVIEW", status="SUCCEEDED",
    ).order_by(AgentRun.id.desc()).first()
    if exact:
        return exact
    return AgentRun.query.filter_by(
        operation_id=task.operation_id, purpose="TASK_REVIEW",
        status="SUCCEEDED",
    ).order_by(AgentRun.id.desc()).first()


def operation_context(task):
    operation = getattr(task, "operation", None)
    if not operation:
        return "", {"characters": 0, "budget": OPERATION_CONTEXT_BUDGET}
    actual = Decimal(db.session.query(func.coalesce(
        func.sum(CostEvent.real_cost_delta), 0
    )).filter_by(operation_id=operation.id).scalar())
    lines = [
        "OPERATION CONTEXT",
        f"Operation #{operation.id}: {operation.title}",
        f"Objective: {operation.objective}",
        f"State: {operation.status}",
        (
            f"Budget: approved TWD {operation.approved_budget_twd}; "
            f"actual TWD {actual}; remaining TWD "
            f"{Decimal(operation.approved_budget_twd) - actual}"
        ),
    ]
    memory = operation.memory_json or {}
    for decision in (memory.get("decisions") or [])[-4:]:
        lines.extend([
            "CEO DECISION",
            f"Action: {decision.get('action', '-')}",
            f"Reason: {decision.get('reason', '-')}",
        ])
    for item in (memory.get("meeting_results") or [])[-3:]:
        result = item.get("result") or {}
        lines.extend([
            f"MEETING RESULT #{item.get('meeting_id', '-')}",
            f"Decision / position: {result.get('position') or '-'}",
            (
                "Qualification / disagreement: "
                f"{result.get('qualification_or_disagreement') or '-'}"
            ),
            f"Risk: {result.get('risk') or '-'}",
            "Actions: " + json.dumps(
                result.get("actions") or [], ensure_ascii=False
            ),
            "Controls: " + json.dumps(
                result.get("controls") or [], ensure_ascii=False
            ),
        ])
    review = _latest_review(task)
    payload = (review.parsed_output_json or {}) if review else {}
    if payload:
        lines.extend([
            f"LATEST REVIEW FOR TASK #{review.task_id}",
            f"Decision: {payload.get('decision') or '-'}",
            f"Summary: {payload.get('summary') or '-'}",
            "Issues:\n" + "\n".join(
                f"- {value}" for value in payload.get("issues") or []
            ),
            "Required changes:\n" + "\n".join(
                f"- {value}" for value in payload.get("required_changes") or []
            ),
        ])
    audit_messages = WorkMessage.query.filter(
        WorkMessage.project_id == operation.project_id,
        WorkMessage.message_type.in_([
            "CEO_REMEDIATION", "CEO_REASSIGNMENT",
        ]),
    ).order_by(WorkMessage.id.desc()).limit(5).all()
    if audit_messages:
        lines.append("REMEDIATION INSTRUCTIONS")
        lines.extend(f"- {item.content}" for item in reversed(audit_messages))
    hires = HiringRequest.query.filter_by(
        operation_id=operation.id, status="HIRED"
    ).order_by(HiringRequest.id.desc()).limit(4).all()
    for request in reversed(hires):
        employee = request.created_employee
        if not employee:
            continue
        lines.extend([
            f"WORKFORCE CHANGE — HIRING REQUEST #{request.id}",
            (
                f"Available Employee #{employee.id}: {employee.name}; "
                f"Department {employee.department.name}; "
                f"Position {employee.position.name}; "
                f"Manager {employee.manager.name if employee.manager else 'Founder'}; "
                f"Role {employee.role_description}; "
                f"Model {employee.current_model.label if employee.current_model else '-'}"
            ),
        ])
    text = _bounded("\n".join(lines), OPERATION_CONTEXT_BUDGET)
    return text, {
        "characters": len(text),
        "budget": OPERATION_CONTEXT_BUDGET,
        "latest_review_run_id": getattr(review, "id", None),
        "meeting_results": min(len(memory.get("meeting_results") or []), 3),
        "hiring_changes": len(hires),
    }


def build_with_composition(employee, project=None, task=None):
    parts = [
        f"EMPLOYEE\n{employee.name} — {employee.role_description}",
        (
            "Department: "
            f"{employee.department.name if employee.department else 'CEO Office'}; "
            "Manager: "
            f"{employee.manager.name if employee.manager else 'Founder'}"
        ),
    ]
    if project:
        parts.append(
            f"PROJECT\n{project.name}\nObjective: {project.objective}\n"
            f"Status: {project.status}; Priority: {project.priority}"
        )
    if task:
        parts.append(
            f"TASK\n{task.title}\nObjective: {task.objective}\n"
            f"Required output: {task.required_output or '-'}\n"
            f"Acceptance: {task.acceptance_criteria or '-'}"
        )
        messages = WorkMessage.query.filter_by(task_id=task.id).order_by(
            WorkMessage.created_at.desc()
        ).limit(5).all()
        if messages:
            parts.append(
                "WORK CONTEXT\n" +
                "\n".join(item.content for item in reversed(messages))
            )
        operation_text, operation_meta = operation_context(task)
        if operation_text:
            parts.append(operation_text)
    else:
        operation_meta = {
            "characters": 0, "budget": OPERATION_CONTEXT_BUDGET
        }
    brain = current(project.id if project else None)
    normal = [item for item in brain if item.kind != "KILLED"]
    killed = [item for item in brain if item.kind == "KILLED"]
    if normal:
        parts.append(
            "CURRENT COMPANY BRAIN\n" + "\n".join(
                f"{item.kind}: {item.title} — {item.content}" +
                (
                    f" [corrects #{item.target_knowledge_id}]"
                    if item.kind == "CORRECTION" else ""
                )
                for item in normal[-12:]
            )
        )
    if killed:
        parts.append(
            "KILLED IDEAS — DO NOT RESURRECT WITHOUT MEANINGFUL NEW EVIDENCE\n"
            + "\n".join(
                f"{item.title} — {item.content} "
                f"[kills hypothesis #{item.target_knowledge_id}]"
                for item in killed[-8:]
            )
        )
    parts.append(
        f"COST\nRemaining company real budget: NT${remaining():,.2f}"
    )
    return "\n\n".join(parts), {"operation_context": operation_meta}


def build(employee, project=None, task=None):
    return build_with_composition(employee, project, task)[0]
