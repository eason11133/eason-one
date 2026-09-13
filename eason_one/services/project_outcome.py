"""Project-level outcome evaluation for Eason One v0.20.

Operation completion is evidence, not Project completion. This module evaluates
one immutable Founder Project Contract against accepted Work/Artifact proof
across the entire Project and is the only module allowed to mark a v0.20 Project
RESULT_READY/COMPLETED-equivalent state.
"""
from __future__ import annotations

from decimal import Decimal
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import func

from ..extensions import db
from ..models import (
    AgentRun, Artifact, ArtifactVersion, CompanyEvent, CostEvent, Decision, Escalation,
    KnowledgeItem, Operation, Project, VerificationRecord, Work, WorkMessage, now,
)
from .company_events import emit
from .project_contract import assert_authority_ledger, governing_terms as get_project_contract

TERMINAL_PROJECT = {"COMPLETED", "CANCELLED"}
ACTIVE_PROJECT = {"ACTIVE", "PLANNING", "BLOCKED", "REVIEW"}


def _norm(value: str) -> str:
    return " ".join(str(value or "").casefold().split()).strip(" .;:,-")


def _contract_hash(contract: dict) -> str | None:
    # Project outcome proof binds to the *current governing terms*, not merely
    # the immutable base Contract. Scope/constraint/deadline amendments must
    # invalidate stale Project proof.
    return contract.get("governing_contract_hash") or contract.get("contract_hash")


def criterion_records(contract: dict) -> list[dict]:
    """Return stable criterion ids inside one governing Contract version.

    P1..P8 are meaningful only together with ``_contract_hash(contract)``.  A
    scope amendment changes that hash, so old review/proof rows cannot silently
    satisfy new Founder terms. This removes brittle model-text equality from the
    Project outcome truth flow.
    """
    return [
        {"id": f"P{index}", "text": str(text)}
        for index, text in enumerate(contract.get("success_criteria") or [], 1)
    ]


def _accepted_versions(work: Work) -> list[ArtifactVersion]:
    return (
        ArtifactVersion.query.join(Artifact)
        .filter(Artifact.work_id == work.id, ArtifactVersion.status == "ACCEPTED")
        .order_by(ArtifactVersion.id).all()
    )


def _work_has_authoritative_acceptance(work: Work) -> bool:
    if work.state != "ACCEPTED":
        return False
    versions = _accepted_versions(work)
    if not versions:
        return False
    # Delivery Work must have durable verification proof; the Work state alone
    # is not enough to satisfy a Project contract.
    return any(
        VerificationRecord.query.filter_by(
            work_id=work.id, artifact_version_id=version.id, status="PASSED"
        ).count() > 0
        for version in versions
    )


def accepted_delivery(project: Project) -> list[Work]:
    rows = Work.query.filter_by(project_id=project.id).order_by(Work.id).all()
    return [
        row for row in rows
        if row.work_type != "MANAGEMENT" and _work_has_authoritative_acceptance(row)
    ]


def accepted_delivery_producer_ids(project: Project) -> set[int]:
    """Employees who produced accepted delivery evidence for this Project.

    Final Project semantic review must not be assigned to one of these producers;
    the reviewer is validating the Company's combined outcome, not self-attesting
    an Artifact it helped create.
    """
    producer_ids: set[int] = set()
    for work in accepted_delivery(project):
        for version in _accepted_versions(work):
            if version.producer_employee_id:
                producer_ids.add(int(version.producer_employee_id))
    return producer_ids


def select_outcome_reviewer(project: Project, fallback_ceo=None):
    """Choose an active non-producer reviewer for Project outcome evidence."""
    Employee = __import__("eason_one.models", fromlist=["Employee"]).Employee
    capabilities = __import__(
        "eason_one.services.team_formation", fromlist=["employee_capabilities"]
    )
    producers = accepted_delivery_producer_ids(project)
    candidates = []
    for employee in Employee.query.filter_by(active=True).order_by(Employee.id).all():
        if employee.id in producers or employee.slug == "ceo":
            continue
        if "CRITICAL_REVIEW" not in capabilities.employee_capabilities(employee):
            continue
        direct = 1 if employee.slug == "critic" else 0
        candidates.append((direct, -employee.id, employee))
    if candidates:
        candidates.sort(reverse=True)
        return candidates[0][-1]
    ceo = fallback_ceo or Employee.query.filter_by(slug="ceo", active=True).first()
    if ceo and ceo.id not in producers:
        return ceo
    return None


def accepted_output_snapshot(project: Project) -> list[dict]:
    """Return compact, lineage-bound company outputs for Founder/result synthesis."""
    Employee = __import__("eason_one.models", fromlist=["Employee"]).Employee
    rows = []
    for work in accepted_delivery(project):
        versions = _accepted_versions(work)
        for version in versions:
            proofs = VerificationRecord.query.filter_by(
                work_id=work.id, artifact_version_id=version.id, status="PASSED"
            ).order_by(VerificationRecord.id).all()
            producer = db.session.get(Employee, version.producer_employee_id) if version.producer_employee_id else None
            producing_run = db.session.get(AgentRun, version.execution_id) if version.execution_id else None
            producing_context = dict(getattr(producing_run, "context_composition_json", None) or {})
            input_artifact_lineage = list(producing_context.get("input_artifact_lineage") or [])
            if not input_artifact_lineage:
                input_artifact_lineage = list(((producing_context.get("operation_context") or {}).get("handoff_lineage") or []))
            provider_sources = [
                row for row in (producing_context.get("provider_sources") or [])
                if isinstance(row, dict) and str(row.get("url") or "").strip()
            ]
            rows.append({
                "work_id": work.id,
                "work_title": work.title,
                "employee_id": getattr(producer, "id", None),
                "employee_name": getattr(producer, "name", None),
                "artifact_id": version.artifact_id,
                "artifact_version_id": version.id,
                "artifact_content_hash": version.content_hash,
                "content": str(version.content_text or version.content_location or "")[:3000],
                "provider_sources": provider_sources[:20],
                "input_artifact_lineage": input_artifact_lineage[:20],
                "verification_ids": [proof.id for proof in proofs],
                "verification_methods": [proof.method for proof in proofs],
            })
    return rows


def project_outcome_summary(project: Project, evaluation: dict) -> str:
    """Use durable semantic review summary when available, else deterministic evidence summary."""
    review = latest_project_review(
        project.id, contract_hash=evaluation.get("contract_hash"),
        input_hash=review_input_hash(project),
    )
    payload = dict(getattr(review, "parsed_output_json", None) or {}) if review else {}
    summary = " ".join(str(payload.get("summary") or "").split()).strip()
    if summary:
        return summary[:1400]
    outputs = accepted_output_snapshot(project)
    if not outputs:
        return "Project Contract is satisfied by durable company evidence."
    people = []
    for row in outputs:
        name = row.get("employee_name")
        if name and name not in people:
            people.append(name)
    return (
        f"{len(outputs)} accepted company output(s) from {', '.join(people) if people else 'the Project team'} "
        "collectively satisfy the Founder-approved Project Contract."
    )[:1400]


def latest_project_review(
    project_id: int, *, contract_hash: str | None = None, input_hash: str | None = None
) -> AgentRun | None:
    rows = (
        AgentRun.query.filter_by(
            project_id=project_id, purpose="PROJECT_OUTCOME_REVIEW", status="SUCCEEDED"
        ).order_by(AgentRun.id.desc()).all()
    )
    for row in rows:
        context = dict(row.context_composition_json or {})
        if contract_hash is not None and str(context.get("contract_hash") or "") != str(contract_hash):
            continue
        if input_hash is not None and str(context.get("project_outcome_input_hash") or "") != str(input_hash):
            continue
        return row
    return None


def _semantic_results(
    run: AgentRun | None, contract: dict, *, expected_input_hash: str | None = None
) -> dict[str, dict]:
    if not run:
        return {}
    context = dict(run.context_composition_json or {})
    if str(context.get("contract_hash") or "") != str(_contract_hash(contract) or ""):
        return {}
    if expected_input_hash is not None and str(context.get("project_outcome_input_hash") or "") != str(expected_input_hash):
        return {}
    payload = run.parsed_output_json or {}
    rows = payload.get("criteria") or []
    records = criterion_records(contract)
    by_text = {_norm(item["text"]): item["id"] for item in records}
    valid_ids = {item["id"] for item in records}
    result: dict[str, dict] = {}
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue
            criterion_id = str(row.get("criterion_id") or "")
            # Compatibility for already persisted v1 reviews. Their proof is
            # still bound to the current input/Contract elsewhere; map exact
            # historical criterion text once, then keep all new reviews id-based.
            if criterion_id not in valid_ids and row.get("criterion"):
                criterion_id = by_text.get(_norm(row.get("criterion")), "")
            if criterion_id in valid_ids:
                result[criterion_id] = row
    return result



def _project_host_verification_results(project: Project) -> dict[str, dict]:
    """Return current-contract deterministic Project proof keyed by criterion.

    A historical PASSED row is not automatically current proof.  The exact
    ArtifactVersion/content hash named by the proof must still be an ACCEPTED
    version owned by an authoritatively accepted delivery Work in this Project.
    This keeps Project Result truth bound to current Company evidence rather
    than a stale host observation left behind by later reconciliation.
    """
    contract = get_project_contract(project)
    contract_hash = _contract_hash(contract)
    result: dict[str, dict] = {}
    rows = (
        VerificationRecord.query.join(Work, VerificationRecord.work_id == Work.id)
        .filter(
            Work.project_id == project.id,
            VerificationRecord.method == "PROJECT_HOST_HTTP_REVERIFY",
            VerificationRecord.status == "PASSED",
        )
        .order_by(VerificationRecord.id.desc()).all()
    )
    for row in rows:
        details = dict(row.details_json or {})
        if details.get("project_contract_hash") != contract_hash:
            continue
        proof_work_id = int(details.get("work_id") or 0)
        proof_version_id = int(details.get("artifact_version_id") or 0)
        proof_hash = str(details.get("artifact_content_hash") or "")
        work = db.session.get(Work, proof_work_id) if proof_work_id else None
        version = db.session.get(ArtifactVersion, proof_version_id) if proof_version_id else None
        if (
            not work
            or work.project_id != project.id
            or not _work_has_authoritative_acceptance(work)
            or not version
            or version.artifact.work_id != work.id
            or version.status != "ACCEPTED"
            or str(version.content_hash or "") != proof_hash
        ):
            continue
        criterion_id = str(details.get("criterion_id") or "")
        if not criterion_id:
            # Compatibility for r7 and earlier proof rows.
            criterion = str(details.get("criterion") or "")
            mapping = {_norm(item["text"]): item["id"] for item in criterion_records(contract)}
            criterion_id = mapping.get(_norm(criterion), "")
        if criterion_id and criterion_id not in result:
            result[criterion_id] = details
    return result


def _accepted_repository_artifact(project: Project):
    """Return one accepted code artifact that still points at the local repo."""
    fallback = Path.cwd()
    fallback_is_repo = (fallback / "pyproject.toml").exists() and (fallback / "eason_one").is_dir()
    for work in reversed(accepted_delivery(project)):
        boundary = dict((work.runtime_control_json or {}).get("codex_execution_boundary") or {})
        boundary_repo = str(boundary.get("repo_path") or "").strip()
        for version in reversed(_accepted_versions(work)):
            if boundary_repo and getattr(version.artifact, "artifact_type", None) == "CODE_CHANGE":
                try:
                    repo = Path(boundary_repo)
                    if repo.exists() and repo.is_dir():
                        return work, version, repo
                except OSError:
                    pass
            location = str(version.content_location or "").strip()
            if location:
                try:
                    target = Path(location)
                    if target.exists() and target.is_dir():
                        return work, version, target
                    if target.exists() and target.is_file():
                        for parent in target.parents:
                            if (parent / ".git").exists():
                                return work, version, parent
                except OSError:
                    pass
            if fallback_is_repo and getattr(version.artifact, "artifact_type", None) == "CODE_CHANGE":
                return work, version, fallback
    return None


def refresh_deterministic_http_evidence(project: Project | int) -> dict:
    """Prove Project HTTP criteria locally before buying another Employee run.

    This is Project-level verification of an already accepted repository
    artifact.  It does not mutate product code and it does not create Founder
    authority.  The verifier uses an isolated real loopback HTTP server, so a
    missing "actual HTTP" proof is not delegated back to Codex as paid work.
    """
    if isinstance(project, int):
        project = db.session.get(Project, project)
    if not project:
        raise ValueError("PROJECT_NOT_FOUND")
    contract = get_project_contract(project)
    existing = _project_host_verification_results(project)
    artifact_row = _accepted_repository_artifact(project)
    if not artifact_row:
        return {"status": "NO_ACCEPTED_REPOSITORY_ARTIFACT", "project_id": project.id, "new_proofs": 0}
    work, version, repo = artifact_row
    host = __import__("eason_one.services.host_validation", fromlist=["_run_http_contract"])
    acceptance = __import__("eason_one.services.acceptance_contract", fromlist=["classify_criterion"])
    new_proofs = 0
    rows = []
    project_context = "\n".join(filter(None, [
        str(contract.get("objective") or ""),
        *[str(value) for value in (contract.get("success_criteria") or [])],
        *[str(value) for value in (contract.get("constraints") or [])],
    ]))
    for criterion_record in criterion_records(contract):
        criterion_id = criterion_record["id"]
        criterion = criterion_record["text"]
        if acceptance.classify_criterion(criterion, task_context=project_context) != "HOST_HTTP_CONTRACT":
            continue
        if criterion_id in existing:
            rows.append({"criterion_id": criterion_id, "criterion": criterion, "status": "REUSED"})
            continue
        # Explicit endpoint criteria carry their own path. Response-only criteria
        # borrow the single unambiguous endpoint from the complete frozen Project
        # context; this mirrors Work-level proof ownership instead of forcing a
        # paid semantic Project review.
        explicit_http = __import__("re").search(
            r"\b(?:GET|POST|PUT|PATCH|DELETE)\s+/[A-Za-z0-9_./<>:-]+",
            str(criterion), flags=__import__("re").IGNORECASE,
        )
        probe_text = str(criterion) if explicit_http else str(criterion) + "\n" + project_context
        probe = SimpleNamespace(
            project=None, operation=None,
            title="", objective="", acceptance_criteria=probe_text,
        )
        check = host._run_http_contract(probe, repo)
        if not check or not check.get("success"):
            rows.append({
                "criterion_id": criterion_id, "criterion": criterion, "status": "FAILED",
                "failure_reason": (check or {}).get("failure_reason"),
                "error": (check or {}).get("error"),
            })
            continue
        details = {
            "project_contract_hash": _contract_hash(contract),
            "criterion_id": criterion_id,
            "criterion": criterion,
            "proof_owner": "HOST_HTTP_CONTRACT",
            "transport": ((check.get("observation") or {}).get("transport") or "REAL_LOOPBACK_HTTP"),
            "observation": check.get("observation") or {},
            "contract": check.get("contract") or {},
            "test": check.get("test") or {},
            "artifact_content_hash": version.content_hash,
            "artifact_version_id": version.id,
            "work_id": work.id,
            "repository": str(repo),
        }
        db.session.add(VerificationRecord(
            work_id=work.id,
            artifact_version_id=version.id,
            verifier_employee_id=None,
            agent_run_id=None,
            method="PROJECT_HOST_HTTP_REVERIFY",
            status="PASSED",
            details_json=details,
        ))
        emit(
            "PROJECT_HOST_HTTP_REVERIFIED", actor_type="RUNTIME",
            project_id=project.id, work_id=work.id, artifact_id=version.artifact_id,
            correlation_id=f"project:{project.id}",
            payload={
                "criterion_id": criterion_id,
                "criterion": criterion,
                "project_contract_hash": _contract_hash(contract),
                "artifact_version_id": version.id,
                "transport": details["transport"],
            },
        )
        new_proofs += 1
        rows.append({"criterion_id": criterion_id, "criterion": criterion, "status": "PASSED", "observation": details["observation"]})
    if new_proofs:
        db.session.commit()
    return {"status": "REFRESHED", "project_id": project.id, "new_proofs": new_proofs, "results": rows}

def evaluate(project: Project | int) -> dict:
    if isinstance(project, int):
        project = db.session.get(Project, project)
    if not project:
        raise ValueError("PROJECT_NOT_FOUND")
    contract = get_project_contract(project)
    records = criterion_records(contract)
    accepted = accepted_delivery(project)
    current_review_input_hash = review_input_hash(project)
    semantic = _semantic_results(
        latest_project_review(
            project.id, contract_hash=_contract_hash(contract),
            input_hash=current_review_input_hash,
        ),
        contract, expected_input_hash=current_review_input_hash,
    )
    host_project = _project_host_verification_results(project)
    result_rows = []
    for criterion_record in records:
        criterion_id = criterion_record["id"]
        criterion = criterion_record["text"]
        # Project completion is never inferred from a model paraphrase of Work
        # acceptance text. Deterministic proof is bound by criterion_id and
        # semantic proof is produced by the Project-level reviewer against the
        # immutable Contract. Exact-text Work matching remains compatibility
        # evidence only and cannot close the Project on its own.
        host_proof = host_project.get(criterion_id)
        if host_proof:
            result_rows.append({
                "criterion_id": criterion_id, "criterion": criterion, "status": "SATISFIED",
                "method": "PROJECT_HOST_HTTP_REVERIFY",
                "evidence": json.dumps(host_proof.get("observation") or {}, ensure_ascii=False),
                "work_ids": [int(host_proof.get("work_id"))] if host_proof.get("work_id") else [],
            })
            continue
        reviewed = semantic.get(criterion_id)
        if reviewed and reviewed.get("status") == "SATISFIED":
            result_rows.append({
                "criterion_id": criterion_id, "criterion": criterion, "status": "SATISFIED", "method": "PROJECT_OUTCOME_REVIEW",
                "evidence": reviewed.get("evidence"), "work_ids": reviewed.get("work_ids") or [],
            })
        elif reviewed and reviewed.get("status") == "NOT_SATISFIED":
            result_rows.append({
                "criterion_id": criterion_id, "criterion": criterion, "status": "NOT_SATISFIED", "method": "PROJECT_OUTCOME_REVIEW",
                "evidence": reviewed.get("evidence"), "work_ids": reviewed.get("work_ids") or [],
            })
        else:
            result_rows.append({
                "criterion_id": criterion_id, "criterion": criterion, "status": "UNPROVEN", "method": "MISSING_PROJECT_PROOF",
                "work_ids": [],
            })
    statuses = {row["status"] for row in result_rows}
    overall = (
        "SATISFIED" if result_rows and statuses == {"SATISFIED"}
        else "NOT_SATISFIED" if "NOT_SATISFIED" in statuses
        else "INSUFFICIENT_EVIDENCE"
    )
    authority = assert_authority_ledger(project)
    return {
        "project_id": project.id,
        "contract_hash": _contract_hash(contract),
        "authority_hash": authority.get("authority_hash"),
        "effective_budget_limit_twd": authority.get("effective_budget_limit_twd"),
        "overall_status": overall,
        "criteria": result_rows,
        "accepted_work_ids": [row.id for row in accepted],
    }


def evidence_packet(project: Project) -> str:
    contract = get_project_contract(project)
    accepted = accepted_delivery(project)
    rows = [
        f"PROJECT #{project.id}: {project.name}",
        f"OBJECTIVE: {contract.get('objective')}",
        "FOUNDER SUCCESS CRITERIA:",
        *[f"[{item['id']}] {item['text']}" for item in criterion_records(contract)],
        "CONSTRAINTS:",
        *[f"- {value}" for value in contract.get("constraints") or []],
        "ACCEPTED WORK EVIDENCE:",
    ]
    for work in accepted:
        rows.append(f"WORK #{work.id}: {work.title}")
        rows.append("Acceptance: " + str(work.acceptance_criteria or "-"))
        for version in _accepted_versions(work):
            content = " ".join(str(version.content_text or "").split())[:1600]
            rows.append(
                f"ArtifactVersion #{version.id}; hash={version.content_hash}; location={version.content_location or '-'}; content={content or '-'}"
            )
            producing_run = db.session.get(AgentRun, version.execution_id) if version.execution_id else None
            producing_context = dict(getattr(producing_run, "context_composition_json", None) or {})
            provider_sources = [
                row for row in (producing_context.get("provider_sources") or [])
                if isinstance(row, dict) and str(row.get("url") or "").strip()
            ]
            input_lineage = list(producing_context.get("input_artifact_lineage") or [])
            if not input_lineage:
                input_lineage = list(((producing_context.get("operation_context") or {}).get("handoff_lineage") or []))
            if input_lineage:
                rows.append("Accepted upstream Company Artifact lineage consumed by this output:")
                for upstream in input_lineage[:20]:
                    rows.append(
                        f"- depth={upstream.get('dependency_depth', 1)}; Work #{upstream.get('work_id')}; "
                        f"ArtifactVersion #{upstream.get('artifact_version_id')}; hash={upstream.get('artifact_content_hash')}; "
                        f"verification={upstream.get('verification_record_ids') or '-'}"
                    )
                    for source in (upstream.get("provider_sources") or [])[:8]:
                        if isinstance(source, dict) and str(source.get("url") or "").strip():
                            rows.append(
                                f"  source: {str(source.get('title') or 'Source').strip()}: {str(source.get('url') or '').strip()}"
                            )
            if provider_sources:
                rows.append("Provider-observed source lineage for this accepted ArtifactVersion:")
                for source in provider_sources[:20]:
                    rows.append(
                        f"- {str(source.get('title') or 'Source').strip()}: {str(source.get('url') or '').strip()}"
                    )
    host_proofs = _project_host_verification_results(project)
    if host_proofs:
        rows.append("PROJECT-LEVEL DETERMINISTIC HOST PROOF:")
        for proof in host_proofs.values():
            rows.append(
                f"- {proof.get('criterion')}: PASSED via {proof.get('transport')}; "
                f"observation={json.dumps(proof.get('observation') or {}, ensure_ascii=False)[:1600]}"
            )
    return "\n".join(rows)[:18000]


def review_input_hash(project: Project) -> str:
    """Hash semantic-review authority inputs, excluding additive host proof.

    A later deterministic HTTP proof must not invalidate an already-valid
    semantic judgment over the same accepted Artifacts. Accepted Artifact
    lineage or governing Founder terms *do* invalidate it.
    """
    contract = get_project_contract(project)
    accepted = []
    for work in accepted_delivery(project):
        version_rows = []
        for version in _accepted_versions(work):
            producing_run = db.session.get(AgentRun, version.execution_id) if version.execution_id else None
            producing_context = dict(getattr(producing_run, "context_composition_json", None) or {})
            provider_sources = [
                {
                    "title": str(row.get("title") or "").strip(),
                    "url": str(row.get("url") or "").strip(),
                }
                for row in (producing_context.get("provider_sources") or [])
                if isinstance(row, dict) and str(row.get("url") or "").strip()
            ]
            input_lineage = list(producing_context.get("input_artifact_lineage") or [])
            if not input_lineage:
                input_lineage = list(((producing_context.get("operation_context") or {}).get("handoff_lineage") or []))
            lineage_fingerprint = []
            for upstream in input_lineage[:20]:
                if not isinstance(upstream, dict):
                    continue
                lineage_fingerprint.append({
                    "dependency_depth": upstream.get("dependency_depth"),
                    "work_id": upstream.get("work_id"),
                    "artifact_version_id": upstream.get("artifact_version_id"),
                    "artifact_content_hash": upstream.get("artifact_content_hash"),
                    "verification_record_ids": list(upstream.get("verification_record_ids") or [])[:20],
                    "provider_sources": [
                        {
                            "title": str(source.get("title") or "").strip(),
                            "url": str(source.get("url") or "").strip(),
                        }
                        for source in (upstream.get("provider_sources") or [])[:20]
                        if isinstance(source, dict) and str(source.get("url") or "").strip()
                    ],
                })
            version_rows.append({
                "id": version.id,
                "content_hash": version.content_hash,
                "provider_sources": provider_sources[:20],
                "input_artifact_lineage": lineage_fingerprint,
            })
        accepted.append({
            "work_id": work.id,
            "artifact_versions": version_rows,
        })
    body = {
        "version": "PROJECT_SEMANTIC_REVIEW_BASIS_V2",
        "project_id": project.id,
        "contract_hash": _contract_hash(contract),
        "accepted": accepted,
    }
    return hashlib.sha256(
        json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def outcome_basis_hash(project: Project, evaluation: dict | None = None) -> str:
    """Fingerprint the exact Company evidence that makes Result Ready true.

    Project Contract and budget authority hashes are necessary but not enough:
    accepted Artifact/source lineage or deterministic host proof can change
    during reconciliation without changing Founder terms.  The Result proof
    therefore binds those current evidence inputs as well.
    """
    evaluation = evaluation or evaluate(project)
    host = _project_host_verification_results(project)
    host_rows = []
    for criterion_id, details in sorted(host.items()):
        host_rows.append({
            "criterion_id": criterion_id,
            "work_id": details.get("work_id"),
            "artifact_version_id": details.get("artifact_version_id"),
            "artifact_content_hash": details.get("artifact_content_hash"),
            "transport": details.get("transport"),
            "observation": details.get("observation") or {},
        })
    criteria = [{
        "criterion_id": row.get("criterion_id"),
        "status": row.get("status"),
        "method": row.get("method"),
        "work_ids": list(row.get("work_ids") or []),
        "evidence": row.get("evidence"),
    } for row in (evaluation.get("criteria") or [])]
    body = {
        "version": "PROJECT_RESULT_BASIS_V1",
        "project_id": project.id,
        "contract_hash": evaluation.get("contract_hash"),
        "authority_hash": evaluation.get("authority_hash"),
        "semantic_review_basis_hash": review_input_hash(project),
        "criteria": criteria,
        "host_proofs": host_rows,
    }
    return hashlib.sha256(
        json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def needs_semantic_review(project: Project) -> bool:
    current = evaluate(project)
    return any(row["method"] == "MISSING_PROJECT_PROOF" for row in current["criteria"])


def _persist_result_artifact(project: Project, evaluation: dict, summary: dict) -> tuple[int | None, int | None]:
    """Persist the verified Project result as durable management evidence.

    This Artifact is a projection of already-proven Project truth, not a second
    source of acceptance authority. Its verification record binds the exact
    immutable Project Contract hash and criterion result set used at closure.
    """
    operation = (
        Operation.query.filter_by(project_id=project.id)
        .filter(Operation.approved_at.isnot(None))
        .order_by(Operation.id.desc()).first()
    )
    if not operation:
        return None, None
    management = __import__(
        "eason_one.services.work_runtime", fromlist=["ensure_management_work"]
    ).ensure_management_work(operation)
    artifact = Artifact.query.filter_by(
        work_id=management.id, artifact_type="PROJECT_RESULT"
    ).order_by(Artifact.id).first()
    if not artifact:
        artifact = Artifact(
            project_id=project.id, work_id=management.id,
            artifact_type="PROJECT_RESULT", title=f"Verified result: {project.name}"[:220],
        )
        db.session.add(artifact)
        db.session.flush()

    basis_hash = outcome_basis_hash(project, evaluation)
    content = json.dumps({
        "project_id": project.id,
        "project_name": project.name,
        "project_contract_hash": evaluation.get("contract_hash"),
        "project_authority_hash": evaluation.get("authority_hash"),
        "project_outcome_basis_hash": basis_hash,
        "criterion_results": evaluation.get("criteria") or [],
        "accepted_work_ids": evaluation.get("accepted_work_ids") or [],
        "accepted_outputs": accepted_output_snapshot(project),
        "founder_summary": summary,
    }, ensure_ascii=False, sort_keys=True, indent=2)
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    version = ArtifactVersion.query.filter_by(
        artifact_id=artifact.id, content_hash=content_hash
    ).order_by(ArtifactVersion.id.desc()).first()
    if not version:
        version_no = (
            db.session.query(func.coalesce(func.max(ArtifactVersion.version), 0))
            .filter(ArtifactVersion.artifact_id == artifact.id).scalar() or 0
        ) + 1
        version = ArtifactVersion(
            artifact_id=artifact.id, version=version_no,
            # PROJECT_RESULT is a deterministic Company projection of accepted
            # multi-Employee evidence. Do not misattribute it to the CEO merely
            # because the CEO owns Founder communication.
            producer_employee_id=None,
            status="SUBMITTED", content_text=content, content_hash=content_hash,
        )
        db.session.add(version)
        db.session.flush()
        emit(
            "ARTIFACT_SUBMITTED", actor_type="RUNTIME", project_id=project.id,
            work_id=management.id, artifact_id=artifact.id,
            correlation_id=f"project:{project.id}",
            payload={"artifact_version_id": version.id, "version": version.version, "type": "PROJECT_RESULT"},
        )

    verification = VerificationRecord.query.filter_by(
        work_id=management.id, artifact_version_id=version.id,
        method="PROJECT_CONTRACT_OUTCOME", status="PASSED",
    ).order_by(VerificationRecord.id.desc()).first()
    if not verification:
        verification = VerificationRecord(
            work_id=management.id, artifact_version_id=version.id,
            verifier_employee_id=None, method="PROJECT_CONTRACT_OUTCOME", status="PASSED",
            details_json={
                "project_contract_hash": evaluation.get("contract_hash"),
                "project_authority_hash": evaluation.get("authority_hash"),
                "project_outcome_basis_hash": basis_hash,
                "criterion_results": evaluation.get("criteria") or [],
                "accepted_work_ids": evaluation.get("accepted_work_ids") or [],
            },
        )
        db.session.add(verification)
        db.session.flush()
    version.status = "ACCEPTED"
    version.accepted_at = version.accepted_at or now()
    # Management Work is historical/projection work. It may be accepted only
    # after Project truth is already SATISFIED, never the other way around.
    if management.state not in {"ACCEPTED", "ABANDONED", "CANCELLED"}:
        runtime = __import__("eason_one.services.work_runtime", fromlist=["transition"] )
        if management.state not in {"VERIFYING", "EXECUTING"}:
            # ensure_management_work currently creates EXECUTING; this guard is
            # for migrated historical rows. Illegal histories stay evidence-only.
            pass
        elif management.state != "VERIFYING":
            runtime.transition(management, "VERIFYING", reason="Project Contract outcome verified")
        if management.state == "VERIFYING":
            runtime.transition(management, "ACCEPTED", reason="Verified Project result persisted")
    return artifact.id, version.id


def close_result_ready(
    project: Project,
    evaluation: dict | None = None,
    *,
    management_decision_id: int | None = None,
) -> dict:
    """Persist Result Ready only from freshly recomputed Project truth.

    Callers may supply the evaluation they just observed for diagnostics, but it
    is never closure authority.  Re-read the current Contract/evidence basis at
    the commit boundary so a stale/fabricated evaluation cannot manufacture a
    Founder-ready result.
    """
    if project.status in TERMINAL_PROJECT:
        return {"status": "RESULT_READY", "project_id": project.id}
    governance = __import__(
        "eason_one.services.governance", fromlist=["attention"]
    )
    if governance.attention(project):
        raise ValueError("PROJECT_RESULT_READY_BLOCKED_BY_FOUNDER_AUTHORITY")
    active_delivery = Work.query.filter(
        Work.project_id == project.id,
        Work.work_type != "MANAGEMENT",
        Work.state.in_(["PROPOSED", "READY", "EXECUTING", "WAITING", "VERIFYING"]),
    ).count()
    if active_delivery:
        raise ValueError("PROJECT_RESULT_READY_BLOCKED_BY_ACTIVE_WORK")
    current_evaluation = evaluate(project)
    if current_evaluation.get("overall_status") != "SATISFIED":
        raise ValueError("PROJECT_CONTRACT_NOT_SATISFIED")
    evaluation = current_evaluation

    # REVIEW means the Project outcome is verified and ready for Founder review.
    # Founder may later archive/mark COMPLETED; CEO prose is not a commit point.
    project.status = "REVIEW"
    meaningful_summary = project_outcome_summary(project, evaluation)
    project.current_state_summary = meaningful_summary
    project.next_milestone = "Founder review of the verified Project outcome."
    spent = Decimal(
        db.session.query(func.coalesce(func.sum(CostEvent.real_cost_delta), 0))
        .filter(CostEvent.project_id == project.id).scalar() or 0
    )
    basis_hash = outcome_basis_hash(project, evaluation)
    summary = {
        "headline": "Project result is ready.",
        "summary": project.current_state_summary,
        "result": f"Verified from {len(accepted_output_snapshot(project))} accepted team output(s) against the immutable Founder Project Contract.",
        "next_move": project.next_milestone,
        "cost_twd": str(spent),
        "verification": "PROJECT_CONTRACT_SATISFIED",
        "project_contract_hash": evaluation.get("contract_hash"),
        "project_authority_hash": evaluation.get("authority_hash"),
        "project_outcome_basis_hash": basis_hash,
        "criterion_results": evaluation.get("criteria") or [],
    }
    artifact_id, artifact_version_id = _persist_result_artifact(project, evaluation, summary)
    summary["project_result_artifact_id"] = artifact_id
    summary["project_result_artifact_version_id"] = artifact_version_id

    # Older Founder screens may read operation.founder_report_json, but this is
    # now explicitly a projection. Operation status cannot grant Project closure.
    operations = (
        Operation.query.filter_by(project_id=project.id)
        .filter(Operation.approved_at.isnot(None))
        .order_by(Operation.id).all()
    )
    for operation in operations:
        # Founder Result Ready is Project truth, not permission to rewrite Mission
        # history. FAILED/CANCELLED/COMPLETED and other Mission lifecycle states
        # remain exactly as they happened; older Founder screens may read only
        # this compatibility result projection.
        operation.founder_report_json = summary

    ceo = project.owner
    message_text = summary["summary"] + "\n\n" + summary["result"]
    if ceo and not WorkMessage.query.filter_by(
        project_id=project.id, message_type="CEO_TO_FOUNDER", content=message_text
    ).first():
        db.session.add(WorkMessage(
            project_id=project.id, sender_employee_id=ceo.id,
            message_type="CEO_TO_FOUNDER", content=message_text,
        ))
    existing = CompanyEvent.query.filter_by(
        event_type="PROJECT_RESULT_READY", project_id=project.id
    ).order_by(CompanyEvent.id.desc()).first()
    if (
        not existing
        or (existing.payload_json or {}).get("contract_hash") != evaluation.get("contract_hash")
        or (existing.payload_json or {}).get("project_outcome_basis_hash") != basis_hash
    ):
        emit(
            "PROJECT_RESULT_READY", actor_type="RUNTIME", project_id=project.id,
            correlation_id=f"project:{project.id}",
            payload={
                "contract_hash": evaluation.get("contract_hash"),
                "authority_hash": evaluation.get("authority_hash"),
                "project_outcome_basis_hash": basis_hash,
                "ceo_management_decision_id": management_decision_id,
                "criterion_results": evaluation.get("criteria") or [],
                "accepted_work_ids": evaluation.get("accepted_work_ids") or [],
                "project_result_artifact_id": artifact_id,
                "project_result_artifact_version_id": artifact_version_id,
            },
        )
    db.session.commit()
    return {
        "status": "RESULT_READY", "project_id": project.id, "evaluation": evaluation,
        "artifact_id": artifact_id, "artifact_version_id": artifact_version_id,
    }


def result_ready_proof(project: Project | int) -> dict | None:
    """Return Project Result proof bound to current Contract, authority and evidence."""
    if isinstance(project, int):
        project = db.session.get(Project, project)
    if not project:
        return None
    contract = get_project_contract(project, require=False)
    if not contract:
        return None
    authority = assert_authority_ledger(project)
    current_evaluation = evaluate(project)
    if current_evaluation.get("overall_status") != "SATISFIED":
        return None
    current_basis_hash = outcome_basis_hash(project, current_evaluation)
    artifacts = (
        Artifact.query.filter_by(project_id=project.id, artifact_type="PROJECT_RESULT")
        .order_by(Artifact.id.desc()).all()
    )
    for artifact in artifacts:
        versions = (
            ArtifactVersion.query.filter_by(artifact_id=artifact.id, status="ACCEPTED")
            .order_by(ArtifactVersion.version.desc(), ArtifactVersion.id.desc()).all()
        )
        for version in versions:
            proofs = VerificationRecord.query.filter_by(
                work_id=artifact.work_id, artifact_version_id=version.id,
                method="PROJECT_CONTRACT_OUTCOME", status="PASSED",
            ).order_by(VerificationRecord.id.desc()).all()
            for proof in proofs:
                details = dict(proof.details_json or {})
                if (
                    details.get("project_contract_hash") == _contract_hash(contract)
                    and details.get("project_authority_hash") == authority.get("authority_hash")
                    and details.get("project_outcome_basis_hash") == current_basis_hash
                ):
                    return {
                        "artifact": artifact, "version": version, "verification": proof,
                        "contract_hash": _contract_hash(contract),
                        "authority_hash": authority.get("authority_hash"),
                        "outcome_basis_hash": current_basis_hash,
                    }
    return None


FOUNDER_PROJECT_COMPLETION_SCHEMA = "FOUNDER_PROJECT_COMPLETION_V1"


def _founder_completion_decision(project_id: int) -> Decision | None:
    return Decision.query.filter_by(
        legacy_source="PROJECT_RESULT_ACCEPTANCE", legacy_source_id=int(project_id)
    ).first()


def _completion_basis(decision: Decision) -> dict:
    try:
        payload = json.loads(decision.authority_basis or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        payload = {}
    return payload if isinstance(payload, dict) else {}


def _founder_completion_event(project_id: int, decision_id: int) -> CompanyEvent | None:
    return CompanyEvent.query.filter_by(
        event_type="FOUNDER_PROJECT_COMPLETED",
        project_id=int(project_id),
        decision_id=int(decision_id),
    ).order_by(CompanyEvent.id).first()


def _canonical_completion_replay(project: Project) -> dict | None:
    """Return the already-committed Founder completion without minting new truth.

    COMPLETED is terminal Founder authority. A browser retry, network retry, or
    restart replay must therefore read the exact existing Decision/event pair
    instead of trying to complete the Project again. Legacy/corrupt terminal
    rows without that pair fail closed rather than being silently blessed.
    """
    if str(project.status or "").upper() != "COMPLETED":
        return None
    completion = _founder_completion_decision(project.id)
    if completion is None or completion.state != "COMMITTED" or completion.decision != "ACCEPT_AND_COMPLETE":
        raise ValueError("PROJECT_COMPLETION_AUTHORITY_PROOF_MISSING")
    basis = _completion_basis(completion)
    if (
        basis.get("version") != FOUNDER_PROJECT_COMPLETION_SCHEMA
        or basis.get("authority") != "FOUNDER"
    ):
        raise ValueError("PROJECT_COMPLETION_AUTHORITY_PROOF_INVALID")
    event = _founder_completion_event(project.id, completion.id)
    if event is None:
        raise ValueError("PROJECT_COMPLETION_AUDIT_EVENT_MISSING")
    payload = dict(event.payload_json or {})
    expected = {
        "project_contract_hash": basis.get("governing_contract_hash"),
        "project_authority_hash": basis.get("budget_authority_hash"),
        "project_result_artifact_id": basis.get("project_result_artifact_id"),
        "project_result_artifact_version_id": basis.get("project_result_artifact_version_id"),
        "project_result_verification_id": basis.get("project_result_verification_id"),
    }
    if any(payload.get(key) != value for key, value in expected.items()):
        raise ValueError("PROJECT_COMPLETION_AUDIT_PROOF_MISMATCH")
    return {
        "status": "ALREADY_COMPLETED",
        "project_id": project.id,
        "artifact_id": basis.get("project_result_artifact_id"),
        "artifact_version_id": basis.get("project_result_artifact_version_id"),
        "verification_id": basis.get("project_result_verification_id"),
        "completion_decision_id": completion.id,
        "completion_event_id": event.id,
        "replayed": True,
    }


def accept_founder_completion(project: Project | int) -> dict:
    """Commit or safely replay explicit Founder acceptance of verified Result truth."""
    if isinstance(project, int):
        project = db.session.get(Project, project)
    if not project:
        raise ValueError("PROJECT_NOT_FOUND")

    replay = _canonical_completion_replay(project)
    if replay is not None:
        return replay
    if project.status != "REVIEW":
        raise ValueError("PROJECT_NOT_RESULT_READY")

    # A committed Founder-completion Decision while the Project is not terminal
    # would violate the atomic Decision -> lifecycle -> event contract. Do not
    # reuse or overwrite such inconsistent truth.
    if _founder_completion_decision(project.id) is not None:
        raise ValueError("PROJECT_COMPLETION_STATE_INCONSISTENT")

    proof = result_ready_proof(project)
    if not proof:
        raise ValueError("PROJECT_RESULT_PROOF_MISSING")
    current = evaluate(project)
    if current.get("overall_status") != "SATISFIED":
        raise ValueError("PROJECT_CONTRACT_NO_LONGER_SATISFIED")

    project.status = "COMPLETED"
    project.current_state_summary = "Founder accepted the verified Project result as complete."
    project.next_milestone = None

    # Founder final acceptance is first-class Decision truth, bound to the exact
    # current governing Contract, budget authority, Result Artifact and
    # Verification proof. KnowledgeItem below remains a compatibility/audit
    # projection, not the governing decision. Decision + lifecycle + terminal
    # source event settle in one DB transaction; closure learning consumes the
    # durable event later and is never itself completion authority.
    basis = {
        "version": FOUNDER_PROJECT_COMPLETION_SCHEMA,
        "authority": "FOUNDER",
        "governing_contract_hash": proof["contract_hash"],
        "budget_authority_hash": proof["authority_hash"],
        "project_result_artifact_id": proof["artifact"].id,
        "project_result_artifact_version_id": proof["version"].id,
        "project_result_verification_id": proof["verification"].id,
    }
    completion = Decision(
        legacy_source="PROJECT_RESULT_ACCEPTANCE", legacy_source_id=project.id,
        project_id=project.id, work_id=proof["artifact"].work_id,
        proposed_by_employee_id=project.owner_employee_id, decided_by_employee_id=None,
        question=f"Accept verified Project #{project.id} result?",
        decision="ACCEPT_AND_COMPLETE",
        rationale="Founder accepted the Result already verified against the current governing Project Contract.",
        state="COMMITTED", authority_basis=json.dumps(basis, ensure_ascii=False, sort_keys=True),
        committed_at=now(),
    )
    db.session.add(completion); db.session.flush()
    if not KnowledgeItem.query.filter_by(
        project_id=project.id, kind="DECISION", title="Founder accepted Project result",
        founder_approved=True,
    ).first():
        db.session.add(KnowledgeItem(
            project_id=project.id, kind="DECISION",
            title="Founder accepted Project result",
            content=(proof["version"].content_text or "Verified Project Result accepted by Founder."),
            rationale="Founder accepted the Project Result already verified against the immutable Founder Project Contract.",
            source_ref=f"project:{project.id}:founder_acceptance:v020",
            origin_employee_id=project.owner_employee_id, founder_approved=True,
        ))
    completion_event = emit(
        "FOUNDER_PROJECT_COMPLETED", actor_type="FOUNDER", project_id=project.id,
        artifact_id=proof["artifact"].id, decision_id=completion.id,
        correlation_id=f"founder:project-completion:{project.id}:decision:{completion.id}",
        payload={
            "completion_schema": FOUNDER_PROJECT_COMPLETION_SCHEMA,
            "project_contract_hash": proof["contract_hash"],
            "project_authority_hash": proof["authority_hash"],
            "project_result_artifact_id": proof["artifact"].id,
            "project_result_artifact_version_id": proof["version"].id,
            "project_result_verification_id": proof["verification"].id,
        },
    )
    db.session.commit()
    return {
        "status": "COMPLETED", "project_id": project.id,
        "artifact_id": proof["artifact"].id, "artifact_version_id": proof["version"].id,
        "verification_id": proof["verification"].id,
        "completion_decision_id": completion.id,
        "completion_event_id": completion_event.id,
        "replayed": False,
    }


def mark_continuation_needed(project: Project, evaluation: dict, *, reason: str | None = None) -> dict:
    can_activate = __import__(
        "eason_one.services.work_runtime", fromlist=["project_can_activate"]
    ).project_can_activate(project)
    if project.status not in TERMINAL_PROJECT and can_activate:
        project.status = "ACTIVE"
    missing = [row["criterion"] for row in evaluation.get("criteria") or [] if row["status"] != "SATISFIED"]
    if can_activate:
        project.current_state_summary = reason or (
            "Project remains active. Accepted Mission evidence does not yet satisfy the full Founder Project Contract."
        )
        project.next_milestone = missing[0] if missing else "CEO will plan the next bounded Work within existing Founder authority."
    db.session.commit()
    return {"status": "CONTINUATION_REQUIRED", "project_id": project.id, "missing_criteria": missing}
