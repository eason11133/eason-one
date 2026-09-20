import json
from decimal import Decimal

from eason_one import create_app
from eason_one.extensions import db
from eason_one.models import (
    AgentRun, Artifact, ArtifactVersion, CompanyEvent, Employee,
    ExternalEffectAttempt, ModelConfig, Operation, Project, Task, WaitCondition, Work,
)
from eason_one.providers import ProviderResult
from eason_one.seed import seed
from eason_one.services import multi_agent
from eason_one.services import operation_kernel as kernel
from eason_one.services.operations import approve, next_step, propose_operation


def _plan(project, owners):
    return {
        "mode": "OPERATION_PLAN",
        "executive_response": "Run bounded company work.",
        "operation": {
            "title": "Company Core Batch A",
            "objective": "Exercise Work/Execution/Artifact truth.",
            "project_id": project.id,
            "budget_twd": 10,
            "tasks": [
                {
                    "title": f"Branch {index+1}",
                    "objective": f"Produce bounded result {index+1}.",
                    "assignee_employee_id": employee.id,
                    "reviewer_employee_id": None,
                    "acceptance_criteria": ["A durable accepted Artifact exists."],
                }
                for index, employee in enumerate(owners)
            ],
            "meeting_policy": "NEVER",
            "meeting_config": {
                "trigger": "NEVER", "participant_employee_ids": [],
                "max_rounds": 1, "max_speakers_per_round": 1,
                "contribution_output_cap": 192, "token_limit": 6000,
                "budget_twd": 0, "retry_limit": 0,
            },
            "completion_criteria": ["Approved Work is accepted."],
        },
    }


def _operation(owners):
    ceo = Employee.query.filter_by(slug="ceo").one()
    project = Project(
        name="Company Core vNext",
        objective="Prove durable company work.",
        status="ACTIVE", priority="HIGH", environment="LIVE", origin="TEST",
        owner_employee_id=ceo.id, real_budget_limit=Decimal("100"),
    )
    db.session.add(project); db.session.flush()
    operation = propose_operation(ceo, _plan(project, owners), route_type="FULL_PROJECT")
    approve(operation); db.session.refresh(operation)
    return operation, project


def _start(operation):
    kernel.transition(operation, "RUNNING", "TEST_RUNTIME_STARTED", stage="EXECUTION", actor_type="RUNTIME")
    db.session.refresh(operation)


def test_approval_materializes_work_as_company_truth_and_execution_closes_to_artifact(ctx):
    researcher = Employee.query.filter_by(slug="researcher").one()
    operation, project = _operation([researcher])
    task = operation.tasks[0]
    work = db.session.get(Work, task.work_id)
    assert work is not None
    assert work.state == "READY"
    assert work.assignments[-1].employee_id == researcher.id

    _start(operation)
    result = next_step(operation, "batch-a-single-work")
    assert result["status"] == "SUCCEEDED"

    db.session.expire_all()
    work = db.session.get(Work, work.id)
    assert work.state == "ACCEPTED"
    runs = AgentRun.query.filter_by(work_id=work.id, purpose="TASK_EXECUTION").all()
    assert len(runs) == 1
    assert runs[0].outcome == "SUCCEEDED"
    artifact = Artifact.query.filter_by(work_id=work.id).one()
    version = ArtifactVersion.query.filter_by(artifact_id=artifact.id).one()
    assert version.status == "ACCEPTED"
    assert version.execution_id == runs[0].id
    event_types = [row.event_type for row in CompanyEvent.query.filter_by(work_id=work.id).order_by(CompanyEvent.id)]
    for required in ("WORK_CREATED", "WORK_ASSIGNED", "EXECUTION_STARTED", "EXECUTION_SUCCEEDED", "ARTIFACT_SUBMITTED", "ARTIFACT_ACCEPTED", "WORK_ACCEPTED"):
        assert required in event_types


def test_post_dispatch_ambiguity_is_work_scoped_and_does_not_freeze_sibling(ctx, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    researcher = Employee.query.filter_by(slug="researcher").one()
    critic = Employee.query.filter_by(slug="critic").one()
    good = ModelConfig(
        label="Good", provider_key="openai", model_name="good",
        input_price_per_million=1, output_price_per_million=1,
        currency="TWD", max_output_tokens=4096,
    )
    ambiguous = ModelConfig(
        label="Ambiguous", provider_key="anthropic", model_name="ambiguous",
        input_price_per_million=1, output_price_per_million=1,
        currency="TWD", max_output_tokens=4096,
    )
    db.session.add_all([good, ambiguous]); db.session.flush()
    researcher.current_model_config_id = good.id
    critic.current_model_config_id = ambiguous.id
    db.session.commit()
    operation, project = _operation([researcher, critic])
    tasks = list(operation.tasks)
    multi_agent.persist_plan(operation, {
        "strategy": "PARALLEL_DAG", "rationale": "Independent branches.", "max_parallelism": 2,
        "tasks": [
            {"task_id": tasks[0].id, "depends_on_task_ids": [], "role": "WORKER", "reason": "healthy"},
            {"task_id": tasks[1].id, "depends_on_task_ids": [], "role": "WORKER", "reason": "ambiguous"},
        ],
    })
    _start(operation)

    class GoodProvider:
        def complete(self, *args, **kwargs):
            payload={"result_summary":"Healthy sibling completed.","knowledge_proposals":[]}
            return ProviderResult(json.dumps(payload), 100, 50, request_id="good-rq", response_id="good-rs")
    class AmbiguousProvider:
        def complete(self, *args, **kwargs):
            raise RuntimeError("connection lost after request dispatch")
    monkeypatch.setattr(
        "eason_one.services.execution.get_provider",
        lambda key: AmbiguousProvider() if key == "anthropic" else GoodProvider(),
    )

    wave = next_step(operation, "batch-a-ambiguous-wave")
    assert wave["status"] == "PARTIAL"
    db.session.expire_all()
    tasks = Task.query.filter_by(operation_id=operation.id).order_by(Task.id).all()
    assert tasks[0].status == "DONE"
    assert tasks[1].status == "BLOCKED"
    good_work = db.session.get(Work, tasks[0].work_id)
    bad_work = db.session.get(Work, tasks[1].work_id)
    assert good_work.state == "ACCEPTED"
    assert bad_work.state == "WAITING"
    wait = WaitCondition.query.filter_by(work_id=bad_work.id, state="OPEN").one()
    assert wait.condition_type == "RECONCILIATION"
    bad_run = AgentRun.query.filter_by(work_id=bad_work.id, purpose="TASK_EXECUTION").one()
    assert bad_run.outcome == "FAILED_AMBIGUOUS"
    effect = ExternalEffectAttempt.query.filter_by(execution_id=bad_run.id).one()
    assert effect.state == "AMBIGUOUS_POST_DISPATCH"
    operation = db.session.get(Operation, operation.id)
    assert kernel.authoritative_status(operation) == "RUNNING"
    assert operation.founder_report_json is None

    recovery = next_step(operation, "batch-a-ambiguous-recovery")
    assert recovery["status"] == "RECONCILIATION_REQUIRED"
    operation = db.session.get(Operation, operation.id)
    assert kernel.authoritative_status(operation) == "WAITING_INPUT"
    assert operation.founder_report_json is None


def test_legacy_task_backfill_links_historical_execution_to_work(ctx):
    from eason_one.services.work_runtime import backfill_legacy_work_spine
    ceo = Employee.query.filter_by(slug="ceo").one()
    researcher = Employee.query.filter_by(slug="researcher").one()
    project = Project(
        name="Legacy", objective="Legacy import", status="ACTIVE", priority="MEDIUM",
        environment="LIVE", origin="TEST", owner_employee_id=ceo.id,
    )
    db.session.add(project); db.session.flush()
    task = Task(
        project_id=project.id, title="Legacy Task", objective="Old work", status="DONE",
        priority="MEDIUM", assigned_employee_id=researcher.id,
        created_by_employee_id=ceo.id, result_summary="Historical result",
    )
    db.session.add(task); db.session.flush()
    model = researcher.current_model
    run = AgentRun(
        employee_id=researcher.id, project_id=project.id, task_id=task.id,
        model_config_id=model.id, purpose="TASK_EXECUTION", user_request=task.objective,
        system_prompt_snapshot="legacy", context_snapshot="legacy", raw_output="legacy",
        status="SUCCEEDED", provider_key_snapshot=model.provider_key,
        model_name_snapshot=model.model_name, input_price_snapshot=0, output_price_snapshot=0,
        currency_snapshot="TWD", currency="TWD", outcome=None,
    )
    db.session.add(run); db.session.commit()

    assert backfill_legacy_work_spine() == 1
    db.session.refresh(task); db.session.refresh(run)
    work = db.session.get(Work, task.work_id)
    assert work.state == "ACCEPTED"
    assert run.work_id == work.id
    assert CompanyEvent.query.filter_by(work_id=work.id, event_type="WORK_IMPORTED").count() == 1
    assert backfill_legacy_work_spine() == 0


def test_restart_preserves_work_execution_artifact_and_event_truth(tmp_path):
    db_path = tmp_path / "restart.db"
    config={"TESTING":True,"SQLALCHEMY_DATABASE_URI":f"sqlite:///{db_path}","ALLOW_MOCK_PROVIDER":True}
    app1=create_app(config)
    with app1.app_context():
        db.drop_all(); db.create_all(); seed()
        researcher=Employee.query.filter_by(slug="researcher").one()
        operation, project=_operation([researcher])
        _start(operation)
        result=next_step(operation,"restart-before-close")
        assert result["status"]=="SUCCEEDED"
        work_id=operation.tasks[0].work_id
        run_id=result["agent_run_id"]
        artifact_version_id=result["artifact_version_id"]
        project_id=project.id
        db.session.remove()

    app2=create_app(config)
    with app2.app_context():
        work=db.session.get(Work,work_id)
        run=db.session.get(AgentRun,run_id)
        version=db.session.get(ArtifactVersion,artifact_version_id)
        assert work.state=="ACCEPTED"
        assert run.work_id==work.id and run.outcome=="SUCCEEDED"
        assert version.status=="ACCEPTED"
        assert CompanyEvent.query.filter_by(project_id=project_id,work_id=work.id,event_type="WORK_ACCEPTED").count()==1


def test_completion_guard_uses_work_and_artifact_truth_not_legacy_task_status(ctx):
    from eason_one.services.operations import completion_guard
    from eason_one.services.work_runtime import open_wait

    researcher = Employee.query.filter_by(slug="researcher").one()
    operation, _ = _operation([researcher])
    task = operation.tasks[0]
    work = db.session.get(Work, task.work_id)

    # A legacy surface claiming DONE may not override unresolved Work truth.
    task.status = "DONE"
    open_wait(work, "INTERNAL_RECOVERY", "Execution still requires recovery.")
    db.session.commit()

    allowed, reasons = completion_guard(operation)
    assert allowed is False
    assert any(f"Work #{work.id} WAITING" in reason for reason in reasons)


def test_reassignment_preserves_work_assignment_history_and_resolves_internal_wait(ctx):
    from eason_one.services.operations import reassign_task
    from eason_one.services.work_runtime import open_wait

    researcher = Employee.query.filter_by(slug="researcher").one()
    critic = Employee.query.filter_by(slug="critic").one()
    operation, _ = _operation([researcher])
    _start(operation)
    task = operation.tasks[0]
    work = db.session.get(Work, task.work_id)
    task.status = "BLOCKED"
    open_wait(work, "INTERNAL_RECOVERY", "Original strategy failed safely.")
    db.session.commit()

    reassign_task(
        operation, task, critic, None,
        "Critic has a better fit for the bounded recovery.",
    )
    db.session.expire_all()
    work = db.session.get(Work, work.id)
    assignments = sorted(work.assignments, key=lambda row: row.id)
    assert len(assignments) == 2
    assert assignments[0].employee_id == researcher.id
    assert assignments[0].ended_at is not None
    assert assignments[1].employee_id == critic.id
    assert assignments[1].ended_at is None
    assert work.state == "READY"
    assert WaitCondition.query.filter_by(
        work_id=work.id, condition_type="INTERNAL_RECOVERY", state="OPEN"
    ).count() == 0
    task = db.session.get(Task, task.id)
    assert task.status == "ASSIGNED"
    assert task.assigned_employee_id == critic.id


def test_management_execution_has_work_and_final_report_closes_management_work(ctx):
    from eason_one.services.company_truth import project_snapshot

    researcher = Employee.query.filter_by(slug="researcher").one()
    operation, project = _operation([researcher])
    _start(operation)

    execution = next_step(operation, "batch-a-closure-execution")
    assert execution["kind"] == "TASK"
    assert db.session.get(Work, operation.tasks[0].work_id).state == "ACCEPTED"

    verification = next_step(operation, "batch-a-closure-verification")
    assert verification["kind"] == "GOAL_VERIFICATION"
    assert verification["overall_status"] == "SATISFIED"
    verification_run = AgentRun.query.filter_by(
        operation_id=operation.id, purpose="GOAL_VERIFICATION"
    ).one()
    management_work = db.session.get(Work, verification_run.work_id)
    assert management_work is not None
    assert management_work.work_type == "MANAGEMENT"
    assert management_work.state == "EXECUTING"

    report = next_step(operation, "batch-a-closure-report")
    assert report["kind"] == "REPORT"
    db.session.expire_all()
    operation = db.session.get(Operation, operation.id)
    management_work = db.session.get(Work, management_work.id)
    assert operation.status == "COMPLETED"
    assert management_work.state == "ACCEPTED"

    report_run = AgentRun.query.filter_by(
        operation_id=operation.id, purpose="CEO_OPERATION_REPORT"
    ).one()
    assert report_run.work_id == management_work.id
    artifact = Artifact.query.filter_by(
        work_id=management_work.id, artifact_type="FOUNDER_REPORT"
    ).one()
    version = ArtifactVersion.query.filter_by(artifact_id=artifact.id).one()
    assert version.status == "ACCEPTED"
    assert version.execution_id == report_run.id
    assert CompanyEvent.query.filter_by(
        project_id=project.id, event_type="PROJECT_OUTCOME_READY"
    ).count() == 1

    truth = project_snapshot(project.id)
    assert truth["state"] == "RESULT_READY"
    assert truth["progress"] == 100
    # Every model-backed execution created during this closure is attributable.
    unattributed = AgentRun.query.filter(
        AgentRun.operation_id == operation.id,
        AgentRun.purpose.in_(["TASK_EXECUTION", "GOAL_VERIFICATION", "CEO_OPERATION_REPORT"]),
        AgentRun.work_id.is_(None),
    ).count()
    assert unattributed == 0


def test_restart_recovers_successful_execution_into_artifact_without_provider_recall(tmp_path):
    from eason_one.models import OperationStep
    from eason_one.services.task_execution import run_task
    from eason_one.services.work_runtime import transition as transition_work

    db_path = tmp_path / "open-execution-recovery.db"
    config = {
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": f"sqlite:///{db_path}",
        "ALLOW_MOCK_PROVIDER": True,
    }
    app1 = create_app(config)
    with app1.app_context():
        db.drop_all(); db.create_all(); seed()
        researcher = Employee.query.filter_by(slug="researcher").one()
        operation, _ = _operation([researcher])
        _start(operation)
        task = operation.tasks[0]
        work = db.session.get(Work, task.work_id)
        task.status = "WORKING"
        transition_work(work, "EXECUTING", reason="Simulate in-flight execution")
        step = OperationStep(
            operation_id=operation.id,
            idempotency_key="crash-after-provider-response",
            logical_key=f"task-recovery:{task.id}",
            kind="TASK", status="EXECUTION_STARTED", task_id=task.id,
        )
        db.session.add(step); db.session.flush()
        run = run_task(task)
        assert run.status == "SUCCEEDED"
        assert Artifact.query.filter_by(work_id=work.id).count() == 0
        step.agent_run_id = run.id
        operation_id, work_id, run_id = operation.id, work.id, run.id
        db.session.commit(); db.session.remove()

    # create_app performs the real stale-worker startup recovery (RUNNING→QUEUED).
    app2 = create_app(config)
    with app2.app_context():
        from eason_one.services.operations import _recover_open_step
        operation = db.session.get(Operation, operation_id)
        recovered = _recover_open_step(operation)
        assert recovered["kind"] == "TASK"
        assert recovered["recovered"] is True
        work = db.session.get(Work, work_id)
        assert work.state == "ACCEPTED"
        version = ArtifactVersion.query.join(Artifact).filter(
            Artifact.work_id == work.id
        ).one()
        assert version.status == "ACCEPTED"
        assert version.execution_id == run_id
        # Recovery materializes persisted output; it never creates a replacement provider run.
        assert AgentRun.query.filter_by(work_id=work.id, purpose="TASK_EXECUTION").count() == 1


def test_report_recovery_materializes_persisted_outcome_without_second_report_call(ctx):
    from eason_one.models import OperationStep
    from eason_one.schemas import SYNTHESIS_SCHEMA
    from eason_one.services.ceo_context import compose
    from eason_one.services.execution import execute
    from eason_one.services.operations import _recover_open_step
    from eason_one.services.work_runtime import ensure_management_work

    researcher = Employee.query.filter_by(slug="researcher").one()
    operation, project = _operation([researcher])
    _start(operation)
    next_step(operation, "report-recovery-work")
    verification = next_step(operation, "report-recovery-verification")
    assert verification["overall_status"] == "SATISFIED"

    ceo = Employee.query.filter_by(slug="ceo").one()
    management_work = ensure_management_work(operation)
    composed = compose(
        ceo, founder_request="Produce the final Founder operation report.",
        operation=operation, project=project,
    )
    step = OperationStep(
        operation_id=operation.id,
        idempotency_key="crash-before-report-materialization",
        logical_key="report-recovery:final",
        kind="REPORT", status="EXECUTION_STARTED",
    )
    db.session.add(step); db.session.flush()
    run = execute(
        ceo, "CEO_OPERATION_REPORT",
        f"Report completion of operation #{operation.id}",
        project=project, operation=operation, work=management_work,
        context_override=composed.text,
        context_composition=composed.composition,
        system_prompt_override=(
            ceo.system_instructions
            + "\nCEO_PROJECT_SYNTHESIS\nReturn only strict JSON briefing fields."
        ),
        response_schema=SYNTHESIS_SCHEMA,
    )
    assert run.status == "SUCCEEDED"
    step.agent_run_id = run.id
    db.session.commit()

    # Simulate the startup stale-worker recovery state without invoking the provider again.
    kernel.transition(
        operation, "QUEUED", "TEST_SIMULATED_RESTART", stage="QUEUED",
        force=True,
    )
    recovered = _recover_open_step(operation)
    assert recovered["kind"] == "REPORT"
    assert recovered["recovered"] is True
    assert operation.status == "COMPLETED"
    assert db.session.get(Work, management_work.id).state == "ACCEPTED"
    assert AgentRun.query.filter_by(
        operation_id=operation.id, purpose="CEO_OPERATION_REPORT"
    ).count() == 1
    assert CompanyEvent.query.filter_by(
        project_id=project.id, event_type="PROJECT_OUTCOME_READY",
        execution_id=run.id,
    ).count() == 1


def test_v015_company_event_schema_migrates_without_losing_history(tmp_path):
    """A rolled-back v0.15 database used the same table name with another schema.

    Batch A must preserve that stream, create the vNext observation table, import
    the compatible envelope exactly once, and remain safe to run again.
    """
    from sqlalchemy import inspect, text
    from eason_one import _prepare_legacy_schema_collisions, _upgrade_v1_database

    db_path = tmp_path / "v015-company-event.db"
    config = {
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": f"sqlite:///{db_path}",
        "ALLOW_MOCK_PROVIDER": True,
    }
    app = create_app(config)
    with app.app_context():
        # Replace only the new event table with the exact rolled-back v0.15
        # shape; other current tables stay intact so this test isolates the
        # table-name collision that exists in real historical databases.
        db.session.execute(text("DROP TABLE company_event"))
        db.session.execute(text("""
            CREATE TABLE company_event (
                id INTEGER PRIMARY KEY,
                company_id INTEGER NOT NULL,
                event_type VARCHAR(80) NOT NULL,
                actor_type VARCHAR(30),
                actor_employee_id INTEGER,
                project_id INTEGER,
                object_type VARCHAR(40),
                object_id INTEGER,
                correlation_id VARCHAR(120),
                caused_by_event_id INTEGER,
                idempotency_key VARCHAR(160),
                institution_version INTEGER NOT NULL,
                payload_json JSON,
                created_at DATETIME
            )
        """))
        db.session.execute(text("""
            INSERT INTO company_event
              (id, company_id, event_type, actor_type, actor_employee_id,
               project_id, object_type, object_id, correlation_id,
               caused_by_event_id, idempotency_key, institution_version,
               payload_json, created_at)
            VALUES
              (41, 1, 'LEGACY_EVENT', 'EMPLOYEE', 7, 3, 'task', 11,
               'legacy-flow', NULL, 'legacy-41', 15, '{"legacy": true}',
               '2026-08-09 12:00:00')
        """))
        db.session.commit()

        _prepare_legacy_schema_collisions()
        db.create_all()
        _upgrade_v1_database()
        inspector = inspect(db.engine)
        assert "company_event" in inspector.get_table_names()
        assert "company_event_v015_legacy" in inspector.get_table_names()
        cols = {row["name"] for row in inspector.get_columns("company_event")}
        assert {"actor_id", "work_id", "execution_id", "causation_id", "schema_version"}.issubset(cols)

        imported = db.session.execute(text(
            "SELECT event_type, actor_type, actor_id, project_id, correlation_id, "
            "schema_version, payload_json FROM company_event WHERE id=41"
        )).mappings().one()
        assert imported["event_type"] == "LEGACY_EVENT"
        assert imported["actor_type"] == "EMPLOYEE"
        assert imported["actor_id"] == 7
        assert imported["project_id"] == 3
        assert imported["correlation_id"] == "legacy-flow"
        assert imported["schema_version"] == 0
        assert json.loads(imported["payload_json"])["legacy"] is True
        assert db.session.execute(text(
            "SELECT COUNT(*) FROM company_event_v015_legacy"
        )).scalar_one() == 1

        # Re-running the compatibility migration must not rename/copy again.
        _upgrade_v1_database()
        assert db.session.execute(text(
            "SELECT COUNT(*) FROM company_event WHERE id=41"
        )).scalar_one() == 1
        assert db.session.execute(text(
            "SELECT COUNT(*) FROM company_event_v015_legacy"
        )).scalar_one() == 1


def _persisted_management_run(operation, payload):
    from eason_one.services.work_runtime import ensure_management_work

    ceo = Employee.query.filter_by(slug="ceo").one()
    model = ceo.current_model
    management_work = ensure_management_work(operation)
    run = AgentRun(
        employee_id=ceo.id,
        project_id=operation.project_id,
        operation_id=operation.id,
        work_id=management_work.id,
        model_config_id=model.id,
        purpose="CEO_OPERATION_DECISION",
        user_request="Choose the next bounded internal action.",
        system_prompt_snapshot=ceo.system_instructions,
        context_snapshot="persisted management context",
        raw_output=json.dumps(payload),
        parsed_output_json=payload,
        status="SUCCEEDED",
        outcome="SUCCEEDED",
        provider_key_snapshot=model.provider_key,
        model_name_snapshot=model.model_name,
        input_price_snapshot=model.input_price_per_million,
        output_price_snapshot=model.output_price_per_million,
        currency_snapshot=model.currency,
        currency=model.currency,
    )
    db.session.add(run); db.session.flush()
    return run


def test_recovery_replays_continue_decision_without_second_ceo_call_or_founder(ctx):
    from eason_one.models import Decision, Escalation, OperationStep
    from eason_one.services.operations import _recover_open_step
    from eason_one.services.work_runtime import open_wait

    researcher = Employee.query.filter_by(slug="researcher").one()
    operation, _ = _operation([researcher])
    _start(operation)
    task = operation.tasks[0]
    work = db.session.get(Work, task.work_id)
    task.status = "BLOCKED"
    open_wait(work, "INTERNAL_RECOVERY", "Safe execution strategy was exhausted.")
    payload = {
        "action": "CONTINUE", "reason": "Retry with the approved internal strategy.",
        "goal_status": "IN_PROGRESS", "confidence": 0.8,
        "next_task_id": task.id, "meeting": None, "hiring_need": None,
        "founder_request": None, "task_plan": None,
    }
    run = _persisted_management_run(operation, payload)
    step = OperationStep(
        operation_id=operation.id,
        idempotency_key="recover-ceo-continue",
        logical_key="decision:recover-ceo-continue",
        kind="DECISION", status="EXECUTION_STARTED",
        task_id=task.id, agent_run_id=run.id,
    )
    db.session.add(step); db.session.commit()

    before = AgentRun.query.filter_by(
        operation_id=operation.id, purpose="CEO_OPERATION_DECISION"
    ).count()
    result = _recover_open_step(operation)
    assert result["status"] == "RECOVERED"
    assert result["action"] == "CONTINUE"
    db.session.expire_all()
    assert db.session.get(Task, task.id).status == "ASSIGNED"
    assert db.session.get(Work, work.id).state == "READY"
    assert WaitCondition.query.filter_by(work_id=work.id, state="OPEN").count() == 0
    assert AgentRun.query.filter_by(
        operation_id=operation.id, purpose="CEO_OPERATION_DECISION"
    ).count() == before
    assert Decision.query.filter_by(source_execution_id=run.id).count() == 1
    assert Escalation.query.filter_by(operation_id=operation.id, state="OPEN").count() == 0
    assert kernel.authoritative_status(db.session.get(Operation, operation.id)) == "RUNNING"


def test_management_meeting_side_effect_is_idempotent_by_decision_execution(ctx):
    from eason_one.models import Decision, Meeting
    from eason_one.services.operations import _apply_management_decision
    from eason_one.services.work_runtime import open_wait

    researcher = Employee.query.filter_by(slug="researcher").one()
    operation, _ = _operation([researcher])
    _start(operation)
    plan = dict(operation.plan_json)
    op_plan = dict(plan["operation"])
    op_plan["meeting_policy"] = "ON_MATERIAL_CONFLICT"
    op_plan["meeting_config"] = dict(op_plan["meeting_config"])
    op_plan["meeting_config"]["trigger"] = "ON_MATERIAL_CONFLICT"
    plan["operation"] = op_plan
    operation.plan_json = plan
    task = operation.tasks[0]
    work = db.session.get(Work, task.work_id)
    task.status = "BLOCKED"
    open_wait(work, "INTERNAL_RECOVERY", "Material conflict needs coordination.")
    payload = {
        "action": "MEETING", "reason": "Resolve a material evidence conflict.",
        "goal_status": "IN_PROGRESS", "confidence": 0.7,
        "next_task_id": None,
        "meeting": {"question": "Which evidence should govern?", "budget_twd": 0.5},
        "hiring_need": None, "founder_request": None, "task_plan": None,
    }
    run = _persisted_management_run(operation, payload)
    db.session.commit()

    first = _apply_management_decision(operation, run, payload, blocked_task=task)
    second = _apply_management_decision(operation, run, payload, blocked_task=task)
    assert first["meeting_id"] == second["meeting_id"]
    assert Meeting.query.filter_by(source_execution_id=run.id).count() == 1
    assert Decision.query.filter_by(source_execution_id=run.id).count() == 1


def test_management_hiring_request_is_idempotent_by_decision_execution(ctx):
    from eason_one.models import Decision, HiringRequest
    from eason_one.services.operations import _apply_management_decision

    researcher = Employee.query.filter_by(slug="researcher").one()
    operation, _ = _operation([researcher])
    _start(operation)
    payload = {
        "action": "HIRING_REQUEST", "reason": "A missing capability blocks repeated Projects.",
        "goal_status": "IN_PROGRESS", "confidence": 0.75,
        "next_task_id": None, "meeting": None,
        "hiring_need": {
            "role_needed": "Data Specialist",
            "problem": "The current team lacks a required data capability.",
            "why_now": "The capability is required for the approved Project.",
            "responsibilities": ["Validate project data"],
            "capabilities": ["Data analysis"],
            "urgency": "MEDIUM",
            "use_frequency": "RECURRING",
        },
        "founder_request": None, "task_plan": None,
    }
    run = _persisted_management_run(operation, payload)
    db.session.commit()

    first = _apply_management_decision(operation, run, payload)
    second = _apply_management_decision(operation, run, payload)
    assert first["hiring_request_id"] == second["hiring_request_id"]
    assert HiringRequest.query.filter_by(source_execution_id=run.id).count() == 1
    assert Decision.query.filter_by(source_execution_id=run.id).count() == 1


def test_work_scoped_meeting_retry_exhaustion_returns_to_internal_replan_not_founder(ctx):
    from eason_one.models import Escalation, Meeting, OperationStep
    from eason_one.services.operations import (
        _apply_management_decision, _handle_work_scoped_meeting_failure,
    )

    researcher = Employee.query.filter_by(slug="researcher").one()
    operation, _ = _operation([researcher])
    _start(operation)
    plan = dict(operation.plan_json)
    op_plan = dict(plan["operation"])
    op_plan["meeting_policy"] = "ON_MATERIAL_CONFLICT"
    op_plan["meeting_config"] = dict(op_plan["meeting_config"])
    op_plan["meeting_config"]["trigger"] = "ON_MATERIAL_CONFLICT"
    op_plan["meeting_config"]["retry_limit"] = 0
    plan["operation"] = op_plan
    operation.plan_json = plan
    task = operation.tasks[0]
    work = db.session.get(Work, task.work_id)
    task.status = "BLOCKED"
    payload = {
        "action": "MEETING", "reason": "Resolve the blocked Work conflict.",
        "goal_status": "IN_PROGRESS", "confidence": 0.7,
        "next_task_id": None,
        "meeting": {"question": "Can the evidence conflict be resolved?", "budget_twd": 0.5},
        "hiring_need": None, "founder_request": None, "task_plan": None,
    }
    decision_run = _persisted_management_run(operation, payload)
    result = _apply_management_decision(operation, decision_run, payload, blocked_task=task)
    meeting = db.session.get(Meeting, result["meeting_id"])
    assert meeting.related_work_id == work.id

    model = researcher.current_model
    failed_run = AgentRun(
        employee_id=researcher.id, project_id=operation.project_id,
        operation_id=operation.id, work_id=work.id, meeting_id=meeting.id,
        model_config_id=model.id, purpose="MEETING_CONTRIBUTION",
        user_request="Contribute to the Work-scoped Meeting.",
        system_prompt_snapshot=researcher.system_instructions,
        context_snapshot="meeting recovery test", raw_output="{}",
        status="FAILED", outcome="FAILED_KNOWN", failure_reason="TEST_FAILURE",
        real_cost=Decimal("0.01"), currency=model.currency,
        provider_key_snapshot=model.provider_key, model_name_snapshot=model.model_name,
        input_price_snapshot=model.input_price_per_million,
        output_price_snapshot=model.output_price_per_million,
        currency_snapshot=model.currency,
    )
    db.session.add(failed_run); db.session.flush()
    meeting.status = "PAUSED"
    meeting.kernel_status = "WAITING_FOR_INPUTS"
    meeting.current_stage = "WAITING_FOR_INPUTS"
    meeting.paid_failure_json = {
        "run_id": failed_run.id, "reason": "TEST_FAILURE", "retry_strategy": "REPLAY"
    }
    step = OperationStep(
        operation_id=operation.id, idempotency_key="meeting-retry-exhausted",
        logical_key="meeting-step:retry-exhausted", kind="MEETING_STEP",
        status="EXECUTION_STARTED", agent_run_id=failed_run.id,
    )
    db.session.add(step); db.session.commit()

    handled = _handle_work_scoped_meeting_failure(operation, meeting, step, failed_run)
    assert handled["status"] == "WORK_WAITING"
    db.session.expire_all()
    work = db.session.get(Work, work.id)
    meeting = db.session.get(Meeting, meeting.id)
    assert work.state == "WAITING"
    wait = WaitCondition.query.filter_by(
        work_id=work.id, condition_type="INTERNAL_RECOVERY", state="OPEN"
    ).one()
    assert "Meeting" in wait.reason
    assert db.session.get(Task, task.id).status == "BLOCKED"
    assert meeting.status == "ENDED"
    assert meeting.kernel_status == "CANCELLED"
    assert Escalation.query.filter_by(operation_id=operation.id, state="OPEN").count() == 0
    assert kernel.authoritative_status(db.session.get(Operation, operation.id)) == "RUNNING"


def test_v015_artifact_and_decision_collisions_preserve_and_import_truth(tmp_path):
    """Rolled-back v0.15 Artifact/Decision tables must not poison vNext ORM."""
    import sqlite3

    db_path = tmp_path / "v015-artifact-decision.db"
    config = {
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": f"sqlite:///{db_path}",
        "ALLOW_MOCK_PROVIDER": True,
    }
    app1 = create_app(config)
    with app1.app_context():
        db.drop_all(); db.create_all(); seed()
        researcher = Employee.query.filter_by(slug="researcher").one()
        operation, project = _operation([researcher])
        task = operation.tasks[0]
        researcher_id = researcher.id
        project_id, task_id, work_id = project.id, task.id, task.work_id
        db.session.commit(); db.session.remove()
        db.engine.dispose()

    # Recreate the exact conflicting table names outside SQLAlchemy, as a real
    # rolled-back DB would have them before the Batch A app starts.
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA foreign_keys=OFF")
    for table in ("verification_record", "artifact_version", "artifact", "decision"):
        con.execute(f"DROP TABLE IF EXISTS {table}")
    con.execute("""
        CREATE TABLE artifact (
            id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL, work_item_id INTEGER,
            legacy_task_id INTEGER, source_agent_run_id INTEGER, artifact_type VARCHAR(40) NOT NULL,
            title VARCHAR(220) NOT NULL, summary TEXT NOT NULL, version INTEGER NOT NULL,
            status VARCHAR(24) NOT NULL, creator_employee_id INTEGER NOT NULL,
            content_ref VARCHAR(500), content_json JSON, verification_status VARCHAR(24) NOT NULL,
            verification_note TEXT, acceptance_status VARCHAR(24) NOT NULL, acceptance_note TEXT,
            verified_at DATETIME, accepted_at DATETIME, updated_at DATETIME NOT NULL, created_at DATETIME NOT NULL
        )
    """)
    con.execute("""
        CREATE TABLE decision (
            id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL, meeting_id INTEGER,
            source_work_item_id INTEGER, title VARCHAR(220) NOT NULL, question TEXT,
            status VARCHAR(24) NOT NULL, decision_maker_type VARCHAR(30) NOT NULL,
            decision_maker_employee_id INTEGER, authority_source VARCHAR(220) NOT NULL,
            chosen_action TEXT NOT NULL, reason TEXT NOT NULL, outcome_summary TEXT,
            made_at DATETIME NOT NULL, created_at DATETIME NOT NULL
        )
    """)
    con.execute("CREATE TABLE IF NOT EXISTS work_item (id INTEGER PRIMARY KEY, legacy_task_id INTEGER)")
    con.execute("INSERT INTO work_item(id,legacy_task_id) VALUES(901,?)", (task_id,))
    con.execute("""
        INSERT INTO artifact
          (id,project_id,work_item_id,legacy_task_id,source_agent_run_id,artifact_type,title,summary,
           version,status,creator_employee_id,content_ref,content_json,verification_status,
           verification_note,acceptance_status,acceptance_note,verified_at,accepted_at,updated_at,created_at)
        VALUES(701,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        project_id, 901, task_id, None, "WORK_RESULT", "Legacy result", "usable result",
        1, "ACCEPTED", researcher_id, None, '{"fact": true}', "VERIFIED",
        "legacy verified", "ACCEPTED", "legacy accepted",
        "2026-08-09 12:00:00", "2026-08-09 12:01:00",
        "2026-08-09 12:01:00", "2026-08-09 12:00:00",
    ))
    con.execute("""
        INSERT INTO decision
          (id,project_id,meeting_id,source_work_item_id,title,question,status,
           decision_maker_type,decision_maker_employee_id,authority_source,chosen_action,
           reason,outcome_summary,made_at,created_at)
        VALUES(801,?,NULL,901,'Legacy decision','Ship?', 'RECORDED','EMPLOYEE',?,
               'Approved project authority','WAIT_FOR_VERIFIED_EVIDENCE','Need evidence',
               'Evidence required','2026-08-09 12:02:00','2026-08-09 12:02:00')
    """, (project_id, researcher_id))
    con.commit(); con.close()

    app2 = create_app(config)
    with app2.app_context():
        from eason_one.models import Decision, VerificationRecord
        artifact = Artifact.query.filter_by(
            legacy_source="v015_artifact", legacy_source_id=701
        ).one()
        version = ArtifactVersion.query.filter_by(artifact_id=artifact.id).one()
        decision = Decision.query.filter_by(
            legacy_source="v015_decision", legacy_source_id=801
        ).one()
        assert artifact.work_id == work_id
        assert version.status == "ACCEPTED"
        assert decision.work_id == work_id
        assert decision.decision == "WAIT_FOR_VERIFIED_EVIDENCE"
        assert VerificationRecord.query.filter_by(
            artifact_version_id=version.id, method="LEGACY_V015_IMPORT"
        ).count() == 1
        assert CompanyEvent.query.filter_by(event_type="ARTIFACT_IMPORTED").count() == 1
        assert CompanyEvent.query.filter_by(event_type="DECISION_IMPORTED").count() == 1
        db.session.remove(); db.engine.dispose()

    con = sqlite3.connect(db_path)
    tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "artifact_v015_legacy" in tables
    assert "decision_v015_legacy" in tables
    con.close()

    app3 = create_app(config)
    with app3.app_context():
        from eason_one.models import Decision
        assert Artifact.query.filter_by(legacy_source="v015_artifact", legacy_source_id=701).count() == 1
        assert Decision.query.filter_by(legacy_source="v015_decision", legacy_source_id=801).count() == 1
        assert CompanyEvent.query.filter_by(event_type="ARTIFACT_IMPORTED").count() == 1
        assert CompanyEvent.query.filter_by(event_type="DECISION_IMPORTED").count() == 1


def test_vnext_actual_call_estimate_is_not_vetoed_by_legacy_single_or_stage_caps(ctx, monkeypatch):
    """Regression for live Run #95 class of failure.

    A stale planning-time Operation single-call/stage estimate may not veto an
    Execution that belongs to a Work and still fits the approved Project/Work
    authority. The durable reservation must use actual execution cost truth.
    """
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    researcher = Employee.query.filter_by(slug="researcher").one()
    real = ModelConfig(
        label="vNext authority test", provider_key="openai", model_name="vnext-test",
        input_price_per_million=Decimal("8"), output_price_per_million=Decimal("16"),
        currency="TWD", max_output_tokens=4096, active=True, archived=False,
    )
    db.session.add(real); db.session.flush()
    researcher.current_model_config_id = real.id
    db.session.commit()

    operation, _ = _operation([researcher])
    task = operation.tasks[0]
    work = db.session.get(Work, task.work_id)
    operation.approved_budget_twd = Decimal("10")
    operation.hard_cost_cap_twd = Decimal("10")
    operation.single_call_cost_cap_twd = Decimal("0.0001")
    operation.stage_cost_cap_twd = Decimal("0.0001")
    memory = dict(operation.memory_json or {})
    memory["stage_budget_caps"] = {"TASK_EXECUTION": "0.0001"}
    operation.memory_json = memory
    work.resource_ceiling_twd = Decimal("10")
    db.session.commit()
    _start(operation)

    class Provider:
        def complete(self, *args, **kwargs):
            payload = {"result_summary": "The bounded Work completed.", "knowledge_proposals": []}
            return ProviderResult(json.dumps(payload), 100, 50, request_id="rq-vnext", response_id="rs-vnext")

    monkeypatch.setattr("eason_one.services.execution.get_provider", lambda key: Provider())
    result = next_step(operation, "vnext-real-estimate-over-legacy-cap")
    assert result["status"] == "SUCCEEDED"
    db.session.expire_all()
    work = db.session.get(Work, work.id)
    operation = db.session.get(Operation, operation.id)
    assert work.state == "ACCEPTED"
    assert kernel.authoritative_status(operation) == "RUNNING"
    assert operation.founder_report_json is None
    from eason_one.models import Escalation
    assert Escalation.query.filter_by(operation_id=operation.id, state="OPEN").count() == 0


def test_management_safe_failure_stays_internal_and_never_manufactures_founder_gate(ctx, monkeypatch):
    from eason_one.models import Escalation
    from eason_one.services.operations import _decision_step
    from eason_one.services.work_runtime import open_wait

    researcher = Employee.query.filter_by(slug="researcher").one()
    operation, _ = _operation([researcher])
    _start(operation)
    task = operation.tasks[0]
    work = db.session.get(Work, task.work_id)
    task.status = "BLOCKED"
    open_wait(work, "INTERNAL_RECOVERY", "Force a management replan.")
    db.session.commit()

    def fail_preflight(*args, **kwargs):
        raise ValueError("synthetic configured-provider preflight failure")

    monkeypatch.setattr("eason_one.services.execution.provider_preflight", fail_preflight)
    result = _decision_step(operation, "vnext-management-safe-failure", blocked_task=task)
    assert result["status"] == "WAITING_INTERNAL"
    db.session.expire_all()
    operation = db.session.get(Operation, operation.id)
    assert kernel.authoritative_status(operation) == "WAITING_INPUT"
    assert operation.founder_report_json is None
    assert Escalation.query.filter_by(operation_id=operation.id, state="OPEN").count() == 0
    management = Work.query.filter_by(operation_id=operation.id, work_type="MANAGEMENT").one()
    assert management.state == "WAITING"
    assert WaitCondition.query.filter_by(work_id=management.id, state="OPEN").count() >= 1


def test_vnext_unhandled_runtime_error_pauses_internal_recovery_not_founder(ctx, monkeypatch):
    from eason_one.models import Escalation
    from eason_one.services.operation_runtime import run_until_gate

    researcher = Employee.query.filter_by(slug="researcher").one()
    operation, _ = _operation([researcher])
    # Leave QUEUED so run_until_gate owns the normal durable startup transition.

    def crash(*args, **kwargs):
        raise RuntimeError("synthetic internal runtime defect")

    monkeypatch.setattr("eason_one.services.operations.next_step", crash)
    snapshot = run_until_gate(operation.id, max_steps=1)
    db.session.expire_all()
    operation = db.session.get(Operation, operation.id)
    assert snapshot["kernel_status"] == "WAITING_INPUT"
    assert kernel.authoritative_status(operation) == "WAITING_INPUT"
    assert operation.founder_report_json is None
    assert Escalation.query.filter_by(operation_id=operation.id, state="OPEN").count() == 0
    management = Work.query.filter_by(operation_id=operation.id, work_type="MANAGEMENT").one()
    assert WaitCondition.query.filter_by(work_id=management.id, state="OPEN").count() >= 1


def test_live_run95_regression_ceo_decision_uses_vnext_authority_not_stale_single_call_cap(ctx, monkeypatch):
    """The exact live failure class: CEO_OPERATION_DECISION was preflight-blocked
    with provider calls still at zero because a stale Operation single-call cap
    overruled already-approved Project authority.
    """
    from eason_one.models import Escalation
    from eason_one.services.work_runtime import open_wait

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    ceo = Employee.query.filter_by(slug="ceo").one()
    researcher = Employee.query.filter_by(slug="researcher").one()
    real = ModelConfig(
        label="Luna regression", provider_key="openai", model_name="gpt-5.6-luna-test",
        input_price_per_million=Decimal("8"), output_price_per_million=Decimal("16"),
        currency="TWD", max_output_tokens=4096, active=True, archived=False,
    )
    db.session.add(real); db.session.flush()
    ceo.current_model_config_id = real.id
    researcher.current_model_config_id = real.id
    db.session.commit()

    operation, _ = _operation([researcher])
    operation.approved_budget_twd = Decimal("10")
    operation.hard_cost_cap_twd = Decimal("10")
    operation.single_call_cost_cap_twd = Decimal("0.0001")
    operation.stage_cost_cap_twd = Decimal("0.0001")
    task = operation.tasks[0]
    work = db.session.get(Work, task.work_id)
    work.resource_ceiling_twd = Decimal("10")
    task.status = "BLOCKED"
    open_wait(work, "INTERNAL_RECOVERY", "Simulate a safe branch blocker requiring CEO replan.")
    memory = dict(operation.memory_json or {})
    memory["stage_budget_caps"] = {"CEO_OPERATION_DECISION": "0.0001"}
    operation.memory_json = memory
    db.session.commit()
    _start(operation)

    payload = {
        "action": "CONTINUE",
        "reason": "Resume the bounded Work under existing Project authority.",
        "goal_status": "IN_PROGRESS",
        "confidence": 0.9,
        "next_task_id": task.id,
        "meeting": None,
        "hiring_need": None,
        "founder_request": None,
        "task_plan": None,
    }

    class Provider:
        def complete(self, *args, **kwargs):
            return ProviderResult(json.dumps(payload), 120, 80, request_id="rq-95", response_id="rs-95")

    monkeypatch.setattr("eason_one.services.execution.get_provider", lambda key: Provider())
    result = next_step(operation, "run95-regression")
    assert result["kind"] == "DECISION"
    assert result["action"] == "CONTINUE"
    db.session.expire_all()
    operation = db.session.get(Operation, operation.id)
    task = db.session.get(Task, task.id)
    work = db.session.get(Work, work.id)
    run = AgentRun.query.filter_by(operation_id=operation.id, purpose="CEO_OPERATION_DECISION").one()
    assert run.status == "SUCCEEDED"
    assert run.work_id is not None
    assert run.input_tokens > 0 and run.output_tokens > 0
    assert task.status == "ASSIGNED"
    assert work.state == "READY"
    assert kernel.authoritative_status(operation) == "RUNNING"
    assert operation.founder_report_json is None
    assert Escalation.query.filter_by(operation_id=operation.id, state="OPEN").count() == 0


def test_vnext_authority_admission_is_not_reapplied_as_full_budget_after_spend(ctx):
    from eason_one.models import CostEvent
    from eason_one.services.company import get_company
    from eason_one.services.operations import ensure_full_execution_authority

    researcher = Employee.query.filter_by(slug="researcher").one()
    operation, project = _operation([researcher])
    _start(operation)
    task = operation.tasks[0]
    work = db.session.get(Work, task.work_id)
    operation.approved_budget_twd = Decimal("1.6000")
    operation.hard_cost_cap_twd = Decimal("1.6000")
    operation.single_call_cost_cap_twd = Decimal("0.3000")
    operation.stage_cost_cap_twd = Decimal("0.3000")
    project.real_budget_limit = Decimal("1.6000")
    work.resource_ceiling_twd = Decimal("1.6000")

    model = researcher.current_model
    run = AgentRun(
        employee_id=researcher.id, project_id=project.id, operation_id=operation.id,
        task_id=task.id, work_id=work.id, model_config_id=model.id,
        purpose="TASK_EXECUTION", user_request="historical paid work",
        system_prompt_snapshot="x", context_snapshot="x", raw_output="done",
        status="SUCCEEDED", outcome="SUCCEEDED", provider_key_snapshot=model.provider_key,
        model_name_snapshot=model.model_name, input_price_snapshot=0, output_price_snapshot=0,
        currency_snapshot="TWD", currency="TWD", real_cost=Decimal("0.3000"),
    )
    db.session.add(run); db.session.flush()
    db.session.add(CostEvent(
        company_id=get_company().id, employee_id=researcher.id, project_id=project.id,
        task_id=task.id, agent_run_id=run.id, operation_id=operation.id, work_id=work.id,
        stage="TASK_EXECUTION", category="MODEL", description="already spent",
        internal_credits_delta=0, real_cost_delta=Decimal("0.3000"), currency="TWD",
    ))
    db.session.commit()

    assert ensure_full_execution_authority(operation) is True
    db.session.expire_all()
    operation = db.session.get(Operation, operation.id)
    assert kernel.authoritative_status(operation) == "RUNNING"
    assert operation.founder_report_json is None
    # Compatibility caps are normalized, but no extra Founder authority was invented.
    assert Decimal(operation.approved_budget_twd) == Decimal("1.6000")
    assert Decimal(operation.single_call_cost_cap_twd) == Decimal("1.6000")


def test_vnext_serial_scheduler_uses_work_truth_when_legacy_task_status_is_stale(ctx):
    """A stale Task=BLOCKED label may not veto authoritative Work=READY."""
    researcher = Employee.query.filter_by(slug="researcher").one()
    operation, _ = _operation([researcher])
    task = operation.tasks[0]
    work = db.session.get(Work, task.work_id)
    assert work.state == "READY"

    # Reproduce split-brain legacy state without changing Company Core truth.
    task.status = "BLOCKED"
    db.session.commit()
    _start(operation)

    result = next_step(operation, "closure-inc1-stale-task")
    assert result["kind"] == "TASK"
    assert result["status"] == "SUCCEEDED"
    db.session.expire_all()
    task = db.session.get(Task, task.id)
    work = db.session.get(Work, work.id)
    assert work.state == "ACCEPTED"
    assert task.status == "DONE"  # one-way compatibility projection


def test_vnext_dependency_scheduler_uses_work_dependency_not_legacy_task_done(ctx):
    """Upstream Work acceptance, not stale Task status, releases downstream Work."""
    researcher = Employee.query.filter_by(slug="researcher").one()
    critic = Employee.query.filter_by(slug="critic").one()
    operation, _ = _operation([researcher, critic])
    tasks = list(operation.tasks)
    multi_agent.persist_plan(operation, {
        "strategy": "PARALLEL_DAG",
        "rationale": "Second branch consumes the first branch.",
        "max_parallelism": 2,
        "tasks": [
            {"task_id": tasks[0].id, "depends_on_task_ids": [], "role": "WORKER", "reason": "upstream"},
            {"task_id": tasks[1].id, "depends_on_task_ids": [tasks[0].id], "role": "WORKER", "reason": "downstream"},
        ],
    })
    upstream = db.session.get(Work, tasks[0].work_id)
    downstream = db.session.get(Work, tasks[1].work_id)
    from eason_one.services.work_runtime import transition as transition_work
    transition_work(upstream, "EXECUTING", reason="Fixture upstream started")
    transition_work(upstream, "ACCEPTED", reason="Fixture upstream accepted")
    tasks[0].status = "BLOCKED"  # intentionally stale adapter
    db.session.commit()

    ready = multi_agent.ready_tasks(operation)
    assert [task.id for task in ready] == [tasks[1].id]
    assert downstream.state == "READY"


def test_resume_queued_operations_attaches_only_authorized_company_work(ctx, monkeypatch):
    from eason_one.services import operation_runtime

    researcher = Employee.query.filter_by(slug="researcher").one()
    operation, _ = _operation([researcher])
    assert kernel.authoritative_status(operation) == "QUEUED"
    assert operation.approved_at is not None

    started = []
    monkeypatch.setattr(
        operation_runtime,
        "start_background",
        lambda operation_id: started.append(operation_id) or {"state": "READY"},
    )
    resumed = operation_runtime.resume_queued_operations()
    assert resumed == [operation.id]
    assert started == [operation.id]


def test_create_app_production_startup_invokes_durable_queue_resume(tmp_path, monkeypatch):
    from eason_one.services import operation_runtime

    calls = []
    monkeypatch.setattr(
        operation_runtime,
        "resume_queued_operations",
        lambda: calls.append("resume") or [],
    )
    app = create_app({
        "TESTING": True,
        "AUTO_START_OPERATION_RUNTIME": True,
        "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'startup-resume.db'}",
        "ALLOW_MOCK_PROVIDER": True,
    })
    assert app is not None
    assert calls == ["resume"]


def test_no_reviewer_verification_records_only_deterministic_scope(ctx):
    from eason_one.models import VerificationRecord

    researcher = Employee.query.filter_by(slug="researcher").one()
    operation, _ = _operation([researcher])
    _start(operation)
    result = next_step(operation, "closure-inc1-truthful-no-review")
    assert result["status"] == "SUCCEEDED"
    assert result["review"] == "DETERMINISTIC_EXECUTION_VALIDATION"

    work = db.session.get(Work, operation.tasks[0].work_id)
    version = ArtifactVersion.query.join(Artifact).filter(Artifact.work_id == work.id).one()
    record = VerificationRecord.query.filter_by(
        work_id=work.id, artifact_version_id=version.id, status="PASSED"
    ).one()
    assert record.method == "DETERMINISTIC_EXECUTION_VALIDATION"
    assert record.details_json["scope"] == "execution_schema_and_durable_artifact_integrity"
    assert record.details_json["semantic_acceptance_criteria_verified"] is False


def test_durable_runtime_reaches_result_ready_without_founder_orchestration(ctx):
    from eason_one.models import Escalation
    from eason_one.services.operation_runtime import run_until_gate

    researcher = Employee.query.filter_by(slug="researcher").one()
    operation, project = _operation([researcher])
    snapshot = run_until_gate(operation.id, max_steps=12)

    db.session.expire_all()
    operation = db.session.get(Operation, operation.id)
    project = db.session.get(Project, project.id)
    delivery = [work for work in operation.works if work.work_type != "MANAGEMENT"]
    management = [work for work in operation.works if work.work_type == "MANAGEMENT"]

    assert snapshot["kernel_status"] == "COMPLETED"
    assert operation.status == "COMPLETED"
    assert project.status == "REVIEW"  # result-ready; Founder acceptance is a separate authority boundary
    assert delivery and all(work.state == "ACCEPTED" for work in delivery)
    assert management and all(work.state == "ACCEPTED" for work in management)
    assert Artifact.query.filter_by(project_id=project.id).count() >= 2
    assert Escalation.query.filter_by(project_id=project.id, state="OPEN").count() == 0
