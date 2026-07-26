import json
from decimal import Decimal

from eason_one.extensions import db
from eason_one.models import (
    Employee,
    Meeting,
    MeetingMessage,
    MeetingParticipant,
    Project,
    Task,
)
from eason_one.services.company import get_company


def _meeting(ceo, status="RUNNING", minutes=None):
    meeting = Meeting(
        company_id=get_company().id,
        title=f"Founder UI {status} Discussion",
        purpose="Founder UI acceptance",
        agenda="Decide the next Company action",
        chair_employee_id=ceo.id,
        status=status,
        execution_profile="ECONOMY",
        max_rounds=1,
        real_cost_limit_twd=Decimal("2"),
        minutes_json=minutes,
    )
    db.session.add(meeting)
    db.session.flush()
    db.session.add(MeetingParticipant(
        meeting_id=meeting.id, employee_id=ceo.id, role="CHAIR"
    ))
    db.session.commit()
    return meeting


def test_primary_navigation_and_command_core(client):
    page = client.get("/command").get_data(as_text=True)
    nav = page.split('<nav class="primary-nav">', 1)[1].split("</nav>", 1)[0]
    assert nav.count('class="nav-item') == 4
    assert all(f">{label}</a>" in nav for label in (
        "Command", "Work", "Team", "Company"
    ))
    assert all(f">{label}</a>" not in nav for label in (
        "Meetings", "Models", "Costs", "Runs", "Inbox"
    ))
    assert 'class="ceo-core"' in page
    assert 'class="ceo-orbit ' in page
    assert 'class="command-input"' in page


def test_command_load_is_zero_provider_call(client, monkeypatch):
    calls = []

    def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("Command load must not construct a provider")

    monkeypatch.setattr("eason_one.services.execution.get_provider", forbidden)
    assert client.get("/command").status_code == 200
    assert calls == []


def test_team_uses_real_nested_personnel_identity(client, ctx):
    ceo = Employee.query.filter_by(slug="ceo").one()
    manager = Employee.query.filter_by(manager_id=ceo.id).first()
    worker = Employee.query.filter_by(manager_id=manager.id).first()
    assert manager is not None and worker is not None
    page = client.get("/team").get_data(as_text=True)
    manager_tree = page.split(
        f'<article class="personnel-tree" data-tree-root="{manager.id}">',
        1,
    )[1].split(f"<!-- personnel-tree:{manager.id} -->", 1)[0]
    assert manager_tree.index(f'data-employee-id="{manager.id}"') < (
        manager_tree.index('class="direct-reports"')
    ) < manager_tree.index(f'data-employee-id="{worker.id}"')
    assert page.count('class="employee-avatar"') == Employee.query.count()
    assert page.count('class="avatar-head"') == Employee.query.count()
    assert page.count('class="avatar-shoulders"') == Employee.query.count()


def test_meeting_lobby_is_activity_first_and_planner_secondary(client):
    page = client.get("/meetings").get_data(as_text=True)
    assert 'class="meeting-lobby"' in page
    assert 'id="meeting-live"' in page
    assert 'id="meeting-recent"' in page
    assert 'class="meeting-planner"' in page
    planner = page.split('class="meeting-planner"', 1)[1]
    assert "<details open" not in planner.split("</details>", 1)[0]
    assert page.index('id="meeting-live"') < page.index(
        'class="meeting-planner"'
    )


def test_lobby_normal_records_hide_mock_implementation_language(client, ctx):
    ceo = Employee.query.filter_by(slug="ceo").one()
    _meeting(ceo, status="ENDED", minutes={
        "positions": [{"position": "Review result", "actions": []}],
        "responses": [],
        "agreement": [],
        "evidence_ids": [],
    })
    page = client.get("/meetings").get_data(as_text=True)
    normal = page.split('class="meeting-planner"', 1)[0]
    assert "LOCAL MOCK" not in normal.upper()
    assert "DETERMINISTIC-MOCK" not in normal.upper()
    assert "provider_key" not in normal


def test_active_meeting_is_dialogue_first(client, ctx):
    ceo = Employee.query.filter_by(slug="ceo").one()
    meeting = _meeting(ceo)
    db.session.add(MeetingMessage(
        meeting_id=meeting.id,
        employee_id=ceo.id,
        speaker_type="EMPLOYEE",
        round_number=1,
        message_type="CONTRIBUTION",
        content=json.dumps({
            "position": "Run a ten-person prepaid probe before expanding.",
            "actions": ["Freeze workflow"],
            "risk": "Consulting contamination",
        }),
        validation_status="VALID",
    ))
    db.session.commit()
    page = client.get(f"/meetings/{meeting.id}").get_data(as_text=True)
    assert 'class="meeting-room-layout active-mode"' in page
    assert 'class="meeting-room"' in page
    assert "LIVE SUMMARY" in page
    assert 'class="dialogue-card"' in page
    assert "Run a ten-person prepaid probe before expanding." in page
    assert page.index("Run a ten-person prepaid probe before expanding.") < (
        page.index('class="structured-contribution"')
    )
    structured = page.split('class="structured-contribution"', 1)[1]
    assert "<summary>View structured contribution</summary>" in structured
    assert "<details open" not in structured.split("</details>", 1)[0]


def test_terminal_meeting_is_result_first_and_controls_are_omitted(client, ctx):
    ceo = Employee.query.filter_by(slug="ceo").one()
    meeting = _meeting(ceo, status="ENDED", minutes={
        "positions": [{"position": "Run the probe", "actions": ["Recruit"]}],
        "responses": [{"relation": "QUALIFY", "core_point": "Freeze scope",
                       "controls": ["No consulting"], "risk": "Contamination"}],
        "agreement": ["Use a prepaid probe"],
        "evidence_ids": [],
        "provider_calls": 2,
    })
    page = client.get(f"/meetings/{meeting.id}").get_data(as_text=True)
    assert page.index('class="result-mode"') < page.index(
        'class="discussion-details"'
    )
    assert "<details open" not in page.split(
        'class="discussion-details"', 1
    )[1].split(">", 1)[0]
    assert "FOUNDER CONTROL / OBSERVING" not in page
    result_context = page.split('class="result-context"', 1)[1].split(
        "</div>", 1
    )[0]
    assert "Participants · CEO" in result_context


def test_recovered_result_preserves_founder_truth(client, ctx):
    ceo = Employee.query.filter_by(slug="ceo").one()
    meeting = _meeting(ceo, status="TERMINATED_BY_FOUNDER", minutes={
        "positions": [{"position": "Preserve intelligence", "actions": []}],
        "responses": [],
        "agreement": [],
        "evidence_ids": [],
        "recovery": {
            "original_status": "TERMINATED_BY_FOUNDER",
            "provider": "historical",
            "model": "historical",
        },
    })
    page = client.get(f"/meetings/{meeting.id}").get_data(as_text=True)
    assert "TERMINATED BY FOUNDER" in page
    assert "LOCAL · 0 API CALLS" in page
    assert page.index('class="result-mode"') < page.index(
        'class="discussion-details"'
    )


def test_work_progress_is_deterministic(client, ctx):
    ceo = Employee.query.filter_by(slug="ceo").one()
    project = Project(
        name="Founder UI Work",
        objective="Use real progress",
        status="ACTIVE",
        environment="LIVE",
        owner=ceo,
    )
    db.session.add(project)
    db.session.flush()
    db.session.add_all([
        Task(project_id=project.id, title="Done", objective="Done",
             status="DONE"),
        Task(project_id=project.id, title="Open", objective="Open",
             status="TODO"),
    ])
    db.session.commit()
    page = client.get("/work").get_data(as_text=True)
    assert "1 / 2 tasks done" in page
    assert "42%" not in page
    assert "Task ID" not in page


def test_normal_navigation_is_zero_provider_call(client, ctx, monkeypatch):
    ceo = Employee.query.filter_by(slug="ceo").one()
    meeting = _meeting(ceo, status="ENDED", minutes={
        "positions": [{"position": "Done", "actions": []}],
        "responses": [],
        "agreement": [],
        "evidence_ids": [],
    })
    calls = []

    def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("navigation must not construct a provider")

    monkeypatch.setattr("eason_one.services.execution.get_provider", forbidden)
    for path in (
        "/command",
        "/work",
        "/team",
        "/company",
        "/meetings",
        f"/meetings/{meeting.id}",
    ):
        assert client.get(path).status_code == 200
    assert calls == []


def test_shared_tokens_and_reduced_motion_are_present():
    with open("eason_one/static/app.css", encoding="utf-8") as handle:
        css = handle.read()
    for token in (
        "--canvas:",
        "--surface-1:",
        "--surface-2:",
        "--surface-3:",
        "--text-primary:",
        "--text-secondary:",
        "--text-muted:",
    ):
        assert token in css
    assert "@media (prefers-reduced-motion: reduce)" in css
