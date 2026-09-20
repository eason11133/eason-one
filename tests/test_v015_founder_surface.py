import pytest

pytest.skip("retired v0.15 WorkItem Founder surface; v0.20 Headquarters tests own the current projection", allow_module_level=True)

from eason_one.extensions import db
from eason_one.models import CompanyEvent, Employee, Project, Task, WorkItem


def test_root_returns_to_real_eason_one_headquarters(client):
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/headquarters")


def test_old_v014_home_is_only_a_compatibility_redirect(client):
    response = client.get("/home", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/headquarters")


def test_quiet_founder_home_has_core_company_surfaces(client):
    response = client.get("/headquarters")
    text = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "What is the company doing right now?" in text
    assert "NEEDS FOUNDER" in text
    assert "CEO · DIRECT LINE" in text
    assert "Mission Control" not in text.split('<nav class="hq-nav v015-nav">', 1)[1].split('</nav>', 1)[0]


def test_ceo_office_is_management_surface_not_dashboard_metrics(client):
    response = client.get("/headquarters/ceo")
    text = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "Manage, delegate, and unblock the company." in text
    assert "CEO FOCUS" in text
    assert "DELEGATED WORK" in text
    assert "Talk to CEO" not in text


def test_project_workspace_uses_work_items_and_company_truth(client, ctx):
    ceo = Employee.query.filter_by(slug="ceo").one()
    project = Project(name="Founder Surface Pilot", objective="Ship a truthful pilot.", owner_employee_id=ceo.id, environment="LIVE", status="ACTIVE")
    db.session.add(project); db.session.flush()
    task = Task(project_id=project.id, title="Build pilot", objective="Implement the first UI slice.", created_by_employee_id=ceo.id, assigned_employee_id=ceo.id, status="WORKING")
    db.session.add(task); db.session.commit()
    # Runtime writes create/sync the WorkItem. Founder GET requests must not be
    # responsible for migration or hidden state mutation.
    __import__(
        "eason_one.services.vnext_backbone", fromlist=["sync_work_item"]
    ).sync_work_item(task)
    db.session.commit()
    assert WorkItem.query.filter_by(project_id=project.id).count() == 1
    response = client.get(f"/headquarters/projects/{project.id}")
    text = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "WORK ITEMS" in text
    assert "ARTIFACTS & EVIDENCE" in text
    assert "COMPANY TRUTH" in text


def test_history_reads_append_only_company_event_stream(client, ctx):
    company = __import__(
        "eason_one.services.company", fromlist=["get_company"]
    ).get_company()
    event = CompanyEvent(
        company_id=company.id,
        event_type="TEST_EVENT",
        actor_type="SYSTEM",
        object_type="TEST",
        idempotency_key="test:v015:history:read",
        payload_json={"title": "Persisted before Founder opened History"},
    )
    db.session.add(event)
    db.session.commit()
    before = CompanyEvent.query.count()
    response = client.get("/headquarters/history")
    assert CompanyEvent.query.count() == before
    text = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "What actually happened." in text
    assert "COMPANY EVENTS" in text
    assert "Persisted before Founder opened History" in text
