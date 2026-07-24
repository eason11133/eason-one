from ..extensions import db
from ..models import FounderInterview, FounderInterviewMessage, Task, EmployeeLearningRecord
from .execution import execute
def start(employee, project_id=None):
    i=FounderInterview(employee_id=employee.id,project_id=project_id); db.session.add(i); db.session.commit(); return i
def ask(interview, content):
    before=interview.employee.system_instructions
    db.session.add(FounderInterviewMessage(interview_id=interview.id,speaker="FOUNDER",content=content)); db.session.commit()
    e=interview.employee
    tasks=Task.query.filter_by(assigned_employee_id=e.id).order_by(Task.updated_at.desc()).limit(10).all()
    learning=EmployeeLearningRecord.query.filter_by(employee_id=e.id).order_by(EmployeeLearningRecord.created_at.desc()).limit(5).all()
    recent=FounderInterviewMessage.query.filter_by(interview_id=interview.id).order_by(FounderInterviewMessage.created_at.desc()).limit(12).all()
    context=[f"EMPLOYEE\n{e.name}; role: {e.role_description}; department: {e.department.name if e.department else 'CEO Office / Assurance'}; position: {e.position.name} L{e.position.level}; manager: {e.manager.name if e.manager else 'Founder'}; model: {e.current_model.label}"]
    if tasks: context.append("WORK\n"+"\n".join(f"{t.status}: {t.project.name} / {t.title} — {t.result_summary or t.objective}" for t in tasks))
    if learning: context.append("RECENT LEARNING\n"+"\n".join(f"{x.title}: {x.content}" for x in learning))
    if interview.project_id:
        from .context import build
        context.append(build(e,db.session.get(__import__("eason_one.models",fromlist=["Project"]).Project,interview.project_id)))
    if recent: context.append("SAME INTERVIEW — RECENT CONVERSATION\n"+"\n".join(f"{m.speaker}: {m.content}" for m in reversed(recent)))
    run=execute(e,"FOUNDER_INTERVIEW",content,context_override="\n\n".join(context))
    db.session.add(FounderInterviewMessage(interview_id=interview.id,speaker="EMPLOYEE",content=run.raw_output)); db.session.commit()
    assert interview.employee.system_instructions==before
    return run
