# Eason One — Core / Architecture Review Bundle


---

## FILE: docs\ONE_WEEK_TARGET.md

``markdown
# One-Week Real Project Target

The current slice can run the Beauty Conversion Pilot with manual, explicit model calls. The smallest follow-on needed for a serious one-week test is:

1. Configure and verify one paid production model in TWD, including a conservative pre-call maximum-cost reservation rather than only current-spend and post-token enforcement.
2. Add structured CEO plan parsing/validation for model-produced project/task proposals instead of the deliberately deterministic two-task starter plan.
3. Add task-specific evidence capture and links, plus Founder UI actions for approving/correcting knowledge proposals.
4. Add project-level synthesis that selects completed task outputs and produces a concise Founder report.
5. Improve review storage with explicit accept/reject criteria and blocker categorization.
6. Run the actual Beauty pilot, record real market evidence, costs, decisions, failures, and only then add contribution events.

No autonomous workers, new employees, embeddings, meetings, or organization redesign are required for that test.

``

---

## FILE: docs\V1_ARCHITECTURE.md

``markdown
# V1 Architecture

Eason One is a server-rendered Flask application with SQLAlchemy and SQLite. `create_app` owns configuration and database initialization. Explicit SQLAlchemy models preserve organizational identity, work, governance, executions, and ledgers.

The deterministic boundary lives in `eason_one/services`. Routes accept Founder actions but delegate project creation, task transitions/review, executions, budget calculations, model changes, interviews, and knowledge approval. LLM output is stored as an `AgentRun` or a pending `Proposal`; it cannot directly change authoritative Project/Task state or Company Brain truth.

Employees are persistent identities linked to replaceable `ModelConfig` records. Every change is appended to `EmployeeModelHistory`. Provider selection uses `provider_key`; V1 includes a deterministic Mock provider and an environment-backed OpenAI Responses adapter.

Execution builds a filtered text context from employee identity, optional project/task, recent task messages, current non-superseded Company Brain items, and remaining real budget. Projectless calls receive company-level knowledge only. Each call creates an immutable run attempt with provider/model/pricing snapshots. A conservative maximum-cost check runs before provider invocation; known actual spend is committed exactly once before downstream parsing. Retries create new runs.

Structured workflows pass strict JSON Schemas through the provider boundary to OpenAI Responses while retaining deterministic application validation. Structured Task results can create pending knowledge Proposals, never authoritative knowledge. Founder-approved Decisions may link same-Project or company-level approved basis items through `BASIS_FOR` references in the same transaction.

Task transitions use a fixed transition map. Execution leads to REVIEW; an explicit reviewer accept/reject action leads to DONE or WORKING. CEO JSON is validated against a narrow project-plan schema and materialized in one transaction. Proposal approval is an explicit Founder action through the same Brain validation path. Knowledge corrections and killed hypotheses are append-only and their replacement/warning records remain in effective context.

The Jinja UI exposes CEO command, project/task execution and review, persistent employee inspection/interviews, Founder Inbox, cost ledger, and run audit pages. No background execution is implied.

``

---

## FILE: eason_one.egg-info\dependency_links.txt

``text


``

---

## FILE: eason_one.egg-info\requires.txt

``text
Flask>=3.1
Flask-SQLAlchemy>=3.1
SQLAlchemy>=2.0
openai>=1.0

[test]
pytest>=8.0

``

---

## FILE: eason_one.egg-info\SOURCES.txt

``text
README.md
pyproject.toml
eason_one/__init__.py
eason_one/extensions.py
eason_one/i18n.py
eason_one/models.py
eason_one/providers.py
eason_one/routes.py
eason_one/seed.py
eason_one.egg-info/PKG-INFO
eason_one.egg-info/SOURCES.txt
eason_one.egg-info/dependency_links.txt
eason_one.egg-info/requires.txt
eason_one.egg-info/top_level.txt
eason_one/services/__init__.py
eason_one/services/approvals.py
eason_one/services/brain.py
eason_one/services/ceo.py
eason_one/services/company.py
eason_one/services/context.py
eason_one/services/contributions.py
eason_one/services/costs.py
eason_one/services/employees.py
eason_one/services/execution.py
eason_one/services/interviews.py
eason_one/services/learning.py
eason_one/services/projects.py
eason_one/services/tasks.py
eason_one/static/app.css
eason_one/templates/base.html
eason_one/templates/ceo.html
eason_one/templates/costs.html
eason_one/templates/employee.html
eason_one/templates/employees.html
eason_one/templates/inbox.html
eason_one/templates/interview.html
eason_one/templates/project.html
eason_one/templates/projects.html
eason_one/templates/run.html
eason_one/templates/unseeded.html
tests/test_core.py
tests/test_hardening.py
``

---

## FILE: eason_one.egg-info\top_level.txt

``text
eason_one

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
    if "work_message" in inspector.get_table_names():
        cols={x["name"] for x in inspector.get_columns("work_message")}
        if "agent_run_id" not in cols: db.session.execute(text("ALTER TABLE work_message ADD COLUMN agent_run_id INTEGER REFERENCES agent_run(id)"))
    db.session.commit()

``

---

## FILE: eason_one\extensions.py

``python
from flask_sqlalchemy import SQLAlchemy
db = SQLAlchemy()


``

---

## FILE: eason_one\i18n.py

``python
from flask import session
TRANSLATIONS={
 "en":{"nav.ceo":"CEO","nav.projects":"Projects","nav.employees":"Employees","nav.inbox":"Founder Inbox","nav.costs":"Cost Control","nav.models":"Models","lang.en":"EN","lang.zh":"繁中","button.execute":"Execute CEO Request","button.interview":"Interview Employee"},
 "zh-TW":{"nav.ceo":"CEO","nav.projects":"專案","nav.employees":"員工","nav.inbox":"創辦人收件匣","nav.costs":"成本控制","nav.models":"模型","lang.en":"EN","lang.zh":"繁中","button.execute":"執行 CEO 指令","button.interview":"訪談員工"}
}
def translate(key,language=None):
    lang=language or session.get("language","en")
    return TRANSLATIONS.get(lang,TRANSLATIONS["en"]).get(key,TRANSLATIONS["en"].get(key,key))

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
    model_config_id = db.Column(db.Integer, db.ForeignKey("model_config.id"), nullable=False)
    purpose = db.Column(db.String(80), nullable=False)
    user_request = db.Column(db.Text, nullable=False)
    system_prompt_snapshot = db.Column(db.Text, nullable=False)
    context_snapshot = db.Column(db.Text, nullable=False)
    raw_output = db.Column(db.Text)
    parsed_output_json = db.Column(db.JSON)
    status = db.Column(db.String(20), default="CREATED", nullable=False)
    provider_request_id = db.Column(db.String(160))
    input_tokens = db.Column(db.Integer)
    output_tokens = db.Column(db.Integer)
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

``

---

## FILE: eason_one\providers.py

``python
from dataclasses import dataclass
import json
import os
@dataclass
class ProviderResult:
    text: str
    input_tokens: int
    output_tokens: int
    request_id: str|None=None
class MockProvider:
    def complete(self, model_config, system_prompt, user_prompt, context, max_output_tokens, response_schema=None):
        if "TASK_REVIEW" in system_prompt:
            decision="REVISE" if "revise" in user_prompt.lower() else ("BLOCK" if "block" in user_prompt.lower() else "ACCEPT")
            text=json.dumps({"decision":decision,"summary":"Mock reviewer assessed the result against acceptance criteria.",
                "issues":[] if decision=="ACCEPT" else ["A material issue remains"],"required_changes":[] if decision=="ACCEPT" else ["Address the identified issue"]})
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
        return ProviderResult(text, len((system_prompt+context+user_prompt).split()), len(text.split()), "mock-local")
class OpenAIProvider:
    def complete(self, model_config, system_prompt, user_prompt, context, max_output_tokens, response_schema=None):
        from openai import OpenAI
        kwargs={"model":model_config.model_name,"instructions":system_prompt,"input":f"{context}\n\nUSER REQUEST:\n{user_prompt}","max_output_tokens":max_output_tokens}
        if response_schema:
            kwargs["text"]={"format":{"type":"json_schema","name":response_schema["name"],"strict":True,"schema":response_schema["schema"]}}
        r=OpenAI(api_key=os.environ["OPENAI_API_KEY"]).responses.create(**kwargs)
        return ProviderResult(r.output_text, r.usage.input_tokens, r.usage.output_tokens, r.id)
def _project_name(request):
    words=request.strip().rstrip(".").split()
    if "project" in [x.lower() for x in words]:
        words=words[:[x.lower() for x in words].index("project")]
    while words and words[0].lower() in {"create","start","a","an","the","build"}: words.pop(0)
    return " ".join(words[:8]).title() or "Founder Initiative"
def get_provider(key):
    if key=="mock": return MockProvider()
    if key=="openai": return OpenAIProvider()
    raise ValueError(f"Unknown provider: {key}")

``

---

## FILE: eason_one\routes.py

``python
from decimal import Decimal
from flask import Blueprint, render_template, request, redirect, url_for, flash, abort, session
from sqlalchemy import func
from .extensions import db
from .models import *
from .services.company import get_company, spent, remaining
from .services.projects import create_project
from .services.tasks import create_task, transition, review
from .services.execution import execute
from .services.ceo import founder_request, materialize_project_plan,generate_project_briefing
from .services.interviews import start, ask
from .services.contributions import total, project_totals
from .services.learning import create as create_learning
from .services.reviews import run_review
from .services.brain import add_knowledge,active_hypotheses,current
from .services.approvals import review_proposal
from .services.employees import change_model
from .services.task_execution import run_task
from .services import model_configs as model_config_service
from .services.costs import conservative_estimate

bp=Blueprint("main",__name__)
@bp.route("/")
def index(): return redirect(url_for("main.ceo"))
@bp.route("/language/<language>",methods=["POST"])
def language(language):
    if language not in {"en","zh-TW"}: abort(400)
    session["language"]=language
    return redirect(request.form.get("next") or request.referrer or url_for("main.ceo"))

@bp.route("/ceo",methods=["GET","POST"])
def ceo():
    company=get_company()
    if not company: return render_template("unseeded.html")
    if request.method=="POST":
        try:
            run,proposal=founder_request(Employee.query.filter_by(slug="ceo").one(),request.form["request"])
            if proposal: flash(run.parsed_output_json["executive_response"]+" Plan is waiting in Founder Inbox.","ok")
            elif run.parsed_output_json and run.parsed_output_json.get("mode")=="STATUS_QUERY": flash(run.parsed_output_json["executive_response"],"ok")
            else: flash("CEO output failed validation; no proposal was created.","error")
            return redirect(url_for("main.ceo"))
        except Exception as e: flash(str(e),"error")
    metrics={"active_projects":Project.query.filter(Project.status.in_(["PLANNING","ACTIVE","BLOCKED","REVIEW"])).count(),
      "active_tasks":Task.query.filter(Task.status.in_(["ASSIGNED","WORKING","REVIEW"])).count(),
      "blocked":Task.query.filter_by(status="BLOCKED").count(),"pending":Proposal.query.filter_by(status="PENDING").count()}
    projects=Project.query.order_by(Project.updated_at.desc()).all()
    recent=Task.query.filter_by(status="DONE").order_by(Task.completed_at.desc()).limit(6).all()
    latest_ceo=AgentRun.query.filter_by(purpose="CEO_FOUNDER_REQUEST",status="SUCCEEDED").order_by(AgentRun.started_at.desc()).first()
    return render_template("ceo.html",company=company,spent=spent(),remaining=remaining(),metrics=metrics,projects=projects,recent=recent,latest_ceo=latest_ceo)

@bp.route("/projects")
def projects(): return render_template("projects.html",projects=Project.query.order_by(Project.updated_at.desc()).all())
@bp.route("/projects/new",methods=["POST"])
def project_new():
    p=create_project(request.form["name"],request.form["objective"],Employee.query.get_or_404(request.form["owner_id"]),status="ACTIVE")
    return redirect(url_for("main.project_detail",id=p.id))
@bp.route("/projects/<int:id>")
def project_detail(id):
    p=Project.query.get_or_404(id)
    counts=dict(db.session.query(Task.status,func.count(Task.id)).filter_by(project_id=id).group_by(Task.status).all())
    costs=Decimal(db.session.query(func.coalesce(func.sum(CostEvent.real_cost_delta),0)).filter_by(project_id=id).scalar())
    contributions=db.session.query(Employee.name,func.sum(ContributionEvent.value)).join(Employee).filter(ContributionEvent.project_id==id,ContributionEvent.scope=="PROJECT").group_by(Employee.name).all()
    knowledge=current(id); history=KnowledgeItem.query.filter_by(project_id=id,founder_approved=True).order_by(KnowledgeItem.created_at.desc()).all()
    reports=WorkMessage.query.filter_by(project_id=id,message_type="REPORT").order_by(WorkMessage.created_at.desc()).all()
    basis_refs=KnowledgeReference.query.filter(KnowledgeReference.to_knowledge_id.in_([k.id for k in knowledge] or [-1]),KnowledgeReference.relation_type=="BASIS_FOR").all()
    decision_bases={}
    for ref in basis_refs: decision_bases.setdefault(ref.to_knowledge_id,[]).append(db.session.get(KnowledgeItem,ref.from_knowledge_id))
    return render_template("project.html",p=p,counts=counts,costs=costs,contributions=contributions,knowledge=knowledge,
      employees=Employee.query.filter_by(active=True).all(),reports=reports,knowledge_targets=KnowledgeItem.query.filter_by(project_id=id,founder_approved=True).all(),
      active_hypotheses=active_hypotheses(id),knowledge_history=history,
      basis_options=[k for k in knowledge if k.kind in {"FACT","HYPOTHESIS","EVIDENCE","CORRECTION"}],decision_bases=decision_bases)
@bp.route("/projects/<int:id>/tasks",methods=["POST"])
def task_new(id):
    p=Project.query.get_or_404(id); assignee=Employee.query.get(request.form.get("assignee_id")); reviewer=Employee.query.get(request.form.get("reviewer_id"))
    create_task(p,request.form["title"],request.form["objective"],Employee.query.filter_by(slug="ceo").first(),assignee,reviewer,
      required_output=request.form.get("required_output"),acceptance_criteria=request.form.get("acceptance_criteria"))
    return redirect(url_for("main.project_detail",id=id))
@bp.route("/tasks/<int:id>/run",methods=["POST"])
def task_run(id):
    t=Task.query.get_or_404(id)
    try:
        if t.status=="ASSIGNED": transition(t,"WORKING")
        run=run_task(t)
        if not run.parsed_output_json: raise ValueError(run.error_text or "Task result failed validation")
        transition(t,"REVIEW"); flash("Structured Task result stored; governed knowledge proposals are in Founder Inbox.","ok")
        return redirect(url_for("main.run_detail",id=run.id))
    except Exception as e: flash(str(e),"error"); return redirect(url_for("main.project_detail",id=t.project_id))
@bp.route("/tasks/<int:id>/review",methods=["POST"])
def task_review(id):
    t=Task.query.get_or_404(id)
    try: review(t,t.reviewer or Employee.query.filter_by(slug="ceo").one(),request.form["content"],request.form["decision"]=="accept")
    except Exception as e: flash(str(e),"error")
    return redirect(url_for("main.project_detail",id=t.project_id))
@bp.route("/tasks/<int:id>/run-review",methods=["POST"])
def task_run_review(id):
    t=Task.query.get_or_404(id)
    try: run_review(t,instruction=request.form.get("instruction","Review this Task")); flash("Reviewer execution completed.","ok")
    except Exception as ex: flash(str(ex),"error")
    return redirect(url_for("main.project_detail",id=t.project_id))
@bp.route("/projects/<int:id>/briefing",methods=["POST"])
def project_briefing(id):
    p=Project.query.get_or_404(id); run=generate_project_briefing(Employee.query.filter_by(slug="ceo").one(),p)
    flash("CEO briefing generated." if run.parsed_output_json else "CEO briefing failed validation.","ok" if run.parsed_output_json else "error")
    return redirect(url_for("main.project_detail",id=id))
@bp.route("/projects/<int:id>/knowledge",methods=["POST"])
def project_knowledge(id):
    p=Project.query.get_or_404(id)
    try:
        add_knowledge(request.form["kind"],request.form["title"],request.form["content"],project_id=p.id,founder_approved=True,
          source_ref=request.form.get("source_ref") or None,rationale=request.form.get("rationale") or None,
          target_knowledge_id=int(request.form["target_knowledge_id"]) if request.form.get("target_knowledge_id") else None,
          basis_knowledge_ids=[int(x) for x in request.form.getlist("basis_knowledge_ids")])
        flash("Founder knowledge recorded.","ok")
    except Exception as ex: flash(str(ex),"error")
    return redirect(url_for("main.project_detail",id=id))

@bp.route("/employees")
def employees():
    rows=[]
    for e in Employee.query.order_by(Employee.id).all():
        rows.append({"e":e,"active_tasks":Task.query.filter_by(assigned_employee_id=e.id).filter(Task.status.notin_(["DONE","FAILED","CANCELLED"])).all(),
      "project_contribution":sum(v for _,v in project_totals(e.id)),"company_contribution":total(e.id,"COMPANY"),
          "cost":Decimal(db.session.query(func.coalesce(func.sum(CostEvent.real_cost_delta),0)).filter_by(employee_id=e.id).scalar()),
          "tokens":db.session.query(func.coalesce(func.sum(AgentRun.input_tokens+AgentRun.output_tokens),0)).filter_by(employee_id=e.id).scalar()})
    return render_template("employees.html",rows=rows)
@bp.route("/employees/<int:id>")
def employee_detail(id):
    e=Employee.query.get_or_404(id); tasks=Task.query.filter_by(assigned_employee_id=id).order_by(Task.updated_at.desc()).all()
    runs=AgentRun.query.filter_by(employee_id=id).order_by(AgentRun.started_at.desc()).limit(12).all()
    history=EmployeeModelHistory.query.filter_by(employee_id=id).order_by(EmployeeModelHistory.started_at.desc()).all()
    learning=EmployeeLearningRecord.query.filter_by(employee_id=id).order_by(EmployeeLearningRecord.created_at.desc()).limit(10).all()
    return render_template("employee.html",e=e,tasks=tasks,runs=runs,history=history,learning=learning,
      project_contributions=project_totals(id),cc=total(id,"COMPANY"),projects=Project.query.order_by(Project.name).all(),
      model_configs=ModelConfig.query.filter_by(active=True).order_by(ModelConfig.label).all())
@bp.route("/employees/<int:id>/learning",methods=["POST"])
def employee_learning(id):
    e=Employee.query.get_or_404(id); project=Project.query.get(request.form.get("project_id")) if request.form.get("project_id") else None
    task=Task.query.get(request.form.get("task_id")) if request.form.get("task_id") else None
    try: create_learning(e,request.form["title"],request.form["content"],project,task,request.form.get("source_ref"),request.form.get("validated")=="1")
    except Exception as ex: flash(str(ex),"error")
    return redirect(url_for("main.employee_detail",id=id))
@bp.route("/employees/<int:id>/model",methods=["POST"])
def employee_model(id):
    e=Employee.query.get_or_404(id); model=ModelConfig.query.get_or_404(request.form["model_config_id"])
    if not model.active: abort(400)
    change_model(e,model,request.form.get("reason") or "Founder reassignment")
    return redirect(url_for("main.employee_detail",id=id))
@bp.route("/employees/<int:id>/interview",methods=["GET","POST"])
def interview(id):
    e=Employee.query.get_or_404(id); interview=FounderInterview.query.filter_by(employee_id=id,ended_at=None).order_by(FounderInterview.started_at.desc()).first()
    if not interview: interview=start(e)
    if request.method=="POST":
        try: ask(interview,request.form["content"]); return redirect(url_for("main.interview",id=id))
        except Exception as ex: flash(str(ex),"error")
    return render_template("interview.html",e=e,interview=interview)

@bp.route("/inbox")
def inbox(): return render_template("inbox.html",proposals=Proposal.query.order_by(Proposal.created_at.desc()).all())
@bp.route("/inbox/<int:id>/materialize",methods=["POST"])
def materialize(id):
    proposal=Proposal.query.get_or_404(id)
    if proposal.status!="PENDING" or proposal.payload_json.get("type")!="PROJECT_PLAN": abort(400)
    try: p=materialize_project_plan(proposal)
    except Exception as ex: flash(str(ex),"error"); return redirect(url_for("main.inbox"))
    return redirect(url_for("main.project_detail",id=p.id))
@bp.route("/inbox/<int:id>/reject",methods=["POST"])
def reject(id):
    p=Proposal.query.get_or_404(id)
    if p.status!="PENDING": abort(400)
    p.status="REJECTED"; p.reviewed_at=now(); db.session.commit(); return redirect(url_for("main.inbox"))
@bp.route("/inbox/<int:id>/knowledge-review",methods=["POST"])
def knowledge_review(id):
    p=Proposal.query.get_or_404(id); decision=request.form["decision"]
    try:
        corrected=None
        if decision=="CORRECTED":
            corrected={"kind":request.form["kind"],"title":request.form["title"],"content":request.form["content"],
              "source_ref":request.form.get("source_ref") or None,"rationale":request.form.get("rationale") or None,
              "target_knowledge_id":int(request.form["target_knowledge_id"]) if request.form.get("target_knowledge_id") else None,
              "basis_knowledge_ids":[int(x) for x in request.form.getlist("basis_knowledge_ids")] or p.payload_json.get("basis_knowledge_ids",[])}
        review_proposal(p,decision,request.form.get("note"),corrected)
    except Exception as ex: flash(str(ex),"error")
    return redirect(url_for("main.inbox"))
@bp.route("/models",methods=["GET","POST"])
def models():
    if request.method=="POST":
        try:
            model_config_service.create(request.form["label"],request.form["provider_key"],request.form["model_name"],
              request.form["input_price_per_million"],request.form["output_price_per_million"],request.form["currency"],request.form["max_output_tokens"])
        except Exception as ex: db.session.rollback(); flash(str(ex),"error")
    models=ModelConfig.query.order_by(ModelConfig.created_at.desc()).all()
    estimates={m.id:conservative_estimate(m,"Representative pending execution framing",m.max_output_tokens) for m in models}
    return render_template("models.html",models=models,estimates=estimates,remaining=remaining())
@bp.route("/models/<int:id>/toggle",methods=["POST"])
def model_toggle(id):
    m=ModelConfig.query.get_or_404(id); model_config_service.toggle(m); return redirect(url_for("main.models"))
@bp.route("/costs")
def costs():
    c=get_company(); return render_template("costs.html",company=c,spent=spent(),remaining=remaining(),events=CostEvent.query.order_by(CostEvent.created_at.desc()).all())
@bp.route("/runs/<int:id>")
def run_detail(id): return render_template("run.html",run=AgentRun.query.get_or_404(id))

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

``

---

## FILE: eason_one\seed.py

``python
import click
from .extensions import db
from .models import Company, Department, Position, ModelConfig, Employee, EmployeeModelHistory

def seed():
    if Company.query.first(): return Company.query.first()
    company=Company(name="Eason One",real_budget_limit=3000,currency="TWD")
    research=Department(name="Research Department"); engineering=Department(name="Engineering Department")
    ceop=Position(name="CEO",level=4); director=Position(name="Director",level=3); employee=Position(name="Employee",level=1)
    mock=ModelConfig(label="Local Mock",provider_key="mock",model_name="deterministic-mock",
        input_price_per_million=0,output_price_per_million=0,currency="TWD")
    db.session.add_all([company,research,engineering,ceop,director,employee,mock]); db.session.flush()
    ceo=Employee(name="CEO",slug="ceo",position_id=ceop.id,role_description="Chief Coordinator / Executive Interface / Company Synthesizer",
        system_instructions="Be a concise executive. Propose actions; never mutate authoritative state.",current_model_config_id=mock.id,salary_credits_per_week=1000)
    db.session.add(ceo); db.session.flush()
    rows=[
      ("Research Director","research-director",research,director,ceo,"Plan and review research."),
      ("Researcher","researcher",research,employee,None,"Gather evidence and distinguish Company Brain facts, model reasoning/prior knowledge, and claims requiring external verification. Never fabricate web research or citations."),
      ("Engineering Director","engineering-director",engineering,director,ceo,"Coordinate and review engineering."),
      ("Engineer","engineer",engineering,employee,None,"Analyze and implement scoped engineering work."),
      ("Critic","critic",None,employee,ceo,"Perform independent adversarial review and surface blockers."),
    ]
    people=[ceo]
    for name,slug,dept,pos,manager,role in rows:
        e=Employee(name=name,slug=slug,department_id=getattr(dept,"id",None),position_id=pos.id,
          manager_id=getattr(manager,"id",None),role_description=role,system_instructions=role+" Do not change company policy.",
          current_model_config_id=mock.id,salary_credits_per_week=500)
        db.session.add(e); db.session.flush(); people.append(e)
    people[2].manager_id=people[1].id; people[4].manager_id=people[3].id
    for person in people: db.session.add(EmployeeModelHistory(employee_id=person.id,model_config_id=mock.id,reason="Initial assignment"))
    db.session.commit(); return company

@click.command("seed")
def seed_command():
    seed(); click.echo("Eason One seeded.")

``

---

## FILE: eason_one\services\__init__.py

``python


``

---

## FILE: eason_one\services\approvals.py

``python
from ..extensions import db
from ..models import now
from .brain import build_knowledge,validate_basis_ids
from ..models import KnowledgeReference
def review_proposal(proposal, decision, note=None, corrected=None):
    if proposal.status!="PENDING" or decision not in {"APPROVED","REJECTED","CORRECTED"}: raise ValueError("Invalid proposal review")
    try:
        if decision in {"APPROVED","CORRECTED"}:
            p=corrected if decision=="CORRECTED" else proposal.payload_json
            p=dict(p); basis=p.pop("basis_knowledge_ids",[]) or []
            if p.get("kind")=="DECISION": validate_basis_ids(basis,proposal.project_id)
            item=build_knowledge(project_id=proposal.project_id,founder_approved=True,origin_agent_run_id=proposal.agent_run_id,
                origin_employee_id=proposal.proposed_by_employee_id,**p)
            db.session.add(item); db.session.flush()
            if p.get("kind")=="DECISION":
                for source in basis: db.session.add(KnowledgeReference(from_knowledge_id=source,to_knowledge_id=item.id,relation_type="BASIS_FOR"))
            proposal.materialized_knowledge_id=item.id
        proposal.status=decision; proposal.review_note=note; proposal.reviewed_at=now()
        if corrected: proposal.corrected_payload_json=corrected
        db.session.commit(); return proposal
    except Exception:
        db.session.rollback(); raise

``

---

## FILE: eason_one\services\brain.py

``python
from ..extensions import db
from ..models import KnowledgeItem,KnowledgeReference
KINDS={"FACT","HYPOTHESIS","EVIDENCE","DECISION","CORRECTION","KILLED"}
def validate(kind, **kwargs):
    if kind not in KINDS: raise ValueError("Unknown knowledge kind")
    if kind=="EVIDENCE" and not kwargs.get("source_ref"): raise ValueError("Evidence requires source_ref")
    if kind=="DECISION" and not kwargs.get("rationale"): raise ValueError("Decision requires rationale")
    if kind in {"CORRECTION","KILLED"} and not kwargs.get("target_knowledge_id"): raise ValueError(f"{kind} requires a target")
    if kind in {"CORRECTION","KILLED"}:
        target=db.session.get(KnowledgeItem,kwargs["target_knowledge_id"])
        if not target: raise ValueError(f"{kind} target does not exist")
        if kind=="KILLED" and target.kind!="HYPOTHESIS": raise ValueError("KILLED target must be a HYPOTHESIS")
    return True
def build_knowledge(kind,title,content,**kwargs):
    kwargs.pop("basis_knowledge_ids",None)
    validate(kind,**kwargs); return KnowledgeItem(kind=kind,title=title,content=content,**kwargs)
def validate_basis_ids(ids,project_id):
    if not isinstance(ids,list) or len(set(ids))!=len(ids): raise ValueError("Invalid basis IDs")
    rows=[]
    for ident in ids:
        item=db.session.get(KnowledgeItem,ident)
        if not item or not item.founder_approved: raise ValueError("Decision basis must be Founder-approved")
        if item.project_id not in {None,project_id}: raise ValueError("Decision basis belongs to an unrelated Project")
        rows.append(item)
    return rows
def add_knowledge(kind,title,content,**kwargs):
    basis=kwargs.pop("basis_knowledge_ids",[]) or []
    try:
        if kind=="DECISION": validate_basis_ids(basis,kwargs.get("project_id"))
        item=build_knowledge(kind,title,content,**kwargs); db.session.add(item); db.session.flush()
        if kind=="DECISION":
            for source in basis: db.session.add(KnowledgeReference(from_knowledge_id=source,to_knowledge_id=item.id,relation_type="BASIS_FOR"))
        db.session.commit(); return item
    except Exception: db.session.rollback(); raise
def current(project_id=None):
    q=KnowledgeItem.query.filter_by(founder_approved=True)
    if project_id is None: q=q.filter(KnowledgeItem.project_id.is_(None))
    else: q=q.filter((KnowledgeItem.project_id==project_id)|(KnowledgeItem.project_id.is_(None)))
    items=q.all(); superseded={x.target_knowledge_id for x in items if x.kind in {"CORRECTION","KILLED"}}
    return [x for x in items if x.id not in superseded]
def active_hypotheses(project_id=None):
    return [x for x in current(project_id) if x.kind=="HYPOTHESIS"]

``

---

## FILE: eason_one\services\ceo.py

``python
import json
from decimal import Decimal
from sqlalchemy import func
from ..extensions import db
from ..models import Proposal,Employee,Project,Task,CostEvent,WorkMessage,ContributionEvent,now
from .execution import execute
from .company import get_company,spent,remaining
from .brain import current
from ..schemas import CEO_SCHEMA,SYNTHESIS_SCHEMA

MODES={"NEW_PROJECT","PROJECT_ACTION","STATUS_QUERY"}
PRIORITIES={"LOW","MEDIUM","HIGH","CRITICAL"}
TASK_FIELDS={"title","objective","assignee_slug","reviewer_slug","required_output","acceptance_criteria"}

def operating_context():
    roster=[]
    for e in Employee.query.filter_by(active=True).order_by(Employee.id):
        active=Task.query.filter_by(assigned_employee_id=e.id).filter(Task.status.in_(["ASSIGNED","WORKING","BLOCKED","REVIEW"])).all()
        names=sorted({t.project.name for t in active})
        roster.append(f"{e.name}\nID: {e.id}; slug: {e.slug}; department: {e.department.name if e.department else 'CEO Office / Assurance'}; "
          f"position: {e.position.name} / level {e.position.level}; manager: {e.manager.name if e.manager else 'Founder'}; "
          f"role: {e.role_description}; model: {e.current_model.label} / {e.current_model.model_name}; active tasks: {len(active)}; projects: {', '.join(names) or '-'}")
    summaries=[]
    for p in Project.query.filter(Project.status.in_(["PLANNING","ACTIVE","BLOCKED","REVIEW"])).order_by(Project.id):
        counts=dict(db.session.query(Task.status,func.count(Task.id)).filter_by(project_id=p.id).group_by(Task.status).all())
        recent=[x.title for x in Task.query.filter_by(project_id=p.id,status="DONE").order_by(Task.completed_at.desc()).limit(3)]
        cost=Decimal(db.session.query(func.coalesce(func.sum(CostEvent.real_cost_delta),0)).filter_by(project_id=p.id).scalar())
        summaries.append(f"Project #{p.id}: {p.name}; status: {p.status}; priority: {p.priority}; owner: {p.owner.name}; "
          f"tasks: {sum(counts.values())}; blocked: {counts.get('BLOCKED',0)}; review: {counts.get('REVIEW',0)}; "
          f"recent completed: {', '.join(recent) or '-'}; cost: {p.owner.current_model.currency} {cost}")
    company=get_company()
    return "ORGANIZATION\n\n"+"\n\n".join(roster)+"\n\nCOMPANY STATUS\n"+("\n".join(summaries) or "No active Projects.")+(
      f"\n\nBUDGET\nlimit: {company.currency} {company.real_budget_limit}; spent: {spent()}; remaining: {remaining()}; "
      f"pending Founder Proposals: {Proposal.query.filter_by(status='PENDING').count()}")

def _validate_tasks(tasks):
    if not isinstance(tasks,list) or not 1<=len(tasks)<=12: raise ValueError("Plan requires 1–12 tasks")
    active={e.slug for e in Employee.query.filter_by(active=True)}
    for item in tasks:
        if not isinstance(item,dict) or set(item)!=TASK_FIELDS: raise ValueError("Invalid task fields")
        for field in {"title","objective","assignee_slug","required_output","acceptance_criteria"}:
            if not isinstance(item[field],str) or not item[field].strip(): raise ValueError(f"Task {field} is required")
        if item["assignee_slug"] not in active: raise ValueError("Unknown or inactive assignee")
        if item["reviewer_slug"] is not None and item["reviewer_slug"] not in active: raise ValueError("Unknown or inactive reviewer")

def validate_plan(payload):
    if not isinstance(payload,dict) or payload.get("mode") not in MODES: raise ValueError("Invalid CEO request mode")
    if not isinstance(payload.get("executive_response"),str) or not payload["executive_response"].strip(): raise ValueError("Missing executive response")
    mode=payload["mode"]
    expected={"mode","executive_response"}
    if mode=="NEW_PROJECT":
        expected|={"project","tasks"}; project=payload.get("project")
        if not isinstance(project,dict) or set(project)!={"name","objective","priority"}: raise ValueError("Invalid project fields")
        if not project["name"].strip() or not project["objective"].strip() or project["priority"] not in PRIORITIES: raise ValueError("Invalid project")
        _validate_tasks(payload.get("tasks"))
    elif mode=="PROJECT_ACTION":
        expected|={"project_id","tasks"}; _validate_tasks(payload.get("tasks"))
        if not db.session.get(Project,payload.get("project_id")): raise ValueError("CEO referenced an unknown Project ID")
    else:
        expected|={"project_id"}
        if payload.get("project_id") is not None and not db.session.get(Project,payload["project_id"]): raise ValueError("CEO referenced an unknown Project ID")
    full={"mode","executive_response","project","project_id","tasks"}
    keys=frozenset(payload)
    if keys not in {frozenset(expected),frozenset(full)}: raise ValueError("CEO response has unexpected fields")
    if keys==frozenset(full):
        if mode=="NEW_PROJECT" and payload["project_id"] is not None: raise ValueError("NEW_PROJECT cannot reference an existing Project")
        if mode in {"PROJECT_ACTION","STATUS_QUERY"} and payload["project"] is not None: raise ValueError("Existing-project modes cannot define a new Project")
        if mode=="STATUS_QUERY" and payload["tasks"]: raise ValueError("STATUS_QUERY cannot propose Tasks")
    return payload

def founder_request(ceo,request):
    prompt=ceo.system_instructions+"\nCEO_FOUNDER_REQUEST\nReturn only strict JSON using NEW_PROJECT, PROJECT_ACTION, or STATUS_QUERY. Assign only roster slugs. Never mutate authority."
    run=execute(ceo,"CEO_FOUNDER_REQUEST",request,context_override=operating_context(),system_prompt_override=prompt,response_schema=CEO_SCHEMA)
    try:
        plan=validate_plan(json.loads(run.raw_output)); run.parsed_output_json=plan
        if plan["mode"]=="STATUS_QUERY": db.session.commit(); return run,None
        proposal=Proposal(project_id=plan.get("project_id"),agent_run_id=run.id,proposed_by_employee_id=ceo.id,
          payload_json={"type":"PROJECT_PLAN","plan":plan},status="PENDING")
        db.session.add(proposal); db.session.commit(); return run,proposal
    except Exception as exc:
        run.error_text=f"CEO plan validation failed: {exc}"; db.session.commit(); return run,None

def _add_tasks(project,plan,owner,fault_after_task=None):
    for index,item in enumerate(plan["tasks"]):
        assignee=Employee.query.filter_by(slug=item["assignee_slug"],active=True).one()
        reviewer=Employee.query.filter_by(slug=item["reviewer_slug"],active=True).one() if item["reviewer_slug"] else None
        db.session.add(Task(project_id=project.id,title=item["title"],objective=item["objective"],status="ASSIGNED",
          priority=plan.get("project",{}).get("priority",project.priority),created_by_employee_id=owner.id,assigned_employee_id=assignee.id,
          reviewer_employee_id=getattr(reviewer,"id",None),required_output=item["required_output"],acceptance_criteria=item["acceptance_criteria"]))
        db.session.flush()
        if fault_after_task is not None and index==fault_after_task: raise RuntimeError("Injected materialization failure")

def materialize_project_plan(proposal,fault_after_task=None):
    if proposal.status!="PENDING" or proposal.payload_json.get("type")!="PROJECT_PLAN": raise ValueError("Proposal is not a pending project plan")
    try:
        plan=validate_plan(proposal.payload_json["plan"]); owner=db.session.get(Employee,proposal.proposed_by_employee_id)
        if plan["mode"]=="NEW_PROJECT":
            project=Project(name=plan["project"]["name"],objective=plan["project"]["objective"],priority=plan["project"]["priority"],status="ACTIVE",owner_employee_id=owner.id)
            db.session.add(project); db.session.flush()
        else: project=db.session.get(Project,plan["project_id"])
        _add_tasks(project,plan,owner,fault_after_task)
        proposal.status="APPROVED"; proposal.review_note="Founder materialized validated CEO plan"; proposal.reviewed_at=now()
        db.session.commit(); return project
    except Exception: db.session.rollback(); raise

SYNTHESIS_FIELDS={"executive_summary","result","key_findings","disagreements_or_risks","unresolved_questions","founder_decisions_required","recommended_next_actions"}
def generate_project_briefing(ceo,project):
    tasks=Task.query.filter_by(project_id=project.id).order_by(Task.id).all()
    messages=WorkMessage.query.filter_by(project_id=project.id).order_by(WorkMessage.created_at).all()
    knowledge=current(project.id)
    contributions=db.session.query(Employee.name,func.sum(ContributionEvent.value)).join(Employee).filter(
      ContributionEvent.project_id==project.id,ContributionEvent.scope=="PROJECT").group_by(Employee.name).all()
    context=f"PROJECT\n#{project.id} {project.name}; status {project.status}; objective: {project.objective}\n\nTASK RESULTS\n"+(
      "\n".join(f"{t.status} {t.title}: {t.result_summary or 'unresolved'}" for t in tasks))
    context+="\n\nREVIEWS / WORK MESSAGES\n"+"\n".join(m.content for m in messages)
    context+="\n\nEFFECTIVE BRAIN\n"+"\n".join(f"{k.kind}: {k.title} — {k.content}" for k in knowledge)
    context+="\n\nPROJECT CONTRIBUTION\n"+"\n".join(f"{n}: {v}" for n,v in contributions)
    context+=f"\n\nPROJECT COST\n{db.session.query(func.coalesce(func.sum(CostEvent.real_cost_delta),0)).filter_by(project_id=project.id).scalar()}"
    prompt=ceo.system_instructions+"\nCEO_PROJECT_SYNTHESIS\nReturn only strict JSON briefing fields."
    run=execute(ceo,"CEO_PROJECT_SYNTHESIS",f"Synthesize Project #{project.id}",project=project,context_override=context,system_prompt_override=prompt,response_schema=SYNTHESIS_SCHEMA)
    try:
        payload=json.loads(run.raw_output)
        if set(payload)!=SYNTHESIS_FIELDS or not all(isinstance(payload[x],str) if x in {"executive_summary","result"} else isinstance(payload[x],list) for x in SYNTHESIS_FIELDS): raise ValueError("Invalid CEO briefing")
        run.parsed_output_json=payload
        db.session.add(WorkMessage(project_id=project.id,sender_employee_id=ceo.id,message_type="REPORT",
          content=payload["executive_summary"],agent_run_id=run.id)); db.session.commit(); return run
    except Exception as exc:
        run.error_text=f"Briefing validation failed: {exc}"; db.session.commit(); return run

``

---

## FILE: eason_one\services\company.py

``python
from decimal import Decimal
from sqlalchemy import func
from ..extensions import db
from ..models import Company, CostEvent

def get_company():
    return Company.query.first()

def spent(company=None):
    company = company or get_company()
    return Decimal(db.session.query(func.coalesce(func.sum(CostEvent.real_cost_delta), 0)).filter_by(company_id=company.id).scalar())

def remaining(company=None):
    company = company or get_company()
    return Decimal(company.real_budget_limit) - spent(company)


``

---

## FILE: eason_one\services\context.py

``python
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

``

---

## FILE: eason_one\services\contributions.py

``python
from decimal import Decimal
from sqlalchemy import func
from ..extensions import db
from ..models import ContributionEvent
def total(employee_id, scope, project_id=None):
    q=db.session.query(func.coalesce(func.sum(ContributionEvent.value),0)).filter_by(employee_id=employee_id,scope=scope)
    if scope=="PROJECT": q=q.filter_by(project_id=project_id)
    return Decimal(q.scalar())
def project_totals(employee_id):
    from ..models import Project
    return db.session.query(Project,func.coalesce(func.sum(ContributionEvent.value),0)).join(
        ContributionEvent,ContributionEvent.project_id==Project.id).filter(
        ContributionEvent.employee_id==employee_id,ContributionEvent.scope=="PROJECT").group_by(Project.id).all()

``

---

## FILE: eason_one\services\costs.py

``python
from decimal import Decimal
from ..extensions import db
from ..models import CostEvent
from .company import get_company, remaining
def calculate(model, input_tokens, output_tokens):
    return (Decimal(input_tokens)*Decimal(model.input_price_per_million)+Decimal(output_tokens)*Decimal(model.output_price_per_million))/Decimal(1_000_000)
def ensure_budget(estimated=Decimal("0")):
    if remaining() <= 0 or estimated > remaining(): raise ValueError("Company real budget is exhausted")
def conservative_estimate(model, prompt_text, max_output_tokens):
    # UTF-8 bytes plus 25% and fixed framing overhead deliberately overestimate.
    byte_bound=len(prompt_text.encode("utf-8"))
    estimated_input=max(1, (byte_bound*5+3)//4 + 256)
    return calculate(model,estimated_input,max_output_tokens)
def record(run):
    existing=CostEvent.query.filter_by(agent_run_id=run.id,category="MODEL").first()
    if existing: return existing
    event=CostEvent(company_id=get_company().id, employee_id=run.employee_id, project_id=run.project_id,
        task_id=run.task_id, agent_run_id=run.id, category="MODEL", description=f"{run.purpose}: {run.model_config.label}",
        internal_credits_delta=0, real_cost_delta=run.real_cost or 0, currency=run.currency)
    db.session.add(event); return event

``

---

## FILE: eason_one\services\employees.py

``python
from ..extensions import db
from ..models import EmployeeModelHistory, now

def change_model(employee, model_config, reason=None):
    current = EmployeeModelHistory.query.filter_by(employee_id=employee.id, ended_at=None).first()
    if current:
        current.ended_at = now()
    employee.current_model = model_config
    db.session.add(EmployeeModelHistory(employee_id=employee.id, model_config_id=model_config.id if model_config else None, reason=reason))
    db.session.commit()
    return employee


``

---

## FILE: eason_one\services\execution.py

``python
from ..extensions import db
from ..models import AgentRun, now
from ..providers import get_provider
from .context import build
from .costs import ensure_budget, calculate, record, conservative_estimate
def execute(employee, purpose, user_request, project=None, task=None, context_override=None, postprocess=None, system_prompt_override=None,response_schema=None):
    if not employee.current_model: raise ValueError("Employee has no model")
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
    framed=f"INSTRUCTIONS:\n{system_prompt}\n\nCONTEXT:\n{context}\n\nUSER REQUEST:\n{user_request}"
    maximum=conservative_estimate(model,framed,model.max_output_tokens)
    ensure_budget(maximum)
    run=AgentRun(employee_id=employee.id,project_id=getattr(project,"id",None),task_id=getattr(task,"id",None),
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
        run.provider_request_id=result.request_id; run.real_cost=calculate(model,result.input_tokens,result.output_tokens)
        run.status="SUCCEEDED"; run.finished_at=now(); record(run); db.session.commit()
        if postprocess:
            try: postprocess(run)
            except Exception as exc:
                run.error_text=f"Downstream processing failed: {exc}"; db.session.commit(); raise
    except Exception as exc:
        run.status="FAILED"; run.error_text=str(exc); run.finished_at=now(); db.session.commit(); raise
    return run

``

---

## FILE: eason_one\services\interviews.py

``python
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

``

---

## FILE: eason_one\services\learning.py

``python
from ..extensions import db
from ..models import EmployeeLearningRecord
def create(employee,title,content,project=None,task=None,source_ref=None,validated=False):
    if not title.strip() or not content.strip(): raise ValueError("Learning title and content are required")
    if task and task.assigned_employee_id!=employee.id: raise ValueError("Task does not belong to employee")
    if task and project and task.project_id!=project.id: raise ValueError("Task does not belong to project")
    row=EmployeeLearningRecord(employee_id=employee.id,project_id=getattr(project,"id",None),task_id=getattr(task,"id",None),
        title=title.strip(),content=content.strip(),source_ref=source_ref or None,validated=bool(validated))
    db.session.add(row); db.session.commit(); return row

``

---

## FILE: eason_one\services\model_configs.py

``python
from decimal import Decimal
from ..extensions import db
from ..models import ModelConfig
from .company import get_company
PROVIDERS={"mock","openai"}
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

## FILE: eason_one\services\projects.py

``python
from ..extensions import db
from ..models import Project
VALID = {"PLANNING","ACTIVE","BLOCKED","REVIEW","COMPLETED","FAILED","CANCELLED","PARKED"}
def create_project(name, objective, owner, priority="MEDIUM", status="PLANNING"):
    if status not in VALID: raise ValueError("Invalid project status")
    project = Project(name=name, objective=objective, owner_employee_id=owner.id, priority=priority, status=status)
    db.session.add(project); db.session.commit()
    return project


``

---

## FILE: eason_one\services\reviews.py

``python
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

``

---

## FILE: eason_one\services\task_execution.py

``python
import json
from ..extensions import db
from ..models import Proposal
from ..schemas import TASK_EXECUTION_SCHEMA
from .execution import execute
from .brain import validate_basis_ids

FIELDS={"result_summary","knowledge_proposals"}
CANDIDATE_FIELDS={"kind","title","content","source_ref","rationale","basis_knowledge_ids"}
def run_task(task):
    employee=task.assigned_employee
    prompt=employee.system_instructions+"\nTASK_EXECUTION\nReturn the structured Task result. Do not claim web research or fabricate citations."
    run=execute(employee,"TASK_EXECUTION",task.objective,task.project,task,system_prompt_override=prompt,response_schema=TASK_EXECUTION_SCHEMA)
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

``

---

## FILE: eason_one\services\tasks.py

``python
from ..extensions import db
from ..models import Task, WorkMessage, now
TRANSITIONS = {
    "TODO":{"ASSIGNED","CANCELLED"}, "ASSIGNED":{"WORKING","CANCELLED"},
    "WORKING":{"BLOCKED","REVIEW","FAILED"}, "BLOCKED":{"WORKING","CANCELLED"},
    "REVIEW":{"DONE","WORKING","BLOCKED"}, "DONE":set(), "FAILED":{"WORKING"}, "CANCELLED":set()
}
def create_task(project, title, objective, creator=None, assignee=None, reviewer=None, **kwargs):
    status = "ASSIGNED" if assignee else "TODO"
    task = Task(project_id=project.id, title=title, objective=objective, status=status,
        created_by_employee_id=getattr(creator,"id",None), assigned_employee_id=getattr(assignee,"id",None),
        reviewer_employee_id=getattr(reviewer,"id",None), **kwargs)
    db.session.add(task); db.session.commit(); return task
def assign(task, employee):
    if task.status != "TODO": raise ValueError("Only TODO tasks can be assigned")
    task.assigned_employee_id=employee.id; transition(task,"ASSIGNED"); return task
def transition(task, target):
    if target not in TRANSITIONS.get(task.status,set()): raise ValueError(f"Invalid transition {task.status} -> {target}")
    task.status=target
    if target=="DONE": task.completed_at=now()
    db.session.commit(); return task
def store_result(task, result):
    if task.status not in {"WORKING","ASSIGNED"}: raise ValueError("Task is not executable")
    if task.status=="ASSIGNED": transition(task,"WORKING")
    task.result_summary=result
    transition(task,"REVIEW")
    return task
def review(task, reviewer, content, accepted):
    if task.status!="REVIEW": raise ValueError("Task is not in review")
    db.session.add(WorkMessage(project_id=task.project_id, task_id=task.id, sender_employee_id=reviewer.id,
        recipient_employee_id=task.assigned_employee_id, message_type="REVIEW", content=content))
    db.session.flush(); transition(task, "DONE" if accepted else "WORKING"); return task


``

---

## FILE: pyproject.toml

``toml
[project]
name = "eason-one"
version = "0.1.0"
requires-python = ">=3.13"
dependencies = ["Flask>=3.1", "Flask-SQLAlchemy>=3.1", "SQLAlchemy>=2.0", "openai>=1.0"]

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

## FILE: README.md

``markdown
# Eason One

The first usable vertical slice of a personal AI-native company operating system. It provides a persistent CEO, Employees, governed Projects and Tasks, explicit model executions, reviews, Founder interviews, Company Brain proposals, and an auditable NT$3,000 real-cost budget.

## Run

```powershell
python -m pip install -e ".[test]"
flask --app run.py seed
flask --app run.py run
```

Open `http://127.0.0.1:5000`. The default database is `instance/eason_one.db`.

The seeded team uses the zero-cost `MockProvider`, so the complete flow works without credentials. To configure a real provider, create an active `ModelConfig` with `provider_key="openai"`, its exact `model_name`, positive input/output prices in TWD, and a maximum output-token limit; then assign it through the employee service and set `OPENAI_API_KEY`. Real-provider zero pricing and currency mismatches are rejected before invocation. Environment variables hold secrets only and cannot override the Employee's organizational model assignment.

## Test

```powershell
pytest -q
```

``

---

## FILE: run.py

``python
from eason_one import create_app

app = create_app()

if __name__ == "__main__":
    app.run(debug=True)


``
