from decimal import Decimal

from eason_one.extensions import db
from eason_one.models import Employee, Meeting, MeetingParticipant
from eason_one.services.company import get_company


def test_command_is_primary(client):
    response = client.get("/")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/command")


def test_primary_navigation_is_exact(client):
    page = client.get("/command").get_data(as_text=True)
    nav = page.split('<nav class="primary-nav">', 1)[1].split("</nav>", 1)[0]
    assert [label for label in ("Command", "Work", "Team", "Company")
            if f">{label}</a>" in nav] == ["Command", "Work", "Team", "Company"]
    assert nav.count('class="nav-item') == 4
    for forbidden in ("Models", "Meetings", "Costs", "Runs"):
        assert f">{forbidden}</a>" not in nav


def test_command_has_dominant_stage_primitives(client):
    page = client.get("/command").get_data(as_text=True)
    for class_name in (
        "command-stage",
        "ceo-core",
        "ceo-orbit",
        "command-input",
        "telemetry-strip",
        "executive-brief",
    ):
        assert f'class="{class_name}' in page


def test_command_dom_hierarchy(client):
    page = client.get("/command").get_data(as_text=True)
    stage = page.index('class="command-stage')
    brief = page.index('class="executive-brief')
    operations = page.index('id="active-company"')
    activity = page.index('class="event-stream')
    assert stage < brief < operations < activity


def test_command_load_constructs_no_provider(client, monkeypatch):
    calls = []

    def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("GET /command must not construct a provider")

    monkeypatch.setattr("eason_one.services.execution.get_provider", forbidden)
    assert client.get("/command").status_code == 200
    assert calls == []


def test_team_renders_real_hierarchy(client, ctx):
    ceo = Employee.query.filter_by(slug="ceo").one()
    report = Employee.query.filter(Employee.manager_id == ceo.id).first()
    assert report is not None and report.department is not None
    page = client.get("/team").get_data(as_text=True)
    assert 'class="org-chart' in page
    assert 'class="org-branch' in page
    assert 'class="employee-node' in page
    assert ceo.name in page
    assert report.name in page
    assert report.department.name in page
    assert f'data-manager-id="{ceo.id}"' in page


def test_company_has_semantic_system_groups(client):
    page = client.get("/company").get_data(as_text=True)
    for group in ("Intelligence", "Operations", "Infrastructure"):
        assert f'data-system-group="{group.lower()}"' in page
        assert f">{group}<" in page
    assert page.count('class="system-group') == 3


def test_meeting_uses_conversation_stage_not_ellipse(client, ctx):
    ceo = Employee.query.filter_by(slug="ceo").one()
    meeting = Meeting(
        company_id=get_company().id,
        title="Acceptance Room",
        purpose="Presentation acceptance",
        agenda="Review the presentation",
        chair_employee_id=ceo.id,
        status="PLANNED",
        execution_profile="ECONOMY",
        max_rounds=1,
        real_cost_limit_twd=Decimal("1"),
    )
    db.session.add(meeting)
    db.session.flush()
    db.session.add(MeetingParticipant(
        meeting_id=meeting.id, employee_id=ceo.id, role="CHAIR"
    ))
    db.session.commit()
    page = client.get(f"/meetings/{meeting.id}").get_data(as_text=True)
    assert 'class="meeting-stage' in page
    assert 'class="seat ' in page
    assert 'class="speech"' in page
    assert "table-core" not in page


def test_advanced_routes_remain_reachable(client, ctx):
    ceo = Employee.query.filter_by(slug="ceo").one()
    for path in (
        "/projects",
        f"/employees/{ceo.id}",
        "/meetings",
        "/models",
        "/costs",
        "/inbox",
        "/company/runs",
        "/company/brain",
    ):
        assert client.get(path).status_code == 200


def test_shared_css_has_reduced_motion_contract():
    with open("eason_one/static/app.css", encoding="utf-8") as handle:
        css = handle.read()
    assert "@media (prefers-reduced-motion: reduce)" in css
    assert ".ceo-orbit" in css
    assert "animation: none" in css
