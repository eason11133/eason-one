"""Execution and verification owned by durable Work.

Task remains a compatibility adapter for the proven prompt/context/Codex code.
It mirrors Work state; it never decides which business step runs next.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path
import hashlib
import json

from ..extensions import db
from ..models import AgentRun, Artifact, ArtifactVersion, Employee, VerificationRecord, WorkMessage, now
from ..schemas import REVIEW_SCHEMA
from . import artifacts, work_runtime
from .context import build_with_composition
from .execution import execute
from .execution_policy import select_execution_model, select_retry_model, select_research_model
from .task_execution import run_task

_MAX_PROVIDER_FAILOVER_ATTEMPTS = 4
_MAX_TRANSIENT_PROVIDER_ATTEMPTS = 3
_MAX_DEFAULT_AUTOMATIC_ATTEMPTS = 2

_RETRYABLE = {
    "PROVIDER_PREFLIGHT_FAILED", "REFUSAL", "OUTPUT_TRUNCATED",
    "PROVIDER_INCOMPLETE", "PROVIDER_TRANSIENT_REJECTED", "PROVIDER_REQUEST_REJECTED",
    "STRUCTURED_OUTPUT_INVALID", "POSTPROCESS_FAILED",
    "RESEARCH_SOURCE_EVIDENCE_MISSING", "RESEARCH_TOOL_UNAVAILABLE",
    "CODEX_PROCESS_FAILED", "CODEX_VALIDATION_FAILED", "CODEX_READ_ONLY_ATTEMPTED_CHANGE",
}


def automatic_retry_allowed(run, task) -> bool:
    """Whether Company runtime still owns a bounded recovery attempt for this failure.

    OUTPUT_TRUNCATED plus both transient and definitive provider rejection are
    deliberately included. A definitive HTTP rejection proves that no completion
    was returned, so ExternalEffectAttempt marks it replay-safe. For a generic
    Employee the Company may therefore try one different governed model/provider
    instead of turning a provider-specific 4xx/configuration failure into a dead
    SYSTEM_RECOVERY wait. Dedicated provider-family Researchers still cannot
    cross provider because execution_policy will return no unauthorized alternate.
    Engineer/Codex keeps its own tool-specific bounded retry path.
    """
    reason = getattr(run, "failure_reason", None)
    if reason in {"PROVIDER_PREFLIGHT_FAILED", "PROVIDER_REQUEST_REJECTED", "RESEARCH_TOOL_UNAVAILABLE"}:
        max_attempts = _MAX_PROVIDER_FAILOVER_ATTEMPTS
    elif reason == "PROVIDER_TRANSIENT_REJECTED":
        max_attempts = _MAX_TRANSIENT_PROVIDER_ATTEMPTS
    else:
        max_attempts = _MAX_DEFAULT_AUTOMATIC_ATTEMPTS
    return bool(
        getattr(run, "outcome", None) in {"FAILED_SAFE", "FAILED_KNOWN"}
        and reason in _RETRYABLE
        and int(getattr(run, "attempt_number", 1) or 1) < max_attempts
        and not (getattr(task, "assigned_employee", None) and task.assigned_employee.slug == "engineer")
    )


def _retry_chain_model_ids(run) -> set[int]:
    """Exact model ids already tried in this replacement chain.

    Recovery may walk A -> B -> C, but it must never bounce A -> B -> A after a
    known rejection.  The durable retry_of_run lineage is authoritative and also
    survives process restart.
    """
    result: set[int] = set()
    seen: set[int] = set()
    current = run
    while current is not None and int(getattr(current, "id", 0) or 0) not in seen:
        run_id = int(getattr(current, "id", 0) or 0)
        if run_id:
            seen.add(run_id)
        model_id = getattr(current, "model_config_id", None)
        if model_id is not None:
            result.add(int(model_id))
        prior_id = getattr(current, "retry_of_run_id", None)
        current = db.session.get(AgentRun, prior_id) if prior_id else None
    return result


def _same_model_retry_already_used(run) -> bool:
    prior_id = getattr(run, "retry_of_run_id", None)
    if not prior_id:
        return False
    prior = db.session.get(AgentRun, prior_id)
    return bool(
        prior is not None
        and getattr(prior, "model_config_id", None) is not None
        and int(prior.model_config_id) == int(getattr(run, "model_config_id", -1) or -1)
    )


def automatic_recovery_model(work, task, failed_run):
    """Return the next lawful model for one Company-owned recovery step.

    This is intentionally selection-only: it never dispatches a provider.  It is
    shared by normal Work execution and restart SYSTEM_RECOVERY maintenance so a
    newly available lawful path can resume without Founder intervention.
    """
    if not automatic_retry_allowed(failed_run, task):
        return None
    primary_capability = __import__(
        "eason_one.services.team_formation", fromlist=["infer_primary_capability"]
    ).infer_primary_capability(work)[0]
    composition = dict(getattr(failed_run, "context_composition_json", None) or {})
    execution_mode = str(composition.get("execution_mode") or "").upper()
    live_research = bool(primary_capability == "RESEARCH" and execution_mode == "LIVE_WEB_RESEARCH")
    employee = task.assigned_employee

    # One same-model compact/transient retry is useful, but never repeat it again
    # after that exact model already replaced itself once.
    if (
        getattr(failed_run, "failure_reason", None) in {"OUTPUT_TRUNCATED", "PROVIDER_TRANSIENT_REJECTED"}
        and not _same_model_retry_already_used(failed_run)
    ):
        same_model = getattr(failed_run, "model_config", None)
        eligible = __import__(
            "eason_one.services.execution_policy", fromlist=["retry_model_eligible"]
        ).retry_model_eligible(
            employee, same_model, work.operation, web_search=live_research
        )
        if eligible:
            return same_model

    attempted_model_ids = _retry_chain_model_ids(failed_run)
    if live_research:
        return select_research_model(
            employee, work.operation, exclude_run=failed_run,
            exclude_model_ids=attempted_model_ids,
        )
    return select_retry_model(
        employee, failed_run, work.operation, "TASK_EXECUTION",
        exclude_model_ids=attempted_model_ids,
    )


_VERIFICATION_ENV_MARKERS = (
    "verification", "verify", "pytest", "test suite", "http", "flask",
    "runtime", "dependency", "dependencies", "wsl", "python",
)
_GENUINE_FOUNDER_AUTHORITY_MARKERS = (
    "database migration", "run migration", "deploy", "deployment",
    "delete data", "production data", "secret", "credential",
    "outside the repository", "outside repository", "protected file",
    "rewrite git history", "force push", "network access outside",
    "requires founder approval", "need founder approval", "needs founder approval",
    "additional budget", "budget authorization",
)


_REVIEW_EVIDENCE_PROTOCOL = "WORK_REVIEW_V4_ARTIFACT_SOURCE_LINEAGE"


def review_output_token_limit(model, semantic_criteria: list[dict]) -> int:
    """Use the reviewer's configured output envelope without a hidden ceiling.

    Work review is authority-bearing: truncating a valid semantic decision can
    abandon a delivery branch and block every dependent Work item.  Historical
    1,100/3,200 token helper ceilings therefore became platform faults as the
    review schema and evidence packet grew.  Real spend is governed by Project
    budget authority; output safety is handled by the compact bounded schema and
    one Company-owned retry, not by a second hidden token budget.
    """
    configured = int(getattr(model, "max_output_tokens", 0) or 0)
    if configured <= 0:
        raise ValueError("REVIEW_MODEL_OUTPUT_ENVELOPE_INVALID")
    return configured


def _latest_review_attempt(work):
    return (
        AgentRun.query.filter_by(work_id=work.id, purpose="TASK_REVIEW")
        .order_by(AgentRun.id.desc()).first()
    )



def _unresolved_later_attempt(work, *, purpose, after_run_id):
    """Return a later attempt whose external-effect truth still forbids replay.

    An older successful result is useful for crash recovery only when no newer
    attempt has an unproven post-dispatch outcome.  A known later failure may be
    ignored for output reuse; a RUNNING/FAILED_AMBIGUOUS attempt may still have
    spent money or produced an effect and therefore remains a hard boundary.
    """
    rows = (
        AgentRun.query.filter_by(work_id=work.id, purpose=purpose)
        .filter(AgentRun.id > int(after_run_id))
        .order_by(AgentRun.id.asc()).all()
    )
    effects = __import__("eason_one.models", fromlist=["ExternalEffectAttempt"]).ExternalEffectAttempt
    for row in rows:
        if row.status == "RUNNING" or str(row.outcome or "") == "FAILED_AMBIGUOUS":
            return row
        effect = (
            effects.query.filter_by(execution_id=row.id)
            .order_by(effects.id.desc()).first()
        )
        if effect is not None and str(effect.state or "").upper() in {
            "DISPATCHING", "RESPONSE_RECEIVED", "AMBIGUOUS_POST_DISPATCH",
        }:
            return row
    return None

def _durable_execution_for_work(work, task):
    """Newest already-paid successful execution that is still lawful to consume.

    Historical builds could persist a successful provider response and then,
    before Artifact projection completed, accidentally create a later duplicate
    attempt which failed. Looking only at the latest AgentRun would hide the
    durable success and could buy the same Work a third time after restart.

    Reuse remains fail-closed: an independently rejected Artifact, stale Founder
    execution terms, or an Engineer run whose newly approved exception requires
    a fresh Codex execution all disqualify the historical result.
    """
    candidates = (
        AgentRun.query.filter_by(work_id=work.id, purpose="TASK_EXECUTION", status="SUCCEEDED")
        .order_by(AgentRun.id.desc()).all()
    )
    for persisted in candidates:
        if _unresolved_later_attempt(
            work, purpose="TASK_EXECUTION", after_run_id=persisted.id
        ) is not None:
            # A newer unknown dispatch must be reconciled before the Work can
            # progress, even though an older output is already sufficient.
            continue
        prior_version = (
            ArtifactVersion.query.filter_by(execution_id=persisted.id)
            .order_by(ArtifactVersion.id.desc()).first()
        )
        if prior_version and prior_version.status == "REJECTED":
            continue
        if not _run_matches_current_project_terms(persisted, work):
            if persisted.resolution_status != "STALE_PROJECT_TERMS_SUPERSEDED":
                persisted.resolution_status = "STALE_PROJECT_TERMS_SUPERSEDED"
                persisted.resolved_at = now()
                persisted.resolution_note = (
                    "Founder Project execution terms changed after this paid result was produced; "
                    "the historical Run is preserved as audit evidence but cannot be silently reused."
                )
                db.session.commit()
            continue
        if task.assigned_employee and task.assigned_employee.slug == "engineer":
            prior_payload = dict(persisted.parsed_output_json or {})
            prior_codex = dict(prior_payload.get("codex") or {})
            if codex_founder_gate_required(persisted, prior_codex):
                prior_reason = prior_codex.get("founder_reason") or (
                    "Engineering work requires authority outside the approved boundary."
                )
                prior_action = _execution_authority_action(
                    work, persisted, escalation_type="CODEX_RISK_APPROVAL",
                    reason=prior_reason, codex=prior_codex,
                )
                receipt = __import__(
                    "eason_one.services.governance", fromlist=["approval_receipt"]
                ).approval_receipt(
                    project=work.project, work=work,
                    authority_type="CODEX_RISK_APPROVAL", requested_action=prior_action,
                )
                if receipt:
                    persisted.resolution_status = "FOUNDER_APPROVAL_REQUIRES_FRESH_EXECUTION"
                    persisted.resolution_note = (
                        f"Founder Decision #{receipt['decision_id']} approved this exact action; "
                        "the pre-approval AgentRun remains audit evidence and cannot be reused for execution."
                    )
                    db.session.commit()
                    continue
        return persisted
    return None


def _durable_review_for_target(work, target: dict):
    """Newest already-paid successful review for this exact immutable target.

    Historical builds could crash after one successful review, then accidentally
    purchase a second review which itself failed. Looking only at the latest
    attempt would hide the earlier durable success and could buy a third call.
    Exact ArtifactVersion/content/Contract matching makes reuse safe while
    preserving every later failed attempt as audit history.
    """
    candidates = (
        AgentRun.query.filter_by(work_id=work.id, purpose="TASK_REVIEW", status="SUCCEEDED")
        .order_by(AgentRun.id.desc()).all()
    )
    return next((
        run for run in candidates
        if _unresolved_later_attempt(work, purpose="TASK_REVIEW", after_run_id=run.id) is None
        and _review_run_matches_target(run, target, work)
    ), None)


def _compact_review_recovery(work) -> bool:
    """Whether the next review is a compact retry after a truncation.

    The failed paid attempt remains immutable audit evidence.  This flag only
    changes the next prompt shape so the same frozen ArtifactVersion can be
    reviewed inside the same ModelConfig envelope without replaying production.
    """
    prior = _latest_review_attempt(work)
    return bool(
        prior
        and prior.status == "FAILED"
        and prior.failure_reason == "OUTPUT_TRUNCATED"
        and prior.resolution_status in {
            "WORK_RETRY_SCHEDULED",
            "SYSTEM_REVIEW_OUTPUT_CAP_SUPERSEDED",
        }
    )




def _run_matches_current_project_terms(run, work) -> bool:
    """Never reuse a paid result under Founder terms that changed afterwards.

    New Runs carry the exact execution-terms hash. Historical Runs predate that
    snapshot, so they remain reusable only when no non-budget Project amendment
    occurred after the Run began. This preserves already-paid legacy results
    while still making live scope/constraint/deadline changes fail closed.
    """
    project = getattr(work, "project", None)
    if project is None:
        return True
    contract = __import__(
        "eason_one.services.project_contract",
        fromlist=["is_vnext_governed", "execution_terms_hash", "execution_terms_changed_after"],
    )
    if not contract.is_vnext_governed(project):
        return True
    current_hash = contract.execution_terms_hash(project)
    composition = dict(getattr(run, "context_composition_json", None) or {})
    snapshot_hash = str(composition.get("project_execution_terms_hash") or "").strip()
    if snapshot_hash:
        return snapshot_hash == current_hash
    return not contract.execution_terms_changed_after(project, getattr(run, "started_at", None))


def _evidence_review_generation(work, version, contract: dict) -> int:
    """Return the deliberate semantic re-review generation for this exact target.

    Generation 0 is the ordinary first review.  A bounded EVIDENCE_REVIEW_RETRY
    increments the generation while preserving the same immutable
    ArtifactVersion/content/Contract.  This separates an intentional fresh
    re-review from crash recovery: restart may reuse a paid success only within
    the same generation, never the prior UNPROVEN review that scheduled the
    retry.
    """
    control = dict(work.runtime_control_json or {})
    marker = dict(control.get("evidence_review_retry") or {})
    contract_hash = str(contract.get("contract_hash") or "")
    if (
        int(marker.get("artifact_version_id") or 0) == int(version.id)
        and str(marker.get("acceptance_contract_hash") or "") == contract_hash
    ):
        try:
            return max(0, int(marker.get("generation") or 0))
        except (TypeError, ValueError):
            return 0

    # S06 compatibility/adoption: S06 could persist an EVIDENCE_REVIEW_RETRY
    # gate without a generation marker, then repeatedly reuse the same paid
    # UNPROVEN review on every WAITING -> VERIFYING wake-up.  Adopt that durable
    # gate as a deliberate next-generation review without mutating the DB in the
    # installer.  The number of already-persisted exact VerificationRecords is
    # the intended next generation; duplicate historical wake-up gates do not
    # inflate it because they created no new review evidence.
    legacy_prefix = (
        f"EVIDENCE_REVIEW_RETRY:{int(work.id)}:{int(version.id)}:"
        f"{contract_hash[:24]}"
    )
    has_legacy_retry = any(
        str(row.get("condition_type") or "") == "EVIDENCE_REVIEW_RETRY"
        and str(row.get("issue_code") or "").startswith(legacy_prefix)
        for row in (control.get("gates") or [])
    )
    if has_legacy_retry:
        exact_reviews = 0
        for record in VerificationRecord.query.filter_by(
            work_id=work.id, artifact_version_id=version.id, method="INDEPENDENT_REVIEW"
        ).order_by(VerificationRecord.id).all():
            details = dict(record.details_json or {})
            if str(details.get("acceptance_contract_hash") or "") == contract_hash:
                exact_reviews += 1
        if exact_reviews:
            return exact_reviews
    return 0


def _review_target(version, contract: dict, work=None) -> dict:
    return {
        "artifact_version_id": int(version.id),
        "artifact_content_hash": str(version.content_hash or ""),
        "acceptance_contract_hash": str(contract.get("contract_hash") or ""),
        "evidence_review_generation": (
            _evidence_review_generation(work, version, contract) if work is not None else 0
        ),
    }


def _review_run_matches_target(run, target: dict, work) -> bool:
    if run is None or run.status != "SUCCEEDED":
        return False
    if not _run_matches_current_project_terms(run, work):
        return False
    composition = dict(getattr(run, "context_composition_json", None) or {})
    snapshot = composition.get("review_target")
    if isinstance(snapshot, dict):
        base_keys = ("artifact_version_id", "artifact_content_hash", "acceptance_contract_hash")
        if not all(str(snapshot.get(key) or "") == str(target.get(key) or "") for key in base_keys):
            return False
        # Historical review_target snapshots predate deliberate evidence-review
        # generations and therefore mean generation 0.  Once a bounded semantic
        # re-review is scheduled, generation > 0 requires a fresh paid review;
        # reusing the earlier UNPROVEN success would create a WAITING/VERIFYING
        # maintenance loop without acquiring new evidence.
        try:
            snapshot_generation = int(snapshot.get("evidence_review_generation") or 0)
            target_generation = int(target.get("evidence_review_generation") or 0)
        except (TypeError, ValueError):
            return False
        return snapshot_generation == target_generation

    # Compatibility for a paid review completed before review_target became a
    # first-class snapshot. The old prompt already embedded these three exact
    # immutable identifiers, so a crash after provider return can still resume
    # without buying the same review twice.
    context = str(getattr(run, "context_snapshot", "") or "")
    # Legacy context-only matching is valid only for the ordinary first review.
    # A deliberate evidence re-review (generation > 0) must have an explicit
    # generation snapshot so crash recovery cannot fall back to the older
    # UNPROVEN review.
    if int(target.get("evidence_review_generation") or 0) != 0:
        return False
    return bool(
        f"Contract hash: {target['acceptance_contract_hash']}" in context
        and f'"artifact_version_id": {target["artifact_version_id"]}' in context
        and f'"artifact_content_hash": "{target["artifact_content_hash"]}"' in context
    )


def _existing_review_projection(work, version, run, contract: dict):
    """Return durable local projection for this exact paid semantic review.

    A provider-successful TASK_REVIEW may survive a crash after its
    VerificationRecord/WorkMessage was written but before Work acceptance was
    finalized.  Replaying local projection must not duplicate evidence because
    review-attempt counting uses VerificationRecord truth.
    """
    verification = (
        VerificationRecord.query.filter_by(
            work_id=work.id,
            artifact_version_id=version.id,
            method="INDEPENDENT_REVIEW",
            agent_run_id=run.id,
        )
        .order_by(VerificationRecord.id.desc())
        .first()
    )
    if verification is not None:
        details = dict(verification.details_json or {})
        if str(details.get("acceptance_contract_hash") or "") != str(contract.get("contract_hash") or ""):
            verification = None

    message = WorkMessage.query.filter_by(
        project_id=work.project_id,
        task_id=_task(work).id,
        message_type="REVIEW",
        agent_run_id=run.id,
    ).order_by(WorkMessage.id.desc()).first()
    return verification, message


def _artifact_provider_source_lineage(version):
    """Return provider-observed sources bound to this exact ArtifactVersion.

    Research source provenance belongs to the producing Execution/Artifact, not
    to a later reviewer call.  The independent reviewer consumes this frozen
    Company Truth; it is never required to manufacture a second set of sources
    merely to prove that the first set existed.
    """
    producer_run = db.session.get(AgentRun, version.execution_id) if version.execution_id else None
    context = dict(getattr(producer_run, "context_composition_json", None) or {})
    sources = []
    for row in (context.get("provider_sources") or []):
        if not isinstance(row, dict):
            continue
        url = str(row.get("url") or "").strip()
        if not url:
            continue
        sources.append({
            "title": str(row.get("title") or "Source").strip()[:500],
            "url": url[:2000],
            "date": str(row.get("date") or row.get("last_updated") or "").strip()[:200],
        })
    return producer_run, sources[:40]

def _authoritative_host_review_evidence(work, version, contract: dict) -> dict:
    """Compact deterministic proof visible to the semantic reviewer.

    Producer/Codex output is captured before Windows host validation runs, so a
    truthful producer may say that WSL could not execute HTTP verification even
    though Eason One subsequently proves the exact contract on the Windows host.
    Semantic review must see that later durable Company Truth, but it may not
    reclassify or overwrite proof ownership.
    """
    rows = (
        VerificationRecord.query.filter_by(
            work_id=work.id,
            artifact_version_id=version.id,
            method="HOST_ENGINEERING_VALIDATION",
            status="PASSED",
        )
        .order_by(VerificationRecord.id)
        .all()
    )
    compact = []
    for record in rows:
        details = dict(record.details_json or {})
        if details.get("acceptance_contract_hash") != contract.get("contract_hash"):
            continue
        bound_hash = details.get("artifact_content_hash")
        if bound_hash and bound_hash != version.content_hash:
            continue
        criterion_results = {}
        for criterion_id, result in dict(details.get("criterion_results") or {}).items():
            result = dict(result or {})
            evidence = str(result.get("evidence") or "")
            criterion_results[str(criterion_id)] = {
                "status": result.get("status") or "UNPROVEN",
                "source": result.get("source") or record.method,
                "evidence": evidence[:2400],
            }
        checks = []
        for check in ((details.get("host_observations") or {}).get("checks") or []):
            if not isinstance(check, dict):
                continue
            row = {"kind": check.get("kind"), "status": check.get("status")}
            detail = check.get("detail")
            if isinstance(detail, dict):
                test = detail.get("test") or {}
                observation = detail.get("observation") or {}
                row["detail"] = {
                    "command": test.get("command") or detail.get("command"),
                    "status": test.get("status") or detail.get("status"),
                    "test_detail": str(test.get("detail") or detail.get("detail") or "")[:2400],
                    "observation": {
                        "status": observation.get("status"),
                        "json": observation.get("json"),
                        "url": observation.get("url"),
                        "transport": observation.get("transport"),
                    },
                    "contract": check.get("contract"),
                }
            elif detail:
                row["detail"] = str(detail)[:1200]
            checks.append(row)
        compact.append({
            "verification_record_id": record.id,
            "artifact_version_id": version.id,
            "artifact_content_hash": version.content_hash,
            "criterion_results": criterion_results,
            "checks": checks,
        })
    return {
        "protocol": _REVIEW_EVIDENCE_PROTOCOL,
        "contract_hash": contract.get("contract_hash"),
        "artifact_version_id": version.id,
        "artifact_content_hash": version.content_hash,
        "host_verifications": compact,
    }


def codex_founder_request_is_verification_only(run, codex: dict) -> bool:
    """Return True only when Codex's Founder request is superseded by host proof.

    Codex runs inside WSL and is not the final acceptance authority. If its only
    blocker is that *it* cannot run the approved verification in that sandbox,
    while Eason One's Windows host verifier already completed the bounded
    acceptance successfully, that limitation is an internal execution detail,
    not Founder work. Genuine authority/risk requests remain Founder-owned.
    """
    if not codex.get("needs_founder"):
        return False
    context = dict(getattr(run, "context_composition_json", None) or {})
    host = dict(context.get("host_validation") or {})
    if not host.get("attempted") or not host.get("success"):
        return False
    checks = [row for row in (host.get("checks") or []) if isinstance(row, dict)]
    authoritative_kinds = {"HTTP_CONTRACT", "EXACT_REPLACEMENT", "FOCUSED_TEST_SUITE", "REGRESSION_SUITE"}
    if not any(row.get("kind") in authoritative_kinds and row.get("status") == "PASSED" for row in checks):
        # Repository-delta equality alone does not prove the Founder-approved outcome.
        return False
    reason = " ".join(str(codex.get("founder_reason") or "").casefold().split())
    if not reason:
        return False
    if any(marker in reason for marker in _GENUINE_FOUNDER_AUTHORITY_MARKERS):
        return False
    return any(marker in reason for marker in _VERIFICATION_ENV_MARKERS)


def codex_founder_gate_required(run, codex: dict) -> bool:
    return bool(codex.get("needs_founder")) and not codex_founder_request_is_verification_only(run, codex)


def _task(work):
    task = work_runtime.task_for_work(work)
    if not task:
        raise ValueError(f"Work #{work.id} has no execution adapter")
    assignment = work_runtime.active_assignment(work)
    if not assignment:
        raise ValueError(f"Work #{work.id} has no active Employee assignment")
    if task.assigned_employee_id != assignment.employee_id:
        task.assigned_employee_id = assignment.employee_id
    return task


def _latest_artifact_version(work):
    return (
        ArtifactVersion.query.join(Artifact)
        .filter(Artifact.work_id == work.id)
        .order_by(ArtifactVersion.id.desc()).first()
    )


def _execution_authority_action(work, run, *, escalation_type: str, reason: str, codex: dict | None = None) -> dict:
    governance = __import__(
        "eason_one.services.governance", fromlist=["exact_action_payload"]
    )
    context = dict(getattr(run, "context_composition_json", None) or {})
    codex = dict(codex or {})
    return governance.exact_action_payload(
        work=work, authority_type=escalation_type, reason=reason,
        risks=codex.get("risks") or [],
        extra={
            "execution_id": getattr(run, "id", None),
            "task_id": getattr(run, "task_id", None),
            "repository": context.get("repository"),
            "founder_reason": codex.get("founder_reason") or reason,
        },
    )


def _open_founder_gate(work, reason: str, *, escalation_type: str, run=None, requested_action=None):
    """Open one canonical exact Founder gate or fail back to internal recovery."""
    governance = __import__(
        "eason_one.services.governance",
        fromlist=["request_budget_gate", "open_gate"],
    )
    operation = work.operation
    actor = getattr(run, "employee_id", None)
    if escalation_type == "BUDGET_AUTHORIZATION":
        authority = dict(((getattr(run, "context_composition_json", None) or {}).get("authority_failure") or {}))
        additional = authority.get("additional_twd")
        scope = str(authority.get("scope") or "PROJECT").upper()
        try:
            amount = Decimal(str(additional or 0))
        except Exception:
            amount = Decimal("0")
        if scope != "PROJECT" or amount <= 0:
            raise ValueError("BUDGET_AUTHORITY_REQUIRES_EXACT_PROJECT_SHORTFALL")
        row = governance.request_budget_gate(
            project=work.project, additional_twd=amount, reason=reason,
            work=work, operation=operation, created_by_employee_id=actor,
        )
    else:
        if not isinstance(requested_action, dict) or not requested_action:
            raise ValueError("EXECUTION_AUTHORITY_REQUIRES_EXACT_ACTION")
        row = governance.open_gate(
            project=work.project, escalation_type=escalation_type, reason=reason,
            work=work, operation=operation, created_by_employee_id=actor,
            authority_payload={"requested_action": requested_action},
            recommendation="Approve only this exact bounded action; a different action requires a new Founder decision.",
        )
    work_runtime.open_wait(
        work, "FOUNDER_DECISION", reason, gate_key=f"escalation:{row.id}"
    )
    # Temporary Operation projection for old Mission controls. Exact authority is
    # frozen in Escalation.options_json and later consumed by governance.resolve_gate.
    if operation:
        approved = __import__(
            "eason_one.services.governance", fromlist=["approve_option"]
        ).approve_option(row)
        operation.founder_report_json = {
            "decision_kind": escalation_type,
            "summary": reason,
            "governance_escalation_id": row.id,
            **({
                "additional_budget_twd": approved.get("additional_budget_twd"),
                "scope": "PROJECT",
            } if escalation_type == "BUDGET_AUTHORIZATION" else {}),
        }
    return row


def _schedule_evidence_review_retry(work, run, version, contract: dict, reason: str) -> str:
    """Retry semantic proof without replaying the implementation.

    ``UNPROVEN`` means the reviewer could not prove one or more semantic
    criteria from the current durable evidence packet. It is not evidence that
    the repository implementation is wrong, so the retry owner is the reviewer
    path, not Codex/TASK_EXECUTION. One bounded re-review is allowed according
    to the Work retry envelope. If the same ArtifactVersion still cannot be
    proven, terminate the bounded Mission truthfully while preserving the
    submitted Artifact + host proof so Project-level continuation can plan an
    evidence-only next move.
    """
    contract_hash = str(contract.get("contract_hash") or "")
    prior_reviews = []
    for record in VerificationRecord.query.filter_by(
        work_id=work.id, artifact_version_id=version.id, method="INDEPENDENT_REVIEW"
    ).order_by(VerificationRecord.id).all():
        details = dict(record.details_json or {})
        if str(details.get("acceptance_contract_hash") or "") == contract_hash:
            prior_reviews.append(record)

    max_attempts = max(1, int(work.retry_limit or 0) + 1)
    attempts = len(prior_reviews)
    if attempts < max_attempts:
        delay = min(30, 2 * (2 ** max(0, attempts - 1)))
        current_target = dict((run.context_composition_json or {}).get("review_target") or {})
        try:
            current_generation = max(0, int(current_target.get("evidence_review_generation") or 0))
        except (TypeError, ValueError):
            current_generation = 0
        next_generation = current_generation + 1
        control = dict(work.runtime_control_json or {})
        control["evidence_review_retry"] = {
            "artifact_version_id": int(version.id),
            "acceptance_contract_hash": contract_hash,
            "generation": next_generation,
            "source_review_run_id": int(run.id),
        }
        work.runtime_control_json = control
        run.resolution_status = "EVIDENCE_REVIEW_RETRY_SCHEDULED"
        run.resolution_note = (
            f"Semantic evidence remains UNPROVEN; retry independent review only after {delay}s. "
            "Implementation and host proof are preserved."
        )
        work_runtime.open_wait(
            work, "EVIDENCE_REVIEW_RETRY", reason,
            retry_after=now() + timedelta(seconds=delay),
            resume_state="VERIFYING",
            issue_code=(
                f"EVIDENCE_REVIEW_RETRY:{int(work.id)}:{int(version.id)}:"
                f"{str(contract_hash)[:24]}:GEN{next_generation}:RUN{int(run.id)}"
            ),
        )
        __import__(
            "eason_one.services.company_events", fromlist=["emit"]
        ).emit(
            "WORK_EVIDENCE_REVIEW_RETRY_SCHEDULED", actor_type="RUNTIME",
            project_id=work.project_id, work_id=work.id, execution_id=run.id,
            correlation_id=f"work:{work.id}",
            payload={
                "artifact_version_id": version.id,
                "acceptance_contract_hash": contract_hash,
                "attempt": attempts,
                "max_attempts": max_attempts,
                "review_generation": next_generation,
                "source_review_run_id": run.id,
                "implementation_replayed": False,
            },
        )
        return "EVIDENCE_REVIEW_RETRY_SCHEDULED"

    terminal_reason = (
        "EVIDENCE_REVIEW_EXHAUSTED: independent review remained UNPROVEN after "
        f"{attempts} bounded review attempt(s) for the same ArtifactVersion. "
        "The implementation Artifact and authoritative host proof are preserved; "
        "Project continuation must collect/reconcile missing evidence and must not "
        "replay the same implementation merely to obtain proof."
    )
    run.resolution_status = "EVIDENCE_REVIEW_EXHAUSTED"
    run.resolution_note = terminal_reason
    work_runtime.transition(
        work, "ABANDONED", actor_type="RUNTIME", reason=terminal_reason
    )
    __import__(
        "eason_one.services.company_events", fromlist=["emit"]
    ).emit(
        "WORK_EVIDENCE_REVIEW_EXHAUSTED", actor_type="RUNTIME",
        project_id=work.project_id, work_id=work.id, execution_id=run.id,
        correlation_id=f"work:{work.id}",
        payload={
            "artifact_version_id": version.id,
            "acceptance_contract_hash": contract_hash,
            "review_attempts": attempts,
            "implementation_replayed": False,
            "artifact_rejected": False,
            "next_owner": "PROJECT_CONTINUATION_EVIDENCE_ONLY",
        },
    )
    return "EVIDENCE_REVIEW_EXHAUSTED"


def _work_effect_reconciliation_issue(run) -> str:
    purpose = str(getattr(run, "purpose", None) or "UNKNOWN").upper()
    return f"WORK_EFFECT_RECONCILIATION:{purpose}:{int(run.id)}"


def _open_work_effect_reconciliation(work, run, reason: str) -> None:
    """Fail closed on one exact unresolved provider effect.

    The gate owner is the durable AgentRun, not the whole Work. Later settlement
    maintenance may retire only this exact gate once the external-effect truth is
    no longer ambiguous; it never authorizes replay by itself.
    """
    work_runtime.open_wait(
        work, "RECONCILIATION", reason,
        issue_code=_work_effect_reconciliation_issue(run),
    )


def _work_retry_failure_signature(run, purpose: str) -> str:
    """Stable recovery signature for one materially identical Work failure.

    Retry budget belongs to the failure class, not to the lifetime of a Work.
    Keep provider/model ids out of the signature so a cross-model retry cannot
    manufacture a fresh budget for the same underlying failure.
    """
    raw = "|".join([
        str(purpose or getattr(run, "purpose", None) or "TASK_EXECUTION").upper(),
        str(getattr(run, "outcome", None) or ""),
        str(getattr(run, "failure_reason", None) or "UNKNOWN"),
        str(getattr(run, "failure_stage", None) or ""),
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def _work_retry_issue_code(run, purpose: str) -> str:
    return f"WORK_INTERNAL_RECOVERY:{str(purpose).upper()}:{_work_retry_failure_signature(run, purpose)}"


def _automatic_work_failure_runs(work, purpose: str):
    ignored = {
        "SYSTEM_VALIDATION_SCOPE_FAULT",
        "SYSTEM_PROVIDER_ROUTING_SUPERSEDED",
        "SYSTEM_VNEXT_ELAPSED_LIMIT_SUPERSEDED",
        "SYSTEM_PERPLEXITY_RESPONSE_FORMAT_SUPERSEDED",
        "SYSTEM_RESEARCH_REVIEW_EVIDENCE_SCOPE_SUPERSEDED",
        "SYSTEM_REVIEW_OUTPUT_CAP_SUPERSEDED",
    }
    return [
        row for row in AgentRun.query.filter_by(work_id=work.id, purpose=purpose).all()
        if row.status == "FAILED" and row.resolution_status not in ignored
    ]


def _schedule_internal_retry(
    work, run, reason: str, *, purpose: str | None = None, replay_authority_run=None
):
    purpose = purpose or getattr(run, "purpose", None) or "TASK_EXECUTION"
    authority_run = replay_authority_run or run
    retry_ok, retry_basis = __import__(
        "eason_one.services.external_effects", fromlist=["retry_authorized"]
    ).retry_authorized(authority_run)
    if not retry_ok:
        run.resolution_status = "RECONCILIATION_REQUIRED"
        run.resolution_note = f"Automatic replay denied: {retry_basis}."
        work_runtime.open_wait(
            work, "RECONCILIATION",
            reason + f" Automatic replay is not authorized ({retry_basis}).",
            issue_code=f"WORK_REPLAY_AUTHORITY_RECONCILIATION:{str(purpose).upper()}:{int(run.id)}",
        )
        return "RECONCILIATION_REQUIRED"

    failed_runs = _automatic_work_failure_runs(work, purpose)
    signature = _work_retry_failure_signature(run, purpose)
    same_failure_attempts = sum(
        1 for row in failed_runs
        if _work_retry_failure_signature(row, purpose) == signature
    )
    max_attempts = max(1, int(work.retry_limit or 0) + 1)
    # Different failures must not consume one another's retry envelope, but a
    # Work also cannot buy unlimited retries merely by presenting new failure
    # labels. Two bounded failure families is enough evidence that the runtime
    # path itself needs system repair rather than more spend.
    overall_ceiling = max_attempts * 2
    if len(failed_runs) > overall_ceiling:
        run.resolution_status = "WORK_RECOVERY_ANOMALY_CEILING"
        run.resolution_note = (
            f"Automatic Work recovery stopped after {len(failed_runs)} failed {purpose} runs "
            "across multiple failure classes; system repair is required before more spend."
        )
        work_runtime.open_wait(
            work, "SYSTEM_RECOVERY", run.resolution_note,
            issue_code=f"WORK_RECOVERY_ANOMALY_CEILING:{str(purpose).upper()}:{int(work.id)}",
        )
        return "SYSTEM_RECOVERY_REQUIRED"

    if same_failure_attempts < max_attempts:
        delay = min(30, 2 * (2 ** max(0, same_failure_attempts - 1)))
        rejection = dict((run.context_composition_json or {}).get("provider_rejection") or {})
        try:
            provider_delay = int(rejection.get("retry_after_seconds")) if rejection.get("retry_after_seconds") is not None else 0
        except (TypeError, ValueError):
            provider_delay = 0
        delay = max(delay, min(60, max(0, provider_delay)))
        run.resolution_status = "WORK_RETRY_SCHEDULED"
        run.resolution_note = (
            f"Work retry scheduled after {delay}s for {purpose}; "
            f"failure-signature attempt {same_failure_attempts}/{max_attempts}."
        )
        work_runtime.open_wait(
            work, "INTERNAL_RECOVERY", reason,
            retry_after=now() + timedelta(seconds=delay),
            issue_code=_work_retry_issue_code(run, purpose),
        )
        return "RETRY_SCHEDULED"

    # The same materially identical failure exhausted its bounded envelope. This
    # is a truthful Mission failure, unlike the anomaly ceiling above where many
    # unrelated runtime faults indicate a system problem.
    run.resolution_status = "WORK_RETRY_EXHAUSTED"
    run.resolution_note = f"Bounded Work recovery exhausted for {purpose} failure signature {signature}."
    work_runtime.transition(
        work, "ABANDONED", actor_type="RUNTIME",
        reason=reason + " Automatic bounded retries for the same failure are exhausted.",
    )
    return "FAILED_INTERNAL"


def _host_proof_scope(work) -> dict:
    version = _latest_artifact_version(work)
    control = dict(work.runtime_control_json or {})
    contract = dict(control.get("acceptance_contract") or {})
    return {
        "artifact_version_id": getattr(version, "id", None),
        "artifact_content_hash": getattr(version, "content_hash", None),
        "acceptance_contract_hash": contract.get("contract_hash"),
    }


def _host_proof_scope_key(scope: dict) -> str:
    raw = "|".join([
        str(scope.get("artifact_version_id") or "NONE"),
        str(scope.get("artifact_content_hash") or ""),
        str(scope.get("acceptance_contract_hash") or ""),
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def _schedule_host_proof_retry(work, run, reason: str) -> str:
    """Retry deterministic host proof for one exact Artifact + Contract.

    Retry ownership follows immutable proof identity. A retry consumed by
    ArtifactVersion N must never poison a later ArtifactVersion N+1, while a
    restart of the same Artifact may not reset its local retry allowance.
    """
    control = dict(work.runtime_control_json or {})
    scope = _host_proof_scope(work)
    scope_key = _host_proof_scope_key(scope)
    prior = dict(control.get("host_proof_retry") or {})
    used = int(prior.get("attempt") or 0) if prior.get("scope_key") == scope_key else 0
    max_retries = 1
    retry_issue = f"HOST_PROOF_RETRY:{int(work.id)}:{scope_key}"
    reconciliation_issue = f"HOST_PROOF_RECONCILIATION:{int(work.id)}:{scope_key}"

    if used < max_retries:
        used += 1
        control["host_proof_retry"] = {
            "scope_key": scope_key,
            **scope,
            "attempt": used,
            "max_retries": max_retries,
            "scheduled_at": now().isoformat(),
            "implementation_replayed": False,
        }
        # Drop the legacy Work-global counter so later code cannot accidentally
        # inherit validation history from a different ArtifactVersion.
        control.pop("host_proof_retry_count", None)
        work.runtime_control_json = control
        if run is not None:
            run.resolution_status = "HOST_PROOF_RETRY_SCHEDULED"
            run.resolution_note = (
                "Deterministic host proof will be retried for the same exact ArtifactVersion/Contract "
                "without a new implementation call."
            )
        work_runtime.open_wait(
            work, "HOST_PROOF_RETRY", reason,
            retry_after=now() + timedelta(seconds=2), resume_state="EXECUTING",
            issue_code=retry_issue,
        )
        return "HOST_PROOF_RETRY_SCHEDULED"

    if run is not None:
        run.resolution_status = "HOST_PROOF_RETRY_EXHAUSTED"
        run.resolution_note = (
            "Deterministic host proof retry exhausted for this exact ArtifactVersion/Contract; "
            "Company reconciliation is required."
        )
    work_runtime.open_wait(
        work, "RECONCILIATION",
        reason + " Deterministic host proof retry was exhausted without replaying implementation.",
        issue_code=reconciliation_issue,
    )
    return "RECONCILIATION_REQUIRED"


def _handle_acceptance_contract_error(work, task, exc: ValueError) -> dict:
    """Route immutable-authority drift to the correct lifecycle owner."""
    reason = str(exc)
    if reason.startswith("WORK_PROJECT_TERMS_CHANGED:"):
        # This Work was valid when approved, but a later Founder Project
        # amendment changed objective/success/constraints/deadline. The stale
        # Work must terminate so Project sequencing can create a fresh bounded
        # Mission under the new Contract; it must not sit forever in a generic
        # RECONCILIATION wait or execute against obsolete authority.
        if work.state not in work_runtime.TERMINAL_WORK_STATES:
            work_runtime.transition(
                work, "CANCELLED", actor_type="RUNTIME",
                reason=reason,
            )
        work_runtime.sync_task_projection(work, task)
        __import__(
            "eason_one.services.company_events", fromlist=["emit"]
        ).emit(
            "WORK_STALE_AFTER_PROJECT_AMENDMENT", actor_type="RUNTIME",
            project_id=work.project_id, work_id=work.id,
            correlation_id=f"work:{work.id}",
            payload={"reason": reason, "replan_required": True},
        )
        db.session.commit()
        return {"status": "STALE_WORK_CANCELLED", "work_id": work.id}
    work_runtime.open_wait(
        work, "RECONCILIATION", reason,
        issue_code=f"WORK_ACCEPTANCE_CONTRACT_RECONCILIATION:{int(work.id)}",
    )
    work_runtime.sync_task_projection(work, task)
    db.session.commit()
    return {"status": "RECONCILIATION_REQUIRED", "work_id": work.id}


def execute_work(work, *, materialize_only=False):
    project = getattr(work, "project", None)
    if work_runtime.project_is_terminal(project):
        return {
            "status": "PROJECT_TERMINAL",
            "project_status": str(getattr(project, "status", "") or "").upper(),
            "work_id": work.id,
        }
    if getattr(project, "status", None) == "PAUSED":
        return {"status": "PROJECT_PAUSED", "work_id": work.id}
    if materialize_only:
        # Deterministic crash recovery only: this mode may project an already-paid
        # exact TASK_EXECUTION result into Artifact/Verification/Work truth, but it
        # must never authorize a fresh provider/tool dispatch. It is used when the
        # process died after the durable AgentRun succeeded but before local
        # ArtifactVersion materialization completed.
        if work.state != "VERIFYING":
            raise ValueError(f"Work #{work.id} materialization recovery requires VERIFYING state")
    elif work.state not in {"READY", "EXECUTING"}:
        raise ValueError(f"Work #{work.id} is not executable from {work.state}")
    initial_state = work.state
    if not work_runtime.dependencies_satisfied(work):
        work_runtime.open_wait(work, "DEPENDENCY", "Waiting for upstream Work to be accepted.")
        db.session.commit()
        return {"status": "WAITING_DEPENDENCY", "work_id": work.id}

    task = _task(work)
    try:
        acceptance_contract = __import__(
            "eason_one.services.acceptance_contract", fromlist=["ensure_for_work"]
        ).ensure_for_work(work, task=task)
    except ValueError as exc:
        return _handle_acceptance_contract_error(work, task, exc)
    if work.state == "READY":
        work_runtime.transition(
            work, "EXECUTING", actor_type="EMPLOYEE",
            actor_id=task.assigned_employee_id, reason="Employee execution started",
        )
    work_runtime.sync_task_projection(work, task)
    db.session.commit()

    # Resume from durable execution truth before invoking any provider/tool again.
    # A process may die after an AgentRun is persisted but before Artifact/Work
    # finalization. Reusing that attempt is both safer and cheaper than replaying it.
    run = None
    persisted = (
        AgentRun.query.filter_by(work_id=work.id, purpose="TASK_EXECUTION")
        .order_by(AgentRun.id.desc()).first()
    )
    if persisted is not None and persisted.status == "RUNNING":
        # Never create a second provider/tool call while a durable attempt is
        # still live. Restart recovery owns classification of genuinely stale
        # RUNNING rows before another execution may be authorized.
        return {"status": "EXECUTION_IN_PROGRESS", "work_id": work.id, "run_id": persisted.id}
    reusable_success = _durable_execution_for_work(work, task)
    if reusable_success is not None:
        # A later duplicate failure must never hide an earlier exact durable
        # success. The failed attempt stays in audit history, while Artifact/Work
        # projection resumes from the already-paid successful response.  If the
        # process died after provider settlement but before TASK_EXECUTION local
        # schema/materialization, finish that deterministic postprocess from the
        # durable raw response instead of purchasing the provider call again.
        run = reusable_success
        if run.status == "SUCCEEDED" and not run.parsed_output_json:
            run = __import__(
                "eason_one.services.task_execution", fromlist=["materialize_successful_run"]
            ).materialize_successful_run(task, run, recovery=True)
    elif (
        initial_state == "EXECUTING"
        and persisted
        and persisted.status == "FAILED"
        and persisted.resolution_status != "WORK_RETRY_SCHEDULED"
    ):
        # A durable failure that was not yet translated into a Work wait before
        # process loss is handled now; do not silently make a second paid call.
        # Once a durable retry wait has been scheduled and resolved, the next
        # runtime turn must create a fresh attempt instead of re-consuming the
        # same failed AgentRun until retries are exhausted.
        run = persisted
    if materialize_only and run is None:
        # No exact durable success exists, therefore local recovery has nothing to
        # project. Fail closed rather than buying a new execution from a recovery
        # path whose contract is explicitly zero-provider-call.
        return {"status": "MATERIALIZATION_SOURCE_UNAVAILABLE", "work_id": work.id}
    if run is None:
        # SYSTEM_RECOVERY maintenance may have proven that an untried lawful model
        # is now available. It records the exact source Run + candidate before
        # resolving the wait; execution consumes that durable authorization here.
        control = dict(work.runtime_control_json or {})
        recovery = dict(control.get("safe_system_recovery_retry") or {})
        if recovery:
            source_run = db.session.get(AgentRun, recovery.get("source_run_id"))
            ModelConfig = __import__("eason_one.models", fromlist=["ModelConfig"]).ModelConfig
            recovery_model = db.session.get(ModelConfig, recovery.get("model_config_id"))
            expected = automatic_recovery_model(work, task, source_run) if source_run is not None else None
            if recovery_model is None or expected is None or int(expected.id) != int(recovery_model.id):
                control.pop("safe_system_recovery_retry", None)
                work.runtime_control_json = control
                work_runtime.open_wait(
                    work, "SYSTEM_RECOVERY",
                    "The previously discovered automatic recovery path is no longer lawful under current Project/provider constraints.",
                    issue_code=str(recovery.get("issue_code") or "SAFE_RECOVERY_PATH_CHANGED"),
                )
                work_runtime.sync_task_projection(work, task)
                db.session.commit()
                return {"status": "SYSTEM_RECOVERY_REQUIRED", "work_id": work.id}
            run = run_task(task, retry_of_run=source_run, model_override=recovery_model)
            control = dict(work.runtime_control_json or {})
            control.pop("safe_system_recovery_retry", None)
            work.runtime_control_json = control
            db.session.commit()
        else:
            run = run_task(task)
    if materialize_only and (run.status != "SUCCEEDED" or not run.parsed_output_json):
        # Local crash recovery is deliberately incapable of turning a parsing or
        # postprocess failure into a fresh paid attempt. Normal runtime recovery
        # owns any later bounded retry decision.
        return {
            "status": "MATERIALIZATION_LOCAL_POSTPROCESS_FAILED",
            "work_id": work.id, "run_id": run.id,
        }
    if run.status != "SUCCEEDED":
        # Walk a bounded chain of lawful alternatives instead of stopping after
        # only one failed replacement. Every hop is still guarded by durable
        # external-effect replay safety, Project authority and real cost budget.
        while automatic_retry_allowed(run, task):
            alternate = automatic_recovery_model(work, task, run)
            if alternate is None:
                break
            run = run_task(task, retry_of_run=run, model_override=alternate)
            if run.status == "SUCCEEDED":
                break

    if run.status != "SUCCEEDED" or not run.parsed_output_json:
        reason = run.error_text or run.failure_reason or "Execution did not produce an accepted result."
        if run.outcome == "FAILED_AMBIGUOUS":
            _open_work_effect_reconciliation(work, run, reason)
            status = "RECONCILIATION_REQUIRED"
        elif run.failure_reason == "FOUNDER_BUDGET_EXTENSION_REQUIRED":
            try:
                _open_founder_gate(work, reason, escalation_type="BUDGET_AUTHORIZATION", run=run)
                status = "NEEDS_FOUNDER"
            except __import__(
                "eason_one.services.governance", fromlist=["FounderAuthorityPreviouslyRejected"]
            ).FounderAuthorityPreviouslyRejected as exc:
                __import__(
                    "eason_one.services.governance", fromlist=["apply_rejected_budget_boundary"]
                ).apply_rejected_budget_boundary(
                    project=work.project, work=work, decision_id=exc.decision_id
                )
                status = "AUTHORITY_EXHAUSTED"
            except ValueError:
                # A model/provider saying "budget" is not authority evidence.
                # Without a deterministic exact positive Project shortfall, this
                # remains Company reconciliation instead of a blank Founder gate.
                work_runtime.open_wait(
                    work, "RECONCILIATION",
                    reason + " Exact positive Project budget shortfall was not proven.",
                )
                status = "RECONCILIATION_REQUIRED"
        elif run.failure_reason == "AUTHORITY_BLOCKED":
            # Generic authority-looking strings are deliberately non-Founder.
            # A precise boundary must be classified by deterministic runtime.
            work_runtime.open_wait(
                work, "RECONCILIATION",
                reason + " Runtime did not identify a precise Founder-only authority type.",
            )
            status = "RECONCILIATION_REQUIRED"
        elif run.failure_reason == "MISSING_EVIDENCE":
            evidence_meta = dict((run.context_composition_json or {}).get("evidence_retrieval") or {})
            basis_hash = str(evidence_meta.get("basis_hash") or "")
            issue_code = f"MISSING_EVIDENCE:{basis_hash}" if basis_hash else f"MISSING_EVIDENCE:RUN:{int(run.id)}"
            work_runtime.open_wait(
                work, "DEPENDENCY", reason,
                issue_code=issue_code,
                resume_state="EXECUTING",
            )
            status = "WAITING_EVIDENCE"
        elif run.failure_reason == "RESEARCH_TOOL_UNAVAILABLE":
            work_runtime.open_wait(work, "SYSTEM_RECOVERY", reason, issue_code="RESEARCH_TOOL_UNAVAILABLE")
            status = "SYSTEM_RECOVERY_REQUIRED"
        elif run.failure_reason == "PROVIDER_REQUEST_REJECTED":
            work_runtime.open_wait(
                work, "SYSTEM_RECOVERY", reason,
                issue_code=f"PROVIDER_REQUEST_REJECTED_{run.provider_key_snapshot or 'UNKNOWN'}",
            )
            status = "SYSTEM_RECOVERY_REQUIRED"
        elif run.failure_reason in {"ACCEPTANCE_CONTRACT_MISMATCH", "ACCEPTANCE_CONTRACT_MISSING"}:
            work_runtime.open_wait(
                work, "RECONCILIATION", reason,
                issue_code=f"WORK_ACCEPTANCE_CONTRACT_RECONCILIATION:{int(work.id)}",
            )
            status = "RECONCILIATION_REQUIRED"
        else:
            status = _schedule_internal_retry(work, run, reason)
        work_runtime.sync_task_projection(work, task)
        db.session.commit()
        return {"status": status, "work_id": work.id, "run_id": run.id}

    payload = dict(run.parsed_output_json or {})
    task.result_summary = payload.get("result_summary") or task.result_summary or run.raw_output
    is_engineer = bool(task.assigned_employee and task.assigned_employee.slug == "engineer")
    run_context = dict(run.context_composition_json or {})

    codex = dict(payload.get("codex") or {})
    if is_engineer and codex_founder_gate_required(run, codex):
        # A successful tool process is not acceptance authority. If Codex made
        # any local write before discovering a genuine Founder authority need,
        # restore that exact attempt first. Verification-only WSL limitations
        # are reconciled separately when authoritative Windows host proof passed.
        rollback = __import__(
            "eason_one.services.codex_connector", fromlist=["rollback_completed_write"]
        ).rollback_completed_write(run, reason="FOUNDER_AUTHORITY_REQUIRED")
        if not rollback.get("safe"):
            work_runtime.open_wait(
                work,
                "RECONCILIATION",
                "Codex requires additional Founder authority, but an unaccepted local repository delta cannot be rolled back safely: "
                + str(rollback.get("reason") or "unknown rollback conflict"),
            )
            work_runtime.sync_task_projection(work, task)
            db.session.commit()
            return {
                "status": "RECONCILIATION_REQUIRED",
                "work_id": work.id,
                "run_id": run.id,
            }
        reason = codex.get("founder_reason") or "Engineering work requires authority outside the approved boundary."
        requested_action = _execution_authority_action(
            work, run, escalation_type="CODEX_RISK_APPROVAL", reason=reason, codex=codex,
        )
        receipt = __import__(
            "eason_one.services.governance", fromlist=["approval_receipt"]
        ).approval_receipt(
            project=work.project, work=work, authority_type="CODEX_RISK_APPROVAL",
            requested_action=requested_action,
        )
        if receipt:
            # The exact action was already approved under the still-current
            # Contract/authority hashes. Asking Founder again is a runtime defect,
            # not a new authority question. Never create a duplicate gate.
            run.resolution_status = "APPROVED_EXCEPTION_NOT_CONSUMED"
            run.resolution_note = (
                f"Exact action is already authorized by Founder Decision #{receipt['decision_id']}; "
                "Codex repeated the same authority request instead of consuming the receipt."
            )
            work_runtime.open_wait(
                work, "RECONCILIATION",
                "Codex repeated an already-approved exact authority request; runtime must reconcile without asking Founder again.",
            )
            work_runtime.sync_task_projection(work, task)
            db.session.commit()
            return {
                "status": "RECONCILIATION_REQUIRED", "work_id": work.id,
                "run_id": run.id, "founder_decision_id": receipt["decision_id"],
            }
        _open_founder_gate(
            work, reason, escalation_type="CODEX_RISK_APPROVAL", run=run,
            requested_action=requested_action,
        )
        work_runtime.sync_task_projection(work, task)
        db.session.commit()
        return {"status": "NEEDS_FOUNDER", "work_id": work.id, "run_id": run.id}
    if is_engineer and codex_founder_request_is_verification_only(run, codex):
        context = dict(run.context_composition_json or {})
        context["codex_founder_request_reconciled"] = {
            "reason": codex.get("founder_reason"),
            "basis": "WINDOWS_HOST_ACCEPTANCE_PASSED",
            "at": now().isoformat(),
        }
        run.context_composition_json = context
        run.resolution_status = run.resolution_status or "SYSTEM_HOST_VERIFICATION_SUPERSEDED_CODEX_GATE"
        run.resolution_note = run.resolution_note or (
            "Codex could not run verification in its WSL sandbox, but Eason One's authoritative host verification passed; no Founder authority was required."
        )

    artifact_content = task.result_summary or run.raw_output
    engineering_paths = []
    artifact_location = None
    if is_engineer:
        boundary = dict((work.runtime_control_json or {}).get("codex_execution_boundary") or {})
        engineering_paths = [str(value) for value in (boundary.get("allowed_paths") or []) if str(value or "").strip()]
        repository = str(run_context.get("repository") or boundary.get("repo_path") or "").strip()
        if engineering_paths:
            artifact_content = (artifact_content or "").rstrip() + "\n\nDELIVERABLE PATHS\n" + "\n".join(
                f"- {value}" for value in engineering_paths
            )
        if repository and len(engineering_paths) == 1:
            artifact_location = str(Path(repository) / Path(engineering_paths[0]))
        elif repository:
            artifact_location = repository
    provider_sources = [
        row for row in (run_context.get("provider_sources") or [])
        if isinstance(row,dict) and str(row.get("url") or "").strip()
    ]
    if provider_sources and not is_engineer:
        source_lines=[]
        for index,row in enumerate(provider_sources,1):
            title=str(row.get("title") or "Source").strip()
            url=str(row.get("url") or "").strip()
            date=str(row.get("date") or row.get("last_updated") or "").strip()
            source_lines.append(f"[{index}] {title} — {url}" + (f" ({date})" if date else ""))
        artifact_content = (artifact_content or "").rstrip() + "\n\nPROVIDER-OBSERVED SOURCES\n" + "\n".join(source_lines)

    producer_capability = __import__(
        "eason_one.services.team_formation",fromlist=["infer_primary_capability"]
    ).infer_primary_capability(work)[0]
    artifact_type = (
        "CODE_CHANGE" if is_engineer
        else "RESEARCH_REPORT" if producer_capability == "RESEARCH"
        else "WORK_RESULT"
    )
    version = artifacts.submit_from_execution(
        work, run,
        artifact_type=artifact_type,
        title=work.title,
        content_text=artifact_content,
        content_location=artifact_location if is_engineer else None,
    )

    acceptance = __import__(
        "eason_one.services.acceptance_contract",
        fromlist=["host_criteria", "semantic_criteria"],
    )
    host_items = acceptance.host_criteria(acceptance_contract)
    semantic_items = acceptance.semantic_criteria(acceptance_contract)

    if is_engineer:
        # Codex connector already performed deterministic host validation before
        # declaring this execution SUCCEEDED. Persist that proof independently
        # from Artifact acceptance so mixed contracts can still await semantic
        # review without throwing host evidence away.
        host_validation = dict((run.context_composition_json or {}).get("host_validation") or {})
        all_results = dict(host_validation.get("criterion_results") or {})
        host_ids = {str(item.get("id")) for item in host_items}
        host_results = {
            key: value for key, value in all_results.items() if str(key) in host_ids
        }
        if host_items:
            host_complete = all(
                (host_results.get(str(item.get("id"))) or {}).get("status") == "PASSED"
                for item in host_items
            )
            artifacts.record_verification(
                work,
                version,
                method="HOST_ENGINEERING_VALIDATION",
                status="PASSED" if host_complete else "FAILED",
                verifier_employee_id=None,
                agent_run_id=run.id,
                details={
                    "acceptance_contract_hash": acceptance_contract.get("contract_hash"),
                    "criteria_hash": acceptance_contract.get("criteria_hash"),
                    "criterion_results": host_results,
                    "host_observations": {
                        "repository_delta_match": host_validation.get("repository_delta_match"),
                        "actual_changed_files": host_validation.get("actual_changed_files"),
                        "changed_file_hashes": host_validation.get("changed_file_hashes"),
                        "repository_delta_hash": host_validation.get("repository_delta_hash"),
                        "checks": host_validation.get("checks"),
                    },
                    "artifact_content_hash": version.content_hash,
                    "producing_execution_id": run.id,
                },
            )
            if not host_complete:
                artifacts.reject(
                    work, version, method="HOST_ENGINEERING_VALIDATION_OUTCOME",
                    verifier_employee_id=None, agent_run_id=run.id,
                    details={
                        "acceptance_contract_hash": acceptance_contract.get("contract_hash"),
                        "criterion_results": host_results,
                        "reason": "A deterministic Founder-approved criterion failed authoritative host validation.",
                    },
                )
                retry_status = _schedule_internal_retry(
                    work, run,
                    "A deterministic Founder-approved criterion failed authoritative host validation.",
                )
                work_runtime.sync_task_projection(work, task)
                db.session.commit()
                return {"status": retry_status, "work_id": work.id, "run_id": run.id}

    if semantic_items:
        frozen_reviewer_id = acceptance_contract.get("reviewer_employee_id")
        if not frozen_reviewer_id or frozen_reviewer_id == version.producer_employee_id:
            work_runtime.open_wait(
                work,
                "RECONCILIATION",
                "Semantic acceptance requires a frozen independent reviewer different from the Artifact producer.",
                issue_code=f"WORK_REVIEWER_AUTHORITY_RECONCILIATION:{int(work.id)}",
            )
            work_runtime.sync_task_projection(work, task)
            db.session.commit()
            return {"status": "RECONCILIATION_REQUIRED", "work_id": work.id, "run_id": run.id}
        if work.state != "VERIFYING":
            work_runtime.transition(
                work, "VERIFYING",
                reason="Deterministic proof is preserved; independent reviewer must prove semantic criteria",
            )
        work_runtime.sync_task_projection(work, task)
        db.session.commit()
        return {
            "status": "VERIFYING",
            "work_id": work.id,
            "run_id": run.id,
            "artifact_version_id": version.id,
        }

    # Purely deterministic Work can close only if all frozen criteria are now
    # covered by authoritative proof records for this exact ArtifactVersion.
    accepted = artifacts.finalize_contract_acceptance(
        work, version, contract=acceptance_contract, agent_run_id=run.id,
    )
    if accepted is None:
        retry_status = _schedule_host_proof_retry(
            work, run,
            "Work execution succeeded, but the frozen acceptance contract is not fully proven.",
        )
        work_runtime.sync_task_projection(work, task)
        db.session.commit()
        return {"status": retry_status, "work_id": work.id, "run_id": run.id}

    work_runtime.sync_task_projection(work, task)
    db.session.commit()
    if is_engineer:
        __import__(
            "eason_one.services.codex_connector", fromlist=["cleanup_recovery_snapshot"]
        ).cleanup_recovery_snapshot(run)
    return {"status": "ACCEPTED", "work_id": work.id, "run_id": run.id, "artifact_version_id": version.id}


def review_work(work):
    project = getattr(work, "project", None)
    if work_runtime.project_is_terminal(project):
        return {
            "status": "PROJECT_TERMINAL",
            "project_status": str(getattr(project, "status", "") or "").upper(),
            "work_id": work.id,
        }
    if getattr(project, "status", None) == "PAUSED":
        return {"status": "PROJECT_PAUSED", "work_id": work.id}
    if work.state != "VERIFYING":
        raise ValueError(f"Work #{work.id} is not awaiting review")
    task = _task(work)
    try:
        contract = __import__(
            "eason_one.services.acceptance_contract", fromlist=["ensure_for_work"]
        ).ensure_for_work(work, task=task)
    except ValueError as exc:
        return _handle_acceptance_contract_error(work, task, exc)

    acceptance = __import__(
        "eason_one.services.acceptance_contract", fromlist=["semantic_criteria"]
    )
    semantic = acceptance.semantic_criteria(contract)
    version = _latest_artifact_version(work)
    if not version:
        # Crash window: the paid TASK_EXECUTION may already be durable while the
        # process died before submit_from_execution()/host-proof projection. Reuse
        # that exact result locally; never purchase a second Employee execution.
        durable = _durable_execution_for_work(work, task)
        if durable is not None:
            recovered = execute_work(work, materialize_only=True)
            version = _latest_artifact_version(work)
            if version is not None:
                # Pure deterministic Work may have closed during materialization.
                if work.state == "ACCEPTED":
                    return recovered
            else:
                # Keep the exact durable run as audit truth; an inability to
                # materialize it is a local Company integrity problem, not grounds
                # for replaying the provider call.
                recovered_status = str((recovered or {}).get("status") or "")
                if recovered_status not in {"MATERIALIZATION_SOURCE_UNAVAILABLE", "RECONCILIATION_REQUIRED"}:
                    recovered_status = "RECONCILIATION_REQUIRED"
        if not version:
            work_runtime.open_wait(
                work, "RECONCILIATION",
                "Work reached VERIFYING without a submitted ArtifactVersion; Company runtime must reconcile materialization truth.",
                issue_code=f"WORK_ARTIFACT_MATERIALIZATION_RECONCILIATION:{int(work.id)}",
            )
            db.session.commit()
            return {"status": "RECONCILIATION_REQUIRED", "work_id": work.id}

    if not semantic:
        accepted = artifacts.finalize_contract_acceptance(work, version, contract=contract)
        if accepted is None:
            producer_run = db.session.get(AgentRun, version.execution_id) if version.execution_id else None
            retry_status = _schedule_host_proof_retry(
                work, producer_run,
                "No semantic review is required, but deterministic criterion proof is incomplete.",
            )
            work_runtime.sync_task_projection(work, task)
            db.session.commit()
            return {"status": retry_status, "work_id": work.id}
        work_runtime.sync_task_projection(work, task)
        db.session.commit()
        return {"status": "ACCEPTED", "work_id": work.id}

    reviewer_id = contract.get("reviewer_employee_id")
    reviewer = db.session.get(Employee, reviewer_id) if reviewer_id else None
    if not reviewer_id:
        work_runtime.open_wait(
            work,
            "RECONCILIATION",
            "Semantic acceptance contract has no frozen independent reviewer authority.",
            issue_code=f"WORK_REVIEWER_AUTHORITY_RECONCILIATION:{int(work.id)}",
        )
        work_runtime.sync_task_projection(work, task)
        db.session.commit()
        return {"status": "RECONCILIATION_REQUIRED", "work_id": work.id}
    if reviewer is None or not reviewer.active:
        work_runtime.open_wait(
            work,
            "SYSTEM_RECOVERY",
            "The exact frozen independent reviewer is temporarily unavailable; Company will resume when that same reviewer is active again.",
            issue_code=f"WORK_REVIEWER_UNAVAILABLE:{int(work.id)}:{int(reviewer_id)}",
        )
        work_runtime.sync_task_projection(work, task)
        db.session.commit()
        return {"status": "SYSTEM_RECOVERY_REQUIRED", "work_id": work.id}
    if reviewer.id == version.producer_employee_id or task.reviewer_employee_id != reviewer.id:
        work_runtime.open_wait(
            work,
            "RECONCILIATION",
            "Frozen independent reviewer authority conflicts with the Artifact producer or current compatibility Task projection.",
            issue_code=f"WORK_REVIEWER_AUTHORITY_RECONCILIATION:{int(work.id)}",
        )
        work_runtime.sync_task_projection(work, task)
        db.session.commit()
        return {"status": "RECONCILIATION_REQUIRED", "work_id": work.id}

    criterion_lines = "\n".join(
        f"- {item['id']}: {item['text']}" for item in semantic
    )
    context, composition = build_with_composition(reviewer, work.project, task)
    producer_capability = __import__(
        "eason_one.services.team_formation",fromlist=["infer_primary_capability"]
    ).infer_primary_capability(work)[0]

    # V1.7: the frozen reviewer is also a Persistent Employee. Prior canonical
    # review/owner outcomes may inform review technique and scrutiny, but they
    # are explicitly lower authority than this exact ArtifactVersion, frozen
    # acceptance contract, and host verification evidence.
    learning_text, learning_meta = __import__(
        "eason_one.services.employee_memory", fromlist=["execution_learning_context"]
    ).execution_learning_context(
        reviewer, work.project, task, purpose="TASK_REVIEW", capability=producer_capability
    )
    composition = dict(composition or {})
    learning_meta = dict(learning_meta or {})
    composition["persistent_employee_memory"] = learning_meta
    if learning_text:
        context = (context or "") + "\n\n" + learning_text

    review_target = _review_target(version, contract, work)
    composition["review_target"] = review_target
    host_evidence = _authoritative_host_review_evidence(work, version, contract)
    composition["review_evidence_protocol"] = _REVIEW_EVIDENCE_PROTOCOL
    composition["authoritative_host_verification_ids"] = [
        row.get("verification_record_id")
        for row in (host_evidence.get("host_verifications") or [])
    ]
    context += (
        "\n\nFROZEN ACCEPTANCE CONTRACT\n"
        f"Contract hash: {contract.get('contract_hash')}\n"
        "Review ONLY these semantic criteria; deterministic host criteria are owned by runtime evidence:\n"
        + criterion_lines
        + "\n\nSUBMITTED WORK RESULT\n"
        + (version.content_text or version.content_location or "-")
        + "\n\nAUTHORITATIVE HOST PROOF FOR THIS EXACT ARTIFACT VERSION\n"
        + json.dumps(host_evidence, ensure_ascii=False, sort_keys=True)
    )
    producer_run, provider_sources = _artifact_provider_source_lineage(version)
    if producer_capability == "RESEARCH":
        # Initial Research execution already fails closed when its provider did
        # not return observed sources.  Review the exact persisted lineage from
        # that ArtifactVersion instead of forcing the reviewer to buy/recreate a
        # second web-search trace.  Missing lineage here is a materialization
        # integrity fault and must be repaired before any paid review call.
        if not provider_sources:
            work_runtime.open_wait(
                work, "RECONCILIATION",
                "Research ArtifactVersion has no provider-observed source lineage from its producing Execution; review cannot proceed.",
                issue_code="RESEARCH_ARTIFACT_SOURCE_LINEAGE_MISSING",
            )
            work_runtime.sync_task_projection(work, task)
            db.session.commit()
            return {"status":"RECONCILIATION_REQUIRED","work_id":work.id}
        composition["reviewed_artifact_source_lineage"] = {
            "artifact_version_id": version.id,
            "artifact_content_hash": version.content_hash,
            "producing_execution_id": getattr(producer_run, "id", None),
            "provider_sources": provider_sources,
        }
        context += (
            "\n\nPROVIDER-OBSERVED SOURCE LINEAGE FOR THIS EXACT ARTIFACT VERSION\n"
            + json.dumps(composition["reviewed_artifact_source_lineage"], ensure_ascii=False, sort_keys=True)
        )
    # A crash can happen after the paid reviewer response is durable but before
    # review_work persists VerificationRecord/Work state. Resume that exact Run
    # instead of paying the reviewer a second time.
    prior_review = _latest_review_attempt(work)
    resumable_review = _durable_review_for_target(work, review_target)
    if resumable_review is not None:
        run = resumable_review
    elif prior_review is not None and prior_review.status == "RUNNING":
        return {"status": "REVIEW_IN_PROGRESS", "work_id": work.id, "run_id": prior_review.id}
    elif prior_review is not None and str(prior_review.outcome or "") == "FAILED_AMBIGUOUS":
        reason = prior_review.error_text or (
            "A newer semantic-review dispatch has unresolved provider truth; reconciliation is required before any replay."
        )
        _open_work_effect_reconciliation(work, prior_review, reason)
        work_runtime.sync_task_projection(work, task)
        db.session.commit()
        return {"status": "RECONCILIATION_REQUIRED", "work_id": work.id, "run_id": prior_review.id}
    else:
        # Review actor locality is preserved: the frozen reviewer Employee owns
        # the semantic decision, and execution policy chooses its substrate.
        model = select_execution_model(reviewer, work.operation, "TASK_REVIEW")
        compact_recovery = _compact_review_recovery(work)
        run = execute(
            reviewer,
            "TASK_REVIEW",
            "Independently review the submitted Artifact against every frozen semantic criterion.",
            project=work.project,
            task=task,
            work=work,
            operation=work.operation,
            context_override=context,
            context_composition=composition,
            system_prompt_override=(
                reviewer.system_instructions
                + "\nWORK_REVIEW_V2\n"
                  "Return only the required JSON. Include exactly one criterion_results row for every semantic criterion ID. "
                  "Use PASSED only when persisted Company Truth for this exact ArtifactVersion provides enough evidence. "
                  "That truth includes the submitted Artifact plus AUTHORITATIVE HOST PROOF bound to the same contract hash, artifact version, and content hash. "
                  "A producer statement that WSL could not run verification is an earlier sandbox limitation and must not override a later PASSED host VerificationRecord for facts that record directly observed. "
                  "Do not re-judge HOST_* criteria or invent facts beyond the supplied host observations. Use UNPROVEN when evidence is insufficient. "
                  "Decision ACCEPT is allowed only when every semantic criterion is PASSED."
                  + (
                      " For RESEARCH Work, the provider-observed source lineage bound to the submitted ArtifactVersion is supplied as immutable Company Truth. "
                      "Use that lineage to evaluate source-grounding and claim support; do not claim fresh browsing and do not require the review call itself to emit a second source list."
                      if producer_capability == "RESEARCH" else ""
                  )
                  + (
                      " COMPACT_RECOVERY: the previous review was truncated. Return the shortest sufficient JSON. "
                      "Summary <= 240 characters; each evidence field <= 320 characters; issues/required_changes <= 4 concise rows. "
                      "Do not restate the Artifact, source list, criteria text, or reasoning."
                      if compact_recovery else ""
                  )
            ),
            response_schema=REVIEW_SCHEMA,
            model_override=model,
            max_output_tokens_override=review_output_token_limit(model, semantic),
            prompt_version="work-review-v3-compact-recovery" if compact_recovery else "work-review-v3",
        )
    if run.status != "SUCCEEDED":
        reason = run.error_text or run.failure_reason or "Independent review failed."
        if run.outcome == "FAILED_AMBIGUOUS":
            _open_work_effect_reconciliation(work, run, reason)
            status = "RECONCILIATION_REQUIRED"
        elif run.failure_reason == "FOUNDER_BUDGET_EXTENSION_REQUIRED":
            try:
                _open_founder_gate(work, reason, escalation_type="BUDGET_AUTHORIZATION", run=run)
                status = "NEEDS_FOUNDER"
            except __import__(
                "eason_one.services.governance", fromlist=["FounderAuthorityPreviouslyRejected"]
            ).FounderAuthorityPreviouslyRejected as exc:
                __import__(
                    "eason_one.services.governance", fromlist=["apply_rejected_budget_boundary"]
                ).apply_rejected_budget_boundary(
                    project=work.project, work=work, decision_id=exc.decision_id
                )
                status = "AUTHORITY_EXHAUSTED"
            except ValueError:
                # A model/provider saying "budget" is not authority evidence.
                # Without a deterministic exact positive Project shortfall, this
                # remains Company reconciliation instead of a blank Founder gate.
                work_runtime.open_wait(
                    work, "RECONCILIATION",
                    reason + " Exact positive Project budget shortfall was not proven.",
                )
                status = "RECONCILIATION_REQUIRED"
        elif run.failure_reason == "AUTHORITY_BLOCKED":
            # Generic authority-looking strings are deliberately non-Founder.
            # A precise boundary must be classified by deterministic runtime.
            work_runtime.open_wait(
                work, "RECONCILIATION",
                reason + " Runtime did not identify a precise Founder-only authority type.",
            )
            status = "RECONCILIATION_REQUIRED"
        else:
            status = _schedule_internal_retry(work, run, reason, purpose="TASK_REVIEW")
        db.session.commit()
        return {"status": status, "work_id": work.id, "run_id": run.id}

    try:
        payload = dict(run.parsed_output_json or json.loads(run.raw_output or "{}"))
        if payload.get("decision") not in {"ACCEPT", "REVISE", "BLOCK"}:
            raise ValueError("Invalid review decision")
        expected_ids = [str(item.get("id")) for item in semantic]
        rows = payload.get("criterion_results")
        if not isinstance(rows, list):
            raise ValueError("criterion_results must be a list")
        result_map = {}
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("criterion_results contains a non-object row")
            criterion_id = str(row.get("criterion_id") or "")
            if criterion_id in result_map:
                raise ValueError(f"duplicate semantic criterion result: {criterion_id}")
            if row.get("status") not in {"PASSED", "FAILED", "UNPROVEN"}:
                raise ValueError(f"invalid status for {criterion_id}")
            result_map[criterion_id] = {
                "criterion_id": criterion_id,
                "status": row.get("status"),
                "evidence": str(row.get("evidence") or ""),
                "source": "INDEPENDENT_REVIEW",
            }
        if set(result_map) != set(expected_ids):
            raise ValueError(
                f"semantic criterion coverage mismatch; expected {expected_ids}, got {sorted(result_map)}"
            )
        all_passed = all(result_map[key]["status"] == "PASSED" for key in expected_ids)
        if payload.get("decision") == "ACCEPT" and not all_passed:
            payload["decision"] = "REVISE"
            payload.setdefault("issues", []).append(
                "Runtime overrode ACCEPT because one or more frozen semantic criteria are FAILED or UNPROVEN."
            )
        run.parsed_output_json = payload
        run.structured_validation_status = "PASSED"
        run.structured_validation_errors_json = []
    except Exception as exc:
        run.status = "FAILED"
        run.failure_reason = "STRUCTURED_OUTPUT_INVALID"
        run.outcome = "FAILED_KNOWN"
        run.failure_stage = "POSTPROCESS"
        run.error_text = f"Review validation failed: {exc}"
        db.session.commit()
        status = _schedule_internal_retry(work, run, run.error_text, purpose="TASK_REVIEW")
        db.session.commit()
        return {"status": status, "work_id": work.id, "run_id": run.id}

    existing_verification, existing_message = _existing_review_projection(
        work, version, run, contract
    )
    if existing_message is None:
        db.session.add(WorkMessage(
            project_id=work.project_id,
            task_id=task.id,
            sender_employee_id=reviewer.id,
            recipient_employee_id=task.assigned_employee_id,
            message_type="REVIEW",
            content=payload.get("summary") or payload["decision"],
            agent_run_id=run.id,
        ))
    overall_status = (
        "PASSED" if all(row["status"] == "PASSED" for row in result_map.values())
        else "FAILED" if any(row["status"] == "FAILED" for row in result_map.values())
        else "UNPROVEN"
    )
    if existing_verification is None:
        artifacts.record_verification(
            work,
            version,
            method="INDEPENDENT_REVIEW",
            status=overall_status,
            verifier_employee_id=reviewer.id,
            agent_run_id=run.id,
            details={
                "acceptance_contract_hash": contract.get("contract_hash"),
                "criteria_hash": contract.get("criteria_hash"),
                "criterion_results": result_map,
                "decision": payload.get("decision"),
                "summary": payload.get("summary"),
                "issues": payload.get("issues") or [],
                "required_changes": payload.get("required_changes") or [],
                "artifact_content_hash": version.content_hash,
                "artifact_producer_employee_id": version.producer_employee_id,
                "reviewer_employee_id": reviewer.id,
            },
        )

    decision = payload["decision"]
    if decision == "ACCEPT" and overall_status == "PASSED":
        accepted = artifacts.finalize_contract_acceptance(
            work, version, contract=contract, agent_run_id=run.id,
        )
        if accepted is None:
            producer_run = db.session.get(AgentRun, version.execution_id) if version.execution_id else None
            retry_status = _schedule_host_proof_retry(
                work, producer_run or run,
                "Independent review passed, but another frozen criterion still lacks authoritative proof.",
            )
            work_runtime.sync_task_projection(work, task)
            db.session.commit()
            return {"status": retry_status, "work_id": work.id, "run_id": run.id}
        producer_run = db.session.get(AgentRun, version.execution_id) if version.execution_id else None
        work_runtime.sync_task_projection(work, task)
        db.session.commit()
        if producer_run and (producer_run.context_composition_json or {}).get("codex_recovery"):
            __import__(
                "eason_one.services.codex_connector", fromlist=["cleanup_recovery_snapshot"]
            ).cleanup_recovery_snapshot(producer_run)
        return {"status": "ACCEPTED", "work_id": work.id, "run_id": run.id}

    # UNPROVEN is an evidence state, not an implementation failure. Retry only
    # the independent review path while preserving the exact ArtifactVersion +
    # host proof. Once bounded review attempts are exhausted, terminate this
    # Mission as an evidence failure so Project-level continuation can collect
    # missing proof without replaying Codex implementation.
    if overall_status == "UNPROVEN":
        reason = payload.get("summary") or (
            "Artifact implementation is not disproven, but semantic evidence is incomplete."
        )
        status = _schedule_evidence_review_retry(work, run, version, contract, reason)
        __import__(
            "eason_one.services.company_events", fromlist=["emit"]
        ).emit(
            "WORK_EVIDENCE_UNPROVEN", actor_type="RUNTIME",
            project_id=work.project_id, work_id=work.id, execution_id=run.id,
            correlation_id=f"work:{work.id}",
            payload={
                "artifact_version_id": version.id,
                "acceptance_contract_hash": contract.get("contract_hash"),
                "criterion_results": result_map,
                "implementation_replayed": False,
                "artifact_rejected": False,
                "next_status": status,
            },
        )
        work_runtime.sync_task_projection(work, task)
        db.session.commit()
        return {
            "status": status, "work_id": work.id,
            "run_id": run.id, "artifact_version_id": version.id,
        }

    producer_run = db.session.get(AgentRun, version.execution_id) if version.execution_id else None
    if producer_run and (producer_run.context_composition_json or {}).get("codex_recovery"):
        rollback = __import__(
            "eason_one.services.codex_connector", fromlist=["rollback_completed_write"]
        ).rollback_completed_write(producer_run, reason="ARTIFACT_REJECTED")
        if not rollback.get("safe"):
            artifacts.reject(
                work, version, method="INDEPENDENT_REVIEW_OUTCOME",
                verifier_employee_id=reviewer.id, agent_run_id=run.id,
                details={
                    "acceptance_contract_hash": contract.get("contract_hash"),
                    "criterion_results": result_map,
                    "decision": decision,
                    "summary": payload.get("summary"),
                    "rollback": rollback,
                },
            )
            work_runtime.open_wait(
                work, "RECONCILIATION",
                "Independent review rejected a code Artifact, but its local repository delta cannot be rolled back safely: "
                + str(rollback.get("reason") or "unknown rollback conflict"),
            )
            work_runtime.sync_task_projection(work, task)
            db.session.commit()
            return {"status": "RECONCILIATION_REQUIRED", "work_id": work.id, "run_id": run.id}

    artifacts.reject(
        work,
        version,
        method="INDEPENDENT_REVIEW_OUTCOME",
        verifier_employee_id=reviewer.id,
        agent_run_id=run.id,
        details={
            "acceptance_contract_hash": contract.get("contract_hash"),
            "criterion_results": result_map,
            "decision": decision,
            "summary": payload.get("summary"),
        },
    )
    status = _schedule_internal_retry(
        work,
        run,
        payload.get("summary") or "Independent review did not prove the frozen semantic acceptance criteria.",
        purpose="TASK_EXECUTION",
        replay_authority_run=producer_run or run,
    )
    work_runtime.sync_task_projection(work, task)
    db.session.commit()
    return {"status": status, "work_id": work.id, "run_id": run.id}
