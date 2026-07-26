import json
from decimal import Decimal

import pytest

from eason_one.extensions import db
from eason_one.models import (
    AgentRun, CostEvent, Employee, HiringRequest, Meeting, ModelConfig,
    Operation, OperationStep, Project, TalentTemplate, Task,
)
from eason_one.schemas import CEO_SCHEMA
from eason_one.services import operations, workforce
from eason_one.services.company import get_company


def _plan(employees, budget="2.00"):
    ceo = next(e for e in employees if e.slug == "ceo")
    worker = next(e for e in employees if e.slug == "researcher")
    reviewer = next(e for e in employees if e.slug == "research-director")
    return {
        "mode": "OPERATION_PLAN",
        "executive_response": "I prepared one bounded internal operation.",
        "operation": {
            "title": "Qualified prospect preparation",
            "objective": "Prepare a reviewable qualified-prospect test.",
            "project_id": None,
            "budget_twd": budget,
            "tasks": [{
                "title": "Prepare prospect test",
                "objective": "Define the smallest qualified-prospect test.",
                "assignee_employee_id": worker.id,
                "reviewer_employee_id": reviewer.id,
                "acceptance_criteria": ["Scope is bounded", "Risk is explicit"],
            }],
            "meeting_policy": "Only for a material unresolved conflict.",
            "completion_criteria": ["Task accepted", "CEO report persisted"],
        },
    }, ceo


def _operation(ctx, budget="2.00"):
    plan, ceo = _plan(Employee.query.all(), budget)
    return operations.propose_operation(ceo, plan)


def test_command_is_ceo_office_and_get_is_zero_call(client, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "eason_one.services.execution.get_provider",
        lambda *a, **k: calls.append((a, k)),
    )
    page = client.get("/command").get_data(as_text=True)
    assert 'class="ceo-office"' in page
    assert 'class="ceo-core"' in page
    assert "CEO REPORT" in page
    assert "Recent conversation" in page
    for forbidden in (
        "Active Company", "Company activity pulse", "CEO Brief",
        'class="command-lower"', 'class="executive-brief"',
    ):
        assert forbidden not in page
    assert calls == []


def test_deterministic_ceo_report_surfaces_completion_and_attention(client, ctx):
    operation = _operation(ctx)
    operation.status = "COMPLETED"
    operation.founder_report_json = {
        "headline": "Prospect preparation is complete.",
        "summary": "The bounded result is ready.",
        "next_move": "Review the result.",
    }
    db.session.commit()
    assert "Prospect preparation is complete." in client.get(
        "/command"
    ).get_data(as_text=True)
    operation.status = "WAITING_FOR_FOUNDER"
    operation.founder_report_json = {
        "headline": "I need one decision.",
        "summary": "Additional authorization is required.",
        "next_move": "Approve or stop.",
    }
    db.session.commit()
    assert "I need one decision." in client.get(
        "/command"
    ).get_data(as_text=True)


def test_operation_schema_and_plan_validation(ctx):
    assert "OPERATION_PLAN" in (
        CEO_SCHEMA["schema"]["properties"]["mode"]["enum"]
    )
    plan, _ = _plan(Employee.query.all())
    assert operations.validate_plan(plan) == plan


def test_operation_waits_for_founder_then_materializes_once(ctx):
    operation = _operation(ctx)
    assert operation.status == "PLANNED"
    assert operation.tasks == []
    assert AgentRun.query.count() == 0
    approved = operations.approve(operation)
    assert approved.status == "RUNNING"
    assert approved.approved_budget_twd == Decimal("2.0000")
    assert len(approved.tasks) == 1
    assert approved.tasks[0].status == "ASSIGNED"
    task_id = approved.tasks[0].id
    assert operations.approve(operation).tasks[0].id == task_id


def test_browser_loop_is_one_call_deterministic_and_idempotent(ctx):
    operation = operations.approve(_operation(ctx))
    first = operations.next_step(operation, "browser-1")
    assert first["kind"] == "TASK"
    assert AgentRun.query.count() == 1
    assert operation.tasks[0].status == "REVIEW"
    repeated = operations.next_step(operation, "browser-1")
    assert repeated == first
    assert AgentRun.query.count() == 1
    second = operations.next_step(operation, "browser-2")
    assert second["kind"] == "REVIEW"
    assert AgentRun.query.count() == 2
    assert operation.tasks[0].status == "DONE"


def test_operation_budget_company_budget_and_stop_are_hard_gates(ctx):
    operation = operations.approve(_operation(ctx, "0.10"))
    with pytest.raises(ValueError, match="operation budget"):
        operations.ensure_budget(operation, Decimal("0.11"))
    company = get_company()
    company.real_budget_limit = Decimal("0")
    db.session.commit()
    with pytest.raises(ValueError, match="Company real budget"):
        operations.ensure_budget(operation, Decimal("0.01"))
    company.real_budget_limit = Decimal("3000")
    db.session.commit()
    operations.stop(operation)
    before = AgentRun.query.count()
    with pytest.raises(ValueError, match="not running"):
        operations.next_step(operation, "after-stop")
    assert AgentRun.query.count() == before


def test_completed_and_waiting_operations_persist_founder_reports(ctx):
    operation = operations.approve(_operation(ctx))
    operation.tasks[0].status = "DONE"
    db.session.commit()
    result = operations.next_step(operation, "final-report")
    assert result["kind"] == "REPORT"
    assert operation.status == "COMPLETED"
    assert operation.founder_report_json["summary"]
    waiting = operations.approve(_operation(ctx, "0.01"))
    operations.wait_for_founder(
        waiting, "Additional authorization required.", Decimal("0.25")
    )
    assert waiting.status == "WAITING_FOR_FOUNDER"
    assert waiting.founder_report_json["additional_budget_twd"] == "0.25"


def test_ceo_meeting_reuses_engine_and_budgets(ctx):
    operation = operations.approve(_operation(ctx))
    participants = Employee.query.filter(
        Employee.slug.in_(["ceo", "critic"])
    ).all()
    meeting = operations.create_meeting(
        operation,
        "Resolve a material conflict",
        participants,
        Decimal("0.50"),
    )
    assert isinstance(meeting, Meeting)
    assert meeting.created_by == "CEO"
    assert meeting.project_id == operation.project_id
    assert meeting.real_cost_limit_twd == Decimal("0.5000")
    assert meeting.operation_id == operation.id
    with pytest.raises(ValueError, match="operation budget"):
        operations.create_meeting(
            operation, "Too expensive", participants, Decimal("3")
        )


def test_talent_template_is_inert_and_not_org_headcount(client, ctx):
    before_employees = Employee.query.count()
    candidate = workforce.create_talent_template({
        "name": "Market Research Analyst",
        "role_title": "Market Research Analyst",
        "department_hint": "Research",
        "mission": "Analyze market evidence.",
        "responsibilities": ["Evaluate evidence"],
        "instructions": "Separate fact from inference.",
        "skills": ["Market analysis"],
        "suggested_tools": [],
        "deliverables": ["Research brief"],
        "success_metrics": ["Decision-useful evidence"],
        "source": "Founder manual",
    })
    assert isinstance(candidate, TalentTemplate)
    assert Employee.query.count() == before_employees
    assert AgentRun.query.count() == 0
    team = client.get("/team").get_data(as_text=True)
    assert candidate.name not in team.split("PEOPLE OPERATIONS", 1)[0]


def test_hr_department_and_separate_people_operations(client, ctx):
    hr = Employee.query.filter_by(slug="hr-director").one()
    assert hr.department.name == "Human Resources"
    assert hr.position.name == "HR Director"
    team = client.get("/team").get_data(as_text=True)
    assert "PEOPLE OPERATIONS" in team
    hr_page = client.get("/team/hr").get_data(as_text=True)
    for text in ("NEEDS FOUNDER", "OPEN REQUESTS", "TALENT POOL",
                 "ACTIVE WORKFORCE", "ADD CANDIDATE"):
        assert text in hr_page


def test_authorized_requesters_and_hr_review_are_required(ctx):
    founder_request = workforce.request_hire(
        requested_by_type="FOUNDER",
        role_needed="Finance Analyst",
        problem="Financial capacity gap",
        why_now="An operation needs financial review",
        responsibilities=["Review economics"],
        capabilities=["Financial analysis"],
        urgency="MEDIUM",
        use_frequency="OCCASIONAL",
    )
    ceo = Employee.query.filter_by(slug="ceo").one()
    director = Employee.query.filter_by(slug="research-director").one()
    worker = Employee.query.filter_by(slug="researcher").one()
    assert workforce.request_hire(
        requester=ceo, requested_by_type="EMPLOYEE",
        role_needed="Analyst", problem="Gap", why_now="Now",
        responsibilities=["Analyze"], capabilities=["Analysis"],
        urgency="LOW", use_frequency="OCCASIONAL",
    ).status == "REQUESTED"
    assert workforce.request_hire(
        requester=director, requested_by_type="EMPLOYEE",
        role_needed="Reviewer", problem="Gap", why_now="Now",
        responsibilities=["Review"], capabilities=["Review"],
        urgency="LOW", use_frequency="OCCASIONAL",
    ).status == "REQUESTED"
    with pytest.raises(ValueError, match="management"):
        workforce.request_hire(
            requester=worker, requested_by_type="EMPLOYEE",
            role_needed="Assistant", problem="Gap", why_now="Now",
            responsibilities=["Assist"], capabilities=["Assistance"],
            urgency="LOW", use_frequency="OCCASIONAL",
        )
    with pytest.raises(ValueError, match="HR review"):
        workforce.approve_hire(founder_request)


def test_hr_proposal_has_alternatives_model_money_and_probation(ctx):
    request = workforce.request_hire(
        requested_by_type="FOUNDER",
        role_needed="Market Analyst", problem="Capacity gap", why_now="Now",
        responsibilities=["Analyze"], capabilities=["Analysis"],
        urgency="HIGH", use_frequency="RECURRING",
    )
    model = ModelConfig.query.filter_by(active=True).first()
    workforce.review_request(
        request,
        existing_staff_alternative="Researcher can cover a temporary probe.",
        recommendation="TEMPORARY",
        recommended_model=model,
        estimated_input_tokens=1000,
        estimated_output_tokens=500,
        expected_calls=2,
        max_mission_budget_twd=Decimal("1.20"),
        expected_benefit="Faster qualified evidence review.",
        redundancy_risk="MEDIUM",
        alternatives=["No hire", "Use existing Researcher", "Cheaper model"],
        success_criteria=["Three useful assignments"],
        probation_assignments=3,
    )
    assert request.status == "FOUNDER_REVIEW"
    proposal = request.hr_assessment_json
    assert proposal["existing_staff_alternative"]
    assert request.recommended_model_config_id == model.id
    assert Decimal(proposal["estimated_cost_per_mission_twd"]) == (
        workforce.estimate_mission_cost(model, 1000, 500, 2)
    )
    assert proposal["max_mission_budget_twd"] == "1.20"
    assert proposal["expected_benefit"]
    assert proposal["success_criteria"]
    assert proposal["probation_assignments"] == 3


def test_founder_hire_approval_is_zero_call_exactly_once_and_rejection_safe(
    ctx, monkeypatch
):
    calls = []
    monkeypatch.setattr(
        "eason_one.services.execution.get_provider",
        lambda *a, **k: calls.append((a, k)),
    )
    candidate = workforce.create_talent_template({
        "name": "Finance Analyst", "role_title": "Finance Analyst",
        "department_hint": "Human Resources", "mission": "Review finance.",
        "responsibilities": ["Review finance"], "instructions": "Be precise.",
        "skills": ["Finance"], "suggested_tools": [],
        "deliverables": ["Financial review"], "success_metrics": ["Accuracy"],
        "source": "Founder manual",
    })
    request = workforce.request_hire(
        requested_by_type="FOUNDER", role_needed="Finance Analyst",
        problem="Gap", why_now="Now", responsibilities=["Review"],
        capabilities=["Finance"], urgency="HIGH", use_frequency="RECURRING",
        talent_template=candidate,
    )
    model = ModelConfig.query.filter_by(active=True).first()
    workforce.review_request(
        request, existing_staff_alternative="No durable internal capacity.",
        recommendation="HIRE", recommended_model=model,
        estimated_input_tokens=100, estimated_output_tokens=100,
        expected_calls=1, max_mission_budget_twd=Decimal("1"),
        expected_benefit="Financial review capacity.", redundancy_risk="LOW",
        alternatives=["No hire"], success_criteria=["Useful review"],
        probation_assignments=3,
    )
    before = Employee.query.count()
    employee = workforce.approve_hire(request)
    assert Employee.query.count() == before + 1
    assert workforce.approve_hire(request).id == employee.id
    assert Employee.query.count() == before + 1
    assert employee.current_model_config_id == model.id
    assert employee.name == candidate.name
    assert calls == []
    rejected = workforce.request_hire(
        requested_by_type="FOUNDER", role_needed="Duplicate",
        problem="Maybe", why_now="Later", responsibilities=["None"],
        capabilities=["None"], urgency="LOW", use_frequency="OCCASIONAL",
    )
    workforce.reject_request(rejected, "Not justified")
    assert rejected.status == "REJECTED"
    assert rejected.created_employee_id is None


def test_importer_is_idempotent_and_never_creates_employees(ctx, tmp_path):
    source = tmp_path / "talent.json"
    source.write_text(json.dumps([{
        "name": "Exact Candidate", "role_title": "Exact Role",
        "department_hint": "Research", "mission": "Exact mission",
        "responsibilities": ["Exact responsibility"],
        "instructions": "Exact instructions", "skills": ["Exact skill"],
        "suggested_tools": ["Exact tool"], "deliverables": ["Exact output"],
        "success_metrics": ["Exact metric"], "source": "Founder library",
    }]), encoding="utf-8")
    before = Employee.query.count()
    first = workforce.import_talent(source)
    second = workforce.import_talent(source)
    assert [x.name for x in first] == ["Exact Candidate"]
    assert [x.id for x in second] == [x.id for x in first]
    assert TalentTemplate.query.count() == 1
    assert Employee.query.count() == before


def test_no_repository_source_means_no_fabricated_147(ctx):
    source = workforce.find_founder_talent_source()
    if source is None:
        assert TalentTemplate.query.count() == 0
        assert workforce.import_founder_library() == {
            "imported": 0, "source": None,
            "message": "147-source file required for import.",
        }


def test_founder_overrides_remain_reachable(client):
    project_page = client.get("/projects/1")
    if project_page.status_code == 200:
        text = project_page.get_data(as_text=True)
        assert "FOUNDER OVERRIDE" in text
        assert "/tasks/" in text or "Create Task" in text
    meetings = client.get("/meetings").get_data(as_text=True)
    assert "New Meeting" in meetings
    assert "meeting-planner" in meetings


def test_deep_navy_tokens_replace_near_black_canvas():
    css = open("eason_one/static/app.css", encoding="utf-8").read()
    for exact in (
        "--canvas:#081A2B", "--surface-1:#0D2438",
        "--surface-2:#12344D", "--surface-3:#184866",
        "--text-primary:#F4FAFF", "--text-secondary:#A9C3D6",
        "--text-muted:#7292AA", "--accent:#55D6FF",
    ):
        assert exact.lower() in css.lower()
    assert "var(--canvas)" in css


def test_operation_audit_links_runs_and_costs(ctx):
    operation = operations.approve(_operation(ctx))
    operations.next_step(operation, "audit-step")
    run = AgentRun.query.one()
    assert run.operation_id == operation.id
    event = CostEvent.query.filter_by(agent_run_id=run.id).one()
    assert event.operation_id == operation.id
    assert OperationStep.query.filter_by(
        operation_id=operation.id, agent_run_id=run.id
    ).one()
