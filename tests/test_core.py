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
    assert e.id==eid and Employee.query.count()==7 and e.current_model.model_name=="v2"
    assert Employee.query.filter_by(slug="hr-director").one().position.name=="HR Director"
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
