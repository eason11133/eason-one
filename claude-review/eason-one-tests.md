# Eason One — Test Review Bundle


---

## FILE: tests\conftest.py

``python
import pytest
from eason_one import create_app
from eason_one.extensions import db
from eason_one.seed import seed

@pytest.fixture()
def app(tmp_path):
    app=create_app({"TESTING":True,"SQLALCHEMY_DATABASE_URI":f"sqlite:///{tmp_path/'test.db'}"})
    with app.app_context():
        db.drop_all(); db.create_all(); seed()
        yield app
        db.session.remove()

@pytest.fixture()
def ctx(app):
    with app.app_context(): yield

@pytest.fixture()
def client(app): return app.test_client()

``

---

## FILE: tests\test_core.py

``python
from decimal import Decimal
import pytest
from eason_one.extensions import db
from eason_one.models import *
from eason_one.services.employees import change_model
from eason_one.services.projects import create_project
from eason_one.services.tasks import create_task, assign, transition, store_result, review
from eason_one.services.company import spent, remaining, get_company
from eason_one.services.execution import execute
from eason_one.services.contributions import total
from eason_one.services.brain import add_knowledge, active_hypotheses
from eason_one.services.approvals import review_proposal
from eason_one.services.ceo import founder_request
from eason_one.services.interviews import start, ask

def people():
    return Employee.query.filter_by(slug="ceo").one(),Employee.query.filter_by(slug="researcher").one(),Employee.query.filter_by(slug="critic").one()

def project():
    ceo,_,_=people(); return create_project("Pilot","Determine viability",ceo)

def test_employee_identity_and_model_history(ctx):
    _,e,_=people(); eid=e.id
    m=ModelConfig(label="Other",provider_key="mock",model_name="v2",input_price_per_million=0,output_price_per_million=0)
    db.session.add(m); db.session.commit(); change_model(e,m,"Upgrade")
    assert e.id==eid and Employee.query.count()==6 and e.current_model.model_name=="v2"
    assert EmployeeModelHistory.query.filter_by(employee_id=e.id).count()==2
    assert EmployeeModelHistory.query.filter_by(employee_id=e.id,ended_at=None).one().model_config_id==m.id

def test_project_task_assignment_and_transitions(ctx):
    ceo,e,critic=people(); p=project()
    t=create_task(p,"Research","Find evidence",ceo)
    assert p.id and t.status=="TODO"
    assign(t,e); assert t.status=="ASSIGNED"
    with pytest.raises(ValueError): transition(t,"DONE")
    transition(t,"WORKING")
    with pytest.raises(ValueError): transition(t,"DONE")
    store_result(t,"Evidence report"); assert t.status=="REVIEW" and t.result_summary=="Evidence report"
    review(t,critic,"Accepted",True)
    assert t.status=="DONE" and t.completed_at and WorkMessage.query.filter_by(task_id=t.id,message_type="REVIEW").count()==1

def test_rejected_review_returns_to_work(ctx):
    ceo,e,critic=people(); t=create_task(project(),"Task","Work",ceo,e,critic)
    transition(t,"WORKING"); store_result(t,"Draft"); review(t,critic,"Needs sources",False)
    assert t.status=="WORKING"

def test_budget_cost_ledger_and_hard_cap(ctx):
    _,e,_=people(); p=project()
    model=e.current_model; model.input_price_per_million=100; model.output_price_per_million=200; db.session.commit()
    run=execute(e,"TEST","hello world",p)
    expected=(Decimal(run.input_tokens)*100+Decimal(run.output_tokens)*200)/Decimal(1_000_000)
    assert run.real_cost==expected and CostEvent.query.filter_by(agent_run_id=run.id).one()
    assert get_company().real_budget_limit==3000 and spent()==expected and remaining()==Decimal("3000")-expected
    db.session.add(CostEvent(company_id=get_company().id,category="ADJUSTMENT",description="Exhaust",internal_credits_delta=0,real_cost_delta=remaining(),currency="TWD")); db.session.commit()
    with pytest.raises(ValueError,match="exhausted"): execute(e,"TEST","blocked")

def test_contribution_scopes_are_independent(ctx):
    _,e,_=people(); p=project(); p2=create_project("Other","Other",people()[0])
    db.session.add_all([
      ContributionEvent(employee_id=e.id,project_id=p.id,scope="PROJECT",event_type="DELIVERY",value=5,reason="Founder"),
      ContributionEvent(employee_id=e.id,project_id=p2.id,scope="PROJECT",event_type="DELIVERY",value=7,reason="Founder"),
      ContributionEvent(employee_id=e.id,scope="COMPANY",event_type="REUSE",value=2,reason="Founder")]); db.session.commit()
    assert total(e.id,"PROJECT",p.id)==5 and total(e.id,"PROJECT",p2.id)==7 and total(e.id,"COMPANY")==2

def test_brain_validation_append_only_and_killed_filter(ctx):
    with pytest.raises(ValueError): add_knowledge("EVIDENCE","Claim","Observation",founder_approved=True)
    with pytest.raises(ValueError): add_knowledge("DECISION","Choice","Do it",founder_approved=True)
    h=add_knowledge("HYPOTHESIS","Idea","Might work",founder_approved=True)
    c=add_knowledge("CORRECTION","Correction","Revised",target_knowledge_id=h.id,founder_approved=True)
    assert KnowledgeItem.query.get(h.id) and KnowledgeItem.query.get(c.id)
    h2=add_knowledge("HYPOTHESIS","Other","Maybe",founder_approved=True)
    killed=add_knowledge("KILLED","Killed","Disproved",target_knowledge_id=h2.id,founder_approved=True)
    assert KnowledgeItem.query.get(h2.id) and killed and h2 not in active_hypotheses()

def test_rejected_proposal_never_materializes(ctx):
    ceo,_,_=people(); run=execute(ceo,"TEST","proposal")
    p=Proposal(agent_run_id=run.id,proposed_by_employee_id=ceo.id,payload_json={"kind":"FACT","title":"X","content":"Y"})
    db.session.add(p); db.session.commit(); review_proposal(p,"REJECTED","No evidence")
    assert p.materialized_knowledge_id is None and KnowledgeItem.query.count()==0

def test_ai_run_cannot_approve_proposal(ctx):
    ceo,_,_=people(); run,p=founder_request(ceo,"Create a test project")
    assert run.status=="SUCCEEDED" and run.parsed_output_json and p.status=="PENDING"
    assert p.materialized_knowledge_id is None

def test_ceo_vertical_flow_routes_are_governed(ctx,client):
    response=client.post("/ceo",data={"request":"Create a Beauty LINE Consultation project"},follow_redirects=True)
    assert response.status_code==200 and AgentRun.query.filter_by(purpose="CEO_FOUNDER_REQUEST").count()==1
    proposal=Proposal.query.one(); assert Project.query.count()==0 and proposal.status=="PENDING"
    client.post(f"/inbox/{proposal.id}/materialize")
    assert Project.query.count()==1 and Task.query.count()>=1 and proposal.status=="APPROVED"

def test_interview_persists_without_instruction_mutation(ctx):
    _,e,_=people(); original=e.system_instructions; i=start(e)
    ask(i,"What are your blockers?")
    assert FounderInterviewMessage.query.filter_by(interview_id=i.id).count()==2
    assert db.session.get(Employee,e.id).system_instructions==original

def test_key_routes_reject_invalid_review(ctx,client):
    ceo,e,critic=people(); t=create_task(project(),"Task","Work",ceo,e,critic)
    response=client.post(f"/tasks/{t.id}/review",data={"content":"skip","decision":"accept"},follow_redirects=True)
    assert response.status_code==200 and db.session.get(Task,t.id).status=="ASSIGNED"

``

---

## FILE: tests\test_hardening.py

``python
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
    assert "專案".encode() in client.get("/ceo").data
    assert "員工".encode() in client.get("/employees").data
def test_missing_zh_translation_falls_back_to_english(app):
    from eason_one.i18n import TRANSLATIONS
    TRANSLATIONS["en"]["test.only.en"]="English fallback"
    with app.test_request_context("/"):
        from flask import session
        session["language"]="zh-TW"; assert translate("test.only.en")=="English fallback"

``

---

## FILE: tests\test_slice003.py

``python
import json
from decimal import Decimal
import pytest
from sqlalchemy.orm.attributes import flag_modified
from eason_one.extensions import db
from eason_one.models import *
from eason_one.providers import ProviderResult
from eason_one.services.ceo import founder_request,operating_context,materialize_project_plan,generate_project_briefing
from eason_one.services.projects import create_project
from eason_one.services.tasks import create_task,transition,store_result
from eason_one.services.reviews import run_review
from eason_one.services.costs import conservative_estimate
from eason_one.services.execution import execute
from eason_one.services.employees import change_model
from eason_one.services.approvals import review_proposal

def people():
    return (Employee.query.filter_by(slug="ceo").one(),Employee.query.filter_by(slug="researcher").one(),
      Employee.query.filter_by(slug="research-director").one())
def ready_task(instruction="work"):
    ceo,researcher,director=people(); p=create_project("Beauty","Test demand",ceo,status="ACTIVE")
    t=create_task(p,"Research",instruction,ceo,researcher,director,required_output="Brief",acceptance_criteria="Evidence")
    transition(t,"WORKING"); store_result(t,"Result with evidence"); return p,t

def test_ceo_request_never_mutates_permanent_instructions(ctx):
    ceo,_,_=people(); original=ceo.system_instructions; founder_request(ceo,"Create a pilot project")
    assert db.session.get(Employee,ceo.id).system_instructions==original
def test_ceo_execution_failure_preserves_instructions(ctx,monkeypatch):
    ceo,_,_=people(); original=ceo.system_instructions
    class Broken:
        def complete(self,*a,**k): raise RuntimeError("provider failed")
    monkeypatch.setattr("eason_one.services.execution.get_provider",lambda key:Broken())
    with pytest.raises(RuntimeError): founder_request(ceo,"Create a pilot project")
    assert db.session.get(Employee,ceo.id).system_instructions==original
def test_ceo_context_contains_every_active_slug(ctx):
    text=operating_context()
    for e in Employee.query.filter_by(active=True): assert f"slug: {e.slug}" in text
def test_ceo_context_contains_project_summaries_not_raw_knowledge(ctx):
    ceo,_,_=people(); a=create_project("Alpha","A",ceo,status="ACTIVE"); b=create_project("Beta","B",ceo,status="ACTIVE")
    db.session.add_all([KnowledgeItem(project_id=a.id,kind="FACT",title="A",content="ALPHA_RAW",founder_approved=True),
      KnowledgeItem(project_id=b.id,kind="FACT",title="B",content="BETA_RAW",founder_approved=True)]); db.session.commit()
    text=operating_context(); assert f"Project #{a.id}: Alpha" in text and f"Project #{b.id}: Beta" in text
    assert "ALPHA_RAW" not in text and "BETA_RAW" not in text

def test_new_project_creates_governed_proposal(ctx):
    run,p=founder_request(people()[0],"Create a Beauty consultation project")
    assert run.parsed_output_json["mode"]=="NEW_PROJECT" and p.status=="PENDING" and Project.query.count()==0
def test_project_action_proposes_tasks_for_existing_project(ctx):
    p,_=ready_task(); run,proposal=founder_request(people()[0],"Add task and continue project")
    assert run.parsed_output_json["mode"]=="PROJECT_ACTION" and proposal.project_id==p.id
def test_status_query_creates_no_proposal_or_project(ctx):
    p,_=ready_task(); before=(Proposal.query.count(),Project.query.count())
    run,proposal=founder_request(people()[0],"How is the Beauty project going?")
    assert run.parsed_output_json["mode"]=="STATUS_QUERY" and proposal is None
    assert (Proposal.query.count(),Project.query.count())==before
def test_invalid_project_id_is_rejected(ctx,monkeypatch):
    class P:
        def complete(self,*a,**k): return ProviderResult(json.dumps({"mode":"STATUS_QUERY","executive_response":"x","project_id":999}),1,1)
    monkeypatch.setattr("eason_one.services.execution.get_provider",lambda key:P())
    run,p=founder_request(people()[0],"status"); assert p is None and "unknown Project" in run.error_text

@pytest.mark.parametrize(("instruction","expected"),[("accept","DONE"),("please revise","WORKING"),("block this","BLOCKED")])
def test_reviewer_decisions_apply_deterministic_transition(ctx,instruction,expected):
    _,task=ready_task(); run=run_review(task,instruction=instruction)
    assert task.status==expected and run.purpose=="TASK_REVIEW"
def test_review_message_links_to_reviewer_run(ctx):
    _,task=ready_task(); run=run_review(task)
    message=WorkMessage.query.filter_by(task_id=task.id,message_type="REVIEW").one()
    assert message.agent_run_id==run.id and message.sender_employee_id==task.reviewer_employee_id
def test_malformed_review_preserves_review_state(ctx,monkeypatch):
    _,task=ready_task()
    class P:
        def complete(self,*a,**k): return ProviderResult("bad",1,1)
    monkeypatch.setattr("eason_one.services.execution.get_provider",lambda key:P())
    run_review(task); assert db.session.get(Task,task.id).status=="REVIEW" and WorkMessage.query.count()==0

def test_ceo_synthesis_receives_results_and_is_stored(ctx,client):
    project,task=ready_task(); run_review(task)
    run=generate_project_briefing(people()[0],project)
    assert "Result with evidence" in run.context_snapshot and run.parsed_output_json["executive_summary"]
    report=WorkMessage.query.filter_by(project_id=project.id,message_type="REPORT").one()
    assert report.agent_run_id==run.id and client.get(f"/projects/{project.id}").data.find(report.content.encode())>=0
def test_ceo_synthesis_does_not_complete_project(ctx):
    project,_=ready_task(); generate_project_briefing(people()[0],project)
    assert project.status=="ACTIVE"

def test_manual_evidence_and_decision_ui_use_brain_validation(ctx,client):
    project,_=ready_task()
    client.post(f"/projects/{project.id}/knowledge",data={"kind":"EVIDENCE","title":"E","content":"x"})
    client.post(f"/projects/{project.id}/knowledge",data={"kind":"DECISION","title":"D","content":"x"})
    assert KnowledgeItem.query.count()==0
    client.post(f"/projects/{project.id}/knowledge",data={"kind":"EVIDENCE","title":"E","content":"x","source_ref":"source:1"})
    assert KnowledgeItem.query.one().source_ref=="source:1"
def _knowledge_proposal(payload):
    ceo,_,_=people(); run=execute(ceo,"TEST","proposal")
    p=Proposal(agent_run_id=run.id,proposed_by_employee_id=ceo.id,payload_json=payload)
    db.session.add(p); db.session.commit(); return p
def test_founder_approves_normal_knowledge_proposal(ctx):
    p=_knowledge_proposal({"kind":"FACT","title":"F","content":"true"}); review_proposal(p,"APPROVED")
    assert p.materialized_knowledge_id and KnowledgeItem.query.count()==1
def test_founder_rejects_normal_knowledge_proposal(ctx):
    p=_knowledge_proposal({"kind":"FACT","title":"F","content":"maybe"}); review_proposal(p,"REJECTED")
    assert KnowledgeItem.query.count()==0 and p.status=="REJECTED"
def test_founder_corrects_normal_knowledge_proposal(ctx):
    p=_knowledge_proposal({"kind":"FACT","title":"Wrong","content":"wrong"})
    review_proposal(p,"CORRECTED",corrected={"kind":"FACT","title":"Right","content":"correct"})
    assert db.session.get(KnowledgeItem,p.materialized_knowledge_id).title=="Right"

def test_agent_run_page_uses_historical_snapshot(ctx,client):
    _,employee,_=people(); run=execute(employee,"TEST","x"); old=run.model_name_snapshot
    employee.current_model.model_name="live-changed"; db.session.commit(); page=client.get(f"/runs/{run.id}").data
    assert old.encode() in page and b"Historical Execution Identity" in page
def test_unicode_estimation_uses_utf8_upper_bound(ctx):
    model=people()[0].current_model; model.input_price_per_million=1
    english=conservative_estimate(model,"abcd",0); chinese=conservative_estimate(model,"中文中文",0); mixed=conservative_estimate(model,"a中🙂",0)
    assert chinese>=english and mixed>english
def test_paid_currency_mismatch_rejected_before_provider(ctx,monkeypatch):
    _,employee,_=people(); employee.current_model.input_price_per_million=1; employee.current_model.currency="USD"; db.session.commit(); called=[]
    monkeypatch.setattr("eason_one.services.execution.get_provider",lambda key:called.append(True))
    with pytest.raises(ValueError,match="currency"): execute(employee,"TEST","x")
    assert called==[]
def test_model_change_preserves_identity_history_and_run_snapshot(ctx):
    _,employee,_=people(); eid=employee.id; run=execute(employee,"TEST","before"); old=run.model_name_snapshot
    new=ModelConfig(label="Mock Two",provider_key="mock",model_name="mock-v2",input_price_per_million=0,output_price_per_million=0,currency="TWD")
    db.session.add(new); db.session.commit(); change_model(employee,new,"Founder test")
    assert employee.id==eid and EmployeeModelHistory.query.filter_by(employee_id=eid).count()==2 and run.model_name_snapshot==old
def test_fault_after_first_task_rolls_back_everything(ctx):
    ceo,_,_=people(); _,proposal=founder_request(ceo,"Create a rollback project")
    second=dict(proposal.payload_json["plan"]["tasks"][0]); proposal.payload_json["plan"]["tasks"].append(second)
    flag_modified(proposal,"payload_json"); db.session.commit()
    with pytest.raises(RuntimeError): materialize_project_plan(proposal,fault_after_task=0)
    assert Project.query.count()==0 and Task.query.count()==0 and db.session.get(Proposal,proposal.id).status=="PENDING"

``

---

## FILE: tests\test_slice004.py

``python
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
    assert run.parsed_output_json["executive_response"].encode() in client.get("/ceo").data and Proposal.query.count()==0

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
    current_section=page.split(b"Historical Knowledge")[0]
    assert b"KILLED_MARKER" in current_section and b"OLD_TARGET" not in current_section

``
