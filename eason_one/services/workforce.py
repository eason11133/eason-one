import csv
import hashlib
import json
import re
from decimal import Decimal
from pathlib import Path

from ..extensions import db
from ..models import (
    AgentRun, Department, Employee, EmployeeModelHistory, HiringRequest,
    ModelConfig, Position, TalentTemplate, now,
)
from .company import remaining
from .costs import calculate, estimate_execution
from .execution import execute
from ..schemas import HR_ASSESSMENT_SCHEMA

TEMPLATE_FIELDS = {
    "name", "role_title", "department_hint", "mission", "responsibilities",
    "instructions", "skills", "suggested_tools", "deliverables",
    "success_metrics", "source",
}
REQUEST_STATES = {
    "REQUESTED", "HR_REVIEW", "FOUNDER_REVIEW", "APPROVED", "REJECTED",
    "HIRED", "CANCELLED",
}


def _list(value, field):
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise ValueError(f"{field} must be a list of text values")
    return value


def _source_key(payload):
    raw = "|".join((
        payload["source"], payload["name"], payload["role_title"],
        payload["mission"],
    ))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def create_talent_template(payload):
    if not isinstance(payload, dict) or not TEMPLATE_FIELDS.issubset(payload):
        raise ValueError("TalentTemplate fields are incomplete")
    for field in ("name", "role_title", "mission", "instructions", "source"):
        if not isinstance(payload[field], str) or not payload[field].strip():
            raise ValueError(f"TalentTemplate {field} is required")
    key = payload.get("source_key") or _source_key(payload)
    existing = TalentTemplate.query.filter_by(source_key=key).first()
    if existing:
        return existing
    template = TalentTemplate(
        name=payload["name"].strip(),
        role_title=payload["role_title"].strip(),
        department_hint=(payload.get("department_hint") or "").strip() or None,
        mission=payload["mission"].strip(),
        responsibilities_json=_list(payload["responsibilities"], "responsibilities"),
        instructions=payload["instructions"].strip(),
        skills_json=_list(payload["skills"], "skills"),
        suggested_tools_json=_list(payload["suggested_tools"], "suggested_tools"),
        deliverables_json=_list(payload["deliverables"], "deliverables"),
        success_metrics_json=_list(payload["success_metrics"], "success_metrics"),
        source=payload["source"].strip(),
        source_key=key,
        active_in_pool=bool(payload.get("active_in_pool", True)),
        suggested_model_class=payload.get("suggested_model_class"),
        notes=payload.get("notes"),
    )
    db.session.add(template)
    db.session.commit()
    return template


def _split(value):
    if isinstance(value, list):
        return value
    if value is None or value == "":
        return []
    return [item.strip() for item in str(value).split("|") if item.strip()]


def import_talent(path):
    path = Path(path)
    if path.suffix.lower() == ".json":
        rows = json.loads(path.read_text(encoding="utf-8-sig"))
    elif path.suffix.lower() == ".csv":
        with path.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
    else:
        raise ValueError("Talent importer supports JSON or CSV")
    if not isinstance(rows, list):
        raise ValueError("Talent source must contain a list")
    imported = []
    for row in rows:
        normalized = dict(row)
        for field in (
            "responsibilities", "skills", "suggested_tools", "deliverables",
            "success_metrics",
        ):
            normalized[field] = _split(normalized.get(field))
        imported.append(create_talent_template(normalized))
    return imported


def find_founder_talent_source():
    root = Path(__file__).resolve().parents[2]
    candidates = []
    for pattern in (
        "talent-library*.json", "talent_library*.json", "talent-pool*.json",
        "ai-employees*.json", "talent-library*.csv", "talent_library*.csv",
        "talent-pool*.csv", "ai-employees*.csv",
    ):
        candidates.extend(root.glob(pattern))
        candidates.extend((root / "data").glob(pattern) if (root / "data").exists() else [])
    return sorted(candidates)[0] if candidates else None


def import_founder_library():
    source = find_founder_talent_source()
    if source is None:
        return {
            "imported": 0,
            "source": None,
            "message": "147-source file required for import.",
        }
    items = import_talent(source)
    return {"imported": len(items), "source": str(source), "message": "Imported"}


def request_hire(*, requested_by_type, role_needed, problem, why_now,
                 responsibilities, capabilities, urgency, use_frequency,
                 requester=None, operation=None, project=None,
                 talent_template=None):
    if requested_by_type not in {"FOUNDER", "EMPLOYEE"}:
        raise ValueError("Unknown hiring requester type")
    if requested_by_type == "EMPLOYEE":
        if not requester or requester.position.level < 3:
            raise ValueError("Only management Employees may submit a hiring request")
    for value, label in (
        (role_needed, "role"), (problem, "problem"), (why_now, "why now"),
    ):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"Hiring {label} is required")
    hr=Employee.query.filter_by(slug="hr-director").first()
    assessment_budget=(assessment_authorization(hr) if
      requested_by_type=="FOUNDER" and hr and hr.current_model else None)
    request = HiringRequest(
        requested_by_type=requested_by_type,
        requester_employee_id=getattr(requester, "id", None),
        operation_id=getattr(operation, "id", None),
        project_id=getattr(project, "id", None),
        talent_template_id=getattr(talent_template, "id", None),
        role_needed=role_needed.strip(),
        problem=problem.strip(),
        why_now=why_now.strip(),
        responsibilities_json=_list(responsibilities, "responsibilities"),
        capabilities_json=_list(capabilities, "capabilities"),
        urgency=urgency,
        use_frequency=use_frequency,
        status="REQUESTED",
        assessment_budget_twd=assessment_budget,
    )
    db.session.add(request)
    db.session.commit()
    return request


def estimate_mission_cost(model, input_tokens, output_tokens, calls):
    return calculate(
        model, int(input_tokens) * int(calls), int(output_tokens) * int(calls)
    )


def assessment_authorization(hr):
    if not hr or not hr.current_model:
        return None
    context="Bounded HR assessment context including workforce, candidates, models, and budgets."
    estimate=estimate_execution(
        hr.current_model,hr.system_instructions,context,
        "Assess one governed HiringRequest",hr.current_model.max_output_tokens,
        HR_ASSESSMENT_SCHEMA)
    return estimate.real_cost


def _assessment_context(request):
    operation=__import__("eason_one.models",fromlist=["Operation"]).Operation.query.get(
      request.operation_id) if request.operation_id else None
    lines=[
      f"HIRING REQUEST #{request.id}",
      f"Role needed: {request.role_needed}",
      f"Problem: {request.problem}",
      f"Why now: {request.why_now}",
      f"Responsibilities: {json.dumps(request.responsibilities_json,ensure_ascii=False)}",
      f"Capabilities: {json.dumps(request.capabilities_json,ensure_ascii=False)}",
      f"Urgency: {request.urgency}; use frequency: {request.use_frequency}",
      f"Requester: {request.requester.name if request.requester else 'Founder'}",
      f"Company remaining budget: TWD {remaining()}",
    ]
    if operation:
        from .operations import remaining_budget
        lines.append(f"Operation #{operation.id}: {operation.title}; objective: {operation.objective}; remaining TWD {remaining_budget(operation)}")
    lines.append("ACTIVE WORKFORCE")
    for employee in Employee.query.filter_by(active=True).order_by(Employee.id):
        lines.append(f"EMPLOYEE #{employee.id}: {employee.name}; {employee.position.name}; department {employee.department.name if employee.department else 'CEO Office'}; manager {employee.manager.name if employee.manager else 'Founder'}")
    lines.append("DEPARTMENTS")
    for department in Department.query.filter_by(active=True).order_by(Department.id):
        lines.append(f"DEPARTMENT #{department.id}: {department.name}")
    lines.append("TALENT POOL")
    for candidate in TalentTemplate.query.filter_by(active_in_pool=True).order_by(TalentTemplate.id).limit(20):
        lines.append(f"CANDIDATE #{candidate.id}: {candidate.role_title}; {candidate.department_hint or '-'}; skills {candidate.skills_json}")
    lines.append("AVAILABLE MODELS")
    for model in ModelConfig.query.filter_by(active=True,archived=False).order_by(ModelConfig.id):
        lines.append(f"MODEL #{model.id}: {model.label}; input {model.input_price_per_million}; output {model.output_price_per_million}; max output {model.max_output_tokens}")
    return "\n".join(lines)


def assess_request(request):
    if request.status not in {"REQUESTED","HR_REVIEW"}:
        if request.status=="FOUNDER_REVIEW" and request.hr_agent_run_id:
            return db.session.get(AgentRun,request.hr_agent_run_id)
        raise ValueError("Hiring request is not ready for HR assessment")
    hr=Employee.query.filter_by(slug="hr-director").one()
    if not hr.current_model:
        raise ValueError("HR Director has no ModelConfig; Founder assignment is required")
    operation=__import__("eason_one.models",fromlist=["Operation"]).Operation.query.get(
      request.operation_id) if request.operation_id else None
    context=_assessment_context(request)
    if not operation:
        maximum=assessment_authorization(hr)
        if request.assessment_budget_twd is None or Decimal(request.assessment_budget_twd)<Decimal(maximum):
            raise ValueError("Founder HR assessment authorization is insufficient")
    request.status="HR_REVIEW"; db.session.commit()
    run=execute(hr,"HR_ASSESSMENT",f"Assess HiringRequest #{request.id}",
      project=__import__("eason_one.models",fromlist=["Project"]).Project.query.get(request.project_id) if request.project_id else None,
      operation=operation,context_override=context,
      system_prompt_override=hr.system_instructions+"\nHR_ASSESSMENT\nReturn one strict workforce recommendation. Application code computes all currency arithmetic.",
      response_schema=HR_ASSESSMENT_SCHEMA)
    if run.status!="SUCCEEDED":
        raise ValueError(run.error_text or "HR assessment failed")
    payload=json.loads(run.raw_output)
    department=db.session.get(Department,payload["target_department_id"])
    manager=db.session.get(Employee,payload["manager_employee_id"])
    model=db.session.get(ModelConfig,payload["recommended_model_config_id"])
    if not department or not department.active: raise ValueError("HR proposed unknown Department")
    if not manager or not manager.active or manager.position.level<3: raise ValueError("HR proposed invalid manager")
    if manager.department_id!=department.id and manager.slug!="ceo": raise ValueError("HR manager does not belong to target Department")
    if not model or not model.active or model.archived: raise ValueError("HR proposed unavailable ModelConfig")
    mission=estimate_mission_cost(model,payload["estimated_input_tokens"],
      payload["estimated_output_tokens"],payload["expected_calls_per_mission"])
    monthly=mission*int(payload["missions_per_month"])
    assessment=dict(payload)
    assessment["financial_math"]={
      "estimated_input_cost_twd":str(calculate(model,payload["estimated_input_tokens"]*payload["expected_calls_per_mission"],0)),
      "estimated_output_cost_twd":str(calculate(model,0,payload["estimated_output_tokens"]*payload["expected_calls_per_mission"])),
      "estimated_mission_cost_twd":str(mission),
      "estimated_monthly_cost_twd":str(monthly),
      "maximum_proposed_exposure_twd":str(Decimal(str(payload["max_mission_budget_twd"]))),
      "company_budget_remaining_twd":str(remaining())}
    assessment["resource_envelope"]={
      "max_mission_budget_twd":str(Decimal(str(payload["max_mission_budget_twd"]))),
      "expected_calls_per_mission":payload["expected_calls_per_mission"],
      "missions_per_month":payload["missions_per_month"]}
    request.hr_assessment_json=assessment
    request.hr_agent_run_id=run.id
    request.target_department_id=department.id
    request.target_position=payload["target_position"]
    request.manager_employee_id=manager.id
    request.recommended_model_config_id=model.id
    request.resource_envelope_json=assessment["resource_envelope"]
    request.status="FOUNDER_REVIEW"
    run.parsed_output_json=payload
    db.session.commit()
    return run


def review_request(request, *, existing_staff_alternative, recommendation,
                   recommended_model, estimated_input_tokens,
                   estimated_output_tokens, expected_calls,
                   max_mission_budget_twd, expected_benefit,
                   redundancy_risk, alternatives, success_criteria,
                   probation_assignments):
    if request.status not in {"REQUESTED", "HR_REVIEW"}:
        raise ValueError("Hiring request is not open for HR review")
    if recommendation not in {"HIRE", "DO NOT HIRE", "TEMPORARY", "USE EXISTING STAFF"}:
        raise ValueError("Invalid HR recommendation")
    if recommended_model and (not recommended_model.active or recommended_model.archived):
        raise ValueError("HR may recommend only an active ModelConfig")
    cost = (
        estimate_mission_cost(
            recommended_model, estimated_input_tokens, estimated_output_tokens,
            expected_calls,
        )
        if recommended_model else Decimal("0")
    )
    assessment = {
        "existing_staff_alternative": existing_staff_alternative,
        "need_duration": request.use_frequency,
        "overlap_review": "Compared with active workforce and Talent Pool.",
        "recommendation": recommendation,
        "estimated_input_tokens": int(estimated_input_tokens),
        "estimated_output_tokens": int(estimated_output_tokens),
        "expected_calls": int(expected_calls),
        "estimated_cost_per_mission_twd": str(cost),
        "max_mission_budget_twd": str(Decimal(max_mission_budget_twd)),
        "company_budget_remaining_twd": str(remaining()),
        "expected_benefit": expected_benefit,
        "redundancy_risk": redundancy_risk,
        "alternatives": _list(alternatives, "alternatives"),
        "success_criteria": _list(success_criteria, "success criteria"),
        "probation_assignments": int(probation_assignments),
    }
    request.hr_assessment_json = assessment
    request.recommended_model_config_id = getattr(recommended_model, "id", None)
    request.status = "FOUNDER_REVIEW"
    department=_department(request.talent_template.department_hint if request.talent_template else "Human Resources")
    request.target_department_id=department.id
    request.target_position=request.talent_template.role_title if request.talent_template else request.role_needed
    manager=(Employee.query.filter_by(department_id=department.id)
      .join(Position).filter(Position.level>=3).order_by(Position.level.desc()).first()
      or Employee.query.filter_by(slug="hr-director").one())
    request.manager_employee_id=manager.id
    request.resource_envelope_json={"max_mission_budget_twd":str(Decimal(max_mission_budget_twd))}
    db.session.commit()
    return request


def _slug(text):
    base = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "employee"
    slug = base
    index = 2
    while Employee.query.filter_by(slug=slug).first():
        slug = f"{base}-{index}"
        index += 1
    return slug


def _department(hint):
    if hint:
        exact = Department.query.filter(
            db.func.lower(Department.name) == hint.lower()
        ).first()
        if exact:
            return exact
        partial = Department.query.filter(
            db.func.lower(Department.name).like(f"%{hint.lower()}%")
        ).first()
        if partial:
            return partial
    return Department.query.filter_by(name="Human Resources").one()


def approve_hire(request):
    if request.status == "HIRED" and request.created_employee:
        return request.created_employee
    if request.status != "FOUNDER_REVIEW" or not request.hr_assessment_json:
        raise ValueError("HR review is required before Founder approval")
    template = request.talent_template
    role = request.target_position or (template.role_title if template else request.role_needed)
    position = Position.query.filter_by(name=role).first()
    if not position:
        position = Position(name=role, level=1, description=request.problem)
        db.session.add(position)
        db.session.flush()
    department=db.session.get(Department,request.target_department_id)
    manager=db.session.get(Employee,request.manager_employee_id)
    if not department or not manager: raise ValueError("Approved organizational placement is incomplete")
    if manager.department_id!=department.id and manager.slug!="ceo": raise ValueError("Approved manager is outside target Department")
    instructions=(request.hr_assessment_json or {}).get("instructions") or (
      template.instructions if template else "Perform the approved role within Founder-authorized operations.")
    employee = Employee(
        name=template.name if template else role,
        slug=_slug(template.name if template else role),
        department_id=department.id,
        position_id=position.id,
        manager_id=manager.id,
        role_description=template.mission if template else request.problem,
        system_instructions=instructions,
        current_model_config_id=request.recommended_model_config_id,
        salary_credits_per_week=0,
        active=True,
        employment_status="PROBATION",
        probation_target_assignments=int(
            request.hr_assessment_json.get("probation_assignments", 3)
        ),
        hiring_request_id=request.id,
    )
    db.session.add(employee)
    db.session.flush()
    if employee.current_model_config_id:
        db.session.add(EmployeeModelHistory(
            employee_id=employee.id,
            model_config_id=employee.current_model_config_id,
            reason=f"Founder-approved hire request #{request.id}",
        ))
    request.status = "HIRED"
    request.founder_decision = "APPROVED"
    request.founder_decision_note = "Founder approved governed hire"
    request.founder_decided_at = now()
    request.created_employee_id = employee.id
    if request.operation_id:
        operation=__import__("eason_one.models",fromlist=["Operation"]).Operation.query.get(request.operation_id)
        if operation and operation.status=="WAITING_FOR_FOUNDER":
            operation.status="RUNNING"; operation.waiting_reason=None
    db.session.commit()
    return employee


def reject_request(request, note):
    if request.status == "HIRED":
        raise ValueError("A completed hire cannot be rejected")
    request.status = "REJECTED"
    request.founder_decision = "REJECTED"
    request.founder_decision_note = note
    request.founder_decided_at = now()
    db.session.commit()
    return request
