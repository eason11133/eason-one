from decimal import Decimal

from eason_one.extensions import db
from eason_one.models import Employee, Meeting, MeetingParticipant
from eason_one.services.company import get_company


def test_headquarters_is_primary(client):
    response = client.get("/")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/headquarters")


def test_primary_navigation_uses_only_headquarters_rooms(client):
    page = client.get("/headquarters").get_data(as_text=True)
    nav = page.split('<nav class="hq-nav">', 1)[1].split("</nav>", 1)[0]
    for label in ("HQ", "Missions", "Results", "Research", "Attention", "Memory", "Finance", "System"):
        assert f">{label}</span>" in nav
    for deferred in ("People", "Meetings"):
        assert f">{deferred}</span>" not in nav
    for legacy in ('href="/command"', 'href="/work"', 'href="/team"', 'href="/company"'):
        assert legacy not in nav


def test_headquarters_is_a_front_desk_not_one_dense_dashboard(client):
    page = client.get("/headquarters").get_data(as_text=True)
    assert "FOUNDER HEADQUARTERS" in page
    assert 'class="briefing-composer layout-quiet"' in page
    assert 'data-briefing-block="idle"' in page
    assert 'data-briefing-composer' in page
    assert 'class="adaptive-ceo-briefing' not in page
    assert 'class="context-room-links"' not in page
    assert 'class="department-floor"' not in page
    assert 'class="activity-stream"' not in page


def test_headquarters_get_constructs_no_provider(client, monkeypatch):
    calls = []

    def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("GET /headquarters must not construct a provider")

    monkeypatch.setattr("eason_one.services.execution.get_provider", forbidden)
    assert client.get("/headquarters").status_code == 200
    assert calls == []


def test_people_room_renders_persistent_employee_directory(client, ctx):
    ceo = Employee.query.filter_by(slug="ceo").one()
    report = Employee.query.filter(Employee.manager_id == ceo.id).first()
    assert report is not None
    page = client.get("/headquarters/people").get_data(as_text=True)
    assert "PEOPLE & DEPARTMENTS" in page
    assert 'class="org-floor"' in page
    assert 'class="person-station state-' in page
    assert ceo.name in page
    assert report.name in page
    assert f'/headquarters/employees/{report.id}' in page


def test_meeting_room_uses_new_headquarters_scene_and_existing_actions(client, ctx):
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
    page = client.get(f"/headquarters/meetings/{meeting.id}").get_data(as_text=True)
    assert 'class="hq-conversation-room"' in page
    assert 'class="hq-participant-seat preview-person seat-' in page
    prepared=page.split("PREPARED ROOM",1)[1]
    assert "Waiting to contribute." not in prepared
    assert 'class="seat-speech"' not in prepared
    assert f'action="/meetings/{meeting.id}/start"' in page
    assert f'/headquarters/system/runs' in page or "Technical audit" in page


def test_running_meeting_autorun_page_renders_without_block_scope_leak(client, ctx):
    ceo = Employee.query.filter_by(slug="ceo").one()
    meeting = Meeting(
        company_id=get_company().id,
        title="Autorun Acceptance Room",
        purpose="Verify the live runner can attach",
        agenda="Run one bounded live step",
        chair_employee_id=ceo.id,
        status="RUNNING",
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

    response = client.get(f"/headquarters/meetings/{meeting.id}?autorun=1")
    page = response.get_data(as_text=True)

    assert response.status_code == 200
    assert f"/meetings/{meeting.id}/next-step" in page
    assert f"/headquarters/meetings/{meeting.id}" in page
    assert "runMeeting" in page


def test_legacy_primary_pages_redirect_into_headquarters(client, ctx):
    ceo = Employee.query.filter_by(slug="ceo").one()
    expected = {
        "/command": "/headquarters",
        "/work": "/headquarters/missions",
        "/team": "/headquarters/people",
        "/company": "/headquarters",
        "/company/brain": "/headquarters/memory",
        "/company/runs": "/headquarters/system/runs",
        "/employees": "/headquarters/people",
        f"/employees/{ceo.id}": f"/headquarters/employees/{ceo.id}",
        "/meetings": "/headquarters/meetings",
        "/projects": "/headquarters/missions",
        "/inbox": "/headquarters/attention",
        "/models": "/headquarters/system/models",
        "/costs": "/headquarters/finance",
        "/team/hr": "/headquarters/people/talent",
    }
    for path, destination in expected.items():
        response = client.get(path)
        assert response.status_code == 302
        assert response.headers["Location"].endswith(destination)


def test_headquarters_rooms_are_reachable(client, ctx):
    ceo = Employee.query.filter_by(slug="ceo").one()
    for path in (
        "/headquarters",
        "/headquarters/missions",
        "/headquarters/people",
        "/headquarters/meetings",
        "/headquarters/results",
        "/headquarters/attention",
        "/headquarters/memory",
        "/headquarters/finance",
        "/headquarters/system",
        "/headquarters/system/models",
        "/headquarters/system/runs",
        f"/headquarters/employees/{ceo.id}",
        "/mobile",
    ):
        assert client.get(path).status_code == 200


def test_shared_css_has_reduced_motion_contract():
    with open("eason_one/static/app.css", encoding="utf-8") as handle:
        css = handle.read()
    assert "@media (prefers-reduced-motion: reduce)" in css
    assert "animation: none" in css
