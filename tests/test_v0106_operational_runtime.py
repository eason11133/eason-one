from decimal import Decimal

from eason_one.extensions import db
from eason_one.models import AgentRun, Operation, Task
from eason_one.services.ceo import founder_request
from eason_one.services.current_company import projection
from eason_one.services.founder_decisions import decide
from eason_one.services.operation_runtime import run_until_gate, runtime_snapshot
from eason_one.services.operations import (
    ensure_full_execution_authority,
    execution_budget_estimate,
    plan_fingerprint,
    propose_operation,
)

REQUEST = (
    "Prepare a three-step plan to improve CEO Direct Line reliability. Do not modify "
    "any files. Include the objective, acceptance criteria, risks, and proposed employees."
)


def _plan(client):
    preview = client.post('/headquarters/ceo/preview', json={'request': REQUEST, 'mode': 'AUTO'})
    assert preview.get_json()['route'] == 'ACT'
    response = client.post('/headquarters/ceo/execute', json={'request': REQUEST, 'mode': 'ACT'})
    assert response.status_code == 200, response.get_json()
    payload = response.get_json()
    return db.session.get(Operation, payload['answer']['proposal']['operation_id'])


def test_native_hq_has_one_direct_line_and_no_dom_rearrangement_bridge(client, ctx):
    page = client.get('/headquarters').get_data(as_text=True)
    assert 'v0106-native-root' in page
    assert page.count('Talk to your CEO') == 1
    assert page.count('hq-ceo-line-button') == 1
    assert 'headquarters-v0106.css' in page
    assert 'headquarters-v0103.js' not in page
    assert 'data-ceo-office-form' in page
    assert 'CEO OFFICE · FOUNDER ON SITE' in page


def test_repeated_founder_plan_reuses_open_operation(client, ctx):
    first = _plan(client)
    second = _plan(client)
    assert first.id == second.id
    assert Operation.query.count() == 1
    assert AgentRun.query.filter_by(purpose='CEO_FOUNDER_REQUEST').count() == 2


def test_planned_operations_are_needs_founder_not_active(client, ctx):
    operation = _plan(client)
    row = next(item for item in projection()['operations'] if item['operation'].id == operation.id)
    assert row['classification'] == 'WAITING_FOR_FOUNDER'
    page = client.get('/headquarters/missions').get_data(as_text=True)
    assert '>0 ACTIVE<' in page
    assert '>1 NEED FOUNDER<' in page


def test_approved_operation_runs_to_delivery_with_persisted_employee_runs(client, ctx):
    operation = _plan(client)
    estimate, _, _ = execution_budget_estimate(operation.plan_json)
    assert Decimal(operation.approved_budget_twd) >= estimate
    decide(operation, 'APPROVE')
    assert operation.status == 'RUNNING'
    assert Task.query.filter_by(operation_id=operation.id).count() >= 1

    state = run_until_gate(operation.id, max_steps=48)
    db.session.expire_all()
    operation = db.session.get(Operation, operation.id)
    assert state['state'] == 'COMPLETED'
    assert operation.status == 'COMPLETED'
    assert all(task.status in {'DONE', 'CANCELLED'} for task in operation.tasks)
    execution_runs = AgentRun.query.filter(
        AgentRun.operation_id == operation.id,
        AgentRun.purpose != 'CEO_FOUNDER_REQUEST',
    ).all()
    assert execution_runs
    assert any(run.task_id for run in execution_runs)
    assert any(run.purpose == 'CEO_OPERATION_REPORT' for run in execution_runs)
    assert operation.founder_report_json


def test_insufficient_legacy_authority_stops_before_employee_run(client, ctx):
    operation = _plan(client)
    operation.approved_budget_twd = Decimal('0.0300')
    operation.plan_json['operation']['budget_twd'] = '0.03'
    db.session.commit()
    # Materialize using a temporarily sufficient approval, then restore the
    # legacy unsafe authority to reproduce Mission #4 without spending.
    safe = Decimal('10')
    operation.approved_budget_twd = safe
    db.session.commit()
    decide(operation, 'APPROVE')
    operation.approved_budget_twd = Decimal('0.0010')
    db.session.commit()

    before = AgentRun.query.filter(
        AgentRun.operation_id == operation.id,
        AgentRun.purpose != 'CEO_FOUNDER_REQUEST',
    ).count()
    assert ensure_full_execution_authority(operation) is False
    after = AgentRun.query.filter(
        AgentRun.operation_id == operation.id,
        AgentRun.purpose != 'CEO_FOUNDER_REQUEST',
    ).count()
    assert after == before == 0
    assert operation.status == 'WAITING_FOR_FOUNDER'
    assert (operation.founder_report_json or {}).get('decision_kind') == 'BUDGET_AUTHORIZATION'


def test_mission_approval_redirect_and_runtime_monitor_are_wired(client, ctx):
    operation = _plan(client)
    response = client.post(
        f'/operations/{operation.id}/founder-decision',
        data={'action': 'APPROVE'},
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert 'autorun=1' in response.headers['Location']
    page = client.get(response.headers['Location']).get_data(as_text=True)
    assert 'data-operation-monitor' in page
    assert 'data-operation-runner' in page
    assert 'operation-runner.js?v=0.10.8' in page



def test_paused_operations_are_not_reported_as_active(client, ctx):
    operation = _plan(client)
    operation.status = 'PAUSED'
    db.session.commit()
    row = next(item for item in projection()['operations'] if item['operation'].id == operation.id)
    assert row['classification'] == 'WAITING_FOR_FOUNDER'

def test_runtime_state_is_honest_when_no_worker_is_alive(client, ctx):
    operation = _plan(client)
    decide(operation, 'APPROVE')
    snapshot = runtime_snapshot(operation)
    assert snapshot['worker_alive'] is False
    assert snapshot['state'] == 'READY'
    live = client.get(f'/operations/{operation.id}/runtime').get_json()
    assert live['runtime']['worker_alive'] is False
    assert live['operation']['workers'] == []


def test_waiting_for_founder_status_is_never_counted_active(client, ctx):
    operation = _plan(client)
    operation.status = 'WAITING_FOR_FOUNDER'
    operation.memory_json = {}
    operation.founder_report_json = {'decision_kind': 'FOUNDER_AUTHORITY'}
    db.session.commit()
    row = next(item for item in projection()['operations'] if item['operation'].id == operation.id)
    assert row['classification'] == 'WAITING_FOR_FOUNDER'


def test_same_title_variant_reuses_open_mission(client, ctx):
    first = _plan(client)
    variant = dict(first.plan_json)
    variant['operation'] = dict(variant['operation'])
    variant['operation']['tasks'] = [dict(item) for item in variant['operation']['tasks']]
    variant['operation']['tasks'][0]['objective'] += ' with a slightly reworded evidence note'
    ceo = __import__('eason_one.models', fromlist=['Employee']).Employee.query.filter_by(slug='ceo').one()
    reused = propose_operation(ceo, variant)
    assert reused.id == first.id
    assert Operation.query.count() == 1


def test_explicit_runtime_start_dispatches_employee_runs(client, ctx):
    import time

    operation = _plan(client)
    decide(operation, 'APPROVE')
    response = client.post(f'/operations/{operation.id}/runtime/start')
    assert response.status_code == 200, response.get_json()
    deadline = time.time() + 8
    latest = None
    while time.time() < deadline:
        latest = client.get(f'/operations/{operation.id}/runtime').get_json()
        if latest['operation']['status'] != 'RUNNING' and not latest['runtime']['worker_alive']:
            break
        time.sleep(0.05)
    db.session.expire_all()
    operation = db.session.get(Operation, operation.id)
    assert operation.status == 'COMPLETED', latest
    assert AgentRun.query.filter(
        AgentRun.operation_id == operation.id,
        AgentRun.purpose != 'CEO_FOUNDER_REQUEST',
    ).count() > 0
