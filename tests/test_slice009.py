from decimal import Decimal

from eason_one.extensions import db
from eason_one.models import (
    AgentRun,
    Employee,
    Meeting,
    MeetingParticipant,
    Project,
    Proposal,
    Task,
)
from eason_one.services import command as command_service
from eason_one.services.ceo import founder_request
from eason_one.services.company import get_company
from eason_one.services.meetings import result_view


def test_primary_shell_and_advanced_surfaces(client):
    response = client.get("/")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/command")

    page = client.get("/command").get_data(as_text=True)
    for label in ("Command", "Work", "Team", "Company"):
        assert f">{label}</a>" in page
    primary = page.split("</nav>", 1)[0]
    for label in ("Meetings", "Models", "Cost", "Runs"):
        assert label not in primary

    assert client.get("/ceo").status_code == 302
    for path in ("/work", "/team", "/company", "/projects", "/employees",
                 "/meetings", "/models", "/costs", "/inbox", "/company/runs"):
        assert client.get(path).status_code == 200


def test_primary_page_loads_never_construct_provider(client, monkeypatch):
    calls = []

    def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("provider must not be constructed during navigation")

    monkeypatch.setattr("eason_one.services.execution.get_provider", forbidden)
    for path in ("/command", "/work", "/team", "/company", "/command"):
        assert client.get(path).status_code == 200
    assert calls == []


def test_command_and_work_only_surface_live_projects(client, ctx):
    ceo = Employee.query.filter_by(slug="ceo").one()
    live = Project(
        name="Founder Live Work",
        objective="Validate demand",
        status="ACTIVE",
        environment="LIVE",
        owner=ceo,
        next_milestone="Recruit prospects",
    )
    smoke = Project(
        name="Hidden Smoke Work",
        objective="Test fixture",
        status="ACTIVE",
        environment="SMOKE",
        owner=ceo,
    )
    db.session.add_all([live, smoke])
    db.session.commit()

    for path in ("/command", "/work"):
        page = client.get(path).get_data(as_text=True)
        assert "Founder Live Work" in page
        assert "Hidden Smoke Work" not in page


def test_attention_is_conditional_and_activity_is_persisted(client, ctx):
    empty = client.get("/command").get_data(as_text=True)
    assert "Needs you" not in empty

    ceo = Employee.query.filter_by(slug="ceo").one()
    run, _ = founder_request(ceo, "What would you recommend?")
    proposal = Proposal(
        agent_run_id=run.id,
        proposed_by_employee_id=ceo.id,
        status="PENDING",
        payload_json={"type": "PROJECT_PLAN", "plan": {
            "project": {"name": "Governed Proposal"}
        }},
    )
    db.session.add(proposal)
    db.session.commit()
    page = client.get("/command").get_data(as_text=True)
    assert "Needs you" in page
    assert "Governed Proposal" in page
    assert "Company activity" in page


def test_advisory_is_non_mutating(ctx):
    ceo = Employee.query.filter_by(slug="ceo").one()
    before = Proposal.query.count()
    run, proposal = founder_request(ceo, "What would you recommend?")
    assert run.parsed_output_json["mode"] == "ADVISORY"
    assert proposal is None
    assert Proposal.query.count() == before


def test_work_progress_is_deterministic_and_no_fake_percentage(client, ctx):
    ceo = Employee.query.filter_by(slug="ceo").one()
    project = Project(
        name="Deterministic Progress",
        objective="Measure actual completion",
        status="ACTIVE",
        environment="LIVE",
        owner=ceo,
    )
    no_tasks = Project(
        name="Lifecycle Only",
        objective="Has no tasks",
        status="PLANNING",
        environment="LIVE",
        owner=ceo,
    )
    db.session.add_all([project, no_tasks])
    db.session.flush()
    db.session.add_all([
        Task(project_id=project.id, title="Done", objective="Done", status="DONE"),
        Task(project_id=project.id, title="Open", objective="Open", status="TODO"),
    ])
    db.session.commit()
    page = client.get("/work").get_data(as_text=True)
    assert "1 / 2 tasks done" in page
    assert "Lifecycle planning" in page
    assert "42%" not in page


def test_team_status_priority_and_live_task_filter(client, ctx):
    ceo = Employee.query.filter_by(slug="ceo").one()
    worker = Employee.query.filter(Employee.slug != "ceo").first()
    assert command_service.employee_status(worker) == "AVAILABLE"

    project = Project(
        name="Live Assignment",
        objective="Execute",
        status="ACTIVE",
        environment="LIVE",
        owner=ceo,
    )
    db.session.add(project)
    db.session.flush()
    db.session.add(Task(
        project_id=project.id,
        title="Current work",
        objective="Execute",
        status="ASSIGNED",
        assigned_employee_id=worker.id,
    ))
    db.session.commit()
    assert command_service.employee_status(worker) == "WORKING"

    meeting = Meeting(
        title="Live discussion",
        purpose="Review",
        agenda="Review",
        status="RUNNING",
        company_id=get_company().id,
        chair_employee_id=ceo.id,
        max_rounds=1,
        execution_profile="ECONOMY",
        real_cost_limit_twd=Decimal("1"),
    )
    db.session.add(meeting)
    db.session.flush()
    db.session.add(MeetingParticipant(
        meeting_id=meeting.id, employee_id=worker.id, role="MEMBER"
    ))
    db.session.commit()
    assert command_service.employee_status(worker) == "IN MEETING"

    worker.active = False
    db.session.commit()
    assert command_service.employee_status(worker) == "DISABLED"
    page = client.get("/team").get_data(as_text=True)
    assert worker.department.name in page


def test_active_meeting_banner_and_detail_pages_render(client, ctx):
    ceo = Employee.query.filter_by(slug="ceo").one()
    meeting = Meeting(
        title="Founder-visible Meeting",
        purpose="Decide",
        agenda="Decide",
        status="WAITING_FOR_FOUNDER",
        company_id=get_company().id,
        chair_employee_id=ceo.id,
        max_rounds=1,
        execution_profile="ECONOMY",
        real_cost_limit_twd=Decimal("1"),
    )
    db.session.add(meeting)
    db.session.flush()
    db.session.add(MeetingParticipant(
        meeting_id=meeting.id, employee_id=ceo.id, role="CHAIR"
    ))
    db.session.commit()
    assert "Founder-visible Meeting" in client.get("/command").get_data(as_text=True)
    assert client.get(f"/meetings/{meeting.id}").status_code == 200
    assert client.get(f"/employees/{ceo.id}").status_code == 200


def test_standard_result_normalization_uses_persisted_runs(ctx):
    ceo = Employee.query.filter_by(slug="ceo").one()
    meeting = Meeting(
        title="Synthesis",
        purpose="Synthesize",
        agenda="Synthesize",
        status="ENDED",
        company_id=get_company().id,
        chair_employee_id=ceo.id,
        max_rounds=2,
        execution_profile="STANDARD",
        real_cost_limit_twd=Decimal("5"),
        minutes_json={
            "agreements": ["Proceed carefully"],
            "disagreements": ["Timing remains open"],
            "actions": ["Run the probe"],
            "evidence_referenced": [12],
            "founder_decisions_required": ["Approve the probe"],
        },
    )
    db.session.add(meeting)
    db.session.flush()
    db.session.add(AgentRun(
        employee_id=ceo.id,
        meeting_id=meeting.id,
        model_config_id=ceo.current_model_config_id,
        purpose="MEETING_SYNTHESIS",
        provider_key_snapshot="mock",
        model_name_snapshot="mock",
        status="SUCCEEDED",
        user_request="Synthesize",
        system_prompt_snapshot="system",
        context_snapshot="context",
        input_price_snapshot=Decimal("0"),
        output_price_snapshot=Decimal("0"),
        currency_snapshot="TWD",
    ))
    db.session.commit()
    view = result_view(meeting)
    assert view["agreement"] == ["Proceed carefully"]
    assert view["actions"] == ["Run the probe"]
    assert view["founder_decisions"] == ["Approve the probe"]
    assert view["calls"] == 0
