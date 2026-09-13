import hashlib
import json
from ..extensions import db
from ..models import AgentRun,WorkMessage,Employee
from .execution import execute
from .tasks import transition
from .context import build_with_composition
from .execution_policy import select_execution_model
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
    project = getattr(task, "project", None)
    if project is not None and __import__(
        "eason_one.services.work_runtime", fromlist=["project_is_terminal"]
    ).project_is_terminal(project):
        raise ValueError(
            f"PROJECT_TERMINAL_TASK_REVIEW_FORBIDDEN:{str(project.status or '').upper()}"
        )
    if task.status!="REVIEW": raise ValueError("Task is not in REVIEW")
    allowed=task.reviewer or Employee.query.filter_by(slug="ceo").one()
    reviewer=reviewer or allowed
    if reviewer.id!=allowed.id: raise ValueError("Only the assigned reviewer may run review")
    context, composition = build_with_composition(reviewer,task.project,task)
    context += f"\n\nASSIGNED EMPLOYEE RESULT\n{task.result_summary or '-'}"
    prompt=reviewer.system_instructions+"\nTASK_REVIEW\nReturn only JSON: decision ACCEPT|REVISE|BLOCK, summary, issues, required_changes."
    model=select_execution_model(reviewer,getattr(task,"operation",None),"TASK_REVIEW")
    # Legacy Task review used to treat every browser invocation as a brand-new
    # paid review, even when the exact submitted result/instruction had not
    # changed. Bind the paid attempts to one immutable review-target hash so a
    # refresh can consume the durable response instead of buying it again.
    target_payload={
      "task_id":int(task.id),
      "reviewer_id":int(reviewer.id),
      "instruction":instruction,
      "result_summary":task.result_summary or "",
    }
    target_hash=hashlib.sha256(
      json.dumps(target_payload,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    composition=dict(composition or {})
    composition["task_review_target"]={"hash":target_hash}

    matching=[]
    for candidate in AgentRun.query.filter_by(
      task_id=task.id,employee_id=reviewer.id,purpose="TASK_REVIEW"
    ).order_by(AgentRun.id).all():
        marker=((candidate.context_composition_json or {}).get("task_review_target") or {})
        if marker.get("hash")==target_hash:
            matching.append(candidate)
    latest=matching[-1] if matching else None

    def _execute_review(*, system_prompt, prompt_version, retry_of_run=None, recovery=None):
        local_composition=dict(composition or {})
        if recovery:
            local_composition["company_recovery"] = dict(recovery)
        return execute(reviewer,"TASK_REVIEW",instruction,task.project,task,
          context_override=context,context_composition=local_composition,
          system_prompt_override=system_prompt,response_schema=REVIEW_SCHEMA,
          operation=getattr(task,"operation",None),model_override=model,
          # This review can carry multiple issues/required changes.  Do not
          # truncate it behind a hidden 1,200-token ceiling; use the current
          # ModelConfig envelope and one bounded compact retry instead.
          max_output_tokens_override=int(model.max_output_tokens),
          prompt_version=prompt_version,retry_of_run=retry_of_run)

    if latest is not None and latest.status=="SUCCEEDED":
        run=latest
    elif latest is not None and (latest.status=="RUNNING" or str(latest.outcome or "")=="FAILED_AMBIGUOUS"):
        return latest
    elif latest is not None and len(matching)>=2:
        return latest
    elif latest is not None:
        retry_ok,_=__import__(
          "eason_one.services.external_effects",fromlist=["retry_authorized"]
        ).retry_authorized(latest)
        if not retry_ok:
            return latest
        run=_execute_review(
          system_prompt=(prompt+(
            "\nTASK_REVIEW_TRUNCATION_RECOVERY\n"
            "The previous review hit the configured output ceiling. Return the same review compactly. "
            "Keep the decision, one concise summary, only material issues, and only actionable required changes. "
            "Do not repeat the submitted result."
          ) if latest.failure_reason=="OUTPUT_TRUNCATED" else prompt),
          prompt_version=(
            "task-review-v2-truncation-recovery"
            if latest.failure_reason=="OUTPUT_TRUNCATED" else "task-review-v2-bounded-retry"
          ),
          retry_of_run=latest,
          recovery={
            "reason":latest.failure_reason or "KNOWN_FAILURE",
            "prior_run_id":latest.id,
            "policy":(
              "ONE_SAME_PROVIDER_COMPACT_RETRY"
              if latest.failure_reason=="OUTPUT_TRUNCATED" else "ONE_EXACT_TARGET_RETRY"
            ),
            "max_attempts":2,
          },
        )
    else:
        run=_execute_review(system_prompt=prompt,prompt_version="task-review-v2")
    if run.status!="SUCCEEDED": return run
    try:
        payload=validate_review(json.loads(run.raw_output)); run.parsed_output_json=payload
        run.structured_validation_status="PASSED"; run.structured_validation_errors_json=[]
        db.session.add(WorkMessage(project_id=task.project_id,task_id=task.id,sender_employee_id=reviewer.id,
          recipient_employee_id=task.assigned_employee_id,message_type="REVIEW",content=payload["summary"],agent_run_id=run.id))
        operation=getattr(task,"operation",None)
        if payload["decision"]=="REVISE" and operation is not None:
            kernel=__import__("eason_one.services.operation_kernel",fromlist=["append_event","transition","authoritative_status"])
            if int(operation.revision_count or 0) >= int(operation.max_revisions or 0):
                transition(task,"BLOCKED")
                work=__import__("eason_one.services.work_runtime",fromlist=["work_for_task","open_wait"]).work_for_task(task)
                if work:
                    __import__("eason_one.services.work_runtime",fromlist=["open_wait"]).open_wait(
                        work,"INTERNAL_RECOVERY",
                        "Maximum automatic review revisions reached; Project management must replan or reassign this Work."
                    )
                kernel.append_event(operation,"REVISION_LIMIT_REACHED",
                  from_status=kernel.authoritative_status(operation),to_status=kernel.authoritative_status(operation),
                  stage="TASK_REVIEW",payload={"task_id":task.id,"work_id":getattr(task,"work_id",None),
                    "revision_count":int(operation.revision_count or 0),"max_revisions":int(operation.max_revisions or 0)})
            else:
                operation.revision_count=int(operation.revision_count or 0)+1
                kernel.append_event(operation,"REVISION_AUTHORIZED",
                  from_status=kernel.authoritative_status(operation),to_status=kernel.authoritative_status(operation),
                  stage="TASK_REVIEW",payload={"task_id":task.id,"revision_count":operation.revision_count,
                    "max_revisions":operation.max_revisions})
                transition(task,"WORKING")
        else:
            transition(task,DECISIONS[payload["decision"]])
        db.session.commit(); return run
    except Exception as exc:
        db.session.rollback()
        persisted=db.session.get(__import__("eason_one.models",fromlist=["AgentRun"]).AgentRun,run.id)
        persisted.status="FAILED"; persisted.failure_reason="STRUCTURED_OUTPUT_INVALID"
        persisted.structured_validation_status="FAILED"; persisted.structured_validation_errors_json=[str(exc)]
        persisted.error_text=f"Review validation failed: {exc}"; db.session.commit()
        return persisted
