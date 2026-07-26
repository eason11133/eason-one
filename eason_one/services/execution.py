from ..extensions import db
import json
from ..models import AgentRun, now
from ..providers import get_provider
from .context import build
from .costs import ensure_budget, calculate, record, estimate_execution
def execute(employee, purpose, user_request, project=None, task=None, context_override=None, postprocess=None, system_prompt_override=None,response_schema=None,meeting=None,max_output_tokens_override=None,context_composition=None,operation=None):
    if not employee.current_model: raise ValueError("Employee has no model")
    if employee.current_model.archived: raise ValueError("Employee's current ModelConfig is archived")
    if not employee.current_model.active: raise ValueError("Employee's current ModelConfig is inactive")
    context=context_override if context_override is not None else build(employee,project,task)
    model=employee.current_model
    system_prompt=system_prompt_override if system_prompt_override is not None else employee.system_instructions
    company=__import__("eason_one.services.company",fromlist=["get_company"]).get_company()
    if model.provider_key!="mock":
        if model.input_price_per_million<=0 or model.output_price_per_million<=0: raise ValueError("Real provider prices must both be greater than zero")
        if model.currency!=company.currency: raise ValueError("Paid model currency must match company budget currency")
        if model.max_output_tokens<=0: raise ValueError("Maximum output tokens must be positive")
    elif (model.input_price_per_million or model.output_price_per_million) and model.currency!=company.currency:
        raise ValueError("Paid model currency must match company budget currency")
    effective_max=model.max_output_tokens if max_output_tokens_override is None else int(max_output_tokens_override)
    if effective_max<=0 or effective_max>model.max_output_tokens:
        raise ValueError("max_output_tokens_override must be positive and not exceed ModelConfig maximum")
    estimate=estimate_execution(model,system_prompt,context,user_request,effective_max,response_schema)
    ensure_budget(estimate.real_cost)
    operation=operation or getattr(task,"operation",None) or getattr(meeting,"operation",None)
    if operation is not None:
        __import__("eason_one.services.operations",fromlist=["ensure_budget"]).ensure_budget(
          operation,estimate.real_cost,company_checked=True)
    composition=dict(context_composition or {})
    if response_schema is not None:
        schema_chars=len(json.dumps(response_schema,sort_keys=True,separators=(",",":"),ensure_ascii=False))
        composition["schema_overhead"]={"chars":schema_chars,"estimated_tokens":512+(schema_chars+3)//4}
    run=AgentRun(employee_id=employee.id,project_id=getattr(project,"id",None),task_id=getattr(task,"id",None),meeting_id=getattr(meeting,"id",None),
        operation_id=getattr(operation,"id",None),
        model_config_id=model.id,purpose=purpose,user_request=user_request,
        system_prompt_snapshot=system_prompt,context_snapshot=context,
        context_composition_json=composition or None,response_schema_snapshot_json=response_schema,
        provider_key_snapshot=model.provider_key,model_name_snapshot=model.model_name,
        input_price_snapshot=model.input_price_per_million,output_price_snapshot=model.output_price_per_million,
        currency_snapshot=model.currency,currency=model.currency)
    run.effective_max_output_tokens=effective_max
    db.session.add(run); db.session.commit()
    try:
        provider=get_provider(model.provider_key)
        result=(provider.complete(model,system_prompt,user_request,context,effective_max,response_schema)
          if response_schema is not None else provider.complete(model,system_prompt,user_request,context,effective_max))
        run.raw_output=result.text; run.input_tokens=result.input_tokens; run.output_tokens=result.output_tokens
        run.cache_creation_input_tokens=result.cache_creation_input_tokens
        run.cache_read_input_tokens=result.cache_read_input_tokens
        run.provider_request_id=result.request_id; run.provider_response_id=result.response_id
        run.provider_stop_reason=getattr(result,"stop_reason",None)
        provider_name=model.provider_key
        if result.cache_creation_input_tokens or result.cache_read_input_tokens:
            run.real_cost=None
            run.status="FAILED"; run.error_text=(f"{provider_name} returned unexpected prompt-cache usage; "
              "exact cache-cost reconciliation is not enabled")
        else:
            run.real_cost=calculate(model,result.input_tokens,result.output_tokens)
            if result.refusal:
                run.status="FAILED"; run.failure_reason="REFUSAL"; run.error_text=f"{provider_name} refusal: {result.refusal}"
            elif result.status!="completed":
                detail=f": {result.incomplete_reason}" if result.incomplete_reason else ""
                run.status="FAILED"
                run.failure_reason=("OUTPUT_TRUNCATED" if result.incomplete_reason in {
                  "max_tokens","max_output_tokens","model_context_window_exceeded"} else "PROVIDER_INCOMPLETE")
                run.error_text=f"{run.failure_reason}: {provider_name} response {result.status}{detail}"
            else: run.status="SUCCEEDED"
            record(run)
        run.finished_at=now(); db.session.commit()
        if run.status=="FAILED": return run
        if postprocess:
            try: postprocess(run)
            except Exception as exc:
                run.error_text=f"Downstream processing failed: {exc}"; db.session.commit(); raise
    except Exception as exc:
        run.status="FAILED"; run.error_text=str(exc); run.finished_at=now(); db.session.commit(); raise
    return run
