import json
from decimal import Decimal
from pathlib import Path

from eason_one import _upgrade_v1_database
from eason_one.extensions import db
from eason_one.models import (
    AgentRun, CostEvent, Employee, HiringRequest, Operation,
)
from eason_one.providers import ProviderResult
from eason_one.schemas import CEO_SCHEMA
from eason_one.services.ceo import founder_request
from eason_one.services.company import get_company


def _compact_plan():
    ceo=Employee.query.filter_by(slug="ceo").one()
    worker=Employee.query.filter_by(slug="researcher").one()
    reviewer=Employee.query.filter_by(slug="research-director").one()
    deliverables=[
      "Interview design","Recruiting brief","Evidence rubric",
      "Pricing probe","Objection map","Decision memo"]
    return {"mode":"OPERATION_PLAN",
      "executive_response":"Run six bounded commercial validation work packets.",
      "project":None,"project_id":None,"tasks":[],
      "operation":{"title":"Commercial validation",
        "objective":"Validate demand without development or hiring.",
        "project_id":None,"budget_twd":5,
        "tasks":[{"title":title,"objective":f"Produce {title.lower()}.",
          "assignee_employee_id":worker.id,
          "reviewer_employee_id":reviewer.id,
          "acceptance_criteria":["Output is decision-ready"]}
          for title in deliverables],
        "meeting_policy":"Meet only on material conflict.",
        "completion_criteria":["All six outputs pass review",
          "Demand decision is supported"]}}


def test_compact_founder_plan_contract_stays_bounded_and_single_call(
    ctx, monkeypatch
):
    ceo=Employee.query.filter_by(slug="ceo").one()
    ceo.current_model.max_output_tokens=4096
    db.session.commit()
    payload=_compact_plan()
    seen=[]
    class Provider:
        def complete(self,model,system,user,context,maximum,schema=None):
            seen.append((maximum,system))
            return ProviderResult(json.dumps(payload),100,300)
    monkeypatch.setattr(
      "eason_one.services.execution.get_provider",lambda _:Provider())
    request=("Validate commercial demand with interview design, recruiting "
      "brief, evidence rubric, pricing probe, objection map, and decision "
      "memo. Do not develop or hire; choose employees and reviewers.")
    run,operation=founder_request(ceo,request)
    assert run.status=="SUCCEEDED"
    assert seen[0][0]==2048 and len(seen)==1
    assert "minimum executable plan" in seen[0][1]
    assert len(json.dumps(payload,separators=(",",":")))<5000
    assert 1<=len(payload["operation"]["tasks"])<=6
    assert operation.status=="PLANNED" and not operation.tasks
    assert not operation.approved_at


def test_compact_founder_schema_limits_are_frozen():
    schema=CEO_SCHEMA["schema"]
    operation=schema["properties"]["operation"]["anyOf"][0]["properties"]
    task=operation["tasks"]["items"]["properties"]
    assert schema["properties"]["executive_response"]["maxLength"]==220
    assert operation["title"]["maxLength"]==80
    assert operation["objective"]["maxLength"]==180
    assert operation["tasks"]["maxItems"]==6
    assert task["title"]["maxLength"]==70
    assert task["objective"]["maxLength"]==140
    assert task["acceptance_criteria"]["maxItems"]==2
    assert task["acceptance_criteria"]["items"]["maxLength"]==120
    assert operation["meeting_policy"]["maxLength"]==80
    assert operation["completion_criteria"]["maxItems"]==6
    assert operation["completion_criteria"]["items"]["maxLength"]==120


def _legacy_request(recommendation):
    return HiringRequest(
      requested_by_type="FOUNDER",role_needed="Legacy role",problem="Gap",
      why_now="Historical",responsibilities_json=["Work"],
      capabilities_json=["Capability"],urgency="LOW",
      use_frequency="OCCASIONAL",status="FOUNDER_REVIEW",
      hr_assessment_json={"recommendation":recommendation,
        "expected_benefit":"Historical assessment"})


def test_legacy_non_hire_founder_reviews_reconcile_without_history_loss(ctx):
    requests=[_legacy_request(value) for value in (
      "USE_EXISTING_STAFF","DO_NOT_HIRE","TEMPORARY","HIRE")]
    db.session.add_all(requests); db.session.commit()
    before_runs=AgentRun.query.count()
    before_costs=CostEvent.query.count()
    _upgrade_v1_database()
    assert [item.status for item in requests]==[
      "ASSESSMENT_COMPLETE","ASSESSMENT_COMPLETE",
      "ASSESSMENT_COMPLETE","FOUNDER_REVIEW"]
    assert AgentRun.query.count()==before_runs
    assert CostEvent.query.count()==before_costs
    assert all(item.hr_assessment_json for item in requests)


def test_founder_currency_aggregates_exactly_then_rounds_half_up(client,ctx):
    company=get_company()
    db.session.add_all([
      CostEvent(company_id=company.id,category="MODEL_USAGE",
        description="fraction one",real_cost_delta=Decimal("0.004"),
        internal_credits_delta=0),
      CostEvent(company_id=company.id,category="MODEL_USAGE",
        description="fraction two",real_cost_delta=Decimal("0.004"),
        internal_credits_delta=0)])
    db.session.commit()
    sidebar=client.get("/command").get_data(as_text=True)
    costs=client.get("/costs").get_data(as_text=True)
    assert "TOTAL AI SPEND" in sidebar
    assert "NT$ 0.01" in sidebar
    assert "Today's real spend" not in sidebar
    assert costs.count("NT$ 0.00")>=2
    assert "NT$ 0.01" in costs
    assert "0.008000" not in costs


def test_windows_launcher_is_repo_local_and_explicit():
    setup=Path("scripts/setup-dev.ps1").read_text(encoding="utf-8")
    run=Path("scripts/run-dev.ps1").read_text(encoding="utf-8")
    ignore=Path(".gitignore").read_text(encoding="utf-8")
    assert "Python 3.13 is required" in setup
    assert 'pip install -e "${repoRoot}[test]"' in setup
    assert '.venv\\Scripts\\python.exe' in setup
    assert '.venv\\Scripts\\python.exe' in run
    assert "import openai, anthropic" in run
    assert "-m flask --app run.py run" in run
    assert ".venv/" in ignore
