from decimal import Decimal

from eason_one.extensions import db
from eason_one.providers import ProviderResult
from eason_one.services.ceo import founder_request
from eason_one.models import AgentRun, Employee, Meeting, MeetingParticipant, ModelConfig, Operation, Project, Task


def _mission():
    ceo = Employee.query.filter_by(slug="ceo").one()
    researcher = Employee.query.filter_by(slug="researcher").one()
    critic = Employee.query.filter_by(slug="critic").one()
    project = Project(
        name="WildOne Delivery",
        objective="Complete a Founder-ready WildOne delivery slice.",
        status="ACTIVE",
        priority="HIGH",
        environment="LIVE",
        origin="EXISTING",
        owner_employee_id=ceo.id,
        real_budget_limit=Decimal("4000"),
        current_state_summary="Engineering and evidence work are active.",
        next_milestone="Integrated deployment demo",
    )
    db.session.add(project)
    db.session.flush()
    operation = Operation(
        title="WildOne next-stage delivery",
        objective="Research, implement, review, integrate, and demonstrate the next stage.",
        project_id=project.id,
        proposed_by_employee_id=ceo.id,
        status="RUNNING",
        plan_json={"operation": {"completion_criteria": ["Tests pass", "Demo is reviewable"]}},
        approved_budget_twd=Decimal("4000"),
    )
    db.session.add(operation)
    db.session.flush()
    db.session.add_all([
        Task(
            project_id=project.id,
            operation_id=operation.id,
            title="Validate rescue workflow evidence",
            objective="Confirm the workflow basis before implementation.",
            status="DONE",
            assigned_employee_id=researcher.id,
            reviewer_employee_id=critic.id,
            required_output="Evidence package",
            result_summary="Validated workflow evidence package.",
        ),
        Task(
            project_id=project.id,
            operation_id=operation.id,
            title="Integrate backend delivery",
            objective="Connect implementation, tests, and documentation.",
            status="WORKING",
            assigned_employee_id=researcher.id,
            reviewer_employee_id=critic.id,
            required_output="Integrated delivery demo",
        ),
    ])
    meeting = Meeting(
        company_id=1,
        project_id=project.id,
        operation_id=operation.id,
        title="WildOne integration review",
        purpose="Resolve integration risk.",
        agenda="Confirm evidence, implementation, and review handoff.",
        chair_employee_id=ceo.id,
        status="RUNNING",
        current_round=1,
        max_rounds=3,
        token_limit=12000,
        real_cost_limit_twd=Decimal("100"),
    )
    db.session.add(meeting)
    db.session.flush()
    db.session.add_all([
        MeetingParticipant(meeting_id=meeting.id, employee_id=ceo.id, role="Chair"),
        MeetingParticipant(meeting_id=meeting.id, employee_id=researcher.id, role="Contributor"),
        MeetingParticipant(meeting_id=meeting.id, employee_id=critic.id, role="Reviewer"),
    ])
    db.session.commit()
    return operation, researcher, meeting


def test_headquarters_renders_concise_founder_front_desk(client, ctx):
    operation, researcher, meeting = _mission()
    page = client.get("/headquarters").get_data(as_text=True)
    assert "FOUNDER HEADQUARTERS" in page
    assert meeting.title in page
    assert 'class="briefing-composer layout-meeting-first"' in page
    assert 'data-lead-kind="meeting"' in page
    assert 'data-briefing-block="meeting"' in page
    assert 'data-briefing-block="mission"' in page


def test_headquarters_deep_spaces_render(client, ctx):
    operation, researcher, meeting = _mission()
    missions = client.get("/headquarters/missions").get_data(as_text=True)
    assert "MISSION CONTROL" in missions
    assert operation.title in missions
    assert 'class="mission-stage-row status-active"' in missions
    mission = client.get(f"/headquarters/missions/{operation.id}").get_data(as_text=True)
    assert "MISSION SPINE" in mission
    assert "Integrate backend delivery" in mission
    assert "ARTIFACT BAY" in mission
    people = client.get("/headquarters/people").get_data(as_text=True)
    assert researcher.name in people
    assert 'class="org-floor"' in people
    assert 'class="person-station state-' in people
    employee = client.get(f"/headquarters/employees/{researcher.id}").get_data(as_text=True)
    assert "PERSISTENT EMPLOYEE IDENTITY" in employee
    assert "AI CORE HISTORY" in employee
    assert "Promotion moves the whole Employee" in employee
    meeting_lobby = client.get("/headquarters/meetings").get_data(as_text=True)
    assert 'class="meeting-world-card status-running"' in meeting_lobby
    assert "CURRENT ISSUE" in meeting_lobby
    meeting_page = client.get(f"/headquarters/meetings/{meeting.id}").get_data(as_text=True)
    assert "LIVE ROOM" in meeting_page
    assert "FOUNDER PRESENCE" in meeting_page
    assert 'class="live-participant-grid tiles-3"' in meeting_page
    assert meeting_page.count('class="live-participant-tile') == 3
    mobile = client.get("/mobile").get_data(as_text=True)
    assert "CEO LINE" in mobile
    assert "EMPLOYEE PROGRESS" in mobile
    assert operation.title in mobile


def test_local_ceo_briefing_is_read_only(client, ctx):
    operation, _, _ = _mission()
    before = AgentRun.query.count()
    response = client.post(
        "/headquarters",
        data={"request": "How is the company?", "mode": "briefing"},
        follow_redirects=True,
    )
    page = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "CEO headquarters briefing" in page
    assert operation.title in page
    assert AgentRun.query.count() == before


def test_headquarters_state_api(client, ctx):
    operation, _, _ = _mission()
    payload = client.get("/api/headquarters/state").get_json()
    assert payload["ready"] is True
    assert payload["focus"]["id"] == operation.id
    assert payload["working_employees"] >= 1


def test_source_text_is_preserved_in_headquarters(client, ctx):
    operation, _, _ = _mission()
    operation.title = "專案驗收 10 人、NT$300–500"
    operation.plan_json = {"operation": {"completion_criteria": ["招募 10 人並保留完整原始紀錄"]}}
    operation.tasks[0].title = "建立招募與收費流程"
    db.session.commit()
    mission = client.get(f"/headquarters/missions/{operation.id}").get_data(as_text=True)
    assert "專案驗收 10 人、NT$300–500" in mission
    assert "招募 10 人並保留完整原始紀錄" in mission
    assert "建立招募與收費流程" in mission


def test_hr_talent_catalog_is_honest_about_missing_source(client, ctx):
    page = client.get("/headquarters/people/talent").get_data(as_text=True)
    assert "HR OFFICE · TALENT CATALOG" in page
    assert "Roles are a verified talent pool" in page
    assert "Eason One will not invent missing roles" in page or "Verified source detected" in page


def test_operation_budget_labels_are_explicit(client, ctx):
    operation, _, _ = _mission()
    page = client.get(f"/headquarters/missions/{operation.id}").get_data(as_text=True)
    assert "OPERATION BUDGET" in page
    assert "Spent" in page
    assert "Authorized" in page
    assert "Company ceiling" in page
    assert "not the whole company budget" in page


def test_archived_model_uses_restore_lifecycle_instead_of_enable(client, ctx):
    model = ModelConfig.query.filter_by(provider_key="mock").one()
    employees = Employee.query.filter_by(current_model_config_id=model.id, active=True).all()
    assert employees
    model.archived = True
    model.active = False
    db.session.commit()

    page = client.get("/headquarters/system/models").get_data(as_text=True)
    card = page.split(model.model_name, 1)[1]
    assert "RESTORE CONFIGURATION" in card
    assert f'action="/models/{model.id}/toggle"' not in card
    assert "still reference it" in card

    response = client.post(f"/models/{model.id}/restore", follow_redirects=True)
    assert response.status_code == 200
    db.session.refresh(model)
    assert model.archived is False
    assert model.active is True


def test_assigned_model_cannot_be_archived(client, ctx):
    model = ModelConfig.query.filter_by(provider_key="mock").one()
    response = client.post(f"/models/{model.id}/archive", follow_redirects=True)
    page = response.get_data(as_text=True)
    db.session.refresh(model)
    assert model.archived is False
    assert "Reassign" in page


def test_live_meeting_promotes_recent_speakers_into_four_tile_surface(client, ctx):
    operation, _, meeting = _mission()
    extras = Employee.query.filter(~Employee.id.in_([p.employee_id for p in meeting.participants])).limit(3).all()
    for employee in extras:
        db.session.add(MeetingParticipant(meeting_id=meeting.id, employee_id=employee.id, role="Contributor"))
    db.session.commit()

    page = client.get(f"/headquarters/meetings/{meeting.id}").get_data(as_text=True)
    assert 'data-live-participant-grid' in page
    assert page.count('class="live-participant-tile') >= 5
    assert "PARTICIPANTS IN BACKGROUND" in page
    assert "promoteSpeaker" in page or "latest speaker is promoted" in page


def test_company_status_command_uses_deterministic_snapshot_and_composed_report(client, ctx):
    operation, researcher, _ = _mission()
    before=(Operation.query.count(),Task.query.count(),Meeting.query.count())
    response=client.post(
        "/headquarters",
        data={
            "request":"Give me a factual status report of Eason One. Report the current priority Mission, stage, Employees working, blocking incidents, Founder actions, and next expected result.",
            "mode":"command",
        },
        follow_redirects=True,
    )
    page=response.get_data(as_text=True)
    run=AgentRun.query.order_by(AgentRun.id.desc()).first()
    assert run.purpose=="CEO_STATUS_REPORT"
    assert run.status=="SUCCEEDED"
    assert run.structured_validation_status=="PASSED"
    report=run.parsed_output_json["status_report"]
    assert report["priority_mission"]["operation_id"]==operation.id
    assert report["priority_mission"]["title"]==operation.title
    assert any(item["employee_id"]==researcher.id for item in report["employees_working"])
    assert "CEO VALIDATED REPORT" in page
    assert "Priority Mission" in page
    assert "Current Mission stage" in page
    assert "Employees working now" in page
    assert "Blocking incidents" in page
    assert "Founder actions required" in page
    assert "Next expected result" in page
    assert 'data-lead-kind="ceo_report"' in page
    assert 'class="briefing-block block-ceo_report span-8"' in page
    assert before==(Operation.query.count(),Task.query.count(),Meeting.query.count())



def test_company_status_validation_rejects_contradictory_model_summary(ctx, monkeypatch):
    operation, _, _ = _mission()
    ceo = Employee.query.filter_by(slug="ceo").one()
    before = (Operation.query.count(), Task.query.count(), Meeting.query.count())

    class ContradictoryStatusProvider:
        def complete(self, *args, **kwargs):
            return ProviderResult(
                '{"executive_summary":"No Mission record named Eason One exists."}',
                80,
                16,
                response_id="contradictory-status",
            )

    monkeypatch.setattr(
        "eason_one.services.execution.get_provider",
        lambda _: ContradictoryStatusProvider(),
    )

    run, created = founder_request(
        ceo,
        "Give me a factual company status report for Eason One.",
    )

    assert created is None
    assert run.purpose == "CEO_STATUS_REPORT"
    assert run.status == "FAILED"
    assert run.failure_reason == "STATUS_REPORT_VALIDATION_FAILED"
    assert run.structured_validation_status == "FAILED"
    assert "contradicts the persisted priority Mission" in run.error_text
    assert before == (Operation.query.count(), Task.query.count(), Meeting.query.count())
    assert operation.status == "RUNNING"

def test_planned_meeting_preview_has_people_but_no_conversation_boxes(client, ctx):
    _, _, meeting = _mission()
    meeting.status="PLANNED"
    db.session.commit()
    page=client.get(f"/headquarters/meetings/{meeting.id}").get_data(as_text=True)
    assert 'class="hq-participant-seat preview-person seat-' in page
    planned=page.split("PREPARED ROOM",1)[1].split("LIVE BRIEF",1)[0] if "LIVE BRIEF" in page else page
    assert "Waiting to contribute." not in planned
    assert 'class="seat-speech"' not in planned
    assert "CURRENT ISSUE" in planned
