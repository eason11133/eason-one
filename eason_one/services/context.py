import hashlib
import json
import re
from decimal import Decimal

from sqlalchemy import func

from ..extensions import db
from ..models import AgentRun, Artifact, ArtifactVersion, CostEvent, HiringRequest, Work, WorkMessage, Project, KnowledgeItem, ResearchRecord, VerificationRecord
from .brain import current
from .company import remaining

OPERATION_CONTEXT_BUDGET = 6000


_EVIDENCE_STOPWORDS = {
    "about", "after", "against", "available", "before", "between", "compare",
    "current", "decision", "eason", "evidence", "existing", "final", "founder",
    "mission", "operation", "output", "project", "recommendation", "requested",
    "should", "task", "using", "validate", "within", "without",
}

def _evidence_terms(task):
    operation = getattr(task, "operation", None)
    plan = getattr(operation, "plan_json", None) or {}
    text = " ".join(filter(None, [
        getattr(task, "title", None), getattr(task, "objective", None),
        getattr(operation, "title", None), getattr(operation, "objective", None),
        plan.get("executive_response") if isinstance(plan, dict) else None,
    ]))
    terms = []
    for token in re.findall(r"[A-Za-z][A-Za-z0-9_-]{3,}|[\u4e00-\u9fff]{2,}", text.casefold()):
        if token not in _EVIDENCE_STOPWORDS and token not in terms:
            terms.append(token)
    return terms[:24]

def relevant_existing_evidence(task, limit=8):
    """Retrieve persisted, source-labelled evidence relevant to this Task.

    This is deterministic local retrieval, not web research.  It intentionally
    searches across persisted Founder/CEO dialogue and existing company records
    because a newly-created decision Project otherwise has an empty Project-local
    Brain even when Eason One already contains the evidence in another surface.
    """
    terms = _evidence_terms(task)
    if not terms:
        return [], {"terms": [], "relevant_items": 0}

    candidates = []
    for row in WorkMessage.query.filter(WorkMessage.message_type.in_([
        "FOUNDER_TO_CEO", "CEO_TO_FOUNDER", "CEO_REMEDIATION", "CEO_REASSIGNMENT",
    ])).order_by(WorkMessage.id.desc()).limit(100).all():
        candidates.append((f"WORK_MESSAGE #{row.id} · {row.message_type}", row.content or ""))
    # Project objectives/status summaries are planning metadata, not evidence.
    # Counting the current Project row as evidence would let an evidence-only
    # Task satisfy its own preflight merely because the question text appears in
    # the Project Contract. Durable evidence comes from source-labelled messages,
    # approved Knowledge/Research records, or accepted+verified Artifacts below.

    # Accepted + independently verified ArtifactVersions are durable Company
    # evidence too.  Only current ACCEPTED Work/Artifact truth is eligible; a
    # stale SUBMITTED/REJECTED version can never wake an evidence-only Work.
    accepted_versions = (
        ArtifactVersion.query.join(Artifact, ArtifactVersion.artifact_id == Artifact.id)
        .join(Work, Artifact.work_id == Work.id)
        .filter(
            Work.state == "ACCEPTED",
            ArtifactVersion.status == "ACCEPTED",
        )
        .order_by(ArtifactVersion.id.desc()).limit(60).all()
    )
    for version in accepted_versions:
        if not VerificationRecord.query.filter_by(
            work_id=version.artifact.work_id,
            artifact_version_id=version.id,
            status="PASSED",
        ).first():
            continue
        content = " ".join((version.content_text or "").split())
        if not content:
            continue
        candidates.append((
            f"ACCEPTED_ARTIFACT_VERSION #{version.id} · {version.artifact.title} · {version.content_hash}",
            content,
        ))
    # Only knowledge that can itself function as evidence is eligible here.
    # Founder-approved DECISION/HYPOTHESIS/contract rows are authoritative
    # planning/governance truth, but they do not prove the claims they ask the
    # Company to validate.  In particular, Project Contract and success-criteria
    # DECISION rows must never satisfy an evidence-only preflight merely because
    # the Task repeats their wording.
    knowledge_rows = (
        KnowledgeItem.query.filter(
            KnowledgeItem.founder_approved.is_(True),
            KnowledgeItem.kind.in_(["FACT", "EVIDENCE"]),
        )
        .order_by(KnowledgeItem.id.desc()).limit(80).all()
    )
    for row in knowledge_rows:
        candidates.append((f"KNOWLEDGE #{row.id} · {row.kind} · {row.title}", row.content or ""))
    for row in ResearchRecord.query.filter_by(founder_approved=True).order_by(ResearchRecord.id.desc()).limit(50).all():
        candidates.append((f"RESEARCH_RECORD {row.record_key} · {row.record_type} · {row.title}", row.summary or ""))

    scored = []
    for source, content in candidates:
        haystack = f"{source} {content}".casefold()
        matched = [term for term in terms if term in haystack]
        if not matched:
            continue
        # Prefer records matching multiple task-specific entities, then recency.
        score = len(matched) + sum(2 for term in matched if term in {"wildone", "english", "trainer", "sqlalchemy", "meeting"})
        scored.append((score, source, content, matched))
    scored.sort(key=lambda item: item[0], reverse=True)
    rows = []
    seen = set()
    for score, source, content, matched in scored:
        fingerprint = (source.split(" · ")[0], content[:120])
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        rows.append({
            "source": source,
            "content": " ".join(content.split())[:700],
            "matched_terms": matched[:8],
            "score": score,
        })
        if len(rows) >= limit:
            break
    basis_payload = {
        "terms": terms,
        "rows": [
            {"source": row["source"], "content": row["content"]}
            for row in rows
        ],
    }
    basis_hash = hashlib.sha256(
        json.dumps(basis_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return rows, {
        "terms": terms,
        "relevant_items": len(rows),
        "basis_hash": basis_hash,
    }


def _bounded(text, budget):
    if len(text) <= budget:
        return text
    return text[:budget - 1] + "…"


def _latest_review(task):
    if not task or not task.operation_id:
        return None
    exact = AgentRun.query.filter_by(
        operation_id=task.operation_id, task_id=task.id,
        purpose="TASK_REVIEW", status="SUCCEEDED",
    ).order_by(AgentRun.id.desc()).first()
    if exact or getattr(task, "work_id", None):
        # vNext Work context is branch-local. Falling back to another Task's
        # review leaks unrelated critique/rework instructions into this Work and
        # can make a long Project drift after one sibling branch is reviewed.
        return exact
    # Pre-Work legacy Operations historically used one operation-level review.
    return AgentRun.query.filter_by(
        operation_id=task.operation_id, purpose="TASK_REVIEW",
        status="SUCCEEDED",
    ).order_by(AgentRun.id.desc()).first()


def operation_context(task):
    operation = getattr(task, "operation", None)
    if not operation:
        return "", {"characters": 0, "budget": OPERATION_CONTEXT_BUDGET}
    actual = Decimal(db.session.query(func.coalesce(
        func.sum(CostEvent.real_cost_delta), 0
    )).filter_by(operation_id=operation.id).scalar())
    handoff_lineage = []
    lines = [
        "OPERATION CONTEXT",
        f"Operation #{operation.id}: {operation.title}",
        f"Objective: {operation.objective}",
        f"State: {operation.status}",
        (
            f"Budget: approved TWD {operation.approved_budget_twd}; "
            f"actual TWD {actual}; remaining TWD "
            f"{Decimal(operation.approved_budget_twd) - actual}"
        ),
    ]
    # Hand only required persisted evidence to the current Task. Legacy serial
    # Operations keep the historical earlier-Task handoff. Multi-Agent DAG
    # Operations receive only declared dependencies so parallel branches do not
    # leak unrelated conclusions into each other's context.
    if task is not None:
        # Company Core vNext: WorkDependency is the dependency truth.  The
        # orchestration plan is only the management decision that projected
        # these edges; downstream Employee context must consume the durable Work
        # graph itself so execution eligibility and artifact handoff cannot drift.
        predecessors = []
        predecessor_depths = {}
        if getattr(task, "work_id", None):
            models = __import__(
                "eason_one.models", fromlist=["Work", "WorkDependency"]
            )
            task_by_work = {item.work_id: item for item in operation.tasks if item.work_id}
            queue = [(task.work_id, 0)]
            seen_work_ids = {task.work_id}
            # Preserve the accepted dependency lineage, not merely one hop. A
            # Research -> Product -> Engineer chain must not lose the original
            # sourced evidence when Engineer only directly depends on Product.
            while queue and len(predecessors) < 8:
                current_work_id, depth = queue.pop(0)
                edges = models.WorkDependency.query.filter_by(work_id=current_work_id).order_by(models.WorkDependency.id).all()
                for edge in edges:
                    upstream_work_id = edge.depends_on_work_id
                    if upstream_work_id in seen_work_ids:
                        continue
                    seen_work_ids.add(upstream_work_id)
                    predecessor_work = db.session.get(models.Work, upstream_work_id)
                    predecessor = task_by_work.get(upstream_work_id)
                    if predecessor_work is None or predecessor is None or predecessor_work.state != "ACCEPTED":
                        continue
                    predecessors.append(predecessor)
                    predecessor_depths[predecessor.id] = depth + 1
                    queue.append((upstream_work_id, depth + 1))
                    if len(predecessors) >= 8:
                        break
        else:
            # Pre-vNext compatibility only.
            predecessors = [
                item for item in operation.tasks
                if item.id < task.id and item.status in {"DONE", "CANCELLED"}
            ][-4:]
            predecessor_depths = {item.id: 1 for item in predecessors}
        for predecessor in predecessors:
            run = AgentRun.query.filter_by(
                operation_id=operation.id, task_id=predecessor.id,
                status="SUCCEEDED",
            ).order_by(AgentRun.id.desc()).first()
            predecessor_work = None
            accepted_version = None
            verification_ids = []
            if predecessor.work_id:
                models = __import__(
                    "eason_one.models",
                    fromlist=["Work", "Artifact", "ArtifactVersion", "VerificationRecord"],
                )
                predecessor_work = db.session.get(models.Work, predecessor.work_id)
                accepted_version = (
                    models.ArtifactVersion.query.join(
                        models.Artifact, models.ArtifactVersion.artifact_id == models.Artifact.id
                    ).filter(
                        models.Artifact.work_id == predecessor.work_id,
                        models.ArtifactVersion.status == "ACCEPTED",
                    ).order_by(models.ArtifactVersion.id.desc()).first()
                )
                if accepted_version is not None:
                    verification_ids = [
                        row.id for row in models.VerificationRecord.query.filter_by(
                            work_id=predecessor.work_id, artifact_version_id=accepted_version.id
                        ).order_by(models.VerificationRecord.id).all()
                    ]
            lines.extend([
                (
                    f"PREDECESSOR WORK #{predecessor_work.id} — ACCEPTED"
                    if predecessor_work is not None
                    else f"PREDECESSOR TASK #{predecessor.id} — {predecessor.status}"
                ),
                f"Dependency depth: {predecessor_depths.get(predecessor.id, 1)}",
                f"Title: {predecessor.title}",
                f"Accepted result summary: {predecessor.result_summary or '-'}",
                (
                    f"Accepted ArtifactVersion: #{accepted_version.id}; hash: {accepted_version.content_hash}; "
                    f"verification records: {verification_ids or '-'}"
                    if accepted_version is not None else
                    f"Evidence Run: #{run.id if run else '-'}; validation: {getattr(run, 'structured_validation_status', None) or '-'}"
                ),
            ])
            # Research source lineage is structured runtime truth on the exact
            # producing AgentRun. Put lineage before prose excerpts so bounded
            # context truncation cannot silently remove the evidence chain.
            accepted_run = (
                db.session.get(AgentRun, accepted_version.execution_id)
                if accepted_version is not None and accepted_version.execution_id else run
            )
            accepted_sources = [
                row for row in ((getattr(accepted_run, "context_composition_json", None) or {}).get("provider_sources") or [])
                if isinstance(row, dict) and str(row.get("url") or "").strip()
            ]
            if accepted_version is not None and predecessor_work is not None:
                handoff_lineage.append({
                    "dependency_depth": predecessor_depths.get(predecessor.id, 1),
                    "work_id": predecessor_work.id,
                    "task_id": predecessor.id,
                    "artifact_version_id": accepted_version.id,
                    "artifact_content_hash": accepted_version.content_hash,
                    "producing_execution_id": getattr(accepted_run, "id", None),
                    "verification_record_ids": verification_ids,
                    "provider_sources": [
                        {"title": str(row.get("title") or "").strip(), "url": str(row.get("url") or "").strip()}
                        for row in accepted_sources[:20]
                    ],
                })
            if accepted_sources:
                lines.append("Provider-observed sources bound to the accepted ArtifactVersion:")
                for source in accepted_sources[:12]:
                    title = str(source.get("title") or "Source").strip()
                    url = str(source.get("url") or "").strip()
                    lines.append(f"- {title}: {url}")
            if accepted_version is not None and accepted_version.content_text:
                artifact_text = " ".join(accepted_version.content_text.split())
                lines.append(f"Accepted Artifact excerpt: {artifact_text[:1000]}")
            # For governed Work, engineering handoff details must come from the
            # exact Execution that produced the ACCEPTED ArtifactVersion. A newer
            # successful-but-unaccepted duplicate/rework Run is not dependency
            # truth and must never leak changed_files/tests into downstream Work.
            codex_run = accepted_run if accepted_version is not None else run
            codex = ((getattr(codex_run, "parsed_output_json", None) or {}).get("codex") or {}) if codex_run else {}
            if codex:
                lines.extend([
                    "Changed files: " + json.dumps(codex.get("changed_files") or [], ensure_ascii=False),
                    "Tests: " + json.dumps(codex.get("tests") or [], ensure_ascii=False),
                    "Acceptance: " + json.dumps(codex.get("acceptance") or [], ensure_ascii=False),
                ])

    memory = operation.memory_json or {}
    for decision in (memory.get("decisions") or [])[-4:]:
        lines.extend([
            "CEO DECISION",
            f"Action: {decision.get('action', '-')}",
            f"Reason: {decision.get('reason', '-')}",
        ])
    for item in (memory.get("meeting_results") or [])[-3:]:
        result = item.get("result") or {}
        lines.extend([
            f"MEETING RESULT #{item.get('meeting_id', '-')}",
            f"Decision / position: {result.get('position') or '-'}",
            (
                "Qualification / disagreement: "
                f"{result.get('qualification_or_disagreement') or '-'}"
            ),
            f"Risk: {result.get('risk') or '-'}",
            "Actions: " + json.dumps(
                result.get("actions") or [], ensure_ascii=False
            ),
            "Controls: " + json.dumps(
                result.get("controls") or [], ensure_ascii=False
            ),
        ])
    review = _latest_review(task)
    payload = (review.parsed_output_json or {}) if review else {}
    if payload:
        lines.extend([
            f"LATEST REVIEW FOR TASK #{review.task_id}",
            f"Decision: {payload.get('decision') or '-'}",
            f"Summary: {payload.get('summary') or '-'}",
            "Issues:\n" + "\n".join(
                f"- {value}" for value in payload.get("issues") or []
            ),
            "Required changes:\n" + "\n".join(
                f"- {value}" for value in payload.get("required_changes") or []
            ),
        ])
    audit_messages = WorkMessage.query.filter(
        WorkMessage.project_id == operation.project_id,
        WorkMessage.message_type.in_([
            "CEO_REMEDIATION", "CEO_REASSIGNMENT",
        ]),
    ).order_by(WorkMessage.id.desc()).limit(5).all()
    if audit_messages:
        lines.append("REMEDIATION INSTRUCTIONS")
        lines.extend(f"- {item.content}" for item in reversed(audit_messages))
    hires = HiringRequest.query.filter_by(
        operation_id=operation.id, status="HIRED"
    ).order_by(HiringRequest.id.desc()).limit(4).all()
    for request in reversed(hires):
        employee = request.created_employee
        if not employee:
            continue
        lines.extend([
            f"WORKFORCE CHANGE — HIRING REQUEST #{request.id}",
            (
                f"Available Employee #{employee.id}: {employee.name}; "
                f"Department {employee.department.name}; "
                f"Position {employee.position.name}; "
                f"Manager {employee.manager.name if employee.manager else 'Founder'}; "
                f"Role {employee.role_description}; "
                f"Model {employee.current_model.label if employee.current_model else '-'}"
            ),
        ])
    text = _bounded("\n".join(lines), OPERATION_CONTEXT_BUDGET)
    return text, {
        "characters": len(text),
        "budget": OPERATION_CONTEXT_BUDGET,
        "latest_review_run_id": getattr(review, "id", None),
        "meeting_results": min(len(memory.get("meeting_results") or []), 3),
        "hiring_changes": len(hires),
        "handoff_lineage": handoff_lineage,
    }


def build_with_composition(employee, project=None, task=None):
    parts = [
        f"EMPLOYEE\n{employee.name} — {employee.role_description}",
        (
            "Department: "
            f"{employee.department.name if employee.department else 'CEO Office'}; "
            "Manager: "
            f"{employee.manager.name if employee.manager else 'Founder'}"
        ),
    ]
    if project:
        terms = __import__(
            "eason_one.services.project_contract", fromlist=["governing_terms"]
        ).governing_terms(project)
        parts.append(
            f"PROJECT\n{project.name}\nObjective: {terms.get('objective') or '-'}\n"
            f"Status: {project.status}; Priority: {project.priority}\n"
            f"Constraints: {', '.join(terms.get('constraints') or []) or '-'}\n"
            f"Deadline: {terms.get('deadline') or '-'}; Project budget authority TWD {terms.get('budget_limit_twd') or '-'}"
        )
    if task:
        parts.append(
            f"TASK\n{task.title}\nObjective: {task.objective}\n"
            f"Required output: {task.required_output or '-'}\n"
            f"Acceptance: {task.acceptance_criteria or '-'}"
        )
        messages = WorkMessage.query.filter_by(task_id=task.id).order_by(
            WorkMessage.created_at.desc()
        ).limit(5).all()
        if messages:
            parts.append(
                "WORK CONTEXT\n" +
                "\n".join(item.content for item in reversed(messages))
            )
        operation_text, operation_meta = operation_context(task)
        if operation_text:
            parts.append(operation_text)
    else:
        operation_meta = {
            "characters": 0, "budget": OPERATION_CONTEXT_BUDGET
        }
    employee_memory = __import__(
        "eason_one.services.employee_memory",
        fromlist=["relevant_experience", "founder_feedback_context"],
    )
    experience_text, experience_meta = employee_memory.relevant_experience(employee, project, task)
    if experience_text:
        parts.append(experience_text)
    feedback_text, feedback_meta = employee_memory.founder_feedback_context(employee)
    if feedback_text:
        parts.append(feedback_text)
    evidence_rows, evidence_meta = relevant_existing_evidence(task) if task else ([], {"terms": [], "relevant_items": 0})
    if evidence_rows:
        parts.append(
            "RETRIEVED EXISTING EASON ONE EVIDENCE\n" + "\n".join(
                f"SOURCE: {row['source']}\nCLAIM: {row['content']}"
                for row in evidence_rows
            )
        )
    brain = current(project.id if project else None)
    normal = [item for item in brain if item.kind != "KILLED"
      and item.source_ref != "operation-budget-governance"]
    killed = [item for item in brain if item.kind == "KILLED"]
    if normal:
        parts.append(
            "CURRENT COMPANY BRAIN\n" + "\n".join(
                f"{item.kind}: {item.title} — {item.content}" +
                (
                    f" [corrects #{item.target_knowledge_id}]"
                    if item.kind == "CORRECTION" else ""
                )
                for item in normal[-12:]
            )
        )
    if killed:
        parts.append(
            "KILLED IDEAS — DO NOT RESURRECT WITHOUT MEANINGFUL NEW EVIDENCE\n"
            + "\n".join(
                f"{item.title} — {item.content} "
                f"[kills hypothesis #{item.target_knowledge_id}]"
                for item in killed[-8:]
            )
        )
    parts.append(
        f"COST\nRemaining company real budget: NT${remaining():,.2f}"
    )
    return "\n\n".join(parts), {
        "operation_context": operation_meta,
        "employee_experience": experience_meta,
        "founder_feedback": feedback_meta,
        "evidence_retrieval": evidence_meta,
    }


def build(employee, project=None, task=None):
    return build_with_composition(employee, project, task)[0]
