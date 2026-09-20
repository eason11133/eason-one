from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from eason_one.extensions import db
from eason_one.models import AgentRun, Employee, Project, ResearchRecord, Task
from eason_one.services.headquarters import route_ceo_intent


def test_ceo_office_is_primary_hq_surface(client, ctx):
    page = client.get('/headquarters').get_data(as_text=True)
    assert 'CEO OFFICE · FOUNDER ON SITE' in page
    assert 'CEO Office' in page
    assert 'CEO RECOMMENDATION' in page
    assert 'data-ceo-office-form' in page
    assert 'data-ceo-runtime' in page
    assert 'Needs your attention' in page
    assert 'CURRENT MISSION' in page


def test_ceo_intent_router_separates_brief_advise_and_act(ctx):
    assert route_ceo_intent('Give me the current company status.')['route'] == 'BRIEF'
    assert route_ceo_intent('What is the most important problem right now?')['route'] == 'ADVISE'
    assert route_ceo_intent('Prepare a plan to build the first dogfooding project.')['route'] == 'ACT'
    assert route_ceo_intent('anything', 'BRIEF')['route'] == 'BRIEF'


def test_brief_preview_is_immediate_and_does_not_call_provider(client, ctx, monkeypatch):
    before = AgentRun.query.count()
    calls = []

    def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError('Provider-backed founder_request must not run for preview')

    monkeypatch.setattr('eason_one.routes.founder_request', forbidden)
    response = client.post('/headquarters/ceo/preview', json={
        'request': 'Give me the current company status.',
        'mode': 'AUTO',
    })
    payload = response.get_json()
    assert response.status_code == 200
    assert payload['route'] == 'BRIEF'
    assert payload['execute_required'] is False
    assert payload['immediate']['title'] == 'CEO headquarters briefing'
    assert payload['server_preview_ms'] >= 0
    assert AgentRun.query.count() == before
    assert calls == []


def test_advise_preview_returns_useful_context_before_model(client, ctx):
    response = client.post('/headquarters/ceo/preview', json={
        'request': 'What is the most important problem right now?',
        'mode': 'AUTO',
    })
    payload = response.get_json()
    assert payload['route'] == 'ADVISE'
    assert payload['execute_required'] is True
    assert payload['immediate']['title'] == 'Immediate company context'
    assert any('Founder decisions' in fact for fact in payload['immediate']['facts'])


def test_advice_execution_returns_normalized_ceo_response(client, ctx, monkeypatch):
    run = SimpleNamespace(
        id=812,
        status='SUCCEEDED',
        purpose='CEO_FOUNDER_REQUEST',
        parsed_output_json={
            'mode': 'ADVISORY',
            'executive_response': 'Fix the verified execution loop before adding more surface area.',
        },
        error_text=None,
    )
    monkeypatch.setattr('eason_one.routes.founder_request', lambda *args, **kwargs: (run, None))
    response = client.post('/headquarters/ceo/execute', json={
        'request': 'What should we fix first?',
        'mode': 'ADVISE',
    })
    payload = response.get_json()
    assert response.status_code == 200
    assert payload['route'] == 'ADVISE'
    assert payload['answer']['title'] == 'CEO judgment ready'
    assert payload['answer']['run_id'] == 812
    assert 'verified execution loop' in payload['answer']['summary']


def test_ceo_office_latency_is_captured_automatically(client, ctx):
    response = client.post('/headquarters/ceo/metrics', json={
        'request_id': 'test-v08-latency',
        'route': 'BRIEF',
        'first_useful_ms': 145.2,
        'total_ms': 151.8,
        'preview_server_ms': 31.4,
        'success': True,
    })
    assert response.status_code == 200
    record = ResearchRecord.query.filter_by(record_key='CEO-UX-test-v08-latency').one()
    assert record.record_type == 'UX_METRIC'
    assert record.founder_approved is False
    assert record.metadata_json['first_useful_ms'] == 145.2
    research = client.get('/headquarters/research').get_data(as_text=True)
    assert 'CEO UX interactions' in research
    assert 'CEO office interaction latency' not in research  # background telemetry, not Founder evidence


def test_since_last_visit_uses_session_and_authoritative_activity(client, ctx):
    first = client.get('/headquarters')
    assert first.status_code == 200
    ceo = Employee.query.filter_by(slug='ceo').one()
    project = Project(
        name='Session project', objective='Test visit deltas', status='ACTIVE',
        priority='MEDIUM', environment='LIVE', owner_employee_id=ceo.id,
    )
    db.session.add(project)
    db.session.flush()
    task = Task(
        project_id=project.id, title='Session-visible authoritative change',
        objective='Expose a new persisted activity event', status='WORKING',
        assigned_employee_id=ceo.id, updated_at=datetime.now(timezone.utc) + timedelta(seconds=1),
    )
    db.session.add(task)
    db.session.commit()
    second = client.get('/headquarters').get_data(as_text=True)
    assert 'SINCE YOUR LAST VISIT' in second
    assert 'Session-visible authoritative change' in second


def test_chinese_add_endpoint_with_budget_routes_to_governed_work():
    request = (
        '在 Eason One 本身新增一個 GET /api/founder-ping endpoint，必須用真實啟動的服務做 HTTP 驗證，'
        '回傳 HTTP 200，JSON 至少包含 service="eason-one"、status="ok" 和目前 application version。'
        '要留下可追溯 Artifact 與 real loopback HTTP verification。Project 總預算 NT$5。'
    )
    routed = route_ceo_intent(request)
    assert routed['route'] == 'ACT'
    assert routed['route_type'] == 'AUTO_DELEGATION'
