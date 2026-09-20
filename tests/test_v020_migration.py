from decimal import Decimal

from eason_one.extensions import db
from eason_one.models import (
    Artifact, ArtifactVersion, CompanyEvent, Employee, KnowledgeItem, Operation,
    Project, VerificationRecord, Work, now,
)
from eason_one.services.project_contract import get as get_project_contract
from scripts.migrate_v020 import migrate_current_app


def _legacy_project(status, *, name):
    ceo = Employee.query.filter_by(slug="ceo").one()
    project = Project(
        name=name, objective="Deliver the real Founder outcome.", status=status,
        priority="HIGH", environment="LIVE", origin="TEST", owner_employee_id=ceo.id,
        real_budget_limit=Decimal("20"), known_constraints="Keep authority bounded.",
    )
    db.session.add(project); db.session.flush()
    db.session.add(KnowledgeItem(
        project_id=project.id, kind="DECISION", title="Project success criteria",
        content="- Founder criterion", rationale="Historical Founder authority",
        source_ref=f"project:{project.id}:founder_contract", origin_employee_id=ceo.id,
        founder_approved=True,
    ))
    operation = Operation(
        title=f"{name} Mission", objective="Bounded attempt", project_id=project.id,
        proposed_by_employee_id=ceo.id, status="FAILED" if status == "FAILED" else "COMPLETED",
        kernel_status="FAILED" if status == "FAILED" else "COMPLETED",
        route_type="FULL_PROJECT", current_stage="LEGACY_TERMINAL",
        plan_json={"mode":"OPERATION_PLAN","executive_response":"legacy","operation":{
            "title":f"{name} Mission","objective":"Bounded attempt","project_id":project.id,
            "budget_twd":5,"tasks":[],"meeting_policy":"NEVER","meeting_config":{
                "trigger":"NEVER","participant_employee_ids":[],"max_rounds":1,
                "max_speakers_per_round":1,"contribution_output_cap":192,"token_limit":6000,
                "budget_twd":0,"retry_limit":0},"completion_criteria":["Mission criterion"]}},
        approved_budget_twd=Decimal("5"), hard_cost_cap_twd=Decimal("5"),
        stage_cost_cap_twd=Decimal("5"), single_call_cost_cap_twd=Decimal("5"),
        max_calls=8, max_revisions=1, max_messages=16, max_elapsed_seconds=3600,
        approved_at=now(), ended_at=now(),
        memory_json={"runtime_semantics":"WORK_CORE_V018","core_cutover_version":"0.18.0"},
    )
    db.session.add(operation); db.session.flush()
    return project, operation


def test_v020_migration_reclaims_project_outcome_authority_from_legacy_closure(ctx):
    researcher = Employee.query.filter_by(slug="researcher").one()
    review_project, review_operation = _legacy_project("REVIEW", name="Legacy review")
    work = Work(
        project_id=review_project.id, operation_id=review_operation.id,
        title="Historical accepted Mission work", purpose="Legacy delivery",
        expected_output="Artifact", acceptance_criteria="Mission criterion",
        state="ACCEPTED", work_type="DELIVERY", priority="HIGH",
        created_by_employee_id=researcher.id, resource_ceiling_twd=Decimal("2"),
        retry_limit=1, accepted_at=now(),
    )
    db.session.add(work); db.session.flush()
    artifact = Artifact(
        project_id=review_project.id, work_id=work.id,
        artifact_type="WORK_RESULT", title="Historical Mission artifact",
    )
    db.session.add(artifact); db.session.flush()
    version = ArtifactVersion(
        artifact_id=artifact.id, version=1, producer_employee_id=researcher.id,
        status="ACCEPTED", content_text="Mission evidence only", content_hash="b" * 64,
        accepted_at=now(),
    )
    db.session.add(version); db.session.flush()
    db.session.add(VerificationRecord(
        work_id=work.id, artifact_version_id=version.id,
        method="HOST_ENGINEERING_VALIDATION", status="PASSED",
        details_json={"historical": True},
    ))

    failed_project, failed_operation = _legacy_project("FAILED", name="Legacy failed")
    db.session.add(CompanyEvent(
        event_type="PROJECT_FAILED", actor_type="RUNTIME", project_id=failed_project.id,
        correlation_id=f"project:{failed_project.id}",
        payload_json={"operation_id": failed_operation.id, "reason":"Mission failure propagated"},
    ))
    db.session.commit()

    summary = migrate_current_app()
    db.session.refresh(review_project); db.session.refresh(failed_project)
    db.session.refresh(review_operation); db.session.refresh(failed_operation)

    assert review_project.status == "ACTIVE"
    assert review_project.id in summary["reopened_review"]
    assert failed_project.status == "ACTIVE"
    assert failed_project.id in summary["reopened_legacy_failure"]
    assert review_operation.memory_json["core_rebuild_version"] == "0.20.0"
    assert failed_operation.memory_json["core_rebuild_version"] == "0.20.0"
    review_contract_hash = get_project_contract(review_project)["contract_hash"]
    failed_contract_hash = get_project_contract(failed_project)["contract_hash"]
    assert get_project_contract(review_project)["success_criteria"] == ["Founder criterion"]
    assert get_project_contract(failed_project)["success_criteria"] == ["Founder criterion"]

    # Re-running the explicit migration is safe: it may re-audit/freeze the
    # already identical contract, but it must not reopen again or mutate the
    # immutable authority ledger.
    second = migrate_current_app()
    db.session.refresh(review_project); db.session.refresh(failed_project)
    assert second["reopened_review"] == []
    assert second["reopened_legacy_failure"] == []
    assert review_project.status == "ACTIVE"
    assert failed_project.status == "ACTIVE"
    assert get_project_contract(review_project)["contract_hash"] == review_contract_hash
    assert get_project_contract(failed_project)["contract_hash"] == failed_contract_hash
