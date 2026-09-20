from pathlib import Path

from decimal import Decimal

from eason_one.extensions import db
from eason_one.models import Employee, Project


def test_v014_shell_matches_sparse_founder_navigation(ctx):
    """The v0.14 sparse-shell intent must survive later Founder-experience releases."""
    template = Path("eason_one/templates/hq_base.html").read_text(encoding="utf-8")
    assert "headquarters-v014.css" in template
    assert "('/headquarters','HQ','⌂')" in template
    assert "('/headquarters/projects','Projects','□')" in template
    assert "('/headquarters/people','People','P')" in template
    assert "('/headquarters/meetings','Meetings','◎')" in template
    assert "('/headquarters/truth','Company Truth','↺')" in template
    assert "('/headquarters/finance','Finance','₵')" in template
    assert "('/headquarters/ceo-office','CEO Office'" not in template
    assert "Runtime / Mission Control" in template
    assert "Advanced" in template


def test_v014_home_is_sparse_and_ceo_direct_line_still_works(client):
    response = client.get("/headquarters")
    assert response.status_code == 200
    text = response.get_data(as_text=True)
    assert "COMPANY · FOUNDER RE-ENTRY" in text
    assert "CURRENT COMPANY SITUATION" in text
    assert "data-command-open" in text
    assert "COMPANY API" not in text
    assert "Project Health" not in text


def test_v014_ceo_office_is_management_surface_not_chat_dashboard(client):
    response = client.get("/headquarters/ceo-office")
    assert response.status_code == 200
    text = response.get_data(as_text=True)
    assert "Talk to CEO" in text
    assert "CEO Focus" not in text
    assert "Decision Queue" not in text
    assert "Delegations in Progress" not in text
    assert "Blocked / At Risk" not in text


def test_v014_projects_are_outcome_first_and_low_noise(client):
    response = client.get("/headquarters/projects")
    assert response.status_code == 200
    text = response.get_data(as_text=True)
    assert "Real outcomes the company is responsible for." in text
    assert "Give the company an outcome, not a Task." in text
    assert "Project Health" not in text
    assert "Next Milestones" not in text
    assert "Search projects" not in text
    assert "%</" not in text


def test_v014_project_detail_exposes_work_truth_without_making_mission_primary(client, ctx):
    owner = Employee.query.filter_by(slug="ceo").one()
    project = Project(
        name="V014 UI Project",
        objective="Exercise the sparse Project detail surface.",
        status="ACTIVE", priority="HIGH", environment="LIVE", origin="TEST",
        owner_employee_id=owner.id, real_budget_limit=Decimal("100"),
        next_milestone="Verify the new Project workspace.",
    )
    db.session.add(project)
    db.session.commit()
    response = client.get(f"/headquarters/projects/{project.id}")
    assert response.status_code == 200
    text = response.get_data(as_text=True)
    for label in (
        "PROJECT · OUTCOME SPACE",
        "CURRENT FRONTIER",
        "WITHOUT FOUNDER",
        "PEOPLE",
        "WORK",
        "ARTIFACTS & EVIDENCE",
        "DECISIONS",
        "MEETINGS",
    ):
        assert label in text
    assert "Advanced · runtime records" in text
    assert "Project Health" not in text


def test_v014_does_not_rewrite_meeting_room_templates(ctx):
    meeting = Path("eason_one/templates/hq_meeting.html").read_text(encoding="utf-8")
    lobby = Path("eason_one/templates/hq_meetings.html").read_text(encoding="utf-8")
    assert "headquarters-v014" not in meeting
    assert "headquarters-v014" not in lobby
