from decimal import Decimal

from eason_one.extensions import db
from eason_one.models import AgentRun, Employee, Operation, Project, WaitCondition, Work
from eason_one.services import company_runtime, operations, stabilization, work_budget, work_runtime
from eason_one.services.core_v018 import RUNTIME_SEMANTICS


def _approved_v018_operation():
    ceo = Employee.query.filter_by(slug="ceo").one()
    researcher = Employee.query.filter_by(slug="researcher").one()
    project = Project(
        name="v0.18 Core Cutover",
        objective="Prove Work owns execution truth.",
        status="ACTIVE",
        priority="HIGH",
        environment="LIVE",
        origin="TEST",
        owner_employee_id=ceo.id,
        real_budget_limit=Decimal("1000"),
    )
    db.session.add(project)
    db.session.flush()
    # v0.20 diagnostic fixture: every governed Project must have explicit
    # Founder-owned completion authority before a Mission can be approved.
    __import__("eason_one.services.project_contract", fromlist=["freeze"]).freeze(
        project, success_criteria=["A verified Artifact exists."],
        constraints=[], origin_employee_id=ceo.id,
    )
    plan = {
        "mode": "OPERATION_PLAN",
        "executive_response": "Run one bounded Work.",
        "operation": {
            "title": "v0.18 bounded Work",
            "objective": "Exercise the Work-first control plane.",
            "project_id": project.id,
            "budget_twd": 100,
            "tasks": [{
                "title": "Produce one result",
                "objective": "Produce a bounded result.",
                "assignee_employee_id": researcher.id,
                "reviewer_employee_id": None,
                "acceptance_criteria": ["A verified Artifact exists."],
            }],
            "meeting_policy": "NEVER",
            "meeting_config": {
                "trigger": "NEVER",
                "participant_employee_ids": [],
                "max_rounds": 1,
                "max_speakers_per_round": 1,
                "contribution_output_cap": 192,
                "token_limit": 6000,
                "budget_twd": 0,
                "retry_limit": 0,
            },
            "completion_criteria": ["The Work is accepted."],
        },
    }
    operation = operations.propose_operation(ceo, plan, route_type="FULL_PROJECT")
    operations.approve(operation)
    db.session.refresh(operation)
    return operation, project


def test_v018_approval_moves_governing_gates_onto_work(ctx):
    operation, _ = _approved_v018_operation()
    assert (operation.memory_json or {}).get("runtime_semantics") == RUNTIME_SEMANTICS
    work = next(row for row in operation.works if row.work_type != "MANAGEMENT")
    task = work_runtime.task_for_work(work)

    work_runtime.open_wait(work, "FOUNDER_DECISION", "Founder paused the Work.")
    db.session.commit()
    db.session.refresh(work)
    db.session.refresh(task)

    assert work.state == "WAITING"
    assert task.status == "BLOCKED"  # compatibility projection only
    assert work_runtime.has_open_gate(work, "FOUNDER_DECISION")
    assert WaitCondition.query.filter_by(work_id=work.id, state="OPEN").count() == 0
    assert (work.runtime_control_json or {}).get("version") == RUNTIME_SEMANTICS

    work_runtime.resolve_waits(work, "FOUNDER_DECISION", note="Founder resumed.")
    db.session.commit()
    db.session.refresh(work)
    db.session.refresh(task)
    assert work.state == "READY"
    assert task.status == "ASSIGNED"


def test_task_status_cannot_veto_v018_scheduler(ctx):
    operation, _ = _approved_v018_operation()
    work = next(row for row in operation.works if row.work_type != "MANAGEMENT")
    task = work_runtime.task_for_work(work)
    task.status = "FAILED"
    db.session.commit()

    selected = company_runtime._eligible_work()
    assert selected is not None
    assert selected.id == work.id
    assert selected.state == "READY"


def test_v018_project_budget_is_only_founder_hard_execution_envelope(ctx):
    operation, project = _approved_v018_operation()
    work = next(row for row in operation.works if row.work_type != "MANAGEMENT")
    operation.approved_budget_twd = Decimal("0.01")
    operation.hard_cost_cap_twd = Decimal("0.01")
    work.resource_ceiling_twd = Decimal("0.01")
    project.real_budget_limit = Decimal("1000")
    db.session.commit()

    assert work_budget.ensure(work, Decimal("1.00")) is True


def test_founder_pause_resume_uses_manual_control_gate_not_governance(ctx):
    operation, _ = _approved_v018_operation()
    work = next(row for row in operation.works if row.work_type != "MANAGEMENT")

    operations.pause(operation)
    db.session.refresh(work)
    assert work.state == "WAITING"
    # Manual pause is a Founder control checkpoint, not new Founder authority.
    assert work_runtime.has_open_gate(work, "FOUNDER_PAUSE")
    assert not work_runtime.has_open_gate(work, "FOUNDER_DECISION")
    assert WaitCondition.query.filter_by(work_id=work.id, state="OPEN").count() == 0

    operations.resume(operation)
    db.session.refresh(work)
    assert work.state == "READY"
    assert not work_runtime.has_open_gate(work, "FOUNDER_PAUSE")
    assert not work_runtime.has_open_gate(work, "FOUNDER_DECISION")


def test_old_running_agent_run_cannot_appear_as_current_company_work(ctx):
    operation, _ = _approved_v018_operation()
    work = next(row for row in operation.works if row.work_type != "MANAGEMENT")
    employee = work_runtime.active_assignment(work).employee
    model = employee.current_model

    old_project = Project(
        name="Historical Project",
        objective="Must stay historical.",
        status="ACTIVE",
        priority="MEDIUM",
        environment="LIVE",
        origin="TEST",
        owner_employee_id=Employee.query.filter_by(slug="ceo").one().id,
        real_budget_limit=Decimal("10"),
    )
    db.session.add(old_project)
    db.session.flush()
    old_operation = Operation(
        title="Historical v0.17 Operation",
        objective="Historical",
        status="RUNNING",
        kernel_status="RUNNING",
        route_type="FULL_PROJECT",
        project_id=old_project.id,
        proposed_by_employee_id=Employee.query.filter_by(slug="ceo").one().id,
        approved_at=operation.approved_at,
        approved_budget_twd=Decimal("10"),
        hard_cost_cap_twd=Decimal("10"),
        stage_cost_cap_twd=Decimal("10"),
        single_call_cost_cap_twd=Decimal("10"),
        max_calls=1,
        max_revisions=0,
        max_messages=1,
        max_elapsed_seconds=60,
        plan_json={"mode": "LEGACY_TEST", "operation": {}},
        memory_json={"runtime_semantics": "WORK_VNEXT"},
    )
    db.session.add(old_operation)
    db.session.flush()
    old_run = AgentRun(
        employee_id=employee.id,
        project_id=old_project.id,
        operation_id=old_operation.id,
        model_config_id=model.id,
        purpose="TASK_EXECUTION",
        user_request="historical",
        system_prompt_snapshot="historical",
        context_snapshot="historical",
        status="RUNNING",
        provider_key_snapshot=model.provider_key,
        model_name_snapshot=model.model_name,
        input_price_snapshot=model.input_price_per_million,
        output_price_snapshot=model.output_price_per_million,
        currency_snapshot=model.currency,
        currency=model.currency,
    )
    db.session.add(old_run)
    db.session.commit()

    # No current v0.18 run exists, therefore the historical RUNNING row must not
    # manufacture a global "working" employee or runtime banner.
    assert stabilization.active_execution_run() is None


def test_safe_migration_is_one_time_once_v018_exists(ctx):
    _approved_v018_operation()
    ceo = Employee.query.filter_by(slug="ceo").one()
    legacy_project = Project(
        name="Older v0.17",
        objective="Remain dormant.",
        status="ACTIVE",
        priority="LOW",
        environment="LIVE",
        origin="TEST",
        owner_employee_id=ceo.id,
    )
    db.session.add(legacy_project)
    db.session.flush()
    legacy = Operation(
        title="Older v0.17",
        objective="Remain dormant",
        status="RUNNING",
        kernel_status="RUNNING",
        route_type="FULL_PROJECT",
        project_id=legacy_project.id,
        proposed_by_employee_id=ceo.id,
        approved_at=operation_time(),
        approved_budget_twd=Decimal("1"),
        hard_cost_cap_twd=Decimal("1"),
        stage_cost_cap_twd=Decimal("1"),
        single_call_cost_cap_twd=Decimal("1"),
        max_calls=1,
        max_revisions=0,
        max_messages=1,
        max_elapsed_seconds=60,
        plan_json={"mode": "LEGACY_TEST", "operation": {}},
        memory_json={"runtime_semantics": "WORK_VNEXT"},
    )
    db.session.add(legacy)
    db.session.commit()

    assert company_runtime.migrate_latest_v017_candidate() is None
    db.session.refresh(legacy)
    assert (legacy.memory_json or {}).get("runtime_semantics") == "WORK_VNEXT"


def operation_time():
    from eason_one.models import now
    return now()
