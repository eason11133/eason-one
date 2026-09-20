from decimal import Decimal

from ..extensions import db
from ..models import Employee, ModelConfig
from .company import get_company

PROVIDERS = {"mock", "openai", "anthropic", "gemini", "perplexity", "codex"}


def validate(provider_key, input_price, output_price, currency, max_output_tokens, request_price_per_call=0):
    ip, op = Decimal(input_price), Decimal(output_price)
    rp = Decimal(request_price_per_call or 0)
    if provider_key not in PROVIDERS:
        raise ValueError("Unsupported provider")
    if ip < 0 or op < 0 or rp < 0:
        raise ValueError("Prices cannot be negative")
    if int(max_output_tokens) <= 0:
        raise ValueError("Maximum output tokens must be positive")
    if provider_key in {"mock", "codex"}:
        if ip != 0 or op != 0 or rp != 0:
            raise ValueError("Mock and Codex tool configurations must use zero API price")
    else:
        if ip <= 0 or op <= 0:
            raise ValueError("Real provider prices must both be greater than zero")
        if provider_key == "perplexity" and rp <= 0:
            raise ValueError("Perplexity/Sonar requires a positive Request fee / call in Company currency")
        if currency != get_company().currency:
            raise ValueError("Real provider currency must match company currency")
    return ip, op, rp


def create(label, provider_key, model_name, input_price, output_price, currency, max_output_tokens, request_price_per_call=0):
    if not label.strip() or not model_name.strip():
        raise ValueError("Label and model name are required")
    currency = currency.upper()
    ip, op, rp = validate(provider_key, input_price, output_price, currency, max_output_tokens, request_price_per_call)
    row = ModelConfig(
        label=label.strip(),
        provider_key=provider_key,
        model_name=model_name.strip(),
        input_price_per_million=ip,
        output_price_per_million=op,
        request_price_per_call=rp,
        currency=currency,
        max_output_tokens=int(max_output_tokens),
        active=True,
    )
    db.session.add(row)
    db.session.commit()
    return row


def toggle(model):
    if model.archived:
        raise ValueError("Archived ModelConfig must be restored before it can be enabled")
    model.active = not model.active
    db.session.commit()
    return model


def edit(model, label, model_name, input_price, output_price, currency, max_output_tokens, request_price_per_call=0):
    if model.archived:
        raise ValueError("Restore the archived ModelConfig before editing it")
    currency = currency.upper()
    ip, op, rp = validate(model.provider_key, input_price, output_price, currency, max_output_tokens, request_price_per_call)
    if not label.strip() or not model_name.strip():
        raise ValueError("Label and model name are required")
    model.label = label.strip()
    model.model_name = model_name.strip()
    model.input_price_per_million = ip
    model.output_price_per_million = op
    model.request_price_per_call = rp
    model.currency = currency
    model.max_output_tokens = int(max_output_tokens)
    db.session.commit()
    return model


def archive(model):
    assigned = Employee.query.filter_by(
        current_model_config_id=model.id,
        active=True,
    ).count()
    if assigned:
        raise ValueError(
            f"Reassign {assigned} active Employee(s) before archiving this ModelConfig"
        )
    model.archived = True
    model.active = False
    db.session.commit()
    return model


def restore(model):
    if not model.archived:
        raise ValueError("ModelConfig is not archived")
    # Restoration is explicit and immediately makes the configuration available
    # again. Provider readiness is still reported separately by System health.
    model.archived = False
    model.active = True
    db.session.commit()
    return model


def delete(model):
    from ..models import AgentRun, EmployeeModelHistory

    if Employee.query.filter_by(current_model_config_id=model.id).first():
        raise ValueError("Assigned ModelConfig cannot be deleted; archive it instead")
    if AgentRun.query.filter_by(model_config_id=model.id).first():
        raise ValueError("Historically used ModelConfig cannot be deleted; archive it instead")
    if EmployeeModelHistory.query.filter_by(model_config_id=model.id).first():
        raise ValueError("ModelConfig history prevents deletion; archive it instead")
    db.session.delete(model)
    db.session.commit()
