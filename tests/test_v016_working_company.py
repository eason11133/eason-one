from decimal import Decimal
from pathlib import Path

import pytest

pytest.skip("retired v0.16 OpportunityEvent projection; v0.20 persistent Work/Assignment tests are authoritative", allow_module_level=True)

from eason_one.extensions import db
from eason_one.models import Employee, OpportunityEvent, Project
from eason_one.services.projects import create_project
from eason_one.services.tasks import create_task


def test_v016_is_formal_working_company_release(ctx):
    base = Path("eason_one/templates/hq_base.html").read_text(encoding="utf-8")
    projection = Path("eason_one/services/founder_experience.py").read_text(encoding="utf-8")
    routes = Path("eason_one/routes.py").read_text(encoding="utf-8")
    assert "headquarters-v016.css" in base
    assert "v016-working-company" in base
    assert "experimental research slice" not in projection.lower()
    assert "bounded v0.16 experiment" not in routes.lower()


def test_v016_company_is_reentry_not_dashboard(client):
    response = client.get("/headquarters")
    assert response.status_code == 200
    text = response.get_data(as_text=True)
    assert "COMPANY · FOUNDER RE-ENTRY" in text
    assert "CURRENT COMPANY SITUATION" in text
    assert "WHAT CHANGED" in text
    assert "Project Health" not in text
    assert "COMPANY PULSE" not in text
    assert "OPPORTUNITIES OPEN" not in text


def test_v016_project_is_outcome_space_with_frontier(client, ctx):
    ceo = Employee.query.filter_by(slug="ceo").one()
    engineer = Employee.query.filter_by(slug="engineer").one()
    project = create_project(
        "Working Company Project",
        "Ship one real outcome that can be independently checked.",
        ceo,
        status="ACTIVE",
        real_budget_limit=Decimal("120"),
    )
    create_task(
        project,
        "Build the bounded implementation",
        "Create a reviewable work product.",
        creator=ceo,
        assignee=engineer,
        required_output="Reviewable artifact",
        acceptance_criteria="Independent verification passes",
        eligible_employee_ids=[engineer.id],
        selection_reason="Engineer owns this bounded implementation class.",
    )

    response = client.get(f"/headquarters/projects/{project.id}")
    assert response.status_code == 200
    text = response.get_data(as_text=True)
    assert "PROJECT · OUTCOME SPACE" in text
    assert "CURRENT FRONTIER" in text
    assert "WITHOUT FOUNDER" in text
    assert "ARTIFACTS &amp; EVIDENCE" in text or "ARTIFACTS & EVIDENCE" in text
    assert "Coordination that changed this Project" in text
    assert "Project Health" not in text
    assert "% complete" not in text.lower()


def test_v016_employee_surfaces_selected_and_missed_opportunity_without_score(client, ctx):
    ceo = Employee.query.filter_by(slug="ceo").one()
    engineer = Employee.query.filter_by(slug="engineer").one()
    critic = Employee.query.filter_by(slug="critic").one()
    project = create_project("Opportunity Truth", "Observe allocation, not a score.", ceo, status="ACTIVE")

    task1 = create_task(
        project,
        "Primary implementation",
        "Implement the first slice.",
        creator=ceo,
        assignee=engineer,
        eligible_employee_ids=[engineer.id, critic.id],
        selection_reason="Engineer has the closest current implementation context.",
    )
    task2 = create_task(
        project,
        "Independent challenge",
        "Challenge the implementation assumptions.",
        creator=ceo,
        assignee=critic,
        eligible_employee_ids=[engineer.id, critic.id],
        selection_reason="Critic is deliberately independent for this challenge.",
    )
    assert task1 and task2
    assert OpportunityEvent.query.filter_by(project_id=project.id).count() >= 2

    response = client.get(f"/headquarters/employees/{engineer.id}")
    assert response.status_code == 200
    text = response.get_data(as_text=True)
    assert "CURRENT PROFESSIONAL STATE" in text
    assert "OPPORTUNITY" in text
    assert "ELIGIBLE, NOT SELECTED" in text
    assert "Primary implementation" in text
    assert "Independent challenge" in text
    assert "Contribution score" not in text
    assert "friendship" not in text.lower()


def test_v016_employee_does_not_fake_wallet_or_store_balance(client, ctx):
    employee = Employee.query.filter_by(slug="engineer").one()
    response = client.get(f"/headquarters/employees/{employee.id}")
    text = response.get_data(as_text=True)
    assert "Personal wallet/store ledger is not operational in this runtime yet" in text
    assert "no fake balance is shown" in text.lower()
