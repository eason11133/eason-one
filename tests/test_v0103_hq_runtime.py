from eason_one.models import AgentRun, Operation, WorkMessage
from eason_one.services.ceo import is_company_status_query
from eason_one.services.headquarters import route_ceo_intent

MIXED_PLAN_REQUEST = (
    "Review the current Eason One project status and prepare a concrete three-step "
    "plan for the next development task. Do not modify any files yet. Define the "
    "objective, acceptance criteria, risks, and which employees should participate."
)

BOUNDED_PLAN_REQUEST = (
    "Prepare a three-step plan to improve CEO Direct Line reliability. Do not modify "
    "any files. Include the objective, acceptance criteria, risks, and proposed employees."
)


def test_mixed_status_and_plan_is_never_collapsed_to_local_brief(ctx):
    assert is_company_status_query(MIXED_PLAN_REQUEST) is False
    routed = route_ceo_intent(MIXED_PLAN_REQUEST)
    assert routed["route"] == "ACT"


def test_pure_company_status_stays_deterministic(ctx):
    request = "Give me the current company status."
    assert is_company_status_query(request) is True
    assert route_ceo_intent(request)["route"] == "BRIEF"


def test_exact_founder_plan_request_creates_persisted_run_and_planned_authority(client, ctx):
    before = AgentRun.query.count()
    preview = client.post("/headquarters/ceo/preview", json={
        "request": MIXED_PLAN_REQUEST,
        "mode": "AUTO",
    })
    preview_payload = preview.get_json()
    assert preview.status_code == 200
    assert preview_payload["route"] == "ACT"
    assert preview_payload["execute_required"] is True

    response = client.post("/headquarters/ceo/execute", json={
        "request": MIXED_PLAN_REQUEST,
        "mode": preview_payload["route"],
    })
    payload = response.get_json()
    assert response.status_code == 200, payload
    assert payload["run_status"] == "SUCCEEDED"
    assert payload["run_id"] is not None
    assert AgentRun.query.count() == before + 1

    run = AgentRun.query.get(payload["run_id"])
    assert run is not None
    assert run.purpose == "CEO_FOUNDER_REQUEST"
    assert run.user_request == MIXED_PLAN_REQUEST
    assert run.provider_key_snapshot
    assert run.model_name_snapshot
    assert run.input_tokens is not None
    assert run.output_tokens is not None
    assert run.real_cost is not None
    assert run.finished_at is not None

    proposal = payload["answer"]["proposal"]
    assert payload["proposal_type"] == "OPERATION"
    assert proposal["status"] == "PLANNED"
    assert proposal["operation_id"]
    operation = Operation.query.get(proposal["operation_id"])
    assert operation.status == "PLANNED"
    assert "No new authority was auto-approved." in payload["answer"]["facts"]
    assert WorkMessage.query.filter_by(message_type="FOUNDER_TO_CEO").count() == 1
    assert WorkMessage.query.filter_by(message_type="CEO_TO_FOUNDER", agent_run_id=run.id).count() == 1


def test_second_reliability_prompt_also_enters_run_path(client, ctx):
    preview = client.post("/headquarters/ceo/preview", json={
        "request": BOUNDED_PLAN_REQUEST,
        "mode": "AUTO",
    }).get_json()
    assert preview["route"] == "ACT"
    assert preview["execute_required"] is True
    response = client.post("/headquarters/ceo/execute", json={
        "request": BOUNDED_PLAN_REQUEST,
        "mode": preview["route"],
    })
    payload = response.get_json()
    assert response.status_code == 200, payload
    assert payload["run_status"] == "SUCCEEDED"
    assert payload["answer"]["run_id"] == payload["run_id"]
    assert "Objective:" in payload["answer"]["summary"]
    assert "Risks:" in payload["answer"]["summary"]
    assert "Acceptance:" in payload["answer"]["summary"]


def test_hq_loads_native_v0106_assets_without_dom_rearrangement(client, ctx):
    page = client.get("/headquarters").get_data(as_text=True)
    assert page.count("headquarters-v0103.css") == 1
    assert page.count("headquarters-v0106.css") == 1
    assert "headquarters-v0103.js" not in page
    assert "headquarters-v0102.js" not in page
    assert "v0106-native-root" in page


def test_v0106_layout_uses_the_authoritative_runtime(client, ctx):
    canonical = client.get("/static/headquarters.js").get_data(as_text=True)
    assert "fetch('/headquarters/ceo/preview'" in canonical
    assert "fetch('/headquarters/ceo/execute'" in canonical
    assert "/runtime/start" in canonical
    assert "stopImmediatePropagation" not in canonical
