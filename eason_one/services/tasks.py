from ..extensions import db
from ..models import Task, WorkMessage, now

def _assert_legacy_task_writer_allowed(task=None, project=None):
    project = project or getattr(task, "project", None)
    if project is not None and __import__(
        "eason_one.services.work_runtime", fromlist=["project_is_terminal"]
    ).project_is_terminal(project):
        raise ValueError(
            f"PROJECT_TERMINAL_LEGACY_TASK_WRITER_FORBIDDEN:{str(project.status or '').upper()}"
        )
    if task is not None and getattr(task, "work_id", None):
        raise ValueError("WORK_BACKED_TASK_LEGACY_WRITER_FORBIDDEN")
    if project is not None and __import__(
        "eason_one.services.project_contract", fromlist=["is_vnext_governed"]
    ).is_vnext_governed(project):
        raise ValueError("GOVERNED_PROJECT_TASK_LEGACY_WRITER_FORBIDDEN")

TRANSITIONS = {
    "TODO":{"ASSIGNED","CANCELLED"}, "ASSIGNED":{"WORKING","CANCELLED"},
    "WORKING":{"BLOCKED","REVIEW","FAILED"}, "BLOCKED":{"WORKING","CANCELLED"},
    "REVIEW":{"DONE","WORKING","BLOCKED"}, "DONE":set(), "FAILED":{"WORKING"}, "CANCELLED":set()
}
def create_task(project, title, objective, creator=None, assignee=None, reviewer=None, **kwargs):
    _assert_legacy_task_writer_allowed(project=project)
    status = "ASSIGNED" if assignee else "TODO"
    task = Task(project_id=project.id, title=title, objective=objective, status=status,
        created_by_employee_id=getattr(creator,"id",None), assigned_employee_id=getattr(assignee,"id",None),
        reviewer_employee_id=getattr(reviewer,"id",None), **kwargs)
    db.session.add(task); db.session.commit(); return task
def assign(task, employee):
    _assert_legacy_task_writer_allowed(task=task)
    if task.status != "TODO": raise ValueError("Only TODO tasks can be assigned")
    task.assigned_employee_id=employee.id; transition(task,"ASSIGNED"); return task
def transition(task, target):
    _assert_legacy_task_writer_allowed(task=task)
    if target not in TRANSITIONS.get(task.status,set()): raise ValueError(f"Invalid transition {task.status} -> {target}")
    task.status=target
    if target=="DONE": task.completed_at=now()
    db.session.commit(); return task
def store_result(task, result):
    _assert_legacy_task_writer_allowed(task=task)
    if task.status not in {"WORKING","ASSIGNED"}: raise ValueError("Task is not executable")
    if task.status=="ASSIGNED": transition(task,"WORKING")
    task.result_summary=result
    transition(task,"REVIEW")
    return task
def review(task, reviewer, content, accepted):
    _assert_legacy_task_writer_allowed(task=task)
    if task.status!="REVIEW": raise ValueError("Task is not in review")
    db.session.add(WorkMessage(project_id=task.project_id, task_id=task.id, sender_employee_id=reviewer.id,
        recipient_employee_id=task.assigned_employee_id, message_type="REVIEW", content=content))
    db.session.flush(); transition(task, "DONE" if accepted else "WORKING"); return task

