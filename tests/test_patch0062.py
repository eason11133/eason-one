from eason_one.extensions import db
from eason_one.models import Employee, Project, Task
from eason_one.services.ceo import operating_context
from eason_one.services.projects import create_project


def researcher_line(context):
    return context.split("Researcher\n", 1)[1].split("\n\n", 1)[0]


def assigned_work(environment, project_name, status="WORKING"):
    ceo = Employee.query.filter_by(slug="ceo").one()
    researcher = Employee.query.filter_by(slug="researcher").one()
    project = create_project(
        project_name, f"{project_name} objective", ceo, status="ACTIVE",
        environment=environment,
    )
    task = Task(
        project_id=project.id, title=f"{project_name} task", objective="Work",
        status=status, assigned_employee_id=researcher.id,
    )
    db.session.add(task)
    db.session.commit()
    return project, task


def test_live_task_appears_in_employee_workload(ctx):
    assigned_work("LIVE", "Live Product")
    line = researcher_line(operating_context())
    assert "active tasks: 1" in line
    assert "projects: Live Product" in line


def test_smoke_task_is_excluded_without_historical_mutation(ctx):
    project, task = assigned_work("SMOKE", "Legacy Smoke")
    project_id, task_id, original_status = project.id, task.id, task.status
    context = operating_context()
    line = researcher_line(context)
    assert "active tasks: 0" in line and "projects: -" in line
    assert "Legacy Smoke" not in context
    assert "No active Projects." in context
    assert db.session.get(Project, project_id).environment == "SMOKE"
    assert db.session.get(Task, task_id).status == original_status


def test_archived_task_is_excluded_without_record_changes(ctx):
    project, task = assigned_work("ARCHIVED", "Historical Archive", status="BLOCKED")
    context = operating_context()
    assert "active tasks: 0" in researcher_line(context)
    assert "Historical Archive" not in context
    assert db.session.get(Project, project.id).environment == "ARCHIVED"
    assert db.session.get(Task, task.id).status == "BLOCKED"


def test_mixed_workload_and_company_status_include_only_live(ctx):
    live_project, live_task = assigned_work("LIVE", "Current Live")
    smoke_project, smoke_task = assigned_work("SMOKE", "Old Smoke", status="ASSIGNED")
    archived_project, archived_task = assigned_work("ARCHIVED", "Old Archive", status="REVIEW")
    preserved = {
        live_task.id: live_task.status,
        smoke_task.id: smoke_task.status,
        archived_task.id: archived_task.status,
    }

    context = operating_context()
    line = researcher_line(context)
    assert "active tasks: 1" in line
    assert "projects: Current Live" in line
    assert "Old Smoke" not in context and "Old Archive" not in context
    assert f"Project #{live_project.id}: Current Live" in context
    assert f"Project #{smoke_project.id}" not in context
    assert f"Project #{archived_project.id}" not in context
    assert Task.query.count() == 3
    assert {task.id: task.status for task in Task.query.all()} == preserved
