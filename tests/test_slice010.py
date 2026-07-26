from decimal import Decimal

from eason_one.extensions import db
from eason_one.models import AgentRun, Employee, Operation, TalentTemplate, Task
from eason_one.services.ceo import founder_request
from eason_one.services import operations


def test_founder_objective_produces_operation_without_preapproval_work(ctx):
    ceo = Employee.query.filter_by(slug="ceo").one()
    before = Task.query.count()
    run, operation = founder_request(
        ceo, "Prepare a bounded qualified prospect validation"
    )
    assert run.status == "SUCCEEDED"
    assert isinstance(operation, Operation)
    assert operation.status == "PLANNED"
    assert operation.approved_at is None
    assert Task.query.count() == before


def test_founder_approval_route_enters_browser_runner(client, ctx):
    ceo = Employee.query.filter_by(slug="ceo").one()
    _, operation = founder_request(
        ceo, "Prepare a bounded qualified prospect validation"
    )
    response = client.post(f"/operations/{operation.id}/approve")
    assert response.status_code == 302
    assert response.headers["Location"].endswith(
        f"/operations/{operation.id}?autorun=1"
    )
    db.session.refresh(operation)
    assert operation.status == "RUNNING"
    page = client.get(f"/operations/{operation.id}").get_data(as_text=True)
    assert "CEO-managed operation" in page
    assert "CEO owner" in page
    assert "FOUNDER OVERRIDE / ADVANCED" in page


def test_operation_http_step_is_idempotent_and_one_call(client, ctx):
    ceo = Employee.query.filter_by(slug="ceo").one()
    _, operation = founder_request(
        ceo, "Prepare a bounded qualified prospect validation"
    )
    operations.approve(operation)
    before = AgentRun.query.count()
    first = client.post(
        f"/operations/{operation.id}/next-step",
        headers={"Idempotency-Key": "same-browser-step"},
    )
    assert first.status_code == 200
    assert AgentRun.query.count() == before + 1
    duplicate = client.post(
        f"/operations/{operation.id}/next-step",
        headers={"Idempotency-Key": "same-browser-step"},
    )
    assert duplicate.status_code == 200
    assert duplicate.get_json()["agent_run_id"] == first.get_json()["agent_run_id"]
    assert AgentRun.query.count() == before + 1


def test_closing_runner_has_no_server_background_execution(client, ctx):
    ceo = Employee.query.filter_by(slug="ceo").one()
    _, operation = founder_request(
        ceo, "Prepare a bounded qualified prospect validation"
    )
    operations.approve(operation)
    client.get(f"/operations/{operation.id}?autorun=1")
    assert AgentRun.query.filter_by(operation_id=operation.id).count() == 0


def test_manual_candidate_route_never_hires(client, ctx):
    before = Employee.query.count()
    response = client.post("/team/hr/candidates", data={
        "name": "Security Reviewer Candidate",
        "role_title": "Security Reviewer",
        "department_hint": "Engineering",
        "mission": "Review security assumptions.",
        "responsibilities": "Review threats",
        "instructions": "Challenge unsupported security claims.",
        "skills": "Threat modeling",
        "suggested_tools": "",
        "deliverables": "Security review",
        "success_metrics": "Material risks found",
    })
    assert response.status_code == 302
    assert TalentTemplate.query.filter_by(
        name="Security Reviewer Candidate"
    ).one()
    assert Employee.query.count() == before


def test_operation_actual_and_remaining_budget_reconcile(ctx):
    ceo = Employee.query.filter_by(slug="ceo").one()
    _, operation = founder_request(
        ceo, "Prepare a bounded qualified prospect validation"
    )
    operations.approve(operation)
    operations.next_step(operation, "cost-step")
    cost = operations.actual_cost(operation)
    assert cost >= Decimal("0")
    assert operations.remaining_budget(operation) == (
        Decimal(operation.approved_budget_twd) - cost
    )
