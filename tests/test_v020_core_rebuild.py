from decimal import Decimal
import os

import pytest

from eason_one.extensions import db
from eason_one.models import (
    AgentRun, Artifact, ArtifactVersion, Employee, Escalation, KnowledgeItem, Operation, Project,
    VerificationRecord, Work, now,
)
from eason_one.services import company_kernel, company_truth, operation_kernel, operations, project_contract, project_outcome


def _project(success=("Founder criterion",)):
    ceo = Employee.query.filter_by(slug="ceo").one()
    project = Project(
        name="v0.20 Project", objective="Deliver the Founder outcome.", status="ACTIVE",
        priority="HIGH", environment="LIVE", origin="TEST", owner_employee_id=ceo.id,
        real_budget_limit=Decimal("1000"), known_constraints="Do not expand authority.",
    )
    db.session.add(project); db.session.flush()
    project_contract.freeze(project, success_criteria=list(success), constraints=["Do not expand authority."], origin_employee_id=ceo.id)
    db.session.commit()
    return project


def _operation(project, *, completion=("Mission criterion",), status="RUNNING"):
    ceo = Employee.query.filter_by(slug="ceo").one()
    operation = Operation(
        title="Bounded Mission", objective="Produce bounded evidence.", project_id=project.id,
        proposed_by_employee_id=ceo.id, status=status, kernel_status=status,
        route_type="FULL_PROJECT", current_stage="COMPANY_KERNEL_V020",
        plan_json={"mode":"OPERATION_PLAN","executive_response":"go","operation":{
            "title":"Bounded Mission","objective":"Produce bounded evidence.","project_id":project.id,
            "budget_twd":100,"tasks":[],"meeting_policy":"NEVER","meeting_config":{
                "trigger":"NEVER","participant_employee_ids":[],"max_rounds":1,
                "max_speakers_per_round":1,"contribution_output_cap":192,"token_limit":6000,
                "budget_twd":0,"retry_limit":0},"completion_criteria":list(completion)}},
        approved_budget_twd=Decimal("100"), hard_cost_cap_twd=Decimal("100"),
        stage_cost_cap_twd=Decimal("100"), single_call_cost_cap_twd=Decimal("100"),
        max_calls=8, max_revisions=1, max_messages=16, max_elapsed_seconds=3600,
        approved_at=now(), memory_json={"runtime_semantics":"WORK_CORE_V018","core_rebuild_version":"0.20.0"},
    )
    db.session.add(operation); db.session.flush(); return operation


def _accepted_work(project, operation, criterion):
    employee = Employee.query.filter_by(slug="researcher").one()
    work = Work(
        project_id=project.id, operation_id=operation.id, title="Accepted delivery",
        purpose="Produce evidence", expected_output="Artifact", acceptance_criteria=criterion,
        state="ACCEPTED", work_type="DELIVERY", priority="HIGH", created_by_employee_id=employee.id,
        resource_ceiling_twd=Decimal("50"), retry_limit=1, accepted_at=now(),
    )
    db.session.add(work); db.session.flush()
    artifact = Artifact(project_id=project.id, work_id=work.id, artifact_type="WORK_RESULT", title="Evidence")
    db.session.add(artifact); db.session.flush()
    version = ArtifactVersion(
        artifact_id=artifact.id, version=1, producer_employee_id=employee.id,
        status="ACCEPTED", content_text="durable accepted evidence", content_hash="a"*64, accepted_at=now(),
    )
    db.session.add(version); db.session.flush()
    db.session.add(VerificationRecord(
        work_id=work.id, artifact_version_id=version.id, method="HOST_ENGINEERING_VALIDATION",
        status="PASSED", details_json={"proof":"accepted"},
    ))
    db.session.commit()
    contract = project_contract.governing_terms(project)
    criteria = list(contract.get("success_criteria") or [])
    if criterion in criteria:
        reviewer = Employee.query.filter_by(slug="critic").one()
        model = reviewer.current_model
        db.session.add(AgentRun(
            employee_id=reviewer.id, project_id=project.id, operation_id=operation.id,
            work_id=work.id, model_config_id=model.id, purpose="PROJECT_OUTCOME_REVIEW",
            user_request="Review current Project evidence", system_prompt_snapshot="review",
            context_snapshot="evidence", status="SUCCEEDED", outcome="SUCCEEDED",
            parsed_output_json={"criteria":[{
                "criterion_id": f"P{index}", "status":"SATISFIED",
                "evidence":"Fixture independent Project review.", "work_ids":[work.id],
            } for index, _ in enumerate(criteria, 1)], "summary":"Fixture review passed."},
            context_composition_json={
                "contract_hash": contract["governing_contract_hash"],
                "project_outcome_input_hash": project_outcome.review_input_hash(project),
            },
            provider_key_snapshot=model.provider_key, model_name_snapshot=model.model_name,
            input_price_snapshot=0, output_price_snapshot=0,
            currency_snapshot="TWD", currency="TWD", real_cost=0,
        ))
        db.session.commit()
    return work



def test_vnext_project_management_can_run_after_mission_completion(ctx):
    project = _project(("Founder outcome requires independent review",))
    operation = _operation(project, status="COMPLETED")
    ceo = Employee.query.filter_by(slug="ceo").one()
    management = Work(
        project_id=project.id, operation_id=operation.id, title="Project outcome control",
        purpose="Run independent Project outcome review", expected_output="Project review",
        acceptance_criteria="Project evidence is reviewed against the Founder Contract",
        state="EXECUTING", work_type="MANAGEMENT", priority="HIGH",
        created_by_employee_id=ceo.id,
        resource_ceiling_twd=Decimal("100"), retry_limit=1,
    )
    db.session.add(management); db.session.commit()

    # Mission completion is historical process truth. It must not revoke the
    # active Project/Work authority needed for the final independent review.
    operation_kernel._assert_vnext_dispatch_state(operation, management)


def test_vnext_project_management_can_plan_continuation_after_failed_mission(ctx):
    project = _project(("Founder outcome still needs evidence",))
    operation = _operation(project, status="FAILED")
    ceo = Employee.query.filter_by(slug="ceo").one()
    management = Work(
        project_id=project.id, operation_id=operation.id, title="Project continuation control",
        purpose="Plan bounded continuation", expected_output="Continuation plan",
        acceptance_criteria="Continuation stays inside Founder Project authority",
        state="EXECUTING", work_type="MANAGEMENT", priority="HIGH",
        created_by_employee_id=ceo.id,
        resource_ceiling_twd=Decimal("100"), retry_limit=1,
    )
    db.session.add(management); db.session.commit()

    operation_kernel._assert_vnext_dispatch_state(operation, management)


def test_terminal_mission_never_reopens_delivery_provider_execution(ctx):
    project = _project(("Founder outcome",))
    operation = _operation(project, status="COMPLETED")
    researcher = Employee.query.filter_by(slug="researcher").one()
    delivery = Work(
        project_id=project.id, operation_id=operation.id, title="Stale delivery",
        purpose="Must not replay after Mission completion", expected_output="Artifact",
        acceptance_criteria="Delivery", state="EXECUTING", work_type="DELIVERY", priority="HIGH",
        created_by_employee_id=researcher.id,
        resource_ceiling_twd=Decimal("50"), retry_limit=1,
    )
    db.session.add(delivery); db.session.commit()

    with pytest.raises(ValueError, match="Operation lifecycle"):
        operation_kernel._assert_vnext_dispatch_state(operation, delivery)

def test_founder_project_contract_is_immutable_and_mission_criteria_cannot_replace_it(ctx):
    project = _project(("Founder criterion",))
    _operation(project, completion=("Different Mission criterion",))
    before = project_contract.get(project)
    after = project_contract.freeze(project, success_criteria=["Different Mission criterion"])
    assert before["contract_hash"] == after["contract_hash"]
    assert after["success_criteria"] == ["Founder criterion"]


def test_abandoned_work_fails_mission_not_project(ctx):
    project = _project()
    operation = _operation(project)
    employee = Employee.query.filter_by(slug="researcher").one()
    work = Work(
        project_id=project.id, operation_id=operation.id, title="Failed bounded attempt",
        purpose="try", state="ABANDONED", work_type="DELIVERY", priority="HIGH",
        created_by_employee_id=employee.id, retry_limit=1, abandoned_at=now(),
    )
    db.session.add(work); db.session.commit()
    result = company_kernel._mark_mission_failed(operation)
    db.session.refresh(project); db.session.refresh(operation)
    assert result["status"] == "MISSION_FAILED"
    assert operation.status == "FAILED"
    assert project.status == "ACTIVE"


def test_result_ready_does_not_rewrite_failed_mission_history(ctx):
    criterion = "Founder outcome is proven despite an earlier failed Mission"
    project = _project((criterion,))
    failed_operation = _operation(project, status="FAILED", completion=("Earlier attempt",))
    successful_operation = _operation(project, completion=("Final evidence",))
    _accepted_work(project, successful_operation, criterion)
    evaluation = project_outcome.evaluate(project)
    assert evaluation["overall_status"] == "SATISFIED"

    project_outcome.close_result_ready(project, evaluation)
    db.session.refresh(failed_operation); db.session.refresh(project)

    assert project.status == "REVIEW"
    assert failed_operation.status == "FAILED"
    assert failed_operation.founder_report_json["verification"] == "PROJECT_CONTRACT_SATISFIED"


def test_operation_completion_is_not_project_completion(ctx):
    project = _project(("Founder outcome must be proven",))
    operation = _operation(project, completion=("Mission is done",))
    _accepted_work(project, operation, "Mission is done")
    evaluation = project_outcome.evaluate(project)
    assert evaluation["overall_status"] == "INSUFFICIENT_EVIDENCE"
    assert project.status == "ACTIVE"


def test_only_project_contract_satisfaction_creates_result_ready_artifact(ctx):
    criterion = "Health endpoint returns ok"
    project = _project((criterion,))
    operation = _operation(project, completion=("Some Mission envelope criterion",))
    _accepted_work(project, operation, criterion)
    evaluation = project_outcome.evaluate(project)
    assert evaluation["overall_status"] == "SATISFIED"
    result = project_outcome.close_result_ready(project, evaluation)
    db.session.refresh(project)
    assert project.status == "REVIEW"
    assert result["artifact_id"] is not None
    version = db.session.get(ArtifactVersion, result["artifact_version_id"])
    assert version.status == "ACCEPTED"
    proof = VerificationRecord.query.filter_by(
        artifact_version_id=version.id, method="PROJECT_CONTRACT_OUTCOME", status="PASSED"
    ).one()
    assert proof.details_json["project_contract_hash"] == evaluation["contract_hash"]
    durable = project_outcome.result_ready_proof(project)
    assert durable and durable["version"].id == version.id
    completed = project_outcome.accept_founder_completion(project)
    db.session.refresh(project)
    assert completed["status"] == "COMPLETED"
    assert project.status == "COMPLETED"


def test_result_ready_recomputes_current_truth_instead_of_trusting_caller(ctx):
    project = _project(("Founder outcome requires Project-level proof",))
    operation = _operation(project, completion=("Mission evidence",))
    _accepted_work(project, operation, "Mission evidence")
    fake = {
        "project_id": project.id,
        "contract_hash": project_contract.governing_terms(project)["governing_contract_hash"],
        "authority_hash": project_contract.assert_authority_ledger(project)["authority_hash"],
        "overall_status": "SATISFIED",
        "criteria": [{"criterion_id":"P1","criterion":"Founder outcome requires Project-level proof","status":"SATISFIED","method":"FAKE","work_ids":[]}],
        "accepted_work_ids": [],
    }
    with pytest.raises(ValueError, match="PROJECT_CONTRACT_NOT_SATISFIED"):
        project_outcome.close_result_ready(project, fake)
    db.session.refresh(project)
    assert project.status == "ACTIVE"


def test_result_ready_proof_invalidates_when_accepted_evidence_changes(ctx):
    criterion = "Health endpoint returns ok"
    project = _project((criterion,))
    operation = _operation(project, completion=("HTTP delivery",))
    work = _accepted_work(project, operation, criterion)
    version = (
        ArtifactVersion.query.join(Artifact)
        .filter(Artifact.work_id == work.id, ArtifactVersion.status == "ACCEPTED")
        .one()
    )
    contract = project_contract.governing_terms(project)
    db.session.add(VerificationRecord(
        work_id=work.id, artifact_version_id=version.id, method="PROJECT_HOST_HTTP_REVERIFY",
        status="PASSED", details_json={
            "project_contract_hash": contract["governing_contract_hash"],
            "criterion_id": "P1", "criterion": criterion,
            "proof_owner": "HOST_HTTP_CONTRACT", "transport": "REAL_LOOPBACK_HTTP",
            "observation": {"status": 200}, "artifact_content_hash": version.content_hash,
            "artifact_version_id": version.id, "work_id": work.id,
        },
    ))
    db.session.commit()
    evaluation = project_outcome.evaluate(project)
    assert evaluation["overall_status"] == "SATISFIED"
    result = project_outcome.close_result_ready(project, evaluation)
    assert project_outcome.result_ready_proof(project)["version"].id == result["artifact_version_id"]

    # Simulate later evidence reconciliation changing the accepted Artifact basis.
    # The old Project Result must stop being current immediately.
    version.content_hash = "b" * 64
    db.session.commit()
    assert project_outcome.result_ready_proof(project) is None



def test_stale_result_ready_reopens_company_owned_outcome_reconciliation(ctx, monkeypatch):
    criterion = "Health endpoint returns ok"
    project = _project((criterion,))
    operation = _operation(project, completion=("HTTP delivery",), status="COMPLETED")
    work = _accepted_work(project, operation, criterion)
    version = (
        ArtifactVersion.query.join(Artifact)
        .filter(Artifact.work_id == work.id, ArtifactVersion.status == "ACCEPTED")
        .one()
    )
    contract = project_contract.governing_terms(project)
    db.session.add(VerificationRecord(
        work_id=work.id, artifact_version_id=version.id, method="PROJECT_HOST_HTTP_REVERIFY",
        status="PASSED", details_json={
            "project_contract_hash": contract["governing_contract_hash"],
            "criterion_id": "P1", "criterion": criterion,
            "proof_owner": "HOST_HTTP_CONTRACT", "transport": "REAL_LOOPBACK_HTTP",
            "observation": {"status": 200}, "artifact_content_hash": version.content_hash,
            "artifact_version_id": version.id, "work_id": work.id,
        },
    ))
    db.session.commit()
    project_outcome.close_result_ready(project, project_outcome.evaluate(project))
    db.session.refresh(project)
    assert project.status == "REVIEW"

    # Accepted evidence changes after Result Ready. Founder is not asked to fix
    # internal proof freshness; Company must withdraw the stale projection and
    # resume its normal outcome path automatically.
    version.content_hash = "c" * 64
    db.session.commit()
    monkeypatch.setattr(
        company_kernel, "_run_project_outcome_review",
        lambda *_args, **_kwargs: {"status": "REVIEW_RETRY_REQUIRED"},
    )
    result = company_kernel._advance_project(project)
    db.session.refresh(project)
    assert project.status == "ACTIVE"
    assert result["status"] == "REVIEW_RETRY_REQUIRED"
    management = company_kernel._management_work(operation)
    assert not any(
        row.get("state") == "OPEN" and row.get("condition_type") == "RECONCILIATION"
        for row in ((management.runtime_control_json or {}).get("gates") or [])
    )

def test_company_truth_treats_historical_abandonment_as_recovery_not_project_failure(ctx):
    project = _project()
    operation = _operation(project, status="FAILED")
    employee = Employee.query.filter_by(slug="researcher").one()
    db.session.add(Work(
        project_id=project.id, operation_id=operation.id, title="Abandoned Mission work",
        purpose="try", state="ABANDONED", work_type="DELIVERY", priority="HIGH",
        created_by_employee_id=employee.id, retry_limit=1, abandoned_at=now(),
    ))
    db.session.commit()
    snapshot = company_truth.project_snapshot(project)
    assert snapshot["state"] == "RECOVERING"
    assert project.status == "ACTIVE"


def test_delegated_ceo_mission_does_not_create_fake_founder_approval(ctx):
    project = _project(("Ship one verified result",))
    ceo = Employee.query.filter_by(slug="ceo").one()
    researcher = Employee.query.filter_by(slug="researcher").one()
    plan = {
        "mode":"OPERATION_PLAN","executive_response":"continue",
        "operation":{
            "title":"Delegated continuation","objective":"Address unsatisfied Founder evidence.",
            "project_id":project.id,"budget_twd":100,
            "tasks":[{"title":"Produce proof","objective":"Produce proof","assignee_employee_id":researcher.id,
                      "reviewer_employee_id":None,"acceptance_criteria":["Ship one verified result"]}],
            "meeting_policy":"NEVER","meeting_config":{"trigger":"NEVER","participant_employee_ids":[],
                "max_rounds":1,"max_speakers_per_round":1,"contribution_output_cap":192,"token_limit":6000,
                "budget_twd":0,"retry_limit":0},
            "completion_criteria":["Bounded Mission produced useful evidence"],
        },
    }
    operation = operations.propose_operation(
        ceo, plan, route_type="FULL_PROJECT", authority_source="PROJECT_DELEGATED_CEO"
    )
    attention = (operation.memory_json or {}).get("founder_attention_events") or []
    assert (operation.memory_json or {}).get("authority_source") == "PROJECT_DELEGATED_CEO"
    assert all(row.get("status") != "PENDING" for row in attention)
    assert all(row.get("kind") != "OPERATION_APPROVAL" for row in attention)
    before_cap = project.real_budget_limit
    operations.approve(operation, authority_source="PROJECT_DELEGATED_CEO")
    db.session.refresh(operation); db.session.refresh(project)
    assert operation.approved_at is not None
    assert project.real_budget_limit == before_cap
    assert Escalation.query.filter_by(operation_id=operation.id, state="OPEN").count() == 0


def test_default_new_project_authority_reserves_one_bounded_continuation(ctx):
    ceo = Employee.query.filter_by(slug="ceo").one()
    researcher = Employee.query.filter_by(slug="researcher").one()
    plan = {
        "mode":"OPERATION_PLAN","executive_response":"start",
        "operation":{
            "title":"Initial bounded Mission","objective":"Produce first evidence.",
            "project_id":None,"budget_twd":100,
            "tasks":[{"title":"Produce first proof","objective":"Produce first proof",
                      "assignee_employee_id":researcher.id,"reviewer_employee_id":None,
                      "acceptance_criteria":["First verified proof exists"]}],
            "meeting_policy":"NEVER","meeting_config":{"trigger":"NEVER","participant_employee_ids":[],
                "max_rounds":1,"max_speakers_per_round":1,"contribution_output_cap":192,"token_limit":6000,
                "budget_twd":0,"retry_limit":0},
            "completion_criteria":["First Mission evidence exists"],
        },
    }
    operation = operations.propose_operation(ceo, plan, founder_request="Build the outcome and manage it autonomously.")
    memory = dict(operation.memory_json or {})
    memory["new_project_spec"] = {
        "name":"Autonomous Project","objective":"Deliver the full Founder outcome.","priority":"HIGH",
        "success_criteria":["Full Founder outcome is verified"],
        "constraints":["Do not contact external parties"],"deadline":None,
    }
    operation.memory_json = memory
    snapshot = operations.reconcile_pending_proposal_authority(operation)
    db.session.refresh(operation)

    first_move = Decimal(str(snapshot["first_move_envelope_twd"]))
    project_envelope = Decimal(str(snapshot["project_envelope_twd"]))
    assert project_envelope > first_move
    assert Decimal(str(operation.memory_json["project_authorized_budget_twd"])) == project_envelope
    assert snapshot["project_envelope_source"] == "SYSTEM_PRICED_BOUNDED_PROJECT_ENVELOPE"

    operations.approve(operation)
    db.session.refresh(operation)
    project = db.session.get(Project, operation.project_id)
    contract = project_contract.get(project)
    assert Decimal(project.real_budget_limit) == project_envelope
    assert Decimal(contract["budget_limit_twd"]) == project_envelope
    assert Decimal(operation.approved_budget_twd) == first_move


def test_founder_cannot_complete_review_project_from_generic_mission_result(ctx):
    criterion = "Verified Founder outcome"
    project = _project((criterion,))
    operation = _operation(project, completion=(criterion,))
    _accepted_work(project, operation, criterion)
    project.status = "REVIEW"
    db.session.commit()
    # A generic accepted Work/Artifact is not the Project Result commit point.
    # Project Outcome must first persist PROJECT_RESULT + contract-bound proof.
    try:
        project_outcome.accept_founder_completion(project)
        assert False, "generic Mission evidence must not authorize Project completion"
    except ValueError as exc:
        assert str(exc) == "PROJECT_RESULT_PROOF_MISSING"
    db.session.refresh(project)
    assert project.status == "REVIEW"



def test_founder_budget_extension_is_append_only_contract_amendment(ctx):
    project = _project(("Verified Founder outcome",))
    base = project_contract.get(project)
    before_authority = project_contract.effective_authority(project)

    amendment = project_contract.authorize_budget_extension(
        project,
        additional_twd=Decimal("250"),
        reason="Founder approved one additional recovery envelope.",
        source_ref=f"project:{project.id}:test_budget_amendment",
    )
    db.session.commit()
    db.session.refresh(project)

    after_base = project_contract.get(project)
    after_authority = project_contract.effective_authority(project)
    rows = KnowledgeItem.query.filter_by(
        project_id=project.id,
        title=project_contract.AMENDMENT_TITLE,
        founder_approved=True,
    ).all()

    assert after_base["contract_hash"] == base["contract_hash"]
    assert after_base["budget_limit_twd"] == base["budget_limit_twd"]
    assert len(rows) == 1
    assert Decimal(str(after_authority["effective_budget_limit_twd"])) == Decimal("1250")
    assert Decimal(project.real_budget_limit) == Decimal("1250")
    assert amendment["base_contract_hash"] == base["contract_hash"]
    assert after_authority["authority_hash"] != before_authority["authority_hash"]
    assert project_contract.assert_authority_ledger(project)["authority_hash"] == after_authority["authority_hash"]


def test_direct_project_budget_tamper_breaks_internal_authority(ctx):
    project = _project(("Verified Founder outcome",))
    project.real_budget_limit = Decimal("9999")
    db.session.flush()
    try:
        project_contract.assert_internal_authority(project, requested_budget_twd=Decimal("10"))
        assert False, "direct Project budget mutation must not become delegated authority"
    except ValueError as exc:
        assert str(exc) == "PROJECT_BUDGET_LEDGER_MISMATCH"


def test_result_ready_project_cannot_receive_budget_amendment(ctx):
    criterion = "Verified Founder outcome"
    project = _project((criterion,))
    operation = _operation(project, completion=("Mission evidence",))
    _accepted_work(project, operation, criterion)
    evaluation = project_outcome.evaluate(project)
    project_outcome.close_result_ready(project, evaluation)
    db.session.refresh(project)
    assert project.status == "REVIEW"

    try:
        project_contract.authorize_budget_extension(
            project,
            additional_twd=Decimal("1"),
            reason="Should be rejected after Result Ready.",
        )
        assert False, "Result Ready authority must be frozen"
    except ValueError as exc:
        assert str(exc) == "PROJECT_CONTRACT_NOT_AMENDABLE"

def test_host_http_acceptance_uses_defined_shared_method_path_matcher():
    from eason_one.services import host_validation

    task = type("TaskLike", (), {
        "project": None,
        "operation": None,
        "title": "Version Info API",
        "objective": "Expose GET /api/version-info",
        "acceptance_criteria": "GET /api/version-info returns HTTP 200",
    })()
    contract = host_validation._extract_http_contract(task)
    criterion_match = host_validation._HTTP_METHOD_PATH_RE.search(task.acceptance_criteria)

    assert contract["method"] == "GET"
    assert contract["path"] == "/api/version-info"
    assert criterion_match is not None
    assert criterion_match.group(1).upper() == contract["method"]
    assert criterion_match.group(2) == contract["path"]



def test_r5_live_http_verifier_uses_real_loopback_transport(ctx):
    # This is intentionally a Windows host Product Gate. Linux CI/sandbox may
    # exercise the parser and deterministic evidence logic, but must not fake a
    # Windows service/loopback success.
    if os.name != "nt":
        pytest.skip("Windows host-only real loopback Product Gate")
    from pathlib import Path
    from types import SimpleNamespace
    from eason_one.services import host_validation

    probe = SimpleNamespace(
        project=None,
        operation=None,
        title="GET /api/healthz must return HTTP 200",
        objective="GET /api/healthz must return HTTP 200",
        acceptance_criteria="GET /api/healthz must return HTTP 200",
    )
    result = host_validation._run_http_contract(probe, Path.cwd())
    assert result and result["success"] is True
    assert result["observation"]["transport"] == "REAL_LOOPBACK_HTTP"
    assert result["observation"]["status"] == 200
    assert result["observation"]["url"].startswith("http://127.0.0.1:")


def test_r5_project_http_reverification_can_satisfy_project_without_new_agent_run(ctx, monkeypatch):
    from pathlib import Path
    from eason_one.models import AgentRun
    from eason_one.services import host_validation

    criterion = "GET /api/healthz must return HTTP 200"
    project = _project((criterion,))
    operation = _operation(project, completion=("Mission envelope only",))
    work = _accepted_work(project, operation, "Implementation exists")
    version = ArtifactVersion.query.join(Artifact).filter(Artifact.work_id == work.id).one()
    version.content_location = str(Path.cwd())
    db.session.commit()

    before_runs = AgentRun.query.filter_by(project_id=project.id).count()
    monkeypatch.setattr(host_validation, "_run_http_contract", lambda task, repo: {
        "success": True,
        "failure_reason": None,
        "contract": {"method": "GET", "path": "/api/healthz", "expected_json": {}},
        "observation": {
            "status": 200,
            "json": {"ok": True, "service": "eason-one"},
            "url": "http://127.0.0.1:54321/api/healthz",
            "transport": "REAL_LOOPBACK_HTTP",
        },
        "test": {"command": "live loopback", "status": "PASSED", "detail": "HTTP 200"},
    })

    refresh = project_outcome.refresh_deterministic_http_evidence(project)
    evaluation = project_outcome.evaluate(project)
    after_runs = AgentRun.query.filter_by(project_id=project.id).count()

    assert refresh["new_proofs"] == 1
    assert evaluation["overall_status"] == "SATISFIED"
    assert evaluation["criteria"][0]["method"] == "PROJECT_HOST_HTTP_REVERIFY"
    assert after_runs == before_runs


def test_r5_repeated_same_failure_signature_is_detectable_without_operation_id(ctx):
    project = _project(("GET /api/healthz must return HTTP 200",))
    evaluation = {
        "accepted_work_ids": [11],
        "criteria": [{
            "criterion": "GET /api/healthz must return HTTP 200",
            "status": "UNPROVEN",
        }],
    }
    signature = company_kernel._normalized_failure_signature(
        project, evaluation, "HTTP verification environment failed"
    )
    for index in range(2):
        operation = _operation(project, status="FAILED")
        memory = dict(operation.memory_json or {})
        memory.update({
            "authority_source": "PROJECT_DELEGATED_CEO",
            "continuation_failure_signature": signature,
            "attempt": index + 1,
        })
        operation.memory_json = memory
    db.session.commit()
    assert company_kernel._repeated_failure_count(project, signature) == 2


def test_r6_new_exact_founder_gate_supersedes_only_older_founder_gates(ctx):
    from eason_one.services import governance

    project = _project(("Founder outcome",))
    operation = _operation(project)
    # Historical generic authority-looking rows are not Founder authority in
    # v0.20; preserve them as audit/recovery data instead of laundering them
    # into a Founder decision.
    generic = Escalation(
        project_id=project.id, operation_id=operation.id,
        escalation_type="PROJECT_AUTHORITY", state="OPEN",
        reason="Historical generic authority label.",
    )
    old_founder = Escalation(
        project_id=project.id, operation_id=operation.id,
        escalation_type="BUDGET_AUTHORIZATION", state="OPEN",
        reason="Old exact Project budget authority shortfall.",
        options_json=[
            {"action": "APPROVE", "additional_budget_twd": "2", "scope": "PROJECT"},
            {"action": "REJECT"},
        ],
    )
    db.session.add_all([generic, old_founder])
    db.session.commit()

    gate = governance.request_budget_gate(
        project=project, additional_twd=Decimal("5"),
        reason="Current exact Project budget authority shortfall.",
        operation=operation,
    )
    db.session.commit()
    db.session.refresh(generic); db.session.refresh(old_founder); db.session.refresh(gate)

    assert gate.state == "OPEN"
    assert gate.reason == "Current exact Project budget authority shortfall."
    assert old_founder.state == "OPEN"
    assert old_founder.resolution is None
    assert generic.state == "OPEN"
    assert governance.current_gate(project).id == gate.id
    assert [row.id for row in governance.attention(project)] == [gate.id]


def test_r6_failed_mission_cannot_skip_review_of_existing_project_evidence(ctx, monkeypatch):
    project = _project(("Semantic Founder criterion",))
    operation = _operation(project, status="FAILED")
    employee = Employee.query.filter_by(slug="researcher").one()
    failed_work = Work(
        project_id=project.id, operation_id=operation.id, title="Failed recovery",
        purpose="try", acceptance_criteria="Runtime verification",
        state="ABANDONED", work_type="DELIVERY", priority="HIGH",
        created_by_employee_id=employee.id, retry_limit=1, abandoned_at=now(),
    )
    db.session.add(failed_work)
    db.session.commit()

    evaluation = {
        "project_id": project.id,
        "overall_status": "INSUFFICIENT_EVIDENCE",
        "criteria": [{
            "criterion": "Semantic Founder criterion",
            "status": "UNPROVEN",
            "method": "MISSING_PROJECT_PROOF",
            "work_ids": [],
        }],
        "accepted_work_ids": [999],
    }
    calls = []

    monkeypatch.setattr(project_outcome, "evaluate", lambda row: evaluation)
    monkeypatch.setattr(project_outcome, "refresh_deterministic_http_evidence", lambda row: {"new_proofs": 0})
    monkeypatch.setattr(project_outcome, "needs_semantic_review", lambda row: True)
    monkeypatch.setattr(project_outcome, "mark_continuation_needed", lambda *a, **k: None)
    monkeypatch.setattr(company_kernel, "_run_project_outcome_review", lambda *a, **k: calls.append("review") or {"status": "PROJECT_REVIEWED"})
    monkeypatch.setattr(company_kernel, "_plan_continuation", lambda *a, **k: calls.append("plan") or {"status": "PLANNED"})

    result = company_kernel._advance_project(project)

    assert result["status"] == "PLANNED"
    assert calls == ["review", "plan"]


def test_r6_founder_surface_ignores_generic_authority_and_shows_canonical_gate(ctx):
    from eason_one.services import governance, project_company

    project = _project(("Founder outcome",))
    operation = _operation(project)
    for index in range(3):
        db.session.add(Escalation(
            project_id=project.id, operation_id=operation.id,
            escalation_type="PROJECT_AUTHORITY", state="OPEN",
            reason=f"Historical generic authority {index}",
        ))
    db.session.commit()
    gate = governance.request_budget_gate(
        project=project, additional_twd=Decimal("3"),
        reason="Exact current Founder budget authority.",
        operation=operation,
    )
    db.session.commit()

    attention = project_company._project_attention_map()[project.id]
    escalation_items = [row for row in attention if row.get("escalation_id") is not None]
    assert len(escalation_items) == 1
    assert escalation_items[0]["escalation_id"] == gate.id
    assert escalation_items[0]["reason"] == "Exact current Founder budget authority."


def test_r6_independent_review_context_can_consume_exact_artifact_host_proof(ctx):
    from eason_one.services import work_execution

    project = _project(("GET /api/founder-ping returns the required JSON",))
    operation = _operation(project)
    employee = Employee.query.filter_by(slug="engineer").one()
    work = Work(
        project_id=project.id, operation_id=operation.id, title="Founder ping delivery",
        purpose="Implement endpoint", acceptance_criteria="Semantic response contract",
        state="VERIFYING", work_type="DELIVERY", priority="HIGH",
        created_by_employee_id=employee.id, retry_limit=1,
    )
    db.session.add(work); db.session.flush()
    artifact = Artifact(project_id=project.id, work_id=work.id, artifact_type="CODE_CHANGE", title="Founder ping")
    db.session.add(artifact); db.session.flush()
    version = ArtifactVersion(
        artifact_id=artifact.id, version=1, producer_employee_id=employee.id,
        status="SUBMITTED", content_text="WSL could not run HTTP verification.",
        content_hash="c" * 64,
    )
    db.session.add(version); db.session.flush()
    contract = {"contract_hash": "contract-1", "criteria_hash": "criteria-1", "criteria": []}
    host_detail = '{"status":200,"json":{"service":"eason-one","status":"ok","version":"0.20.0"},"transport":"REAL_LOOPBACK_HTTP"}'
    db.session.add(VerificationRecord(
        work_id=work.id, artifact_version_id=version.id,
        method="HOST_ENGINEERING_VALIDATION", status="PASSED",
        details_json={
            "acceptance_contract_hash": "contract-1",
            "artifact_content_hash": version.content_hash,
            "criterion_results": {
                "C1": {"status": "PASSED", "source": "HOST_HTTP_CONTRACT", "evidence": host_detail}
            },
            "host_observations": {"checks": [{"kind": "HTTP_CONTRACT", "status": "PASSED"}]},
        },
    ))
    db.session.commit()

    evidence = work_execution._authoritative_host_review_evidence(work, version, contract)

    assert evidence["protocol"] == "WORK_REVIEW_V4_ARTIFACT_SOURCE_LINEAGE"
    assert evidence["artifact_content_hash"] == version.content_hash
    assert len(evidence["host_verifications"]) == 1
    result = evidence["host_verifications"][0]["criterion_results"]["C1"]
    assert result["status"] == "PASSED"
    assert "REAL_LOOPBACK_HTTP" in result["evidence"]
    assert '"version":"0.20.0"' in result["evidence"]


def test_r6_repaired_host_review_handoff_reopens_system_recovery_once(ctx):
    project = _project(("Founder endpoint is verified",))
    operation = _operation(project, status="FAILED")
    operation.memory_json = {
        **dict(operation.memory_json or {}),
        "authority_source": "PROJECT_DELEGATED_CEO",
        "continuation_failure_signature": "same-failure",
    }
    engineer = Employee.query.filter_by(slug="engineer").one()
    work = Work(
        project_id=project.id, operation_id=operation.id, title="Rejected delivery",
        purpose="Deliver endpoint", acceptance_criteria="Response JSON is correct",
        state="ABANDONED", work_type="DELIVERY", priority="HIGH",
        created_by_employee_id=engineer.id, retry_limit=1, abandoned_at=now(),
    )
    db.session.add(work); db.session.flush()
    artifact = Artifact(project_id=project.id, work_id=work.id, artifact_type="CODE_CHANGE", title="Rejected endpoint")
    db.session.add(artifact); db.session.flush()
    version = ArtifactVersion(
        artifact_id=artifact.id, version=1, producer_employee_id=engineer.id,
        status="REJECTED", content_text="WSL verification unavailable", content_hash="d" * 64,
        rejected_at=now(),
    )
    db.session.add(version); db.session.flush()
    db.session.add_all([
        VerificationRecord(
            work_id=work.id, artifact_version_id=version.id,
            method="HOST_ENGINEERING_VALIDATION", status="PASSED",
            details_json={
                "artifact_content_hash": version.content_hash,
                "criterion_results": {"C1": {"status": "PASSED", "source": "HOST_HTTP_CONTRACT"}},
            },
        ),
        VerificationRecord(
            work_id=work.id, artifact_version_id=version.id,
            method="INDEPENDENT_REVIEW", status="UNPROVEN",
            details_json={"criterion_results": {"C2": {"status": "UNPROVEN", "source": "INDEPENDENT_REVIEW"}}},
        ),
        VerificationRecord(
            work_id=work.id, artifact_version_id=version.id,
            method="INDEPENDENT_REVIEW_OUTCOME", status="FAILED",
            details_json={"decision": "REVISE"},
        ),
    ])
    management = company_kernel._management_work(operation)
    work_runtime = __import__("eason_one.services.work_runtime", fromlist=["open_wait"])
    work_runtime.open_wait(management, "SYSTEM_RECOVERY", "Repeated identical recovery failed twice.")
    project.status = "BLOCKED"
    db.session.commit()

    assert company_kernel._reconcile_repaired_system_recovery(project, operation, management) is True
    db.session.flush()
    assert not work_runtime.has_open_gate(management, "SYSTEM_RECOVERY")
    assert project.status == "ACTIVE"
    override = (management.runtime_control_json or {})["system_recovery_protocol_override"]
    assert override["protocol"] == "HOST_PROOF_REVIEW_HANDOFF_V2"
    assert override["consumed"] is False
    assert company_kernel._consume_system_recovery_protocol_override(management, "same-failure") is True
    assert company_kernel._consume_system_recovery_protocol_override(management, "same-failure") is False
