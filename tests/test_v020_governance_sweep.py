from decimal import Decimal
from types import SimpleNamespace

from eason_one.extensions import db
from eason_one.models import Employee, Operation, Project, Work, now
from eason_one.schemas import CEO_SCHEMA
from eason_one.services import (
    acceptance_contract, company_kernel, company_truth, governance,
    host_validation, operations, project_contract, work_runtime,
)


def _project():
    ceo = Employee.query.filter_by(slug="ceo").one()
    project = Project(
        name="Sweep Project", objective="Deliver deterministic HTTP proof.",
        status="ACTIVE", priority="HIGH", environment="LIVE", origin="TEST",
        owner_employee_id=ceo.id, real_budget_limit=Decimal("20"),
        known_constraints="Stay bounded.",
    )
    db.session.add(project); db.session.flush()
    project_contract.freeze(
        project,
        success_criteria=[
            "GET /api/sweep returns HTTP 200.",
            'Response JSON contains service="eason-one" and current application version.',
        ],
        constraints=["Stay bounded."], origin_employee_id=ceo.id,
    )
    db.session.commit()
    return project


def _operation(project):
    ceo = Employee.query.filter_by(slug="ceo").one()
    operation = Operation(
        title="Sweep Mission", objective="Deliver bounded proof", project_id=project.id,
        proposed_by_employee_id=ceo.id, status="RUNNING", kernel_status="RUNNING",
        route_type="SINGLE_WORKER", current_stage="COMPANY_KERNEL_V020",
        plan_json={"mode":"OPERATION_PLAN","executive_response":"go","operation":{
            "title":"Sweep Mission","objective":"Deliver bounded proof","project_id":project.id,
            "budget_twd":"5","tasks":[],"meeting_policy":"NEVER","meeting_config":{
                "trigger":"NEVER","participant_employee_ids":[],"max_rounds":1,
                "max_speakers_per_round":1,"contribution_output_cap":192,"token_limit":6000,
                "budget_twd":0,"retry_limit":0},"completion_criteria":["Evidence delivered"]}},
        approved_budget_twd=Decimal("5"), hard_cost_cap_twd=Decimal("5"),
        stage_cost_cap_twd=Decimal("5"), single_call_cost_cap_twd=Decimal("5"),
        max_calls=6, max_revisions=1, max_messages=12, max_elapsed_seconds=3600,
        approved_at=now(), memory_json={"runtime_semantics":"WORK_CORE_V018","core_rebuild_version":"0.20.0"},
    )
    db.session.add(operation); db.session.commit()
    return operation


def _work(project, operation, *, state="READY", work_type="DELIVERY", title="Sweep work"):
    engineer = Employee.query.filter_by(slug="engineer").one()
    work = Work(
        project_id=project.id, operation_id=operation.id, title=title,
        purpose="GET /api/sweep must return HTTP 200 with current application version and preserve health check.",
        expected_output="Artifact",
        acceptance_criteria=(
            "GET /api/sweep must return HTTP 200.\n"
            'Response JSON contains service="eason-one" and current application version.\n'
            "Existing health check remains HTTP 200."
        ),
        state=state, work_type=work_type, priority="HIGH",
        created_by_employee_id=project.owner_employee_id,
        resource_ceiling_twd=Decimal("5"), retry_limit=1,
    )
    db.session.add(work); db.session.flush()
    if work_type != "MANAGEMENT":
        from eason_one.models import WorkAssignment
        db.session.add(WorkAssignment(
            work_id=work.id, employee_id=engineer.id, responsibility="OWNER",
            assigned_by_employee_id=project.owner_employee_id,
            reason="Sweep test owner",
        ))
    db.session.commit()
    return work


def test_authority_schema_keeps_full_bounded_acceptance_contract():
    operation = CEO_SCHEMA["schema"]["properties"]["operation"]["anyOf"][0]["properties"]
    task = operation["tasks"]["items"]["properties"]
    assert task["acceptance_criteria"]["maxItems"] == 8
    assert task["acceptance_criteria"]["items"]["maxLength"] == 280
    assert operation["completion_criteria"]["maxItems"] == 8
    assert operation["completion_criteria"]["items"]["maxLength"] == 280
    project = CEO_SCHEMA["schema"]["properties"]["project"]["anyOf"][0]["properties"]
    assert project["success_criteria"]["items"]["maxLength"] == 280


def test_operation_plan_sanitizes_provider_control_garbage_before_freeze(ctx):
    engineer = Employee.query.filter_by(slug="engineer").one()
    payload = {
        "mode":"OPERATION_PLAN", "executive_response":"Do it\ufffc safely",
        "operation":{
            "title":"Ping\ufffc delivery", "objective":"Implement\ufffd endpoint", "project_id":None,
            "budget_twd":1, "tasks":[{
                "title":"Implement ping", "objective":"Create endpoint\ufffc",
                "assignee_employee_id":engineer.id, "reviewer_employee_id":None,
                "acceptance_criteria":[
                    "GET /api/ping returns HTTP 200.\ufffc",
                    'Response JSON contains service="eason-one".\ufffc',
                    "Current application version is returned.\ufffc",
                    "Existing health check remains HTTP 200.\ufffc",
                ],
                "required_capabilities":["SOFTWARE_ENGINEERING"],
                "write_scope":{"version":"CODEX_WRITE_SCOPE_V1","paths":["eason_one/routes.py"]},
            }],
            "meeting_policy":"NEVER", "meeting_config":{
                "trigger":"NEVER","participant_employee_ids":[],"max_rounds":1,
                "max_speakers_per_round":1,"contribution_output_cap":192,
                "token_limit":6000,"budget_twd":0,"retry_limit":0,
            },
            "completion_criteria":["All four criteria are proven.\ufffc"],
        },
    }
    result = operations.validate_plan(payload)
    encoded = str(result)
    assert "\ufffc" not in encoded and "\ufffd" not in encoded
    assert len(result["operation"]["tasks"][0]["acceptance_criteria"]) == 4


def test_response_only_http_criterion_uses_single_frozen_endpoint_context():
    criterion = 'Response JSON contains service="eason-one", status="ok", and current application version.'
    context = "GET /api/founder-ping returns HTTP 200.\n" + criterion
    assert acceptance_contract.classify_criterion(
        criterion, task_context=context
    ) == "HOST_HTTP_CONTRACT"


def test_http_contract_requires_exact_200_current_version_and_health():
    task = SimpleNamespace(
        project=None, operation=None, title="Founder ping",
        objective="GET /api/founder-ping",
        acceptance_criteria=(
            'Return HTTP 200 and response JSON contains service="eason-one", status="ok", '
            "and current application version. Existing health check must remain healthy."
        ),
    )
    contract = host_validation._extract_http_contract(task)
    assert contract["method"] == "GET"
    assert contract["path"] == "/api/founder-ping"
    assert contract["expected_status"] == 200
    assert contract["require_current_version"] is True
    assert contract["version_source"] == "pyproject.toml:[project].version"
    assert contract["require_health_check"] is True
    assert contract["health_check"] == {"method":"GET", "path":"/api/healthz", "expected_status":200}


def test_different_founder_authority_needs_are_queued_not_silently_superseded(ctx):
    project = _project(); operation = _operation(project)
    first_work = _work(project, operation, title="First")
    second_work = _work(project, operation, title="Second")
    first = governance.open_gate(
        project=project, operation=operation, work=first_work,
        escalation_type="CODEX_RISK_APPROVAL", reason="Approve exact A",
        authority_payload={"requested_action":{"command":"A"}},
    )
    second = governance.open_gate(
        project=project, operation=operation, work=second_work,
        escalation_type="EXTERNAL_EFFECT_AUTHORIZATION", reason="Approve exact B",
        authority_payload={"requested_action":{"command":"B"}},
    )
    db.session.commit(); db.session.refresh(first); db.session.refresh(second)
    assert first.state == "OPEN"
    assert second.state == "OPEN"
    # FIFO: a later need must not jump ahead of an unanswered Founder question.
    assert governance.current_gate(project).id == first.id
    assert governance.attention(project)[0].id == first.id

    governance.resolve_gate(first, "REJECT", reason="No Codex exception")
    db.session.refresh(second); db.session.refresh(project)
    assert second.state == "OPEN"
    assert governance.current_gate(project).id == second.id
    assert project.status == "BLOCKED"


def test_budget_reject_terminalizes_exact_delivery_attempt_and_does_not_resume_it(ctx):
    project = _project(); operation = _operation(project)
    work = _work(project, operation, state="EXECUTING")
    gate = governance.request_budget_gate(
        project=project, operation=operation, work=work,
        additional_twd=Decimal("2"), reason="Exact deterministic shortfall",
    )
    work_runtime.open_wait(work, "FOUNDER_DECISION", "Exact deterministic shortfall")
    db.session.commit()
    decision = governance.resolve_gate(gate, "REJECT", reason="Keep current cap")
    db.session.refresh(work); db.session.refresh(project)
    assert decision.decision == "REJECT"
    assert work.state == "CANCELLED"
    assert project.status == "BLOCKED"
    assert not work_runtime.has_open_gate(work, "FOUNDER_DECISION")
    assert company_kernel._mission_has_failure(operation) is True


def test_current_authority_wait_outranks_historical_abandoned_work_in_truth(ctx):
    project = _project(); operation = _operation(project)
    old = _work(project, operation, state="ABANDONED", title="Old failed attempt")
    management = work_runtime.ensure_management_work(operation)
    work_runtime.open_wait(
        management, "AUTHORITY_EXHAUSTED",
        "Founder rejected additional budget under the current Contract.",
    )
    project.status = "BLOCKED"
    db.session.commit()
    snap = company_truth.project_snapshot(project)
    assert old.state == "ABANDONED"
    assert snap["state"] == "BLOCKED"
    assert "rejected additional budget" in snap["reason"]


def test_platform_recovery_reopen_is_explicit_and_audited(ctx):
    project = _project(); operation = _operation(project)
    work = _work(project, operation, state="ABANDONED", title="Platform-poisoned work")
    work.abandoned_at = now(); db.session.commit()
    work_runtime.reopen_abandoned(work, "READY", reason="Proven platform defect")
    db.session.commit(); db.session.refresh(work)
    assert work.state == "READY" and work.abandoned_at is None
    from eason_one.models import CompanyEvent
    event = CompanyEvent.query.filter_by(event_type="WORK_SYSTEM_REOPENED", work_id=work.id).one()
    assert (event.payload_json or {}).get("reason") == "Proven platform defect"


def test_generic_internal_escalation_does_not_freeze_project_progress(ctx):
    project = _project(); operation = _operation(project)
    management = work_runtime.ensure_management_work(operation)
    from eason_one.models import Escalation
    db.session.add(Escalation(
        project_id=project.id, operation_id=operation.id, work_id=management.id,
        escalation_type="RECONCILIATION", state="OPEN", reason="Internal evidence repair",
    ))
    db.session.commit()
    assert governance.attention(project) == []


def test_result_ready_refuses_open_founder_authority_or_active_delivery(ctx):
    project = _project(); operation = _operation(project)
    active = _work(project, operation, state="READY")
    evaluation = {"overall_status":"SATISFIED","contract_hash":"x","authority_hash":"y","criteria":[],"accepted_work_ids":[]}
    import pytest
    from eason_one.services import project_outcome
    with pytest.raises(ValueError, match="ACTIVE_WORK"):
        project_outcome.close_result_ready(project, evaluation)
    work_runtime.transition(active, "CANCELLED", reason="test cleanup")
    governance.request_budget_gate(project=project, additional_twd=1, reason="Need exact extra budget")
    db.session.commit()
    with pytest.raises(ValueError, match="FOUNDER_AUTHORITY"):
        project_outcome.close_result_ready(project, evaluation)


def test_queued_founder_gate_cannot_be_resolved_out_of_order_but_project_cancel_terminates_queue(ctx):
    project = _project(); operation = _operation(project)
    first_work = _work(project, operation, title="First queued authority")
    second_work = _work(project, operation, title="Second queued authority")
    first = governance.open_gate(
        project=project, operation=operation, work=first_work,
        escalation_type="CODEX_RISK_APPROVAL", reason="Approve A",
        authority_payload={"requested_action":{"command":"A"}},
    )
    second = governance.open_gate(
        project=project, operation=operation, work=second_work,
        escalation_type="EXTERNAL_EFFECT_AUTHORIZATION", reason="Approve B",
        authority_payload={"requested_action":{"command":"B"}},
    )
    import pytest
    with pytest.raises(ValueError, match="FOUNDER_GATE_NOT_CURRENT"):
        governance.resolve_gate(second, "REJECT", reason="out of order")
    cancel = governance.open_gate(
        project=project, escalation_type="PROJECT_CANCEL",
        reason="Founder explicitly cancels the Project", authority_payload={},
    )
    governance.resolve_gate(cancel, "APPROVE", reason="Founder cancelled entire Project")
    db.session.commit(); db.session.refresh(first); db.session.refresh(second); db.session.refresh(project)
    assert project.status == "CANCELLED"
    assert first.state == "RESOLVED" and first.resolution == "PROJECT_CANCELLED"
    assert second.state == "RESOLVED" and second.resolution == "PROJECT_CANCELLED"
    assert governance.attention(project) == []


def test_restart_adoption_does_not_reopen_blocked_or_result_ready_project(ctx):
    project = _project(); operation = _operation(project)
    management = work_runtime.ensure_management_work(operation)
    work_runtime.open_wait(management, "SYSTEM_RECOVERY", "Repair required")
    project.status = "BLOCKED"
    db.session.commit()
    company_kernel.adopt_operation(operation)
    db.session.refresh(project)
    assert project.status == "BLOCKED"

    work_runtime.resolve_waits(management, "SYSTEM_RECOVERY", note="test")
    project.status = "REVIEW"
    db.session.commit()
    company_kernel.adopt_operation(operation)
    db.session.refresh(project)
    assert project.status == "REVIEW"


def test_project_hard_blocker_prevents_unrelated_active_revival(ctx):
    from eason_one.services import project_outcome
    project = _project(); operation = _operation(project)
    management = work_runtime.ensure_management_work(operation)
    work_runtime.open_wait(management, "RECONCILIATION", "Current durable reconciliation blocker")
    project.status = "BLOCKED"
    db.session.commit()
    assert work_runtime.project_can_activate(project) is False
    blockers = work_runtime.project_hard_blockers(project)
    assert any(row.get("condition_type") == "RECONCILIATION" for row in blockers)
    project_outcome.mark_continuation_needed(
        project,
        {"criteria":[{"criterion":"Need proof","status":"UNPROVEN"}]},
        reason="A historical Mission failed",
    )
    db.session.refresh(project)
    assert project.status == "BLOCKED"
    assert "Current durable reconciliation blocker" in str(work_runtime.project_hard_blockers(project)[0].get("reason"))


def test_rejected_delivery_budget_places_management_on_authority_exhausted(ctx):
    project = _project(); operation = _operation(project)
    delivery = _work(project, operation, state="EXECUTING", title="Needs budget")
    gate = governance.request_budget_gate(
        project=project, operation=operation, work=delivery,
        additional_twd=Decimal("1.25"), reason="Exact delivery shortfall",
    )
    work_runtime.open_wait(delivery, "FOUNDER_DECISION", "Exact delivery shortfall")
    db.session.commit()
    governance.resolve_gate(gate, "REJECT", reason="Keep Project cap")
    db.session.refresh(delivery); db.session.refresh(project)
    management = work_runtime.ensure_management_work(operation)
    assert delivery.state == "CANCELLED"
    assert work_runtime.has_open_gate(management, "AUTHORITY_EXHAUSTED")
    assert project.status == "BLOCKED"
    assert work_runtime.project_can_activate(project) is False


def test_codex_boundary_is_frozen_on_work_and_ignores_later_operation_memory(ctx):
    from eason_one.models import Task
    from eason_one.services import codex_connector
    project = _project(); operation = _operation(project)
    work = _work(project, operation, title="Read-only governed inspection")
    work.purpose = "Read-only analysis; do not modify the repository."
    work.acceptance_criteria = "Report existing repository evidence without modifying files."
    engineer = Employee.query.filter_by(slug="engineer").one()
    task = Task(
        project_id=project.id, operation_id=operation.id, work_id=work.id,
        title=work.title, objective=work.purpose, status="ASSIGNED", priority="HIGH",
        created_by_employee_id=project.owner_employee_id,
        assigned_employee_id=engineer.id, reviewer_employee_id=None,
        required_output="Read-only evidence", acceptance_criteria=work.acceptance_criteria,
    )
    db.session.add(task); db.session.flush()
    first = codex_connector.ensure_execution_boundary(work, task)
    db.session.commit()
    assert first["version"] == "CODEX_EXECUTION_BOUNDARY_V2"
    assert first["read_only"] is True
    assert first["project_execution_terms_hash"] == project_contract.execution_terms_hash(project)
    first_hash = first["boundary_hash"]
    memory = dict(operation.memory_json or {})
    memory["engineering"] = {
        "repo_path": "C:\\should-not-win",
        "allowed_paths": ["everything"],
        "forbidden_paths": [],
        "max_changed_files": 999,
    }
    operation.memory_json = memory
    db.session.commit()
    second = codex_connector.ensure_execution_boundary(work, task)
    assert second == first
    assert second["boundary_hash"] == first_hash
    spec = codex_connector.build_job_spec(task)
    assert "READ ONLY" in spec
    assert "Maximum changed files: 0" in spec
    assert "NO FILE WRITES AUTHORIZED" in spec
    assert "C:\\should-not-win" not in spec
