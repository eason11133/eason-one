from copy import deepcopy
from decimal import Decimal
import importlib.util
from pathlib import Path

from eason_one.extensions import db
from eason_one.models import AgentRun, Employee, Operation
from eason_one.services.operation_runtime import runtime_snapshot

REQUEST = (
    "Prepare a three-step plan to improve CEO Direct Line reliability. Do not modify "
    "any files. Include the objective, acceptance criteria, risks, and proposed employees."
)


def _plan(client):
    preview = client.post('/headquarters/ceo/preview', json={'request': REQUEST, 'mode': 'AUTO'})
    assert preview.get_json()['route'] == 'ACT'
    response = client.post('/headquarters/ceo/execute', json={'request': REQUEST, 'mode': 'ACT'})
    assert response.status_code == 200, response.get_json()
    return db.session.get(Operation, response.get_json()['answer']['proposal']['operation_id'])


def _load_migration():
    path = Path(__file__).resolve().parents[1] / 'scripts' / 'migrate_v0108.py'
    spec = importlib.util.spec_from_file_location('migrate_v0108_test_module', path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_budget_gate_has_one_authoritative_runtime_state(client, ctx):
    operation = _plan(client)
    operation.status = 'WAITING_FOR_FOUNDER'
    operation.waiting_reason = 'Authorized execution is below the system estimate.'
    operation.founder_report_json = {'decision_kind': 'BUDGET_AUTHORIZATION'}
    operation.memory_json = {'runtime': {'state': 'WORKING', 'worker_alive': True}}
    db.session.commit()

    first = client.get(f'/operations/{operation.id}/runtime').get_json()
    second = client.get(f'/operations/{operation.id}/runtime').get_json()
    assert first['runtime']['state'] == second['runtime']['state'] == 'WAITING_FOR_BUDGET'
    assert first['runtime']['worker_alive'] is False
    assert first['runtime']['execution_run'] is None
    assert first['runtime']['planning_run']['purpose'] == 'CEO_FOUNDER_REQUEST'

    page = client.get(f'/headquarters/missions/{operation.id}').get_data(as_text=True)
    assert 'Execution is waiting for Founder budget authority.' in page
    assert 'Employee Run</span><b data-runtime-run>None' in page
    assert 'Planning Run #' in page
    assert 'Execution paused at a Founder gate.' not in page


def test_pre_execution_budget_gate_reports_zero_delivery_progress(client, ctx):
    operation = _plan(client)
    operation.status = 'WAITING_FOR_FOUNDER'
    operation.founder_report_json = {'decision_kind': 'BUDGET_AUTHORIZATION'}
    operation.memory_json = {'execution_budget_estimate_twd': '4.2460'}
    db.session.commit()

    page = client.get('/headquarters/missions').get_data(as_text=True)
    assert '<strong>0%</strong>' in page
    assert 'FOUNDER GATE' in page
    assert 'BUDGET AUTHORIZATION' in page
    assert 'Estimate NT$ 4.2460' in page


def test_runner_never_auto_reloads_or_rebuilds_terminal_page(ctx):
    runner = (Path(__file__).resolve().parents[1] / 'eason_one/static/operation-runner.js').read_text(encoding='utf-8')
    assert 'window.location.reload()' not in runner
    assert 'lastSignature' in runner
    assert 'if (signature === lastSignature) return;' in runner
    assert 'if (!alive && status !== "RUNNING") break;' in runner


def test_v0108_migration_supersedes_repeated_open_shell_and_normalizes_runtime(client, ctx):
    operation = _plan(client)
    ceo = Employee.query.filter_by(slug='ceo').one()
    duplicate = Operation(
        title='  ' + operation.title.upper() + '  ',
        objective=operation.objective + ' using the same read-only evidence.',
        proposed_by_employee_id=ceo.id,
        status='WAITING_FOR_FOUNDER',
        plan_json=deepcopy(operation.plan_json),
        approved_budget_twd=Decimal('0.03'),
        founder_report_json={'decision_kind': 'BUDGET_AUTHORIZATION'},
        memory_json={'runtime': {'state': 'WORKING', 'worker_alive': True}},
    )
    db.session.add(duplicate)
    db.session.commit()

    summary = _load_migration().migrate()
    db.session.expire_all()
    rows = Operation.query.order_by(Operation.id).all()
    assert sum(row.status == 'SUPERSEDED' for row in rows) == 1
    assert summary['superseded']
    canonical = next(row for row in rows if row.status != 'SUPERSEDED')
    memory = canonical.memory_json or {}
    runtime = memory.get('runtime') or {}
    assert runtime.get('worker_alive') is False
