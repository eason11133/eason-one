from ..extensions import db
import hashlib
import json
from ..models import AgentRun, now
from ..providers import get_provider
from ..provider_protocol import classify_http_failure, retry_after_seconds, canonical_incomplete_reason
from .context import build
from .costs import ensure_budget, calculate, record, estimate_execution


def provider_preflight(model, *, operation=None, purpose=None, web_search=False):
    from flask import current_app
    import os
    project = getattr(operation, "project", None) if operation is not None else None
    if str(getattr(project, "status", "") or "").upper() == "PAUSED":
        raise ValueError("PROJECT_PAUSED: Founder paused this Project; no new provider dispatch is authorized.")
    if model.provider_key == "mock":
        simulation = bool(
            current_app.config.get("TESTING")
            or current_app.config.get("ALLOW_MOCK_PROVIDER")
            or ((getattr(operation, "memory_json", None) or {}).get("simulation_mode"))
        )
        if not simulation:
            raise ValueError(
                "MockProvider is forbidden for a formal Mission. Configure a real "
                "model or use an explicit simulation/test environment."
            )
        return True
    credentials = {
        "openai": "OPENAI_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
        "gemini": "GEMINI_API_KEY",
        "perplexity": "PERPLEXITY_API_KEY",
    }
    if model.provider_key == "codex":
        if purpose not in {None, "TASK_EXECUTION"}:
            raise ValueError("Codex CLI is an Engineering tool, not a general model provider")
        __import__(
            "eason_one.services.codex_connector", fromlist=["_resolve_codex_command"]
        )._resolve_codex_command()
        return True
    credential = credentials.get(model.provider_key)
    # Release tests replace the provider boundary with a deterministic local
    # double. Requiring a production secret before that boundary made the same
    # suite depend on whether a deployment .env happened to be present.
    if credential and not current_app.config.get("TESTING") and not os.getenv(credential):
        raise ValueError(
            f"{model.provider_key.title()} provider is not configured. "
            f"Set {credential} and verify the configuration in Models & Providers."
        )
    if model.provider_key not in {*credentials, "codex"}:
        raise ValueError(f"Unsupported provider: {model.provider_key}")
    if (
        (model.provider_key == "perplexity" or (model.provider_key == "openai" and web_search))
        and operation is not None
        and not current_app.config.get("TESTING")
        and getattr(model, "request_price_per_call", 0) <= 0
    ):
        raise ValueError(
            f"{model.provider_key.title()} web research requires an explicit request_price_per_call in Company currency so hosted search cost cannot bypass the governed budget."
        )
    return True




def _provider_failure_classification(exc):
    """Normalize provider HTTP failures using the shared protocol contract."""
    return classify_http_failure(exc)


_OPERATION_STEP_KIND_BY_PURPOSE = {
    "TASK_EXECUTION": "TASK",
    "TASK_REVIEW": "REVIEW",
    "ORCHESTRATION_PLAN": "ORCHESTRATION",
    "CEO_OPERATION_DECISION": "DECISION",
    "GOAL_VERIFICATION": "GOAL_VERIFICATION",
    "CEO_OPERATION_REPORT": "REPORT",
}


def _operation_step_for_execution(operation, purpose, task, retry_of_run=None):
    """Return the exact open OperationStep that owns this paid execution.

    OperationStep is orchestration truth while AgentRun/ExternalEffectAttempt own
    provider truth.  The old runtime linked those rows only *after* execute()
    returned, leaving a crash window where a paid response existed but the open
    step still looked pre-dispatch and could be replayed.  Bind the step to the
    AgentRun before the provider boundary instead.

    A retry may replace only the Run currently bound to the same step.  Multiple
    eligible open steps are a deterministic consistency failure and must stop
    before any external side effect.
    """
    if operation is None:
        return None
    kind = _OPERATION_STEP_KIND_BY_PURPOSE.get(str(purpose or ""))
    if kind is None:
        return None
    OperationStep = __import__(
        "eason_one.models", fromlist=["OperationStep"]
    ).OperationStep
    query = OperationStep.query.filter(
        OperationStep.operation_id == operation.id,
        OperationStep.kind == kind,
        OperationStep.status.in_(["CLAIMED", "EXECUTION_STARTED", "PROVIDER_CALL_STARTED", "AMBIGUOUS"]),
    )
    if kind in {"TASK", "REVIEW"}:
        task_id = int(getattr(task, "id", 0) or 0)
        if not task_id:
            raise ValueError(f"{kind}_EXECUTION_REQUIRES_TASK_LINEAGE")
        query = query.filter(OperationStep.task_id == task_id)
    rows = query.order_by(OperationStep.id.desc()).all()
    if not rows:
        return None
    if len(rows) != 1:
        raise ValueError(
            f"OPERATION_STEP_EXECUTION_BINDING_AMBIGUOUS: {len(rows)} open {kind} steps for Operation #{operation.id}"
        )
    step = rows[0]
    bound = int(getattr(step, "agent_run_id", 0) or 0)
    if retry_of_run is None:
        if bound:
            raise ValueError(
                f"OPERATION_STEP_ALREADY_BOUND: step #{step.id} already owns AgentRun #{bound}; recover it instead of buying another call"
            )
    else:
        prior_id = int(getattr(retry_of_run, "id", 0) or 0)
        if bound and bound != prior_id:
            raise ValueError(
                f"OPERATION_STEP_RETRY_LINEAGE_MISMATCH: step #{step.id} owns AgentRun #{bound}, not retry source #{prior_id}"
            )
    return step


def execute(employee, purpose, user_request, project=None, task=None, context_override=None, postprocess=None, system_prompt_override=None,response_schema=None,meeting=None,max_output_tokens_override=None,context_composition=None,operation=None,prompt_version=None,retry_of_run=None,model_override=None,work=None):
    """Execute one governed model attempt and persist external-effect truth.

    Company Core vNext treats this AgentRun as an Execution Attempt.  Provider
    preflight/budget failures are durable FAILED_SAFE attempts; exceptions after
    the durable dispatch boundary are FAILED_AMBIGUOUS until reconciled.  The
    caller decides what that means for the parent Work.

    Persistent Employee identity is distinct from one model invocation.  A formal
    governed Operation must never fail merely because an Employee still carries a
    mock/default persistent binding: unless the caller deliberately selected a
    specialized model (for example live Web Search), the central execution boundary
    applies the deterministic execution policy and routes the attempt to a configured
    real provider without mutating the Employee's persistent ModelConfig.
    """
    if not employee.current_model: raise ValueError("Employee has no model")
    operation=operation or getattr(task,"operation",None) or getattr(meeting,"operation",None)
    if retry_of_run is not None:
        if str(getattr(retry_of_run, "purpose", "") or "") != str(purpose or ""):
            raise ValueError("EXECUTION_RETRY_PURPOSE_LINEAGE_MISMATCH")
        if operation is not None and int(getattr(retry_of_run, "operation_id", 0) or 0) != int(operation.id):
            raise ValueError("EXECUTION_RETRY_OPERATION_LINEAGE_MISMATCH")
        if task is not None and int(getattr(retry_of_run, "task_id", 0) or 0) != int(task.id):
            raise ValueError("EXECUTION_RETRY_TASK_LINEAGE_MISMATCH")
        if meeting is not None and int(getattr(retry_of_run, "meeting_id", 0) or 0) != int(meeting.id):
            raise ValueError("EXECUTION_RETRY_MEETING_LINEAGE_MISMATCH")
        if work is not None and int(getattr(retry_of_run, "work_id", 0) or 0) != int(work.id):
            raise ValueError("EXECUTION_RETRY_WORK_LINEAGE_MISMATCH")
        replay_ok, replay_reason = __import__(
            "eason_one.services.external_effects", fromlist=["retry_authorized"]
        ).retry_authorized(retry_of_run)
        if not replay_ok:
            raise ValueError(
                f"EXECUTION_RETRY_FORBIDDEN_UNRESOLVED_EFFECT: {replay_reason}"
            )
    composition=dict(context_composition or {})
    execution_mode=str(composition.get("execution_mode") or "").upper()
    web_search = execution_mode in {"LIVE_WEB_RESEARCH", "LIVE_WEB_RESEARCH_REVIEW"}
    if model_override is None and operation is not None:
        model=__import__(
            "eason_one.services.execution_policy", fromlist=["select_execution_model"]
        ).select_execution_model(employee, operation, purpose)
    else:
        model=model_override or employee.current_model
    if model_override is not None and operation is not None:
        policy=__import__(
            "eason_one.services.execution_policy", fromlist=["model_override_authorized"]
        )
        if not policy.model_override_authorized(
            employee, model, operation, web_search=web_search
        ):
            raise ValueError(
                "MODEL_OVERRIDE_VIOLATES_APPROVED_EXECUTION_POLICY: caller-selected model is outside durable Founder/Project authority."
            )
    if model.archived: raise ValueError("Employee's execution ModelConfig is archived")
    if not model.active: raise ValueError("Employee's execution ModelConfig is inactive")
    if project is None and operation is not None:
        project = getattr(operation, "project", None)
    if work is None and task is not None:
        work=__import__("eason_one.services.work_runtime",fromlist=["work_for_task"]).work_for_task(task)
    if work is None and meeting is not None and getattr(meeting, "related_work_id", None):
        work=db.session.get(__import__("eason_one.models",fromlist=["Work"]).Work, meeting.related_work_id)
    if project is None and work is not None:
        project = getattr(work, "project", None)

    # Terminal Project lifecycle is a hard external-effect fence. Existing
    # provider/cost truth may still be reconciled by restart settlement, but no
    # new AgentRun/provider attempt may be created after Founder completion or
    # cancellation. Keep this at the central execution boundary so stale Work,
    # Meeting, HR, recovery or scheduler callers cannot buy around the fence.
    if project is not None and __import__(
        "eason_one.services.work_runtime", fromlist=["project_is_terminal"]
    ).project_is_terminal(project):
        raise ValueError(
            f"PROJECT_TERMINAL_EXECUTION_FORBIDDEN:{str(getattr(project, 'status', '') or '').upper()}"
        )

    # Governed Project execution must be traceable to one exact authority
    # lineage. A Project object by itself is never a model-spend capability.
    if project is not None and __import__(
        "eason_one.services.project_contract", fromlist=["is_vnext_governed"]
    ).is_vnext_governed(project):
        if operation is None or work is None:
            raise ValueError("GOVERNED_PROJECT_EXECUTION_REQUIRES_OPERATION_AND_WORK_AUTHORITY")
        if int(getattr(operation, "project_id", 0) or 0) != int(project.id):
            raise ValueError("EXECUTION_OPERATION_PROJECT_LINEAGE_MISMATCH")
        if int(getattr(work, "project_id", 0) or 0) != int(project.id):
            raise ValueError("EXECUTION_WORK_PROJECT_LINEAGE_MISMATCH")
        if int(getattr(work, "operation_id", 0) or 0) != int(operation.id):
            raise ValueError("EXECUTION_WORK_OPERATION_LINEAGE_MISMATCH")

    context=context_override if context_override is not None else build(employee,project,task)
    system_prompt=system_prompt_override if system_prompt_override is not None else employee.system_instructions
    if model.provider_key == "codex":
        raise ValueError("Codex CLI must be invoked through Engineer → Codex Connector, not generic execute()")
    company=__import__("eason_one.services.company",fromlist=["get_company"]).get_company()
    requested_max = model.max_output_tokens if max_output_tokens_override is None else int(max_output_tokens_override)
    if requested_max <= 0:
        raise ValueError("max_output_tokens_override must be positive")
    # A formal Employee may be routed away from a stale persistent model binding.
    # The execution boundary owns provider compatibility, so cap the caller's
    # requested envelope to the selected provider instead of failing merely
    # because the persistent binding advertised a larger limit.
    effective_max = min(requested_max, int(model.max_output_tokens))
    if project is not None and __import__(
        "eason_one.services.project_contract", fromlist=["is_vnext_governed"]
    ).is_vnext_governed(project):
        composition["project_execution_terms_hash"] = __import__(
            "eason_one.services.project_contract", fromlist=["execution_terms_hash"]
        ).execution_terms_hash(project)
    if operation is not None or model_override is not None:
        composition["model_policy"] = __import__(
            "eason_one.services.execution_policy", fromlist=["policy_audit"]
        ).policy_audit(employee, model, operation)
    if response_schema is not None:
        schema_chars=len(json.dumps(response_schema,sort_keys=True,separators=(",",":"),ensure_ascii=False))
        composition["schema_overhead"]={"chars":schema_chars,"estimated_tokens":512+(schema_chars+3)//4}
    estimate=estimate_execution(model,system_prompt,context,user_request,effective_max,response_schema,include_request_fee=web_search)
    prompt_hash=hashlib.sha256(system_prompt.encode("utf-8")).hexdigest()
    context_hash=hashlib.sha256(context.encode("utf-8")).hexdigest()
    if work is not None:
        attempt_number=(AgentRun.query.filter_by(work_id=work.id,purpose=purpose).count()+1)
    else:
        attempt_number=1 if retry_of_run is None else int(getattr(retry_of_run,"attempt_number",1) or 1)+1
    run=AgentRun(
        employee_id=employee.id,project_id=getattr(project,"id",None),task_id=getattr(task,"id",None),meeting_id=getattr(meeting,"id",None),
        operation_id=getattr(operation,"id",None),work_id=getattr(work,"id",None),
        model_config_id=model.id,purpose=purpose,user_request=user_request,
        system_prompt_snapshot=system_prompt,context_snapshot=context,
        context_composition_json=composition or None,response_schema_snapshot_json=response_schema,
        prompt_version=prompt_version or f"{purpose.lower()}-unversioned",
        prompt_hash=prompt_hash,context_hash=context_hash,
        retry_of_run_id=getattr(retry_of_run,"id",None),attempt_number=attempt_number,
        role_snapshot=(employee.role_description or None),
        position_snapshot=(employee.position.name if getattr(employee,"position",None) else None),
        manager_snapshot=(employee.manager.name if getattr(employee,"manager",None) else None),
        instruction_version=prompt_version or f"{purpose.lower()}-unversioned",
        available_tools_snapshot_json=composition.get("available_tools"),
        used_tools_snapshot_json=composition.get("used_tools"),
        status="RUNNING",outcome="RUNNING",failure_stage=None,
        provider_key_snapshot=model.provider_key,model_name_snapshot=model.model_name,
        input_price_snapshot=model.input_price_per_million,output_price_snapshot=model.output_price_per_million,
        request_price_snapshot=getattr(model,"request_price_per_call",0) or 0,
        currency_snapshot=model.currency,currency=model.currency,
    )
    # Claim the owning orchestration step before the AgentRun exists so an
    # already-bound open step cannot accidentally buy a second provider call.
    operation_step = _operation_step_for_execution(
        operation, purpose, task, retry_of_run=retry_of_run
    )
    run.effective_max_output_tokens=effective_max
    db.session.add(run); db.session.flush()
    if operation_step is not None:
        operation_step.agent_run_id = run.id
    external=__import__("eason_one.services.external_effects",fromlist=["prepare","emit_execution_event"])
    effect=external.prepare(
        run,provider=model.provider_key,system_prompt=system_prompt,user_request=user_request,
        context=context,estimated_cost_twd=estimate.real_cost,effect_kind="MODEL_INFERENCE",
    )
    external.emit_execution_event(run,"EXECUTION_STARTED",payload={"attempt":attempt_number,"provider":model.provider_key,"model":model.model_name})
    if retry_of_run is not None:
        retry_of_run.replacement_run_id=run.id
        retry_of_run.resolution_status="RETRY_STARTED"
        retry_of_run.resolved_at=now()
        retry_of_run.resolution_note=f"Replacement Run #{run.id} started with {run.prompt_version}."
    db.session.commit()

    reservation=None
    try:
        # All checks below are deterministic and happen before the durable
        # provider-dispatch boundary.  Any failure is safe to retry/replan.
        provider_preflight(model, operation=operation, purpose=purpose, web_search=web_search)
        if model.provider_key!="mock":
            if model.input_price_per_million<=0 or model.output_price_per_million<=0:
                raise ValueError("Real provider prices must both be greater than zero")
            if model.currency!=company.currency:
                raise ValueError("Paid model currency must match company budget currency")
            if model.max_output_tokens<=0:
                raise ValueError("Maximum output tokens must be positive")
        elif (model.input_price_per_million or model.output_price_per_million) and model.currency!=company.currency:
            raise ValueError("Paid model currency must match company budget currency")
        ensure_budget(estimate.real_cost)
        work_vnext = bool(
            work is not None
            and operation is not None
            and (operation.memory_json or {}).get("runtime_semantics") in {"WORK_VNEXT", "WORK_CORE_V018"}
        )
        if work_vnext:
            # Work-first runtime: Project/Work authority owns provider spend.
            # Operation kernel reservations are a legacy execution mechanism and
            # must not decide whether an approved Work can run.
            __import__("eason_one.services.work_budget", fromlist=["ensure"]).ensure(
                work, estimate.real_cost
            )
        elif operation is not None:
            __import__("eason_one.services.operations",fromlist=["ensure_budget"]).ensure_budget(
                operation,estimate.real_cost,company_checked=True,stage=purpose,work=work)
        if operation is not None and model.provider_key != "mock":
            # Every real provider dispatch, including Work-first vNext, enters
            # the same durable reservation ledger. For vNext the reservation
            # enforces Company -> Project -> Work authority atomically and does
            # not resurrect legacy Operation/stage budget authority.
            reservation=__import__(
                "eason_one.services.operation_kernel",fromlist=["reserve_provider_call"]
            ).reserve_provider_call(
                operation,stage=purpose,estimate_twd=estimate.real_cost,
                idempotency_key=f"agent-run:{run.id}",agent_run_id=run.id,
            )
            effect=db.session.get(
                __import__("eason_one.models",fromlist=["ExternalEffectAttempt"]).ExternalEffectAttempt,
                effect.id,
            )
            effect.cost_reservation_id=reservation.id
            effect.state="RESERVED"
            db.session.commit()
    except Exception as exc:
        run.status="FAILED"
        is_founder_budget_extension = (
            exc.__class__.__name__ in {"OperationBudgetRequired", "WorkBudgetRequired"}
            and hasattr(exc, "additional")
        )
        run.failure_reason=(
            "FOUNDER_BUDGET_EXTENSION_REQUIRED"
            if is_founder_budget_extension
            else "AUTHORITY_BLOCKED"
            if "budget" in str(exc).lower() or "authority" in str(exc).lower()
            else "PROVIDER_PREFLIGHT_FAILED"
        )
        if is_founder_budget_extension:
            composition = dict(run.context_composition_json or {})
            composition["authority_failure"] = {
                "kind": "BUDGET_EXTENSION_REQUIRED",
                "approved_twd": str(getattr(exc, "approved", "0")),
                "spent_twd": str(getattr(exc, "spent", "0")),
                "additional_twd": str(getattr(exc, "additional", "0")),
                "scope": str(getattr(exc, "scope", "MISSION")),
            }
            run.context_composition_json = composition
        run.outcome="FAILED_SAFE"
        run.failure_stage="PRE_DISPATCH"
        run.error_text=str(exc)
        run.finished_at=now()
        external.mark_pre_dispatch_failure(effect,exc)
        if retry_of_run is not None:
            retry_of_run.resolution_status="REPLACED_FAILED_SAFE"
            retry_of_run.resolution_note=f"Replacement Run #{run.id} failed safely before provider dispatch."
        external.emit_execution_event(run,"EXECUTION_FAILED_SAFE",payload={"error":str(exc),"failure_reason":run.failure_reason})
        db.session.commit()
        return run

    provider=get_provider(model.provider_key)
    external.mark_dispatching(effect)
    # This commit is intentional: after it succeeds, a process crash is no
    # longer safe to interpret as "the provider was never called".
    db.session.commit()
    try:
        result=(provider.complete(
            model,system_prompt,user_request,context,effective_max,response_schema,
            tool_mode="web_search" if web_search else None,
        ) if response_schema is not None else provider.complete(
            model,system_prompt,user_request,context,effective_max,
            tool_mode="web_search" if web_search else None,
        ))
    except Exception as exc:
        failure_class, rejected_status = _provider_failure_classification(exc)
        if failure_class is not None:
            transient = failure_class == "TRANSIENT"
            retry_after = retry_after_seconds(exc) if transient else None
            run.status="FAILED"
            run.failure_reason=("PROVIDER_TRANSIENT_REJECTED" if transient else "PROVIDER_REQUEST_REJECTED")
            run.outcome="FAILED_KNOWN"
            run.failure_stage="POST_DISPATCH"
            run.error_text=str(exc)
            run.finished_at=now()
            run.provider_request_id=getattr(exc,"request_id",None)
            observed=dict(run.context_composition_json or {})
            observed["provider_rejection"]={
                "http_status":rejected_status,
                "definitive":True,
                "retryable":transient,
                "retry_after_seconds":retry_after,
                "provider":model.provider_key,
            }
            run.context_composition_json=observed
            external.mark_rejected(effect,exc)
            if retry_of_run is not None:
                retry_of_run.resolution_status=("REPLACED_TRANSIENT_REJECTED" if transient else "REPLACED_REJECTED")
                retry_of_run.resolution_note=(
                    f"Replacement Run #{run.id} received retryable provider HTTP {rejected_status}."
                    if transient else
                    f"Replacement Run #{run.id} was definitively rejected by provider HTTP {rejected_status}."
                )
            # A non-success HTTP response is observed rejection, not an unknown
            # post-dispatch effect. Eason One owns retries (SDK retries are off),
            # so release this attempt's reservation before any bounded replay.
            db.session.commit()
            if reservation is not None and reservation.status in {"RESERVED", "AMBIGUOUS"}:
                __import__("eason_one.services.operation_kernel",fromlist=["resolve_reservation"]).resolve_reservation(
                    reservation,status="RELEASED",
                    note=(f"Provider returned retryable HTTP {rejected_status}; no accepted model completion was returned."
                          if transient else
                          f"Provider definitively rejected request with HTTP {rejected_status}; no model completion was returned.")
                )
            external.emit_execution_event(
                run,"EXECUTION_FAILED_KNOWN",
                payload={"error":str(exc),"failure_reason":run.failure_reason,"http_status":rejected_status,"retryable":transient,"retry_after_seconds":retry_after},
            )
            db.session.commit()
            return run

        run.status="FAILED"
        run.failure_reason="PROVIDER_DISPATCH_AMBIGUOUS"
        run.outcome="FAILED_AMBIGUOUS"
        run.failure_stage="POST_DISPATCH"
        run.error_text=str(exc)
        run.finished_at=now()
        external.mark_ambiguous(effect,exc)
        if reservation is not None and reservation.status == "RESERVED":
            try:
                __import__("eason_one.services.operation_kernel",fromlist=["resolve_reservation"]).resolve_reservation(
                    reservation,status="AMBIGUOUS",note="Provider invocation raised after the durable dispatch boundary."
                )
            except Exception:
                db.session.rollback()
                run=db.session.get(AgentRun,run.id)
                effect=db.session.get(__import__("eason_one.models",fromlist=["ExternalEffectAttempt"]).ExternalEffectAttempt,effect.id)
                run.status="FAILED"; run.failure_reason="PROVIDER_DISPATCH_AMBIGUOUS"; run.outcome="FAILED_AMBIGUOUS"; run.failure_stage="POST_DISPATCH"; run.error_text=str(exc); run.finished_at=now()
                external.mark_ambiguous(effect,exc)
        if retry_of_run is not None:
            retry_of_run.resolution_status="REPLACED_AMBIGUOUS"
            retry_of_run.resolution_note=f"Replacement Run #{run.id} became ambiguous after provider dispatch."
        external.emit_execution_event(run,"EXECUTION_FAILED_AMBIGUOUS",payload={"error":str(exc)})
        db.session.commit()
        return run

    external.mark_response(effect,result)
    run.raw_output=result.text
    run.output_hash=hashlib.sha256((result.text or "").encode("utf-8")).hexdigest()
    run.input_tokens=result.input_tokens; run.output_tokens=result.output_tokens
    run.cache_creation_input_tokens=result.cache_creation_input_tokens
    run.cache_read_input_tokens=result.cache_read_input_tokens
    run.provider_request_id=result.request_id; run.provider_response_id=result.response_id
    run.provider_stop_reason=getattr(result,"stop_reason",None)
    observed_sources=list(getattr(result,"sources",None) or [])
    provider_usage=dict(getattr(result,"provider_usage",None) or {})
    used_tools=list(getattr(result,"used_tools",None) or [])
    observed=dict(run.context_composition_json or {})
    observed["provider_response_checkpoint"]={
        "schema":"PROVIDER_RESPONSE_CHECKPOINT_V1",
        "status":str(getattr(result,"status","") or ""),
        "incomplete_reason":canonical_incomplete_reason(getattr(result,"incomplete_reason",None)),
        "refusal":str(getattr(result,"refusal","") or "") or None,
        "include_request_fee":bool(web_search or model.provider_key == "perplexity"),
    }
    if observed_sources or provider_usage:
        if observed_sources:
            observed["provider_sources"] = observed_sources
        if provider_usage:
            observed["provider_usage"] = provider_usage
    run.context_composition_json=observed
    if used_tools:
        run.used_tools_snapshot_json=used_tools
    # This is the first durable evidence that the provider returned a response.
    # Commit the raw body, usage, response ids, and response semantics together
    # before cost/status settlement.  A crash after this point can finish the
    # exact response locally; it must never buy a replacement provider call.
    db.session.commit()
    provider_name=model.provider_key
    if result.cache_creation_input_tokens or result.cache_read_input_tokens:
        run.real_cost=None
        run.status="FAILED"
        run.failure_reason="COST_RECONCILIATION_REQUIRED"
        run.outcome="FAILED_AMBIGUOUS"
        run.failure_stage="SETTLEMENT"
        run.error_text=(f"{provider_name} returned unexpected prompt-cache usage; exact cache-cost reconciliation is not enabled")
        external.mark_ambiguous(effect,run.error_text)
    else:
        run.real_cost=calculate(model,result.input_tokens,result.output_tokens,include_request_fee=web_search)
        if result.refusal:
            run.status="FAILED"; run.failure_reason="REFUSAL"; run.outcome="FAILED_KNOWN"; run.failure_stage="RESPONSE"
            run.error_text=f"{provider_name} refusal: {result.refusal}"
        elif result.status!="completed":
            incomplete_reason=canonical_incomplete_reason(result.incomplete_reason)
            detail=f": {incomplete_reason}" if incomplete_reason else ""
            run.status="FAILED"
            run.failure_reason=("OUTPUT_TRUNCATED" if incomplete_reason == "max_output_tokens" else "PROVIDER_INCOMPLETE")
            run.outcome="FAILED_KNOWN"; run.failure_stage="RESPONSE"
            run.error_text=f"{run.failure_reason}: {provider_name} response {result.status}{detail}"
        else:
            run.status="SUCCEEDED"; run.outcome="SUCCEEDED"; run.failure_stage=None
        record(run)
        external.mark_persisted(effect)
    run.finished_at=now()
    if reservation is not None:
        kernel=__import__("eason_one.services.operation_kernel",fromlist=["resolve_reservation"])
        if run.real_cost is None:
            kernel.resolve_reservation(reservation,status="AMBIGUOUS",note="Provider returned usage that cannot yet be reconciled exactly.")
        else:
            kernel.resolve_reservation(reservation,actual_twd=run.real_cost,status="CONSUMED",note=f"Agent Run #{run.id} finished with exact token usage.")
    if run.real_cost is not None:
        external.mark_settled(effect,actual_cost_twd=run.real_cost)
    if retry_of_run is not None:
        retry_of_run.resolution_status=("REPLACED_SUCCEEDED" if run.status=="SUCCEEDED" else "REPLACED_FAILED")
        retry_of_run.resolution_note=f"Replacement Run #{run.id} finished with status {run.status}."
    external.emit_execution_event(
        run,"EXECUTION_SUCCEEDED" if run.status=="SUCCEEDED" else "EXECUTION_FAILED_KNOWN",
        payload={
            "failure_reason":run.failure_reason,
            "used_tools":run.used_tools_snapshot_json or [],
            "provider_source_count":len((run.context_composition_json or {}).get("provider_sources") or []),
        },
    )
    db.session.commit()
    if run.status=="FAILED":
        return run
    if postprocess:
        try:
            postprocess(run)
        except Exception as exc:
            run.status="FAILED"
            run.failure_reason="POSTPROCESS_FAILED"
            run.outcome="FAILED_KNOWN"
            run.failure_stage="POSTPROCESS"
            run.error_text=f"Downstream processing failed: {exc}"
            db.session.commit()
            return run
    return run
