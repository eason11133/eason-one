from decimal import Decimal
from pathlib import Path

from eason_one.extensions import db
from eason_one.models import Employee, Project


def test_v015_primary_navigation_is_founder_company_model(ctx):
    template = Path("eason_one/templates/hq_base.html").read_text(encoding="utf-8")
    assert "('/headquarters','HQ','⌂')" in template
    assert "('/headquarters/projects','Projects','□')" in template
    assert "('/headquarters/people','People','P')" in template
    assert "('/headquarters/meetings','Meetings','◎')" in template
    assert "('/headquarters/truth','Company Truth','↺')" in template
    assert "('/headquarters/finance','Finance','₵')" in template
    assert "('/headquarters/ceo-office','CEO Office'" not in template
    assert "Runtime / Mission Control" in template
    assert "data-command-open" in template


def test_v015_hq_is_briefing_not_dashboard(client):
    response = client.get("/headquarters")
    assert response.status_code == 200
    text = response.get_data(as_text=True)
    assert "COMPANY · FOUNDER RE-ENTRY" in text
    assert "CURRENT COMPANY SITUATION" in text
    assert "Where the real outcomes are now" in text
    assert "Company Truth" in text
    assert "Finance" in text
    assert "Founder / HQ Home" not in text
    assert "Live Meetings / Company Activity" not in text
    assert "Project Health" not in text


def test_v015_ceo_office_route_is_contextual_ceo_not_dashboard(client):
    response = client.get("/headquarters/ceo-office")
    assert response.status_code == 200
    text = response.get_data(as_text=True)
    assert "Talk to CEO" in text
    assert "CEO Focus" not in text
    assert "Decision Queue" not in text
    assert "Delegations in Progress" not in text
    assert "Blocked / At Risk" not in text


def test_v015_projects_remove_fake_precision_and_internal_dashboard(client):
    response = client.get("/headquarters/projects")
    assert response.status_code == 200
    text = response.get_data(as_text=True)
    assert "Real outcomes the company is responsible for." in text
    assert "Give the company an outcome, not a Task." in text
    assert "Project Health" not in text
    assert "Next Milestones" not in text
    assert "Search projects" not in text
    assert "%</" not in text


def test_v015_project_detail_is_now_people_work_artifact_decision_meeting(client, ctx):
    owner = Employee.query.filter_by(slug="ceo").one()
    project = Project(
        name="Founder Outcome Project",
        objective="Ship one real Founder outcome.",
        status="ACTIVE", priority="HIGH", environment="LIVE", origin="EXISTING",
        owner_employee_id=owner.id, real_budget_limit=Decimal("100"),
        next_milestone="Verify the first real artifact.",
    )
    db.session.add(project)
    db.session.commit()
    response = client.get(f"/headquarters/projects/{project.id}")
    assert response.status_code == 200
    text = response.get_data(as_text=True)
    for label in ("PROJECT · OUTCOME SPACE", "CURRENT FRONTIER", "WITHOUT FOUNDER", "PEOPLE", "WORK", "ARTIFACTS & EVIDENCE", "DECISIONS", "MEETINGS"):
        assert label in text
    assert "Project Health" not in text
    assert "Progress" not in text
    assert "Advanced · runtime records" in text


def test_v015_people_is_people_not_hr_dashboard(client):
    response = client.get("/headquarters/people")
    assert response.status_code == 200
    text = response.get_data(as_text=True)
    assert "The people doing the work." in text
    assert "WORKING NOW" in text
    assert "AVAILABLE" in text
    assert "ORGANIZATION" in text
    assert "The company as an operating organization." not in text
    assert "Workforce planning is separate from the active roster." not in text


def test_v015_meeting_lobby_and_room_remove_mission_first_copy(client, ctx):
    lobby = client.get("/headquarters/meetings")
    assert lobby.status_code == 200
    text = lobby.get_data(as_text=True)
    assert "Coordination only when the work needs it." in text
    assert "carry the conclusion back to its Mission" not in text
    room = Path("eason_one/templates/hq_meeting.html").read_text(encoding="utf-8")
    assert "RETURN TO MISSION" not in room
    assert "RETURN TO PROJECT" in room


def test_v015_company_truth_replaces_results_gallery(client):
    response = client.get("/headquarters/truth")
    assert response.status_code == 200
    text = response.get_data(as_text=True)
    assert "COMPANY TRUTH" in text
    assert "What actually happened." in text
    assert "What the company actually made." not in text
    legacy = client.get("/headquarters/results")
    assert legacy.status_code == 200
    assert "What actually happened." in legacy.get_data(as_text=True)


def test_v015_finance_is_primary_and_truthful(client):
    response = client.get("/headquarters/finance")
    assert response.status_code == 200
    text = response.get_data(as_text=True)
    assert "Every cost remains attributable." in text
    assert "COMPANY AUTHORITY" in text
    assert "RECENT COST" in text


def test_v015_employee_and_attention_routes_render_new_experience(client, ctx):
    employee = Employee.query.filter_by(slug="engineer").one()
    response = client.get(f"/headquarters/employees/{employee.id}")
    assert response.status_code == 200
    text = response.get_data(as_text=True)
    assert "PERSISTENT EMPLOYEE" in text
    assert "CURRENT PROFESSIONAL STATE" in text
    assert "CONTRIBUTION" in text
    assert "OPPORTUNITY" in text
    attention = client.get("/headquarters/attention")
    assert attention.status_code == 200
    attention_text = attention.get_data(as_text=True)
    assert "Only decisions the company cannot make itself." in attention_text
    assert "RUNTIME INCIDENTS" in attention_text or "Nothing needs Founder authority." in attention_text
