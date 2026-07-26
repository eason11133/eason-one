import json
from decimal import Decimal
import pytest
from eason_one.extensions import db
from eason_one.models import *
from eason_one.providers import ProviderResult
from eason_one.services.brain import add_knowledge,current,active_hypotheses
from eason_one.services.context import build
from eason_one.services.approvals import review_proposal
from eason_one.services.execution import execute
from eason_one.services.company import remaining
from eason_one.services.ceo import founder_request,materialize_project_plan
from eason_one.services.projects import create_project
from eason_one.services.interviews import start,ask
from eason_one.services.contributions import total,project_totals
from eason_one.services.learning import create as create_learning
from eason_one.i18n import translate

def people():
    return Employee.query.filter_by(slug="ceo").one(),Employee.query.filter_by(slug="researcher").one()
def projects():
    ceo,_=people(); return create_project("Alpha","A",ceo),create_project("Beta","B",ceo)
def proposal(payload):
    ceo,_=people(); run=execute(ceo,"TEST","proposal")
    p=Proposal(agent_run_id=run.id,proposed_by_employee_id=ceo.id,payload_json=payload)
    db.session.add(p); db.session.commit(); return p

def test_killed_targeting_fact_fails(ctx):
    fact=add_knowledge("FACT","F","fact",founder_approved=True)
    with pytest.raises(ValueError,match="HYPOTHESIS"): add_knowledge("KILLED","K","no",target_knowledge_id=fact.id,founder_approved=True)
def test_killed_targeting_missing_id_fails(ctx):
    with pytest.raises(ValueError,match="does not exist"): add_knowledge("KILLED","K","no",target_knowledge_id=999,founder_approved=True)
def test_correction_targeting_missing_id_fails(ctx):
    with pytest.raises(ValueError,match="does not exist"): add_knowledge("CORRECTION","C","new",target_knowledge_id=999,founder_approved=True)
def test_correction_replaces_target_and_remains_effective(ctx):
    fact=add_knowledge("FACT","Old","old",founder_approved=True); correction=add_knowledge("CORRECTION","New","new",target_knowledge_id=fact.id,founder_approved=True)
    assert fact not in current() and correction in current()
def test_killed_target_inactive_but_marker_effective(ctx):
    h=add_knowledge("HYPOTHESIS","Bad idea","bad",founder_approved=True); marker=add_knowledge("KILLED","Do not retry","failed",target_knowledge_id=h.id,founder_approved=True)
    assert h not in active_hypotheses() and marker in current()
def test_killed_marker_reaches_agent_context(ctx):
    ceo,_=people(); h=add_knowledge("HYPOTHESIS","Bad idea","bad",founder_approved=True); add_knowledge("KILLED","Never again","failed",target_knowledge_id=h.id,founder_approved=True)
    text=build(ceo); assert "KILLED IDEAS" in text and "Never again" in text

@pytest.mark.parametrize("payload",[
 {"kind":"EVIDENCE","title":"E","content":"x"},
 {"kind":"DECISION","title":"D","content":"x"},
 {"kind":"KILLED","title":"K","content":"x","target_knowledge_id":999},
])
def test_proposal_approval_cannot_bypass_brain_validation(ctx,payload):
    p=proposal(payload)
    with pytest.raises(ValueError): review_proposal(p,"APPROVED")
    assert db.session.get(Proposal,p.id).status=="PENDING" and KnowledgeItem.query.count()==0

def test_project_context_excludes_other_project_knowledge(ctx):
    ceo,_=people(); a,b=projects()
    add_knowledge("FACT","A secret","ALPHA_RAW",project_id=a.id,founder_approved=True)
    add_knowledge("FACT","B secret","BETA_RAW",project_id=b.id,founder_approved=True)
    assert "ALPHA_RAW" in build(ceo,a) and "BETA_RAW" not in build(ceo,a)
def test_projectless_context_excludes_all_project_raw_knowledge(ctx):
    ceo,_=people(); a,b=projects()
    add_knowledge("FACT","A","ALPHA_RAW",project_id=a.id,founder_approved=True); add_knowledge("FACT","B","BETA_RAW",project_id=b.id,founder_approved=True)
    text=build(ceo); assert "ALPHA_RAW" not in text and "BETA_RAW" not in text

def test_provider_receives_selected_model_config(ctx,monkeypatch):
    _,e=people(); seen={}
    class P:
        def complete(self,model_config,system_prompt,user_prompt,context,max_output_tokens):
            seen["model"]=model_config.model_name; return ProviderResult("ok",2,1,"r")
    monkeypatch.setattr("eason_one.services.execution.get_provider",lambda key:P())
    e.current_model.model_name="organization-selected-model"; db.session.commit(); execute(e,"TEST","x")
    assert seen["model"]=="organization-selected-model"
def test_agent_run_snapshots_are_immutable(ctx):
    _,e=people(); run=execute(e,"TEST","x"); snapshots=(run.provider_key_snapshot,run.model_name_snapshot,run.input_price_snapshot,run.output_price_snapshot,run.currency_snapshot)
    e.current_model.model_name="changed"; e.current_model.input_price_per_million=999; db.session.commit()
    assert snapshots==(run.provider_key_snapshot,run.model_name_snapshot,run.input_price_snapshot,run.output_price_snapshot,run.currency_snapshot)

def test_precall_budget_rejection_never_invokes_provider(ctx,monkeypatch):
    _,e=people(); called=[]; e.current_model.output_price_per_million=10_000_000; db.session.commit()
    monkeypatch.setattr("eason_one.services.execution.get_provider",lambda key:called.append(True))
    with pytest.raises(ValueError): execute(e,"TEST","x")
    assert called==[]
def test_successful_paid_call_has_exactly_one_cost_event(ctx):
    _,e=people(); e.current_model.input_price_per_million=10; db.session.commit(); run=execute(e,"TEST","x")
    assert CostEvent.query.filter_by(agent_run_id=run.id,category="MODEL").count()==1
def test_known_cost_survives_downstream_failure(ctx):
    _,e=people(); e.current_model.input_price_per_million=10; db.session.commit()
    with pytest.raises(RuntimeError): execute(e,"TEST","x",postprocess=lambda run:(_ for _ in ()).throw(RuntimeError("parse")))
    run=AgentRun.query.order_by(AgentRun.id.desc()).first()
    assert run.real_cost>0 and CostEvent.query.filter_by(agent_run_id=run.id).count()==1 and remaining()<3000
def test_recording_same_run_does_not_duplicate_cost(ctx):
    from eason_one.services.costs import record
    _,e=people(); run=execute(e,"TEST","x"); record(run); db.session.commit()
    assert CostEvent.query.filter_by(agent_run_id=run.id).count()==1

def test_ceo_structured_output_changes_tasks(ctx):
    ceo,_=people(); _,research=founder_request(ceo,"Investigate consumer demand project")
    _,engineering=founder_request(ceo,"Build a software website project")
    assert research.payload_json["plan"]["tasks"][0]["assignee_slug"]=="researcher"
    assert engineering.payload_json["plan"]["tasks"][0]["assignee_slug"]=="engineer"
def test_invalid_ceo_output_creates_no_proposal(ctx,monkeypatch):
    ceo,_=people()
    class P:
        def complete(self,*args,**kwargs): return ProviderResult("not-json",1,1)
    monkeypatch.setattr("eason_one.services.execution.get_provider",lambda key:P())
    run,p=founder_request(ceo,"anything")
    assert p is None and Proposal.query.count()==0 and "validation failed" in run.error_text
def test_invalid_second_task_rolls_back_materialization(ctx):
    ceo,_=people(); _,p=founder_request(ceo,"Investigate demand project")
    second=dict(p.payload_json["plan"]["tasks"][0]); second["assignee_slug"]="missing"; p.payload_json["plan"]["tasks"].append(second)
    from sqlalchemy.orm.attributes import flag_modified
    flag_modified(p,"payload_json"); db.session.commit()
    with pytest.raises(ValueError): materialize_project_plan(p)
    assert Project.query.count()==0 and Task.query.count()==0 and db.session.get(Proposal,p.id).status=="PENDING"

def test_interview_second_turn_contains_first_turn(ctx,monkeypatch):
    _,e=people(); captured=[]
    class P:
        def complete(self,model_config,system_prompt,user_prompt,context,max_output_tokens):
            captured.append(context); return ProviderResult("answer",1,1)
    monkeypatch.setattr("eason_one.services.execution.get_provider",lambda key:P())
    i=start(e); ask(i,"FIRST UNIQUE QUESTION"); ask(i,"second question")
    assert "FIRST UNIQUE QUESTION" in captured[1] and FounderInterview.query.count()==1
def test_project_contribution_grouped_and_company_independent(ctx):
    _,e=people(); a,b=projects()
    db.session.add_all([ContributionEvent(employee_id=e.id,project_id=a.id,scope="PROJECT",event_type="X",value=12,reason="x"),
      ContributionEvent(employee_id=e.id,project_id=b.id,scope="PROJECT",event_type="X",value=7,reason="x"),
      ContributionEvent(employee_id=e.id,scope="COMPANY",event_type="X",value=18,reason="x")]); db.session.commit()
    grouped={p.name:v for p,v in project_totals(e.id)}
    assert grouped=={"Alpha":12,"Beta":7} and total(e.id,"COMPANY")==18
def test_founder_can_create_validated_learning(ctx):
    _,e=people(); row=create_learning(e,"Lesson","Evidence",source_ref="founder:1",validated=True)
    assert row.validated and row.source_ref=="founder:1"
def test_execution_cannot_create_validated_learning(ctx):
    _,e=people(); execute(e,"TEST","claim learned"); assert EmployeeLearningRecord.query.count()==0
def test_package_discovery_excludes_instance():
    text=open("pyproject.toml",encoding="utf-8").read(); assert 'include = ["eason_one*"]' in text and 'exclude = ["instance*", "tests*"]' in text
def test_english_default(app):
    with app.test_request_context("/"): assert translate("nav.projects")=="Projects"
def test_language_switch_and_session_persistence(client):
    client.post("/language/zh-TW",data={"next":"/ceo"})
    assert "指揮中心".encode() in client.get("/command").data
    assert "團隊".encode() in client.get("/team").data
def test_missing_zh_translation_falls_back_to_english(app):
    from eason_one.i18n import TRANSLATIONS
    TRANSLATIONS["en"]["test.only.en"]="English fallback"
    with app.test_request_context("/"):
        from flask import session
        session["language"]="zh-TW"; assert translate("test.only.en")=="English fallback"
