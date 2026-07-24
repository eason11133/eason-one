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
