"""Release-gated ports of still-current v0.20 invariants.

These tests intentionally exercise *current* Project Contract / Work / provider
semantics.  Historical modules remain in-tree for audit, but their old fixtures
and exception contracts are not reused when they conflict with current v0.20
truth.
"""

from copy import deepcopy
import hashlib
import json
from decimal import Decimal

import pytest

from eason_one import create_app
from eason_one.extensions import db
from eason_one.models import (
    AgentRun,
    Artifact,
    ArtifactVersion,
    Company,
    CompanyEvent,
    CostEvent,
    CostReservation,
    Employee,
    EmployeeLearningRecord,
    Escalation,
    ExternalEffectAttempt,
    KnowledgeItem,
    MarketContract,
    MarketLedgerEntry,
    ModelConfig,
    Position,
    Project,
    Task,
    VerificationRecord,
    Work,
    WorkDependency,
    now,
)
from eason_one.providers import ProviderResult
from eason_one.seed import seed
from eason_one.services import acceptance_contract, company_events, operations, project_company, project_contract, project_outcome, work_runtime
from eason_one.services.execution import execute
from eason_one.services.operation_kernel import reserve_provider_call
from eason_one.services.work_execution import execute_work

from tests import test_hardening as hardening
from tests import test_patch0061a as patch0061a
from tests import test_patch0072 as patch0072
from tests import test_persistent_employee_memory as employee_memory
from tests import test_v020_core_rebuild as core
from tests import test_v020_multi_employee_company as multi


def _test_app(db_path):
    return create_app({
        "TESTING": True,
        "AUTO_START_COMPANY_RUNTIME": False,
        "AUTO_START_OPERATION_RUNTIME": False,
        "SQLALCHEMY_DATABASE_URI": f"sqlite:///{db_path}",
    })


def test_current_durable_restart_preserves_work_artifact_event_truth(tmp_path):
    """Restart proof uses the current immutable Project Contract and Work spine."""
    db_path = tmp_path / "current-restart.db"
    app1 = _test_app(db_path)
    with app1.app_context():
        db.drop_all(); db.create_all(); seed()
        criterion = "Current accepted evidence survives process restart"
        project = core._project((criterion,))
        operation = core._operation(project)
        work = core._accepted_work(project, operation, criterion)
        version = (
            ArtifactVersion.query.join(Artifact)
            .filter(Artifact.work_id == work.id)
            .order_by(ArtifactVersion.id.desc()).one()
        )
        event = company_events.emit(
            "WORK_ACCEPTED",
            actor_type="RUNTIME",
            project_id=project.id,
            work_id=work.id,
            artifact_id=version.artifact_id,
            correlation_id=f"work:{work.id}",
            payload={"release_test": "durable_restart"},
            commit=True,
        )
        project_id, work_id, version_id, event_id = project.id, work.id, version.id, event.id
        contract_hash = project_contract.get(project)["contract_hash"]
        db.session.remove()

    app2 = _test_app(db_path)
    with app2.app_context():
        work = db.session.get(Work, work_id)
        version = db.session.get(ArtifactVersion, version_id)
        event = db.session.get(CompanyEvent, event_id)
        assert work is not None and work.state == "ACCEPTED"
        assert version is not None and version.status == "ACCEPTED"
        assert event is not None and event.event_type == "WORK_ACCEPTED" and event.work_id == work_id
        assert VerificationRecord.query.filter_by(
            artifact_version_id=version_id, status="PASSED"
        ).count() >= 1
        assert project_contract.get(work.project)["contract_hash"] == contract_hash


def test_current_restart_closes_unbound_ceo_planning_without_replay(tmp_path):
    """CEO planning has no Work yet, so restart recovery must reconcile it separately."""
    from eason_one.services import runtime_recovery

    db_path = tmp_path / "founder-planning-restart.db"
    app = _test_app(db_path)
    with app.app_context():
        db.drop_all(); db.create_all(); seed()
        ceo = Employee.query.filter_by(slug="ceo").one()
        model = ceo.current_model
        runs = []
        for suffix, state in (("safe", "PREPARED"), ("unknown", "DISPATCHING")):
            run = AgentRun(
                employee_id=ceo.id, model_config_id=model.id,
                purpose="CEO_FOUNDER_REQUEST", user_request=f"restart-{suffix}",
                system_prompt_snapshot="restart test", context_snapshot="restart test",
                context_composition_json={"founder_request_id": f"restart-{suffix}"},
                status="RUNNING", outcome="RUNNING",
                provider_key_snapshot=model.provider_key, model_name_snapshot=model.model_name,
                input_price_snapshot=model.input_price_per_million,
                output_price_snapshot=model.output_price_per_million,
                request_price_snapshot=getattr(model, "request_price_per_call", 0) or 0,
                currency_snapshot=model.currency, currency=model.currency,
                effective_max_output_tokens=model.max_output_tokens,
            )
            db.session.add(run); db.session.flush()
            effect = ExternalEffectAttempt(
                execution_id=run.id, provider=model.provider_key, effect_kind="MODEL_INFERENCE",
                request_fingerprint=f"restart-{suffix}", idempotency_key=f"MODEL_INFERENCE:execution:{run.id}",
                state=state, estimated_cost_twd=Decimal("0"),
                dispatched_at=now() if state == "DISPATCHING" else None,
            )
            db.session.add(effect); db.session.flush()
            runs.append((run.id, effect.id, state))
        db.session.commit()
        cutoff = now()

        assert runtime_recovery.recover_interrupted_founder_requests(before=cutoff) == 2

        safe_run = db.session.get(AgentRun, runs[0][0])
        safe_effect = db.session.get(ExternalEffectAttempt, runs[0][1])
        unknown_run = db.session.get(AgentRun, runs[1][0])
        unknown_effect = db.session.get(ExternalEffectAttempt, runs[1][1])
        assert safe_run.outcome == "FAILED_SAFE"
        assert safe_run.failure_reason == "PROCESS_RESTART_BEFORE_DISPATCH"
        assert safe_effect.state == "FAILED_PRE_DISPATCH"
        assert unknown_run.outcome == "FAILED_AMBIGUOUS"
        assert unknown_run.failure_reason == "PROCESS_RESTART_AFTER_DISPATCH"
        assert unknown_effect.state == "AMBIGUOUS_POST_DISPATCH"


def test_current_restart_does_not_touch_ceo_planning_started_after_process_cutoff(tmp_path):
    """Startup recovery cutoff must never race a live planning call in this process."""
    from eason_one.services import runtime_recovery

    db_path = tmp_path / "founder-planning-live.db"
    app = _test_app(db_path)
    with app.app_context():
        db.drop_all(); db.create_all(); seed()
        cutoff = now()
        ceo = Employee.query.filter_by(slug="ceo").one()
        model = ceo.current_model
        run = AgentRun(
            employee_id=ceo.id, model_config_id=model.id, purpose="CEO_FOUNDER_REQUEST",
            user_request="live-current-process", system_prompt_snapshot="live", context_snapshot="live",
            status="RUNNING", outcome="RUNNING",
            provider_key_snapshot=model.provider_key, model_name_snapshot=model.model_name,
            input_price_snapshot=model.input_price_per_million, output_price_snapshot=model.output_price_per_million,
            request_price_snapshot=getattr(model, "request_price_per_call", 0) or 0,
            currency_snapshot=model.currency, currency=model.currency, effective_max_output_tokens=model.max_output_tokens,
        )
        db.session.add(run); db.session.flush()
        effect = ExternalEffectAttempt(
            execution_id=run.id, provider=model.provider_key, effect_kind="MODEL_INFERENCE",
            request_fingerprint="live-current-process", idempotency_key=f"MODEL_INFERENCE:execution:{run.id}",
            state="DISPATCHING", estimated_cost_twd=Decimal("0"), dispatched_at=now(),
        )
        db.session.add(effect); db.session.commit()

        assert runtime_recovery.recover_interrupted_founder_requests(before=cutoff) == 0
        assert db.session.get(AgentRun, run.id).status == "RUNNING"
        assert db.session.get(ExternalEffectAttempt, effect.id).state == "DISPATCHING"


def test_current_restart_recovers_persisted_success_without_provider_recall(tmp_path, monkeypatch):
    """A persisted SUCCEEDED Execution is reused after restart; provider is not replayed."""
    db_path = tmp_path / "persisted-success.db"
    app1 = _test_app(db_path)
    with app1.app_context():
        db.drop_all(); db.create_all(); seed()
        operation, project, researcher, _critic = multi._setup()
        work = sorted(
            [row for row in operation.works if row.work_type != "MANAGEMENT"],
            key=lambda row: row.id,
        )[0]
        task = work_runtime.task_for_work(work)
        assert task is not None
        acceptance_contract.ensure_for_work(work, task=task)
        if work.state == "READY":
            work_runtime.transition(
                work, "EXECUTING", actor_type="EMPLOYEE", actor_id=researcher.id,
                reason="Simulate crash after durable provider response",
            )
        task.status = "WORKING"
        model = researcher.current_model
        run = AgentRun(
            employee_id=researcher.id,
            project_id=project.id,
            operation_id=operation.id,
            work_id=work.id,
            task_id=task.id,
            model_config_id=model.id,
            purpose="TASK_EXECUTION",
            user_request=task.objective or "Produce current Work result",
            system_prompt_snapshot="current release persisted success",
            context_snapshot="current release persisted success",
            raw_output="persisted provider result",
            parsed_output_json={
                "result_summary": "persisted provider result",
                "knowledge_proposals": [],
            },
            context_composition_json={
                "provider_sources": [{"title": "Persisted source", "url": "https://example.test/source"}],
            },
            status="SUCCEEDED",
            outcome="SUCCEEDED",
            provider_key_snapshot=model.provider_key,
            model_name_snapshot=model.model_name,
            input_price_snapshot=model.input_price_per_million,
            output_price_snapshot=model.output_price_per_million,
            request_price_snapshot=getattr(model, "request_price_per_call", 0) or 0,
            currency_snapshot=model.currency,
            currency=model.currency,
            input_tokens=10,
            output_tokens=5,
            real_cost=Decimal("0"),
            finished_at=now(),
        )
        db.session.add(run); db.session.commit()
        work_id, run_id = work.id, run.id
        db.session.remove()

    calls = []
    def forbidden_replay(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("persisted success must not replay provider execution")
    monkeypatch.setattr("eason_one.services.work_execution.run_task", forbidden_replay)

    app2 = _test_app(db_path)
    with app2.app_context():
        work = db.session.get(Work, work_id)
        result = execute_work(work)
        assert calls == []
        assert result["run_id"] == run_id
        assert AgentRun.query.filter_by(work_id=work_id, purpose="TASK_EXECUTION").count() == 1
        version = ArtifactVersion.query.filter_by(execution_id=run_id).one()
        assert version.status in {"SUBMITTED", "ACCEPTED"}
        assert db.session.get(Work, work_id).state in {"VERIFYING", "ACCEPTED", "WAITING"}


def test_current_restart_finishes_known_provider_settlement_without_replay(ctx):
    """A crash after durable provider result cannot leave phantom reserved budget forever."""
    from eason_one.services import runtime_recovery

    operation, project, researcher, _critic = multi._setup()
    work = sorted([row for row in operation.works if row.work_type != "MANAGEMENT"], key=lambda row: row.id)[0]
    task = work_runtime.task_for_work(work)
    model = researcher.current_model
    run = AgentRun(
        employee_id=researcher.id, project_id=project.id, operation_id=operation.id,
        work_id=work.id, task_id=task.id, model_config_id=model.id,
        purpose="TASK_EXECUTION", user_request="durable request",
        system_prompt_snapshot="system", context_snapshot="context",
        raw_output="durable response", parsed_output_json={"result_summary": "durable response", "knowledge_proposals": []},
        status="SUCCEEDED", outcome="SUCCEEDED", real_cost=Decimal("0.420000"), currency="TWD",
        provider_key_snapshot=model.provider_key, model_name_snapshot=model.model_name,
        input_price_snapshot=model.input_price_per_million, output_price_snapshot=model.output_price_per_million,
        request_price_snapshot=getattr(model, "request_price_per_call", 0) or 0,
        currency_snapshot=model.currency, input_tokens=100, output_tokens=50, finished_at=now(),
    )
    db.session.add(run); db.session.flush()
    reservation = CostReservation(
        operation_id=operation.id, agent_run_id=run.id, stage="TASK_EXECUTION",
        idempotency_key=f"agent-run:{run.id}", estimated_twd=Decimal("0.500000"), status="RESERVED",
    )
    db.session.add(reservation); db.session.flush()
    effect = ExternalEffectAttempt(
        execution_id=run.id, work_id=work.id, operation_id=operation.id,
        provider=model.provider_key, effect_kind="MODEL_INFERENCE",
        request_fingerprint="a" * 64, idempotency_key=f"MODEL_INFERENCE:work:{work.id}:purpose:TASK_EXECUTION",
        state="PERSISTED", estimated_cost_twd=Decimal("0.500000"),
        cost_reservation_id=reservation.id, persisted_at=now(),
    )
    db.session.add(effect); db.session.commit()

    runtime_recovery.reconcile_interrupted_provider_settlement()
    db.session.refresh(reservation); db.session.refresh(effect)
    assert reservation.status == "CONSUMED"
    assert Decimal(reservation.actual_twd) == Decimal("0.420000")
    assert effect.state == "SETTLED"
    assert Decimal(effect.actual_cost_twd) == Decimal("0.420000")


def test_restart_finalizes_durable_provider_response_checkpoint_without_replay(ctx):
    """A returned paid response survives a crash before status/cost settlement."""
    from eason_one.services import external_effects, runtime_recovery

    operation, project, researcher, _critic = multi._setup()
    work = sorted([row for row in operation.works if row.work_type != "MANAGEMENT"], key=lambda row: row.id)[0]
    task = work_runtime.task_for_work(work)
    model = researcher.current_model
    run = AgentRun(
        employee_id=researcher.id, project_id=project.id, operation_id=operation.id,
        work_id=work.id, task_id=task.id, model_config_id=model.id,
        purpose="TASK_EXECUTION", user_request="durable checkpoint request",
        system_prompt_snapshot="system", context_snapshot="context",
        raw_output="provider returned this exact paid response",
        output_hash=hashlib.sha256(b"provider returned this exact paid response").hexdigest(),
        status="RUNNING", outcome="RUNNING", currency="TWD",
        provider_key_snapshot=model.provider_key, model_name_snapshot=model.model_name,
        input_price_snapshot=Decimal("100.0000"), output_price_snapshot=Decimal("200.0000"),
        request_price_snapshot=Decimal("0.300000"), currency_snapshot="TWD",
        input_tokens=1000, output_tokens=500,
        context_composition_json={
            "provider_response_checkpoint": {
                "schema": "PROVIDER_RESPONSE_CHECKPOINT_V1",
                "status": "completed", "incomplete_reason": None,
                "refusal": None, "include_request_fee": True,
            }
        },
    )
    db.session.add(run); db.session.flush()
    reservation = CostReservation(
        operation_id=operation.id, agent_run_id=run.id, stage="TASK_EXECUTION",
        idempotency_key=f"agent-run:{run.id}", estimated_twd=Decimal("0.600000"), status="RESERVED",
    )
    db.session.add(reservation); db.session.flush()
    effect = ExternalEffectAttempt(
        execution_id=run.id, work_id=work.id, operation_id=operation.id,
        provider=model.provider_key, effect_kind="MODEL_INFERENCE",
        request_fingerprint="d" * 64,
        idempotency_key=f"MODEL_INFERENCE:work:{work.id}:purpose:TASK_EXECUTION",
        state="RESPONSE_RECEIVED", estimated_cost_twd=Decimal("0.600000"),
        cost_reservation_id=reservation.id, dispatched_at=now(), response_received_at=now(),
    )
    db.session.add(effect); db.session.commit()

    assert runtime_recovery.reconcile_interrupted_provider_settlement() == 1
    db.session.refresh(run); db.session.refresh(effect); db.session.refresh(reservation)
    assert run.status == "SUCCEEDED"
    assert run.outcome == "SUCCEEDED"
    assert run.raw_output == "provider returned this exact paid response"
    assert Decimal(run.real_cost) == Decimal("0.500000")
    assert effect.state == "SETTLED"
    assert reservation.status == "CONSUMED"
    assert CostEvent.query.filter_by(agent_run_id=run.id, category="MODEL").count() == 1
    allowed, reason = external_effects.retry_authorized(run)
    assert allowed is False
    assert reason == "MODEL_INFERENCE_SUCCESS_ALREADY_PAID"
    assert runtime_recovery.reconcile_interrupted_provider_settlement() == 0
    assert CostEvent.query.filter_by(agent_run_id=run.id, category="MODEL").count() == 1


def test_settled_successful_model_execution_is_never_retry_authority(ctx):
    """Provider/model fallback cannot use a paid success as its retry source."""
    from eason_one.services import external_effects

    operation, project, researcher, _critic = multi._setup()
    work = sorted([row for row in operation.works if row.work_type != "MANAGEMENT"], key=lambda row: row.id)[0]
    model = researcher.current_model
    run = AgentRun(
        employee_id=researcher.id, project_id=project.id, operation_id=operation.id,
        work_id=work.id, model_config_id=model.id, purpose="TASK_EXECUTION",
        user_request="already succeeded", system_prompt_snapshot="system", context_snapshot="context",
        raw_output="paid success", status="SUCCEEDED", outcome="SUCCEEDED", real_cost=Decimal("0.1"),
        provider_key_snapshot=model.provider_key, model_name_snapshot=model.model_name,
        input_price_snapshot=model.input_price_per_million, output_price_snapshot=model.output_price_per_million,
        request_price_snapshot=getattr(model, "request_price_per_call", 0) or 0,
        currency_snapshot=model.currency, currency=model.currency, finished_at=now(),
    )
    db.session.add(run); db.session.flush()
    db.session.add(ExternalEffectAttempt(
        execution_id=run.id, work_id=work.id, operation_id=operation.id,
        provider=model.provider_key, effect_kind="MODEL_INFERENCE",
        request_fingerprint="e" * 64,
        idempotency_key=f"MODEL_INFERENCE:work:{work.id}:purpose:TASK_EXECUTION",
        state="SETTLED", estimated_cost_twd=Decimal("0.1"), actual_cost_twd=Decimal("0.1"),
        dispatched_at=now(), response_received_at=now(), persisted_at=now(), settled_at=now(),
    ))
    db.session.commit()

    assert external_effects.retry_authorized(run) == (
        False, "MODEL_INFERENCE_SUCCESS_ALREADY_PAID"
    )


def test_current_restart_releases_definitive_provider_rejection_reservation(ctx):
    """A known HTTP rejection must not strand budget after a crash before release bookkeeping."""
    from eason_one.services import runtime_recovery

    operation, project, researcher, _critic = multi._setup()
    work = sorted([row for row in operation.works if row.work_type != "MANAGEMENT"], key=lambda row: row.id)[0]
    task = work_runtime.task_for_work(work)
    model = researcher.current_model
    run = AgentRun(
        employee_id=researcher.id, project_id=project.id, operation_id=operation.id,
        work_id=work.id, task_id=task.id, model_config_id=model.id,
        purpose="TASK_EXECUTION", user_request="rejected request",
        system_prompt_snapshot="system", context_snapshot="context",
        status="FAILED", outcome="FAILED_KNOWN", failure_stage="POST_DISPATCH",
        failure_reason="PROVIDER_TRANSIENT_REJECTED", error_text="HTTP 429",
        provider_key_snapshot=model.provider_key, model_name_snapshot=model.model_name,
        input_price_snapshot=model.input_price_per_million, output_price_snapshot=model.output_price_per_million,
        request_price_snapshot=getattr(model, "request_price_per_call", 0) or 0,
        currency_snapshot=model.currency, currency="TWD", finished_at=now(),
    )
    db.session.add(run); db.session.flush()
    reservation = CostReservation(
        operation_id=operation.id, agent_run_id=run.id, stage="TASK_EXECUTION",
        idempotency_key=f"agent-run:{run.id}", estimated_twd=Decimal("0.500000"), status="RESERVED",
    )
    db.session.add(reservation); db.session.flush()
    effect = ExternalEffectAttempt(
        execution_id=run.id, work_id=work.id, operation_id=operation.id,
        provider=model.provider_key, effect_kind="MODEL_INFERENCE",
        request_fingerprint="b" * 64, idempotency_key=f"MODEL_INFERENCE:work:{work.id}:purpose:TASK_EXECUTION",
        state="REJECTED_POST_DISPATCH", estimated_cost_twd=Decimal("0.500000"),
        cost_reservation_id=reservation.id, dispatched_at=now(), response_received_at=now(),
    )
    db.session.add(effect); db.session.commit()

    runtime_recovery.reconcile_interrupted_provider_settlement()
    db.session.refresh(reservation)
    assert reservation.status == "RELEASED"
    assert reservation.actual_twd is None


def test_current_restart_after_unknown_dispatch_holds_budget_and_forbids_replay(ctx):
    """Unknown post-dispatch outcome becomes explicit reconciliation truth, never a blind second call."""
    from eason_one.services import external_effects, runtime_recovery

    operation, project, researcher, _critic = multi._setup()
    work = sorted([row for row in operation.works if row.work_type != "MANAGEMENT"], key=lambda row: row.id)[0]
    task = work_runtime.task_for_work(work)
    if work.state == "READY":
        work_runtime.transition(work, "EXECUTING", actor_type="EMPLOYEE", actor_id=researcher.id, reason="simulate interrupted dispatch")
    model = researcher.current_model
    run = AgentRun(
        employee_id=researcher.id, project_id=project.id, operation_id=operation.id,
        work_id=work.id, task_id=task.id, model_config_id=model.id,
        purpose="TASK_EXECUTION", user_request="unknown request",
        system_prompt_snapshot="system", context_snapshot="context",
        status="RUNNING", outcome="RUNNING",
        provider_key_snapshot=model.provider_key, model_name_snapshot=model.model_name,
        input_price_snapshot=model.input_price_per_million, output_price_snapshot=model.output_price_per_million,
        request_price_snapshot=getattr(model, "request_price_per_call", 0) or 0,
        currency_snapshot=model.currency, currency="TWD",
    )
    db.session.add(run); db.session.flush()
    reservation = CostReservation(
        operation_id=operation.id, agent_run_id=run.id, stage="TASK_EXECUTION",
        idempotency_key=f"agent-run:{run.id}", estimated_twd=Decimal("0.500000"), status="RESERVED",
    )
    db.session.add(reservation); db.session.flush()
    effect = ExternalEffectAttempt(
        execution_id=run.id, work_id=work.id, operation_id=operation.id,
        provider=model.provider_key, effect_kind="MODEL_INFERENCE",
        request_fingerprint="c" * 64, idempotency_key=f"MODEL_INFERENCE:work:{work.id}:purpose:TASK_EXECUTION",
        state="DISPATCHING", estimated_cost_twd=Decimal("0.500000"),
        cost_reservation_id=reservation.id, dispatched_at=now(),
    )
    db.session.add(effect); db.session.commit()

    runtime_recovery.recover_stale_work()
    db.session.refresh(run); db.session.refresh(effect); db.session.refresh(reservation); db.session.refresh(work)
    assert run.outcome == "FAILED_AMBIGUOUS"
    assert effect.state == "AMBIGUOUS_POST_DISPATCH"
    assert reservation.status == "AMBIGUOUS"
    assert work.state == "WAITING"
    allowed, reason = external_effects.retry_authorized(run)
    assert allowed is False
    assert reason == "AMBIGUOUS_POST_DISPATCH"


def test_current_dependency_scheduler_uses_workdependency_not_stale_task(ctx):
    """Legacy Task status cannot release or re-block a current Work dependency."""
    operation, _project, _researcher, _critic = multi._setup()
    tasks = sorted(operation.tasks, key=lambda row: row.id)
    works = [db.session.get(Work, task.work_id) for task in tasks]
    upstream = works[:2]
    downstream = works[2]

    # Legacy Task claims DONE, but upstream Work is not accepted: dependency stays closed.
    for task in tasks[:2]:
        task.status = "DONE"
    db.session.commit()
    assert work_runtime.dependencies_satisfied(downstream) is False

    # Current Work acceptance opens the dependency. Then deliberately stale the
    # legacy Task projection again; scheduler truth must remain WorkDependency.
    for work in upstream:
        if work.state == "READY":
            work_runtime.transition(work, "EXECUTING", reason="release dependency proof")
        if work.state == "EXECUTING":
            work_runtime.transition(work, "ACCEPTED", reason="release dependency proof")
    for task in tasks[:2]:
        task.status = "ASSIGNED"
    db.session.commit()
    assert work_runtime.dependencies_satisfied(downstream) is True


def test_current_runtime_can_reach_result_ready_without_founder_orchestration(ctx):
    """Current Contract evidence can create Result Ready without a Founder recovery step."""
    criterion = "Current Project outcome is independently proven"
    project = core._project((criterion,))
    operation = core._operation(project)
    core._accepted_work(project, operation, criterion)

    assert Escalation.query.filter_by(project_id=project.id, state="OPEN").count() == 0
    evaluation = project_outcome.evaluate(project)
    assert evaluation["overall_status"] == "SATISFIED"
    result = project_outcome.close_result_ready(project, evaluation)
    db.session.refresh(project)

    assert project.status == "REVIEW"
    assert result["artifact_version_id"] is not None
    assert project_outcome.result_ready_proof(project) is not None
    assert Escalation.query.filter_by(project_id=project.id, state="OPEN").count() == 0


def test_current_provider_side_effect_claim_is_at_most_once(ctx):
    """The durable dispatch claim is idempotent independently of budget sizing.

    Positive-cost budget authority is release-gated by the dedicated budget tests.
    This invariant isolates the side-effect claim itself: the same idempotency
    key may create only one reservation/call claim and may increment call_count
    only once.  A zero-cost estimate is intentional so this test does not
    accidentally exercise a legacy Operation pricing cap instead of idempotency.
    """
    operation, _project, _researcher, _critic = multi._setup()
    before_calls = int(operation.call_count or 0)
    key = "release-provider-side-effect-at-most-once"
    first = reserve_provider_call(
        operation,
        stage="TASK_EXECUTION",
        estimate_twd=Decimal("0"),
        idempotency_key=key,
    )
    second = reserve_provider_call(
        operation,
        stage="TASK_EXECUTION",
        estimate_twd=Decimal("0"),
        idempotency_key=key,
    )
    db.session.refresh(operation)
    assert first.id == second.id
    assert int(operation.call_count or 0) == before_calls + 1


def test_current_sdk_clients_disable_hidden_retries(monkeypatch):
    patch0061a.test_real_sdk_clients_disable_hidden_retries(monkeypatch)


@pytest.mark.parametrize("reason", ["max_tokens", "max_output_tokens"])
def test_current_output_truncation_is_explicit_and_cost_preserved(ctx, monkeypatch, reason):
    patch0072.test_provider_truncation_maps_to_output_truncated_and_keeps_cost(
        ctx, monkeypatch, reason
    )


def test_current_exact_cost_uses_provider_usage(ctx, monkeypatch):
    """Current execution settles exact provider-reported token usage once."""
    employee = Employee.query.filter_by(slug="researcher").one()
    model = employee.current_model
    model.input_price_per_million = Decimal("1000")
    model.output_price_per_million = Decimal("2000")
    model.currency = "TWD"
    db.session.commit()

    class Provider:
        def complete(self, *args, **kwargs):
            return ProviderResult(
                text="ok",
                input_tokens=20,
                output_tokens=5,
                response_id="release-response",
                request_id="release-request",
            )

    monkeypatch.setattr("eason_one.services.execution.get_provider", lambda key: Provider())
    run = execute(employee, "TEST", "current exact-cost contract")
    assert run.status == "SUCCEEDED"
    assert Decimal(run.real_cost) == Decimal("0.030000")
    assert CostEvent.query.filter_by(agent_run_id=run.id, category="MODEL").count() == 1


def test_current_cost_recording_is_idempotent(ctx):
    hardening.test_recording_same_run_does_not_duplicate_cost(ctx)


def test_current_budget_preflight_blocks_before_provider(ctx, monkeypatch):
    """Current pre-dispatch budget failure is durable FAILED_SAFE, not an exception contract."""
    employee = Employee.query.filter_by(slug="researcher").one()
    employee.current_model.output_price_per_million = Decimal("10000000")
    employee.current_model.input_price_per_million = Decimal("10000000")
    db.session.commit()
    called = []

    def provider_should_not_be_constructed(key):
        called.append(key)
        raise AssertionError("provider must not be constructed after budget preflight rejection")

    monkeypatch.setattr("eason_one.services.execution.get_provider", provider_should_not_be_constructed)
    run = execute(employee, "TEST", "current budget preflight contract")
    assert called == []
    assert run.status == "FAILED"
    assert run.outcome == "FAILED_SAFE"
    assert run.failure_stage == "PRE_DISPATCH"
    assert run.failure_reason == "AUTHORITY_BLOCKED"


def test_current_agent_run_provider_price_snapshots_are_immutable(ctx):
    hardening.test_agent_run_snapshots_are_immutable(ctx)


def test_current_accepted_work_becomes_persistent_employee_experience(ctx):
    employee_memory.test_accepted_work_becomes_outcome_backed_employee_experience(ctx)


def _fresh_engineering_plan(ctx, *, write_scope):
    ceo = Employee.query.filter_by(slug="ceo").one()
    engineer = Employee.query.filter_by(slug="engineer").one()
    return {
        "mode": "OPERATION_PLAN",
        "executive_response": "Create one bounded HTML deliverable.",
        "project": {
            "name": "Fresh engineering contract",
            "objective": "Produce one bounded HTML deliverable.",
            "priority": "HIGH",
            "success_criteria": ["The approved HTML deliverable exists and is reviewable."],
            "constraints": [],
            "deadline": None,
        },
        "project_id": None,
        "operation": {
            "title": "Create bounded HTML deliverable",
            "objective": "Create exactly the approved HTML file.",
            "project_id": None,
            "budget_twd": 1,
            "tasks": [{
                "title": "Create fresh_project_output/result.html",
                "objective": "Create exactly fresh_project_output/result.html and no other file.",
                "assignee_employee_id": engineer.id,
                "reviewer_employee_id": engineer.id,
                "acceptance_criteria": [
                    "fresh_project_output/result.html exists and no other repository path is changed."
                ],
                "required_capabilities": ["SOFTWARE_ENGINEERING"],
                "write_scope": write_scope,
            }],
            "meeting_policy": "NEVER",
            "meeting_config": {
                "trigger": "NEVER", "participant_employee_ids": [],
                "max_rounds": 1, "max_speakers_per_round": 1,
                "contribution_output_cap": 192, "token_limit": 6000,
                "budget_twd": 0, "retry_limit": 0,
            },
            "completion_criteria": ["The approved HTML deliverable is persisted with evidence."],
        },
    }


def test_current_fresh_engineering_plan_requires_exact_scope_before_founder_approval(ctx):
    """Writable engineering authority must be complete before Founder approval."""
    missing = _fresh_engineering_plan(ctx, write_scope=None)
    with pytest.raises(ValueError, match="explicit CODEX_WRITE_SCOPE_V1"):
        operations.validate_plan(missing)

    exact = _fresh_engineering_plan(ctx, write_scope={
        "version": "CODEX_WRITE_SCOPE_V1",
        "paths": ["fresh_project_output/result.html"],
    })
    validated = operations.validate_plan(exact)
    task = validated["operation"]["tasks"][0]
    assert task["write_scope"] == {
        "version": "CODEX_WRITE_SCOPE_V1",
        "paths": ["fresh_project_output/result.html"],
    }


def test_current_fresh_engineering_scope_becomes_work_deliverable_truth(ctx):
    """Approved exact paths survive materialization instead of becoming 'Operation result'."""
    plan = operations.validate_plan(_fresh_engineering_plan(ctx, write_scope={
        "version": "CODEX_WRITE_SCOPE_V1",
        "paths": ["fresh_project_output/result.html"],
    }))
    project = core._project(("The approved HTML deliverable exists and is reviewable.",))
    operation = core._operation(project)
    operation.plan_json = deepcopy(plan)
    operation.plan_json["project"] = None
    operation.plan_json["project_id"] = project.id
    operation.plan_json["operation"]["project_id"] = project.id
    db.session.flush()

    works = work_runtime.materialize_operation_works(operation, operation.plan_json["operation"])
    delivery = [work for work in works if work.work_type != "MANAGEMENT"][0]
    task = work_runtime.task_for_work(delivery)
    control = dict(delivery.runtime_control_json or {})

    assert delivery.expected_output == "fresh_project_output/result.html"
    assert task.required_output == "fresh_project_output/result.html"
    assert control["codex_write_scope"]["paths"] == ["fresh_project_output/result.html"]
    assert control["deliverable_targets"] == {
        "version": "DELIVERABLE_TARGETS_V1",
        "kind": "REPOSITORY_PATHS",
        "paths": ["fresh_project_output/result.html"],
        "source": "APPROVED_OPERATION_PLAN",
    }
    assert control["codex_execution_boundary"]["allowed_paths"] == ["fresh_project_output/result.html"]


def test_current_explicit_read_only_engineering_can_use_null_scope(ctx):
    """Null scope means no write authority, never an implicit broad write grant."""
    plan = _fresh_engineering_plan(ctx, write_scope=None)
    task = plan["operation"]["tasks"][0]
    task["title"] = "Read-only engineering analysis"
    task["objective"] = "Read-only analysis only; do not modify files."
    task["acceptance_criteria"] = ["No file changes are made; return analysis evidence only."]
    validated = operations.validate_plan(plan)
    assert validated["operation"]["tasks"][0]["write_scope"] is None


def _ceo_founder_recovery_payload(ctx):
    ceo = Employee.query.filter_by(slug="ceo").one()
    researcher = Employee.query.filter_by(slug="researcher").one()
    return {
        "mode": "OPERATION_PLAN",
        "executive_response": "Research the market and return one bounded decision brief.",
        "project": {
            "name": "Fresh CEO planning recovery",
            "objective": "Research a bounded market question and produce a decision brief.",
            "priority": "HIGH",
            "success_criteria": ["A sourced decision brief is produced and reviewable."],
            "constraints": ["Stay inside the Founder-approved budget."],
            "deadline": None,
        },
        "project_id": None,
        "operation": {
            "title": "Research bounded market question",
            "objective": "Research current evidence and produce a decision brief.",
            "project_id": None,
            "budget_twd": 2,
            "tasks": [{
                "title": "Research current market evidence",
                "objective": "Collect current evidence needed for the Founder decision.",
                "assignee_employee_id": researcher.id,
                "reviewer_employee_id": None,
                "acceptance_criteria": ["The brief cites current evidence and supports a clear recommendation."],
                "required_capabilities": ["RESEARCH"],
                "write_scope": None,
            }],
            "meeting_policy": "NEVER",
            "meeting_config": {
                "trigger": "NEVER", "participant_employee_ids": [],
                "max_rounds": 1, "max_speakers_per_round": 1,
                "contribution_output_cap": 192, "token_limit": 6000,
                "budget_twd": 0, "retry_limit": 0,
            },
            "completion_criteria": ["A sourced decision brief supports a clear Founder decision."],
        },
    }


def test_current_ceo_founder_planning_uses_full_model_output_envelope(ctx, monkeypatch):
    """Founder planning must not impose a stale ceiling below ModelConfig."""
    from eason_one.services.ceo import founder_request

    ceo = Employee.query.filter_by(slug="ceo").one()
    ceo.current_model.max_output_tokens = 4096
    db.session.commit()
    seen = []
    payload = _ceo_founder_recovery_payload(ctx)

    class Provider:
        def complete(self, *args, **kwargs):
            seen.append(int(args[4]))
            return ProviderResult(json.dumps(payload), 20, 20)

    monkeypatch.setattr("eason_one.services.execution.get_provider", lambda _key: Provider())
    run, operation = founder_request(
        ceo, "Research current AI skill marketplaces and prepare a decision brief."
    )
    assert run.status == "SUCCEEDED"
    assert operation is not None
    assert seen == [4096]


def test_current_ceo_founder_truncation_recovers_once_without_founder_intervention(ctx, monkeypatch):
    """Known planning truncation is Company-owned and gets one same-provider compact retry."""
    from eason_one.services.ceo import founder_request

    ceo = Employee.query.filter_by(slug="ceo").one()
    ceo.current_model.max_output_tokens = 4096
    db.session.commit()
    payload = _ceo_founder_recovery_payload(ctx)
    calls = []

    class Provider:
        def complete(self, *args, **kwargs):
            calls.append((int(args[4]), args[1]))
            if len(calls) == 1:
                return ProviderResult(
                    '{"mode":"OPERATION_PLAN","executive_response":"partial"',
                    20, 4096, status="incomplete",
                    incomplete_reason="max_output_tokens",
                    stop_reason="max_output_tokens",
                )
            return ProviderResult(json.dumps(payload), 20, 1200)

    provider = Provider()
    monkeypatch.setattr("eason_one.services.execution.get_provider", lambda _key: provider)
    before_operations = operations.Operation.query.count() if hasattr(operations, "Operation") else None
    run, operation = founder_request(
        ceo, "Research current AI skill marketplaces and prepare a decision brief."
    )
    runs = AgentRun.query.filter_by(purpose="CEO_FOUNDER_REQUEST").order_by(AgentRun.id).all()
    first, second = runs[-2], runs[-1]

    assert len(calls) == 2
    assert [cap for cap, _prompt in calls] == [4096, 4096]
    assert "CEO_FOUNDER_REQUEST_TRUNCATION_RECOVERY" in calls[1][1]
    assert first.failure_reason == "OUTPUT_TRUNCATED"
    assert first.replacement_run_id == second.id
    assert first.resolution_status == "REPLACED_SUCCEEDED"
    assert second.retry_of_run_id == first.id
    assert second.attempt_number == first.attempt_number + 1
    assert run.id == second.id and run.status == "SUCCEEDED"
    assert operation is not None


def test_current_ceo_founder_request_id_reuses_completed_paid_plan_without_provider_recall(ctx, monkeypatch):
    """Repeating one browser request id returns the same durable paid planning result."""
    from eason_one.services.ceo import founder_request

    ceo = Employee.query.filter_by(slug="ceo").one()
    payload = _ceo_founder_recovery_payload(ctx)
    calls = []

    class Provider:
        def complete(self, *args, **kwargs):
            calls.append(1)
            return ProviderResult(json.dumps(payload), 20, 20)

    monkeypatch.setattr("eason_one.services.execution.get_provider", lambda _key: Provider())
    request_text = "Research current AI skill marketplaces and prepare a decision brief."
    run1, operation1 = founder_request(
        ceo, request_text, request_id="founder-http-request-1"
    )
    run2, operation2 = founder_request(
        ceo, request_text, request_id="founder-http-request-1"
    )

    assert calls == [1]
    assert run1.id == run2.id
    assert operation1 is not None and operation2 is not None
    assert operation1.id == operation2.id
    assert (run1.context_composition_json or {}).get("founder_request_id") == "founder-http-request-1"


def test_current_ceo_founder_request_id_recovers_paid_plan_after_materialization_crash(ctx, monkeypatch):
    """A crash after provider persistence must materialize from the paid Run, not buy another plan."""
    from eason_one.services.ceo import founder_request

    ceo = Employee.query.filter_by(slug="ceo").one()
    payload = _ceo_founder_recovery_payload(ctx)
    calls = []

    class Provider:
        def complete(self, *args, **kwargs):
            calls.append(1)
            return ProviderResult(json.dumps(payload), 20, 20)

    monkeypatch.setattr("eason_one.services.execution.get_provider", lambda _key: Provider())
    original_propose = operations.propose_operation

    def crash_before_materialization(*args, **kwargs):
        raise SystemExit("simulated process death after paid CEO response")

    monkeypatch.setattr(operations, "propose_operation", crash_before_materialization)
    request_text = "Research current AI skill marketplaces and prepare a decision brief."
    with pytest.raises(SystemExit):
        founder_request(ceo, request_text, request_id="founder-http-request-crash")
    db.session.rollback()

    paid_run = AgentRun.query.filter_by(
        purpose="CEO_FOUNDER_REQUEST"
    ).order_by(AgentRun.id.desc()).first()
    assert paid_run is not None and paid_run.status == "SUCCEEDED"
    assert paid_run.operation_id is None
    assert calls == [1]

    monkeypatch.setattr(operations, "propose_operation", original_propose)
    recovered_run, operation = founder_request(
        ceo, request_text, request_id="founder-http-request-crash"
    )

    assert recovered_run.id == paid_run.id
    assert operation is not None
    assert recovered_run.operation_id == operation.id
    assert calls == [1]


def test_current_ceo_founder_request_id_conflict_fails_closed_without_second_provider_call(ctx, monkeypatch):
    """One id can never be rebound to different Founder text or scope."""
    from eason_one.services.ceo import founder_request

    ceo = Employee.query.filter_by(slug="ceo").one()
    payload = _ceo_founder_recovery_payload(ctx)
    calls = []

    class Provider:
        def complete(self, *args, **kwargs):
            calls.append(1)
            return ProviderResult(json.dumps(payload), 20, 20)

    monkeypatch.setattr("eason_one.services.execution.get_provider", lambda _key: Provider())
    founder_request(
        ceo, "Research one bounded market question.",
        request_id="founder-http-request-conflict",
    )
    with pytest.raises(ValueError, match="FOUNDER_REQUEST_ID_CONFLICT"):
        founder_request(
            ceo, "Build a completely different product.",
            request_id="founder-http-request-conflict",
        )
    assert calls == [1]


def test_current_ceo_founder_truncation_retry_is_bounded_to_one(ctx, monkeypatch):
    """Two truncations fail visibly; runtime must not enter an unbounded paid loop."""
    from eason_one.services.ceo import founder_request

    ceo = Employee.query.filter_by(slug="ceo").one()
    ceo.current_model.max_output_tokens = 4096
    db.session.commit()
    calls = []
    before_operations = __import__("eason_one.models", fromlist=["Operation"]).Operation.query.count()

    class Provider:
        def complete(self, *args, **kwargs):
            calls.append(int(args[4]))
            return ProviderResult(
                '{"mode":"OPERATION_PLAN","executive_response":"partial"',
                20, 4096, status="incomplete",
                incomplete_reason="max_output_tokens",
                stop_reason="max_output_tokens",
            )

    monkeypatch.setattr("eason_one.services.execution.get_provider", lambda _key: Provider())
    run, operation = founder_request(
        ceo, "Research current AI skill marketplaces and prepare a decision brief."
    )
    after_operations = __import__("eason_one.models", fromlist=["Operation"]).Operation.query.count()

    assert calls == [4096, 4096]
    assert run.status == "FAILED"
    assert run.failure_reason == "OUTPUT_TRUNCATED"
    assert operation is None
    assert after_operations == before_operations
    recent = AgentRun.query.filter_by(purpose="CEO_FOUNDER_REQUEST").order_by(AgentRun.id.desc()).limit(2).all()
    assert len(recent) == 2
    assert all(row.failure_reason == "OUTPUT_TRUNCATED" for row in recent)



def _pulse_running_execution(ctx):
    operation, project, researcher, _critic = multi._setup()
    work = sorted(
        [row for row in operation.works if row.work_type != "MANAGEMENT"],
        key=lambda row: row.id,
    )[0]
    if work.state == "READY":
        work_runtime.transition(
            work, "EXECUTING", actor_type="EMPLOYEE", actor_id=researcher.id,
            reason="Company Pulse current execution fixture",
        )
    task = work_runtime.task_for_work(work)
    model = researcher.current_model
    run = AgentRun(
        employee_id=researcher.id,
        project_id=project.id,
        operation_id=operation.id,
        work_id=work.id,
        task_id=task.id if task else None,
        model_config_id=model.id,
        purpose="TASK_EXECUTION",
        user_request=work.purpose,
        system_prompt_snapshot="company-pulse-v1-test",
        context_snapshot="company-pulse-v1-test",
        status="RUNNING",
        outcome="RUNNING",
        provider_key_snapshot=model.provider_key,
        model_name_snapshot=model.model_name,
        input_price_snapshot=model.input_price_per_million,
        output_price_snapshot=model.output_price_per_million,
        request_price_snapshot=getattr(model, "request_price_per_call", 0) or 0,
        currency_snapshot=model.currency,
        currency=model.currency,
        effective_max_output_tokens=model.max_output_tokens,
    )
    db.session.add(run); db.session.flush()
    effect = ExternalEffectAttempt(
        execution_id=run.id,
        work_id=work.id,
        operation_id=operation.id,
        provider=model.provider_key,
        effect_kind="MODEL_INFERENCE",
        request_fingerprint=f"pulse-test-{run.id}",
        idempotency_key=f"pulse-test-{run.id}",
        state="DISPATCHING",
        estimated_cost_twd=Decimal("0"),
        dispatched_at=now(),
    )
    db.session.add(effect)
    company_events.emit(
        "EXECUTION_STARTED", actor_type="EMPLOYEE", actor_id=researcher.id,
        project_id=project.id, work_id=work.id, execution_id=run.id,
        correlation_id=f"work:{work.id}",
        payload={"provider": model.provider_key, "model": model.model_name},
    )
    db.session.commit()
    return operation, project, researcher, work, run, effect


def test_current_company_pulse_exposes_real_running_work_provider_and_elapsed(ctx, client):
    """Founder sees persisted live execution truth, not a generic ACTIVE label."""
    _operation, project, researcher, work, run, _effect = _pulse_running_execution(ctx)
    pulse = project_company.company_pulse_snapshot()
    row = next(item for item in pulse["rows"] if item["employee"].id == researcher.id)

    assert row["project"].id == project.id
    assert row["work"].id == work.id
    assert row["run"].id == run.id
    assert row["status"] == "WORKING"
    assert row["provider"] == f"{run.provider_key_snapshot} / {run.model_name_snapshot}"
    assert row["elapsed"] is not None
    assert "Provider request dispatched" in row["detail"]
    assert pulse["counts"]["WORKING"] >= 1

    html = client.get("/headquarters/pulse").get_data(as_text=True)
    assert "COMPANY NOW" in html
    assert researcher.name in html
    assert "WORKING" in html
    assert "Provider request dispatched" in html
    assert f"/headquarters/system/runs/{run.id}" in html


def test_current_company_pulse_calls_internal_reconciliation_recovering_not_founder(ctx):
    """Company-owned recovery is visibly RECOVERING and does not masquerade as NEEDS YOU."""
    operation, project, researcher, _critic = multi._setup()
    work = sorted(
        [row for row in operation.works if row.work_type != "MANAGEMENT"],
        key=lambda row: row.id,
    )[0]
    work_runtime.open_wait(
        work, "RECONCILIATION",
        "Provider effect is being reconciled by Company Runtime.",
        resume_state="READY", issue_code="PULSE_TEST_RECONCILIATION",
    )
    db.session.commit()

    pulse = project_company.company_pulse_snapshot()
    row = next(item for item in pulse["rows"] if item["employee"].id == researcher.id)
    assert row["project"].id == project.id
    assert row["status"] == "RECOVERING"
    assert "Founder intervention is not required" in row["next_step"]
    assert pulse["counts"]["RECOVERING"] >= 1


def test_current_company_pulse_poll_is_read_only(ctx, client):
    """Polling observability must not wake runtime, call providers, or write Company Truth."""
    _pulse_running_execution(ctx)
    before = {
        "events": CompanyEvent.query.count(),
        "runs": AgentRun.query.count(),
        "effects": ExternalEffectAttempt.query.count(),
        "works": Work.query.count(),
    }
    first = client.get("/headquarters/pulse")
    second = client.get("/headquarters/pulse")
    after = {
        "events": CompanyEvent.query.count(),
        "runs": AgentRun.query.count(),
        "effects": ExternalEffectAttempt.query.count(),
        "works": Work.query.count(),
    }
    assert first.status_code == 200 and second.status_code == 200
    assert first.headers.get("Cache-Control") == "no-store, max-age=0"
    assert before == after



def test_current_persistent_employee_learning_changes_future_staffing_tie_break(ctx):
    """Accepted cross-Project experience must be consumed by future staffing."""
    from eason_one.services import team_formation

    position = Position.query.filter_by(name="Employee").one()
    model = Employee.query.filter_by(slug="researcher").one().current_model
    experienced = Employee(
        name="Growth Specialist A", slug="growth-specialist-a", position_id=position.id,
        role_description="Marketing, growth, campaign and acquisition specialist.",
        system_instructions="Deliver governed marketing work.",
        current_model_config_id=model.id, salary_credits_per_week=0,
    )
    newcomer = Employee(
        name="Growth Specialist B", slug="growth-specialist-b", position_id=position.id,
        role_description="Marketing, growth, campaign and acquisition specialist.",
        system_instructions="Deliver governed marketing work.",
        current_model_config_id=model.id, salary_credits_per_week=0,
    )
    db.session.add_all([experienced, newcomer]); db.session.flush()
    db.session.add(EmployeeLearningRecord(
        employee_id=experienced.id,
        learning_type="WORK_EXPERIENCE",
        validation_basis="CANONICAL_WORK_ACCEPTANCE",
        evidence_json={"capability": "MARKETING", "execution_attempt_ids": [901]},
        title="Accepted owner experience — launch validation",
        content="Accepted owner experience on a prior governed marketing Work.",
        source_ref="WORK:901/ARTIFACT_VERSION:901/ACCEPTANCE:901",
        validated=True,
    ))
    db.session.flush()

    selected = team_formation.best_existing_employee("MARKETING")
    assert selected.id == experienced.id
    evidence = team_formation.staffing_selection_evidence("MARKETING", selected)
    assert evidence["selected_employee_id"] == experienced.id
    assert evidence["experience_influenced_selection"] is True
    assert evidence["candidates"][0]["accepted_owner_experience"] == 1
    assert "accepted owner outcome" in evidence["reason"]


def test_current_learning_never_invents_employee_capability(ctx):
    """Learning can rank eligible staff, but cannot grant a missing role capability."""
    from eason_one.services import team_formation

    position = Position.query.filter_by(name="Employee").one()
    model = Employee.query.filter_by(slug="researcher").one().current_model
    learner = Employee(
        name="General Learner", slug="general-learner", position_id=position.id,
        role_description="General company assistant with no marketing responsibility.",
        system_instructions="Assist only inside assigned authority.",
        current_model_config_id=model.id, salary_credits_per_week=0,
    )
    db.session.add(learner); db.session.flush()
    db.session.add(EmployeeLearningRecord(
        employee_id=learner.id,
        learning_type="WORK_EXPERIENCE",
        validation_basis="CANONICAL_WORK_ACCEPTANCE",
        evidence_json={"capability": "FINANCE", "execution_attempt_ids": [902]},
        title="Accepted owner experience — finance artifact",
        content="Accepted finance-shaped evidence from a prior Work.",
        source_ref="WORK:902/ARTIFACT_VERSION:902/ACCEPTANCE:902",
        validated=True,
    ))
    db.session.flush()

    assert "FINANCE" not in team_formation.employee_capabilities(learner)
    assert team_formation.best_existing_employee("FINANCE") is None


def test_current_employee_dossier_exposes_outcome_backed_evolution(ctx):
    from eason_one.services import headquarters

    researcher = Employee.query.filter_by(slug="researcher").one()
    db.session.add(EmployeeLearningRecord(
        employee_id=researcher.id,
        learning_type="WORK_EXPERIENCE",
        validation_basis="CANONICAL_WORK_ACCEPTANCE",
        evidence_json={"capability": "RESEARCH", "execution_attempt_ids": [903, 904]},
        title="Accepted owner experience — research",
        content="Accepted research outcome after one recovery attempt.",
        source_ref="WORK:903/ARTIFACT_VERSION:903/ACCEPTANCE:903",
        validated=True,
    ))
    db.session.flush()

    snapshot = headquarters.employee_snapshot(researcher)
    research = next(row for row in snapshot["evolution"]["capabilities"] if row["capability"] == "RESEARCH")
    assert research["owner_acceptances"] == 1
    assert research["recovery_burden"] == 1
    assert snapshot["evolution"]["canonical_record_count"] == 1


def test_current_project_cost_breakdown_is_persisted_truth_only(ctx):
    from eason_one.models import Company
    from eason_one.services import project_company

    company = Company.query.one()
    ceo = Employee.query.filter_by(slug="ceo").one()
    researcher = Employee.query.filter_by(slug="researcher").one()
    project = Project(
        name="Cost truth project", objective="Show exact persisted spend", status="ACTIVE",
        priority="HIGH", environment="LIVE", owner_employee_id=ceo.id,
        real_budget_limit=Decimal("10"),
    )
    db.session.add(project); db.session.flush()
    db.session.add_all([
        CostEvent(
            company_id=company.id, employee_id=researcher.id, project_id=project.id,
            stage="RESEARCH", category="MODEL", description="Research call",
            real_cost_delta=Decimal("1.25"), currency="TWD",
        ),
        CostEvent(
            company_id=company.id, employee_id=ceo.id, project_id=project.id,
            stage="REVIEW", category="MODEL", description="Review call",
            real_cost_delta=Decimal("0.75"), currency="TWD",
        ),
    ])
    db.session.flush()

    breakdown = project_company._project_cost_breakdown(project, {
        "budget_limit": Decimal("10"), "budget_remaining": Decimal("8"),
    })
    assert breakdown["spent"] == Decimal("2.00")
    assert breakdown["remaining"] == Decimal("8")
    assert {row["employee_name"] for row in breakdown["by_employee"]} == {"CEO", "Researcher"}
    note = breakdown["truth_note"]
    assert note.startswith("Observed local model cost is a billing ledger.")
    assert "Project execution authority is a separate Founder-approved envelope" in note



def test_current_research_surface_separates_roadmap_from_proven_evidence(ctx):
    from eason_one.services import research

    snapshot = research.snapshot()
    roadmap = {row["name"]: row["status"] for row in snapshot["vision"]["roadmap"]}
    assert roadmap["AI-native Company OS"] == "MANDATORY CORE"
    assert roadmap["Learning & Evolution"] == "ACTIVE BUILD · CONSUMPTION V1.4"
    assert roadmap["Eason Market / AI Employee Economy"] == "ACTIVE BUILD · INTERNAL V1.3"
    assert roadmap["Software ↔ Physical Production"] == "FROZEN"
    assert any("fresh full-Project" in row for row in snapshot["vision"]["boundaries"])
    assert all("value" in row and "detail" in row for row in snapshot["vision"]["evidence"])



def test_current_founder_intelligence_surfaces_render(ctx, client):
    researcher = Employee.query.filter_by(slug="researcher").one()
    response = client.get(f"/headquarters/employees/{researcher.id}")
    assert response.status_code == 200
    assert b"CAPABILITY MEMORY" in response.data

    research_response = client.get("/headquarters/research")
    assert research_response.status_code == 200
    assert b"RESEARCH NORTH STAR" in research_response.data
    assert b"ORIGINAL EASON ONE TARGET" in research_response.data

    _operation, project, _researcher, _critic = multi._setup()
    project_response = client.get(f"/headquarters/projects/{project.id}")
    assert project_response.status_code == 200
    assert b"COST TRUTH" in project_response.data
    assert b"OBSERVED MODEL COST" in project_response.data
    assert b"AUTHORITY LEFT" in project_response.data


def test_current_eason_market_awards_only_eligible_internal_labor_without_touching_twd(ctx):
    """Internal EC market may allocate eligible labor, never mutate real Project budget."""
    from eason_one.models import MarketContract, MarketOffer, MarketOrder
    from eason_one.services import market, team_formation

    ceo = Employee.query.filter_by(slug="ceo").one()
    position = Position.query.filter_by(name="Employee").one()
    model = Employee.query.filter_by(slug="researcher").one().current_model
    low_quote = Employee(
        name="Market Specialist A", slug="market-specialist-a", position_id=position.id,
        role_description="Marketing, growth, acquisition and go-to-market specialist.",
        system_instructions="Deliver governed marketing work.",
        current_model_config_id=model.id, salary_credits_per_week=Decimal("0"),
    )
    high_quote = Employee(
        name="Market Specialist B", slug="market-specialist-b", position_id=position.id,
        role_description="Marketing, growth, acquisition and go-to-market specialist.",
        system_instructions="Deliver governed marketing work.",
        current_model_config_id=model.id, salary_credits_per_week=Decimal("1000"),
    )
    db.session.add_all([low_quote, high_quote]); db.session.flush()
    project = Project(
        name="Internal market proof", objective="Allocate one marketing Work",
        status="ACTIVE", priority="HIGH", environment="LIVE",
        owner_employee_id=ceo.id, real_budget_limit=Decimal("10"),
    )
    db.session.add(project); db.session.flush()
    work = Work(
        project_id=project.id, title="Validate launch positioning",
        purpose="Deliver marketing analysis", expected_output="Decision-ready positioning",
        acceptance_criteria="Evidence-backed result", state="READY", work_type="DELIVERY",
        resource_ceiling_twd=Decimal("3"), retry_limit=1,
        runtime_control_json={"staffing_requirements": {"required_capabilities": ["MARKETING"]}},
    )
    db.session.add(work); db.session.flush()
    ranked = [
        row for row in team_formation.ranked_existing_employees("MARKETING")
        if row[5].id in {low_quote.id, high_quote.id}
    ]
    before_twd = project.real_budget_limit
    before_costs = CostEvent.query.filter_by(project_id=project.id).count()
    award = market.award_internal_work(work, "MARKETING", ranked, opened_by_employee_id=ceo.id)
    db.session.flush()

    assert award["employee"].id == low_quote.id
    assert MarketOrder.query.filter_by(work_id=work.id).count() == 1
    assert MarketOffer.query.filter_by(order_id=award["order"].id).count() == 2
    assert MarketContract.query.filter_by(work_id=work.id).one().currency == "EC"
    assert project.real_budget_limit == before_twd
    assert CostEvent.query.filter_by(project_id=project.id).count() == before_costs


def test_current_market_contract_evidence_is_json_native_inside_work_control(ctx):
    """Market evidence persisted into Work JSON must never leak Decimal/datetime objects."""
    from eason_one.services import market, team_formation

    ceo = Employee.query.filter_by(slug="ceo").one()
    researcher = Employee.query.filter_by(slug="researcher").one()
    project = Project(
        name="Market JSON evidence proof", objective="Persist one market contract envelope",
        status="ACTIVE", priority="MEDIUM", environment="LIVE",
        owner_employee_id=ceo.id, real_budget_limit=Decimal("10"),
    )
    db.session.add(project); db.session.flush()
    work = Work(
        project_id=project.id, title="Research evidence contract",
        purpose="Produce governed research", expected_output="Verified research result",
        acceptance_criteria="Pass verification", state="READY", work_type="DELIVERY",
        resource_ceiling_twd=Decimal("2"), retry_limit=1,
        runtime_control_json={"staffing_requirements": {"required_capabilities": ["RESEARCH"]}},
    )
    db.session.add(work); db.session.flush()
    ranked = team_formation.ranked_existing_employees("RESEARCH")
    award = market.award_internal_work(
        work, "RESEARCH", ranked, opened_by_employee_id=ceo.id,
        preferred_employee_id=researcher.id,
    )
    evidence = market.contract_evidence(award["contract"])

    # This is the exact serialization boundary that failed in the V1.3 installer.
    json.dumps(evidence)
    work.runtime_control_json = {
        "staffing_requirements": {"required_capabilities": ["RESEARCH"]},
        "team_formation": {"selection_evidence": {"market_contract": evidence}},
    }
    db.session.flush()
    assert isinstance(evidence["agreed_ec"], str)


def test_current_eason_market_settles_once_only_after_accepted_artifact_evidence(ctx):
    """Market payment is verification-bound and idempotent."""
    from eason_one.models import MarketLedgerEntry
    from eason_one.services import market, team_formation

    ceo = Employee.query.filter_by(slug="ceo").one()
    position = Position.query.filter_by(name="Employee").one()
    model = Employee.query.filter_by(slug="researcher").one().current_model
    seller = Employee(
        name="Settlement Specialist", slug="settlement-specialist", position_id=position.id,
        role_description="Marketing, growth and acquisition specialist.",
        system_instructions="Deliver governed marketing work.",
        current_model_config_id=model.id, salary_credits_per_week=0,
    )
    db.session.add(seller); db.session.flush()
    project = Project(
        name="Market settlement proof", objective="Settle verified labor",
        status="ACTIVE", priority="MEDIUM", environment="LIVE",
        owner_employee_id=ceo.id, real_budget_limit=Decimal("10"),
    )
    db.session.add(project); db.session.flush()
    work = Work(
        project_id=project.id, title="Marketing result", purpose="Produce accepted marketing output",
        expected_output="Verified output", acceptance_criteria="Pass verification",
        state="READY", work_type="DELIVERY", resource_ceiling_twd=Decimal("2"), retry_limit=1,
        runtime_control_json={"staffing_requirements": {"required_capabilities": ["MARKETING"]}},
    )
    db.session.add(work); db.session.flush()
    ranked = [row for row in team_formation.ranked_existing_employees("MARKETING") if row[5].id == seller.id]
    award = market.award_internal_work(work, "MARKETING", ranked, opened_by_employee_id=ceo.id)
    contract = award["contract"]
    assert market.wallet_balance(seller.id) == Decimal("0.00")

    artifact = Artifact(project_id=project.id, work_id=work.id, artifact_type="WORK_RESULT", title="Accepted marketing output")
    db.session.add(artifact); db.session.flush()
    version = ArtifactVersion(
        artifact_id=artifact.id, version=1, producer_employee_id=seller.id,
        status="ACCEPTED", content_text="verified", content_hash="a" * 64, accepted_at=now(),
    )
    db.session.add(version); db.session.flush()
    acceptance = VerificationRecord(
        work_id=work.id, artifact_version_id=version.id, verifier_employee_id=None,
        method="ACCEPTANCE_CONTRACT", status="PASSED", details_json={"proof": True},
    )
    db.session.add(acceptance)
    work.state = "ACCEPTED"; work.accepted_at = now(); db.session.flush()

    market.settle_accepted_work(work, version, acceptance)
    first_balance = market.wallet_balance(seller.id)
    market.settle_accepted_work(work, version, acceptance)
    db.session.flush()

    assert contract.status == "SETTLED"
    assert contract.artifact_version_id == version.id
    assert contract.verification_record_id == acceptance.id
    assert first_balance == Decimal(contract.agreed_ec).quantize(Decimal("0.01"))
    assert market.wallet_balance(seller.id) == first_balance
    assert MarketLedgerEntry.query.filter_by(contract_id=contract.id, entry_type="WORK_ACCEPTANCE_PAYMENT").count() == 1


def test_current_eason_market_blocks_payment_when_accepted_producer_is_not_contracted_seller(ctx):
    """Artifact acceptance cannot pay the wrong Persistent Employee."""
    from eason_one.models import MarketLedgerEntry
    from eason_one.services import market, team_formation

    ceo = Employee.query.filter_by(slug="ceo").one()
    researcher = Employee.query.filter_by(slug="researcher").one()
    project = Project(
        name="Market producer mismatch", objective="Fail closed on seller mismatch",
        status="ACTIVE", priority="MEDIUM", environment="LIVE",
        owner_employee_id=ceo.id, real_budget_limit=Decimal("10"),
    )
    db.session.add(project); db.session.flush()
    work = Work(
        project_id=project.id, title="Research contract", purpose="Research market evidence",
        expected_output="Research result", acceptance_criteria="Verified sources",
        state="READY", work_type="DELIVERY", resource_ceiling_twd=Decimal("2"), retry_limit=1,
        runtime_control_json={"staffing_requirements": {"required_capabilities": ["RESEARCH"]}},
    )
    db.session.add(work); db.session.flush()
    ranked = [row for row in team_formation.ranked_existing_employees("RESEARCH") if row[5].id == researcher.id]
    award = market.award_internal_work(work, "RESEARCH", ranked, opened_by_employee_id=ceo.id)
    contract = award["contract"]

    artifact = Artifact(project_id=project.id, work_id=work.id, artifact_type="WORK_RESULT", title="Mismatch output")
    db.session.add(artifact); db.session.flush()
    version = ArtifactVersion(
        artifact_id=artifact.id, version=1, producer_employee_id=ceo.id,
        status="ACCEPTED", content_text="mismatch", content_hash="b" * 64, accepted_at=now(),
    )
    db.session.add(version); db.session.flush()
    acceptance = VerificationRecord(
        work_id=work.id, artifact_version_id=version.id, method="ACCEPTANCE_CONTRACT",
        status="PASSED", details_json={"proof": True},
    )
    db.session.add(acceptance)
    work.state = "ACCEPTED"; work.accepted_at = now(); db.session.flush()

    market.settle_accepted_work(work, version, acceptance)
    assert contract.status == "DISPUTED"
    assert market.wallet_balance(researcher.id) == Decimal("0.00")
    assert MarketLedgerEntry.query.filter_by(contract_id=contract.id).count() == 0


def test_current_eason_market_founder_surface_and_research_boundary_render(ctx, client):
    from eason_one.services import research

    response = client.get("/headquarters/market")
    assert response.status_code == 200
    assert b"EASON MARKET" in response.data
    assert b"Authority first. Market second. Payment last." in response.data

    snapshot = research.snapshot()
    roadmap = {row["name"]: row["status"] for row in snapshot["vision"]["roadmap"]}
    assert roadmap["Eason Market / AI Employee Economy"] == "ACTIVE BUILD \u00b7 INTERNAL V1.3"
    assert any("intra-company V1.3" in row for row in snapshot["vision"]["boundaries"])


def test_current_ceo_planning_consumes_canonical_company_learning_without_granting_authority(ctx):
    """Accepted employee history must enter CEO planning context as bounded evidence."""
    from eason_one.services import ceo_context

    ceo = Employee.query.filter_by(slug="ceo").one()
    researcher = Employee.query.filter_by(slug="researcher").one()
    db.session.add(EmployeeLearningRecord(
        employee_id=researcher.id,
        learning_type="WORK_EXPERIENCE",
        validation_basis="CANONICAL_WORK_ACCEPTANCE",
        evidence_json={"capability": "RESEARCH", "execution_attempt_ids": [1901]},
        title="Accepted owner experience — research planning context",
        content="Accepted research outcome from a prior governed Work.",
        source_ref="WORK:1901/ARTIFACT_VERSION:1901/ACCEPTANCE:1901",
        validated=True,
    ))
    # An unvalidated claim must not become company learning context.
    db.session.add(EmployeeLearningRecord(
        employee_id=researcher.id,
        learning_type="WORK_EXPERIENCE",
        validation_basis="MODEL_SELF_REPORT",
        evidence_json={"capability": "MARKETING", "execution_attempt_ids": [1902]},
        title="Unvalidated marketing claim",
        content="This must not be treated as learned authority.",
        source_ref="RUN:1902",
        validated=False,
    ))
    db.session.flush()

    composed = ceo_context.compose(ceo, founder_request="Plan a fresh evidence-backed research Project")
    assert "COMPANY LEARNING" in composed.text
    assert "RESEARCH: 1 owner acceptance(s)" in composed.text
    assert "Validated canonical outcome evidence available: 1 record(s)" in composed.text
    assert "MARKETING: 1 owner acceptance(s)" not in composed.text
    assert "cannot mint capability" in composed.text
    assert composed.composition["company_learning"]["included"] == 1


def test_current_market_compensation_evolves_from_accepted_experience(ctx):
    """Accepted experience changes future EC compensation, never capability eligibility."""
    from eason_one.models import MarketOffer
    from eason_one.services import market, team_formation

    ceo = Employee.query.filter_by(slug="ceo").one()
    position = Position.query.filter_by(name="Employee").one()
    model = Employee.query.filter_by(slug="researcher").one().current_model
    experienced = Employee(
        name="Proven Growth Specialist", slug="proven-growth-specialist", position_id=position.id,
        role_description="Marketing, growth, campaign and acquisition specialist.",
        system_instructions="Deliver governed marketing work.", current_model_config_id=model.id,
        salary_credits_per_week=0,
    )
    newcomer = Employee(
        name="New Growth Specialist", slug="new-growth-specialist", position_id=position.id,
        role_description="Marketing, growth, campaign and acquisition specialist.",
        system_instructions="Deliver governed marketing work.", current_model_config_id=model.id,
        salary_credits_per_week=0,
    )
    db.session.add_all([experienced, newcomer]); db.session.flush()
    db.session.add(EmployeeLearningRecord(
        employee_id=experienced.id,
        learning_type="WORK_EXPERIENCE",
        validation_basis="CANONICAL_WORK_ACCEPTANCE",
        evidence_json={"capability": "MARKETING", "execution_attempt_ids": [2001]},
        title="Accepted marketing owner outcome",
        content="One accepted governed marketing outcome.",
        source_ref="WORK:2001/ARTIFACT_VERSION:2001/ACCEPTANCE:2001",
        validated=True,
    ))
    project = Project(
        name="Compensation evolution proof", objective="Show experience changes internal compensation",
        status="ACTIVE", priority="MEDIUM", environment="LIVE", owner_employee_id=ceo.id,
        real_budget_limit=Decimal("10"),
    )
    db.session.add(project); db.session.flush()
    work = Work(
        project_id=project.id, title="Launch positioning", purpose="Produce governed marketing result",
        expected_output="Verified marketing result", acceptance_criteria="Pass verification",
        state="READY", work_type="DELIVERY", resource_ceiling_twd=Decimal("2"), retry_limit=1,
        runtime_control_json={"staffing_requirements": {"required_capabilities": ["MARKETING"]}},
    )
    db.session.add(work); db.session.flush()

    ranked = [
        row for row in team_formation.ranked_existing_employees("MARKETING")
        if row[5].id in {experienced.id, newcomer.id}
    ]
    award = market.award_internal_work(work, "MARKETING", ranked, opened_by_employee_id=ceo.id)
    db.session.flush()
    offers = {row.employee_id: row for row in MarketOffer.query.filter_by(order_id=award["order"].id).all()}

    assert award["employee"].id == experienced.id
    assert Decimal(offers[experienced.id].quote_ec) > Decimal(offers[newcomer.id].quote_ec)
    assert offers[experienced.id].rank_json["experience_score"] > offers[newcomer.id].rank_json["experience_score"]
    reputation = market.employee_market_reputation(experienced)
    assert reputation["band"] == "PROVEN"
    assert reputation["accepted_owner_outcomes"] == 1
    assert "cannot create capability" in reputation["truth_note"]


def test_current_research_surface_marks_behavioral_learning_and_internal_market_v12(ctx):
    from eason_one.services import research

    snapshot = research.snapshot()
    roadmap = {row["name"]: row["status"] for row in snapshot["vision"]["roadmap"]}
    assert roadmap["Learning & Evolution"] == "ACTIVE BUILD · CONSUMPTION V1.4"
    assert roadmap["Eason Market / AI Employee Economy"] == "ACTIVE BUILD · INTERNAL V1.3"
    assert any("intra-company V1.3" in row for row in snapshot["vision"]["boundaries"])



def test_current_plan_rejects_unknown_delivery_capability_before_founder_approval(ctx):
    """An approved Project may never materialize a Work the Company cannot staff semantically."""
    plan = _ceo_founder_recovery_payload(ctx)
    plan["operation"]["tasks"][0]["required_capabilities"] = ["QUANTUM_FORTUNE_TELLING"]
    with pytest.raises(ValueError, match="Unknown accountable capability"):
        operations.validate_plan(plan)


def test_current_learning_counterfactual_proves_when_experience_changes_staffing_behavior(ctx):
    """Behavioral learning proof compares against the same eligible roster without experience."""
    from eason_one.services import employee_evolution, team_formation

    position = Position.query.filter_by(name="Employee").one()
    model = Employee.query.filter_by(slug="researcher").one().current_model
    # Newcomer is created first, so deterministic no-learning tie-break prefers it.
    newcomer = Employee(
        name="Counterfactual Growth Newcomer", slug="counterfactual-growth-newcomer",
        position_id=position.id,
        role_description="Marketing, growth, campaign and acquisition specialist.",
        system_instructions="Deliver governed marketing work.",
        current_model_config_id=model.id, salary_credits_per_week=0,
    )
    experienced = Employee(
        name="Counterfactual Growth Proven", slug="counterfactual-growth-proven",
        position_id=position.id,
        role_description="Marketing, growth, campaign and acquisition specialist.",
        system_instructions="Deliver governed marketing work.",
        current_model_config_id=model.id, salary_credits_per_week=0,
    )
    db.session.add_all([newcomer, experienced]); db.session.flush()
    db.session.add(EmployeeLearningRecord(
        employee_id=experienced.id,
        learning_type="WORK_EXPERIENCE",
        validation_basis="CANONICAL_WORK_ACCEPTANCE",
        evidence_json={"capability": "MARKETING", "execution_attempt_ids": [2101]},
        title="Accepted owner experience — counterfactual marketing",
        content="Accepted governed marketing outcome.",
        source_ref="WORK:2101/ARTIFACT_VERSION:2101/ACCEPTANCE:2101",
        validated=True,
    ))
    db.session.flush()

    selected = team_formation.best_existing_employee("MARKETING")
    assert selected.id == experienced.id
    evidence = team_formation.staffing_selection_evidence("MARKETING", selected)
    assert evidence["schema"] == "STAFFING_SELECTION_EVIDENCE_V1_1"
    assert evidence["experience_influenced_selection"] is True
    assert evidence["behavior_changed_vs_no_learning"] is True
    assert evidence["counterfactual_without_learning"]["employee_id"] == newcomer.id

    project = Project(
        name="Behavioral evolution evidence", objective="Persist one causal staffing decision",
        status="ACTIVE", priority="MEDIUM", environment="LIVE",
        owner_employee_id=Employee.query.filter_by(slug="ceo").one().id,
        real_budget_limit=Decimal("5"),
    )
    db.session.add(project); db.session.flush()
    work = Work(
        project_id=project.id, title="Counterfactual marketing Work",
        purpose="Persist staffing evidence", expected_output="Verified marketing result",
        acceptance_criteria="Pass verification", state="READY", work_type="DELIVERY",
        resource_ceiling_twd=Decimal("1"), retry_limit=1,
        runtime_control_json={"team_formation": {"selection_evidence": evidence}},
    )
    db.session.add(work); db.session.flush()

    summary = employee_evolution.company_behavioral_evolution_summary()
    assert summary["staffing_decisions_with_evidence"] >= 1
    assert summary["experience_influenced_decisions"] >= 1
    assert summary["counterfactual_behavior_changes"] >= 1
    assert any(row["selected_employee_id"] == experienced.id for row in summary["examples"])


def test_current_learning_does_not_claim_behavior_change_when_no_learning_winner_is_same(ctx):
    from eason_one.services import team_formation

    position = Position.query.filter_by(name="Employee").one()
    model = Employee.query.filter_by(slug="researcher").one().current_model
    experienced = Employee(
        name="Stable Growth Proven", slug="stable-growth-proven", position_id=position.id,
        role_description="Marketing, growth, campaign and acquisition specialist.",
        system_instructions="Deliver governed marketing work.", current_model_config_id=model.id,
        salary_credits_per_week=0,
    )
    newcomer = Employee(
        name="Stable Growth Newcomer", slug="stable-growth-newcomer", position_id=position.id,
        role_description="Marketing, growth, campaign and acquisition specialist.",
        system_instructions="Deliver governed marketing work.", current_model_config_id=model.id,
        salary_credits_per_week=0,
    )
    db.session.add_all([experienced, newcomer]); db.session.flush()
    db.session.add(EmployeeLearningRecord(
        employee_id=experienced.id, learning_type="WORK_EXPERIENCE",
        validation_basis="CANONICAL_WORK_ACCEPTANCE",
        evidence_json={"capability": "MARKETING", "execution_attempt_ids": [2102]},
        title="Accepted owner experience — stable tie-break", content="Accepted outcome.",
        source_ref="WORK:2102/ARTIFACT_VERSION:2102/ACCEPTANCE:2102", validated=True,
    ))
    db.session.flush()

    selected = team_formation.best_existing_employee("MARKETING")
    assert selected.id == experienced.id
    evidence = team_formation.staffing_selection_evidence("MARKETING", selected)
    assert evidence["experience_influenced_selection"] is True
    assert evidence["counterfactual_without_learning"]["employee_id"] == experienced.id
    assert evidence["behavior_changed_vs_no_learning"] is False


def test_current_task_execution_uses_modelconfig_full_output_envelope_and_compact_retry(ctx, monkeypatch):
    """Task execution must not repeat the old hidden 1,600/2,400-token ceiling mistake."""
    from types import SimpleNamespace
    from eason_one.services import task_execution

    employee = Employee.query.filter_by(slug="critic").one()
    employee.current_model.max_output_tokens = 4096
    project = Project(
        name="Task output envelope", objective="Exercise full configured output authority",
        status="ACTIVE", priority="MEDIUM", environment="LIVE",
        owner_employee_id=Employee.query.filter_by(slug="ceo").one().id,
        real_budget_limit=Decimal("5"),
    )
    db.session.add(project); db.session.flush()
    task = Task(
        project_id=project.id, title="Produce bounded critique", objective="Produce a concise critique",
        status="ASSIGNED", assigned_employee_id=employee.id, reviewer_employee_id=None,
        required_output="Critique", acceptance_criteria="Structured result",
    )
    db.session.add(task); db.session.flush()

    monkeypatch.setattr(task_execution, "build_with_composition", lambda *_a, **_k: ("CTX", {}))
    monkeypatch.setattr(task_execution, "select_execution_model", lambda *_a, **_k: employee.current_model)
    calls = []

    def fake_execute(*_args, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            status="SUCCEEDED", raw_output='{"result_summary":"ok","knowledge_proposals":[]}',
            context_composition_json={}, parsed_output_json=None,
            structured_validation_status=None, structured_validation_errors_json=None,
        )

    monkeypatch.setattr(task_execution, "execute", fake_execute)
    monkeypatch.setattr(task_execution, "materialize_successful_run", lambda _task, run, **_kwargs: run)
    task_execution.run_task(task)
    assert calls[-1]["max_output_tokens_override"] == 4096
    assert "TASK_OUTPUT_TRUNCATION_RECOVERY_V1" not in calls[-1]["system_prompt_override"]

    previous = SimpleNamespace(failure_reason="OUTPUT_TRUNCATED", id=999, attempt_number=1)
    task.status = "WORKING"
    task_execution.run_task(task, retry_of_run=previous)
    assert calls[-1]["max_output_tokens_override"] == 4096
    assert "TASK_OUTPUT_TRUNCATION_RECOVERY_V1" in calls[-1]["system_prompt_override"]
    assert calls[-1]["prompt_version"].endswith("compact-retry-v1")



def test_current_task_execution_consumes_only_canonical_persistent_employee_memory(ctx, monkeypatch):
    """Accepted cross-Project outcomes reach the assigned Employee with exact provenance."""
    from types import SimpleNamespace
    from eason_one.services import task_execution

    employee = Employee.query.filter_by(slug="critic").one()
    ceo = Employee.query.filter_by(slug="ceo").one()
    prior = Project(
        name="Prior accepted learning", objective="Create canonical experience",
        status="DONE", priority="MEDIUM", environment="LIVE",
        owner_employee_id=ceo.id, real_budget_limit=Decimal("1"),
    )
    current = Project(
        name="Next governed project", objective="Consume relevant prior experience",
        status="ACTIVE", priority="MEDIUM", environment="LIVE",
        owner_employee_id=ceo.id, real_budget_limit=Decimal("5"),
    )
    db.session.add_all([prior, current]); db.session.flush()
    canonical = EmployeeLearningRecord(
        employee_id=employee.id, project_id=prior.id, learning_type="WORK_EXPERIENCE",
        validation_basis="CANONICAL_WORK_ACCEPTANCE",
        evidence_json={"capability": "RESEARCH", "execution_attempt_ids": [4101]},
        title="Accepted owner experience — decision brief",
        content="Accepted research decision brief with source-grounded evidence.",
        source_ref="WORK:4101/ARTIFACT_VERSION:4101/ACCEPTANCE:4101", validated=True,
    )
    noncanonical = EmployeeLearningRecord(
        employee_id=employee.id, project_id=prior.id, learning_type="WORK_EXPERIENCE",
        validation_basis="FOUNDER_VALIDATED_NOTE",
        evidence_json={"capability": "RESEARCH", "execution_attempt_ids": [4102]},
        title="Manual note that must not become execution memory",
        content="This validated note is not canonical accepted-Work evidence.",
        source_ref="NOTE:4102", validated=True,
    )
    db.session.add_all([canonical, noncanonical]); db.session.flush()
    task = Task(
        project_id=current.id, title="Review market decision evidence",
        objective="Review a source-grounded market decision brief",
        status="ASSIGNED", assigned_employee_id=employee.id, reviewer_employee_id=None,
        required_output="Decision-ready critique", acceptance_criteria="Use current evidence",
    )
    db.session.add(task); db.session.flush()

    monkeypatch.setattr(task_execution, "build_with_composition", lambda *_a, **_k: ("BASE CONTEXT", {"base": True}))
    monkeypatch.setattr(task_execution, "select_execution_model", lambda *_a, **_k: employee.current_model)
    calls = []

    def fake_execute(*_args, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            status="SUCCEEDED", raw_output='{"result_summary":"ok","knowledge_proposals":[]}',
            context_composition_json=kwargs["context_composition"], parsed_output_json=None,
            structured_validation_status=None, structured_validation_errors_json=None,
        )

    monkeypatch.setattr(task_execution, "execute", fake_execute)
    monkeypatch.setattr(task_execution, "materialize_successful_run", lambda _task, run, **_kwargs: run)
    task_execution.run_task(task)
    assert len(calls) == 1
    call = calls[0]
    assert "Accepted owner experience — decision brief" in call["context_override"]
    assert "Manual note that must not become execution memory" not in call["context_override"]
    assert "historical experience is never fresh web evidence" in call["context_override"]
    memory = call["context_composition"]["persistent_employee_memory"]
    assert memory["schema"] == "PERSISTENT_EMPLOYEE_EXECUTION_MEMORY_V1"
    assert memory["experience_record_ids"] == [canonical.id]
    assert noncanonical.id not in memory["experience_record_ids"]
    assert memory["validation_basis"] == "CANONICAL_WORK_ACCEPTANCE"
    assert memory["authority_effect"] is False
    assert memory["capability_effect"] is False
    assert memory["budget_effect"] is False
    assert memory["current_fact_effect"] is False


def test_current_review_runtime_consumes_persistent_reviewer_memory_below_current_artifact_truth(ctx):
    """Reviewer learning is wired into the real review path but cannot become current proof."""
    import inspect
    from eason_one.services import work_execution

    source = inspect.getsource(work_execution.review_work)
    assert "execution_learning_context" in source
    assert 'purpose="TASK_REVIEW"' in source
    assert 'composition["persistent_employee_memory"]' in source
    assert source.index("execution_learning_context") < source.index("FROZEN ACCEPTANCE CONTRACT")
    assert source.index("FROZEN ACCEPTANCE CONTRACT") < source.index("AUTHORITATIVE HOST PROOF")


def test_current_engineer_codex_consumes_persistent_memory_without_expanding_execution_boundary(ctx):
    """Engineer history reaches Codex as precedent and persists on the accountable Engineer Run."""
    import inspect
    from eason_one.services import codex_connector

    helper = inspect.getsource(codex_connector._persistent_engineer_learning)
    job = inspect.getsource(codex_connector.build_job_spec)
    create = inspect.getsource(codex_connector._create_run)
    assert "execution_learning_context" in helper
    assert 'purpose="TASK_EXECUTION"' in helper
    assert "infer_primary_capability" in helper
    assert "PERSISTENT EMPLOYEE EXPERIENCE — CANONICAL PRECEDENT ONLY; THIS DOES NOT EXPAND AUTHORITY" in job
    assert job.index("PERSISTENT EMPLOYEE EXPERIENCE") < job.index("AUTHORITY BOUNDARY")
    assert '"persistent_employee_memory": dict(learning_meta or {})' in create
    assert "Codex is a tool, not an Employee" in create


def test_current_learning_consumption_summary_requires_canonical_persisted_run_provenance(ctx):
    """Learning-consumption metrics count exact canonical record ids, not arbitrary Run metadata."""
    from eason_one.services import employee_evolution

    operation, project, researcher, critic = multi._setup()
    work = sorted([row for row in operation.works if row.work_type != "MANAGEMENT"], key=lambda row: row.id)[0]
    canonical = EmployeeLearningRecord(
        employee_id=researcher.id, project_id=project.id, work_id=work.id,
        learning_type="WORK_EXPERIENCE", validation_basis="CANONICAL_WORK_ACCEPTANCE",
        evidence_json={"capability": "RESEARCH", "execution_attempt_ids": [4201]},
        title="Accepted canonical research outcome", content="Accepted governed outcome.",
        source_ref="WORK:4201/ARTIFACT_VERSION:4201/ACCEPTANCE:4201", validated=True,
    )
    noncanonical = EmployeeLearningRecord(
        employee_id=researcher.id, project_id=project.id, work_id=work.id,
        learning_type="WORK_EXPERIENCE", validation_basis="FOUNDER_VALIDATED_NOTE",
        evidence_json={"capability": "RESEARCH", "execution_attempt_ids": [4202]},
        title="Manual note", content="Not canonical accepted-Work evidence.",
        source_ref="NOTE:4202", validated=True,
    )
    db.session.add_all([canonical, noncanonical]); db.session.flush()
    good = _v16_run(researcher, project, operation, work, purpose="TASK_EXECUTION", status="SUCCEEDED", max_tokens=4096)
    good.context_composition_json = {
        "persistent_employee_memory": {
            "schema": "PERSISTENT_EMPLOYEE_EXECUTION_MEMORY_V1",
            "validation_basis": "CANONICAL_WORK_ACCEPTANCE",
            "experience_record_ids": [canonical.id],
            "matched_records": 1,
            "required_capability": "RESEARCH",
            "authority_effect": False,
        }
    }
    db.session.add(ExternalEffectAttempt(
        execution_id=good.id, work_id=work.id, operation_id=operation.id,
        provider=good.provider_key_snapshot, effect_kind="MODEL_INFERENCE",
        request_fingerprint=f"learning-good-{good.id}", idempotency_key=f"learning-good-{good.id}",
        state="DISPATCHING", estimated_cost_twd=Decimal("0"), dispatched_at=now(),
    ))
    predispatch = _v16_run(researcher, project, operation, work, purpose="TASK_EXECUTION", status="FAILED", max_tokens=4096)
    predispatch.context_composition_json = deepcopy(good.context_composition_json)
    predispatch.failure_stage = "PRE_DISPATCH"
    bad = _v16_run(critic, project, operation, work, purpose="TASK_REVIEW", status="SUCCEEDED", max_tokens=4096)
    bad.context_composition_json = {
        "persistent_employee_memory": {
            "schema": "PERSISTENT_EMPLOYEE_EXECUTION_MEMORY_V1",
            "validation_basis": "CANONICAL_WORK_ACCEPTANCE",
            "experience_record_ids": [noncanonical.id],
            "matched_records": 1,
        }
    }
    db.session.flush()

    summary = employee_evolution.learning_consumption_summary()
    assert summary["runs_with_canonical_memory"] == 1
    assert summary["task_execution_runs"] == 1
    assert summary["task_review_runs"] == 0
    assert summary["unique_learning_records_consumed"] == 1
    assert summary["examples"][0]["run_id"] == good.id
    assert summary["examples"][0]["experience_record_ids"] == [canonical.id]
    assert all(row["run_id"] != bad.id for row in summary["examples"])
    assert all(row["run_id"] != predispatch.id for row in summary["examples"])
    assert "dispatch boundary" in summary["truth_note"]
    assert "not that the later outcome improved" in summary["truth_note"]

def test_current_company_runtime_owns_output_truncation_retry(ctx):
    """Known task truncation is retryable Company work, not a Founder escalation."""
    from types import SimpleNamespace
    from eason_one.services.work_execution import automatic_retry_allowed

    employee = Employee.query.filter_by(slug="critic").one()
    task = SimpleNamespace(assigned_employee=employee)
    run = SimpleNamespace(outcome="FAILED_KNOWN", failure_reason="OUTPUT_TRUNCATED", attempt_number=1)
    assert automatic_retry_allowed(run, task) is True
    run.failure_reason = "PROVIDER_TRANSIENT_REJECTED"
    assert automatic_retry_allowed(run, task) is True
    run.failure_reason = "RESEARCH_TOOL_UNAVAILABLE"
    run.outcome = "FAILED_SAFE"
    assert automatic_retry_allowed(run, task) is True
    run.attempt_number = 4
    assert automatic_retry_allowed(run, task) is False
    run.attempt_number = 1
    run.failure_reason = "NOT_A_RETRYABLE_REASON"
    assert automatic_retry_allowed(run, task) is False


def test_current_eason_market_offer_persists_base_quote_and_experience_premium(ctx):
    from eason_one.models import MarketOffer
    from eason_one.services import market, team_formation

    ceo = Employee.query.filter_by(slug="ceo").one()
    position = Position.query.filter_by(name="Employee").one()
    model = Employee.query.filter_by(slug="researcher").one().current_model
    employee = Employee(
        name="Premium Evidence Specialist", slug="premium-evidence-specialist",
        position_id=position.id,
        role_description="Marketing, growth, campaign and acquisition specialist.",
        system_instructions="Deliver governed marketing work.", current_model_config_id=model.id,
        salary_credits_per_week=0,
    )
    db.session.add(employee); db.session.flush()
    db.session.add(EmployeeLearningRecord(
        employee_id=employee.id, learning_type="WORK_EXPERIENCE",
        validation_basis="CANONICAL_WORK_ACCEPTANCE",
        evidence_json={"capability": "MARKETING", "execution_attempt_ids": [2201]},
        title="Accepted owner experience — premium evidence", content="Accepted outcome.",
        source_ref="WORK:2201/ARTIFACT_VERSION:2201/ACCEPTANCE:2201", validated=True,
    ))
    project = Project(
        name="Market pricing causality", objective="Persist quote causality",
        status="ACTIVE", priority="MEDIUM", environment="LIVE", owner_employee_id=ceo.id,
        real_budget_limit=Decimal("5"),
    )
    db.session.add(project); db.session.flush()
    work = Work(
        project_id=project.id, title="Price marketing work", purpose="Persist one offer",
        expected_output="Marketing result", acceptance_criteria="Pass verification",
        state="READY", work_type="DELIVERY", resource_ceiling_twd=Decimal("1"), retry_limit=1,
        runtime_control_json={"staffing_requirements": {"required_capabilities": ["MARKETING"]}},
    )
    db.session.add(work); db.session.flush()
    ranked = [row for row in team_formation.ranked_existing_employees("MARKETING") if row[5].id == employee.id]
    award = market.award_internal_work(work, "MARKETING", ranked, opened_by_employee_id=ceo.id)
    db.session.flush()
    offer = db.session.get(MarketOffer, award["offer"].id)
    breakdown = offer.rationale_json["quote_breakdown"]

    assert offer.rationale_json["policy"] == "INTERNAL_MARKET_V1_3"
    assert Decimal(breakdown["base_quote_ec"]) > 0
    assert Decimal(breakdown["experience_premium_ec"]) > 0
    assert Decimal(breakdown["quote_ec"]) == Decimal(offer.quote_ec)
    summary = market.economic_evolution_summary()
    assert summary["offers_with_experience_premium"] >= 1
    assert summary["total_experience_premium_ec"] > 0


def test_current_eason_market_voids_unsettled_contract_when_work_is_cancelled(ctx):
    from eason_one.models import MarketLedgerEntry
    from eason_one.services import market, team_formation

    ceo = Employee.query.filter_by(slug="ceo").one()
    researcher = Employee.query.filter_by(slug="researcher").one()
    project = Project(
        name="Void market contract", objective="No ghost payable contract after cancellation",
        status="ACTIVE", priority="MEDIUM", environment="LIVE", owner_employee_id=ceo.id,
        real_budget_limit=Decimal("5"),
    )
    db.session.add(project); db.session.flush()
    work = Work(
        project_id=project.id, title="Cancelled research labor", purpose="Research then cancel",
        expected_output="Research result", acceptance_criteria="Verified sources",
        state="READY", work_type="DELIVERY", resource_ceiling_twd=Decimal("1"), retry_limit=1,
        runtime_control_json={"staffing_requirements": {"required_capabilities": ["RESEARCH"]}},
    )
    db.session.add(work); db.session.flush()
    ranked = [row for row in team_formation.ranked_existing_employees("RESEARCH") if row[5].id == researcher.id]
    award = market.award_internal_work(work, "RESEARCH", ranked, opened_by_employee_id=ceo.id)
    contract = award["contract"]
    assert contract.status == "ACTIVE"

    work_runtime.transition(work, "CANCELLED", reason="Founder Project terms superseded this Work")
    db.session.flush()
    assert contract.status == "VOIDED"
    assert contract.order.status == "VOIDED"
    assert "No EC paid" in contract.settlement_note
    assert MarketLedgerEntry.query.filter_by(contract_id=contract.id).count() == 0
    assert market.wallet_balance(researcher.id) == Decimal("0.00")


def test_current_disputed_market_contract_can_never_become_payable_later(ctx):
    from eason_one.models import MarketLedgerEntry
    from eason_one.services import market, team_formation

    ceo = Employee.query.filter_by(slug="ceo").one()
    researcher = Employee.query.filter_by(slug="researcher").one()
    project = Project(
        name="Dispute is final", objective="Fail closed after producer mismatch",
        status="ACTIVE", priority="MEDIUM", environment="LIVE", owner_employee_id=ceo.id,
        real_budget_limit=Decimal("5"),
    )
    db.session.add(project); db.session.flush()
    work = Work(
        project_id=project.id, title="Disputed research labor", purpose="Produce governed research",
        expected_output="Research result", acceptance_criteria="Verified sources",
        state="READY", work_type="DELIVERY", resource_ceiling_twd=Decimal("1"), retry_limit=1,
        runtime_control_json={"staffing_requirements": {"required_capabilities": ["RESEARCH"]}},
    )
    db.session.add(work); db.session.flush()
    ranked = [row for row in team_formation.ranked_existing_employees("RESEARCH") if row[5].id == researcher.id]
    contract = market.award_internal_work(work, "RESEARCH", ranked, opened_by_employee_id=ceo.id)["contract"]

    bad_artifact = Artifact(project_id=project.id, work_id=work.id, artifact_type="WORK_RESULT", title="Wrong producer")
    db.session.add(bad_artifact); db.session.flush()
    bad_version = ArtifactVersion(
        artifact_id=bad_artifact.id, version=1, producer_employee_id=ceo.id,
        status="ACCEPTED", content_text="wrong", content_hash="d" * 64, accepted_at=now(),
    )
    db.session.add(bad_version); db.session.flush()
    bad_acceptance = VerificationRecord(
        work_id=work.id, artifact_version_id=bad_version.id, method="ACCEPTANCE_CONTRACT",
        status="PASSED", details_json={"proof": True},
    )
    db.session.add(bad_acceptance); work.state = "ACCEPTED"; work.accepted_at = now(); db.session.flush()
    market.settle_accepted_work(work, bad_version, bad_acceptance)
    assert contract.status == "DISPUTED"

    good_artifact = Artifact(project_id=project.id, work_id=work.id, artifact_type="WORK_RESULT", title="Later correct producer")
    db.session.add(good_artifact); db.session.flush()
    good_version = ArtifactVersion(
        artifact_id=good_artifact.id, version=1, producer_employee_id=researcher.id,
        status="ACCEPTED", content_text="correct later", content_hash="e" * 64, accepted_at=now(),
    )
    db.session.add(good_version); db.session.flush()
    good_acceptance = VerificationRecord(
        work_id=work.id, artifact_version_id=good_version.id, method="ACCEPTANCE_CONTRACT",
        status="PASSED", details_json={"proof": True},
    )
    db.session.add(good_acceptance); db.session.flush()
    market.settle_accepted_work(work, good_version, good_acceptance)

    assert contract.status == "DISPUTED"
    assert market.wallet_balance(researcher.id) == Decimal("0.00")
    assert MarketLedgerEntry.query.filter_by(contract_id=contract.id).count() == 0


def test_current_research_surface_exposes_observed_behavior_and_economic_causality(ctx, client):
    response = client.get("/headquarters/research")
    assert response.status_code == 200
    assert b"OBSERVED BEHAVIOR CHANGE" in response.data
    assert b"LEARNING V1.7" in response.data
    assert b"counterfactual" in response.data.lower()

    market_response = client.get("/headquarters/market")
    assert market_response.status_code == 200
    assert b"V1.3" in market_response.data


def test_current_market_reassignment_supersedes_unpaid_generation_and_pays_only_new_owner(ctx):
    """Legitimate Company reassignment rotates internal labor authority without rewriting history."""
    from eason_one.models import MarketContract, MarketLedgerEntry, MarketOrder
    from eason_one.services import market, team_formation

    ceo = Employee.query.filter_by(slug="ceo").one()
    researcher = Employee.query.filter_by(slug="researcher").one()
    position = Position.query.filter_by(name="Employee").one()
    replacement = Employee(
        name="Replacement Research Specialist",
        slug="replacement-research-specialist",
        position_id=position.id,
        role_description="Research evidence sources and market findings.",
        system_instructions="Deliver governed research Work.",
        current_model_config_id=researcher.current_model_config_id,
        salary_credits_per_week=0,
        active=True,
        employment_status="ACTIVE",
    )
    db.session.add(replacement); db.session.flush()
    project = Project(
        name="Market reassignment generations",
        objective="Preserve economic truth when Company changes legitimate Work owner",
        status="ACTIVE", priority="MEDIUM", environment="LIVE",
        owner_employee_id=ceo.id, real_budget_limit=Decimal("5"),
    )
    db.session.add(project); db.session.flush()
    work = Work(
        project_id=project.id,
        title="Research current evidence",
        purpose="Collect governed sources",
        expected_output="Research result",
        acceptance_criteria="Accepted evidence",
        state="READY", work_type="DELIVERY",
        resource_ceiling_twd=Decimal("1"), retry_limit=2,
        runtime_control_json={"staffing_requirements": {"required_capabilities": ["RESEARCH"]}},
    )
    db.session.add(work); db.session.flush()

    work_runtime.reassign(
        work, researcher.id, assigned_by_employee_id=ceo.id,
        reason="Initial governed Research owner.",
    )
    ranked = team_formation.ranked_existing_employees("RESEARCH")
    first_award = market.award_internal_work(
        work, "RESEARCH", ranked,
        opened_by_employee_id=ceo.id,
        preferred_employee_id=researcher.id,
    )
    first = first_award["contract"]
    assert first.generation == 1 and first.status == "ACTIVE"

    work_runtime.reassign(
        work, replacement.id, assigned_by_employee_id=ceo.id,
        reason="Company recovery moved the governed Work to another eligible Research Employee.",
    )
    db.session.flush()
    history = market.contract_history_for_work(work.id)
    assert len(history) == 2
    second, prior = history[0], history[1]
    assert prior.id == first.id and prior.status == "SUPERSEDED"
    assert prior.superseded_at is not None
    assert second.status == "ACTIVE" and second.generation == 2
    assert second.seller_employee_id == replacement.id
    assert second.supersedes_contract_id == prior.id
    assert second.order.supersedes_order_id == prior.order_id
    assert MarketOrder.query.filter_by(work_id=work.id).count() == 2
    assert MarketContract.query.filter_by(work_id=work.id).count() == 2
    assert MarketLedgerEntry.query.filter_by(contract_id=prior.id).count() == 0

    artifact = Artifact(
        project_id=project.id, work_id=work.id,
        artifact_type="RESEARCH_REPORT", title="Replacement accepted result",
    )
    db.session.add(artifact); db.session.flush()
    version = ArtifactVersion(
        artifact_id=artifact.id, version=1, producer_employee_id=replacement.id,
        status="ACCEPTED", content_text="accepted replacement research",
        content_hash="a" * 64, accepted_at=now(),
    )
    db.session.add(version); db.session.flush()
    acceptance = VerificationRecord(
        work_id=work.id, artifact_version_id=version.id,
        method="ACCEPTANCE_CONTRACT", status="PASSED",
        details_json={"proof": True},
    )
    db.session.add(acceptance)
    work.state = "ACCEPTED"; work.accepted_at = now(); db.session.flush()
    market.settle_accepted_work(work, version, acceptance)
    db.session.flush()

    assert second.status == "SETTLED"
    assert prior.status == "SUPERSEDED"
    assert market.wallet_balance(replacement.id) == Decimal(second.agreed_ec).quantize(Decimal("0.01"))
    assert market.wallet_balance(researcher.id) == Decimal("0.00")
    assert MarketLedgerEntry.query.filter_by(contract_id=second.id, entry_type="WORK_ACCEPTANCE_PAYMENT").count() == 1
    assert MarketLedgerEntry.query.filter_by(contract_id=prior.id).count() == 0


def test_current_reassignment_cannot_bypass_governed_work_capability(ctx):
    """Management reassignment itself cannot mint delivery capability."""
    ceo = Employee.query.filter_by(slug="ceo").one()
    researcher = Employee.query.filter_by(slug="researcher").one()
    project = Project(
        name="Reassignment capability authority",
        objective="Reject an active but unqualified replacement",
        status="ACTIVE", priority="MEDIUM", environment="LIVE",
        owner_employee_id=ceo.id, real_budget_limit=Decimal("5"),
    )
    db.session.add(project); db.session.flush()
    work = Work(
        project_id=project.id, title="Implement governed code", purpose="Modify software",
        expected_output="Code change", acceptance_criteria="Verified implementation",
        state="READY", work_type="DELIVERY", resource_ceiling_twd=Decimal("1"), retry_limit=1,
        runtime_control_json={"staffing_requirements": {"required_capabilities": ["SOFTWARE_ENGINEERING"]}},
    )
    db.session.add(work); db.session.flush()
    with pytest.raises(ValueError, match="WORK_REASSIGNMENT_CAPABILITY_MISMATCH"):
        work_runtime.reassign(
            work, researcher.id, assigned_by_employee_id=ceo.id,
            reason="This must not bypass capability truth.",
        )
    assert work_runtime.active_assignment(work) is None


def test_current_probation_completes_only_from_canonical_accepted_owner_work(ctx):
    """Persistent Employee employment state evolves from real accepted outcomes, not self-report."""
    from eason_one.services import employee_memory, employee_evolution

    ceo = Employee.query.filter_by(slug="ceo").one()
    researcher = Employee.query.filter_by(slug="researcher").one()
    position = Position.query.filter_by(name="Employee").one()
    probationary = Employee(
        name="Probation Research Employee",
        slug="probation-research-employee",
        position_id=position.id,
        role_description="Research evidence and current sources.",
        system_instructions="Deliver governed Research Work.",
        current_model_config_id=researcher.current_model_config_id,
        salary_credits_per_week=0,
        active=True,
        employment_status="PROBATION",
        probation_target_assignments=2,
    )
    db.session.add(probationary); db.session.flush()
    project = Project(
        name="Evidence-backed probation", objective="Complete two accepted owner assignments",
        status="ACTIVE", priority="MEDIUM", environment="LIVE",
        owner_employee_id=ceo.id, real_budget_limit=Decimal("5"),
    )
    db.session.add(project); db.session.flush()

    def accepted_work(index):
        work = Work(
            project_id=project.id, title=f"Probation research {index}", purpose="Collect evidence",
            expected_output="Research result", acceptance_criteria="Accepted evidence",
            state="ACCEPTED", work_type="DELIVERY", resource_ceiling_twd=Decimal("1"), retry_limit=1,
            runtime_control_json={
                "staffing_requirements": {"required_capabilities": ["RESEARCH"]},
                "team_formation": {"required_capability": "RESEARCH"},
            },
            accepted_at=now(),
        )
        db.session.add(work); db.session.flush()
        artifact = Artifact(
            project_id=project.id, work_id=work.id,
            artifact_type="RESEARCH_REPORT", title=f"Accepted research {index}",
        )
        db.session.add(artifact); db.session.flush()
        version = ArtifactVersion(
            artifact_id=artifact.id, version=1, producer_employee_id=probationary.id,
            status="ACCEPTED", content_text=f"accepted {index}",
            content_hash=(str(index) * 64)[:64], accepted_at=now(),
        )
        db.session.add(version); db.session.flush()
        acceptance = VerificationRecord(
            work_id=work.id, artifact_version_id=version.id,
            method="ACCEPTANCE_CONTRACT", status="PASSED", details_json={"proof": True},
        )
        db.session.add(acceptance); db.session.flush()
        employee_memory.capture_accepted_work_experience(work, version, acceptance)
        db.session.flush()
        return work, version, acceptance

    first = accepted_work(1)
    assert probationary.employment_status == "PROBATION"
    status = employee_evolution.probation_status(probationary)
    assert status["accepted_owner_assignments"] == 1
    assert status["remaining_assignments"] == 1
    assert CompanyEvent.query.filter_by(event_type="EMPLOYEE_PROBATION_COMPLETED", actor_id=probationary.id).count() == 0

    second = accepted_work(2)
    assert probationary.employment_status == "ACTIVE"
    status = employee_evolution.probation_status(probationary)
    assert status["accepted_owner_assignments"] == 2
    assert status["remaining_assignments"] == 0
    events = CompanyEvent.query.filter_by(event_type="EMPLOYEE_PROBATION_COMPLETED", actor_id=probationary.id).all()
    assert len(events) == 1
    assert events[0].payload_json["capability_change"] is False
    assert events[0].payload_json["authority_change"] is False

    # Idempotent recapture cannot create a second promotion event.
    employee_memory.capture_accepted_work_experience(*second)
    db.session.flush()
    assert CompanyEvent.query.filter_by(event_type="EMPLOYEE_PROBATION_COMPLETED", actor_id=probationary.id).count() == 1


def test_current_authority_bearing_planners_do_not_reintroduce_stale_hidden_output_caps(ctx):
    """Continuation/HR/output planners size envelopes from current model/schema, not old magic ceilings."""
    import inspect
    from eason_one.services import company_kernel, workforce, multi_agent, operations as operations_service, reviews as task_reviews

    continuation = inspect.getsource(company_kernel._plan_continuation)
    outcome_review = inspect.getsource(company_kernel._run_project_outcome_review)
    goal_verification = inspect.getsource(operations_service._goal_verification_step)
    operation_decision = inspect.getsource(operations_service._decision_step)
    hr = inspect.getsource(workforce.assess_request)
    hr_authorization = inspect.getsource(workforce.assessment_authorization)
    orchestration = inspect.getsource(multi_agent.call_orchestrator)
    legacy_task_review = inspect.getsource(task_reviews.run_review)

    assert "min(2600" not in continuation
    assert "max_output_tokens_override=int(ceo.current_model.max_output_tokens)" in continuation
    assert "PROJECT_CONTINUATION_TRUNCATION_RECOVERY" in continuation
    assert "ONE_SAME_PROVIDER_COMPACT_RETRY" in continuation

    assert "project-outcome-review-v3-truncation-recovery" in outcome_review
    assert "400 * max(1, len(criterion_ids))" in outcome_review
    assert "PROJECT_OUTCOME_REVIEW_TRUNCATION_RECOVERY" in outcome_review

    assert "min(1600" not in goal_verification
    assert "max_output_tokens_override=int(ceo.current_model.max_output_tokens)" in goal_verification
    assert "GOAL_VERIFICATION_TRUNCATION_RECOVERY" in goal_verification
    assert "ONE_SAME_PROVIDER_COMPACT_RETRY" in goal_verification

    assert "min(ceo.current_model.max_output_tokens, 900)" not in operation_decision
    assert "max_output_tokens_override=int(selected_model.max_output_tokens)" in operation_decision
    assert "CEO_OPERATION_DECISION_TRUNCATION_RECOVERY" in operation_decision
    assert "ONE_SAME_PROVIDER_COMPACT_RETRY" in operation_decision

    assert "1536" not in hr
    assert "max_output_tokens_override=int(hr.current_model.max_output_tokens)" not in hr
    assert "model_override=model_override" in hr
    assert "HR_ASSESSMENT_TRUNCATION_RECOVERY" in hr
    assert "int(model.max_output_tokens)" in hr_authorization

    assert "max_output_tokens_override=min(900" not in orchestration
    assert "600 + 320 * task_count" in orchestration

    assert "min(model.max_output_tokens,1200)" not in legacy_task_review
    assert "max_output_tokens_override=int(model.max_output_tokens)" in legacy_task_review
    assert "TASK_REVIEW_TRUNCATION_RECOVERY" in legacy_task_review
    assert "ONE_SAME_PROVIDER_COMPACT_RETRY" in legacy_task_review


def test_current_normal_runtime_failures_never_manufacture_founder_work(ctx):
    """Provider/coordination/evidence recovery is Company work unless an exact canonical authority delta exists."""
    import inspect
    from eason_one.services import operations as operations_service

    meeting_handler = inspect.getsource(operations_service._handle_paid_meeting_failure)
    meeting_advance = inspect.getsource(operations_service._advance_meeting)
    engineering_delivery = inspect.getsource(operations_service._deterministic_engineering_delivery)
    open_step_recovery = inspect.getsource(operations_service._recover_open_step)
    founder_boundary = inspect.getsource(operations_service.wait_for_founder)

    assert '"INTERNAL_RECOVERY"' in meeting_handler
    assert 'ensure_management_work' in meeting_handler
    assert 'founder_action_required": False' in meeting_handler
    assert "wait_for_founder(" not in meeting_advance

    assert "ENGINEERING_EVIDENCE_REPAIR" not in engineering_delivery
    assert "ENGINEERING_ACCEPTANCE_REPAIR" not in engineering_delivery
    assert '"INTERNAL_RECOVERY"' in engineering_delivery
    assert '"RECONCILIATION"' in engineering_delivery
    assert '"founder_action_required": False' in engineering_delivery

    assert "Unknown persisted paid OperationStep kind" in open_step_recovery
    assert "pause_for_internal_runtime_recovery" in open_step_recovery

    # The legacy compatibility entry point is fail-closed for vNext: only an
    # exact positive budget shortfall may be converted into a Founder gate.
    assert 'if kind == "BUDGET_AUTHORIZATION"' in founder_boundary
    assert "Legacy decision kind" in founder_boundary
    assert "pause_for_internal_runtime_recovery" in founder_boundary


def test_current_ceo_and_hr_cannot_turn_normal_company_recovery_into_founder_orchestration(ctx):
    import inspect
    from eason_one.services import operations as operations_service

    apply_decision = inspect.getsource(operations_service._apply_management_decision)
    decision_step = inspect.getsource(operations_service._decision_step)
    hiring_reconcile = inspect.getsource(operations_service._reconcile_hiring_founder_review)

    assert "FOUNDER_REJECTED_INTERNAL" in apply_decision
    assert '"founder_action_required": False' in apply_decision
    assert "wait_for_founder(" not in apply_decision
    assert "Do not use FOUNDER as a generic recovery action" in decision_step

    assert "commit_delegated_hire" in hiring_reconcile
    assert "RECONCILIATION" in hiring_reconcile
    assert '"founder_action_required": False' in hiring_reconcile


def test_current_employee_learning_surface_distinguishes_authority_from_history(ctx):
    """Accepted history may influence selection but never becomes capability authority by display accident."""
    import inspect
    from eason_one.services import employee_evolution

    source = inspect.getsource(employee_evolution.employee_profile)
    assert '"authorized_capability"' in source
    assert '"historical_only"' in source
    assert '"authorized_capabilities"' in source
    assert '"historical_evidence_capabilities"' in source


def _v16_run(employee, project, operation, work, *, purpose, status, max_tokens, failure_reason=None, stop_reason=None):
    """Create one durable test Execution with the current immutable snapshot contract."""
    model = employee.current_model
    row = AgentRun(
        employee_id=employee.id,
        project_id=project.id,
        operation_id=operation.id,
        work_id=work.id,
        model_config_id=model.id,
        purpose=purpose,
        user_request=f"{purpose} regression fixture",
        system_prompt_snapshot="v1.6 regression system snapshot",
        context_snapshot="v1.6 regression context snapshot",
        status=status,
        outcome="SUCCEEDED" if status == "SUCCEEDED" else "FAILED_KNOWN",
        failure_reason=failure_reason,
        provider_stop_reason=stop_reason,
        effective_max_output_tokens=max_tokens,
        provider_key_snapshot=model.provider_key,
        model_name_snapshot=model.model_name,
        input_price_snapshot=model.input_price_per_million,
        output_price_snapshot=model.output_price_per_million,
        request_price_snapshot=model.request_price_per_call,
        currency_snapshot=model.currency,
        currency=model.currency,
        real_cost=Decimal("0"),
        finished_at=now(),
    )
    db.session.add(row)
    db.session.flush()
    return row


def test_current_review_output_cap_recovery_reuses_exact_artifact_without_producer_replay(ctx):
    """A historical 3,200-token review fault reopens only semantic review of the exact persisted Artifact."""
    from eason_one.services import runtime_recovery
    from eason_one.services import artifacts as artifact_service

    operation, project, researcher, critic = multi._setup()
    work = sorted([row for row in operation.works if row.work_type != "MANAGEMENT"], key=lambda row: row.id)[0]
    task = work_runtime.task_for_work(work)
    assert task is not None

    reviewer_model = ModelConfig(
        label="Claude current review envelope",
        provider_key="anthropic",
        model_name="claude-sonnet-current-test",
        input_price_per_million=Decimal("1"),
        output_price_per_million=Decimal("2"),
        request_price_per_call=Decimal("0"),
        currency="TWD",
        max_output_tokens=4096,
        active=True,
        archived=False,
    )
    db.session.add(reviewer_model); db.session.flush()
    critic.current_model_config_id = reviewer_model.id
    task.reviewer_employee_id = critic.id
    contract = acceptance_contract.build(
        title=work.title,
        objective=work.purpose,
        criteria=["Recommendation is supported by the persisted evidence."],
        reviewer_employee_id=critic.id,
        owner_employee_id=researcher.id,
        project_execution_terms_hash=project_contract.execution_terms_hash(project),
    )
    control = dict(work.runtime_control_json or {})
    control["acceptance_contract"] = contract
    work.runtime_control_json = control
    work.acceptance_criteria = "Recommendation is supported by the persisted evidence."
    task.acceptance_criteria = work.acceptance_criteria
    work.retry_limit = 1

    producer = _v16_run(
        researcher, project, operation, work,
        purpose="TASK_EXECUTION", status="SUCCEEDED", max_tokens=1200,
    )
    artifact = Artifact(
        project_id=project.id,
        work_id=work.id,
        artifact_type="RESEARCH_REPORT",
        title="Persisted research artifact",
    )
    db.session.add(artifact); db.session.flush()
    content = "Persisted provider research that must not be replayed."
    version = ArtifactVersion(
        artifact_id=artifact.id,
        version=1,
        producer_employee_id=researcher.id,
        execution_id=producer.id,
        status="SUBMITTED",
        content_text=content,
        content_hash=artifact_service._hash(content, None),
    )
    db.session.add(version); db.session.flush()

    first = _v16_run(
        critic, project, operation, work,
        purpose="TASK_REVIEW", status="FAILED", max_tokens=3200,
        failure_reason="OUTPUT_TRUNCATED", stop_reason="max_output_tokens",
    )
    first.resolution_status = "WORK_RETRY_SCHEDULED"
    second = _v16_run(
        critic, project, operation, work,
        purpose="TASK_REVIEW", status="FAILED", max_tokens=3200,
        failure_reason="OUTPUT_TRUNCATED", stop_reason="max_output_tokens",
    )
    second.resolution_status = "WORK_RETRY_EXHAUSTED"
    work.state = "ABANDONED"
    work.abandoned_at = now()
    db.session.commit()

    producer_count = AgentRun.query.filter_by(work_id=work.id, purpose="TASK_EXECUTION").count()
    review_count = AgentRun.query.filter_by(work_id=work.id, purpose="TASK_REVIEW").count()
    recovered = runtime_recovery.reconcile_superseded_review_output_caps()
    db.session.refresh(work); db.session.refresh(version); db.session.refresh(first); db.session.refresh(second)

    assert recovered == [work.id]
    assert work.state == "VERIFYING"
    assert version.status == "SUBMITTED" and version.execution_id == producer.id
    assert AgentRun.query.filter_by(work_id=work.id, purpose="TASK_EXECUTION").count() == producer_count
    assert AgentRun.query.filter_by(work_id=work.id, purpose="TASK_REVIEW").count() == review_count
    assert first.resolution_status == "SYSTEM_REVIEW_OUTPUT_CAP_SUPERSEDED"
    assert second.resolution_status == "SYSTEM_REVIEW_OUTPUT_CAP_SUPERSEDED"
    recovery = (work.runtime_control_json or {})["review_output_cap_recovery"]
    assert recovery["artifact_version_id"] == version.id
    assert recovery["producing_execution_id"] == producer.id
    assert recovery["old_limit"] == 3200
    assert recovery["new_limit"] == 4096
    assert recovery["implementation_replayed"] is False


def test_current_market_reissues_voided_contract_as_new_generation_after_proven_system_reopen(ctx):
    """Platform repair never mutates a VOIDED EC promise back to ACTIVE."""
    from eason_one.services import market

    operation, project, researcher, _critic = multi._setup()
    work = sorted([row for row in operation.works if row.work_type != "MANAGEMENT"], key=lambda row: row.id)[0]
    original = market.contract_for_work(work.id)
    assert original is not None and original.status == "ACTIVE"
    original_id = original.id
    original_order_id = original.order_id
    seller_id = original.seller_employee_id
    agreed = Decimal(original.agreed_ec)

    work_runtime.transition(work, "EXECUTING", reason="exercise system-reopen market truth")
    work_runtime.transition(work, "ABANDONED", reason="proven platform fault fixture")
    db.session.flush()
    db.session.refresh(original)
    assert original.status == "VOIDED"
    assert MarketLedgerEntry.query.filter_by(contract_id=original.id).count() == 0

    work_runtime.reopen_abandoned(
        work, "VERIFYING",
        reason="Recovered exact Artifact review after proven platform output-envelope defect.",
    )
    db.session.commit()

    history = market.contract_history_for_work(work.id)
    assert len(history) == 2
    current, old = history[0], history[1]
    assert old.id == original_id and old.status == "VOIDED" and old.order_id == original_order_id
    assert current.status == "ACTIVE"
    assert current.generation == old.generation + 1
    assert current.supersedes_contract_id == old.id
    assert current.order.supersedes_order_id == old.order_id
    assert current.seller_employee_id == seller_id
    assert Decimal(current.agreed_ec) == agreed
    assert MarketLedgerEntry.query.filter(MarketLedgerEntry.contract_id.in_([old.id, current.id])).count() == 0


def test_current_failed_upstream_cannot_deadlock_project_behind_ready_descendants(ctx):
    """Once bounded recovery is exhausted, failed dependency descendants are closed so Project continuation can own the next move."""
    from eason_one.services import company_kernel

    operation, project, _researcher, _critic = multi._setup()
    delivery = sorted([row for row in operation.works if row.work_type != "MANAGEMENT"], key=lambda row: row.id)
    research, independent, synthesis = delivery

    # Leave exactly one failed upstream branch and one descendant whose dependency
    # graph can no longer be satisfied. The independent sibling has already closed.
    work_runtime.transition(independent, "EXECUTING", reason="close independent sibling")
    work_runtime.transition(independent, "ACCEPTED", reason="close independent sibling")
    work_runtime.transition(research, "EXECUTING", reason="bounded failure fixture")
    work_runtime.transition(research, "ABANDONED", reason="bounded recovery exhausted")
    assert synthesis.state == "READY"
    assert any(edge.depends_on_work_id == research.id for edge in WorkDependency.query.filter_by(work_id=synthesis.id).all())
    db.session.commit()

    assert company_kernel._has_active_delivery(project) is False
    result = company_kernel._advance_project(project)
    db.session.refresh(operation); db.session.refresh(synthesis)

    assert result["status"] == "MISSION_FAILED"
    assert operation.status == "FAILED"
    assert synthesis.state == "CANCELLED"
    assert project.status != "FAILED"


def test_current_company_truth_does_not_mask_failed_delivery_with_long_lived_management_run(ctx):
    """A persistent CEO management envelope is not Founder-visible WORKING truth after delivery exhausted."""
    from eason_one.services import company_truth

    operation, project, _researcher, _critic = multi._setup()
    delivery = sorted([row for row in operation.works if row.work_type != "MANAGEMENT"], key=lambda row: row.id)
    research, independent, _synthesis = delivery
    work_runtime.transition(independent, "EXECUTING", reason="close independent sibling")
    work_runtime.transition(independent, "ACCEPTED", reason="close independent sibling")
    work_runtime.transition(research, "EXECUTING", reason="bounded failure fixture")
    work_runtime.transition(research, "ABANDONED", reason="bounded recovery exhausted")

    management = next(row for row in operation.works if row.work_type == "MANAGEMENT")
    ceo = Employee.query.filter_by(slug="ceo").one()
    _v16_run(ceo, project, operation, management, purpose="CEO_MANAGEMENT", status="RUNNING", max_tokens=1200)
    db.session.commit()

    truth = company_truth.project_snapshot(project)
    assert truth["state"] == "RECOVERING"
    assert truth["state"] != "WORKING"


def test_current_project_surface_separates_execution_provider_from_review_provider(ctx):
    """Founder sees who produced the Artifact separately from who reviewed it."""
    from eason_one.services import project_company

    operation, project, researcher, critic = multi._setup()
    work = sorted([row for row in operation.works if row.work_type != "MANAGEMENT"], key=lambda row: row.id)[0]

    research_model = ModelConfig(
        label="Perplexity research fixture", provider_key="perplexity", model_name="sonar-test",
        input_price_per_million=Decimal("1"), output_price_per_million=Decimal("1"),
        request_price_per_call=Decimal("0.1"), currency="TWD", max_output_tokens=4096,
    )
    review_model = ModelConfig(
        label="Claude review fixture", provider_key="anthropic", model_name="claude-review-test",
        input_price_per_million=Decimal("1"), output_price_per_million=Decimal("1"),
        request_price_per_call=Decimal("0"), currency="TWD", max_output_tokens=4096,
    )
    db.session.add_all([research_model, review_model]); db.session.flush()
    researcher.current_model_config_id = research_model.id
    critic.current_model_config_id = review_model.id
    _v16_run(researcher, project, operation, work, purpose="TASK_EXECUTION", status="SUCCEEDED", max_tokens=4096)
    _v16_run(
        critic, project, operation, work, purpose="TASK_REVIEW", status="FAILED", max_tokens=4096,
        failure_reason="STRUCTURED_OUTPUT_INVALID", stop_reason="stop",
    )
    db.session.commit()

    row = next(item for item in project_company.project_snapshot(project)["works"] if item["work"].id == work.id)
    assert row["employee"].id == researcher.id
    assert row["execution_provider"] == "perplexity / sonar-test"
    assert row["review_provider"] == "anthropic / claude-review-test"


def test_current_project_cost_surface_separates_observed_billing_from_execution_authority(ctx, monkeypatch):
    """Founder budget math cannot imply observed pre-approval spend consumed Project execution authority."""
    from eason_one.services import project_company

    operation, project, researcher, _critic = multi._setup()
    run = _v16_run(
        researcher, project, operation,
        sorted([row for row in operation.works if row.work_type != "MANAGEMENT"], key=lambda row: row.id)[0],
        purpose="TASK_EXECUTION", status="SUCCEEDED", max_tokens=1200,
    )
    db.session.add(CostEvent(
        company_id=Company.query.order_by(Company.id).one().id,
        project_id=project.id,
        operation_id=operation.id,
        work_id=run.work_id,
        employee_id=researcher.id,
        agent_run_id=run.id,
        stage="TEST",
        category="MODEL",
        description="Observed model billing fixture outside the Project execution envelope.",
        internal_credits_delta=Decimal("0"),
        real_cost_delta=Decimal("2.920000"),
        currency="TWD",
    ))
    db.session.commit()
    monkeypatch.setattr("eason_one.services.operations.project_remaining_authority", lambda _project: Decimal("7.9200"))

    card = {"budget_limit": Decimal("10.0000"), "budget_remaining": Decimal("7.9200")}
    breakdown = project_company._project_cost_breakdown(project, card)
    assert breakdown["observed_model_cost"] == Decimal("2.920000")
    assert breakdown["execution_authority_used"] == Decimal("2.080000")
    assert breakdown["remaining"] == Decimal("7.9200")
    assert breakdown["outside_execution_envelope"] == Decimal("0.840000")


def test_current_ready_independent_sibling_remains_real_progress_after_other_branch_fails(ctx):
    """One failed branch must not make Founder truth RECOVERING while an independent READY sibling is runnable."""
    from eason_one.services import company_kernel, company_truth

    operation, project, _researcher, _critic = multi._setup()
    delivery = sorted([row for row in operation.works if row.work_type != "MANAGEMENT"], key=lambda row: row.id)
    failed, independent, synthesis = delivery
    work_runtime.transition(failed, "EXECUTING", reason="bounded failure fixture")
    work_runtime.transition(failed, "ABANDONED", reason="bounded recovery exhausted")
    assert independent.state == "READY"
    assert synthesis.state == "READY"

    assert company_kernel._has_active_delivery(project) is True
    truth = company_truth.project_snapshot(project)
    assert truth["state"] == "READY"
    assert truth["state"] != "RECOVERING"


def test_current_dependency_wait_without_target_uses_durable_workdependency_truth(ctx):
    """vNext DEPENDENCY waits may omit target_work_id; the WorkDependency DAG still owns liveness."""
    from eason_one.services import company_kernel

    operation, project, _researcher, _critic = multi._setup()
    delivery = sorted([row for row in operation.works if row.work_type != "MANAGEMENT"], key=lambda row: row.id)
    first, second, synthesis = delivery
    work_runtime.open_wait(synthesis, "DEPENDENCY", "Waiting for accepted upstream evidence.")
    gate = work_runtime.open_gates(synthesis, "DEPENDENCY")[0]
    assert gate.get("target_work_id") is None
    assert WorkDependency.query.filter_by(work_id=synthesis.id).count() == 2

    # Both upstream branches are still capable of satisfying the DAG.
    assert company_kernel._has_active_delivery(project) is True

    work_runtime.transition(first, "EXECUTING", reason="fail first upstream")
    work_runtime.transition(first, "ABANDONED", reason="bounded failure exhausted")
    # The independent second upstream is still READY, so delivery is live.
    assert second.state == "READY"
    assert company_kernel._has_active_delivery(project) is True

    work_runtime.transition(second, "EXECUTING", reason="fail second upstream")
    work_runtime.transition(second, "ABANDONED", reason="bounded failure exhausted")
    # Now every durable upstream edge is terminal failed; the dependency wait
    # cannot keep the Mission falsely alive.
    assert company_kernel._has_active_delivery(project) is False


def test_current_founder_proposal_supports_direct_yes_no_and_hides_internal_plan_by_default(ctx):
    """Founder can answer yes/no like chat while authority/technical detail remains expandable."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    js = (root / "eason_one" / "static" / "headquarters.js").read_text(encoding="utf-8")
    css = (root / "eason_one" / "static" / "headquarters.css").read_text(encoding="utf-8")
    assert "YES — RUN IT" in js
    assert "NO — DECLINE" in js
    assert "yesWords" in js and "noWords" in js
    assert "data-decline-operation" in js
    assert "View company plan / authority / technical details" in js
    assert "A decision-ready HTML brief you can open in your browser" in js
    assert ".proposal-company-plan" in css


def test_current_ceo_proposal_no_declines_without_granting_project_authority(client):
    """Direct NO is a real Founder rejection of the pending proposal, not a chat-only cosmetic action."""
    app = client.application
    with app.app_context():
        ceo = Employee.query.filter_by(slug="ceo").one()
        researcher = Employee.query.filter_by(slug="researcher").one()
        critic = Employee.query.filter_by(slug="critic").one()
        operation = operations.propose_operation(
            ceo,
            multi._plan(researcher, critic),
            route_type="FULL_PROJECT",
            founder_request="Please evaluate this bounded project.",
        )
        operation_id = operation.id
        assert operation.status in {"PLANNED", "WAITING_FOR_FOUNDER"}
        assert operation.approved_at is None
        assert operation.project_id is None

    response = client.post(f"/headquarters/ceo/operations/{operation_id}/decline")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["declined"] is True
    assert payload["project_id"] is None

    with app.app_context():
        operation = db.session.get(type(operation), operation_id)
        assert operation.approved_at is None
        assert operation.project_id is None
        assert operation.status not in {"RUNNING", "APPROVED"}
        decisions = list((operation.memory_json or {}).get("founder_decisions") or [])
        assert decisions and decisions[-1]["action"] == "REJECT"


def _install_research_department_provider_configs():
    """Test-only real-provider roster; no credential/provider call is made."""
    specs = (
        ("openai", "gpt-research-test", "OpenAI research test", "0.05"),
        ("anthropic", "claude-research-test", "Claude research test", "0"),
        ("gemini", "gemini-research-test", "Gemini research test", "0"),
        ("perplexity", "sonar-research-test", "Perplexity research test", "0.05"),
    )
    models = {}
    for provider, model_name, label, request_fee in specs:
        model = ModelConfig(
            label=label, provider_key=provider, model_name=model_name,
            input_price_per_million=Decimal("1"), output_price_per_million=Decimal("2"),
            request_price_per_call=Decimal(request_fee), currency="TWD",
            max_output_tokens=4096, active=True, archived=False,
        )
        db.session.add(model); db.session.flush()
        models[provider] = model
    from eason_one.seed import ensure_research_department_roster
    ensure_research_department_roster()
    db.session.flush()
    return models


def test_current_research_department_materializes_persistent_provider_family_roster_idempotently(ctx):
    """Multi-AI capacity is a fixed Department roster, not one-off pseudo-employees."""
    from eason_one.models import EmployeeModelHistory
    from eason_one.seed import ensure_research_department_roster
    from eason_one.services import research_department

    legacy = Employee.query.filter_by(slug="researcher").one()
    legacy_id = legacy.id
    _install_research_department_provider_configs()

    director = Employee.query.filter_by(slug="research-director").one()
    specialists = research_department.specialist_employees(active_only=True)
    assert [row.slug for row in specialists] == [
        "openai-researcher", "claude-researcher", "gemini-researcher", "perplexity-researcher",
    ]
    assert len({row.id for row in specialists}) == 4
    assert all(row.manager_id == director.id for row in specialists)
    assert all(row.department.name == "Research Department" for row in specialists)
    assert all(Decimal(row.salary_credits_per_week) == Decimal("500") for row in specialists)
    assert {research_department.provider_family(row) for row in specialists} == {
        "openai", "anthropic", "gemini", "perplexity",
    }
    assert all(row.current_model.provider_key == research_department.provider_family(row) for row in specialists)
    assert db.session.get(Employee, legacy_id).slug == "researcher"
    assert db.session.get(Employee, legacy_id).active is True

    ids = {row.slug: row.id for row in specialists}
    history_before = EmployeeModelHistory.query.filter(
        EmployeeModelHistory.employee_id.in_(list(ids.values()))
    ).count()
    ensure_research_department_roster()
    db.session.flush()
    assert {row.slug: row.id for row in research_department.specialist_employees(active_only=True)} == ids
    assert EmployeeModelHistory.query.filter(
        EmployeeModelHistory.employee_id.in_(list(ids.values()))
    ).count() == history_before


def test_current_multi_ai_research_compiles_named_persistent_researchers_plus_director_synthesis(ctx):
    """Multi-AI expands the research stage without deleting the rest of the CEO-approved Project."""
    from eason_one.services import research_department

    _install_research_department_provider_configs()
    legacy = Employee.query.filter_by(slug="researcher").one()
    critic = Employee.query.filter_by(slug="critic").one()
    plan = multi._plan(legacy, critic)
    plan["operation"]["objective"] = "Assess whether an overseas AI-skill business is commercially attractive."
    original_tail = deepcopy(plan["operation"]["tasks"][1:])
    normalized, notes = research_department.normalize_ceo_plan(
        deepcopy(plan),
        "請用 Claude、GPT、Gemini 三個 AI 獨立研究這個海外 AI Skill 市場，最後整合。",
    )
    tasks = normalized["operation"]["tasks"]
    assert len(tasks) == 6
    assignees = [db.session.get(Employee, row["assignee_employee_id"]).slug for row in tasks]
    assert assignees[:4] == ["openai-researcher", "claude-researcher", "gemini-researcher", "research-director"]
    assert assignees[4:] == ["critic", "researcher"]
    assert len(set(assignees[:3])) == 3
    assert all(row["required_capabilities"] == ["RESEARCH"] for row in tasks[:4])
    assert tasks[4:] == original_tail
    assert normalized["operation"]["meeting_policy"] == "NEVER"
    assert normalized["operation"]["meeting_config"]["trigger"] == "NEVER"
    assert "continue the rest" in normalized["executive_response"]
    assert notes

    validated = operations.validate_plan(deepcopy(normalized))
    assert len(validated["operation"]["tasks"]) == 6
    assert [row["title"] for row in validated["operation"]["tasks"][-2:]] == [
        "Critical branch", "Synthesis branch",
    ]


def test_current_multi_ai_research_preserves_strategy_and_delivery_after_research(ctx):
    """A Research -> Strategy -> HTML Project may not collapse into research-only Work."""
    from eason_one.services import research_department

    _install_research_department_provider_configs()
    researcher = Employee.query.filter_by(slug="researcher").one()
    critic = Employee.query.filter_by(slug="critic").one()
    engineer = Employee.query.filter_by(slug="engineer").one()
    plan = deepcopy(multi._plan(researcher, critic))
    plan["operation"]["tasks"] = [
        {
            "title": "Market research",
            "objective": "Establish current external market evidence for downstream strategy.",
            "assignee_employee_id": researcher.id,
            "reviewer_employee_id": critic.id,
            "required_capabilities": ["RESEARCH"],
            "acceptance_criteria": ["Current evidence is sourced and uncertainty is explicit."],
        },
        {
            "title": "Product strategy",
            "objective": "Use the accepted research evidence to decide positioning and offer design.",
            "assignee_employee_id": researcher.id,
            "reviewer_employee_id": critic.id,
            "required_capabilities": ["PRODUCT_STRATEGY"],
            "acceptance_criteria": ["The recommendation cites the accepted research handoff."],
        },
        {
            "title": "Build founder HTML brief",
            "objective": "Implement a directly openable HTML brief from the approved product strategy.",
            "assignee_employee_id": engineer.id,
            "reviewer_employee_id": critic.id,
            "required_capabilities": ["SOFTWARE_ENGINEERING"],
            "acceptance_criteria": ["A directly openable HTML deliverable exists."],
            "write_scope": {
                "version": "CODEX_WRITE_SCOPE_V1",
                "paths": ["founder_briefs/market_strategy.html"],
            },
        },
    ]
    strategy_before = deepcopy(plan["operation"]["tasks"][1])
    html_before = deepcopy(plan["operation"]["tasks"][2])

    normalized, _ = research_department.normalize_ceo_plan(
        deepcopy(plan),
        "用 Claude、GPT、Gemini 各自研究市場，研究主管整合後繼續做策略和 HTML。",
    )
    tasks = normalized["operation"]["tasks"]
    assert tasks[-2] == strategy_before
    assert tasks[-1] == html_before
    assert [row["title"] for row in tasks].count("Product strategy") == 1
    assert [row["title"] for row in tasks].count("Build founder HTML brief") == 1
    operations.validate_plan(deepcopy(normalized))


def test_current_generic_multi_ai_research_uses_active_department_roster_but_named_roster_is_exact(ctx):
    """Generic multi-AI may use the team; explicit named providers are not silently expanded or substituted."""
    from eason_one.services import research_department

    _install_research_department_provider_configs()
    legacy = Employee.query.filter_by(slug="researcher").one()
    critic = Employee.query.filter_by(slug="critic").one()

    generic, _ = research_department.normalize_ceo_plan(
        deepcopy(multi._plan(legacy, critic)),
        "我要多個 AI 獨立研究這個問題，再由研究部門整合。",
    )
    generic_slugs = [db.session.get(Employee, row["assignee_employee_id"]).slug for row in generic["operation"]["tasks"]]
    assert generic_slugs[:5] == [
        "openai-researcher", "claude-researcher", "gemini-researcher", "perplexity-researcher", "research-director",
    ]
    assert generic_slugs[5:] == ["critic", "researcher"]

    named, _ = research_department.normalize_ceo_plan(
        deepcopy(multi._plan(legacy, critic)),
        "用 Claude 跟 Perplexity 兩個 AI 獨立研究，最後回來整合。",
    )
    named_slugs = [db.session.get(Employee, row["assignee_employee_id"]).slug for row in named["operation"]["tasks"]]
    assert named_slugs == ["claude-researcher", "perplexity-researcher", "research-director", "critic", "researcher"]


def test_current_provider_negation_is_authority_not_a_multi_ai_keyword_shortcut(ctx):
    """'Do not use X' must never be reversed merely because X appears in the sentence."""
    from eason_one.services import research_department

    _install_research_department_provider_configs()
    legacy = Employee.query.filter_by(slug="researcher").one()
    critic = Employee.query.filter_by(slug="critic").one()

    single_request = "這份研究不要 OpenAI，只要 Claude。"
    assert research_department.requests_multi_ai_research(single_request) is False
    single, _ = research_department.normalize_ceo_plan(
        deepcopy(multi._plan(legacy, critic)), single_request,
    )
    research_task = next(row for row in single["operation"]["tasks"] if row["title"] == "Research branch")
    assert db.session.get(Employee, research_task["assignee_employee_id"]).slug == "claude-researcher"
    assert "do not use OpenAI" in research_task["objective"]

    multi_request = "我要多個 AI 研究，但不要 OpenAI。"
    expanded, _ = research_department.normalize_ceo_plan(
        deepcopy(multi._plan(legacy, critic)), multi_request,
    )
    branch_slugs = [
        db.session.get(Employee, row["assignee_employee_id"]).slug
        for row in expanded["operation"]["tasks"][:4]
    ]
    assert branch_slugs == ["claude-researcher", "gemini-researcher", "perplexity-researcher", "research-director"]
    assert "openai-researcher" not in branch_slugs

    # The prohibition is Operation-wide execution authority, not merely a roster
    # filter. Even the Director synthesis may not route back through OpenAI.
    ceo = Employee.query.filter_by(slug="ceo").one()
    director = Employee.query.filter_by(slug="research-director").one()
    openai_model = ModelConfig.query.filter_by(provider_key="openai", active=True).order_by(ModelConfig.id).first()
    director.current_model_config_id = openai_model.id
    monkeypatch_env = {
        "OPENAI_API_KEY": "test-key", "ANTHROPIC_API_KEY": "test-key",
        "GEMINI_API_KEY": "test-key", "PERPLEXITY_API_KEY": "test-key",
    }
    # ctx exposes pytest's app context but not monkeypatch; os.environ is restored
    # manually to keep this regression self-contained.
    import os
    previous = {key: os.environ.get(key) for key in monkeypatch_env}
    try:
        os.environ.update(monkeypatch_env)
        operation = operations.propose_operation(
            ceo, expanded, route_type="FULL_PROJECT", founder_request=multi_request,
        )
        assert (operation.memory_json or {}).get("execution_constraints", {}).get("excluded_providers") == ["openai"]
        from eason_one.services import execution_policy
        selected = execution_policy.select_execution_model(director, operation, "TASK_EXECUTION")
        assert selected.provider_key != "openai"
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_current_research_routing_makes_multi_ai_a_project_not_a_fake_meeting(ctx):
    """Independent multi-model research is parallel Work; an explicit meeting still remains a Meeting."""
    from eason_one.services import operation_kernel

    route = operation_kernel.route_command("請用 Claude、GPT、Gemini 多個 AI 獨立研究這個市場")
    assert route["route_type"] == "FULL_PROJECT"
    assert "multiple-AI research" in route["reason"]

    meeting = operation_kernel.route_command("請讓 Claude、GPT、Gemini 開會一起討論這個研究")
    assert meeting["route_type"] == "SHORT_MEETING"


def test_current_explicit_single_provider_research_maps_to_dedicated_employee(ctx):
    """CEO/Founder can pick one named AI Researcher without inventing another Employee."""
    from eason_one.services import research_department

    _install_research_department_provider_configs()
    legacy = Employee.query.filter_by(slug="researcher").one()
    critic = Employee.query.filter_by(slug="critic").one()
    plan, notes = research_department.normalize_ceo_plan(
        deepcopy(multi._plan(legacy, critic)),
        "這份研究只叫 Claude Researcher 去做。",
    )
    research_task = next(row for row in plan["operation"]["tasks"] if "RESEARCH" in row["required_capabilities"])
    assert db.session.get(Employee, research_task["assignee_employee_id"]).slug == "claude-researcher"
    assert research_task["reviewer_employee_id"] is None
    assert any("Claude Researcher" in row for row in notes)


def test_current_research_director_delegates_ordinary_work_using_task_metadata_and_keeps_synthesis(ctx):
    """Head can choose a specialist, while Department synthesis stays owned by the Head."""
    from eason_one.models import WorkAssignment
    from eason_one.services import team_formation

    _install_research_department_provider_configs()
    ceo = Employee.query.filter_by(slug="ceo").one()
    director = Employee.query.filter_by(slug="research-director").one()
    claude = Employee.query.filter_by(slug="claude-researcher").one()
    project = Project(
        name="Research Department delegation", objective="Delegate governed research",
        status="ACTIVE", priority="MEDIUM", environment="LIVE", owner_employee_id=ceo.id,
        real_budget_limit=Decimal("10"),
    )
    db.session.add(project); db.session.flush()
    work = Work(
        project_id=project.id, title="External market research", purpose="Assess the market independently",
        expected_output="Research perspective", acceptance_criteria="Return an explicit uncertainty statement",
        state="READY", work_type="DELIVERY", resource_ceiling_twd=Decimal("2"), retry_limit=1,
        runtime_control_json={"staffing_requirements": {"required_capabilities": ["RESEARCH"]}},
    )
    db.session.add(work); db.session.flush()
    task = Task(
        project_id=project.id, work_id=work.id, title="Claude branch",
        objective="Use Claude Researcher for this approved research branch.", status="ASSIGNED",
        assigned_employee_id=director.id, reviewer_employee_id=None,
        required_output="Independent perspective", acceptance_criteria=work.acceptance_criteria,
    )
    db.session.add(task)
    db.session.add(WorkAssignment(
        work_id=work.id, employee_id=director.id, responsibility="OWNER",
        assigned_by_employee_id=ceo.id, reason="Founder delegated staffing to Research Director",
    ))
    db.session.flush()

    result = team_formation.reconcile_work(work)
    assert result["status"] == "RESEARCH_DEPARTMENT_DELEGATED"
    assert result["employee_id"] == claude.id
    assignment = work_runtime.active_assignment(work)
    assert assignment.employee_id == claude.id
    assert task.assigned_employee_id == claude.id
    evidence = (work.runtime_control_json or {})["team_formation"]["selection_evidence"]
    assert evidence["schema"] == "RESEARCH_DEPARTMENT_DELEGATION_V1"
    assert evidence["manager_employee_id"] == director.id
    assert evidence["selected_employee_id"] == claude.id
    assert evidence["selected_provider"] == "anthropic"

    synthesis = Work(
        project_id=project.id, title="Synthesize Research Department findings",
        purpose="Reconcile accepted specialist findings and preserve disagreements",
        expected_output="Department conclusion", acceptance_criteria="Explain material disagreement",
        state="READY", work_type="DELIVERY", resource_ceiling_twd=Decimal("2"), retry_limit=1,
        runtime_control_json={"staffing_requirements": {"required_capabilities": ["RESEARCH"]}},
    )
    db.session.add(synthesis); db.session.flush()
    db.session.add(WorkAssignment(
        work_id=synthesis.id, employee_id=director.id, responsibility="OWNER",
        assigned_by_employee_id=ceo.id, reason="Director owns synthesis",
    ))
    db.session.flush()
    kept = team_formation.reconcile_work(synthesis)
    assert kept["status"] == "TEAM_MATCHED"
    assert kept["employee_id"] == director.id
    assert work_runtime.active_assignment(synthesis).employee_id == director.id


def test_current_dedicated_researcher_provider_family_is_a_hard_execution_boundary(ctx, monkeypatch):
    """A named AI Researcher may upgrade model version in-family but cannot silently become another provider."""
    from eason_one.services import execution_policy

    models = _install_research_department_provider_configs()
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("PERPLEXITY_API_KEY", "test-key")
    claude = Employee.query.filter_by(slug="claude-researcher").one()
    openai = Employee.query.filter_by(slug="openai-researcher").one()

    assert execution_policy.select_execution_model(claude).provider_key == "anthropic"
    assert execution_policy.select_research_model(claude) is None
    assert execution_policy.select_execution_model(openai).provider_key == "openai"
    assert execution_policy.select_research_model(openai).provider_key == "openai"

    claude.current_model_config_id = models["openai"].id
    db.session.flush()
    with pytest.raises(ValueError, match="RESEARCH_SPECIALIZATION_MISMATCH"):
        execution_policy.select_execution_model(claude)


def test_current_retry_selection_rejects_malformed_model_config_even_during_recovery(ctx, monkeypatch):
    """Recovery cannot make an unpriced/zero-output model executable when first-attempt policy would reject it."""
    from eason_one.services import execution_policy

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    employee = Employee.query.filter_by(slug="researcher").one()
    for existing in ModelConfig.query.filter_by(provider_key="openai").all():
        existing.active = False
    db.session.flush()
    malformed = ModelConfig(
        label="Broken cheap retry", provider_key="openai", model_name="broken-retry",
        input_price_per_million=Decimal("0"), output_price_per_million=Decimal("0"),
        request_price_per_call=Decimal("0"), currency="TWD", max_output_tokens=0,
        active=True, archived=False,
    )
    valid = ModelConfig(
        label="Valid retry", provider_key="openai", model_name="valid-retry",
        input_price_per_million=Decimal("1"), output_price_per_million=Decimal("2"),
        request_price_per_call=Decimal("0.1"), currency="TWD", max_output_tokens=4096,
        active=True, archived=False,
    )
    db.session.add_all([malformed, valid]); db.session.flush()
    failed = type("FailedRun", (), {
        "provider_key_snapshot": "openai", "model_name_snapshot": "failed-primary",
    })()

    selected = execution_policy.select_retry_model(employee, failed)
    assert selected is not None
    assert selected.id == valid.id
    assert execution_policy.retry_model_eligible(employee, malformed) is False


def test_current_orchestration_context_never_claims_invalid_provider_binding_is_executable(ctx):
    """Planner context exposes model-policy failure instead of silently advertising employee.current_model."""
    from eason_one.services import company_kernel, multi_agent, research_department

    models = _install_research_department_provider_configs()
    ceo = Employee.query.filter_by(slug="ceo").one()
    legacy = Employee.query.filter_by(slug="researcher").one()
    critic = Employee.query.filter_by(slug="critic").one()
    plan, _ = research_department.normalize_ceo_plan(
        deepcopy(multi._plan(legacy, critic)),
        "只用 Claude 做這份研究。",
    )
    operation = operations.propose_operation(
        ceo, plan, route_type="FULL_PROJECT", founder_request="只用 Claude 做這份研究。",
    )
    operations.approve(operation)
    company_kernel.adopt_operation(operation)
    db.session.refresh(operation)
    claude_task = next(
        task for task in operation.tasks
        if getattr(task.assigned_employee, "slug", "") == "claude-researcher"
    )
    claude = claude_task.assigned_employee
    claude.current_model_config_id = models["openai"].id
    db.session.flush()

    row = multi_agent._task_model(claude_task, operation)
    assert row["provider"] is None
    assert row["model"] is None
    assert "RESEARCH_SPECIALIZATION_MISMATCH" in row["execution_model_error"]
    context = multi_agent.orchestration_context(operation)
    assert "execution_model_error:" in context
    assert "RESEARCH_SPECIALIZATION_MISMATCH" in context


def test_current_research_department_employee_cards_show_provider_model_without_merging_identity(ctx):
    """Founder can see 'who' and 'which AI core' as separate identity fields."""
    from pathlib import Path

    template = (Path(__file__).resolve().parents[1] / "eason_one" / "templates" / "_employee_card.html").read_text(encoding="utf-8")
    assert "item.employee.name" in template
    assert "item.employee.current_model.provider_key" in template
    assert "item.employee.current_model.model_name" in template
    assert "Researcher · Claude" not in template


def test_current_multi_ai_research_fallback_topology_runs_specialists_independently_then_director(ctx):
    """Planner outage cannot serialize specialists or bypass the accountable Director handoff."""
    from eason_one.services import company_kernel, multi_agent, research_department

    _install_research_department_provider_configs()
    ceo = Employee.query.filter_by(slug="ceo").one()
    legacy = Employee.query.filter_by(slug="researcher").one()
    critic = Employee.query.filter_by(slug="critic").one()
    plan, _ = research_department.normalize_ceo_plan(
        deepcopy(multi._plan(legacy, critic)),
        "用 Claude、GPT、Gemini 三個 AI 獨立研究，最後由研究主管整合。",
    )
    operation = operations.propose_operation(
        ceo, plan, route_type="FULL_PROJECT",
        founder_request="用 Claude、GPT、Gemini 三個 AI 獨立研究，最後由研究主管整合。",
    )
    operations.approve(operation)
    company_kernel.adopt_operation(operation)
    db.session.refresh(operation)

    fallback = multi_agent.conservative_fallback(operation, "planner unavailable")
    rows = {row["task_id"]: row for row in fallback["tasks"]}
    branches = [
        task for task in operation.tasks
        if getattr(task.assigned_employee, "slug", "") in {"openai-researcher", "claude-researcher", "gemini-researcher"}
    ]
    director = next(
        task for task in operation.tasks
        if getattr(task.assigned_employee, "slug", "") == "research-director"
    )
    old_synthesis = next(task for task in operation.tasks if task.title == "Synthesis branch")

    assert len(branches) == 3
    assert all(rows[task.id]["depends_on_task_ids"] == [] for task in branches)
    assert set(rows[director.id]["depends_on_task_ids"]) == {task.id for task in branches}
    assert rows[director.id]["role"] == "SYNTHESIS"
    assert director.id in rows[old_synthesis.id]["depends_on_task_ids"]
    assert fallback["max_parallelism"] >= 3


def test_current_successful_orchestrator_cannot_return_legal_dag_that_skips_researchers(ctx):
    """A cycle-free planner answer is still invalid if Director synthesis does not wait for all selected specialists."""
    from eason_one.services import company_kernel, multi_agent, research_department

    _install_research_department_provider_configs()
    ceo = Employee.query.filter_by(slug="ceo").one()
    legacy = Employee.query.filter_by(slug="researcher").one()
    critic = Employee.query.filter_by(slug="critic").one()
    plan, _ = research_department.normalize_ceo_plan(
        deepcopy(multi._plan(legacy, critic)),
        "用 Claude、GPT、Gemini 三個 AI 獨立研究，最後由研究主管整合。",
    )
    operation = operations.propose_operation(
        ceo, plan, route_type="FULL_PROJECT",
        founder_request="用 Claude、GPT、Gemini 三個 AI 獨立研究，最後由研究主管整合。",
    )
    operations.approve(operation)
    company_kernel.adopt_operation(operation)
    db.session.refresh(operation)

    branches = [
        task for task in operation.tasks
        if getattr(task.assigned_employee, "slug", "") in {"openai-researcher", "claude-researcher", "gemini-researcher"}
    ]
    director = next(task for task in operation.tasks if getattr(task.assigned_employee, "slug", "") == "research-director")
    critical = next(task for task in operation.tasks if task.title == "Critical branch")
    final_synthesis = next(task for task in operation.tasks if task.title == "Synthesis branch")
    branch_ids = [task.id for task in branches]

    wrong_rows = [
        {"task_id": task.id, "depends_on_task_ids": [], "role": "WORKER", "reason": "Independent specialist."}
        for task in branches
    ] + [
        {"task_id": director.id, "depends_on_task_ids": [branch_ids[0]], "role": "SYNTHESIS", "reason": "Incorrect partial synthesis."},
        {"task_id": critical.id, "depends_on_task_ids": [], "role": "VERIFIER", "reason": "Independent challenge."},
        {"task_id": final_synthesis.id, "depends_on_task_ids": [director.id, critical.id], "role": "SYNTHESIS", "reason": "Final synthesis."},
    ]
    with pytest.raises(ValueError, match="must wait for every selected Researcher"):
        multi_agent.validate_payload(operation, {
            "strategy": "PARALLEL_DAG", "rationale": "Looks legal but drops two branches.",
            "max_parallelism": 3, "tasks": wrong_rows,
        })

    correct_rows = [
        {"task_id": task.id, "depends_on_task_ids": [], "role": "WORKER", "reason": "Independent specialist."}
        for task in branches
    ] + [
        {"task_id": director.id, "depends_on_task_ids": branch_ids, "role": "SYNTHESIS", "reason": "Consumes every specialist."},
        {"task_id": critical.id, "depends_on_task_ids": [], "role": "VERIFIER", "reason": "Independent challenge."},
        {"task_id": final_synthesis.id, "depends_on_task_ids": [director.id, critical.id], "role": "SYNTHESIS", "reason": "Final synthesis."},
    ]
    normalized = multi_agent.validate_payload(operation, {
        "strategy": "PARALLEL_DAG", "rationale": "All approved research branches converge before downstream synthesis.",
        "max_parallelism": 3, "tasks": correct_rows,
    })
    director_row = next(row for row in normalized["tasks"] if row["task_id"] == director.id)
    assert set(director_row["depends_on_task_ids"]) == set(branch_ids)


def test_current_live_research_review_budget_prices_semantic_reviewer_not_search_request(monkeypatch, ctx):
    """Founder preapproval prices the workflow review_work() actually executes."""
    legacy = Employee.query.filter_by(slug="researcher").one()
    critic = Employee.query.filter_by(slug="critic").one()

    monkeypatch.setattr(operations, "_estimate_call", lambda employee, input_tokens, output_tokens, **_kwargs: Decimal("1"))
    monkeypatch.setattr(operations, "_estimate_research_call", lambda employee, input_tokens, output_tokens, **_kwargs: Decimal("9"))

    _estimate, breakdown, _meeting = operations.execution_budget_estimate(
        deepcopy(multi._plan(legacy, critic))
    )
    research_execution = next(
        row for row in breakdown
        if row.get("kind") == "TASK_EXECUTION" and row.get("title") == "Research branch"
    )
    research_review = next(
        row for row in breakdown
        if row.get("kind") == "TASK_REVIEW" and row.get("title") == "Research branch"
    )
    assert Decimal(research_execution["estimated_twd"]) == Decimal("9")
    assert Decimal(research_review["estimated_twd"]) == Decimal("1")


def test_current_research_only_constraint_scopes_director_and_retry_without_blocking_unrelated_critic(ctx, monkeypatch):
    """Research-only provider authority follows the Department, not unrelated downstream roles."""
    from eason_one.services import execution_policy, research_department

    models = _install_research_department_provider_configs()
    for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "PERPLEXITY_API_KEY"):
        monkeypatch.setenv(key, "test-key")

    legacy = Employee.query.filter_by(slug="researcher").one()
    critic = Employee.query.filter_by(slug="critic").one()
    ceo = Employee.query.filter_by(slug="ceo").one()
    director = Employee.query.filter_by(slug="research-director").one()
    request = "這個研究只用 Claude，研究完成後照正常流程做獨立批判。"
    plan, _ = research_department.normalize_ceo_plan(deepcopy(multi._plan(legacy, critic)), request)
    operation = operations.propose_operation(ceo, plan, route_type="FULL_PROJECT", founder_request=request)

    constraints = execution_policy.execution_constraints(operation)
    assert constraints["research_allowed_providers"] == ["anthropic"]
    assert execution_policy._research_provider_scope(director, constraints) == {"anthropic"}
    assert execution_policy._research_provider_scope(critic, constraints) == set()
    assert execution_policy.retry_model_eligible(director, models["anthropic"], operation) is True
    assert execution_policy.retry_model_eligible(director, models["openai"], operation) is False
    # The Research allowlist is not a global provider ban. A non-Research Critic
    # may still use its governed execution policy after the research stage.
    assert execution_policy.retry_model_eligible(critic, models["openai"], operation) is True


def test_current_open_operation_dedupe_cannot_swallow_new_provider_authority(ctx):
    """Same title/project under changed Founder provider authority is a new proposal, not a duplicate click."""
    _install_research_department_provider_configs()
    ceo = Employee.query.filter_by(slug="ceo").one()
    legacy = Employee.query.filter_by(slug="researcher").one()
    critic = Employee.query.filter_by(slug="critic").one()
    plan = multi._plan(legacy, critic)

    first = operations.propose_operation(
        ceo, deepcopy(plan), route_type="FULL_PROJECT",
        founder_request="這份研究不要 OpenAI。",
    )
    second = operations.propose_operation(
        ceo, deepcopy(plan), route_type="FULL_PROJECT",
        founder_request="這份研究不要 Gemini。",
    )
    assert second.id != first.id
    assert (first.memory_json or {})["execution_constraints"]["excluded_providers"] == ["openai"]
    assert (second.memory_json or {})["execution_constraints"]["excluded_providers"] == ["gemini"]


def test_current_open_operation_dedupe_cannot_swallow_new_founder_budget_cap(ctx):
    """Changing a hard budget cap changes authority even when the generated plan text is identical."""
    ceo = Employee.query.filter_by(slug="ceo").one()
    legacy = Employee.query.filter_by(slug="researcher").one()
    critic = Employee.query.filter_by(slug="critic").one()
    plan = multi._plan(legacy, critic)

    first = operations.propose_operation(
        ceo, deepcopy(plan), route_type="FULL_PROJECT",
        founder_request="這個專案預算上限 100 元。",
    )
    second = operations.propose_operation(
        ceo, deepcopy(plan), route_type="FULL_PROJECT",
        founder_request="這個專案預算上限 90 元。",
    )
    assert second.id != first.id
    assert (first.memory_json or {})["founder_declared_budget_cap_twd"] == "100.0000"
    assert (second.memory_json or {})["founder_declared_budget_cap_twd"] == "90.0000"


def test_current_new_project_freezes_execution_constraints_and_later_mission_inherits_them(ctx):
    """A Founder restriction from Project creation survives later CEO-delegated Missions even when wording disappears."""
    _install_research_department_provider_configs()
    ceo = Employee.query.filter_by(slug="ceo").one()
    legacy = Employee.query.filter_by(slug="researcher").one()
    critic = Employee.query.filter_by(slug="critic").one()
    initial_request = "建立這個研究專案，但整個專案不要使用 OpenAI。"

    initial = operations.propose_operation(
        ceo, deepcopy(multi._plan(legacy, critic)), route_type="FULL_PROJECT",
        founder_request=initial_request,
    )
    operations.approve(initial)
    project = db.session.get(Project, initial.project_id)
    terms = project_contract.governing_terms(project)
    assert any("do not use provider: openai" in row.casefold() for row in terms["constraints"])

    followup_plan = deepcopy(multi._plan(legacy, critic))
    followup_plan["project_id"] = project.id
    followup_plan["operation"]["project_id"] = project.id
    # No fresh provider wording: authority must come from the frozen Project Contract.
    followup = operations.propose_operation(
        ceo, followup_plan, route_type="FULL_PROJECT",
        founder_request="繼續下一批研究工作。",
        authority_source="PROJECT_DELEGATED_CEO",
    )
    assert (followup.memory_json or {})["execution_constraints"]["excluded_providers"] == ["openai"]


def test_current_team_formation_roster_excludes_research_provider_forbidden_by_operation(ctx):
    """Staffing must not award Work to a specialist Runtime would reject at dispatch."""
    from eason_one.services import research_department, team_formation

    _install_research_department_provider_configs()
    ceo = Employee.query.filter_by(slug="ceo").one()
    legacy = Employee.query.filter_by(slug="researcher").one()
    critic = Employee.query.filter_by(slug="critic").one()
    request = "我要多個 AI 研究，但不要 OpenAI。"
    plan, _ = research_department.normalize_ceo_plan(deepcopy(multi._plan(legacy, critic)), request)
    operation = operations.propose_operation(ceo, plan, route_type="FULL_PROJECT", founder_request=request)
    operations.approve(operation)
    work = next(
        row for row in operation.works
        if row.work_type != "MANAGEMENT"
        and team_formation.infer_primary_capability(row)[0] == "RESEARCH"
    )
    ranked = team_formation._ranked_for_work(work, "RESEARCH")
    slugs = [row[5].slug for row in ranked]
    assert "openai-researcher" not in slugs
    assert any(slug in slugs for slug in ("claude-researcher", "gemini-researcher", "perplexity-researcher"))


def test_current_authority_repricing_uses_same_durable_execution_constraints(ctx, monkeypatch):
    """Approval-time repricing may not forget restrictions that proposal-time pricing obeyed."""
    ceo = Employee.query.filter_by(slug="ceo").one()
    legacy = Employee.query.filter_by(slug="researcher").one()
    critic = Employee.query.filter_by(slug="critic").one()
    operation = operations.propose_operation(
        ceo, deepcopy(multi._plan(legacy, critic)), route_type="FULL_PROJECT",
        founder_request="這個專案不要高價模型。",
    )
    assert (operation.memory_json or {})["execution_constraints"]["no_premium"] is True

    original = operations.execution_budget_estimate
    seen = []

    def recording_estimate(plan, *, include_orchestration=False, execution_constraints_override=None):
        seen.append(dict(execution_constraints_override or {}))
        return original(
            plan,
            include_orchestration=include_orchestration,
            execution_constraints_override=execution_constraints_override,
        )

    monkeypatch.setattr(operations, "execution_budget_estimate", recording_estimate)
    operations.proposal_authority_snapshot(operation)
    assert seen and seen[-1].get("no_premium") is True


def test_current_ceo_project_metadata_merge_cannot_erase_founder_execution_authority(ctx, monkeypatch):
    """CEO post-processing may enrich Project metadata but cannot overwrite compiled Founder constraints."""
    from eason_one.services.ceo import founder_request

    _install_research_department_provider_configs()
    ceo = Employee.query.filter_by(slug="ceo").one()
    payload = _ceo_founder_recovery_payload(ctx)
    payload["project"]["constraints"] = ["Keep the research scope bounded."]

    class Provider:
        def complete(self, *args, **kwargs):
            return ProviderResult(json.dumps(payload), 20, 120)

    monkeypatch.setattr("eason_one.services.execution.get_provider", lambda _key: Provider())
    run, operation = founder_request(
        ceo, "只用 Claude 做這份研究，建立專案後也不要自己換其他研究模型。"
    )
    assert run.status == "SUCCEEDED" and operation is not None, getattr(run, "error_text", None)
    memory = dict(operation.memory_json or {})
    assert memory["execution_constraints"]["research_allowed_providers"] == ["anthropic"]
    rows = [str(row).casefold() for row in (memory["new_project_spec"] or {}).get("constraints") or []]
    assert any("keep the research scope bounded" in row for row in rows)
    assert any("research providers only" in row and "claude" in row for row in rows)


def test_current_execute_boundary_rejects_caller_model_override_outside_founder_authority(ctx):
    """No review/meeting/retry caller can bypass provider authority by passing model_override directly."""
    from eason_one.services.execution import execute as governed_execute

    models = _install_research_department_provider_configs()
    ceo = Employee.query.filter_by(slug="ceo").one()
    legacy = Employee.query.filter_by(slug="researcher").one()
    critic = Employee.query.filter_by(slug="critic").one()
    plan = multi._plan(legacy, critic)
    operation = operations.propose_operation(
        ceo, plan, route_type="FULL_PROJECT",
        founder_request="這個專案不要使用 OpenAI。",
    )
    before = AgentRun.query.count()
    with pytest.raises(ValueError, match="MODEL_OVERRIDE_VIOLATES_APPROVED_EXECUTION_POLICY"):
        governed_execute(
            critic, "TASK_REVIEW", "review",
            context_override="bounded context", system_prompt_override="review",
            operation=operation, model_override=models["openai"],
        )
    assert AgentRun.query.count() == before


def test_current_project_constraint_amendment_revokes_stale_open_mission_provider_authority(ctx):
    """An open Mission cannot keep using provider authority the Founder later revoked at Project level."""
    from eason_one.services import execution_policy, research_department

    _install_research_department_provider_configs()
    ceo = Employee.query.filter_by(slug="ceo").one()
    legacy = Employee.query.filter_by(slug="researcher").one()
    critic = Employee.query.filter_by(slug="critic").one()
    request = "建立研究專案，而且研究只用 Claude。"
    plan, _ = research_department.normalize_ceo_plan(deepcopy(multi._plan(legacy, critic)), request)
    operation = operations.propose_operation(ceo, plan, route_type="FULL_PROJECT", founder_request=request)
    operations.approve(operation)
    project = db.session.get(Project, operation.project_id)

    project_contract.authorize_constraint_change(
        project,
        constraints=["Research providers only: Perplexity."],
        reason="Founder changed the Project research-provider boundary.",
        origin_employee_id=ceo.id,
    )
    db.session.commit()

    constraints = execution_policy.execution_constraints(operation)
    assert constraints.get("constraint_conflict") is True
    director = Employee.query.filter_by(slug="research-director").one()
    with pytest.raises(ValueError, match="FOUNDER_PROVIDER_CONSTRAINT_CONFLICT"):
        execution_policy.select_execution_model(director, operation, "TASK_EXECUTION")


def test_current_restart_finishes_effect_settlement_after_reservation_already_consumed(ctx):
    """Crash after cost consumption but before effect SETTLED must finish bookkeeping without replay."""
    from eason_one.services import runtime_recovery

    operation, project, researcher, _critic = multi._setup()
    work = sorted([row for row in operation.works if row.work_type != "MANAGEMENT"], key=lambda row: row.id)[0]
    task = work_runtime.task_for_work(work)
    model = researcher.current_model
    run = AgentRun(
        employee_id=researcher.id, project_id=project.id, operation_id=operation.id,
        work_id=work.id, task_id=task.id, model_config_id=model.id,
        purpose="TASK_EXECUTION", user_request="already consumed",
        system_prompt_snapshot="system", context_snapshot="context",
        raw_output="durable result", parsed_output_json={"result_summary": "durable result", "knowledge_proposals": []},
        status="SUCCEEDED", outcome="SUCCEEDED", real_cost=Decimal("0.330000"), currency="TWD",
        provider_key_snapshot=model.provider_key, model_name_snapshot=model.model_name,
        input_price_snapshot=model.input_price_per_million, output_price_snapshot=model.output_price_per_million,
        request_price_snapshot=getattr(model, "request_price_per_call", 0) or 0,
        currency_snapshot=model.currency, input_tokens=80, output_tokens=40, finished_at=now(),
    )
    db.session.add(run); db.session.flush()
    reservation = CostReservation(
        operation_id=operation.id, agent_run_id=run.id, stage="TASK_EXECUTION",
        idempotency_key=f"agent-run:{run.id}", estimated_twd=Decimal("0.400000"),
        actual_twd=Decimal("0.330000"), status="CONSUMED", resolved_at=now(),
    )
    db.session.add(reservation); db.session.flush()
    effect = ExternalEffectAttempt(
        execution_id=run.id, work_id=work.id, operation_id=operation.id,
        provider=model.provider_key, effect_kind="MODEL_INFERENCE",
        request_fingerprint="d" * 64,
        idempotency_key=f"MODEL_INFERENCE:work:{work.id}:purpose:TASK_EXECUTION",
        state="PERSISTED", estimated_cost_twd=Decimal("0.400000"),
        cost_reservation_id=reservation.id, persisted_at=now(),
    )
    db.session.add(effect); db.session.commit()

    runtime_recovery.reconcile_interrupted_provider_settlement()
    db.session.refresh(reservation); db.session.refresh(effect)
    assert reservation.status == "CONSUMED"
    assert Decimal(reservation.actual_twd) == Decimal("0.330000")
    assert effect.state == "SETTLED"
    assert Decimal(effect.actual_cost_twd) == Decimal("0.330000")


def test_current_paid_review_response_is_reused_after_crash_before_verification_persistence(ctx, monkeypatch):
    """A durable successful reviewer response cannot trigger a second paid reviewer call after restart."""
    from eason_one.services import artifacts as artifact_service
    from eason_one.services import work_execution

    operation, project, researcher, critic = multi._setup()
    work = sorted([row for row in operation.works if row.work_type != "MANAGEMENT"], key=lambda row: row.id)[0]
    task = work_runtime.task_for_work(work)
    task.reviewer_employee_id = critic.id
    contract = acceptance_contract.build(
        title=work.title,
        objective=work.purpose,
        criteria=["The persisted recommendation is supported by the submitted evidence."],
        reviewer_employee_id=critic.id,
        owner_employee_id=researcher.id,
        project_execution_terms_hash=project_contract.execution_terms_hash(project),
    )
    control = dict(work.runtime_control_json or {})
    control["acceptance_contract"] = contract
    work.runtime_control_json = control
    work.acceptance_criteria = "The persisted recommendation is supported by the submitted evidence."
    task.acceptance_criteria = work.acceptance_criteria
    if work.state == "READY":
        work_runtime.transition(work, "EXECUTING", actor_type="EMPLOYEE", actor_id=researcher.id, reason="produce artifact")
    work_runtime.transition(work, "VERIFYING", actor_type="RUNTIME", reason="await reviewer")

    producer = _v16_run(
        researcher, project, operation, work,
        purpose="TASK_EXECUTION", status="SUCCEEDED", max_tokens=1200,
    )
    producer.context_composition_json = {
        "provider_sources": [{"title": "Persisted source", "url": "https://example.test/review-source"}],
        "project_execution_terms_hash": project_contract.execution_terms_hash(project),
    }
    artifact = Artifact(
        project_id=project.id, work_id=work.id,
        artifact_type="RESEARCH_REPORT", title="Crash-resumable artifact",
    )
    db.session.add(artifact); db.session.flush()
    content = "Recommendation supported by persisted evidence."
    version = ArtifactVersion(
        artifact_id=artifact.id, version=1, producer_employee_id=researcher.id,
        execution_id=producer.id, status="SUBMITTED", content_text=content,
        content_hash=artifact_service._hash(content, None),
    )
    db.session.add(version); db.session.flush()

    criterion_id = str((contract.get("criteria") or [])[0]["id"])
    payload = {
        "decision": "ACCEPT",
        "summary": "The submitted evidence supports the recommendation.",
        "issues": [],
        "required_changes": [],
        "criterion_results": [{
            "criterion_id": criterion_id,
            "status": "PASSED",
            "evidence": "The submitted artifact and persisted source lineage support the recommendation.",
        }],
    }
    review = _v16_run(
        critic, project, operation, work,
        purpose="TASK_REVIEW", status="SUCCEEDED", max_tokens=4096,
    )
    review.raw_output = json.dumps(payload)
    review.context_composition_json = {
        "review_target": {
            "artifact_version_id": version.id,
            "artifact_content_hash": version.content_hash,
            "acceptance_contract_hash": contract["contract_hash"],
        },
        "project_execution_terms_hash": project_contract.execution_terms_hash(project),
    }
    db.session.commit()

    calls = []
    def forbidden_second_review(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("durable successful review must be resumed, not purchased twice")
    monkeypatch.setattr(work_execution, "execute", forbidden_second_review)

    result = work_execution.review_work(db.session.get(Work, work.id))
    assert calls == []
    assert result["run_id"] == review.id
    assert AgentRun.query.filter_by(work_id=work.id, purpose="TASK_REVIEW").count() == 1
    assert db.session.get(Work, work.id).state == "ACCEPTED"
    assert db.session.get(ArtifactVersion, version.id).status == "ACCEPTED"


def test_current_successful_run_becomes_stale_after_founder_execution_term_change(ctx):
    """A paid result may be preserved as history but not silently reused after Founder changes constraints."""
    from eason_one.services import work_execution

    operation, project, researcher, _critic = multi._setup()
    work = sorted([row for row in operation.works if row.work_type != "MANAGEMENT"], key=lambda row: row.id)[0]
    run = _v16_run(
        researcher, project, operation, work,
        purpose="TASK_EXECUTION", status="SUCCEEDED", max_tokens=1200,
    )
    run.context_composition_json = {
        "project_execution_terms_hash": project_contract.execution_terms_hash(project),
    }
    db.session.commit()
    assert work_execution._run_matches_current_project_terms(run, work) is True

    project_contract.authorize_constraint_change(
        project,
        constraints=["Do not widen Founder authority.", "Research providers only: Claude."],
        reason="Founder narrowed the live execution boundary.",
        origin_employee_id=project.owner_employee_id,
    )
    db.session.commit()
    assert work_execution._run_matches_current_project_terms(run, work) is False


def test_current_older_paid_review_success_survives_newer_failed_duplicate_attempt(ctx, monkeypatch):
    """A newer failed duplicate must not hide an earlier exact successful paid review after restart."""
    from eason_one.services import artifacts as artifact_service
    from eason_one.services import work_execution

    operation, project, researcher, critic = multi._setup()
    work = sorted([row for row in operation.works if row.work_type != "MANAGEMENT"], key=lambda row: row.id)[0]
    task = work_runtime.task_for_work(work)
    task.reviewer_employee_id = critic.id
    contract = acceptance_contract.build(
        title=work.title,
        objective=work.purpose,
        criteria=["The persisted recommendation is supported by the submitted evidence."],
        reviewer_employee_id=critic.id,
        owner_employee_id=researcher.id,
        project_execution_terms_hash=project_contract.execution_terms_hash(project),
    )
    control = dict(work.runtime_control_json or {})
    control["acceptance_contract"] = contract
    work.runtime_control_json = control
    work.acceptance_criteria = "The persisted recommendation is supported by the submitted evidence."
    task.acceptance_criteria = work.acceptance_criteria
    if work.state == "READY":
        work_runtime.transition(work, "EXECUTING", actor_type="EMPLOYEE", actor_id=researcher.id, reason="produce artifact")
    work_runtime.transition(work, "VERIFYING", actor_type="RUNTIME", reason="await reviewer")

    producer = _v16_run(researcher, project, operation, work, purpose="TASK_EXECUTION", status="SUCCEEDED", max_tokens=1200)
    producer.context_composition_json = {
        "provider_sources": [{"title": "Persisted source", "url": "https://example.test/review-source-old"}],
        "project_execution_terms_hash": project_contract.execution_terms_hash(project),
    }
    artifact = Artifact(project_id=project.id, work_id=work.id, artifact_type="RESEARCH_REPORT", title="Older durable review artifact")
    db.session.add(artifact); db.session.flush()
    content = "Recommendation supported by persisted evidence."
    version = ArtifactVersion(
        artifact_id=artifact.id, version=1, producer_employee_id=researcher.id,
        execution_id=producer.id, status="SUBMITTED", content_text=content,
        content_hash=artifact_service._hash(content, None),
    )
    db.session.add(version); db.session.flush()

    criterion_id = str((contract.get("criteria") or [])[0]["id"])
    payload = {
        "decision": "ACCEPT", "summary": "Supported.", "issues": [], "required_changes": [],
        "criterion_results": [{"criterion_id": criterion_id, "status": "PASSED", "evidence": "Persisted evidence supports it."}],
    }
    success = _v16_run(critic, project, operation, work, purpose="TASK_REVIEW", status="SUCCEEDED", max_tokens=4096)
    success.raw_output = json.dumps(payload)
    success.context_composition_json = {
        "review_target": {
            "artifact_version_id": version.id,
            "artifact_content_hash": version.content_hash,
            "acceptance_contract_hash": contract["contract_hash"],
        },
        "project_execution_terms_hash": project_contract.execution_terms_hash(project),
    }
    failed_duplicate = _v16_run(critic, project, operation, work, purpose="TASK_REVIEW", status="FAILED", max_tokens=4096)
    failed_duplicate.outcome = "FAILED_KNOWN"
    failed_duplicate.failure_reason = "PROVIDER_TRANSIENT_REJECTED"
    failed_duplicate.error_text = "historical accidental duplicate failed"
    db.session.commit()

    calls = []
    def forbidden_third_review(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("older exact successful review must be reused instead of buying a third call")
    monkeypatch.setattr(work_execution, "execute", forbidden_third_review)

    result = work_execution.review_work(db.session.get(Work, work.id))
    assert calls == []
    assert result["run_id"] == success.id
    assert AgentRun.query.filter_by(work_id=work.id, purpose="TASK_REVIEW").count() == 2
    assert db.session.get(Work, work.id).state == "ACCEPTED"



def test_current_older_paid_execution_success_survives_newer_failed_duplicate_attempt(ctx, monkeypatch):
    """A later failed duplicate must not hide an earlier exact successful paid Work execution."""
    from eason_one.services import work_execution

    operation, project, researcher, _critic = multi._setup()
    work = sorted(
        [row for row in operation.works if row.work_type != "MANAGEMENT"],
        key=lambda row: row.id,
    )[0]
    task = work_runtime.task_for_work(work)
    acceptance_contract.ensure_for_work(work, task=task)
    if work.state == "READY":
        work_runtime.transition(
            work, "EXECUTING", actor_type="EMPLOYEE", actor_id=researcher.id,
            reason="simulate persisted success followed by accidental duplicate",
        )

    success = _v16_run(
        researcher, project, operation, work,
        purpose="TASK_EXECUTION", status="SUCCEEDED", max_tokens=1200,
    )
    success.raw_output = "persisted successful research result"
    success.parsed_output_json = {
        "result_summary": "persisted successful research result",
        "knowledge_proposals": [],
    }
    success.context_composition_json = {
        "provider_sources": [{"title": "Persisted source", "url": "https://example.test/older-success"}],
        "project_execution_terms_hash": project_contract.execution_terms_hash(project),
    }

    failed_duplicate = _v16_run(
        researcher, project, operation, work,
        purpose="TASK_EXECUTION", status="FAILED", max_tokens=1200,
        failure_reason="PROVIDER_TRANSIENT_REJECTED",
    )
    failed_duplicate.outcome = "FAILED_KNOWN"
    failed_duplicate.error_text = "historical accidental duplicate failed"
    failed_duplicate.context_composition_json = {
        "project_execution_terms_hash": project_contract.execution_terms_hash(project),
    }
    db.session.commit()

    calls = []
    def forbidden_third_execution(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("older exact successful execution must be resumed instead of buying a third call")
    monkeypatch.setattr(work_execution, "run_task", forbidden_third_execution)

    result = work_execution.execute_work(db.session.get(Work, work.id))
    assert calls == []
    assert result["run_id"] == success.id
    assert AgentRun.query.filter_by(work_id=work.id, purpose="TASK_EXECUTION").count() == 2
    assert ArtifactVersion.query.filter_by(execution_id=success.id).count() == 1


def test_current_project_outcome_review_reuses_older_paid_success_and_finishes_projection(ctx, monkeypatch):
    """Restart consumes an older exact Project review even when a later accidental duplicate failed."""
    from eason_one.services import company_kernel

    criterion = "Founder outcome is supported by accepted evidence."
    project = core._project((criterion,))
    operation = core._operation(project)
    accepted = core._accepted_work(project, operation, criterion)
    input_hash = project_outcome.review_input_hash(project)
    contract_hash = project_contract.governing_terms(project)["governing_contract_hash"]

    success = project_outcome.latest_project_review(
        project.id, contract_hash=contract_hash, input_hash=input_hash,
    )
    assert success is not None
    success.structured_validation_status = None
    success.context_composition_json = {
        **dict(success.context_composition_json or {}),
        "project_execution_terms_hash": project_contract.execution_terms_hash(project),
    }

    critic = Employee.query.filter_by(slug="critic").one()
    management = company_kernel._management_work(operation)
    failed_duplicate = _v16_run(
        critic, project, operation, management,
        purpose="PROJECT_OUTCOME_REVIEW", status="FAILED", max_tokens=4096,
        failure_reason="PROVIDER_TRANSIENT_REJECTED",
    )
    failed_duplicate.outcome = "FAILED_KNOWN"
    failed_duplicate.error_text = "historical accidental duplicate failed"
    failed_duplicate.context_composition_json = {
        "project_outcome_input_hash": input_hash,
        "contract_hash": contract_hash,
        "project_execution_terms_hash": project_contract.execution_terms_hash(project),
    }
    db.session.commit()

    calls = []
    def forbidden_third_review(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("older exact Project review must be consumed instead of buying a third call")
    monkeypatch.setattr("eason_one.services.execution.execute", forbidden_third_review)

    result = company_kernel._run_project_outcome_review(project, operation)
    assert calls == []
    assert result["status"] == "REVIEW_REUSED"
    assert result["run_id"] == success.id
    db.session.refresh(success)
    assert success.structured_validation_status == "PASSED"
    assert success.parsed_output_json["criteria"][0]["work_ids"] == [accepted.id]
    assert AgentRun.query.filter_by(project_id=project.id, purpose="PROJECT_OUTCOME_REVIEW").count() == 2


def test_current_project_continuation_reuses_paid_planning_result_after_restart(ctx, monkeypatch):
    """A successful CEO continuation plan is resumed after process loss instead of purchased again."""
    from eason_one.services import company_kernel

    project = core._project(("Founder outcome still needs one bounded next move.",))
    prior_operation = core._operation(project, status="FAILED")
    ceo = Employee.query.filter_by(slug="ceo").one()
    researcher = Employee.query.filter_by(slug="researcher").one()
    critic = Employee.query.filter_by(slug="critic").one()
    management = company_kernel._management_work(prior_operation)

    # Use the canonical current outcome projection. The restart proof is about
    # reusing a paid plan for the same durable truth, not preserving an older
    # fixture vocabulary for criterion status.
    evaluation = project_outcome.evaluate(project)
    contract = project_contract.governing_terms(project)
    missing = [row for row in evaluation["criteria"] if row.get("status") != "SATISFIED"]
    basis = json.dumps({
        "contract_hash": contract.get("governing_contract_hash") or contract.get("contract_hash"),
        "accepted_work_ids": evaluation.get("accepted_work_ids") or [],
        "accepted_evidence": company_kernel._accepted_evidence_identity(project),
        "missing": missing,
        "failed_operation_id": None,
        "failure_reason": None,
        "failure_mode": "GENERAL",
    }, ensure_ascii=False, sort_keys=True)
    evidence_hash = hashlib.sha256(basis.encode("utf-8")).hexdigest()

    plan = deepcopy(multi._plan(researcher, critic))
    plan["project"] = None
    plan["project_id"] = project.id
    plan["operation"]["project_id"] = project.id
    plan["operation"]["title"] = "Crash-resumable bounded continuation"

    success = _v16_run(
        ceo, project, prior_operation, management,
        purpose="CEO_PROJECT_CONTINUATION", status="SUCCEEDED", max_tokens=4096,
    )
    success.raw_output = json.dumps(plan)
    success.context_composition_json = {
        "continuation_evidence_hash": evidence_hash,
        "contract_hash": contract.get("governing_contract_hash") or contract.get("contract_hash"),
        "project_execution_terms_hash": project_contract.execution_terms_hash(project),
        "continuation_mode": "GENERAL",
    }

    failed_duplicate = _v16_run(
        ceo, project, prior_operation, management,
        purpose="CEO_PROJECT_CONTINUATION", status="FAILED", max_tokens=4096,
        failure_reason="PROVIDER_TRANSIENT_REJECTED",
    )
    failed_duplicate.outcome = "FAILED_KNOWN"
    failed_duplicate.error_text = "historical accidental duplicate failed"
    failed_duplicate.context_composition_json = {
        "continuation_evidence_hash": evidence_hash,
        "project_execution_terms_hash": project_contract.execution_terms_hash(project),
    }
    db.session.commit()

    calls = []
    def forbidden_third_plan(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("durable continuation plan must be projected instead of purchased again")
    monkeypatch.setattr("eason_one.services.execution.execute", forbidden_third_plan)

    result = company_kernel._plan_continuation(project, prior_operation, evaluation)
    assert calls == []
    assert result["status"] == "CONTINUATION_CREATED"
    assert result["run_id"] == success.id
    assert result["reused_planning_run"] is True
    continuation = db.session.get(type(prior_operation), result["operation_id"])
    assert continuation.id != prior_operation.id
    assert (continuation.memory_json or {})["continuation_source_run_id"] == success.id
    assert (continuation.memory_json or {})["continuation_evidence_hash"] == evidence_hash
    assert continuation.approved_at is not None
    assert AgentRun.query.filter_by(project_id=project.id, purpose="CEO_PROJECT_CONTINUATION").count() == 2


def test_current_hr_assessment_reuses_older_paid_success_after_restart(ctx, monkeypatch):
    """Paid HR truth survives a crash before HiringRequest projection and a later failed duplicate."""
    from eason_one.services import company_kernel, workforce

    operation, project, researcher, _critic = multi._setup()
    ceo = Employee.query.filter_by(slug="ceo").one()
    hr = Employee.query.filter_by(slug="hr-director").one()
    management = company_kernel._management_work(operation)
    department = researcher.department
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    model = ModelConfig(
        label="HR reuse configured OpenAI", provider_key="openai", model_name="hr-reuse-openai",
        input_price_per_million=Decimal("1"), output_price_per_million=Decimal("2"),
        request_price_per_call=Decimal("0"), currency="TWD", max_output_tokens=2048,
        active=True, archived=False,
    )
    db.session.add(model); db.session.flush()

    request = workforce.request_hire(
        requested_by_type="FOUNDER",
        role_needed="Research support",
        problem="A bounded capability gap needs HR assessment.",
        why_now="Current Project execution is waiting for staffing truth.",
        responsibilities=["Support governed research"],
        capabilities=["RESEARCH"],
        urgency="MEDIUM",
        use_frequency="PROJECT_NEED",
        operation=operation,
        project=project,
    )

    payload = {
        "recommendation": "USE EXISTING STAFF",
        "existing_staff_alternative": researcher.name,
        "need_duration": "PROJECT",
        "duplicate_capability": "Existing Researcher can own the bounded work.",
        "candidate_template_id": None,
        "target_department_id": department.id,
        "target_position": "Research Support",
        "manager_employee_id": ceo.id,
        "recommended_model_config_id": model.id,
        "estimated_input_tokens": 1000,
        "estimated_output_tokens": 500,
        "expected_calls_per_mission": 1,
        "missions_per_month": 1,
        "max_mission_budget_twd": 2.0,
        "expected_benefit": "Reuse existing accountable capability.",
        "redundancy_risk": "LOW",
        "alternatives": ["Use existing Researcher"],
        "success_criteria": ["Capability gap closes without unnecessary hire"],
        "probation_assignments": 1,
        "instructions": "Keep work inside current Project authority.",
    }
    success = _v16_run(
        hr, project, operation, management,
        purpose="HR_ASSESSMENT", status="SUCCEEDED", max_tokens=4096,
    )
    success.raw_output = json.dumps(payload)
    success.context_composition_json = {
        "hiring_request_id": request.id,
        "authority": "GOVERNED_HR_ASSESSMENT",
        "project_execution_terms_hash": project_contract.execution_terms_hash(project),
    }

    failed_duplicate = _v16_run(
        hr, project, operation, management,
        purpose="HR_ASSESSMENT", status="FAILED", max_tokens=4096,
        failure_reason="PROVIDER_TRANSIENT_REJECTED",
    )
    failed_duplicate.outcome = "FAILED_KNOWN"
    failed_duplicate.error_text = "historical accidental duplicate failed"
    failed_duplicate.context_composition_json = {
        "hiring_request_id": request.id,
        "project_execution_terms_hash": project_contract.execution_terms_hash(project),
    }
    request.status = "HR_REVIEW"
    db.session.commit()

    retry = workforce.assessment_retry_state(request, current=now())
    assert retry["state"] == "READY"
    assert retry["reusable_run"].id == success.id

    calls = []
    def forbidden_third_assessment(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("durable paid HR assessment must be projected instead of purchased again")
    monkeypatch.setattr(workforce, "execute", forbidden_third_assessment)

    result = workforce.assess_request(db.session.get(type(request), request.id))
    assert calls == []
    assert result.id == success.id
    db.session.refresh(request)
    assert request.hr_agent_run_id == success.id
    assert request.status == "ASSESSMENT_COMPLETE"
    assert request.target_department_id == department.id
    assert request.recommended_model_config_id is not None
    assert AgentRun.query.filter_by(project_id=project.id, purpose="HR_ASSESSMENT").count() == 2


def test_current_older_success_never_masks_newer_ambiguous_execution_spend(ctx, monkeypatch):
    """Known output cannot advance Work while a newer provider dispatch still has unknown truth."""
    from eason_one.services import work_execution

    operation, project, researcher, _critic = multi._setup()
    work = sorted(
        [row for row in operation.works if row.work_type != "MANAGEMENT"],
        key=lambda row: row.id,
    )[0]
    task = work_runtime.task_for_work(work)
    acceptance_contract.ensure_for_work(work, task=task)
    if work.state == "READY":
        work_runtime.transition(
            work, "EXECUTING", actor_type="EMPLOYEE", actor_id=researcher.id,
            reason="simulate ambiguous duplicate after durable success",
        )

    success = _v16_run(
        researcher, project, operation, work,
        purpose="TASK_EXECUTION", status="SUCCEEDED", max_tokens=1200,
    )
    success.raw_output = "older paid success"
    success.parsed_output_json = {"result_summary": "older paid success", "knowledge_proposals": []}
    success.context_composition_json = {
        "project_execution_terms_hash": project_contract.execution_terms_hash(project),
    }
    ambiguous = _v16_run(
        researcher, project, operation, work,
        purpose="TASK_EXECUTION", status="FAILED", max_tokens=1200,
        failure_reason="PROVIDER_DISPATCH_AMBIGUOUS",
    )
    ambiguous.outcome = "FAILED_AMBIGUOUS"
    ambiguous.failure_stage = "POST_DISPATCH"
    ambiguous.error_text = "provider outcome unknown"
    ambiguous.context_composition_json = {
        "project_execution_terms_hash": project_contract.execution_terms_hash(project),
    }
    db.session.commit()

    calls = []
    monkeypatch.setattr(
        work_execution, "run_task",
        lambda *a, **k: calls.append((a, k)) or (_ for _ in ()).throw(AssertionError("ambiguous effect forbids replay")),
    )
    result = work_execution.execute_work(db.session.get(Work, work.id))
    assert calls == []
    assert result["status"] == "RECONCILIATION_REQUIRED"
    assert result["run_id"] == ambiguous.id
    gates = work_runtime.open_gates(db.session.get(Work, work.id))
    assert any(
        row.get("condition_type") == "RECONCILIATION"
        and row.get("issue_code") == f"WORK_EFFECT_RECONCILIATION:TASK_EXECUTION:{ambiguous.id}"
        for row in gates
    )


def test_current_settled_delivery_effect_retires_only_exact_work_reconciliation(ctx, monkeypatch):
    """A settled delivery effect wakes the exact Work gate without replaying provider work."""
    from eason_one.services import runtime_recovery, work_execution

    operation, project, researcher, _critic = multi._setup()
    work = sorted(
        [row for row in operation.works if row.work_type != "MANAGEMENT"],
        key=lambda row: row.id,
    )[0]
    task = work_runtime.task_for_work(work)
    acceptance_contract.ensure_for_work(work, task=task)
    if work.state == "READY":
        work_runtime.transition(
            work, "EXECUTING", actor_type="EMPLOYEE", actor_id=researcher.id,
            reason="simulate unresolved delivery effect",
        )

    run = _v16_run(
        researcher, project, operation, work,
        purpose="TASK_EXECUTION", status="FAILED", max_tokens=1200,
        failure_reason="PROVIDER_DISPATCH_AMBIGUOUS",
    )
    run.outcome = "FAILED_AMBIGUOUS"
    run.failure_stage = "POST_DISPATCH"
    run.error_text = "provider effect unresolved"
    run.context_composition_json = {
        "project_execution_terms_hash": project_contract.execution_terms_hash(project),
    }
    db.session.flush()
    effect = ExternalEffectAttempt(
        execution_id=run.id, provider=run.provider_key_snapshot, effect_kind="MODEL_INFERENCE",
        request_fingerprint=f"delivery-effect-{run.id}",
        idempotency_key=f"MODEL_INFERENCE:execution:{run.id}",
        state="AMBIGUOUS_POST_DISPATCH", estimated_cost_twd=Decimal("0"), dispatched_at=now(),
    )
    db.session.add(effect); db.session.commit()

    calls = []
    monkeypatch.setattr(
        work_execution, "run_task",
        lambda *a, **k: calls.append((a, k)) or (_ for _ in ()).throw(AssertionError("reconciliation must not replay provider work")),
    )
    first = work_execution.execute_work(db.session.get(Work, work.id))
    assert first["status"] == "RECONCILIATION_REQUIRED"
    issue = f"WORK_EFFECT_RECONCILIATION:TASK_EXECUTION:{run.id}"
    assert any(row.get("issue_code") == issue for row in work_runtime.open_gates(db.session.get(Work, work.id)))
    assert calls == []

    # Later settlement proves a definitive rejection. Maintenance may retire
    # only this exact reconciliation gate; it still performs zero provider calls.
    run = db.session.get(AgentRun, run.id)
    effect = db.session.get(ExternalEffectAttempt, effect.id)
    run.outcome = "FAILED_KNOWN"
    run.failure_reason = "PROVIDER_REQUEST_REJECTED"
    effect.state = "REJECTED_POST_DISPATCH"
    db.session.commit()

    assert runtime_recovery.resolve_settled_work_effect_reconciliation() == 1
    assert calls == []
    assert not any(
        row.get("condition_type") == "RECONCILIATION" and row.get("issue_code") == issue
        for row in work_runtime.open_gates(db.session.get(Work, work.id))
    )


def test_current_project_review_blocks_on_newer_ambiguous_duplicate_even_with_older_success(ctx, monkeypatch):
    """Project completion cannot hide an unknown later reviewer charge/effect."""
    from eason_one.services import company_kernel

    criterion = "Founder outcome is supported by accepted evidence."
    project = core._project((criterion,))
    operation = core._operation(project)
    core._accepted_work(project, operation, criterion)
    input_hash = project_outcome.review_input_hash(project)
    contract_hash = project_contract.governing_terms(project)["governing_contract_hash"]
    success = project_outcome.latest_project_review(
        project.id, contract_hash=contract_hash, input_hash=input_hash,
    )
    assert success is not None
    success.context_composition_json = {
        **dict(success.context_composition_json or {}),
        "project_execution_terms_hash": project_contract.execution_terms_hash(project),
    }

    critic = Employee.query.filter_by(slug="critic").one()
    management = company_kernel._management_work(operation)
    ambiguous = _v16_run(
        critic, project, operation, management,
        purpose="PROJECT_OUTCOME_REVIEW", status="FAILED", max_tokens=4096,
        failure_reason="PROVIDER_DISPATCH_AMBIGUOUS",
    )
    ambiguous.outcome = "FAILED_AMBIGUOUS"
    ambiguous.failure_stage = "POST_DISPATCH"
    ambiguous.error_text = "review dispatch outcome unknown"
    ambiguous.context_composition_json = {
        "project_outcome_input_hash": input_hash,
        "contract_hash": contract_hash,
        "project_execution_terms_hash": project_contract.execution_terms_hash(project),
    }
    db.session.commit()

    calls = []
    monkeypatch.setattr(
        "eason_one.services.execution.execute",
        lambda *a, **k: calls.append((a, k)) or (_ for _ in ()).throw(AssertionError("ambiguous review forbids replay")),
    )
    result = company_kernel._run_project_outcome_review(project, operation)
    assert calls == []
    assert result["status"] == "RECONCILIATION_REQUIRED"
    assert result["run_id"] == ambiguous.id


def test_current_hr_older_success_does_not_hide_newer_ambiguous_dispatch(ctx):
    """HR must reconcile unknown later spend before consuming an older assessment."""
    from eason_one.services import company_kernel, workforce

    operation, project, researcher, _critic = multi._setup()
    ceo = Employee.query.filter_by(slug="ceo").one()
    hr = Employee.query.filter_by(slug="hr-director").one()
    management = company_kernel._management_work(operation)
    request = workforce.request_hire(
        requested_by_type="FOUNDER",
        role_needed="Research support",
        problem="Capability gap",
        why_now="Project needs staffing truth",
        responsibilities=["Research support"],
        capabilities=["RESEARCH"],
        urgency="MEDIUM",
        use_frequency="PROJECT_NEED",
        operation=operation,
        project=project,
    )
    payload = {
        "recommendation": "USE EXISTING STAFF",
        "existing_staff_alternative": researcher.name,
        "need_duration": "PROJECT",
        "duplicate_capability": "Existing capability",
        "candidate_template_id": None,
        "target_department_id": researcher.department_id,
        "target_position": "Research Support",
        "manager_employee_id": ceo.id,
        "recommended_model_config_id": researcher.current_model.id,
        "estimated_input_tokens": 1000,
        "estimated_output_tokens": 500,
        "expected_calls_per_mission": 1,
        "missions_per_month": 1,
        "max_mission_budget_twd": 2.0,
        "expected_benefit": "Reuse staff",
        "redundancy_risk": "LOW",
        "alternatives": ["Reuse staff"],
        "success_criteria": ["Gap closed"],
        "probation_assignments": 1,
        "instructions": "Stay in Project authority",
    }
    success = _v16_run(hr, project, operation, management, purpose="HR_ASSESSMENT", status="SUCCEEDED", max_tokens=4096)
    success.raw_output = json.dumps(payload)
    success.context_composition_json = {
        "hiring_request_id": request.id,
        "project_execution_terms_hash": project_contract.execution_terms_hash(project),
    }
    ambiguous = _v16_run(
        hr, project, operation, management,
        purpose="HR_ASSESSMENT", status="FAILED", max_tokens=4096,
        failure_reason="PROVIDER_DISPATCH_AMBIGUOUS",
    )
    ambiguous.outcome = "FAILED_AMBIGUOUS"
    ambiguous.failure_stage = "POST_DISPATCH"
    ambiguous.error_text = "HR provider outcome unknown"
    ambiguous.context_composition_json = {
        "hiring_request_id": request.id,
        "project_execution_terms_hash": project_contract.execution_terms_hash(project),
    }
    request.status = "HR_REVIEW"
    db.session.commit()

    retry = workforce.assessment_retry_state(request, current=now())
    assert retry["state"] == "RECONCILIATION"
    assert retry["last_run"].id == ambiguous.id
    with pytest.raises(ValueError, match="reconcile before replay"):
        workforce.assess_request(request)


def test_current_restart_materializes_paid_task_response_without_provider_replay(ctx, monkeypatch):
    """Crash after provider settlement but before Task JSON projection must not buy the same answer twice."""
    from eason_one.services import task_execution, work_execution

    operation, project, researcher, critic = multi._setup()
    work = sorted(
        [row for row in operation.works if row.work_type != "MANAGEMENT"],
        key=lambda row: row.id,
    )[1]
    task = work_runtime.task_for_work(work)
    run = _v16_run(
        critic, project, operation, work,
        purpose="TASK_EXECUTION", status="SUCCEEDED", max_tokens=1200,
    )
    run.task_id = task.id
    run.raw_output = json.dumps({
        "result_summary": "Durable paid response recovered locally.",
        "knowledge_proposals": [],
    })
    run.parsed_output_json = None
    run.context_composition_json = {
        "project_execution_terms_hash": project_contract.execution_terms_hash(project),
        "execution_mode": "STANDARD",
    }
    db.session.commit()

    calls = []
    monkeypatch.setattr(
        work_execution,
        "run_task",
        lambda *a, **k: calls.append((a, k)) or (_ for _ in ()).throw(
            AssertionError("already-paid Task response must be postprocessed locally")
        ),
    )

    result = work_execution.execute_work(db.session.get(Work, work.id))
    recovered = db.session.get(AgentRun, run.id)
    assert calls == []
    assert recovered.parsed_output_json["result_summary"] == "Durable paid response recovered locally."
    assert (recovered.context_composition_json or {})["durable_postprocess_recovery"]["provider_replayed"] is False
    assert AgentRun.query.filter_by(work_id=work.id, purpose="TASK_EXECUTION").count() == 1
    assert result["work_id"] == work.id


def test_current_vnext_context_never_leaks_sibling_review_into_unrelated_work(ctx):
    """A sibling Work's critique is not context for another governed Work branch."""
    from eason_one.services import context as context_service

    operation, project, researcher, critic = multi._setup()
    tasks = sorted(operation.tasks, key=lambda row: row.id)
    reviewed, unrelated = tasks[0], tasks[1]
    review = _v16_run(
        critic, project, operation, db.session.get(Work, reviewed.work_id),
        purpose="TASK_REVIEW", status="SUCCEEDED", max_tokens=1200,
    )
    review.task_id = reviewed.id
    review.parsed_output_json = {
        "decision": "REVISE",
        "summary": "Only the research branch needs revision.",
        "issues": ["research-only issue"],
        "required_changes": ["research-only fix"],
        "criterion_results": [],
    }
    db.session.commit()

    assert context_service._latest_review(unrelated) is None
    text, _meta = context_service.operation_context(unrelated)
    assert "research-only issue" not in text
    assert "research-only fix" not in text


def test_current_topology_never_hides_newer_ambiguous_planner_spend(ctx, monkeypatch):
    """Older valid topology cannot erase a newer unknown provider dispatch after restart."""
    from eason_one.services import company_kernel, multi_agent

    operation, project, researcher, critic = multi._setup()
    saved_plan = dict((operation.memory_json or {}).get("multi_agent_orchestration") or {})
    memory = dict(operation.memory_json or {})
    memory.pop("multi_agent_orchestration", None)
    memory["multi_agent_current_wave"] = None
    operation.memory_json = memory
    WorkDependency.query.filter(
        WorkDependency.work_id.in_([row.id for row in operation.works])
    ).delete(synchronize_session=False)
    management = work_runtime.ensure_management_work(operation)

    success = _v16_run(
        Employee.query.filter_by(slug="ceo").one(), project, operation, management,
        purpose="ORCHESTRATION_PLAN", status="SUCCEEDED", max_tokens=2400,
    )
    success.parsed_output_json = {
        key: saved_plan[key] for key in ("strategy", "rationale", "max_parallelism", "tasks")
    }
    ambiguous = _v16_run(
        Employee.query.filter_by(slug="ceo").one(), project, operation, management,
        purpose="ORCHESTRATION_PLAN", status="FAILED", max_tokens=2400,
        failure_reason="PROVIDER_DISPATCH_AMBIGUOUS",
    )
    ambiguous.outcome = "FAILED_AMBIGUOUS"
    ambiguous.failure_stage = "POST_DISPATCH"
    ambiguous.error_text = "planner provider outcome is unknown"
    db.session.commit()

    calls = []
    monkeypatch.setattr(
        multi_agent,
        "call_orchestrator",
        lambda *a, **k: calls.append((a, k)) or (_ for _ in ()).throw(
            AssertionError("ambiguous topology effect forbids another provider call")
        ),
    )
    result = company_kernel._plan_work_topology(db.session.get(type(operation), operation.id))
    assert calls == []
    assert result["status"] == "TOPOLOGY_RECONCILIATION_REQUIRED"
    assert result["run_id"] == ambiguous.id
    assert multi_agent.current_plan(operation) is None
    assert any(
        gate.get("condition_type") == "RECONCILIATION"
        and gate.get("issue_code") == f"ORCHESTRATION_RUN_{ambiguous.id}_EFFECT_UNRESOLVED"
        for gate in work_runtime.open_gates(management)
    )


def test_current_operation_step_binds_paid_execution_before_provider_boundary(ctx):
    """Operation orchestration cannot lose the AgentRun link in the paid-call crash window."""
    import inspect
    from eason_one.services import execution as execution_service
    from eason_one.services import operations as operations_service

    execute_source = inspect.getsource(execution_service.execute)
    binding_source = inspect.getsource(execution_service._operation_step_for_execution)
    recovery_source = inspect.getsource(operations_service._recover_unbound_step_execution)
    open_step_source = inspect.getsource(operations_service._recover_open_step)

    assert "operation_step.agent_run_id = run.id" in execute_source
    assert execute_source.index("operation_step.agent_run_id = run.id") < execute_source.index("mark_dispatching(effect)")
    assert "OPERATION_STEP_ALREADY_BOUND" in binding_source
    assert "OPERATION_STEP_RETRY_LINEAGE_MISMATCH" in binding_source

    # Historical rows created before this binding existed are recoverable only
    # when one exact purpose/task/time-bounded retry lineage can be proven.
    assert "AgentRun.started_at >= step.created_at" in recovery_source
    assert "len(roots) != 1" in recovery_source
    assert "internal reconciliation is required before replay" in recovery_source
    assert "_recover_unbound_step_execution(step)" in open_step_source


def test_current_retry_boundary_rejects_unresolved_external_effects(ctx):
    """No caller may bypass effect reconciliation by invoking execute(... retry_of_run=...)."""
    import inspect
    from eason_one.services import execution as execution_service

    source = inspect.getsource(execution_service.execute)
    assert "retry_authorized" in source
    assert "EXECUTION_RETRY_FORBIDDEN_UNRESOLVED_EFFECT" in source
    assert "EXECUTION_RETRY_PURPOSE_LINEAGE_MISMATCH" in source
    assert "EXECUTION_RETRY_OPERATION_LINEAGE_MISMATCH" in source
    assert "EXECUTION_RETRY_TASK_LINEAGE_MISMATCH" in source
    assert "EXECUTION_RETRY_MEETING_LINEAGE_MISMATCH" in source


def test_current_interview_transport_retry_reuses_paid_response_without_provider_recall(ctx, monkeypatch):
    from eason_one.models import FounderInterviewMessage
    from eason_one.services import interviews

    employee = Employee.query.filter_by(slug="researcher").one()
    interview = interviews.start(employee)

    class Provider:
        def __init__(self):
            self.calls = 0
        def complete(self, *args, **kwargs):
            self.calls += 1
            return ProviderResult(
                "Use the accepted evidence and do not repeat provider work.",
                14, 8,
                request_id=f"interview-request-{self.calls}",
                response_id=f"interview-response-{self.calls}",
            )

    provider = Provider()
    monkeypatch.setattr("eason_one.services.execution.get_provider", lambda key: provider)
    original = interviews._materialize_employee_reply
    monkeypatch.setattr(
        interviews,
        "_materialize_employee_reply",
        lambda *args, **kwargs: (_ for _ in ()).throw(SystemExit("crash after paid response")),
    )

    with pytest.raises(SystemExit):
        interviews.ask(interview, "What did you learn?", request_id="same-browser-request")

    run = AgentRun.query.filter_by(employee_id=employee.id, purpose="FOUNDER_INTERVIEW").one()
    assert run.status == "SUCCEEDED"
    assert provider.calls == 1
    assert FounderInterviewMessage.query.filter_by(interview_id=interview.id, speaker="EMPLOYEE").count() == 0

    monkeypatch.setattr(interviews, "_materialize_employee_reply", original)
    recovered = interviews.ask(interview, "What did you learn?", request_id="same-browser-request")
    assert recovered.id == run.id
    assert provider.calls == 1
    assert FounderInterviewMessage.query.filter_by(interview_id=interview.id, speaker="FOUNDER").count() == 1
    assert FounderInterviewMessage.query.filter_by(interview_id=interview.id, speaker="EMPLOYEE").count() == 1
    assert ((recovered.context_composition_json or {}).get("founder_interview") or {}).get("provider_replayed") is False


def test_current_interview_unknown_dispatch_is_never_blind_replayed(ctx, monkeypatch):
    from eason_one.models import FounderInterviewMessage
    from eason_one.services import interviews

    employee = Employee.query.filter_by(slug="researcher").one()
    interview = interviews.start(employee)

    class ProcessDeathProvider:
        def __init__(self):
            self.calls = 0
        def complete(self, *args, **kwargs):
            self.calls += 1
            raise SystemExit("provider dispatch outcome unknown")

    provider = ProcessDeathProvider()
    monkeypatch.setattr("eason_one.services.execution.get_provider", lambda key: provider)

    with pytest.raises(SystemExit):
        interviews.ask(interview, "Give me the current state.", request_id="ambiguous-browser-request")

    run = AgentRun.query.filter_by(employee_id=employee.id, purpose="FOUNDER_INTERVIEW").one()
    effect = ExternalEffectAttempt.query.filter_by(execution_id=run.id).one()
    assert run.status == "RUNNING"
    assert effect.state == "DISPATCHING"
    assert provider.calls == 1

    replay = interviews.ask(interview, "Give me the current state.", request_id="ambiguous-browser-request")
    assert replay.id == run.id
    assert provider.calls == 1
    assert FounderInterviewMessage.query.filter_by(interview_id=interview.id, speaker="FOUNDER").count() == 1
    assert FounderInterviewMessage.query.filter_by(interview_id=interview.id, speaker="EMPLOYEE").count() == 0


def test_current_interview_pre_run_crash_reuses_pending_founder_turn(ctx, monkeypatch):
    from eason_one.models import FounderInterviewMessage
    from eason_one.services import interviews

    employee = Employee.query.filter_by(slug="researcher").one()
    interview = interviews.start(employee)
    original_execute = interviews.execute
    calls = {"n": 0}

    def crash_before_agent_run(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise SystemExit("crash before AgentRun creation")
        return original_execute(*args, **kwargs)

    monkeypatch.setattr(interviews, "execute", crash_before_agent_run)
    with pytest.raises(SystemExit):
        interviews.ask(interview, "Same exact pending turn", request_id="pre-run-request")

    assert AgentRun.query.filter_by(employee_id=employee.id, purpose="FOUNDER_INTERVIEW").count() == 0
    assert FounderInterviewMessage.query.filter_by(interview_id=interview.id, speaker="FOUNDER").count() == 1

    run = interviews.ask(interview, "Same exact pending turn", request_id="pre-run-request")
    assert run is not None
    assert FounderInterviewMessage.query.filter_by(interview_id=interview.id, speaker="FOUNDER").count() == 1


def test_current_status_report_transport_retry_reuses_paid_response_without_second_call(ctx, monkeypatch):
    from eason_one.services import ceo as ceo_service

    chief = Employee.query.filter_by(slug="ceo").one()

    class Provider:
        def __init__(self):
            self.calls = 0
        def complete(self, *args, **kwargs):
            self.calls += 1
            return ProviderResult(
                json.dumps({"executive_summary": "Company truth is unchanged; use the persisted snapshot."}),
                12, 8,
                request_id=f"status-request-{self.calls}",
                response_id=f"status-response-{self.calls}",
            )

    provider = Provider()
    monkeypatch.setattr("eason_one.services.execution.get_provider", lambda key: provider)
    original = ceo_service._materialize_status_report
    monkeypatch.setattr(
        ceo_service,
        "_materialize_status_report",
        lambda *args, **kwargs: (_ for _ in ()).throw(SystemExit("crash after paid status response")),
    )

    with pytest.raises(SystemExit):
        ceo_service.founder_status_report(chief, "Give me company status", request_id="status-browser-1")

    run = AgentRun.query.filter_by(purpose="CEO_STATUS_REPORT").one()
    assert run.status == "SUCCEEDED"
    assert provider.calls == 1
    assert run.parsed_output_json is None

    monkeypatch.setattr(ceo_service, "_materialize_status_report", original)
    recovered, proposal = ceo_service.founder_status_report(
        chief, "Give me company status", request_id="status-browser-1"
    )
    assert proposal is None
    assert recovered.id == run.id
    assert provider.calls == 1
    assert (recovered.parsed_output_json or {}).get("status_report") is not None


def test_current_status_report_same_request_has_bounded_known_failure_retry(ctx, monkeypatch):
    from eason_one.services import ceo as ceo_service

    chief = Employee.query.filter_by(slug="ceo").one()

    class TruncatedProvider:
        def __init__(self):
            self.calls = 0
        def complete(self, *args, **kwargs):
            self.calls += 1
            return ProviderResult(
                "{}", 10, 10,
                request_id=f"status-truncated-request-{self.calls}",
                response_id=f"status-truncated-response-{self.calls}",
                status="incomplete", incomplete_reason="max_output_tokens",
            )

    provider = TruncatedProvider()
    monkeypatch.setattr("eason_one.services.execution.get_provider", lambda key: provider)

    first, _ = ceo_service.founder_status_report(
        chief, "Give me company status", request_id="status-bounded-retry"
    )
    assert first.status == "FAILED"
    assert provider.calls == 1

    second, _ = ceo_service.founder_status_report(
        chief, "Give me company status", request_id="status-bounded-retry"
    )
    assert second.status == "FAILED"
    assert second.attempt_number == 2
    assert provider.calls == 2

    third, _ = ceo_service.founder_status_report(
        chief, "Give me company status", request_id="status-bounded-retry"
    )
    assert third.id == second.id
    assert provider.calls == 2


def test_current_founder_planning_compact_retry_cannot_repeat_forever_on_browser_replay(ctx, monkeypatch):
    from eason_one.services import ceo as ceo_service

    chief = Employee.query.filter_by(slug="ceo").one()

    class TruncatedProvider:
        def __init__(self):
            self.calls = 0
        def complete(self, *args, **kwargs):
            self.calls += 1
            return ProviderResult(
                "{}", 10, 10,
                request_id=f"planning-truncated-request-{self.calls}",
                response_id=f"planning-truncated-response-{self.calls}",
                status="incomplete", incomplete_reason="max_output_tokens",
            )

    provider = TruncatedProvider()
    monkeypatch.setattr("eason_one.services.execution.get_provider", lambda key: provider)

    run, _ = ceo_service.founder_request(
        chief,
        "Research the current market and produce a decision brief.",
        request_id="founder-planning-bounded-retry",
    )
    # The first request owns exactly one compact truncation replacement.
    assert run.status == "FAILED"
    assert run.attempt_number == 2
    assert provider.calls == 2

    replay, _ = ceo_service.founder_request(
        chief,
        "Research the current market and produce a decision brief.",
        request_id="founder-planning-bounded-retry",
    )
    assert replay.id == run.id
    assert provider.calls == 2


def test_current_interview_same_request_cannot_create_unbounded_paid_retries(ctx, monkeypatch):
    from eason_one.services import interviews

    employee = Employee.query.filter_by(slug="researcher").one()
    interview = interviews.start(employee)

    class TruncatedProvider:
        def __init__(self):
            self.calls = 0
        def complete(self, *args, **kwargs):
            self.calls += 1
            return ProviderResult(
                "partial", 10, 10,
                request_id=f"interview-truncated-request-{self.calls}",
                response_id=f"interview-truncated-response-{self.calls}",
                status="incomplete", incomplete_reason="max_output_tokens",
            )

    provider = TruncatedProvider()
    monkeypatch.setattr("eason_one.services.execution.get_provider", lambda key: provider)

    first = interviews.ask(interview, "Give me the durable answer.", request_id="interview-bounded")
    assert first.status == "FAILED"
    second = interviews.ask(interview, "Give me the durable answer.", request_id="interview-bounded")
    assert second.status == "FAILED"
    assert second.attempt_number == 2
    third = interviews.ask(interview, "Give me the durable answer.", request_id="interview-bounded")
    assert third.id == second.id
    assert provider.calls == 2


def test_current_restart_classifies_old_unowned_dispatch_without_replay(ctx, monkeypatch):
    from datetime import timedelta
    from eason_one.services import runtime_recovery

    chief = Employee.query.filter_by(slug="ceo").one()

    class ProcessDeathProvider:
        def __init__(self):
            self.calls = 0
        def complete(self, *args, **kwargs):
            self.calls += 1
            raise SystemExit("process died with standalone provider request in flight")

    provider = ProcessDeathProvider()
    monkeypatch.setattr("eason_one.services.execution.get_provider", lambda key: provider)

    with pytest.raises(SystemExit):
        execute(
            chief, "CEO_STATUS_REPORT", "Give me company status",
            context_override="{}",
            system_prompt_override=chief.system_instructions + "\nCEO_STATUS_REPORT",
            response_schema={"name": "status", "schema": {"type": "object"}},
        )

    run = AgentRun.query.filter_by(purpose="CEO_STATUS_REPORT").one()
    effect = ExternalEffectAttempt.query.filter_by(execution_id=run.id).one()
    assert run.status == "RUNNING"
    assert effect.state == "DISPATCHING"
    assert provider.calls == 1

    changed = runtime_recovery.recover_interrupted_standalone_runs(
        before=now() + timedelta(seconds=1)
    )
    db.session.expire_all()
    run = db.session.get(AgentRun, run.id)
    effect = ExternalEffectAttempt.query.filter_by(execution_id=run.id).one()
    assert changed == 1
    assert provider.calls == 1
    assert run.outcome == "FAILED_AMBIGUOUS"
    assert effect.state == "AMBIGUOUS_POST_DISPATCH"


def test_current_project_management_compact_recovery_respects_total_attempt_cap(ctx):
    from eason_one.services import company_kernel

    operation, project, researcher, critic = multi._setup()
    management = work_runtime.ensure_management_work(operation)
    fingerprint = "same-evidence"
    assert company_kernel._project_attempt_budget_available(
        project.id, purpose="CEO_PROJECT_CONTINUATION",
        fingerprint_key="continuation_evidence_hash", fingerprint=fingerprint,
    )
    for _ in range(3):
        run = _v16_run(
            Employee.query.filter_by(slug="ceo").one(), project, operation, management,
            purpose="CEO_PROJECT_CONTINUATION", status="FAILED", max_tokens=2400,
            failure_reason="OUTPUT_TRUNCATED",
        )
        run.context_composition_json = {
            **dict(run.context_composition_json or {}),
            "continuation_evidence_hash": fingerprint,
        }
    db.session.commit()
    assert not company_kernel._project_attempt_budget_available(
        project.id, purpose="CEO_PROJECT_CONTINUATION",
        fingerprint_key="continuation_evidence_hash", fingerprint=fingerprint,
    )


def test_current_hr_compact_truncation_retry_is_inside_hr_attempt_budget(ctx):
    import inspect
    from eason_one.services import workforce

    source = inspect.getsource(workforce.assess_request)
    assert "len(assessment_attempts(request)) < HR_ASSESSMENT_MAX_AUTOMATIC_ATTEMPTS" in source


def test_current_legacy_task_review_same_result_cannot_be_rebought_forever(ctx, monkeypatch):
    from eason_one.services import reviews

    operation, project, researcher, critic = multi._setup()
    task = sorted(operation.tasks, key=lambda row: row.id)[0]
    task.status = "REVIEW"
    task.result_summary = "Exact unchanged submitted result"
    db.session.commit()

    class TruncatedProvider:
        def __init__(self):
            self.calls = 0
        def complete(self, *args, **kwargs):
            self.calls += 1
            return ProviderResult(
                "{}", 10, 10,
                request_id=f"legacy-review-truncated-request-{self.calls}",
                response_id=f"legacy-review-truncated-response-{self.calls}",
                status="incomplete", incomplete_reason="max_output_tokens",
            )

    provider = TruncatedProvider()
    monkeypatch.setattr("eason_one.services.execution.get_provider", lambda key: provider)

    first = reviews.run_review(task)
    assert first.status == "FAILED"
    assert provider.calls == 1

    second = reviews.run_review(task)
    assert second.status == "FAILED"
    assert second.attempt_number == 2
    assert provider.calls == 2

    third = reviews.run_review(task)
    assert third.id == second.id
    assert provider.calls == 2


def test_current_orchestration_recovers_paid_raw_response_without_provider_replay(ctx, monkeypatch):
    """A crash after paid topology response must finish local DAG materialization only."""
    from eason_one.services import multi_agent

    operation, project, researcher, critic = multi._setup()
    tasks = sorted(operation.tasks, key=lambda row: row.id)
    memory = dict(operation.memory_json or {})
    memory.pop("multi_agent_orchestration", None)
    memory["multi_agent_current_wave"] = None
    operation.memory_json = memory
    db.session.commit()

    payload = {
        "strategy": "PARALLEL_DAG",
        "rationale": "Two independent branches feed one bounded synthesis.",
        "max_parallelism": 2,
        "tasks": [
            {"task_id": tasks[0].id, "depends_on_task_ids": [], "role": "WORKER", "reason": "Independent evidence."},
            {"task_id": tasks[1].id, "depends_on_task_ids": [], "role": "VERIFIER", "reason": "Independent challenge."},
            {"task_id": tasks[2].id, "depends_on_task_ids": [tasks[0].id, tasks[1].id], "role": "SYNTHESIS", "reason": "Consumes both accepted branches."},
        ],
    }

    class Provider:
        def __init__(self):
            self.calls = 0
        def complete(self, *args, **kwargs):
            self.calls += 1
            return ProviderResult(
                json.dumps(payload), 20, 20,
                request_id=f"orchestration-request-{self.calls}",
                response_id=f"orchestration-response-{self.calls}",
            )

    provider = Provider()
    monkeypatch.setattr("eason_one.services.execution.get_provider", lambda key: provider)
    original_validate = multi_agent.validate_payload
    monkeypatch.setattr(
        multi_agent,
        "validate_payload",
        lambda *a, **k: (_ for _ in ()).throw(SystemExit("crash after paid orchestration response")),
    )

    with pytest.raises(SystemExit):
        multi_agent.call_orchestrator(operation)

    paid = AgentRun.query.filter_by(
        operation_id=operation.id, purpose="ORCHESTRATION_PLAN"
    ).one()
    effect = ExternalEffectAttempt.query.filter_by(execution_id=paid.id).one()
    assert paid.status == "SUCCEEDED"
    assert paid.raw_output
    assert paid.parsed_output_json is None
    assert effect.state == "SETTLED"
    assert provider.calls == 1

    monkeypatch.setattr(multi_agent, "validate_payload", original_validate)
    recovered = multi_agent.call_orchestrator(operation)
    assert recovered.id == paid.id
    assert recovered.status == "SUCCEEDED"
    assert recovered.parsed_output_json["strategy"] == "PARALLEL_DAG"
    assert (recovered.context_composition_json or {})["orchestration_local_recovery"]["provider_replayed"] is False
    assert provider.calls == 1
    assert AgentRun.query.filter_by(
        operation_id=operation.id, purpose="ORCHESTRATION_PLAN"
    ).count() == 1


def test_current_definitive_provider_rejection_can_fail_over_without_founder(ctx):
    """A proven no-completion 4xx is company-owned when another governed model exists."""
    from types import SimpleNamespace
    from eason_one.services import execution_policy, work_execution

    operation, project, researcher, critic = multi._setup()
    task = sorted(operation.tasks, key=lambda row: row.id)[0]
    failed = SimpleNamespace(
        outcome="FAILED_KNOWN",
        failure_reason="PROVIDER_REQUEST_REJECTED",
        provider_key_snapshot="openai",
        model_name_snapshot="rejected-openai-model",
    )

    assert work_execution.automatic_retry_allowed(failed, task) is True
    alternate = execution_policy.select_retry_model(
        task.assigned_employee, failed, operation, "TASK_EXECUTION"
    )
    assert alternate is not None
    assert alternate.provider_key != failed.provider_key_snapshot


def test_current_provider_configuration_fault_prefers_another_governed_provider(ctx, monkeypatch):
    """A missing provider credential is a provider-family fault, not three retries of the same boundary."""
    from types import SimpleNamespace
    from eason_one.services import execution_policy

    operation, _project, _researcher, _critic = multi._setup()
    ceo = Employee.query.filter_by(slug="ceo").one()
    for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "PERPLEXITY_API_KEY"):
        monkeypatch.setenv(key, "test-key")

    failed = SimpleNamespace(
        outcome="FAILED_SAFE",
        failure_reason="PROVIDER_PREFLIGHT_FAILED",
        error_text="OpenAI provider is not configured. Set OPENAI_API_KEY and verify the configuration in Models & Providers.",
        provider_key_snapshot="openai",
        model_name_snapshot="unavailable-openai-model",
    )
    alternate = execution_policy.select_retry_model(
        ceo, failed, operation, "HR_ASSESSMENT"
    )
    assert alternate is not None
    assert alternate.provider_key != "openai"


def test_current_hr_assessment_fails_over_to_another_provider_without_founder(ctx, monkeypatch):
    """HR may recover from a known provider rejection inside existing Project authority."""
    from eason_one.services import company_kernel, workforce

    operation, project, researcher, _critic = multi._setup()
    ceo = Employee.query.filter_by(slug="ceo").one()
    hr = Employee.query.filter_by(slug="hr-director").one()
    management = company_kernel._management_work(operation)
    for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "PERPLEXITY_API_KEY"):
        monkeypatch.setenv(key, "test-key")

    openai_model = ModelConfig(
        label="HR primary OpenAI regression",
        provider_key="openai",
        model_name="hr-primary-openai",
        input_price_per_million=Decimal("1"),
        output_price_per_million=Decimal("2"),
        request_price_per_call=Decimal("0"),
        currency="TWD",
        max_output_tokens=2048,
        active=True,
        archived=False,
    )
    gemini_model = ModelConfig(
        label="HR alternate Gemini regression",
        provider_key="gemini",
        model_name="hr-alternate-gemini",
        input_price_per_million=Decimal("1.2"),
        output_price_per_million=Decimal("2.2"),
        request_price_per_call=Decimal("0"),
        currency="TWD",
        max_output_tokens=2048,
        active=True,
        archived=False,
    )
    db.session.add_all([openai_model, gemini_model]); db.session.flush()
    hr.current_model = openai_model
    db.session.commit()

    request = workforce.request_hire(
        requested_by_type="FOUNDER",
        role_needed="Research support",
        problem="A bounded capability gap needs HR assessment.",
        why_now="Current Project execution is waiting for staffing truth.",
        responsibilities=["Support governed research"],
        capabilities=["RESEARCH"],
        urgency="MEDIUM",
        use_frequency="PROJECT_NEED",
        operation=operation,
        project=project,
    )
    payload = {
        "recommendation": "USE EXISTING STAFF",
        "existing_staff_alternative": researcher.name,
        "need_duration": "PROJECT",
        "duplicate_capability": "Existing Researcher can own the bounded work.",
        "candidate_template_id": None,
        "target_department_id": researcher.department_id,
        "target_position": "Research Support",
        "manager_employee_id": ceo.id,
        "recommended_model_config_id": researcher.current_model.id,
        "estimated_input_tokens": 1000,
        "estimated_output_tokens": 500,
        "expected_calls_per_mission": 1,
        "missions_per_month": 1,
        "max_mission_budget_twd": 2.0,
        "expected_benefit": "Reuse existing accountable capability.",
        "redundancy_risk": "LOW",
        "alternatives": ["Use existing Researcher"],
        "success_criteria": ["Capability gap closes without unnecessary hire"],
        "probation_assignments": 1,
        "instructions": "Keep work inside current Project authority.",
    }
    calls = []

    def fake_execute(employee, purpose, user_request, **kwargs):
        selected = kwargs.get("model_override") or employee.current_model
        calls.append(selected)
        first = len(calls) == 1
        run = AgentRun(
            employee_id=employee.id,
            project_id=project.id,
            operation_id=operation.id,
            work_id=management.id,
            model_config_id=selected.id,
            purpose=purpose,
            user_request=user_request,
            system_prompt_snapshot=kwargs.get("system_prompt_override") or "hr",
            context_snapshot=kwargs.get("context_override") or "context",
            context_composition_json={
                **dict(kwargs.get("context_composition") or {}),
                "project_execution_terms_hash": project_contract.execution_terms_hash(project),
            },
            raw_output=None if first else json.dumps(payload),
            status="FAILED" if first else "SUCCEEDED",
            outcome="FAILED_KNOWN" if first else "SUCCEEDED",
            failure_reason="PROVIDER_REQUEST_REJECTED" if first else None,
            failure_stage="POST_DISPATCH" if first else None,
            error_text="HTTP 400 provider rejected request" if first else None,
            provider_key_snapshot=selected.provider_key,
            model_name_snapshot=selected.model_name,
            input_price_snapshot=selected.input_price_per_million,
            output_price_snapshot=selected.output_price_per_million,
            request_price_snapshot=selected.request_price_per_call,
            currency_snapshot=selected.currency,
            currency=selected.currency,
            real_cost=Decimal("0"),
            finished_at=now(),
        )
        db.session.add(run); db.session.commit()
        return run

    monkeypatch.setattr(workforce, "execute", fake_execute)
    result = workforce.assess_request(request)
    assert result.status == "SUCCEEDED"
    assert len(calls) == 2
    assert calls[0].provider_key == "openai"
    assert calls[1].provider_key != "openai"
    assert request.status == "ASSESSMENT_COMPLETE"
    assert request.hr_agent_run_id == result.id


def test_current_staffing_model_obeys_project_provider_exclusion(ctx, monkeypatch):
    """HR may not bind a new Employee to a provider the Founder has excluded."""
    from eason_one.services import workforce, execution_policy

    operation, project, _researcher, _critic = multi._setup()
    for key in ("OPENAI_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.setenv(key, "test-key")
    memory = dict(operation.memory_json or {})
    constraints = dict(memory.get("execution_constraints") or {})
    constraints["excluded_providers"] = ["openai"]
    memory["execution_constraints"] = constraints
    operation.memory_json = memory

    openai_model = ModelConfig(
        label="Staffing excluded OpenAI",
        provider_key="openai",
        model_name="staffing-excluded-openai",
        input_price_per_million=Decimal("1"), output_price_per_million=Decimal("2"),
        request_price_per_call=Decimal("0"), currency="TWD", max_output_tokens=2048,
        active=True, archived=False,
    )
    gemini_model = ModelConfig(
        label="Staffing allowed Gemini",
        provider_key="gemini",
        model_name="staffing-allowed-gemini",
        input_price_per_million=Decimal("1"), output_price_per_million=Decimal("2"),
        request_price_per_call=Decimal("0"), currency="TWD", max_output_tokens=2048,
        active=True, archived=False,
    )
    db.session.add_all([openai_model, gemini_model]); db.session.commit()
    request = workforce.request_hire(
        requested_by_type="FOUNDER",
        role_needed="Operations analyst",
        problem="Need bounded analysis capacity.",
        why_now="Approved Project workload requires it.",
        responsibilities=["Analyze bounded Project evidence"],
        capabilities=["ANALYSIS"],
        urgency="MEDIUM",
        use_frequency="PROJECT_NEED",
        operation=operation,
        project=project,
    )
    selected = execution_policy.select_staffing_model(request, openai_model)
    assert selected is not None
    assert selected.provider_key != "openai"


def test_current_paid_hr_projection_error_does_not_rebuy_provider(ctx, monkeypatch):
    """A locally invalid paid HR result becomes Company recovery, not a paid retry loop."""
    from eason_one.services import company_kernel, workforce

    operation, project, _researcher, _critic = multi._setup()
    hr = Employee.query.filter_by(slug="hr-director").one()
    management = company_kernel._management_work(operation)
    request = workforce.request_hire(
        requested_by_type="FOUNDER",
        role_needed="Bounded specialist",
        problem="Need a staffing decision.",
        why_now="Project is waiting on staffing truth.",
        responsibilities=["Bounded work"],
        capabilities=["ANALYSIS"],
        urgency="MEDIUM",
        use_frequency="PROJECT_NEED",
        operation=operation,
        project=project,
    )
    paid = _v16_run(
        hr, project, operation, management,
        purpose="HR_ASSESSMENT", status="SUCCEEDED", max_tokens=2048,
    )
    paid.raw_output = json.dumps({"intentionally": "invalid local staffing projection"})
    paid.context_composition_json = {
        "hiring_request_id": request.id,
        "project_execution_terms_hash": project_contract.execution_terms_hash(project),
    }
    request.status = "HR_REVIEW"
    db.session.commit()

    calls = []
    def projection_failure(_request):
        calls.append(_request.id)
        raise ValueError("HR proposed unknown Department")
    monkeypatch.setattr(workforce, "assess_request", projection_failure)

    result = company_kernel._advance_hiring(request)
    assert calls == [request.id]
    db.session.refresh(request)
    assert result["status"] == "HIRING_SYSTEM_RECOVERY_REQUIRED"
    assert request.status == "SYSTEM_RECOVERY"
    assert AgentRun.query.filter_by(purpose="HR_ASSESSMENT").filter(AgentRun.id > paid.id).count() == 0



def test_current_research_perspective_recovery_stays_inside_dedicated_provider_family(ctx, monkeypatch):
    """Claude/Gemini Researcher recovery may upgrade models but never become live-web cross-provider routing."""
    from types import SimpleNamespace
    from eason_one.services import work_execution

    _install_research_department_provider_configs()
    operation, _project, _researcher, _critic = multi._setup()
    claude = Employee.query.filter_by(slug="claude-researcher").one()
    for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "PERPLEXITY_API_KEY"):
        monkeypatch.setenv(key, "test-key")
    first = ModelConfig(
        label="Claude perspective recovery first", provider_key="anthropic", model_name="claude-perspective-a",
        input_price_per_million=Decimal("1"), output_price_per_million=Decimal("2"),
        request_price_per_call=Decimal("0"), currency="TWD", max_output_tokens=2048,
        active=True, archived=False,
    )
    second = ModelConfig(
        label="Claude perspective recovery second", provider_key="anthropic", model_name="claude-perspective-b",
        input_price_per_million=Decimal("1.1"), output_price_per_million=Decimal("2.1"),
        request_price_per_call=Decimal("0"), currency="TWD", max_output_tokens=2048,
        active=True, archived=False,
    )
    db.session.add_all([first, second]); db.session.flush()
    claude.current_model = first
    db.session.commit()

    task = SimpleNamespace(assigned_employee=claude)
    work = SimpleNamespace(operation=operation)
    monkeypatch.setattr(
        "eason_one.services.team_formation.infer_primary_capability",
        lambda _work: ("RESEARCH", 1.0),
    )
    failed = SimpleNamespace(
        id=0, retry_of_run_id=None, model_config_id=first.id, model_config=first,
        outcome="FAILED_KNOWN", failure_reason="PROVIDER_REQUEST_REJECTED", attempt_number=1,
        provider_key_snapshot="anthropic", model_name_snapshot=first.model_name,
        context_composition_json={"execution_mode": "MODEL_RESEARCH_PERSPECTIVE"},
    )
    selected = work_execution.automatic_recovery_model(work, task, failed)
    assert selected is not None
    assert selected.id != first.id
    assert selected.provider_key == "anthropic"
    assert selected.provider_key != "mock"


def test_current_hr_system_recovery_reopens_only_when_new_lawful_model_exists(ctx, monkeypatch):
    """A historical known provider fault may self-resume after configuration exposes an untried lawful path."""
    from eason_one.services import workforce

    operation, project, _researcher, _critic = multi._setup()
    hr = Employee.query.filter_by(slug="hr-director").one()
    for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "PERPLEXITY_API_KEY"):
        monkeypatch.setenv(key, "test-key")
    primary = ModelConfig(
        label="HR recovery dead provider", provider_key="openai", model_name="hr-recovery-dead",
        input_price_per_million=Decimal("1"), output_price_per_million=Decimal("2"),
        request_price_per_call=Decimal("0"), currency="TWD", max_output_tokens=2048,
        active=True, archived=False,
    )
    db.session.add(primary); db.session.flush()
    hr.current_model = primary
    request = workforce.request_hire(
        requested_by_type="FOUNDER", role_needed="Recovery specialist",
        problem="Need bounded staffing truth.", why_now="Project is waiting.",
        responsibilities=["Bounded work"], capabilities=["ANALYSIS"], urgency="MEDIUM",
        use_frequency="PROJECT_NEED", operation=operation, project=project,
    )
    failed = AgentRun(
        employee_id=hr.id, project_id=project.id, operation_id=operation.id,
        model_config_id=primary.id, purpose="HR_ASSESSMENT", user_request=f"Assess HiringRequest #{request.id}",
        system_prompt_snapshot="hr", context_snapshot="context",
        context_composition_json={
            "hiring_request_id": request.id,
            "project_execution_terms_hash": project_contract.execution_terms_hash(project),
        },
        status="FAILED", outcome="FAILED_KNOWN", failure_reason="PROVIDER_REQUEST_REJECTED",
        failure_stage="POST_DISPATCH", error_text="HTTP 400 provider rejected request",
        provider_key_snapshot="openai", model_name_snapshot=primary.model_name,
        input_price_snapshot=primary.input_price_per_million, output_price_snapshot=primary.output_price_per_million,
        request_price_snapshot=primary.request_price_per_call, currency_snapshot="TWD", currency="TWD",
        real_cost=Decimal("0"), finished_at=now(), attempt_number=1,
    )
    db.session.add(failed); request.status = "SYSTEM_RECOVERY"; db.session.commit()

    # Exclude all other seeded real models first so the recovery remains blocked.
    seeded = ModelConfig.query.filter(ModelConfig.id != primary.id).all()
    prior_active = {row.id: row.active for row in seeded}
    for row in seeded:
        if row.provider_key in {"openai", "anthropic", "gemini", "perplexity"}:
            row.active = False
    db.session.commit()
    blocked = workforce.assessment_retry_state(request, current=now())
    assert blocked["state"] == "SYSTEM_RECOVERY"

    alternate = ModelConfig(
        label="HR recovery new lawful provider", provider_key="gemini", model_name="hr-recovery-gemini",
        input_price_per_million=Decimal("1"), output_price_per_million=Decimal("2"),
        request_price_per_call=Decimal("0"), currency="TWD", max_output_tokens=2048,
        active=True, archived=False,
    )
    db.session.add(alternate); db.session.commit()
    resumed = workforce.assessment_retry_state(request, current=now())
    assert resumed["state"] == "READY"
    assert resumed["recovery_model_id"] == alternate.id

    for row in seeded:
        row.active = prior_active[row.id]
    db.session.commit()


def test_current_internal_recovery_never_becomes_timerless_limbo(ctx):
    """Three retries for one exact failure may back off; the fourth becomes scoped SYSTEM_RECOVERY."""
    operation, project, _researcher, _critic = multi._setup()
    management = work_runtime.ensure_management_work(operation)
    reason = "same bounded local fault"

    for _attempt in range(3):
        assert operations.pause_for_internal_runtime_recovery(
            operation, reason, work=management,
            condition_type="INTERNAL_RECOVERY",
        )
        gates = work_runtime.open_gates(management)
        current = next(row for row in gates if row.get("condition_type") == "INTERNAL_RECOVERY")
        assert current.get("retry_after") is not None
        assert str(current.get("issue_code") or "").startswith("INTERNAL_RECOVERY:")
        assert work_runtime.resolve_waits(
            management, "INTERNAL_RECOVERY", issue_code=current.get("issue_code"),
            note="simulate bounded retry becoming due",
        ) == 1

    assert operations.pause_for_internal_runtime_recovery(
        operation, reason, work=management,
        condition_type="INTERNAL_RECOVERY",
    )
    gates = work_runtime.open_gates(management)
    exhausted = next(row for row in gates if row.get("condition_type") == "SYSTEM_RECOVERY")
    assert str(exhausted.get("issue_code") or "").startswith("INTERNAL_RECOVERY_EXHAUSTED:")
    assert exhausted.get("retry_after") is None
    assert "system recovery" in project.current_state_summary.casefold()


def test_current_unrelated_internal_faults_do_not_share_retry_budget(ctx):
    """A Work's historical unrelated faults cannot make a new failure look exhausted."""
    operation, _project, _researcher, _critic = multi._setup()
    management = work_runtime.ensure_management_work(operation)
    issue_codes = []
    for index in range(4):
        assert operations.pause_for_internal_runtime_recovery(
            operation, f"distinct local fault {index}", work=management,
            condition_type="INTERNAL_RECOVERY",
        )
        gates = work_runtime.open_gates(management)
        current = next(row for row in gates if row.get("condition_type") == "INTERNAL_RECOVERY")
        issue = str(current.get("issue_code") or "")
        assert issue.startswith("INTERNAL_RECOVERY:")
        issue_codes.append(issue)
        assert not any(row.get("condition_type") == "SYSTEM_RECOVERY" for row in gates)
        assert work_runtime.resolve_waits(
            management, "INTERNAL_RECOVERY", issue_code=issue,
            note="distinct failure recovered",
        ) == 1
    assert len(set(issue_codes)) == 4


def test_current_internal_wait_timer_resolves_only_the_due_scoped_issue(ctx):
    """A timer firing for recovery A must not erase unrelated recovery B on the same Work."""
    from eason_one.services import runtime_recovery

    operation, _project, _researcher, _critic = multi._setup()
    management = work_runtime.ensure_management_work(operation)
    work_runtime.open_wait(
        management, "INTERNAL_RECOVERY", "fault A", retry_after=now(),
        issue_code="INTERNAL_RECOVERY:test-a",
    )
    work_runtime.open_wait(
        management, "INTERNAL_RECOVERY", "fault B", retry_after=now(),
        issue_code="INTERNAL_RECOVERY:test-b",
    )
    db.session.commit()

    assert runtime_recovery.resolve_internal_waits() == 1
    remaining = [
        row for row in work_runtime.open_gates(management)
        if row.get("condition_type") == "INTERNAL_RECOVERY"
    ]
    assert len(remaining) == 1
    assert remaining[0].get("issue_code") in {"INTERNAL_RECOVERY:test-a", "INTERNAL_RECOVERY:test-b"}


def test_current_task_runner_cannot_bypass_open_work_gate_or_claim_step(ctx):
    """Compatibility task execution must not erase Work-owned recovery/governance truth."""
    from eason_one.models import OperationStep

    operation, _project, _researcher, _critic = multi._setup()
    work = sorted(
        [row for row in operation.works if row.work_type != "MANAGEMENT"],
        key=lambda row: row.id,
    )[0]
    task = work_runtime.task_for_work(work)
    work_runtime.open_wait(
        work, "RECONCILIATION", "durable external-effect truth is unresolved",
        issue_code="TEST_RECONCILIATION_GATE",
    )
    db.session.commit()
    before_steps = OperationStep.query.filter_by(operation_id=operation.id).count()
    before_runs = AgentRun.query.filter_by(work_id=work.id).count()

    with pytest.raises(ValueError, match="WORK_WAIT_GATE_STILL_OPEN"):
        operations._run_task_step(operation, "blocked-work-must-not-claim", task)

    assert OperationStep.query.filter_by(operation_id=operation.id).count() == before_steps
    assert AgentRun.query.filter_by(work_id=work.id).count() == before_runs
    assert any(
        row.get("condition_type") == "RECONCILIATION"
        and row.get("issue_code") == "TEST_RECONCILIATION_GATE"
        for row in work_runtime.open_gates(work)
    )


def test_current_repeated_kernel_exception_stops_in_system_recovery_without_abandoning_work(ctx, monkeypatch):
    """Repeated Company-code faults stop paid/business progression but preserve the Work for repair."""
    from eason_one.services import company_kernel, work_execution

    operation, _project, _researcher, _critic = multi._setup()
    work = sorted(
        [row for row in operation.works if row.work_type != "MANAGEMENT"],
        key=lambda row: row.id,
    )[0]

    monkeypatch.setattr(
        work_execution, "execute_work",
        lambda _work: (_ for _ in ()).throw(RuntimeError("deterministic kernel fault")),
    )

    for _attempt in range(3):
        result = company_kernel._dispatch_work(work)
        assert result["status"] == "RETRY_SCHEDULED"
        db.session.expire_all()
        work = db.session.get(Work, work.id)
        gate = next(
            row for row in work_runtime.open_gates(work)
            if row.get("condition_type") == "INTERNAL_RECOVERY"
        )
        assert str(gate.get("issue_code") or "").startswith("WORK_RUNTIME_EXCEPTION:")
        assert work_runtime.resolve_waits(
            work, "INTERNAL_RECOVERY", issue_code=gate.get("issue_code"),
            note="simulate repaired retry window",
        ) == 1
        db.session.commit()

    result = company_kernel._dispatch_work(work)
    db.session.expire_all()
    work = db.session.get(Work, work.id)
    assert result["status"] == "SYSTEM_RECOVERY_REQUIRED"
    assert work.state != "ABANDONED"
    gate = next(
        row for row in work_runtime.open_gates(work)
        if row.get("condition_type") == "SYSTEM_RECOVERY"
    )
    assert str(gate.get("issue_code") or "").startswith("WORK_RUNTIME_EXCEPTION_EXHAUSTED:")
    event = CompanyEvent.query.filter_by(
        event_type="WORK_RUNTIME_EXCEPTION", work_id=work.id,
    ).order_by(CompanyEvent.id.desc()).first()
    assert event is not None
    assert event.payload_json["attempt"] == 4
    assert event.payload_json["work_abandoned"] is False


def test_current_project_management_system_recovery_reopens_without_provider_call(ctx, monkeypatch):
    """Configuration repair may reopen exact Project-management provider recovery, but maintenance never dispatches."""
    from eason_one.services import execution_policy, external_effects, runtime_recovery

    operation, project, _researcher, _critic = multi._setup()
    management = work_runtime.ensure_management_work(operation)
    ceo = Employee.query.filter_by(slug="ceo").one()
    fingerprint = "management-provider-recovery-fingerprint"
    failed = _v16_run(
        ceo, project, operation, management,
        purpose="CEO_PROJECT_CONTINUATION", status="FAILED", max_tokens=2400,
        failure_reason="PROVIDER_REQUEST_REJECTED",
    )
    failed.outcome = "FAILED_KNOWN"
    failed.context_composition_json = {
        **dict(failed.context_composition_json or {}),
        "continuation_evidence_hash": fingerprint,
    }
    candidate = ModelConfig(
        label="Project management recovery alternate",
        provider_key="anthropic", model_name="project-management-recovery-alternate",
        input_price_per_million=Decimal("1"), output_price_per_million=Decimal("2"),
        request_price_per_call=Decimal("0"), currency="TWD", max_output_tokens=4096,
        active=True, archived=False,
    )
    db.session.add(candidate); db.session.flush()
    issue = f"PROJECT_MANAGEMENT_PROVIDER_RECOVERY:CEO_PROJECT_CONTINUATION:{failed.id}"
    work_runtime.open_wait(
        management, "SYSTEM_RECOVERY", "provider configuration needs another lawful model",
        issue_code=issue,
    )
    project.status = "BLOCKED"
    db.session.commit()

    monkeypatch.setattr(external_effects, "retry_authorized", lambda _run: (True, "known safe failure"))
    monkeypatch.setattr(execution_policy, "select_retry_model", lambda *args, **kwargs: candidate)

    changed = runtime_recovery.resolve_safe_project_management_system_recovery()
    db.session.expire_all()
    management = db.session.get(Work, management.id)
    assert changed == 1
    assert not any(
        row.get("condition_type") == "SYSTEM_RECOVERY" and row.get("issue_code") == issue
        for row in work_runtime.open_gates(management)
    )
    event = CompanyEvent.query.filter_by(
        event_type="PROJECT_MANAGEMENT_SYSTEM_RECOVERY_AUTO_RESUMED",
        work_id=management.id,
    ).one()
    assert event.payload_json["provider_dispatched_by_recovery"] is False
    assert event.payload_json["selected_model_config_id"] == candidate.id



def test_current_project_management_recovery_reuses_older_paid_success_before_looking_for_new_model(ctx, monkeypatch):
    """A later safe failure cannot strand an older exact paid success behind SYSTEM_RECOVERY."""
    from eason_one.services import execution_policy, external_effects, runtime_recovery

    operation, project, _researcher, _critic = multi._setup()
    management = work_runtime.ensure_management_work(operation)
    ceo = Employee.query.filter_by(slug="ceo").one()
    fingerprint = "management-paid-success-recovery-fingerprint"
    terms_hash = project_contract.execution_terms_hash(project)

    success = _v16_run(
        ceo, project, operation, management,
        purpose="CEO_PROJECT_CONTINUATION", status="SUCCEEDED", max_tokens=2400,
    )
    success.context_composition_json = {
        **dict(success.context_composition_json or {}),
        "continuation_evidence_hash": fingerprint,
        "project_execution_terms_hash": terms_hash,
    }
    failed = _v16_run(
        ceo, project, operation, management,
        purpose="CEO_PROJECT_CONTINUATION", status="FAILED", max_tokens=2400,
        failure_reason="PROVIDER_REQUEST_REJECTED",
    )
    failed.outcome = "FAILED_KNOWN"
    failed.context_composition_json = {
        **dict(failed.context_composition_json or {}),
        "continuation_evidence_hash": fingerprint,
        "project_execution_terms_hash": terms_hash,
    }
    issue = f"PROJECT_MANAGEMENT_PROVIDER_RECOVERY:CEO_PROJECT_CONTINUATION:{failed.id}"
    work_runtime.open_wait(
        management, "SYSTEM_RECOVERY", "later duplicate failed after durable success",
        issue_code=issue,
    )
    project.status = "BLOCKED"
    db.session.commit()

    monkeypatch.setattr(
        external_effects, "retry_authorized",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("paid success recovery must not ask whether another replay is authorized")
        ),
    )
    monkeypatch.setattr(
        execution_policy, "select_retry_model",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("paid success recovery must not select or buy another model")
        ),
    )

    changed = runtime_recovery.resolve_safe_project_management_system_recovery()
    db.session.expire_all()
    management = db.session.get(Work, management.id)
    assert changed == 1
    assert not any(
        row.get("condition_type") == "SYSTEM_RECOVERY" and row.get("issue_code") == issue
        for row in work_runtime.open_gates(management)
    )
    event = CompanyEvent.query.filter_by(
        event_type="PROJECT_MANAGEMENT_PAID_SUCCESS_RECOVERY_RESOLVED",
        work_id=management.id,
    ).one()
    assert event.payload_json["reusable_success_run_id"] == success.id
    assert event.payload_json["provider_dispatched_by_recovery"] is False


def test_current_project_management_recovery_retires_exact_gate_after_founder_terms_change(ctx, monkeypatch):
    """A provider-recovery gate from old Founder execution terms cannot deadlock the amended Project."""
    from eason_one.services import execution_policy, external_effects, runtime_recovery

    operation, project, _researcher, _critic = multi._setup()
    management = work_runtime.ensure_management_work(operation)
    ceo = Employee.query.filter_by(slug="ceo").one()
    fingerprint = "stale-founder-terms-management-fingerprint"
    old_terms_hash = project_contract.execution_terms_hash(project)
    failed = _v16_run(
        ceo, project, operation, management,
        purpose="PROJECT_OUTCOME_REVIEW", status="FAILED", max_tokens=2400,
        failure_reason="PROVIDER_REQUEST_REJECTED",
    )
    failed.outcome = "FAILED_KNOWN"
    failed.context_composition_json = {
        **dict(failed.context_composition_json or {}),
        "project_outcome_input_hash": fingerprint,
        "project_execution_terms_hash": old_terms_hash,
    }
    issue = f"PROJECT_MANAGEMENT_PROVIDER_RECOVERY:PROJECT_OUTCOME_REVIEW:{failed.id}"
    work_runtime.open_wait(
        management, "SYSTEM_RECOVERY", "old terms provider recovery",
        issue_code=issue,
    )
    project.status = "BLOCKED"
    db.session.commit()

    project_contract.authorize_constraint_change(
        project,
        constraints=["Use only the amended Founder execution boundary."],
        reason="Founder amended execution terms while Project management was recovering.",
        origin_employee_id=project.owner_employee_id,
    )
    assert project_contract.execution_terms_hash(project) != old_terms_hash

    monkeypatch.setattr(
        external_effects, "retry_authorized",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("obsolete old-terms recovery must not authorize a replay")
        ),
    )
    monkeypatch.setattr(
        execution_policy, "select_retry_model",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("obsolete old-terms recovery must not select another model")
        ),
    )

    changed = runtime_recovery.resolve_safe_project_management_system_recovery()
    db.session.expire_all()
    management = db.session.get(Work, management.id)
    assert changed == 1
    assert not any(
        row.get("condition_type") == "SYSTEM_RECOVERY" and row.get("issue_code") == issue
        for row in work_runtime.open_gates(management)
    )
    event = CompanyEvent.query.filter_by(
        event_type="PROJECT_MANAGEMENT_STALE_RECOVERY_RETIRED",
        work_id=management.id,
    ).one()
    assert event.payload_json["reason"] == "PROJECT_EXECUTION_TERMS_CHANGED"
    assert event.payload_json["provider_dispatched_by_recovery"] is False


def test_current_project_outcome_review_recovery_retires_old_fingerprint_gate(ctx, monkeypatch):
    """Changed accepted evidence retires only the exact old review-recovery gate; no provider call is needed."""
    from eason_one.services import company_kernel, execution_policy, external_effects, runtime_recovery

    operation, project, _researcher, _critic = multi._setup()
    management = work_runtime.ensure_management_work(operation)
    ceo = Employee.query.filter_by(slug="ceo").one()
    old_fingerprint = "old-project-outcome-evidence-fingerprint"
    failed = _v16_run(
        ceo, project, operation, management,
        purpose="PROJECT_OUTCOME_REVIEW", status="FAILED", max_tokens=2400,
        failure_reason="PROVIDER_REQUEST_REJECTED",
    )
    failed.outcome = "FAILED_KNOWN"
    failed.context_composition_json = {
        **dict(failed.context_composition_json or {}),
        "project_outcome_input_hash": old_fingerprint,
        "project_execution_terms_hash": project_contract.execution_terms_hash(project),
    }
    issue = f"PROJECT_MANAGEMENT_PROVIDER_RECOVERY:PROJECT_OUTCOME_REVIEW:{failed.id}"
    work_runtime.open_wait(
        management, "SYSTEM_RECOVERY", "old Project evidence review recovery",
        issue_code=issue,
    )
    project.status = "BLOCKED"
    db.session.commit()

    monkeypatch.setattr(company_kernel, "_review_input_hash", lambda _project: "new-current-evidence-fingerprint")
    monkeypatch.setattr(
        external_effects, "retry_authorized",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("stale review fingerprint must not authorize a replay")
        ),
    )
    monkeypatch.setattr(
        execution_policy, "select_retry_model",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("stale review fingerprint must not select another model")
        ),
    )

    changed = runtime_recovery.resolve_safe_project_management_system_recovery()
    db.session.expire_all()
    management = db.session.get(Work, management.id)
    assert changed == 1
    assert not any(
        row.get("condition_type") == "SYSTEM_RECOVERY" and row.get("issue_code") == issue
        for row in work_runtime.open_gates(management)
    )
    event = CompanyEvent.query.filter_by(
        event_type="PROJECT_MANAGEMENT_STALE_RECOVERY_RETIRED",
        work_id=management.id,
    ).one()
    assert event.payload_json["reason"] == "PROJECT_OUTCOME_EVIDENCE_CHANGED"
    assert event.payload_json["current_fingerprint"] == "new-current-evidence-fingerprint"
    assert event.payload_json["provider_dispatched_by_recovery"] is False


def test_current_project_management_recovery_never_bypasses_three_attempt_cap(ctx, monkeypatch):
    """A newly configured model cannot silently grant a fourth paid Project-management attempt."""
    from eason_one.services import company_kernel, execution_policy, external_effects, runtime_recovery

    operation, project, _researcher, _critic = multi._setup()
    management = work_runtime.ensure_management_work(operation)
    ceo = Employee.query.filter_by(slug="ceo").one()
    fingerprint = "management-attempt-cap-fingerprint"
    runs = []
    for index in range(3):
        run = _v16_run(
            ceo, project, operation, management,
            purpose="PROJECT_OUTCOME_REVIEW", status="FAILED", max_tokens=2400,
            failure_reason="PROVIDER_REQUEST_REJECTED",
        )
        run.outcome = "FAILED_KNOWN"
        run.context_composition_json = {
            **dict(run.context_composition_json or {}),
            "project_outcome_input_hash": fingerprint,
        }
        runs.append(run)
    db.session.commit()
    latest = runs[-1]
    candidate = ModelConfig(
        label="Project outcome recovery alternate",
        provider_key="anthropic", model_name="project-outcome-recovery-alternate",
        input_price_per_million=Decimal("1"), output_price_per_million=Decimal("2"),
        request_price_per_call=Decimal("0"), currency="TWD", max_output_tokens=4096,
        active=True, archived=False,
    )
    db.session.add(candidate); db.session.flush()
    issue = f"PROJECT_MANAGEMENT_PROVIDER_RECOVERY:PROJECT_OUTCOME_REVIEW:{latest.id}"
    work_runtime.open_wait(management, "SYSTEM_RECOVERY", "attempt cap reached", issue_code=issue)
    db.session.commit()

    # Keep this test on the exact current review input. A deliberately stale
    # fingerprint is covered separately by
    # test_current_project_outcome_review_recovery_retires_old_fingerprint_gate
    # and is correctly retired without replay.
    monkeypatch.setattr(company_kernel, "_review_input_hash", lambda _project: fingerprint)
    monkeypatch.setattr(external_effects, "retry_authorized", lambda _run: (True, "known safe failure"))
    monkeypatch.setattr(execution_policy, "select_retry_model", lambda *args, **kwargs: candidate)

    assert runtime_recovery.resolve_safe_project_management_system_recovery() == 0
    assert any(
        row.get("condition_type") == "SYSTEM_RECOVERY" and row.get("issue_code") == issue
        for row in work_runtime.open_gates(management)
    )


def test_current_project_management_retry_candidate_hard_stops_at_three_attempts(ctx, monkeypatch):
    """Normal Company sequencing cannot discover a fourth model after the durable cap is exhausted."""
    from eason_one.services import company_kernel, execution_policy, external_effects

    operation, project, _researcher, _critic = multi._setup()
    management = work_runtime.ensure_management_work(operation)
    ceo = Employee.query.filter_by(slug="ceo").one()
    fingerprint = "management-candidate-hard-cap-fingerprint"
    runs = []
    for _index in range(3):
        run = _v16_run(
            ceo, project, operation, management,
            purpose="PROJECT_OUTCOME_REVIEW", status="FAILED", max_tokens=2400,
            failure_reason="PROVIDER_REQUEST_REJECTED",
        )
        run.outcome = "FAILED_KNOWN"
        run.context_composition_json = {
            **dict(run.context_composition_json or {}),
            "project_outcome_input_hash": fingerprint,
        }
        runs.append(run)
    db.session.commit()

    monkeypatch.setattr(external_effects, "retry_authorized", lambda _run: (True, "known safe failure"))
    monkeypatch.setattr(
        execution_policy, "select_retry_model",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("three-attempt hard cap must stop before selecting a fourth ModelConfig")
        ),
    )

    assert company_kernel._project_management_recovery_candidate(
        ceo, operation, runs[-1], purpose="PROJECT_OUTCOME_REVIEW",
        fingerprint_key="project_outcome_input_hash", fingerprint=fingerprint,
    ) is None


def test_current_project_management_retry_wiring_consumes_untried_model(ctx):
    import inspect
    from eason_one.services import company_kernel

    review = inspect.getsource(company_kernel._run_project_outcome_review)
    continuation = inspect.getsource(company_kernel._plan_continuation)
    for source in (review, continuation):
        assert "_project_management_recovery_candidate" in source
        assert "model_override=resume_model" in source
        assert "DIFFERENT_GOVERNED_UNTRIED_MODEL" in source


def test_current_host_proof_recovery_never_clears_unrelated_system_gate(ctx, monkeypatch):
    """A repaired host-proof protocol may not resolve provider/internal recovery truth on the same management Work."""
    from eason_one.services import company_kernel

    operation, project, _researcher, _critic = multi._setup()
    management = work_runtime.ensure_management_work(operation)
    signature = "host-proof-scope-signature"
    memory = dict(operation.memory_json or {})
    memory["continuation_failure_signature"] = signature
    operation.memory_json = memory
    wrong_issue = "PROJECT_MANAGEMENT_PROVIDER_RECOVERY:PROJECT_OUTCOME_REVIEW:999999"
    work_runtime.open_wait(
        management,
        "SYSTEM_RECOVERY",
        "A Project management provider path is unavailable.",
        issue_code=wrong_issue,
    )
    db.session.commit()

    monkeypatch.setattr(
        company_kernel,
        "_latest_rejected_delivery_with_host_proof",
        lambda _project: {
            "work_id": 123,
            "artifact_version_id": 456,
            "host_verification_id": 789,
            "review_verification_id": 790,
        },
    )

    assert company_kernel._reconcile_repaired_system_recovery(project, operation, management) is False
    assert any(
        row.get("condition_type") == "SYSTEM_RECOVERY" and row.get("issue_code") == wrong_issue
        for row in work_runtime.open_gates(management)
    )
    assert not (management.runtime_control_json or {}).get("system_recovery_protocol_override")


def test_current_host_proof_recovery_resolves_only_its_scoped_gate(ctx, monkeypatch):
    """The repeated-failure host-proof protocol owns one exact SYSTEM_RECOVERY issue code."""
    from eason_one.services import company_kernel

    operation, project, _researcher, _critic = multi._setup()
    management = work_runtime.ensure_management_work(operation)
    signature = "host-proof-exact-signature"
    memory = dict(operation.memory_json or {})
    memory["continuation_failure_signature"] = signature
    operation.memory_json = memory
    issue = company_kernel._system_recovery_protocol_issue_code(signature)
    work_runtime.open_wait(
        management,
        "SYSTEM_RECOVERY",
        "The same bounded Project failure repeated twice without new accepted evidence. Company Kernel stopped the loop instead of spending more Founder budget.",
        issue_code=issue,
    )
    db.session.commit()

    monkeypatch.setattr(
        company_kernel,
        "_latest_rejected_delivery_with_host_proof",
        lambda _project: {
            "work_id": 123,
            "artifact_version_id": 456,
            "host_verification_id": 789,
            "review_verification_id": 790,
        },
    )

    assert company_kernel._reconcile_repaired_system_recovery(project, operation, management) is True
    assert not any(
        row.get("condition_type") == "SYSTEM_RECOVERY" and row.get("issue_code") == issue
        for row in work_runtime.open_gates(management)
    )
    override = dict((management.runtime_control_json or {}).get("system_recovery_protocol_override") or {})
    assert override.get("protocol") == "HOST_PROOF_REVIEW_HANDOFF_V2"
    assert override.get("failure_signature") == signature
    assert override.get("consumed") is False


def test_current_repaired_adoption_never_clears_unrelated_reconciliation(ctx):
    """A successful startup adoption may retire only its own historical adoption gate."""
    from eason_one.services import company_kernel

    operation, _project, _researcher, _critic = multi._setup()
    management = work_runtime.ensure_management_work(operation)
    unrelated = "PROJECT_RESULT_RECONCILIATION:OTHER_TRUTH"
    work_runtime.open_wait(
        management,
        "RECONCILIATION",
        "Independent Project result truth still needs reconciliation.",
        issue_code=unrelated,
    )
    db.session.commit()

    assert company_kernel._resolve_repaired_adoption_gate(operation) is False
    assert any(
        row.get("condition_type") == "RECONCILIATION" and row.get("issue_code") == unrelated
        for row in work_runtime.open_gates(management)
    )


def test_current_repaired_adoption_resolves_exact_operation_gate(ctx):
    """Once the same approved Operation adopts cleanly, its scoped startup gate no longer blocks the Project."""
    from eason_one.services import company_kernel

    operation, project, _researcher, _critic = multi._setup()
    management = work_runtime.ensure_management_work(operation)
    issue = company_kernel._operation_adoption_issue_code(operation)
    work_runtime.open_wait(
        management,
        "RECONCILIATION",
        "Company Kernel adoption failed closed: temporary deterministic fixture fault",
        issue_code=issue,
    )
    project.status = "BLOCKED"
    db.session.commit()

    assert company_kernel._resolve_repaired_adoption_gate(operation) is True
    assert not any(
        row.get("condition_type") == "RECONCILIATION" and row.get("issue_code") == issue
        for row in work_runtime.open_gates(management)
    )
    event = CompanyEvent.query.filter_by(
        event_type="PROJECT_ADOPTION_RECOVERY_RESOLVED",
        project_id=project.id,
    ).order_by(CompanyEvent.id.desc()).first()
    assert event is not None
    assert event.payload_json["operation_id"] == operation.id
    assert event.payload_json["provider_call"] is False


def test_current_continuation_approval_recovery_resolver_never_approves_in_maintenance(ctx, monkeypatch):
    """Maintenance may retire the exact readiness gate but normal Company sequencing owns delegated approval."""
    from eason_one.services import runtime_recovery

    operation, project, _researcher, _critic = multi._setup()
    management = work_runtime.ensure_management_work(operation)
    issue = f"CONTINUATION_APPROVAL_RECOVERY:{operation.id}"
    work_runtime.open_wait(
        management, "SYSTEM_RECOVERY",
        "ENGINEERING_TOOL_UNAVAILABLE: temporary local readiness fault",
        issue_code=issue,
    )
    project.status = "BLOCKED"
    db.session.commit()

    # An already-approved Operation models the race where normal sequencing
    # finished between maintenance scans. Recovery must only retire its stale gate.
    changed = runtime_recovery.resolve_safe_continuation_approval_system_recovery()
    db.session.expire_all()
    management = db.session.get(Work, management.id)
    operation = db.session.get(type(operation), operation.id)
    assert changed == 1
    assert operation.approved_at is not None
    assert not any(
        row.get("condition_type") == "SYSTEM_RECOVERY" and row.get("issue_code") == issue
        for row in work_runtime.open_gates(management)
    )
    event = CompanyEvent.query.filter_by(
        event_type="CONTINUATION_APPROVAL_SYSTEM_RECOVERY_AUTO_RESUMED",
        work_id=management.id,
    ).one()
    assert event.payload_json["operation_approved_by_recovery"] is False
    assert event.payload_json["provider_dispatched_by_recovery"] is False


def test_current_delegated_continuation_readiness_wiring_is_provider_free(ctx):
    """The recovery path probes readiness first and only the normal Project tick calls approve()."""
    import inspect
    from eason_one.services import company_kernel, operations, runtime_recovery

    probe = inspect.getsource(operations.delegated_approval_readiness)
    resolver = inspect.getsource(runtime_recovery.resolve_safe_continuation_approval_system_recovery)
    advance = inspect.getsource(company_kernel._advance_project)
    resume = inspect.getsource(company_kernel._resume_ready_delegated_continuation)
    plan = inspect.getsource(company_kernel._plan_continuation)

    assert "execution_budget_estimate" in probe
    assert "_preapproval_tool_readiness_snapshot" in probe
    assert "approve(" not in probe
    assert "delegated_approval_readiness" in resolver
    assert "operation_approved_by_recovery" in resolver
    assert "_resume_ready_delegated_continuation" in advance
    assert "operations_mod.approve" in resume
    assert "CONTINUATION_APPROVAL_RECOVERY" in plan or "_continuation_approval_issue_code" in plan


def test_current_project_management_retry_timer_resolves_only_exact_fingerprint(ctx):
    """One due Project-management retry may not retire another retry on the same management Work."""
    from eason_one.services import company_kernel

    operation, project, _researcher, _critic = multi._setup()
    management = work_runtime.ensure_management_work(operation)
    first = "review-fingerprint-a"
    second = "review-fingerprint-b"

    company_kernel._bounded_project_retry(
        project, operation, management,
        purpose="PROJECT_OUTCOME_REVIEW",
        fingerprint_key="project_outcome_input_hash",
        fingerprint=first,
        reason="first bounded local retry",
    )
    company_kernel._bounded_project_retry(
        project, operation, management,
        purpose="PROJECT_OUTCOME_REVIEW",
        fingerprint_key="project_outcome_input_hash",
        fingerprint=second,
        reason="second bounded local retry",
    )
    gates = [
        row for row in work_runtime.open_gates(management)
        if row.get("condition_type") == "INTERNAL_RECOVERY"
    ]
    first_issue = company_kernel._project_management_retry_issue_code("PROJECT_OUTCOME_REVIEW", first)
    second_issue = company_kernel._project_management_retry_issue_code("PROJECT_OUTCOME_REVIEW", second)
    assert first_issue != second_issue
    assert {row.get("issue_code") for row in gates} >= {first_issue, second_issue}

    assert work_runtime.resolve_waits(
        management, "INTERNAL_RECOVERY", issue_code=first_issue,
        note="Only the first exact management retry became due.",
    ) == 1
    remaining = work_runtime.open_gates(management)
    assert not any(row.get("issue_code") == first_issue for row in remaining)
    assert any(row.get("issue_code") == second_issue for row in remaining)


def test_current_restart_predispatch_retry_never_clears_unrelated_internal_gate(ctx):
    """Restart recovery owns one exact AgentRun gate instead of generic INTERNAL_RECOVERY."""
    from datetime import timedelta
    from eason_one.services import runtime_recovery

    operation, project, researcher, _critic = multi._setup()
    work = sorted([row for row in operation.works if row.work_type != "MANAGEMENT"], key=lambda row: row.id)[0]
    task = work_runtime.task_for_work(work)
    work.state = "EXECUTING"
    unrelated = "UNRELATED_INTERNAL_RECOVERY_FOR_ANOTHER_SUBSYSTEM"
    work_runtime.open_wait(
        work, "INTERNAL_RECOVERY", "Independent later recovery.",
        retry_after=now() + timedelta(hours=1), issue_code=unrelated,
    )
    # open_wait makes the Work WAITING; model the process having been in EXECUTING
    # when it died while retaining the independent persisted gate.
    work.state = "EXECUTING"
    run = AgentRun(
        employee_id=researcher.id, project_id=project.id, operation_id=operation.id,
        work_id=work.id, task_id=task.id, model_config_id=researcher.current_model.id,
        purpose="TASK_EXECUTION", user_request="restart predispatch regression",
        system_prompt_snapshot="system", context_snapshot="context",
        status="RUNNING", outcome=None,
        provider_key_snapshot=researcher.current_model.provider_key,
        model_name_snapshot=researcher.current_model.model_name,
        input_price_snapshot=researcher.current_model.input_price_per_million,
        output_price_snapshot=researcher.current_model.output_price_per_million,
        request_price_snapshot=researcher.current_model.request_price_per_call,
        currency_snapshot=researcher.current_model.currency,
        currency=researcher.current_model.currency,
    )
    db.session.add(run); db.session.commit()

    assert runtime_recovery.recover_stale_work() >= 1
    db.session.expire_all()
    work = db.session.get(Work, work.id)
    restart_issue = f"RESTART_RUN_{run.id}_PRE_DISPATCH_RETRY"
    assert any(row.get("issue_code") == restart_issue for row in work_runtime.open_gates(work))
    assert any(row.get("issue_code") == unrelated for row in work_runtime.open_gates(work))

    runtime_recovery.resolve_internal_waits()
    db.session.expire_all()
    work = db.session.get(Work, work.id)
    assert not any(row.get("issue_code") == restart_issue for row in work_runtime.open_gates(work))
    assert any(row.get("issue_code") == unrelated for row in work_runtime.open_gates(work))


def test_current_orchestration_reconciliation_retires_only_settled_exact_gate(ctx):
    """Known topology truth may reopen sequencing, but unrelated reconciliation remains blocked."""
    from eason_one.services import runtime_recovery

    operation, project, _researcher, _critic = multi._setup()
    management = work_runtime.ensure_management_work(operation)
    ceo = Employee.query.filter_by(slug="ceo").one()
    run = _v16_run(
        ceo, project, operation, management,
        purpose="ORCHESTRATION_PLAN", status="FAILED", max_tokens=2400,
        failure_reason="PROVIDER_REQUEST_REJECTED",
    )
    run.outcome = "FAILED_KNOWN"
    run.error_text = "HTTP 400 topology request rejected"
    issue = f"ORCHESTRATION_RUN_{run.id}_EFFECT_UNRESOLVED"
    unrelated = "PROJECT_RESULT_RECONCILIATION:INDEPENDENT"
    work_runtime.open_wait(management, "RECONCILIATION", "old unresolved topology effect", issue_code=issue)
    work_runtime.open_wait(management, "RECONCILIATION", "independent result truth", issue_code=unrelated)
    project.status = "BLOCKED"
    db.session.commit()

    assert runtime_recovery.resolve_settled_orchestration_reconciliation() == 1
    db.session.expire_all()
    management = db.session.get(Work, management.id)
    assert not any(row.get("issue_code") == issue for row in work_runtime.open_gates(management))
    assert any(row.get("issue_code") == unrelated for row in work_runtime.open_gates(management))
    event = CompanyEvent.query.filter_by(
        event_type="ORCHESTRATION_RECONCILIATION_RESOLVED", work_id=management.id,
    ).one()
    assert event.payload_json["run_id"] == run.id
    assert event.payload_json["provider_dispatched_by_recovery"] is False


def test_current_project_ceo_recovery_is_scoped_and_provider_free(ctx):
    """Restored CEO staffing reopens only its exact gate and buys no planning call."""
    from eason_one.services import runtime_recovery

    operation, project, _researcher, _critic = multi._setup()
    management = work_runtime.ensure_management_work(operation)
    ceo = Employee.query.filter_by(slug="ceo").one()
    issue = f"PROJECT_CEO_UNAVAILABLE:{project.id}"
    unrelated = "INDEPENDENT_SYSTEM_RECOVERY"
    work_runtime.open_wait(management, "SYSTEM_RECOVERY", "CEO unavailable", issue_code=issue)
    work_runtime.open_wait(management, "SYSTEM_RECOVERY", "other repair", issue_code=unrelated)
    project.status = "BLOCKED"
    db.session.commit()

    assert ceo.active is True
    assert runtime_recovery.resolve_safe_project_ceo_recovery() == 1
    db.session.expire_all()
    management = db.session.get(Work, management.id)
    assert not any(row.get("issue_code") == issue for row in work_runtime.open_gates(management))
    assert any(row.get("issue_code") == unrelated for row in work_runtime.open_gates(management))
    event = CompanyEvent.query.filter_by(
        event_type="PROJECT_CEO_RECOVERY_RESOLVED", work_id=management.id,
    ).one()
    assert event.payload_json["ceo_employee_id"] == ceo.id
    assert event.payload_json["provider_dispatched_by_recovery"] is False


def test_current_project_outcome_reviewer_recovery_is_scoped_and_provider_free(ctx, monkeypatch):
    """Staffing repair reopens only its exact reviewer gate; review itself stays in normal sequencing."""
    from eason_one.services import runtime_recovery
    import eason_one.services.project_outcome as project_outcome

    operation, project, researcher, _critic = multi._setup()
    management = work_runtime.ensure_management_work(operation)
    issue = f"PROJECT_OUTCOME_REVIEWER_UNAVAILABLE:{project.id}"
    unrelated = "INDEPENDENT_SYSTEM_RECOVERY"
    work_runtime.open_wait(management, "SYSTEM_RECOVERY", "reviewer unavailable", issue_code=issue)
    work_runtime.open_wait(management, "SYSTEM_RECOVERY", "other system repair", issue_code=unrelated)
    project.status = "BLOCKED"
    db.session.commit()

    monkeypatch.setattr(project_outcome, "select_outcome_reviewer", lambda _project, fallback_ceo=None: researcher)
    assert runtime_recovery.resolve_safe_project_outcome_reviewer_recovery() == 1
    db.session.expire_all()
    management = db.session.get(Work, management.id)
    assert not any(row.get("issue_code") == issue for row in work_runtime.open_gates(management))
    assert any(row.get("issue_code") == unrelated for row in work_runtime.open_gates(management))
    event = CompanyEvent.query.filter_by(
        event_type="PROJECT_OUTCOME_REVIEWER_RECOVERY_RESOLVED", work_id=management.id,
    ).one()
    assert event.payload_json["reviewer_employee_id"] == researcher.id
    assert event.payload_json["provider_dispatched_by_recovery"] is False


def test_current_project_management_reconciliation_retires_only_settled_exact_gate(ctx):
    """Settled management truth reopens sequencing without clearing unrelated reconciliation."""
    from eason_one.services import runtime_recovery

    operation, project, _researcher, _critic = multi._setup()
    management = work_runtime.ensure_management_work(operation)
    ceo = Employee.query.filter_by(slug="ceo").one()
    run = _v16_run(
        ceo, project, operation, management,
        purpose="PROJECT_OUTCOME_REVIEW", status="FAILED", max_tokens=2400,
        failure_reason="PROVIDER_REQUEST_REJECTED",
    )
    run.outcome = "FAILED_KNOWN"
    run.error_text = "definitive review provider rejection"
    issue = f"PROJECT_MANAGEMENT_RECONCILIATION:PROJECT_OUTCOME_REVIEW:{run.id}"
    unrelated = "PROJECT_RESULT_RECONCILIATION:INDEPENDENT"
    work_runtime.open_wait(management, "RECONCILIATION", "old unresolved review effect", issue_code=issue)
    work_runtime.open_wait(management, "RECONCILIATION", "independent result truth", issue_code=unrelated)
    project.status = "BLOCKED"
    db.session.commit()

    assert runtime_recovery.resolve_settled_project_management_reconciliation() == 1
    db.session.expire_all()
    management = db.session.get(Work, management.id)
    assert not any(row.get("issue_code") == issue for row in work_runtime.open_gates(management))
    assert any(row.get("issue_code") == unrelated for row in work_runtime.open_gates(management))
    event = CompanyEvent.query.filter_by(
        event_type="PROJECT_MANAGEMENT_RECONCILIATION_RESOLVED", work_id=management.id,
    ).one()
    assert event.payload_json["run_id"] == run.id
    assert event.payload_json["purpose"] == "PROJECT_OUTCOME_REVIEW"
    assert event.payload_json["provider_dispatched_by_recovery"] is False


def test_current_orchestration_known_failure_after_restart_uses_fallback_without_rebuy(ctx, monkeypatch):
    """A durable known topology failure projects the same conservative fallback instead of a second paid call."""
    from eason_one.services import company_kernel, multi_agent

    operation, project, _researcher, _critic = multi._setup()
    memory = dict(operation.memory_json or {})
    memory.pop("multi_agent_orchestration", None)
    memory["multi_agent_current_wave"] = None
    operation.memory_json = memory
    WorkDependency.query.filter(
        WorkDependency.work_id.in_([row.id for row in operation.works])
    ).delete(synchronize_session=False)
    management = work_runtime.ensure_management_work(operation)
    ceo = Employee.query.filter_by(slug="ceo").one()
    failed = _v16_run(
        ceo, project, operation, management,
        purpose="ORCHESTRATION_PLAN", status="FAILED", max_tokens=2400,
        failure_reason="PROVIDER_REQUEST_REJECTED",
    )
    failed.outcome = "FAILED_KNOWN"
    failed.error_text = "definitive topology provider rejection"
    db.session.commit()

    calls = []
    monkeypatch.setattr(
        multi_agent, "call_orchestrator",
        lambda *a, **k: calls.append((a, k)) or (_ for _ in ()).throw(
            AssertionError("known durable topology failure must not repurchase orchestration")
        ),
    )
    result = company_kernel._plan_work_topology(db.session.get(type(operation), operation.id))
    assert calls == []
    assert result["status"] == "TOPOLOGY_CONSERVATIVE_FALLBACK"
    assert result["source_failed_run_id"] == failed.id
    assert result["provider_replayed"] is False
    assert multi_agent.current_plan(operation) is not None


def test_current_project_result_proof_reconciliation_reopens_only_exact_gate(ctx, monkeypatch):
    """A repaired Result Ready proof read retires only its own reconciliation gate."""
    from eason_one.services import runtime_recovery
    import eason_one.services.project_outcome as project_outcome

    operation, project, _researcher, _critic = multi._setup()
    management = work_runtime.ensure_management_work(operation)
    issue = f"PROJECT_RESULT_PROOF_RECONCILIATION:{project.id}"
    unrelated = "PROJECT_RESULT_RECONCILIATION:INDEPENDENT"
    work_runtime.open_wait(management, "RECONCILIATION", "result proof read failed", issue_code=issue)
    work_runtime.open_wait(management, "RECONCILIATION", "unrelated evidence reconciliation", issue_code=unrelated)
    project.status = "BLOCKED"
    db.session.commit()

    monkeypatch.setattr(project_outcome, "result_ready_proof", lambda _project: True)
    assert runtime_recovery.resolve_project_result_proof_reconciliation() == 1
    db.session.expire_all()
    management = db.session.get(Work, management.id)
    project = db.session.get(type(project), project.id)
    assert not any(row.get("issue_code") == issue for row in work_runtime.open_gates(management))
    assert any(row.get("issue_code") == unrelated for row in work_runtime.open_gates(management))
    # The unrelated reconciliation remains authoritative, so resolving the Result proof
    # gate alone must not change the Project back to REVIEW/ACTIVE.
    assert project.status == "BLOCKED"
    event = CompanyEvent.query.filter_by(
        event_type="PROJECT_RESULT_PROOF_RECONCILIATION_RESOLVED", work_id=management.id,
    ).one()
    assert event.payload_json["proof_current"] is True
    assert event.payload_json["provider_dispatched_by_recovery"] is False


def test_current_project_outcome_evaluation_reconciliation_reopens_only_exact_gate(ctx, monkeypatch):
    """A repaired deterministic outcome evaluator does not clear unrelated reconciliation."""
    from eason_one.services import runtime_recovery
    import eason_one.services.project_outcome as project_outcome

    operation, project, _researcher, _critic = multi._setup()
    management = work_runtime.ensure_management_work(operation)
    issue = f"PROJECT_OUTCOME_EVALUATION_RECONCILIATION:{project.id}:{operation.id}"
    unrelated = "PROJECT_RESULT_RECONCILIATION:INDEPENDENT"
    work_runtime.open_wait(management, "RECONCILIATION", "outcome evaluator failed", issue_code=issue)
    work_runtime.open_wait(management, "RECONCILIATION", "independent evidence issue", issue_code=unrelated)
    project.status = "BLOCKED"
    db.session.commit()

    monkeypatch.setattr(project_outcome, "evaluate", lambda _project: {"overall_status": "NOT_SATISFIED"})
    assert runtime_recovery.resolve_project_outcome_evaluation_reconciliation() == 1
    db.session.expire_all()
    management = db.session.get(Work, management.id)
    assert not any(row.get("issue_code") == issue for row in work_runtime.open_gates(management))
    assert any(row.get("issue_code") == unrelated for row in work_runtime.open_gates(management))
    event = CompanyEvent.query.filter_by(
        event_type="PROJECT_OUTCOME_EVALUATION_RECONCILIATION_RESOLVED", work_id=management.id,
    ).one()
    assert event.payload_json["overall_status"] == "NOT_SATISFIED"
    assert event.payload_json["provider_dispatched_by_recovery"] is False


def test_current_final_closure_claimed_crash_window_is_resumable_without_provider_replay():
    """Final verification/report CLAIMED state is recognized only before provider boundary."""
    import inspect
    from types import SimpleNamespace
    from eason_one.services import operations

    safe = SimpleNamespace(status="CLAIMED", agent_run_id=None, provider_started_at=None)
    unsafe_run = SimpleNamespace(status="CLAIMED", agent_run_id=9, provider_started_at=None)
    unsafe_boundary = SimpleNamespace(status="CLAIMED", agent_run_id=None, provider_started_at=now())
    assert operations._claimed_step_is_safe_to_resume(safe) is True
    assert operations._claimed_step_is_safe_to_resume(unsafe_run) is False
    assert operations._claimed_step_is_safe_to_resume(unsafe_boundary) is False

    report_source = inspect.getsource(operations._report_step)
    verification_source = inspect.getsource(operations._goal_verification_step)
    next_source = inspect.getsource(operations.next_step)
    assert "_claimed_step_is_safe_to_resume" in report_source
    assert "_recover_open_step" in report_source
    assert "_claimed_step_is_safe_to_resume" in verification_source
    assert 'existing.kind == "GOAL_VERIFICATION"' in next_source
    assert 'existing.kind == "REPORT"' in next_source


def test_current_all_operationstep_pre_provider_claims_have_safe_restart_paths():
    """A local CLAIMED breadcrumb cannot permanently strand non-final OperationStep kinds."""
    import inspect
    from eason_one.services import operations

    handlers = {
        "TASK": operations._run_task_step,
        "REVIEW": operations._run_review_step,
        "MEETING_RESULT": operations._consume_meeting,
        "MEETING_STEP": operations._advance_meeting,
        "DECISION": operations._decision_step,
        "HR_ASSESSMENT": operations._hr_step,
        "GOAL_VERIFICATION": operations._goal_verification_step,
        "REPORT": operations._report_step,
        "ORCHESTRATION": operations._run_orchestration_step,
    }
    for kind, fn in handlers.items():
        source = inspect.getsource(fn)
        assert "_claimed_step_is_safe_to_resume" in source, kind

    next_source = inspect.getsource(operations.next_step)
    for kind in handlers:
        assert f'existing.kind == "{kind}"' in next_source or (
            kind in {"MEETING_RESULT", "MEETING_STEP"}
            and 'existing.kind in {"MEETING_RESULT", "MEETING_STEP"}' in next_source
        ), kind

    recovery_source = inspect.getsource(operations._recover_open_step)
    assert 'OPEN_STEP | {"CLAIMED"}' in recovery_source
    assert '"FAILED_PRE_PROVIDER_RECOVERED"' in recovery_source
    assert '"provider_replayed": False' in recovery_source


def test_current_meeting_result_claimed_recovery_is_idempotent_projection():
    """Resuming a deterministic Meeting-result claim replaces existing projection instead of duplicating it."""
    import inspect
    from eason_one.services import operations

    source = inspect.getsource(operations._consume_meeting)
    assert "existing_index = next" in source
    assert 'int(item.get("meeting_id") or 0) == int(meeting.id)' in source
    assert "outcomes[existing_index] = row" in source


def test_current_operationstep_recovery_and_reassignment_are_issue_scoped():
    """Legacy Operation recovery cannot let reassignment clear unrelated internal gates."""
    import inspect
    from eason_one.services import operations

    recover_source = inspect.getsource(operations._recover_open_step)
    task_source = inspect.getsource(operations._run_task_step)
    meeting_source = inspect.getsource(operations._handle_paid_meeting_failure)
    reassign_source = inspect.getsource(operations.reassign_task)

    assert "OPERATION_STEP_" in recover_source
    assert "_INTERNAL_RECOVERY" in recover_source
    assert "_EFFECT_RECONCILIATION" in recover_source
    assert "TASK_EXECUTION_RUN_" in task_source
    assert "MEETING_" in meeting_source and "_PAID_FAILURE_EXHAUSTED" in meeting_source
    assert "open_gates" in reassign_source
    assert 'issue.startswith("TASK_EXECUTION_RUN_")' in reassign_source
    assert 'issue.startswith("OPERATION_STEP_")' in reassign_source
    assert 'issue_code=issue' in reassign_source
    assert 'resolve_waits(\n            work, "INTERNAL_RECOVERY",\n            note=' not in reassign_source


def test_current_delegated_continuation_reconciliation_is_scoped_and_reprobed():
    """A repaired continuation invariant must wake the already-paid Mission without replanning."""
    import inspect
    from eason_one.services import company_kernel, runtime_recovery

    kernel_source = inspect.getsource(company_kernel)
    recovery_source = inspect.getsource(runtime_recovery.resolve_safe_continuation_approval_system_recovery)

    assert 'CONTINUATION_APPROVAL_RECONCILIATION:' in kernel_source
    assert '_continuation_approval_reconciliation_issue_code' in kernel_source
    assert '"SYSTEM_RECOVERY": "CONTINUATION_APPROVAL_RECOVERY:"' in recovery_source
    assert '"RECONCILIATION": "CONTINUATION_APPROVAL_RECONCILIATION:"' in recovery_source
    assert 'delegated_approval_readiness(operation)' in recovery_source
    assert 'work_runtime.resolve_waits(\n            work, current_condition, issue_code=issue' in recovery_source
    assert 'operation_approved_by_recovery": False' in recovery_source


def test_current_operationstep_initial_failure_waits_have_exact_owners():
    """First-observed OperationStep failure and restart recovery use the same scoped gate identity."""
    import inspect
    from eason_one.services import operations

    pause_source = inspect.getsource(operations.pause_for_internal_runtime_recovery)
    mark_source = inspect.getsource(operations._mark_paid_or_ambiguous)
    recovery_source = inspect.getsource(operations._recover_open_step)

    assert 'issue_code=None' in pause_source
    assert 'explicit_issue_code' in pause_source
    assert 'OPERATION_STEP_{int(step.id)}_RUN_{int(run.id)}_INTERNAL_RECOVERY' in mark_source
    assert 'OPERATION_STEP_{int(step.id)}_RUN_{int(run.id)}_EFFECT_RECONCILIATION' in mark_source
    assert 'OPERATION_STEP_{int(step.id)}_UNBOUND_EXECUTION_RECONCILIATION' in recovery_source
    assert 'MATERIALIZATION_RECONCILIATION' in recovery_source


def test_current_paid_execution_can_reproject_artifact_locally_after_verify_crash(ctx, monkeypatch):
    """VERIFYING may recover a durable paid execution without a second provider/tool call."""
    from eason_one.services import work_execution

    operation, project, researcher, critic = multi._setup()
    work = sorted(
        [row for row in operation.works if row.work_type != "MANAGEMENT"],
        key=lambda row: row.id,
    )[0]
    task = work_runtime.task_for_work(work)
    task.reviewer_employee_id = critic.id
    contract = acceptance_contract.build(
        title=work.title,
        objective=work.purpose,
        criteria=["The persisted recommendation is supported by its submitted evidence."],
        reviewer_employee_id=critic.id,
        owner_employee_id=researcher.id,
        project_execution_terms_hash=project_contract.execution_terms_hash(project),
    )
    control = dict(work.runtime_control_json or {})
    control["acceptance_contract"] = contract
    work.runtime_control_json = control
    work.acceptance_criteria = "The persisted recommendation is supported by its submitted evidence."
    task.acceptance_criteria = work.acceptance_criteria
    if work.state == "READY":
        work_runtime.transition(
            work, "EXECUTING", actor_type="EMPLOYEE", actor_id=researcher.id,
            reason="simulate paid execution before local Artifact materialization",
        )
    work_runtime.transition(work, "VERIFYING", actor_type="RUNTIME", reason="process died before Artifact projection")

    success = _v16_run(
        researcher, project, operation, work,
        purpose="TASK_EXECUTION", status="SUCCEEDED", max_tokens=1200,
    )
    success.raw_output = "persisted paid research result"
    success.parsed_output_json = {
        "result_summary": "persisted paid research result",
        "knowledge_proposals": [],
    }
    success.context_composition_json = {
        "provider_sources": [{"title": "Persisted source", "url": "https://example.test/materialize-only"}],
        "project_execution_terms_hash": project_contract.execution_terms_hash(project),
    }
    db.session.commit()
    assert ArtifactVersion.query.filter_by(execution_id=success.id).count() == 0

    calls = []
    def forbidden_second_execution(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("materialization recovery must not buy another TASK_EXECUTION")
    monkeypatch.setattr(work_execution, "run_task", forbidden_second_execution)

    result = work_execution.execute_work(db.session.get(Work, work.id), materialize_only=True)
    assert calls == []
    assert result["run_id"] == success.id
    assert AgentRun.query.filter_by(work_id=work.id, purpose="TASK_EXECUTION").count() == 1
    assert ArtifactVersion.query.filter_by(execution_id=success.id).count() == 1
    assert db.session.get(Work, work.id).state == "VERIFYING"


def test_current_frozen_reviewer_unavailable_recovers_without_reassignment_or_provider_call(ctx, monkeypatch):
    """A temporarily inactive frozen reviewer is Company recovery, not a permanent reconciliation."""
    from eason_one.services import artifacts as artifact_service
    from eason_one.services import runtime_recovery, work_execution

    operation, project, researcher, critic = multi._setup()
    work = sorted(
        [row for row in operation.works if row.work_type != "MANAGEMENT"],
        key=lambda row: row.id,
    )[0]
    task = work_runtime.task_for_work(work)
    task.reviewer_employee_id = critic.id
    contract = acceptance_contract.build(
        title=work.title,
        objective=work.purpose,
        criteria=["The persisted recommendation is supported by submitted evidence."],
        reviewer_employee_id=critic.id,
        owner_employee_id=researcher.id,
        project_execution_terms_hash=project_contract.execution_terms_hash(project),
    )
    control = dict(work.runtime_control_json or {})
    control["acceptance_contract"] = contract
    work.runtime_control_json = control
    work.acceptance_criteria = "The persisted recommendation is supported by submitted evidence."
    task.acceptance_criteria = work.acceptance_criteria
    if work.state == "READY":
        work_runtime.transition(work, "EXECUTING", actor_type="EMPLOYEE", actor_id=researcher.id, reason="produce artifact")
    work_runtime.transition(work, "VERIFYING", actor_type="RUNTIME", reason="await frozen reviewer")

    producer = _v16_run(researcher, project, operation, work, purpose="TASK_EXECUTION", status="SUCCEEDED", max_tokens=1200)
    producer.context_composition_json = {
        "provider_sources": [{"title": "Persisted source", "url": "https://example.test/reviewer-recovery"}],
        "project_execution_terms_hash": project_contract.execution_terms_hash(project),
    }
    artifact = Artifact(project_id=project.id, work_id=work.id, artifact_type="RESEARCH_REPORT", title="Reviewer recovery artifact")
    db.session.add(artifact); db.session.flush()
    content = "Persisted evidence for reviewer recovery."
    version = ArtifactVersion(
        artifact_id=artifact.id, version=1, producer_employee_id=researcher.id,
        execution_id=producer.id, status="SUBMITTED", content_text=content,
        content_hash=artifact_service._hash(content, None),
    )
    db.session.add(version)
    critic.active = False
    db.session.commit()

    calls = []
    monkeypatch.setattr(
        work_execution, "execute",
        lambda *args, **kwargs: calls.append((args, kwargs)) or (_ for _ in ()).throw(AssertionError("review provider must not run")),
    )
    result = work_execution.review_work(db.session.get(Work, work.id))
    assert result["status"] == "SYSTEM_RECOVERY_REQUIRED"
    assert calls == []
    issue = f"WORK_REVIEWER_UNAVAILABLE:{work.id}:{critic.id}"
    assert any(
        row.get("condition_type") == "SYSTEM_RECOVERY" and row.get("issue_code") == issue
        for row in work_runtime.open_gates(db.session.get(Work, work.id))
    )

    critic.active = True
    db.session.commit()
    assert runtime_recovery.resolve_repaired_work_integrity_waits() == 1
    assert not any(
        row.get("issue_code") == issue
        for row in work_runtime.open_gates(db.session.get(Work, work.id))
    )
    assert calls == []


def test_current_research_source_lineage_gate_retires_when_durable_lineage_is_repaired(ctx, monkeypatch):
    """Missing source lineage can wake from local durable truth without purchasing a review."""
    from eason_one.services import artifacts as artifact_service
    from eason_one.services import runtime_recovery, work_execution

    operation, project, researcher, critic = multi._setup()
    work = sorted(
        [row for row in operation.works if row.work_type != "MANAGEMENT"],
        key=lambda row: row.id,
    )[0]
    task = work_runtime.task_for_work(work)
    task.reviewer_employee_id = critic.id
    contract = acceptance_contract.build(
        title=work.title,
        objective=work.purpose,
        criteria=["Research claims are grounded in provider-observed sources."],
        reviewer_employee_id=critic.id,
        owner_employee_id=researcher.id,
        project_execution_terms_hash=project_contract.execution_terms_hash(project),
    )
    control = dict(work.runtime_control_json or {})
    control["acceptance_contract"] = contract
    work.runtime_control_json = control
    work.acceptance_criteria = "Research claims are grounded in provider-observed sources."
    task.acceptance_criteria = work.acceptance_criteria
    if work.state == "READY":
        work_runtime.transition(work, "EXECUTING", actor_type="EMPLOYEE", actor_id=researcher.id, reason="produce research")
    work_runtime.transition(work, "VERIFYING", actor_type="RUNTIME", reason="await semantic review")

    producer = _v16_run(researcher, project, operation, work, purpose="TASK_EXECUTION", status="SUCCEEDED", max_tokens=1200)
    producer.context_composition_json = {
        "project_execution_terms_hash": project_contract.execution_terms_hash(project),
        "provider_sources": [],
    }
    artifact = Artifact(project_id=project.id, work_id=work.id, artifact_type="RESEARCH_REPORT", title="Source lineage recovery artifact")
    db.session.add(artifact); db.session.flush()
    content = "Research result whose durable source lineage was temporarily missing."
    version = ArtifactVersion(
        artifact_id=artifact.id, version=1, producer_employee_id=researcher.id,
        execution_id=producer.id, status="SUBMITTED", content_text=content,
        content_hash=artifact_service._hash(content, None),
    )
    db.session.add(version); db.session.commit()

    calls = []
    monkeypatch.setattr(
        work_execution, "execute",
        lambda *args, **kwargs: calls.append((args, kwargs)) or (_ for _ in ()).throw(AssertionError("review must not run before lineage exists")),
    )
    result = work_execution.review_work(db.session.get(Work, work.id))
    assert result["status"] == "RECONCILIATION_REQUIRED"
    assert calls == []
    assert any(
        row.get("condition_type") == "RECONCILIATION" and row.get("issue_code") == "RESEARCH_ARTIFACT_SOURCE_LINEAGE_MISSING"
        for row in work_runtime.open_gates(db.session.get(Work, work.id))
    )

    producer.context_composition_json = {
        **dict(producer.context_composition_json or {}),
        "provider_sources": [{"title": "Recovered source", "url": "https://example.test/recovered-lineage"}],
    }
    db.session.commit()
    assert runtime_recovery.resolve_repaired_work_integrity_waits() == 1
    assert not any(
        row.get("issue_code") == "RESEARCH_ARTIFACT_SOURCE_LINEAGE_MISSING"
        for row in work_runtime.open_gates(db.session.get(Work, work.id))
    )
    assert calls == []


def test_current_paid_review_local_projection_is_idempotent_by_run_and_artifact():
    """Restart projection cannot duplicate review evidence and consume retry budget twice."""
    import inspect
    from eason_one.services import work_execution

    helper = inspect.getsource(work_execution._existing_review_projection)
    review = inspect.getsource(work_execution.review_work)
    assert 'method="INDEPENDENT_REVIEW"' in helper
    assert 'agent_run_id=run.id' in helper
    assert 'artifact_version_id=version.id' in helper
    assert 'acceptance_contract_hash' in helper
    assert 'if existing_message is None' in review
    assert 'if existing_verification is None' in review


def test_current_work_retry_budget_is_per_failure_signature_with_global_anomaly_ceiling():
    """Different Work failures do not steal one another's retries or create unlimited spend."""
    import inspect
    from types import SimpleNamespace
    from eason_one.services import work_execution

    a = SimpleNamespace(
        purpose="TASK_EXECUTION", outcome="FAILED_KNOWN",
        failure_reason="STRUCTURED_OUTPUT_INVALID", failure_stage="POSTPROCESS",
        provider_key_snapshot="openai", model_config_id=1,
    )
    same_other_model = SimpleNamespace(
        purpose="TASK_EXECUTION", outcome="FAILED_KNOWN",
        failure_reason="STRUCTURED_OUTPUT_INVALID", failure_stage="POSTPROCESS",
        provider_key_snapshot="anthropic", model_config_id=99,
    )
    different = SimpleNamespace(
        purpose="TASK_EXECUTION", outcome="FAILED_KNOWN",
        failure_reason="PROVIDER_REQUEST_REJECTED", failure_stage="POST_DISPATCH",
        provider_key_snapshot="openai", model_config_id=1,
    )
    assert work_execution._work_retry_failure_signature(a, "TASK_EXECUTION") == work_execution._work_retry_failure_signature(same_other_model, "TASK_EXECUTION")
    assert work_execution._work_retry_failure_signature(a, "TASK_EXECUTION") != work_execution._work_retry_failure_signature(different, "TASK_EXECUTION")

    source = inspect.getsource(work_execution._schedule_internal_retry)
    assert "same_failure_attempts" in source
    assert "overall_ceiling" in source
    assert "WORK_RECOVERY_ANOMALY_CEILING" in source
    assert "WORK_INTERNAL_RECOVERY" in inspect.getsource(work_execution._work_retry_issue_code)


def test_current_host_proof_retry_budget_is_artifact_contract_scoped():
    """A new ArtifactVersion never inherits an earlier Artifact's local proof retry debt."""
    import inspect
    from eason_one.services import work_execution

    first = {
        "artifact_version_id": 10,
        "artifact_content_hash": "a" * 64,
        "acceptance_contract_hash": "contract-1",
    }
    same = dict(first)
    next_version = {**first, "artifact_version_id": 11, "artifact_content_hash": "b" * 64}
    assert work_execution._host_proof_scope_key(first) == work_execution._host_proof_scope_key(same)
    assert work_execution._host_proof_scope_key(first) != work_execution._host_proof_scope_key(next_version)

    source = inspect.getsource(work_execution._schedule_host_proof_retry)
    assert 'prior.get("scope_key") == scope_key' in source
    assert 'control.pop("host_proof_retry_count", None)' in source
    assert "HOST_PROOF_RECONCILIATION" in source


def test_current_goal_verification_excludes_superseded_artifacts_and_stale_reviews():
    """Project completion evidence is bound to the current accepted ArtifactVersion only."""
    import inspect
    from eason_one.services import operations

    source = inspect.getsource(operations.goal_evidence_packet)
    assert "accepted_version_by_work" in source
    assert ".order_by(models.ArtifactVersion.id.desc())" in source
    assert ".first()" in source
    assert "versions[:3]" not in source
    assert 'target_version_id != int(version.id)' in source
    assert 'target_hash != str(version.content_hash or "")' in source
    assert "Historical reviews remain audit history" in source


def test_current_goal_completion_rejects_superseded_accepted_artifact(ctx):
    """A Work cannot prove Project completion with an older ACCEPTED version when a newer revision exists."""
    from eason_one.services import operations

    criterion = "Current delivery evidence must be the authoritative latest ArtifactVersion"
    project = core._project((criterion,))
    operation = core._operation(project)
    work = core._accepted_work(project, operation, criterion)
    accepted = (
        ArtifactVersion.query.join(Artifact)
        .filter(Artifact.work_id == work.id)
        .order_by(ArtifactVersion.id.desc()).one()
    )
    newer = ArtifactVersion(
        artifact_id=accepted.artifact_id,
        version=int(accepted.version or 1) + 1,
        producer_employee_id=accepted.producer_employee_id,
        execution_id=accepted.execution_id,
        status="SUBMITTED",
        content_text="newer revision still awaiting current acceptance",
        content_hash="f" * 64,
    )
    db.session.add(newer); db.session.commit()

    allowed, reasons = operations.completion_guard(operation)
    assert allowed is False
    assert any("newer non-accepted ArtifactVersion" in reason for reason in reasons)
    with pytest.raises(ValueError, match="current latest ACCEPTED ArtifactVersion"):
        operations.goal_evidence_packet(operation)


def test_current_goal_review_evidence_requires_exact_artifact_hash_and_contract():
    """Current completion authority rejects a review missing either Artifact content or frozen Contract identity."""
    import inspect
    from eason_one.services import operations

    source = inspect.getsource(operations.goal_evidence_packet)
    assert 'target_hash != str(version.content_hash or "")' in source
    assert 'target_contract_hash != current_contract_hash' in source
    assert 'acceptance_contract_hash_by_work' in source
    assert 'if work.state != "ACCEPTED"' in source


def test_current_orphaned_waiting_with_accepted_artifact_restores_accepted_not_ready(ctx):
    """Restart repair must never make already-accepted durable evidence executable again."""
    from eason_one.services import runtime_recovery

    criterion = "Accepted evidence must not be replayed after orphaned WAITING recovery"
    project = core._project((criterion,))
    operation = core._operation(project)
    work = core._accepted_work(project, operation, criterion)
    version = (
        ArtifactVersion.query.join(Artifact)
        .filter(Artifact.work_id == work.id)
        .order_by(ArtifactVersion.id.desc()).one()
    )
    assert version.status == "ACCEPTED"
    assert VerificationRecord.query.filter_by(
        work_id=work.id, artifact_version_id=version.id, status="PASSED"
    ).count() >= 1

    before_runs = AgentRun.query.filter_by(work_id=work.id).count()
    control = dict(work.runtime_control_json or {})
    control["gates"] = []
    work.runtime_control_json = control
    work.state = "WAITING"
    db.session.commit()

    repaired = runtime_recovery.repair_orphaned_waiting_work()
    assert repaired >= 1
    restored = db.session.get(Work, work.id)
    assert restored.state == "ACCEPTED"
    assert AgentRun.query.filter_by(work_id=work.id).count() == before_runs


def test_current_completion_guard_does_not_bypass_open_gate_on_accepted_work(ctx):
    """ACCEPTED state cannot override an unresolved durable governance/recovery gate."""
    from eason_one.services import operations

    criterion = "Accepted work must also have no unresolved durable gate"
    project = core._project((criterion,))
    operation = core._operation(project)
    work = core._accepted_work(project, operation, criterion)
    work_runtime.open_wait(
        work, "RECONCILIATION", "Simulated unresolved accepted-work integrity gate.",
        issue_code=f"TEST_ACCEPTED_GATE:{work.id}",
    )
    db.session.commit()

    allowed, reasons = operations.completion_guard(operation)
    assert allowed is False
    assert any("unresolved governance/recovery gate" in reason for reason in reasons)


def test_current_goal_review_selection_is_work_authoritative_not_task_authoritative():
    """vNext current review identity follows Work; compatibility Task IDs are metadata only."""
    import inspect
    from eason_one.services import operations

    source = inspect.getsource(operations.goal_evidence_packet)
    assert "if work_id not in latest_reviews" in source
    assert '"work_id": work_id' in source
    assert '"task_id": run.task_id' in source
    assert "if run.task_id not in latest_reviews" not in source



def test_current_project_retry_identity_changes_when_exact_accepted_artifact_changes(ctx):
    """New accepted evidence must not inherit old continuation/repeated-failure debt by Work id alone."""
    from eason_one.services import company_kernel

    criterion = "Current accepted Artifact identity must scope Project recovery"
    project = core._project((criterion,))
    operation = core._operation(project)
    work = core._accepted_work(project, operation, criterion)
    evaluation = {
        "accepted_work_ids": [work.id],
        "criteria": [{"criterion": criterion, "status": "INSUFFICIENT_EVIDENCE"}],
    }
    before = company_kernel._accepted_evidence_identity(project)
    sig_before = company_kernel._normalized_failure_signature(project, evaluation, "same deterministic failure")

    current = (
        ArtifactVersion.query.join(Artifact)
        .filter(Artifact.work_id == work.id)
        .order_by(ArtifactVersion.id.desc()).one()
    )
    replacement = ArtifactVersion(
        artifact_id=current.artifact_id, version=int(current.version or 1) + 1,
        producer_employee_id=current.producer_employee_id, execution_id=current.execution_id,
        status="ACCEPTED", content_text="new accepted durable evidence",
        content_hash="9" * 64,
    )
    db.session.add(replacement); db.session.commit()

    after = company_kernel._accepted_evidence_identity(project)
    sig_after = company_kernel._normalized_failure_signature(project, evaluation, "same deterministic failure")
    assert before != after
    assert before[0]["work_id"] == after[0]["work_id"] == work.id
    assert before[0]["artifact_version_id"] != after[0]["artifact_version_id"]
    assert sig_before != sig_after


def test_current_orphaned_waiting_repair_never_revives_work_from_old_project_terms(ctx):
    """Founder Project amendments make orphaned old Work stale; restart repair must cancel/replan, not revive it."""
    from eason_one.services import runtime_recovery

    criterion = "Delivery must remain inside current Founder Project terms"
    project = core._project((criterion,))
    operation = core._operation(project)
    work = core._accepted_work(project, operation, criterion)
    task = work_runtime.task_for_work(work)
    __import__(
        "eason_one.services.acceptance_contract", fromlist=["ensure_for_work"]
    ).ensure_for_work(work, task=task)
    db.session.commit()
    control = dict(work.runtime_control_json or {})
    assert dict(control.get("acceptance_contract") or {}).get("project_execution_terms_hash")

    project_contract.authorize_constraint_change(
        project,
        constraints=["Use only the newly authorized execution boundary."],
        reason="Founder changed the live Project execution terms after this Work was accepted.",
        origin_employee_id=project.owner_employee_id,
    )
    control = dict(work.runtime_control_json or {})
    control["gates"] = []
    work.runtime_control_json = control
    work.state = "WAITING"
    db.session.commit()

    repaired = runtime_recovery.repair_orphaned_waiting_work()
    assert repaired >= 1
    stale = db.session.get(Work, work.id)
    assert stale.state == "CANCELLED"
    assert stale.state != "READY"
    assert stale.state != "ACCEPTED"


def test_current_pending_delegated_continuation_is_superseded_when_accepted_evidence_changes(ctx):
    """A paid CEO plan may not be approved after its exact accepted-evidence basis changes."""
    from eason_one.services import company_kernel

    criterion = "Founder outcome still needs current accepted evidence."
    project = core._project((criterion,))
    prior = core._operation(project, status="FAILED")
    ceo = Employee.query.filter_by(slug="ceo").one()
    researcher = Employee.query.filter_by(slug="researcher").one()
    critic = Employee.query.filter_by(slug="critic").one()
    management = company_kernel._management_work(prior)

    evaluation = project_outcome.evaluate(project)
    evidence_hash = company_kernel._continuation_evidence_hash(
        project, prior, evaluation, failure_reason=None, failure_mode="GENERAL",
    )
    contract = project_contract.governing_terms(project)

    plan = deepcopy(multi._plan(researcher, critic))
    plan["project"] = None
    plan["project_id"] = project.id
    plan["operation"]["project_id"] = project.id
    plan["operation"]["title"] = "Pending delegated continuation for stale-evidence regression"
    pending = operations.propose_operation(
        ceo, plan, route_type=None, founder_request=None,
        authority_source="PROJECT_DELEGATED_CEO",
    )
    source = _v16_run(
        ceo, project, prior, management,
        purpose="CEO_PROJECT_CONTINUATION", status="SUCCEEDED", max_tokens=4096,
    )
    memory = dict(pending.memory_json or {})
    memory.update({
        "authority_source": "PROJECT_DELEGATED_CEO",
        "continuation_evidence_hash": evidence_hash,
        "continuation_source_run_id": source.id,
        "continuation_mode": "GENERAL",
        "project_contract_hash": contract.get("governing_contract_hash") or contract.get("contract_hash"),
    })
    pending.memory_json = memory
    db.session.commit()

    # New durable accepted evidence appears while the paid plan is still waiting
    # for approval/readiness. The old plan must become audit history, not current
    # execution authority.
    core._accepted_work(project, prior, criterion)
    db.session.commit()

    readiness = operations.delegated_approval_readiness(pending)
    assert readiness["ready"] is False
    assert readiness["kind"] == "STALE_CONTINUATION"
    assert "evidence changed" in readiness["reason"].casefold()

    result = company_kernel._resume_ready_delegated_continuation(project, pending)
    db.session.expire_all()
    pending = db.session.get(type(pending), pending.id)
    assert result["status"] == "CONTINUATION_STALE_PLAN_SUPERSEDED"
    assert pending.approved_at is None
    assert pending.status == "SUPERSEDED"
    assert pending.kernel_status == "SUPERSEDED"
    assert company_kernel._latest_operation(project).id == prior.id
    event = CompanyEvent.query.filter_by(
        event_type="PROJECT_CONTINUATION_STALE_PLAN_SUPERSEDED",
        project_id=project.id,
    ).order_by(CompanyEvent.id.desc()).first()
    assert event is not None
    assert event.payload_json["operation_id"] == pending.id
    assert event.payload_json["provider_call"] is False
    assert event.payload_json["operation_approved"] is False


def test_current_delegated_approve_has_independent_stale_plan_guard():
    """Direct approval cannot bypass continuation currentness even outside the normal recovery path."""
    import inspect
    from eason_one.services import company_kernel, operations, runtime_recovery

    readiness = inspect.getsource(operations.delegated_approval_readiness)
    approve_source = inspect.getsource(operations.approve)
    resume_source = inspect.getsource(company_kernel._resume_ready_delegated_continuation)
    latest_source = inspect.getsource(company_kernel._latest_operation)
    exists_source = inspect.getsource(company_kernel._continuation_exists)
    recovery_source = inspect.getsource(runtime_recovery.resolve_safe_continuation_approval_system_recovery)

    assert "_delegated_continuation_currentness" in readiness
    assert "_delegated_continuation_currentness" in approve_source
    assert 'kind == "STALE_CONTINUATION"' in resume_source
    assert 'Operation.status != "SUPERSEDED"' in latest_source
    assert 'operation.status or "").upper() == "SUPERSEDED"' in exists_source
    assert 'kind == "STALE_CONTINUATION"' in recovery_source
    assert "_supersede_stale_delegated_continuation" in recovery_source


def test_current_evidence_review_retry_requires_fresh_review_generation(ctx):
    """A bounded UNPROVEN re-review must not reuse the paid review that scheduled it.

    Crash recovery still reuses paid success within one generation (covered by
    test_current_paid_review_response_is_reused_after_crash_before_verification_persistence),
    but an explicit EVIDENCE_REVIEW_RETRY is a new semantic observation attempt
    for the same immutable ArtifactVersion/Contract and therefore needs a fresh
    review generation.
    """
    from eason_one.services import artifacts as artifact_service
    from eason_one.services import work_execution

    operation, project, researcher, critic = multi._setup()
    work = sorted(
        [row for row in operation.works if row.work_type != "MANAGEMENT"],
        key=lambda row: row.id,
    )[0]
    work.retry_limit = 1
    task = work_runtime.task_for_work(work)
    task.reviewer_employee_id = critic.id
    contract = acceptance_contract.build(
        title=work.title,
        objective=work.purpose,
        criteria=["Every semantic claim is traceable to persisted evidence."],
        reviewer_employee_id=critic.id,
        owner_employee_id=researcher.id,
        project_execution_terms_hash=project_contract.execution_terms_hash(project),
    )
    control = dict(work.runtime_control_json or {})
    control["acceptance_contract"] = contract
    work.runtime_control_json = control
    work.acceptance_criteria = "Every semantic claim is traceable to persisted evidence."
    task.acceptance_criteria = work.acceptance_criteria
    if work.state == "READY":
        work_runtime.transition(
            work, "EXECUTING", actor_type="EMPLOYEE", actor_id=researcher.id,
            reason="produce artifact for evidence review generation test",
        )
    work_runtime.transition(work, "VERIFYING", actor_type="RUNTIME", reason="await reviewer")

    producer = _v16_run(
        researcher, project, operation, work,
        purpose="TASK_EXECUTION", status="SUCCEEDED", max_tokens=1200,
    )
    producer.context_composition_json = {
        "provider_sources": [{"title": "Persisted source", "url": "https://example.test/evidence-retry"}],
        "project_execution_terms_hash": project_contract.execution_terms_hash(project),
    }
    artifact = Artifact(
        project_id=project.id, work_id=work.id,
        artifact_type="RESEARCH_REPORT", title="Evidence retry artifact",
    )
    db.session.add(artifact); db.session.flush()
    content = "Claim set requiring a bounded independent evidence re-review."
    version = ArtifactVersion(
        artifact_id=artifact.id, version=1, producer_employee_id=researcher.id,
        execution_id=producer.id, status="SUBMITTED", content_text=content,
        content_hash=artifact_service._hash(content, None),
    )
    db.session.add(version); db.session.flush()

    first_target = work_execution._review_target(version, contract, work)
    assert first_target["evidence_review_generation"] == 0
    first_review = _v16_run(
        critic, project, operation, work,
        purpose="TASK_REVIEW", status="SUCCEEDED", max_tokens=4096,
    )
    first_review.context_composition_json = {
        "review_target": first_target,
        "project_execution_terms_hash": project_contract.execution_terms_hash(project),
    }
    db.session.add(VerificationRecord(
        work_id=work.id,
        artifact_version_id=version.id,
        method="INDEPENDENT_REVIEW",
        status="UNPROVEN",
        verifier_employee_id=critic.id,
        agent_run_id=first_review.id,
        details_json={
            "acceptance_contract_hash": contract["contract_hash"],
            "artifact_content_hash": version.content_hash,
        },
    ))
    db.session.commit()

    status = work_execution._schedule_evidence_review_retry(
        work, first_review, version, contract,
        "Semantic evidence remains incomplete for this exact ArtifactVersion.",
    )
    db.session.commit()
    assert status == "EVIDENCE_REVIEW_RETRY_SCHEDULED"
    assert work.state == "WAITING"
    marker = dict((work.runtime_control_json or {}).get("evidence_review_retry") or {})
    assert marker["generation"] == 1
    assert marker["source_review_run_id"] == first_review.id

    gate = work_runtime.open_gates(work, "EVIDENCE_REVIEW_RETRY")[0]
    assert work_runtime.resolve_waits(
        work,
        "EVIDENCE_REVIEW_RETRY",
        issue_code=gate["issue_code"],
        note="Bounded independent evidence re-review is due.",
    ) == 1
    db.session.commit()
    assert work.state == "VERIFYING"

    retry_target = work_execution._review_target(version, contract, work)
    assert retry_target["evidence_review_generation"] == 1
    assert work_execution._review_run_matches_target(first_review, retry_target, work) is False
    assert work_execution._durable_review_for_target(work, retry_target) is None

    second_review = _v16_run(
        critic, project, operation, work,
        purpose="TASK_REVIEW", status="SUCCEEDED", max_tokens=4096,
    )
    second_review.context_composition_json = {
        "review_target": retry_target,
        "project_execution_terms_hash": project_contract.execution_terms_hash(project),
    }
    db.session.commit()
    assert work_execution._durable_review_for_target(work, retry_target).id == second_review.id


def test_current_s06_evidence_retry_gate_is_adopted_as_fresh_review_generation(ctx):
    """An in-flight S06 WAITING/VERIFYING evidence retry must escape the old reuse loop after S07."""
    from eason_one.services import artifacts as artifact_service
    from eason_one.services import work_execution

    operation, project, researcher, critic = multi._setup()
    work = sorted(
        [row for row in operation.works if row.work_type != "MANAGEMENT"],
        key=lambda row: row.id,
    )[0]
    work.retry_limit = 1
    task = work_runtime.task_for_work(work)
    task.reviewer_employee_id = critic.id
    contract = acceptance_contract.build(
        title=work.title,
        objective=work.purpose,
        criteria=["Every semantic claim is traceable to persisted evidence."],
        reviewer_employee_id=critic.id,
        owner_employee_id=researcher.id,
        project_execution_terms_hash=project_contract.execution_terms_hash(project),
    )
    control = dict(work.runtime_control_json or {})
    control["acceptance_contract"] = contract
    work.runtime_control_json = control
    if work.state == "READY":
        work_runtime.transition(work, "EXECUTING", actor_type="EMPLOYEE", actor_id=researcher.id, reason="produce")
    work_runtime.transition(work, "VERIFYING", actor_type="RUNTIME", reason="review")

    producer = _v16_run(researcher, project, operation, work, purpose="TASK_EXECUTION", status="SUCCEEDED", max_tokens=1200)
    producer.context_composition_json = {
        "provider_sources": [{"title": "Persisted source", "url": "https://example.test/s06-adoption"}],
        "project_execution_terms_hash": project_contract.execution_terms_hash(project),
    }
    artifact = Artifact(project_id=project.id, work_id=work.id, artifact_type="RESEARCH_REPORT", title="S06 adoption")
    db.session.add(artifact); db.session.flush()
    content = "Persisted S06 artifact awaiting a bounded semantic re-review."
    version = ArtifactVersion(
        artifact_id=artifact.id, version=1, producer_employee_id=researcher.id,
        execution_id=producer.id, status="SUBMITTED", content_text=content,
        content_hash=artifact_service._hash(content, None),
    )
    db.session.add(version); db.session.flush()

    old_review = _v16_run(critic, project, operation, work, purpose="TASK_REVIEW", status="SUCCEEDED", max_tokens=4096)
    old_review.context_composition_json = {
        "review_target": {
            "artifact_version_id": version.id,
            "artifact_content_hash": version.content_hash,
            "acceptance_contract_hash": contract["contract_hash"],
        },
        "project_execution_terms_hash": project_contract.execution_terms_hash(project),
    }
    db.session.add(VerificationRecord(
        work_id=work.id, artifact_version_id=version.id, method="INDEPENDENT_REVIEW",
        status="UNPROVEN", verifier_employee_id=critic.id, agent_run_id=old_review.id,
        details_json={"acceptance_contract_hash": contract["contract_hash"]},
    ))
    db.session.flush()

    # Exact durable shape left by S06: retry gate existed, but no
    # evidence_review_retry generation marker was persisted.
    control = dict(work.runtime_control_json or {})
    gates = list(control.get("gates") or [])
    gates.append({
        "id": f"work:{work.id}:gate:legacy-s06",
        "condition_type": "EVIDENCE_REVIEW_RETRY",
        "state": "RESOLVED",
        "reason": "Semantic evidence remains incomplete.",
        "issue_code": (
            f"EVIDENCE_REVIEW_RETRY:{work.id}:{version.id}:"
            f"{contract['contract_hash'][:24]}"
        ),
        "resume_state": "VERIFYING",
        "owner": "RUNTIME_EVIDENCE_REVIEW",
        "exit_policy": "retry_after or evidence-review exhaustion",
    })
    control["gates"] = gates
    control.pop("evidence_review_retry", None)
    work.runtime_control_json = control
    db.session.commit()

    adopted_target = work_execution._review_target(version, contract, work)
    assert adopted_target["evidence_review_generation"] == 1
    assert work_execution._review_run_matches_target(old_review, adopted_target, work) is False
    assert work_execution._durable_review_for_target(work, adopted_target) is None



def test_current_project_contract_and_success_criteria_are_not_evidence(ctx):
    """Founder authority/criteria describe what must be proven; they are not proof."""
    from eason_one.services import context as context_service

    operation, _project, _researcher, _critic = multi._setup()
    work = sorted(
        [row for row in operation.works if row.work_type != "MANAGEMENT"],
        key=lambda row: row.id,
    )[0]
    task = work_runtime.task_for_work(work)
    # This wording deliberately overlaps the immutable Project success-criteria
    # DECISION created by multi._setup().  Retrieval must still return no
    # evidence until an actual FACT/EVIDENCE/verified Artifact exists.
    task.title = "Independent specialist Work evidence reconciliation"
    task.objective = "Prove that independent specialist Work is accepted and handed off."
    operation.title = "Independent specialist Work evidence reconciliation"
    operation.objective = "Use persisted evidence to prove accepted specialist handoff."
    db.session.flush()

    rows, meta = context_service.relevant_existing_evidence(task)
    assert rows == []
    assert meta["relevant_items"] == 0
    assert not any("DECISION" in str(row.get("source") or "") for row in rows)


def test_current_missing_evidence_wait_is_quiescent_until_new_persisted_evidence(ctx):
    """Missing-evidence preflight must not wake itself on every scheduler tick."""
    from eason_one.services import context as context_service
    from eason_one.services import runtime_recovery, task_execution

    operation, project, researcher, _critic = multi._setup()
    work = sorted(
        [row for row in operation.works if row.work_type != "MANAGEMENT"],
        key=lambda row: row.id,
    )[0]
    task = work_runtime.task_for_work(work)
    unique = "zxqv-evidence-needle-88421"
    task.title = f"Reconcile {unique}"
    task.objective = f"Use only persisted evidence about {unique}."
    operation.title = f"Evidence-only {unique}"
    operation.objective = f"Reconcile existing evidence for {unique}."
    db.session.flush()

    rows, meta = context_service.relevant_existing_evidence(task)
    assert rows == []
    assert meta["relevant_items"] == 0
    assert meta["basis_hash"]

    if work.state == "READY":
        work_runtime.transition(
            work, "EXECUTING", actor_type="EMPLOYEE", actor_id=researcher.id,
            reason="evidence-only preflight",
        )
    run = task_execution._missing_evidence_run(
        task, researcher, researcher.current_model, "", {"evidence_retrieval": meta}
    )
    issue = f"MISSING_EVIDENCE:{meta['basis_hash']}"
    work_runtime.open_wait(
        work, "DEPENDENCY", run.error_text,
        issue_code=issue, resume_state="EXECUTING",
    )
    work_runtime.sync_task_projection(work, task)
    db.session.commit()
    before_runs = AgentRun.query.filter_by(work_id=work.id, purpose="TASK_EXECUTION").count()

    assert runtime_recovery.resolve_internal_waits() == 0
    db.session.refresh(work)
    assert work.state == "WAITING"
    assert work_runtime.has_open_gate(work, "DEPENDENCY")
    assert AgentRun.query.filter_by(work_id=work.id, purpose="TASK_EXECUTION").count() == before_runs

    db.session.add(KnowledgeItem(
        project_id=project.id,
        kind="EVIDENCE",
        title=f"Persisted evidence for {unique}",
        content=f"Verified historical observation supporting {unique}.",
        source_ref="release-regression://missing-evidence",
        founder_approved=True,
    ))
    db.session.commit()

    rows, meta2 = context_service.relevant_existing_evidence(task)
    assert meta2["relevant_items"] >= 1
    assert any(unique in row["content"] for row in rows)
    assert runtime_recovery.resolve_internal_waits() == 1
    db.session.refresh(work)
    assert work.state == "EXECUTING"
    assert not work_runtime.has_open_gate(work, "DEPENDENCY")
    assert AgentRun.query.filter_by(work_id=work.id, purpose="TASK_EXECUTION").count() == before_runs


def test_current_project_pause_preserves_truth_and_blocks_new_execution_until_resume(ctx):
    """Founder Project pause is non-terminal and must survive every normal dispatch path."""
    from eason_one.services import company_kernel, company_runtime, company_truth, project_company

    operation, project, _researcher, _critic = multi._setup()
    delivery = sorted(
        [row for row in operation.works if row.work_type != "MANAGEMENT"],
        key=lambda row: row.id,
    )[0]
    before_runs = AgentRun.query.filter_by(project_id=project.id).count()

    paused = company_runtime.pause_project(
        project, reason="Founder paused old Project for dual-line validation."
    )
    db.session.refresh(project)
    assert project.status == "PAUSED"
    assert paused >= 1
    assert work_runtime.project_can_activate(project) is False
    assert company_kernel._advance_project(project) is None
    assert all(row.project_id != project.id for row in company_kernel._eligible_works(max_parallelism=4))
    assert company_truth.project_snapshot(project)["state"] == "PAUSED"
    assert all(row["project"].id != project.id for row in project_company.home_snapshot()["active_projects"])
    assert execute_work(delivery) == {"status": "PROJECT_PAUSED", "work_id": delivery.id}
    assert AgentRun.query.filter_by(project_id=project.id).count() == before_runs
    assert any(
        gate.get("condition_type") == "FOUNDER_PAUSE"
        and gate.get("issue_code") == f"PROJECT_PAUSE:{project.id}"
        for gate in work_runtime.open_gates(delivery)
    )

    # Durable rows may retain their pre-pause RUNNING state for settlement and
    # restart classification. They are checkpoints, not current employee work.
    task = work_runtime.task_for_work(delivery)
    checkpoint = AgentRun(
        employee_id=_researcher.id, project_id=project.id, operation_id=operation.id,
        work_id=delivery.id, task_id=task.id, model_config_id=_researcher.current_model.id,
        purpose="TASK_EXECUTION", user_request="paused projection checkpoint",
        system_prompt_snapshot="system", context_snapshot="context", status="RUNNING",
        provider_key_snapshot=_researcher.current_model.provider_key,
        model_name_snapshot=_researcher.current_model.model_name,
        input_price_snapshot=_researcher.current_model.input_price_per_million,
        output_price_snapshot=_researcher.current_model.output_price_per_million,
        request_price_snapshot=_researcher.current_model.request_price_per_call,
        currency_snapshot=_researcher.current_model.currency,
        currency=_researcher.current_model.currency,
    )
    db.session.add(checkpoint); db.session.commit()
    activity = company_truth.employee_activity(_researcher)
    assert activity["activity_type"] == "AVAILABLE"
    assert activity["project_id"] is None

    resolved = company_runtime.resume_project(project, resolution="dual-line validation resume")
    db.session.refresh(project)
    assert resolved >= 1
    assert project.status in {"ACTIVE", "BLOCKED"}
    assert not any(
        gate.get("condition_type") == "FOUNDER_PAUSE"
        and gate.get("issue_code") == f"PROJECT_PAUSE:{project.id}"
        for work in project.works
        for gate in work_runtime.open_gates(work)
    )


def test_paused_project_blocks_startup_adoption_staffing_and_wait_recovery(ctx):
    """Pause is one global mutation fence, including pre-dispatch management paths."""
    from datetime import timedelta
    from eason_one.services import company_kernel, company_runtime, runtime_recovery, team_formation

    operation, project, _researcher, _critic = multi._setup()
    delivery = sorted(
        [row for row in operation.works if row.work_type != "MANAGEMENT"],
        key=lambda row: row.id,
    )[0]
    work_runtime.open_wait(
        delivery, "RETRY_BACKOFF", "A bounded retry would ordinarily be due.",
        retry_after=now() - timedelta(seconds=1),
        issue_code=f"PAUSE_FENCE:{delivery.id}", resume_state="READY",
    )
    db.session.commit()

    company_runtime.pause_project(project, reason="Founder pause mutation-fence proof.")
    before_work_ids = [row.id for row in Work.query.filter_by(project_id=project.id).order_by(Work.id).all()]
    before_assignments = [
        (row.id, row.employee_id, row.ended_at)
        for work in Work.query.filter_by(project_id=project.id).all()
        for row in work.assignments
    ]
    before_control = dict(delivery.runtime_control_json or {})

    assert company_kernel.adopt_operation(operation) == []
    assert team_formation.reconcile_pending_team() is None
    assert runtime_recovery.resolve_internal_waits() == 0
    db.session.refresh(project)
    db.session.refresh(delivery)
    assert project.status == "PAUSED"
    assert work_runtime.has_open_gate(delivery, "RETRY_BACKOFF")
    assert dict(delivery.runtime_control_json or {}) == before_control
    assert [row.id for row in Work.query.filter_by(project_id=project.id).order_by(Work.id).all()] == before_work_ids
    assert [
        (row.id, row.employee_id, row.ended_at)
        for work in Work.query.filter_by(project_id=project.id).all()
        for row in work.assignments
    ] == before_assignments


def test_paused_project_defers_interrupted_meeting_materialization(ctx, monkeypatch):
    """Restart may settle cost truth, but must not mutate Meeting truth while paused."""
    from datetime import timedelta
    from eason_one.models import MeetingStep
    from eason_one.services import company_runtime, meetings, runtime_recovery

    _operation, project, researcher, critic = multi._setup()
    meeting = meetings.create(
        "Paused restart recovery", "Preserve meeting truth", "Do not materialize until Resume",
        critic, [critic, researcher], project=project,
    )
    step = MeetingStep(
        meeting_id=meeting.id, logical_key="paused-restart-proof", kind="CONTRIBUTION",
        round_number=1, employee_id=researcher.id, status="RUNNING",
    )
    db.session.add(step); db.session.commit()
    company_runtime.pause_project(project, reason="Founder paused Meeting recovery proof.")

    monkeypatch.setattr(
        meetings, "recover_interrupted_step",
        lambda _step: (_ for _ in ()).throw(AssertionError("paused Meeting materialized")),
    )
    before = (step.status, meeting.kernel_status, meeting.current_summary_json)
    assert runtime_recovery.recover_interrupted_meeting_steps(
        before=now() + timedelta(seconds=1)
    ) == 0
    db.session.refresh(step); db.session.refresh(meeting); db.session.refresh(project)
    assert project.status == "PAUSED"
    assert (step.status, meeting.kernel_status, meeting.current_summary_json) == before



def test_current_s07_unkeyed_missing_evidence_wait_is_adopted_without_replay(ctx):
    """S08 must convert the live S07 loop state into an exact quiescent evidence gate."""
    from eason_one.services import context as context_service
    from eason_one.services import runtime_recovery, task_execution

    operation, project, researcher, _critic = multi._setup()
    work = sorted(
        [row for row in operation.works if row.work_type != "MANAGEMENT"],
        key=lambda row: row.id,
    )[0]
    task = work_runtime.task_for_work(work)
    unique = "s07-live-missing-evidence-77291"
    task.title = f"Reconcile {unique}"
    task.objective = f"Use only persisted evidence about {unique}."
    operation.title = f"Evidence-only {unique}"
    operation.objective = f"Reconcile existing evidence for {unique}."
    db.session.flush()

    _rows, meta = context_service.relevant_existing_evidence(task)
    assert meta["relevant_items"] == 0
    if work.state == "READY":
        work_runtime.transition(
            work, "EXECUTING", actor_type="EMPLOYEE", actor_id=researcher.id,
            reason="simulate S07 preflight",
        )
    run = task_execution._missing_evidence_run(
        task, researcher, researcher.current_model, "",
        {"evidence_retrieval": {"terms": meta["terms"], "relevant_items": 0}},
    )
    # S07 opened a generic edge-less DEPENDENCY wait with no issue_code.
    work_runtime.open_wait(
        work, "DEPENDENCY", run.error_text,
        resume_state="EXECUTING",
    )
    db.session.commit()
    before_runs = AgentRun.query.filter_by(work_id=work.id, purpose="TASK_EXECUTION").count()

    assert runtime_recovery.resolve_internal_waits() == 1
    db.session.refresh(work)
    gates = work_runtime.open_gates(work, "DEPENDENCY")
    assert work.state == "WAITING"
    assert len(gates) == 1
    assert str(gates[0].get("issue_code") or "").startswith("MISSING_EVIDENCE:")
    assert AgentRun.query.filter_by(work_id=work.id, purpose="TASK_EXECUTION").count() == before_runs
    # A second maintenance tick with no new evidence is fully quiescent.
    assert runtime_recovery.resolve_internal_waits() == 0
    assert AgentRun.query.filter_by(work_id=work.id, purpose="TASK_EXECUTION").count() == before_runs
