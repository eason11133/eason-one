from __future__ import annotations

from ..extensions import db
from ..models import AgentRun, EmployeeLearningRecord, FounderInterview, FounderInterviewMessage, Task
from .execution import execute
from .text_normalization import clean_text


def start(employee, project_id=None):
    interview = FounderInterview(employee_id=employee.id, project_id=project_id)
    db.session.add(interview)
    db.session.commit()
    return interview


def _normalize_request_id(value):
    value = clean_text(str(value or ""), multiline=False).strip()
    if not value:
        return None
    if len(value) > 160:
        raise ValueError("Interview request ID is too long")
    return value


def _run_for_request(interview, request_id, content):
    """Return the exact durable paid Run for one browser interview request.

    The request id lives on AgentRun rather than in process/session memory.  A
    browser transport retry after a successful provider call therefore consumes
    the same immutable response instead of buying another answer.  Reusing a key
    for a different interview or different text is a hard conflict.
    """
    request_id = _normalize_request_id(request_id)
    if request_id is None:
        return None
    for run in (
        AgentRun.query.filter_by(employee_id=interview.employee_id, purpose="FOUNDER_INTERVIEW")
        .order_by(AgentRun.id.desc())
        .limit(80)
        .all()
    ):
        composition = dict(run.context_composition_json or {})
        marker = dict(composition.get("founder_interview") or {})
        if marker.get("request_id") != request_id:
            continue
        if int(marker.get("interview_id") or 0) != int(interview.id):
            raise ValueError("INTERVIEW_REQUEST_ID_CONFLICT: interview scope differs from durable request")
        if str(run.user_request or "") != str(content or ""):
            raise ValueError("INTERVIEW_REQUEST_ID_CONFLICT: request text differs from durable request")
        return run
    return None


def _unanswered_founder_message(interview, content):
    """Reuse only the immediately pending identical Founder message.

    This closes the tiny pre-AgentRun crash window: the Founder message may have
    committed but execute() may never have created a Run.  Once an Employee reply
    exists, asking the same words again is intentionally a new turn.
    """
    last = (
        FounderInterviewMessage.query.filter_by(interview_id=interview.id)
        .order_by(FounderInterviewMessage.id.desc())
        .first()
    )
    if last and last.speaker == "FOUNDER" and str(last.content or "") == str(content or ""):
        return last
    row = FounderInterviewMessage(
        interview_id=interview.id,
        speaker="FOUNDER",
        content=content,
    )
    db.session.add(row)
    db.session.commit()
    return row


def _founder_message_for_run(interview, run):
    marker = dict((run.context_composition_json or {}).get("founder_interview") or {})
    message_id = int(marker.get("founder_message_id") or 0)
    if message_id:
        row = db.session.get(FounderInterviewMessage, message_id)
        if row is None or int(row.interview_id) != int(interview.id) or row.speaker != "FOUNDER":
            raise ValueError("INTERVIEW_RUN_FOUNDER_MESSAGE_LINEAGE_MISMATCH")
        return row
    return None


def _materialize_employee_reply(interview, founder_message, run, *, recovery=False):
    """Project one paid successful Run into the interview exactly once."""
    if run.status != "SUCCEEDED":
        return run
    if int(run.employee_id or 0) != int(interview.employee_id):
        raise ValueError("INTERVIEW_RUN_EMPLOYEE_LINEAGE_MISMATCH")
    marker = dict((run.context_composition_json or {}).get("founder_interview") or {})
    if int(marker.get("interview_id") or 0) != int(interview.id):
        raise ValueError("INTERVIEW_RUN_SCOPE_LINEAGE_MISMATCH")
    if founder_message is None:
        founder_message = _founder_message_for_run(interview, run)
    if founder_message is None:
        raise ValueError("INTERVIEW_RUN_FOUNDER_MESSAGE_MISSING")

    existing = (
        FounderInterviewMessage.query.filter(
            FounderInterviewMessage.interview_id == interview.id,
            FounderInterviewMessage.id > founder_message.id,
            FounderInterviewMessage.speaker == "EMPLOYEE",
            FounderInterviewMessage.content == (run.raw_output or ""),
        )
        .order_by(FounderInterviewMessage.id)
        .first()
    )
    if existing is None:
        db.session.add(
            FounderInterviewMessage(
                interview_id=interview.id,
                speaker="EMPLOYEE",
                content=run.raw_output or "",
            )
        )
    if recovery:
        composition = dict(run.context_composition_json or {})
        marker = dict(composition.get("founder_interview") or {})
        marker["reply_materialization_recovered"] = True
        marker["provider_replayed"] = False
        composition["founder_interview"] = marker
        run.context_composition_json = composition
    db.session.commit()
    return run


def _build_context(interview, founder_message):
    employee = interview.employee
    tasks = (
        Task.query.filter_by(assigned_employee_id=employee.id)
        .order_by(Task.updated_at.desc())
        .limit(10)
        .all()
    )
    learning = (
        EmployeeLearningRecord.query.filter_by(employee_id=employee.id)
        .order_by(EmployeeLearningRecord.created_at.desc())
        .limit(5)
        .all()
    )
    # Include the new Founder message exactly once. The query is bounded by its
    # durable row id so a later browser retry cannot accidentally pull in newer
    # conversation turns and change the paid request fingerprint.
    recent = (
        FounderInterviewMessage.query.filter(
            FounderInterviewMessage.interview_id == interview.id,
            FounderInterviewMessage.id <= founder_message.id,
        )
        .order_by(FounderInterviewMessage.id.desc())
        .limit(12)
        .all()
    )
    context = [
        f"EMPLOYEE\n{employee.name}; role: {employee.role_description}; department: "
        f"{employee.department.name if employee.department else 'CEO Office / Assurance'}; "
        f"position: {employee.position.name} L{employee.position.level}; manager: "
        f"{employee.manager.name if employee.manager else 'Founder'}; model: {employee.current_model.label}"
    ]
    if tasks:
        context.append(
            "WORK\n"
            + "\n".join(
                f"{task.status}: {task.project.name} / {task.title} — {task.result_summary or task.objective}"
                for task in tasks
            )
        )
    if learning:
        context.append(
            "RECENT LEARNING\n"
            + "\n".join(f"{row.title}: {row.content}" for row in learning)
        )
    if interview.project_id:
        from .context import build
        project_model = __import__("eason_one.models", fromlist=["Project"]).Project
        context.append(build(employee, db.session.get(project_model, interview.project_id)))
    if recent:
        context.append(
            "SAME INTERVIEW — RECENT CONVERSATION\n"
            + "\n".join(f"{message.speaker}: {message.content}" for message in reversed(recent))
        )
    return "\n\n".join(context)


def ask(interview, content, request_id=None):
    content = str(content or "").strip()
    if not content:
        raise ValueError("Interview message is required")
    before = interview.employee.system_instructions
    request_id = _normalize_request_id(request_id)

    prior = _run_for_request(interview, request_id, content)
    if prior is not None:
        founder_message = _founder_message_for_run(interview, prior)
        if prior.status == "SUCCEEDED":
            result = _materialize_employee_reply(interview, founder_message, prior, recovery=True)
            assert interview.employee.system_instructions == before
            return result
        if prior.status == "RUNNING" or str(prior.outcome or "") == "FAILED_AMBIGUOUS":
            # The provider may already be executing or may already have charged.
            # Never turn an HTTP/browser retry into a second model call.
            assert interview.employee.system_instructions == before
            return prior
        # One exact browser request may own at most one automatic replacement.
        # Repeated refreshes after a known provider/model failure must not create
        # an unbounded spend loop under the same request id. A genuinely new
        # Founder attempt uses a new request id / conversation turn.
        if int(prior.attempt_number or 1) >= 2:
            assert interview.employee.system_instructions == before
            return prior
        replay_ok, _ = __import__(
            "eason_one.services.external_effects", fromlist=["retry_authorized"]
        ).retry_authorized(prior)
        if not replay_ok:
            assert interview.employee.system_instructions == before
            return prior
        if founder_message is None:
            raise ValueError("INTERVIEW_RETRY_FOUNDER_MESSAGE_MISSING")
        run = execute(
            interview.employee,
            "FOUNDER_INTERVIEW",
            content,
            context_override=prior.context_snapshot,
            system_prompt_override=prior.system_prompt_snapshot,
            context_composition={
                "founder_interview": {
                    "interview_id": interview.id,
                    "founder_message_id": founder_message.id,
                    "request_id": request_id,
                    "retry_of_run_id": prior.id,
                }
            },
            prompt_version="founder-interview-v2-retry",
            retry_of_run=prior,
        )
        if run.status == "SUCCEEDED":
            _materialize_employee_reply(interview, founder_message, run)
        assert interview.employee.system_instructions == before
        return run

    founder_message = _unanswered_founder_message(interview, content)
    context = _build_context(interview, founder_message)
    run = execute(
        interview.employee,
        "FOUNDER_INTERVIEW",
        content,
        context_override=context,
        context_composition={
            "founder_interview": {
                "interview_id": interview.id,
                "founder_message_id": founder_message.id,
                "request_id": request_id,
            }
        },
        prompt_version="founder-interview-v2",
    )
    if run.status == "SUCCEEDED":
        _materialize_employee_reply(interview, founder_message, run)
    assert interview.employee.system_instructions == before
    return run
