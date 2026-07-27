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

def meaningful_total(employee_id):
    """Return deterministic completed contribution units without double count."""
    from ..models import AgentRun, MeetingStep
    base=total(employee_id,"COMPANY")
    eligible={
      "CEO_FOUNDER_REQUEST","CEO_PROJECT_SYNTHESIS","CEO_OPERATION_DECISION",
      "GOAL_VERIFICATION","MEETING_CONTRIBUTION","MEETING_SYNTHESIS",
      "HR_ASSESSMENT",
    }
    represented={
      event.related_reference for event in ContributionEvent.query.filter_by(
        employee_id=employee_id).filter(
          ContributionEvent.related_reference.isnot(None)).all()
    }
    count=0
    for run in AgentRun.query.filter(
      AgentRun.employee_id==employee_id,AgentRun.purpose.in_(eligible)).all():
        recovered=MeetingStep.query.filter_by(
          agent_run_id=run.id,status="SUCCEEDED").first() is not None
        if (run.status=="SUCCEEDED" or recovered) and (
          f"agent_run:{run.id}" not in represented
        ):
            count+=1
    return base+Decimal(count)
