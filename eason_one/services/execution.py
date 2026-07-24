from ..extensions import db
from ..models import AgentRun, now
from ..providers import get_provider
from .context import build
from .costs import ensure_budget, calculate, record, estimate_execution
def execute(employee, purpose, user_request, project=None, task=None, context_override=None, postprocess=None, system_prompt_override=None,response_schema=None,meeting=None):
    if not employee.current_model: raise ValueError("Employee has no model")
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
    estimate=estimate_execution(model,system_prompt,context,user_request,model.max_output_tokens)
    ensure_budget(estimate.real_cost)
    run=AgentRun(employee_id=employee.id,project_id=getattr(project,"id",None),task_id=getattr(task,"id",None),meeting_id=getattr(meeting,"id",None),
        model_config_id=model.id,purpose=purpose,user_request=user_request,
        system_prompt_snapshot=system_prompt,context_snapshot=context,
        provider_key_snapshot=model.provider_key,model_name_snapshot=model.model_name,
        input_price_snapshot=model.input_price_per_million,output_price_snapshot=model.output_price_per_million,
        currency_snapshot=model.currency,currency=model.currency)
    db.session.add(run); db.session.commit()
    try:
        provider=get_provider(model.provider_key)
        result=(provider.complete(model,system_prompt,user_request,context,model.max_output_tokens,response_schema)
          if response_schema is not None else provider.complete(model,system_prompt,user_request,context,model.max_output_tokens))
        run.raw_output=result.text; run.input_tokens=result.input_tokens; run.output_tokens=result.output_tokens
        run.provider_request_id=result.request_id; run.provider_response_id=result.response_id
        run.real_cost=calculate(model,result.input_tokens,result.output_tokens)
        if result.refusal:
            run.status="FAILED"; run.error_text=f"OpenAI refusal: {result.refusal}"
        elif result.status!="completed":
            detail=f": {result.incomplete_reason}" if result.incomplete_reason else ""
            run.status="FAILED"; run.error_text=f"OpenAI response {result.status}{detail}"
        else: run.status="SUCCEEDED"
        run.finished_at=now(); record(run); db.session.commit()
        if run.status=="FAILED": return run
        if postprocess:
            try: postprocess(run)
            except Exception as exc:
                run.error_text=f"Downstream processing failed: {exc}"; db.session.commit(); raise
    except Exception as exc:
        run.status="FAILED"; run.error_text=str(exc); run.finished_at=now(); db.session.commit(); raise
    return run
