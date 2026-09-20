"""Research-grade read models and exports for Eason One.

The operational tables remain authoritative.  This module never invokes a
provider and never rewrites execution truth.  It turns persisted runs,
Meetings, Tasks, costs, prompts, and Founder notes into a report-ready ledger.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal
import csv
import io
import json
from typing import Any

from ..extensions import db
from ..models import (
    AgentRun,
    Artifact,
    ArtifactVersion,
    Company,
    CompanyEvent,
    CostEvent,
    Employee,
    EmployeeLearningRecord,
    EmployeeModelHistory,
    KnowledgeItem,
    Meeting,
    MeetingMessage,
    MeetingStep,
    Operation,
    OperationStep,
    Project,
    ResearchRecord,
    Task,
    VerificationRecord,
    Work,
)

RECORD_TYPES = (
    "DECISION",
    "EXPERIMENT",
    "FAILURE",
    "FIX",
    "PROMPT",
    "ARTIFACT",
    "HYPOTHESIS",
    "EVIDENCE",
    "NOTE",
)
RECORD_STATUSES = ("OPEN", "ACTIVE", "VALIDATED", "REJECTED", "SUPERSEDED", "CLOSED")


def _decimal(value: Any) -> Decimal:
    return Decimal(value or 0)


def _iso(value):
    return value.isoformat() if value else None


def _run_call_confirmed(run: AgentRun) -> bool:
    return bool(
        run.provider_response_id
        or run.input_tokens is not None
        or run.output_tokens is not None
        or (run.real_cost is not None and _decimal(run.real_cost) > 0)
    )


def _run_row(run: AgentRun, *, include_payload: bool = False) -> dict[str, Any]:
    row = {
        "id": run.id,
        "purpose": run.purpose,
        "status": run.status,
        "employee_id": run.employee_id,
        "project_id": run.project_id,
        "task_id": run.task_id,
        "operation_id": run.operation_id,
        "meeting_id": run.meeting_id,
        "provider": run.provider_key_snapshot,
        "model": run.model_name_snapshot,
        "prompt_version": run.prompt_version,
        "prompt_hash": run.prompt_hash,
        "context_hash": run.context_hash,
        "output_hash": run.output_hash,
        "retry_of_run_id": run.retry_of_run_id,
        "replacement_run_id": run.replacement_run_id,
        "input_tokens": run.input_tokens or 0,
        "output_tokens": run.output_tokens or 0,
        "effective_max_output_tokens": run.effective_max_output_tokens,
        "real_cost": str(run.real_cost) if run.real_cost is not None else None,
        "currency": run.currency_snapshot,
        "provider_stop_reason": run.provider_stop_reason,
        "failure_reason": run.failure_reason,
        "validation_status": run.structured_validation_status,
        "validation_warnings": run.structured_validation_warnings_json or [],
        "validation_errors": run.structured_validation_errors_json or [],
        "resolution_status": run.resolution_status,
        "resolution_note": run.resolution_note,
        "started_at": _iso(run.started_at),
        "finished_at": _iso(run.finished_at),
        "provider_call_confirmed": _run_call_confirmed(run),
    }
    if include_payload:
        row.update(
            {
                "user_request": run.user_request,
                "system_prompt": run.system_prompt_snapshot,
                "context": run.context_snapshot,
                "context_composition": run.context_composition_json,
                "response_schema": run.response_schema_snapshot_json,
                "raw_output": run.raw_output,
                "parsed_output": run.parsed_output_json,
                "error_text": run.error_text,
            }
        )
    return row


def _record_row(record: ResearchRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "record_key": record.record_key,
        "record_type": record.record_type,
        "title": record.title,
        "summary": record.summary,
        "status": record.status,
        "version_label": record.version_label,
        "project_id": record.project_id,
        "operation_id": record.operation_id,
        "task_id": record.task_id,
        "meeting_id": record.meeting_id,
        "agent_run_id": record.agent_run_id,
        "source_ref": record.source_ref,
        "metadata": record.metadata_json or {},
        "founder_approved": record.founder_approved,
        "created_at": _iso(record.created_at),
        "updated_at": _iso(record.updated_at),
    }


def _next_key(record_type: str) -> str:
    prefix = {
        "DECISION": "DEC",
        "EXPERIMENT": "EXP",
        "FAILURE": "FAIL",
        "FIX": "FIX",
        "PROMPT": "PROMPT",
        "ARTIFACT": "ART",
        "HYPOTHESIS": "HYP",
        "EVIDENCE": "EVID",
        "NOTE": "NOTE",
    }[record_type]
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    stem = f"{prefix}-{day}-"
    existing = ResearchRecord.query.filter(ResearchRecord.record_key.like(stem + "%")).count()
    return f"{stem}{existing + 1:03d}"


def create_record(
    *,
    record_type: str,
    title: str,
    summary: str,
    status: str = "OPEN",
    version_label: str | None = None,
    project_id: int | None = None,
    operation_id: int | None = None,
    task_id: int | None = None,
    meeting_id: int | None = None,
    agent_run_id: int | None = None,
    source_ref: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> ResearchRecord:
    record_type = (record_type or "").strip().upper()
    status = (status or "OPEN").strip().upper()
    title = " ".join((title or "").split())
    summary = (summary or "").strip()
    if record_type not in RECORD_TYPES:
        raise ValueError("Unsupported research record type")
    if status not in RECORD_STATUSES:
        raise ValueError("Unsupported research record status")
    if not title or len(title) > 220:
        raise ValueError("Research record title is required and must be at most 220 characters")
    if not summary or len(summary) > 12000:
        raise ValueError("Research record summary is required and must be at most 12,000 characters")
    record = ResearchRecord(
        record_key=_next_key(record_type),
        record_type=record_type,
        title=title,
        summary=summary,
        status=status,
        version_label=(version_label or "").strip() or None,
        project_id=project_id,
        operation_id=operation_id,
        task_id=task_id,
        meeting_id=meeting_id,
        agent_run_id=agent_run_id,
        source_ref=(source_ref or "").strip() or None,
        metadata_json=metadata or None,
        founder_approved=True,
    )
    db.session.add(record)
    db.session.commit()
    return record


def _vision_and_feasibility(runs: list[AgentRun]) -> dict[str, Any]:
    """Research/vision projection grounded in current persisted system truth.

    Roadmap labels are explicit product intent, while evidence counters come
    only from the current database. The surface must not promote an unproven
    fresh end-to-end Project into a completed claim.
    """
    execution_substrates = sorted({
        str(run.provider_key_snapshot or "").strip().lower()
        for run in runs
        if str(run.provider_key_snapshot or "").strip().lower() not in {"", "mock", "legacy"}
        and run.status in {"RUNNING", "SUCCEEDED", "FAILED"}
    })
    work_total = Work.query.count()
    work_accepted = Work.query.filter_by(state="ACCEPTED").count()
    accepted_versions = ArtifactVersion.query.filter_by(status="ACCEPTED").count()
    passed_verifications = VerificationRecord.query.filter_by(status="PASSED").count()
    learning_records = EmployeeLearningRecord.query.filter_by(validated=True).count()
    outcome_learning = EmployeeLearningRecord.query.filter(
        EmployeeLearningRecord.validated.is_(True),
        EmployeeLearningRecord.validation_basis == "CANONICAL_WORK_ACCEPTANCE",
        EmployeeLearningRecord.learning_type.in_(("WORK_EXPERIENCE", "REVIEW_EXPERIENCE")),
    ).count()
    employees = Employee.query.filter_by(active=True).count()
    model_history = EmployeeModelHistory.query.count()
    market_models = __import__(
        "eason_one.models", fromlist=["MarketOrder", "MarketContract", "MarketLedgerEntry"]
    )
    market_orders = market_models.MarketOrder.query.count()
    market_settled = market_models.MarketContract.query.filter_by(status="SETTLED").count()
    market_superseded = market_models.MarketContract.query.filter_by(status="SUPERSEDED").count()
    market_ledger = market_models.MarketLedgerEntry.query.count()
    probation_completed = CompanyEvent.query.filter_by(event_type="EMPLOYEE_PROBATION_COMPLETED").count()
    employee_evolution = __import__(
        "eason_one.services.employee_evolution",
        fromlist=["company_behavioral_evolution_summary", "learning_consumption_summary"],
    )
    behavioral = employee_evolution.company_behavioral_evolution_summary()
    consumption = employee_evolution.learning_consumption_summary()
    economic = __import__(
        "eason_one.services.market", fromlist=["economic_evolution_summary"]
    ).economic_evolution_summary()
    research_department = __import__(
        "eason_one.services.research_department",
        fromlist=["specialist_employees", "provider_family", "LABEL_BY_PROVIDER"],
    )
    research_specialists = research_department.specialist_employees(active_only=True)
    research_roster = [
        {
            "employee_id": employee.id,
            "employee_name": employee.name,
            "provider_family": research_department.provider_family(employee),
            "provider_label": research_department.LABEL_BY_PROVIDER.get(
                research_department.provider_family(employee), research_department.provider_family(employee)
            ),
            "model": getattr(getattr(employee, "current_model", None), "model_name", None),
            "salary_ec_per_week": str(employee.salary_credits_per_week or 0),
        }
        for employee in research_specialists
    ]

    return {
        "north_star": "Founder sets goal, budget, authority and constraints; an accountable AI company organizes, executes, learns, verifies and returns evidence-backed outcomes.",
        "research_questions": [
            "Can probabilistic AI workers operate inside deterministic authority, budget and evidence boundaries?",
            "Can an Employee remain a durable accountable identity even when its model/provider changes?",
            "Can a company recover, hand off and verify Work without making the Founder the runtime scheduler?",
            "Can accepted organizational experience change future staffing and eventually support an AI labor market?",
        ],
        "evolution_loop": [
            {"step": "1", "name": "Verified outcome", "detail": "Only accepted Artifact + PASS Verification becomes canonical experience."},
            {"step": "2", "name": "Persistent Employee memory", "detail": "Owner/reviewer outcome evidence and recovery burden stay attached to the Employee across Projects."},
            {"step": "3", "name": "Execution consumption", "detail": "Bounded canonical experience enters CEO planning and the assigned Employee/reviewer Run context with exact learning-record provenance."},
            {"step": "4", "name": "Staffing changes", "detail": "Among already-capable Employees, accepted experience can change who receives future Work."},
            {"step": "5", "name": "Employment evolution", "detail": "Delegated hires leave probation only after the configured number of canonical accepted owner assignments; the transition grants no new capability or authority."},
            {"step": "6", "name": "Economic consequence", "detail": "Eason Market settles accepted Work in EC, gives proven Employees a bounded evidence-backed skill premium, and preserves superseded unpaid contract generations when Work is legitimately reassigned."},
            {"step": "7", "name": "Next Project", "detail": "The company starts the next Project with changed organizational memory rather than a clean slate."},
        ],
        "roadmap": [
            {"name": "AI-native Company OS", "status": "MANDATORY CORE", "detail": "Founder goal → governed Project → Work → execution → verified result."},
            {"name": "Persistent AI Employees", "status": "MANDATORY CORE", "detail": "Durable identity, role, history, accountability and replaceable AI Core."},
            {"name": "Research Department / Multi-AI", "status": "ACTIVE BUILD · DEPARTMENT V1", "detail": "Research Director can delegate to persistent OpenAI, Claude, Gemini and Perplexity Researcher Employees. Explicit multi-AI research becomes independent specialist Work followed by accountable department synthesis; exact model versions remain replaceable within each provider family."},
            {"name": "Autonomous Company Operation", "status": "MANDATORY CORE", "detail": "Planning, staffing, handoff, review, recovery and rework stay company-owned."},
            {"name": "Company Truth / Authority / Governance", "status": "MANDATORY CORE", "detail": "Budget, contracts, evidence and Result Ready remain deterministic authority."},
            {"name": "Learning & Evolution", "status": "ACTIVE BUILD · CONSUMPTION V1.4", "detail": "Validated accepted Work changes future staffing and CEO planning, and now reaches the assigned Employee/reviewer execution context with exact canonical record provenance. Persisted no-learning counterfactuals remain the stricter proof of causal staffing change; owner outcomes can also complete delegated-hire probation without minting authority."},
            {"name": "Eason Market / AI Employee Economy", "status": "ACTIVE BUILD · INTERNAL V1.3", "detail": "Intra-company Work demand → eligible offers → EC contract → accepted delivery → verification-bound settlement; experience changes future EC quotes, and legitimate reassignment creates auditable superseding contract generations. Cross-company market remains later."},
            {"name": "Game-like Company World / Client", "status": "LATER", "detail": "Visualize real company truth; never fabricate NPC activity."},
            {"name": "Company #001 → Multi-company", "status": "LATER", "detail": "Company becomes an isolation/authority boundary and can contract across companies."},
            {"name": "Software ↔ Physical Production", "status": "FROZEN", "detail": "Hardware/robotics expansion intentionally deferred during the two-week core push."},
        ],
        "evidence": [
            {"claim": "Persistent employees exist as durable identities", "value": employees, "unit": "active Employees", "detail": f"{model_history} model-assignment history records preserve identity across AI Core changes."},
            {"claim": "Research Department has provider-specialized accountable identities", "value": len(research_roster), "unit": "active AI Researchers", "detail": " · ".join(f"{row['employee_name']} → {row['provider_label']} / {row['model']}" for row in research_roster) if research_roster else "No configured provider-specialized Researcher is active on this host yet."},
            {"claim": "Company Work survives individual model calls", "value": work_total, "unit": "durable Work records", "detail": f"{work_accepted} accepted Work records are persisted independently of Run lifecycle."},
            {"claim": "Results have evidence/verification lineage", "value": accepted_versions, "unit": "accepted ArtifactVersions", "detail": f"{passed_verifications} passed VerificationRecords are persisted."},
            {"claim": "Multiple execution substrates have been exercised", "value": len(execution_substrates), "unit": "non-mock substrates", "detail": " · ".join(execution_substrates) if execution_substrates else "No non-mock execution substrate is persisted yet."},
            {"claim": "Employee experience is durable and reaches future work", "value": learning_records, "unit": "validated learning records", "detail": f"{outcome_learning} are canonical accepted-Work experience records. {consumption['runs_with_canonical_memory']} later Run(s) persisted canonical memory consumption, {behavioral['counterfactual_behavior_changes']} staffing decision(s) prove a different no-learning counterfactual, and {probation_completed} probation completion event(s) are evidence-backed."},
            {"claim": "An internal AI labor-market loop is durable", "value": market_orders, "unit": "market orders", "detail": f"{market_settled} contracts are verification-settled, {market_superseded} unpaid contract generation(s) are superseded rather than overwritten, {market_ledger} EC ledger entries are persisted, and {economic['offers_with_experience_premium']} offer(s) carry an explicit outcome-backed EC skill premium. This is internal V1.3, not cross-company commerce."},
        ],
        "behavioral_proof": {
            "consumption": consumption,
            "staffing": behavioral,
            "economy": economic,
            "research_department": {"active_specialists": len(research_roster), "roster": research_roster},
        },
        "boundaries": [
            "Engineering evidence is not the same as fresh full-Project product acceptance.",
            "A new unseen Project still has to prove ordinary planning → execution → recovery → verification → Result Ready without Founder runtime intervention.",
            "Provider-family specialization is not a source claim: Claude/Gemini Researcher execution is model-perspective only until that provider has a governed live-search adapter; Eason One never cross-routes a named specialist merely to obtain web evidence.",
            "Eason Market is currently an intra-company V1.3; cross-company market, multi-company operation, game-like client and physical production are not claimed as complete today.",
            "No external Eason One user adoption or product-market validation is claimed.",
        ],
    }


def snapshot() -> dict[str, Any]:
    company = Company.query.first()
    runs = AgentRun.query.order_by(AgentRun.id.desc()).all()
    confirmed = [run for run in runs if _run_call_confirmed(run)]
    successes = [run for run in confirmed if run.status == "SUCCEEDED"]
    failures = [run for run in confirmed if run.status == "FAILED"]
    roots = [run for run in confirmed if not run.retry_of_run_id]
    first_pass_successes = [run for run in roots if run.status == "SUCCEEDED"]
    retries = [run for run in confirmed if run.retry_of_run_id]
    total_cost = sum((_decimal(run.real_cost) for run in confirmed), Decimal("0"))
    input_tokens = sum(run.input_tokens or 0 for run in confirmed)
    output_tokens = sum(run.output_tokens or 0 for run in confirmed)
    truncations = [run for run in failures if run.failure_reason == "OUTPUT_TRUNCATED"]

    prompt_versions = defaultdict(lambda: {"runs": 0, "success": 0, "failed": 0, "cost": Decimal("0"), "tokens": 0})
    for run in confirmed:
        key = run.prompt_version or "UNVERSIONED"
        row = prompt_versions[key]
        row["runs"] += 1
        row["success"] += run.status == "SUCCEEDED"
        row["failed"] += run.status == "FAILED"
        row["cost"] += _decimal(run.real_cost)
        row["tokens"] += (run.input_tokens or 0) + (run.output_tokens or 0)
    prompt_version_rows = [
        {"version": key, **value, "cost": value["cost"]}
        for key, value in sorted(prompt_versions.items(), key=lambda item: item[1]["runs"], reverse=True)
    ]

    meetings = Meeting.query.order_by(Meeting.id.desc()).all()
    meeting_rows = []
    for meeting in meetings:
        meeting_runs = [run for run in confirmed if run.meeting_id == meeting.id]
        meeting_rows.append(
            {
                "meeting": meeting,
                "calls": len(meeting_runs),
                "success": sum(run.status == "SUCCEEDED" for run in meeting_runs),
                "failed": sum(run.status == "FAILED" for run in meeting_runs),
                "retries": sum(bool(run.retry_of_run_id) for run in meeting_runs),
                "tokens": sum((run.input_tokens or 0) + (run.output_tokens or 0) for run in meeting_runs),
                "cost": sum((_decimal(run.real_cost) for run in meeting_runs), Decimal("0")),
            }
        )

    records = ResearchRecord.query.filter(
        ResearchRecord.record_type != "UX_METRIC"
    ).order_by(ResearchRecord.created_at.desc()).all()
    ux_metrics = ResearchRecord.query.filter_by(record_type="UX_METRIC").order_by(
        ResearchRecord.created_at.desc()
    ).all()
    recent_failures = failures[:20]
    operation_failures = OperationStep.query.filter(OperationStep.status.in_(["FAILED", "PAID_FAILED"])).order_by(OperationStep.id.desc()).limit(20).all()
    meeting_failures = MeetingStep.query.filter(MeetingStep.status.in_(["FAILED", "PAID_FAILED"])).order_by(MeetingStep.id.desc()).limit(20).all()

    missing = {
        "prompt_version": sum(not run.prompt_version for run in confirmed),
        "prompt_hash": sum(not run.prompt_hash for run in confirmed),
        "context_hash": sum(not run.context_hash for run in confirmed),
        "output_hash": sum(run.raw_output is not None and not run.output_hash for run in confirmed),
        "unreconciled_cost": sum(run.real_cost is None for run in confirmed),
    }
    capture_score = 100
    if confirmed:
        penalty = sum(missing.values()) / (len(confirmed) * 5)
        capture_score = max(0, round(100 * (1 - penalty)))

    return {
        "company": company,
        "records": records,
        "ux_metrics": ux_metrics[:100],
        "record_types": RECORD_TYPES,
        "record_statuses": RECORD_STATUSES,
        "projects": Project.query.order_by(Project.updated_at.desc()).all(),
        "operations": Operation.query.order_by(Operation.updated_at.desc()).limit(30).all(),
        "tasks": Task.query.order_by(Task.updated_at.desc()).limit(50).all(),
        "meetings": meeting_rows,
        "runs": runs[:80],
        "failures": recent_failures,
        "operation_failures": operation_failures,
        "meeting_failures": meeting_failures,
        "prompt_versions": prompt_version_rows,
        "capture": {"score": capture_score, "missing": missing},
        "vision": _vision_and_feasibility(runs),
        "metrics": {
            "provider_calls": len(confirmed),
            "successful_calls": len(successes),
            "failed_calls": len(failures),
            "retry_calls": len(retries),
            "first_pass_success_rate": (len(first_pass_successes) / len(roots) * 100 if roots else 0),
            "truncations": len(truncations),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "real_cost": total_cost,
            "research_records": len(records),
            "ceo_ux_interactions": len(ux_metrics),
        },
    }


def full_export() -> dict[str, Any]:
    all_runs = AgentRun.query.order_by(AgentRun.id).all()
    return {
        "schema": "eason-one-research-export-v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "vision_and_feasibility": _vision_and_feasibility(all_runs),
        "company": (
            {
                "id": Company.query.first().id,
                "name": Company.query.first().name,
                "currency": Company.query.first().currency,
            }
            if Company.query.first()
            else None
        ),
        "research_records": [_record_row(row) for row in ResearchRecord.query.order_by(ResearchRecord.id).all()],
        "agent_runs": [_run_row(run, include_payload=True) for run in all_runs],
        "meetings": [
            {
                "id": meeting.id,
                "title": meeting.title,
                "purpose": meeting.purpose,
                "agenda": meeting.agenda,
                "status": meeting.status,
                "profile": meeting.execution_profile,
                "rounds": meeting.current_round,
                "max_rounds": meeting.max_rounds,
                "token_limit": meeting.token_limit,
                "cost_limit_twd": str(meeting.real_cost_limit_twd),
                "summary": meeting.current_summary_json,
                "minutes": meeting.minutes_json,
                "started_at": _iso(meeting.started_at),
                "ended_at": _iso(meeting.ended_at),
            }
            for meeting in Meeting.query.order_by(Meeting.id).all()
        ],
        "meeting_messages": [
            {
                "id": row.id,
                "meeting_id": row.meeting_id,
                "employee_id": row.employee_id,
                "speaker_type": row.speaker_type,
                "round": row.round_number,
                "message_type": row.message_type,
                "content": row.content,
                "agent_run_id": row.agent_run_id,
                "validation_status": row.validation_status,
                "validation_warnings": row.validation_warnings_json or [],
                "created_at": _iso(row.created_at),
            }
            for row in MeetingMessage.query.order_by(MeetingMessage.id).all()
        ],
        "operations": [
            {
                "id": row.id,
                "title": row.title,
                "objective": row.objective,
                "status": row.status,
                "project_id": row.project_id,
                "plan": row.plan_json,
                "approved_budget_twd": str(row.approved_budget_twd),
                "actual_cost_twd": str(row.actual_cost_twd),
                "founder_report": row.founder_report_json,
                "waiting_reason": row.waiting_reason,
                "created_at": _iso(row.created_at),
                "updated_at": _iso(row.updated_at),
            }
            for row in Operation.query.order_by(Operation.id).all()
        ],
        "tasks": [
            {
                "id": row.id,
                "project_id": row.project_id,
                "operation_id": row.operation_id,
                "title": row.title,
                "objective": row.objective,
                "status": row.status,
                "assigned_employee_id": row.assigned_employee_id,
                "reviewer_employee_id": row.reviewer_employee_id,
                "required_output": row.required_output,
                "acceptance_criteria": row.acceptance_criteria,
                "result_summary": row.result_summary,
                "created_at": _iso(row.created_at),
                "updated_at": _iso(row.updated_at),
            }
            for row in Task.query.order_by(Task.id).all()
        ],
        "cost_events": [
            {
                "id": row.id,
                "employee_id": row.employee_id,
                "project_id": row.project_id,
                "task_id": row.task_id,
                "operation_id": row.operation_id,
                "agent_run_id": row.agent_run_id,
                "category": row.category,
                "description": row.description,
                "internal_credits_delta": str(row.internal_credits_delta),
                "real_cost_delta": str(row.real_cost_delta),
                "currency": row.currency,
                "created_at": _iso(row.created_at),
            }
            for row in CostEvent.query.order_by(CostEvent.id).all()
        ],
        "knowledge": [
            {
                "id": row.id,
                "project_id": row.project_id,
                "kind": row.kind,
                "title": row.title,
                "content": row.content,
                "rationale": row.rationale,
                "source_ref": row.source_ref,
                "origin_employee_id": row.origin_employee_id,
                "origin_agent_run_id": row.origin_agent_run_id,
                "founder_approved": row.founder_approved,
                "target_knowledge_id": row.target_knowledge_id,
                "created_at": _iso(row.created_at),
            }
            for row in KnowledgeItem.query.order_by(KnowledgeItem.id).all()
        ],
        "employee_model_history": [
            {
                "id": row.id,
                "employee_id": row.employee_id,
                "model_config_id": row.model_config_id,
                "reason": row.reason,
                "started_at": _iso(row.started_at),
                "ended_at": _iso(row.ended_at),
            }
            for row in EmployeeModelHistory.query.order_by(EmployeeModelHistory.id).all()
        ],
    }


def runs_csv() -> str:
    output = io.StringIO()
    fields = [
        "id",
        "started_at",
        "finished_at",
        "purpose",
        "status",
        "employee_id",
        "project_id",
        "task_id",
        "operation_id",
        "meeting_id",
        "provider",
        "model",
        "prompt_version",
        "prompt_hash",
        "context_hash",
        "output_hash",
        "retry_of_run_id",
        "replacement_run_id",
        "input_tokens",
        "output_tokens",
        "effective_max_output_tokens",
        "real_cost",
        "currency",
        "provider_stop_reason",
        "failure_reason",
        "validation_status",
        "resolution_status",
    ]
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    for run in AgentRun.query.order_by(AgentRun.id).all():
        row = _run_row(run)
        writer.writerow({field: row.get(field) for field in fields})
    return output.getvalue()
