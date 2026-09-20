from decimal import Decimal
from types import SimpleNamespace

import pytest

from eason_one.extensions import db
from eason_one.models import AgentRun, Employee, Meeting, Operation, Project, Task, WorkMessage
from eason_one.services import operations
from eason_one.services.headquarters import operation_live_snapshot


def _plan(ceo, engineer, critic, trigger="BEFORE_FINAL_REPORT"):
    return {
        "mode":"OPERATION_PLAN",
        "executive_response":"I will use Engineering for the first answer, Critic for independent review, and a bounded Meeting before I report back.",
        "operation":{
            "title":"Bounded CEO orchestration test",
            "objective":"Produce and verify one useful answer for the Founder.",
            "project_id":None,
            "budget_twd":2.0,
            "tasks":[{
                "title":"Draft answer",
                "objective":"Produce the first bounded answer.",
                "assignee_employee_id":engineer.id,
                "reviewer_employee_id":critic.id,
                "acceptance_criteria":["Answer is specific and reviewable"],
            }],
            "meeting_policy":"AUTO",
            "meeting_config":{
                "trigger":trigger,
                "participant_employee_ids":[engineer.id,critic.id],
                "max_rounds":2,
                "max_speakers_per_round":3,
                "contribution_output_cap":512,
                "token_limit":5000,
                "budget_twd":0.8,
                "retry_limit":1,
            },
            "completion_criteria":["A reviewed answer and Founder-facing report exist"],
        },
    }


def test_hq_has_one_ceo_composer_and_no_manual_route_buttons(client, ctx):
    page=client.get('/headquarters').get_data(as_text=True)
    assert 'data-ceo-dialogue' in page
    assert 'data-ceo-office-form' in page
    assert page.count('name="request"') == 1
    assert 'value="BRIEF"' not in page
    assert 'value="ADVISE"' not in page
    assert 'value="ACT"' not in page
    assert '>SEND<' in page
    script=client.get('/static/headquarters.js').get_data(as_text=True)
    assert "textarea.value = '';" in script
    assert 'data-approve-operation' in script
    assert 'runOperation' in script


def test_operation_plan_governs_meeting_envelope(ctx):
    ceo=Employee.query.filter_by(slug='ceo').one()
    engineer=Employee.query.filter_by(slug='engineer').first() or Employee.query.filter(Employee.id!=ceo.id).first()
    critic=Employee.query.filter_by(slug='critic').first() or Employee.query.filter(Employee.id.notin_([ceo.id,engineer.id])).first()
    plan=_plan(ceo,engineer,critic)
    validated=operations.validate_plan(plan)
    config=validated['operation']['meeting_config']
    assert config['trigger']=='BEFORE_FINAL_REPORT'
    assert config['token_limit']==11000
    assert config['retry_limit']==1
    bad=_plan(ceo,engineer,critic)
    bad['operation']['meeting_config']['token_limit']=50000
    with pytest.raises(ValueError,match='governed range'):
        operations.validate_plan(bad)


def test_live_snapshot_reports_workers_progress_budget_and_meeting(ctx):
    ceo=Employee.query.filter_by(slug='ceo').one()
    engineer=Employee.query.filter_by(slug='engineer').first() or Employee.query.filter(Employee.id!=ceo.id).first()
    critic=Employee.query.filter_by(slug='critic').first() or Employee.query.filter(Employee.id.notin_([ceo.id,engineer.id])).first()
    operation=operations.propose_operation(ceo,_plan(ceo,engineer,critic))
    operations.approve(operation)
    state=operation_live_snapshot(operation)
    assert state['status']=='RUNNING'
    assert state['total']==1
    assert state['progress']==8
    assert state['budget']=='2.0000'
    assert state['workers'] == []
    assert state['current_task'] is None
    assert any(task.assigned_employee_id == engineer.id for task in operation.tasks)
    meeting=operations._start_planned_final_meeting(operation)
    state=operation_live_snapshot(operation)
    assert state['meeting']['token_limit']==11000
    assert state['meeting']['max_rounds']==2
    assert state['meeting']['status']=='RUNNING'


def test_ceo_execute_returns_team_budget_and_meeting_proposal(client, ctx, monkeypatch):
    ceo=Employee.query.filter_by(slug='ceo').one()
    engineer=Employee.query.filter_by(slug='engineer').first() or Employee.query.filter(Employee.id!=ceo.id).first()
    critic=Employee.query.filter_by(slug='critic').first() or Employee.query.filter(Employee.id.notin_([ceo.id,engineer.id])).first()
    operation=operations.propose_operation(ceo,_plan(ceo,engineer,critic))
    run=SimpleNamespace(id=901,status='SUCCEEDED',purpose='CEO_FOUNDER_REQUEST',
      parsed_output_json={'mode':'OPERATION_PLAN','executive_response':'I recommend a bounded two-person team and one review Meeting.'},
      error_text=None)
    monkeypatch.setattr('eason_one.routes.founder_request',lambda *args,**kwargs:(run,operation))
    response=client.post('/headquarters/ceo/execute',json={
      'request':'Have the team solve this difficult question and report back.','mode':'ACT'})
    payload=response.get_json()
    assert response.status_code==200
    proposal=payload['answer']['proposal']
    assert proposal['budget_twd']=='2.0000'
    assert proposal['tasks'][0]['assignee']==engineer.name
    assert proposal['tasks'][0]['reviewer']==critic.name
    assert proposal['meeting']['token_limit']==11000
    assert proposal['meeting']['trigger']=='BEFORE_FINAL_REPORT'
    assert WorkMessage.query.filter_by(message_type='FOUNDER_TO_CEO').count()==1
    assert WorkMessage.query.filter_by(message_type='CEO_TO_FOUNDER').count()==1


def test_ceo_approval_api_materializes_and_starts_operation(client, ctx):
    ceo=Employee.query.filter_by(slug='ceo').one()
    engineer=Employee.query.filter_by(slug='engineer').first() or Employee.query.filter(Employee.id!=ceo.id).first()
    critic=Employee.query.filter_by(slug='critic').first() or Employee.query.filter(Employee.id.notin_([ceo.id,engineer.id])).first()
    operation=operations.propose_operation(ceo,_plan(ceo,engineer,critic))
    response=client.post(f'/headquarters/ceo/operations/{operation.id}/approve')
    payload=response.get_json()
    assert response.status_code==200
    assert payload['approved'] is True
    assert payload['operation']['status']=='RUNNING'
    assert Task.query.filter_by(operation_id=operation.id).count()==1


def test_problem_request_routes_to_team_execution(ctx):
    from eason_one.services.headquarters import route_ceo_intent
    assert route_ceo_intent(
        "Have the team investigate this difficult problem and report the answer."
    )["route"] == "ACT"
    assert route_ceo_intent("請叫員工調查這個問題並找出答案")["route"] == "ACT"


def test_one_approved_problem_runs_team_meeting_and_ceo_report(ctx):
    ceo=Employee.query.filter_by(slug='ceo').one()
    engineer=Employee.query.filter_by(slug='engineer').one()
    critic=Employee.query.filter_by(slug='critic').one()
    operation=operations.propose_operation(ceo,_plan(ceo,engineer,critic))
    operations.approve(operation)

    observed=[]
    for sequence in range(30):
        state=operation_live_snapshot(operation)
        observed.append((state['status'],state['progress'],state['stage']))
        if operation.status!='RUNNING':
            break
        operations.next_step(operation,f'v09-e2e-{sequence}')

    state=operation_live_snapshot(operation)
    assert operation.status=='COMPLETED'
    assert state['progress']==100
    assert state['founder_report']['result']
    assert state['remaining']==state['budget']  # deterministic mock costs zero
    assert any(progress==75 for _,progress,_ in observed)
    assert any(progress==80 for _,progress,_ in observed)  # Meeting in progress
    assert any(progress==85 for _,progress,_ in observed)  # Meeting incorporated
    meeting=Meeting.query.filter_by(operation_id=operation.id).one()
    assert meeting.status=='ENDED'
    assert meeting.token_limit==11000
    assert meeting.minutes_json
    assert state['meeting']['tokens_used']>0
    assert state['meeting']['result']['agreement']
    assert AgentRun.query.filter_by(operation_id=operation.id,purpose='TASK_EXECUTION',status='SUCCEEDED').count()==1
    assert AgentRun.query.filter_by(operation_id=operation.id,purpose='TASK_REVIEW',status='SUCCEEDED').count()==1
    assert AgentRun.query.filter_by(operation_id=operation.id,purpose='MEETING_CONTRIBUTION',status='SUCCEEDED').count()>=2
    assert AgentRun.query.filter_by(operation_id=operation.id,purpose='GOAL_VERIFICATION',status='SUCCEEDED').count()==1
    assert AgentRun.query.filter_by(operation_id=operation.id,purpose='CEO_OPERATION_REPORT',status='SUCCEEDED').count()==1
    assert WorkMessage.query.filter_by(message_type='CEO_TO_FOUNDER').count()>=1


def test_one_founder_problem_executes_through_hq_api(client, ctx):
    request_text="Have the team investigate this difficult problem and report the answer."
    response=client.post('/headquarters/ceo/execute',json={
        'request':request_text,'mode':'AUTO',
    })
    payload=response.get_json()
    assert response.status_code==200
    assert payload['route']=='ACT'
    proposal=payload['answer']['proposal']
    assert proposal['tasks'][0]['assignee']=='Researcher'
    assert proposal['tasks'][0]['reviewer']=='Research Director'
    assert proposal['meeting']['trigger']=='BEFORE_FINAL_REPORT'
    assert proposal['meeting']['token_limit']==11000
    assert proposal['meeting']['retry_limit']==1

    operation_id=proposal['operation_id']
    approved=client.post(f'/headquarters/ceo/operations/{operation_id}/approve')
    assert approved.status_code==200

    snapshots=[]
    for sequence in range(30):
        state=client.get(f'/headquarters/ceo/operations/{operation_id}/state').get_json()
        snapshots.append(state)
        if not state['continue_allowed']:
            break
        step=client.post(
            f'/operations/{operation_id}/next-step',
            headers={'Idempotency-Key':f'v09-api-e2e-{sequence}'},
        )
        assert step.status_code==200,step.get_json()

    final=snapshots[-1]
    assert final['status']=='COMPLETED'
    assert final['progress']==100
    assert final['founder_report']['summary']
    assert final['meeting']['status']=='ENDED'
    assert final['meeting']['tokens_used']<=final['meeting']['token_limit']
    assert final['spent']<=final['budget']
    assert any(row['progress']==80 for row in snapshots)
    assert any(row['progress']==85 for row in snapshots)
    assert any(row['progress']==95 for row in snapshots)


def test_approved_meeting_retry_is_automatic_but_bounded(ctx, monkeypatch):
    ceo=Employee.query.filter_by(slug='ceo').one()
    engineer=Employee.query.filter_by(slug='engineer').one()
    critic=Employee.query.filter_by(slug='critic').one()
    operation=operations.propose_operation(ceo,_plan(ceo,engineer,critic))
    operations.approve(operation)
    for task in operation.tasks:
        task.status='DONE'
    db.session.commit()
    meeting=operations._start_planned_final_meeting(operation)

    def fail_with_paid_state(room,key):
        room.status='PAUSED'
        room.paid_failure_json={
            'run_id':77,'retry_strategy':'COMPACT_REPAIR','reason':'OUTPUT_TRUNCATED'
        }
        db.session.commit()
        raise ValueError('OUTPUT_TRUNCATED')

    retried=[]
    def retry(room):
        retried.append(room.id)
        room.status='RUNNING'
        room.paid_failure_json=None
        db.session.commit()
        return room

    monkeypatch.setattr(operations.meeting_service,'next_step_idempotent',fail_with_paid_state)
    monkeypatch.setattr(operations.meeting_service,'retry_paid_step',retry)
    monkeypatch.setattr(operations.meeting_service,'call_breakdown',lambda room:{'retries':0})
    result=operations._advance_meeting(operation,meeting,'auto-retry-1')
    assert result['kind']=='MEETING_AUTO_RETRY'
    assert result['retry_strategy']=='COMPACT_REPAIR'
    assert retried==[meeting.id]
    assert operation.status=='RUNNING'

    monkeypatch.setattr(operations.meeting_service,'call_breakdown',lambda room:{'retries':1})
    with pytest.raises(ValueError,match='retry allowance exhausted'):
        operations._advance_meeting(operation,meeting,'auto-retry-2')
    assert operation.status=='WAITING_FOR_FOUNDER'
