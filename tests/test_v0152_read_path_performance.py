import pytest

pytest.skip("retired v0.15 WorkItem read-model contract; v0.20 uses Project/Work projections", allow_module_level=True)

from eason_one.extensions import db
from eason_one.models import Employee, Project, Task, WorkItem


def _boom(*args, **kwargs):
    raise AssertionError("Founder GET path invoked mutation/maintenance work")


def test_founder_get_does_not_bootstrap_or_reconcile(client, ctx, monkeypatch):
    import eason_one.services.project_company as project_company
    import eason_one.services.vnext_backbone as backbone

    monkeypatch.setattr(backbone, "bootstrap_vnext", _boom)
    monkeypatch.setattr(project_company, "_reconcile_completed_operation_attention", _boom)
    monkeypatch.setattr(project_company, "_recover_internal_project_budget_gates", _boom)
    monkeypatch.setattr(project_company, "_reconcile_superseded_project_proposals", _boom)

    ceo = Employee.query.filter_by(slug="ceo").one()
    project = Project(
        name="Pure Read Regression",
        objective="Founder pages must render without maintenance side effects.",
        owner_employee_id=ceo.id,
        environment="LIVE",
        status="ACTIVE",
    )
    db.session.add(project)
    db.session.commit()

    for path in (
        "/headquarters",
        "/headquarters/ceo",
        "/headquarters/projects",
        f"/headquarters/projects/{project.id}",
        "/headquarters/history",
    ):
        response = client.get(path)
        assert response.status_code == 200, path


def test_founder_get_does_not_migrate_direct_legacy_task(client, ctx):
    ceo = Employee.query.filter_by(slug="ceo").one()
    project = Project(
        name="No Hidden Backfill",
        objective="GET must not turn reads into migrations.",
        owner_employee_id=ceo.id,
        environment="LIVE",
        status="ACTIVE",
    )
    db.session.add(project)
    db.session.flush()
    task = Task(
        project_id=project.id,
        title="Legacy-only task",
        objective="Remain legacy-only until an explicit runtime/migration sync.",
        created_by_employee_id=ceo.id,
        assigned_employee_id=ceo.id,
        status="WORKING",
    )
    db.session.add(task)
    db.session.commit()

    assert WorkItem.query.filter_by(legacy_task_id=task.id).count() == 0
    response = client.get("/headquarters")
    assert response.status_code == 200
    assert WorkItem.query.filter_by(legacy_task_id=task.id).count() == 0

    # The explicit runtime bridge still works when the write path asks for it.
    __import__(
        "eason_one.services.vnext_backbone", fromlist=["sync_work_item"]
    ).sync_work_item(task)
    db.session.commit()
    assert WorkItem.query.filter_by(legacy_task_id=task.id).count() == 1


def test_history_uses_bounded_recent_result_feed(client, ctx, monkeypatch):
    import eason_one.services.project_company as project_company

    monkeypatch.setattr(project_company, "results_snapshot", _boom)
    response = client.get("/headquarters/history")
    assert response.status_code == 200
