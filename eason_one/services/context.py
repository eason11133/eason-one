from ..models import WorkMessage
from .brain import current
from .company import remaining
def build(employee, project=None, task=None):
    parts=[f"EMPLOYEE\n{employee.name} — {employee.role_description}",
        f"Department: {employee.department.name if employee.department else 'CEO Office'}; Manager: {employee.manager.name if employee.manager else 'Founder'}"]
    if project: parts.append(f"PROJECT\n{project.name}\nObjective: {project.objective}\nStatus: {project.status}; Priority: {project.priority}")
    if task:
        parts.append(f"TASK\n{task.title}\nObjective: {task.objective}\nRequired output: {task.required_output or '-'}\nAcceptance: {task.acceptance_criteria or '-'}")
        msgs=WorkMessage.query.filter_by(task_id=task.id).order_by(WorkMessage.created_at.desc()).limit(5).all()
        if msgs: parts.append("WORK CONTEXT\n"+"\n".join(x.content for x in reversed(msgs)))
    brain=current(project.id if project else None)
    normal=[x for x in brain if x.kind!="KILLED"]; killed=[x for x in brain if x.kind=="KILLED"]
    if normal: parts.append("CURRENT COMPANY BRAIN\n"+"\n".join(f"{x.kind}: {x.title} — {x.content}"+(f" [corrects #{x.target_knowledge_id}]" if x.kind=="CORRECTION" else "") for x in normal[-12:]))
    if killed: parts.append("KILLED IDEAS — DO NOT RESURRECT WITHOUT MEANINGFUL NEW EVIDENCE\n"+"\n".join(f"{x.title} — {x.content} [kills hypothesis #{x.target_knowledge_id}]" for x in killed[-8:]))
    parts.append(f"COST\nRemaining company real budget: NT${remaining():,.2f}")
    return "\n\n".join(parts)
