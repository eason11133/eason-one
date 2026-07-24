from decimal import Decimal
from sqlalchemy import func
from ..extensions import db
from ..models import ContributionEvent
def total(employee_id, scope, project_id=None):
    q=db.session.query(func.coalesce(func.sum(ContributionEvent.value),0)).filter_by(employee_id=employee_id,scope=scope)
    if scope=="PROJECT": q=q.filter_by(project_id=project_id)
    return Decimal(q.scalar())
def project_totals(employee_id):
    from ..models import Project
    return db.session.query(Project,func.coalesce(func.sum(ContributionEvent.value),0)).join(
        ContributionEvent,ContributionEvent.project_id==Project.id).filter(
        ContributionEvent.employee_id==employee_id,ContributionEvent.scope=="PROJECT").group_by(Project.id).all()
