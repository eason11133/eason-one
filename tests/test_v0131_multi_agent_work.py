from decimal import Decimal

from eason_one.extensions import db
from eason_one.models import AgentRun, Employee, ModelConfig, Operation, OperationStep, Project, Task
from eason_one.services import operation_kernel as kernel
from eason_one.services import multi_agent
from eason_one.services.context import operation_context
from eason_one.services.operations import approve, next_step, propose_operation, ensure_full_execution_authority, recover_multi_agent_preprovider_gate, wait_for_founder
from eason_one.services.project_company import project_snapshot


def _plan(project, ceo, researcher, critic):
    return {
        "mode": "OPERATION_PLAN",
        "executive_response": "Run two independent specialist branches, then continue only from persisted evidence.",
        "operation": {
            "title": "Multi-Agent bounded work",
            "objective": "Test useful parallel specialist execution without changing approved scope.",
            "project_id": project.id,
            "budget_twd": 10,
            "tasks": [
                {
                    "title": "Research branch",
                    "objective": "Produce one bounded evidence branch.",
                    "assignee_employee_id": researcher.id,
                    "reviewer_employee_id": None,
                    "acceptance_criteria": ["A persisted result exists."],
                },
                {
                    "title": "Critical branch",
                    "objective": "Independently challenge the problem.",
                    "assignee_employee_id": critic.id,
                    "reviewer_employee_id": None,
                    "acceptance_criteria": ["A persisted critical result exists."],
                },
                {
                    "title": "Follow-up branch",
                    "objective": "Use only the declared upstream evidence.",
                    "assignee_employee_id": researcher.id,
                    "reviewer_employee_id": None,
                    "acceptance_criteria": ["The declared dependency is used."],
                },
            ],
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
            "completion_criteria": ["The bounded multi-agent work is complete."],
        },
    }


def _setup_operation():
    ceo = Employee.query.filter_by(slug="ceo").one()
    researcher = Employee.query.filter_by(slug="researcher").one()
    critic = Employee.query.filter_by(slug="critic").one()
    project = Project(
        name="Multi-Agent Project",
        objective="Run real bounded parallel work.",
        status="ACTIVE",
        priority="HIGH",
        environment="LIVE",
        origin="TEST",
        owner_employee_id=ceo.id,
        real_budget_limit=Decimal("100"),
    )
    db.session.add(project)
    db.session.flush()
    operation = propose_operation(ceo, _plan(project, ceo, researcher, critic), route_type="FULL_PROJECT")
    approve(operation)
    db.session.refresh(operation)
    return operation, project, researcher, critic


def _start(operation):
    kernel.transition(
        operation,
        "RUNNING",
        "TEST_RUNTIME_STARTED",
        stage="EXECUTION",
        actor_type="RUNTIME",
    )
    db.session.refresh(operation)


def test_new_multi_task_operation_opts_into_multi_agent_without_touching_legacy(ctx):
    operation, project, researcher, critic = _setup_operation()
    assert operation.memory_json["multi_agent_policy_version"] == "ma-work-v1"
    assert operation.memory_json["multi_agent_enabled"] is True
    assert len(operation.tasks) == 3

    # Existing persisted Operations without the marker remain serial.
    memory = dict(operation.memory_json or {})
    memory.pop("multi_agent_policy_version", None)
    memory["multi_agent_enabled"] = False
    operation.memory_json = memory
    db.session.commit()
    _start(operation)

    result = next_step(operation, "legacy-step-1")
    assert result["kind"] == "TASK"
    db.session.expire_all()
    tasks = Task.query.filter_by(operation_id=operation.id).order_by(Task.id).all()
    assert tasks[0].status == "DONE"
    assert tasks[1].status == "ASSIGNED"
    assert tasks[2].status == "ASSIGNED"


def test_parallel_wave_runs_heterogeneous_employee_branches_and_preserves_dependency_context(ctx):
    operation, project, researcher, critic = _setup_operation()

    # Give Critic a distinct mock intelligence binding so execution truth proves
    # that heterogeneous Employee bindings survive the parallel wave.
    critic_model = ModelConfig(
        label="Critic Mock",
        provider_key="mock",
        model_name="mock-critic",
        input_price_per_million=0,
        output_price_per_million=0,
        currency="TWD",
        max_output_tokens=4096,
    )
    db.session.add(critic_model)
    db.session.flush()
    critic.current_model_config_id = critic_model.id
    db.session.commit()

    tasks = list(operation.tasks)
    plan = {
        "strategy": "PARALLEL_DAG",
        "rationale": "Research and Critic can work independently; the follow-up consumes only Research evidence.",
        "max_parallelism": 2,
        "tasks": [
            {"task_id": tasks[0].id, "depends_on_task_ids": [], "role": "WORKER", "reason": "Independent research branch."},
            {"task_id": tasks[1].id, "depends_on_task_ids": [], "role": "WORKER", "reason": "Independent critical branch."},
            {"task_id": tasks[2].id, "depends_on_task_ids": [tasks[0].id], "role": "SYNTHESIS", "reason": "Consumes the research branch only."},
        ],
    }
    multi_agent.persist_plan(operation, plan)
    _start(operation)

    wave = next_step(operation, "parallel-wave-1")
    assert wave["kind"] == "PARALLEL_WAVE"
    assert set(wave["task_ids"]) == {tasks[0].id, tasks[1].id}

    db.session.expire_all()
    tasks = Task.query.filter_by(operation_id=operation.id).order_by(Task.id).all()
    assert tasks[0].status == "DONE"
    assert tasks[1].status == "DONE"
    assert tasks[2].status == "ASSIGNED"

    runs = AgentRun.query.filter(
        AgentRun.operation_id == operation.id,
        AgentRun.purpose == "TASK_EXECUTION",
    ).order_by(AgentRun.task_id).all()
    assert len(runs) == 2
    snapshots = {run.task_id: run.model_name_snapshot for run in runs}
    assert snapshots[tasks[0].id] == "deterministic-mock"
    assert snapshots[tasks[1].id] == "mock-critic"

    text, composition = operation_context(tasks[2])
    assert f"PREDECESSOR TASK #{tasks[0].id}" in text
    assert f"PREDECESSOR TASK #{tasks[1].id}" not in text

    follow_up = next_step(operation, "parallel-wave-2")
    assert follow_up["kind"] == "TASK"
    assert follow_up["task_id"] == tasks[2].id


def test_orchestration_projection_is_added_to_existing_project_surface_without_replacing_it(ctx):
    operation, project, researcher, critic = _setup_operation()
    tasks = list(operation.tasks)
    multi_agent.persist_plan(operation, {
        "strategy": "PARALLEL_DAG",
        "rationale": "Two independent specialist branches are useful.",
        "max_parallelism": 2,
        "tasks": [
            {"task_id": tasks[0].id, "depends_on_task_ids": [], "role": "WORKER", "reason": "Independent branch."},
            {"task_id": tasks[1].id, "depends_on_task_ids": [], "role": "VERIFIER", "reason": "Independent challenge."},
            {"task_id": tasks[2].id, "depends_on_task_ids": [tasks[0].id, tasks[1].id], "role": "SYNTHESIS", "reason": "Needs both persisted results."},
        ],
    })
    snapshot = project_snapshot(project)
    assert snapshot["orchestration"] is not None
    assert snapshot["orchestration"]["strategy"] == "PARALLEL_DAG"
    assert len(snapshot["orchestration"]["tasks"]) == 3


def test_orchestration_rejects_cycles_and_unknown_task_ids(ctx):
    operation, project, researcher, critic = _setup_operation()
    tasks = list(operation.tasks)
    bad = {
        "strategy": "PARALLEL_DAG",
        "rationale": "Invalid cycle.",
        "max_parallelism": 2,
        "tasks": [
            {"task_id": tasks[0].id, "depends_on_task_ids": [tasks[1].id], "role": "WORKER", "reason": "bad"},
            {"task_id": tasks[1].id, "depends_on_task_ids": [tasks[0].id], "role": "WORKER", "reason": "bad"},
            {"task_id": tasks[2].id, "depends_on_task_ids": [], "role": "WORKER", "reason": "bad"},
        ],
    }
    try:
        multi_agent.validate_payload(operation, bad)
    except ValueError as exc:
        assert "cycle" in str(exc).lower()
    else:
        raise AssertionError("cycle should be rejected")


def test_runtime_first_plans_then_runs_real_parallel_employee_wave(ctx):
    operation, project, researcher, critic = _setup_operation()
    _start(operation)

    first = next_step(operation, "runtime-ma-1")
    assert first["kind"] == "ORCHESTRATION"
    assert first["strategy"] == "PARALLEL_DAG"
    planner = AgentRun.query.filter_by(
        operation_id=operation.id, purpose="ORCHESTRATION_PLAN"
    ).one()
    assert planner.status == "SUCCEEDED"

    second = next_step(operation, "runtime-ma-2")
    assert second["kind"] == "PARALLEL_WAVE"
    assert len(second["task_ids"]) == 3
    db.session.expire_all()
    assert all(task.status == "DONE" for task in Task.query.filter_by(operation_id=operation.id).all())


def test_project_route_renders_parallel_work_inside_existing_project_page(ctx, client):
    operation, project, researcher, critic = _setup_operation()
    tasks = list(operation.tasks)
    multi_agent.persist_plan(operation, {
        "strategy": "PARALLEL_DAG",
        "rationale": "Render the real approved work graph.",
        "max_parallelism": 2,
        "tasks": [
            {"task_id": tasks[0].id, "depends_on_task_ids": [], "role": "WORKER", "reason": "Independent."},
            {"task_id": tasks[1].id, "depends_on_task_ids": [], "role": "VERIFIER", "reason": "Independent."},
            {"task_id": tasks[2].id, "depends_on_task_ids": [tasks[0].id, tasks[1].id], "role": "SYNTHESIS", "reason": "Consumes both."},
        ],
    })
    response = client.get(f"/headquarters/projects/{project.id}")
    assert response.status_code == 200
    text = response.get_data(as_text=True)
    assert "MULTI-AGENT WORK" in text
    assert "How approved work is being divided." in text
    assert "Research branch" in text
    assert "Critical branch" in text
    # Existing baseline Project sections remain present.
    assert "What the company is doing." in text
    assert "What the company has actually produced." in text
    assert "People on this Project" in text


def test_parallel_paid_branch_reservations_keep_operation_event_sequence_unique(ctx, monkeypatch):
    import json
    import threading
    import time
    from eason_one.models import CostReservation, OperationEvent
    from eason_one.providers import ProviderResult

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    ceo = Employee.query.filter_by(slug="ceo").one()
    researcher = Employee.query.filter_by(slug="researcher").one()
    critic = Employee.query.filter_by(slug="critic").one()
    paid_a = ModelConfig(
        label="Paid A", provider_key="openai", model_name="paid-a",
        input_price_per_million=1, output_price_per_million=1,
        currency="TWD", max_output_tokens=4096,
    )
    paid_b = ModelConfig(
        label="Paid B", provider_key="openai", model_name="paid-b",
        input_price_per_million=1, output_price_per_million=1,
        currency="TWD", max_output_tokens=4096,
    )
    db.session.add_all([paid_a, paid_b]); db.session.flush()
    researcher.current_model_config_id = paid_a.id
    critic.current_model_config_id = paid_b.id
    project = Project(
        name="Paid Parallel", objective="Exercise concurrent reservation truth.",
        status="ACTIVE", priority="HIGH", environment="LIVE", origin="TEST",
        owner_employee_id=ceo.id, real_budget_limit=Decimal("100"),
    )
    db.session.add(project); db.session.flush()
    operation = propose_operation(ceo, _plan(project, ceo, researcher, critic), route_type="FULL_PROJECT")
    approve(operation); db.session.refresh(operation)
    tasks = list(operation.tasks)
    multi_agent.persist_plan(operation, {
        "strategy": "PARALLEL_DAG",
        "rationale": "The first two paid branches are independent; follow-up waits for both.",
        "max_parallelism": 2,
        "tasks": [
            {"task_id": tasks[0].id, "depends_on_task_ids": [], "role": "WORKER", "reason": "Independent paid branch."},
            {"task_id": tasks[1].id, "depends_on_task_ids": [], "role": "WORKER", "reason": "Independent paid branch."},
            {"task_id": tasks[2].id, "depends_on_task_ids": [tasks[0].id, tasks[1].id], "role": "SYNTHESIS", "reason": "Wait for both."},
        ],
    })
    _start(operation)

    barrier = threading.Barrier(2)
    class PaidProvider:
        def complete(self, *args, **kwargs):
            barrier.wait(timeout=3)
            time.sleep(0.05)
            payload = {
                "result_summary": "Concurrent paid branch completed.",
                "knowledge_proposals": [],
            }
            return ProviderResult(json.dumps(payload), 100, 50, request_id="paid", response_id="paid")

    monkeypatch.setattr("eason_one.services.execution.get_provider", lambda _: PaidProvider())
    wave = next_step(operation, "paid-parallel-wave")
    assert wave["kind"] == "PARALLEL_WAVE"
    assert all(row["status"] == "SUCCEEDED" for row in wave["task_results"])

    reservations = CostReservation.query.filter_by(operation_id=operation.id).all()
    assert len(reservations) == 2
    assert all(row.status == "CONSUMED" for row in reservations)
    events = OperationEvent.query.filter_by(operation_id=operation.id).order_by(OperationEvent.sequence).all()
    sequences = [row.sequence for row in events]
    assert len(sequences) == len(set(sequences))


def test_formal_work_can_override_mock_employee_binding_with_governed_real_model(ctx, app, monkeypatch):
    import json
    from eason_one.providers import ProviderResult
    from eason_one.services import operations as operation_service

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    real = ModelConfig(
        label="Formal Fallback", provider_key="openai", model_name="formal-fallback",
        input_price_per_million=1, output_price_per_million=1,
        currency="TWD", max_output_tokens=4096,
    )
    db.session.add(real); db.session.commit()

    operation, project, researcher, critic = _setup_operation()
    task = list(operation.tasks)[0]
    assert task.assigned_employee.current_model.provider_key == "mock"
    _start(operation)

    class Provider:
        def complete(self, *args, **kwargs):
            payload = {"result_summary": "Formal routed work completed.", "knowledge_proposals": []}
            return ProviderResult(json.dumps(payload), 100, 50, request_id="real", response_id="real")

    monkeypatch.setattr("eason_one.services.execution.get_provider", lambda _: Provider())
    app.config["TESTING"] = False
    try:
        assert ensure_full_execution_authority(operation) is True
        result = operation_service._run_task_step(operation, "formal-binding-override", task)
    finally:
        app.config["TESTING"] = True

    assert result["status"] == "SUCCEEDED"
    run = AgentRun.query.filter_by(operation_id=operation.id, task_id=task.id, purpose="TASK_EXECUTION").one()
    assert run.provider_key_snapshot == "openai"
    assert run.model_name_snapshot == "formal-fallback"
    db.session.refresh(task)
    assert task.status == "DONE"


def test_parallel_preflight_failure_recovers_locally_without_founder_or_sibling_freeze(ctx, monkeypatch):
    import json
    import time
    from eason_one.providers import ProviderResult
    from eason_one.services import execution as execution_service

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    operation, project, researcher, critic = _setup_operation()
    good = ModelConfig(
        label="Good Parallel", provider_key="openai", model_name="good-parallel",
        input_price_per_million=1, output_price_per_million=1,
        currency="TWD", max_output_tokens=4096,
    )
    bad = ModelConfig(
        label="Bad Parallel", provider_key="unsupported-test-provider", model_name="bad-parallel",
        input_price_per_million=1, output_price_per_million=1,
        currency="TWD", max_output_tokens=4096,
    )
    db.session.add_all([good, bad]); db.session.flush()
    researcher.current_model_config_id = good.id
    critic.current_model_config_id = bad.id
    db.session.commit()

    tasks = list(operation.tasks)
    multi_agent.persist_plan(operation, {
        "strategy": "PARALLEL_DAG",
        "rationale": "Two independent branches; one intentionally fails preflight.",
        "max_parallelism": 2,
        "tasks": [
            {"task_id": tasks[0].id, "depends_on_task_ids": [], "role": "WORKER", "reason": "Healthy branch."},
            {"task_id": tasks[1].id, "depends_on_task_ids": [], "role": "WORKER", "reason": "Preflight failure branch."},
            {"task_id": tasks[2].id, "depends_on_task_ids": [tasks[0].id], "role": "SYNTHESIS", "reason": "Later work."},
        ],
    })
    _start(operation)

    original_preflight = execution_service.provider_preflight
    def ordered_preflight(model, *, operation=None, purpose=None):
        if model.model_name == "good-parallel":
            time.sleep(0.12)
        return original_preflight(model, operation=operation, purpose=purpose)
    monkeypatch.setattr(execution_service, "provider_preflight", ordered_preflight)

    class Provider:
        def complete(self, *args, **kwargs):
            payload = {"result_summary": "Healthy sibling completed.", "knowledge_proposals": []}
            return ProviderResult(json.dumps(payload), 100, 50, request_id="good", response_id="good")
    monkeypatch.setattr(execution_service, "get_provider", lambda _: Provider())

    wave = next_step(operation, "parallel-preflight-isolation")
    assert wave["kind"] == "PARALLEL_WAVE"
    assert wave["status"] == "COMPLETED"

    db.session.expire_all()
    refreshed = Task.query.filter_by(operation_id=operation.id).order_by(Task.id).all()
    assert refreshed[0].status == "DONE"
    assert refreshed[1].status == "DONE"
    good_run = AgentRun.query.filter_by(
        operation_id=operation.id, task_id=refreshed[0].id, purpose="TASK_EXECUTION"
    ).one()
    assert good_run.status == "SUCCEEDED"
    recovered_runs = AgentRun.query.filter_by(
        operation_id=operation.id, task_id=refreshed[1].id, purpose="TASK_EXECUTION"
    ).order_by(AgentRun.id).all()
    assert len(recovered_runs) == 2
    assert recovered_runs[0].outcome == "FAILED_SAFE"
    assert recovered_runs[0].failure_stage == "PRE_DISPATCH"
    assert recovered_runs[1].status == "SUCCEEDED"
    assert recovered_runs[1].retry_of_run_id == recovered_runs[0].id
    from eason_one.models import ExternalEffectAttempt, Work
    bad_work = db.session.get(Work, refreshed[1].work_id)
    assert bad_work.state == "ACCEPTED"
    first_effect = ExternalEffectAttempt.query.filter_by(execution_id=recovered_runs[0].id).one()
    second_effect = ExternalEffectAttempt.query.filter_by(execution_id=recovered_runs[1].id).one()
    assert first_effect.state == "FAILED_PRE_DISPATCH"
    assert second_effect.state == "SETTLED"
    operation = db.session.get(Operation, operation.id)
    assert kernel.authoritative_status(operation) == "RUNNING"
    assert operation.founder_report_json is None


def test_known_phase1_zero_provider_gate_recovers_without_new_founder_approval(ctx):
    operation, project, researcher, critic = _setup_operation()
    tasks = list(operation.tasks)
    multi_agent.persist_plan(operation, {
        "strategy": "PARALLEL_DAG",
        "rationale": "Reproduce the known Phase-1 pre-provider race.",
        "max_parallelism": 2,
        "tasks": [
            {"task_id": tasks[0].id, "depends_on_task_ids": [], "role": "WORKER", "reason": "branch"},
            {"task_id": tasks[1].id, "depends_on_task_ids": [], "role": "WORKER", "reason": "branch"},
            {"task_id": tasks[2].id, "depends_on_task_ids": [tasks[0].id], "role": "SYNTHESIS", "reason": "later"},
        ],
    })
    _start(operation)
    tasks[0].status = "BLOCKED"
    tasks[1].status = "BLOCKED"
    db.session.add_all([
        OperationStep(
            operation_id=operation.id, task_id=tasks[0].id,
            idempotency_key="old-a", logical_key=f"task:{tasks[0].id}:execute:1",
            kind="TASK", status="AMBIGUOUS",
            error_text="Formal Mission execution cannot use MockProvider",
        ),
        OperationStep(
            operation_id=operation.id, task_id=tasks[1].id,
            idempotency_key="old-b", logical_key=f"task:{tasks[1].id}:execute:1",
            kind="TASK", status="AMBIGUOUS",
            error_text="Operation is not authorized for execution",
        ),
    ])
    db.session.commit()
    wait_for_founder(
        operation,
        "Provider-call truth is ambiguous; recovery is required before continuing.",
    )
    assert kernel.authoritative_status(operation) == "WAITING_APPROVAL"

    assert recover_multi_agent_preprovider_gate(operation) is True
    db.session.expire_all()
    operation = db.session.get(Operation, operation.id)
    refreshed = Task.query.filter_by(operation_id=operation.id).order_by(Task.id).all()
    assert refreshed[0].status == "ASSIGNED"
    assert refreshed[1].status == "ASSIGNED"
    assert kernel.authoritative_status(operation) == "QUEUED"
    assert operation.waiting_reason is None
    assert operation.founder_report_json is None
    recovered_steps = OperationStep.query.filter_by(operation_id=operation.id).order_by(OperationStep.id).all()
    assert all(step.status == "FAILED_PRE_PROVIDER_RECOVERED" for step in recovered_steps[-2:])
