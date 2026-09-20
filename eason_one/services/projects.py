from ..extensions import db
from ..models import Project
VALID = {"PLANNING","ACTIVE","BLOCKED","REVIEW","COMPLETED","FAILED","CANCELLED","PARKED"}
def create_project(name, objective, owner, priority="MEDIUM", status="PLANNING",environment="LIVE",origin="NEW",**kwargs):
    if status not in VALID: raise ValueError("Invalid project status")
    if environment not in {"LIVE","SMOKE","ARCHIVED"} or origin not in {"NEW","EXISTING"}: raise ValueError("Invalid Project classification")
    project = Project(name=name,objective=objective,owner_employee_id=owner.id,priority=priority,status=status,
      environment=environment,origin=origin,**kwargs)
    db.session.add(project); db.session.commit()
    return project
def classify(project,environment):
    if environment not in {"LIVE","SMOKE","ARCHIVED"}: raise ValueError("Invalid Project classification")
    governed = __import__(
        "eason_one.services.project_contract", fromlist=["is_vnext_governed"]
    ).is_vnext_governed(project)
    if governed:
        raise ValueError("GOVERNED_PROJECT_CLASSIFICATION_IMMUTABLE")
    if project.environment == "ARCHIVED" and environment != "ARCHIVED":
        raise ValueError("ARCHIVED_PROJECT_CANNOT_RETURN_TO_LIVE_CLASSIFICATION")
    project.environment=environment; db.session.commit(); return project
