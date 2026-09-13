"""Process/restart recovery for the v0.20 Work-first Company Kernel.

Recovery classifies durable execution/effect truth. It never decides Project
success/failure and never replays a post-dispatch effect by guesswork.
"""
from __future__ import annotations

from decimal import Decimal

from ..extensions import db
from ..models import (
    AgentRun, Artifact, ArtifactVersion, CostReservation, Escalation, ExternalEffectAttempt, Meeting, MeetingStep, VerificationRecord, Work, now,
)
from . import work_runtime
from .company_events import emit


def _is_kernel_work(work: Work) -> bool:
    # Founder pause and terminal Project lifecycle are durable execution fences.
    # Recovery may still settle already-observed provider/cost truth in the
    # dedicated settlement pass, but it must not repair gates, reopen Work,
    # mutate staffing, or manufacture a new runnable state for those Projects.
    project = getattr(work, "project", None)
    if project is not None and work_runtime.project_execution_fenced(project):
        return False
    return bool(work.operation and __import__(
        "eason_one.services.core_v018", fromlist=["is_v018_operation"]
    ).is_v018_operation(work.operation))


def resolve_internal_waits() -> int:
    resolved = 0
    current = now()
    for work in Work.query.filter_by(state="WAITING").order_by(Work.id).all():
        if not _is_kernel_work(work):
            continue
        for gate in list(work_runtime.open_gates(work)):
            kind = gate.get("condition_type")
            if kind == "DEPENDENCY":
                issue = str(gate.get("issue_code") or "")
                task = work_runtime.task_for_work(work)

                # Adopt the exact durable shape left by S07.  Its missing-evidence
                # DEPENDENCY gate had no issue_code, so generic dependency recovery
                # treated an edge-less evidence wait as already satisfied and woke
                # it every tick.  Detect that lineage from the latest local-safe
                # preflight Run, then convert the old gate to the S08 exact owner
                # without any provider dispatch.
                if not issue and task is not None:
                    latest = (
                        AgentRun.query.filter_by(work_id=work.id, purpose="TASK_EXECUTION")
                        .order_by(AgentRun.id.desc()).first()
                    )
                    if latest is not None and str(latest.failure_reason or "") == "MISSING_EVIDENCE":
                        _rows, meta = __import__(
                            "eason_one.services.context", fromlist=["relevant_existing_evidence"]
                        ).relevant_existing_evidence(task)
                        if int((meta or {}).get("relevant_items") or 0) <= 0:
                            basis_hash = str((meta or {}).get("basis_hash") or "")
                            adopted_issue = (
                                f"MISSING_EVIDENCE:{basis_hash}"
                                if basis_hash else f"MISSING_EVIDENCE:RUN:{int(latest.id)}"
                            )
                            changed = work_runtime.resolve_waits(
                                work, "DEPENDENCY",
                                note="S08 adopted the S07 edge-less missing-evidence wait into an exact evidence gate.",
                            )
                            if changed:
                                work_runtime.open_wait(
                                    work, "DEPENDENCY",
                                    latest.error_text or "Required persisted evidence is missing.",
                                    issue_code=adopted_issue,
                                    resume_state="EXECUTING",
                                )
                                resolved += changed
                            break
                        # New durable evidence appeared while Eason One was off.
                        # Resolve the old S07 gate and let ordinary sequencing
                        # consume that evidence exactly once.
                        resolved += work_runtime.resolve_waits(
                            work, "DEPENDENCY",
                            note="Persisted evidence now exists; the S07 missing-evidence wait may resume.",
                        )
                        break

                if issue.startswith("MISSING_EVIDENCE:"):
                    # Evidence-only preflight is not a WorkDependency edge.  Re-probe
                    # only deterministic local evidence; no provider call is made.
                    if task is None:
                        continue
                    _rows, meta = __import__(
                        "eason_one.services.context", fromlist=["relevant_existing_evidence"]
                    ).relevant_existing_evidence(task)
                    if int((meta or {}).get("relevant_items") or 0) <= 0:
                        continue
                    resolved += work_runtime.resolve_waits(
                        work, "DEPENDENCY", issue_code=issue,
                        note=(
                            "New persisted evidence now satisfies the evidence-only preflight; "
                            "the Work may resume without replaying the prior missing-evidence attempt."
                        ),
                    )
                    break
                if work_runtime.dependencies_satisfied(work):
                    resolved += work_runtime.resolve_waits(work, "DEPENDENCY", note="Upstream Work is accepted.")
                    break
            if kind in {
                "INTERNAL_RECOVERY", "RETRY_BACKOFF", "EVIDENCE_REVIEW_RETRY", "HOST_PROOF_RETRY"
            } and work_runtime.retry_due(gate, current):
                note = (
                    "Bounded independent evidence re-review is due."
                    if kind == "EVIDENCE_REVIEW_RETRY"
                    else "Deterministic host proof revalidation is due without implementation replay."
                    if kind == "HOST_PROOF_RETRY"
                    else "Bounded internal retry is due."
                )
                issue_code = gate.get("issue_code")
                kwargs = {"issue_code": issue_code} if issue_code else {}
                resolved += work_runtime.resolve_waits(work, kind, note=note, **kwargs)
                break
    if resolved:
        db.session.commit()
    return resolved


def resolve_settled_orchestration_reconciliation() -> int:
    """Retire only exact topology-effect gates whose durable truth is no longer unresolved.

    The orchestration planner opens ORCHESTRATION_RUN_<id>_EFFECT_UNRESOLVED
    when replay is unsafe. If later settlement/reconciliation proves that exact
    run succeeded, failed before dispatch, was definitively rejected, or only
    needs local postprocess, the old gate must not permanently prevent the
    Company from projecting/reusing the already-paid truth. This maintenance
    performs zero provider calls and resolves no unscoped reconciliation.
    """
    prefix = "ORCHESTRATION_RUN_"
    suffix = "_EFFECT_UNRESOLVED"
    changed = 0
    for work in Work.query.filter_by(state="WAITING", work_type="MANAGEMENT").order_by(Work.id).all():
        if not _is_kernel_work(work):
            continue
        for gate in list(work_runtime.open_gates(work)):
            if str(gate.get("condition_type") or "").upper() != "RECONCILIATION":
                continue
            issue = str(gate.get("issue_code") or "")
            if not (issue.startswith(prefix) and issue.endswith(suffix)):
                continue
            raw_id = issue[len(prefix):-len(suffix)]
            try:
                run_id = int(raw_id)
            except (TypeError, ValueError):
                continue
            run = db.session.get(AgentRun, run_id)
            if (
                run is None
                or int(getattr(run, "work_id", 0) or 0) != int(work.id)
                or str(getattr(run, "purpose", "") or "") != "ORCHESTRATION_PLAN"
                or int(getattr(run, "operation_id", 0) or 0) != int(getattr(work, "operation_id", 0) or 0)
            ):
                continue
            effect = (
                ExternalEffectAttempt.query.filter_by(execution_id=run.id)
                .order_by(ExternalEffectAttempt.id.desc()).first()
            )
            effect_state = str(getattr(effect, "state", "") or "").upper()
            outcome = str(getattr(run, "outcome", "") or "").upper()
            status = str(getattr(run, "status", "") or "").upper()
            still_unresolved = (
                status == "RUNNING"
                or outcome == "FAILED_AMBIGUOUS"
                or effect_state in {"DISPATCHING", "RESPONSE_RECEIVED", "AMBIGUOUS_POST_DISPATCH"}
            )
            if still_unresolved:
                continue
            reusable_paid_truth = (
                (status == "SUCCEEDED" and effect_state in {"PERSISTED", "SETTLED"})
                or (
                    getattr(run, "failure_reason", None) == "POSTPROCESS_FAILED"
                    and bool(getattr(run, "raw_output", None))
                    and effect_state in {"PERSISTED", "SETTLED"}
                )
            )
            definitive_failure = outcome in {"FAILED_SAFE", "FAILED_KNOWN"}
            if not (reusable_paid_truth or definitive_failure):
                continue
            resolved = work_runtime.resolve_waits(
                work, "RECONCILIATION", issue_code=issue,
                note=(
                    f"Orchestration Run #{run.id} no longer has unresolved external-effect truth; "
                    "normal Company sequencing may now reuse/project the durable result or deterministic fallback."
                ),
            )
            if not resolved:
                continue
            project = work.project
            if project is not None and work_runtime.project_can_activate(project):
                project.status = "ACTIVE"
                project.current_state_summary = (
                    "Company reconciled the exact topology effect; normal sequencing can resume without blind replay."
                )
            emit(
                "ORCHESTRATION_RECONCILIATION_RESOLVED", actor_type="RUNTIME",
                project_id=work.project_id, work_id=work.id, correlation_id=f"work:{work.id}",
                payload={
                    "run_id": run.id, "issue_code": issue, "run_status": run.status,
                    "run_outcome": run.outcome, "effect_state": effect_state,
                    "provider_dispatched_by_recovery": False,
                },
            )
            changed += 1
            break
    if changed:
        db.session.commit()
    return changed


def resolve_settled_project_management_reconciliation() -> int:
    """Retire exact outcome-review/continuation reconciliation after settlement.

    Project management uses one persistent MANAGEMENT Work for several paid
    decision effects. A post-dispatch ambiguous attempt must block replay, but
    once durable settlement makes that exact AgentRun non-ambiguous the old gate
    must not strand the Project forever. Maintenance performs zero provider calls
    and never resolves unscoped or unrelated reconciliation. Normal sequencing
    then reuses a paid success or applies ordinary bounded recovery to a known
    failure.
    """
    prefix = "PROJECT_MANAGEMENT_RECONCILIATION:"
    allowed = {"PROJECT_OUTCOME_REVIEW", "CEO_PROJECT_CONTINUATION"}
    changed = 0
    for work in Work.query.filter_by(state="WAITING", work_type="MANAGEMENT").order_by(Work.id).all():
        if not _is_kernel_work(work):
            continue
        for gate in list(work_runtime.open_gates(work)):
            if str(gate.get("condition_type") or "").upper() != "RECONCILIATION":
                continue
            issue = str(gate.get("issue_code") or "")
            if not issue.startswith(prefix):
                continue
            parts = issue.split(":", 2)
            if len(parts) != 3:
                continue
            purpose = str(parts[1] or "").upper()
            if purpose not in allowed:
                continue
            try:
                run_id = int(parts[2])
            except (TypeError, ValueError):
                continue
            run = db.session.get(AgentRun, run_id)
            if (
                run is None
                or int(getattr(run, "work_id", 0) or 0) != int(work.id)
                or str(getattr(run, "purpose", "") or "").upper() != purpose
                or int(getattr(run, "project_id", 0) or 0) != int(getattr(work, "project_id", 0) or 0)
            ):
                continue
            effect = (
                ExternalEffectAttempt.query.filter_by(execution_id=run.id)
                .order_by(ExternalEffectAttempt.id.desc()).first()
            )
            effect_state = str(getattr(effect, "state", "") or "").upper()
            status = str(getattr(run, "status", "") or "").upper()
            outcome = str(getattr(run, "outcome", "") or "").upper()
            still_unresolved = (
                status == "RUNNING"
                or outcome == "FAILED_AMBIGUOUS"
                or effect_state in {"DISPATCHING", "RESPONSE_RECEIVED", "AMBIGUOUS_POST_DISPATCH"}
            )
            if still_unresolved:
                continue
            reusable_paid_truth = (
                (status == "SUCCEEDED" and effect_state in {"PERSISTED", "SETTLED", ""})
                or (
                    getattr(run, "failure_reason", None) == "POSTPROCESS_FAILED"
                    and bool(getattr(run, "raw_output", None))
                    and effect_state in {"PERSISTED", "SETTLED"}
                )
            )
            definitive_failure = outcome in {"FAILED_SAFE", "FAILED_KNOWN"}
            if not (reusable_paid_truth or definitive_failure):
                continue
            resolved = work_runtime.resolve_waits(
                work, "RECONCILIATION", issue_code=issue,
                note=(
                    f"{purpose} Run #{run.id} no longer has unresolved external-effect truth; "
                    "normal Project sequencing may reuse/project the durable result or bounded recovery."
                ),
            )
            if not resolved:
                continue
            project = work.project
            if project is not None and work_runtime.project_can_activate(project):
                project.status = "ACTIVE"
                project.current_state_summary = (
                    "Company reconciled the exact Project-management effect; normal sequencing can resume without blind replay."
                )
            emit(
                "PROJECT_MANAGEMENT_RECONCILIATION_RESOLVED", actor_type="RUNTIME",
                project_id=work.project_id, work_id=work.id, correlation_id=f"work:{work.id}",
                payload={
                    "purpose": purpose, "run_id": run.id, "issue_code": issue,
                    "run_status": run.status, "run_outcome": run.outcome,
                    "effect_state": effect_state, "provider_dispatched_by_recovery": False,
                },
            )
            changed += 1
            break
    if changed:
        db.session.commit()
    return changed


def resolve_project_result_proof_reconciliation() -> int:
    """Retire only the exact Result Ready proof gate once proof reads are healthy again.

    A REVIEW Project can fail closed when result_ready_proof() itself raises due
    to a transient data/code invariant.  Persisted Result Ready truth must not be
    stranded after that invariant is repaired.  This maintenance only probes the
    current durable proof and resolves its exact gate; it never creates a result,
    calls a provider, or changes Founder authority.
    """
    prefix = "PROJECT_RESULT_PROOF_RECONCILIATION:"
    changed = 0
    outcome = __import__(
        "eason_one.services.project_outcome", fromlist=["result_ready_proof"]
    )
    for work in Work.query.filter_by(state="WAITING", work_type="MANAGEMENT").order_by(Work.id).all():
        if not _is_kernel_work(work) or work.project is None:
            continue
        project = work.project
        issue = f"{prefix}{int(project.id)}"
        gate = next((
            row for row in work_runtime.open_gates(work)
            if str(row.get("condition_type") or "").upper() == "RECONCILIATION"
            and str(row.get("issue_code") or "") == issue
        ), None)
        if gate is None:
            continue
        try:
            proof_is_current = bool(outcome.result_ready_proof(project))
        except Exception:
            continue
        resolved = work_runtime.resolve_waits(
            work, "RECONCILIATION", issue_code=issue,
            note=(
                "Project Result proof can be evaluated again; normal Company sequencing owns "
                "Result Ready preservation or evidence reconciliation."
            ),
        )
        if not resolved:
            continue
        remaining = work_runtime.open_gates(work)
        if not remaining and proof_is_current:
            project.status = "REVIEW"
            project.current_state_summary = (
                "Current Result Ready proof is readable again; the existing durable result remains the Company truth."
            )
        elif not remaining and work_runtime.project_can_activate(project):
            project.status = "ACTIVE"
            project.current_state_summary = (
                "Result proof validation recovered; Company outcome reconciliation can resume from current durable evidence."
            )
        emit(
            "PROJECT_RESULT_PROOF_RECONCILIATION_RESOLVED", actor_type="RUNTIME",
            project_id=project.id, work_id=work.id, correlation_id=f"project:{project.id}",
            payload={
                "issue_code": issue, "proof_current": proof_is_current,
                "provider_dispatched_by_recovery": False,
            },
        )
        changed += 1
    if changed:
        db.session.commit()
    return changed


def resolve_project_outcome_evaluation_reconciliation() -> int:
    """Retire one exact Project outcome-evaluation gate after the invariant is repaired.

    evaluate(project) is deterministic Project evidence interpretation.  If it
    once raised, the Project must fail closed; but after source/data repair the
    old reconciliation gate cannot permanently suppress the normal Project tick.
    This function only proves that evaluation can execute again and resolves the
    exact project+operation owner.
    """
    prefix = "PROJECT_OUTCOME_EVALUATION_RECONCILIATION:"
    changed = 0
    outcome = __import__(
        "eason_one.services.project_outcome", fromlist=["evaluate"]
    )
    for work in Work.query.filter_by(state="WAITING", work_type="MANAGEMENT").order_by(Work.id).all():
        if not _is_kernel_work(work) or work.project is None or work.operation is None:
            continue
        project = work.project
        operation = work.operation
        issue = f"{prefix}{int(project.id)}:{int(operation.id)}"
        gate = next((
            row for row in work_runtime.open_gates(work)
            if str(row.get("condition_type") or "").upper() == "RECONCILIATION"
            and str(row.get("issue_code") or "") == issue
        ), None)
        if gate is None:
            continue
        try:
            evaluation = outcome.evaluate(project)
        except Exception:
            continue
        resolved = work_runtime.resolve_waits(
            work, "RECONCILIATION", issue_code=issue,
            note=(
                "Project outcome evidence can be evaluated again; normal Company sequencing will "
                "decide Result Ready or bounded continuation."
            ),
        )
        if not resolved:
            continue
        if work_runtime.project_can_activate(project):
            project.status = "ACTIVE"
            project.current_state_summary = (
                "Project outcome evaluation recovered; Company sequencing can continue from existing durable evidence."
            )
        emit(
            "PROJECT_OUTCOME_EVALUATION_RECONCILIATION_RESOLVED", actor_type="RUNTIME",
            project_id=project.id, work_id=work.id, correlation_id=f"project:{project.id}",
            payload={
                "issue_code": issue,
                "overall_status": (evaluation or {}).get("overall_status") if isinstance(evaluation, dict) else None,
                "provider_dispatched_by_recovery": False,
            },
        )
        changed += 1
    if changed:
        db.session.commit()
    return changed


def resolve_settled_work_effect_reconciliation() -> int:
    """Retire exact delivery Work effect gates once durable truth is settled.

    TASK_EXECUTION/TASK_REVIEW may fail closed on a post-dispatch ambiguity while
    the provider settlement is still unknown.  Once the exact AgentRun/effect is
    no longer ambiguous, the old gate must not permanently strand the Work.  This
    maintenance performs zero provider calls and does not itself authorize a
    replay; normal Work execution/review decides reuse or bounded recovery from
    the now-durable run truth.
    """
    prefix = "WORK_EFFECT_RECONCILIATION:"
    allowed = {"TASK_EXECUTION", "TASK_REVIEW"}
    changed = 0
    for work in Work.query.filter_by(state="WAITING").order_by(Work.id).all():
        if not _is_kernel_work(work) or work.work_type == "MANAGEMENT":
            continue
        for gate in list(work_runtime.open_gates(work)):
            if str(gate.get("condition_type") or "").upper() != "RECONCILIATION":
                continue
            issue = str(gate.get("issue_code") or "")
            if not issue.startswith(prefix):
                continue
            parts = issue.split(":", 2)
            if len(parts) != 3:
                continue
            purpose = str(parts[1] or "").upper()
            if purpose not in allowed:
                continue
            try:
                run_id = int(parts[2])
            except (TypeError, ValueError):
                continue
            run = db.session.get(AgentRun, run_id)
            if (
                run is None
                or int(getattr(run, "work_id", 0) or 0) != int(work.id)
                or str(getattr(run, "purpose", "") or "").upper() != purpose
                or int(getattr(run, "project_id", 0) or 0) != int(getattr(work, "project_id", 0) or 0)
                or int(getattr(run, "operation_id", 0) or 0) != int(getattr(work, "operation_id", 0) or 0)
            ):
                continue
            effect = (
                ExternalEffectAttempt.query.filter_by(execution_id=run.id)
                .order_by(ExternalEffectAttempt.id.desc()).first()
            )
            if effect is None:
                # An effect-less ambiguous run cannot be proven settled here.
                continue
            effect_state = str(getattr(effect, "state", "") or "").upper()
            status = str(getattr(run, "status", "") or "").upper()
            outcome = str(getattr(run, "outcome", "") or "").upper()
            still_unresolved = (
                status == "RUNNING"
                or outcome == "FAILED_AMBIGUOUS"
                or effect_state in {"DISPATCHING", "RESPONSE_RECEIVED", "AMBIGUOUS_POST_DISPATCH"}
            )
            if still_unresolved:
                continue
            reusable_paid_truth = (
                (status == "SUCCEEDED" and effect_state in {"PERSISTED", "SETTLED"})
                or (
                    getattr(run, "failure_reason", None) == "POSTPROCESS_FAILED"
                    and bool(getattr(run, "raw_output", None))
                    and effect_state in {"PERSISTED", "SETTLED"}
                )
            )
            definitive_failure = outcome in {"FAILED_SAFE", "FAILED_KNOWN"}
            if not (reusable_paid_truth or definitive_failure):
                continue
            resolved = work_runtime.resolve_waits(
                work, "RECONCILIATION", issue_code=issue,
                note=(
                    f"{purpose} Run #{run.id} no longer has unresolved provider-effect truth; "
                    "normal Work sequencing may reuse/project durable output or apply bounded recovery without blind replay."
                ),
            )
            if not resolved:
                continue
            try:
                task = __import__(
                    "eason_one.services.work_runtime", fromlist=["task_for_work", "sync_task_projection"]
                ).task_for_work(work)
                if task is not None:
                    work_runtime.sync_task_projection(work, task)
            except Exception:
                # Gate ownership is authoritative even if the compatibility Task
                # projection is unavailable; do not reopen or clear other gates.
                pass
            project = work.project
            if project is not None and work_runtime.project_can_activate(project):
                project.status = "ACTIVE"
                project.current_state_summary = (
                    f"{work.title} provider-effect truth is settled; Company sequencing can resume without replaying an unknown effect."
                )
            emit(
                "WORK_EFFECT_RECONCILIATION_RESOLVED", actor_type="RUNTIME",
                project_id=work.project_id, work_id=work.id, execution_id=run.id,
                correlation_id=f"work:{work.id}",
                payload={
                    "purpose": purpose, "run_id": run.id, "issue_code": issue,
                    "run_status": run.status, "run_outcome": run.outcome,
                    "effect_state": effect_state, "provider_dispatched_by_recovery": False,
                },
            )
            changed += 1
            break
    if changed:
        db.session.commit()
    return changed


def resolve_repaired_work_integrity_waits() -> int:
    """Retire exact Work integrity/staffing gates after deterministic repair.

    These gates represent local Company truth, not Founder authority and not a
    license to replay a provider effect.  Maintenance only proves that the exact
    frozen contract/reviewer/artifact lineage is usable again, then resolves the
    matching issue.  Normal Work sequencing performs any later materialization
    or paid review.  This routine itself performs zero provider calls.
    """
    changed = 0
    work_execution = __import__(
        "eason_one.services.work_execution",
        fromlist=[
            "_task", "_latest_artifact_version", "_durable_execution_for_work",
            "_artifact_provider_source_lineage",
        ],
    )
    acceptance_contract = __import__(
        "eason_one.services.acceptance_contract", fromlist=["ensure_for_work"]
    )
    Employee = __import__("eason_one.models", fromlist=["Employee"]).Employee

    for work in Work.query.filter_by(state="WAITING").order_by(Work.id).all():
        if not _is_kernel_work(work) or work.work_type == "MANAGEMENT":
            continue
        try:
            task = work_execution._task(work)
        except Exception:
            continue
        gates = list(work_runtime.open_gates(work))
        for gate in gates:
            kind = str(gate.get("condition_type") or "").upper()
            issue = str(gate.get("issue_code") or "")
            resolved = 0
            note = None

            if issue == f"WORK_ACCEPTANCE_CONTRACT_RECONCILIATION:{int(work.id)}" and kind == "RECONCILIATION":
                try:
                    acceptance_contract.ensure_for_work(work, task=task)
                except Exception:
                    continue
                note = "The exact frozen Work acceptance contract is valid again; normal Work sequencing may resume."
                resolved = work_runtime.resolve_waits(
                    work, "RECONCILIATION", issue_code=issue, note=note,
                )

            elif issue == f"WORK_ARTIFACT_MATERIALIZATION_RECONCILIATION:{int(work.id)}" and kind == "RECONCILIATION":
                version = work_execution._latest_artifact_version(work)
                durable = work_execution._durable_execution_for_work(work, task) if version is None else None
                if version is None and durable is None:
                    continue
                note = (
                    "An exact submitted ArtifactVersion is now present."
                    if version is not None
                    else "An exact already-paid successful execution is available for zero-call local Artifact materialization."
                )
                resolved = work_runtime.resolve_waits(
                    work, "RECONCILIATION", issue_code=issue, note=note,
                )

            elif issue.startswith("WORK_REVIEWER_UNAVAILABLE:") and kind == "SYSTEM_RECOVERY":
                parts = issue.split(":")
                if len(parts) != 3:
                    continue
                try:
                    issue_work_id = int(parts[1]); reviewer_id = int(parts[2])
                except (TypeError, ValueError):
                    continue
                if issue_work_id != int(work.id) or reviewer_id <= 0:
                    continue
                try:
                    contract = acceptance_contract.ensure_for_work(work, task=task)
                except Exception:
                    continue
                reviewer = db.session.get(Employee, reviewer_id)
                version = work_execution._latest_artifact_version(work)
                if (
                    reviewer is None or not reviewer.active or version is None
                    or int(contract.get("reviewer_employee_id") or 0) != reviewer_id
                    or int(getattr(task, "reviewer_employee_id", 0) or 0) != reviewer_id
                    or int(getattr(version, "producer_employee_id", 0) or 0) == reviewer_id
                ):
                    continue
                note = "The same frozen independent reviewer is active and lawful again; normal review may resume."
                resolved = work_runtime.resolve_waits(
                    work, "SYSTEM_RECOVERY", issue_code=issue, note=note,
                )

            elif issue == f"WORK_REVIEWER_AUTHORITY_RECONCILIATION:{int(work.id)}" and kind == "RECONCILIATION":
                try:
                    contract = acceptance_contract.ensure_for_work(work, task=task)
                except Exception:
                    continue
                reviewer_id = int(contract.get("reviewer_employee_id") or 0)
                reviewer = db.session.get(Employee, reviewer_id) if reviewer_id else None
                version = work_execution._latest_artifact_version(work)
                if (
                    reviewer is None or not reviewer.active or version is None
                    or int(getattr(task, "reviewer_employee_id", 0) or 0) != reviewer_id
                    or int(getattr(version, "producer_employee_id", 0) or 0) == reviewer_id
                ):
                    continue
                note = "Frozen reviewer authority and Artifact producer separation are consistent again."
                resolved = work_runtime.resolve_waits(
                    work, "RECONCILIATION", issue_code=issue, note=note,
                )

            elif issue == "RESEARCH_ARTIFACT_SOURCE_LINEAGE_MISSING" and kind == "RECONCILIATION":
                version = work_execution._latest_artifact_version(work)
                if version is None:
                    continue
                _run, sources = work_execution._artifact_provider_source_lineage(version)
                if not sources:
                    continue
                note = "Provider-observed source lineage is present again for the exact Research ArtifactVersion."
                resolved = work_runtime.resolve_waits(
                    work, "RECONCILIATION", issue_code=issue, note=note,
                )

            if not resolved:
                continue
            try:
                work_runtime.sync_task_projection(work, task)
            except Exception:
                pass
            project = work.project
            if project is not None and work_runtime.project_can_activate(project):
                project.status = "ACTIVE"
                project.current_state_summary = (
                    f"{work.title} local integrity/reviewer readiness recovered; Company sequencing can continue."
                )
            emit(
                "WORK_INTEGRITY_RECOVERY_RESOLVED", actor_type="RUNTIME",
                project_id=work.project_id, work_id=work.id,
                correlation_id=f"work:{work.id}",
                payload={
                    "issue_code": issue, "condition_type": kind,
                    "provider_dispatched_by_recovery": False,
                },
            )
            changed += 1
            break
    if changed:
        db.session.commit()
    return changed


def resolve_safe_system_recovery_waits() -> int:
    """Resume only SYSTEM_RECOVERY waits with a provably safe untried path.

    SYSTEM_RECOVERY is intentionally not a generic timer.  Most such waits need a
    code/config repair or human investigation.  This routine handles the narrow
    subset where durable execution truth proves the prior effect is replay-safe
    *and* current Project policy exposes another lawful ModelConfig that has not
    already appeared in the retry chain.  It never calls a provider itself; it
    stores the exact candidate and lets normal Work execution perform the guarded
    retry. Ambiguous post-dispatch effects and Founder authority remain untouched.
    """
    recovered = 0
    work_execution = __import__(
        "eason_one.services.work_execution",
        fromlist=["automatic_retry_allowed", "automatic_recovery_model", "_task"],
    )
    external_effects = __import__(
        "eason_one.services.external_effects", fromlist=["retry_authorized"]
    )
    for work in Work.query.filter_by(state="WAITING").order_by(Work.id).all():
        if not _is_kernel_work(work) or work.work_type == "MANAGEMENT":
            continue
        control = dict(work.runtime_control_json or {})
        if control.get("safe_system_recovery_retry"):
            continue
        matching = None
        for gate in work_runtime.open_gates(work):
            if gate.get("condition_type") != "SYSTEM_RECOVERY":
                continue
            issue = str(gate.get("issue_code") or "")
            if issue == "RESEARCH_TOOL_UNAVAILABLE" or issue.startswith("PROVIDER_REQUEST_REJECTED_"):
                matching = dict(gate)
                break
        if matching is None or not matching.get("issue_code"):
            continue
        latest = (
            AgentRun.query.filter_by(work_id=work.id, purpose="TASK_EXECUTION")
            .order_by(AgentRun.id.desc()).first()
        )
        try:
            task = work_execution._task(work)
        except ValueError:
            continue
        if latest is None or not work_execution.automatic_retry_allowed(latest, task):
            continue
        replay_ok, _reason = external_effects.retry_authorized(latest)
        if not replay_ok:
            continue
        candidate = work_execution.automatic_recovery_model(work, task, latest)
        if candidate is None:
            continue
        control["safe_system_recovery_retry"] = {
            "source_run_id": latest.id,
            "model_config_id": candidate.id,
            "provider": candidate.provider_key,
            "model": candidate.model_name,
            "issue_code": str(matching["issue_code"]),
            "authorized_at": now().isoformat(),
            "provider_dispatched_by_recovery": False,
        }
        work.runtime_control_json = control
        db.session.flush()
        resolved = work_runtime.resolve_waits(
            work, "SYSTEM_RECOVERY",
            issue_code=str(matching["issue_code"]),
            note=(
                f"Company recovery found an untried lawful model path ({candidate.provider_key}/{candidate.model_name}); "
                "normal Work execution will perform the guarded retry."
            ),
        )
        if not resolved:
            control = dict(work.runtime_control_json or {})
            control.pop("safe_system_recovery_retry", None)
            work.runtime_control_json = control
            continue
        work_runtime.sync_task_projection(work, task)
        project = work.project
        if project is not None and work_runtime.project_can_activate(project):
            project.status = "ACTIVE"
            project.current_state_summary = f"{work.title} is resuming through a safe Company-owned recovery path."
        emit(
            "WORK_SYSTEM_RECOVERY_AUTO_RESUMED", actor_type="RUNTIME",
            project_id=work.project_id, work_id=work.id, correlation_id=f"work:{work.id}",
            payload={
                "source_run_id": latest.id,
                "issue_code": str(matching["issue_code"]),
                "selected_model_config_id": candidate.id,
                "selected_provider": candidate.provider_key,
                "selected_model": candidate.model_name,
                "provider_dispatched_by_recovery": False,
            },
        )
        recovered += 1
    if recovered:
        db.session.commit()
    return recovered


def resolve_safe_project_ceo_recovery() -> int:
    """Reopen exact continuation staffing gates when an active CEO exists again.

    CEO availability is mutable Company staffing truth. Maintenance only checks
    current employee state and resolves PROJECT_CEO_UNAVAILABLE:<project_id>; it
    does not buy a planning call or create/approve a continuation Mission.
    """
    Project = __import__("eason_one.models", fromlist=["Project"]).Project
    Employee = __import__("eason_one.models", fromlist=["Employee"]).Employee
    prefix = "PROJECT_CEO_UNAVAILABLE:"
    changed = 0
    for work in Work.query.filter_by(state="WAITING", work_type="MANAGEMENT").order_by(Work.id).all():
        if not _is_kernel_work(work):
            continue
        for gate in list(work_runtime.open_gates(work)):
            if str(gate.get("condition_type") or "").upper() != "SYSTEM_RECOVERY":
                continue
            issue = str(gate.get("issue_code") or "")
            if not issue.startswith(prefix):
                continue
            try:
                project_id = int(issue[len(prefix):])
            except (TypeError, ValueError):
                continue
            project = db.session.get(Project, project_id)
            if project is None or int(getattr(work, "project_id", 0) or 0) != project_id:
                continue
            ceo = project.owner if project.owner and project.owner.active else Employee.query.filter_by(slug="ceo", active=True).first()
            if ceo is None:
                continue
            resolved = work_runtime.resolve_waits(
                work, "SYSTEM_RECOVERY", issue_code=issue,
                note=(
                    f"Active CEO {ceo.name} is available again; normal Project sequencing may resume continuation planning."
                ),
            )
            if not resolved:
                continue
            if work_runtime.project_can_activate(project):
                project.status = "ACTIVE"
                project.current_state_summary = "CEO continuation ownership is available again; Company sequencing can resume."
            emit(
                "PROJECT_CEO_RECOVERY_RESOLVED", actor_type="RUNTIME",
                project_id=project.id, work_id=work.id, correlation_id=f"work:{work.id}",
                payload={
                    "issue_code": issue, "ceo_employee_id": ceo.id,
                    "provider_dispatched_by_recovery": False,
                },
            )
            changed += 1
            break
    if changed:
        db.session.commit()
    return changed


def resolve_safe_project_outcome_reviewer_recovery() -> int:
    """Reopen exact outcome-review staffing gates when an independent reviewer exists.

    Reviewer availability is mutable Company staffing truth, not Founder authority.
    Maintenance performs only a read-only selector check and resolves the exact
    PROJECT_OUTCOME_REVIEWER_UNAVAILABLE:<project_id> gate. The actual paid
    review remains normal Project sequencing.
    """
    Project = __import__("eason_one.models", fromlist=["Project"]).Project
    Employee = __import__("eason_one.models", fromlist=["Employee"]).Employee
    outcome = __import__(
        "eason_one.services.project_outcome", fromlist=["select_outcome_reviewer"]
    )
    prefix = "PROJECT_OUTCOME_REVIEWER_UNAVAILABLE:"
    changed = 0
    for work in Work.query.filter_by(state="WAITING", work_type="MANAGEMENT").order_by(Work.id).all():
        if not _is_kernel_work(work):
            continue
        for gate in list(work_runtime.open_gates(work)):
            if str(gate.get("condition_type") or "").upper() != "SYSTEM_RECOVERY":
                continue
            issue = str(gate.get("issue_code") or "")
            if not issue.startswith(prefix):
                continue
            try:
                project_id = int(issue[len(prefix):])
            except (TypeError, ValueError):
                continue
            project = db.session.get(Project, project_id)
            if project is None or int(getattr(work, "project_id", 0) or 0) != project_id:
                continue
            ceo = project.owner or Employee.query.filter_by(slug="ceo", active=True).first()
            reviewer = outcome.select_outcome_reviewer(project, fallback_ceo=ceo)
            if reviewer is None:
                continue
            resolved = work_runtime.resolve_waits(
                work, "SYSTEM_RECOVERY", issue_code=issue,
                note=(
                    f"Independent Project outcome reviewer {reviewer.name} is now available; "
                    "normal Project sequencing may perform the governed review."
                ),
            )
            if not resolved:
                continue
            if work_runtime.project_can_activate(project):
                project.status = "ACTIVE"
                project.current_state_summary = (
                    "Independent outcome-review ownership is available again; Company sequencing can resume."
                )
            emit(
                "PROJECT_OUTCOME_REVIEWER_RECOVERY_RESOLVED", actor_type="RUNTIME",
                project_id=project.id, work_id=work.id, correlation_id=f"work:{work.id}",
                payload={
                    "issue_code": issue, "reviewer_employee_id": reviewer.id,
                    "provider_dispatched_by_recovery": False,
                },
            )
            changed += 1
            break
    if changed:
        db.session.commit()
    return changed


def resolve_safe_project_management_system_recovery() -> int:
    """Reopen Project management provider recovery only when a new safe path exists.

    Outcome review and continuation planning are paid Company-management effects.
    Maintenance never calls a provider. It only resolves the exact SYSTEM_RECOVERY
    gate after durable truth proves the failed attempt is replay-safe, the total
    per-fingerprint attempt cap is not exhausted, and current Founder/Project
    policy exposes an untried lawful ModelConfig. Normal Project sequencing then
    performs the guarded replacement Run with retry lineage.
    """
    recovered = 0
    policy = __import__(
        "eason_one.services.execution_policy", fromlist=["select_retry_model"]
    )
    external_effects = __import__(
        "eason_one.services.external_effects", fromlist=["retry_authorized"]
    )
    employee_model = __import__("eason_one.models", fromlist=["Employee"]).Employee
    fingerprint_keys = {
        "PROJECT_OUTCOME_REVIEW": "project_outcome_input_hash",
        "CEO_PROJECT_CONTINUATION": "continuation_evidence_hash",
    }
    for work in Work.query.filter_by(state="WAITING", work_type="MANAGEMENT").order_by(Work.id).all():
        if not _is_kernel_work(work):
            continue
        for gate in list(work_runtime.open_gates(work)):
            if gate.get("condition_type") != "SYSTEM_RECOVERY":
                continue
            issue = str(gate.get("issue_code") or "")
            if not issue.startswith("PROJECT_MANAGEMENT_PROVIDER_RECOVERY:"):
                continue
            parts = issue.split(":", 2)
            if len(parts) != 3:
                continue
            purpose = str(parts[1] or "").upper()
            fingerprint_key = fingerprint_keys.get(purpose)
            if fingerprint_key is None:
                continue
            try:
                run_id = int(parts[2])
            except (TypeError, ValueError):
                continue
            failed = db.session.get(AgentRun, run_id)
            if (
                failed is None
                or int(getattr(failed, "work_id", 0) or 0) != int(work.id)
                or str(getattr(failed, "purpose", "") or "").upper() != purpose
                or str(getattr(failed, "failure_reason", "") or "")
                not in {"PROVIDER_REQUEST_REJECTED", "PROVIDER_PREFLIGHT_FAILED"}
                or str(getattr(failed, "outcome", "") or "") not in {"FAILED_SAFE", "FAILED_KNOWN"}
            ):
                continue
            fingerprint = str((failed.context_composition_json or {}).get(fingerprint_key) or "")
            if not fingerprint:
                continue
            project = work.project
            operation = work.operation
            if project is None or operation is None:
                continue

            # An exact provider-recovery gate belongs to the Project truth that
            # existed when the failed Run was dispatched. If Founder execution
            # terms have changed since then, that old retry path is obsolete.
            # Retire only this exact gate; normal Project sequencing will
            # recompute current evidence/authority and decide the next lawful
            # action. Never require a new provider/model merely to clear stale
            # recovery truth.
            contracts = __import__(
                "eason_one.services.project_contract",
                fromlist=["execution_terms_hash", "execution_terms_changed_after"],
            )
            context = dict(failed.context_composition_json or {})
            terms_snapshot = str(context.get("project_execution_terms_hash") or "")
            current_terms_hash = contracts.execution_terms_hash(project)
            stale_terms = (
                terms_snapshot != current_terms_hash
                if terms_snapshot
                else contracts.execution_terms_changed_after(project, failed.started_at)
            )
            if stale_terms:
                resolved = work_runtime.resolve_waits(
                    work, "SYSTEM_RECOVERY", issue_code=issue,
                    note=(
                        "Founder Project execution terms changed after this failed management Run; "
                        "the exact old provider-recovery gate is obsolete and current sequencing will recompute authority."
                    ),
                )
                if not resolved:
                    continue
                if work_runtime.project_can_activate(project):
                    project.status = "ACTIVE"
                    project.current_state_summary = (
                        "An obsolete Project-management recovery gate was retired after Founder Project terms changed; "
                        "Company sequencing will recompute from current authority."
                    )
                emit(
                    "PROJECT_MANAGEMENT_STALE_RECOVERY_RETIRED", actor_type="RUNTIME",
                    project_id=work.project_id, work_id=work.id, correlation_id=f"work:{work.id}",
                    payload={
                        "purpose": purpose, "source_run_id": failed.id,
                        "fingerprint_key": fingerprint_key, "fingerprint": fingerprint,
                        "reason": "PROJECT_EXECUTION_TERMS_CHANGED",
                        "provider_dispatched_by_recovery": False,
                    },
                )
                recovered += 1
                break

            # A later accidental failed duplicate must never hide an earlier
            # durable paid success for the same exact management input. The
            # normal business tick already knows how to project/reuse that Run;
            # maintenance only removes the exact recovery gate that would
            # otherwise prevent the tick from ever reaching the reuse path.
            kernel = __import__(
                "eason_one.services.company_kernel",
                fromlist=["_reusable_successful_project_attempt", "_review_input_hash"],
            )
            if purpose == "PROJECT_OUTCOME_REVIEW":
                try:
                    current_fingerprint = str(kernel._review_input_hash(project) or "")
                except Exception:
                    current_fingerprint = fingerprint
                if current_fingerprint and current_fingerprint != fingerprint:
                    resolved = work_runtime.resolve_waits(
                        work, "SYSTEM_RECOVERY", issue_code=issue,
                        note=(
                            "Project outcome evidence changed after this failed management Run; "
                            "the exact old provider-recovery gate is stale and current review sequencing will recompute the fingerprint."
                        ),
                    )
                    if not resolved:
                        continue
                    if work_runtime.project_can_activate(project):
                        project.status = "ACTIVE"
                        project.current_state_summary = (
                            "A stale Project outcome-review recovery gate was retired because current accepted evidence changed."
                        )
                    emit(
                        "PROJECT_MANAGEMENT_STALE_RECOVERY_RETIRED", actor_type="RUNTIME",
                        project_id=work.project_id, work_id=work.id, correlation_id=f"work:{work.id}",
                        payload={
                            "purpose": purpose, "source_run_id": failed.id,
                            "fingerprint_key": fingerprint_key, "fingerprint": fingerprint,
                            "current_fingerprint": current_fingerprint,
                            "reason": "PROJECT_OUTCOME_EVIDENCE_CHANGED",
                            "provider_dispatched_by_recovery": False,
                        },
                    )
                    recovered += 1
                    break

            reusable = kernel._reusable_successful_project_attempt(
                project, purpose=purpose,
                fingerprint_key=fingerprint_key, fingerprint=fingerprint,
            )
            if reusable is not None:
                resolved = work_runtime.resolve_waits(
                    work, "SYSTEM_RECOVERY", issue_code=issue,
                    note=(
                        f"Durable paid {purpose} Run #{reusable.id} already succeeded for this exact input; "
                        "normal sequencing will reuse/project it instead of buying another provider call."
                    ),
                )
                if not resolved:
                    continue
                if work_runtime.project_can_activate(project):
                    project.status = "ACTIVE"
                    project.current_state_summary = (
                        "Company recovered an already-paid successful Project-management result and can resume deterministic projection."
                    )
                emit(
                    "PROJECT_MANAGEMENT_PAID_SUCCESS_RECOVERY_RESOLVED", actor_type="RUNTIME",
                    project_id=work.project_id, work_id=work.id, correlation_id=f"work:{work.id}",
                    payload={
                        "purpose": purpose, "source_run_id": failed.id,
                        "reusable_success_run_id": reusable.id,
                        "fingerprint_key": fingerprint_key, "fingerprint": fingerprint,
                        "provider_dispatched_by_recovery": False,
                    },
                )
                recovered += 1
                break

            replay_ok, _reason = external_effects.retry_authorized(failed)
            if not replay_ok:
                continue
            attempts = [
                row for row in AgentRun.query.filter_by(
                    project_id=failed.project_id, purpose=purpose
                ).order_by(AgentRun.id).all()
                if str((row.context_composition_json or {}).get(fingerprint_key) or "") == fingerprint
            ]
            # Three paid attempts for one exact evidence fingerprint is the hard
            # management cap. Configuration changes do not silently grant more.
            if len(attempts) >= 3:
                continue
            employee = db.session.get(employee_model, failed.employee_id)
            if employee is None:
                continue
            excluded = {
                int(row.model_config_id) for row in attempts if getattr(row, "model_config_id", None) is not None
            }
            candidate = policy.select_retry_model(
                employee, failed, operation, purpose, exclude_model_ids=excluded
            )
            if candidate is None:
                continue
            resolved = work_runtime.resolve_waits(
                work, "SYSTEM_RECOVERY", issue_code=issue,
                note=(
                    f"Company recovery found an untried lawful Project-management model "
                    f"({candidate.provider_key}/{candidate.model_name}); normal sequencing will perform the guarded retry."
                ),
            )
            if not resolved:
                continue
            project = work.project
            if project is not None and work_runtime.project_can_activate(project):
                project.status = "ACTIVE"
                project.current_state_summary = (
                    "Company found a safe untried management-model path and is resuming Project sequencing."
                )
            emit(
                "PROJECT_MANAGEMENT_SYSTEM_RECOVERY_AUTO_RESUMED", actor_type="RUNTIME",
                project_id=work.project_id, work_id=work.id, correlation_id=f"work:{work.id}",
                payload={
                    "purpose": purpose, "source_run_id": failed.id,
                    "fingerprint_key": fingerprint_key, "fingerprint": fingerprint,
                    "selected_model_config_id": candidate.id,
                    "selected_provider": candidate.provider_key,
                    "selected_model": candidate.model_name,
                    "provider_dispatched_by_recovery": False,
                },
            )
            recovered += 1
            break
    if recovered:
        db.session.commit()
    return recovered


def resolve_safe_continuation_approval_system_recovery() -> int:
    """Re-probe only exact delegated-continuation approval recovery gates.

    Both runtime SYSTEM_RECOVERY and deterministic RECONCILIATION are scoped to
    one already-paid CEO-delegated Operation.  Maintenance never approves that
    Operation and never dispatches a provider.  It only retires or reclassifies
    the exact readiness gate when current structured truth changes, so a
    transient proposal/invariant fault cannot strand the existing Mission
    forever after the underlying source/data condition is repaired.
    """
    Operation = __import__("eason_one.models", fromlist=["Operation"]).Operation
    operations = __import__(
        "eason_one.services.operations", fromlist=["delegated_approval_readiness"]
    )
    governance = __import__(
        "eason_one.services.governance",
        fromlist=[
            "open_gate", "FounderAuthorityPreviouslyRejected",
            "apply_rejected_budget_boundary",
        ],
    )
    prefixes = {
        "SYSTEM_RECOVERY": "CONTINUATION_APPROVAL_RECOVERY:",
        "RECONCILIATION": "CONTINUATION_APPROVAL_RECONCILIATION:",
    }
    changed = 0
    for work in Work.query.filter_by(state="WAITING", work_type="MANAGEMENT").order_by(Work.id).all():
        if not _is_kernel_work(work):
            continue
        matching = None
        operation_id = None
        current_condition = None
        for gate in work_runtime.open_gates(work):
            condition = str(gate.get("condition_type") or "").upper()
            prefix = prefixes.get(condition)
            if prefix is None:
                continue
            issue = str(gate.get("issue_code") or "")
            if not issue.startswith(prefix):
                continue
            try:
                candidate_id = int(issue[len(prefix):])
            except (TypeError, ValueError):
                continue
            matching = dict(gate)
            operation_id = candidate_id
            current_condition = condition
            break
        if matching is None or operation_id is None or current_condition is None:
            continue
        operation = db.session.get(Operation, operation_id)
        if operation is None or int(operation.id) != int(getattr(work, "operation_id", 0) or 0):
            continue
        if operation.approved_at is not None:
            # Approval already succeeded elsewhere; the exact readiness gate is stale.
            readiness = {"ready": True, "kind": "READY", "reason": None}
        else:
            readiness = operations.delegated_approval_readiness(operation)
        kind = str(readiness.get("kind") or "RECONCILIATION").upper()
        if kind == "STALE_CONTINUATION":
            kernel = __import__(
                "eason_one.services.company_kernel",
                fromlist=["_supersede_stale_delegated_continuation"],
            )
            reason = str(readiness.get("reason") or "Delegated continuation planning basis is stale.")
            kernel._supersede_stale_delegated_continuation(
                work.project, operation, reason=reason, currentness=readiness,
            )
            changed += 1
            continue
        target_condition = (
            "SYSTEM_RECOVERY" if kind == "SYSTEM_RECOVERY"
            else "RECONCILIATION" if kind == "RECONCILIATION"
            else None
        )
        # Nothing changed: keep the exact gate and do not create noisy writes.
        if not readiness.get("ready") and target_condition == current_condition:
            continue

        issue = str(matching.get("issue_code"))
        resolved = work_runtime.resolve_waits(
            work, current_condition, issue_code=issue,
            note=(
                "Delegated continuation readiness changed; the stale exact recovery gate is retired "
                "without approving, materializing, or dispatching the Mission."
            ),
        )
        if not resolved:
            continue
        project = work.project

        if readiness.get("ready"):
            if project is not None and work_runtime.project_can_activate(project):
                project.status = "ACTIVE"
                project.current_state_summary = (
                    "Company readiness recovered; normal sequencing will approve the existing delegated continuation Mission."
                )
            event_type = (
                "CONTINUATION_APPROVAL_SYSTEM_RECOVERY_AUTO_RESUMED"
                if current_condition == "SYSTEM_RECOVERY"
                else "CONTINUATION_APPROVAL_RECONCILIATION_AUTO_RESUMED"
            )
            emit(
                event_type, actor_type="RUNTIME",
                project_id=work.project_id, work_id=work.id, correlation_id=f"work:{work.id}",
                payload={
                    "operation_id": operation.id, "issue_code": issue,
                    "from": current_condition,
                    "provider_dispatched_by_recovery": False,
                    "operation_approved_by_recovery": False,
                },
            )
            changed += 1
            continue

        reason = str(readiness.get("reason") or "Delegated continuation approval needs reconciliation.")
        if kind == "BUDGET_AUTHORIZATION":
            amount = readiness.get("additional_budget_twd")
            try:
                governance.open_gate(
                    project=project, escalation_type="BUDGET_AUTHORIZATION", reason=reason,
                    operation=operation, work=work,
                    created_by_employee_id=getattr(getattr(project, "owner", None), "id", None),
                    authority_payload={"additional_budget_twd": str(amount), "scope": "PROJECT"},
                    recommendation="Approve only the exact additional Project budget if intended.",
                )
                if project is not None:
                    project.status = "BLOCKED"
                    project.current_state_summary = reason[:1400]
                    project.next_milestone = "Founder budget authority is required for the exact priced continuation shortfall."
            except governance.FounderAuthorityPreviouslyRejected as rejected:
                governance.apply_rejected_budget_boundary(
                    project=project, work=work, decision_id=rejected.decision_id
                )
            emit(
                "CONTINUATION_APPROVAL_RECOVERY_RECLASSIFIED", actor_type="RUNTIME",
                project_id=work.project_id, work_id=work.id, correlation_id=f"work:{work.id}",
                payload={
                    "operation_id": operation.id, "from": current_condition,
                    "to": "BUDGET_AUTHORIZATION", "additional_budget_twd": str(amount),
                    "provider_dispatched_by_recovery": False,
                },
            )
            changed += 1
            continue

        if kind == "SYSTEM_RECOVERY":
            target_issue = f"CONTINUATION_APPROVAL_RECOVERY:{operation.id}"
            work_runtime.open_wait(work, "SYSTEM_RECOVERY", reason, issue_code=target_issue)
            target = "SYSTEM_RECOVERY"
            milestone = "Company runtime readiness must recover before the existing delegated Mission can be approved."
        else:
            target_issue = f"CONTINUATION_APPROVAL_RECONCILIATION:{operation.id}"
            work_runtime.open_wait(work, "RECONCILIATION", reason, issue_code=target_issue)
            target = "RECONCILIATION"
            milestone = "Company must reconcile delegated Mission approval truth before execution resumes."
        if project is not None:
            project.status = "BLOCKED"
            project.current_state_summary = reason[:1400]
            project.next_milestone = milestone
        emit(
            "CONTINUATION_APPROVAL_RECOVERY_RECLASSIFIED", actor_type="RUNTIME",
            project_id=work.project_id, work_id=work.id, correlation_id=f"work:{work.id}",
            payload={
                "operation_id": operation.id, "from": current_condition, "to": target,
                "issue_code": target_issue, "provider_dispatched_by_recovery": False,
            },
        )
        changed += 1
    if changed:
        db.session.commit()
    return changed

def resolve_safe_hiring_system_recovery() -> int:
    """Resume HR provider recovery when a new lawful untried model appears.

    Paid local-projection failures and exhausted/ambiguous attempts are deliberately
    excluded.  This only reopens a staffing branch whose latest provider failure is
    replay-safe and whose *current* Project/provider policy now has an untried
    candidate. No provider is called by maintenance.
    """
    HiringRequest = __import__("eason_one.models", fromlist=["HiringRequest"]).HiringRequest
    workforce = __import__(
        "eason_one.services.workforce",
        fromlist=["assessment_retry_state", "source_work_for_request"],
    )
    Operation = __import__("eason_one.models", fromlist=["Operation"]).Operation
    recovered = 0
    Project = __import__("eason_one.models", fromlist=["Project"]).Project
    for request in (
        HiringRequest.query.filter_by(status="SYSTEM_RECOVERY").order_by(HiringRequest.id).all()
    ):
        project = db.session.get(Project, request.project_id) if request.project_id else None
        if project is not None and work_runtime.project_execution_fenced(project):
            continue
        state = workforce.assessment_retry_state(request, current=now())
        if state.get("state") != "READY" or not state.get("recovery_model_id"):
            continue
        recovery_work = workforce.source_work_for_request(request)
        if recovery_work is None and getattr(request, "operation_id", None):
            operation = db.session.get(Operation, request.operation_id)
            if operation is not None:
                recovery_work = work_runtime.ensure_management_work(operation)
        if recovery_work is None:
            continue
        if not _is_kernel_work(recovery_work):
            continue
        issue_code = f"HIRING_REQUEST_{request.id}_PROVIDER_RECOVERY"
        matching = None
        for gate in work_runtime.open_gates(recovery_work):
            if gate.get("condition_type") != "SYSTEM_RECOVERY":
                continue
            if str(gate.get("issue_code") or "") == issue_code:
                matching = gate
                break
        if matching is None:
            # Older unscoped SYSTEM_RECOVERY gates are deliberately left alone;
            # without an issue_code we cannot prove that resolving this wait would
            # not also bypass another system condition on the same Work.
            continue
        resolved = work_runtime.resolve_waits(
            recovery_work, "SYSTEM_RECOVERY",
            issue_code=(str(matching.get("issue_code")) if matching.get("issue_code") else None),
            note=(
                f"HR recovery found a newly lawful untried ModelConfig #{state['recovery_model_id']}; "
                "normal HR execution will retry through the durable failed-run lineage."
            ),
        )
        if not resolved:
            continue
        request.status = "HR_REVIEW"
        project = getattr(recovery_work, "project", None)
        if project is not None and work_runtime.project_can_activate(project):
            project.status = "ACTIVE"
            project.current_state_summary = "Company HR found a safe provider recovery path and is resuming staffing."
        emit(
            "HIRING_SYSTEM_RECOVERY_AUTO_RESUMED", actor_type="RUNTIME",
            project_id=getattr(request, "project_id", None), work_id=recovery_work.id,
            correlation_id=(f"project:{request.project_id}" if request.project_id else f"hiring:{request.id}"),
            payload={
                "hiring_request_id": request.id,
                "source_run_id": getattr(state.get("last_run"), "id", None),
                "selected_model_config_id": state.get("recovery_model_id"),
                "provider_dispatched_by_recovery": False,
            },
        )
        recovered += 1
    if recovered:
        db.session.commit()
    return recovered


def repair_orphaned_waiting_work() -> int:
    repaired = 0
    for work in Work.query.filter_by(state="WAITING").order_by(Work.id).all():
        if not _is_kernel_work(work) or work_runtime.primary_gate(work):
            continue
        if work.work_type == "MANAGEMENT":
            resume = "EXECUTING"
        else:
            # Orphan repair must re-prove the Work still belongs to the current
            # immutable Founder Project execution terms before reviving any
            # historical Artifact truth.  Otherwise a post-amendment restart
            # could resurrect stale evidence or make stale Work executable.
            task = work_runtime.task_for_work(work)
            acceptance = __import__(
                "eason_one.services.acceptance_contract", fromlist=["ensure_for_work"]
            )
            try:
                acceptance.ensure_for_work(work, task=task)
            except ValueError as exc:
                reason = str(exc)
                if reason.startswith("WORK_PROJECT_TERMS_CHANGED:"):
                    work_runtime.transition(
                        work, "CANCELLED", actor_type="RUNTIME",
                        reason=reason,
                    )
                    work_runtime.sync_task_projection(work, task)
                    repaired += 1
                    continue
                work_runtime.open_wait(
                    work, "RECONCILIATION", reason,
                    issue_code=f"WORK_ACCEPTANCE_CONTRACT_RECONCILIATION:{int(work.id)}",
                )
                work_runtime.sync_task_projection(work, task)
                repaired += 1
                continue
            latest = (
                ArtifactVersion.query.join(Artifact).filter(Artifact.work_id == work.id)
                .order_by(ArtifactVersion.id.desc()).first()
            )
            if latest is not None and latest.status == "ACCEPTED":
                # Durable accepted evidence outranks the stale WAITING projection.
                # Require at least one PASSED verification bound to the exact
                # version before restoring terminal Work truth; if that proof is
                # missing, resume VERIFYING for deterministic reconciliation and
                # never make an accepted Artifact executable again.
                proof = (
                    VerificationRecord.query.filter_by(
                        work_id=work.id, artifact_version_id=latest.id, status="PASSED"
                    )
                    .order_by(VerificationRecord.id.desc()).first()
                )
                resume = "ACCEPTED" if proof is not None else "VERIFYING"
            elif latest is not None and latest.status == "SUBMITTED":
                resume = "VERIFYING"
            else:
                resume = "READY"
        recovery_reason = "Recovered WAITING Work with no durable gate from current Artifact truth."
        if resume == "ACCEPTED":
            work_runtime.restore_durable_accepted(
                work, actor_type="RUNTIME", reason=recovery_reason,
            )
        else:
            work_runtime.transition(
                work, resume, actor_type="RUNTIME", reason=recovery_reason,
            )
            work_runtime.sync_task_projection(work)
        repaired += 1
    if repaired:
        db.session.commit()
    return repaired


def reconcile_interrupted_provider_settlement() -> int:
    """Finish durable reservation bookkeeping after a process interruption.

    Provider execution and budget settlement intentionally cross multiple durable
    commits. A crash after the provider result is persisted but before the cost
    reservation is consumed must not leave phantom reserved authority forever.
    Likewise, a definitively rejected HTTP request releases its reservation,
    while an unknown post-dispatch effect remains held as AMBIGUOUS.  This routine
    only reconciles already-durable truth; it never calls a provider or replays a
    Work.
    """
    kernel = __import__(
        "eason_one.services.operation_kernel", fromlist=["resolve_reservation"]
    )
    effects = __import__(
        "eason_one.services.external_effects", fromlist=["mark_settled", "mark_ambiguous"]
    )
    changed = 0
    reservations = (
        CostReservation.query.filter(CostReservation.status.in_(["RESERVED", "AMBIGUOUS", "CONSUMED"]))
        .filter(CostReservation.agent_run_id.is_not(None))
        .order_by(CostReservation.id).all()
    )
    for reservation in reservations:
        run = db.session.get(AgentRun, reservation.agent_run_id)
        if run is None:
            continue
        effect = (
            ExternalEffectAttempt.query.filter_by(execution_id=run.id)
            .order_by(ExternalEffectAttempt.id.desc()).first()
        )
        if effect is None:
            # No effect ledger means there is not enough truth to release or
            # charge this authority automatically. Leave it reserved.
            continue
        state = str(effect.state or "").upper()

        if state == "RESPONSE_RECEIVED" and run.status == "RUNNING" and run.raw_output is not None:
            checkpoint = dict((run.context_composition_json or {}).get("provider_response_checkpoint") or {})
            if checkpoint.get("schema") != "PROVIDER_RESPONSE_CHECKPOINT_V1":
                # A legacy RESPONSE_RECEIVED row lacks enough durable response
                # semantics to decide success/refusal/truncation. Preserve it as
                # ambiguous instead of guessing or replaying.
                effects.mark_ambiguous(effect, "Provider response exists without a complete durable response checkpoint.")
                kernel.resolve_reservation(
                    reservation, status="AMBIGUOUS",
                    note=f"Agent Run #{run.id} has an incomplete provider-response checkpoint; replay is forbidden.",
                )
                changed += 1
                continue
            if int(run.cache_creation_input_tokens or 0) or int(run.cache_read_input_tokens or 0):
                run.status = "FAILED"
                run.outcome = "FAILED_AMBIGUOUS"
                run.failure_reason = "COST_RECONCILIATION_REQUIRED"
                run.failure_stage = "SETTLEMENT"
                run.error_text = "Provider returned prompt-cache usage that cannot be reconciled exactly."
                run.finished_at = now()
                effects.mark_ambiguous(effect, run.error_text)
                kernel.resolve_reservation(reservation, status="AMBIGUOUS", note=run.error_text)
                changed += 1
                continue

            input_cost = Decimal(int(run.input_tokens or 0)) * Decimal(run.input_price_snapshot or 0)
            output_cost = Decimal(int(run.output_tokens or 0)) * Decimal(run.output_price_snapshot or 0)
            request_cost = Decimal(run.request_price_snapshot or 0) if checkpoint.get("include_request_fee") else Decimal("0")
            run.real_cost = (input_cost + output_cost) / Decimal(1_000_000) + request_cost
            refusal = str(checkpoint.get("refusal") or "")
            response_status = str(checkpoint.get("status") or "")
            incomplete_reason = str(checkpoint.get("incomplete_reason") or "")
            provider_name = str(run.provider_key_snapshot or "provider")
            if refusal:
                run.status = "FAILED"
                run.failure_reason = "REFUSAL"
                run.outcome = "FAILED_KNOWN"
                run.failure_stage = "RESPONSE"
                run.error_text = f"{provider_name} refusal: {refusal}"
            elif response_status != "completed":
                run.status = "FAILED"
                run.failure_reason = "OUTPUT_TRUNCATED" if incomplete_reason == "max_output_tokens" else "PROVIDER_INCOMPLETE"
                run.outcome = "FAILED_KNOWN"
                run.failure_stage = "RESPONSE"
                detail = f": {incomplete_reason}" if incomplete_reason else ""
                run.error_text = f"{run.failure_reason}: {provider_name} response {response_status}{detail}"
            else:
                run.status = "SUCCEEDED"
                run.outcome = "SUCCEEDED"
                run.failure_stage = None
            run.finished_at = now()
            __import__("eason_one.services.costs", fromlist=["record"]).record(run)
            __import__("eason_one.services.external_effects", fromlist=["mark_persisted"]).mark_persisted(effect)
            kernel.resolve_reservation(
                reservation, actual_twd=run.real_cost, status="CONSUMED",
                note=f"Restart recovery finalized the durable provider response for Agent Run #{run.id} without replay.",
            )
            effects.mark_settled(effect, actual_cost_twd=run.real_cost)
            changed += 1
            continue

        if run.status == "RUNNING":
            continue

        if state in {"PERSISTED", "SETTLED"} and run.real_cost is not None:
            if state != "SETTLED":
                effects.mark_settled(effect, actual_cost_twd=run.real_cost)
                changed += 1
            if reservation.status != "CONSUMED":
                kernel.resolve_reservation(
                    reservation, actual_twd=run.real_cost, status="CONSUMED",
                    note=(
                        f"Restart reconciliation consumed the exact persisted provider cost for Agent Run #{run.id}; "
                        "no provider replay occurred."
                    ),
                )
                changed += 1
            elif reservation.actual_twd is None:
                # A crash can occur after reservation status flips to CONSUMED
                # but before all exact-cost projection fields are durable. Fill
                # only the already-proven cost; never reopen the reservation.
                reservation.actual_twd = run.real_cost
                reservation.resolution_note = reservation.resolution_note or (
                    f"Restart reconciliation completed exact cost metadata for Agent Run #{run.id}."
                )
                changed += 1
            continue

        if state == "REJECTED_POST_DISPATCH" and run.outcome == "FAILED_KNOWN":
            kernel.resolve_reservation(
                reservation, status="RELEASED",
                note=(
                    f"Restart reconciliation released definitively rejected provider request for Agent Run #{run.id}; "
                    "no model completion was returned."
                ),
            )
            changed += 1
            continue

        if state == "FAILED_PRE_DISPATCH" or run.outcome == "FAILED_SAFE":
            kernel.resolve_reservation(
                reservation, status="RELEASED",
                note=f"Restart reconciliation released pre-dispatch reservation for Agent Run #{run.id}.",
            )
            changed += 1
            continue

        if state in {"DISPATCHING", "RESPONSE_RECEIVED", "AMBIGUOUS_POST_DISPATCH"} or run.outcome == "FAILED_AMBIGUOUS":
            if state != "AMBIGUOUS_POST_DISPATCH":
                effects.mark_ambiguous(
                    effect,
                    "Process interruption left provider dispatch outcome unproven; replay remains forbidden until reconciled.",
                )
            kernel.resolve_reservation(
                reservation, status="AMBIGUOUS",
                note=(
                    f"Restart reconciliation preserved authority for Agent Run #{run.id}: provider dispatch outcome is unproven."
                ),
            )
            changed += 1
    if changed:
        db.session.commit()
    return changed




def recover_interrupted_meeting_steps(*, before) -> int:
    """Recover Meeting steps left RUNNING by a previous process.

    A Meeting provider call is split across two durable phases: ``execute()``
    persists the AgentRun/effect/cost first, then ``meetings`` validates and
    materializes router/contribution/synthesis state.  A crash between those
    phases must reuse the paid response locally.  A crash after dispatch but
    before a durable response is intentionally *not* replayed.
    """
    meeting_service = __import__(
        "eason_one.services.meetings",
        fromlist=["recover_interrupted_step", "_run_bound_to_step", "_handle_step_failure", "_fail_step"],
    )
    effects = __import__(
        "eason_one.services.external_effects",
        fromlist=["mark_pre_dispatch_failure", "mark_ambiguous"],
    )
    kernel = __import__(
        "eason_one.services.operation_kernel", fromlist=["resolve_reservation"]
    )
    changed = 0
    rows = (
        MeetingStep.query.filter(
            MeetingStep.status == "RUNNING",
            MeetingStep.created_at < before,
        )
        .order_by(MeetingStep.id)
        .all()
    )
    for step in rows:
        meeting = db.session.get(Meeting, step.meeting_id)
        if meeting is None:
            step.status = "FAILED"
            step.error_text = "Meeting no longer exists; interrupted step cannot execute."
            step.finished_at = now()
            changed += 1
            continue
        project = db.session.get(
            __import__("eason_one.models", fromlist=["Project"]).Project,
            meeting.project_id,
        ) if meeting.project_id else None
        if str(getattr(project, "status", "") or "").upper() == "PAUSED":
            # Provider/cost settlement is handled independently, but Meeting
            # messages, minutes, evidence and lifecycle state must remain frozen
            # until explicit Founder Resume.
            continue

        # Best case: execute() already durably finished.  Consume the exact paid
        # response and finish only local state materialization.
        if meeting_service.recover_interrupted_step(step):
            changed += 1
            continue

        run = meeting_service._run_bound_to_step(step)
        if run is None:
            possible = meeting_service._possible_unbound_runs(step)
            if possible:
                # Historical code did not bind MeetingStep -> AgentRun. If more
                # than one Run is plausible, replay authority is unknowable.
                # Preserve that uncertainty rather than guessing one answer or
                # buying another.
                step.status = "AMBIGUOUS"
                step.error_text = (
                    f"{len(possible)} historical provider Runs could belong to this interrupted Meeting step; automatic replay is forbidden."
                )
                step.finished_at = now()
                db.session.commit()
                meeting_service.close_for_internal_recovery(meeting, step.error_text)
            else:
                # No plausible Run exists, so execute() never reached its first
                # durable AgentRun commit. This is safe to retry under the
                # Meeting's bounded recovery policy.
                meeting_service._fail_step(
                    meeting, step, None,
                    "Process restarted before a Meeting provider attempt was durably created; safe retry remains bounded by Meeting policy.",
                )
            changed += 1
            continue

        if run.status == "FAILED":
            meeting_service._handle_step_failure(
                meeting, step, run, run.error_text or run.failure_reason or "Interrupted Meeting attempt failed."
            )
            changed += 1
            continue

        if run.status != "RUNNING":
            # Unknown/noncanonical state: never infer replay permission.
            step.status = "AMBIGUOUS"
            step.agent_run_id = run.id
            step.error_text = f"Interrupted Meeting Run #{run.id} has noncanonical status {run.status}; automatic replay forbidden."
            step.finished_at = now()
            db.session.commit()
            meeting_service.close_for_internal_recovery(meeting, step.error_text)
            changed += 1
            continue

        effect = (
            ExternalEffectAttempt.query.filter_by(execution_id=run.id)
            .order_by(ExternalEffectAttempt.id.desc()).first()
        )
        state = str(getattr(effect, "state", "") or "").upper()
        reservation = (
            db.session.get(CostReservation, effect.cost_reservation_id)
            if effect is not None and effect.cost_reservation_id else None
        )

        if effect is None or state in {"PREPARED", "RESERVED", "FAILED_PRE_DISPATCH"}:
            run.status = "FAILED"
            run.outcome = "FAILED_SAFE"
            run.failure_reason = "PROCESS_RESTART_BEFORE_DISPATCH"
            run.failure_stage = "PRE_DISPATCH"
            run.error_text = "Process restarted before Meeting provider dispatch was durably observed."
            run.finished_at = now()
            if effect is not None and state != "FAILED_PRE_DISPATCH":
                effects.mark_pre_dispatch_failure(effect, run.error_text)
            if reservation is not None and reservation.status in {"RESERVED", "AMBIGUOUS"}:
                kernel.resolve_reservation(
                    reservation, status="RELEASED",
                    note=f"Restart reconciliation released pre-dispatch Meeting authority for Agent Run #{run.id}.",
                )
            meeting_service._fail_step(meeting, step, run, run.error_text)
            changed += 1
            continue

        # DISPATCHING / RESPONSE_RECEIVED / unexpected post-dispatch state: the
        # provider may have accepted and billed the request.  Preserve held
        # authority and terminate this Meeting attempt rather than replay it.
        run.status = "FAILED"
        run.outcome = "FAILED_AMBIGUOUS"
        run.failure_reason = "PROCESS_RESTART_AFTER_DISPATCH"
        run.failure_stage = "POST_DISPATCH"
        run.error_text = (
            "Process restarted after Meeting provider dispatch; the provider outcome is unresolved and automatic replay is forbidden."
        )
        run.finished_at = now()
        if effect is not None and state != "AMBIGUOUS_POST_DISPATCH":
            effects.mark_ambiguous(effect, run.error_text)
        if reservation is not None and reservation.status == "RESERVED":
            reservation.status = "AMBIGUOUS"
            reservation.resolved_at = None
            reservation.resolution_note = run.error_text
        db.session.commit()
        meeting_service._handle_step_failure(meeting, step, run, run.error_text)
        changed += 1

    if changed:
        db.session.commit()
    return changed

def recover_interrupted_founder_requests(*, before) -> int:
    """Close CEO planning Runs that belong to a previous process.

    Founder planning has no Work yet, so generic stale-Work recovery cannot see
    it.  The Company Runtime passes a process-start cutoff captured before the
    serving app can accept requests.  Only older RUNNING planning attempts are
    touched, which avoids racing a live provider call in the current process.
    This routine never calls a provider.
    """
    effects = __import__(
        "eason_one.services.external_effects",
        fromlist=["mark_pre_dispatch_failure", "mark_ambiguous"],
    )
    kernel = __import__(
        "eason_one.services.operation_kernel", fromlist=["resolve_reservation"]
    )
    changed = 0
    rows = (
        AgentRun.query.filter(
            AgentRun.purpose == "CEO_FOUNDER_REQUEST",
            AgentRun.status == "RUNNING",
            AgentRun.started_at < before,
        )
        .order_by(AgentRun.id)
        .all()
    )
    for run in rows:
        effect = (
            ExternalEffectAttempt.query.filter_by(execution_id=run.id)
            .order_by(ExternalEffectAttempt.id.desc()).first()
        )
        state = str(getattr(effect, "state", "") or "").upper()
        reservation = (
            db.session.get(CostReservation, effect.cost_reservation_id)
            if effect is not None and effect.cost_reservation_id else None
        )
        if effect is None or state in {"PREPARED", "RESERVED", "FAILED_PRE_DISPATCH"}:
            run.status = "FAILED"
            run.outcome = "FAILED_SAFE"
            run.failure_reason = "PROCESS_RESTART_BEFORE_DISPATCH"
            run.failure_stage = "PRE_DISPATCH"
            run.error_text = (
                "The previous process ended before CEO planning provider dispatch was durably observed; "
                "a fresh Founder request may retry safely."
            )
            run.finished_at = now()
            if effect is not None and state != "FAILED_PRE_DISPATCH":
                effects.mark_pre_dispatch_failure(effect, run.error_text)
            if reservation is not None and reservation.status in {"RESERVED", "AMBIGUOUS"}:
                kernel.resolve_reservation(
                    reservation, status="RELEASED",
                    note=f"Restart reconciliation released pre-dispatch CEO planning authority for Agent Run #{run.id}.",
                )
        else:
            # DISPATCHING / RESPONSE_RECEIVED and any unexpected post-dispatch
            # state cannot be replayed because the provider may already have
            # accepted and billed the request while the response body was lost.
            run.status = "FAILED"
            run.outcome = "FAILED_AMBIGUOUS"
            run.failure_reason = "PROCESS_RESTART_AFTER_DISPATCH"
            run.failure_stage = "POST_DISPATCH"
            run.error_text = (
                "The previous process ended after CEO planning provider dispatch; effect/cost truth is unresolved "
                "and automatic replay is forbidden."
            )
            run.finished_at = now()
            if effect is not None and state != "AMBIGUOUS_POST_DISPATCH":
                effects.mark_ambiguous(effect, run.error_text)
            if reservation is not None and reservation.status == "RESERVED":
                reservation.status = "AMBIGUOUS"
                reservation.resolved_at = None
                reservation.resolution_note = run.error_text
        changed += 1
    if changed:
        db.session.commit()
    return changed

def recover_interrupted_standalone_runs(*, before) -> int:
    """Classify paid Runs from a previous process that have no runtime owner.

    Founder interviews, read-only CEO status summaries, legacy Project briefings
    and other standalone model calls do not have a Work/Meeting loop that can
    discover them after restart. Leaving them RUNNING forever is dangerous: a UI
    retry may either stall permanently or create a second paid call elsewhere.
    This routine performs no provider call. It only classifies the already
    durable external-effect boundary so callers can reuse/retry safely.
    """
    effects = __import__(
        "eason_one.services.external_effects",
        fromlist=["mark_pre_dispatch_failure", "mark_ambiguous"],
    )
    kernel = __import__(
        "eason_one.services.operation_kernel", fromlist=["resolve_reservation"]
    )
    changed = 0
    rows = (
        AgentRun.query.filter(
            AgentRun.status == "RUNNING",
            AgentRun.started_at < before,
            AgentRun.operation_id.is_(None),
            AgentRun.work_id.is_(None),
            AgentRun.meeting_id.is_(None),
            AgentRun.purpose != "CEO_FOUNDER_REQUEST",
        )
        .order_by(AgentRun.id)
        .all()
    )
    for run in rows:
        effect = (
            ExternalEffectAttempt.query.filter_by(execution_id=run.id)
            .order_by(ExternalEffectAttempt.id.desc()).first()
        )
        state = str(getattr(effect, "state", "") or "").upper()
        reservation = (
            db.session.get(CostReservation, effect.cost_reservation_id)
            if effect is not None and effect.cost_reservation_id else None
        )
        if effect is None or state in {"PREPARED", "RESERVED", "FAILED_PRE_DISPATCH"}:
            run.status = "FAILED"
            run.outcome = "FAILED_SAFE"
            run.failure_reason = "PROCESS_RESTART_BEFORE_DISPATCH"
            run.failure_stage = "PRE_DISPATCH"
            run.error_text = (
                "The previous process ended before standalone provider dispatch was durably observed; "
                "the exact request may retry safely under its own bounded policy."
            )
            run.finished_at = now()
            if effect is not None and state != "FAILED_PRE_DISPATCH":
                effects.mark_pre_dispatch_failure(effect, run.error_text)
            if reservation is not None and reservation.status in {"RESERVED", "AMBIGUOUS"}:
                kernel.resolve_reservation(
                    reservation, status="RELEASED",
                    note=f"Restart reconciliation released pre-dispatch standalone authority for Agent Run #{run.id}.",
                )
        else:
            run.status = "FAILED"
            run.outcome = "FAILED_AMBIGUOUS"
            run.failure_reason = "PROCESS_RESTART_AFTER_DISPATCH"
            run.failure_stage = "POST_DISPATCH"
            run.error_text = (
                "The previous process ended after standalone provider dispatch; effect/cost truth is unresolved "
                "and automatic replay is forbidden."
            )
            run.finished_at = now()
            if effect is not None and state != "AMBIGUOUS_POST_DISPATCH":
                effects.mark_ambiguous(effect, run.error_text)
            if reservation is not None and reservation.status == "RESERVED":
                reservation.status = "AMBIGUOUS"
                reservation.resolved_at = None
                reservation.resolution_note = run.error_text
        changed += 1
    if changed:
        db.session.commit()
    return changed


def recover_stale_work() -> int:
    changed = 0
    for work in Work.query.filter(Work.state.in_(["EXECUTING", "VERIFYING"])).order_by(Work.id).all():
        if not _is_kernel_work(work):
            continue
        live = AgentRun.query.filter_by(work_id=work.id, status="RUNNING").order_by(AgentRun.id.desc()).first()
        if not live:
            continue
        effect = ExternalEffectAttempt.query.filter_by(execution_id=live.id).order_by(ExternalEffectAttempt.id.desc()).first()
        state = getattr(effect, "state", None)
        if live.provider_key_snapshot == "codex":
            codex = __import__(
                "eason_one.services.codex_connector", fromlist=["reconcile_interrupted_run", "cleanup_recovery_snapshot"]
            )
            result = codex.reconcile_interrupted_run(live)
            if result.get("safe"):
                work_runtime.open_wait(
                    work, "INTERNAL_RECOVERY", result.get("reason") or "Safe Codex restart recovery.", retry_after=now(),
                    issue_code=f"CODEX_RESTART_RUN_{live.id}_SAFE_RETRY",
                )
                db.session.commit()
                codex.cleanup_recovery_snapshot(live)
            else:
                live.status = "FAILED"
                live.outcome = "FAILED_AMBIGUOUS"
                live.failure_reason = "CODEX_PROCESS_RESTART_RECONCILIATION"
                live.failure_stage = "CODEX_TOOL"
                live.error_text = result.get("reason") or "Interrupted Codex effect cannot be proven safe to replay."
                live.finished_at = now()
                if effect is not None:
                    __import__("eason_one.services.external_effects", fromlist=["mark_ambiguous"]).mark_ambiguous(effect, live.error_text)
                work_runtime.open_wait(
                    work, "RECONCILIATION", live.error_text,
                    issue_code=f"CODEX_RESTART_RUN_{live.id}_RECONCILIATION",
                )
            changed += 1
        elif state in {None, "PREPARED", "RESERVED", "FAILED_PRE_DISPATCH"}:
            live.status = "FAILED"
            live.outcome = "FAILED_SAFE"
            live.failure_reason = "PROCESS_RESTART_BEFORE_DISPATCH"
            live.failure_stage = "PRE_DISPATCH"
            live.error_text = "Process restarted before external dispatch was durably observed."
            live.finished_at = now()
            work_runtime.open_wait(
                work, "INTERNAL_RECOVERY", live.error_text, retry_after=now(),
                issue_code=f"RESTART_RUN_{live.id}_PRE_DISPATCH_RETRY",
            )
            changed += 1
        else:
            live.status = "FAILED"
            live.outcome = "FAILED_AMBIGUOUS"
            live.failure_reason = "PROCESS_RESTART_AFTER_DISPATCH"
            live.failure_stage = "POST_DISPATCH"
            live.error_text = "Process restarted after external dispatch; reconciliation is required before replay."
            live.finished_at = now()
            if effect is not None and effect.state != "AMBIGUOUS_POST_DISPATCH":
                __import__(
                    "eason_one.services.external_effects", fromlist=["mark_ambiguous"]
                ).mark_ambiguous(effect, live.error_text)
            reservation = (
                db.session.get(CostReservation, effect.cost_reservation_id)
                if effect is not None and effect.cost_reservation_id else None
            )
            if reservation is not None and reservation.status == "RESERVED":
                # Keep the estimated authority held, but label why it is held.
                # This is not a release and not permission to replay.
                reservation.status = "AMBIGUOUS"
                reservation.resolved_at = None
                reservation.resolution_note = live.error_text
            work_runtime.open_wait(
                work, "RECONCILIATION", live.error_text,
                issue_code=f"RESTART_RUN_{live.id}_POST_DISPATCH_RECONCILIATION",
            )
            changed += 1
    if changed:
        db.session.commit()
    return changed


def _is_validation_scope_fault(run: AgentRun | None) -> bool:
    """Prove a historical host validation veto was only unrelated repo tests."""
    if not run or run.failure_reason != "HOST_VALIDATION_FAILED":
        return False
    context = dict(run.context_composition_json or {})
    host = dict(context.get("host_validation") or {})
    checks = [row for row in (host.get("checks") or []) if isinstance(row, dict)]
    failed = [row for row in checks if row.get("status") == "FAILED"]
    http_passed = any(
        row.get("kind") == "HTTP_CONTRACT" and row.get("status") == "PASSED"
        for row in checks
    )
    regression_only = bool(failed) and all(
        row.get("kind") == "REGRESSION_SUITE" for row in failed
    )
    # codex_connector restores failed write deltas before committing failure.
    reverted = "failed_write_delta_reverted" in context
    return http_passed and regression_only and reverted


def reconcile_validation_scope_faults() -> list[int]:
    """Reopen only Work provably poisoned by the historical validation policy."""
    recovered: list[int] = []
    governance = __import__(
        "eason_one.services.governance", fromlist=["current_gate"]
    )
    for work in Work.query.filter_by(state="ABANDONED").order_by(Work.id).all():
        if work.work_type == "MANAGEMENT" or not _is_kernel_work(work):
            continue
        control = dict(work.runtime_control_json or {})
        if control.get("validation_scope_recovery_used"):
            continue
        latest = (
            AgentRun.query.filter_by(work_id=work.id, purpose="TASK_EXECUTION")
            .order_by(AgentRun.id.desc()).first()
        )
        if not _is_validation_scope_fault(latest):
            continue

        poisoned = []
        for run in AgentRun.query.filter_by(work_id=work.id, purpose="TASK_EXECUTION").all():
            if _is_validation_scope_fault(run):
                run.resolution_status = "SYSTEM_VALIDATION_SCOPE_FAULT"
                run.resolved_at = now()
                run.resolution_note = (
                    "v0.18 host validation used repository-wide regression outside "
                    "the approved Work acceptance scope; failed write delta was reverted."
                )
                poisoned.append(run.id)

        operation = work.operation
        project = work.project
        work_runtime.reopen_abandoned(
            work, "READY",
            reason="Recovered Work after a proven historical host-validation scope defect.",
        )
        control["validation_scope_recovery_used"] = True
        control["validation_scope_recovery"] = {
            "at": now().isoformat(),
            "source_run_id": latest.id,
            "platform_fault_run_ids": poisoned,
            "reason": "UNRELATED_REPOSITORY_REGRESSION_VETO",
        }
        work.runtime_control_json = control
        work_runtime.sync_task_projection(work)

        operation.status = "RUNNING"
        operation.kernel_status = "RUNNING"
        operation.current_stage = "COMPANY_KERNEL_V020"
        operation.waiting_reason = None
        operation.ended_at = None
        operation.founder_report_json = None
        operation.lease_owner = None
        operation.lease_expires_at = None

        # Do not hide an unrelated current Founder authority gate while repairing
        # a historical platform fault.
        if project and work_runtime.project_can_activate(project):
            project.status = "ACTIVE"
            project.current_state_summary = (
                f"{work.title} was reopened after Eason One reconciled an invalid "
                "repository-wide verification veto."
            )
            project.next_milestone = (
                "Engineer will retry the same approved Work under task-scoped host acceptance."
            )

        for management in [row for row in operation.works if row.work_type == "MANAGEMENT"]:
            if management.state == "ABANDONED":
                work_runtime.reopen_abandoned(
                    management, "EXECUTING",
                    reason="Management Work reopened with the repaired delivery Work after a proven platform defect.",
                )

        emit(
            "WORK_SYSTEM_RECONCILED", actor_type="RUNTIME",
            project_id=work.project_id, work_id=work.id,
            correlation_id=f"work:{work.id}",
            payload={
                "reason": "UNRELATED_REPOSITORY_REGRESSION_VETO",
                "source_run_id": latest.id,
                "reopened_state": "READY",
                "new_provider_call": False,
            },
        )
        if project:
            emit(
                "PROJECT_RECOVERED", actor_type="RUNTIME",
                project_id=project.id, work_id=work.id,
                correlation_id=f"project:{project.id}",
                payload={"reason": "SYSTEM_VALIDATION_SCOPE_FAULT"},
            )
        recovered.append(work.id)

    if recovered:
        db.session.commit()
    return recovered


def reconcile_superseded_codex_verification_gates() -> list[int]:
    """Invalidate Founder gates proven to be internal WSL verification defects."""
    reconciled: list[int] = []
    work_execution = __import__(
        "eason_one.services.work_execution",
        fromlist=["codex_founder_request_is_verification_only"],
    )
    governance = __import__(
        "eason_one.services.governance",
        fromlist=["invalidate_gate", "current_gate"],
    )
    for work in Work.query.filter_by(state="WAITING").order_by(Work.id).all():
        if work.work_type == "MANAGEMENT" or not _is_kernel_work(work):
            continue
        if not work_runtime.has_open_gate(work, "FOUNDER_DECISION"):
            continue
        latest = (
            AgentRun.query.filter_by(work_id=work.id, purpose="TASK_EXECUTION")
            .order_by(AgentRun.id.desc()).first()
        )
        if not latest or latest.status != "SUCCEEDED" or latest.provider_key_snapshot != "codex":
            continue
        codex = dict((latest.parsed_output_json or {}).get("codex") or {})
        if not work_execution.codex_founder_request_is_verification_only(latest, codex):
            continue

        reason = (
            "Eason One invalidated an internal Founder gate: Codex could not run "
            "verification inside WSL, while authoritative Windows host acceptance had already passed."
        )
        # Work wait is a compatibility projection; remove it even if an older
        # database lost the Escalation row. Canonical Escalations themselves are
        # invalidated only by the Governance owner below.
        resolved = work_runtime.resolve_waits(work, "FOUNDER_DECISION", note=reason)
        gates = Escalation.query.filter_by(
            work_id=work.id, state="OPEN", escalation_type="CODEX_RISK_APPROVAL"
        ).all()
        for gate in gates:
            governance.invalidate_gate(
                gate,
                resolution="SYSTEM_HOST_VERIFICATION_SUPERSEDED_CODEX_GATE",
                reason=reason,
            )
        if not resolved and not gates:
            continue

        context = dict(latest.context_composition_json or {})
        context["codex_founder_gate_reconciled"] = {
            "at": now().isoformat(),
            "reason": codex.get("founder_reason"),
            "basis": "WINDOWS_HOST_ACCEPTANCE_PASSED",
        }
        latest.context_composition_json = context
        latest.resolution_status = (
            latest.resolution_status or "SYSTEM_HOST_VERIFICATION_SUPERSEDED_CODEX_GATE"
        )
        latest.resolution_note = latest.resolution_note or (
            "WSL verification limitation was internal; Windows host acceptance already passed."
        )

        operation = work.operation
        operation.status = "RUNNING"
        operation.kernel_status = "RUNNING"
        operation.current_stage = "COMPANY_KERNEL_V020"
        operation.waiting_reason = None
        if (
            operation.project
            and operation.project.status == "BLOCKED"
            and work_runtime.project_can_activate(operation.project)
        ):
            operation.project.status = "ACTIVE"
        if operation.project:
            operation.project.current_state_summary = (
                f"{work.title} is continuing from the already-successful Engineer execution; "
                "an internal WSL verification limitation did not require Founder authority."
            )
            operation.project.next_milestone = (
                "Persist the already-proven Artifact and continue Project evidence review without another Codex call."
            )

        emit(
            "WORK_SYSTEM_RECONCILED", actor_type="RUNTIME",
            project_id=work.project_id, work_id=work.id,
            correlation_id=f"work:{work.id}",
            payload={
                "reason": "CODEX_WSL_VERIFICATION_GATE_SUPERSEDED_BY_HOST",
                "source_run_id": latest.id,
                "new_provider_call": False,
                "host_validation_success": True,
            },
        )
        reconciled.append(work.id)

    if reconciled:
        db.session.commit()
    return reconciled


def reconcile_reopened_rejected_founder_gates() -> list[int]:
    """Invalidate duplicate Founder questions already rejected under current truth.

    r6 could persist Founder REJECT correctly, then immediately open another
    BUDGET_AUTHORIZATION when the same paid closure step re-hit the unchanged
    cap. On restart/upgrade, retire those already-open duplicates without asking
    Founder to reject a second time.
    """
    reconciled: list[int] = []
    governance = __import__(
        "eason_one.services.governance",
        fromlist=["rejected_decision_for_gate", "invalidate_gate", "apply_rejected_budget_boundary"],
    )
    for gate in Escalation.query.filter_by(state="OPEN").order_by(Escalation.id).all():
        if str(gate.escalation_type or "").upper() != "BUDGET_AUTHORIZATION":
            continue
        rejection = governance.rejected_decision_for_gate(gate)
        if rejection is None:
            continue
        reason = (
            "This automated Project budget question is invalid because Founder already rejected "
            f"additional budget under the unchanged governing authority (Decision #{rejection.id}). "
            "Company execution must stop or replan inside the existing cap."
        )
        governance.invalidate_gate(
            gate, resolution="FOUNDER_REJECT_NEGATIVE_AUTHORITY_RECONCILED",
            reason=reason, actor_type="RUNTIME",
        )
        work = db.session.get(Work, gate.work_id) if gate.work_id else None
        project = gate.project
        if project and project.status not in {"REVIEW", "COMPLETED", "CANCELLED"}:
            governance.apply_rejected_budget_boundary(
                project=project, work=work, decision_id=rejection.id, escalation_id=gate.id
            )
        emit(
            "FOUNDER_REJECT_NEGATIVE_AUTHORITY_RECONCILED", actor_type="RUNTIME",
            project_id=gate.project_id, work_id=gate.work_id,
            correlation_id=f"project:{gate.project_id}",
            payload={
                "invalidated_escalation_id": gate.id,
                "rejected_decision_id": rejection.id,
                "authority_type": "BUDGET_AUTHORIZATION",
                "authority_granted": False,
            },
        )
        reconciled.append(gate.id)
    if reconciled:
        db.session.commit()
    return reconciled



def reconcile_superseded_research_provider_failures() -> list[int]:
    """Reopen one exhausted Research Work when its failed provider route is now obsolete.

    This is intentionally narrow: only terminal Research Work with no accepted
    Artifact, a safe/known exhausted execution failure, and a newly effective
    configured research provider may be reopened. The historical failed runs
    remain durable audit truth but no longer consume the new provider's bounded
    retry budget.
    """
    recovered: list[int] = []
    team = __import__(
        "eason_one.services.team_formation", fromlist=["infer_primary_capability"]
    )
    policy = __import__(
        "eason_one.services.execution_policy", fromlist=["select_research_model"]
    )
    artifacts = __import__(
        "eason_one.models", fromlist=["ArtifactVersion", "Artifact"]
    )
    for work in Work.query.filter_by(state="ABANDONED").order_by(Work.id).all():
        if work.work_type == "MANAGEMENT" or not _is_kernel_work(work):
            continue
        capability, _ = team.infer_primary_capability(work)
        if capability != "RESEARCH":
            continue
        control = dict(work.runtime_control_json or {})
        if control.get("research_provider_recovery_used"):
            continue
        latest = (
            AgentRun.query.filter_by(work_id=work.id, purpose="TASK_EXECUTION")
            .order_by(AgentRun.id.desc()).first()
        )
        if not latest or latest.status != "FAILED":
            continue
        if latest.resolution_status != "WORK_RETRY_EXHAUSTED":
            continue
        if latest.outcome not in {"FAILED_SAFE", "FAILED_KNOWN"}:
            continue
        if latest.failure_reason not in {
            "OUTPUT_TRUNCATED", "RESEARCH_TOOL_UNAVAILABLE",
            "PROVIDER_PREFLIGHT_FAILED", "STRUCTURED_OUTPUT_INVALID",
            "RESEARCH_SOURCE_EVIDENCE_MISSING",
        }:
            continue
        accepted = (
            artifacts.ArtifactVersion.query.join(artifacts.Artifact)
            .filter(artifacts.Artifact.work_id == work.id)
            .filter(artifacts.ArtifactVersion.status.in_(["SUBMITTED", "ACCEPTED"]))
            .first()
        )
        if accepted is not None:
            continue
        assignment = work_runtime.active_assignment(work)
        employee = assignment.employee if assignment else None
        if employee is None:
            continue
        try:
            current_model = policy.select_research_model(employee, work.operation)
        except Exception:
            continue
        if current_model is None:
            continue
        old_route = (str(latest.provider_key_snapshot or ""), str(latest.model_name_snapshot or ""))
        new_route = (str(current_model.provider_key or ""), str(current_model.model_name or ""))
        if old_route == new_route:
            continue

        superseded_ids = []
        for run in AgentRun.query.filter_by(work_id=work.id, purpose="TASK_EXECUTION").all():
            if run.status == "FAILED" and (
                str(run.provider_key_snapshot or ""), str(run.model_name_snapshot or "")
            ) != new_route:
                run.resolution_status = "SYSTEM_PROVIDER_ROUTING_SUPERSEDED"
                run.resolution_note = (
                    f"Historical Research execution route {run.provider_key_snapshot}/{run.model_name_snapshot} "
                    f"was superseded by configured formal route {new_route[0]}/{new_route[1]}."
                )
                run.resolved_at = now()
                superseded_ids.append(run.id)

        work_runtime.reopen_abandoned(
            work, "READY",
            reason=(
                f"Recovered exhausted Research Work after formal provider route changed from "
                f"{old_route[0]}/{old_route[1]} to {new_route[0]}/{new_route[1]}."
            ),
        )
        control = dict(work.runtime_control_json or {})
        control["research_provider_recovery_used"] = True
        control["research_provider_recovery"] = {
            "at": now().isoformat(),
            "source_run_id": latest.id,
            "superseded_run_ids": superseded_ids,
            "old_provider": old_route[0],
            "old_model": old_route[1],
            "new_provider": new_route[0],
            "new_model": new_route[1],
        }
        work.runtime_control_json = control
        work_runtime.sync_task_projection(work)
        project = work.project
        if project and work_runtime.project_can_activate(project):
            project.status = "ACTIVE"
            project.current_state_summary = (
                f"{work.title} was reopened after Eason One adopted the newly configured Research provider."
            )
            project.next_milestone = "Research will retry once through the current formal provider route."
        emit(
            "WORK_RESEARCH_PROVIDER_ROUTE_RECONCILED", actor_type="RUNTIME",
            project_id=work.project_id, work_id=work.id, correlation_id=f"work:{work.id}",
            payload={
                "source_run_id": latest.id,
                "superseded_run_ids": superseded_ids,
                "old_route": {"provider": old_route[0], "model": old_route[1]},
                "new_route": {"provider": new_route[0], "model": new_route[1]},
                "new_provider_call": False,
            },
        )
        recovered.append(work.id)
    if recovered:
        db.session.commit()
    return recovered

def reconcile_vnext_legacy_elapsed_vetoes() -> list[int]:
    """Reopen Work stopped only by the historical Operation wall-clock veto.

    vNext Project/Work execution is durable across restarts and long waits.  A
    provider attempt that failed PRE_DISPATCH solely because the legacy Mission
    had existed longer than ``max_elapsed_seconds`` never crossed an external
    effect boundary and therefore may be safely retried once after the platform
    defect is repaired.  No accepted Artifact may exist and the exact failure
    signature is required.
    """
    recovered: list[int] = []
    for work in Work.query.filter_by(state="ABANDONED").order_by(Work.id).all():
        if work.work_type == "MANAGEMENT" or not _is_kernel_work(work):
            continue
        control = dict(work.runtime_control_json or {})
        if control.get("vnext_elapsed_limit_recovery_used"):
            continue
        latest = (
            AgentRun.query.filter_by(work_id=work.id, purpose="TASK_EXECUTION")
            .order_by(AgentRun.id.desc()).first()
        )
        if not latest or latest.status != "FAILED":
            continue
        if latest.failure_reason != "PROVIDER_PREFLIGHT_FAILED" or latest.failure_stage != "PRE_DISPATCH":
            continue
        if "Operation maximum elapsed time reached" not in str(latest.error_text or ""):
            continue
        accepted = (
            ArtifactVersion.query.join(Artifact)
            .filter(Artifact.work_id == work.id)
            .filter(ArtifactVersion.status.in_(["SUBMITTED", "ACCEPTED"]))
            .first()
        )
        if accepted is not None:
            continue

        superseded_ids: list[int] = []
        for run in AgentRun.query.filter_by(work_id=work.id, purpose="TASK_EXECUTION").all():
            if (
                run.status == "FAILED"
                and run.failure_reason == "PROVIDER_PREFLIGHT_FAILED"
                and run.failure_stage == "PRE_DISPATCH"
                and "Operation maximum elapsed time reached" in str(run.error_text or "")
            ):
                run.resolution_status = "SYSTEM_VNEXT_ELAPSED_LIMIT_SUPERSEDED"
                run.resolution_note = (
                    "Historical vNext dispatch was vetoed before provider dispatch by the legacy "
                    "Operation wall-clock max_elapsed_seconds guard. Project/Work authority now owns "
                    "durable execution; this platform-fault attempt does not consume Work retry budget."
                )
                run.resolved_at = now()
                superseded_ids.append(run.id)

        work_runtime.reopen_abandoned(
            work, "READY",
            reason=(
                "Recovered Work after Eason One removed the legacy Operation wall-clock veto "
                "from durable vNext Project execution."
            ),
        )
        control = dict(work.runtime_control_json or {})
        control["vnext_elapsed_limit_recovery_used"] = True
        control["vnext_elapsed_limit_recovery"] = {
            "at": now().isoformat(),
            "source_run_id": latest.id,
            "superseded_run_ids": superseded_ids,
            "reason": "LEGACY_OPERATION_WALL_CLOCK_VETO",
        }
        work.runtime_control_json = control
        work_runtime.sync_task_projection(work)

        project = work.project
        if project and work_runtime.project_can_activate(project):
            project.status = "ACTIVE"
            project.current_state_summary = (
                f"{work.title} was reopened after Eason One removed a stale legacy Operation elapsed-time veto."
            )
            project.next_milestone = "Company Runtime will dispatch the same approved Work under current Project/Work authority."
        emit(
            "WORK_SYSTEM_RECONCILED", actor_type="RUNTIME",
            project_id=work.project_id, work_id=work.id,
            correlation_id=f"work:{work.id}",
            payload={
                "reason": "LEGACY_OPERATION_WALL_CLOCK_VETO",
                "source_run_id": latest.id,
                "superseded_run_ids": superseded_ids,
                "reopened_state": "READY",
                "new_provider_call": False,
            },
        )
        recovered.append(work.id)

    if recovered:
        db.session.commit()
    return recovered




def reconcile_perplexity_response_format_rejections() -> list[int]:
    """Reconcile the historical Sonar ``json_object`` adapter defect.

    Perplexity returned a definitive HTTP 400, so the request produced no model
    completion. After the adapter moves to ``json_schema``, this exact known
    platform fault may be replayed once. The historical run/effect remain audit
    truth, its ambiguous reservation is released, and no accepted Artifact may
    exist.
    """
    recovered: list[int] = []
    models = __import__(
        "eason_one.models", fromlist=["CostReservation"]
    )
    kernel = __import__(
        "eason_one.services.operation_kernel", fromlist=["resolve_reservation"]
    )
    effects = __import__(
        "eason_one.services.external_effects", fromlist=["mark_rejected"]
    )
    for work in Work.query.filter_by(state="WAITING").order_by(Work.id).all():
        if work.work_type == "MANAGEMENT" or not _is_kernel_work(work):
            continue
        control = dict(work.runtime_control_json or {})
        if control.get("perplexity_response_format_recovery_used"):
            continue
        latest = (
            AgentRun.query.filter_by(work_id=work.id, purpose="TASK_EXECUTION")
            .order_by(AgentRun.id.desc()).first()
        )
        if not latest or latest.status != "FAILED":
            continue
        error = str(latest.error_text or "")
        if not (
            latest.provider_key_snapshot == "perplexity"
            and latest.failure_reason == "PROVIDER_DISPATCH_AMBIGUOUS"
            and latest.failure_stage == "POST_DISPATCH"
            and "response_format.type" in error
            and "json_object" in error
            and "400" in error
        ):
            continue
        accepted = (
            ArtifactVersion.query.join(Artifact)
            .filter(Artifact.work_id == work.id)
            .filter(ArtifactVersion.status.in_(["SUBMITTED", "ACCEPTED"]))
            .first()
        )
        if accepted is not None:
            continue
        effect = (
            ExternalEffectAttempt.query.filter_by(execution_id=latest.id)
            .order_by(ExternalEffectAttempt.id.desc()).first()
        )
        if effect is None or effect.state != "AMBIGUOUS_POST_DISPATCH":
            continue

        matching_gate = None
        gates = [dict(row) for row in (control.get("gates") or [])]
        for gate in gates:
            if (
                gate.get("state") == "OPEN"
                and gate.get("condition_type") == "RECONCILIATION"
                and "response_format.type" in str(gate.get("reason") or "")
                and "json_object" in str(gate.get("reason") or "")
            ):
                matching_gate = gate
                gate["resume_state"] = "READY"
                break
        if matching_gate is None or not matching_gate.get("issue_code"):
            continue

        latest.failure_reason = "PROVIDER_REQUEST_REJECTED"
        latest.outcome = "FAILED_KNOWN"
        latest.resolution_status = "SYSTEM_PERPLEXITY_RESPONSE_FORMAT_SUPERSEDED"
        latest.resolved_at = now()
        latest.resolution_note = (
            "Perplexity definitively rejected the obsolete json_object response format with HTTP 400. "
            "The Sonar adapter now uses official json_schema structured output; replay is safe."
        )
        effects.mark_rejected(effect, error)
        control["gates"] = gates
        control["perplexity_response_format_recovery_used"] = True
        control["perplexity_response_format_recovery"] = {
            "at": now().isoformat(),
            "source_run_id": latest.id,
            "provider": "perplexity",
            "old_response_format": "json_object",
            "new_response_format": "json_schema",
            "http_status": 400,
        }
        work.runtime_control_json = control
        db.session.commit()

        reservation = (
            db.session.get(models.CostReservation, effect.cost_reservation_id)
            if effect.cost_reservation_id else None
        )
        if reservation is not None and reservation.status in {"RESERVED", "AMBIGUOUS"}:
            kernel.resolve_reservation(
                reservation, status="RELEASED",
                note="Perplexity HTTP 400 definitively rejected obsolete json_object response format; no model completion was returned.",
            )

        work = db.session.get(Work, work.id)
        resolved = work_runtime.resolve_waits(
            work, "RECONCILIATION",
            issue_code=str(matching_gate["issue_code"]),
            note="Perplexity Sonar structured-output adapter repaired: json_object -> json_schema.",
        )
        if not resolved:
            continue
        work_runtime.sync_task_projection(work)
        project = work.project
        if project and work_runtime.project_can_activate(project):
            project.status = "ACTIVE"
            project.current_state_summary = (
                f"{work.title} is ready after Eason One repaired the Perplexity Sonar structured-output adapter."
            )
            project.next_milestone = "Research will retry through Perplexity Sonar using JSON Schema structured output."
        emit(
            "WORK_SYSTEM_RECONCILED", actor_type="RUNTIME",
            project_id=work.project_id, work_id=work.id, correlation_id=f"work:{work.id}",
            payload={
                "reason": "PERPLEXITY_JSON_OBJECT_RESPONSE_FORMAT_REJECTED",
                "source_run_id": latest.id,
                "http_status": 400,
                "old_response_format": "json_object",
                "new_response_format": "json_schema",
                "new_provider_call": False,
            },
        )
        recovered.append(work.id)

    if recovered:
        db.session.commit()
    return recovered



def reconcile_research_review_evidence_scope_faults() -> list[int]:
    """Repair the historical Research-review evidence-ownership defect.

    Research execution already fails closed unless its provider returned
    observed source lineage. Older runtime then made a second, unrelated demand:
    TASK_REVIEW itself also had to emit provider sources, and it could override
    the frozen reviewer with a Research provider to chase that redundant proof.

    If the exact submitted ArtifactVersion is backed by a successful producing
    run with provider_sources, any review failure caused solely by that obsolete
    rule is a platform policy fault. Preserve all paid run/cost history, mark the
    affected review attempts superseded for retry accounting, and resume the
    same ArtifactVersion at VERIFYING without replaying Research.
    """
    recovered: list[int] = []
    for work in Work.query.filter(Work.state.in_(["WAITING", "ABANDONED"])).order_by(Work.id).all():
        if work.work_type == "MANAGEMENT" or not _is_kernel_work(work):
            continue
        control = dict(work.runtime_control_json or {})
        if control.get("research_review_evidence_scope_recovery_used"):
            continue

        review_runs = (
            AgentRun.query.filter_by(work_id=work.id, purpose="TASK_REVIEW")
            .order_by(AgentRun.id).all()
        )
        faulty_runs = [
            run for run in review_runs
            if run.status == "FAILED"
            and run.failure_reason == "RESEARCH_REVIEW_SOURCE_EVIDENCE_MISSING"
            and run.failure_stage == "POSTPROCESS"
            and "no provider-observed source evidence" in str(run.error_text or "")
        ]
        if not faulty_runs:
            continue
        latest_review = review_runs[-1] if review_runs else None
        if latest_review is not None and latest_review not in faulty_runs and latest_review.status == "SUCCEEDED":
            # A newer valid review already supersedes the historical fault.
            continue

        version = (
            ArtifactVersion.query.join(Artifact)
            .filter(Artifact.work_id == work.id)
            .filter(ArtifactVersion.status.in_(["SUBMITTED", "ACCEPTED"]))
            .order_by(ArtifactVersion.id.desc()).first()
        )
        if version is None or not version.execution_id:
            continue
        producer_run = db.session.get(AgentRun, version.execution_id)
        producer_sources = [
            row for row in ((getattr(producer_run, "context_composition_json", None) or {}).get("provider_sources") or [])
            if isinstance(row, dict) and str(row.get("url") or "").strip()
        ] if producer_run else []
        if not producer_run or producer_run.status != "SUCCEEDED" or not producer_sources:
            continue

        for run in faulty_runs:
            run.resolution_status = "SYSTEM_RESEARCH_REVIEW_EVIDENCE_SCOPE_SUPERSEDED"
            run.resolved_at = now()
            run.resolution_note = (
                "Provider execution completed, but an obsolete platform rule rejected TASK_REVIEW because the reviewer call "
                "did not emit a second source list. Source provenance is owned by the exact Research ArtifactVersion: "
                f"producing Run #{producer_run.id} has {len(producer_sources)} provider-observed source(s)."
            )

        gates = [dict(row) for row in (control.get("gates") or [])]
        resolved_gate_ids = []
        for gate in gates:
            if (
                gate.get("state") == "OPEN"
                and gate.get("condition_type") == "INTERNAL_RECOVERY"
                and "no provider-observed source evidence" in str(gate.get("reason") or "")
                and gate.get("resume_state") == "VERIFYING"
            ):
                gate["state"] = "RESOLVED"
                gate["resolved_at"] = now().isoformat()
                gate["resolution_note"] = (
                    "Research source provenance is owned by the exact producing ArtifactVersion; "
                    "independent review no longer requires a second provider source trace."
                )
                resolved_gate_ids.append(gate.get("id"))

        control["gates"] = gates
        control["research_review_evidence_scope_recovery_used"] = True
        control["research_review_evidence_scope_recovery"] = {
            "at": now().isoformat(),
            "source_review_run_ids": [run.id for run in faulty_runs],
            "artifact_version_id": version.id,
            "producing_execution_id": producer_run.id,
            "provider_source_count": len(producer_sources),
            "old_policy": "REVIEW_CALL_MUST_EMIT_PROVIDER_SOURCES",
            "new_policy": "REVIEW_EXACT_ARTIFACT_PROVIDER_SOURCE_LINEAGE",
            "resolved_gate_ids": resolved_gate_ids,
        }
        work.runtime_control_json = control

        if work.state == "ABANDONED":
            work_runtime.reopen_abandoned(
                work, "VERIFYING",
                reason=(
                    "Reopened after Eason One repaired Research review evidence ownership; "
                    "the existing Research ArtifactVersion remains valid and implementation is not replayed."
                ),
            )
        elif work.state == "WAITING":
            remaining = [row for row in gates if row.get("state") == "OPEN"]
            if not remaining:
                work_runtime.transition(
                    work, "VERIFYING", actor_type="RUNTIME",
                    reason="Research review evidence ownership repaired; exact ArtifactVersion remains the review subject.",
                )
        work_runtime.sync_task_projection(work)
        project = work.project
        if project and work_runtime.project_can_activate(project):
            project.status = "ACTIVE"
            project.current_state_summary = (
                f"{work.title} is awaiting independent review of its persisted Research Artifact/source lineage."
            )
            project.next_milestone = "Frozen reviewer will evaluate the existing Research Artifact without replaying Research."
        emit(
            "WORK_SYSTEM_RECONCILED", actor_type="RUNTIME",
            project_id=work.project_id, work_id=work.id, correlation_id=f"work:{work.id}",
            payload={
                "reason": "RESEARCH_REVIEW_EVIDENCE_OWNERSHIP_SCOPE",
                "source_review_run_ids": [run.id for run in faulty_runs],
                "artifact_version_id": version.id,
                "producing_execution_id": producer_run.id,
                "provider_source_count": len(producer_sources),
                "implementation_replayed": False,
                "new_provider_call": False,
            },
        )
        recovered.append(work.id)

    if recovered:
        db.session.commit()
    return recovered


def reconcile_superseded_review_output_caps() -> list[int]:
    """Resume exact Artifact review attempts exhausted by an obsolete hidden cap.

    A review truncation is a platform-policy fault only when all of these are
    durable facts: the producer succeeded, the exact ArtifactVersion is still
    submitted, the terminal review attempt stopped at max output tokens, and the
    current reviewer ModelConfig permits a strictly larger envelope than the one
    actually used.  Paid attempts remain immutable audit evidence.  Production
    execution / Research is never replayed.
    """
    recovered: list[int] = []
    work_execution = __import__(
        "eason_one.services.work_execution", fromlist=["review_output_token_limit"]
    )
    policy = __import__(
        "eason_one.services.execution_policy", fromlist=["select_execution_model"]
    )
    Employee = __import__("eason_one.models", fromlist=["Employee"]).Employee
    for work in Work.query.filter_by(state="ABANDONED").order_by(Work.id).all():
        if work.work_type == "MANAGEMENT" or not _is_kernel_work(work):
            continue
        control = dict(work.runtime_control_json or {})
        if control.get("review_output_cap_recovery_used"):
            continue
        version = (
            ArtifactVersion.query.join(Artifact)
            .filter(Artifact.work_id == work.id, ArtifactVersion.status == "SUBMITTED")
            .order_by(ArtifactVersion.id.desc()).first()
        )
        if version is None or not version.execution_id:
            continue
        producer = db.session.get(AgentRun, version.execution_id)
        if producer is None or producer.status != "SUCCEEDED":
            continue
        reviews = (
            AgentRun.query.filter_by(work_id=work.id, purpose="TASK_REVIEW")
            .filter(AgentRun.id > producer.id).order_by(AgentRun.id).all()
        )
        if not reviews:
            continue
        latest = reviews[-1]
        if not (
            latest.status == "FAILED"
            and latest.failure_reason == "OUTPUT_TRUNCATED"
            and latest.provider_stop_reason in {"max_tokens", "max_output_tokens"}
        ):
            continue
        task = next((row for row in work.operation.tasks if row.work_id == work.id), None)
        contract = dict(control.get("acceptance_contract") or {})
        reviewer_id = contract.get("reviewer_employee_id")
        reviewer = db.session.get(Employee, reviewer_id) if reviewer_id else None
        if task is None or reviewer is None or not reviewer.active:
            continue
        semantic = [row for row in (contract.get("criteria") or []) if row.get("kind") != "HOST"]
        model = policy.select_execution_model(reviewer, work.operation, "TASK_REVIEW")
        new_limit = work_execution.review_output_token_limit(model, semantic)
        truncations = [
            run for run in reviews
            if run.status == "FAILED"
            and run.failure_reason == "OUTPUT_TRUNCATED"
            and run.provider_stop_reason in {"max_tokens", "max_output_tokens"}
            and 0 < int(run.effective_max_output_tokens or 0) < int(new_limit)
        ]
        if latest not in truncations:
            continue
        old_limit = max(int(run.effective_max_output_tokens or 0) for run in truncations)

        for run in truncations:
            run.resolution_status = "SYSTEM_REVIEW_OUTPUT_CAP_SUPERSEDED"
            run.resolved_at = now()
            run.resolution_note = (
                f"Historical review was truncated by an obsolete hidden {run.effective_max_output_tokens}-token "
                f"envelope; current reviewer ModelConfig allows {new_limit}. The exact ArtifactVersion is reused."
            )
        work_runtime.reopen_abandoned(
            work, "VERIFYING",
            reason=(
                "Reopened exact ArtifactVersion review after removing an obsolete hidden output ceiling; "
                "producer execution and Artifact are reused without replay."
            ),
        )
        control = dict(work.runtime_control_json or {})
        control["review_output_cap_recovery_used"] = True
        control["review_output_cap_recovery"] = {
            "at": now().isoformat(),
            "artifact_version_id": version.id,
            "producing_execution_id": producer.id,
            "review_run_ids": [run.id for run in truncations],
            "old_limit": old_limit,
            "new_limit": new_limit,
            "implementation_replayed": False,
            "recovery_prompt": "COMPACT_REVIEW",
        }
        work.runtime_control_json = control
        work_runtime.sync_task_projection(work, task)
        if work.project and work_runtime.project_can_activate(work.project):
            work.project.status = "ACTIVE"
            work.project.current_state_summary = (
                f"{work.title} is recovering independent review of its persisted ArtifactVersion."
            )
            work.project.next_milestone = (
                "Frozen reviewer will retry the same Artifact with the full ModelConfig envelope and compact output contract."
            )
        emit(
            "WORK_SYSTEM_RECONCILED", actor_type="RUNTIME",
            project_id=work.project_id, work_id=work.id,
            correlation_id=f"work:{work.id}",
            payload={
                "reason": "REVIEW_OUTPUT_CAP_SUPERSEDED",
                "artifact_version_id": version.id,
                "producing_execution_id": producer.id,
                "review_run_ids": [run.id for run in truncations],
                "old_limit": old_limit,
                "new_limit": new_limit,
                "implementation_replayed": False,
                "new_provider_call": False,
            },
        )
        recovered.append(work.id)
    if recovered:
        db.session.commit()
    return recovered

def reconcile_known_platform_faults() -> int:
    """Current v0.20 bounded repairs; no legacy runtime business call is made."""
    return (
        len(reconcile_validation_scope_faults())
        + len(reconcile_superseded_research_provider_failures())
        + len(reconcile_vnext_legacy_elapsed_vetoes())
        + len(reconcile_perplexity_response_format_rejections())
        + len(reconcile_research_review_evidence_scope_faults())
        + len(reconcile_superseded_review_output_caps())
        + len(reconcile_superseded_codex_verification_gates())
        + len(reconcile_reopened_rejected_founder_gates())
    )
