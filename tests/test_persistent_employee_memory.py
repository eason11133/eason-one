from eason_one.extensions import db
from eason_one.models import Artifact, ArtifactVersion, Employee, EmployeeLearningRecord, Project, VerificationRecord, Work
from eason_one.services.employee_memory import capture_accepted_work_experience, relevant_experience


def test_accepted_work_becomes_outcome_backed_employee_experience(ctx):
    employee = Employee.query.filter(Employee.slug != "ceo").first()
    ceo = Employee.query.filter_by(slug="ceo").one()
    project = Project(
        name="Memory project", objective="Research durable handoff behavior", status="ACTIVE",
        priority="HIGH", environment="LIVE", owner_employee_id=ceo.id,
    )
    db.session.add(project); db.session.flush()
    work = Work(
        project_id=project.id, title="Research handoff", purpose="Research durable artifact handoff",
        expected_output="Evidence-backed report", acceptance_criteria="Source-grounded and accepted",
        state="ACCEPTED", work_type="DELIVERY", created_by_employee_id=ceo.id,
    )
    db.session.add(work); db.session.flush()
    artifact = Artifact(project_id=project.id, work_id=work.id, title="Research handoff")
    db.session.add(artifact); db.session.flush()
    version = ArtifactVersion(
        artifact_id=artifact.id, version=1, producer_employee_id=employee.id,
        status="ACCEPTED", content_text="durable handoff evidence", content_hash="a" * 64,
    )
    db.session.add(version); db.session.flush()
    proof = VerificationRecord(
        work_id=work.id, artifact_version_id=version.id, method="ACCEPTANCE_CONTRACT",
        status="PASSED", details_json={"criterion_results": {"c1": {"status": "PASSED"}}},
    )
    db.session.add(proof); db.session.flush()

    rows = capture_accepted_work_experience(work, version, proof)
    db.session.flush()
    assert len(rows) == 1
    row = rows[0]
    assert row.validation_basis == "CANONICAL_WORK_ACCEPTANCE"
    assert row.work_id == work.id
    assert row.artifact_version_id == version.id
    assert row.validated is True

    text, meta = relevant_experience(employee, project, None)
    assert f"Work #{work.id}" in text
    assert row.id in meta["experience_record_ids"]

    capture_accepted_work_experience(work, version, proof)
    db.session.flush()
    assert EmployeeLearningRecord.query.filter_by(
        employee_id=employee.id, work_id=work.id, artifact_version_id=version.id,
        learning_type="WORK_EXPERIENCE",
    ).count() == 1
