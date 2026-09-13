import json
from ..extensions import db
from ..models import Proposal, AgentRun, now
from ..schemas import TASK_EXECUTION_SCHEMA
from .execution import execute
from .brain import validate_basis_ids
from .context import build_with_composition
from .execution_policy import execution_constraints, select_execution_model, select_research_model

FIELDS={"result_summary","knowledge_proposals"}
CANDIDATE_FIELDS={"kind","title","content","source_ref","rationale","basis_knowledge_ids"}


def _missing_evidence_run(task, employee, model, context, composition, *, retry_of_run=None):
    message = (
        "BLOCKED_MISSING_EVIDENCE: the approved Task requires existing Eason One "
        "evidence, but deterministic retrieval found no relevant persisted source. "
        "No Provider call was made."
    )
    run = AgentRun(
        employee_id=employee.id, project_id=task.project_id, task_id=task.id,
        work_id=getattr(task,"work_id",None),
        operation_id=getattr(task, "operation_id", None), model_config_id=model.id,
        purpose="TASK_EXECUTION", user_request=task.objective,
        system_prompt_snapshot=employee.system_instructions, context_snapshot=context,
        context_composition_json=composition, prompt_version="task-evidence-preflight-v1",
        provider_key_snapshot=model.provider_key, model_name_snapshot=model.model_name,
        input_price_snapshot=model.input_price_per_million,
        output_price_snapshot=model.output_price_per_million,
        currency_snapshot=model.currency, currency=model.currency,
        effective_max_output_tokens=0, status="FAILED", failure_reason="MISSING_EVIDENCE",
        retry_of_run_id=getattr(retry_of_run,"id",None),
        attempt_number=(int(getattr(retry_of_run,"attempt_number",0) or 0)+1 if retry_of_run else 1),
        role_snapshot=employee.role_description,
        position_snapshot=(employee.position.name if employee.position else None),
        manager_snapshot=(employee.manager.name if employee.manager else None),
        instruction_version="task-evidence-preflight-v1",
        outcome="FAILED_SAFE",failure_stage="PRE_DISPATCH",
        structured_validation_status="BLOCKED",
        structured_validation_errors_json=[message], error_text=message,
        real_cost=0, finished_at=now(),
    )
    db.session.add(run)
    db.session.flush()
    __import__("eason_one.services.external_effects",fromlist=["emit_execution_event"]).emit_execution_event(
        run,"EXECUTION_FAILED_SAFE",payload={"failure_reason":"MISSING_EVIDENCE"}
    )
    db.session.commit()
    return run



def _research_tool_unavailable_run(task, employee, model, context, composition, *, retry_of_run=None):
    message = (
        "RESEARCH_TOOL_UNAVAILABLE: this Work is accountable for live external research, "
        "but no configured bounded research provider is available with governed request pricing. "
        "No Provider call was made."
    )
    run = AgentRun(
        employee_id=employee.id, project_id=task.project_id, task_id=task.id,
        work_id=getattr(task,"work_id",None), operation_id=getattr(task,"operation_id",None),
        model_config_id=model.id, purpose="TASK_EXECUTION", user_request=task.objective,
        system_prompt_snapshot=employee.system_instructions, context_snapshot=context,
        context_composition_json=composition, prompt_version="live-research-preflight-v1",
        provider_key_snapshot=model.provider_key, model_name_snapshot=model.model_name,
        input_price_snapshot=model.input_price_per_million,
        output_price_snapshot=model.output_price_per_million,
        request_price_snapshot=getattr(model,"request_price_per_call",0) or 0,
        currency_snapshot=model.currency, currency=model.currency,
        effective_max_output_tokens=0, status="FAILED", failure_reason="RESEARCH_TOOL_UNAVAILABLE",
        retry_of_run_id=getattr(retry_of_run,"id",None),
        attempt_number=(int(getattr(retry_of_run,"attempt_number",0) or 0)+1 if retry_of_run else 1),
        role_snapshot=employee.role_description,
        position_snapshot=(employee.position.name if employee.position else None),
        manager_snapshot=(employee.manager.name if employee.manager else None),
        instruction_version="live-research-preflight-v1", outcome="FAILED_SAFE",
        failure_stage="PRE_DISPATCH", structured_validation_status="BLOCKED",
        structured_validation_errors_json=[message], error_text=message, real_cost=0, finished_at=now(),
    )
    db.session.add(run); db.session.flush()
    __import__("eason_one.services.external_effects",fromlist=["emit_execution_event"]).emit_execution_event(
        run,"EXECUTION_FAILED_SAFE",payload={"failure_reason":"RESEARCH_TOOL_UNAVAILABLE"}
    )
    db.session.commit()
    return run

def _research_synthesis_input_missing_run(task, employee, model, context, composition, *, retry_of_run=None):
    message = (
        "RESEARCH_SYNTHESIS_INPUT_MISSING: Research Director synthesis requires accepted upstream "
        "Research Department Work/Artifact lineage. No Provider call was made."
    )
    run = AgentRun(
        employee_id=employee.id, project_id=task.project_id, task_id=task.id,
        work_id=getattr(task,"work_id",None), operation_id=getattr(task,"operation_id",None),
        model_config_id=model.id, purpose="TASK_EXECUTION", user_request=task.objective,
        system_prompt_snapshot=employee.system_instructions, context_snapshot=context,
        context_composition_json=composition, prompt_version="research-department-synthesis-preflight-v1",
        provider_key_snapshot=model.provider_key, model_name_snapshot=model.model_name,
        input_price_snapshot=model.input_price_per_million, output_price_snapshot=model.output_price_per_million,
        currency_snapshot=model.currency, currency=model.currency, effective_max_output_tokens=0,
        status="FAILED", failure_reason="RESEARCH_SYNTHESIS_INPUT_MISSING",
        retry_of_run_id=getattr(retry_of_run,"id",None),
        attempt_number=(int(getattr(retry_of_run,"attempt_number",0) or 0)+1 if retry_of_run else 1),
        role_snapshot=employee.role_description,
        position_snapshot=(employee.position.name if employee.position else None),
        manager_snapshot=(employee.manager.name if employee.manager else None),
        instruction_version="research-department-synthesis-preflight-v1", outcome="FAILED_SAFE",
        failure_stage="PRE_DISPATCH", structured_validation_status="BLOCKED",
        structured_validation_errors_json=[message], error_text=message, real_cost=0, finished_at=now(),
    )
    db.session.add(run); db.session.flush()
    __import__("eason_one.services.external_effects",fromlist=["emit_execution_event"]).emit_execution_event(
        run,"EXECUTION_FAILED_SAFE",payload={"failure_reason":"RESEARCH_SYNTHESIS_INPUT_MISSING"}
    )
    db.session.commit()
    return run


def _persist_proposals(task, employee, run, payload):
    for item in payload["knowledge_proposals"]:
        try:
            if not isinstance(item,dict) or set(item)!=CANDIDATE_FIELDS: raise ValueError()
            if item["kind"] not in {"FACT","HYPOTHESIS","EVIDENCE","DECISION"}: raise ValueError()
            if not item["title"].strip() or not item["content"].strip(): raise ValueError()
            if item["kind"]=="EVIDENCE" and not item["source_ref"]: raise ValueError()
            execution_mode = str((run.context_composition_json or {}).get("execution_mode") or "")
            if execution_mode == "MODEL_RESEARCH_PERSPECTIVE" and item["kind"] == "EVIDENCE":
                # A dedicated Claude/Gemini (or non-search) Researcher may offer
                # analysis/hypotheses, but cannot mint external evidence merely
                # because its Employee title says Researcher.
                raise ValueError()
            observed_sources = {
                str(row.get("url") or "").strip()
                for row in ((run.context_composition_json or {}).get("provider_sources") or [])
                if isinstance(row,dict) and str(row.get("url") or "").strip()
            }
            if item["kind"]=="EVIDENCE" and observed_sources and str(item["source_ref"]).strip() not in observed_sources:
                # A model-authored URL is not provider-observed evidence.
                raise ValueError()
            if item["kind"]=="DECISION" and not item["rationale"]: raise ValueError()
            if item["basis_knowledge_ids"]: validate_basis_ids(item["basis_knowledge_ids"],task.project_id)
            db.session.add(Proposal(project_id=task.project_id,agent_run_id=run.id,proposed_by_employee_id=employee.id,payload_json=item,status="PENDING"))
        except (ValueError,TypeError,KeyError):
            continue


def _validate_payload(payload):
    # Codex adds a governed audit envelope beside the standard Task result.
    # General model Tasks must still match the original schema exactly.
    keys=set(payload)
    if not FIELDS.issubset(keys) or keys-FIELDS not in (set(), {"codex"}) or not isinstance(payload["result_summary"],str) or not payload["result_summary"].strip():
        raise ValueError("Invalid Task result")
    if not isinstance(payload["knowledge_proposals"],list) or len(payload["knowledge_proposals"])>8:
        raise ValueError("Invalid knowledge proposal list")
    return payload


def materialize_successful_run(task, run, *, recovery=False):
    """Finish deterministic Task postprocessing without buying another model call.

    execution.execute() durably commits provider/effect/cost truth before this
    module validates TASK_EXECUTION_SCHEMA and persists Task/Proposal projection.
    A process can therefore die with a fully paid SUCCEEDED AgentRun whose
    parsed_output_json is still empty.  Replaying the provider in that state is
    both wasteful and incorrect: the immutable raw response is already Company
    Truth.  This helper re-runs only deterministic local postprocessing.
    """
    if run is None or run.status != "SUCCEEDED":
        return run
    if int(getattr(run, "task_id", 0) or 0) != int(getattr(task, "id", 0) or 0):
        raise ValueError("TASK_POSTPROCESS_RUN_LINEAGE_MISMATCH")
    if getattr(task, "work_id", None) and int(getattr(run, "work_id", 0) or 0) != int(task.work_id):
        raise ValueError("TASK_POSTPROCESS_WORK_LINEAGE_MISMATCH")
    if run.parsed_output_json:
        return run

    employee = db.session.get(__import__("eason_one.models", fromlist=["Employee"]).Employee, run.employee_id)
    if employee is None:
        raise ValueError("TASK_POSTPROCESS_EMPLOYEE_MISSING")

    composition = dict(run.context_composition_json or {})
    execution_mode = str(composition.get("execution_mode") or "").upper()
    if execution_mode == "LIVE_WEB_RESEARCH" and not composition.get("provider_sources"):
        run.status = "FAILED"
        run.failure_reason = "RESEARCH_SOURCE_EVIDENCE_MISSING"
        run.outcome = "FAILED_KNOWN"
        run.failure_stage = "POSTPROCESS"
        run.error_text = (
            "Live research returned no provider-observed source evidence; Eason One will not "
            "treat untraceable model output as Research."
        )
        run.structured_validation_status = "FAILED"
        run.structured_validation_errors_json = [run.error_text]
        if recovery:
            composition["durable_postprocess_recovery"] = {
                "provider_replayed": False,
                "basis": "PAID_AGENT_RUN_RAW_OUTPUT",
                "result": "RESEARCH_SOURCE_EVIDENCE_MISSING",
            }
            run.context_composition_json = composition
        db.session.commit()
        return run

    try:
        payload = _validate_payload(json.loads(run.raw_output or "{}"))
        run.parsed_output_json = payload
        run.structured_validation_status = "PASSED"
        run.structured_validation_errors_json = []
        task.result_summary = payload["result_summary"]
        _persist_proposals(task, employee, run, payload)
        if recovery:
            composition = dict(run.context_composition_json or {})
            composition["durable_postprocess_recovery"] = {
                "provider_replayed": False,
                "basis": "PAID_AGENT_RUN_RAW_OUTPUT",
                "result": "MATERIALIZED",
            }
            run.context_composition_json = composition
            __import__(
                "eason_one.services.external_effects", fromlist=["emit_execution_event"]
            ).emit_execution_event(
                run,
                "EXECUTION_POSTPROCESS_RECOVERED",
                payload={"provider_replayed": False, "task_id": task.id},
            )
        db.session.commit()
        return run
    except Exception as exc:
        db.session.rollback()
        persisted = db.session.get(type(run), run.id)
        persisted.status = "FAILED"
        persisted.outcome = "FAILED_KNOWN"
        persisted.failure_reason = "STRUCTURED_OUTPUT_INVALID"
        persisted.failure_stage = "POSTPROCESS"
        persisted.structured_validation_status = "FAILED"
        persisted.structured_validation_errors_json = [str(exc)]
        persisted.error_text = f"Task result validation failed: {exc}"
        if recovery:
            composition = dict(persisted.context_composition_json or {})
            composition["durable_postprocess_recovery"] = {
                "provider_replayed": False,
                "basis": "PAID_AGENT_RUN_RAW_OUTPUT",
                "result": "STRUCTURED_OUTPUT_INVALID",
            }
            persisted.context_composition_json = composition
        db.session.commit()
        return persisted


def run_task(task, *, retry_of_run=None, model_override=None):
    project = getattr(task, "project", None)
    if project is not None and __import__(
        "eason_one.services.work_runtime", fromlist=["project_is_terminal"]
    ).project_is_terminal(project):
        raise ValueError(
            f"PROJECT_TERMINAL_TASK_EXECUTION_FORBIDDEN:{str(project.status or '').upper()}"
        )
    if task.status not in {"ASSIGNED","WORKING"}: raise ValueError("Task is not executable")
    employee=task.assigned_employee
    operation=getattr(task,"operation",None)

    # Engineer is accountable for the bounded Codex Job Spec and its evidence.
    # Codex is a local tool, never a silent MockProvider or a separate Employee.
    if employee and employee.slug=="engineer":
        run=__import__(
            "eason_one.services.codex_connector",fromlist=["run_codex_task"]
        ).run_codex_task(task)
        if run.status!="SUCCEEDED" or not run.parsed_output_json:
            return run
        try:
            payload=_validate_payload(dict(run.parsed_output_json))
            task.result_summary=payload["result_summary"]
            _persist_proposals(task,employee,run,payload)
            db.session.commit()
            return run
        except Exception as exc:
            db.session.rollback()
            persisted=db.session.get(type(run),run.id)
            persisted.status="FAILED"
            persisted.failure_reason="CODEX_RESULT_INVALID"
            persisted.structured_validation_status="FAILED"
            persisted.structured_validation_errors_json=[str(exc)]
            persisted.error_text=f"Codex Task result validation failed: {exc}"
            db.session.commit()
            return persisted

    if not employee or not employee.current_model:
        raise ValueError("Employee has no execution model configured")

    # Persistent Employee identity and its default binding are not the same as
    # one execution decision.  Formal work may legitimately override a mock or
    # otherwise disallowed default binding through the deterministic execution
    # policy.  The selected model is validated again by execution.provider_preflight
    # immediately before the governed provider call.
    context, composition = build_with_composition(employee, task.project, task)
    constraints = execution_constraints(operation)
    work = __import__("eason_one.services.work_runtime",fromlist=["work_for_task"]).work_for_task(task)
    capability = None
    if work is not None:
        capability = __import__(
            "eason_one.services.team_formation",fromlist=["infer_primary_capability"]
        ).infer_primary_capability(work)[0]

    # V1.7: canonical accepted outcomes now reach the Employee who is actually
    # doing the next governed Work, not only CEO planning/staffing surfaces.
    # The retrieval function is fail-closed to CANONICAL_WORK_ACCEPTANCE and
    # persists exact record ids in the Run composition for later causal audit.
    learning_text, learning_meta = __import__(
        "eason_one.services.employee_memory", fromlist=["execution_learning_context"]
    ).execution_learning_context(
        employee, task.project, task, purpose="TASK_EXECUTION", capability=capability
    )
    composition = dict(composition or {})
    learning_meta = dict(learning_meta or {})
    composition["persistent_employee_memory"] = learning_meta
    if learning_text:
        context = (context or "") + "\n\n" + learning_text

    research_department = __import__(
        "eason_one.services.research_department",
        fromlist=["provider_family", "is_synthesis_text"],
    )
    dedicated_provider = research_department.provider_family(employee)
    department_synthesis = bool(
        capability == "RESEARCH" and employee.slug == "research-director"
        and research_department.is_synthesis_text(task.title, task.objective, task.required_output)
    )
    wants_external_research = bool(capability == "RESEARCH" and not constraints.get("existing_evidence_only"))
    perspective_research = bool(
        wants_external_research and dedicated_provider in {"anthropic", "gemini"}
    )
    live_research = bool(wants_external_research and not department_synthesis and not perspective_research)

    if department_synthesis:
        selected_model = model_override or select_execution_model(employee, operation, "TASK_EXECUTION")
        composition = dict(composition or {})
        composition["execution_mode"] = "RESEARCH_DEPARTMENT_SYNTHESIS"
        composition["available_tools"] = []
        lineage = list(((composition.get("operation_context") or {}).get("handoff_lineage") or []))
        if not lineage:
            return _research_synthesis_input_missing_run(
                task, employee, selected_model, context, composition, retry_of_run=retry_of_run
            )
        prompt=(
            employee.system_instructions
            + "\nRESEARCH_DEPARTMENT_SYNTHESIS_V1\n"
              "Synthesize only the accepted upstream Research Department outputs and their persisted source lineage. "
              "Compare agreement, disagreement, evidence strength, uncertainty, and model-only reasoning. "
              "Do not add new external facts or pretend an unsourced specialist opinion is provider-observed evidence. "
              "Return one accountable department conclusion in the standard Task schema."
        )
    elif perspective_research:
        selected_model = model_override or select_execution_model(employee, operation, "TASK_EXECUTION")
        composition = dict(composition or {})
        composition["execution_mode"] = "MODEL_RESEARCH_PERSPECTIVE"
        composition["available_tools"] = []
        composition["research_specialization"] = {
            "provider_family": dedicated_provider,
            "employee_id": employee.id,
            "employee_slug": employee.slug,
            "source_status": "MODEL_REASONING_NOT_LIVE_WEB_EVIDENCE",
        }
        prompt=(
            employee.system_instructions
            + "\nMODEL_RESEARCH_PERSPECTIVE_V1\n"
              "Give an independent model-specific research perspective on the approved question. "
              "You do not have provider-observed live web evidence in this execution mode. Distinguish inference, prior knowledge, "
              "assumptions, and uncertainty; never fabricate citations or external EVIDENCE proposals. "
              "Return the structured Task result so the Research Director can compare it with sourced branches."
        )
    elif live_research:
        selected_model = model_override
        allowed = ({"mock"} if __import__("flask",fromlist=["current_app"]).current_app.config.get("TESTING") else {"openai", "perplexity"})
        if selected_model is not None and selected_model.provider_key not in allowed:
            selected_model = None
        selected_model = selected_model or select_research_model(employee, operation)
        # A model-specialized OpenAI/Perplexity Researcher keeps its Employee
        # identity even when its configured core lacks a governed search tool.
        # Fall back to an explicitly unsourced model perspective rather than
        # silently executing through a different provider family.
        if selected_model is None and dedicated_provider in {"openai", "perplexity"}:
            selected_model = model_override or select_execution_model(employee, operation, "TASK_EXECUTION")
            live_research = False
            perspective_research = True
            composition = dict(composition or {})
            composition["execution_mode"] = "MODEL_RESEARCH_PERSPECTIVE"
            composition["available_tools"] = []
            composition["research_specialization"] = {
                "provider_family": dedicated_provider,
                "employee_id": employee.id,
                "employee_slug": employee.slug,
                "source_status": "MODEL_REASONING_NOT_LIVE_WEB_EVIDENCE",
            }
            prompt=(
                employee.system_instructions
                + "\nMODEL_RESEARCH_PERSPECTIVE_V1\n"
                  "No governed live-search configuration is available for this provider-family execution. "
                  "Give an independent model perspective, label assumptions and uncertainty, and never fabricate citations or EVIDENCE. "
                  "Return the standard Task result for later Research Director synthesis."
            )
        else:
            if selected_model is None:
                audit_model = employee.current_model
                composition = dict(composition or {})
                composition["execution_mode"] = "LIVE_WEB_RESEARCH"
                composition["available_tools"] = ["WEB_SEARCH"]
                return _research_tool_unavailable_run(
                    task, employee, audit_model, context, composition, retry_of_run=retry_of_run
                )
            composition = dict(composition or {})
            composition["execution_mode"] = "LIVE_WEB_RESEARCH"
            composition["available_tools"] = ["WEB_SEARCH"]
            if dedicated_provider:
                composition["research_specialization"] = {
                    "provider_family": dedicated_provider,
                    "employee_id": employee.id,
                    "employee_slug": employee.slug,
                    "source_status": "PROVIDER_OBSERVED_WEB_EVIDENCE_REQUIRED",
                }
            prompt=(
                employee.system_instructions
                + "\nLIVE_RESEARCH_TASK_V1\n"
                  "Use the provider's live web-search evidence for external factual claims. "
                  "Do not invent citations or claim a source the provider did not observe. "
                  "If the available sources do not support an answer, state the uncertainty. "
                  "Return the structured Task result; runtime persists provider sources separately from your prose."
            )
    else:
        selected_model = model_override or select_execution_model(employee, operation, "TASK_EXECUTION")
        prompt=employee.system_instructions+"\nTASK_EXECUTION\nReturn the structured Task result. Do not claim web research or fabricate citations. Keep the result concise and source-grounded."

    if constraints.get("existing_evidence_only") and not (
        (composition.get("evidence_retrieval") or {}).get("relevant_items")
    ):
        return _missing_evidence_run(task, employee, selected_model, context, composition, retry_of_run=retry_of_run)
    # ModelConfig is the configured output authority.  Do not silently impose a
    # smaller product ceiling here: live #21 proved that old 1,600/2,400 caps can
    # turn otherwise-valid paid work into OUTPUT_TRUNCATED failures.
    output_cap = int(selected_model.max_output_tokens)
    prompt_version = (
        "research-department-synthesis-v1" if department_synthesis else
        "model-research-perspective-v1" if perspective_research else
        "live-research-task-v1" if live_research else "task-execution-v1"
    )
    if retry_of_run is not None and retry_of_run.failure_reason == "OUTPUT_TRUNCATED":
        # A full-envelope truncation cannot be fixed by pretending the model has
        # more authority than ModelConfig.  The one bounded retry instead asks
        # for the same governed schema in a denser form while preserving source
        # grounding and every material conclusion.
        prompt += (
            "\nTASK_OUTPUT_TRUNCATION_RECOVERY_V1\n"
            "The previous attempt reached the configured output envelope. Return the same required JSON schema "
            "more compactly. Preserve every material conclusion and source-grounded fact, remove repeated prose, "
            "keep result_summary decision-ready, and include only knowledge proposals that materially change the result."
        )
        prompt_version += "-compact-retry-v1"
    run=execute(employee,"TASK_EXECUTION",task.objective,task.project,task,
      context_override=context,context_composition=composition,
      system_prompt_override=prompt,response_schema=TASK_EXECUTION_SCHEMA,
      operation=operation,model_override=selected_model,work=work,
      retry_of_run=retry_of_run,
      prompt_version=prompt_version,
      max_output_tokens_override=output_cap)
    if run.status!="SUCCEEDED": return run
    return materialize_successful_run(task, run)
