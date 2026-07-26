from dataclasses import dataclass
import json
from decimal import Decimal

from sqlalchemy import func

from ..extensions import db
from ..models import (
    AgentRun, CostEvent, Employee, HiringRequest, Meeting, Operation, Project,
    Task,
)
from .brain import current
from .company import remaining as company_remaining
from .meetings import result_view


@dataclass(frozen=True)
class ComposedCEOContext:
    text: str
    composition: dict
    character_budget: int


SECTION_BUDGETS = {
    "working_memory": 3200,
    "operation": 3400,
    "project": 2800,
    "meetings": 3000,
    "brain": 2200,
    "workforce": 2000,
}
TOTAL_BUDGET = sum(SECTION_BUDGETS.values())


def _bounded(lines, budget):
    output = []
    used = 0
    for line in lines:
        text = str(line).strip()
        if not text:
            continue
        addition = len(text) + (2 if output else 0)
        if used + addition > budget:
            available = budget - used
            if available > 32:
                output.append(text[:available - 1] + "…")
            break
        output.append(text)
        used += addition
    return "\n\n".join(output)


def _working_memory():
    runs = AgentRun.query.filter_by(
        purpose="CEO_FOUNDER_REQUEST", status="SUCCEEDED"
    ).order_by(AgentRun.started_at.desc()).limit(6).all()
    lines = []
    for run in reversed(runs):
        response = (run.parsed_output_json or {}).get("executive_response")
        lines.append(f"Founder: {run.user_request}")
        if response:
            lines.append(f"CEO: {response}")
    return lines, len(runs)


def _operation_memory(operation):
    if not operation:
        return [], 0
    cost = Decimal(db.session.query(
        func.coalesce(func.sum(CostEvent.real_cost_delta), 0)
    ).filter_by(operation_id=operation.id).scalar())
    lines = [
        f"Operation #{operation.id}: {operation.title}",
        f"Objective: {operation.objective}",
        f"State: {operation.status}",
        f"Budget: approved TWD {operation.approved_budget_twd}; actual TWD {cost}; remaining TWD {Decimal(operation.approved_budget_twd)-cost}",
        "Tasks:\n" + "\n".join(
            f"- #{task.id} {task.status} {task.title}; assignee {task.assigned_employee.name if task.assigned_employee else '-'}; "
            f"reviewer {task.reviewer.name if task.reviewer else '-'}; result {task.result_summary or '-'}"
            for task in operation.tasks
        ),
    ]
    if operation.memory_json:
        lines.append("Persisted operation outcomes: " + json.dumps(
            operation.memory_json, ensure_ascii=False
        ))
    if operation.waiting_reason:
        lines.append("Waiting reason: " + operation.waiting_reason)
    return lines, len(operation.tasks)


def _project_memory(project):
    if not project:
        return [], 0
    tasks = Task.query.filter_by(project_id=project.id).order_by(Task.id.desc()).limit(10)
    lines = [
        f"Project #{project.id}: {project.name}",
        f"Objective: {project.objective}",
        f"Status: {project.status}",
        f"State: {project.current_state_summary or '-'}",
        f"Constraints: {project.known_constraints or '-'}",
        f"Next milestone: {project.next_milestone or '-'}",
    ]
    for task in tasks:
        if task.result_summary:
            lines.append(
                f"Task result #{task.id} {task.title}: {task.result_summary}"
            )
    return lines, len(lines) - 6


def _meeting_memory(operation, project):
    query = Meeting.query.filter(
        Meeting.status.in_(["ENDED", "TERMINATED_BY_FOUNDER"])
    )
    if operation:
        query = query.filter(Meeting.operation_id == operation.id)
    elif project:
        query = query.filter(Meeting.project_id == project.id)
    meetings = query.order_by(Meeting.ended_at.desc()).limit(4).all()
    lines = []
    for meeting in meetings:
        result = result_view(meeting)
        if not result:
            continue
        lines.append(
            f"Meeting #{meeting.id} {meeting.title}\n"
            f"Position: {result.get('position') or '-'}\n"
            f"Qualification / disagreement: {result.get('qualification_or_disagreement') or '-'}\n"
            f"Risk: {result.get('risk') or '-'}\n"
            f"Actions: {json.dumps(result.get('actions') or [], ensure_ascii=False)}\n"
            f"Controls: {json.dumps(result.get('controls') or [], ensure_ascii=False)}"
        )
    return lines, len(lines)


def _brain_memory(project):
    items = current(getattr(project, "id", None))
    items = sorted(items, key=lambda item: item.id, reverse=True)[:12]
    return [
        f"{item.kind} #{item.id} {item.title}: {item.content}"
        for item in reversed(items)
    ], len(items)


def _workforce_memory(operation, project):
    employees = Employee.query.filter_by(active=True).order_by(Employee.id).all()
    requests = HiringRequest.query.filter(HiringRequest.status.in_(
        ["REQUESTED", "HR_REVIEW", "FOUNDER_REVIEW", "HIRED"]
    ))
    if operation:
        requests = requests.filter(
            (HiringRequest.operation_id == operation.id)
            | (HiringRequest.operation_id.is_(None))
        )
    rows = [
        "Active workforce:\n" + "\n".join(
            f"- {employee.name}; ID: {employee.id}; slug: {employee.slug}; {employee.position.name}; "
            f"{employee.department.name if employee.department else 'CEO Office'}; "
            f"model {employee.current_model.label if employee.current_model else 'UNASSIGNED'}"
            for employee in employees
        ),
        f"Company budget remaining: TWD {company_remaining()}",
    ]
    open_requests = requests.order_by(HiringRequest.updated_at.desc()).limit(8).all()
    for request in open_requests:
        hired = request.created_employee
        placement = ""
        if hired:
            placement = (
                f"; available Employee #{hired.id} {hired.name}; "
                f"Department {hired.department.name}; "
                f"Position {hired.position.name}; "
                f"Manager {hired.manager.name if hired.manager else 'Founder'}; "
                f"role {hired.role_description}; "
                f"model {hired.current_model.label if hired.current_model else '-'}"
            )
        rows.append(
            f"Hiring request #{request.id}: {request.status}; {request.role_needed}; "
            f"problem {request.problem}; recommendation "
            f"{(request.hr_assessment_json or {}).get('recommendation', '-')}"
            f"{placement}"
        )
    return rows, len(open_requests)


def compose(ceo, founder_request=None, operation=None, project=None):
    if operation and not project:
        project = operation.project
    sections = {}
    metadata = {}
    producers = {
        "working_memory": _working_memory,
        "operation": lambda: _operation_memory(operation),
        "project": lambda: _project_memory(project),
        "meetings": lambda: _meeting_memory(operation, project),
        "brain": lambda: _brain_memory(project),
        "workforce": lambda: _workforce_memory(operation, project),
    }
    for name, producer in producers.items():
        lines, included = producer()
        text = _bounded(lines, SECTION_BUDGETS[name])
        sections[name] = text
        metadata[name] = {
            "included": included,
            "characters": len(text),
            "budget": SECTION_BUDGETS[name],
        }
    prefix = (
        f"CURRENT FOUNDER REQUEST\n{founder_request}\n\n"
        if founder_request else ""
    )
    body = "\n\n".join(
        f"{name.replace('_', ' ').upper()}\n{text}"
        for name, text in sections.items() if text
    )
    text = (prefix + body)[:TOTAL_BUDGET]
    return ComposedCEOContext(
        text=text,
        composition=metadata,
        character_budget=TOTAL_BUDGET,
    )
