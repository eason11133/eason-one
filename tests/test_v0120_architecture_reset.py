from decimal import Decimal

import pytest

from eason_one.extensions import db
from eason_one.models import Employee, MeetingEvent, Operation, OperationEvent, Project
from eason_one.services import meeting_kernel, operation_kernel
from eason_one.services import meetings


def _operation(*, status="PLANNED", kernel_status="CREATED", cap="1"):
    ceo=Employee.query.filter_by(slug="ceo").one()
    row=Operation(
        title="V0.12 bounded work", objective="Verify the reset contract.",
        proposed_by_employee_id=ceo.id, status=status, kernel_status=kernel_status,
        route_type="SINGLE_WORKER", current_stage=kernel_status,
        plan_json={"operation":{"completion_criteria":["Done"]}},
        approved_budget_twd=Decimal(cap), hard_cost_cap_twd=Decimal(cap),
        stage_cost_cap_twd=Decimal(cap), single_call_cost_cap_twd=Decimal(cap),
        max_calls=2, max_revisions=1, max_messages=8, max_elapsed_seconds=3600,
    )
    db.session.add(row); db.session.flush()
    return row


def test_router_chooses_smallest_sufficient_path(ctx):
    assert operation_kernel.route_command("What is our current cost?")["route_type"]=="DIRECT_RESPONSE"
    assert operation_kernel.route_command("Pause Mission #4")["route_type"]=="DETERMINISTIC_ACTION"
    assert operation_kernel.route_command("@Engineer fix this bounded bug")["route_type"]=="SINGLE_WORKER"
    assert operation_kernel.route_command("@Researcher @Engineer 開一場短會")["route_type"]=="SHORT_MEETING"
    assert operation_kernel.route_command("建立一個多階段完整專案")["route_type"]=="FULL_PROJECT"


def test_initial_founder_gate_keeps_legacy_planned_projection(ctx):
    operation=_operation()
    operation_kernel.set_route(operation,"SINGLE_WORKER","One specialist is sufficient.",commit=False)
    operation_kernel.transition(operation,"WAITING_APPROVAL","FOUNDER_APPROVAL_REQUESTED",stage="APPROVAL")
    assert operation.kernel_status=="WAITING_APPROVAL"
    assert operation.status=="PLANNED"
    assert [event.event_type for event in OperationEvent.query.filter_by(operation_id=operation.id).order_by(OperationEvent.sequence)]==[
        "ROUTE_SELECTED","FOUNDER_APPROVAL_REQUESTED"
    ]


def test_provider_reservation_is_atomic_and_hard_capped(ctx):
    operation=_operation(status="RUNNING",kernel_status="RUNNING",cap="0.50")
    operation.single_call_cost_cap_twd=Decimal("0.40")
    first=operation_kernel.reserve_provider_call(
        operation,stage="TASK_EXECUTION",estimate_twd=Decimal("0.30"),idempotency_key="call:1"
    )
    assert first.status=="RESERVED"
    with pytest.raises(ValueError,match="remaining Operation authority"):
        operation_kernel.reserve_provider_call(
            operation,stage="TASK_EXECUTION",estimate_twd=Decimal("0.30"),idempotency_key="call:2"
        )
    operation_kernel.resolve_reservation(first,actual_twd=Decimal("0.20"),status="CONSUMED")
    snapshot=operation_kernel.budget_snapshot(operation)
    assert snapshot["reserved"]==Decimal("0.000000")
    assert operation.call_count==1


def test_meeting_is_persistent_bounded_escalation(ctx):
    ceo=Employee.query.filter_by(slug="ceo").one()
    engineer=Employee.query.filter_by(slug="engineer").one()
    project=Project(name="Meeting reset",objective="Bound a real conflict",owner_employee_id=ceo.id)
    db.session.add(project); db.session.flush()
    meeting=meetings.create(
        "Bounded decision","Resolve one cross-role conflict","Choose one safe implementation",
        ceo,[engineer],project=project,profile="ECONOMY",max_rounds=1,max_messages=4,
    )
    assert meeting.kernel_status=="READY"
    assert meeting.status=="PLANNED"
    meetings.start_auto(meeting)
    assert meeting.kernel_status=="ACTIVE"
    assert meeting.status=="RUNNING"
    meetings.pause(meeting)
    assert meeting.kernel_status=="WAITING_FOR_INPUTS"
    assert meeting.status=="PAUSED"
    assert MeetingEvent.query.filter_by(meeting_id=meeting.id).count()>=3


def test_legacy_running_state_is_imported_without_becoming_created(ctx):
    operation=_operation(status="RUNNING",kernel_status="CREATED")
    assert operation_kernel.authoritative_status(operation)=="RUNNING"
