from decimal import Decimal
import json

import pytest

from eason_one.extensions import db
from eason_one.models import (
    Decision, Department, Employee, Escalation, HiringRequest, Meeting, MeetingEvent, ModelConfig,
    Operation, Project, Work, now,
)
from eason_one.services import (
    command, company_runtime, company_truth, founder_decisions, governance,
    meeting_coordination, meeting_kernel, operation_kernel, operations, project_contract,
    runtime_recovery, work_runtime, workforce,
)


def _project(status="ACTIVE"):
    ceo = Employee.query.filter_by(slug="ceo").one()
    project = Project(
        name="Governed Project", objective="Deliver exact outcome.", status=status,
        priority="HIGH", environment="LIVE", origin="TEST", owner_employee_id=ceo.id,
        real_budget_limit=Decimal("100"), known_constraints="Stay bounded.",
    )
    db.session.add(project); db.session.flush()
    project_contract.freeze(
        project, success_criteria=["Outcome is proven"], constraints=["Stay bounded."],
        origin_employee_id=ceo.id,
    )
    db.session.commit()
    return project


def _operation(project):
    ceo = Employee.query.filter_by(slug="ceo").one()
    operation = Operation(
        title="Governed Mission", objective="Produce evidence", project_id=project.id,
        proposed_by_employee_id=ceo.id, status="RUNNING", kernel_status="RUNNING",
        route_type="FULL_PROJECT", current_stage="COMPANY_KERNEL_V020",
        plan_json={"mode":"OPERATION_PLAN","executive_response":"go","operation":{
            "title":"Governed Mission","objective":"Produce evidence","project_id":project.id,
            "budget_twd":"20","tasks":[],"meeting_policy":"NEVER","meeting_config":{
                "trigger":"NEVER","participant_employee_ids":[],"max_rounds":1,
                "max_speakers_per_round":1,"contribution_output_cap":192,"token_limit":6000,
                "budget_twd":0,"retry_limit":0},"completion_criteria":["Mission evidence"]}},
        approved_budget_twd=Decimal("20"), hard_cost_cap_twd=Decimal("20"),
        stage_cost_cap_twd=Decimal("20"), single_call_cost_cap_twd=Decimal("20"),
        max_calls=8, max_revisions=1, max_messages=16, max_elapsed_seconds=3600,
        approved_at=now(), memory_json={"runtime_semantics":"WORK_CORE_V018","core_rebuild_version":"0.20.0"},
    )
    db.session.add(operation); db.session.commit()
    return operation


def _work(project, operation, title):
    employee = Employee.query.filter_by(slug="researcher").one()
    work = Work(
        project_id=project.id, operation_id=operation.id, title=title, purpose="Do bounded work",
        expected_output="Artifact", acceptance_criteria="Evidence exists", state="READY",
        work_type="DELIVERY", priority="HIGH", created_by_employee_id=employee.id,
        resource_ceiling_twd=Decimal("10"), retry_limit=1,
    )
    db.session.add(work); db.session.commit()
    return work


def test_generic_escalation_writer_cannot_mint_vnext_founder_authority(ctx):
    project = _project(); operation = _operation(project); work = _work(project, operation, "A")
    with pytest.raises(ValueError, match="V020_FOUNDER_AUTHORITY_MUST_USE_CANONICAL_GOVERNANCE"):
        __import__("eason_one.services.escalations", fromlist=["open_escalation"]).open_escalation(
            project_id=project.id, operation_id=operation.id, work_id=work.id,
            escalation_type="CODEX_RISK_APPROVAL", reason="unsafe",
        )


def test_manual_resume_does_not_resolve_governance_or_reconciliation(ctx):
    project = _project(); operation = _operation(project); work = _work(project, operation, "A")
    governance.request_budget_gate(
        project=project, additional_twd=Decimal("5"), reason="Exact shortfall",
        work=work, operation=operation,
    )
    work_runtime.open_wait(work, "FOUNDER_DECISION", "Exact shortfall")
    work_runtime.open_wait(work, "RECONCILIATION", "Independent runtime reconciliation")
    company_runtime.pause_operation(operation)
    db.session.commit()

    resolved = company_runtime.resume_after_founder(operation)
    db.session.refresh(work)
    gates = {row["condition_type"]: row["state"] for row in work_runtime.open_gates(work)}
    assert resolved == 1
    assert "FOUNDER_PAUSE" not in gates
    assert gates["FOUNDER_DECISION"] == "OPEN"
    assert gates["RECONCILIATION"] == "OPEN"
    assert governance.current_gate(project) is not None


def test_mission_cancel_does_not_resolve_project_founder_gate(ctx):
    project = _project(); operation = _operation(project); work = _work(project, operation, "A")
    gate = governance.request_budget_gate(
        project=project, additional_twd=Decimal("5"), reason="Project authority still needed",
        work=work, operation=operation,
    )
    company_runtime.cancel_operation(operation)
    db.session.refresh(gate); db.session.refresh(project)
    assert gate.state == "OPEN"
    assert project.status != "CANCELLED"


def test_governed_approved_mission_cannot_use_legacy_founder_decision_path(ctx):
    project = _project(); operation = _operation(project)
    operation.status = "WAITING_FOR_FOUNDER"
    operation.founder_report_json = {"decision_kind":"FOUNDER_AUTHORITY", "summary":"legacy"}
    db.session.commit()
    with pytest.raises(ValueError, match="no current canonical Founder Governance gate"):
        founder_decisions.decide(operation, "APPROVE", reason="legacy path must fail")


def test_internal_escalation_is_recovery_not_founder_attention(ctx):
    project = _project(); operation = _operation(project); work = _work(project, operation, "A")
    db.session.add(Escalation(
        project_id=project.id, operation_id=operation.id, work_id=work.id,
        escalation_type="RUNTIME_RECONCILIATION", state="OPEN", reason="Internal ambiguity",
    ))
    db.session.commit()
    snap = company_truth.project_snapshot(project)
    assert snap["state"] == "RECOVERING"
    assert snap["open_founder_escalations"] == []
    assert len(snap["open_internal_escalations"]) == 1


def test_different_founder_gates_queue_without_destroying_prior_authority(ctx):
    project = _project(); operation = _operation(project)
    first = _work(project, operation, "A"); second = _work(project, operation, "B")
    gate1 = governance.open_gate(
        project=project, escalation_type="CODEX_RISK_APPROVAL", reason="Approve exact A",
        work=first, operation=operation,
        authority_payload={"requested_action":{"command":"A"}},
    )
    work_runtime.open_wait(first, "FOUNDER_DECISION", "Approve exact A")
    gate2 = governance.open_gate(
        project=project, escalation_type="CODEX_RISK_APPROVAL", reason="Approve exact B",
        work=second, operation=operation,
        authority_payload={"requested_action":{"command":"B"}},
    )
    work_runtime.open_wait(second, "FOUNDER_DECISION", "Approve exact B")
    db.session.commit(); db.session.refresh(gate1); db.session.refresh(gate2); db.session.refresh(first)

    assert gate1.state == "OPEN"
    assert gate2.state == "OPEN"
    first_open = {row["condition_type"] for row in work_runtime.open_gates(first)}
    assert "FOUNDER_DECISION" in first_open
    assert governance.current_gate(project).id == gate2.id


def test_vnext_mission_budget_snapshot_fails_closed_on_contract_projection_drift(ctx):
    project = _project(); operation = _operation(project)
    project.real_budget_limit = Decimal("999")
    db.session.flush()
    with pytest.raises(ValueError, match="PROJECT_BUDGET_LEDGER_MISMATCH"):
        operation_kernel.public_snapshot(operation)


def test_existing_project_proposal_budget_reader_fails_closed_on_contract_projection_drift(ctx):
    project = _project(); operation = _operation(project)
    researcher = Employee.query.filter_by(slug="researcher").one()
    plan = dict(operation.plan_json)
    data = dict(plan["operation"])
    data["tasks"] = [{
        "title":"Bounded proof", "objective":"Produce proof",
        "assignee_employee_id":researcher.id, "reviewer_employee_id":None,
        "acceptance_criteria":["Outcome is proven"],
    }]
    plan["operation"] = data
    operation.plan_json = plan
    project.real_budget_limit = Decimal("999")
    db.session.flush()
    with pytest.raises(ValueError, match="PROJECT_BUDGET_LEDGER_MISMATCH"):
        operations.proposal_authority_snapshot(operation)


def test_budget_gate_requires_exact_positive_project_amount(ctx):
    project = _project(); operation = _operation(project)
    with pytest.raises(ValueError, match="FOUNDER_BUDGET_GATE_REQUIRES_EXACT_POSITIVE_AMOUNT"):
        governance.request_budget_gate(project=project, additional_twd=0, reason="blank", operation=operation)


def test_rejected_budget_authority_is_durable_negative_receipt(ctx):
    project = _project(); operation = _operation(project)
    gate = governance.request_budget_gate(
        project=project, additional_twd=Decimal("5"), reason="Need more Project authority", operation=operation,
    )
    decision = governance.resolve_gate(gate, "REJECT", reason="Keep the current Founder cap")
    db.session.refresh(project)

    assert decision.decision == "REJECT"
    assert governance.current_gate(project) is None
    assert Decimal(str(project_contract.effective_authority(project)["effective_budget_limit_twd"])) == Decimal("100")

    # A slightly different runtime estimate is not a materially new Founder
    # authority question. The rejected cap remains binding while the governing
    # Contract/authority hashes are unchanged.
    with pytest.raises(governance.FounderAuthorityPreviouslyRejected, match="FOUNDER_AUTHORITY_PREVIOUSLY_REJECTED"):
        governance.request_budget_gate(
            project=project, additional_twd=Decimal("5.25"),
            reason="Recomputed shortfall after the same rejected cap", operation=operation,
        )

    assert governance.current_gate(project) is None
    assert Escalation.query.filter_by(project_id=project.id).count() == 1
    assert Decision.query.filter_by(project_id=project.id, decision="REJECT").count() == 1


def test_restart_reconciles_already_open_duplicate_budget_gate_after_reject(ctx):
    project = _project(); operation = _operation(project); work = _work(project, operation, "A")
    first = governance.request_budget_gate(
        project=project, additional_twd=Decimal("5"), reason="Need more Project authority",
        work=work, operation=operation,
    )
    rejection = governance.resolve_gate(first, "REJECT", reason="Keep current cap")

    # Recreate the r6 defect: an automated next pass already opened another
    # budget question before r7 was installed. Reconciliation must retire it
    # without asking Founder to reject twice.
    duplicate = governance.request_budget_gate(
        project=project, additional_twd=Decimal("5.25"), reason="Same cap reached again",
        work=work, operation=operation, allow_founder_reconsideration=True,
    )
    work_runtime.open_wait(work, "FOUNDER_DECISION", "Same cap reached again")
    db.session.commit()

    repaired = runtime_recovery.reconcile_reopened_rejected_founder_gates()
    db.session.refresh(project); db.session.refresh(duplicate); db.session.refresh(work)

    assert duplicate.id in repaired
    assert duplicate.state == "RESOLVED"
    assert duplicate.resolution == "FOUNDER_REJECT_NEGATIVE_AUTHORITY_RECONCILED"
    assert governance.current_gate(project) is None
    assert project.status == "BLOCKED"
    assert work.state == "CANCELLED"
    assert not work_runtime.open_gates(work)
    assert Decision.query.filter_by(project_id=project.id, decision="REJECT").count() == 1
    assert Decision.query.filter_by(legacy_source="FOUNDER_ESCALATION", legacy_source_id=duplicate.id).count() == 0
    assert rejection.state == "COMMITTED"


def test_founder_can_explicitly_reconsider_budget_after_prior_rejection(ctx):
    project = _project(); operation = _operation(project)
    gate = governance.request_budget_gate(
        project=project, additional_twd=Decimal("5"), reason="Need more Project authority", operation=operation,
    )
    governance.resolve_gate(gate, "REJECT", reason="Not now")

    # A direct Founder-initiated budget change is different from Company code
    # repeatedly reopening the rejected question.
    reconsidered = governance.request_budget_gate(
        project=project, additional_twd=Decimal("2"),
        reason="Founder explicitly reconsidered the Project cap", operation=operation,
        allow_founder_reconsideration=True,
    )
    governance.resolve_gate(reconsidered, "APPROVE", reason="Founder explicitly changed the cap")
    assert Decimal(str(project_contract.effective_authority(project)["effective_budget_limit_twd"])) == Decimal("102")


def test_approve_cannot_override_frozen_founder_payload(ctx):
    project = _project(); operation = _operation(project)
    gate = governance.request_budget_gate(
        project=project, additional_twd=Decimal("5"), reason="Exact shortfall", operation=operation,
    )
    db.session.commit()

    with pytest.raises(ValueError, match="FOUNDER_APPROVE_MUST_CONSUME_FROZEN_PAYLOAD"):
        governance.resolve_gate(
            gate, "APPROVE", reason="attempted override",
            changes={"additional_budget_twd":"999", "scope":"PROJECT"},
        )

    db.session.refresh(project); db.session.refresh(gate)
    assert Decimal(str(project.real_budget_limit)) == Decimal("100")
    assert gate.state == "OPEN"
    assert Decision.query.filter_by(legacy_source="FOUNDER_ESCALATION", legacy_source_id=gate.id).count() == 0


def test_modify_revalidates_and_audits_exact_resolved_payload(ctx):
    project = _project(); operation = _operation(project)
    gate = governance.request_budget_gate(
        project=project, additional_twd=Decimal("5"), reason="Exact shortfall", operation=operation,
    )
    decision = governance.resolve_gate(
        gate, "MODIFY", reason="Founder chose a different exact amount",
        changes={"additional_budget_twd":"7", "scope":"PROJECT"},
    )
    basis = json.loads(decision.authority_basis)

    assert Decimal(str(project.real_budget_limit)) == Decimal("107")
    assert basis["frozen_approve_payload"]["additional_budget_twd"] == "5"
    assert basis["resolved_authority_payload"]["additional_budget_twd"] == "7"
    assert basis["founder_changes"]["additional_budget_twd"] == "7"
    assert basis["applied_effects"]["additional_budget_twd"] == "7"
    assert basis["version"] == "FOUNDER_GOVERNANCE_DECISION_V2"


def test_modify_cannot_bypass_type_specific_validation(ctx):
    project = _project(); operation = _operation(project)
    gate = governance.request_budget_gate(
        project=project, additional_twd=Decimal("5"), reason="Exact shortfall", operation=operation,
    )
    db.session.commit()
    with pytest.raises(ValueError, match="FOUNDER_BUDGET_GATE_REQUIRES_EXACT_POSITIVE_AMOUNT"):
        governance.resolve_gate(
            gate, "MODIFY", reason="invalid modified amount",
            changes={"additional_budget_twd":"0", "scope":"PROJECT"},
        )
    db.session.refresh(project); db.session.refresh(gate)
    assert Decimal(str(project.real_budget_limit)) == Decimal("100")
    assert gate.state == "OPEN"


def test_stale_approve_rechecks_result_ready_lock_at_commit(ctx):
    project = _project(); operation = _operation(project)
    gate = governance.request_budget_gate(
        project=project, additional_twd=Decimal("5"), reason="Exact shortfall", operation=operation,
    )
    db.session.commit()
    project.status = "REVIEW"
    db.session.commit()

    with pytest.raises(ValueError, match="PROJECT_RESULT_READY_AUTHORITY_LOCKED"):
        governance.resolve_gate(gate, "APPROVE", reason="stale approval must fail")

    db.session.refresh(project); db.session.refresh(gate)
    assert Decimal(str(project.real_budget_limit)) == Decimal("100")
    assert gate.state == "OPEN"
    assert Decision.query.filter_by(legacy_source="FOUNDER_ESCALATION", legacy_source_id=gate.id).count() == 0


def test_mission_budget_http_writer_amends_project_contract_not_operation_budget(ctx, client):
    project = _project(); operation = _operation(project)
    before_operation = Decimal(str(operation.approved_budget_twd))
    response = client.post(
        f"/operations/{operation.id}/budget",
        data={"additional_budget_twd":"5"},
    )
    assert response.status_code == 302
    db.session.refresh(project); db.session.refresh(operation)
    assert Decimal(str(project_contract.effective_authority(project)["effective_budget_limit_twd"])) == Decimal("105")
    assert Decimal(str(project.real_budget_limit)) == Decimal("105")
    assert Decimal(str(operation.approved_budget_twd)) == before_operation


def test_ceo_budget_http_writer_amends_project_contract_not_operation_budget(ctx, client):
    project = _project(); operation = _operation(project)
    before_operation = Decimal(str(operation.approved_budget_twd))
    response = client.post(
        f"/headquarters/ceo/operations/{operation.id}/budget",
        data={"additional_budget_twd":"6"},
        headers={"Accept":"application/json"},
    )
    assert response.status_code == 200
    db.session.refresh(project); db.session.refresh(operation)
    assert Decimal(str(project_contract.effective_authority(project)["effective_budget_limit_twd"])) == Decimal("106")
    assert Decimal(str(project.real_budget_limit)) == Decimal("106")
    assert Decimal(str(operation.approved_budget_twd)) == before_operation


def test_result_ready_locks_new_authority(ctx):
    project = _project(status="REVIEW"); operation = _operation(project)
    project.status = "REVIEW"; db.session.commit()
    with pytest.raises(ValueError, match="PROJECT_RESULT_READY_AUTHORITY_LOCKED"):
        governance.request_budget_gate(project=project, additional_twd=Decimal("5"), reason="late", operation=operation)


def test_exact_action_receipt_is_bound_to_current_contract_and_authority(ctx):
    project = _project(); operation = _operation(project); work = _work(project, operation, "A")
    requested = {"command":"bounded-codex-action", "path":"repo"}
    gate = governance.open_gate(
        project=project, escalation_type="CODEX_RISK_APPROVAL", reason="Exact Codex exception",
        work=work, operation=operation, authority_payload={"requested_action":requested},
    )
    governance.resolve_gate(gate, "APPROVE", reason="Approve exact exception")
    receipt = governance.approval_receipt(
        project=project, work=work, authority_type="CODEX_RISK_APPROVAL", requested_action=requested,
    )
    assert receipt and receipt["decision_id"]
    assert Decision.query.filter_by(id=receipt["decision_id"], state="COMMITTED").one()

    project_contract.authorize_budget_extension(
        project, additional_twd=Decimal("1"), reason="Later Founder budget change",
        origin_employee_id=project.owner_employee_id,
    )
    db.session.commit()
    assert governance.approval_receipt(
        project=project, work=work, authority_type="CODEX_RISK_APPROVAL", requested_action=requested,
    ) is None



def _delegated_request(project):
    requester = Employee.query.filter_by(slug="research-director").one()
    department = Department.query.filter_by(name="Research Department").one()
    model = ModelConfig.query.filter_by(active=True, archived=False).first()
    request = HiringRequest(
        requested_by_type="EMPLOYEE", requester_employee_id=requester.id,
        project_id=project.id, role_needed="Governed Project Specialist",
        problem="Current persistent team lacks one bounded specialist capability.",
        why_now="Current Project Work requires the capability.",
        responsibilities_json=["Perform bounded Project Work"],
        capabilities_json=["Specialist analysis"], urgency="MEDIUM",
        use_frequency="PROJECT", status="FOUNDER_REVIEW",
        hr_assessment_json={
            "recommendation":"HIRE", "reasoning":"Persistent capability gap is proven.",
            "probation_assignments":2,
        },
        recommended_model_config_id=model.id,
        target_department_id=department.id,
        target_position="Governed Project Specialist",
        manager_employee_id=requester.id,
        resource_envelope_json={"max_mission_budget_twd":"0"},
    )
    db.session.add(request); db.session.commit()
    return request


def test_delegated_hire_materializes_without_founder_authority(ctx):
    project = _project(); request = _delegated_request(project)
    employee = workforce.commit_delegated_hire(request)
    db.session.refresh(request)

    assert employee.active is True
    assert Decimal(employee.salary_credits_per_week) == Decimal("0")
    assert request.status == "HIRED"
    assert request.founder_decision == "NOT_REQUIRED"
    assert governance.attention(project) == []

    decision = Decision.query.filter_by(
        legacy_source="DELEGATED_HIRING", legacy_source_id=request.id
    ).one()
    assert decision.state == "COMMITTED"
    assert decision.decision == "HIRE"
    assert '"authority": "COMPANY_DELEGATED"' in decision.authority_basis
    assert '"new_budget_authority_twd": "0"' in decision.authority_basis


def test_delegated_hire_fails_after_result_ready_boundary(ctx):
    project = _project(); request = _delegated_request(project)
    project.status = "REVIEW"; db.session.commit()
    with pytest.raises(ValueError, match="outside delegated Project authority"):
        workforce.commit_delegated_hire(request)
    assert request.created_employee_id is None


def test_gate_identity_ignores_reason_wording_for_same_exact_authority(ctx):
    project = _project(); operation = _operation(project); work = _work(project, operation, "A")
    first = governance.open_gate(
        project=project, escalation_type="CODEX_RISK_APPROVAL", reason="First wording",
        work=work, operation=operation, authority_payload={"requested_action":{"command":"A"}},
    )
    second = governance.open_gate(
        project=project, escalation_type="CODEX_RISK_APPROVAL", reason="Paraphrased wording",
        work=work, operation=operation, authority_payload={"requested_action":{"command":"A"}},
    )
    assert second.id == first.id
    assert Escalation.query.filter_by(project_id=project.id, state="OPEN").count() == 1


def test_project_cancel_is_project_level_and_audited(ctx):
    project = _project(); operation = _operation(project); first = _work(project, operation, "A")
    gate = governance.open_gate(
        project=project, escalation_type="PROJECT_CANCEL",
        reason="Founder explicitly cancels the entire Project.", authority_payload={},
    )
    decision = governance.resolve_gate(gate, "APPROVE", reason="Cancel entire Project")
    db.session.refresh(project); db.session.refresh(operation); db.session.refresh(first)
    assert project.status == "CANCELLED"
    assert operation.status == "CANCELLED"
    assert first.state == "CANCELLED"
    assert decision.decision == "APPROVE"
    assert Decision.query.filter_by(id=decision.id, state="COMMITTED").one()


def test_completed_project_cannot_be_rewritten_as_cancelled(ctx):
    project = _project(status="ACTIVE")
    project.status = "COMPLETED"; db.session.commit()
    with pytest.raises(ValueError, match="PROJECT_ALREADY_COMPLETED"):
        governance.open_gate(
            project=project, escalation_type="PROJECT_CANCEL", reason="too late", authority_payload={}
        )



def test_manual_hr_review_path_consumes_delegated_authority_without_founder(ctx):
    project = _project(); operation = _operation(project)
    requester = Employee.query.filter_by(slug="research-director").one()
    model = ModelConfig.query.filter_by(active=True, archived=False).first()
    request = workforce.request_hire(
        requested_by_type="EMPLOYEE", requester=requester, operation=operation, project=project,
        role_needed="Manual Review Specialist", problem="Need one bounded capability",
        why_now="Project Work requires it", responsibilities=["Bounded work"],
        capabilities=["Specialist analysis"], urgency="MEDIUM", use_frequency="PROJECT",
    )
    workforce.review_request(
        request, existing_staff_alternative="No current Employee has the exact capability",
        recommendation="HIRE", recommended_model=model, estimated_input_tokens=100,
        estimated_output_tokens=100, expected_calls=1, max_mission_budget_twd=Decimal("1"),
        expected_benefit="Close capability gap", redundancy_risk="Low",
        alternatives=["Use existing staff was assessed"], success_criteria=["Deliver bounded result"],
        probation_assignments=2,
    )
    db.session.refresh(request)
    assert request.status == "HIRED"
    assert request.founder_decision == "NOT_REQUIRED"
    assert request.created_employee_id is not None
    assert governance.attention(project) == []


def _meeting(project, operation, *, status="PAUSED"):
    company = __import__("eason_one.services.company", fromlist=["get_company"]).get_company()
    chair = Employee.query.filter_by(slug="ceo").one()
    meeting = Meeting(
        company_id=company.id, project_id=project.id, operation_id=operation.id,
        title="Governed Meeting", purpose="Resolve bounded coordination",
        agenda="Review evidence", chair_employee_id=chair.id, status=status,
        kernel_status=("WAITING_FOR_INPUTS" if status in {"PAUSED", "WAITING_FOR_FOUNDER"} else "READY"),
        current_stage=("WAITING_FOR_INPUTS" if status in {"PAUSED", "WAITING_FOR_FOUNDER"} else "READY"),
        max_messages=8, max_rounds=2, token_limit=6000, real_cost_limit_twd=Decimal("1"),
        max_speakers_per_round=2, contribution_output_cap=256, router_output_cap=128,
        synthesis_output_cap=256,
    )
    db.session.add(meeting); db.session.commit()
    return meeting


def test_meeting_failure_projection_is_company_recovery_not_founder_attention(ctx):
    project = _project(); operation = _operation(project)
    meeting = _meeting(project, operation, status="PAUSED")
    meeting_kernel.append_event(
        meeting, "MEETING_SYNTHESIS_FAILED", from_status="SYNTHESIZING",
        to_status="WAITING_FOR_INPUTS", stage="WAITING_FOR_INPUTS",
        payload={"error":"provider failed"},
    )
    db.session.commit()

    assert meeting_coordination.requires_founder_input(meeting) is False
    assert not any(
        row["kind"] == "MEETING" and row.get("href") == f"/meetings/{meeting.id}"
        for row in command._attention()
    )


def test_only_explicit_meeting_question_becomes_founder_attention(ctx):
    project = _project(); operation = _operation(project)
    meeting = _meeting(project, operation, status="PAUSED")
    meeting_kernel.transition(
        meeting, "WAITING_FOR_INPUTS", "MEETING_FOUNDER_INPUT_REQUIRED",
        stage="WAITING_FOR_INPUTS", legacy_status="WAITING_FOR_FOUNDER", commit=False,
        payload={"question":"Which business constraint should govern this tradeoff?"},
    )
    meeting.routing_json = {
        "waiting_for_founder": True, "founder_message_id_at_wait": 0,
        "question": "Which business constraint should govern this tradeoff?",
    }
    db.session.commit()

    assert meeting_coordination.requires_founder_input(meeting) is True
    assert any(
        row["kind"] == "MEETING" and row.get("href") == f"/meetings/{meeting.id}"
        for row in command._attention()
    )


def test_stale_waiting_for_founder_string_is_not_authority_or_attention(ctx):
    project = _project(); operation = _operation(project)
    meeting = _meeting(project, operation, status="WAITING_FOR_FOUNDER")
    meeting.routing_json = {"waiting_for_founder": False}
    db.session.commit()

    assert meeting_coordination.requires_founder_input(meeting) is False
    assert command.project_view(project)["attention"] is False


def test_platform_gate_invalidation_never_creates_founder_decision_or_authority(ctx):
    project = _project(); operation = _operation(project); work = _work(project, operation, "A")
    gate = governance.open_gate(
        project=project, escalation_type="CODEX_RISK_APPROVAL",
        reason="Exact action appeared to require Founder authority",
        work=work, operation=operation, authority_payload={"requested_action":{"command":"A"}},
    )
    before_contract = project_contract.governing_terms(project)
    before_decisions = Decision.query.count()
    governance.invalidate_gate(
        gate, resolution="PLATFORM_FAULT_INVALIDATED",
        reason="The platform condition was proven false before dispatch.",
    )
    db.session.refresh(gate)

    assert gate.state == "RESOLVED"
    assert gate.resolution == "PLATFORM_FAULT_INVALIDATED"
    assert Decision.query.count() == before_decisions
    assert project_contract.governing_terms(project) == before_contract
    assert governance.approval_receipt(
        project=project, work=work, authority_type="CODEX_RISK_APPROVAL",
        requested_action={"command":"A"},
    ) is None


def test_governed_operation_briefing_ignores_legacy_pause_report_without_canonical_gate(ctx):
    project = _project(); operation = _operation(project)
    operation.status = "PAUSED"
    operation.founder_report_json = {
        "decision_kind": "FOUNDER_AUTHORITY",
        "summary": "Legacy runtime report must not manufacture Founder attention.",
    }
    operation.waiting_reason = "Internal provider recovery is pending."
    db.session.commit()

    briefing = command._operation_briefing(operation)
    assert briefing["founder_decision"] is None
    assert briefing["founder_attention"] is None


def test_governed_operation_briefing_reads_exact_canonical_gate(ctx):
    project = _project(); operation = _operation(project); work = _work(project, operation, "A")
    gate = governance.request_budget_gate(
        project=project, additional_twd=Decimal("5"), reason="Exact Project shortfall",
        work=work, operation=operation,
    )
    db.session.commit()

    briefing = command._operation_briefing(operation)
    assert briefing["founder_decision"]["escalation_id"] == gate.id
    assert briefing["founder_decision"]["kind"] == "BUDGET_AUTHORIZATION"
    assert briefing["founder_decision"]["additional_required_twd"] == Decimal("5")
    assert briefing["founder_attention"] == "Exact Project shortfall"


def test_headquarters_fault_contains_historical_project_without_contract(ctx, client):
    ceo = Employee.query.filter_by(slug="ceo").one()
    historical = Project(
        name="Historical without Contract", objective="Legacy audit record", status="COMPLETED",
        priority="LOW", environment="LIVE", origin="LEGACY", owner_employee_id=ceo.id,
        real_budget_limit=Decimal("0"), known_constraints="",
    )
    db.session.add(historical); db.session.commit()

    response = client.get("/headquarters")
    assert response.status_code == 200
    snapshot = __import__(
        "eason_one.services.project_company", fromlist=["home_snapshot"]
    ).home_snapshot()
    card = next(row for row in snapshot["projects"] if row["project"].id == historical.id)
    assert card["contract_integrity_error"] is None


def test_headquarters_fault_contains_governed_missing_contract_but_execution_stays_fail_closed(ctx, client):
    project = _project(); _operation(project)
    contract_row = __import__("eason_one.models", fromlist=["KnowledgeItem"]).KnowledgeItem.query.filter_by(
        project_id=project.id, title=project_contract.CONTRACT_TITLE, founder_approved=True,
    ).one()
    db.session.delete(contract_row); db.session.commit()

    # Strict authority/execution reader still refuses to synthesize vNext truth.
    with pytest.raises(ValueError, match="PROJECT_CONTRACT_MISSING"):
        project_contract.governing_terms(project)

    # Founder read surface remains available and exposes the integrity block.
    response = client.get("/headquarters")
    assert response.status_code == 200
    assert b"Governance integrity blocked" in response.data


def test_read_projection_never_falls_back_for_governed_missing_contract(ctx):
    project = _project(); _operation(project)
    contract_row = __import__("eason_one.models", fromlist=["KnowledgeItem"]).KnowledgeItem.query.filter_by(
        project_id=project.id, title=project_contract.CONTRACT_TITLE, founder_approved=True,
    ).one()
    db.session.delete(contract_row); db.session.commit()

    projection = project_contract.read_projection(project)
    assert projection["is_vnext_governed"] is True
    assert projection["terms"] is None
    assert projection["integrity_error"] == "PROJECT_CONTRACT_MISSING"


def test_founder_chinese_add_endpoint_with_budget_is_governed_work_not_budget_brief(ctx):
    request = (
        '在 Eason One 本身新增一個 GET /api/founder-ping endpoint，必須用真實啟動的服務做 HTTP 驗證，'
        '回傳 HTTP 200，JSON 至少包含 service="eason-one"、status="ok" 和目前 application version。'
        '要留下可追溯 Artifact 與 real loopback HTTP verification。Project 總預算 NT$5。'
    )
    routed = __import__(
        'eason_one.services.headquarters', fromlist=['route_ceo_intent']
    ).route_ceo_intent(request)
    assert routed['route'] == 'ACT'
    assert routed['route_type'] == 'AUTO_DELEGATION'
