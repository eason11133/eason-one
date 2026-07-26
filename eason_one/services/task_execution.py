import json
from ..extensions import db
from ..models import Proposal
from ..schemas import TASK_EXECUTION_SCHEMA
from .execution import execute
from .brain import validate_basis_ids
from .context import build_with_composition

FIELDS={"result_summary","knowledge_proposals"}
CANDIDATE_FIELDS={"kind","title","content","source_ref","rationale","basis_knowledge_ids"}
def run_task(task):
    if task.status not in {"ASSIGNED","WORKING"}: raise ValueError("Task is not executable")
    employee=task.assigned_employee
    prompt=employee.system_instructions+"\nTASK_EXECUTION\nReturn the structured Task result. Do not claim web research or fabricate citations."
    context, composition = build_with_composition(employee, task.project, task)
    run=execute(employee,"TASK_EXECUTION",task.objective,task.project,task,
      context_override=context,context_composition=composition,
      system_prompt_override=prompt,response_schema=TASK_EXECUTION_SCHEMA)
    if run.status!="SUCCEEDED": return run
    try:
        payload=json.loads(run.raw_output)
        if set(payload)!=FIELDS or not isinstance(payload["result_summary"],str) or not payload["result_summary"].strip(): raise ValueError("Invalid Task result")
        if not isinstance(payload["knowledge_proposals"],list) or len(payload["knowledge_proposals"])>8: raise ValueError("Invalid knowledge proposal list")
        run.parsed_output_json=payload; task.result_summary=payload["result_summary"]
        for item in payload["knowledge_proposals"]:
            try:
                if not isinstance(item,dict) or set(item)!=CANDIDATE_FIELDS: raise ValueError()
                if item["kind"] not in {"FACT","HYPOTHESIS","EVIDENCE","DECISION"}: raise ValueError()
                if not item["title"].strip() or not item["content"].strip(): raise ValueError()
                if item["kind"]=="EVIDENCE" and not item["source_ref"]: raise ValueError()
                if item["kind"]=="DECISION" and not item["rationale"]: raise ValueError()
                if item["basis_knowledge_ids"]: validate_basis_ids(item["basis_knowledge_ids"],task.project_id)
                db.session.add(Proposal(project_id=task.project_id,agent_run_id=run.id,proposed_by_employee_id=employee.id,payload_json=item,status="PENDING"))
            except (ValueError,TypeError,KeyError): continue
        db.session.commit(); return run
    except Exception as exc:
        db.session.rollback(); persisted=db.session.get(type(run),run.id); persisted.error_text=f"Task result validation failed: {exc}"; db.session.commit(); return persisted
