from decimal import Decimal

import pytest

from eason_one.extensions import db
from eason_one.models import (
    AgentRun, Employee, KnowledgeItem, Operation, Project,
)
from eason_one.services import operations
from eason_one.services.current_company import (
    brain_projection, failure_groups, local_briefing, projection,
)
from eason_one.services.execution import execute


def _operation():
    ceo=Employee.query.filter_by(slug="ceo").one()
    worker=Employee.query.filter_by(slug="researcher").one()
    reviewer=Employee.query.filter_by(slug="research-director").one()
    item=operations.propose_operation(ceo,{
      "mode":"OPERATION_PLAN","executive_response":"Bounded work.",
      "operation":{"title":"Coherence operation","objective":"Verify coherence.",
        "project_id":None,"budget_twd":"1",
        "tasks":[{"title":"Current task","objective":"Produce evidence.",
          "assignee_employee_id":worker.id,
          "reviewer_employee_id":reviewer.id,
          "acceptance_criteria":["Reviewable"]}],
        "meeting_policy":"NEVER","completion_criteria":["Evidence exists"]}})
    operations.approve(item)
    return item


def test_projection_classifies_operation_once_and_routes_agree(client,ctx):
    operation=_operation()
    operations.wait_for_founder(
      operation,"Budget decision.","0.25","BUDGET_AUTHORIZATION")
    view=projection()
    row=next(row for row in view["operations"]
      if row["operation"].id==operation.id)
    assert row["classification"]=="WAITING_FOR_FOUNDER"
    missions=client.get("/headquarters/missions").get_data(as_text=True)
    attention=client.get("/headquarters/attention").get_data(as_text=True)
    mission=client.get(f"/headquarters/missions/{operation.id}").get_data(as_text=True)
    assert "Coherence operation" in missions
    assert "Coherence operation" in attention and "Coherence operation" in mission
    for value in ("1.00","0.00","0.25","1.25"):
        assert value in mission


def test_brain_scope_and_live_legacy_projection(ctx):
    live=_operation().project
    ceo=Employee.query.filter_by(slug="ceo").one()
    smoke=Project(name="Legacy smoke",objective="Test",status="ACTIVE",
      environment="SMOKE",origin="TEST",priority="LOW",
      owner_employee_id=ceo.id)
    db.session.add(smoke)
    db.session.add(KnowledgeItem(
      project_id=live.id,kind="FACT",title="Scoped fact",content="Fact",
      founder_approved=True))
    db.session.commit()
    brain=brain_projection()
    assert not brain["by_scope"]["company"]
    assert any(item.title=="Scoped fact" for item in brain["by_scope"]["project"])
    view=projection()
    assert live in view["live_projects"] and smoke in view["legacy_projects"]
    assert smoke not in view["live_projects"]


def test_provider_preflight_blocks_before_run(app,ctx,monkeypatch):
    ceo=Employee.query.filter_by(slug="ceo").one()
    ceo.current_model.provider_key="openai"
    monkeypatch.delenv("OPENAI_API_KEY",raising=False)
    app.config["ENFORCE_PROVIDER_PREFLIGHT"]=True
    before=AgentRun.query.count()
    with pytest.raises(ValueError,match="provider is not configured"):
        execute(ceo,"STATUS_QUERY","status")
    assert AgentRun.query.count()==before


def test_local_briefing_and_failure_grouping_are_read_only(client,ctx):
    before=(Operation.query.count(),AgentRun.query.count(),
      KnowledgeItem.query.count())
    response=client.post("/command",data={
      "request":"Give me a factual briefing of the current company status."},
      follow_redirects=True)
    assert "CEO headquarters briefing" in response.get_data(as_text=True)
    assert (Operation.query.count(),AgentRun.query.count(),
      KnowledgeItem.query.count())==before
    assert "Persisted company status" in local_briefing()

    ceo=Employee.query.filter_by(slug="ceo").one()
    for _ in range(2):
        db.session.add(AgentRun(
          employee_id=ceo.id,model_config_id=ceo.current_model.id,
          purpose="CEO_FOUNDER_REQUEST",status="FAILED",
          user_request="status",system_prompt_snapshot="prompt",
          context_snapshot="context",failure_reason="OUTPUT_TRUNCATED",
          provider_key_snapshot="mock",model_name_snapshot="mock",
          input_price_snapshot=0,output_price_snapshot=0,
          currency_snapshot="TWD",currency="TWD",real_cost=Decimal("0.1")))
    db.session.commit()
    group=next(item for item in failure_groups()
      if item["failure_type"]=="OUTPUT_TRUNCATED")
    assert group["count"]==2 and group["cost"]==Decimal("0.2")
