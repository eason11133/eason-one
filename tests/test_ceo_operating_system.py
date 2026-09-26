"""Release-gated CEO operating contracts.

Slices 1-2 remain deterministic read/judgement layers. Slice 3 adds one narrow
effect boundary: existing-roster assignment for already-approved READY Work
through canonical Team Formation. It must never create provider execution,
Project TWD authority, new Work or new Employees.
"""
from decimal import Decimal
import pytest

from eason_one.extensions import db
from eason_one.models import CompanyEvent, Employee, Work, WorkAssignment
from eason_one.services import ceo_management, ceo_operating, company_events, work_runtime

from tests import test_v020_core_rebuild as core


def _work(project, operation, *, state="READY", title="CEO operating fixture"):
    employee = Employee.query.filter_by(slug="researcher").one()
    row = Work(
        project_id=project.id,
        operation_id=operation.id,
        title=title,
        purpose="Produce decision-relevant evidence.",
        expected_output="Durable evidence artifact",
        acceptance_criteria="Founder criterion",
        state=state,
        work_type="DELIVERY",
        priority="HIGH",
        created_by_employee_id=employee.id,
        resource_ceiling_twd=Decimal("5"),
        retry_limit=1,
    )
    db.session.add(row)
    db.session.flush()
    db.session.add(WorkAssignment(
        work_id=row.id,
        employee_id=employee.id,
        responsibility="OWNER",
        assigned_by_employee_id=Employee.query.filter_by(slug="ceo").one().id,
        reason="CEO operating fixture",
    ))
    db.session.commit()
    return row


def _missing_evidence_gate(work, *, basis_hash="a" * 64):
    work_runtime.open_wait(
        work,
        "DEPENDENCY",
        "Required persisted evidence is missing.",
        issue_code=f"MISSING_EVIDENCE:{basis_hash}",
        resume_state="EXECUTING",
    )
    db.session.commit()


def test_ceo_snapshot_is_deterministic_company_truth_not_provider_identity(ctx):
    project = core._project(("Founder criterion",))
    operation = core._operation(project)
    work = _work(project, operation, state="READY")

    first = ceo_operating.company_operating_snapshot()
    second = ceo_operating.company_operating_snapshot()
    row = next(item for item in first["projects"] if item["project_id"] == project.id)
    employee = next(item for item in first["employees"] if item["slug"] == "researcher")

    assert first["revision"] == second["revision"]
    assert row["operating_state"] == "READY"
    assert row["founder_priority"] == "HIGH"
    assert row["operating_priority"] == "HIGH"
    assert row["current_responsibilities"][0]["work_id"] == work.id
    assert employee["capacity"] == "PRIMARY_ASSIGNED"
    assert employee["primary_responsibility"]["work_id"] == work.id
    assert "provider" not in employee
    assert "model" not in employee


def test_ceo_canonical_missing_evidence_gate_quiesces_without_replaying_healthy_runtime(ctx):
    from eason_one.models import AgentRun
    from eason_one.services import project_company

    project = core._project(("Credible purchase-intent evidence exists",))
    operation = core._operation(project)
    work = _work(project, operation, state="EXECUTING", title="Purchase intent research")
    _missing_evidence_gate(work)

    view = ceo_operating.project_operating_view(project)
    primary = view["blockers"]["primary"]
    employee = ceo_operating.employee_capacity_view(
        Employee.query.filter_by(slug="researcher").one()
    )
    run_count = AgentRun.query.filter_by(project_id=project.id).count()
    first_card = project_company.project_card(project, include_results=False)
    second_card = project_company.project_card(project, include_results=False)

    assert view["contract_evidence"]["state"] == "NOT_PROVEN"
    assert primary["type"] == "EVIDENCE"
    assert primary["actionable_by"] == "NONE"
    assert primary["resolution_state"] == "QUIESCENT"
    assert view["operating_state"] == "QUIESCENT"
    assert employee["capacity"] == "AVAILABLE"
    assert employee["active_responsibilities"] == []
    assert employee["retained_responsibilities"][0]["work_id"] == work.id
    assert first_card["state"] == "WAITING"
    assert second_card["state"] == "WAITING"
    assert "目前缺少可驗證的既有證據" in first_card["state_detail"]
    assert "Technical: BLOCKED_MISSING_EVIDENCE" in first_card["state_detail"]
    assert AgentRun.query.filter_by(project_id=project.id).count() == run_count
    assert not any(
        item["type"] == "EXECUTION" for item in view["blockers"]["all"]
    )


def test_founder_project_surface_prefers_current_wait_over_previous_failure(ctx, client):
    from eason_one.services import project_company

    project = core._project(("Credible purchase-intent evidence exists",))
    operation = core._operation(project)
    failed = _work(project, operation, state="ABANDONED", title="Previous failed research")
    current = _work(project, operation, state="EXECUTING", title="Reconcile current evidence")
    _missing_evidence_gate(current)

    snapshot = project_company.project_snapshot(project)
    assert snapshot["card"]["state"] in {"WAITING", "RECOVERING"}
    assert snapshot["card"]["state"] != "WORKING"
    assert snapshot["focus_work"]["work"].id == current.id
    assert failed.id in {row["work"].id for row in snapshot["previous_attempts"]}
    assert failed.id not in {
        row["work"].id for stage in snapshot["handoff"]["stages"] for row in stage["nodes"]
    }

    html = client.get(f"/headquarters/projects/{project.id}").get_data(as_text=True)
    assert "Reconcile current evidence" in html
    assert "目前缺少可驗證的既有證據" in html
    assert "Technical details & audit" in html
    assert "PROJECT TEAM" in html
    assert "PROGRESS" in html
    assert "NO ACTION NEEDED" in html
    assert html.index("PROJECT TEAM") < html.index("PREVIOUS ATTEMPTS / HISTORY")
    assert "Previous failed research" in html[html.index("PREVIOUS ATTEMPTS / HISTORY"):]


def test_evidence_review_exhaustion_requires_replan_until_exact_missing_basis_is_proven(ctx):
    from eason_one.services import ceo_review

    project = core._project(("Credible purchase-intent evidence exists",))
    operation = core._operation(project)
    work = _work(project, operation, state="ABANDONED", title="Purchase intent research")
    company_events.emit(
        "WORK_EVIDENCE_REVIEW_EXHAUSTED",
        actor_type="RUNTIME",
        project_id=project.id,
        work_id=work.id,
        correlation_id=f"work:{work.id}",
        payload={"implementation_replayed": False},
        commit=True,
    )

    view = ceo_operating.project_operating_view(project)
    review = ceo_review.project_review(project)

    assert view["blockers"]["primary"]["type"] == "EVIDENCE"
    assert view["blockers"]["primary"]["actionable_by"] == "CEO"
    assert view["blockers"]["primary"]["resolution_state"] == "ACTIVE"
    assert review["recommendation"] == "REPLAN"
    assert "do not replay" in review["next_meaningful_objective"].lower()


def test_ceo_runtime_recovery_is_execution_not_fake_business_progress(ctx):
    project = core._project(("Founder criterion",))
    operation = core._operation(project)
    work = _work(project, operation, state="READY")
    work_runtime.open_wait(
        work,
        "SYSTEM_RECOVERY",
        "Provider settlement needs platform recovery.",
        issue_code="CEO-SLICE1-RECOVERY",
    )
    db.session.commit()
    company_events.emit(
        "WORK_SYSTEM_RECOVERY_AUTO_RESUMED",
        actor_type="RUNTIME",
        project_id=project.id,
        work_id=work.id,
        commit=True,
    )

    blockers = ceo_operating.classify_project_blockers(project)
    progress = ceo_operating.semantic_progress(project)

    assert blockers["primary"]["type"] == "EXECUTION"
    assert blockers["primary"]["actionable_by"] == "RUNTIME"
    assert progress["events"] == []


def test_paused_project_is_not_misclassified_as_blocker_or_employee_activity(ctx):
    project = core._project(("Founder criterion",))
    operation = core._operation(project)
    work = _work(project, operation, state="READY")
    project.status = "PAUSED"
    project.current_state_summary = "Founder paused this Project."
    db.session.commit()

    project_view = ceo_operating.project_operating_view(project)
    researcher = Employee.query.filter_by(slug="researcher").one()
    employee_view = ceo_operating.employee_capacity_view(researcher)

    assert project_view["operating_state"] == "PAUSED"
    assert project_view["blockers"]["all"] == []
    assert employee_view["capacity"] == "AVAILABLE"
    assert employee_view["active_responsibilities"] == []
    assert work.state == "READY"  # Pause preserves Work truth; projection fences activation.


def test_semantic_progress_counts_verification_and_result_but_not_activity(ctx):
    project = core._project(("Founder criterion",))
    operation = core._operation(project)
    work = _work(project, operation, state="READY")

    company_events.emit(
        "WORK_ASSIGNED", actor_type="CEO", project_id=project.id, work_id=work.id,
        commit=False,
    )
    rejected = company_events.emit(
        "ARTIFACT_REJECTED", actor_type="EMPLOYEE", project_id=project.id,
        work_id=work.id, payload={"reason": "Critic rejected v1"}, commit=False,
    )
    ready = company_events.emit(
        "PROJECT_RESULT_READY", actor_type="RUNTIME", project_id=project.id,
        payload={"proof": "fixture"}, commit=True,
    )

    progress = ceo_operating.semantic_progress(project)

    assert [item["event_id"] for item in progress["events"]] == [rejected.id, ready.id]
    assert [item["progress_type"] for item in progress["events"]] == [
        "VERIFICATION_ADVANCED", "RESULT_ADVANCED"
    ]
    assert progress["latest"]["event_id"] == ready.id
    assert CompanyEvent.query.filter_by(project_id=project.id, event_type="WORK_ASSIGNED").count() == 1


def test_ceo_trigger_policy_ignores_runtime_noise_and_reviews_material_change(ctx):
    from eason_one.services import ceo_review

    project = core._project(("Founder criterion",))
    operation = core._operation(project)
    work = _work(project, operation, state="READY")
    noise = company_events.emit(
        "WORK_SYSTEM_RECOVERY_AUTO_RESUMED", actor_type="RUNTIME",
        project_id=project.id, work_id=work.id, commit=False,
    )
    material = company_events.emit(
        "ARTIFACT_REJECTED", actor_type="EMPLOYEE",
        project_id=project.id, work_id=work.id,
        payload={"artifact_version_id": 123}, commit=True,
    )

    assert ceo_review.classify_event(noise)["disposition"] == "IGNORE"
    result = ceo_review.classify_event(material)
    assert result["disposition"] == "PROJECT_REVIEW"
    assert result["reason"] == "CRITIC_REJECTED"
    assert result["dedupe_key"] == ceo_review.classify_event(material)["dedupe_key"]


def test_work_review_treats_rejected_artifact_as_repair_not_founder_problem(ctx):
    from eason_one.models import Artifact, ArtifactVersion
    from eason_one.services import ceo_review

    project = core._project(("Founder criterion",))
    operation = core._operation(project)
    work = _work(project, operation, state="EXECUTING")
    artifact = Artifact(
        project_id=project.id, work_id=work.id,
        artifact_type="WORK_RESULT", title="Rejected MVP",
    )
    db.session.add(artifact); db.session.flush()
    version = ArtifactVersion(
        artifact_id=artifact.id, version=1,
        producer_employee_id=Employee.query.filter_by(slug="researcher").one().id,
        status="REJECTED", content_text="bad mobile interaction",
        content_hash="b" * 64,
    )
    db.session.add(version); db.session.commit()

    result = ceo_review.work_review(work)

    assert result["outcome"] == "REPAIR_WORK"
    assert result["blocker"] is None
    assert result["intended_actions"] == []


def test_project_review_quiesces_exact_missing_evidence_basis_instead_of_creating_busywork(ctx):
    from eason_one.services import ceo_review

    project = core._project(("Credible purchase-intent evidence exists",))
    operation = core._operation(project)
    work = _work(project, operation, state="EXECUTING", title="Purchase intent research")
    _missing_evidence_gate(work)

    result = ceo_review.project_review(project)

    assert result["recommendation"] == "QUIESCE"
    assert result["primary_blocker"]["type"] == "EVIDENCE"
    assert result["primary_blocker"]["actionable_by"] == "NONE"
    assert result["founder_escalation"] is None


def test_project_review_escalates_real_founder_authority_without_guessing(ctx):
    from eason_one.models import Escalation
    from eason_one.services import ceo_review

    project = core._project(("Founder criterion",))
    ceo = Employee.query.filter_by(slug="ceo").one()
    row = Escalation(
        project_id=project.id,
        escalation_type="BUDGET_AUTHORIZATION",
        state="OPEN",
        reason="A material next step requires TWD 3 beyond current Project authority.",
        options_json=[{"action": "APPROVE", "additional_budget_twd": "3"}],
        recommendation="Approve only if the Founder still wants this evidence path.",
        created_by_employee_id=ceo.id,
    )
    db.session.add(row); db.session.commit()

    result = ceo_review.project_review(project)

    assert result["recommendation"] == "ESCALATE"
    assert result["primary_blocker"]["type"] == "AUTHORITY"
    assert result["primary_blocker"]["actionable_by"] == "FOUNDER"
    assert result["founder_escalation"] is not None


def test_ceo_review_never_runs_on_paused_project(ctx):
    from eason_one.services import ceo_review

    project = core._project(("Founder criterion",))
    operation = core._operation(project)
    work = _work(project, operation, state="READY")
    project.status = "PAUSED"
    db.session.commit()
    event = company_events.emit(
        "ARTIFACT_REJECTED", actor_type="EMPLOYEE",
        project_id=project.id, work_id=work.id, commit=True,
    )

    assert ceo_review.classify_event(event)["disposition"] == "IGNORE"
    import pytest
    with pytest.raises(ValueError, match="PROJECT_REVIEW_FORBIDDEN_PAUSED"):
        ceo_review.project_review(project)


def _unassigned_declared_work(project, operation, capability, *, title="Unassigned governed work"):
    employee = Employee.query.filter_by(slug="ceo").one()
    row = Work(
        project_id=project.id,
        operation_id=operation.id,
        title=title,
        purpose=f"Produce a bounded {capability} outcome.",
        expected_output="Durable governed artifact",
        acceptance_criteria="Founder criterion",
        state="READY",
        work_type="DELIVERY",
        priority="HIGH",
        created_by_employee_id=employee.id,
        resource_ceiling_twd=Decimal("5"),
        retry_limit=1,
        runtime_control_json={
            "staffing_requirements": {"required_capabilities": [capability]}
        },
    )
    db.session.add(row)
    db.session.commit()
    return row


def test_portfolio_keeps_founder_priority_separate_from_operating_focus(ctx):
    from eason_one.services import ceo_management

    strategic = core._project(("Credible purchase-intent evidence exists",))
    strategic.priority = "CRITICAL"
    strategic_operation = core._operation(strategic)
    strategic_work = _work(
        strategic, strategic_operation, state="EXECUTING", title="Purchase intent research"
    )
    _missing_evidence_gate(strategic_work)

    executable = core._project(("Usable MVP exists",))
    executable.priority = "MEDIUM"
    executable_operation = core._operation(executable)
    _unassigned_declared_work(
        executable, executable_operation, "SOFTWARE_ENGINEERING", title="Build usable MVP"
    )
    db.session.commit()

    review = ceo_management.portfolio_review()
    by_id = {row["project_id"]: row for row in review["project_operating_order"]}
    focus_ids = [row["project_id"] for row in review["operating_focus"]]

    assert by_id[strategic.id]["founder_priority"] == "CRITICAL"
    assert by_id[strategic.id]["operating_state"] == "QUIESCENT"
    assert by_id[strategic.id]["resource_executable"] is False
    assert executable.id in focus_ids
    assert strategic.id not in focus_ids


def test_quiescent_retained_responsibility_releases_capacity_for_distinct_ready_work(ctx):
    from eason_one.services import ceo_management

    waiting = core._project(("Credible purchase-intent evidence exists",))
    waiting_operation = core._operation(waiting)
    waiting_work = _work(waiting, waiting_operation, state="EXECUTING", title="Old evidence path")
    _missing_evidence_gate(waiting_work)

    ready = core._project(("New research result exists",))
    ready_operation = core._operation(ready)
    ready_work = _unassigned_declared_work(
        ready, ready_operation, "RESEARCH", title="Distinct decision-relevant research"
    )

    snapshot = ceo_operating.company_operating_snapshot()
    researcher = next(row for row in snapshot["employees"] if row["slug"] == "researcher")
    review = ceo_management.portfolio_review(snapshot)
    proposals = [
        row for row in review["allocation_decisions"] if row["work_id"] == ready_work.id
    ]

    assert researcher["capacity"] == "AVAILABLE"
    assert researcher["active_responsibilities"] == []
    assert researcher["retained_responsibilities"][0]["work_id"] == waiting_work.id
    assert len(proposals) == 1
    assert proposals[0]["employee_id"] == researcher["employee_id"]
    assert proposals[0]["required_capability"] == "RESEARCH"
    assert proposals[0]["effect_authorized"] is False


def test_available_capacity_does_not_manufacture_work_or_operating_focus(ctx):
    from eason_one.services import ceo_management

    project = core._project(("Credible purchase-intent evidence exists",))
    operation = core._operation(project)
    work = _work(project, operation, state="EXECUTING", title="Purchase intent research")
    _missing_evidence_gate(work)

    work_count_before = Work.query.count()
    review = ceo_management.portfolio_review()
    plan = ceo_management.company_plan(review=review)

    assert review["operating_focus"] == []
    assert review["allocation_decisions"] == []
    assert plan["operating_focus"] == []
    assert Work.query.count() == work_count_before


def test_company_plan_persistence_is_deduped_projection_not_domain_mutation(ctx):
    from eason_one.services import ceo_management

    project = core._project(("Credible purchase-intent evidence exists",))
    operation = core._operation(project)
    work = _work(project, operation, state="EXECUTING")
    _missing_evidence_gate(work)

    plan = ceo_management.company_plan()
    first = ceo_management.persist_company_plan(plan)
    second = ceo_management.persist_company_plan(plan)
    rows = CompanyEvent.query.filter_by(event_type=ceo_management.PLAN_EVENT_TYPE).all()

    assert first["status"] == "PERSISTED"
    assert second["status"] == "UNCHANGED"
    assert len(rows) == 1
    assert rows[0].payload_json["domain_truth_mutated"] is False
    assert rows[0].payload_json["plan_hash"] == plan["plan_hash"]


def test_ceo_staffing_intent_validation_authorizes_only_canonical_existing_roster_path(ctx):
    from eason_one.services import ceo_management

    project = core._project(("Decision-relevant research exists",))
    operation = core._operation(project)
    work = _unassigned_declared_work(project, operation, "RESEARCH")

    snapshot = ceo_operating.company_operating_snapshot()
    review = ceo_management.portfolio_review(snapshot)
    proposal = next(row for row in review["allocation_decisions"] if row["work_id"] == work.id)
    intent = ceo_management.proposal_to_action_intent(
        proposal, basis_revision=str(snapshot["revision"])
    )
    validated = ceo_management.validate_action_intent(intent)

    assert validated["status"] == "VALIDATED"
    assert validated["effect_authorized"] is True
    assert validated["reason"] == "CANONICAL_TEAM_FORMATION_EXISTING_ROSTER_AUTHORITY"
    assert WorkAssignment.query.filter_by(work_id=work.id, ended_at=None).count() == 0


def test_ceo_staffing_intent_executes_atomically_and_replay_uses_durable_receipt(ctx):
    from eason_one.services import ceo_management

    project = core._project(("Decision-relevant research exists",))
    operation = core._operation(project)
    work = _unassigned_declared_work(project, operation, "RESEARCH")

    snapshot = ceo_operating.company_operating_snapshot()
    review = ceo_management.portfolio_review(snapshot)
    proposal = next(row for row in review["allocation_decisions"] if row["work_id"] == work.id)
    intent = ceo_management.proposal_to_action_intent(
        proposal, basis_revision=str(snapshot["revision"])
    )

    first = ceo_management.execute_action_intent(intent)
    second = ceo_management.execute_action_intent(intent)
    assignments = WorkAssignment.query.filter_by(work_id=work.id, ended_at=None).all()
    receipts = CompanyEvent.query.filter_by(
        event_type=ceo_management.ACTION_RECEIPT_EVENT_TYPE,
        correlation_id=intent["intent_id"],
    ).all()
    from eason_one.models import Decision
    decisions = Decision.query.filter_by(
        project_id=project.id,
        work_id=work.id,
        decided_by_employee_id=Employee.query.filter_by(slug="ceo").one().id,
        state="COMMITTED",
    ).all()

    assert first["status"] == "APPLIED"
    assert first["replayed_from_receipt"] is False
    assert first["provider_execution_created"] is False
    assert first["project_twd_authority_changed"] is False
    assert first["new_work_created"] is False
    assert first["new_employee_created"] is False
    assert len(assignments) == 1
    assert assignments[0].employee_id == proposal["employee_id"]
    assert len(receipts) == 1
    assert len(decisions) == 1
    assert first["decision_id"] == decisions[0].id
    assert receipts[0].decision_id == decisions[0].id
    assert second["receipt_event_id"] == first["receipt_event_id"]
    assert second["decision_id"] == first["decision_id"]
    assert second["replayed_from_receipt"] is True

    control = dict(db.session.get(Work, work.id).runtime_control_json or {})
    staffing = dict(control.get("team_formation") or {})
    assert staffing["state"] == "REASSIGNED"
    assert staffing["allocation_source"] == "CEO_PORTFOLIO"
    assert staffing["ceo_intent_id"] == intent["intent_id"]
    assert staffing["selection_evidence"]["required_capability"] == "RESEARCH"


def test_ceo_staffing_intent_stales_if_selected_employee_becomes_busy_before_effect(ctx):
    from eason_one.services import ceo_management

    target_project = core._project(("Decision-relevant research exists",))
    target_operation = core._operation(target_project)
    target_work = _unassigned_declared_work(target_project, target_operation, "RESEARCH")

    snapshot = ceo_operating.company_operating_snapshot()
    review = ceo_management.portfolio_review(snapshot)
    proposal = next(row for row in review["allocation_decisions"] if row["work_id"] == target_work.id)
    intent = ceo_management.proposal_to_action_intent(
        proposal, basis_revision=str(snapshot["revision"])
    )

    busy_project = core._project(("A separate research outcome exists",))
    busy_operation = core._operation(busy_project)
    busy_work = Work(
        project_id=busy_project.id,
        operation_id=busy_operation.id,
        title="Newer active responsibility",
        purpose="Consume the selected Researcher's operating capacity.",
        expected_output="Evidence",
        acceptance_criteria="Founder criterion",
        state="EXECUTING",
        work_type="DELIVERY",
        priority="HIGH",
        created_by_employee_id=Employee.query.filter_by(slug="ceo").one().id,
        resource_ceiling_twd=Decimal("5"),
        retry_limit=1,
    )
    db.session.add(busy_work); db.session.flush()
    db.session.add(WorkAssignment(
        work_id=busy_work.id,
        employee_id=proposal["employee_id"],
        responsibility="OWNER",
        assigned_by_employee_id=Employee.query.filter_by(slug="ceo").one().id,
        reason="Newer durable company truth",
    ))
    db.session.commit()

    result = ceo_management.execute_action_intent(intent)

    assert result["status"] == "STALE"
    assert result["effect_authorized"] is False
    assert result["reason"] in {"NO_AVAILABLE_ELIGIBLE_EMPLOYEE", "SELECTION_BASIS_CHANGED"}
    assert WorkAssignment.query.filter_by(work_id=target_work.id, ended_at=None).count() == 0
    assert CompanyEvent.query.filter_by(
        event_type=ceo_management.ACTION_RECEIPT_EVENT_TYPE,
        correlation_id=intent["intent_id"],
    ).count() == 0


def test_ceo_runtime_cutover_is_future_only_and_does_not_replay_historical_events(ctx):
    from eason_one.services import ceo_runtime

    project = core._project(("Founder criterion",))
    operation = core._operation(project)
    work = _work(project, operation, state="ACCEPTED")
    historical = company_events.emit(
        "WORK_ACCEPTED",
        actor_type="RUNTIME",
        project_id=project.id,
        work_id=work.id,
        commit=True,
    )

    cutover = ceo_runtime.ensure_cutover()

    assert cutover["status"] == "CUTOVER_ESTABLISHED"
    assert cutover["source_event_id"] >= historical.id
    assert CompanyEvent.query.filter_by(
        event_type=ceo_runtime.PROJECT_REVIEW_EVENT_TYPE
    ).count() == 0
    plans = CompanyEvent.query.filter_by(
        event_type=ceo_management.PLAN_EVENT_TYPE
    ).all()
    assert len(plans) == 1
    assert plans[0].payload_json["plan"]["schema"] == "CEO_COMPANY_PLAN_V1"
    assert plans[0].payload_json["domain_truth_mutated"] is False
    checkpoint_event = db.session.get(CompanyEvent, cutover["event_id"])
    assert checkpoint_event.payload_json["provider_calls"] == 0

    future = company_events.emit(
        "WORK_ACCEPTED",
        actor_type="RUNTIME",
        project_id=project.id,
        work_id=work.id,
        payload={"version": "future-1"},
        commit=True,
    )
    result = ceo_runtime.process_pending_events(max_events=8)

    assert result["status"] == "PROCESSED"
    assert result["source_event_id"] == future.id
    assert result["material_reviews"] == 1
    reviews = CompanyEvent.query.filter_by(
        event_type=ceo_runtime.PROJECT_REVIEW_EVENT_TYPE
    ).all()
    assert len(reviews) == 1
    assert reviews[0].causation_id == future.id
    assert reviews[0].payload_json["provider_calls"] == 0


def test_ceo_runtime_can_allocate_quiescent_released_capacity_without_provider_or_new_work(ctx):
    from eason_one.models import AgentRun, CostEvent
    from eason_one.services import ceo_management, ceo_runtime

    waiting = core._project(("Credible purchase-intent evidence exists",))
    waiting_operation = core._operation(waiting)
    waiting_work = _work(waiting, waiting_operation, state="EXECUTING", title="Old evidence path")
    _missing_evidence_gate(waiting_work)

    ready = core._project(("New research result exists",))
    ready_operation = core._operation(ready)
    ready_work = _unassigned_declared_work(
        ready, ready_operation, "RESEARCH", title="New decision-relevant research"
    )

    ceo_runtime.ensure_cutover()
    work_count = Work.query.count()
    run_count = AgentRun.query.count()
    cost_count = CostEvent.query.count()
    trigger = company_events.emit(
        "WORK_STAFFING_REPLAN_REQUIRED",
        actor_type="RUNTIME",
        project_id=ready.id,
        work_id=ready_work.id,
        payload={"version": "staffing-1"},
        commit=True,
    )

    result = ceo_runtime.process_pending_events(max_events=8)

    assignment = WorkAssignment.query.filter_by(work_id=ready_work.id, ended_at=None).one()
    receipts = CompanyEvent.query.filter_by(
        event_type=ceo_management.ACTION_RECEIPT_EVENT_TYPE
    ).all()
    assert result["status"] == "PROCESSED"
    assert result["source_event_id"] == trigger.id
    assert result["applied_actions"] == 1
    assert assignment.employee.slug == "researcher"
    assert Work.query.count() == work_count
    assert AgentRun.query.count() == run_count
    assert CostEvent.query.count() == cost_count
    assert len(receipts) == 1
    assert receipts[0].payload_json["provider_execution_created"] is False
    assert receipts[0].payload_json["project_twd_authority_changed"] is False

    # The action emits ordinary organizational events after the source cursor.
    # A later cycle may observe/ignore them, but it must not replay allocation.
    ceo_runtime.process_pending_events(max_events=8)
    assert WorkAssignment.query.filter_by(work_id=ready_work.id, ended_at=None).count() == 1
    assert CompanyEvent.query.filter_by(
        event_type=ceo_management.ACTION_RECEIPT_EVENT_TYPE
    ).count() == 1


def test_ceo_runtime_respects_pause_fence_and_does_not_allocate(ctx):
    from eason_one.services import ceo_runtime

    project = core._project(("Founder criterion",))
    operation = core._operation(project)
    work = _unassigned_declared_work(project, operation, "RESEARCH")
    project.status = "PAUSED"
    db.session.commit()

    ceo_runtime.ensure_cutover()
    company_events.emit(
        "WORK_STAFFING_REPLAN_REQUIRED",
        actor_type="RUNTIME",
        project_id=project.id,
        work_id=work.id,
        payload={"version": "paused-1"},
        commit=True,
    )
    result = ceo_runtime.process_pending_events(max_events=8)

    assert result["status"] == "PROCESSED"
    assert result["material_reviews"] == 0
    assert result["applied_actions"] == 0
    assert WorkAssignment.query.filter_by(work_id=work.id, ended_at=None).count() == 0


def test_terminal_project_releases_employee_capacity_without_erasing_assignment_history(ctx):
    project = core._project(("Founder criterion",))
    operation = core._operation(project)
    work = _work(project, operation, state="EXECUTING", title="Still-open historical work")
    assignment = WorkAssignment.query.filter_by(work_id=work.id, ended_at=None).one()
    researcher = Employee.query.filter_by(slug="researcher").one()

    project.status = "COMPLETED"
    db.session.commit()

    employee_view = ceo_operating.employee_capacity_view(researcher)
    project_view = ceo_operating.project_operating_view(project)

    assert employee_view["capacity"] == "AVAILABLE"
    assert employee_view["active_responsibilities"] == []
    assert project_view["current_responsibilities"] == []
    assert WorkAssignment.query.filter_by(id=assignment.id).one().ended_at is None
    assert Work.query.get(work.id).state == "EXECUTING"


def test_employee_capacity_groups_multiple_queued_works_by_one_active_project(ctx):
    project = core._project(("Founder criterion",))
    operation = core._operation(project)
    first = _work(project, operation, state="EXECUTING", title="Current research")
    second = _work(project, operation, state="READY", title="Queued evidence review")
    researcher = Employee.query.filter_by(slug="researcher").one()

    capacity = ceo_operating.employee_capacity_view(researcher)

    assert capacity["capacity"] == "PRIMARY_ASSIGNED"
    assert capacity["active_project_id"] == project.id
    assert capacity["current_responsibility"]["work_id"] == first.id
    assert [row["work_id"] for row in capacity["queued_responsibilities"]] == [second.id]


def test_employee_capacity_exposes_cross_project_double_assignment_as_inconsistent(ctx):
    first_project = core._project(("First criterion",))
    second_project = core._project(("Second criterion",))
    first = _work(first_project, core._operation(first_project), state="READY", title="First Project Work")
    second = _work(second_project, core._operation(second_project), state="READY", title="Second Project Work")
    researcher = Employee.query.filter_by(slug="researcher").one()

    capacity = ceo_operating.employee_capacity_view(researcher)

    assert capacity["capacity"] == "OVERCOMMITTED"
    assert capacity["assignment_conflict"] is True
    assert capacity["active_project_ids"] == [first_project.id, second_project.id]
    assert {row["work_id"] for row in capacity["active_responsibilities"]} == {first.id, second.id}


def test_reassignment_cannot_bind_employee_to_second_active_project(ctx):
    first_project = core._project(("First criterion",))
    _work(first_project, core._operation(first_project), state="READY", title="First Project Work")
    second_project = core._project(("Second criterion",))
    second_operation = core._operation(second_project)
    employee = Employee.query.filter_by(slug="researcher").one()
    unassigned = Work(
        project_id=second_project.id,
        operation_id=second_operation.id,
        title="Second Project Work",
        purpose="Produce decision-relevant evidence.",
        expected_output="Evidence",
        acceptance_criteria="Founder criterion",
        state="READY",
        work_type="DELIVERY",
        priority="HIGH",
        created_by_employee_id=Employee.query.filter_by(slug="ceo").one().id,
        resource_ceiling_twd=Decimal("5"),
        retry_limit=1,
    )
    db.session.add(unassigned)
    db.session.commit()

    with pytest.raises(ValueError, match="EMPLOYEE_ACTIVE_PROJECT_CONFLICT"):
        work_runtime.reassign(
            unassigned,
            employee.id,
            assigned_by_employee_id=Employee.query.filter_by(slug="ceo").one().id,
            reason="Must respect Project capacity.",
        )


def test_founder_project_snapshot_projects_semantic_progress_and_employee_queue(ctx):
    from eason_one.services import project_company

    project = core._project(("Founder criterion",))
    operation = core._operation(project)
    current = _work(project, operation, state="EXECUTING", title="Reconcile market evidence")
    queued = _work(project, operation, state="READY", title="Review evidence quality")
    _missing_evidence_gate(current)

    snapshot = project_company.project_snapshot(project)
    researcher = next(row for row in snapshot["project_team"] if row["employee"].slug == "researcher")

    assert snapshot["semantic_progress"]["current_phase"] == "Evidence"
    assert snapshot["semantic_progress"]["show_percent"] is False
    assert researcher["status"] == "WAITING"
    assert researcher["current_work"].id == current.id
    assert researcher["next_work"].id == queued.id
    assert snapshot["founder_blocker"]
    assert "BLOCKED_MISSING_EVIDENCE" not in snapshot["founder_blocker"]


def test_project_team_and_hq_employee_presence_share_waiting_work_truth(ctx):
    from eason_one.services import headquarters, project_company

    project = core._project(("Founder criterion",))
    operation = core._operation(project)
    work = _work(project, operation, state="EXECUTING", title="Reconcile evidence")
    _missing_evidence_gate(work)
    employee = Employee.query.filter_by(slug="researcher").one()

    project_view = project_company.project_snapshot(project)
    team_row = next(row for row in project_view["project_team"] if row["employee"].id == employee.id)
    hq_row = headquarters.employee_presence(employee)

    assert team_row["status"] == "WAITING"
    assert hq_row["state"] == "WAITING"
    assert hq_row["project"].id == project.id
    assert hq_row["work"].id == work.id
    assert hq_row["current_activity"] == team_row["current_work"].title


def test_project_closure_captures_authoritative_outcome_learning_and_contribution_without_mutation(ctx):
    from eason_one.models import ContributionEvent, EmployeeLearningRecord
    from eason_one.services import ceo_learning, ceo_runtime

    project = core._project(("Founder criterion",))
    operation = core._operation(project)
    work = _work(project, operation, state="ACCEPTED", title="Accepted closure work")
    researcher = Employee.query.filter_by(slug="researcher").one()

    learning = EmployeeLearningRecord(
        employee_id=researcher.id,
        project_id=project.id,
        work_id=work.id,
        learning_type="WORK_EXPERIENCE",
        validation_basis="CANONICAL_WORK_ACCEPTANCE",
        title="Accepted research experience",
        content="Accepted outcome-backed experience.",
        source_ref=f"WORK:{work.id}",
        evidence_json={"capability": "RESEARCH", "work_id": work.id},
        validated=True,
    )
    contribution = ContributionEvent(
        employee_id=researcher.id,
        project_id=project.id,
        scope="PROJECT",
        event_type="ACCEPTED_CONTRIBUTION",
        value=Decimal("1"),
        reason="Accepted outcome-backed contribution.",
        related_reference=f"work:{work.id}",
        status="CONFIRMED",
    )
    db.session.add_all([learning, contribution])
    db.session.commit()

    ceo_runtime.ensure_cutover()
    project.status = "COMPLETED"
    db.session.commit()
    terminal = company_events.emit(
        "FOUNDER_PROJECT_COMPLETED",
        actor_type="FOUNDER",
        project_id=project.id,
        payload={"fixture": "closure-v1"},
        commit=True,
    )

    result = ceo_runtime.process_pending_events(max_events=8)
    closures = CompanyEvent.query.filter_by(
        event_type=ceo_learning.PROJECT_CLOSURE_EVENT_TYPE,
        project_id=project.id,
    ).all()

    assert result["closures_captured"] == 1
    assert len(closures) == 1
    assert closures[0].causation_id == terminal.id
    payload = closures[0].payload_json
    assert payload["terminal_status"] == "COMPLETED"
    assert payload["accepted_work_ids"] == [work.id]
    assert learning.id in payload["canonical_employee_learning_record_ids"]
    assert contribution.id in payload["contribution_event_ids"]
    assert payload["provider_calls"] == 0
    assert payload["domain_truth_mutated"] is False
    assert payload["learning_policy"].startswith("Closure captures authoritative outcomes only")
    assert WorkAssignment.query.filter_by(work_id=work.id, ended_at=None).count() == 1

    # Closure events are internal CEO projections and cannot replay themselves.
    second = ceo_runtime.process_pending_events(max_events=8)
    assert second["closures_captured"] == 0
    assert CompanyEvent.query.filter_by(
        event_type=ceo_learning.PROJECT_CLOSURE_EVENT_TYPE,
        project_id=project.id,
    ).count() == 1


def test_ceo_future_only_cutover_does_not_backfill_historical_project_closure(ctx):
    from eason_one.services import ceo_learning, ceo_runtime

    project = core._project(("Founder criterion",))
    project.status = "COMPLETED"
    db.session.commit()
    historical = company_events.emit(
        "FOUNDER_PROJECT_COMPLETED",
        actor_type="FOUNDER",
        project_id=project.id,
        payload={"fixture": "historical"},
        commit=True,
    )

    cutover = ceo_runtime.ensure_cutover()

    assert cutover["source_event_id"] >= historical.id
    assert CompanyEvent.query.filter_by(
        event_type=ceo_learning.PROJECT_CLOSURE_EVENT_TYPE,
        project_id=project.id,
    ).count() == 0


def test_ceo_closure_observes_staffing_action_outcome_without_claiming_causality(ctx):
    from eason_one.services import ceo_learning, ceo_management

    project = core._project(("Decision-relevant research exists",))
    operation = core._operation(project)
    work = _unassigned_declared_work(project, operation, "RESEARCH", title="Outcome-linked staffing work")

    snapshot = ceo_operating.company_operating_snapshot()
    review = ceo_management.portfolio_review(snapshot)
    proposal = next(row for row in review["allocation_decisions"] if row["work_id"] == work.id)
    intent = ceo_management.proposal_to_action_intent(
        proposal, basis_revision=str(snapshot["revision"])
    )
    applied = ceo_management.execute_action_intent(intent)
    assert applied["status"] == "APPLIED"

    work.state = "ACCEPTED"
    project.status = "COMPLETED"
    db.session.commit()

    facts = ceo_learning.project_closure_facts(project)
    outcome = next(row for row in facts["ceo_action_outcomes"] if row["intent_id"] == intent["intent_id"])

    assert outcome["outcome"] == "OBSERVED_ACCEPTED"
    assert outcome["observed_work_state"] == "ACCEPTED"
    assert outcome["causal_claim"] is False

    captured = ceo_learning.capture_project_closure(project)
    replay = ceo_learning.capture_project_closure(project)
    from eason_one.models import EmployeeLearningRecord
    candidates = EmployeeLearningRecord.query.filter_by(
        employee_id=Employee.query.filter_by(slug="ceo").one().id,
        project_id=project.id,
        learning_type="CEO_DECISION_OUTCOME_CANDIDATE",
    ).all()
    assert captured["status"] == "CAPTURED"
    assert replay["status"] == "UNCHANGED"
    assert len(candidates) == 1
    assert candidates[0].validated is False
    assert candidates[0].validation_basis == "CEO_ACTION_OUTCOME_OBSERVATION"
    assert candidates[0].evidence_json["causal_claim"] is False
    assert captured["ceo_candidate_learning_record_ids"] == [candidates[0].id]


def test_canonical_accepted_work_creates_one_outcome_backed_contribution_on_replay(ctx):
    from eason_one.models import Artifact, ArtifactVersion, ContributionEvent, VerificationRecord
    from eason_one.services import employee_memory

    project = core._project(("Founder criterion",))
    operation = core._operation(project)
    work = _work(project, operation, state="ACCEPTED", title="Outcome-backed contribution work")
    researcher = Employee.query.filter_by(slug="researcher").one()
    artifact = Artifact(
        project_id=project.id,
        work_id=work.id,
        artifact_type="WORK_RESULT",
        title="Accepted output",
    )
    db.session.add(artifact); db.session.flush()
    version = ArtifactVersion(
        artifact_id=artifact.id,
        version=1,
        producer_employee_id=researcher.id,
        status="ACCEPTED",
        content_text="accepted",
        content_hash="c" * 64,
    )
    db.session.add(version); db.session.flush()
    acceptance = VerificationRecord(
        work_id=work.id,
        artifact_version_id=version.id,
        method="ACCEPTANCE_CONTRACT",
        status="PASSED",
        details_json={"proof": True},
    )
    db.session.add(acceptance); db.session.flush()

    employee_memory.capture_accepted_work_experience(work, version, acceptance)
    employee_memory.capture_accepted_work_experience(work, version, acceptance)
    db.session.flush()

    rows = ContributionEvent.query.filter_by(
        employee_id=researcher.id,
        project_id=project.id,
        event_type="CANONICAL_WORK_ACCEPTED",
    ).all()
    assert len(rows) == 1
    assert rows[0].status == "CONFIRMED"
    assert f"WORK:{work.id}" in rows[0].related_reference
    assert f"VERSION:{version.id}" in rows[0].related_reference
    assert f"VerificationRecord #{acceptance.id}" in rows[0].reason


def test_ceo_runtime_captures_founder_cancelled_project_closure_future_only(ctx):
    from eason_one.services import ceo_learning, ceo_runtime

    project = core._project(("Founder criterion",))
    ceo_runtime.ensure_cutover()
    project.status = "CANCELLED"
    db.session.commit()
    cancelled = company_events.emit(
        "PROJECT_CANCELLED_BY_FOUNDER",
        actor_type="FOUNDER",
        project_id=project.id,
        payload={"fixture": "cancelled"},
        commit=True,
    )

    result = ceo_runtime.process_pending_events(max_events=8)
    closure = CompanyEvent.query.filter_by(
        event_type=ceo_learning.PROJECT_CLOSURE_EVENT_TYPE,
        project_id=project.id,
    ).one()

    assert result["closures_captured"] == 1
    assert closure.causation_id == cancelled.id
    assert closure.payload_json["terminal_status"] == "CANCELLED"
    assert closure.payload_json["provider_calls"] == 0


def test_ceo_decision_outcome_candidate_requires_explicit_policy_promotion(ctx):
    from eason_one.models import EmployeeLearningRecord
    from eason_one.services import ceo_learning

    ceo = Employee.query.filter_by(slug="ceo").one()
    candidate = EmployeeLearningRecord(
        employee_id=ceo.id,
        learning_type="CEO_DECISION_OUTCOME_CANDIDATE",
        validation_basis="CEO_ACTION_OUTCOME_OBSERVATION",
        title="Observed staffing outcome",
        content=(
            "A staffing action was followed by accepted Work. This observation alone "
            "does not establish causal reusable policy."
        ),
        source_ref="CEO_ACTION_OUTCOME:test-safe-promotion",
        evidence_json={
            "schema": "ceo-decision-outcome-candidate-v1",
            "scope": "ROLE",
            "outcome": "OBSERVED_ACCEPTED",
            "observed_basis_hash": "b" * 64,
            "causal_claim": False,
        },
        validated=False,
    )
    db.session.add(candidate)
    db.session.commit()

    # The legacy generic Founder validation route must still not turn a factual
    # decision outcome into active policy. Promotion is a distinct governed act.
    ceo_learning.validate_learning(candidate)
    _, before = ceo_learning.active_policy_context(ceo)
    assert candidate.id not in before["validated_learning_ids"]

    promoted = ceo_learning.promote_ceo_learning(
        candidate,
        scope="ROLE",
        policy_statement=(
            "When a higher-priority Project is provably quiescent, available capacity may be "
            "temporarily used on a distinct executable Project without changing strategic priority."
        ),
        founder_authorized=True,
    )
    replay = ceo_learning.promote_ceo_learning(
        candidate,
        scope="ROLE",
        policy_statement="Replay must return the same promotion row.",
        founder_authorized=True,
    )
    context, after = ceo_learning.active_policy_context(ceo)

    assert replay.id == promoted.id
    assert promoted.id in after["validated_learning_ids"]
    assert candidate.id not in after["validated_learning_ids"]
    assert promoted.content in context
    assert after["authority_effect"] is False
    assert after["budget_effect"] is False
    assert after["current_fact_effect"] is False


def test_ceo_policy_supersession_preserves_history_and_removes_old_active_memory(ctx):
    import pytest
    from eason_one.models import EmployeeLearningRecord
    from eason_one.services import ceo_learning

    ceo = Employee.query.filter_by(slug="ceo").one()
    old = EmployeeLearningRecord(
        employee_id=ceo.id,
        learning_type="CEO_POLICY_CANDIDATE",
        title="Old policy",
        content="Use old allocation precedent.",
        source_ref="CEO-EPISODE-old-policy",
        validated=False,
    )
    replacement = EmployeeLearningRecord(
        employee_id=ceo.id,
        learning_type="CEO_POLICY_CANDIDATE",
        title="Replacement policy",
        content="Use newer evidence-conditioned allocation precedent.",
        source_ref="CEO-EPISODE-new-policy",
        validated=False,
    )
    db.session.add_all([old, replacement])
    db.session.commit()
    ceo_learning.validate_learning(old, commit=False)
    ceo_learning.validate_learning(replacement, commit=True)

    ceo_learning.supersede_ceo_policy_learning(
        old,
        replacement,
        reason="Newer Founder-approved evidence narrowed the operating condition.",
        founder_authorized=True,
    )
    context, metadata = ceo_learning.active_policy_context(ceo)

    assert old.id not in metadata["validated_learning_ids"]
    assert replacement.id in metadata["validated_learning_ids"]
    assert "Replacement policy" in context
    assert db.session.get(EmployeeLearningRecord, old.id).evidence_json["superseded_by_learning_id"] == replacement.id
    with pytest.raises(ValueError, match="CANNOT_SUPERSEDE_ITSELF"):
        ceo_learning.supersede_ceo_policy_learning(
            replacement,
            replacement,
            reason="invalid",
            founder_authorized=True,
        )


def test_quiescent_resume_surfaces_employee_overcommitment_without_silent_reassignment(ctx):
    from eason_one.services import ceo_management, ceo_review

    waiting_project = core._project(("Credible purchase-intent evidence exists",))
    waiting_operation = core._operation(waiting_project)
    waiting_work = _work(
        waiting_project,
        waiting_operation,
        state="EXECUTING",
        title="Retained quiescent responsibility",
    )
    basis = "a" * 64
    _missing_evidence_gate(waiting_work, basis_hash=basis)

    active_project = core._project(("A distinct research result exists",))
    active_operation = core._operation(active_project)
    active_work = _unassigned_declared_work(
        active_project,
        active_operation,
        "RESEARCH",
        title="Current active research responsibility",
    )

    snapshot = ceo_operating.company_operating_snapshot()
    review = ceo_management.portfolio_review(snapshot)
    proposal = next(row for row in review["allocation_decisions"] if row["work_id"] == active_work.id)
    intent = ceo_management.proposal_to_action_intent(
        proposal, basis_revision=str(snapshot["revision"])
    )
    applied = ceo_management.execute_action_intent(intent)
    assert applied["status"] == "APPLIED"

    researcher = Employee.query.filter_by(slug="researcher").one()
    before = ceo_operating.employee_capacity_view(researcher)
    assert before["capacity"] == "PRIMARY_ASSIGNED"
    assert before["primary_responsibility"]["work_id"] == active_work.id
    assert before["retained_responsibilities"][0]["work_id"] == waiting_work.id

    # New legitimate evidence removes the exact quiescence gate. The historical
    # assignment becomes operational again; CEO must expose the conflict instead
    # of silently ending either responsibility or pretending capacity is free.
    assert work_runtime.resolve_waits(
        waiting_work,
        "DEPENDENCY",
        issue_code=f"MISSING_EVIDENCE:{basis}",
        note="New persisted evidence changed the missing-evidence basis.",
    ) == 1
    db.session.commit()

    resume_event = CompanyEvent.query.filter_by(
        project_id=waiting_project.id,
        work_id=waiting_work.id,
        event_type="WORK_STARTED",
    ).order_by(CompanyEvent.id.desc()).first()
    assert resume_event is not None
    contention_trigger = ceo_review.classify_event(resume_event)
    assert contention_trigger["disposition"] == "PROJECT_REVIEW"
    assert contention_trigger["reason"] == "RESOURCE_CONTENTION"

    after_snapshot = ceo_operating.company_operating_snapshot()
    after = next(row for row in after_snapshot["employees"] if row["employee_id"] == researcher.id)
    after_review = ceo_management.portfolio_review(after_snapshot)
    finding = next(
        row for row in after_review["resource_contentions"]
        if row["employee_id"] == researcher.id
    )
    plan = ceo_management.company_plan(after_snapshot, after_review)

    assert after["capacity"] == "OVERCOMMITTED"
    assert {row["work_id"] for row in after["active_responsibilities"]} == {
        waiting_work.id, active_work.id,
    }
    assert finding["recommendation"] == "PORTFOLIO_REALLOCATION_REQUIRED"
    assert finding["effect_authorized"] is False
    assert any(
        row.get("blocker_type") == "RESOURCE_CONTENTION"
        and row.get("employee_id") == researcher.id
        for row in after_review["risks"]
    )
    assert plan["resource_contentions"] == after_review["resource_contentions"]
    assert WorkAssignment.query.filter_by(employee_id=researcher.id, ended_at=None).count() == 2


def test_ceo_resource_contention_reallocation_moves_only_ready_unexecuted_work(ctx):
    from eason_one.models import AgentRun, MarketContract
    from eason_one.services import ceo_management, market

    strategic = core._project(("Credible purchase-intent evidence exists",))
    strategic.priority = "CRITICAL"
    strategic_operation = core._operation(strategic)
    resumed_work = _work(
        strategic,
        strategic_operation,
        state="EXECUTING",
        title="High-priority retained research responsibility",
    )
    basis = "c" * 64
    _missing_evidence_gate(resumed_work, basis_hash=basis)

    secondary = core._project(("A distinct research result exists",))
    secondary.priority = "MEDIUM"
    secondary_operation = core._operation(secondary)
    movable_work = _unassigned_declared_work(
        secondary,
        secondary_operation,
        "RESEARCH",
        title="Secondary ready research responsibility",
    )

    # Allocate the released Researcher to the secondary READY Work while the
    # strategic responsibility is canonically quiescent.
    initial_snapshot = ceo_operating.company_operating_snapshot()
    initial_review = ceo_management.portfolio_review(initial_snapshot)
    initial_proposal = next(
        row for row in initial_review["allocation_decisions"]
        if row["work_id"] == movable_work.id
    )
    initial_intent = ceo_management.proposal_to_action_intent(
        initial_proposal,
        basis_revision=str(initial_snapshot["revision"]),
    )
    initial_applied = ceo_management.execute_action_intent(initial_intent)
    assert initial_applied["status"] == "APPLIED"
    overloaded_employee_id = initial_proposal["employee_id"]
    initial_contract = market.contract_for_work(movable_work.id)
    assert initial_contract is not None
    assert initial_contract.status == "ACTIVE"
    assert AgentRun.query.filter_by(work_id=movable_work.id).count() == 0

    # New persisted evidence resumes the strategic responsibility.  The same
    # Employee is now truly overcommitted, but the already EXECUTING strategic
    # Work must never be moved or have its execution transferred.
    assert work_runtime.resolve_waits(
        resumed_work,
        "DEPENDENCY",
        issue_code=f"MISSING_EVIDENCE:{basis}",
        note="New evidence legitimately reactivated the strategic Project.",
    ) == 1
    db.session.commit()

    snapshot = ceo_operating.company_operating_snapshot()
    employee_view = next(
        row for row in snapshot["employees"]
        if row["employee_id"] == overloaded_employee_id
    )
    assert employee_view["capacity"] == "OVERCOMMITTED"

    review = ceo_management.portfolio_review(snapshot)
    proposal = next(
        row for row in review["reallocation_decisions"]
        if row["work_id"] == movable_work.id
    )
    assert proposal["kind"] == "REALLOCATE_EXISTING_EMPLOYEE"
    assert proposal["from_employee_id"] == overloaded_employee_id
    assert proposal["employee_id"] != overloaded_employee_id
    assert proposal["required_capability"] == "RESEARCH"
    assert proposal["effect_authorized"] is False
    assert not any(row["work_id"] == resumed_work.id for row in review["reallocation_decisions"])

    intent = ceo_management.proposal_to_action_intent(
        proposal,
        basis_revision=str(snapshot["revision"]),
    )
    validated = ceo_management.validate_action_intent(intent)
    assert validated["status"] == "VALIDATED"
    assert validated["effect_authorized"] is True
    assert validated["reason"] == "CANONICAL_TEAM_FORMATION_RESOURCE_CONTENTION_REALLOCATION"

    before_runs = AgentRun.query.count()
    first = ceo_management.execute_action_intent(intent)
    second = ceo_management.execute_action_intent(intent)

    assert first["status"] == "APPLIED"
    assert first["action_type"] == "REALLOCATE_EXISTING_EMPLOYEE"
    assert first["from_employee_id"] == overloaded_employee_id
    assert first["employee_id"] == proposal["employee_id"]
    assert first["provider_execution_created"] is False
    assert first["provider_execution_transferred"] is False
    assert first["project_twd_authority_changed"] is False
    assert first["prior_assignment_history_erased"] is False
    assert second["replayed_from_receipt"] is True
    assert second["receipt_event_id"] == first["receipt_event_id"]
    assert AgentRun.query.count() == before_runs

    old_rows = WorkAssignment.query.filter_by(
        work_id=movable_work.id,
        employee_id=overloaded_employee_id,
    ).order_by(WorkAssignment.id).all()
    new_rows = WorkAssignment.query.filter_by(
        work_id=movable_work.id,
        employee_id=proposal["employee_id"],
        ended_at=None,
    ).all()
    assert len(old_rows) == 1
    assert old_rows[0].ended_at is not None
    assert len(new_rows) == 1
    assert WorkAssignment.query.filter_by(work_id=resumed_work.id, ended_at=None).one().employee_id == overloaded_employee_id

    contracts = MarketContract.query.filter_by(work_id=movable_work.id).order_by(MarketContract.generation).all()
    assert len(contracts) == 2
    assert contracts[0].status == "SUPERSEDED"
    assert contracts[0].seller_employee_id == overloaded_employee_id
    assert contracts[1].status == "ACTIVE"
    assert contracts[1].seller_employee_id == proposal["employee_id"]
    assert contracts[1].supersedes_contract_id == contracts[0].id

    from eason_one.models import Decision
    decisions = Decision.query.filter_by(
        project_id=secondary.id,
        work_id=movable_work.id,
        decided_by_employee_id=Employee.query.filter_by(slug="ceo").one().id,
        state="COMMITTED",
    ).order_by(Decision.id).all()
    # One Decision came from the initial assignment and one from the later
    # resource-contention reallocation. Receipt replay adds neither.
    assert len(decisions) == 2
    assert decisions[-1].id == first["decision_id"]
    assert decisions[-1].decision.startswith("REALLOCATE EMP-")

    refreshed = ceo_operating.company_operating_snapshot()
    source_after = next(
        row for row in refreshed["employees"]
        if row["employee_id"] == overloaded_employee_id
    )
    replacement_after = next(
        row for row in refreshed["employees"]
        if row["employee_id"] == proposal["employee_id"]
    )
    assert source_after["capacity"] == "PRIMARY_ASSIGNED"
    assert source_after["primary_responsibility"]["work_id"] == resumed_work.id
    assert replacement_after["capacity"] == "PRIMARY_ASSIGNED"
    assert replacement_after["primary_responsibility"]["work_id"] == movable_work.id


def test_ceo_resource_contention_fails_closed_when_only_inflight_work_could_move(ctx):
    from eason_one.services import ceo_management

    first_project = core._project(("First outcome",))
    first_operation = core._operation(first_project)
    first_work = _work(first_project, first_operation, state="EXECUTING", title="First in-flight responsibility")

    second_project = core._project(("Second outcome",))
    second_operation = core._operation(second_project)
    second_work = Work(
        project_id=second_project.id,
        operation_id=second_operation.id,
        title="Second in-flight responsibility",
        purpose="Another live responsibility that cannot be silently transferred.",
        expected_output="Evidence",
        acceptance_criteria="Founder criterion",
        state="EXECUTING",
        work_type="DELIVERY",
        priority="HIGH",
        created_by_employee_id=Employee.query.filter_by(slug="ceo").one().id,
        resource_ceiling_twd=Decimal("5"),
        retry_limit=1,
        runtime_control_json={"staffing_requirements": {"required_capabilities": ["RESEARCH"]}},
    )
    db.session.add(second_work); db.session.flush()
    researcher = Employee.query.filter_by(slug="researcher").one()
    db.session.add(WorkAssignment(
        work_id=second_work.id,
        employee_id=researcher.id,
        responsibility="OWNER",
        assigned_by_employee_id=Employee.query.filter_by(slug="ceo").one().id,
        reason="Fixture creates a real overcommitment without a safe READY responsibility.",
    ))
    db.session.commit()

    snapshot = ceo_operating.company_operating_snapshot()
    employee_view = next(row for row in snapshot["employees"] if row["employee_id"] == researcher.id)
    review = ceo_management.portfolio_review(snapshot)

    assert employee_view["capacity"] == "OVERCOMMITTED"
    assert {row["work_id"] for row in employee_view["active_responsibilities"]} == {first_work.id, second_work.id}
    assert review["resource_contentions"]
    assert review["reallocation_decisions"] == []
    assert WorkAssignment.query.filter_by(employee_id=researcher.id, ended_at=None).count() == 2


def test_ceo_runtime_resolves_contention_before_allocating_more_work(ctx):
    from eason_one.models import AgentRun
    from eason_one.services import ceo_management, ceo_runtime

    strategic = core._project(("Credible purchase-intent evidence exists",))
    strategic.priority = "CRITICAL"
    strategic_operation = core._operation(strategic)
    resumed_work = _work(
        strategic,
        strategic_operation,
        state="EXECUTING",
        title="Runtime contention strategic responsibility",
    )
    basis = "d" * 64
    _missing_evidence_gate(resumed_work, basis_hash=basis)

    secondary = core._project(("A distinct research result exists",))
    secondary.priority = "MEDIUM"
    secondary_operation = core._operation(secondary)
    movable_work = _unassigned_declared_work(
        secondary,
        secondary_operation,
        "RESEARCH",
        title="Runtime contention movable READY work",
    )

    # Materialize the secondary assignment while the strategic responsibility
    # is safely retained-but-quiescent, then establish the future-only cursor.
    snapshot = ceo_operating.company_operating_snapshot()
    review = ceo_management.portfolio_review(snapshot)
    proposal = next(row for row in review["allocation_decisions"] if row["work_id"] == movable_work.id)
    intent = ceo_management.proposal_to_action_intent(proposal, basis_revision=str(snapshot["revision"]))
    assert ceo_management.execute_action_intent(intent)["status"] == "APPLIED"
    overloaded_employee_id = proposal["employee_id"]
    ceo_runtime.ensure_cutover()

    before_runs = AgentRun.query.count()
    assert work_runtime.resolve_waits(
        resumed_work,
        "DEPENDENCY",
        issue_code=f"MISSING_EVIDENCE:{basis}",
        note="New evidence reactivated the strategic responsibility after CEO cutover.",
    ) == 1
    db.session.commit()

    result = ceo_runtime.process_pending_events(max_events=8)

    assert result["status"] == "PROCESSED"
    assert result["material_reviews"] >= 1
    assert result["applied_actions"] == 1
    assert result["action_result"]["action_type"] == "REALLOCATE_EXISTING_EMPLOYEE"
    assert result["action_result"]["from_employee_id"] == overloaded_employee_id
    assert result["action_result"]["provider_execution_created"] is False
    assert result["action_result"]["provider_execution_transferred"] is False
    assert AgentRun.query.count() == before_runs

    resumed_assignment = WorkAssignment.query.filter_by(work_id=resumed_work.id, ended_at=None).one()
    movable_assignment = WorkAssignment.query.filter_by(work_id=movable_work.id, ended_at=None).one()
    assert resumed_assignment.employee_id == overloaded_employee_id
    assert movable_assignment.employee_id != overloaded_employee_id

    refreshed = ceo_operating.company_operating_snapshot()
    old_owner = next(row for row in refreshed["employees"] if row["employee_id"] == overloaded_employee_id)
    new_owner = next(row for row in refreshed["employees"] if row["employee_id"] == movable_assignment.employee_id)
    assert old_owner["capacity"] == "PRIMARY_ASSIGNED"
    assert old_owner["primary_responsibility"]["work_id"] == resumed_work.id
    assert new_owner["capacity"] == "PRIMARY_ASSIGNED"
    assert new_owner["primary_responsibility"]["work_id"] == movable_work.id

    # Internal CEO events emitted by the effect may be observed on the next
    # cycle, but the durable action receipt prevents a second reallocation.
    ceo_runtime.process_pending_events(max_events=8)
    active = WorkAssignment.query.filter_by(work_id=movable_work.id, ended_at=None).all()
    assert len(active) == 1
    assert CompanyEvent.query.filter_by(
        event_type=ceo_management.ACTION_RECEIPT_EVENT_TYPE,
        correlation_id=result["action_result"]["intent_id"],
    ).count() == 1


def test_promoted_ceo_learning_enters_judgement_as_advisory_and_stales_old_intent(ctx):
    from eason_one.models import EmployeeLearningRecord
    from eason_one.services import ceo_learning, ceo_management, ceo_review

    project = core._project(("Credible purchase-intent evidence exists",))
    operation = core._operation(project)
    work = _work(project, operation, state="EXECUTING", title="Learning-aware quiescence fixture")
    _missing_evidence_gate(work, basis_hash="e" * 64)

    before_review = ceo_review.project_review(project)
    before_intent = ceo_management.project_review_to_management_intent(before_review)
    assert before_review["recommendation"] == "QUIESCE"
    assert before_intent is not None

    ceo = Employee.query.filter_by(slug="ceo").one()
    candidate = EmployeeLearningRecord(
        employee_id=ceo.id,
        learning_type="CEO_DECISION_OUTCOME_CANDIDATE",
        validation_basis="CEO_ACTION_OUTCOME_OBSERVATION",
        title="Observed quiescent allocation outcome",
        content="Observed one bounded quiescent allocation outcome; no causal rule is implied.",
        source_ref="CEO_ACTION_OUTCOME:rc06-advisory",
        evidence_json={
            "schema": "ceo-decision-outcome-candidate-v1",
            "scope": "ROLE",
            "outcome": "OBSERVED_ACCEPTED",
            "observed_basis_hash": "f" * 64,
            "causal_claim": False,
        },
        validated=False,
    )
    db.session.add(candidate)
    db.session.commit()
    promoted = ceo_learning.promote_ceo_learning(
        candidate,
        scope="ROLE",
        policy_statement=(
            "When current Project truth is quiescent, preserve strategic priority while using only lawfully available capacity."
        ),
        founder_authorized=True,
    )

    # The learning changed the judgement basis. An already-built management
    # intent must be rejected instead of silently inheriting new precedent.
    stale = ceo_management.validate_management_intent(before_intent)
    assert stale["status"] == "STALE"
    assert stale["reason"] == "ADVISORY_LEARNING_BASIS_CHANGED"

    after_review = ceo_review.project_review(project)
    portfolio = ceo_management.portfolio_review()
    plan = ceo_management.company_plan(review=portfolio)
    advisory = after_review["advisory_learning"]

    assert promoted.id in advisory["validated_learning_ids"]
    assert promoted.content in advisory["context"]
    assert advisory["authority_effect"] is False
    assert advisory["budget_effect"] is False
    assert advisory["capability_effect"] is False
    assert advisory["current_fact_effect"] is False
    assert promoted.id in portfolio["advisory_learning"]["validated_learning_ids"]
    assert promoted.id in plan["advisory_learning"]["validated_learning_ids"]
    assert plan["advisory_learning"]["authority_effect"] is False


def test_ceo_quiescence_commits_decision_and_receipt_without_pausing_project(ctx):
    import json

    from eason_one.models import Decision, Escalation
    from eason_one.services import ceo_management, ceo_review

    project = core._project(("Credible purchase-intent evidence exists",))
    operation = core._operation(project)
    work = _work(project, operation, state="EXECUTING", title="Quiescence management fixture")
    _missing_evidence_gate(work, basis_hash="1" * 64)
    original_status = project.status

    review = ceo_review.project_review(project)
    intent = ceo_management.project_review_to_management_intent(review)
    assert review["recommendation"] == "QUIESCE"
    assert intent["action_type"] == "DOCUMENT_QUIESCENCE"

    before_escalations = Escalation.query.filter_by(project_id=project.id).count()
    first = ceo_management.execute_management_intent(intent)
    second = ceo_management.execute_management_intent(intent)

    assert first["status"] == "APPLIED"
    assert first["semantic_effect"] == "QUIESCENCE_DOCUMENTED"
    assert first["project_paused"] is False
    assert first["project_terminated"] is False
    assert project.status == original_status
    assert project.status not in {"PAUSED", "CANCELLED", "COMPLETED"}
    assert Escalation.query.filter_by(project_id=project.id).count() == before_escalations
    assert second["replayed_from_receipt"] is True
    assert second["receipt_event_id"] == first["receipt_event_id"]
    assert second["decision_id"] == first["decision_id"]

    decisions = Decision.query.filter_by(project_id=project.id, decision="QUIESCE", state="COMMITTED").all()
    assert len(decisions) == 1
    basis = json.loads(decisions[0].authority_basis)
    assert basis["schema"] == "CEO_MANAGEMENT_DECISION_V1"
    assert basis["project_lifecycle_mutation_authorized"] is False
    assert basis["project_termination_authorized"] is False
    assert basis["resume_conditions"]


def test_ceo_escalation_reuses_exact_current_founder_gate_without_duplicate_question(ctx):
    import json

    from eason_one.models import Decision, Escalation
    from eason_one.services import ceo_management, ceo_review, governance

    project = core._project(("Founder criterion",))
    ceo = Employee.query.filter_by(slug="ceo").one()
    gate = governance.open_gate(
        project=project,
        escalation_type="BUDGET_AUTHORIZATION",
        reason="A material Project path needs exactly NT$3 beyond current authority.",
        created_by_employee_id=ceo.id,
        authority_payload={"additional_budget_twd": "3", "scope": "PROJECT"},
        recommendation="Approve exactly NT$3 only if the Founder wants this path to continue.",
    )
    db.session.commit()

    review = ceo_review.project_review(project)
    intent = ceo_management.project_review_to_management_intent(review)
    assert review["recommendation"] == "ESCALATE"
    assert review["primary_blocker"]["basis"] == f"escalation:{gate.id}"
    assert intent["action_type"] == "SURFACE_FOUNDER_ESCALATION"

    before = Escalation.query.filter_by(project_id=project.id, state="OPEN").count()
    first = ceo_management.execute_management_intent(intent)
    second = ceo_management.execute_management_intent(intent)

    assert first["status"] == "APPLIED"
    assert first["semantic_effect"] == "EXISTING_FOUNDER_GATE_SURFACED"
    assert first["governance_effect"] == "EXISTING_FOUNDER_GATE_REUSED"
    assert first["escalation_id"] == gate.id
    assert Escalation.query.filter_by(project_id=project.id, state="OPEN").count() == before == 1
    assert second["replayed_from_receipt"] is True
    assert second["receipt_event_id"] == first["receipt_event_id"]

    decision = db.session.get(Decision, first["decision_id"])
    basis = json.loads(decision.authority_basis)
    assert decision.decision == f"ESCALATE VIA FOUNDER GATE #{gate.id}"
    assert basis["project_lifecycle_mutation_authorized"] is False
    assert basis["provider_execution_authorized"] is False


def test_ceo_stop_recommendation_is_narrow_and_routes_to_project_cancel_governance(ctx, monkeypatch):
    import json

    from eason_one.models import Decision, Escalation
    from eason_one.services import ceo_management, ceo_review, governance

    project = core._project(("Founder criterion",))

    # Contradiction alone while an executable path remains is a REPLAN, not a stop.
    executable_view = {
        "blockers": {"primary": None, "secondary": [], "all": [], "classification_error": None},
        "contract_evidence": {"state": "CONTRADICTED"},
        "result_state": "NOT_READY",
        "operating_state": "READY",
    }
    monkeypatch.setattr(ceo_operating, "project_operating_view", lambda _project: executable_view)
    assert ceo_review.project_review(project)["recommendation"] == "REPLAN"

    # The exact stop shape is the deterministic classifier contract for
    # authoritative contradiction after all delivery paths are terminal.
    stop_blocker = {
        "type": "EVIDENCE",
        "basis": "project_contract:contradicted",
        "reason": "Current authoritative Project evidence contradicts at least one Founder success criterion.",
        "actionable_by": "CEO",
        "resolution_state": "ACTIVE",
        "work_id": None,
        "source": "PROJECT_OUTCOME",
    }
    stop_view = {
        "blockers": {
            "primary": stop_blocker,
            "secondary": [],
            "all": [stop_blocker],
            "classification_error": None,
        },
        "contract_evidence": {"state": "CONTRADICTED"},
        "result_state": "NOT_READY",
        "operating_state": "QUIESCENT",
    }
    monkeypatch.setattr(ceo_operating, "project_operating_view", lambda _project: stop_view)

    review = ceo_review.project_review(project)
    intent = ceo_management.project_review_to_management_intent(review)
    assert review["recommendation"] == "RECOMMEND_STOP"
    assert intent["action_type"] == "RECOMMEND_PROJECT_STOP"

    before_status = project.status
    first = ceo_management.execute_management_intent(intent)
    second = ceo_management.execute_management_intent(intent)

    gate = db.session.get(Escalation, first["escalation_id"])
    decision = db.session.get(Decision, first["decision_id"])
    basis = json.loads(decision.authority_basis)

    assert first["status"] == "APPLIED"
    assert first["semantic_effect"] == "STOP_RECOMMENDATION_ROUTED_TO_FOUNDER"
    assert first["governance_effect"] == "PROJECT_CANCEL_GATE_OPENED_OR_REUSED"
    assert first["project_terminated"] is False
    assert gate.escalation_type == "PROJECT_CANCEL"
    assert gate.state == "OPEN"
    assert governance.current_gate(project).id == gate.id
    assert project.status == "BLOCKED"  # canonical Governance wait, never CEO termination
    assert before_status != "CANCELLED"
    assert project.status != "CANCELLED"
    assert decision.decision == "RECOMMEND_STOP"
    assert basis["project_termination_authorized"] is False
    assert basis["project_lifecycle_mutation_authorized"] is False
    assert second["replayed_from_receipt"] is True
    assert second["receipt_event_id"] == first["receipt_event_id"]
    assert Escalation.query.filter_by(project_id=project.id, escalation_type="PROJECT_CANCEL").count() == 1
    assert Decision.query.filter_by(project_id=project.id, decision="RECOMMEND_STOP", state="COMMITTED").count() == 1


def test_ceo_project_basis_ignores_internal_ceo_receipts_but_changes_on_domain_request(ctx):
    from eason_one.services import ceo_management

    project = core._project(("Founder criterion",))
    operation = core._operation(project)
    _work(project, operation, state="READY", title="Basis stability fixture")

    before = ceo_operating.project_basis_revision(project)
    company_events.emit(
        ceo_management.ACTION_RECEIPT_EVENT_TYPE,
        actor_type="EMPLOYEE",
        project_id=project.id,
        correlation_id="ceo-internal-basis-fixture",
        payload={"status": "APPLIED", "action_type": "DOCUMENT_QUIESCENCE"},
        commit=True,
    )
    after_internal = ceo_operating.project_basis_revision(project)
    assert after_internal == before

    company_events.emit(
        "ARTIFACT_REJECTED",
        actor_type="EMPLOYEE",
        project_id=project.id,
        correlation_id="domain-basis-change-fixture",
        payload={"artifact_version_id": 999, "reason": "Current domain evidence changed."},
        commit=True,
    )
    after_domain = ceo_operating.project_basis_revision(project)
    assert after_domain != before


def test_ceo_replan_receipt_authorizes_existing_kernel_seam_without_provider_or_work(ctx):
    from eason_one.models import AgentRun, Decision
    from eason_one.services import ceo_management, ceo_review, ceo_runtime, company_kernel

    project = core._project(("Credible purchase-intent evidence exists",))
    operation = core._operation(project)
    work = _work(project, operation, state="ABANDONED", title="Replan authorization fixture")
    company_events.emit(
        "WORK_EVIDENCE_REVIEW_EXHAUSTED",
        actor_type="RUNTIME",
        project_id=project.id,
        work_id=work.id,
        correlation_id=f"work:{work.id}",
        payload={"implementation_replayed": False},
        commit=True,
    )
    ceo_runtime.ensure_cutover()

    before_runs = AgentRun.query.count()
    before_works = Work.query.count()
    pending = company_kernel._ensure_ceo_management_clearance(project, "REPLAN")
    assert pending["authorized"] is False
    assert pending["mode"] == "CEO_DECISION_REQUIRED"
    request = CompanyEvent.query.filter_by(
        project_id=project.id, event_type="CEO_PROJECT_MANAGEMENT_REVIEW_REQUIRED"
    ).one()
    assert request.payload_json["action_type"] == "AUTHORIZE_PROJECT_REPLAN"
    duplicate_pending = company_kernel._ensure_ceo_management_clearance(project, "REPLAN")
    assert duplicate_pending["authorized"] is False
    assert CompanyEvent.query.filter_by(
        project_id=project.id, event_type="CEO_PROJECT_MANAGEMENT_REVIEW_REQUIRED"
    ).count() == 1
    assert AgentRun.query.count() == before_runs
    assert Work.query.count() == before_works

    cycle = ceo_runtime.process_pending_events(max_events=8)
    assert cycle["applied_actions"] == 1
    assert cycle["action_result"]["action_type"] == "AUTHORIZE_PROJECT_REPLAN"
    assert cycle["action_result"]["semantic_effect"] == "KERNEL_PROJECT_REPLAN_AUTHORIZED"
    assert cycle["action_result"]["provider_execution_created"] is False
    assert cycle["action_result"]["kernel_execution_authorized"] is True
    assert AgentRun.query.count() == before_runs
    assert Work.query.count() == before_works

    review = ceo_review.project_review(project)
    assert review["recommendation"] == "REPLAN"
    clearance = company_kernel._ensure_ceo_management_clearance(project, "REPLAN")
    assert clearance["authorized"] is True
    decision = db.session.get(Decision, clearance["decision_id"])
    assert decision.decision == "REPLAN"

    # A later non-CEO Project fact invalidates the old authorization.  The CEO
    # must judge the new basis again instead of letting Kernel reuse stale consent.
    company_events.emit(
        "PROJECT_HOST_HTTP_REVERIFIED",
        actor_type="RUNTIME",
        project_id=project.id,
        correlation_id=f"project:{project.id}",
        payload={"criterion_id": "P1", "artifact_version_id": 999},
        commit=True,
    )
    stale = company_kernel._ensure_ceo_management_clearance(project, "REPLAN")
    assert stale["authorized"] is False
    assert stale["mode"] == "CEO_DECISION_REQUIRED"
    assert CompanyEvent.query.filter_by(
        project_id=project.id, event_type="CEO_PROJECT_MANAGEMENT_REVIEW_REQUIRED"
    ).count() == 2


def test_ceo_request_verification_is_decided_before_kernel_provider_path(ctx, monkeypatch):
    from eason_one.models import AgentRun, Decision
    from eason_one.services import ceo_management, ceo_review, ceo_runtime, company_kernel, project_outcome

    project = core._project(("Founder criterion requires semantic evidence",))
    operation = core._operation(project)
    _work(project, operation, state="ACCEPTED", title="Verification authorization fixture")
    monkeypatch.setattr(project_outcome, "needs_semantic_review", lambda _project: True)

    review = ceo_review.project_review(project)
    assert review["recommendation"] == "REQUEST_VERIFICATION"
    intent = ceo_management.project_review_to_management_intent(review)
    assert intent["action_type"] == "REQUEST_PROJECT_VERIFICATION"

    ceo_runtime.ensure_cutover()
    before_runs = AgentRun.query.count()
    before_works = Work.query.count()
    pending = company_kernel._ensure_ceo_management_clearance(project, "REQUEST_VERIFICATION")
    assert pending["authorized"] is False
    assert CompanyEvent.query.filter_by(
        project_id=project.id, event_type="CEO_PROJECT_MANAGEMENT_REVIEW_REQUIRED"
    ).count() == 1
    assert AgentRun.query.count() == before_runs
    assert Work.query.count() == before_works

    cycle = ceo_runtime.process_pending_events(max_events=8)
    assert cycle["applied_actions"] == 1
    result = cycle["action_result"]
    assert result["action_type"] == "REQUEST_PROJECT_VERIFICATION"
    assert result["semantic_effect"] == "KERNEL_PROJECT_VERIFICATION_REQUESTED"
    assert result["provider_execution_created"] is False
    assert result["kernel_execution_authorized"] is True
    assert AgentRun.query.count() == before_runs
    assert Work.query.count() == before_works

    clearance = company_kernel._ensure_ceo_management_clearance(project, "REQUEST_VERIFICATION")
    assert clearance["authorized"] is True
    decision = db.session.get(Decision, clearance["decision_id"])
    assert decision.decision == "REQUEST_VERIFICATION"


def test_ceo_management_required_event_is_project_review_trigger(ctx):
    from eason_one.services import ceo_review

    project = core._project(("Founder criterion",))
    event = company_events.emit(
        "CEO_PROJECT_MANAGEMENT_REVIEW_REQUIRED",
        actor_type="RUNTIME",
        project_id=project.id,
        correlation_id="ceo-management-trigger-fixture",
        payload={"recommendation": "REPLAN", "provider_calls": 0},
        commit=True,
    )
    trigger = ceo_review.classify_event(event)
    assert trigger["disposition"] == "PROJECT_REVIEW"
    assert trigger["reason"] == "KERNEL_MANAGEMENT_JUDGEMENT_REQUIRED"


def test_ceo_ready_for_outcome_check_precedes_result_ready_projection(ctx, monkeypatch):
    import json

    from eason_one.models import AgentRun, Decision
    from eason_one.services import (
        ceo_management,
        ceo_review,
        ceo_runtime,
        company_kernel,
        project_outcome,
    )

    project = core._project(("Founder criterion",))
    operation = core._operation(project)
    _work(project, operation, state="ACCEPTED", title="Outcome-check handshake fixture")

    # Establish the future-only cutover before replacing the read projection.
    ceo_runtime.ensure_cutover()
    view = {
        "blockers": {"primary": None, "secondary": [], "all": [], "classification_error": None},
        "contract_evidence": {"state": "SUPPORTED", "overall_status": "SATISFIED"},
        "result_state": "NOT_READY",
        "operating_state": "QUIESCENT",
    }
    monkeypatch.setattr(ceo_operating, "project_operating_view", lambda _project: dict(view))
    monkeypatch.setattr(
        project_outcome,
        "evaluate",
        lambda _project: {"overall_status": "SATISFIED", "criteria": []},
    )

    closed = []

    def _fake_close(_project, _evaluation=None, *, management_decision_id=None):
        closed.append(management_decision_id)
        return {
            "status": "RESULT_READY",
            "project_id": _project.id,
            "management_decision_id": management_decision_id,
        }

    monkeypatch.setattr(project_outcome, "close_result_ready", _fake_close)

    before_runs = AgentRun.query.count()
    pending = company_kernel._close_result_ready_after_ceo_review(
        project, {"overall_status": "SATISFIED"}, project_outcome
    )
    assert pending is None
    assert closed == []
    request = CompanyEvent.query.filter_by(
        project_id=project.id,
        event_type="CEO_PROJECT_MANAGEMENT_REVIEW_REQUIRED",
    ).one()
    assert request.payload_json["recommendation"] == "READY_FOR_OUTCOME_CHECK"
    assert request.payload_json["action_type"] == "REQUEST_PROJECT_OUTCOME_CHECK"
    assert AgentRun.query.count() == before_runs

    review = ceo_review.project_review(project)
    intent = ceo_management.project_review_to_management_intent(review)
    assert review["recommendation"] == "READY_FOR_OUTCOME_CHECK"
    assert intent["action_type"] == "REQUEST_PROJECT_OUTCOME_CHECK"
    applied = ceo_management.execute_management_intent(intent)

    assert applied["status"] == "APPLIED"
    assert applied["semantic_effect"] == "KERNEL_PROJECT_OUTCOME_CHECK_REQUESTED"
    assert applied["kernel_outcome_check_requested"] is True
    assert applied["provider_execution_created"] is False
    assert applied["project_result_ready_declared"] is False
    assert applied["project_completed"] is False
    assert AgentRun.query.count() == before_runs

    decision = db.session.get(Decision, applied["decision_id"])
    basis = json.loads(decision.authority_basis)
    assert decision.decision == "READY_FOR_OUTCOME_CHECK"
    assert basis["project_result_ready_authorized"] is False
    assert basis["project_completion_authorized"] is False
    assert basis["project_lifecycle_mutation_authorized"] is False

    result = company_kernel._close_result_ready_after_ceo_review(
        project, {"overall_status": "SATISFIED"}, project_outcome
    )
    assert result["status"] == "RESULT_READY"
    assert closed == [decision.id]


def test_result_ready_project_review_does_not_mint_duplicate_outcome_check_decision(ctx, monkeypatch):
    from eason_one.services import ceo_management, ceo_review

    project = core._project(("Founder criterion",))
    view = {
        "blockers": {"primary": None, "secondary": [], "all": [], "classification_error": None},
        "contract_evidence": {"state": "SUPPORTED", "overall_status": "SATISFIED"},
        "result_state": "RESULT_READY",
        "operating_state": "RESULT_READY",
    }
    monkeypatch.setattr(ceo_operating, "project_operating_view", lambda _project: dict(view))

    review = ceo_review.project_review(project)
    assert review["recommendation"] == "READY_FOR_OUTCOME_CHECK"
    assert ceo_management.project_review_to_management_intent(review) is None


def test_ceo_closure_observes_nonwork_management_lineage_without_hindsight_score(ctx):
    from eason_one.models import Decision, EmployeeLearningRecord, now
    from eason_one.services import ceo_learning, ceo_management

    project = core._project(("Founder criterion",))
    operation = core._operation(project)
    ceo = Employee.query.filter_by(slug="ceo").one()

    replan_decision = Decision(
        project_id=project.id,
        proposed_by_employee_id=ceo.id,
        decided_by_employee_id=ceo.id,
        question="Replan current Project?",
        decision="REPLAN",
        rationale="Current evidence required a distinct continuation path.",
        state="COMMITTED",
        authority_basis='{"schema":"CEO_MANAGEMENT_DECISION_V1"}',
        committed_at=now(),
    )
    outcome_decision = Decision(
        project_id=project.id,
        proposed_by_employee_id=ceo.id,
        decided_by_employee_id=ceo.id,
        question="Run deterministic Project outcome check?",
        decision="READY_FOR_OUTCOME_CHECK",
        rationale="Current accepted evidence satisfies the Founder criteria.",
        state="COMMITTED",
        authority_basis='{"schema":"CEO_MANAGEMENT_DECISION_V1"}',
        committed_at=now(),
    )
    db.session.add_all([replan_decision, outcome_decision])
    db.session.flush()

    memory = dict(operation.memory_json or {})
    memory["ceo_management_decision_id"] = replan_decision.id
    operation.memory_json = memory
    company_events.emit(
        ceo_management.ACTION_RECEIPT_EVENT_TYPE,
        actor_type="EMPLOYEE",
        actor_id=ceo.id,
        project_id=project.id,
        decision_id=replan_decision.id,
        correlation_id="ceo-intent:learning-replan-lineage",
        payload={
            "intent_id": "ceo-intent:learning-replan-lineage",
            "action_type": "AUTHORIZE_PROJECT_REPLAN",
            "status": "APPLIED",
            "project_id": project.id,
            "decision_id": replan_decision.id,
            "provider_execution_created": False,
            "project_twd_authority_changed": False,
            "project_terminated": False,
        },
        commit=False,
    )
    company_events.emit(
        ceo_management.ACTION_RECEIPT_EVENT_TYPE,
        actor_type="EMPLOYEE",
        actor_id=ceo.id,
        project_id=project.id,
        decision_id=outcome_decision.id,
        correlation_id="ceo-intent:learning-outcome-check",
        payload={
            "intent_id": "ceo-intent:learning-outcome-check",
            "action_type": "REQUEST_PROJECT_OUTCOME_CHECK",
            "status": "APPLIED",
            "project_id": project.id,
            "decision_id": outcome_decision.id,
            "provider_execution_created": False,
            "project_twd_authority_changed": False,
            "project_terminated": False,
            "project_result_ready_declared": False,
            "project_completed": False,
        },
        commit=False,
    )
    result_event = company_events.emit(
        "PROJECT_RESULT_READY",
        actor_type="RUNTIME",
        project_id=project.id,
        decision_id=outcome_decision.id,
        correlation_id=f"project:{project.id}:learning-fixture",
        payload={"ceo_management_decision_id": outcome_decision.id},
        commit=False,
    )
    project.status = "COMPLETED"
    db.session.commit()

    facts = ceo_learning.project_closure_facts(project)
    by_action = {row["action_type"]: row for row in facts["ceo_action_outcomes"]}
    replan = by_action["AUTHORIZE_PROJECT_REPLAN"]
    outcome_check = by_action["REQUEST_PROJECT_OUTCOME_CHECK"]

    assert replan["outcome"] == "OBSERVED_CONTINUATION_LINEAGE"
    assert replan["observation_details"]["continuation_operation_ids"] == [operation.id]
    assert replan["performance_assessment"]["score"] is None
    assert replan["performance_assessment"]["decision_basis_quality"] == "NOT_SCORED"
    assert replan["causal_claim"] is False

    assert outcome_check["outcome"] == "OBSERVED_RESULT_READY"
    assert outcome_check["observation_details"]["project_result_ready_event_ids"] == [result_event.id]
    assert outcome_check["performance_assessment"]["cost_attribution"] == "NOT_ATTRIBUTED"
    assert outcome_check["causal_claim"] is False

    captured = ceo_learning.capture_project_closure(project)
    candidates = EmployeeLearningRecord.query.filter_by(
        employee_id=ceo.id,
        project_id=project.id,
        learning_type="CEO_DECISION_OUTCOME_CANDIDATE",
    ).order_by(EmployeeLearningRecord.id).all()
    assert captured["status"] == "CAPTURED"
    assert len(candidates) == 2
    assert {row.evidence_json["outcome"] for row in candidates} == {
        "OBSERVED_CONTINUATION_LINEAGE",
        "OBSERVED_RESULT_READY",
    }
    assert all(row.validated is False for row in candidates)
    assert all(row.evidence_json["causal_claim"] is False for row in candidates)


def test_founder_completion_is_canonical_idempotent_and_emits_one_terminal_event(ctx):
    from eason_one.models import Decision
    from eason_one.services import project_outcome

    criterion = "Founder result is durably complete"
    project = core._project((criterion,))
    operation = core._operation(project, completion=(criterion,))
    core._accepted_work(project, operation, criterion)
    evaluation = project_outcome.evaluate(project)
    assert evaluation["overall_status"] == "SATISFIED"
    project_outcome.close_result_ready(project, evaluation)

    first = project_outcome.accept_founder_completion(project)
    second = project_outcome.accept_founder_completion(project.id)

    assert first["status"] == "COMPLETED"
    assert first["replayed"] is False
    assert second["status"] == "ALREADY_COMPLETED"
    assert second["replayed"] is True
    assert second["completion_decision_id"] == first["completion_decision_id"]
    assert second["completion_event_id"] == first["completion_event_id"]
    assert Decision.query.filter_by(
        legacy_source="PROJECT_RESULT_ACCEPTANCE", legacy_source_id=project.id
    ).count() == 1
    terminal = CompanyEvent.query.filter_by(
        project_id=project.id, event_type="FOUNDER_PROJECT_COMPLETED"
    ).all()
    assert len(terminal) == 1
    assert terminal[0].decision_id == first["completion_decision_id"]
    assert terminal[0].payload_json["project_result_verification_id"] == first["verification_id"]
    assert terminal[0].payload_json["completion_schema"] == "FOUNDER_PROJECT_COMPLETION_V1"
    assert project.status == "COMPLETED"


def test_completed_project_without_canonical_founder_acceptance_cannot_be_replay_blessed(ctx):
    from eason_one.services import project_outcome

    project = core._project(("Founder criterion",))
    project.status = "COMPLETED"
    db.session.commit()

    try:
        project_outcome.accept_founder_completion(project)
        assert False, "manual COMPLETED projection must not become canonical Founder acceptance"
    except ValueError as exc:
        assert str(exc) == "PROJECT_COMPLETION_AUTHORITY_PROOF_MISSING"

    assert CompanyEvent.query.filter_by(
        project_id=project.id, event_type="FOUNDER_PROJECT_COMPLETED"
    ).count() == 0


def test_terminal_completion_event_freezes_closure_learning_across_replay(ctx):
    from eason_one.models import Decision, EmployeeLearningRecord, now
    from eason_one.services import ceo_learning, ceo_management, project_outcome

    criterion = "Founder closure is authoritative"
    project = core._project((criterion,))
    operation = core._operation(project, completion=(criterion,))
    core._accepted_work(project, operation, criterion)
    ceo = Employee.query.filter_by(slug="ceo").one()

    observed_decision = Decision(
        project_id=project.id,
        proposed_by_employee_id=ceo.id,
        decided_by_employee_id=ceo.id,
        question="Document current quiescence context?",
        decision="QUIESCE",
        rationale="Fixture CEO decision that should be frozen into terminal learning.",
        state="COMMITTED",
        authority_basis='{"schema":"CEO_MANAGEMENT_DECISION_V1"}',
        committed_at=now(),
    )
    db.session.add(observed_decision); db.session.flush()
    company_events.emit(
        ceo_management.ACTION_RECEIPT_EVENT_TYPE,
        actor_type="EMPLOYEE",
        actor_id=ceo.id,
        project_id=project.id,
        decision_id=observed_decision.id,
        correlation_id="ceo-intent:pre-terminal-observation",
        payload={
            "intent_id": "ceo-intent:pre-terminal-observation",
            "action_type": "DOCUMENT_QUIESCENCE",
            "status": "APPLIED",
            "project_id": project.id,
            "decision_id": observed_decision.id,
            "provider_execution_created": False,
            "project_twd_authority_changed": False,
            "project_terminated": False,
        },
        commit=True,
    )

    evaluation = project_outcome.evaluate(project)
    project_outcome.close_result_ready(project, evaluation)
    completed = project_outcome.accept_founder_completion(project)
    terminal_event = db.session.get(CompanyEvent, completed["completion_event_id"])

    captured = ceo_learning.capture_project_closure_from_event(terminal_event)
    assert captured["status"] == "CAPTURED"
    authority = captured["terminal_authority"]
    assert authority["canonical"] is True
    assert authority["completion_decision_id"] == completed["completion_decision_id"]
    assert authority["completion_event_id"] == terminal_event.id
    assert authority["project_result_verification_id"] == completed["verification_id"]
    assert authority["learning_authority_effect"] is False

    candidates_before = EmployeeLearningRecord.query.filter_by(
        employee_id=ceo.id,
        project_id=project.id,
        learning_type="CEO_DECISION_OUTCOME_CANDIDATE",
    ).order_by(EmployeeLearningRecord.id).all()
    assert len(candidates_before) == 1

    # Simulate later/stale audit truth appearing after the immutable terminal
    # source event. Replaying that exact source must return the frozen closure
    # before scanning/staging any newly-arrived candidate.
    late_decision = Decision(
        project_id=project.id,
        proposed_by_employee_id=ceo.id,
        decided_by_employee_id=ceo.id,
        question="Late stale CEO action?",
        decision="QUIESCE",
        rationale="Must not enter the already-frozen terminal closure.",
        state="COMMITTED",
        authority_basis='{"schema":"CEO_MANAGEMENT_DECISION_V1"}',
        committed_at=now(),
    )
    db.session.add(late_decision); db.session.flush()
    company_events.emit(
        ceo_management.ACTION_RECEIPT_EVENT_TYPE,
        actor_type="EMPLOYEE",
        actor_id=ceo.id,
        project_id=project.id,
        decision_id=late_decision.id,
        correlation_id="ceo-intent:late-after-terminal",
        payload={
            "intent_id": "ceo-intent:late-after-terminal",
            "action_type": "DOCUMENT_QUIESCENCE",
            "status": "APPLIED",
            "project_id": project.id,
            "decision_id": late_decision.id,
            "provider_execution_created": False,
            "project_twd_authority_changed": False,
            "project_terminated": False,
        },
        commit=True,
    )

    replay = ceo_learning.capture_project_closure_from_event(terminal_event)
    candidates_after = EmployeeLearningRecord.query.filter_by(
        employee_id=ceo.id,
        project_id=project.id,
        learning_type="CEO_DECISION_OUTCOME_CANDIDATE",
    ).order_by(EmployeeLearningRecord.id).all()
    assert replay["status"] == "EXISTING"
    assert replay["closure_event_id"] == captured["closure_event_id"]
    assert [row.id for row in candidates_after] == [row.id for row in candidates_before]
    assert CompanyEvent.query.filter_by(
        event_type=ceo_learning.PROJECT_CLOSURE_EVENT_TYPE,
        project_id=project.id,
    ).count() == 1


def test_rc08_canonical_completion_replay_does_not_depend_on_old_correlation_format(ctx):
    from eason_one.services import project_outcome

    criterion = "RC08 completion remains replayable"
    project = core._project((criterion,))
    operation = core._operation(project, completion=(criterion,))
    core._accepted_work(project, operation, criterion)
    evaluation = project_outcome.evaluate(project)
    project_outcome.close_result_ready(project, evaluation)
    first = project_outcome.accept_founder_completion(project)

    # RC08 used a generic project correlation and did not persist the RC09
    # completion_schema marker in the terminal event payload. Canonical replay
    # must rely on the durable Decision + proof fields, not on that newer label.
    terminal = db.session.get(CompanyEvent, first["completion_event_id"])
    terminal.correlation_id = f"project:{project.id}"
    payload = dict(terminal.payload_json or {})
    payload.pop("completion_schema", None)
    terminal.payload_json = payload
    db.session.commit()

    replay = project_outcome.accept_founder_completion(project)
    assert replay["status"] == "ALREADY_COMPLETED"
    assert replay["completion_decision_id"] == first["completion_decision_id"]
    assert replay["completion_event_id"] == first["completion_event_id"]
    assert CompanyEvent.query.filter_by(
        project_id=project.id, event_type="FOUNDER_PROJECT_COMPLETED"
    ).count() == 1


def test_terminal_project_recovery_cannot_wake_work_or_reactivate_project(ctx):
    from eason_one.models import now
    from eason_one.services import runtime_recovery

    project = core._project(("Founder criterion",))
    operation = core._operation(project)
    work = _work(project, operation, state="READY", title="Terminal recovery fence fixture")
    work_runtime.open_wait(
        work,
        "INTERNAL_RECOVERY",
        "Bounded retry would normally be due immediately.",
        retry_after=now(),
        resume_state="EXECUTING",
        issue_code="RC10_TERMINAL_RECOVERY",
    )
    project.status = "COMPLETED"
    db.session.commit()

    assert work.state == "WAITING"
    assert work_runtime.project_can_activate(project) is False
    assert len(work_runtime.open_gates(work)) == 1

    resolved = runtime_recovery.resolve_internal_waits()
    db.session.refresh(project)
    db.session.refresh(work)

    assert resolved == 0
    assert project.status == "COMPLETED"
    assert work.state == "WAITING"
    assert len(work_runtime.open_gates(work)) == 1


def test_terminal_project_wait_cleanup_never_resumes_waiting_work(ctx):
    project = core._project(("Founder criterion",))
    operation = core._operation(project)
    work = _work(project, operation, state="READY", title="Terminal cleanup fixture")
    work_runtime.open_wait(
        work,
        "INTERNAL_RECOVERY",
        "Historical wait may be retired after terminal closure.",
        resume_state="EXECUTING",
        issue_code="RC10_TERMINAL_CLEANUP",
    )
    project.status = "COMPLETED"
    db.session.commit()

    cleaned = work_runtime.resolve_waits(
        work,
        "INTERNAL_RECOVERY",
        issue_code="RC10_TERMINAL_CLEANUP",
        note="Retire historical wait metadata only.",
    )
    db.session.commit()
    db.session.refresh(project)
    db.session.refresh(work)

    assert cleaned == 1
    assert project.status == "COMPLETED"
    assert work.state == "WAITING"
    assert work_runtime.open_gates(work) == []


def test_terminal_project_blocks_work_creation_transition_wait_and_reassignment(ctx):
    project = core._project(("Founder criterion",))
    operation = core._operation(project)
    work = _work(project, operation, state="READY", title="Terminal Work mutation fixture")
    ceo = Employee.query.filter_by(slug="ceo").one()
    replacement = Employee.query.filter_by(slug="critic").one()
    before_work_count = Work.query.filter_by(project_id=project.id).count()
    before_assignment_count = WorkAssignment.query.filter_by(work_id=work.id).count()
    project.status = "COMPLETED"
    db.session.commit()

    failures = []
    for label, call in (
        (
            "create",
            lambda: work_runtime.create_work(
                project_id=project.id,
                operation_id=operation.id,
                title="Forbidden post-terminal Work",
                purpose="Must never materialize.",
                owner_employee_id=replacement.id,
                created_by_employee_id=ceo.id,
            ),
        ),
        (
            "transition",
            lambda: work_runtime.transition(work, "EXECUTING", reason="Must be fenced"),
        ),
        (
            "wait",
            lambda: work_runtime.open_wait(
                work,
                "INTERNAL_RECOVERY",
                "Must not open a new gate after completion.",
                issue_code="RC10_FORBIDDEN_WAIT",
            ),
        ),
        (
            "reassign",
            lambda: work_runtime.reassign(
                work,
                replacement.id,
                assigned_by_employee_id=ceo.id,
                reason="Must not rotate authority after completion.",
            ),
        ),
    ):
        try:
            call()
            assert False, f"terminal Project must reject Work {label}"
        except ValueError as exc:
            failures.append((label, str(exc)))
            db.session.rollback()
            project = db.session.get(type(project), project.id)
            work = db.session.get(Work, work.id)

    assert failures[0][1] == "PROJECT_TERMINAL_WORK_CREATION_FORBIDDEN:COMPLETED"
    assert all(
        message.startswith("PROJECT_TERMINAL_WORK_MUTATION_FORBIDDEN:")
        for _, message in failures[1:]
    )
    assert Work.query.filter_by(project_id=project.id).count() == before_work_count
    assert WorkAssignment.query.filter_by(work_id=work.id).count() == before_assignment_count
    assert project.status == "COMPLETED"
    assert work.state == "READY"


def test_terminal_project_blocks_work_execution_and_central_provider_boundary(ctx):
    from eason_one.models import AgentRun
    from eason_one.services import execution, work_execution

    project = core._project(("Founder criterion",))
    operation = core._operation(project)
    work = _work(project, operation, state="READY", title="Terminal provider fence fixture")
    employee = Employee.query.filter_by(slug="researcher").one()
    project.status = "COMPLETED"
    db.session.commit()
    before_runs = AgentRun.query.count()

    result = work_execution.execute_work(work)
    assert result == {
        "status": "PROJECT_TERMINAL",
        "project_status": "COMPLETED",
        "work_id": work.id,
    }
    assert AgentRun.query.count() == before_runs

    try:
        execution.execute(
            employee,
            "TASK_EXECUTION",
            "This provider call must never be purchased.",
            project=project,
            operation=operation,
            work=work,
        )
        assert False, "central execution boundary must reject terminal Project spend"
    except ValueError as exc:
        assert str(exc) == "PROJECT_TERMINAL_EXECUTION_FORBIDDEN:COMPLETED"

    assert AgentRun.query.count() == before_runs
    assert project.status == "COMPLETED"
    assert work.state == "READY"


def test_terminal_project_blocks_new_hiring_and_meeting_side_doors(ctx):
    from eason_one.models import HiringRequest, Meeting
    from eason_one.services import meetings, workforce

    project = core._project(("Founder criterion",))
    ceo = Employee.query.filter_by(slug="ceo").one()
    researcher = Employee.query.filter_by(slug="researcher").one()
    project.status = "COMPLETED"
    db.session.commit()
    before_hires = HiringRequest.query.filter_by(project_id=project.id).count()
    before_meetings = Meeting.query.filter_by(project_id=project.id).count()

    try:
        workforce.request_hire(
            requested_by_type="EMPLOYEE",
            requester=ceo,
            project=project,
            role_needed="Post-terminal specialist",
            problem="Must not create new staffing authority after completion.",
            why_now="There is no valid reason after terminal closure.",
            responsibilities=["No-op"],
            capabilities=["No-op"],
            urgency="NORMAL",
            use_frequency="ONE_TIME",
        )
        assert False, "terminal Project must reject new HiringRequest"
    except ValueError as exc:
        assert str(exc) == "PROJECT_TERMINAL_HIRING_REQUEST_FORBIDDEN:COMPLETED"
    db.session.rollback()

    project = db.session.get(type(project), project.id)
    ceo = Employee.query.filter_by(slug="ceo").one()
    researcher = Employee.query.filter_by(slug="researcher").one()
    try:
        meetings.create(
            "Forbidden terminal meeting",
            "Must not create a new execution surface after completion.",
            "No agenda should be materialized.",
            ceo,
            [ceo, researcher],
            project=project,
        )
        assert False, "terminal Project must reject new Meeting"
    except ValueError as exc:
        assert str(exc) == "PROJECT_TERMINAL_MEETING_CREATION_FORBIDDEN:COMPLETED"
    db.session.rollback()

    assert HiringRequest.query.filter_by(project_id=project.id).count() == before_hires
    assert Meeting.query.filter_by(project_id=project.id).count() == before_meetings
    assert db.session.get(type(project), project.id).status == "COMPLETED"


def test_terminal_project_blocks_legacy_task_and_operationstep_execution_side_doors(ctx):
    from eason_one.models import AgentRun, OperationStep, Task
    from eason_one.services import codex_connector, operations, task_execution

    project = core._project(("Founder criterion",))
    operation = core._operation(project)
    work = _work(project, operation, state="READY", title="Legacy task terminal fence fixture")
    researcher = Employee.query.filter_by(slug="researcher").one()
    critic = Employee.query.filter_by(slug="critic").one()
    task = Task(
        project_id=project.id,
        operation_id=operation.id,
        work_id=work.id,
        title="Legacy adapter task",
        objective="Must never execute after terminal closure.",
        status="ASSIGNED",
        priority="HIGH",
        created_by_employee_id=Employee.query.filter_by(slug="ceo").one().id,
        assigned_employee_id=researcher.id,
        reviewer_employee_id=critic.id,
        required_output="No output",
        acceptance_criteria="No post-terminal execution",
    )
    db.session.add(task)
    project.status = "COMPLETED"
    db.session.commit()
    before_runs = AgentRun.query.count()
    before_steps = OperationStep.query.filter_by(operation_id=operation.id).count()

    for call, expected in (
        (
            lambda: task_execution.run_task(task),
            "PROJECT_TERMINAL_TASK_EXECUTION_FORBIDDEN:COMPLETED",
        ),
        (
            lambda: operations._run_task_step(operation, "rc10-terminal-task-step", task),
            "PROJECT_TERMINAL_OPERATION_TASK_FORBIDDEN:COMPLETED",
        ),
        (
            lambda: codex_connector.run_codex_task(task),
            "PROJECT_TERMINAL_CODEX_EXECUTION_FORBIDDEN:COMPLETED",
        ),
    ):
        try:
            call()
            assert False, "terminal Project must reject every legacy task execution side door"
        except ValueError as exc:
            assert str(exc) == expected
        db.session.rollback()
        project = db.session.get(type(project), project.id)
        operation = db.session.get(type(operation), operation.id)
        task = db.session.get(Task, task.id)

    assert AgentRun.query.count() == before_runs
    assert OperationStep.query.filter_by(operation_id=operation.id).count() == before_steps
    assert project.status == "COMPLETED"
    assert work.state == "READY"


def test_terminal_project_blocks_legacy_review_before_skip_claim_or_replay_mutation(ctx):
    from eason_one.models import AgentRun, OperationStep, Task, WorkMessage
    from eason_one.services import operations, reviews

    project = core._project(("Founder criterion",))
    operation = core._operation(project)
    work = _work(project, operation, state="VERIFYING", title="Legacy review terminal fence fixture")
    researcher = Employee.query.filter_by(slug="researcher").one()
    critic = Employee.query.filter_by(slug="critic").one()
    task = Task(
        project_id=project.id,
        operation_id=operation.id,
        work_id=work.id,
        title="Legacy review adapter task",
        objective="Must never review after terminal closure.",
        status="REVIEW",
        priority="HIGH",
        created_by_employee_id=Employee.query.filter_by(slug="ceo").one().id,
        assigned_employee_id=researcher.id,
        reviewer_employee_id=critic.id,
        required_output="Existing result",
        acceptance_criteria="No post-terminal review",
        result_summary="Persisted result",
    )
    db.session.add(task)
    project.status = "COMPLETED"
    db.session.commit()
    before_runs = AgentRun.query.count()
    before_steps = OperationStep.query.filter_by(operation_id=operation.id).count()
    before_messages = WorkMessage.query.filter_by(project_id=project.id).count()

    try:
        reviews.run_review(task)
        assert False, "terminal Project must reject direct legacy review"
    except ValueError as exc:
        assert str(exc) == "PROJECT_TERMINAL_TASK_REVIEW_FORBIDDEN:COMPLETED"
    db.session.rollback()

    project = db.session.get(type(project), project.id)
    operation = db.session.get(type(operation), operation.id)
    task = db.session.get(Task, task.id)
    try:
        operations._run_review_step(operation, "rc10-terminal-review-step", task)
        assert False, "terminal Project must reject OperationStep review before claim"
    except ValueError as exc:
        assert str(exc) == "PROJECT_TERMINAL_OPERATION_REVIEW_FORBIDDEN:COMPLETED"
    db.session.rollback()

    assert AgentRun.query.count() == before_runs
    assert OperationStep.query.filter_by(operation_id=operation.id).count() == before_steps
    assert WorkMessage.query.filter_by(project_id=project.id).count() == before_messages
    assert db.session.get(type(project), project.id).status == "COMPLETED"
    assert db.session.get(Work, work.id).state == "VERIFYING"


def test_terminal_project_blocks_generic_escalation_legacy_task_and_market_award_creation(ctx):
    from eason_one.models import Escalation, Task
    from eason_one.services import escalations, market, tasks

    project = core._project(("Founder criterion",))
    operation = core._operation(project)
    work = _work(project, operation, state="READY", title="Terminal authority surface fixture")
    project.status = "COMPLETED"
    db.session.commit()
    before_escalations = Escalation.query.filter_by(project_id=project.id).count()
    before_tasks = Task.query.filter_by(project_id=project.id).count()

    try:
        escalations.open_escalation(
            project_id=project.id,
            escalation_type="SYSTEM_RECOVERY",
            reason="Must not mint new operational attention after terminal closure.",
            work_id=work.id,
            operation_id=operation.id,
        )
        assert False, "terminal Project must reject new generic Escalation"
    except ValueError as exc:
        assert str(exc) == "PROJECT_TERMINAL_ESCALATION_CREATION_FORBIDDEN:COMPLETED"
    db.session.rollback()

    project = db.session.get(type(project), project.id)
    try:
        tasks.create_task(
            project,
            "Forbidden legacy task",
            "Must not create post-terminal Task truth.",
            creator=Employee.query.filter_by(slug="ceo").one(),
        )
        assert False, "terminal Project must reject legacy Task creation"
    except ValueError as exc:
        assert str(exc) == "PROJECT_TERMINAL_LEGACY_TASK_WRITER_FORBIDDEN:COMPLETED"
    db.session.rollback()

    project = db.session.get(type(project), project.id)
    work = db.session.get(Work, work.id)
    try:
        market._create_award_generation(
            work,
            "RESEARCH",
            [],
            generation=1,
        )
        assert False, "terminal Project must reject new internal-market authority"
    except ValueError as exc:
        assert str(exc) == "PROJECT_TERMINAL_MARKET_AWARD_FORBIDDEN:COMPLETED"
    db.session.rollback()

    assert Escalation.query.filter_by(project_id=project.id).count() == before_escalations
    assert Task.query.filter_by(project_id=project.id).count() == before_tasks
    assert db.session.get(type(project), project.id).status == "COMPLETED"


def test_terminal_project_blocks_meeting_idempotency_before_request_step_write(ctx):
    from eason_one.models import MeetingStep
    from eason_one.services import meetings

    project = core._project(("Founder criterion",))
    ceo = Employee.query.filter_by(slug="ceo").one()
    researcher = Employee.query.filter_by(slug="researcher").one()
    meeting = meetings.create(
        "Terminal idempotency fence fixture",
        "Existing Meeting must not mutate after terminal closure.",
        "No post-terminal request step may be written.",
        ceo,
        [ceo, researcher],
        project=project,
    )
    before_steps = MeetingStep.query.filter_by(meeting_id=meeting.id).count()
    project.status = "COMPLETED"
    db.session.commit()

    try:
        meetings.next_step_idempotent(meeting, "rc10-terminal-meeting-request")
        assert False, "terminal Project must reject Meeting mutation before idempotency breadcrumb write"
    except ValueError as exc:
        assert str(exc) == "PROJECT_TERMINAL_MEETING_MUTATION_FORBIDDEN:NEXT_STEP_IDEMPOTENT:COMPLETED"

    assert MeetingStep.query.filter_by(meeting_id=meeting.id).count() == before_steps
    assert db.session.get(type(project), project.id).status == "COMPLETED"


def test_terminal_operation_claim_replay_retires_local_claim_without_provider_or_domain_materialization(ctx):
    from eason_one.models import AgentRun, OperationStep
    from eason_one.services import operations

    project = core._project(("Founder criterion",))
    operation = core._operation(project)
    step = OperationStep(
        operation_id=operation.id,
        idempotency_key="rc10-terminal-claimed-step",
        logical_key="rc10:terminal:claimed",
        kind="REPORT",
        status="CLAIMED",
    )
    db.session.add(step)
    db.session.commit()
    before_runs = AgentRun.query.count()
    project.status = "COMPLETED"
    db.session.commit()

    result = operations.next_step(operation, "rc10-terminal-claimed-step")
    db.session.refresh(step)
    db.session.refresh(project)

    assert result["status"] == "FAILED_PRE_PROVIDER_TERMINAL"
    assert result["project_status"] == "COMPLETED"
    assert result["provider_replayed"] is False
    assert step.status == "FAILED_PRE_PROVIDER_TERMINAL"
    assert step.agent_run_id is None
    assert step.finished_at is not None
    assert AgentRun.query.count() == before_runs
    assert project.status == "COMPLETED"


def test_terminal_project_open_founder_gate_is_historical_not_attention(ctx):
    from eason_one.models import Escalation
    from eason_one.services import governance

    project = core._project(("Founder criterion",))
    operation = core._operation(project)
    work = _work(project, operation, state="WAITING", title="Terminal attention sanitation fixture")
    gate = Escalation(
        project_id=project.id,
        work_id=work.id,
        operation_id=operation.id,
        escalation_type="BUDGET_AUTHORIZATION",
        state="OPEN",
        reason="Historical unanswered Founder authority row.",
        created_by_employee_id=Employee.query.filter_by(slug="ceo").one().id,
    )
    db.session.add(gate)
    db.session.commit()

    assert governance.current_gate(project).id == gate.id
    assert [row.id for row in governance.attention(project)] == [gate.id]

    project.status = "COMPLETED"
    db.session.commit()

    assert governance.current_gate(project) is None
    assert governance.project_blocking_gate(project) is None
    assert governance.blocks_work(work) is False
    assert governance.attention(project) == []
    assert db.session.get(Escalation, gate.id).state == "OPEN"


def test_terminal_project_truth_freezes_operating_residuals_as_history(ctx):
    from eason_one.models import Escalation
    from eason_one.services import company_truth, meetings

    project = core._project(("Founder criterion",))
    operation = core._operation(project)
    work = _work(project, operation, state="WAITING", title="Terminal residual projection fixture")
    work_runtime.open_wait(
        work,
        "RECONCILIATION",
        "Historical reconciliation wait must remain stored but stop projecting as live.",
        issue_code="RC11:HISTORICAL_WAIT",
    )
    gate = Escalation(
        project_id=project.id,
        work_id=work.id,
        operation_id=operation.id,
        escalation_type="SYSTEM_RECOVERY",
        state="OPEN",
        reason="Historical internal escalation.",
        created_by_employee_id=Employee.query.filter_by(slug="ceo").one().id,
    )
    db.session.add(gate)
    ceo = Employee.query.filter_by(slug="ceo").one()
    researcher = Employee.query.filter_by(slug="researcher").one()
    meeting = meetings.create(
        "Terminal residual meeting",
        "Historical meeting row must not remain present-tense activity.",
        "Preserve history only.",
        ceo,
        [ceo, researcher],
        project=project,
    )
    meeting.operation_id = operation.id
    db.session.commit()

    project.status = "COMPLETED"
    db.session.commit()
    view = company_truth.project_snapshot(project)

    assert view["state"] == "COMPLETED"
    assert view["open_waits"] == []
    assert view["open_escalations"] == []
    assert view["open_founder_escalations"] == []
    assert view["open_internal_escalations"] == []
    assert view["active_executions"] == []
    assert view["open_meetings"] == []
    assert view["historical_terminal_residuals"]["open_wait_count"] >= 1
    assert view["historical_terminal_residuals"]["open_escalation_count"] >= 1
    assert view["historical_terminal_residuals"]["open_meeting_count"] >= 1
    assert db.session.get(type(meeting), meeting.id) is not None
    assert db.session.get(Escalation, gate.id).state == "OPEN"


def test_terminal_assignment_releases_team_formation_workload_without_deleting_history(ctx):
    from eason_one.services import team_formation

    project = core._project(("Founder criterion",))
    operation = core._operation(project)
    work = _work(project, operation, state="WAITING", title="Terminal capacity sanitation fixture")
    researcher = Employee.query.filter_by(slug="researcher").one()

    assert team_formation._active_workload(researcher.id) == 1
    assignment = WorkAssignment.query.filter_by(work_id=work.id, ended_at=None).one()

    project.status = "COMPLETED"
    db.session.commit()

    assert team_formation._active_workload(researcher.id) == 0
    capacity = ceo_operating.employee_capacity_view(researcher)
    assert capacity["capacity"] == "AVAILABLE"
    assert capacity["active_responsibilities"] == []
    assert db.session.get(WorkAssignment, assignment.id).ended_at is None


def test_terminal_project_does_not_pollute_company_or_employee_live_projection(ctx):
    from eason_one.models import Task
    from eason_one.services import command, company_runtime, current_company

    project = core._project(("Founder criterion",))
    operation = core._operation(project)
    work = _work(project, operation, state="WAITING", title="Terminal dashboard sanitation fixture")
    researcher = Employee.query.filter_by(slug="researcher").one()
    critic = Employee.query.filter_by(slug="critic").one()
    task = Task(
        project_id=project.id,
        operation_id=operation.id,
        work_id=work.id,
        title="Historical task adapter",
        objective="Must not project as current work after terminal closure.",
        status="BLOCKED",
        priority="HIGH",
        created_by_employee_id=Employee.query.filter_by(slug="ceo").one().id,
        assigned_employee_id=researcher.id,
        reviewer_employee_id=critic.id,
        required_output="Historical only",
        acceptance_criteria="No terminal ghost activity",
    )
    db.session.add(task)
    db.session.commit()

    project.status = "COMPLETED"
    db.session.commit()

    runtime = company_runtime.runtime_snapshot(operation)
    projection = current_company.projection()
    project_row = next(row for row in projection["operations"] if row["operation"].id == operation.id)

    assert runtime["active"] is False
    assert runtime["active_work"] is None
    assert runtime["active_execution_ids"] == []
    assert runtime["management_gate"] == []
    assert project_row["classification"] == "TERMINAL"
    assert not any(row.id == task.id for row in projection["active_tasks"])
    assert command.employee_status(researcher) == "AVAILABLE"


def test_terminal_ready_work_is_not_global_runtime_or_command_focus(ctx):
    from eason_one.services import project_company, stabilization

    project = core._project(("Founder criterion",))
    operation = core._operation(project)
    work = _work(project, operation, state="READY", title="Terminal global focus sanitation fixture")
    project.status = "COMPLETED"
    db.session.commit()

    runtime = stabilization.global_runtime_snapshot()
    command = project_company.work_command_snapshot()
    project_history = project_company.work_command_snapshot(project.id)

    assert runtime["active"] is False
    assert runtime["queued_work"] is None
    assert not any(row["work"].id == work.id for row in command["rows"])
    assert any(row["work"].id == work.id for row in project_history["rows"])


def test_terminal_history_cannot_reenter_portfolio_or_company_plan_from_audit_snapshot(ctx):
    from eason_one.models import Escalation
    from eason_one.services import ceo_management

    terminal = core._project(("Terminal criterion",))
    terminal_operation = core._operation(terminal)
    terminal_work = _work(
        terminal,
        terminal_operation,
        state="READY",
        title="Historical terminal portfolio responsibility",
    )
    gate = Escalation(
        project_id=terminal.id,
        work_id=terminal_work.id,
        operation_id=terminal_operation.id,
        escalation_type="BUDGET_AUTHORIZATION",
        state="OPEN",
        reason="Historical terminal Founder gate must not re-enter current portfolio truth.",
        created_by_employee_id=Employee.query.filter_by(slug="ceo").one().id,
    )
    db.session.add(gate)
    terminal.status = "COMPLETED"

    current = core._project(("Current criterion",))
    current_operation = core._operation(current)
    current_work = _work(
        current,
        current_operation,
        state="READY",
        title="Current portfolio responsibility",
    )
    db.session.commit()

    audit_snapshot = ceo_operating.company_operating_snapshot(include_terminal=True)
    assert {row["project_id"] for row in audit_snapshot["projects"]} == {terminal.id, current.id}

    review = ceo_management.portfolio_review(audit_snapshot)
    plan = ceo_management.company_plan(audit_snapshot, review)

    assert terminal.id in review["historical_terminal_project_ids"]
    assert review["terminal_history_has_operating_effect"] is False
    assert terminal.id not in {row["project_id"] for row in review["project_operating_order"]}
    assert terminal.id not in {row["project_id"] for row in review["operating_focus"]}
    assert terminal.id not in {int(row.get("project_id") or 0) for row in review["founder_escalations"]}
    assert not any(row.get("work_id") == terminal_work.id for row in review["allocation_decisions"])
    assert current.id in {row["project_id"] for row in review["project_operating_order"]}
    assert current.id in {row["project_id"] for row in review["operating_focus"]}

    assert plan["terminal_history_has_operating_effect"] is False
    assert terminal.id not in {row["project_id"] for row in plan["founder_priorities"]}
    assert terminal.id not in {row["project_id"] for row in plan["project_operating_states"]}
    assert terminal.id not in {int(row.get("project_id") or 0) for row in plan["pending_founder_dependencies"]}
    current_snapshot = ceo_operating.company_operating_snapshot()
    current_review = ceo_management.portfolio_review(current_snapshot)
    current_plan = ceo_management.company_plan(current_snapshot, current_review)
    assert plan["plan_hash"] == current_plan["plan_hash"]


def test_terminal_mission_remains_registry_history_but_never_becomes_current_hq_focus(ctx):
    from eason_one.services import headquarters

    project = core._project(("Founder criterion",))
    operation = core._operation(project)
    project.status = "COMPLETED"
    db.session.commit()

    hq = headquarters.headquarters_snapshot()
    missions = headquarters.missions_snapshot("real")
    mobile = headquarters.mobile_snapshot()

    assert any(row["operation"].id == operation.id for row in missions["groups"]["completed"])
    assert missions["focus"] is None
    assert hq["focus"] is None
    assert mobile["focus"] is None
    assert hq["ceo_state"] == "AVAILABLE"


def test_terminal_running_meeting_is_history_not_employee_or_headquarters_presence(ctx):
    from eason_one.services import headquarters, meetings

    project = core._project(("Founder criterion",))
    operation = core._operation(project)
    ceo = Employee.query.filter_by(slug="ceo").one()
    researcher = Employee.query.filter_by(slug="researcher").one()
    meeting = meetings.create(
        "Terminal presence sanitation fixture",
        "This live Meeting must become history when its Project closes.",
        "Preserve history without current presence.",
        ceo,
        [ceo, researcher],
        project=project,
    )
    meeting.status = "RUNNING"
    db.session.commit()

    project.status = "COMPLETED"
    db.session.commit()

    view = headquarters._meeting_view(meeting)
    employee = headquarters.employee_presence(researcher)
    hq = headquarters.headquarters_snapshot()
    meetings_view = headquarters.meetings_snapshot()

    assert view["historical"] is True
    assert view["is_live"] is False
    assert employee["state"] == "AVAILABLE"
    assert employee["meeting"] is None
    assert hq["live_meeting"] is None
    assert not any(row["meeting"].id == meeting.id for row in hq["meetings"])
    assert any(row["meeting"].id == meeting.id for row in meetings_view["groups"]["completed"])
    assert terminal_project_ids(meetings_view["projects"]) == set()


def terminal_project_ids(projects):
    return {
        project.id
        for project in projects
        if str(project.status or "").upper() in {"COMPLETED", "FAILED", "CANCELLED"}
    }


def test_ceo_current_scope_fallback_never_auto_selects_terminal_project_or_operation(ctx):
    from eason_one.services import ceo as ceo_service

    terminal = core._project(("Terminal criterion",))
    terminal_operation = core._operation(terminal)
    terminal.status = "COMPLETED"
    db.session.commit()

    operation, project = ceo_service._select_real_scope("continue project")
    assert operation is None
    assert project is None

    current = core._project(("Current criterion",))
    current_operation = core._operation(current)
    db.session.commit()

    operation, project = ceo_service._select_real_scope("continue project")
    assert project.id == current.id
    assert operation.id == current_operation.id
    assert operation.id != terminal_operation.id
