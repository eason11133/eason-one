from pathlib import Path

from eason_one import routes
from eason_one.extensions import db
from eason_one.models import AgentRun, Employee, WorkMessage


def _dialogue_rows():
    return (
        WorkMessage.query.filter(
            WorkMessage.message_type.in_({"FOUNDER_TO_CEO", "CEO_TO_FOUNDER"})
        )
        .order_by(WorkMessage.id)
        .all()
    )


def test_provider_exception_persists_matching_ceo_reply(client, ctx, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("provider unavailable for persistence test")

    monkeypatch.setattr(routes, "founder_request", fail)
    response = client.post(
        "/headquarters/ceo/execute",
        json={"request": "Do one bounded task", "mode": "ACT"},
    )

    assert response.status_code == 500
    rows = _dialogue_rows()
    assert [row.message_type for row in rows[-2:]] == [
        "FOUNDER_TO_CEO",
        "CEO_TO_FOUNDER",
    ]
    assert rows[-2].content == "Do one bounded task"
    assert rows[-1].content == "provider unavailable for persistence test"

    html = client.get("/headquarters").get_data(as_text=True)
    assert "Do one bounded task" in html
    assert "provider unavailable for persistence test" in html


def test_failed_agent_run_reply_is_persisted_with_run_link(client, ctx, monkeypatch):
    ceo = Employee.query.filter_by(slug="ceo").one()
    model = ceo.current_model
    run = AgentRun(
        employee_id=ceo.id,
        model_config_id=ceo.current_model_config_id,
        purpose="CEO_ADVISE",
        user_request="Inspect failure",
        system_prompt_snapshot="test",
        context_snapshot="test",
        status="FAILED",
        error_text="canonical failed run reply",
        provider_key_snapshot=model.provider_key,
        model_name_snapshot=model.model_name,
        input_price_snapshot=model.input_price_per_million,
        output_price_snapshot=model.output_price_per_million,
        currency_snapshot=model.currency,
    )
    db.session.add(run)
    db.session.commit()
    monkeypatch.setattr(routes, "founder_request", lambda *args, **kwargs: (run, None))

    response = client.post(
        "/headquarters/ceo/execute",
        json={"request": "Inspect failure", "mode": "ADVISE"},
    )

    assert response.status_code == 502
    rows = _dialogue_rows()
    assert rows[-1].message_type == "CEO_TO_FOUNDER"
    assert rows[-1].content == "canonical failed run reply"
    assert rows[-1].agent_run_id == run.id


def test_client_does_not_render_duplicate_transient_failure_wrapper():
    source = Path("eason_one/static/headquarters.js").read_text(encoding="utf-8")
    assert "executionError.answerRendered = answerRendered" in source
    assert "if (!error.answerRendered)" in source
