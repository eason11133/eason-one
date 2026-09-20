from decimal import Decimal

import pytest

from eason_one.extensions import db
from eason_one.models import (
    Employee, EmployeeLearningRecord, Meeting, Operation, ResearchRecord,
)
from eason_one.schemas import CEO_SCHEMA
from eason_one.services import ceo_learning, meeting_kernel, meetings, operation_kernel
from eason_one.services.operations import founder_declared_budget_cap, validate_plan


def _plan(ceo, researcher, *, reviewer_id=None, meeting_trigger="NEVER"):
    return {
        "mode": "OPERATION_PLAN",
        "executive_response": "I will own the result and use the smallest sufficient organization.",
        "operation": {
            "title": "Founder delegated outcome",
            "objective": "Complete one bounded result without unnecessary management overhead.",
            "project_id": None,
            "budget_twd": 10,
            "tasks": [{
                "title": "Primary specialist work",
                "objective": "Produce the required result.",
                "assignee_employee_id": researcher.id,
                "reviewer_employee_id": reviewer_id,
                "acceptance_criteria": ["A usable result is delivered."],
            }],
            "meeting_policy": "NEVER" if meeting_trigger == "NEVER" else "REQUIRED_ON_MATERIAL_CONFLICT",
            "meeting_config": {
                "trigger": meeting_trigger,
                "participant_employee_ids": [researcher.id, ceo.id] if meeting_trigger != "NEVER" else [],
                "max_rounds": 1,
                "max_speakers_per_round": 2,
                "contribution_output_cap": 256,
                "token_limit": 10000,
                "budget_twd": 1 if meeting_trigger != "NEVER" else 0,
                "retry_limit": 0,
            },
            "completion_criteria": ["The Founder receives a usable result."],
        },
    }


def test_normal_founder_work_is_delegated_to_trainable_ceo(ctx):
    routed = operation_kernel.route_command("Analyze this problem and deliver the best answer.")
    assert routed["route_type"] == "AUTO_DELEGATION"
    assert "persistent CEO" in routed["reason"]


def test_optional_reviewer_is_a_valid_cost_aware_plan(ctx):
    ceo = Employee.query.filter_by(slug="ceo").one()
    researcher = Employee.query.filter_by(slug="researcher").one()
    plan = validate_plan(_plan(ceo, researcher, reviewer_id=None))
    assert plan["operation"]["tasks"][0]["reviewer_employee_id"] is None
    reviewer_schema = CEO_SCHEMA["schema"]["properties"]["operation"]["anyOf"][0]["properties"]["tasks"]["items"]["properties"]["reviewer_employee_id"]
    assert "null" in reviewer_schema["type"]


def test_ceo_cannot_invent_response_or_authority_contract_size(ctx):
    schema = CEO_SCHEMA["schema"]
    assert schema["properties"]["executive_response"]["maxLength"] == 420


def test_completion_reserve_blocks_discretionary_spend(ctx):
    ceo = Employee.query.filter_by(slug="ceo").one()
    operation = Operation(
        title="Reserve completion", objective="Protect final delivery.",
        proposed_by_employee_id=ceo.id, status="RUNNING", kernel_status="RUNNING",
        route_type="SINGLE_WORKER", current_stage="EXECUTION",
        plan_json={"operation": {"completion_criteria": ["Done"]}},
        approved_budget_twd=Decimal("1"), hard_cost_cap_twd=Decimal("1"),
        stage_cost_cap_twd=Decimal("1"), single_call_cost_cap_twd=Decimal("1"),
        estimated_cost_twd=Decimal("1"), max_calls=3, max_revisions=0,
        max_messages=6, max_elapsed_seconds=3600,
        memory_json={"completion_reserve_twd": "0.40", "report_reserve_twd": "0.20"},
    )
    db.session.add(operation); db.session.commit()
    with pytest.raises(ValueError, match="reserved for verification"):
        operation_kernel.reserve_provider_call(
            operation, stage="TASK_EXECUTION", estimate_twd=Decimal("0.70"),
            idempotency_key="discretionary-call",
        )


def test_meeting_gate_can_skip_at_zero_cost(ctx):
    ceo = Employee.query.filter_by(slug="ceo").one()
    researcher = Employee.query.filter_by(slug="researcher").one()
    meeting = meetings.create(
        "Material conflict gate", "Escalate only if needed", "Check conflict",
        ceo, [researcher], profile="ECONOMY", max_rounds=1, max_messages=4,
        real_cost_limit_twd=Decimal("1"),
    )
    meetings.skip(meeting, "No persisted material conflict.")
    assert meeting_kernel.status(meeting) == "CANCELLED"
    assert meeting.minutes_json["provider_calls"] == 0
    assert meeting.minutes_json["skip_reason"] == "No persisted material conflict."


def test_terminal_mission_creates_governed_ceo_training_episode(ctx):
    ceo = Employee.query.filter_by(slug="ceo").one()
    operation = Operation(
        title="Outcome episode", objective="Create learning evidence.",
        proposed_by_employee_id=ceo.id, status="RUNNING", kernel_status="RUNNING",
        route_type="SINGLE_WORKER", current_stage="EXECUTION",
        plan_json={"operation": {"completion_criteria": ["Done"]}},
        approved_budget_twd=Decimal("1"), hard_cost_cap_twd=Decimal("1"),
        stage_cost_cap_twd=Decimal("1"), single_call_cost_cap_twd=Decimal("1"),
        estimated_cost_twd=Decimal("0"), max_calls=1, max_revisions=0,
        max_messages=4, max_elapsed_seconds=3600,
        memory_json={"mission_kind": "REAL_WORK"},
    )
    db.session.add(operation); db.session.commit()
    operation_kernel.transition(operation, "COMPLETED", "TEST_COMPLETED", stage="COMPLETED")
    record = ResearchRecord.query.filter_by(record_key=f"CEO-EPISODE-{operation.id}").one()
    learning = EmployeeLearningRecord.query.filter_by(employee_id=ceo.id, source_ref=record.record_key).one()
    assert record.metadata_json["terminal_status"] == "COMPLETED"
    assert learning.validated is False
    ceo_learning.validate_learning(learning)
    context, metadata = ceo_learning.active_policy_context(ceo)
    assert learning.id in metadata["validated_learning_ids"]
    assert learning.title in context


def test_founder_budget_parser_does_not_confuse_call_limits_with_money(ctx):
    assert founder_declared_budget_cap("Maximum provider calls: 1; maximum revisions: 0") is None
    assert founder_declared_budget_cap("Complete this within NT$20") == Decimal("20.0000")
    assert founder_declared_budget_cap("預算上限 10 元") == Decimal("10.0000")


def test_stage_budget_cap_is_hard_even_when_operation_total_has_room(ctx):
    ceo = Employee.query.filter_by(slug="ceo").one()
    operation = Operation(
        title="Stage cap", objective="Keep CEO allocation governed.",
        proposed_by_employee_id=ceo.id, status="RUNNING", kernel_status="RUNNING",
        route_type="SINGLE_WORKER", current_stage="EXECUTION",
        plan_json={"operation": {"completion_criteria": ["Done"]}},
        approved_budget_twd=Decimal("10"), hard_cost_cap_twd=Decimal("10"),
        stage_cost_cap_twd=Decimal("10"), single_call_cost_cap_twd=Decimal("10"),
        estimated_cost_twd=Decimal("2"), max_calls=3, max_revisions=0,
        max_messages=6, max_elapsed_seconds=3600,
        memory_json={
            "mission_kind": "REAL_WORK",
            "completion_reserve_twd": "0",
            "report_reserve_twd": "0",
            "stage_budget_caps": {"TASK_EXECUTION": "0.50"},
        },
    )
    db.session.add(operation); db.session.commit()
    with pytest.raises(ValueError, match="TASK_EXECUTION stage budget cap"):
        operation_kernel.reserve_provider_call(
            operation, stage="TASK_EXECUTION", estimate_twd=Decimal("0.60"),
            idempotency_key="stage-cap-call",
        )


def test_people_and_meetings_remain_primary_navigation(ctx):
    template = open("eason_one/templates/hq_base.html", encoding="utf-8").read()
    assert "('/headquarters/people','People','P')" in template
    assert "('/headquarters/meetings','Meetings','◎')" in template
