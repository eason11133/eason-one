import sqlite3
from decimal import Decimal

import pytest

pytest.skip("retired v0.15 WorkItem runtime spine; v0.20 Company Kernel is the sole active scheduler", allow_module_level=True)

from sqlalchemy import inspect

from eason_one import create_app
from eason_one.extensions import db
from eason_one.models import (
    AgentRun,
    Artifact,
    ArtifactContributor,
    CompanyEvent,
    Decision,
    Employee,
    OpportunityEvent,
    Project,
    Task,
    WorkItem,
)
from eason_one.services.artifacts import record_acceptance, submit_from_run
from eason_one.services.company_events import recent
from eason_one.services.decisions import record_decision
from eason_one.services.projects import create_project
from eason_one.services.tasks import create_task, transition
from eason_one.services.runtime_spine import bootstrap_runtime_spine


def _run(employee, task, *, host_verified=False):
    model = employee.current_model
    context = {"host_validation": {"success": True, "summary": "Host verification passed."}} if host_verified else {}
    run = AgentRun(
        employee_id=employee.id,
        project_id=task.project_id,
        task_id=task.id,
        operation_id=task.operation_id,
        model_config_id=model.id,
        purpose="TASK_EXECUTION",
        user_request=task.objective,
        system_prompt_snapshot="system",
        context_snapshot="context",
        context_composition_json=context,
        raw_output='{"result_summary":"usable result"}',
        parsed_output_json={"result_summary": "usable result", **({"codex": {"changed_files": ["x.py"], "tests": []}} if host_verified else {})},
        status="SUCCEEDED",
        structured_validation_status="PASSED",
        provider_key_snapshot=model.provider_key,
        model_name_snapshot=model.model_name,
        input_price_snapshot=model.input_price_per_million,
        output_price_snapshot=model.output_price_per_million,
        currency_snapshot=model.currency,
        real_cost=Decimal("0"),
    )
    db.session.add(run)
    db.session.flush()
    return run


def test_v015_project_task_assignment_creates_work_item_opportunity_and_truth(ctx):
    ceo = Employee.query.filter_by(slug="ceo").one()
    engineer = Employee.query.filter_by(slug="engineer").one()
    project = create_project("Runtime Spine", "Prove the new company domain.", ceo, status="ACTIVE")

    task = create_task(
        project,
        "Build one bounded slice",
        "Produce a reviewable implementation result.",
        creator=ceo,
        assignee=engineer,
        required_output="Reviewable implementation",
        acceptance_criteria="Host verification passes",
        eligible_employee_ids=[engineer.id],
        selection_reason="Engineer owns implementation and host verification.",
    )

    item = WorkItem.query.filter_by(legacy_task_id=task.id).one()
    assert item.project_id == project.id
    assert item.assigned_employee_id == engineer.id
    assert item.work_type == "ENGINEERING"
    assert item.authority_source == f"PROJECT:{project.id}"

    opportunity = OpportunityEvent.query.filter_by(work_item_id=item.id).one()
    assert opportunity.selected_employee_id == engineer.id
    assert opportunity.eligible_employee_ids_json == [engineer.id]
    assert "host verification" in opportunity.selection_reason

    kinds = [row.event_type for row in CompanyEvent.query.filter_by(project_id=project.id).all()]
    assert "PROJECT_CREATED" in kinds
    assert "PROJECT_ACTIVATED" in kinds
    assert "WORK_ITEM_CREATED" in kinds
    assert "WORK_ASSIGNED" in kinds
    assert "OPPORTUNITY_ALLOCATED" in kinds


def test_v015_run_materializes_artifact_but_only_host_truth_verifies_it(ctx):
    ceo = Employee.query.filter_by(slug="ceo").one()
    engineer = Employee.query.filter_by(slug="engineer").one()
    project = create_project("Artifact Truth", "Keep run success separate from verification.", ceo, status="ACTIVE")
    task = create_task(project, "Implement", "Implement safely.", creator=ceo, assignee=engineer)

    normal_run = _run(engineer, task, host_verified=False)
    normal = submit_from_run(task, normal_run, commit=True)
    assert normal.verification_status == "UNVERIFIED"
    assert normal.status == "SUBMITTED"
    assert ArtifactContributor.query.filter_by(artifact_id=normal.id, employee_id=engineer.id).one()

    # A second legacy Task gives us a distinct AgentRun/Artifact. Only explicit
    # host verification is allowed to promote it to VERIFIED here.
    verified_task = create_task(project, "Host verify", "Implement and host verify.", creator=ceo, assignee=engineer)
    verified_run = _run(engineer, verified_task, host_verified=True)
    verified = submit_from_run(verified_task, verified_run, commit=True)
    assert verified.verification_status == "VERIFIED"
    assert verified.status == "VERIFIED"
    assert verified.verified_at is not None

    events = CompanyEvent.query.filter_by(project_id=project.id).all()
    assert any(row.event_type == "ARTIFACT_SUBMITTED" and row.object_id == normal.id for row in events)
    assert not any(row.event_type == "ARTIFACT_VERIFIED" and row.object_id == normal.id for row in events)
    assert any(row.event_type == "ARTIFACT_VERIFIED" and row.object_id == verified.id for row in events)


def test_v015_review_acceptance_updates_artifact_without_rewriting_run_truth(ctx):
    ceo = Employee.query.filter_by(slug="ceo").one()
    engineer = Employee.query.filter_by(slug="engineer").one()
    project = create_project("Acceptance", "Separate verification and acceptance.", ceo, status="ACTIVE")
    task = create_task(project, "Ship candidate", "Create candidate.", creator=ceo, assignee=engineer)
    run = _run(engineer, task, host_verified=True)
    artifact = submit_from_run(task, run, commit=True)

    accepted = record_acceptance(task, True, reviewer_employee_id=ceo.id, note="Meets Project acceptance.")
    db.session.commit()
    db.session.refresh(artifact)
    assert accepted.id == artifact.id
    assert artifact.acceptance_status == "ACCEPTED"
    assert artifact.status == "ACCEPTED"
    assert artifact.source_agent_run.status == "SUCCEEDED"
    assert CompanyEvent.query.filter_by(event_type="ARTIFACT_ACCEPTED", object_id=artifact.id).one()


def test_v015_decision_is_first_class_and_links_artifact_evidence(ctx):
    ceo = Employee.query.filter_by(slug="ceo").one()
    engineer = Employee.query.filter_by(slug="engineer").one()
    project = create_project("Decision Truth", "Persist organizational decisions.", ceo, status="ACTIVE")
    task = create_task(project, "Evidence", "Produce evidence.", creator=ceo, assignee=engineer)
    run = _run(engineer, task, host_verified=True)
    artifact = submit_from_run(task, run, commit=True)
    item = WorkItem.query.filter_by(legacy_task_id=task.id).one()

    decision = record_decision(
        project,
        title="Require verified evidence before release",
        question="Can this Project ship?",
        chosen_action="WAIT_FOR_VERIFIED_EVIDENCE",
        reason="Run success alone is insufficient.",
        authority_source=f"PROJECT:{project.id}:FOUNDER_APPROVED_SCOPE",
        decision_maker_type="EMPLOYEE",
        decision_maker_employee_id=ceo.id,
        source_work_item_id=item.id,
        alternatives=[
            {"label": "Ship now", "description": "Accept run success only", "selected": False},
            {"label": "Wait", "description": "Require verified evidence", "selected": True},
        ],
        artifact_ids=[artifact.id],
        commit=True,
    )
    assert db.session.get(Decision, decision.id).chosen_action == "WAIT_FOR_VERIFIED_EVIDENCE"
    assert len(decision.alternatives) == 2
    assert decision.evidence_links[0].artifact_id == artifact.id
    assert CompanyEvent.query.filter_by(event_type="DECISION_RECORDED", object_id=decision.id).one()


def test_v015_bootstrap_is_idempotent_and_does_not_fabricate_eligible_pool(ctx):
    ceo = Employee.query.filter_by(slug="ceo").one()
    engineer = Employee.query.filter_by(slug="engineer").one()
    project = Project(
        name="Legacy", objective="Import existing truth", status="ACTIVE", priority="HIGH",
        environment="LIVE", origin="EXISTING", owner_employee_id=ceo.id,
    )
    db.session.add(project); db.session.flush()
    task = Task(
        project_id=project.id, title="Legacy implementation", objective="Existing work",
        status="ASSIGNED", created_by_employee_id=ceo.id, assigned_employee_id=engineer.id,
    )
    db.session.add(task); db.session.commit()

    first = bootstrap_runtime_spine()
    counts = (
        WorkItem.query.count(), OpportunityEvent.query.count(), CompanyEvent.query.count(), Artifact.query.count()
    )
    second = bootstrap_runtime_spine()
    assert counts == (
        WorkItem.query.count(), OpportunityEvent.query.count(), CompanyEvent.query.count(), Artifact.query.count()
    )
    item = WorkItem.query.filter_by(legacy_task_id=task.id).one()
    opportunity = OpportunityEvent.query.filter_by(work_item_id=item.id).one()
    assert opportunity.eligible_employee_ids_json is None
    assert opportunity.source == "LEGACY_IMPORT"
    assert first["work_items"] >= 1 and second["work_items"] >= 1


def test_v015_company_truth_read_is_pure(ctx):
    before = CompanyEvent.query.count()
    rows = recent(25)
    assert isinstance(rows, list)
    assert CompanyEvent.query.count() == before


def test_v015_prior_v0141_additive_tables_are_extended_in_place(tmp_path):
    db_path = tmp_path / "compat.db"
    con = sqlite3.connect(db_path)
    con.execute("""
        CREATE TABLE work_item (
          id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL, legacy_task_id INTEGER UNIQUE,
          parent_work_item_id INTEGER, work_type VARCHAR(40) NOT NULL DEFAULT 'GENERAL',
          title VARCHAR(180) NOT NULL, objective TEXT NOT NULL,
          status VARCHAR(24) NOT NULL DEFAULT 'QUEUED', priority VARCHAR(20) NOT NULL DEFAULT 'MEDIUM',
          created_by_employee_id INTEGER, assigned_employee_id INTEGER, reviewer_employee_id INTEGER,
          expected_output TEXT, acceptance_criteria_json JSON, budget_credits NUMERIC(12,2),
          deadline DATETIME, result_summary TEXT, completed_at DATETIME, created_at DATETIME NOT NULL,
          updated_at DATETIME NOT NULL
        )
    """)
    con.execute("""
        CREATE TABLE opportunity_event (
          id INTEGER PRIMARY KEY, project_id INTEGER, work_item_id INTEGER,
          opportunity_type VARCHAR(40) NOT NULL DEFAULT 'WORK_ASSIGNMENT',
          allocator_employee_id INTEGER, selected_employee_id INTEGER NOT NULL,
          eligible_employee_ids_json JSON, selection_reason TEXT,
          weight NUMERIC(12,2) NOT NULL DEFAULT 1, source VARCHAR(40) NOT NULL DEFAULT 'RUNTIME',
          created_at DATETIME NOT NULL,
          UNIQUE(work_item_id, selected_employee_id, opportunity_type)
        )
    """)
    con.commit(); con.close()

    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": f"sqlite:///{db_path}"})
    with app.app_context():
        inspector = inspect(db.engine)
        work_cols = {col["name"] for col in inspector.get_columns("work_item")}
        opp_cols = {col["name"] for col in inspector.get_columns("opportunity_event")}
        assert {"operation_id", "completion_condition", "authority_source", "budget_ceiling_twd"} <= work_cols
        assert {"value_class", "status", "resolved_at"} <= opp_cols


def test_v015_actual_task_execution_materializes_artifact(ctx):
    from eason_one.services.task_execution import run_task

    ceo = Employee.query.filter_by(slug="ceo").one()
    researcher = Employee.query.filter_by(slug="researcher").one()
    project = create_project("Execution Artifact", "Turn a real Task run into an Artifact.", ceo, status="ACTIVE")
    task = create_task(
        project,
        "Produce bounded evidence",
        "Return one concrete persisted result.",
        creator=ceo,
        assignee=researcher,
        required_output="Evidence result",
        acceptance_criteria="Concrete result exists",
    )

    run = run_task(task)
    assert run.status == "SUCCEEDED"
    artifact = Artifact.query.filter_by(source_agent_run_id=run.id).one()
    assert artifact.work_item_id == WorkItem.query.filter_by(legacy_task_id=task.id).one().id
    assert artifact.verification_status == "UNVERIFIED"
    assert CompanyEvent.query.filter_by(event_type="ARTIFACT_SUBMITTED", object_id=artifact.id).one()
    assert CompanyEvent.query.filter_by(event_type="COST_RECORDED", project_id=project.id).first() is not None


def test_v015_bounded_kernel_events_are_mirrored_into_company_truth(ctx):
    from eason_one.models import Company, Meeting, Operation
    from eason_one.services.operation_kernel import append_event as append_operation_event
    from eason_one.services.meeting_kernel import append_event as append_meeting_event

    ceo = Employee.query.filter_by(slug="ceo").one()
    company = Company.query.first()
    project = create_project("Kernel Mirror", "Mirror bounded runtime truth.", ceo, status="ACTIVE")
    operation = Operation(
        title="Bounded work",
        objective="Test operation truth mirror",
        project_id=project.id,
        proposed_by_employee_id=ceo.id,
        status="PLANNED",
        kernel_status="WAITING_APPROVAL",
        current_stage="WAITING_APPROVAL",
        plan_json={"test": True},
        approved_budget_twd=Decimal("1"),
    )
    db.session.add(operation); db.session.flush()
    op_event = append_operation_event(
        operation,
        "TEST_EVENT",
        from_status="WAITING_APPROVAL",
        to_status="WAITING_APPROVAL",
        stage="TEST",
        actor_type="SYSTEM",
        payload={"proof": True},
    )

    meeting = Meeting(
        company_id=company.id,
        project_id=project.id,
        title="Truth mirror meeting",
        purpose="Prove meeting event mirror",
        agenda="Test",
        chair_employee_id=ceo.id,
        status="PLANNED",
        kernel_status="READY",
        current_stage="READY",
    )
    db.session.add(meeting); db.session.flush()
    meeting_event = append_meeting_event(
        meeting,
        "TEST_EVENT",
        from_status="READY",
        to_status="READY",
        stage="TEST",
        actor_type="SYSTEM",
        payload={"proof": True},
    )
    db.session.commit()

    op_truth = CompanyEvent.query.filter_by(
        idempotency_key=f"operation:{operation.id}:event:{op_event.sequence}"
    ).one()
    meeting_truth = CompanyEvent.query.filter_by(
        idempotency_key=f"meeting:{meeting.id}:event:{meeting_event.sequence}"
    ).one()
    assert op_truth.event_type == "OPERATION_TEST_EVENT"
    assert meeting_truth.event_type == "MEETING_TEST_EVENT"
    assert op_truth.project_id == project.id == meeting_truth.project_id
