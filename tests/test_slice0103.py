import json
from decimal import Decimal

import pytest

from eason_one.extensions import db
from eason_one.models import AgentRun, Employee, MeetingMessage, OperationStep
from eason_one.providers import MockProvider, ProviderResult
from eason_one.services import operations
from eason_one.services.ceo import founder_request
from eason_one.services.meetings import create as create_meeting


def _finished_operation():
    ceo = Employee.query.filter_by(slug="ceo").one()
    _, operation = founder_request(
        ceo, "Prepare the first qualified-prospect validation plan"
    )
    operation.approved_budget_twd = 20
    operations.approve(operation)
    task = operation.tasks[0]
    task.status = "DONE"
    task.result_summary = "Qualified prospects accepted a narrower prepaid probe."
    db.session.commit()
    return operation


def _verification(operation, status):
    return {
        "overall_status": status,
        "criteria": [{
            "criterion": criterion,
            "status": status,
            "evidence": ["Persisted Task and Review evidence."],
            "reason": (
                "Evidence supports the criterion."
                if status == "SATISFIED"
                else "More governed work is required."
            ),
        } for criterion in operation.plan_json["operation"][
            "completion_criteria"
        ]],
        "summary": f"Goal verification is {status}.",
        "recommended_action": (
            "Produce the final report."
            if status == "SATISFIED"
            else "Create bounded remediation work."
        ),
    }


class VerificationProvider:
    def __init__(self, operation, status, contexts=None):
        self.operation = operation
        self.status = status
        self.contexts = contexts if contexts is not None else []
        self.delegate = MockProvider()
        self.calls = 0

    def complete(self, model, system, user, context, maximum, schema=None):
        self.calls += 1
        if "GOAL_VERIFICATION" in system:
            self.contexts.append(context)
            return ProviderResult(
                json.dumps(_verification(self.operation, self.status)),
                30, 15, response_id="verification",
            )
        if "CEO_OPERATION_DECISION" in system:
            assignee = Employee.query.filter_by(slug="researcher").one()
            reviewer = Employee.query.filter_by(slug="research-director").one()
            return ProviderResult(json.dumps({
                "action": "CREATE_TASK",
                "reason": "Verification requires bounded remediation.",
                "goal_status": self.status,
                "confidence": 0.8,
                "next_task_id": None,
                "meeting": None,
                "hiring_need": None,
                "founder_request": None,
                "task_plan": {
                    "task_id": None,
                    "title": "Goal verification remediation",
                    "objective": "Gather the evidence missing from verification.",
                    "assignee_employee_id": assignee.id,
                    "reviewer_employee_id": reviewer.id,
                    "acceptance_criteria": ["Missing evidence is resolved"],
                    "reason": "Resolve the failed Goal Verification criterion.",
                },
            }), 20, 10, response_id="decision")
        return self.delegate.complete(
            model, system, user, context, maximum, schema
        )


def test_workflow_finished_without_satisfied_verification_cannot_complete(
    ctx, monkeypatch
):
    operation = _finished_operation()
    provider = VerificationProvider(operation, "NOT_SATISFIED")
    monkeypatch.setattr(
        "eason_one.services.execution.get_provider", lambda key: provider
    )
    result = operations.next_step(operation, "verify-not-satisfied")
    assert result["kind"] == "GOAL_VERIFICATION"
    assert operation.status == "RUNNING"
    assert not operation.ended_at
    assert not OperationStep.query.filter_by(
        operation_id=operation.id, kind="REPORT"
    ).first()


def test_satisfied_verification_unlocks_report_and_completion(ctx, monkeypatch):
    operation = _finished_operation()
    provider = VerificationProvider(operation, "SATISFIED")
    monkeypatch.setattr(
        "eason_one.services.execution.get_provider", lambda key: provider
    )
    verification = operations.next_step(operation, "verify-satisfied")
    assert verification["overall_status"] == "SATISFIED"
    assert operation.status == "RUNNING"
    report = operations.next_step(operation, "report-after-verification")
    assert report["kind"] == "REPORT"
    assert operation.status == "COMPLETED"


@pytest.mark.parametrize(
    "status", ["NOT_SATISFIED", "INSUFFICIENT_EVIDENCE"]
)
def test_unsatisfied_verification_routes_to_same_operation_remediation(
    ctx, monkeypatch, status
):
    operation = _finished_operation()
    provider = VerificationProvider(operation, status)
    monkeypatch.setattr(
        "eason_one.services.execution.get_provider", lambda key: provider
    )
    operations.next_step(operation, f"verify-{status}")
    decision = operations.next_step(operation, f"remediate-{status}")
    task = db.session.get(
        __import__("eason_one.models", fromlist=["Task"]).Task,
        decision["task_id"],
    )
    assert decision["action"] == "CREATE_TASK"
    assert task.operation_id == operation.id
    assert task.project_id == operation.project_id
    assert operation.status == "RUNNING"
    assert Decimal(operation.approved_budget_twd) == Decimal("20")


def test_verification_receives_compact_required_evidence_without_transcript(
    ctx, monkeypatch
):
    operation = _finished_operation()
    task = operation.tasks[0]
    reviewer = task.reviewer
    db.session.add(AgentRun(
        employee_id=reviewer.id, project_id=operation.project_id,
        task_id=task.id, operation_id=operation.id,
        model_config_id=reviewer.current_model.id, purpose="TASK_REVIEW",
        user_request="Review", system_prompt_snapshot="review",
        context_snapshot="review", status="SUCCEEDED",
        parsed_output_json={
            "decision": "ACCEPT", "summary": "Evidence accepted.",
            "issues": [], "required_changes": [],
        },
        provider_key_snapshot="mock", model_name_snapshot="mock",
        input_price_snapshot=0, output_price_snapshot=0,
        currency_snapshot="TWD",
    ))
    meeting = create_meeting(
        "Audit Meeting", "Audit", "Audit", reviewer, [reviewer],
        operation.project, max_rounds=1, token_limit=100,
        real_cost_limit_twd=1,
    )
    db.session.add(MeetingMessage(
        meeting_id=meeting.id, speaker_type="SYSTEM",
        message_type="TRANSCRIPT", content="FULL TRANSCRIPT MUST STAY OUT",
    ))
    operation.memory_json = {
        "meeting_results": [{
            "meeting_id": meeting.id,
            "result": {"position": "Use a narrower prepaid probe",
                       "controls": ["No consulting"]},
        }],
        "decisions": [{"action": "CONTINUE", "reason": "Apply evidence."}],
    }
    db.session.commit()
    contexts = []
    provider = VerificationProvider(operation, "SATISFIED", contexts)
    monkeypatch.setattr(
        "eason_one.services.execution.get_provider", lambda key: provider
    )
    operations.next_step(operation, "evidence-packet")
    packet = contexts[0]
    assert operation.objective in packet
    assert "completion_criteria" in packet
    assert task.result_summary in packet
    assert "Evidence accepted" in packet
    assert "Use a narrower prepaid probe" in packet
    assert "company_brain_evidence" in packet
    assert "FULL TRANSCRIPT MUST STAY OUT" not in packet
    run = AgentRun.query.filter_by(
        operation_id=operation.id, purpose="GOAL_VERIFICATION"
    ).one()
    meta = run.context_composition_json["goal_verification_evidence"]
    assert meta["characters"] <= meta["budget"]


def test_same_verification_replay_never_duplicates_provider_call(
    ctx, monkeypatch
):
    operation = _finished_operation()
    provider = VerificationProvider(operation, "SATISFIED")
    monkeypatch.setattr(
        "eason_one.services.execution.get_provider", lambda key: provider
    )
    first = operations.next_step(operation, "same-verification")
    duplicate = operations.next_step(operation, "same-verification")
    assert duplicate == first
    assert provider.calls == 1
    assert OperationStep.query.filter_by(
        operation_id=operation.id, kind="GOAL_VERIFICATION"
    ).count() == 1


def test_ambiguous_verification_boundary_cannot_auto_replay(
    ctx, monkeypatch
):
    operation = _finished_operation()
    _, _, digest = operations.goal_evidence_packet(operation)
    db.session.add(OperationStep(
        operation_id=operation.id, idempotency_key="started-verification",
        logical_key=f"goal_verification:{digest}",
        kind="GOAL_VERIFICATION", status="PROVIDER_CALL_STARTED",
    ))
    db.session.commit()
    calls = []
    monkeypatch.setattr(
        "eason_one.services.execution.get_provider",
        lambda key: calls.append(key),
    )
    with pytest.raises(ValueError, match="recovery"):
        operations.next_step(operation, "resume-verification")
    assert calls == []
    assert operation.status == "WAITING_FOR_FOUNDER"


def test_paid_invalid_verification_cannot_auto_replay(ctx, monkeypatch):
    operation = _finished_operation()
    ceo = Employee.query.filter_by(slug="ceo").one()
    ceo.current_model.input_price_per_million = Decimal("1000")
    ceo.current_model.output_price_per_million = Decimal("1000")
    db.session.commit()
    calls = []

    class Invalid:
        def complete(self, *args, **kwargs):
            calls.append(1)
            return ProviderResult(
                "not-json", 10, 10, response_id="paid-verification"
            )

    monkeypatch.setattr(
        "eason_one.services.execution.get_provider", lambda key: Invalid()
    )
    with pytest.raises(ValueError):
        operations.next_step(operation, "paid-verification")
    with pytest.raises(ValueError, match="not running"):
        operations.next_step(operation, "paid-verification-replay")
    assert calls == [1]
    assert OperationStep.query.filter_by(
        operation_id=operation.id, kind="GOAL_VERIFICATION",
        status="PAID_FAILED",
    ).one()


def test_latest_goal_verification_is_visible_in_ceo_context_and_ui(
    ctx, client, monkeypatch
):
    operation = _finished_operation()
    provider = VerificationProvider(operation, "NOT_SATISFIED")
    monkeypatch.setattr(
        "eason_one.services.execution.get_provider", lambda key: provider
    )
    operations.next_step(operation, "context-verification")
    ceo = Employee.query.filter_by(slug="ceo").one()
    composed = __import__(
        "eason_one.services.ceo_context",
        fromlist=["compose"],
    ).compose(ceo, operation=operation)
    assert "LATEST GOAL VERIFICATION" in composed.text
    assert "NOT_SATISFIED" in composed.text
    assert "Create bounded remediation work" in composed.text
    page = client.get(f"/operations/{operation.id}").get_data(as_text=True)
    assert "GOAL VERIFICATION" in page
    assert "NOT SATISFIED" in page
