from decimal import Decimal

from eason_one.extensions import db
from eason_one.models import Artifact, ArtifactVersion, Employee, Meeting, Project, VerificationRecord, WorkDependency
from eason_one.services import codex_connector, company_kernel, governance, meeting_coordination, multi_agent, work_runtime
from eason_one.services.operations import approve, propose_operation
from eason_one.services.project_company import project_snapshot


def _plan(researcher, critic, *, meeting_trigger="NEVER"):
    return {
        "mode": "OPERATION_PLAN",
        "executive_response": "Use two independent Employees, then synthesize accepted evidence.",
        "project": {
            "name": "Multi Employee Company",
            "objective": "Demonstrate a real governed multi-Employee Project.",
            "priority": "HIGH",
            "success_criteria": ["Independent specialist Work is accepted and handed off."],
            "constraints": ["Do not widen Founder authority."],
            "deadline": None,
        },
        "project_id": None,
        "operation": {
            "title": "Multi Employee Mission",
            "objective": "Research and challenge independently, then synthesize accepted artifacts.",
            "project_id": None,
            "budget_twd": 10,
            "tasks": [
                {
                    "title": "Research branch",
                    "objective": "Produce bounded evidence.",
                    "assignee_employee_id": researcher.id,
                    "reviewer_employee_id": None,
                    "required_capabilities": ["RESEARCH"],
                    "acceptance_criteria": ["A persisted result exists."],
                },
                {
                    "title": "Critical branch",
                    "objective": "Challenge assumptions independently.",
                    "assignee_employee_id": critic.id,
                    "reviewer_employee_id": None,
                    "required_capabilities": ["CRITICAL_REVIEW"],
                    "acceptance_criteria": ["A persisted critical result exists."],
                },
                {
                    "title": "Synthesis branch",
                    "objective": "Use only accepted predecessor evidence.",
                    "assignee_employee_id": researcher.id,
                    "reviewer_employee_id": None,
                    "required_capabilities": ["RESEARCH"],
                    "acceptance_criteria": ["Accepted predecessor evidence is reconciled."],
                },
            ],
            "meeting_policy": meeting_trigger,
            "meeting_config": {
                "trigger": meeting_trigger,
                "participant_employee_ids": ([researcher.id, critic.id] if meeting_trigger != "NEVER" else []),
                "max_rounds": 1,
                "max_speakers_per_round": 1,
                "contribution_output_cap": 192,
                "token_limit": 6000,
                "budget_twd": (2 if meeting_trigger != "NEVER" else 0),
                "retry_limit": 0,
            },
            "completion_criteria": ["The governed company outcome is reviewable."],
        },
    }


def _setup(*, meeting_trigger="NEVER"):
    ceo = Employee.query.filter_by(slug="ceo").one()
    researcher = Employee.query.filter_by(slug="researcher").one()
    critic = Employee.query.filter_by(slug="critic").one()
    operation = propose_operation(ceo, _plan(researcher, critic, meeting_trigger=meeting_trigger), route_type="FULL_PROJECT")
    approve(operation)
    db.session.refresh(operation)
    company_kernel.adopt_operation(operation)
    tasks = list(operation.tasks)
    multi_agent.persist_plan(operation, {
        "strategy": "PARALLEL_DAG",
        "rationale": "Research and critique are independent; synthesis consumes both accepted results.",
        "max_parallelism": 2,
        "tasks": [
            {"task_id": tasks[0].id, "depends_on_task_ids": [], "role": "WORKER", "reason": "Independent evidence."},
            {"task_id": tasks[1].id, "depends_on_task_ids": [], "role": "VERIFIER", "reason": "Independent challenge."},
            {"task_id": tasks[2].id, "depends_on_task_ids": [tasks[0].id, tasks[1].id], "role": "SYNTHESIS", "reason": "Consumes both."},
        ],
    })
    db.session.expire_all()
    operation = db.session.get(type(operation), operation.id)
    return operation, operation.project, researcher, critic


def test_conservative_topology_fallback_keeps_independent_company_branches_parallel(ctx):
    operation, project, researcher, critic = _setup()
    tasks = sorted(operation.tasks, key=lambda row: row.id)

    # Re-plan only to exercise degraded topology. The approved Work itself says
    # the first two branches are independent and the third consumes accepted
    # predecessor evidence. A planner outage must not turn Task order into a
    # fake company-wide serial queue.
    plan = multi_agent.conservative_fallback(operation, "planner unavailable")
    rows = {row["task_id"]: row for row in plan["tasks"]}

    assert plan["strategy"] == "PARALLEL_DAG"
    assert plan["max_parallelism"] >= 2
    assert rows[tasks[0].id]["depends_on_task_ids"] == []
    assert rows[tasks[1].id]["depends_on_task_ids"] == []
    assert rows[tasks[2].id]["depends_on_task_ids"] == [tasks[0].id, tasks[1].id]
    assert rows[tasks[2].id]["role"] == "SYNTHESIS"
    assert plan["fallback"] is True


def test_v020_company_kernel_selects_independent_persistent_employees_together(ctx):
    operation, project, researcher, critic = _setup()
    works = [work for work in operation.works if work.work_type != "MANAGEMENT"]
    selected = company_kernel._eligible_works(max_parallelism=4)
    assert {work.id for work in selected} == {works[0].id, works[1].id}
    assert {work_runtime.active_assignment(work).employee_id for work in selected} == {researcher.id, critic.id}
    assert WorkDependency.query.filter_by(work_id=works[2].id).count() == 2


def test_parallel_wave_counts_frozen_reviewer_as_the_actual_verification_actor(ctx):
    operation, project, researcher, critic = _setup()
    works = [work for work in operation.works if work.work_type != "MANAGEMENT"]
    research, critical = works[0], works[1]

    # The Research Artifact is now awaiting semantic review by Critic. The
    # Critic also owns another READY delivery Work. Those are two actions by the
    # same Persistent Employee and therefore must not run in the same wave.
    work_runtime.transition(research, "EXECUTING", actor_type="EMPLOYEE", actor_id=researcher.id, reason="test execution")
    work_runtime.transition(research, "VERIFYING", actor_type="RUNTIME", reason="test semantic review")
    control = dict(research.runtime_control_json or {})
    control["acceptance_contract"] = {
        "contract_hash": "test-contract",
        "criteria": [{"id": "C1", "text": "Evidence is reviewable", "proof_owner": "INDEPENDENT_REVIEW"}],
        "reviewer_employee_id": critic.id,
    }
    research.runtime_control_json = control
    db.session.commit()

    selected = company_kernel._eligible_works(max_parallelism=4)
    selected_ids = {work.id for work in selected}
    assert research.id in selected_ids
    assert critical.id not in selected_ids
    assert company_kernel._dispatch_actor_id(research) == critic.id


def test_execution_scoped_founder_gate_does_not_freeze_unrelated_employee(ctx):
    operation, project, researcher, critic = _setup()
    works = [work for work in operation.works if work.work_type != "MANAGEMENT"]
    blocked, sibling = works[0], works[1]
    gate = governance.open_gate(
        project=project,
        escalation_type="EXTERNAL_EFFECT_AUTHORIZATION",
        reason="This exact Work wants one bounded external action.",
        work=blocked,
        operation=operation,
        created_by_employee_id=researcher.id,
        authority_payload={"requested_action": {"kind": "EXAMPLE_EXTERNAL_ACTION", "target": "bounded"}},
    )
    work_runtime.open_wait(blocked, "FOUNDER_DECISION", gate.reason)
    db.session.commit()

    assert project.status == "ACTIVE"
    selected = company_kernel._eligible_works(max_parallelism=4)
    assert blocked.id not in {work.id for work in selected}
    assert sibling.id in {work.id for work in selected}
    assert governance.blocks_work(blocked) is True
    assert governance.blocks_work(sibling) is False


def test_project_surface_projects_work_topology_not_task_truth(ctx):
    operation, project, researcher, critic = _setup()
    snapshot = project_snapshot(project)
    graph = snapshot["orchestration"]
    assert graph["strategy"] == "PARALLEL_DAG"
    assert graph["max_parallelism"] == 2
    assert all(row.get("work") is not None for row in graph["tasks"])
    assert {row["employee"].slug for row in graph["tasks"][:2]} == {"researcher", "critic"}


def test_exact_founder_wait_projection_does_not_clear_second_question(ctx):
    operation, project, researcher, critic = _setup()
    work = next(row for row in operation.works if row.work_type != "MANAGEMENT")
    work_runtime.open_wait(
        work, "FOUNDER_DECISION", "First exact question", gate_key="escalation:101"
    )
    work_runtime.open_wait(
        work, "FOUNDER_DECISION", "Second exact question", gate_key="escalation:102"
    )
    assert len(work_runtime.open_gates(work, "FOUNDER_DECISION")) == 2
    assert work_runtime.resolve_waits(
        work, "FOUNDER_DECISION", gate_key="escalation:101", note="Resolved first"
    ) == 1
    remaining = work_runtime.open_gates(work, "FOUNDER_DECISION")
    assert len(remaining) == 1
    assert remaining[0]["gate_key"] == "escalation:102"
    assert work.state == "WAITING"


def test_material_conflict_can_start_real_meeting_before_all_delivery_closes(ctx):
    operation, project, researcher, critic = _setup(meeting_trigger="ON_MATERIAL_CONFLICT")
    meeting = Meeting.query.filter_by(operation_id=operation.id).one()
    management = next(work for work in operation.works if work.work_type == "MANAGEMENT")
    assert meeting.related_work_id == management.id

    memory = dict(operation.memory_json or {})
    memory["decisions"] = [{"action": "MEETING", "reason": "Specialists disagree on a material decision."}]
    operation.memory_json = memory
    db.session.commit()

    result = meeting_coordination.advance_meeting_gate(operation)
    assert result["status"] == "MEETING_STARTED"
    assert meeting.status == "RUNNING"
    assert any(work.state not in {"ACCEPTED", "CANCELLED"} for work in operation.works if work.work_type != "MANAGEMENT")


def test_completed_mid_mission_meeting_becomes_downstream_company_context(ctx):
    operation, project, researcher, critic = _setup(meeting_trigger="ON_MATERIAL_CONFLICT")
    meeting = Meeting.query.filter_by(operation_id=operation.id).one()
    meeting.status = "ENDED"
    meeting.kernel_status = "COMPLETED"
    meeting.minutes_json = {
        "purpose": "Resolve a specialist conflict.",
        "agreements": ["Use the accepted evidence and narrow the next attempt."],
        "disagreements": [],
        "actions": ["Retry only the affected Work with the agreed constraint."],
        "founder_decisions_required": [],
    }
    db.session.commit()

    result = meeting_coordination.advance_meeting_gate(operation)
    assert result["status"] == "MEETING_RESULT_COMMITTED"
    db.session.refresh(operation)
    rows = (operation.memory_json or {}).get("meeting_results") or []
    assert len(rows) == 1
    assert rows[0]["meeting_id"] == meeting.id
    assert "Retry only the affected Work" in rows[0]["result"]["actions"][0]
    assert meeting_coordination.advance_meeting_gate(operation) is None


def test_delivery_reconciliation_is_branch_local_not_project_mutex(ctx):
    operation, project, researcher, critic = _setup()
    works = [work for work in operation.works if work.work_type != "MANAGEMENT"]
    blocked, sibling = works[0], works[1]
    work_runtime.open_wait(
        blocked, "RECONCILIATION",
        "This Employee must reconcile one ambiguous provider effect before replay.",
        issue_code="TEST_BRANCH_LOCAL_RECONCILIATION",
    )
    db.session.commit()

    # A delivery-local integrity incident blocks only that Work. Project-control
    # reconciliation remains globally hard and is covered by the governance
    # floor tests.
    assert work_runtime.project_hard_blockers(project, include_founder=False) == []
    selected = company_kernel._eligible_works(max_parallelism=4)
    assert blocked.id not in {work.id for work in selected}
    assert sibling.id in {work.id for work in selected}


def test_company_batch_keeps_servicing_other_work_after_branch_attention(ctx, monkeypatch):
    results = iter([
        {"kind": "WORK", "status": "NEEDS_FOUNDER", "work_id": 101},
        {"kind": "WORK", "status": "ACCEPTED", "work_id": 202},
        {"kind": "IDLE"},
    ])
    monkeypatch.setattr(company_kernel, "advance_once", lambda: next(results))
    batch = company_kernel.advance_batch(max_steps=8)
    assert [row.get("status") for row in batch["steps"][:2]] == ["NEEDS_FOUNDER", "ACCEPTED"]
    assert batch["steps"][-1]["kind"] == "IDLE"
    assert batch["productive_steps"] == 2


def test_company_kernel_owns_current_project_hiring_requests(ctx, monkeypatch):
    from eason_one.models import HiringRequest
    from eason_one.services import workforce

    operation, project, researcher, critic = _setup()
    request = workforce.request_hire(
        requested_by_type="EMPLOYEE",
        requester=project.owner,
        operation=operation,
        project=project,
        role_needed="Product Specialist",
        problem="The Project requires a persistent capability not currently assigned.",
        why_now="A bounded downstream Work depends on this capability.",
        responsibilities=["Own the specialist Work"],
        capabilities=["product analysis"],
        urgency="HIGH",
        use_frequency="PERSISTENT",
    )
    db.session.commit()
    due = company_kernel._hiring_request_due()
    assert due is not None and due.id == request.id

    # This test is about current-kernel ownership, not provider behavior.
    class _Run:
        id = 909
    def fake_assess(item):
        item.status = "ASSESSMENT_COMPLETE"
        db.session.commit()
        return _Run()
    monkeypatch.setattr(workforce, "assess_request", fake_assess)
    result = company_kernel._advance_hiring(request)
    assert result["status"] == "HIRING_ASSESSED"
    assert db.session.get(HiringRequest, request.id).status == "ASSESSMENT_COMPLETE"


def test_founder_result_opens_exact_accepted_deliverable_not_run_audit(ctx, tmp_path):
    operation, project, researcher, critic = _setup()
    work = next(row for row in operation.works if row.work_type != "MANAGEMENT")
    repo = tmp_path / "repo"
    target = repo / "trial_output" / "brief.html"
    target.parent.mkdir(parents=True)
    target.write_text("<html><body>Founder deliverable</body></html>", encoding="utf-8")
    control = dict(work.runtime_control_json or {})
    scope = codex_connector.freeze_write_scope(
        {"version": "CODEX_WRITE_SCOPE_V1", "paths": ["trial_output/brief.html"]},
        source="TEST_APPROVED_PLAN", authority_ref="test:deliverable",
    )
    control["codex_write_scope"] = scope
    control["deliverable_targets"] = {
        "version": "DELIVERABLE_TARGETS_V1",
        "kind": "REPOSITORY_PATHS",
        "paths": ["trial_output/brief.html"],
        "source": "TEST_APPROVED_PLAN",
    }
    boundary = {
        "version": "CODEX_EXECUTION_BOUNDARY_V2", "project_id": project.id,
        "work_id": work.id, "repo_path": str(repo),
        "allowed_paths": ["trial_output/brief.html"], "forbidden_paths": [],
        "max_changed_files": 1, "read_only": False,
        "project_execution_terms_hash": "test",
    }
    import hashlib, json
    boundary["boundary_hash"] = hashlib.sha256(
        json.dumps(boundary, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    control["codex_execution_boundary"] = boundary
    work.runtime_control_json = control
    artifact = Artifact(
        project_id=project.id, work_id=work.id, artifact_type="CODE_CHANGE", title="Decision brief"
    )
    db.session.add(artifact); db.session.flush()
    version = ArtifactVersion(
        artifact_id=artifact.id, version=1, producer_employee_id=researcher.id,
        status="ACCEPTED", content_text="brief ready", content_hash="a" * 64,
    )
    db.session.add(version); db.session.flush()
    import hashlib
    db.session.add(VerificationRecord(
        work_id=work.id, artifact_version_id=version.id, method="HOST_ENGINEERING_VALIDATION",
        status="PASSED", details_json={"host_observations": {
            "changed_file_hashes": {"trial_output/brief.html": hashlib.sha256(target.read_bytes()).hexdigest()}
        }},
    ))
    db.session.commit()

    result = next(row for row in project_snapshot(project)["results"] if row.get("artifact_version_id") == version.id)
    assert result["deliverables"] == [{
        "artifact_version_id": version.id,
        "path": "trial_output/brief.html",
        "name": "brief.html",
        "available": True,
        "integrity": "VERIFIED",
        "href": f"/headquarters/artifacts/{version.id}/deliverables/0",
    }]
    assert result["href"] == result["deliverables"][0]["href"]


def test_founder_html_deliverable_is_sandboxed_and_unaccepted_artifact_is_not_served(app, client, tmp_path):
    with app.app_context():
        operation, project, researcher, critic = _setup()
        work = next(row for row in operation.works if row.work_type != "MANAGEMENT")
        repo = tmp_path / "repo"
        target = repo / "output" / "result.html"
        target.parent.mkdir(parents=True)
        target.write_text("<script>window.top.location='https://example.com'</script><h1>Result</h1>", encoding="utf-8")
        control = dict(work.runtime_control_json or {})
        scope = codex_connector.freeze_write_scope(
            {"version": "CODEX_WRITE_SCOPE_V1", "paths": ["output/result.html"]},
            source="TEST_APPROVED_PLAN", authority_ref="test:deliverable",
        )
        control["codex_write_scope"] = scope
        control["deliverable_targets"] = {
            "version": "DELIVERABLE_TARGETS_V1", "kind": "REPOSITORY_PATHS",
            "paths": ["output/result.html"], "source": "TEST_APPROVED_PLAN",
        }
        boundary = {
            "version": "CODEX_EXECUTION_BOUNDARY_V2", "project_id": project.id,
            "work_id": work.id, "repo_path": str(repo),
            "allowed_paths": ["output/result.html"], "forbidden_paths": [],
            "max_changed_files": 1, "read_only": False,
            "project_execution_terms_hash": "test",
        }
        import hashlib, json
        boundary["boundary_hash"] = hashlib.sha256(
            json.dumps(boundary, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        control["codex_execution_boundary"] = boundary
        work.runtime_control_json = control
        artifact = Artifact(project_id=project.id, work_id=work.id, artifact_type="CODE_CHANGE", title="Result")
        db.session.add(artifact); db.session.flush()
        accepted = ArtifactVersion(
            artifact_id=artifact.id, version=1, producer_employee_id=researcher.id,
            status="ACCEPTED", content_hash="b" * 64,
        )
        submitted = ArtifactVersion(
            artifact_id=artifact.id, version=2, producer_employee_id=researcher.id,
            status="SUBMITTED", content_hash="c" * 64,
        )
        db.session.add_all([accepted, submitted]); db.session.flush()
        import hashlib
        db.session.add(VerificationRecord(
            work_id=work.id, artifact_version_id=accepted.id, method="HOST_ENGINEERING_VALIDATION",
            status="PASSED", details_json={"host_observations": {
                "changed_file_hashes": {"output/result.html": hashlib.sha256(target.read_bytes()).hexdigest()}
            }},
        ))
        db.session.commit()
        accepted_id, submitted_id = accepted.id, submitted.id

    response = client.get(f"/headquarters/artifacts/{accepted_id}/deliverables/0")
    assert response.status_code == 200
    assert b"<h1>Result</h1>" in response.data
    csp = response.headers.get("Content-Security-Policy", "")
    assert "sandbox" in csp
    assert "script-src 'none'" in csp
    target.write_text("<h1>Changed after acceptance</h1>", encoding="utf-8")
    assert client.get(f"/headquarters/artifacts/{accepted_id}/deliverables/0").status_code == 404
    assert client.get(f"/headquarters/artifacts/{submitted_id}/deliverables/0").status_code == 404
    assert client.get(f"/headquarters/artifacts/{accepted_id}/deliverables/1").status_code == 404


def test_accepted_db_native_artifact_opens_readable_result_before_run_audit(ctx):
    from eason_one.services import artifacts as artifact_service

    operation, project, researcher, critic = _setup()
    work = next(row for row in operation.works if row.work_type != "MANAGEMENT")
    artifact = Artifact(
        project_id=project.id,
        work_id=work.id,
        artifact_type="RESEARCH_REPORT",
        title="Market research brief",
    )
    db.session.add(artifact)
    db.session.flush()
    content = '{"summary":"Three viable markets were found.","recommendation":"Validate the smallest wedge first.","evidence":["Source A","Source B"]}'
    version = ArtifactVersion(
        artifact_id=artifact.id,
        version=1,
        producer_employee_id=researcher.id,
        status="ACCEPTED",
        content_text=content,
        content_hash=artifact_service._hash(content, None),
    )
    db.session.add(version)
    db.session.commit()

    result = next(row for row in project_snapshot(project)["results"] if row.get("artifact_version_id") == version.id)
    assert result["readable"] is True
    assert result["readable_href"] == f"/headquarters/artifacts/{version.id}"
    assert result["href"] == result["readable_href"]


def test_results_index_groups_supporting_artifacts_under_one_project_outcome(ctx):
    from eason_one.services import artifacts as artifact_service
    from eason_one.services.project_company import results_snapshot

    operation, project, researcher, critic = _setup()
    work = next(row for row in operation.works if row.work_type != "MANAGEMENT")
    for index in range(2):
        artifact = Artifact(
            project_id=project.id, work_id=work.id,
            artifact_type="RESEARCH_REPORT", title=f"Supporting brief {index + 1}",
        )
        db.session.add(artifact); db.session.flush()
        content = f'{{"summary":"Evidence brief {index + 1}"}}'
        db.session.add(ArtifactVersion(
            artifact_id=artifact.id, version=1, producer_employee_id=researcher.id,
            status="ACCEPTED", content_text=content,
            content_hash=artifact_service._hash(content, None),
        ))
    db.session.commit()

    project_rows = [row for row in results_snapshot()["results"] if row["project"].id == project.id]
    assert len(project_rows) == 1
    assert project_rows[0]["supporting_results"] == 1
    assert project_rows[0]["team_names"] == [researcher.name]


def test_readable_artifact_route_serves_only_accepted_hash_verified_live_content(app, client):
    from eason_one.services import artifacts as artifact_service

    with app.app_context():
        operation, project, researcher, critic = _setup()
        work = next(row for row in operation.works if row.work_type != "MANAGEMENT")
        artifact = Artifact(
            project_id=project.id,
            work_id=work.id,
            artifact_type="WORK_PRODUCT",
            title="Strategy memo",
        )
        db.session.add(artifact)
        db.session.flush()
        content = '{"recommendation":"Launch a seven-day validation.","steps":["Interview users","Publish offer"]}'
        accepted = ArtifactVersion(
            artifact_id=artifact.id,
            version=1,
            producer_employee_id=researcher.id,
            status="ACCEPTED",
            content_text=content,
            content_hash=artifact_service._hash(content, None),
        )
        submitted_content = "Not accepted yet"
        submitted = ArtifactVersion(
            artifact_id=artifact.id,
            version=2,
            producer_employee_id=researcher.id,
            status="SUBMITTED",
            content_text=submitted_content,
            content_hash=artifact_service._hash(submitted_content, None),
        )
        db.session.add_all([accepted, submitted])
        db.session.commit()
        accepted_id, submitted_id = accepted.id, submitted.id

    response = client.get(f"/headquarters/artifacts/{accepted_id}")
    assert response.status_code == 200
    assert b"Strategy memo" in response.data
    assert b"Launch a seven-day validation." in response.data
    assert b"WHAT WAS DELIVERED" in response.data
    assert b"WHY EASON ONE ACCEPTED IT" in response.data
    assert b"TECHNICAL AUDIT" in response.data
    assert client.get(f"/headquarters/artifacts/{submitted_id}").status_code == 404

    with app.app_context():
        tampered = db.session.get(ArtifactVersion, accepted_id)
        tampered.content_text = "tampered after acceptance"
        db.session.commit()
    tampered_response = client.get(f"/headquarters/artifacts/{accepted_id}")
    assert tampered_response.status_code == 404
    assert b"tampered after acceptance" not in tampered_response.data


def test_readable_research_artifact_shows_only_persisted_http_provider_sources(ctx):
    from eason_one.models import AgentRun
    from eason_one.services import artifacts as artifact_service

    operation, project, researcher, critic = _setup()
    work = next(row for row in operation.works if row.work_type != "MANAGEMENT")
    model = researcher.current_model
    run = AgentRun(
        employee_id=researcher.id,
        project_id=project.id,
        work_id=work.id,
        model_config_id=model.id,
        purpose="TASK_EXECUTION",
        user_request="Persist provider sources for readable research result.",
        system_prompt_snapshot="test system prompt",
        context_snapshot="test context",
        status="SUCCEEDED",
        context_composition_json={"provider_sources": [
            {"title": "Taiwan source", "url": "https://example.com/source", "date": "2026-09-04"},
            {"title": "Unsafe source", "url": "javascript:alert(1)"},
        ]},
        real_cost=Decimal("0"),
        currency=model.currency,
        provider_key_snapshot=model.provider_key,
        model_name_snapshot=model.model_name,
        input_price_snapshot=model.input_price_per_million,
        output_price_snapshot=model.output_price_per_million,
        request_price_snapshot=model.request_price_per_call,
        currency_snapshot=model.currency,
    )
    db.session.add(run)
    db.session.flush()
    artifact = Artifact(project_id=project.id, work_id=work.id, artifact_type="RESEARCH_REPORT", title="Research")
    db.session.add(artifact)
    db.session.flush()
    content = "Evidence-backed research result"
    version = ArtifactVersion(
        artifact_id=artifact.id,
        version=1,
        producer_employee_id=researcher.id,
        execution_id=run.id,
        status="ACCEPTED",
        content_text=content,
        content_hash=artifact_service._hash(content, None),
    )
    db.session.add(version)
    db.session.commit()

    view = artifact_service.readable_artifact(version)
    assert view["sources"] == [{
        "title": "Taiwan source",
        "url": "https://example.com/source",
        "date": "2026-09-04",
    }]


def test_founder_handoff_surface_uses_persisted_work_dependencies(ctx):
    operation, project, researcher, critic = _setup()
    snapshot = project_snapshot(project)
    handoff = snapshot["handoff"]
    assert handoff["node_count"] == 3
    assert handoff["edge_count"] == 2
    assert len(handoff["stages"]) == 2
    assert {row["work"].title for row in handoff["stages"][0]["nodes"]} == {
        "Research branch", "Critical branch"
    }
    synthesis = handoff["stages"][1]["nodes"][0]
    assert synthesis["work"].title == "Synthesis branch"
    assert set(synthesis["dependency_names"]) == {"Research branch", "Critical branch"}


def test_founder_status_calls_running_retry_recovering_not_clean_work(ctx):
    from types import SimpleNamespace
    from eason_one.services import project_company

    work = SimpleNamespace(state="EXECUTING")
    run = SimpleNamespace(status="RUNNING", retry_of_run_id=123)
    assert project_company._pulse_work_status(work, run, None) == "RECOVERING"
