# Eason One — Anthropic Paid-Call Gate Review Bundle

This bundle intentionally contains only code relevant to:
- Anthropic Provider
- shared provider execution
- cost accounting
- structured outputs
- Meeting mixed-provider execution
- regression tests

OpenAI has already passed a real paid smoke test and exact provider-usage reconciliation.
The Meeting engine has already passed its prior real-call gate.

Review the code below as one connected system.


---

## FILE: pyproject.toml

``toml
[project]
name = "eason-one"
version = "0.1.0"
requires-python = ">=3.13"
dependencies = ["Flask>=3.1", "Flask-SQLAlchemy>=3.1", "SQLAlchemy>=2.0", "openai>=2.48.0,<3", "anthropic>=0.117.0,<1"]

[project.optional-dependencies]
test = ["pytest>=8.0"]

[build-system]
requires = ["setuptools>=69"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
include = ["eason_one*"]
exclude = ["instance*", "tests*"]

[tool.setuptools.package-data]
eason_one = ["templates/*.html", "static/*.css"]

[tool.pytest.ini_options]
testpaths = ["tests"]

``

---

## FILE: eason_one\providers.py

``python
from dataclasses import dataclass
from copy import deepcopy
import json
import os
@dataclass
class ProviderResult:
    text: str
    input_tokens: int
    output_tokens: int
    response_id: str|None=None
    request_id: str|None=None
    status: str="completed"
    refusal: str|None=None
    incomplete_reason: str|None=None
    cache_creation_input_tokens: int=0
    cache_read_input_tokens: int=0

_ANTHROPIC_UNSUPPORTED_SCHEMA_KEYS={
    "minLength","maxLength","minimum","maximum","exclusiveMinimum","exclusiveMaximum",
    "multipleOf","minItems","maxItems","uniqueItems","minProperties","maxProperties",
}

def anthropic_compatible_schema(schema):
    """Remove provider-unsupported assertions; application validation keeps the original contract."""
    def convert(value):
        if isinstance(value,dict):
            return {key:convert(item) for key,item in value.items() if key not in _ANTHROPIC_UNSUPPORTED_SCHEMA_KEYS}
        if isinstance(value,list): return [convert(item) for item in value]
        return deepcopy(value)
    return convert(schema)
class MockProvider:
    def complete(self, model_config, system_prompt, user_prompt, context, max_output_tokens, response_schema=None):
        if "TASK_REVIEW" in system_prompt:
            decision="REVISE" if "revise" in user_prompt.lower() else ("BLOCK" if "block" in user_prompt.lower() else "ACCEPT")
            text=json.dumps({"decision":decision,"summary":"Mock reviewer assessed the result against acceptance criteria.",
                "issues":[] if decision=="ACCEPT" else ["A material issue remains"],"required_changes":[] if decision=="ACCEPT" else ["Address the identified issue"]})
        elif "MEETING_SYNTHESIS" in system_prompt:
            text=json.dumps({"agreements":["Use the smallest bounded next action."],
              "disagreements":["Evidence sufficiency remains disputed."],
              "evidence_referenced":["Only evidence recorded in the Meeting context was considered."],
              "rejected_or_unresolved":["External validation remains unresolved."],
              "actions":["Founder reviews and authorizes any next execution."],
              "founder_decisions_required":["Approve, revise, or stop the proposed next action."]})
        elif "CEO_PROJECT_SYNTHESIS" in system_prompt:
            text=json.dumps({"executive_summary":"The team completed a reviewable project cycle.","result":"Available results support a bounded Founder decision.",
              "key_findings":["Completed work and reviewer evidence are recorded"],"disagreements_or_risks":["Mock evidence is not market evidence"],
              "unresolved_questions":["Real customer validation remains"],"founder_decisions_required":["Choose whether to proceed to a paid test"],
              "recommended_next_actions":["Run the smallest authorized real-world validation"]})
        elif "CEO_FOUNDER_REQUEST" in system_prompt:
            status=any(x in user_prompt.lower() for x in ("how is","status","progress"))
            action=any(x in user_prompt.lower() for x in ("add task","next action","continue project"))
            project_ids=__import__("re").findall(r"Project #(\d+)",context)
            if status:
                text=json.dumps({"mode":"STATUS_QUERY","executive_response":"The requested project status is summarized from current operating state.",
                    "project_id":int(project_ids[-1]) if project_ids else None})
                return ProviderResult(text,len((system_prompt+context+user_prompt).split()),len(text.split()),"mock-local")
            engineering = any(x in user_prompt.lower() for x in ("build", "engineering", "software", "website"))
            tasks = ([{"title":"Define implementation scope","objective":user_prompt,"assignee_slug":"engineer","reviewer_slug":"engineering-director",
                "required_output":"Scoped implementation plan","acceptance_criteria":"Risks and deliverables are explicit"}] if engineering else
                [{"title":"Investigate market evidence","objective":user_prompt,"assignee_slug":"researcher","reviewer_slug":"research-director",
                "required_output":"Evidence-backed research brief","acceptance_criteria":"Claims cite sources and uncertainties"}])
            if action and project_ids:
                text=json.dumps({"mode":"PROJECT_ACTION","executive_response":"I prepared additional governed work for the existing project.",
                    "project_id":int(project_ids[-1]),"tasks":tasks})
            else:
                text=json.dumps({"mode":"NEW_PROJECT","executive_response":"I prepared a focused, reviewable plan for Founder approval.",
                    "project":{"name":_project_name(user_prompt),"objective":user_prompt,"priority":"HIGH"},"tasks":tasks})
        elif "TASK_EXECUTION" in system_prompt:
            text=json.dumps({"result_summary":f"Completed assigned work: {user_prompt}",
              "knowledge_proposals":[{"kind":"EVIDENCE","title":"Mock execution evidence","content":"The governed Task execution path completed; this is system evidence, not external market research.",
                "source_ref":"mock:task-execution","rationale":None,"basis_knowledge_ids":[]}]})
        else:
            text=f"EXECUTIVE RESULT\nRequest addressed: {user_prompt}\nRecommendation: proceed with the smallest test, record evidence, and review before commitment."
        return ProviderResult(text,len((system_prompt+context+user_prompt).split()),len(text.split()),response_id="mock-response",status="completed")
class OpenAIProvider:
    def complete(self, model_config, system_prompt, user_prompt, context, max_output_tokens, response_schema=None):
        from openai import OpenAI
        kwargs={"model":model_config.model_name,"instructions":system_prompt,"input":f"{context}\n\nUSER REQUEST:\n{user_prompt}","max_output_tokens":max_output_tokens}
        if response_schema:
            kwargs["text"]={"format":{"type":"json_schema","name":response_schema["name"],"strict":True,"schema":response_schema["schema"]}}
        r=OpenAI(api_key=os.environ["OPENAI_API_KEY"],max_retries=0).responses.create(**kwargs)
        refusal=None
        for item in getattr(r,"output",[]) or []:
            for content in getattr(item,"content",[]) or []:
                if getattr(content,"type",None)=="refusal": refusal=getattr(content,"refusal",None)
        incomplete=getattr(getattr(r,"incomplete_details",None),"reason",None)
        return ProviderResult(getattr(r,"output_text","") or "",r.usage.input_tokens,r.usage.output_tokens,
          response_id=r.id,request_id=getattr(r,"_request_id",None),status=getattr(r,"status",None) or "completed",
          refusal=refusal,incomplete_reason=incomplete)

class AnthropicProvider:
    def complete(self,model_config,system_prompt,user_prompt,context,max_output_tokens,response_schema=None):
        from anthropic import Anthropic
        kwargs={"model":model_config.model_name,"max_tokens":max_output_tokens,"system":system_prompt,
          "messages":[{"role":"user","content":f"{context}\n\nUSER REQUEST:\n{user_prompt}"}]}
        if response_schema:
            kwargs["output_config"]={"format":{"type":"json_schema",
              "schema":anthropic_compatible_schema(response_schema["schema"])}}
        message=Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"],max_retries=0).messages.create(**kwargs)
        text="\n".join(
          getattr(block,"text","") for block in (getattr(message,"content",None) or [])
          if getattr(block,"type",None)=="text" and getattr(block,"text",None) is not None)
        usage=message.usage
        stop_reason=getattr(message,"stop_reason",None)
        refusal=(text or "Request refused") if stop_reason=="refusal" else None
        if stop_reason in {"end_turn","stop_sequence"}: status,incomplete="completed",None
        elif stop_reason=="refusal": status,incomplete="completed",None
        else: status,incomplete="incomplete",stop_reason or "unknown"
        return ProviderResult(text,usage.input_tokens,usage.output_tokens,
          response_id=message.id,request_id=getattr(message,"_request_id",None),status=status,
          refusal=refusal,incomplete_reason=incomplete,
          cache_creation_input_tokens=getattr(usage,"cache_creation_input_tokens",0) or 0,
          cache_read_input_tokens=getattr(usage,"cache_read_input_tokens",0) or 0)
def _project_name(request):
    words=request.strip().rstrip(".").split()
    if "project" in [x.lower() for x in words]:
        words=words[:[x.lower() for x in words].index("project")]
    while words and words[0].lower() in {"create","start","a","an","the","build"}: words.pop(0)
    return " ".join(words[:8]).title() or "Founder Initiative"
def get_provider(key):
    if key=="mock": return MockProvider()
    if key=="openai": return OpenAIProvider()
    if key=="anthropic": return AnthropicProvider()
    raise ValueError(f"Unknown provider: {key}")

``

---

## FILE: eason_one\schemas.py

``python
TASK_FIELDS = {
    "type": "object", "additionalProperties": False,
    "required": ["title", "objective", "assignee_slug", "reviewer_slug", "required_output", "acceptance_criteria"],
    "properties": {
        "title": {"type": "string"}, "objective": {"type": "string"},
        "assignee_slug": {"type": "string"}, "reviewer_slug": {"type": ["string", "null"]},
        "required_output": {"type": "string"}, "acceptance_criteria": {"type": "string"},
    },
}
CEO_SCHEMA = {"name": "ceo_founder_request", "schema": {
    "type": "object", "additionalProperties": False,
    "required": ["mode", "executive_response", "project", "project_id", "tasks"],
    "properties": {
        "mode": {"type": "string", "enum": ["NEW_PROJECT", "PROJECT_ACTION", "STATUS_QUERY"]},
        "executive_response": {"type": "string"},
        "project": {"anyOf": [
            {"type": "object", "additionalProperties": False, "required": ["name", "objective", "priority"],
             "properties": {"name": {"type": "string"}, "objective": {"type": "string"},
                            "priority": {"type": "string", "enum": ["LOW", "MEDIUM", "HIGH", "CRITICAL"]}}},
            {"type": "null"},
        ]},
        "project_id": {"type": ["integer", "null"]},
        "tasks": {"type": "array", "maxItems": 12, "items": TASK_FIELDS},
    },
}}
REVIEW_SCHEMA = {"name": "task_review", "schema": {
    "type": "object", "additionalProperties": False,
    "required": ["decision", "summary", "issues", "required_changes"],
    "properties": {
        "decision": {"type": "string", "enum": ["ACCEPT", "REVISE", "BLOCK"]},
        "summary": {"type": "string"}, "issues": {"type": "array", "items": {"type": "string"}},
        "required_changes": {"type": "array", "items": {"type": "string"}},
    },
}}
SYNTHESIS_SCHEMA = {"name": "ceo_project_synthesis", "schema": {
    "type": "object", "additionalProperties": False,
    "required": ["executive_summary", "result", "key_findings", "disagreements_or_risks",
                 "unresolved_questions", "founder_decisions_required", "recommended_next_actions"],
    "properties": {
        "executive_summary": {"type": "string"}, "result": {"type": "string"},
        "key_findings": {"type": "array", "items": {"type": "string"}},
        "disagreements_or_risks": {"type": "array", "items": {"type": "string"}},
        "unresolved_questions": {"type": "array", "items": {"type": "string"}},
        "founder_decisions_required": {"type": "array", "items": {"type": "string"}},
        "recommended_next_actions": {"type": "array", "items": {"type": "string"}},
    },
}}
KNOWLEDGE_CANDIDATE = {
    "type": "object", "additionalProperties": False,
    "required": ["kind", "title", "content", "source_ref", "rationale", "basis_knowledge_ids"],
    "properties": {
        "kind": {"type": "string", "enum": ["FACT", "HYPOTHESIS", "EVIDENCE", "DECISION"]},
        "title": {"type": "string"}, "content": {"type": "string"},
        "source_ref": {"type": ["string", "null"]}, "rationale": {"type": ["string", "null"]},
        "basis_knowledge_ids": {"type": "array", "items": {"type": "integer"}},
    },
}
TASK_EXECUTION_SCHEMA = {"name": "task_execution", "schema": {
    "type": "object", "additionalProperties": False,
    "required": ["result_summary", "knowledge_proposals"],
    "properties": {
        "result_summary": {"type": "string"},
        "knowledge_proposals": {"type": "array", "maxItems": 8, "items": KNOWLEDGE_CANDIDATE},
    },
}}

MEETING_SYNTHESIS_FIELDS = [
    "agreements", "disagreements", "evidence_referenced",
    "rejected_or_unresolved", "actions", "founder_decisions_required",
]
MEETING_SYNTHESIS_SCHEMA = {"name": "meeting_synthesis", "schema": {
    "type": "object", "additionalProperties": False,
    "required": MEETING_SYNTHESIS_FIELDS,
    "properties": {
        field: {"type": "array", "maxItems": 8, "items": {"type": "string", "maxLength": 500}}
        for field in MEETING_SYNTHESIS_FIELDS
    },
}}

``

---

## FILE: eason_one\models.py

``python
from datetime import datetime, timezone
from .extensions import db

def now():
    return datetime.now(timezone.utc)

class TimestampMixin:
    created_at = db.Column(db.DateTime(timezone=True), default=now, nullable=False)

class Company(db.Model, TimestampMixin):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    real_budget_limit = db.Column(db.Numeric(12, 4), nullable=False)
    currency = db.Column(db.String(8), default="TWD", nullable=False)
    updated_at = db.Column(db.DateTime(timezone=True), default=now, onupdate=now, nullable=False)

class Department(db.Model, TimestampMixin):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    description = db.Column(db.Text)
    active = db.Column(db.Boolean, default=True, nullable=False)

class Position(db.Model, TimestampMixin):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    level = db.Column(db.Integer, nullable=False)
    description = db.Column(db.Text)

class ModelConfig(db.Model, TimestampMixin):
    id = db.Column(db.Integer, primary_key=True)
    label = db.Column(db.String(120), nullable=False)
    provider_key = db.Column(db.String(40), nullable=False)
    model_name = db.Column(db.String(120), nullable=False)
    input_price_per_million = db.Column(db.Numeric(12, 4), default=0, nullable=False)
    output_price_per_million = db.Column(db.Numeric(12, 4), default=0, nullable=False)
    currency = db.Column(db.String(8), default="TWD", nullable=False)
    max_output_tokens = db.Column(db.Integer, default=1200, nullable=False)
    active = db.Column(db.Boolean, default=True, nullable=False)

class Employee(db.Model, TimestampMixin):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    slug = db.Column(db.String(120), unique=True, nullable=False)
    department_id = db.Column(db.Integer, db.ForeignKey("department.id"))
    position_id = db.Column(db.Integer, db.ForeignKey("position.id"), nullable=False)
    manager_id = db.Column(db.Integer, db.ForeignKey("employee.id"))
    role_description = db.Column(db.Text, nullable=False)
    system_instructions = db.Column(db.Text, nullable=False)
    current_model_config_id = db.Column(db.Integer, db.ForeignKey("model_config.id"))
    salary_credits_per_week = db.Column(db.Numeric(12, 2), default=0, nullable=False)
    active = db.Column(db.Boolean, default=True, nullable=False)
    updated_at = db.Column(db.DateTime(timezone=True), default=now, onupdate=now, nullable=False)
    department = db.relationship("Department")
    position = db.relationship("Position")
    manager = db.relationship("Employee", remote_side=[id])
    current_model = db.relationship("ModelConfig")

class EmployeeModelHistory(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.Integer, db.ForeignKey("employee.id"), nullable=False)
    model_config_id = db.Column(db.Integer, db.ForeignKey("model_config.id"))
    reason = db.Column(db.Text)
    started_at = db.Column(db.DateTime(timezone=True), default=now, nullable=False)
    ended_at = db.Column(db.DateTime(timezone=True))
    model_config = db.relationship("ModelConfig")

class Project(db.Model, TimestampMixin):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(160), nullable=False)
    objective = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(20), default="PLANNING", nullable=False)
    priority = db.Column(db.String(20), default="MEDIUM", nullable=False)
    environment = db.Column(db.String(20), default="LIVE", nullable=False)
    origin = db.Column(db.String(20), default="NEW", nullable=False)
    current_state_summary = db.Column(db.Text)
    known_constraints = db.Column(db.Text)
    next_milestone = db.Column(db.Text)
    owner_employee_id = db.Column(db.Integer, db.ForeignKey("employee.id"), nullable=False)
    budget_credits = db.Column(db.Numeric(12, 2))
    real_budget_limit = db.Column(db.Numeric(12, 4))
    deadline = db.Column(db.DateTime(timezone=True))
    updated_at = db.Column(db.DateTime(timezone=True), default=now, onupdate=now, nullable=False)
    owner = db.relationship("Employee")

class Task(db.Model, TimestampMixin):
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"), nullable=False)
    parent_task_id = db.Column(db.Integer, db.ForeignKey("task.id"))
    title = db.Column(db.String(180), nullable=False)
    objective = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(20), default="TODO", nullable=False)
    priority = db.Column(db.String(20), default="MEDIUM", nullable=False)
    created_by_employee_id = db.Column(db.Integer, db.ForeignKey("employee.id"))
    assigned_employee_id = db.Column(db.Integer, db.ForeignKey("employee.id"))
    reviewer_employee_id = db.Column(db.Integer, db.ForeignKey("employee.id"))
    deadline = db.Column(db.DateTime(timezone=True))
    budget_credits = db.Column(db.Numeric(12, 2))
    required_output = db.Column(db.Text)
    acceptance_criteria = db.Column(db.Text)
    result_summary = db.Column(db.Text)
    updated_at = db.Column(db.DateTime(timezone=True), default=now, onupdate=now, nullable=False)
    completed_at = db.Column(db.DateTime(timezone=True))
    project = db.relationship("Project", backref="tasks")
    assigned_employee = db.relationship("Employee", foreign_keys=[assigned_employee_id])
    reviewer = db.relationship("Employee", foreign_keys=[reviewer_employee_id])

class WorkMessage(db.Model, TimestampMixin):
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"))
    task_id = db.Column(db.Integer, db.ForeignKey("task.id"))
    sender_employee_id = db.Column(db.Integer, db.ForeignKey("employee.id"))
    recipient_employee_id = db.Column(db.Integer, db.ForeignKey("employee.id"))
    agent_run_id = db.Column(db.Integer, db.ForeignKey("agent_run.id"))
    message_type = db.Column(db.String(20), nullable=False)
    content = db.Column(db.Text, nullable=False)

class AgentRun(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.Integer, db.ForeignKey("employee.id"), nullable=False)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"))
    task_id = db.Column(db.Integer, db.ForeignKey("task.id"))
    meeting_id = db.Column(db.Integer, db.ForeignKey("meeting.id"))
    model_config_id = db.Column(db.Integer, db.ForeignKey("model_config.id"), nullable=False)
    purpose = db.Column(db.String(80), nullable=False)
    user_request = db.Column(db.Text, nullable=False)
    system_prompt_snapshot = db.Column(db.Text, nullable=False)
    context_snapshot = db.Column(db.Text, nullable=False)
    raw_output = db.Column(db.Text)
    parsed_output_json = db.Column(db.JSON)
    status = db.Column(db.String(20), default="CREATED", nullable=False)
    provider_request_id = db.Column(db.String(160))
    provider_response_id = db.Column(db.String(160))
    input_tokens = db.Column(db.Integer)
    output_tokens = db.Column(db.Integer)
    cache_creation_input_tokens = db.Column(db.Integer, default=0, nullable=False)
    cache_read_input_tokens = db.Column(db.Integer, default=0, nullable=False)
    real_cost = db.Column(db.Numeric(12, 6))
    currency = db.Column(db.String(8), default="TWD", nullable=False)
    provider_key_snapshot = db.Column(db.String(40), nullable=False)
    model_name_snapshot = db.Column(db.String(120), nullable=False)
    input_price_snapshot = db.Column(db.Numeric(12, 4), nullable=False)
    output_price_snapshot = db.Column(db.Numeric(12, 4), nullable=False)
    currency_snapshot = db.Column(db.String(8), nullable=False)
    error_text = db.Column(db.Text)
    started_at = db.Column(db.DateTime(timezone=True), default=now, nullable=False)
    finished_at = db.Column(db.DateTime(timezone=True))
    employee = db.relationship("Employee")
    model_config = db.relationship("ModelConfig")

class CostEvent(db.Model, TimestampMixin):
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=False)
    employee_id = db.Column(db.Integer, db.ForeignKey("employee.id"))
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"))
    task_id = db.Column(db.Integer, db.ForeignKey("task.id"))
    agent_run_id = db.Column(db.Integer, db.ForeignKey("agent_run.id"))
    category = db.Column(db.String(30), nullable=False)
    description = db.Column(db.Text, nullable=False)
    internal_credits_delta = db.Column(db.Numeric(12, 2), default=0, nullable=False)
    real_cost_delta = db.Column(db.Numeric(12, 6), default=0, nullable=False)
    currency = db.Column(db.String(8), default="TWD", nullable=False)

class ContributionEvent(db.Model, TimestampMixin):
    id = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.Integer, db.ForeignKey("employee.id"), nullable=False)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"))
    scope = db.Column(db.String(20), nullable=False)
    event_type = db.Column(db.String(40), nullable=False)
    value = db.Column(db.Numeric(12, 2), nullable=False)
    reason = db.Column(db.Text, nullable=False)
    related_task_id = db.Column(db.Integer, db.ForeignKey("task.id"))
    related_reference = db.Column(db.String(200))
    status = db.Column(db.String(20), default="PROVISIONAL", nullable=False)

class EmployeeLearningRecord(db.Model, TimestampMixin):
    id = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.Integer, db.ForeignKey("employee.id"), nullable=False)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"))
    task_id = db.Column(db.Integer, db.ForeignKey("task.id"))
    title = db.Column(db.String(180), nullable=False)
    content = db.Column(db.Text, nullable=False)
    source_ref = db.Column(db.String(300))
    validated = db.Column(db.Boolean, default=False, nullable=False)

class KnowledgeItem(db.Model, TimestampMixin):
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"))
    kind = db.Column(db.String(20), nullable=False)
    title = db.Column(db.String(180), nullable=False)
    content = db.Column(db.Text, nullable=False)
    rationale = db.Column(db.Text)
    source_ref = db.Column(db.String(300))
    origin_employee_id = db.Column(db.Integer, db.ForeignKey("employee.id"))
    origin_agent_run_id = db.Column(db.Integer, db.ForeignKey("agent_run.id"))
    founder_approved = db.Column(db.Boolean, default=False, nullable=False)
    target_knowledge_id = db.Column(db.Integer, db.ForeignKey("knowledge_item.id"))

class KnowledgeReference(db.Model, TimestampMixin):
    id = db.Column(db.Integer, primary_key=True)
    from_knowledge_id = db.Column(db.Integer, db.ForeignKey("knowledge_item.id"), nullable=False)
    to_knowledge_id = db.Column(db.Integer, db.ForeignKey("knowledge_item.id"), nullable=False)
    relation_type = db.Column(db.String(30), nullable=False)

class Proposal(db.Model, TimestampMixin):
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"))
    agent_run_id = db.Column(db.Integer, db.ForeignKey("agent_run.id"), nullable=False)
    proposed_by_employee_id = db.Column(db.Integer, db.ForeignKey("employee.id"), nullable=False)
    payload_json = db.Column(db.JSON, nullable=False)
    status = db.Column(db.String(20), default="PENDING", nullable=False)
    review_note = db.Column(db.Text)
    corrected_payload_json = db.Column(db.JSON)
    materialized_knowledge_id = db.Column(db.Integer, db.ForeignKey("knowledge_item.id"))
    reviewed_at = db.Column(db.DateTime(timezone=True))

class FounderInterview(db.Model, TimestampMixin):
    id = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.Integer, db.ForeignKey("employee.id"), nullable=False)
    started_at = db.Column(db.DateTime(timezone=True), default=now, nullable=False)
    ended_at = db.Column(db.DateTime(timezone=True))
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"))
    employee = db.relationship("Employee")

class FounderInterviewMessage(db.Model, TimestampMixin):
    id = db.Column(db.Integer, primary_key=True)
    interview_id = db.Column(db.Integer, db.ForeignKey("founder_interview.id"), nullable=False)
    speaker = db.Column(db.String(20), nullable=False)
    content = db.Column(db.Text, nullable=False)
    interview = db.relationship("FounderInterview", backref="messages")

class Meeting(db.Model, TimestampMixin):
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=False)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"))
    title = db.Column(db.String(180), nullable=False)
    purpose = db.Column(db.Text, nullable=False)
    agenda = db.Column(db.Text, nullable=False)
    chair_employee_id = db.Column(db.Integer, db.ForeignKey("employee.id"), nullable=False)
    status = db.Column(db.String(30), default="PLANNED", nullable=False)
    current_round = db.Column(db.Integer, default=0, nullable=False)
    max_rounds = db.Column(db.Integer, default=3, nullable=False)
    token_limit = db.Column(db.Integer, default=12000, nullable=False)
    real_cost_limit_twd = db.Column(db.Numeric(12, 4), default=100, nullable=False)
    created_by = db.Column(db.String(20), default="FOUNDER", nullable=False)
    founder_joined_at = db.Column(db.DateTime(timezone=True))
    current_summary_json = db.Column(db.JSON)
    minutes_json = db.Column(db.JSON)
    started_at = db.Column(db.DateTime(timezone=True))
    ended_at = db.Column(db.DateTime(timezone=True))
    termination_reason = db.Column(db.Text)
    project = db.relationship("Project")
    chair = db.relationship("Employee")

class MeetingParticipant(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    meeting_id = db.Column(db.Integer, db.ForeignKey("meeting.id"), nullable=False)
    employee_id = db.Column(db.Integer, db.ForeignKey("employee.id"), nullable=False)
    role = db.Column(db.String(80))
    joined_at = db.Column(db.DateTime(timezone=True), default=now, nullable=False)
    removed_at = db.Column(db.DateTime(timezone=True))
    employee = db.relationship("Employee")
    meeting = db.relationship("Meeting", backref="participants")
    __table_args__=(db.UniqueConstraint("meeting_id","employee_id"),)

class MeetingMessage(db.Model, TimestampMixin):
    id = db.Column(db.Integer, primary_key=True)
    meeting_id = db.Column(db.Integer, db.ForeignKey("meeting.id"), nullable=False)
    employee_id = db.Column(db.Integer, db.ForeignKey("employee.id"))
    speaker_type = db.Column(db.String(20), nullable=False)
    round_number = db.Column(db.Integer, default=0, nullable=False)
    message_type = db.Column(db.String(40), nullable=False)
    content = db.Column(db.Text, nullable=False)
    agent_run_id = db.Column(db.Integer, db.ForeignKey("agent_run.id"))
    employee = db.relationship("Employee")
    meeting = db.relationship("Meeting", backref="messages")

class FounderFeedbackEvent(db.Model, TimestampMixin):
    id = db.Column(db.Integer, primary_key=True)
    meeting_id = db.Column(db.Integer, db.ForeignKey("meeting.id"), nullable=False)
    message_id = db.Column(db.Integer, db.ForeignKey("meeting_message.id"), nullable=False)
    employee_id = db.Column(db.Integer, db.ForeignKey("employee.id"), nullable=False)
    signal = db.Column(db.String(30), nullable=False)
    note = db.Column(db.Text)

``

---

## FILE: eason_one\__init__.py

``python
import os
from flask import Flask
from .extensions import db

def create_app(test_config=None):
    app = Flask(__name__)
    app.config.from_mapping(
        SECRET_KEY=os.getenv("SECRET_KEY", "dev-only-change-me"),
        SQLALCHEMY_DATABASE_URI=os.getenv("DATABASE_URL", "sqlite:///eason_one.db"),
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
    )
    if test_config:
        app.config.update(test_config)
    db.init_app(app)
    from .routes import bp
    app.register_blueprint(bp)
    from .i18n import translate
    app.jinja_env.globals["t"]=translate
    from .seed import seed_command
    app.cli.add_command(seed_command)
    with app.app_context():
        db.create_all()
        _upgrade_v1_database()
    return app

def _upgrade_v1_database():
    """Small SQLite compatibility migration for pre-Fix-002 local databases."""
    from sqlalchemy import inspect, text
    inspector=inspect(db.engine)
    if "model_config" in inspector.get_table_names():
        cols={x["name"] for x in inspector.get_columns("model_config")}
        if "max_output_tokens" not in cols: db.session.execute(text("ALTER TABLE model_config ADD COLUMN max_output_tokens INTEGER NOT NULL DEFAULT 1200"))
    if "agent_run" in inspector.get_table_names():
        cols={x["name"] for x in inspector.get_columns("agent_run")}
        additions={
          "provider_key_snapshot":"VARCHAR(40) NOT NULL DEFAULT 'legacy'",
          "model_name_snapshot":"VARCHAR(120) NOT NULL DEFAULT 'legacy'",
          "input_price_snapshot":"NUMERIC(12,4) NOT NULL DEFAULT 0",
          "output_price_snapshot":"NUMERIC(12,4) NOT NULL DEFAULT 0",
          "currency_snapshot":"VARCHAR(8) NOT NULL DEFAULT 'TWD'"}
        for name,definition in additions.items():
            if name not in cols: db.session.execute(text(f"ALTER TABLE agent_run ADD COLUMN {name} {definition}"))
        if "provider_response_id" not in cols: db.session.execute(text("ALTER TABLE agent_run ADD COLUMN provider_response_id VARCHAR(160)"))
        if "cache_creation_input_tokens" not in cols: db.session.execute(text("ALTER TABLE agent_run ADD COLUMN cache_creation_input_tokens INTEGER NOT NULL DEFAULT 0"))
        if "cache_read_input_tokens" not in cols: db.session.execute(text("ALTER TABLE agent_run ADD COLUMN cache_read_input_tokens INTEGER NOT NULL DEFAULT 0"))
    if "work_message" in inspector.get_table_names():
        cols={x["name"] for x in inspector.get_columns("work_message")}
        if "agent_run_id" not in cols: db.session.execute(text("ALTER TABLE work_message ADD COLUMN agent_run_id INTEGER REFERENCES agent_run(id)"))
    if "project" in inspector.get_table_names():
        cols={x["name"] for x in inspector.get_columns("project")}
        project_additions={"environment":"VARCHAR(20) NOT NULL DEFAULT 'SMOKE'","origin":"VARCHAR(20) NOT NULL DEFAULT 'NEW'",
          "current_state_summary":"TEXT","known_constraints":"TEXT","next_milestone":"TEXT"}
        for name,definition in project_additions.items():
            if name not in cols: db.session.execute(text(f"ALTER TABLE project ADD COLUMN {name} {definition}"))
    if "agent_run" in inspector.get_table_names():
        cols={x["name"] for x in inspector.get_columns("agent_run")}
        if "meeting_id" not in cols: db.session.execute(text("ALTER TABLE agent_run ADD COLUMN meeting_id INTEGER REFERENCES meeting(id)"))
    db.session.commit()

``

---

## FILE: eason_one\services\model_configs.py

``python
from decimal import Decimal
from ..extensions import db
from ..models import ModelConfig
from .company import get_company
PROVIDERS={"mock","openai","anthropic"}
def validate(provider_key,input_price,output_price,currency,max_output_tokens):
    ip,op=Decimal(input_price),Decimal(output_price)
    if provider_key not in PROVIDERS: raise ValueError("Unsupported provider")
    if ip<0 or op<0: raise ValueError("Prices cannot be negative")
    if int(max_output_tokens)<=0: raise ValueError("Maximum output tokens must be positive")
    if provider_key!="mock":
        if ip<=0 or op<=0: raise ValueError("Real provider prices must both be greater than zero")
        if currency!=get_company().currency: raise ValueError("Real provider currency must match company currency")
    return ip,op
def create(label,provider_key,model_name,input_price,output_price,currency,max_output_tokens):
    if not label.strip() or not model_name.strip(): raise ValueError("Label and model name are required")
    currency=currency.upper(); ip,op=validate(provider_key,input_price,output_price,currency,max_output_tokens)
    row=ModelConfig(label=label.strip(),provider_key=provider_key,model_name=model_name.strip(),input_price_per_million=ip,
      output_price_per_million=op,currency=currency,max_output_tokens=int(max_output_tokens),active=True)
    db.session.add(row); db.session.commit(); return row
def toggle(model):
    model.active=not model.active; db.session.commit(); return model

``

---

## FILE: eason_one\services\execution.py

``python
from ..extensions import db
from ..models import AgentRun, now
from ..providers import get_provider
from .context import build
from .costs import ensure_budget, calculate, record, estimate_execution
def execute(employee, purpose, user_request, project=None, task=None, context_override=None, postprocess=None, system_prompt_override=None,response_schema=None,meeting=None):
    if not employee.current_model: raise ValueError("Employee has no model")
    if not employee.current_model.active: raise ValueError("Employee's current ModelConfig is inactive")
    context=context_override if context_override is not None else build(employee,project,task)
    model=employee.current_model
    system_prompt=system_prompt_override if system_prompt_override is not None else employee.system_instructions
    company=__import__("eason_one.services.company",fromlist=["get_company"]).get_company()
    if model.provider_key!="mock":
        if model.input_price_per_million<=0 or model.output_price_per_million<=0: raise ValueError("Real provider prices must both be greater than zero")
        if model.currency!=company.currency: raise ValueError("Paid model currency must match company budget currency")
        if model.max_output_tokens<=0: raise ValueError("Maximum output tokens must be positive")
    elif (model.input_price_per_million or model.output_price_per_million) and model.currency!=company.currency:
        raise ValueError("Paid model currency must match company budget currency")
    estimate=estimate_execution(model,system_prompt,context,user_request,model.max_output_tokens,response_schema)
    ensure_budget(estimate.real_cost)
    run=AgentRun(employee_id=employee.id,project_id=getattr(project,"id",None),task_id=getattr(task,"id",None),meeting_id=getattr(meeting,"id",None),
        model_config_id=model.id,purpose=purpose,user_request=user_request,
        system_prompt_snapshot=system_prompt,context_snapshot=context,
        provider_key_snapshot=model.provider_key,model_name_snapshot=model.model_name,
        input_price_snapshot=model.input_price_per_million,output_price_snapshot=model.output_price_per_million,
        currency_snapshot=model.currency,currency=model.currency)
    db.session.add(run); db.session.commit()
    try:
        provider=get_provider(model.provider_key)
        result=(provider.complete(model,system_prompt,user_request,context,model.max_output_tokens,response_schema)
          if response_schema is not None else provider.complete(model,system_prompt,user_request,context,model.max_output_tokens))
        run.raw_output=result.text; run.input_tokens=result.input_tokens; run.output_tokens=result.output_tokens
        run.cache_creation_input_tokens=result.cache_creation_input_tokens
        run.cache_read_input_tokens=result.cache_read_input_tokens
        run.provider_request_id=result.request_id; run.provider_response_id=result.response_id
        provider_name=model.provider_key
        if result.cache_creation_input_tokens or result.cache_read_input_tokens:
            run.real_cost=None
            run.status="FAILED"; run.error_text=(f"{provider_name} returned unexpected prompt-cache usage; "
              "exact cache-cost reconciliation is not enabled")
        else:
            run.real_cost=calculate(model,result.input_tokens,result.output_tokens)
            if result.refusal:
                run.status="FAILED"; run.error_text=f"{provider_name} refusal: {result.refusal}"
            elif result.status!="completed":
                detail=f": {result.incomplete_reason}" if result.incomplete_reason else ""
                run.status="FAILED"; run.error_text=f"{provider_name} response {result.status}{detail}"
            else: run.status="SUCCEEDED"
            record(run)
        run.finished_at=now(); db.session.commit()
        if run.status=="FAILED": return run
        if postprocess:
            try: postprocess(run)
            except Exception as exc:
                run.error_text=f"Downstream processing failed: {exc}"; db.session.commit(); raise
    except Exception as exc:
        run.status="FAILED"; run.error_text=str(exc); run.finished_at=now(); db.session.commit(); raise
    return run

``

---

## FILE: eason_one\services\costs.py

``python
from decimal import Decimal
from dataclasses import dataclass
import json
from ..extensions import db
from ..models import CostEvent
from .company import get_company, remaining

@dataclass(frozen=True)
class ExecutionEstimate:
    framed_text: str
    input_tokens: int
    output_tokens: int
    real_cost: Decimal

def calculate(model, input_tokens, output_tokens):
    return (Decimal(input_tokens)*Decimal(model.input_price_per_million)+Decimal(output_tokens)*Decimal(model.output_price_per_million))/Decimal(1_000_000)
def ensure_budget(estimated=Decimal("0")):
    if remaining() <= 0 or estimated > remaining(): raise ValueError("Company real budget is exhausted")
def conservative_estimate(model, prompt_text, max_output_tokens):
    # UTF-8 bytes plus 25% and fixed framing overhead deliberately overestimate.
    byte_bound=len(prompt_text.encode("utf-8"))
    estimated_input=max(1, (byte_bound*5+3)//4 + 256)
    return calculate(model,estimated_input,max_output_tokens)

def execution_frame(system_prompt,context,user_request):
    return f"INSTRUCTIONS:\n{system_prompt}\n\nCONTEXT:\n{context}\n\nUSER REQUEST:\n{user_request}"

STRUCTURED_OUTPUT_OVERHEAD_TOKENS=512

def estimate_execution(model,system_prompt,context,user_request,max_output_tokens=None,response_schema=None):
    output_tokens=model.max_output_tokens if max_output_tokens is None else int(max_output_tokens)
    framed=execution_frame(system_prompt,context,user_request)
    schema_tokens=0
    if response_schema is not None:
        serialized=json.dumps(response_schema,sort_keys=True,separators=(",",":"),ensure_ascii=False)
        framed+=f"\n\nRESPONSE SCHEMA:\n{serialized}"
        schema_tokens=STRUCTURED_OUTPUT_OVERHEAD_TOKENS
    byte_bound=len(framed.encode("utf-8"))
    input_tokens=max(1,(byte_bound*5+3)//4+256+schema_tokens)
    return ExecutionEstimate(framed,input_tokens,output_tokens,calculate(model,input_tokens,output_tokens))
def record(run):
    existing=CostEvent.query.filter_by(agent_run_id=run.id,category="MODEL").first()
    if existing: return existing
    event=CostEvent(company_id=get_company().id, employee_id=run.employee_id, project_id=run.project_id,
        task_id=run.task_id, agent_run_id=run.id, category="MODEL", description=f"{run.purpose}: {run.model_config.label}",
        internal_credits_delta=0, real_cost_delta=run.real_cost or 0, currency=run.currency)
    db.session.add(event); return event

``

---

## FILE: eason_one\services\meetings.py

``python
from decimal import Decimal
import json
from sqlalchemy import func
from ..extensions import db
from ..models import (Meeting,MeetingParticipant,MeetingMessage,FounderFeedbackEvent,Employee,
    AgentRun,CostEvent,ContributionEvent,now)
from .company import get_company,remaining
from .costs import estimate_execution
from .execution import execute
from .brain import current as current_knowledge
from ..schemas import MEETING_SYNTHESIS_FIELDS,MEETING_SYNTHESIS_SCHEMA

ACTIVE="ACTIVE"
TERMINAL={"ENDED","TERMINATED_BY_FOUNDER"}
SIGNALS={"VALUABLE","LOW_VALUE","KEY_INSIGHT","UNSUPPORTED","WASTEFUL","CRITICAL_CATCH"}

def create(title,purpose,agenda,chair,participants,project=None,max_rounds=3,token_limit=12000,real_cost_limit_twd=100):
    if not title.strip() or not purpose.strip() or not agenda.strip(): raise ValueError("Meeting title, purpose, and agenda are required")
    if not chair.id or db.session.get(Employee,chair.id) is not chair: raise ValueError("Meeting chair must be a valid persisted Employee")
    if any(not e.id or db.session.get(Employee,e.id) is not e for e in participants): raise ValueError("Only valid persisted Employees may participate")
    unique={e.id:e for e in participants}
    if chair.id not in unique: unique[chair.id]=chair
    if any(not e.active for e in unique.values()): raise ValueError("Only active Employees may participate")
    if int(max_rounds)<=0 or int(token_limit)<=0 or Decimal(real_cost_limit_twd)<0: raise ValueError("Invalid Meeting limits")
    meeting=Meeting(company_id=get_company().id,project_id=getattr(project,"id",None),title=title.strip(),purpose=purpose.strip(),
      agenda=agenda.strip(),chair_employee_id=chair.id,max_rounds=int(max_rounds),token_limit=int(token_limit),
      real_cost_limit_twd=Decimal(real_cost_limit_twd),status="PLANNED")
    db.session.add(meeting); db.session.flush()
    for employee in unique.values():
        db.session.add(MeetingParticipant(meeting_id=meeting.id,employee_id=employee.id,role="CHAIR" if employee.id==chair.id else "PARTICIPANT"))
    db.session.commit(); return meeting

def start(meeting):
    if meeting.status!="PLANNED": raise ValueError("Only PLANNED Meetings may start")
    meeting.status=ACTIVE; meeting.started_at=now()
    db.session.add(MeetingMessage(meeting_id=meeting.id,speaker_type="SYSTEM",round_number=0,message_type="SYSTEM",content="Meeting started. Founder is observing."))
    db.session.commit(); return meeting

def usage(meeting):
    tokens=db.session.query(func.coalesce(func.sum(
      AgentRun.input_tokens+AgentRun.output_tokens+
      AgentRun.cache_creation_input_tokens+AgentRun.cache_read_input_tokens),0)).filter_by(meeting_id=meeting.id).scalar()
    cost=db.session.query(func.coalesce(func.sum(CostEvent.real_cost_delta),0)).join(AgentRun,CostEvent.agent_run_id==AgentRun.id).filter(AgentRun.meeting_id==meeting.id).scalar()
    return int(tokens or 0),Decimal(cost or 0)

def compact_context(meeting,employee):
    previous=max(0,meeting.current_round)
    recent=MeetingMessage.query.filter_by(meeting_id=meeting.id,round_number=previous).order_by(MeetingMessage.id.desc()).limit(6).all()
    founder=MeetingMessage.query.filter_by(meeting_id=meeting.id,speaker_type="FOUNDER").order_by(MeetingMessage.id.desc()).limit(5).all()
    controls=MeetingMessage.query.filter(MeetingMessage.meeting_id==meeting.id,MeetingMessage.message_type.in_(["FOCUS","REQUEST_EVIDENCE"])).order_by(MeetingMessage.id.desc()).limit(3).all()
    parts=[f"MEETING\n{meeting.title}\nPurpose: {meeting.purpose}\nAgenda: {meeting.agenda}",
      f"ROUND PROTOCOL\nNext round: {meeting.current_round+1}. Round 1: POSITION, BASIS, RISK only. Later rounds: contribute only disagreement, counterevidence, new information, or material refinement.",
      f"EMPLOYEE\n{employee.name}; role: {employee.role_description}"]
    if meeting.project:
        parts.append(
          f"PROJECT #{meeting.project.id}\n{meeting.project.name}; status: {meeting.project.status}; "
          f"origin: {meeting.project.origin}; current state: {meeting.project.current_state_summary or 'Not recorded'}; "
          f"constraints: {meeting.project.known_constraints or 'None recorded'}")
    visible=current_knowledge(meeting.project_id) if meeting.project_id else current_knowledge(None)
    knowledge=list({item.id:item for item in visible}.values())[:12]
    if knowledge:
        parts.append("RELEVANT COMPANY BRAIN IDS\n"+"\n".join(
          f"#{item.id} [{item.kind}] {item.title}" for item in knowledge))
    if meeting.current_summary_json: parts.append("PREVIOUS ROUND SUMMARY\n"+str(meeting.current_summary_json))
    if recent: parts.append("RECENT RELEVANT DISCUSSION\n"+"\n".join(f"{m.message_type}: {m.content}" for m in reversed(recent)))
    if controls: parts.append("FOUNDER COMMANDS\n"+"\n".join(m.content for m in reversed(controls)))
    if founder: parts.append("FOUNDER INTERVENTIONS\n"+"\n".join(m.content for m in reversed(founder)))
    return "\n\n".join(parts)

def _check_step(meeting,employee,system_prompt,context,user_request,response_schema=None,allow_final=False):
    if meeting.status!=ACTIVE: raise ValueError("Meeting is not ACTIVE")
    if not allow_final and meeting.current_round>=meeting.max_rounds: raise ValueError("Meeting maximum rounds reached")
    tokens,cost=usage(meeting)
    estimate=estimate_execution(employee.current_model,system_prompt,context,user_request,response_schema=response_schema)
    if tokens+estimate.input_tokens+estimate.output_tokens>meeting.token_limit: raise ValueError("Meeting token limit would be exceeded")
    if cost+estimate.real_cost>Decimal(meeting.real_cost_limit_twd): raise ValueError("Meeting real-cost limit would be exceeded")
    if estimate.real_cost>remaining(): raise ValueError("Company real budget would be exceeded")

def _derive_summary(meeting):
    messages=MeetingMessage.query.filter_by(meeting_id=meeting.id).order_by(MeetingMessage.id.desc()).limit(12).all()
    employee=[m for m in messages if m.speaker_type=="EMPLOYEE"]
    summary={"current_topic":meeting.agenda,"agreement":employee[0].content if employee else "No positions yet.",
      "disagreement":"Unresolved disagreement remains." if meeting.current_round>1 else "None recorded.",
      "evidence":"Participants must distinguish evidence from reasoning.","pending_question":"What requires Founder decision?",
      "next_likely_action":"Advance the bounded round or end and synthesize.",
      "founder_decision_required":"Review final recommendations."}
    meeting.current_summary_json=summary; return summary

def advance(meeting):
    if meeting.status!=ACTIVE: raise ValueError("Meeting is not ACTIVE")
    if meeting.current_round>=meeting.max_rounds: raise ValueError("Meeting maximum rounds reached")
    next_round=meeting.current_round+1
    for participant in [p for p in meeting.participants if p.removed_at is None]:
        employee=participant.employee
        completed=MeetingMessage.query.join(AgentRun,MeetingMessage.agent_run_id==AgentRun.id).filter(
          MeetingMessage.meeting_id==meeting.id,MeetingMessage.round_number==next_round,
          MeetingMessage.employee_id==employee.id,MeetingMessage.speaker_type=="EMPLOYEE",
          AgentRun.status=="SUCCEEDED").first()
        if completed: continue
        context=compact_context(meeting,employee)
        prompt=employee.system_instructions+"\nMEETING_CONTRIBUTION\nBe compact. Follow the round protocol. Do not mutate authoritative company state."
        user_request=f"Contribute to round {next_round}"
        _check_step(meeting,employee,prompt,context,user_request)
        run=execute(employee,"MEETING_CONTRIBUTION",user_request,meeting.project,
          context_override=context,system_prompt_override=prompt,meeting=meeting)
        if run.status!="SUCCEEDED": raise ValueError(run.error_text or "Meeting contribution failed")
        msg_type="POSITION" if next_round==1 else "MATERIAL_REFINEMENT"
        db.session.add(MeetingMessage(meeting_id=meeting.id,employee_id=employee.id,speaker_type="EMPLOYEE",
          round_number=next_round,message_type=msg_type,content=run.raw_output,agent_run_id=run.id))
        db.session.commit()
    meeting.current_round=next_round; _derive_summary(meeting); db.session.commit(); return meeting

def join_founder(meeting):
    if meeting.status!=ACTIVE: raise ValueError("Meeting is not ACTIVE")
    if not meeting.founder_joined_at:
        meeting.founder_joined_at=now()
        db.session.add(MeetingMessage(meeting_id=meeting.id,speaker_type="SYSTEM",round_number=meeting.current_round,
          message_type="SYSTEM",content="Founder joined the Meeting."))
        db.session.commit()
    return meeting

def intervene(meeting,content):
    if meeting.status!=ACTIVE: raise ValueError("Meeting is not ACTIVE")
    if not meeting.founder_joined_at: raise ValueError("Founder must join before intervening")
    msg=MeetingMessage(meeting_id=meeting.id,speaker_type="FOUNDER",round_number=meeting.current_round,
      message_type="FOUNDER_INTERVENTION",content=content.strip())
    db.session.add(msg); db.session.commit(); return msg

def command(meeting,kind,content):
    if meeting.status!=ACTIVE: raise ValueError("Meeting is not ACTIVE")
    if kind not in {"FOCUS","REQUEST_EVIDENCE"}: raise ValueError("Invalid Meeting command")
    msg=MeetingMessage(meeting_id=meeting.id,speaker_type="FOUNDER",round_number=meeting.current_round,message_type=kind,content=content.strip())
    db.session.add(msg); db.session.commit(); return msg

def stop(meeting,reason=None):
    if meeting.status!=ACTIVE: raise ValueError("Meeting is not ACTIVE")
    meeting.status="TERMINATED_BY_FOUNDER"; meeting.termination_reason=reason; meeting.ended_at=now()
    db.session.add(MeetingMessage(meeting_id=meeting.id,speaker_type="SYSTEM",round_number=meeting.current_round,message_type="SYSTEM",content="Meeting terminated by Founder."))
    db.session.commit(); return meeting

def end_and_synthesize(meeting):
    if meeting.status!=ACTIVE: raise ValueError("Meeting is not ACTIVE")
    context=compact_context(meeting,meeting.chair)
    prompt=meeting.chair.system_instructions+"\nMEETING_SYNTHESIS\nSummarize agreements, disagreements, evidence, unresolved issues, actions, and Founder decisions required."
    user_request="Produce final Meeting synthesis"
    _check_step(meeting,meeting.chair,prompt,context,user_request,response_schema=MEETING_SYNTHESIS_SCHEMA,allow_final=True)
    run=execute(meeting.chair,"MEETING_SYNTHESIS",user_request,meeting.project,
      context_override=context,system_prompt_override=prompt,response_schema=MEETING_SYNTHESIS_SCHEMA,meeting=meeting)
    if run.status!="SUCCEEDED": raise ValueError(run.error_text or "Meeting synthesis failed")
    try:
        synthesis=json.loads(run.raw_output)
        expected=set(MEETING_SYNTHESIS_FIELDS)
        if not isinstance(synthesis,dict) or set(synthesis)!=expected: raise ValueError("unexpected fields")
        for field in expected:
            values=synthesis[field]
            if not isinstance(values,list) or len(values)>8 or any(not isinstance(x,str) or not x.strip() or len(x)>500 for x in values):
                raise ValueError(f"invalid {field}")
        run.parsed_output_json=synthesis
        db.session.commit()
    except Exception as exc:
        run.status="FAILED"; run.error_text=f"Meeting synthesis validation failed: {exc}"
        db.session.commit()
        raise ValueError(run.error_text)
    db.session.add(MeetingMessage(meeting_id=meeting.id,employee_id=meeting.chair.id,speaker_type="EMPLOYEE",
      round_number=meeting.current_round,message_type="CHAIR_SYNTHESIS",content=run.raw_output,agent_run_id=run.id))
    tokens,cost=usage(meeting)
    founder_messages=MeetingMessage.query.filter_by(meeting_id=meeting.id,speaker_type="FOUNDER",message_type="FOUNDER_INTERVENTION").all()
    meeting.minutes_json={"purpose":meeting.purpose,"participants":[p.employee.name for p in meeting.participants],
      **synthesis,"founder_interventions":[m.content for m in founder_messages],
      "token_usage":tokens,"real_cost_twd":str(cost)}
    meeting.status="ENDED"; meeting.ended_at=now(); _derive_summary(meeting); db.session.commit(); return meeting

def feedback(message,signal,note=None):
    if signal not in SIGNALS or message.speaker_type!="EMPLOYEE" or not message.employee_id: raise ValueError("Invalid feedback")
    row=FounderFeedbackEvent(meeting_id=message.meeting_id,message_id=message.id,employee_id=message.employee_id,signal=signal,note=note)
    db.session.add(row); db.session.commit(); return row

``

---

## FILE: tests\test_slice006.py

``python
import json
import os
from decimal import Decimal
from types import SimpleNamespace

import pytest

from eason_one.extensions import db
from eason_one.models import AgentRun, CostEvent, Employee, MeetingMessage, ModelConfig
from eason_one.providers import AnthropicProvider, anthropic_compatible_schema
from eason_one.schemas import (
    CEO_SCHEMA, MEETING_SYNTHESIS_SCHEMA, REVIEW_SCHEMA, SYNTHESIS_SCHEMA,
    TASK_EXECUTION_SCHEMA,
)
from eason_one.services import meetings
from eason_one.services.ceo import founder_request
from eason_one.services.execution import execute
from eason_one.services.model_configs import create as create_model


def anthropic_model(**overrides):
    values = {
        "label": "Claude Test", "provider_key": "anthropic",
        "model_name": "claude-test-exact", "input_price": "10",
        "output_price": "30", "currency": "TWD", "max_output_tokens": 500,
    }
    values.update(overrides)
    return create_model(**values)


def assign(employee=None, **overrides):
    employee = employee or Employee.query.filter_by(slug="ceo").one()
    employee.current_model = anthropic_model(**overrides)
    db.session.commit()
    return employee


def message(text="provider text", stop_reason="end_turn", input_tokens=12,
            output_tokens=4, cache_creation=0, cache_read=0):
    return SimpleNamespace(
        id="msg_anthropic_123", _request_id="req_anthropic_456",
        content=[
            SimpleNamespace(type="text", text=text),
            SimpleNamespace(type="tool_use", name="not-enabled", input={"ignored": True}),
        ],
        stop_reason=stop_reason,
        usage=SimpleNamespace(
            input_tokens=input_tokens, output_tokens=output_tokens,
            cache_creation_input_tokens=cache_creation,
            cache_read_input_tokens=cache_read,
        ),
    )


def fake_sdk(monkeypatch, response=None, error=None):
    captured = {}

    class Messages:
        def create(self, **kwargs):
            captured["kwargs"] = kwargs
            if error:
                raise error
            return response or message()

    class Client:
        def __init__(self, **kwargs):
            captured["client"] = kwargs
            self.messages = Messages()

    monkeypatch.setattr("anthropic.Anthropic", Client)
    return captured


def test_dependency_and_model_service_accept_anthropic(ctx):
    assert "anthropic>=0.117.0,<1" in open("pyproject.toml", encoding="utf-8").read()
    model = anthropic_model()
    assert model.provider_key == "anthropic" and model.model_name == "claude-test-exact"


@pytest.mark.parametrize(("field", "value"), [
    ("input_price", "0"), ("output_price", "0"),
])
def test_anthropic_zero_price_rejected(ctx, field, value):
    with pytest.raises(ValueError, match="greater than zero"):
        anthropic_model(**{field: value})


def test_anthropic_wrong_currency_rejected(ctx):
    with pytest.raises(ValueError, match="currency"):
        anthropic_model(currency="USD")


def test_inactive_anthropic_model_rejected_before_provider(ctx, monkeypatch):
    employee = assign()
    employee.current_model.active = False
    db.session.commit()
    called = []
    monkeypatch.setattr("eason_one.services.execution.get_provider", lambda key: called.append(key))
    with pytest.raises(ValueError, match="inactive"):
        execute(employee, "TEST", "x")
    assert called == [] and AgentRun.query.count() == 0


def test_messages_api_exact_mapping_and_environment_key(ctx, monkeypatch):
    model = anthropic_model(model_name="claude-sonnet-exact-id", max_output_tokens=777)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "environment-only-test-key")
    captured = fake_sdk(monkeypatch)
    result = AnthropicProvider().complete(model, "SYSTEM RULES", "FOUNDER REQUEST", "EFFECTIVE CONTEXT", 777)
    kwargs = captured["kwargs"]
    assert captured["client"] == {"api_key": "environment-only-test-key", "max_retries": 0}
    assert kwargs["model"] == "claude-sonnet-exact-id"
    assert kwargs["max_tokens"] == 777
    assert kwargs["system"] == "SYSTEM RULES"
    assert kwargs["messages"] == [{"role": "user", "content": "EFFECTIVE CONTEXT\n\nUSER REQUEST:\nFOUNDER REQUEST"}]
    assert "system" not in kwargs["messages"][0]
    assert result.text == "provider text"  # unrelated tool block was not concatenated


def test_anthropic_key_is_required_from_environment(ctx, monkeypatch):
    model = anthropic_model()
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    called = []
    monkeypatch.setattr("anthropic.Anthropic", lambda **kwargs: called.append(kwargs))
    with pytest.raises(KeyError, match="ANTHROPIC_API_KEY"):
        AnthropicProvider().complete(model, "s", "u", "c", 10)
    assert called == []


@pytest.mark.parametrize("contract", [
    CEO_SCHEMA, REVIEW_SCHEMA, SYNTHESIS_SCHEMA, TASK_EXECUTION_SCHEMA,
    MEETING_SYNTHESIS_SCHEMA,
])
def test_all_structured_contracts_convert_for_anthropic(ctx, monkeypatch, contract):
    model = anthropic_model()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
    captured = fake_sdk(monkeypatch)
    original = json.loads(json.dumps(contract["schema"]))
    AnthropicProvider().complete(model, "s", "u", "c", 10, contract)
    sent = captured["kwargs"]["output_config"]["format"]
    assert sent["type"] == "json_schema"
    assert sent["schema"]["type"] == original["type"]
    assert sent["schema"]["required"] == original["required"]
    assert sent["schema"]["properties"].keys() == original["properties"].keys()
    assert contract["schema"] == original  # adapter never weakens the application contract


def test_schema_adapter_removes_only_provider_unsupported_assertions():
    source = {"type": "object", "required": ["items"], "additionalProperties": False,
              "properties": {"items": {"type": "array", "maxItems": 2,
                                       "items": {"type": "string", "maxLength": 5}}}}
    converted = anthropic_compatible_schema(source)
    assert converted["required"] == ["items"]
    assert converted["additionalProperties"] is False
    assert converted["properties"]["items"]["items"]["type"] == "string"
    assert "maxItems" not in converted["properties"]["items"]
    assert "maxLength" not in converted["properties"]["items"]["items"]
    assert source["properties"]["items"]["maxItems"] == 2


def test_application_validation_still_rejects_invalid_anthropic_content(ctx, monkeypatch):
    employee = assign()
    invalid = json.dumps({"mode": "NOT_ALLOWED", "executive_response": "x"})
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
    fake_sdk(monkeypatch, message(invalid))
    run, proposal = founder_request(employee, "status")
    assert proposal is None
    assert "Invalid CEO request mode" in run.error_text
    assert run.parsed_output_json is None


def test_response_ids_usage_and_normal_completion_persist(ctx, monkeypatch):
    employee = assign()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
    fake_sdk(monkeypatch, message("ok", input_tokens=19, output_tokens=7))
    run = execute(employee, "TEST", "request")
    assert run.status == "SUCCEEDED"
    assert run.provider_response_id == "msg_anthropic_123"
    assert run.provider_request_id == "req_anthropic_456"
    assert (run.input_tokens, run.output_tokens) == (19, 7)
    assert (run.cache_creation_input_tokens, run.cache_read_input_tokens) == (0, 0)
    assert CostEvent.query.filter_by(agent_run_id=run.id).count() == 1


@pytest.mark.parametrize(("stop_reason", "text", "error_fragment"), [
    ("max_tokens", "partial", "response incomplete: max_tokens"),
    ("refusal", "I cannot comply", "refusal: I cannot comply"),
])
def test_billable_anthropic_non_normal_ledgers_exactly_once(
        ctx, monkeypatch, stop_reason, text, error_fragment):
    employee = assign()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
    fake_sdk(monkeypatch, message(text, stop_reason, input_tokens=20, output_tokens=5))
    run = execute(employee, "TEST", "request")
    assert run.status == "FAILED" and error_fragment in run.error_text
    assert (run.input_tokens, run.output_tokens) == (20, 5)
    assert run.real_cost == Decimal("0.000350")
    assert CostEvent.query.filter_by(agent_run_id=run.id).count() == 1


def test_transport_exception_creates_no_fake_cost(ctx, monkeypatch):
    employee = assign()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
    fake_sdk(monkeypatch, error=RuntimeError("transport unavailable"))
    with pytest.raises(RuntimeError, match="transport unavailable"):
        execute(employee, "TEST", "request")
    run = AgentRun.query.one()
    assert run.status == "FAILED"
    assert CostEvent.query.filter_by(agent_run_id=run.id).count() == 0


def test_unexpected_cache_usage_is_explicit_and_not_claimed_success(ctx, monkeypatch):
    employee = assign()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
    fake_sdk(monkeypatch, message("ok", cache_creation=3, cache_read=2))
    run = execute(employee, "TEST", "request")
    assert run.status == "FAILED"
    assert "exact cache-cost reconciliation is not enabled" in run.error_text
    assert (run.cache_creation_input_tokens, run.cache_read_input_tokens) == (3, 2)
    assert run.real_cost is None
    assert CostEvent.query.filter_by(agent_run_id=run.id).count() == 0


def test_anthropic_run_snapshots_are_immutable(ctx, monkeypatch):
    employee = assign(model_name="claude-original", input_price="11", output_price="33")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
    fake_sdk(monkeypatch)
    run = execute(employee, "TEST", "request")
    employee.current_model.model_name = "claude-changed"
    employee.current_model.input_price_per_million = 99
    employee.current_model.provider_key = "mock"
    db.session.commit()
    assert run.provider_key_snapshot == "anthropic"
    assert run.model_name_snapshot == "claude-original"
    assert run.input_price_snapshot == Decimal("11.0000")
    assert run.output_price_snapshot == Decimal("33.0000")
    assert run.currency_snapshot == "TWD"


def test_anthropic_participant_flows_through_meeting_ledger(ctx, monkeypatch):
    employee = assign(Employee.query.filter_by(slug="researcher").one())
    meeting = meetings.create("Claude review", "Test provider neutrality", "One bounded position",
                              employee, [employee], token_limit=10000, real_cost_limit_twd=10)
    meetings.start(meeting)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
    fake_sdk(monkeypatch, message("Anthropic participant contribution", input_tokens=25, output_tokens=6))
    meetings.advance(meeting)
    linked = MeetingMessage.query.filter_by(
        meeting_id=meeting.id, employee_id=employee.id, round_number=1).one()
    run = db.session.get(AgentRun, linked.agent_run_id)
    assert run.provider_key_snapshot == "anthropic"
    assert meetings.usage(meeting) == (31, run.real_cost)


def test_meeting_ceiling_blocks_anthropic_before_invocation(ctx, monkeypatch):
    employee = assign()
    meeting = meetings.create("Ceiling", "Reject before provider", "Bounded",
                              employee, [employee], token_limit=1, real_cost_limit_twd=10)
    meetings.start(meeting)
    called = []
    monkeypatch.setattr("eason_one.services.execution.get_provider", lambda key: called.append(key))
    with pytest.raises(ValueError, match="token limit"):
        meetings.advance(meeting)
    assert called == [] and AgentRun.query.filter_by(meeting_id=meeting.id).count() == 0

``

---

## FILE: tests\test_patch0061a.py

``python
from decimal import Decimal
from types import SimpleNamespace

import pytest

from eason_one.extensions import db
from eason_one.models import AgentRun, CostEvent, Employee
from eason_one.providers import AnthropicProvider, OpenAIProvider
from eason_one.schemas import MEETING_SYNTHESIS_SCHEMA
from eason_one.services import meetings
from eason_one.services.costs import estimate_execution
from eason_one.services.execution import execute
from eason_one.services.model_configs import create as create_model


def anthropic_employee():
    employee = Employee.query.filter_by(slug="ceo").one()
    model = create_model("Anthropic gate", "anthropic", "claude-gate-test",
                         "1000", "2000", "TWD", 500)
    employee.current_model = model
    db.session.commit()
    return employee


def test_real_sdk_clients_disable_hidden_retries(monkeypatch):
    openai_args = {}
    anthropic_args = {}

    openai_response = SimpleNamespace(
        output_text="ok", usage=SimpleNamespace(input_tokens=1, output_tokens=1),
        id="openai-id", _request_id="openai-request", status="completed",
        output=[], incomplete_details=None,
    )

    class OpenAIClient:
        def __init__(self, **kwargs):
            openai_args.update(kwargs)
            self.responses = SimpleNamespace(create=lambda **kwargs: openai_response)

    anthropic_response = SimpleNamespace(
        id="anthropic-id", _request_id="anthropic-request", stop_reason="end_turn",
        content=[SimpleNamespace(type="text", text="ok")],
        usage=SimpleNamespace(input_tokens=1, output_tokens=1,
                              cache_creation_input_tokens=0, cache_read_input_tokens=0),
    )

    class AnthropicClient:
        def __init__(self, **kwargs):
            anthropic_args.update(kwargs)
            self.messages = SimpleNamespace(create=lambda **kwargs: anthropic_response)

    monkeypatch.setattr("openai.OpenAI", OpenAIClient)
    monkeypatch.setattr("anthropic.Anthropic", AnthropicClient)
    monkeypatch.setenv("OPENAI_API_KEY", "fake-openai")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-anthropic")
    model = SimpleNamespace(model_name="exact-model")
    OpenAIProvider().complete(model, "s", "u", "c", 10)
    AnthropicProvider().complete(model, "s", "u", "c", 10)
    assert openai_args == {"api_key": "fake-openai", "max_retries": 0}
    assert anthropic_args == {"api_key": "fake-anthropic", "max_retries": 0}


def test_schema_aware_execution_estimate_is_canonical(ctx):
    employee = anthropic_employee()
    without = estimate_execution(employee.current_model, "s", "c", "u")
    with_schema = estimate_execution(
        employee.current_model, "s", "c", "u",
        response_schema=MEETING_SYNTHESIS_SCHEMA,
    )
    assert with_schema.input_tokens > without.input_tokens
    assert with_schema.real_cost > without.real_cost
    assert "RESPONSE SCHEMA:" in with_schema.framed_text


@pytest.mark.parametrize("ceiling", ["cost", "token"])
def test_schema_aware_meeting_ceiling_rejects_before_provider(ctx, monkeypatch, ceiling):
    employee = anthropic_employee()
    meeting = meetings.create("Schema gate", "Bound synthesis", "Synthesize",
                              employee, [employee], token_limit=100000,
                              real_cost_limit_twd=100000)
    meetings.start(meeting)
    context = meetings.compact_context(meeting, employee)
    prompt = employee.system_instructions + (
        "\nMEETING_SYNTHESIS\nSummarize agreements, disagreements, evidence, "
        "unresolved issues, actions, and Founder decisions required.")
    request = "Produce final Meeting synthesis"
    without = estimate_execution(employee.current_model, prompt, context, request)
    with_schema = estimate_execution(
        employee.current_model, prompt, context, request,
        response_schema=MEETING_SYNTHESIS_SCHEMA,
    )
    if ceiling == "cost":
        meeting.real_cost_limit_twd = (without.real_cost + with_schema.real_cost) / 2
    else:
        meeting.token_limit = (
            without.input_tokens + without.output_tokens +
            with_schema.input_tokens + with_schema.output_tokens
        ) // 2
    db.session.commit()
    calls = []
    monkeypatch.setattr("eason_one.services.execution.get_provider",
                        lambda key: calls.append(key))
    with pytest.raises(ValueError, match=ceiling):
        meetings.end_and_synthesize(meeting)
    assert calls == []
    assert AgentRun.query.filter_by(meeting_id=meeting.id).count() == 0


def test_execute_company_gate_uses_schema_aware_estimate(ctx, monkeypatch):
    employee = anthropic_employee()
    schema_estimate = estimate_execution(
        employee.current_model, employee.system_instructions, "", "request",
        response_schema=MEETING_SYNTHESIS_SCHEMA,
    )
    captured = {}
    monkeypatch.setattr("eason_one.services.execution.ensure_budget",
                        lambda value: captured.setdefault("estimate", value))

    class Provider:
        def complete(self, *args):
            return SimpleNamespace(
                text="{}", input_tokens=1, output_tokens=1, response_id="r",
                request_id="q", status="completed", refusal=None,
                incomplete_reason=None, cache_creation_input_tokens=0,
                cache_read_input_tokens=0,
            )

    monkeypatch.setattr("eason_one.services.execution.get_provider", lambda key: Provider())
    execute(employee, "TEST", "request", context_override="",
            response_schema=MEETING_SYNTHESIS_SCHEMA)
    assert captured["estimate"] == schema_estimate.real_cost


def test_zero_cache_usage_records_one_exact_cost(ctx, monkeypatch):
    employee = anthropic_employee()

    class Provider:
        def complete(self, *args):
            return SimpleNamespace(
                text="ok", input_tokens=20, output_tokens=5, response_id="r",
                request_id="q", status="completed", refusal=None,
                incomplete_reason=None, cache_creation_input_tokens=0,
                cache_read_input_tokens=0,
            )

    monkeypatch.setattr("eason_one.services.execution.get_provider", lambda key: Provider())
    run = execute(employee, "TEST", "request")
    assert run.status == "SUCCEEDED" and run.real_cost == Decimal("0.030000")
    assert CostEvent.query.filter_by(agent_run_id=run.id).count() == 1


def test_cache_usage_is_auditable_but_has_no_understated_cost_event(ctx, client, monkeypatch):
    employee = anthropic_employee()

    class Provider:
        def complete(self, *args):
            return SimpleNamespace(
                text="ok", input_tokens=20, output_tokens=5, response_id="r",
                request_id="q", status="completed", refusal=None,
                incomplete_reason=None, cache_creation_input_tokens=7,
                cache_read_input_tokens=3,
            )

    monkeypatch.setattr("eason_one.services.execution.get_provider", lambda key: Provider())
    run = execute(employee, "TEST", "request")
    assert run.status == "FAILED" and run.real_cost is None
    assert (run.cache_creation_input_tokens, run.cache_read_input_tokens) == (7, 3)
    assert CostEvent.query.filter_by(agent_run_id=run.id).count() == 0
    page = client.get(f"/runs/{run.id}").get_data(as_text=True)
    assert "UNRECONCILED" in page
    assert "7 creation / 3 read" in page

``

---

## FILE: tests\test_patch0051.py

``python
import json
from decimal import Decimal

import pytest

from eason_one.extensions import db
from eason_one.models import AgentRun, CostEvent, Employee, MeetingMessage, Project, Task
from eason_one.providers import ProviderResult
from eason_one.services import meetings
from eason_one.services.costs import estimate_execution
from eason_one.services.projects import create_project


def employees():
    return [
        Employee.query.filter_by(slug=slug).one()
        for slug in ("ceo", "researcher", "critic")
    ]


def active_meeting(participants=None, **limits):
    people = participants or employees()
    meeting = meetings.create(
        "Gate review", "Prove bounded safe execution", "Position, evidence, risk",
        people[0], people, max_rounds=limits.get("max_rounds", 3),
        token_limit=limits.get("token_limit", 50000),
        real_cost_limit_twd=limits.get("real_cost_limit_twd", 1000),
    )
    meetings.start(meeting)
    return meeting


class PartialRoundProvider:
    def __init__(self):
        self.calls = []
        self.failed_once = False

    def complete(self, model, system_prompt, user_prompt, context, max_output_tokens, response_schema=None):
        employee_name = context.split("EMPLOYEE\n", 1)[1].split(";", 1)[0]
        self.calls.append(employee_name)
        if employee_name == "Researcher" and not self.failed_once:
            self.failed_once = True
            return ProviderResult("", 20, 5, response_id="failed-billable",
                                  status="incomplete", incomplete_reason="max_output_tokens")
        return ProviderResult(f"{employee_name} contribution", 20, 5,
                              response_id=f"ok-{employee_name}", status="completed")


def test_partial_round_retry_skips_success_and_completes_missing_only(ctx, monkeypatch):
    meeting = active_meeting()
    provider = PartialRoundProvider()
    monkeypatch.setattr("eason_one.services.execution.get_provider", lambda key: provider)

    with pytest.raises(ValueError, match="incomplete"):
        meetings.advance(meeting)

    assert provider.calls == ["CEO", "Researcher"]
    assert meeting.current_round == 0
    ceo_messages = MeetingMessage.query.filter_by(
        meeting_id=meeting.id, round_number=1, employee_id=employees()[0].id,
        speaker_type="EMPLOYEE").all()
    assert len(ceo_messages) == 1
    ceo_run_id = ceo_messages[0].agent_run_id
    assert db.session.get(AgentRun, ceo_run_id).status == "SUCCEEDED"
    assert CostEvent.query.filter_by(agent_run_id=ceo_run_id).count() == 1
    failed_b = AgentRun.query.filter_by(
        meeting_id=meeting.id, employee_id=employees()[1].id, status="FAILED").one()
    assert CostEvent.query.filter_by(agent_run_id=failed_b.id).count() == 1

    meetings.advance(meeting)
    assert provider.calls == ["CEO", "Researcher", "Researcher", "Critic"]
    assert meeting.current_round == 1
    assert MeetingMessage.query.filter_by(
        meeting_id=meeting.id, round_number=1, employee_id=employees()[0].id,
        speaker_type="EMPLOYEE").count() == 1
    assert AgentRun.query.filter_by(
        meeting_id=meeting.id, employee_id=employees()[0].id,
        purpose="MEETING_CONTRIBUTION", status="SUCCEEDED").count() == 1
    assert CostEvent.query.filter_by(agent_run_id=ceo_run_id).count() == 1


def test_form_retry_after_partial_round_does_not_rebill_success(client, ctx, monkeypatch):
    meeting = active_meeting()
    provider = PartialRoundProvider()
    monkeypatch.setattr("eason_one.services.execution.get_provider", lambda key: provider)
    client.post(f"/meetings/{meeting.id}/advance")
    assert provider.calls == ["CEO", "Researcher"]
    client.post(f"/meetings/{meeting.id}/advance")
    assert provider.calls == ["CEO", "Researcher", "Researcher", "Critic"]
    assert db.session.get(type(meeting), meeting.id).current_round == 1


@pytest.mark.parametrize("ceiling", ["cost", "tokens"])
def test_meeting_limit_uses_full_execution_frame_before_provider(ctx, monkeypatch, ceiling):
    ceo = employees()[0]
    ceo.system_instructions = "SYSTEM-GOVERNANCE " * 1200
    ceo.current_model.input_price_per_million = Decimal("1000")
    meeting = active_meeting([ceo], token_limit=100000, real_cost_limit_twd=100000)
    context = meetings.compact_context(meeting, ceo)
    prompt = ceo.system_instructions + "\nMEETING_CONTRIBUTION\nBe compact. Follow the round protocol. Do not mutate authoritative company state."
    request = "Contribute to round 1"
    full = estimate_execution(ceo.current_model, prompt, context, request)
    context_only = estimate_execution(ceo.current_model, "", context, request)
    assert full.input_tokens > context_only.input_tokens
    if ceiling == "cost":
        meeting.real_cost_limit_twd = (full.real_cost + context_only.real_cost) / 2
    else:
        meeting.token_limit = (
            full.input_tokens + full.output_tokens +
            context_only.input_tokens + context_only.output_tokens
        ) // 2
    db.session.commit()
    calls = {"count": 0}
    monkeypatch.setattr(
        "eason_one.services.execution.get_provider",
        lambda key: calls.__setitem__("count", calls["count"] + 1),
    )
    with pytest.raises(ValueError, match="cost" if ceiling == "cost" else "token"):
        meetings.advance(meeting)
    assert calls["count"] == 0
    assert AgentRun.query.filter_by(meeting_id=meeting.id).count() == 0


def test_execute_and_meeting_share_canonical_estimate(ctx, monkeypatch):
    ceo = employees()[0]
    meeting = active_meeting([ceo])
    context = meetings.compact_context(meeting, ceo)
    prompt = ceo.system_instructions + "\nMEETING_CONTRIBUTION\nBe compact. Follow the round protocol. Do not mutate authoritative company state."
    request = "Contribute to round 1"
    estimate = estimate_execution(ceo.current_model, prompt, context, request)
    captured = {}

    def capture_budget(value):
        captured["value"] = value

    monkeypatch.setattr("eason_one.services.execution.ensure_budget", capture_budget)
    meetings.advance(meeting)
    assert captured["value"] == estimate.real_cost


def test_ceo_dashboard_shows_only_live_project_work(client, ctx):
    ceo, researcher, _ = employees()
    live = create_project("Visible Live", "live", ceo, status="ACTIVE", environment="LIVE")
    smoke = create_project("Hidden Smoke", "smoke", ceo, status="ACTIVE", environment="SMOKE")
    archived = create_project("Hidden Archive", "old", ceo, status="ACTIVE", environment="ARCHIVED")
    db.session.add_all([
        Task(project_id=live.id, title="Live blocked", objective="x", status="BLOCKED",
             assigned_employee_id=researcher.id),
        Task(project_id=smoke.id, title="Smoke blocked", objective="x", status="BLOCKED",
             assigned_employee_id=researcher.id),
        Task(project_id=archived.id, title="Archived done", objective="x", status="DONE",
             assigned_employee_id=researcher.id),
    ])
    db.session.commit()
    page = client.get("/ceo").get_data(as_text=True)
    assert "Visible Live" in page
    assert "Hidden Smoke" not in page
    assert "Hidden Archive" not in page
    assert "<b>1</b><span>Active Projects</span>" in page
    assert "<b>0</b><span>Active Tasks</span>" in page
    assert "<b>1</b><span>Blocked</span>" in page
    assert "Live blocked" not in page  # dashboard uses aggregate active state, not task titles
    assert "Archived done" not in page


def test_structured_synthesis_populates_minutes_from_validated_output(ctx):
    meeting = active_meeting()
    meetings.advance(meeting)
    meetings.intervene(meeting, "Founder intervention that must be recorded.") if meeting.founder_joined_at else meetings.join_founder(meeting)
    if not MeetingMessage.query.filter_by(meeting_id=meeting.id, message_type="FOUNDER_INTERVENTION").first():
        meetings.intervene(meeting, "Founder intervention that must be recorded.")
    meetings.end_and_synthesize(meeting)
    assert meeting.minutes_json["agreements"] == ["Use the smallest bounded next action."]
    assert meeting.minutes_json["evidence_referenced"] == [
        "Only evidence recorded in the Meeting context was considered."
    ]
    assert meeting.minutes_json["founder_interventions"] == [
        "Founder intervention that must be recorded."
    ]
    assert meeting.minutes_json["token_usage"] > 0
    assert meeting.minutes_json["real_cost_twd"] == "0"


class InvalidSynthesisProvider:
    def complete(self, model, system_prompt, user_prompt, context, max_output_tokens, response_schema=None):
        return ProviderResult(json.dumps({"agreements": ["misleading partial"]}), 30, 10,
                              response_id="billable-invalid", status="completed")


def test_invalid_synthesis_keeps_cost_and_meeting_recoverable(ctx, monkeypatch):
    ceo = employees()[0]
    meeting = active_meeting([ceo])
    provider = InvalidSynthesisProvider()
    monkeypatch.setattr("eason_one.services.execution.get_provider", lambda key: provider)
    with pytest.raises(ValueError, match="validation failed"):
        meetings.end_and_synthesize(meeting)
    run = AgentRun.query.filter_by(meeting_id=meeting.id, purpose="MEETING_SYNTHESIS").one()
    assert run.status == "FAILED"
    assert CostEvent.query.filter_by(agent_run_id=run.id).count() == 1
    assert meeting.status == "ACTIVE"
    assert meeting.minutes_json is None

``
