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
from .stabilization import REAL_WORK, operation_kind


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
    "ceo_policy": 2600,
    "ceo_history": 2600,
    "company_learning": 3200,
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


def _working_memory(founder_request=None, operation=None, project=None):
    runs = AgentRun.query.filter_by(
        purpose="CEO_FOUNDER_REQUEST", status="SUCCEEDED"
    ).order_by(AgentRun.started_at.desc()).limit(6).all()
    request_terms={
        word for word in (founder_request or "").lower().split()
        if len(word)>=5
    }
    lines = []
    for run in reversed(runs):
        if run.operation_id:
            run_operation=db.session.get(Operation,run.operation_id)
            if run_operation and operation_kind(run_operation)!=REAL_WORK:
                continue
        if run.project_id:
            run_project=db.session.get(Project,run.project_id)
            if run_project and run_project.environment!="LIVE":
                continue
        scoped_relevant=(
          (operation and run.operation_id==operation.id)
          or (project and run.project_id==project.id)
        )
        if request_terms and not scoped_relevant and not request_terms.intersection(
          (run.user_request or "").lower().split()
        ):
            continue
        response = (run.parsed_output_json or {}).get("executive_response")
        lines.append(f"Founder: {run.user_request}")
        if response:
            lines.append(f"CEO: {response}")
    return lines, len(runs)


def _operation_memory(operation):
    if not operation or operation_kind(operation)!=REAL_WORK:
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
        memory = operation.memory_json
        verification = memory.get("goal_verification") or {}
        if verification:
            lines.append(
                "LATEST GOAL VERIFICATION\n"
                f"Overall: {verification.get('overall_status', '-')}\n"
                f"Summary: {verification.get('summary', '-')}\n"
                f"Recommended remediation: "
                f"{verification.get('recommended_action', '-')}\n"
                "Criteria:\n" + "\n".join(
                    f"- {item.get('status', '-')}: "
                    f"{item.get('criterion', '-')}; "
                    f"reason {item.get('reason', '-')}"
                    for item in verification.get("criteria") or []
                )
            )
        outcomes = {
            key: value for key, value in memory.items()
            if key != "goal_verification"
        }
        if outcomes:
            lines.append("Persisted operation outcomes: " + json.dumps(
                outcomes, ensure_ascii=False
            ))
    if operation.waiting_reason:
        lines.append("Waiting reason: " + operation.waiting_reason)
    return lines, len(operation.tasks)


def _project_memory(project):
    if not project:
        return [], 0
    tasks = Task.query.filter_by(project_id=project.id).order_by(Task.id.desc()).limit(10)
    terms = __import__(
        "eason_one.services.project_contract", fromlist=["governing_terms"]
    ).governing_terms(project)
    lines = [
        f"Project #{project.id}: {project.name}",
        f"Objective: {terms.get('objective') or '-'}",
        f"Status: {project.status}",
        f"State: {project.current_state_summary or '-'}",
        f"Constraints: {', '.join(terms.get('constraints') or []) or '-'}",
        f"Deadline: {terms.get('deadline') or '-'}",
        f"Project budget authority: TWD {terms.get('budget_limit_twd') or '-'}",
        f"Next milestone: {project.next_milestone or '-'}",
    ]
    for task in tasks:
        if task.result_summary:
            lines.append(
                f"Task result #{task.id} {task.title}: {task.result_summary}"
            )
    return lines, max(0, len(lines) - 8)


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
        ["REQUESTED", "HR_REVIEW", "FOUNDER_REVIEW", "SYSTEM_RECOVERY", "HIRED"]
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



def _company_learning_memory(project=None):
    """Bounded cross-Project learning that is safe for CEO planning to consume.

    Only validated, canonical accepted-Work experience is included.  This
    context can inform decomposition, staffing expectations and review posture,
    but it never grants capability, expands budget authority, or overrides a
    Founder-approved Project Contract.
    """
    evolution = __import__(
        "eason_one.services.employee_evolution", fromlist=["employee_profile"]
    )
    market = __import__(
        "eason_one.services.market", fromlist=["employee_market_profile", "snapshot"]
    )
    employees = Employee.query.filter_by(active=True).order_by(Employee.id).all()
    lines = [
        "COMPANY LEARNING POLICY: use only validated canonical Work outcomes as experience. "
        "Learning may influence planning/staffing/review posture, but cannot mint capability, "
        "change Founder authority, or bypass verification."
    ]
    evidence_count = 0
    for employee in employees:
        if employee.slug == "ceo":
            continue
        profile = evolution.employee_profile(employee)
        proven = [row for row in profile["capabilities"] if row["accepted_evidence"]]
        if not proven:
            continue
        evidence_count += int(profile["canonical_record_count"] or 0)
        capability_bits = []
        for row in proven[:3]:
            capability_bits.append(
                f"{row['capability']}: {row['owner_acceptances']} owner acceptance(s), "
                f"{row['review_acceptances']} review acceptance(s), recovery burden {row['recovery_burden']}, "
                f"depth {row['depth']}"
            )
        economy = market.employee_market_profile(employee)
        lines.append(
            f"Employee #{employee.id} {employee.name} ({employee.position.name if employee.position else employee.slug}): "
            + "; ".join(capability_bits)
            + f". Internal market: {economy['earned']} EC earned, {economy['settled_contracts']} settled contract(s)."
        )
    if evidence_count == 0:
        lines.append("No validated canonical accepted-Work learning is available yet; do not invent prior competence.")
    else:
        lines.append(
            f"Validated canonical outcome evidence available: {evidence_count} record(s). "
            "Prefer proven Employees among already-authorized capability matches, and increase scrutiny when recovery burden is high."
        )
    return lines, evidence_count


def compose(ceo, founder_request=None, operation=None, project=None):
    if operation and operation_kind(operation)!=REAL_WORK:
        operation=None
    if operation and not project:
        project = operation.project
    sections = {}
    metadata = {}
    producers = {
        "working_memory": lambda: _working_memory(
            founder_request, operation, project
        ),
        "operation": lambda: _operation_memory(operation),
        "project": lambda: _project_memory(project),
        "meetings": lambda: _meeting_memory(operation, project),
        "brain": lambda: _brain_memory(project),
        "workforce": lambda: _workforce_memory(operation, project),
        "ceo_policy": lambda: (lambda result: ([result[0]], len(result[1].get("validated_learning_ids", []))))(
            __import__("eason_one.services.ceo_learning", fromlist=["active_policy_context"]).active_policy_context(ceo)
        ),
        "ceo_history": lambda: (lambda result: ([result[0]] if result[0] else [], len(result[1].get("episode_keys", []))))(
            __import__("eason_one.services.ceo_learning", fromlist=["similar_episode_context"]).similar_episode_context(founder_request)
        ),
        "company_learning": lambda: _company_learning_memory(project),
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
