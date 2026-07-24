from decimal import Decimal
from dataclasses import dataclass
from ..extensions import db
from ..models import CostEvent
from .company import get_company, remaining

@dataclass(frozen=True)
class ExecutionEstimate:
    framed_text: str
    input_tokens: int
    output_tokens: int
    real_cost: Decimal

def calculate(model, input_tokens, output_tokens):
    return (Decimal(input_tokens)*Decimal(model.input_price_per_million)+Decimal(output_tokens)*Decimal(model.output_price_per_million))/Decimal(1_000_000)
def ensure_budget(estimated=Decimal("0")):
    if remaining() <= 0 or estimated > remaining(): raise ValueError("Company real budget is exhausted")
def conservative_estimate(model, prompt_text, max_output_tokens):
    # UTF-8 bytes plus 25% and fixed framing overhead deliberately overestimate.
    byte_bound=len(prompt_text.encode("utf-8"))
    estimated_input=max(1, (byte_bound*5+3)//4 + 256)
    return calculate(model,estimated_input,max_output_tokens)

def execution_frame(system_prompt,context,user_request):
    return f"INSTRUCTIONS:\n{system_prompt}\n\nCONTEXT:\n{context}\n\nUSER REQUEST:\n{user_request}"

def estimate_execution(model,system_prompt,context,user_request,max_output_tokens=None):
    output_tokens=model.max_output_tokens if max_output_tokens is None else int(max_output_tokens)
    framed=execution_frame(system_prompt,context,user_request)
    byte_bound=len(framed.encode("utf-8"))
    input_tokens=max(1,(byte_bound*5+3)//4+256)
    return ExecutionEstimate(framed,input_tokens,output_tokens,calculate(model,input_tokens,output_tokens))
def record(run):
    existing=CostEvent.query.filter_by(agent_run_id=run.id,category="MODEL").first()
    if existing: return existing
    event=CostEvent(company_id=get_company().id, employee_id=run.employee_id, project_id=run.project_id,
        task_id=run.task_id, agent_run_id=run.id, category="MODEL", description=f"{run.purpose}: {run.model_config.label}",
        internal_credits_delta=0, real_cost_delta=run.real_cost or 0, currency=run.currency)
    db.session.add(event); return event
