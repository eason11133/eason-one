from decimal import Decimal
from sqlalchemy import func
from ..extensions import db
from ..models import Company, CostEvent

def get_company():
    return Company.query.first()

def spent(company=None):
    company = company or get_company()
    return Decimal(db.session.query(func.coalesce(func.sum(CostEvent.real_cost_delta), 0)).filter_by(company_id=company.id).scalar())

def remaining(company=None):
    company = company or get_company()
    return Decimal(company.real_budget_limit) - spent(company)

