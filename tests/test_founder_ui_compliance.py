from decimal import Decimal

from eason_one.extensions import db
from eason_one.models import Employee, Meeting, MeetingParticipant
from eason_one.services.company import get_company


def _terminal_meeting(ceo, participant, title, minutes):
    meeting = Meeting(
        company_id=get_company().id,
        title=title,
        purpose="Review persisted evidence",
        agenda="Choose the next move",
        chair_employee_id=ceo.id,
        status="TERMINATED_BY_FOUNDER",
        execution_profile="ECONOMY",
        max_rounds=1,
        real_cost_limit_twd=Decimal("2"),
        minutes_json=minutes,
    )
    db.session.add(meeting)
    db.session.flush()
    db.session.add_all([
        MeetingParticipant(
            meeting_id=meeting.id, employee_id=ceo.id, role="CHAIR"
        ),
        MeetingParticipant(
            meeting_id=meeting.id, employee_id=participant.id, role="PARTICIPANT"
        ),
    ])
    db.session.commit()
    return meeting


def test_new_meeting_trigger_opens_closed_planner(client):
    page = client.get("/meetings").get_data(as_text=True)
    assert (
        'id="open-meeting-planner" data-planner-target="meeting-planner"'
        in page
    )
    planner_tag = page.split('id="meeting-planner"', 1)[0].rsplit("<details", 1)[1]
    assert " open" not in planner_tag
    assert "planner.open = true" in page
    assert "planner.scrollIntoView" in page
    assert "firstField.focus" in page


def test_meeting_creation_form_still_creates_a_planned_meeting(client, ctx):
    ceo = Employee.query.filter_by(slug="ceo").one()
    before = Meeting.query.count()
    response = client.post("/meetings", data={
        "title": "Compliance Review",
        "mission_context": "Confirm the Founder UI presentation.",
        "meeting_question": "Is the presentation compliant?",
        "purpose": "Meeting mission",
        "agenda": "",
        "participant_ids": [str(ceo.id)],
        "chair_employee_id": str(ceo.id),
        "project_id": "",
        "execution_profile": "ECONOMY",
    })
    assert response.status_code == 302
    assert Meeting.query.count() == before + 1
    created = Meeting.query.order_by(Meeting.id.desc()).first()
    assert created.title == "Compliance Review"
    assert created.status == "PLANNED"


def test_lobby_uses_real_recovery_result_and_visible_participant_names(
    client, ctx
):
    employees = Employee.query.order_by(Employee.id).limit(2).all()
    ceo, participant = employees
    ordinary = _terminal_meeting(
        ceo,
        participant,
        "Ordinary termination",
        {
            "positions": [{"position": "Preserve the ordinary record",
                           "actions": []}],
            "responses": [],
            "agreement": [],
            "evidence_ids": [],
        },
    )
    recovered = _terminal_meeting(
        ceo,
        participant,
        "Recovered benchmark",
        {
            "positions": [{"position": "Run a 10-person prepaid probe.",
                           "actions": []}],
            "responses": [],
            "agreement": [],
            "evidence_ids": [],
            "recovery": {
                "original_status": "TERMINATED_BY_FOUNDER",
                "provider": "historical",
                "model": "historical",
            },
        },
    )
    page = client.get("/meetings").get_data(as_text=True)
    ordinary_row = page.split(
        f'href="/meetings/{ordinary.id}"', 1
    )[1].split("</a>", 1)[0]
    recovered_row = page.split(
        f'href="/meetings/{recovered.id}"', 1
    )[1].split("</a>", 1)[0]
    assert "TERMINATED" in ordinary_row
    assert "RESULT RECOVERED" not in ordinary_row
    assert "RESULT RECOVERED" in recovered_row
    assert "Run a 10-person prepaid probe." in recovered_row
    assert ceo.name in recovered_row
    assert participant.name in recovered_row
    assert 'class="participant-names"' in recovered_row
    normal = page.split('class="meeting-planner"', 1)[0]
    assert "LOCAL MOCK" not in normal.upper()
    assert "DETERMINISTIC-MOCK" not in normal.upper()


def test_meeting_lobby_get_is_zero_provider_call(client, monkeypatch):
    calls = []

    def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("Meeting Lobby GET must not construct a provider")

    monkeypatch.setattr("eason_one.services.execution.get_provider", forbidden)
    assert client.get("/meetings").status_code == 200
    assert calls == []
