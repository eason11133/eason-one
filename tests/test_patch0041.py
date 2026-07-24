from decimal import Decimal
from types import SimpleNamespace
import pytest
from eason_one.extensions import db
from eason_one.models import *
from eason_one.providers import ProviderResult,OpenAIProvider
from eason_one.services.execution import execute
from eason_one.services.brain import add_knowledge,validate_basis_ids
from eason_one.services.projects import create_project
from eason_one.services.tasks import create_task,transition
from eason_one.services.task_execution import run_task

def people():
    return Employee.query.filter_by(slug="ceo").one(),Employee.query.filter_by(slug="researcher").one()
def project(name="A"):
    return create_project(name,name,people()[0],status="ACTIVE")
def task_in(status):
    p=project(); ceo,e=people(); t=create_task(p,"Task","Work",ceo,e,ceo,required_output="x",acceptance_criteria="x")
    t.status=status; db.session.commit(); return t

def test_dependency_pins_openai_capability_floor():
    text=open("pyproject.toml",encoding="utf-8").read()
    assert "openai>=2.48.0,<3" in text
def test_openai_provider_distinguishes_response_and_request_ids(monkeypatch):
    response=SimpleNamespace(output_text="{}",usage=SimpleNamespace(input_tokens=2,output_tokens=1),id="resp_123",
      _request_id="req_456",status="completed",output=[],incomplete_details=None)
    class Responses:
        def create(self,**kwargs): return response
    class Client:
        def __init__(self,**kwargs): self.responses=Responses()
    monkeypatch.setattr("openai.OpenAI",Client); monkeypatch.setenv("OPENAI_API_KEY","not-real")
    result=OpenAIProvider().complete(SimpleNamespace(model_name="x"),"s","u","c",10)
    assert result.response_id=="resp_123" and result.request_id=="req_456"
def test_agent_run_persists_and_ui_displays_both_ids(ctx,client,monkeypatch):
    _,e=people()
    class P:
        def complete(self,*args): return ProviderResult("ok",2,1,response_id="resp_x",request_id="req_x")
    monkeypatch.setattr("eason_one.services.execution.get_provider",lambda key:P())
    run=execute(e,"TEST","x"); page=client.get(f"/runs/{run.id}").data
    assert run.provider_response_id=="resp_x" and run.provider_request_id=="req_x"
    assert b"Provider request ID: req_x" in page and b"Provider response ID: resp_x" in page

@pytest.mark.parametrize("provider_key",["mock","openai"])
def test_inactive_model_never_invokes_provider(ctx,monkeypatch,provider_key):
    _,e=people(); e.current_model.provider_key=provider_key; e.current_model.active=False
    if provider_key=="openai": e.current_model.input_price_per_million=e.current_model.output_price_per_million=1
    db.session.commit(); called=[]
    monkeypatch.setattr("eason_one.services.execution.get_provider",lambda key:called.append(key))
    with pytest.raises(ValueError,match="inactive"): execute(e,"TEST","x")
    assert not called and AgentRun.query.count()==0
def test_reactivated_model_executes(ctx):
    _,e=people(); e.current_model.active=False; db.session.commit()
    with pytest.raises(ValueError): execute(e,"TEST","x")
    e.current_model.active=True; db.session.commit()
    assert execute(e,"TEST","x").status=="SUCCEEDED"

def test_killed_marker_and_historical_hypothesis_cannot_be_basis(ctx):
    p=project(); h=add_knowledge("HYPOTHESIS","H","x",project_id=p.id,founder_approved=True)
    killed=add_knowledge("KILLED","K","no",project_id=p.id,target_knowledge_id=h.id,founder_approved=True)
    for ident in (h.id,killed.id):
        with pytest.raises(ValueError,match="effective"): validate_basis_ids([ident],p.id)
def test_corrected_target_invalid_but_correction_valid_basis(ctx):
    p=project(); fact=add_knowledge("FACT","Old","x",project_id=p.id,founder_approved=True)
    correction=add_knowledge("CORRECTION","New","y",project_id=p.id,target_knowledge_id=fact.id,founder_approved=True)
    with pytest.raises(ValueError,match="effective"): validate_basis_ids([fact.id],p.id)
    assert validate_basis_ids([correction.id],p.id)==[correction]

def test_project_cannot_kill_other_project_hypothesis(ctx):
    a,b=project("A"),project("B"); h=add_knowledge("HYPOTHESIS","B H","x",project_id=b.id,founder_approved=True)
    with pytest.raises(ValueError,match="unrelated"): add_knowledge("KILLED","K","x",project_id=a.id,target_knowledge_id=h.id,founder_approved=True)
def test_project_cannot_correct_other_project_fact(ctx):
    a,b=project("A"),project("B"); fact=add_knowledge("FACT","B F","x",project_id=b.id,founder_approved=True)
    with pytest.raises(ValueError,match="unrelated"): add_knowledge("CORRECTION","C","x",project_id=a.id,target_knowledge_id=fact.id,founder_approved=True)
def test_project_may_correct_company_fact(ctx):
    a=project(); fact=add_knowledge("FACT","Company","x",founder_approved=True)
    assert add_knowledge("CORRECTION","Scoped correction","y",project_id=a.id,target_knowledge_id=fact.id,founder_approved=True)
def test_company_cannot_target_project_knowledge(ctx):
    p=project(); fact=add_knowledge("FACT","Project","x",project_id=p.id,founder_approved=True)
    with pytest.raises(ValueError,match="Company-level"): add_knowledge("CORRECTION","C","y",target_knowledge_id=fact.id,founder_approved=True)

@pytest.mark.parametrize(("result","message"),[
 (ProviderResult("",10,4,response_id="resp_i",request_id="req_i",status="incomplete",incomplete_reason="max_output_tokens"),"incomplete: max_output_tokens"),
 (ProviderResult("",10,4,response_id="resp_r",request_id="req_r",status="completed",refusal="cannot comply"),"refusal: cannot comply"),
])
def test_billable_non_normal_response_ledgers_once(ctx,monkeypatch,result,message):
    _,e=people(); e.current_model.provider_key="openai"; e.current_model.input_price_per_million=10
    e.current_model.output_price_per_million=20; e.current_model.currency="TWD"; db.session.commit()
    class P:
        def complete(self,*args): return result
    monkeypatch.setattr("eason_one.services.execution.get_provider",lambda key:P())
    run=execute(e,"TEST","x")
    assert run.status=="FAILED" and message in run.error_text
    assert run.input_tokens==10 and run.output_tokens==4 and run.real_cost>0
    assert CostEvent.query.filter_by(agent_run_id=run.id,category="MODEL").count()==1

@pytest.mark.parametrize("status",["REVIEW","DONE","BLOCKED","FAILED","CANCELLED"])
def test_non_executable_task_rejected_before_any_side_effect(ctx,monkeypatch,status):
    task=task_in(status); task.result_summary="original"; db.session.commit(); called=[]
    before=(AgentRun.query.count(),CostEvent.query.count(),Proposal.query.count())
    monkeypatch.setattr("eason_one.services.execution.get_provider",lambda key:called.append(key))
    with pytest.raises(ValueError,match="not executable"): run_task(task)
    assert not called and before==(AgentRun.query.count(),CostEvent.query.count(),Proposal.query.count())
    assert task.result_summary=="original" and task.status==status
@pytest.mark.parametrize("status",["ASSIGNED","WORKING"])
def test_executable_task_states_are_allowed(ctx,status):
    task=task_in(status); assert run_task(task).status=="SUCCEEDED"
def test_done_task_post_cannot_double_bill_or_mutate(ctx,client,monkeypatch):
    task=task_in("DONE"); task.result_summary="final"; db.session.commit(); called=[]
    before=(AgentRun.query.count(),CostEvent.query.count(),Proposal.query.count())
    monkeypatch.setattr("eason_one.services.execution.get_provider",lambda key:called.append(key))
    response=client.post(f"/tasks/{task.id}/run",follow_redirects=True)
    assert response.status_code==200 and not called
    assert before==(AgentRun.query.count(),CostEvent.query.count(),Proposal.query.count())
    assert db.session.get(Task,task.id).result_summary=="final"
