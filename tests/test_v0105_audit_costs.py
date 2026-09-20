from decimal import Decimal

from eason_one.models import AgentRun, Operation
from eason_one.services.headquarters import mission_snapshot, run_snapshot

REQUEST = (
    "Prepare a three-step plan to improve CEO Direct Line reliability. Do not modify "
    "any files. Include the objective, acceptance criteria, risks, and proposed employees."
)


def _create_plan(client):
    preview = client.post('/headquarters/ceo/preview', json={'request': REQUEST, 'mode': 'AUTO'})
    assert preview.status_code == 200
    assert preview.get_json()['route'] == 'ACT'
    response = client.post('/headquarters/ceo/execute', json={'request': REQUEST, 'mode': 'ACT'})
    payload = response.get_json()
    assert response.status_code == 200, payload
    run = AgentRun.query.get(payload['run_id'])
    operation = Operation.query.get(payload['answer']['proposal']['operation_id'])
    return run, operation


def test_planning_run_is_backlinked_and_validation_is_recorded(client, ctx):
    run, operation = _create_plan(client)
    assert run.operation_id == operation.id
    assert run.structured_validation_status == 'PASSED'
    assert run.structured_validation_errors_json == []
    assert run.structured_validation_warnings_json == []


def test_planning_cost_does_not_consume_execution_authority(client, ctx):
    run, operation = _create_plan(client)
    snapshot = mission_snapshot(operation)
    mission = snapshot['mission']
    assert Decimal(mission['planning_cost']) == Decimal(run.real_cost)
    assert Decimal(mission['spent']) == Decimal('0')
    assert Decimal(mission['remaining']) == Decimal(operation.approved_budget_twd)
    assert Decimal(snapshot['company_spent']) >= Decimal(run.real_cost)


def test_run_snapshot_exposes_persisted_latency_and_cost_scope(client, ctx):
    run, operation = _create_plan(client)
    snapshot = run_snapshot(run)
    assert snapshot['operation'].id == operation.id
    assert snapshot['duration_ms'] is not None
    assert snapshot['duration_ms'] >= 0
    assert snapshot['cost_scope'] == 'PLANNING'


def test_independent_mission_does_not_inherit_null_scoped_history(client, ctx):
    run, operation = _create_plan(client)
    assert operation.project_id is None
    snapshot = mission_snapshot(operation)
    assert snapshot['meetings'] == []
    assert snapshot['contributions'] == []


def test_run_and_mission_pages_render_truthful_audit_labels(client, ctx):
    run, operation = _create_plan(client)
    run_html = client.get(f'/headquarters/system/runs/{run.id}').get_data(as_text=True)
    assert f'#{operation.id}' in run_html
    assert 'Run latency' in run_html
    assert 'PLANNING · outside execution envelope' in run_html
    assert 'PASSED' in run_html

    mission_html = client.get(f'/headquarters/missions/{operation.id}').get_data(as_text=True)
    assert 'Planning cost' in mission_html
    assert 'Authorized execution' in mission_html
    assert 'Execution remaining' in mission_html
    assert 'Company spent' in mission_html
