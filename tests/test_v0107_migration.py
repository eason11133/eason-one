from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
import importlib.util
from pathlib import Path

from eason_one.extensions import db
from eason_one.models import AgentRun, Employee, Operation, now
from eason_one.services.operations import propose_operation


def _load_migration():
    path = Path(__file__).resolve().parents[1] / 'scripts' / 'migrate_v0107.py'
    spec = importlib.util.spec_from_file_location('migrate_v0107_test_module', path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def _plan(title='CEO Direct Line Reliability Assessment'):
    return {
        'mode': 'OPERATION_PLAN',
        'executive_response': 'A complete governed reliability assessment is proposed.',
        'operation': {
            'title': title,
            'objective': 'Assess reliability using read-only evidence.',
            'project_id': None,
            'budget_twd': 0.03,
            'tasks': [
                {
                    'title': 'Establish reliability baseline',
                    'objective': 'Map evidence and failure modes.',
                    'assignee_employee_id': 5,
                    'reviewer_employee_id': 4,
                    'acceptance_criteria': ['Baseline is evidence linked.'],
                }
            ],
            'meeting_policy': 'NEVER',
            'meeting_config': {
                'trigger': 'NEVER',
                'participant_employee_ids': [],
                'max_rounds': 1,
                'max_speakers_per_round': 1,
                'contribution_output_cap': 192,
                'token_limit': 6000,
                'budget_twd': 0,
                'retry_limit': 0,
            },
            'completion_criteria': ['A reviewed plan exists.'],
        },
    }


def test_migration_collapses_same_title_variants_without_deleting_history(client, ctx):
    ceo = Employee.query.filter_by(slug='ceo').one()
    first = propose_operation(ceo, _plan())
    # Insert the legacy duplicate directly because V0.10.7 propose_operation now
    # prevents this state from being created again.
    variant = deepcopy(first.plan_json)
    variant['operation']['tasks'][0]['objective'] += ' Reworded by a second planning run.'
    duplicate = Operation(
        title=first.title,
        objective=first.objective,
        proposed_by_employee_id=ceo.id,
        status='PLANNED',
        plan_json=variant,
        approved_budget_twd=Decimal('0.03'),
        memory_json={},
    )
    db.session.add(duplicate)
    db.session.commit()

    summary = _load_migration().migrate()
    db.session.expire_all()
    statuses = {row.id: row.status for row in Operation.query.all()}
    assert list(statuses.values()).count('SUPERSEDED') == 1
    assert summary['superseded']
    assert Operation.query.count() == 2


def test_migration_gates_underfunded_running_shell_before_spend(client, ctx):
    ceo = Employee.query.filter_by(slug='ceo').one()
    plan = _plan('Legacy Running Shell')
    operation = Operation(
        title=plan['operation']['title'],
        objective=plan['operation']['objective'],
        proposed_by_employee_id=ceo.id,
        status='RUNNING',
        approved_at=now(),
        plan_json=plan,
        approved_budget_twd=Decimal('0.0001'),
        memory_json={},
    )
    db.session.add(operation)
    db.session.commit()

    before = AgentRun.query.filter(AgentRun.operation_id == operation.id).count()
    summary = _load_migration().migrate()
    db.session.expire_all()
    operation = db.session.get(Operation, operation.id)
    after = AgentRun.query.filter(AgentRun.operation_id == operation.id).count()
    assert before == after == 0
    assert operation.status == 'WAITING_FOR_FOUNDER'
    assert (operation.founder_report_json or {}).get('decision_kind') == 'BUDGET_AUTHORIZATION'
    assert summary['budget_gates']


def test_migration_pauses_stale_paid_running_operation(client, ctx):
    ceo = Employee.query.filter_by(slug='ceo').one()
    plan = _plan('Stale Historical Mission')
    operation = Operation(
        title=plan['operation']['title'],
        objective=plan['operation']['objective'],
        proposed_by_employee_id=ceo.id,
        status='RUNNING',
        approved_at=now() - timedelta(days=1),
        plan_json=plan,
        approved_budget_twd=Decimal('10'),
        memory_json={},
    )
    db.session.add(operation)
    db.session.flush()
    run = AgentRun(
        employee_id=ceo.id,
        operation_id=operation.id,
        purpose='TASK_EXECUTION',
        status='SUCCEEDED',
        model_config_id=ceo.current_model_config_id,
        user_request='x',
        system_prompt_snapshot='x',
        context_snapshot='x',
        raw_output='{}',
        started_at=now() - timedelta(days=1),
        finished_at=now() - timedelta(days=1),
        input_tokens=1,
        output_tokens=1,
        real_cost=Decimal('0'),
        provider_key_snapshot='mock',
        model_name_snapshot='mock',
        input_price_snapshot=Decimal('0'),
        output_price_snapshot=Decimal('0'),
        currency_snapshot='TWD',
    )
    db.session.add(run)
    db.session.commit()

    summary = _load_migration().migrate()
    db.session.expire_all()
    operation = db.session.get(Operation, operation.id)
    assert operation.status == 'PAUSED'
    assert operation.id in summary['paused_stale']


def test_migration_pauses_funded_running_shell_instead_of_showing_fake_active(client, ctx):
    ceo = Employee.query.filter_by(slug='ceo').one()
    plan = _plan('Funded Browser-Only Shell')
    operation = Operation(
        title=plan['operation']['title'],
        objective=plan['operation']['objective'],
        proposed_by_employee_id=ceo.id,
        status='RUNNING',
        approved_at=now(),
        plan_json=plan,
        approved_budget_twd=Decimal('10'),
        memory_json={},
    )
    db.session.add(operation)
    db.session.commit()

    summary = _load_migration().migrate()
    db.session.expire_all()
    operation = db.session.get(Operation, operation.id)
    assert operation.status == 'PAUSED'
    assert operation.id in summary['paused_stale']
    assert AgentRun.query.filter(AgentRun.operation_id == operation.id).count() == 0



def test_migration_never_leaves_invalid_running_plan_active(client, ctx):
    ceo = Employee.query.filter_by(slug='ceo').one()
    plan = _plan('Invalid Historical Meeting Budget')
    plan['operation']['meeting_policy'] = 'BEFORE_FINAL_REPORT'
    plan['operation']['meeting_config'] = {
        'trigger': 'BEFORE_FINAL_REPORT',
        'participant_employee_ids': [4, 5, 6],
        'max_rounds': 2,
        'max_speakers_per_round': 3,
        'contribution_output_cap': 800,
        'token_limit': 6000,
        'budget_twd': 0.01,
        'retry_limit': 1,
    }
    # This deliberately mirrors the real V0.10.6 failure: the stored Operation
    # budget is smaller than its Meeting budget, so estimation raises before a
    # normal budget gate can be calculated.
    plan['operation']['budget_twd'] = 0.005
    operation = Operation(
        title=plan['operation']['title'],
        objective=plan['operation']['objective'],
        proposed_by_employee_id=ceo.id,
        status='RUNNING',
        approved_at=now(),
        plan_json=plan,
        approved_budget_twd=Decimal('0.0050'),
        memory_json={},
    )
    db.session.add(operation)
    db.session.commit()

    summary = _load_migration().migrate()
    db.session.expire_all()
    operation = db.session.get(Operation, operation.id)
    assert operation.status == 'WAITING_FOR_FOUNDER'
    assert (operation.founder_report_json or {}).get('decision_kind') == 'PLAN_REPAIR'
    assert 'Meeting budget must fit inside the Operation budget' in operation.waiting_reason
    assert operation.id in [row[0] for row in summary['plan_reviews']]
    assert AgentRun.query.filter(AgentRun.operation_id == operation.id).count() == 0
    assert Operation.query.filter_by(status='RUNNING').count() == 0


def test_migration_final_invariant_has_zero_running_rows(client, ctx):
    ceo = Employee.query.filter_by(slug='ceo').one()
    plans = [_plan('Restart Shell A'), _plan('Restart Shell B')]
    for index, plan in enumerate(plans, start=1):
        operation = Operation(
            title=plan['operation']['title'],
            objective=plan['operation']['objective'],
            proposed_by_employee_id=ceo.id,
            status='RUNNING',
            approved_at=now(),
            plan_json=plan,
            approved_budget_twd=Decimal('10'),
            memory_json={},
        )
        db.session.add(operation)
    db.session.commit()

    _load_migration().migrate()
    db.session.expire_all()
    assert Operation.query.filter_by(status='RUNNING').count() == 0
    assert all(row.status in {'PAUSED', 'WAITING_FOR_FOUNDER'} for row in Operation.query.all())
