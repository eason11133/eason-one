from eason_one.extensions import db
from eason_one.models import Employee, Project, WaitCondition, Work, WorkAssignment


def _work(project, employee, title, state, wait_type=None):
    work = Work(project_id=project.id, title=title, purpose=title, state=state)
    db.session.add(work)
    db.session.flush()
    db.session.add(WorkAssignment(work_id=work.id, employee_id=employee.id))
    if wait_type:
        db.session.add(WaitCondition(
            work_id=work.id, condition_type=wait_type, reason=f"{title} reason", state="OPEN"
        ))
    return work


def test_founder_command_view_uses_persisted_work_and_context(client, ctx):
    ceo = Employee.query.filter_by(slug="ceo").one()
    project = Project(name="Truth Command", objective="Show runtime truth", owner_employee_id=ceo.id, environment="LIVE", status="ACTIVE")
    db.session.add(project)
    db.session.flush()
    _work(project, ceo, "Active truth", "EXECUTING")
    _work(project, ceo, "Waiting truth", "WAITING", "RETRY_BACKOFF")
    _work(project, ceo, "Blocked truth", "WAITING", "FOUNDER_DECISION")
    _work(project, ceo, "Failed truth", "ABANDONED")
    _work(project, ceo, "Completed truth", "ACCEPTED")
    db.session.commit()

    response = client.get("/headquarters")
    text = response.get_data(as_text=True)
    assert response.status_code == 200
    for value in ("Active truth", "Waiting truth", "Blocked truth", "Failed truth", "Completed truth", "Truth Command", ceo.name):
        assert value in text


def test_project_status_is_scoped_read_only_and_authoritative(client, ctx):
    ceo = Employee.query.filter_by(slug="ceo").one()
    first = Project(name="Scoped One", objective="One", owner_employee_id=ceo.id, environment="LIVE", status="ACTIVE")
    second = Project(name="Scoped Two", objective="Two", owner_employee_id=ceo.id, environment="LIVE", status="ACTIVE")
    db.session.add_all([first, second])
    db.session.flush()
    owned = _work(first, ceo, "Owned work", "WAITING", "DEPENDENCY")
    _work(second, ceo, "Other work", "ACCEPTED")
    db.session.commit()
    before = (Work.query.count(), WaitCondition.query.count(), WorkAssignment.query.count())

    response = client.get(f"/api/headquarters/projects/{first.id}/status")
    payload = response.get_json()
    assert response.status_code == 200
    assert payload["counts"]["BLOCKED"] == 1
    assert payload["work"] == [{
        "id": owned.id,
        "title": "Owned work",
        "status": "BLOCKED",
        "runtime_state": "WAITING",
        "project": {"id": first.id, "name": "Scoped One"},
        "accountable_employee": {"id": ceo.id, "name": ceo.name},
        "waiting_reason": "Owned work reason",
    }]
    assert (Work.query.count(), WaitCondition.query.count(), WorkAssignment.query.count()) == before
