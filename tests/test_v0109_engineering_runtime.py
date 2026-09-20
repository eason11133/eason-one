from decimal import Decimal
import json

import pytest

from eason_one.extensions import db
from eason_one.models import AgentRun, CostEvent, Employee, ModelConfig, Operation, Project, Task
from eason_one.services.codex_connector import CODEX_PROVIDER_KEY, ensure_codex_model_config
from eason_one.services.execution import provider_preflight
from eason_one.services.founder_decisions import decide
from eason_one.services.operation_runtime import run_until_gate
from eason_one.services.operations import actual_cost, execution_budget_estimate


REQUEST = (
    "Build a small software reliability fix. Keep the scope bounded, use the local "
    "Codex tool, run tests, and do not deploy or change secrets."
)


def _plan(client):
    preview = client.post('/headquarters/ceo/preview', json={'request': REQUEST, 'mode': 'AUTO'})
    assert preview.status_code == 200, preview.get_json()
    assert preview.get_json()['route'] == 'ACT'
    response = client.post('/headquarters/ceo/execute', json={'request': REQUEST, 'mode': 'ACT'})
    assert response.status_code == 200, response.get_json()
    payload = response.get_json()
    return db.session.get(Operation, payload['answer']['proposal']['operation_id'])


def test_engineering_org_is_ceo_to_engineer_to_codex(ctx):
    from scripts.migrate_v0109 import migrate

    summary = migrate()
    engineer = Employee.query.filter_by(slug='engineer').one()
    director = Employee.query.filter_by(slug='engineering-director').one()
    ceo = Employee.query.filter_by(slug='ceo').one()
    assert summary['engineer_codex_assigned'] is True
    assert engineer.manager_id == ceo.id
    assert engineer.current_model.provider_key == 'codex'
    assert engineer.current_model.model_name == 'codex-cli'
    assert director.active is False
    assert director.employment_status == 'INACTIVE'


def test_formal_mock_is_forbidden_outside_explicit_simulation(app, ctx):
    mock = ModelConfig.query.filter_by(provider_key='mock').one()
    engineer = Employee.query.filter_by(slug='engineer').one()
    project = Project(
        name='Formal Mission', objective='Real work', status='ACTIVE', environment='LIVE',
        owner_employee_id=engineer.id,
    )
    db.session.add(project); db.session.flush()
    operation = Operation(
        title='Formal Mission', objective='Real work', project_id=project.id,
        proposed_by_employee_id=Employee.query.filter_by(slug='ceo').one().id,
        status='RUNNING', plan_json={'mode': 'OPERATION_PLAN', 'executive_response': 'x', 'operation': {
            'title': 'Formal Mission', 'objective': 'Real work', 'project_id': project.id,
            'budget_twd': 1, 'tasks': [], 'meeting_policy': 'NEVER', 'meeting_config': {
                'trigger': 'NEVER', 'participant_employee_ids': [], 'max_rounds': 1,
                'max_speakers_per_round': 1, 'contribution_output_cap': 128,
                'token_limit': 6000, 'budget_twd': 0, 'retry_limit': 0,
            }, 'completion_criteria': ['Done'],
        }}, approved_budget_twd=1, memory_json={},
    )
    db.session.add(operation); db.session.commit()
    app.config['TESTING'] = False
    try:
        with pytest.raises(ValueError, match='MockProvider is forbidden'):
            provider_preflight(mock, operation=operation, purpose='TASK_EXECUTION')
    finally:
        app.config['TESTING'] = True


def test_engineering_plan_uses_only_engineer_and_zero_api_execution_estimate(client, ctx):
    operation = _plan(client)
    plan = operation.plan_json['operation']
    engineer = Employee.query.filter_by(slug='engineer').one()
    director = Employee.query.filter_by(slug='engineering-director').one()
    assert plan['meeting_policy'] == 'NEVER'
    assert plan['meeting_config']['trigger'] == 'NEVER'
    assert plan['meeting_config']['participant_employee_ids'] == []
    assert all(item['assignee_employee_id'] == engineer.id for item in plan['tasks'])
    assert all(item['reviewer_employee_id'] == engineer.id for item in plan['tasks'])
    assert all(item['assignee_employee_id'] != director.id for item in plan['tasks'])
    estimate, breakdown, _ = execution_budget_estimate(operation.plan_json)
    assert estimate == Decimal('0.0001')
    assert {row['kind'] for row in breakdown} == {'ENGINEER_CODEX_TOOL', 'DETERMINISTIC_ENGINEERING_VERIFICATION'}


def test_founder_approved_engineering_mission_runs_codex_and_finishes_without_paid_agent_review(client, ctx):
    operation = _plan(client)
    decide(operation, 'APPROVE')
    state = run_until_gate(operation.id, max_steps=16)
    db.session.expire_all()
    operation = db.session.get(Operation, operation.id)
    assert state['state'] == 'COMPLETED'
    assert operation.status == 'COMPLETED'
    assert operation.founder_report_json['engineering_evidence']
    runs = AgentRun.query.filter_by(operation_id=operation.id).order_by(AgentRun.id).all()
    execution_runs = [run for run in runs if run.purpose != 'CEO_FOUNDER_REQUEST']
    assert execution_runs
    assert all(run.provider_key_snapshot == CODEX_PROVIDER_KEY for run in execution_runs)
    assert all(Decimal(run.real_cost or 0) == 0 for run in execution_runs)
    assert all(run.structured_validation_status == 'PASSED' for run in execution_runs)
    assert not any(run.purpose in {'TASK_REVIEW', 'CEO_OPERATION_REPORT', 'GOAL_VERIFICATION'} for run in execution_runs)
    assert all(task.status == 'DONE' for task in operation.tasks)
    assert actual_cost(operation) == 0


def test_engineer_codex_job_spec_preserves_scope_and_audit(client, ctx):
    operation = _plan(client)
    operation.memory_json = {
        'engineering': {
            'allowed_paths': ['eason_one/services'],
            'forbidden_paths': ['.env', 'instance/eason_one.db'],
            'max_changed_files': 4,
            'codex_retry_limit': 1,
        }
    }
    db.session.commit()
    decide(operation, 'APPROVE')
    run_until_gate(operation.id, max_steps=16)
    run = AgentRun.query.filter_by(operation_id=operation.id, provider_key_snapshot='codex').one()
    assert 'Maximum changed files: 4' in run.context_snapshot
    assert 'eason_one/services' in run.context_snapshot
    assert '.env' in run.context_snapshot
    assert run.context_composition_json['repository']
    assert run.context_composition_json['read_only'] is False
    assert run.parsed_output_json['codex']['tests'][0]['status'] == 'PASSED'
    assert run.parsed_output_json['codex']['acceptance'][0]['status'] == 'PASSED'


def test_migration_invalidates_formal_mock_and_reconciles_paid_failure(ctx):
    from scripts.migrate_v0109 import migrate

    ceo = Employee.query.filter_by(slug='ceo').one()
    engineer = Employee.query.filter_by(slug='engineer').one()
    director = Employee.query.filter_by(slug='engineering-director').one()
    mock = ModelConfig.query.filter_by(provider_key='mock').one()
    project = Project(name='Legacy engineering', objective='Repair', status='ACTIVE', environment='LIVE', owner_employee_id=ceo.id)
    db.session.add(project); db.session.flush()
    plan = {'mode': 'OPERATION_PLAN', 'executive_response': 'legacy', 'operation': {
        'title': 'Legacy engineering', 'objective': 'Repair', 'project_id': project.id, 'budget_twd': 4.246,
        'tasks': [
            {'title': 'Engineer baseline', 'objective': 'Inspect', 'assignee_employee_id': engineer.id,
             'reviewer_employee_id': director.id, 'acceptance_criteria': ['Evidence']},
            {'title': 'Director output', 'objective': 'Finish', 'assignee_employee_id': director.id,
             'reviewer_employee_id': engineer.id, 'acceptance_criteria': ['Complete']},
        ],
        'meeting_policy': 'BEFORE_FINAL_REPORT', 'meeting_config': {
            'trigger': 'BEFORE_FINAL_REPORT', 'participant_employee_ids': [director.id, engineer.id],
            'max_rounds': 2, 'max_speakers_per_round': 2, 'contribution_output_cap': 512,
            'token_limit': 6000, 'budget_twd': 0.01, 'retry_limit': 1,
        }, 'completion_criteria': ['Complete'],
    }}
    operation = Operation(title='Legacy engineering', objective='Repair', project_id=project.id,
        proposed_by_employee_id=ceo.id, status='RUNNING', plan_json=plan,
        approved_budget_twd=Decimal('4.246'), actual_cost_twd=0, memory_json={})
    db.session.add(operation); db.session.flush()
    task1 = Task(project_id=project.id, operation_id=operation.id, title='Engineer baseline', objective='Inspect',
        status='DONE', assigned_employee_id=engineer.id, reviewer_employee_id=director.id,
        acceptance_criteria='Evidence', result_summary='Fake mock result')
    task2 = Task(project_id=project.id, operation_id=operation.id, title='Director output', objective='Finish',
        status='ASSIGNED', assigned_employee_id=director.id, reviewer_employee_id=engineer.id,
        acceptance_criteria='Complete')
    db.session.add_all([task1, task2]); db.session.flush()
    mock_run = AgentRun(employee_id=engineer.id, project_id=project.id, task_id=task1.id,
        operation_id=operation.id, model_config_id=mock.id, purpose='TASK_EXECUTION',
        user_request='Inspect', system_prompt_snapshot='mock', context_snapshot='mock', raw_output='{}',
        parsed_output_json={'result_summary': 'fake', 'knowledge_proposals': []}, status='SUCCEEDED',
        provider_key_snapshot='mock', model_name_snapshot='deterministic-mock',
        input_price_snapshot=0, output_price_snapshot=0, currency_snapshot='TWD', currency='TWD', real_cost=0)
    paid = AgentRun(employee_id=Employee.query.filter_by(slug='critic').one().id, project_id=project.id,
        task_id=task2.id, operation_id=operation.id, model_config_id=mock.id, purpose='TASK_EXECUTION',
        user_request='Critique', system_prompt_snapshot='paid', context_snapshot='paid', raw_output='truncated',
        status='FAILED', failure_reason='OUTPUT_TRUNCATED', provider_key_snapshot='anthropic',
        model_name_snapshot='claude-sonnet-5', input_price_snapshot=1, output_price_snapshot=1,
        currency_snapshot='TWD', currency='TWD', real_cost=Decimal('0.574130'))
    db.session.add_all([mock_run, paid]); db.session.flush()
    company_id = __import__('eason_one.models', fromlist=['Company']).Company.query.one().id
    db.session.add(CostEvent(company_id=company_id, employee_id=paid.employee_id, project_id=project.id,
        task_id=task2.id, agent_run_id=paid.id, operation_id=operation.id, category='PROVIDER',
        description='Paid failed run', real_cost_delta=Decimal('0.574130'), currency='TWD'))
    db.session.commit()

    summary = migrate()
    db.session.expire_all()
    operation = db.session.get(Operation, operation.id)
    task1 = db.session.get(Task, task1.id)
    task2 = db.session.get(Task, task2.id)
    mock_run = db.session.get(AgentRun, mock_run.id)
    assert mock_run.resolution_status == 'INVALID_FORMAL_MOCK'
    assert task1.status == 'ASSIGNED'
    assert task1.result_summary is None
    assert task1.reviewer_employee_id == engineer.id
    assert task2.assigned_employee_id == engineer.id
    assert task2.reviewer_employee_id == engineer.id
    assert operation.status == 'PAUSED'
    assert operation.founder_report_json['decision_kind'] == 'ENGINEERING_RUNTIME_REPAIR'
    assert Decimal(operation.actual_cost_twd) == Decimal('0.574130')
    assert Decimal(operation.founder_report_json['remaining_twd']) == Decimal('3.671870')
    assert paid.id not in summary['mock_runs_invalidated']


def test_models_ui_exposes_gemini_perplexity_codex_and_mock_warning(client, ctx):
    ensure_codex_model_config()
    page = client.get('/headquarters/system/models').get_data(as_text=True)
    assert 'value="gemini"' in page
    assert 'value="perplexity"' in page
    assert 'value="codex"' in page
    assert 'Mock is test-only' in page
    assert 'Codex CLI' in page


def test_mission_runtime_ui_uses_live_cost_fields_and_no_page_reload(client, ctx):
    operation = _plan(client)
    decide(operation, 'APPROVE')
    page = client.get(f'/headquarters/missions/{operation.id}').get_data(as_text=True)
    assert 'data-runtime-cost' in page
    assert 'data-runtime-remaining' in page
    assert 'data-runtime-company-spent' in page
    script = client.get('/static/operation-runner.js').get_data(as_text=True)
    assert 'window.location.reload' not in script
    assert 'location.reload' not in script
    assert 'company_spent' in script
