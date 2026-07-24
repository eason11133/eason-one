import json
from ..extensions import db
from ..models import WorkMessage,Employee
from .execution import execute
from .tasks import transition
from .context import build
from ..schemas import REVIEW_SCHEMA

FIELDS={"decision","summary","issues","required_changes"}
DECISIONS={"ACCEPT":"DONE","REVISE":"WORKING","BLOCK":"BLOCKED"}
def validate_review(payload):
    if not isinstance(payload,dict) or set(payload)!=FIELDS: raise ValueError("Invalid review fields")
    if payload["decision"] not in DECISIONS or not isinstance(payload["summary"],str) or not payload["summary"].strip(): raise ValueError("Invalid review decision/summary")
    if not isinstance(payload["issues"],list) or not all(isinstance(x,str) for x in payload["issues"]): raise ValueError("Invalid review issues")
    if not isinstance(payload["required_changes"],list) or not all(isinstance(x,str) for x in payload["required_changes"]): raise ValueError("Invalid required changes")
    return payload
def run_review(task,reviewer=None,instruction="Review this Task"):
    if task.status!="REVIEW": raise ValueError("Task is not in REVIEW")
    allowed=task.reviewer or Employee.query.filter_by(slug="ceo").one()
    reviewer=reviewer or allowed
    if reviewer.id!=allowed.id: raise ValueError("Only the assigned reviewer may run review")
    context=build(reviewer,task.project,task)+f"\n\nASSIGNED EMPLOYEE RESULT\n{task.result_summary or '-'}"
    prompt=reviewer.system_instructions+"\nTASK_REVIEW\nReturn only JSON: decision ACCEPT|REVISE|BLOCK, summary, issues, required_changes."
    run=execute(reviewer,"TASK_REVIEW",instruction,task.project,task,context_override=context,system_prompt_override=prompt,response_schema=REVIEW_SCHEMA)
    if run.status!="SUCCEEDED": return run
    try:
        payload=validate_review(json.loads(run.raw_output)); run.parsed_output_json=payload
        db.session.add(WorkMessage(project_id=task.project_id,task_id=task.id,sender_employee_id=reviewer.id,
          recipient_employee_id=task.assigned_employee_id,message_type="REVIEW",content=payload["summary"],agent_run_id=run.id))
        db.session.flush(); transition(task,DECISIONS[payload["decision"]]); return run
    except Exception as exc:
        db.session.rollback()
        persisted=db.session.get(__import__("eason_one.models",fromlist=["AgentRun"]).AgentRun,run.id)
        persisted.error_text=f"Review validation failed: {exc}"; db.session.commit()
        return persisted
