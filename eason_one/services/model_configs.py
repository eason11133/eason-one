from decimal import Decimal
from ..extensions import db
from ..models import ModelConfig
from .company import get_company
PROVIDERS={"mock","openai","anthropic"}
def validate(provider_key,input_price,output_price,currency,max_output_tokens):
    ip,op=Decimal(input_price),Decimal(output_price)
    if provider_key not in PROVIDERS: raise ValueError("Unsupported provider")
    if ip<0 or op<0: raise ValueError("Prices cannot be negative")
    if int(max_output_tokens)<=0: raise ValueError("Maximum output tokens must be positive")
    if provider_key!="mock":
        if ip<=0 or op<=0: raise ValueError("Real provider prices must both be greater than zero")
        if currency!=get_company().currency: raise ValueError("Real provider currency must match company currency")
    return ip,op
def create(label,provider_key,model_name,input_price,output_price,currency,max_output_tokens):
    if not label.strip() or not model_name.strip(): raise ValueError("Label and model name are required")
    currency=currency.upper(); ip,op=validate(provider_key,input_price,output_price,currency,max_output_tokens)
    row=ModelConfig(label=label.strip(),provider_key=provider_key,model_name=model_name.strip(),input_price_per_million=ip,
      output_price_per_million=op,currency=currency,max_output_tokens=int(max_output_tokens),active=True)
    db.session.add(row); db.session.commit(); return row
def toggle(model):
    if model.archived: raise ValueError("Archived ModelConfig cannot be activated")
    model.active=not model.active; db.session.commit(); return model

def edit(model,label,model_name,input_price,output_price,currency,max_output_tokens):
    currency=currency.upper()
    ip,op=validate(model.provider_key,input_price,output_price,currency,max_output_tokens)
    if not label.strip() or not model_name.strip(): raise ValueError("Label and model name are required")
    model.label=label.strip(); model.model_name=model_name.strip()
    model.input_price_per_million=ip; model.output_price_per_million=op
    model.currency=currency; model.max_output_tokens=int(max_output_tokens)
    db.session.commit(); return model

def archive(model):
    model.archived=True; model.active=False; db.session.commit(); return model

def delete(model):
    from ..models import Employee,EmployeeModelHistory,AgentRun
    if Employee.query.filter_by(current_model_config_id=model.id).first():
        raise ValueError("Assigned ModelConfig cannot be deleted; archive it instead")
    if AgentRun.query.filter_by(model_config_id=model.id).first():
        raise ValueError("Historically used ModelConfig cannot be deleted; archive it instead")
    if EmployeeModelHistory.query.filter_by(model_config_id=model.id).first():
        raise ValueError("ModelConfig history prevents deletion; archive it instead")
    db.session.delete(model); db.session.commit()
