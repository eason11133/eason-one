import json
from decimal import Decimal

from eason_one.extensions import db
from eason_one.models import (
    Employee,
    Meeting,
    MeetingMessage,
    MeetingParticipant,
)
from eason_one.services.company import get_company


def _meeting(ceo, status="RUNNING", minutes=None):
    meeting = Meeting(
        company_id=get_company().id,
        title=f"009.2 {status} Room",
        purpose="Founder presentation acceptance",
        agenda="Discuss the operating decision",
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


def test_four_dark_depth_tokens_are_shared():
    with open("eason_one/static/app.css", encoding="utf-8") as handle:
        css = handle.read()
    expected = (
        "--depth-canvas:",
        "--depth-surface:",
        "--depth-raised:",
        "--depth-focused:",
        "--border-strong:",
        "--border-soft:",
    )
    for token in expected:
        assert token in css
    values = []
    for token in expected[:4]:
        values.append(css.split(token, 1)[1].split(";", 1)[0].strip())
    assert len(set(values)) == 4


def test_command_density_hierarchy_and_primary_navigation(client):
    page = client.get("/command").get_data(as_text=True)
    assert page.index('class="command-stage') < page.index(
        'class="executive-brief'
    ) < page.index('id="active-company"')
    nav = page.split('<nav class="primary-nav">', 1)[1].split("</nav>", 1)[0]
    assert nav.count('class="nav-item') == 4
    assert all(f">{label}</a>" in nav for label in (
        "Command", "Work", "Team", "Company"
    ))


def test_team_nodes_have_person_visuals(client, ctx):
    active = Employee.query.filter_by(active=True).count()
    page = client.get("/team").get_data(as_text=True)
    assert page.count('class="personnel-node') == active
    assert page.count('class="employee-avatar') == active
    assert page.count('class="avatar-head"') == active
    assert page.count('class="avatar-shoulders"') == active


def test_worker_is_nested_beneath_real_manager(client, ctx):
    ceo = Employee.query.filter_by(slug="ceo").one()
    manager = Employee.query.filter_by(manager_id=ceo.id).first()
    worker = Employee.query.filter_by(manager_id=manager.id).first()
    assert manager is not None and worker is not None
    page = client.get("/team").get_data(as_text=True)
    manager_tree = page.split(
        f'<article class="personnel-tree" data-tree-root="{manager.id}">',
        1,
    )[1].split(f"<!-- personnel-tree:{manager.id} -->", 1)[0]
    assert f'data-employee-id="{manager.id}"' in manager_tree
    assert 'class="direct-reports"' in manager_tree
    assert f'data-employee-id="{worker.id}"' in manager_tree
    assert manager_tree.index(f'data-employee-id="{manager.id}"') < (
        manager_tree.index('class="direct-reports"')
    ) < manager_tree.index(f'data-employee-id="{worker.id}"')


def test_active_meeting_has_room_brief_two_column_structure(client, ctx):
    ceo = Employee.query.filter_by(slug="ceo").one()
    meeting = _meeting(ceo)
    page = client.get(f"/meetings/{meeting.id}").get_data(as_text=True)
    assert 'class="meeting-room-layout active-mode"' in page
    assert 'class="meeting-room"' in page
    assert 'class="live-brief"' in page
    assert "table-core" not in page
    assert "seat-" not in page


def test_active_contribution_is_dialogue_first_with_structured_details(
    client, ctx
):
    ceo = Employee.query.filter_by(slug="ceo").one()
    meeting = _meeting(ceo)
    contribution = {
        "position": "Run a ten-person prepaid probe before expanding.",
        "actions": ["Freeze workflow", "Recruit ten prospects"],
        "risk": "Consulting contamination",
    }
    db.session.add(MeetingMessage(
        meeting_id=meeting.id,
        employee_id=ceo.id,
        speaker_type="EMPLOYEE",
        round_number=1,
        message_type="CONTRIBUTION",
        content=json.dumps(contribution),
        validation_status="VALID",
    ))
    db.session.commit()
    page = client.get(f"/meetings/{meeting.id}").get_data(as_text=True)
    assert 'class="dialogue-card"' in page
    assert "Run a ten-person prepaid probe before expanding." in page
    assert page.index("Run a ten-person prepaid probe before expanding.") < (
        page.index('class="structured-contribution"')
    )
    structured = page.split('class="structured-contribution"', 1)[1]
    assert "<summary>View structured contribution</summary>" in structured
    assert "Freeze workflow" in structured
    assert "Consulting contamination" in structured
    assert "<details" in page
    assert "<details open" not in structured.split("</details>", 1)[0]


def test_terminal_result_precedes_collapsed_discussion_and_has_no_empty_controls(
    client, ctx
):
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
    assert 'class="result-mode"' in page
    assert 'class="discussion-details"' in page
    assert page.index('class="result-mode"') < page.index(
        'class="discussion-details"'
    )
    discussion = page.split('class="discussion-details"', 1)[0]
    assert "<details open" not in discussion[-80:]
    assert "FOUNDER CONTROL / OBSERVING" not in page


def test_recovered_terminal_result_preserves_truth(client, ctx):
    ceo = Employee.query.filter_by(slug="ceo").one()
    meeting = _meeting(ceo, status="TERMINATED_BY_FOUNDER", minutes={
        "positions": [{"position": "Preserve paid intelligence",
                       "actions": ["Review locally"]}],
        "responses": [],
        "agreement": [],
        "evidence_ids": [],
        "provider_calls": 1,
        "recovery": {
            "original_status": "TERMINATED_BY_FOUNDER",
            "provider": "anthropic",
            "model": "historical-model",
        },
    })
    page = client.get(f"/meetings/{meeting.id}").get_data(as_text=True)
    assert "TERMINATED_BY_FOUNDER" in page
    assert "LOCAL · 0 API CALLS" in page
    assert page.index('class="result-mode"') < page.index(
        'class="discussion-details"'
    )


def test_navigation_is_zero_provider_call(client, ctx, monkeypatch):
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
        raise AssertionError("navigation must not construct providers")

    monkeypatch.setattr("eason_one.services.execution.get_provider", forbidden)
    for path in ("/command", "/team", f"/meetings/{meeting.id}"):
        assert client.get(path).status_code == 200
    assert calls == []
