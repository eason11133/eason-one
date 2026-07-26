import sys,json
from decimal import Decimal
from types import SimpleNamespace
import pytest
from eason_one.extensions import db
from eason_one.models import *
from eason_one.providers import OpenAIProvider,ProviderResult
from eason_one.schemas import CEO_SCHEMA,REVIEW_SCHEMA,SYNTHESIS_SCHEMA,TASK_EXECUTION_SCHEMA
from eason_one.services.projects import create_project
from eason_one.services.tasks import create_task,transition
from eason_one.services.ceo import founder_request,generate_project_briefing
from eason_one.services.reviews import run_review
from eason_one.services.task_execution import run_task
from eason_one.services.execution import execute
from eason_one.services.brain import add_knowledge,current
from eason_one.services.approvals import review_proposal
from eason_one.services.model_configs import create as create_model
from eason_one.services.costs import conservative_estimate
from eason_one.services.company import get_company,remaining

def people():
    return Employee.query.filter_by(slug="ceo").one(),Employee.query.filter_by(slug="researcher").one(),Employee.query.filter_by(slug="research-director").one()
def project_task():
    ceo,e,r=people(); p=create_project("Beauty","Assess",ceo,status="ACTIVE")
    return p,create_task(p,"Research","Investigate",ceo,e,r,required_output="Brief",acceptance_criteria="Clear")
def capture_openai(monkeypatch):
    seen={}
    class Responses:
        def create(self,**kwargs):
            seen.update(kwargs); return SimpleNamespace(output_text="{}",usage=SimpleNamespace(input_tokens=1,output_tokens=1),id="fake")
    class Client:
        def __init__(self,**kwargs): self.responses=Responses()
    monkeypatch.setitem(sys.modules,"openai",SimpleNamespace(OpenAI=Client))
    monkeypatch.setenv("OPENAI_API_KEY","test-not-real")
    model=SimpleNamespace(model_name="configured-model")
    return seen,model
@pytest.mark.parametrize("schema",[CEO_SCHEMA,REVIEW_SCHEMA,SYNTHESIS_SCHEMA,TASK_EXECUTION_SCHEMA])
def test_openai_provider_passes_strict_schema(monkeypatch,schema):
    seen,model=capture_openai(monkeypatch)
    OpenAIProvider().complete(model,"system","user","context",100,schema)
    fmt=seen["text"]["format"]
    assert fmt["type"]=="json_schema" and fmt["strict"] is True and fmt["schema"]==schema["schema"]

def test_openai_zero_pricing_rejected_before_invocation(ctx,monkeypatch):
    _,e,_=people(); e.current_model.provider_key="openai"; e.current_model.input_price_per_million=0; e.current_model.output_price_per_million=1; db.session.commit(); called=[]
    monkeypatch.setattr("eason_one.services.execution.get_provider",lambda key:called.append(key))
    with pytest.raises(ValueError,match="greater than zero"): execute(e,"TEST","x")
    assert not called
def test_negative_prices_rejected_by_service(ctx):
    with pytest.raises(ValueError,match="negative"): create_model("bad","openai","x",-1,2,"TWD",10)
def test_mock_zero_pricing_is_valid(ctx):
    assert create_model("mock ok","mock","mock",0,0,"TWD",10).id
def test_model_route_cannot_bypass_validation(ctx,client):
    before=ModelConfig.query.count()
    client.post("/models",data={"label":"bad","provider_key":"openai","model_name":"x","input_price_per_million":"0",
      "output_price_per_million":"0","currency":"TWD","max_output_tokens":"100"})
    assert ModelConfig.query.count()==before
def test_near_budget_margin_rejects_before_provider(ctx,monkeypatch):
    _,e,_=people(); e.current_model.provider_key="openai"; e.current_model.input_price_per_million=1_000_000
    e.current_model.output_price_per_million=1; e.current_model.currency="TWD"; e.current_model.max_output_tokens=1; db.session.commit()
    # Leave less than the fixed framing overhead estimate.
    db.session.add(CostEvent(company_id=get_company().id,category="ADJUSTMENT",description="near cap",internal_credits_delta=0,
      real_cost_delta=Decimal("2800"),currency="TWD")); db.session.commit(); called=[]
    monkeypatch.setattr("eason_one.services.execution.get_provider",lambda key:called.append(key))
    with pytest.raises(ValueError,match="exhausted"): execute(e,"TEST","tiny")
    assert not called and conservative_estimate(e.current_model,"tiny",1)>remaining()

def test_valid_status_query_is_successful_ceo_ui(ctx,client):
    p,_=project_task(); response=client.post("/ceo",data={"request":"How is the Beauty project going?"},follow_redirects=True)
    run=AgentRun.query.filter_by(purpose="CEO_FOUNDER_REQUEST").one()
    assert run.parsed_output_json["mode"]=="STATUS_QUERY" and b"failed validation" not in response.data
    assert run.parsed_output_json["executive_response"].encode() in client.get("/command").data and Proposal.query.count()==0

def test_task_execution_stores_result_and_pending_proposal_only(ctx):
    p,t=project_task(); run=run_task(t)
    assert t.result_summary==run.parsed_output_json["result_summary"]
    assert Proposal.query.filter_by(project_id=p.id,status="PENDING").count()==1
    assert KnowledgeItem.query.count()==0
def candidate_result(item):
    return json.dumps({"result_summary":"done","knowledge_proposals":[item]})
@pytest.mark.parametrize("item",[
 {"kind":"EVIDENCE","title":"E","content":"x","source_ref":None,"rationale":None,"basis_knowledge_ids":[]},
 {"kind":"DECISION","title":"D","content":"x","source_ref":None,"rationale":None,"basis_knowledge_ids":[]},
 {"kind":"CORRECTION","title":"C","content":"x","source_ref":None,"rationale":None,"basis_knowledge_ids":[]},
 {"kind":"KILLED","title":"K","content":"x","source_ref":None,"rationale":None,"basis_knowledge_ids":[]},
])
def test_invalid_task_knowledge_candidate_is_skipped(ctx,monkeypatch,item):
    _,t=project_task()
    class P:
        def complete(self,*a): return ProviderResult(candidate_result(item),1,1)
    monkeypatch.setattr("eason_one.services.execution.get_provider",lambda key:P())
    run=run_task(t)
    assert run.parsed_output_json and t.result_summary=="done" and Proposal.query.count()==0 and KnowledgeItem.query.count()==0

def test_decision_basis_same_project_and_company_are_accepted(ctx):
    p,_=project_task()
    local=add_knowledge("EVIDENCE","Local","x",source_ref="s",project_id=p.id,founder_approved=True)
    company=add_knowledge("FACT","Company","x",founder_approved=True)
    decision=add_knowledge("DECISION","Choose","yes",rationale="why",project_id=p.id,founder_approved=True,basis_knowledge_ids=[local.id,company.id])
    refs=KnowledgeReference.query.filter_by(to_knowledge_id=decision.id,relation_type="BASIS_FOR").all()
    assert {r.from_knowledge_id for r in refs}=={local.id,company.id}
def test_unrelated_project_basis_is_rejected(ctx):
    p,_=project_task(); other=create_project("Other","x",people()[0],status="ACTIVE")
    basis=add_knowledge("FACT","Other fact","x",project_id=other.id,founder_approved=True)
    with pytest.raises(ValueError,match="unrelated"): add_knowledge("DECISION","D","x",rationale="r",project_id=p.id,founder_approved=True,basis_knowledge_ids=[basis.id])
def test_decision_proposal_approval_creates_provenance_atomically(ctx):
    p,_=project_task(); basis=add_knowledge("EVIDENCE","E","x",source_ref="s",project_id=p.id,founder_approved=True)
    run=execute(people()[1],"TEST","proposal",p)
    proposal=Proposal(project_id=p.id,agent_run_id=run.id,proposed_by_employee_id=people()[1].id,
      payload_json={"kind":"DECISION","title":"D","content":"yes","rationale":"because","source_ref":None,"target_knowledge_id":None,"basis_knowledge_ids":[basis.id]})
    db.session.add(proposal); db.session.commit(); review_proposal(proposal,"APPROVED")
    assert proposal.materialized_knowledge_id and KnowledgeReference.query.filter_by(to_knowledge_id=proposal.materialized_knowledge_id).count()==1
def test_provenance_failure_rolls_back_decision_and_refs(ctx,monkeypatch):
    p,_=project_task(); basis=add_knowledge("FACT","F","x",project_id=p.id,founder_approved=True)
    original=db.session.add
    def broken(obj):
        if isinstance(obj,KnowledgeReference): raise RuntimeError("reference failure")
        return original(obj)
    monkeypatch.setattr(db.session,"add",broken)
    with pytest.raises(RuntimeError): add_knowledge("DECISION","D","x",rationale="r",project_id=p.id,founder_approved=True,basis_knowledge_ids=[basis.id])
    assert KnowledgeItem.query.filter_by(kind="DECISION").count()==0 and KnowledgeReference.query.count()==0
def test_effective_ui_excludes_targets_and_keeps_killed_marker(ctx,client):
    p,_=project_task(); h=add_knowledge("HYPOTHESIS","OLD_TARGET","old",project_id=p.id,founder_approved=True)
    killed=add_knowledge("KILLED","KILLED_MARKER","no",project_id=p.id,target_knowledge_id=h.id,founder_approved=True)
    effective=current(p.id); assert h not in effective and killed in effective
    page=client.get(f"/projects/{p.id}").data
    current_section=page.split(b"Advanced / operations")[0]
    assert b"KILLED_MARKER" in current_section and b"OLD_TARGET" not in current_section
