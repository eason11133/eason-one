from ..extensions import db
from ..models import EmployeeLearningRecord
def create(employee,title,content,project=None,task=None,source_ref=None,validated=False):
    if not title.strip() or not content.strip(): raise ValueError("Learning title and content are required")
    if task and task.assigned_employee_id!=employee.id: raise ValueError("Task does not belong to employee")
    if task and project and task.project_id!=project.id: raise ValueError("Task does not belong to project")
    row=EmployeeLearningRecord(employee_id=employee.id,project_id=getattr(project,"id",None),task_id=getattr(task,"id",None),
        learning_type="FOUNDER_NOTE", validation_basis="FOUNDER" if validated else None,
        title=title.strip(),content=content.strip(),source_ref=source_ref or None,validated=bool(validated))
    db.session.add(row); db.session.commit(); return row
