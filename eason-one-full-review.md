# Eason One — Full Code Review Bundle


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

Structured workflows pass strict JSON Schemas through the provider boundary to OpenAI Responses while retaining deterministic application validation. OpenAI response IDs and HTTP request IDs are stored separately; incomplete/refused billable responses retain token usage and exactly one ledger entry before being marked failed. Structured Task results can create pending knowledge Proposals, never authoritative knowledge. Founder-approved Decisions may link only currently effective same-Project or company-level approved basis items through `BASIS_FOR` references in the same transaction.

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
openai<3,>=2.48.0
anthropic<1,>=0.117.0

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
eason_one/schemas.py
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
eason_one/services/meetings.py
eason_one/services/model_configs.py
eason_one/services/projects.py
eason_one/services/reviews.py
eason_one/services/task_execution.py
eason_one/services/tasks.py
eason_one/static/app.css
eason_one/static/meetings.css
eason_one/templates/base.html
eason_one/templates/ceo.html
eason_one/templates/costs.html
eason_one/templates/employee.html
eason_one/templates/employees.html
eason_one/templates/inbox.html
eason_one/templates/interview.html
eason_one/templates/meeting.html
eason_one/templates/meetings.html
eason_one/templates/models.html
eason_one/templates/project.html
eason_one/templates/projects.html
eason_one/templates/run.html
eason_one/templates/unseeded.html
tests/test_core.py
tests/test_hardening.py
tests/test_patch0041.py
tests/test_patch0051.py
tests/test_slice003.py
tests/test_slice004.py
tests/test_slice005.py
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
 "en":{"nav.ceo":"CEO","nav.projects":"Projects","nav.meetings":"Meetings","nav.employees":"Employees","nav.inbox":"Founder Inbox","nav.costs":"Cost Control","nav.models":"Models","lang.en":"EN","lang.zh":"繁中","button.execute":"Execute CEO Request","button.interview":"Interview Employee"},
 "zh-TW":{"nav.ceo":"CEO","nav.projects":"專案","nav.meetings":"會議","nav.employees":"員工","nav.inbox":"創辦人收件匣","nav.costs":"成本控制","nav.models":"模型","lang.en":"EN","lang.zh":"繁中","button.execute":"執行 CEO 指令","button.interview":"訪談員工"}
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
        r=OpenAI(api_key=os.environ["OPENAI_API_KEY"]).responses.create(**kwargs)
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
        message=Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"]).messages.create(**kwargs)
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

## FILE: eason_one\routes.py

``python
from decimal import Decimal
from flask import Blueprint, render_template, request, redirect, url_for, flash, abort, session
from sqlalchemy import func
from .extensions import db
from .models import *
from .services.company import get_company, spent, remaining
from .services.projects import create_project,classify
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
from .services import meetings as meeting_service

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
    live_ids=Project.query.with_entities(Project.id).filter_by(environment="LIVE")
    metrics={"active_projects":Project.query.filter(Project.environment=="LIVE",Project.status.in_(["PLANNING","ACTIVE","BLOCKED","REVIEW"])).count(),
      "active_tasks":Task.query.filter(Task.project_id.in_(live_ids),Task.status.in_(["ASSIGNED","WORKING","REVIEW"])).count(),
      "blocked":Task.query.filter(Task.project_id.in_(live_ids),Task.status=="BLOCKED").count(),
      "pending":Proposal.query.filter_by(status="PENDING").count()}
    projects=Project.query.filter_by(environment="LIVE").order_by(Project.updated_at.desc()).all()
    recent=Task.query.filter(Task.project_id.in_(live_ids),Task.status=="DONE").order_by(Task.completed_at.desc()).limit(6).all()
    latest_ceo=AgentRun.query.filter_by(purpose="CEO_FOUNDER_REQUEST",status="SUCCEEDED").order_by(AgentRun.started_at.desc()).first()
    return render_template("ceo.html",company=company,spent=spent(),remaining=remaining(),metrics=metrics,projects=projects,recent=recent,latest_ceo=latest_ceo)

@bp.route("/projects")
def projects(): return render_template("projects.html",projects=Project.query.order_by(Project.updated_at.desc()).all(),employees=Employee.query.filter_by(active=True).all())
@bp.route("/projects/existing",methods=["POST"])
def existing_project():
    try:
        create_project(request.form["name"],request.form["objective"],Employee.query.get_or_404(request.form["owner_id"]),
          priority=request.form["priority"],status=request.form["status"],environment="LIVE",origin="EXISTING",
          current_state_summary=request.form["current_state_summary"],known_constraints=request.form.get("known_constraints"),
          next_milestone=request.form.get("next_milestone"))
    except Exception as ex: flash(str(ex),"error")
    return redirect(url_for("main.projects"))
@bp.route("/projects/<int:id>/classification",methods=["POST"])
def project_classification(id):
    try: classify(Project.query.get_or_404(id),request.form["environment"])
    except Exception as ex: flash(str(ex),"error")
    return redirect(request.referrer or url_for("main.projects"))
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
@bp.route("/meetings",methods=["GET","POST"])
def meetings():
    if request.method=="POST":
        try:
            chair=Employee.query.get_or_404(request.form["chair_employee_id"])
            participants=Employee.query.filter(Employee.id.in_([int(x) for x in request.form.getlist("participant_ids")])).all()
            project=Project.query.get(request.form.get("project_id")) if request.form.get("project_id") else None
            meeting_service.create(request.form["title"],request.form["purpose"],request.form["agenda"],chair,participants,project,
              request.form["max_rounds"],request.form["token_limit"],request.form["real_cost_limit_twd"])
        except Exception as ex: flash(str(ex),"error")
        return redirect(url_for("main.meetings"))
    rows=[]
    for meeting in Meeting.query.order_by(Meeting.created_at.desc()).all():
        tokens,cost=meeting_service.usage(meeting); rows.append((meeting,tokens,cost))
    return render_template("meetings.html",rows=rows,employees=Employee.query.filter_by(active=True).all(),projects=Project.query.filter_by(environment="LIVE").all())
@bp.route("/meetings/<int:id>")
def meeting_room(id):
    meeting=Meeting.query.get_or_404(id); tokens,cost=meeting_service.usage(meeting)
    messages=MeetingMessage.query.filter_by(meeting_id=id).order_by(MeetingMessage.id).all()
    return render_template("meeting.html",meeting=meeting,tokens=tokens,cost=cost,messages=messages,recent=messages[-6:],feedback_signals=sorted(meeting_service.SIGNALS))
def _meeting_action(id,fn,*args):
    meeting=Meeting.query.get_or_404(id)
    try: fn(meeting,*args)
    except Exception as ex: flash(str(ex),"error")
    return redirect(url_for("main.meeting_room",id=id))
@bp.route("/meetings/<int:id>/start",methods=["POST"])
def meeting_start(id): return _meeting_action(id,meeting_service.start)
@bp.route("/meetings/<int:id>/advance",methods=["POST"])
def meeting_advance(id): return _meeting_action(id,meeting_service.advance)
@bp.route("/meetings/<int:id>/join",methods=["POST"])
def meeting_join(id): return _meeting_action(id,meeting_service.join_founder)
@bp.route("/meetings/<int:id>/intervene",methods=["POST"])
def meeting_intervene(id): return _meeting_action(id,meeting_service.intervene,request.form["content"])
@bp.route("/meetings/<int:id>/command",methods=["POST"])
def meeting_command(id): return _meeting_action(id,meeting_service.command,request.form["kind"],request.form["content"])
@bp.route("/meetings/<int:id>/end",methods=["POST"])
def meeting_end(id): return _meeting_action(id,meeting_service.end_and_synthesize)
@bp.route("/meetings/<int:id>/stop",methods=["POST"])
def meeting_stop(id): return _meeting_action(id,meeting_service.stop,request.form.get("reason"))
@bp.route("/meeting-messages/<int:id>/feedback",methods=["POST"])
def meeting_feedback(id):
    message=MeetingMessage.query.get_or_404(id)
    try: meeting_service.feedback(message,request.form["signal"],request.form.get("note"))
    except Exception as ex: flash(str(ex),"error")
    return redirect(url_for("main.meeting_room",id=message.meeting_id))
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
        if not target.founder_approved: raise ValueError(f"{kind} target must be Founder-approved")
        project_id=kwargs.get("project_id")
        if project_id is None and target.project_id is not None: raise ValueError("Company-level knowledge cannot target Project knowledge")
        if project_id is not None and target.project_id not in {None,project_id}: raise ValueError("Knowledge target belongs to an unrelated Project")
        if kind=="KILLED" and target.kind!="HYPOTHESIS": raise ValueError("KILLED target must be a HYPOTHESIS")
    return True
def build_knowledge(kind,title,content,**kwargs):
    kwargs.pop("basis_knowledge_ids",None)
    validate(kind,**kwargs); return KnowledgeItem(kind=kind,title=title,content=content,**kwargs)
def validate_basis_ids(ids,project_id):
    if not isinstance(ids,list) or len(set(ids))!=len(ids): raise ValueError("Invalid basis IDs")
    effective={item.id:item for item in current(project_id)}
    allowed={"FACT","HYPOTHESIS","EVIDENCE","CORRECTION"}
    rows=[]
    for ident in ids:
        item=db.session.get(KnowledgeItem,ident)
        if not item or not item.founder_approved: raise ValueError("Decision basis must be Founder-approved")
        if item.project_id not in {None,project_id}: raise ValueError("Decision basis belongs to an unrelated Project")
        if ident not in effective or item.kind not in allowed: raise ValueError("Decision basis must be currently effective FACT, HYPOTHESIS, EVIDENCE, or CORRECTION")
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
    for p in Project.query.filter(Project.status.in_(["PLANNING","ACTIVE","BLOCKED","REVIEW"]),Project.environment=="LIVE").order_by(Project.id):
        counts=dict(db.session.query(Task.status,func.count(Task.id)).filter_by(project_id=p.id).group_by(Task.status).all())
        recent=[x.title for x in Task.query.filter_by(project_id=p.id,status="DONE").order_by(Task.completed_at.desc()).limit(3)]
        cost=Decimal(db.session.query(func.coalesce(func.sum(CostEvent.real_cost_delta),0)).filter_by(project_id=p.id).scalar())
        summaries.append(f"Project #{p.id}: {p.name}; origin: {p.origin}; status: {p.status}; priority: {p.priority}; owner: {p.owner.name}; "
          f"tasks: {sum(counts.values())}; blocked: {counts.get('BLOCKED',0)}; review: {counts.get('REVIEW',0)}; "
          f"recent completed: {', '.join(recent) or '-'}; cost: {p.owner.current_model.currency} {cost}; "
          f"current state: {p.current_state_summary or '-'}; constraints: {p.known_constraints or '-'}; next milestone: {p.next_milestone or '-'}")
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
    if run.status!="SUCCEEDED": return run,None
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
    if run.status!="SUCCEEDED": return run
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
from dataclasses import dataclass
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

def estimate_execution(model,system_prompt,context,user_request,max_output_tokens=None):
    output_tokens=model.max_output_tokens if max_output_tokens is None else int(max_output_tokens)
    framed=execution_frame(system_prompt,context,user_request)
    byte_bound=len(framed.encode("utf-8"))
    input_tokens=max(1,(byte_bound*5+3)//4+256)
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
    estimate=estimate_execution(model,system_prompt,context,user_request,model.max_output_tokens)
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
        run.real_cost=calculate(model,result.input_tokens,result.output_tokens)
        provider_name=model.provider_key
        if result.cache_creation_input_tokens or result.cache_read_input_tokens:
            run.status="FAILED"; run.error_text=(f"{provider_name} returned unexpected prompt-cache usage; "
              "exact cache-cost reconciliation is not enabled")
        elif result.refusal:
            run.status="FAILED"; run.error_text=f"{provider_name} refusal: {result.refusal}"
        elif result.status!="completed":
            detail=f": {result.incomplete_reason}" if result.incomplete_reason else ""
            run.status="FAILED"; run.error_text=f"{provider_name} response {result.status}{detail}"
        else: run.status="SUCCEEDED"
        run.finished_at=now(); record(run); db.session.commit()
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
    tokens=db.session.query(func.coalesce(func.sum(AgentRun.input_tokens+AgentRun.output_tokens),0)).filter_by(meeting_id=meeting.id).scalar()
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

def _check_step(meeting,employee,system_prompt,context,user_request,allow_final=False):
    if meeting.status!=ACTIVE: raise ValueError("Meeting is not ACTIVE")
    if not allow_final and meeting.current_round>=meeting.max_rounds: raise ValueError("Meeting maximum rounds reached")
    tokens,cost=usage(meeting)
    estimate=estimate_execution(employee.current_model,system_prompt,context,user_request)
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
    _check_step(meeting,meeting.chair,prompt,context,user_request,allow_final=True)
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

## FILE: eason_one\services\projects.py

``python
from ..extensions import db
from ..models import Project
VALID = {"PLANNING","ACTIVE","BLOCKED","REVIEW","COMPLETED","FAILED","CANCELLED","PARKED"}
def create_project(name, objective, owner, priority="MEDIUM", status="PLANNING",environment="LIVE",origin="NEW",**kwargs):
    if status not in VALID: raise ValueError("Invalid project status")
    if environment not in {"LIVE","SMOKE","ARCHIVED"} or origin not in {"NEW","EXISTING"}: raise ValueError("Invalid Project classification")
    project = Project(name=name,objective=objective,owner_employee_id=owner.id,priority=priority,status=status,
      environment=environment,origin=origin,**kwargs)
    db.session.add(project); db.session.commit()
    return project
def classify(project,environment):
    if environment not in {"LIVE","SMOKE","ARCHIVED"}: raise ValueError("Invalid Project classification")
    project.environment=environment; db.session.commit(); return project

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
    if task.status not in {"ASSIGNED","WORKING"}: raise ValueError("Task is not executable")
    employee=task.assigned_employee
    prompt=employee.system_instructions+"\nTASK_EXECUTION\nReturn the structured Task result. Do not claim web research or fabricate citations."
    run=execute(employee,"TASK_EXECUTION",task.objective,task.project,task,system_prompt_override=prompt,response_schema=TASK_EXECUTION_SCHEMA)
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

## FILE: eason_one\static\app.css

``css
:root{--bg:#061016;--panel:#0a1820;--line:#183746;--cyan:#48e7ff;--text:#d6edf2;--muted:#75939d;--green:#61f5a6}*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 80% 0,#0c2a36,transparent 35%),var(--bg);color:var(--text);font:14px Inter,Segoe UI,sans-serif;display:flex;min-height:100vh}aside{width:220px;border-right:1px solid var(--line);padding:24px 16px;position:fixed;height:100vh;background:#071219cc}.brand{font-weight:800;letter-spacing:2px;color:var(--cyan);font-size:17px}.brand small{display:block;color:var(--muted);font-size:9px;margin:7px 0 35px}nav a{display:block;color:#9eb7bf;text-decoration:none;padding:12px;margin:4px 0;border-left:2px solid transparent}nav a:hover{color:var(--cyan);background:#0d2029;border-color:var(--cyan)}main{margin-left:220px;width:calc(100% - 220px);padding:32px;max-width:1600px}header{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:24px}h1{font-size:30px;margin:5px 0}h2{font-size:15px;text-transform:uppercase;letter-spacing:1px}h3,label{font-size:10px;letter-spacing:1.5px;color:var(--cyan)}p{color:#a4bcc3}.budget{text-align:right;font-size:24px;color:var(--green)}.budget small{display:block;color:var(--muted);font-size:9px}.metrics,.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:20px}.metrics>div,.panel,.employee{background:linear-gradient(135deg,#0d2029dd,#08151cdd);border:1px solid var(--line);padding:18px}.metrics b{display:block;font-size:18px;color:#eefcff}.metrics span,.stats small{display:block;color:var(--muted);font-size:9px;margin-top:6px}.grid{display:grid;grid-template-columns:1.4fr 1fr;gap:18px}.panel{margin-bottom:18px}.command textarea{min-height:76px}.row{display:flex;justify-content:space-between;gap:15px;padding:13px 0;border-bottom:1px solid #142b35;color:var(--text);text-decoration:none}.row span{min-width:0}.row small{display:block;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:550px;margin-top:4px}.row i,.task i{color:var(--cyan);font-size:10px;font-style:normal}.task{border:1px solid var(--line);padding:15px;margin:12px 0}.task>div{display:flex;justify-content:space-between}.task small{display:block;color:var(--muted)}input,textarea,select{width:100%;background:#061117;color:var(--text);border:1px solid #245064;padding:11px;margin:5px 0;font:inherit}textarea{min-height:70px}button,.button{background:var(--cyan);color:#03202a;border:0;padding:10px 15px;font-weight:700;cursor:pointer;text-decoration:none;display:inline-block;margin:5px 3px 5px 0}.secondary{background:#18323e;color:var(--text)}pre{white-space:pre-wrap;word-break:break-word;color:#a9cbd3;font:12px Consolas,monospace}.cards{grid-template-columns:repeat(auto-fit,minmax(280px,1fr))}.employee{color:var(--text);text-decoration:none;transition:.2s}.employee:hover{border-color:var(--cyan);transform:translateY(-2px)}.employee h2{text-transform:none;font-size:21px;margin:8px 0}.orb{width:8px;height:8px;border-radius:50%;background:var(--green);box-shadow:0 0 12px var(--green);float:right}.stats{display:grid;grid-template-columns:1fr 1fr;gap:10px;border-top:1px solid var(--line);padding-top:13px}.employee footer{color:var(--muted);font-size:10px;margin-top:15px}.chat>div{max-width:80%;padding:12px;margin:10px 0;border-left:2px solid var(--cyan);background:#07141a}.chat .employee{margin-left:auto;border:0;border-right:2px solid var(--green)}.muted{color:var(--muted)}.flash{padding:12px;border:1px solid var(--line);margin-bottom:15px}.flash.ok{border-color:#276f50}.flash.error{border-color:#8d3847}@media(max-width:800px){aside{position:static;width:100%;height:auto}body{display:block}main{margin:0;width:100%;padding:18px}.grid{grid-template-columns:1fr}nav a{display:inline-block}.brand small{margin-bottom:10px}}

``

---

## FILE: eason_one\static\meetings.css

``css
.project-smoke,.project-archived{opacity:.55}.project-archived{filter:saturate(.35)}.row>a{color:var(--text);text-decoration:none;min-width:0}.meeting-card .stats{grid-template-columns:repeat(3,1fr)}.secondary-card{opacity:.65}.avatar-strip{display:flex;gap:6px;margin:14px 0}.mini-avatar,.avatar-large{display:grid;place-items:center;border-radius:50%;background:#12333f;border:1px solid var(--cyan);color:var(--cyan);font-weight:800}.mini-avatar{width:30px;height:30px;font-size:10px}.meeting-layout{display:grid;grid-template-columns:minmax(520px,1.6fr) minmax(300px,.8fr);gap:18px}.meeting-stage{position:relative;min-height:590px;border:1px solid var(--line);background:radial-gradient(ellipse at center,#12303b 0,#08151c 50%,#061016 76%);overflow:hidden}.table-core{position:absolute;inset:34% 27%;border:1px solid #2a687a;border-radius:50%;display:grid;place-items:center;text-align:center;color:var(--cyan);padding:25px;box-shadow:inset 0 0 40px #092630}.table-core small{color:var(--muted)}.seat{position:absolute;width:190px;text-align:center}.seat small{display:block;color:var(--muted)}.avatar-large{width:58px;height:58px;margin:0 auto 7px}.seat-1{top:5%;left:calc(50% - 95px)}.seat-2{top:40%;left:3%}.seat-3{top:40%;right:3%}.seat-4{bottom:4%;left:18%}.seat-5{bottom:4%;right:18%}.seat-6{top:8%;right:4%}.speech{margin-top:10px;padding:10px;background:#0b222c;border:1px solid #225567;border-radius:8px;text-align:left;font-size:11px;max-height:100px;overflow:auto}.meeting-side{position:static;width:auto;height:auto;border:0;padding:0;background:none}.meeting-side details{border:1px solid var(--line);background:#091820;margin-bottom:10px;padding:14px}.meeting-side summary{color:var(--cyan);font-size:11px;letter-spacing:1px;cursor:pointer}.transcript{max-height:400px;overflow:auto}.transcript>div{border-bottom:1px solid var(--line);padding:9px 0}.transcript small{color:var(--muted)}.transcript form{display:grid;grid-template-columns:1fr 1.5fr auto;gap:4px}.control-panel{margin-top:18px}.control-panel form{display:inline-block;vertical-align:top;min-width:145px}.control-panel input{width:260px}@media(max-width:1050px){.meeting-layout{grid-template-columns:1fr}.meeting-stage{min-height:540px}}@media(max-width:800px){.meeting-stage{min-height:650px}.seat{position:relative!important;inset:auto!important;display:inline-block;width:48%;vertical-align:top;margin:15px 0}.table-core{position:relative;inset:auto;margin:20px auto;width:70%;height:150px}.control-panel form,.control-panel input{display:block;width:100%}}

``

---

## FILE: eason_one\templates\base.html

``html
<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>{% block title %}Eason One{% endblock %}</title>
<link rel="stylesheet" href="{{url_for('static',filename='app.css')}}">
<link rel="stylesheet" href="{{url_for('static',filename='meetings.css')}}"></head>
<body><aside><div class="brand">EASON ONE<small>FOUNDER COMMAND OS</small></div>
<nav><a href="/ceo">{{t('nav.ceo')}}</a><a href="/projects">{{t('nav.projects')}}</a>
<a href="/meetings">{{t('nav.meetings')}}</a><a href="/employees">{{t('nav.employees')}}</a>
<a href="/models">{{t('nav.models')}}</a><a href="/inbox">{{t('nav.inbox')}}</a>
<a href="/costs">{{t('nav.costs')}}</a></nav>
<div class="languages"><form method="post" action="/language/en"><input type="hidden" name="next" value="{{request.path}}"><button>{{t('lang.en')}}</button></form><span>|</span><form method="post" action="/language/zh-TW"><input type="hidden" name="next" value="{{request.path}}"><button>{{t('lang.zh')}}</button></form></div>
</aside><main>{% with ms=get_flashed_messages(with_categories=true) %}{% for c,m in ms %}<div class="flash {{c}}">{{m}}</div>{% endfor %}{% endwith %}{% block content %}{% endblock %}</main></body></html>

``

---

## FILE: eason_one\templates\ceo.html

``html
{% extends "base.html" %}{% block content %}<header><div><label>CEO OPERATING INTERFACE</label><h1>Company Command</h1></div><div class="budget">NT$ {{'%.2f'|format(remaining)}} <small>REMAINING / {{'%.0f'|format(company.real_budget_limit)}}</small></div></header>
<section class="metrics"><div><b>{{metrics.active_projects}}</b><span>Active Projects</span></div><div><b>{{metrics.active_tasks}}</b><span>Active Tasks</span></div><div><b>{{metrics.blocked}}</b><span>Blocked</span></div><div><b>{{metrics.pending}}</b><span>Founder Attention</span></div><div><b>NT$ {{'%.2f'|format(spent)}}</b><span>Real Spend</span></div></section>
<section class="panel command"><label>FOUNDER → CEO</label><form method="post"><textarea name="request" required placeholder="Create a Beauty LINE Consultation project and investigate whether this idea is worth pursuing."></textarea><button>{{t('button.execute')}}</button></form></section>{% if latest_ceo and latest_ceo.parsed_output_json %}<section class="panel"><label>CEO / {{latest_ceo.parsed_output_json.get('mode')}}</label><h2>{{latest_ceo.parsed_output_json.get('executive_response')}}</h2><a class="button" href="/runs/{{latest_ceo.id}}">Audit / Run Details</a></section>{% endif %}
<div class="grid"><section class="panel"><h2>Project Radar</h2>{% for p in projects %}<a class="row" href="/projects/{{p.id}}"><span><b>{{p.name}}</b><small>{{p.status}} · {{p.tasks|length}} tasks</small></span><i>{{p.priority}}</i></a>{% else %}<p class="muted">No projects. Issue a Founder request above.</p>{% endfor %}</section>
<section class="panel"><h2>Recently Completed</h2>{% for t in recent %}<div class="row"><span><b>{{t.title}}</b><small>{{t.project.name}}</small></span><i>DONE</i></div>{% else %}<p class="muted">No completed tasks yet.</p>{% endfor %}</section></div>{% endblock %}

``

---

## FILE: eason_one\templates\costs.html

``html
{% extends "base.html" %}{% block content %}<header><div><label>REAL MONEY / AUDIT LEDGER</label><h1>Cost Control</h1></div><div class="budget">NT$ {{'%.2f'|format(remaining)}}<small>REMAINING</small></div></header><section class="metrics"><div><b>NT$ {{'%.2f'|format(company.real_budget_limit)}}</b><span>Hard Budget</span></div><div><b>NT$ {{'%.6f'|format(spent)}}</b><span>Spent</span></div><div><b>NT$ {{'%.2f'|format(remaining)}}</b><span>Remaining</span></div></section><section class="panel"><h2>Auditable Ledger</h2>{% for e in events %}<a class="row" href="/runs/{{e.agent_run_id}}"><span><b>{{e.category}} · {{e.description}}</b><small>Employee {{e.employee_id}} · Project {{e.project_id or '-'}} · Task {{e.task_id or '-'}}</small></span><i>NT$ {{e.real_cost_delta}}</i></a>{% else %}<p class="muted">No cost events yet.</p>{% endfor %}</section>{% endblock %}

``

---

## FILE: eason_one\templates\employee.html

``html
{% extends "base.html" %}{% block content %}<header><div><label>{{e.department.name if e.department else 'CEO OFFICE / ASSURANCE'}}</label><h1>{{e.name}}</h1><p>{{e.role_description}}</p></div><a class="button" href="/employees/{{e.id}}/interview">{{t('button.interview')}}</a></header><section class="metrics"><div><b>{{e.position.name}} / L{{e.position.level}}</b><span>Position</span></div><div><b>{{e.manager.name if e.manager else 'Founder'}}</b><span>Manager</span></div><div><b>{{e.salary_credits_per_week}}</b><span>Weekly Internal Credits</span></div><div><b>{{cc}}</b><span>Company Contribution</span></div></section><div class="grid"><section class="panel"><h2>Work History</h2>{% for task in tasks %}<a class="row" href="/projects/{{task.project_id}}"><span><b>{{task.title}}</b><small>{{task.project.name}} · {{task.result_summary or 'No result yet'}}</small></span><i>{{task.status}}</i></a>{% else %}<p class="muted">No tasks.</p>{% endfor %}<h2>Learning / Experience</h2>{% for item in learning %}<div class="row"><span><b>{{item.title}}</b><small>{{item.content}}</small></span><i>{{'VALIDATED' if item.validated else 'UNVALIDATED'}}</i></div>{% else %}<p class="muted">No learning records.</p>{% endfor %}</section><section><div class="panel"><h2>Model Assignment</h2><b>{{e.current_model.label if e.current_model else 'None'}}</b><p>{{e.current_model.provider_key if e.current_model else ''}} / {{e.current_model.model_name if e.current_model else ''}}</p><form method="post" action="/employees/{{e.id}}/model"><select name="model_config_id">{% for model in model_configs %}<option value="{{model.id}}" {{'selected' if e.current_model_config_id==model.id}}>{{model.label}} · {{model.model_name}}</option>{% endfor %}</select><input name="reason" placeholder="Reason for reassignment"><button>Change Model</button></form><h3>Model History</h3>{% for h in history %}<p>{{h.model_config.label if h.model_config else 'None'}} · {{h.reason or ''}}</p>{% endfor %}</div><div class="panel"><h2>Project Contribution</h2>{% for project,value in project_contributions %}<div class="row"><b>{{project.name}}</b><i>{{value}}</i></div>{% else %}<p class="muted">No Project Contribution.</p>{% endfor %}</div><div class="panel"><h2>Add Learning Record</h2><form method="post" action="/employees/{{e.id}}/learning"><input name="title" required placeholder="Title"><textarea name="content" required placeholder="Evidence-based learning"></textarea><input name="source_ref" placeholder="Source reference"><select name="project_id"><option value="">Company-level</option>{% for project in projects %}<option value="{{project.id}}">{{project.name}}</option>{% endfor %}</select><label><input type="checkbox" name="validated" value="1"> Founder validated</label><button>Add Learning</button></form></div><div class="panel"><h2>Execution Audit</h2>{% for run in runs %}<a class="row" href="/runs/{{run.id}}"><span><b>{{run.purpose}}</b><small>{{run.started_at}}</small></span><i>{{run.status}}</i></a>{% else %}<p class="muted">No runs.</p>{% endfor %}</div></section></div>{% endblock %}

``

---

## FILE: eason_one\templates\employees.html

``html
{% extends "base.html" %}{% block content %}<header><div><label>PERSISTENT ORGANIZATION</label><h1>Employees</h1></div></header><section class="cards">{% for r in rows %}<a class="employee" href="/employees/{{r.e.id}}"><div class="orb"></div><label>{{r.e.department.name if r.e.department else 'CEO OFFICE / ASSURANCE'}}</label><h2>{{r.e.name}}</h2><p>{{r.e.position.name}} · L{{r.e.position.level}}</p><div class="stats"><span>{{r.active_tasks|length}}<small>ACTIVE TASKS</small></span><span>{{r.project_contribution}}<small>PROJECT CONTRIB.</small></span><span>{{r.company_contribution}}<small>COMPANY CONTRIB.</small></span><span>NT${{'%.3f'|format(r.cost)}}<small>REAL COST</small></span></div><footer>{{r.e.current_model.label if r.e.current_model else 'NO MODEL'}} · {{r.tokens}} TOKENS</footer></a>{% endfor %}</section>{% endblock %}

``

---

## FILE: eason_one\templates\inbox.html

``html
{% extends "base.html" %}{% block content %}<header><div><label>GOVERNANCE</label><h1>Founder Inbox</h1></div></header><section class="panel">{% for p in proposals %}<div class="task"><div><b>Proposal #{{p.id}} · {{p.payload_json.get('type','KNOWLEDGE')}}</b><small>{{p.status}} · Employee {{p.proposed_by_employee_id}}</small></div><pre>{{p.payload_json|tojson(indent=2)}}</pre>{% if p.status=='PENDING' %}{% if p.payload_json.get('type')=='PROJECT_PLAN' %}<form method="post" action="/inbox/{{p.id}}/materialize"><button>Confirm Governed Plan</button></form><form method="post" action="/inbox/{{p.id}}/reject"><button class="secondary">Reject</button></form>{% else %}<form method="post" action="/inbox/{{p.id}}/knowledge-review"><button name="decision" value="APPROVED">Approve Knowledge</button><button name="decision" value="REJECTED" class="secondary">Reject</button></form><details><summary>Correct before approval</summary><form method="post" action="/inbox/{{p.id}}/knowledge-review"><input type="hidden" name="decision" value="CORRECTED"><select name="kind"><option>FACT</option><option>HYPOTHESIS</option><option>EVIDENCE</option><option>DECISION</option><option>CORRECTION</option><option>KILLED</option></select><input name="title" required value="{{p.payload_json.get('title','')}}"><textarea name="content" required>{{p.payload_json.get('content','')}}</textarea><input name="source_ref" value="{{p.payload_json.get('source_ref','') or ''}}" placeholder="Evidence source"><input name="rationale" value="{{p.payload_json.get('rationale','') or ''}}" placeholder="Decision rationale"><input name="target_knowledge_id" value="{{p.payload_json.get('target_knowledge_id','') or ''}}" placeholder="Target knowledge ID"><input name="note" placeholder="Founder correction note"><button>Materialize Corrected Knowledge</button></form></details>{% endif %}{% endif %}</div>{% else %}<p class="muted">No proposals.</p>{% endfor %}</section>{% endblock %}

``

---

## FILE: eason_one\templates\interview.html

``html
{% extends "base.html" %}{% block content %}<header><div><label>FOUNDER INSPECTION CHANNEL</label><h1>Interview · {{e.name}}</h1><p>Conversation persists but cannot mutate policy or authoritative state.</p></div></header><section class="panel chat">{% for m in interview.messages %}<div class="{{m.speaker|lower}}"><label>{{m.speaker}}</label><p>{{m.content}}</p></div>{% else %}<p class="muted">Ask about current work, decisions, blockers, or lessons.</p>{% endfor %}<form method="post"><textarea name="content" required placeholder="Ask {{e.name}}…"></textarea><button>Send & Execute</button></form></section>{% endblock %}

``

---

## FILE: eason_one\templates\meeting.html

``html
{% extends "base.html" %}{% block content %}<header><div><label>MEETING / {{meeting.status}}</label><h1>{{meeting.title}}</h1><p>{{meeting.purpose}}</p></div><div class="budget">TWD {{cost}}<small>{{tokens}} TOKENS · ROUND {{meeting.current_round}}/{{meeting.max_rounds}}</small></div></header><div class="meeting-layout"><section class="meeting-stage"><div class="table-core">EASON ONE<br><small>{{meeting.agenda}}</small></div>{% for participant in meeting.participants %}<div class="seat seat-{{loop.index}}"><div class="avatar-large">{{participant.employee.name.split()|map('first')|join}}</div><b>{{participant.employee.name}}</b><small>{{participant.employee.position.name}} · L{{participant.employee.position.level}}</small>{% set spoken=recent|selectattr('employee_id','equalto',participant.employee_id)|list %}{% if spoken %}<div class="speech">{{spoken[-1].content}}</div>{% endif %}</div>{% endfor %}</section><aside class="meeting-side"><details open><summary>LIVE SUMMARY</summary>{% set s=meeting.current_summary_json or {} %}{% for key in ['current_topic','agreement','disagreement','evidence','pending_question','next_likely_action','founder_decision_required'] %}<label>{{key.replace('_',' ')|upper}}</label><p>{{s.get(key,'Waiting for the first meeting step.')}}</p>{% endfor %}</details><details><summary>TRANSCRIPT</summary><div class="transcript">{% for m in messages %}<div><small>R{{m.round_number}} · {{m.speaker_type}} · {{m.employee.name if m.employee else 'Founder/System'}} · {{m.created_at.strftime('%H:%M')}}</small><p>{{m.content}}</p>{% if m.speaker_type=='EMPLOYEE' %}<form method="post" action="/meeting-messages/{{m.id}}/feedback"><select name="signal">{% for signal in feedback_signals %}<option>{{signal}}</option>{% endfor %}</select><input name="note" placeholder="Optional Founder note"><button>Signal</button></form>{% endif %}</div>{% endfor %}</div></details>{% if meeting.minutes_json %}<details open><summary>MEETING MINUTES</summary><pre>{{meeting.minutes_json|tojson(indent=2)}}</pre></details>{% endif %}</aside></div><section class="panel control-panel"><label>FOUNDER CONTROL / {{'JOINED' if meeting.founder_joined_at else 'OBSERVE'}}</label>{% if meeting.status=='PLANNED' %}<form method="post" action="/meetings/{{meeting.id}}/start"><button>Start Meeting</button></form>{% elif meeting.status=='ACTIVE' %}<form method="post" action="/meetings/{{meeting.id}}/advance"><button>Advance Meeting</button></form>{% if not meeting.founder_joined_at %}<form method="post" action="/meetings/{{meeting.id}}/join"><button>Join Meeting</button></form>{% else %}<form method="post" action="/meetings/{{meeting.id}}/intervene"><input name="content" required placeholder="Founder intervention"><button>Send Intervention</button></form>{% endif %}<form method="post" action="/meetings/{{meeting.id}}/command"><input type="hidden" name="kind" value="FOCUS"><input name="content" required placeholder="Temporary focus for next round"><button>Focus Discussion</button></form><form method="post" action="/meetings/{{meeting.id}}/command"><input type="hidden" name="kind" value="REQUEST_EVIDENCE"><input name="content" value="Surface evidence or explicitly state that evidence is unavailable."><button>Request Evidence</button></form><form method="post" action="/meetings/{{meeting.id}}/end"><button>End & Synthesize</button></form><form method="post" action="/meetings/{{meeting.id}}/stop"><input name="reason" placeholder="Termination reason"><button class="secondary">Stop Meeting</button></form>{% endif %}</section>{% endblock %}

``

---

## FILE: eason_one\templates\meetings.html

``html
{% extends "base.html" %}{% block content %}<header><div><label>COLLABORATION</label><h1>Meeting Lobby</h1><p>Explicit, bounded organizational discussion.</p></div></header><div class="grid"><section><h2>Meetings</h2><div class="cards">{% for meeting,tokens,cost in rows %}<a class="employee meeting-card {{'secondary-card' if meeting.status in ['ENDED','TERMINATED_BY_FOUNDER']}}" href="/meetings/{{meeting.id}}"><label>{{meeting.status}}</label><h2>{{meeting.title}}</h2><p>{{meeting.project.name if meeting.project else 'Company-level'}} · Chair {{meeting.chair.name}}</p><div class="avatar-strip">{% for participant in meeting.participants %}<span class="mini-avatar" title="{{participant.employee.name}}">{{participant.employee.name.split()|map('first')|join}}</span>{% endfor %}</div><div class="stats"><span>{{meeting.current_round}} / {{meeting.max_rounds}}<small>ROUND</small></span><span>{{tokens}}<small>TOKENS</small></span><span>TWD {{cost}}<small>ACTUAL COST</small></span></div></a>{% else %}<p class="muted">No Meetings.</p>{% endfor %}</div></section><section class="panel"><h2>Plan Meeting</h2><form method="post"><input name="title" required placeholder="Meeting title"><textarea name="purpose" required placeholder="Purpose"></textarea><textarea name="agenda" required placeholder="Agenda"></textarea><select name="project_id"><option value="">Company-level</option>{% for p in projects %}<option value="{{p.id}}">{{p.name}}</option>{% endfor %}</select><select name="chair_employee_id">{% for e in employees %}<option value="{{e.id}}">{{e.name}} chairs</option>{% endfor %}</select><label>Participants</label><select name="participant_ids" multiple required size="6">{% for e in employees %}<option value="{{e.id}}">{{e.name}} · {{e.position.name}}</option>{% endfor %}</select><input name="max_rounds" type="number" min="1" value="3"><input name="token_limit" type="number" min="1" value="12000"><input name="real_cost_limit_twd" type="number" min="0" step="0.0001" value="100"><button>Plan Meeting</button></form></section></div>{% endblock %}

``

---

## FILE: eason_one\templates\models.html

``html
{% extends "base.html" %}{% block content %}
<header><div><label>FOUNDER CONFIGURATION</label><h1>Model Management</h1><p>API secrets remain in environment variables. Company remaining budget: TWD {{remaining}}.</p></div></header>
<div class="grid"><section class="panel"><h2>Configured Models</h2>
{% for m in models %}<div class="task"><div><b>{{m.label}}</b><i>{{'ACTIVE' if m.active else 'INACTIVE'}}</i></div>
<p>{{m.provider_key}} / {{m.model_name}} · {{m.currency}} · input {{m.input_price_per_million}} / output {{m.output_price_per_million}} per million · max {{m.max_output_tokens}}</p>
<p>Conservative representative maximum: {{m.currency}} {{estimates[m.id]}}</p>
<form method="post" action="/models/{{m.id}}/toggle"><button class="secondary">{{'Deactivate' if m.active else 'Activate'}}</button></form></div>{% endfor %}
</section><section class="panel"><h2>Create ModelConfig</h2><form method="post">
<input name="label" required placeholder="Label"><select name="provider_key"><option>mock</option><option>openai</option><option>anthropic</option></select>
<input name="model_name" required placeholder="Exact model ID"><input name="input_price_per_million" required value="0">
<input name="output_price_per_million" required value="0"><input name="currency" required value="TWD">
<input name="max_output_tokens" type="number" min="1" required value="1200"><button>Create Model Configuration</button>
</form></section></div>{% endblock %}

``

---

## FILE: eason_one\templates\project.html

``html
{% extends "base.html" %}{% block content %}<header><div><label>PROJECT / {{p.status}}</label><h1>{{p.name}}</h1><p>{{p.objective}}</p></div><div class="budget">NT$ {{'%.4f'|format(costs)}}<small>PROJECT COST</small></div></header><section class="metrics">{% for s in ['TODO','ASSIGNED','WORKING','BLOCKED','REVIEW','DONE'] %}<div><b>{{counts.get(s,0)}}</b><span>{{s}}</span></div>{% endfor %}</section><div class="grid"><section class="panel"><h2>Task Board</h2>{% for task in p.tasks %}<div class="task"><div><b>{{task.title}}</b><small>{{task.assigned_employee.name if task.assigned_employee else 'Unassigned'}} · reviewer {{task.reviewer.name if task.reviewer else 'CEO'}}</small></div><i>{{task.status}}</i><p>{{task.objective}}</p>{% if task.result_summary %}<details><summary>Result</summary><pre>{{task.result_summary}}</pre></details>{% endif %}{% if task.status in ['ASSIGNED','WORKING'] %}<form method="post" action="/tasks/{{task.id}}/run"><button>Run Structured Task</button></form>{% endif %}{% if task.status=='REVIEW' %}<form method="post" action="/tasks/{{task.id}}/run-review"><input name="instruction" value="Review this Task"><button>Run Reviewer · {{task.reviewer.name if task.reviewer else 'CEO'}}</button></form>{% endif %}</div>{% endfor %}<h2>Current Effective Knowledge</h2>{% for k in knowledge %}<div class="row"><span><b>{{k.kind}} #{{k.id}} · {{k.title}}</b><small>{{k.content}}{% if decision_bases.get(k.id) %}<br>Based on: {% for basis in decision_bases[k.id] %}{{basis.kind}} #{{basis.id}} {{basis.title}}{{', ' if not loop.last}}{% endfor %}{% endif %}</small></span></div>{% else %}<p class="muted">No effective knowledge.</p>{% endfor %}<details><summary>Historical Knowledge</summary>{% for k in knowledge_history %}<div class="row"><span><b>{{k.kind}} #{{k.id}} · {{k.title}}</b><small>{{k.content}}</small></span></div>{% endfor %}</details></section><section><div class="panel"><h2>CEO Briefing</h2><form method="post" action="/projects/{{p.id}}/briefing"><button>Generate CEO Briefing</button></form>{% for report in reports %}<a class="row" href="/runs/{{report.agent_run_id}}"><span><b>{{report.content}}</b></span><i>REPORT</i></a>{% endfor %}</div><div class="panel"><h2>Founder Knowledge Capture</h2><form method="post" action="/projects/{{p.id}}/knowledge"><select name="kind"><option>FACT</option><option>HYPOTHESIS</option><option>EVIDENCE</option><option>DECISION</option><option>CORRECTION</option><option>KILLED</option></select><input name="title" required placeholder="Title"><textarea name="content" required placeholder="Knowledge content"></textarea><input name="source_ref" placeholder="Evidence source"><input name="rationale" placeholder="Decision rationale"><select name="target_knowledge_id"><option value="">No correction/kill target</option>{% for k in knowledge_targets %}<option value="{{k.id}}">#{{k.id}} {{k.kind}} · {{k.title}}</option>{% endfor %}</select><label>Decision basis (Ctrl/Cmd for multiple)</label><select name="basis_knowledge_ids" multiple>{% for k in basis_options %}<option value="{{k.id}}">#{{k.id}} {{k.kind}} · {{k.title}}</option>{% endfor %}</select><button>Record Founder Knowledge</button></form></div><div class="panel"><h2>Create Task</h2><form method="post" action="/projects/{{p.id}}/tasks"><input name="title" required><textarea name="objective" required></textarea><select name="assignee_id">{% for e in employees %}<option value="{{e.id}}">{{e.name}}</option>{% endfor %}</select><select name="reviewer_id"><option value="">CEO fallback</option>{% for e in employees %}<option value="{{e.id}}">{{e.name}}</option>{% endfor %}</select><input name="required_output"><input name="acceptance_criteria"><button>Create & Assign</button></form></div></section></div>{% endblock %}

``

---

## FILE: eason_one\templates\projects.html

``html
{% extends "base.html" %}{% block content %}<header><div><label>PORTFOLIO</label><h1>Projects</h1></div></header><div class="grid"><section class="panel"><h2>Project Registry</h2>{% for p in projects %}<div class="row project-{{p.environment|lower}}"><a href="/projects/{{p.id}}"><b>{{p.name}}</b><small>{{p.origin}} · {{p.status}} · {{p.objective}}</small></a><form method="post" action="/projects/{{p.id}}/classification"><select name="environment" onchange="this.form.submit()"><option {{'selected' if p.environment=='LIVE'}}>LIVE</option><option {{'selected' if p.environment=='SMOKE'}}>SMOKE</option><option {{'selected' if p.environment=='ARCHIVED'}}>ARCHIVED</option></select></form></div>{% else %}<p class="muted">No Projects.</p>{% endfor %}</section><section class="panel"><h2>Intake Existing LIVE Project</h2><p>Begin managing already-committed external work without creating Tasks or knowledge.</p><form method="post" action="/projects/existing"><input name="name" required placeholder="Project title"><textarea name="current_state_summary" required placeholder="Concise current-state summary"></textarea><textarea name="objective" required placeholder="Objective"></textarea><select name="status"><option>ACTIVE</option><option>PLANNING</option><option>BLOCKED</option><option>REVIEW</option></select><select name="priority"><option>HIGH</option><option>MEDIUM</option><option>LOW</option><option>CRITICAL</option></select><select name="owner_id">{% for e in employees %}<option value="{{e.id}}">{{e.name}} owns</option>{% endfor %}</select><textarea name="known_constraints" placeholder="Known constraints"></textarea><input name="next_milestone" placeholder="Optional next milestone"><button>Register EXISTING LIVE Project</button></form></section></div>{% endblock %}

``

---

## FILE: eason_one\templates\run.html

``html
{% extends "base.html" %}{% block content %}<header><div><label>AGENT RUN #{{run.id}} / {{run.status}}</label><h1>{{run.purpose}}</h1></div><div class="budget">{{run.currency_snapshot}} {{run.real_cost or 0}}<small>{{run.input_tokens or 0}} IN / {{run.output_tokens or 0}} OUT</small></div></header><div class="grid"><section class="panel"><h2>Output</h2><pre>{{run.raw_output or run.error_text or 'No output'}}</pre>{% if run.parsed_output_json %}<h2>Validated Structured Output</h2><pre>{{run.parsed_output_json|tojson(indent=2)}}</pre>{% endif %}<h2>User Request</h2><pre>{{run.user_request}}</pre></section><section class="panel"><h2>Historical Execution Identity</h2><p>Employee: {{run.employee.name}}</p><p>Provider: {{run.provider_key_snapshot}}</p><p>Model: {{run.model_name_snapshot}}</p><p>Pricing: {{run.currency_snapshot}} {{run.input_price_snapshot}} input / {{run.output_price_snapshot}} output per million</p><p>Provider request ID: {{run.provider_request_id or '-'}}</p><p>Provider response ID: {{run.provider_response_id or '-'}}</p><p>Cache usage: {{run.cache_creation_input_tokens or 0}} creation / {{run.cache_read_input_tokens or 0}} read</p><h3>Current configuration (not historical)</h3><p>{{run.model_config.label}} / {{run.model_config.model_name}}</p><h2>Prompt Snapshot</h2><pre>{{run.system_prompt_snapshot}}</pre><h2>Context Snapshot</h2><pre>{{run.context_snapshot}}</pre></section></div>{% endblock %}

``

---

## FILE: eason_one\templates\unseeded.html

``html
{% extends "base.html" %}{% block content %}<h1>Company not initialized</h1><div class="panel"><p>Run <code>flask --app run.py seed</code>, then reload.</p></div>{% endblock %}

``

---

## FILE: eason-one-full-review.md

``markdown
# Eason One — Full Code Review Bundle


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

Structured workflows pass strict JSON Schemas through the provider boundary to OpenAI Responses while retaining deterministic application validation. OpenAI response IDs and HTTP request IDs are stored separately; incomplete/refused billable responses retain token usage and exactly one ledger entry before being marked failed. Structured Task results can create pending knowledge Proposals, never authoritative knowledge. Founder-approved Decisions may link only currently effective same-Project or company-level approved basis items through `BASIS_FOR` references in the same transaction.

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
openai<3,>=2.48.0
anthropic<1,>=0.117.0

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
eason_one/schemas.py
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
eason_one/services/meetings.py
eason_one/services/model_configs.py
eason_one/services/projects.py
eason_one/services/reviews.py
eason_one/services/task_execution.py
eason_one/services/tasks.py
eason_one/static/app.css
eason_one/static/meetings.css
eason_one/templates/base.html
eason_one/templates/ceo.html
eason_one/templates/costs.html
eason_one/templates/employee.html
eason_one/templates/employees.html
eason_one/templates/inbox.html
eason_one/templates/interview.html
eason_one/templates/meeting.html
eason_one/templates/meetings.html
eason_one/templates/models.html
eason_one/templates/project.html
eason_one/templates/projects.html
eason_one/templates/run.html
eason_one/templates/unseeded.html
tests/test_core.py
tests/test_hardening.py
tests/test_patch0041.py
tests/test_patch0051.py
tests/test_slice003.py
tests/test_slice004.py
tests/test_slice005.py
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
 "en":{"nav.ceo":"CEO","nav.projects":"Projects","nav.meetings":"Meetings","nav.employees":"Employees","nav.inbox":"Founder Inbox","nav.costs":"Cost Control","nav.models":"Models","lang.en":"EN","lang.zh":"繁中","button.execute":"Execute CEO Request","button.interview":"Interview Employee"},
 "zh-TW":{"nav.ceo":"CEO","nav.projects":"專案","nav.meetings":"會議","nav.employees":"員工","nav.inbox":"創辦人收件匣","nav.costs":"成本控制","nav.models":"模型","lang.en":"EN","lang.zh":"繁中","button.execute":"執行 CEO 指令","button.interview":"訪談員工"}
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
        r=OpenAI(api_key=os.environ["OPENAI_API_KEY"]).responses.create(**kwargs)
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
        message=Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"]).messages.create(**kwargs)
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

## FILE: eason_one\routes.py

``python
from decimal import Decimal
from flask import Blueprint, render_template, request, redirect, url_for, flash, abort, session
from sqlalchemy import func
from .extensions import db
from .models import *
from .services.company import get_company, spent, remaining
from .services.projects import create_project,classify
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
from .services import meetings as meeting_service

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
    live_ids=Project.query.with_entities(Project.id).filter_by(environment="LIVE")
    metrics={"active_projects":Project.query.filter(Project.environment=="LIVE",Project.status.in_(["PLANNING","ACTIVE","BLOCKED","REVIEW"])).count(),
      "active_tasks":Task.query.filter(Task.project_id.in_(live_ids),Task.status.in_(["ASSIGNED","WORKING","REVIEW"])).count(),
      "blocked":Task.query.filter(Task.project_id.in_(live_ids),Task.status=="BLOCKED").count(),
      "pending":Proposal.query.filter_by(status="PENDING").count()}
    projects=Project.query.filter_by(environment="LIVE").order_by(Project.updated_at.desc()).all()
    recent=Task.query.filter(Task.project_id.in_(live_ids),Task.status=="DONE").order_by(Task.completed_at.desc()).limit(6).all()
    latest_ceo=AgentRun.query.filter_by(purpose="CEO_FOUNDER_REQUEST",status="SUCCEEDED").order_by(AgentRun.started_at.desc()).first()
    return render_template("ceo.html",company=company,spent=spent(),remaining=remaining(),metrics=metrics,projects=projects,recent=recent,latest_ceo=latest_ceo)

@bp.route("/projects")
def projects(): return render_template("projects.html",projects=Project.query.order_by(Project.updated_at.desc()).all(),employees=Employee.query.filter_by(active=True).all())
@bp.route("/projects/existing",methods=["POST"])
def existing_project():
    try:
        create_project(request.form["name"],request.form["objective"],Employee.query.get_or_404(request.form["owner_id"]),
          priority=request.form["priority"],status=request.form["status"],environment="LIVE",origin="EXISTING",
          current_state_summary=request.form["current_state_summary"],known_constraints=request.form.get("known_constraints"),
          next_milestone=request.form.get("next_milestone"))
    except Exception as ex: flash(str(ex),"error")
    return redirect(url_for("main.projects"))
@bp.route("/projects/<int:id>/classification",methods=["POST"])
def project_classification(id):
    try: classify(Project.query.get_or_404(id),request.form["environment"])
    except Exception as ex: flash(str(ex),"error")
    return redirect(request.referrer or url_for("main.projects"))
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
@bp.route("/meetings",methods=["GET","POST"])
def meetings():
    if request.method=="POST":
        try:
            chair=Employee.query.get_or_404(request.form["chair_employee_id"])
            participants=Employee.query.filter(Employee.id.in_([int(x) for x in request.form.getlist("participant_ids")])).all()
            project=Project.query.get(request.form.get("project_id")) if request.form.get("project_id") else None
            meeting_service.create(request.form["title"],request.form["purpose"],request.form["agenda"],chair,participants,project,
              request.form["max_rounds"],request.form["token_limit"],request.form["real_cost_limit_twd"])
        except Exception as ex: flash(str(ex),"error")
        return redirect(url_for("main.meetings"))
    rows=[]
    for meeting in Meeting.query.order_by(Meeting.created_at.desc()).all():
        tokens,cost=meeting_service.usage(meeting); rows.append((meeting,tokens,cost))
    return render_template("meetings.html",rows=rows,employees=Employee.query.filter_by(active=True).all(),projects=Project.query.filter_by(environment="LIVE").all())
@bp.route("/meetings/<int:id>")
def meeting_room(id):
    meeting=Meeting.query.get_or_404(id); tokens,cost=meeting_service.usage(meeting)
    messages=MeetingMessage.query.filter_by(meeting_id=id).order_by(MeetingMessage.id).all()
    return render_template("meeting.html",meeting=meeting,tokens=tokens,cost=cost,messages=messages,recent=messages[-6:],feedback_signals=sorted(meeting_service.SIGNALS))
def _meeting_action(id,fn,*args):
    meeting=Meeting.query.get_or_404(id)
    try: fn(meeting,*args)
    except Exception as ex: flash(str(ex),"error")
    return redirect(url_for("main.meeting_room",id=id))
@bp.route("/meetings/<int:id>/start",methods=["POST"])
def meeting_start(id): return _meeting_action(id,meeting_service.start)
@bp.route("/meetings/<int:id>/advance",methods=["POST"])
def meeting_advance(id): return _meeting_action(id,meeting_service.advance)
@bp.route("/meetings/<int:id>/join",methods=["POST"])
def meeting_join(id): return _meeting_action(id,meeting_service.join_founder)
@bp.route("/meetings/<int:id>/intervene",methods=["POST"])
def meeting_intervene(id): return _meeting_action(id,meeting_service.intervene,request.form["content"])
@bp.route("/meetings/<int:id>/command",methods=["POST"])
def meeting_command(id): return _meeting_action(id,meeting_service.command,request.form["kind"],request.form["content"])
@bp.route("/meetings/<int:id>/end",methods=["POST"])
def meeting_end(id): return _meeting_action(id,meeting_service.end_and_synthesize)
@bp.route("/meetings/<int:id>/stop",methods=["POST"])
def meeting_stop(id): return _meeting_action(id,meeting_service.stop,request.form.get("reason"))
@bp.route("/meeting-messages/<int:id>/feedback",methods=["POST"])
def meeting_feedback(id):
    message=MeetingMessage.query.get_or_404(id)
    try: meeting_service.feedback(message,request.form["signal"],request.form.get("note"))
    except Exception as ex: flash(str(ex),"error")
    return redirect(url_for("main.meeting_room",id=message.meeting_id))
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
        if not target.founder_approved: raise ValueError(f"{kind} target must be Founder-approved")
        project_id=kwargs.get("project_id")
        if project_id is None and target.project_id is not None: raise ValueError("Company-level knowledge cannot target Project knowledge")
        if project_id is not None and target.project_id not in {None,project_id}: raise ValueError("Knowledge target belongs to an unrelated Project")
        if kind=="KILLED" and target.kind!="HYPOTHESIS": raise ValueError("KILLED target must be a HYPOTHESIS")
    return True
def build_knowledge(kind,title,content,**kwargs):
    kwargs.pop("basis_knowledge_ids",None)
    validate(kind,**kwargs); return KnowledgeItem(kind=kind,title=title,content=content,**kwargs)
def validate_basis_ids(ids,project_id):
    if not isinstance(ids,list) or len(set(ids))!=len(ids): raise ValueError("Invalid basis IDs")
    effective={item.id:item for item in current(project_id)}
    allowed={"FACT","HYPOTHESIS","EVIDENCE","CORRECTION"}
    rows=[]
    for ident in ids:
        item=db.session.get(KnowledgeItem,ident)
        if not item or not item.founder_approved: raise ValueError("Decision basis must be Founder-approved")
        if item.project_id not in {None,project_id}: raise ValueError("Decision basis belongs to an unrelated Project")
        if ident not in effective or item.kind not in allowed: raise ValueError("Decision basis must be currently effective FACT, HYPOTHESIS, EVIDENCE, or CORRECTION")
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
    for p in Project.query.filter(Project.status.in_(["PLANNING","ACTIVE","BLOCKED","REVIEW"]),Project.environment=="LIVE").order_by(Project.id):
        counts=dict(db.session.query(Task.status,func.count(Task.id)).filter_by(project_id=p.id).group_by(Task.status).all())
        recent=[x.title for x in Task.query.filter_by(project_id=p.id,status="DONE").order_by(Task.completed_at.desc()).limit(3)]
        cost=Decimal(db.session.query(func.coalesce(func.sum(CostEvent.real_cost_delta),0)).filter_by(project_id=p.id).scalar())
        summaries.append(f"Project #{p.id}: {p.name}; origin: {p.origin}; status: {p.status}; priority: {p.priority}; owner: {p.owner.name}; "
          f"tasks: {sum(counts.values())}; blocked: {counts.get('BLOCKED',0)}; review: {counts.get('REVIEW',0)}; "
          f"recent completed: {', '.join(recent) or '-'}; cost: {p.owner.current_model.currency} {cost}; "
          f"current state: {p.current_state_summary or '-'}; constraints: {p.known_constraints or '-'}; next milestone: {p.next_milestone or '-'}")
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
    if run.status!="SUCCEEDED": return run,None
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
    if run.status!="SUCCEEDED": return run
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
from dataclasses import dataclass
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

def estimate_execution(model,system_prompt,context,user_request,max_output_tokens=None):
    output_tokens=model.max_output_tokens if max_output_tokens is None else int(max_output_tokens)
    framed=execution_frame(system_prompt,context,user_request)
    byte_bound=len(framed.encode("utf-8"))
    input_tokens=max(1,(byte_bound*5+3)//4+256)
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
    estimate=estimate_execution(model,system_prompt,context,user_request,model.max_output_tokens)
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
        run.real_cost=calculate(model,result.input_tokens,result.output_tokens)
        provider_name=model.provider_key
        if result.cache_creation_input_tokens or result.cache_read_input_tokens:
            run.status="FAILED"; run.error_text=(f"{provider_name} returned unexpected prompt-cache usage; "
              "exact cache-cost reconciliation is not enabled")
        elif result.refusal:
            run.status="FAILED"; run.error_text=f"{provider_name} refusal: {result.refusal}"
        elif result.status!="completed":
            detail=f": {result.incomplete_reason}" if result.incomplete_reason else ""
            run.status="FAILED"; run.error_text=f"{provider_name} response {result.status}{detail}"
        else: run.status="SUCCEEDED"
        run.finished_at=now(); record(run); db.session.commit()
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
    tokens=db.session.query(func.coalesce(func.sum(AgentRun.input_tokens+AgentRun.output_tokens),0)).filter_by(meeting_id=meeting.id).scalar()
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

def _check_step(meeting,employee,system_prompt,context,user_request,allow_final=False):
    if meeting.status!=ACTIVE: raise ValueError("Meeting is not ACTIVE")
    if not allow_final and meeting.current_round>=meeting.max_rounds: raise ValueError("Meeting maximum rounds reached")
    tokens,cost=usage(meeting)
    estimate=estimate_execution(employee.current_model,system_prompt,context,user_request)
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
    _check_step(meeting,meeting.chair,prompt,context,user_request,allow_final=True)
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

## FILE: eason_one\services\projects.py

``python
from ..extensions import db
from ..models import Project
VALID = {"PLANNING","ACTIVE","BLOCKED","REVIEW","COMPLETED","FAILED","CANCELLED","PARKED"}
def create_project(name, objective, owner, priority="MEDIUM", status="PLANNING",environment="LIVE",origin="NEW",**kwargs):
    if status not in VALID: raise ValueError("Invalid project status")
    if environment not in {"LIVE","SMOKE","ARCHIVED"} or origin not in {"NEW","EXISTING"}: raise ValueError("Invalid Project classification")
    project = Project(name=name,objective=objective,owner_employee_id=owner.id,priority=priority,status=status,
      environment=environment,origin=origin,**kwargs)
    db.session.add(project); db.session.commit()
    return project
def classify(project,environment):
    if environment not in {"LIVE","SMOKE","ARCHIVED"}: raise ValueError("Invalid Project classification")
    project.environment=environment; db.session.commit(); return project

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
    if task.status not in {"ASSIGNED","WORKING"}: raise ValueError("Task is not executable")
    employee=task.assigned_employee
    prompt=employee.system_instructions+"\nTASK_EXECUTION\nReturn the structured Task result. Do not claim web research or fabricate citations."
    run=execute(employee,"TASK_EXECUTION",task.objective,task.project,task,system_prompt_override=prompt,response_schema=TASK_EXECUTION_SCHEMA)
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

## FILE: eason_one\static\app.css

``css
:root{--bg:#061016;--panel:#0a1820;--line:#183746;--cyan:#48e7ff;--text:#d6edf2;--muted:#75939d;--green:#61f5a6}*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 80% 0,#0c2a36,transparent 35%),var(--bg);color:var(--text);font:14px Inter,Segoe UI,sans-serif;display:flex;min-height:100vh}aside{width:220px;border-right:1px solid var(--line);padding:24px 16px;position:fixed;height:100vh;background:#071219cc}.brand{font-weight:800;letter-spacing:2px;color:var(--cyan);font-size:17px}.brand small{display:block;color:var(--muted);font-size:9px;margin:7px 0 35px}nav a{display:block;color:#9eb7bf;text-decoration:none;padding:12px;margin:4px 0;border-left:2px solid transparent}nav a:hover{color:var(--cyan);background:#0d2029;border-color:var(--cyan)}main{margin-left:220px;width:calc(100% - 220px);padding:32px;max-width:1600px}header{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:24px}h1{font-size:30px;margin:5px 0}h2{font-size:15px;text-transform:uppercase;letter-spacing:1px}h3,label{font-size:10px;letter-spacing:1.5px;color:var(--cyan)}p{color:#a4bcc3}.budget{text-align:right;font-size:24px;color:var(--green)}.budget small{display:block;color:var(--muted);font-size:9px}.metrics,.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:20px}.metrics>div,.panel,.employee{background:linear-gradient(135deg,#0d2029dd,#08151cdd);border:1px solid var(--line);padding:18px}.metrics b{display:block;font-size:18px;color:#eefcff}.metrics span,.stats small{display:block;color:var(--muted);font-size:9px;margin-top:6px}.grid{display:grid;grid-template-columns:1.4fr 1fr;gap:18px}.panel{margin-bottom:18px}.command textarea{min-height:76px}.row{display:flex;justify-content:space-between;gap:15px;padding:13px 0;border-bottom:1px solid #142b35;color:var(--text);text-decoration:none}.row span{min-width:0}.row small{display:block;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:550px;margin-top:4px}.row i,.task i{color:var(--cyan);font-size:10px;font-style:normal}.task{border:1px solid var(--line);padding:15px;margin:12px 0}.task>div{display:flex;justify-content:space-between}.task small{display:block;color:var(--muted)}input,textarea,select{width:100%;background:#061117;color:var(--text);border:1px solid #245064;padding:11px;margin:5px 0;font:inherit}textarea{min-height:70px}button,.button{background:var(--cyan);color:#03202a;border:0;padding:10px 15px;font-weight:700;cursor:pointer;text-decoration:none;display:inline-block;margin:5px 3px 5px 0}.secondary{background:#18323e;color:var(--text)}pre{white-space:pre-wrap;word-break:break-word;color:#a9cbd3;font:12px Consolas,monospace}.cards{grid-template-columns:repeat(auto-fit,minmax(280px,1fr))}.employee{color:var(--text);text-decoration:none;transition:.2s}.employee:hover{border-color:var(--cyan);transform:translateY(-2px)}.employee h2{text-transform:none;font-size:21px;margin:8px 0}.orb{width:8px;height:8px;border-radius:50%;background:var(--green);box-shadow:0 0 12px var(--green);float:right}.stats{display:grid;grid-template-columns:1fr 1fr;gap:10px;border-top:1px solid var(--line);padding-top:13px}.employee footer{color:var(--muted);font-size:10px;margin-top:15px}.chat>div{max-width:80%;padding:12px;margin:10px 0;border-left:2px solid var(--cyan);background:#07141a}.chat .employee{margin-left:auto;border:0;border-right:2px solid var(--green)}.muted{color:var(--muted)}.flash{padding:12px;border:1px solid var(--line);margin-bottom:15px}.flash.ok{border-color:#276f50}.flash.error{border-color:#8d3847}@media(max-width:800px){aside{position:static;width:100%;height:auto}body{display:block}main{margin:0;width:100%;padding:18px}.grid{grid-template-columns:1fr}nav a{display:inline-block}.brand small{margin-bottom:10px}}

``

---

## FILE: eason_one\static\meetings.css

``css
.project-smoke,.project-archived{opacity:.55}.project-archived{filter:saturate(.35)}.row>a{color:var(--text);text-decoration:none;min-width:0}.meeting-card .stats{grid-template-columns:repeat(3,1fr)}.secondary-card{opacity:.65}.avatar-strip{display:flex;gap:6px;margin:14px 0}.mini-avatar,.avatar-large{display:grid;place-items:center;border-radius:50%;background:#12333f;border:1px solid var(--cyan);color:var(--cyan);font-weight:800}.mini-avatar{width:30px;height:30px;font-size:10px}.meeting-layout{display:grid;grid-template-columns:minmax(520px,1.6fr) minmax(300px,.8fr);gap:18px}.meeting-stage{position:relative;min-height:590px;border:1px solid var(--line);background:radial-gradient(ellipse at center,#12303b 0,#08151c 50%,#061016 76%);overflow:hidden}.table-core{position:absolute;inset:34% 27%;border:1px solid #2a687a;border-radius:50%;display:grid;place-items:center;text-align:center;color:var(--cyan);padding:25px;box-shadow:inset 0 0 40px #092630}.table-core small{color:var(--muted)}.seat{position:absolute;width:190px;text-align:center}.seat small{display:block;color:var(--muted)}.avatar-large{width:58px;height:58px;margin:0 auto 7px}.seat-1{top:5%;left:calc(50% - 95px)}.seat-2{top:40%;left:3%}.seat-3{top:40%;right:3%}.seat-4{bottom:4%;left:18%}.seat-5{bottom:4%;right:18%}.seat-6{top:8%;right:4%}.speech{margin-top:10px;padding:10px;background:#0b222c;border:1px solid #225567;border-radius:8px;text-align:left;font-size:11px;max-height:100px;overflow:auto}.meeting-side{position:static;width:auto;height:auto;border:0;padding:0;background:none}.meeting-side details{border:1px solid var(--line);background:#091820;margin-bottom:10px;padding:14px}.meeting-side summary{color:var(--cyan);font-size:11px;letter-spacing:1px;cursor:pointer}.transcript{max-height:400px;overflow:auto}.transcript>div{border-bottom:1px solid var(--line);padding:9px 0}.transcript small{color:var(--muted)}.transcript form{display:grid;grid-template-columns:1fr 1.5fr auto;gap:4px}.control-panel{margin-top:18px}.control-panel form{display:inline-block;vertical-align:top;min-width:145px}.control-panel input{width:260px}@media(max-width:1050px){.meeting-layout{grid-template-columns:1fr}.meeting-stage{min-height:540px}}@media(max-width:800px){.meeting-stage{min-height:650px}.seat{position:relative!important;inset:auto!important;display:inline-block;width:48%;vertical-align:top;margin:15px 0}.table-core{position:relative;inset:auto;margin:20px auto;width:70%;height:150px}.control-panel form,.control-panel input{display:block;width:100%}}

``

---

## FILE: eason_one\templates\base.html

``html
<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>{% block title %}Eason One{% endblock %}</title>
<link rel="stylesheet" href="{{url_for('static',filename='app.css')}}">
<link rel="stylesheet" href="{{url_for('static',filename='meetings.css')}}"></head>
<body><aside><div class="brand">EASON ONE<small>FOUNDER COMMAND OS</small></div>
<nav><a href="/ceo">{{t('nav.ceo')}}</a><a href="/projects">{{t('nav.projects')}}</a>
<a href="/meetings">{{t('nav.meetings')}}</a><a href="/employees">{{t('nav.employees')}}</a>
<a href="/models">{{t('nav.models')}}</a><a href="/inbox">{{t('nav.inbox')}}</a>
<a href="/costs">{{t('nav.costs')}}</a></nav>
<div class="languages"><form method="post" action="/language/en"><input type="hidden" name="next" value="{{request.path}}"><button>{{t('lang.en')}}</button></form><span>|</span><form method="post" action="/language/zh-TW"><input type="hidden" name="next" value="{{request.path}}"><button>{{t('lang.zh')}}</button></form></div>
</aside><main>{% with ms=get_flashed_messages(with_categories=true) %}{% for c,m in ms %}<div class="flash {{c}}">{{m}}</div>{% endfor %}{% endwith %}{% block content %}{% endblock %}</main></body></html>

``

---

## FILE: eason_one\templates\ceo.html

``html
{% extends "base.html" %}{% block content %}<header><div><label>CEO OPERATING INTERFACE</label><h1>Company Command</h1></div><div class="budget">NT$ {{'%.2f'|format(remaining)}} <small>REMAINING / {{'%.0f'|format(company.real_budget_limit)}}</small></div></header>
<section class="metrics"><div><b>{{metrics.active_projects}}</b><span>Active Projects</span></div><div><b>{{metrics.active_tasks}}</b><span>Active Tasks</span></div><div><b>{{metrics.blocked}}</b><span>Blocked</span></div><div><b>{{metrics.pending}}</b><span>Founder Attention</span></div><div><b>NT$ {{'%.2f'|format(spent)}}</b><span>Real Spend</span></div></section>
<section class="panel command"><label>FOUNDER → CEO</label><form method="post"><textarea name="request" required placeholder="Create a Beauty LINE Consultation project and investigate whether this idea is worth pursuing."></textarea><button>{{t('button.execute')}}</button></form></section>{% if latest_ceo and latest_ceo.parsed_output_json %}<section class="panel"><label>CEO / {{latest_ceo.parsed_output_json.get('mode')}}</label><h2>{{latest_ceo.parsed_output_json.get('executive_response')}}</h2><a class="button" href="/runs/{{latest_ceo.id}}">Audit / Run Details</a></section>{% endif %}
<div class="grid"><section class="panel"><h2>Project Radar</h2>{% for p in projects %}<a class="row" href="/projects/{{p.id}}"><span><b>{{p.name}}</b><small>{{p.status}} · {{p.tasks|length}} tasks</small></span><i>{{p.priority}}</i></a>{% else %}<p class="muted">No projects. Issue a Founder request above.</p>{% endfor %}</section>
<section class="panel"><h2>Recently Completed</h2>{% for t in recent %}<div class="row"><span><b>{{t.title}}</b><small>{{t.project.name}}</small></span><i>DONE</i></div>{% else %}<p class="muted">No completed tasks yet.</p>{% endfor %}</section></div>{% endblock %}

``

---

## FILE: eason_one\templates\costs.html

``html
{% extends "base.html" %}{% block content %}<header><div><label>REAL MONEY / AUDIT LEDGER</label><h1>Cost Control</h1></div><div class="budget">NT$ {{'%.2f'|format(remaining)}}<small>REMAINING</small></div></header><section class="metrics"><div><b>NT$ {{'%.2f'|format(company.real_budget_limit)}}</b><span>Hard Budget</span></div><div><b>NT$ {{'%.6f'|format(spent)}}</b><span>Spent</span></div><div><b>NT$ {{'%.2f'|format(remaining)}}</b><span>Remaining</span></div></section><section class="panel"><h2>Auditable Ledger</h2>{% for e in events %}<a class="row" href="/runs/{{e.agent_run_id}}"><span><b>{{e.category}} · {{e.description}}</b><small>Employee {{e.employee_id}} · Project {{e.project_id or '-'}} · Task {{e.task_id or '-'}}</small></span><i>NT$ {{e.real_cost_delta}}</i></a>{% else %}<p class="muted">No cost events yet.</p>{% endfor %}</section>{% endblock %}

``

---

## FILE: eason_one\templates\employee.html

``html
{% extends "base.html" %}{% block content %}<header><div><label>{{e.department.name if e.department else 'CEO OFFICE / ASSURANCE'}}</label><h1>{{e.name}}</h1><p>{{e.role_description}}</p></div><a class="button" href="/employees/{{e.id}}/interview">{{t('button.interview')}}</a></header><section class="metrics"><div><b>{{e.position.name}} / L{{e.position.level}}</b><span>Position</span></div><div><b>{{e.manager.name if e.manager else 'Founder'}}</b><span>Manager</span></div><div><b>{{e.salary_credits_per_week}}</b><span>Weekly Internal Credits</span></div><div><b>{{cc}}</b><span>Company Contribution</span></div></section><div class="grid"><section class="panel"><h2>Work History</h2>{% for task in tasks %}<a class="row" href="/projects/{{task.project_id}}"><span><b>{{task.title}}</b><small>{{task.project.name}} · {{task.result_summary or 'No result yet'}}</small></span><i>{{task.status}}</i></a>{% else %}<p class="muted">No tasks.</p>{% endfor %}<h2>Learning / Experience</h2>{% for item in learning %}<div class="row"><span><b>{{item.title}}</b><small>{{item.content}}</small></span><i>{{'VALIDATED' if item.validated else 'UNVALIDATED'}}</i></div>{% else %}<p class="muted">No learning records.</p>{% endfor %}</section><section><div class="panel"><h2>Model Assignment</h2><b>{{e.current_model.label if e.current_model else 'None'}}</b><p>{{e.current_model.provider_key if e.current_model else ''}} / {{e.current_model.model_name if e.current_model else ''}}</p><form method="post" action="/employees/{{e.id}}/model"><select name="model_config_id">{% for model in model_configs %}<option value="{{model.id}}" {{'selected' if e.current_model_config_id==model.id}}>{{model.label}} · {{model.model_name}}</option>{% endfor %}</select><input name="reason" placeholder="Reason for reassignment"><button>Change Model</button></form><h3>Model History</h3>{% for h in history %}<p>{{h.model_config.label if h.model_config else 'None'}} · {{h.reason or ''}}</p>{% endfor %}</div><div class="panel"><h2>Project Contribution</h2>{% for project,value in project_contributions %}<div class="row"><b>{{project.name}}</b><i>{{value}}</i></div>{% else %}<p class="muted">No Project Contribution.</p>{% endfor %}</div><div class="panel"><h2>Add Learning Record</h2><form method="post" action="/employees/{{e.id}}/learning"><input name="title" required placeholder="Title"><textarea name="content" required placeholder="Evidence-based learning"></textarea><input name="source_ref" placeholder="Source reference"><select name="project_id"><option value="">Company-level</option>{% for project in projects %}<option value="{{project.id}}">{{project.name}}</option>{% endfor %}</select><label><input type="checkbox" name="validated" value="1"> Founder validated</label><button>Add Learning</button></form></div><div class="panel"><h2>Execution Audit</h2>{% for run in runs %}<a class="row" href="/runs/{{run.id}}"><span><b>{{run.purpose}}</b><small>{{run.started_at}}</small></span><i>{{run.status}}</i></a>{% else %}<p class="muted">No runs.</p>{% endfor %}</div></section></div>{% endblock %}

``

---

## FILE: eason_one\templates\employees.html

``html
{% extends "base.html" %}{% block content %}<header><div><label>PERSISTENT ORGANIZATION</label><h1>Employees</h1></div></header><section class="cards">{% for r in rows %}<a class="employee" href="/employees/{{r.e.id}}"><div class="orb"></div><label>{{r.e.department.name if r.e.department else 'CEO OFFICE / ASSURANCE'}}</label><h2>{{r.e.name}}</h2><p>{{r.e.position.name}} · L{{r.e.position.level}}</p><div class="stats"><span>{{r.active_tasks|length}}<small>ACTIVE TASKS</small></span><span>{{r.project_contribution}}<small>PROJECT CONTRIB.</small></span><span>{{r.company_contribution}}<small>COMPANY CONTRIB.</small></span><span>NT${{'%.3f'|format(r.cost)}}<small>REAL COST</small></span></div><footer>{{r.e.current_model.label if r.e.current_model else 'NO MODEL'}} · {{r.tokens}} TOKENS</footer></a>{% endfor %}</section>{% endblock %}

``

---

## FILE: eason_one\templates\inbox.html

``html
{% extends "base.html" %}{% block content %}<header><div><label>GOVERNANCE</label><h1>Founder Inbox</h1></div></header><section class="panel">{% for p in proposals %}<div class="task"><div><b>Proposal #{{p.id}} · {{p.payload_json.get('type','KNOWLEDGE')}}</b><small>{{p.status}} · Employee {{p.proposed_by_employee_id}}</small></div><pre>{{p.payload_json|tojson(indent=2)}}</pre>{% if p.status=='PENDING' %}{% if p.payload_json.get('type')=='PROJECT_PLAN' %}<form method="post" action="/inbox/{{p.id}}/materialize"><button>Confirm Governed Plan</button></form><form method="post" action="/inbox/{{p.id}}/reject"><button class="secondary">Reject</button></form>{% else %}<form method="post" action="/inbox/{{p.id}}/knowledge-review"><button name="decision" value="APPROVED">Approve Knowledge</button><button name="decision" value="REJECTED" class="secondary">Reject</button></form><details><summary>Correct before approval</summary><form method="post" action="/inbox/{{p.id}}/knowledge-review"><input type="hidden" name="decision" value="CORRECTED"><select name="kind"><option>FACT</option><option>HYPOTHESIS</option><option>EVIDENCE</option><option>DECISION</option><option>CORRECTION</option><option>KILLED</option></select><input name="title" required value="{{p.payload_json.get('title','')}}"><textarea name="content" required>{{p.payload_json.get('content','')}}</textarea><input name="source_ref" value="{{p.payload_json.get('source_ref','') or ''}}" placeholder="Evidence source"><input name="rationale" value="{{p.payload_json.get('rationale','') or ''}}" placeholder="Decision rationale"><input name="target_knowledge_id" value="{{p.payload_json.get('target_knowledge_id','') or ''}}" placeholder="Target knowledge ID"><input name="note" placeholder="Founder correction note"><button>Materialize Corrected Knowledge</button></form></details>{% endif %}{% endif %}</div>{% else %}<p class="muted">No proposals.</p>{% endfor %}</section>{% endblock %}

``

---

## FILE: eason_one\templates\interview.html

``html
{% extends "base.html" %}{% block content %}<header><div><label>FOUNDER INSPECTION CHANNEL</label><h1>Interview · {{e.name}}</h1><p>Conversation persists but cannot mutate policy or authoritative state.</p></div></header><section class="panel chat">{% for m in interview.messages %}<div class="{{m.speaker|lower}}"><label>{{m.speaker}}</label><p>{{m.content}}</p></div>{% else %}<p class="muted">Ask about current work, decisions, blockers, or lessons.</p>{% endfor %}<form method="post"><textarea name="content" required placeholder="Ask {{e.name}}…"></textarea><button>Send & Execute</button></form></section>{% endblock %}

``

---

## FILE: eason_one\templates\meeting.html

``html
{% extends "base.html" %}{% block content %}<header><div><label>MEETING / {{meeting.status}}</label><h1>{{meeting.title}}</h1><p>{{meeting.purpose}}</p></div><div class="budget">TWD {{cost}}<small>{{tokens}} TOKENS · ROUND {{meeting.current_round}}/{{meeting.max_rounds}}</small></div></header><div class="meeting-layout"><section class="meeting-stage"><div class="table-core">EASON ONE<br><small>{{meeting.agenda}}</small></div>{% for participant in meeting.participants %}<div class="seat seat-{{loop.index}}"><div class="avatar-large">{{participant.employee.name.split()|map('first')|join}}</div><b>{{participant.employee.name}}</b><small>{{participant.employee.position.name}} · L{{participant.employee.position.level}}</small>{% set spoken=recent|selectattr('employee_id','equalto',participant.employee_id)|list %}{% if spoken %}<div class="speech">{{spoken[-1].content}}</div>{% endif %}</div>{% endfor %}</section><aside class="meeting-side"><details open><summary>LIVE SUMMARY</summary>{% set s=meeting.current_summary_json or {} %}{% for key in ['current_topic','agreement','disagreement','evidence','pending_question','next_likely_action','founder_decision_required'] %}<label>{{key.replace('_',' ')|upper}}</label><p>{{s.get(key,'Waiting for the first meeting step.')}}</p>{% endfor %}</details><details><summary>TRANSCRIPT</summary><div class="transcript">{% for m in messages %}<div><small>R{{m.round_number}} · {{m.speaker_type}} · {{m.employee.name if m.employee else 'Founder/System'}} · {{m.created_at.strftime('%H:%M')}}</small><p>{{m.content}}</p>{% if m.speaker_type=='EMPLOYEE' %}<form method="post" action="/meeting-messages/{{m.id}}/feedback"><select name="signal">{% for signal in feedback_signals %}<option>{{signal}}</option>{% endfor %}</select><input name="note" placeholder="Optional Founder note"><button>Signal</button></form>{% endif %}</div>{% endfor %}</div></details>{% if meeting.minutes_json %}<details open><summary>MEETING MINUTES</summary><pre>{{meeting.minutes_json|tojson(indent=2)}}</pre></details>{% endif %}</aside></div><section class="panel control-panel"><label>FOUNDER CONTROL / {{'JOINED' if meeting.founder_joined_at else 'OBSERVE'}}</label>{% if meeting.status=='PLANNED' %}<form method="post" action="/meetings/{{meeting.id}}/start"><button>Start Meeting</button></form>{% elif meeting.status=='ACTIVE' %}<form method="post" action="/meetings/{{meeting.id}}/advance"><button>Advance Meeting</button></form>{% if not meeting.founder_joined_at %}<form method="post" action="/meetings/{{meeting.id}}/join"><button>Join Meeting</button></form>{% else %}<form method="post" action="/meetings/{{meeting.id}}/intervene"><input name="content" required placeholder="Founder intervention"><button>Send Intervention</button></form>{% endif %}<form method="post" action="/meetings/{{meeting.id}}/command"><input type="hidden" name="kind" value="FOCUS"><input name="content" required placeholder="Temporary focus for next round"><button>Focus Discussion</button></form><form method="post" action="/meetings/{{meeting.id}}/command"><input type="hidden" name="kind" value="REQUEST_EVIDENCE"><input name="content" value="Surface evidence or explicitly state that evidence is unavailable."><button>Request Evidence</button></form><form method="post" action="/meetings/{{meeting.id}}/end"><button>End & Synthesize</button></form><form method="post" action="/meetings/{{meeting.id}}/stop"><input name="reason" placeholder="Termination reason"><button class="secondary">Stop Meeting</button></form>{% endif %}</section>{% endblock %}

``

---

## FILE: eason_one\templates\meetings.html

``html
{% extends "base.html" %}{% block content %}<header><div><label>COLLABORATION</label><h1>Meeting Lobby</h1><p>Explicit, bounded organizational discussion.</p></div></header><div class="grid"><section><h2>Meetings</h2><div class="cards">{% for meeting,tokens,cost in rows %}<a class="employee meeting-card {{'secondary-card' if meeting.status in ['ENDED','TERMINATED_BY_FOUNDER']}}" href="/meetings/{{meeting.id}}"><label>{{meeting.status}}</label><h2>{{meeting.title}}</h2><p>{{meeting.project.name if meeting.project else 'Company-level'}} · Chair {{meeting.chair.name}}</p><div class="avatar-strip">{% for participant in meeting.participants %}<span class="mini-avatar" title="{{participant.employee.name}}">{{participant.employee.name.split()|map('first')|join}}</span>{% endfor %}</div><div class="stats"><span>{{meeting.current_round}} / {{meeting.max_rounds}}<small>ROUND</small></span><span>{{tokens}}<small>TOKENS</small></span><span>TWD {{cost}}<small>ACTUAL COST</small></span></div></a>{% else %}<p class="muted">No Meetings.</p>{% endfor %}</div></section><section class="panel"><h2>Plan Meeting</h2><form method="post"><input name="title" required placeholder="Meeting title"><textarea name="purpose" required placeholder="Purpose"></textarea><textarea name="agenda" required placeholder="Agenda"></textarea><select name="project_id"><option value="">Company-level</option>{% for p in projects %}<option value="{{p.id}}">{{p.name}}</option>{% endfor %}</select><select name="chair_employee_id">{% for e in employees %}<option value="{{e.id}}">{{e.name}} chairs</option>{% endfor %}</select><label>Participants</label><select name="participant_ids" multiple required size="6">{% for e in employees %}<option value="{{e.id}}">{{e.name}} · {{e.position.name}}</option>{% endfor %}</select><input name="max_rounds" type="number" min="1" value="3"><input name="token_limit" type="number" min="1" value="12000"><input name="real_cost_limit_twd" type="number" min="0" step="0.0001" value="100"><button>Plan Meeting</button></form></section></div>{% endblock %}

``

---

## FILE: eason_one\templates\models.html

``html
{% extends "base.html" %}{% block content %}
<header><div><label>FOUNDER CONFIGURATION</label><h1>Model Management</h1><p>API secrets remain in environment variables. Company remaining budget: TWD {{remaining}}.</p></div></header>
<div class="grid"><section class="panel"><h2>Configured Models</h2>
{% for m in models %}<div class="task"><div><b>{{m.label}}</b><i>{{'ACTIVE' if m.active else 'INACTIVE'}}</i></div>
<p>{{m.provider_key}} / {{m.model_name}} · {{m.currency}} · input {{m.input_price_per_million}} / output {{m.output_price_per_million}} per million · max {{m.max_output_tokens}}</p>
<p>Conservative representative maximum: {{m.currency}} {{estimates[m.id]}}</p>
<form method="post" action="/models/{{m.id}}/toggle"><button class="secondary">{{'Deactivate' if m.active else 'Activate'}}</button></form></div>{% endfor %}
</section><section class="panel"><h2>Create ModelConfig</h2><form method="post">
<input name="label" required placeholder="Label"><select name="provider_key"><option>mock</option><option>openai</option><option>anthropic</option></select>
<input name="model_name" required placeholder="Exact model ID"><input name="input_price_per_million" required value="0">
<input name="output_price_per_million" required value="0"><input name="currency" required value="TWD">
<input name="max_output_tokens" type="number" min="1" required value="1200"><button>Create Model Configuration</button>
</form></section></div>{% endblock %}

``

---

## FILE: eason_one\templates\project.html

``html
{% extends "base.html" %}{% block content %}<header><div><label>PROJECT / {{p.status}}</label><h1>{{p.name}}</h1><p>{{p.objective}}</p></div><div class="budget">NT$ {{'%.4f'|format(costs)}}<small>PROJECT COST</small></div></header><section class="metrics">{% for s in ['TODO','ASSIGNED','WORKING','BLOCKED','REVIEW','DONE'] %}<div><b>{{counts.get(s,0)}}</b><span>{{s}}</span></div>{% endfor %}</section><div class="grid"><section class="panel"><h2>Task Board</h2>{% for task in p.tasks %}<div class="task"><div><b>{{task.title}}</b><small>{{task.assigned_employee.name if task.assigned_employee else 'Unassigned'}} · reviewer {{task.reviewer.name if task.reviewer else 'CEO'}}</small></div><i>{{task.status}}</i><p>{{task.objective}}</p>{% if task.result_summary %}<details><summary>Result</summary><pre>{{task.result_summary}}</pre></details>{% endif %}{% if task.status in ['ASSIGNED','WORKING'] %}<form method="post" action="/tasks/{{task.id}}/run"><button>Run Structured Task</button></form>{% endif %}{% if task.status=='REVIEW' %}<form method="post" action="/tasks/{{task.id}}/run-review"><input name="instruction" value="Review this Task"><button>Run Reviewer · {{task.reviewer.name if task.reviewer else 'CEO'}}</button></form>{% endif %}</div>{% endfor %}<h2>Current Effective Knowledge</h2>{% for k in knowledge %}<div class="row"><span><b>{{k.kind}} #{{k.id}} · {{k.title}}</b><small>{{k.content}}{% if decision_bases.get(k.id) %}<br>Based on: {% for basis in decision_bases[k.id] %}{{basis.kind}} #{{basis.id}} {{basis.title}}{{', ' if not loop.last}}{% endfor %}{% endif %}</small></span></div>{% else %}<p class="muted">No effective knowledge.</p>{% endfor %}<details><summary>Historical Knowledge</summary>{% for k in knowledge_history %}<div class="row"><span><b>{{k.kind}} #{{k.id}} · {{k.title}}</b><small>{{k.content}}</small></span></div>{% endfor %}</details></section><section><div class="panel"><h2>CEO Briefing</h2><form method="post" action="/projects/{{p.id}}/briefing"><button>Generate CEO Briefing</button></form>{% for report in reports %}<a class="row" href="/runs/{{report.agent_run_id}}"><span><b>{{report.content}}</b></span><i>REPORT</i></a>{% endfor %}</div><div class="panel"><h2>Founder Knowledge Capture</h2><form method="post" action="/projects/{{p.id}}/knowledge"><select name="kind"><option>FACT</option><option>HYPOTHESIS</option><option>EVIDENCE</option><option>DECISION</option><option>CORRECTION</option><option>KILLED</option></select><input name="title" required placeholder="Title"><textarea name="content" required placeholder="Knowledge content"></textarea><input name="source_ref" placeholder="Evidence source"><input name="rationale" placeholder="Decision rationale"><select name="target_knowledge_id"><option value="">No correction/kill target</option>{% for k in knowledge_targets %}<option value="{{k.id}}">#{{k.id}} {{k.kind}} · {{k.title}}</option>{% endfor %}</select><label>Decision basis (Ctrl/Cmd for multiple)</label><select name="basis_knowledge_ids" multiple>{% for k in basis_options %}<option value="{{k.id}}">#{{k.id}} {{k.kind}} · {{k.title}}</option>{% endfor %}</select><button>Record Founder Knowledge</button></form></div><div class="panel"><h2>Create Task</h2><form method="post" action="/projects/{{p.id}}/tasks"><input name="title" required><textarea name="objective" required></textarea><select name="assignee_id">{% for e in employees %}<option value="{{e.id}}">{{e.name}}</option>{% endfor %}</select><select name="reviewer_id"><option value="">CEO fallback</option>{% for e in employees %}<option value="{{e.id}}">{{e.name}}</option>{% endfor %}</select><input name="required_output"><input name="acceptance_criteria"><button>Create & Assign</button></form></div></section></div>{% endblock %}

``

---

## FILE: eason_one\templates\projects.html

``html
{% extends "base.html" %}{% block content %}<header><div><label>PORTFOLIO</label><h1>Projects</h1></div></header><div class="grid"><section class="panel"><h2>Project Registry</h2>{% for p in projects %}<div class="row project-{{p.environment|lower}}"><a href="/projects/{{p.id}}"><b>{{p.name}}</b><small>{{p.origin}} · {{p.status}} · {{p.objective}}</small></a><form method="post" action="/projects/{{p.id}}/classification"><select name="environment" onchange="this.form.submit()"><option {{'selected' if p.environment=='LIVE'}}>LIVE</option><option {{'selected' if p.environment=='SMOKE'}}>SMOKE</option><option {{'selected' if p.environment=='ARCHIVED'}}>ARCHIVED</option></select></form></div>{% else %}<p class="muted">No Projects.</p>{% endfor %}</section><section class="panel"><h2>Intake Existing LIVE Project</h2><p>Begin managing already-committed external work without creating Tasks or knowledge.</p><form method="post" action="/projects/existing"><input name="name" required placeholder="Project title"><textarea name="current_state_summary" required placeholder="Concise current-state summary"></textarea><textarea name="objective" required placeholder="Objective"></textarea><select name="status"><option>ACTIVE</option><option>PLANNING</option><option>BLOCKED</option><option>REVIEW</option></select><select name="priority"><option>HIGH</option><option>MEDIUM</option><option>LOW</option><option>CRITICAL</option></select><select name="owner_id">{% for e in employees %}<option value="{{e.id}}">{{e.name}} owns</option>{% endfor %}</select><textarea name="known_constraints" placeholder="Known constraints"></textarea><input name="next_milestone" placeholder="Optional next milestone"><button>Register EXISTING LIVE Project</button></form></section></div>{% endblock %}

``

---

## FILE: eason_one\templates\run.html

``html
{% extends "base.html" %}{% block content %}<header><div><label>AGENT RUN #{{run.id}} / {{run.status}}</label><h1>{{run.purpose}}</h1></div><div class="budget">{{run.currency_snapshot}} {{run.real_cost or 0}}<small>{{run.input_tokens or 0}} IN / {{run.output_tokens or 0}} OUT</small></div></header><div class="grid"><section class="panel"><h2>Output</h2><pre>{{run.raw_output or run.error_text or 'No output'}}</pre>{% if run.parsed_output_json %}<h2>Validated Structured Output</h2><pre>{{run.parsed_output_json|tojson(indent=2)}}</pre>{% endif %}<h2>User Request</h2><pre>{{run.user_request}}</pre></section><section class="panel"><h2>Historical Execution Identity</h2><p>Employee: {{run.employee.name}}</p><p>Provider: {{run.provider_key_snapshot}}</p><p>Model: {{run.model_name_snapshot}}</p><p>Pricing: {{run.currency_snapshot}} {{run.input_price_snapshot}} input / {{run.output_price_snapshot}} output per million</p><p>Provider request ID: {{run.provider_request_id or '-'}}</p><p>Provider response ID: {{run.provider_response_id or '-'}}</p><p>Cache usage: {{run.cache_creation_input_tokens or 0}} creation / {{run.cache_read_input_tokens or 0}} read</p><h3>Current configuration (not historical)</h3><p>{{run.model_config.label}} / {{run.model_config.model_name}}</p><h2>Prompt Snapshot</h2><pre>{{run.system_prompt_snapshot}}</pre><h2>Context Snapshot</h2><pre>{{run.context_snapshot}}</pre></section></div>{% endblock %}

``

---

## FILE: eason_one\templates\unseeded.html

``html
{% extends "base.html" %}{% block content %}<h1>Company not initialized</h1><div class="panel"><p>Run <code>flask --app run.py seed</code>, then reload.</p></div>{% endblock %}

``

``

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

The seeded team uses the zero-cost `MockProvider`, so the complete flow works without credentials. To configure a real provider, create an active `ModelConfig` with `provider_key="openai"`, its exact `model_name`, positive input/output prices in TWD, and a maximum output-token limit; then assign it through the employee service and set `OPENAI_API_KEY`. Real-provider zero pricing, currency mismatches, and inactive configurations are rejected before invocation. OpenAI Responses retain both the response ID and HTTP request ID for audit. Environment variables hold secrets only and cannot override the Employee's organizational model assignment.

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

---

## FILE: tests\conftest.py

``python
import pytest
from eason_one import create_app
from eason_one.extensions import db
from eason_one.seed import seed

@pytest.fixture()
def app(tmp_path):
    app=create_app({"TESTING":True,"SQLALCHEMY_DATABASE_URI":f"sqlite:///{tmp_path/'test.db'}"})
    with app.app_context():
        db.drop_all(); db.create_all(); seed()
        yield app
        db.session.remove()

@pytest.fixture()
def ctx(app):
    with app.app_context(): yield

@pytest.fixture()
def client(app): return app.test_client()

``

---

## FILE: tests\test_core.py

``python
from decimal import Decimal
import pytest
from eason_one.extensions import db
from eason_one.models import *
from eason_one.services.employees import change_model
from eason_one.services.projects import create_project
from eason_one.services.tasks import create_task, assign, transition, store_result, review
from eason_one.services.company import spent, remaining, get_company
from eason_one.services.execution import execute
from eason_one.services.contributions import total
from eason_one.services.brain import add_knowledge, active_hypotheses
from eason_one.services.approvals import review_proposal
from eason_one.services.ceo import founder_request
from eason_one.services.interviews import start, ask

def people():
    return Employee.query.filter_by(slug="ceo").one(),Employee.query.filter_by(slug="researcher").one(),Employee.query.filter_by(slug="critic").one()

def project():
    ceo,_,_=people(); return create_project("Pilot","Determine viability",ceo)

def test_employee_identity_and_model_history(ctx):
    _,e,_=people(); eid=e.id
    m=ModelConfig(label="Other",provider_key="mock",model_name="v2",input_price_per_million=0,output_price_per_million=0)
    db.session.add(m); db.session.commit(); change_model(e,m,"Upgrade")
    assert e.id==eid and Employee.query.count()==6 and e.current_model.model_name=="v2"
    assert EmployeeModelHistory.query.filter_by(employee_id=e.id).count()==2
    assert EmployeeModelHistory.query.filter_by(employee_id=e.id,ended_at=None).one().model_config_id==m.id

def test_project_task_assignment_and_transitions(ctx):
    ceo,e,critic=people(); p=project()
    t=create_task(p,"Research","Find evidence",ceo)
    assert p.id and t.status=="TODO"
    assign(t,e); assert t.status=="ASSIGNED"
    with pytest.raises(ValueError): transition(t,"DONE")
    transition(t,"WORKING")
    with pytest.raises(ValueError): transition(t,"DONE")
    store_result(t,"Evidence report"); assert t.status=="REVIEW" and t.result_summary=="Evidence report"
    review(t,critic,"Accepted",True)
    assert t.status=="DONE" and t.completed_at and WorkMessage.query.filter_by(task_id=t.id,message_type="REVIEW").count()==1

def test_rejected_review_returns_to_work(ctx):
    ceo,e,critic=people(); t=create_task(project(),"Task","Work",ceo,e,critic)
    transition(t,"WORKING"); store_result(t,"Draft"); review(t,critic,"Needs sources",False)
    assert t.status=="WORKING"

def test_budget_cost_ledger_and_hard_cap(ctx):
    _,e,_=people(); p=project()
    model=e.current_model; model.input_price_per_million=100; model.output_price_per_million=200; db.session.commit()
    run=execute(e,"TEST","hello world",p)
    expected=(Decimal(run.input_tokens)*100+Decimal(run.output_tokens)*200)/Decimal(1_000_000)
    assert run.real_cost==expected and CostEvent.query.filter_by(agent_run_id=run.id).one()
    assert get_company().real_budget_limit==3000 and spent()==expected and remaining()==Decimal("3000")-expected
    db.session.add(CostEvent(company_id=get_company().id,category="ADJUSTMENT",description="Exhaust",internal_credits_delta=0,real_cost_delta=remaining(),currency="TWD")); db.session.commit()
    with pytest.raises(ValueError,match="exhausted"): execute(e,"TEST","blocked")

def test_contribution_scopes_are_independent(ctx):
    _,e,_=people(); p=project(); p2=create_project("Other","Other",people()[0])
    db.session.add_all([
      ContributionEvent(employee_id=e.id,project_id=p.id,scope="PROJECT",event_type="DELIVERY",value=5,reason="Founder"),
      ContributionEvent(employee_id=e.id,project_id=p2.id,scope="PROJECT",event_type="DELIVERY",value=7,reason="Founder"),
      ContributionEvent(employee_id=e.id,scope="COMPANY",event_type="REUSE",value=2,reason="Founder")]); db.session.commit()
    assert total(e.id,"PROJECT",p.id)==5 and total(e.id,"PROJECT",p2.id)==7 and total(e.id,"COMPANY")==2

def test_brain_validation_append_only_and_killed_filter(ctx):
    with pytest.raises(ValueError): add_knowledge("EVIDENCE","Claim","Observation",founder_approved=True)
    with pytest.raises(ValueError): add_knowledge("DECISION","Choice","Do it",founder_approved=True)
    h=add_knowledge("HYPOTHESIS","Idea","Might work",founder_approved=True)
    c=add_knowledge("CORRECTION","Correction","Revised",target_knowledge_id=h.id,founder_approved=True)
    assert KnowledgeItem.query.get(h.id) and KnowledgeItem.query.get(c.id)
    h2=add_knowledge("HYPOTHESIS","Other","Maybe",founder_approved=True)
    killed=add_knowledge("KILLED","Killed","Disproved",target_knowledge_id=h2.id,founder_approved=True)
    assert KnowledgeItem.query.get(h2.id) and killed and h2 not in active_hypotheses()

def test_rejected_proposal_never_materializes(ctx):
    ceo,_,_=people(); run=execute(ceo,"TEST","proposal")
    p=Proposal(agent_run_id=run.id,proposed_by_employee_id=ceo.id,payload_json={"kind":"FACT","title":"X","content":"Y"})
    db.session.add(p); db.session.commit(); review_proposal(p,"REJECTED","No evidence")
    assert p.materialized_knowledge_id is None and KnowledgeItem.query.count()==0

def test_ai_run_cannot_approve_proposal(ctx):
    ceo,_,_=people(); run,p=founder_request(ceo,"Create a test project")
    assert run.status=="SUCCEEDED" and run.parsed_output_json and p.status=="PENDING"
    assert p.materialized_knowledge_id is None

def test_ceo_vertical_flow_routes_are_governed(ctx,client):
    response=client.post("/ceo",data={"request":"Create a Beauty LINE Consultation project"},follow_redirects=True)
    assert response.status_code==200 and AgentRun.query.filter_by(purpose="CEO_FOUNDER_REQUEST").count()==1
    proposal=Proposal.query.one(); assert Project.query.count()==0 and proposal.status=="PENDING"
    client.post(f"/inbox/{proposal.id}/materialize")
    assert Project.query.count()==1 and Task.query.count()>=1 and proposal.status=="APPROVED"

def test_interview_persists_without_instruction_mutation(ctx):
    _,e,_=people(); original=e.system_instructions; i=start(e)
    ask(i,"What are your blockers?")
    assert FounderInterviewMessage.query.filter_by(interview_id=i.id).count()==2
    assert db.session.get(Employee,e.id).system_instructions==original

def test_key_routes_reject_invalid_review(ctx,client):
    ceo,e,critic=people(); t=create_task(project(),"Task","Work",ceo,e,critic)
    response=client.post(f"/tasks/{t.id}/review",data={"content":"skip","decision":"accept"},follow_redirects=True)
    assert response.status_code==200 and db.session.get(Task,t.id).status=="ASSIGNED"

``

---

## FILE: tests\test_hardening.py

``python
import json
from decimal import Decimal
import pytest
from eason_one.extensions import db
from eason_one.models import *
from eason_one.providers import ProviderResult
from eason_one.services.brain import add_knowledge,current,active_hypotheses
from eason_one.services.context import build
from eason_one.services.approvals import review_proposal
from eason_one.services.execution import execute
from eason_one.services.company import remaining
from eason_one.services.ceo import founder_request,materialize_project_plan
from eason_one.services.projects import create_project
from eason_one.services.interviews import start,ask
from eason_one.services.contributions import total,project_totals
from eason_one.services.learning import create as create_learning
from eason_one.i18n import translate

def people():
    return Employee.query.filter_by(slug="ceo").one(),Employee.query.filter_by(slug="researcher").one()
def projects():
    ceo,_=people(); return create_project("Alpha","A",ceo),create_project("Beta","B",ceo)
def proposal(payload):
    ceo,_=people(); run=execute(ceo,"TEST","proposal")
    p=Proposal(agent_run_id=run.id,proposed_by_employee_id=ceo.id,payload_json=payload)
    db.session.add(p); db.session.commit(); return p

def test_killed_targeting_fact_fails(ctx):
    fact=add_knowledge("FACT","F","fact",founder_approved=True)
    with pytest.raises(ValueError,match="HYPOTHESIS"): add_knowledge("KILLED","K","no",target_knowledge_id=fact.id,founder_approved=True)
def test_killed_targeting_missing_id_fails(ctx):
    with pytest.raises(ValueError,match="does not exist"): add_knowledge("KILLED","K","no",target_knowledge_id=999,founder_approved=True)
def test_correction_targeting_missing_id_fails(ctx):
    with pytest.raises(ValueError,match="does not exist"): add_knowledge("CORRECTION","C","new",target_knowledge_id=999,founder_approved=True)
def test_correction_replaces_target_and_remains_effective(ctx):
    fact=add_knowledge("FACT","Old","old",founder_approved=True); correction=add_knowledge("CORRECTION","New","new",target_knowledge_id=fact.id,founder_approved=True)
    assert fact not in current() and correction in current()
def test_killed_target_inactive_but_marker_effective(ctx):
    h=add_knowledge("HYPOTHESIS","Bad idea","bad",founder_approved=True); marker=add_knowledge("KILLED","Do not retry","failed",target_knowledge_id=h.id,founder_approved=True)
    assert h not in active_hypotheses() and marker in current()
def test_killed_marker_reaches_agent_context(ctx):
    ceo,_=people(); h=add_knowledge("HYPOTHESIS","Bad idea","bad",founder_approved=True); add_knowledge("KILLED","Never again","failed",target_knowledge_id=h.id,founder_approved=True)
    text=build(ceo); assert "KILLED IDEAS" in text and "Never again" in text

@pytest.mark.parametrize("payload",[
 {"kind":"EVIDENCE","title":"E","content":"x"},
 {"kind":"DECISION","title":"D","content":"x"},
 {"kind":"KILLED","title":"K","content":"x","target_knowledge_id":999},
])
def test_proposal_approval_cannot_bypass_brain_validation(ctx,payload):
    p=proposal(payload)
    with pytest.raises(ValueError): review_proposal(p,"APPROVED")
    assert db.session.get(Proposal,p.id).status=="PENDING" and KnowledgeItem.query.count()==0

def test_project_context_excludes_other_project_knowledge(ctx):
    ceo,_=people(); a,b=projects()
    add_knowledge("FACT","A secret","ALPHA_RAW",project_id=a.id,founder_approved=True)
    add_knowledge("FACT","B secret","BETA_RAW",project_id=b.id,founder_approved=True)
    assert "ALPHA_RAW" in build(ceo,a) and "BETA_RAW" not in build(ceo,a)
def test_projectless_context_excludes_all_project_raw_knowledge(ctx):
    ceo,_=people(); a,b=projects()
    add_knowledge("FACT","A","ALPHA_RAW",project_id=a.id,founder_approved=True); add_knowledge("FACT","B","BETA_RAW",project_id=b.id,founder_approved=True)
    text=build(ceo); assert "ALPHA_RAW" not in text and "BETA_RAW" not in text

def test_provider_receives_selected_model_config(ctx,monkeypatch):
    _,e=people(); seen={}
    class P:
        def complete(self,model_config,system_prompt,user_prompt,context,max_output_tokens):
            seen["model"]=model_config.model_name; return ProviderResult("ok",2,1,"r")
    monkeypatch.setattr("eason_one.services.execution.get_provider",lambda key:P())
    e.current_model.model_name="organization-selected-model"; db.session.commit(); execute(e,"TEST","x")
    assert seen["model"]=="organization-selected-model"
def test_agent_run_snapshots_are_immutable(ctx):
    _,e=people(); run=execute(e,"TEST","x"); snapshots=(run.provider_key_snapshot,run.model_name_snapshot,run.input_price_snapshot,run.output_price_snapshot,run.currency_snapshot)
    e.current_model.model_name="changed"; e.current_model.input_price_per_million=999; db.session.commit()
    assert snapshots==(run.provider_key_snapshot,run.model_name_snapshot,run.input_price_snapshot,run.output_price_snapshot,run.currency_snapshot)

def test_precall_budget_rejection_never_invokes_provider(ctx,monkeypatch):
    _,e=people(); called=[]; e.current_model.output_price_per_million=10_000_000; db.session.commit()
    monkeypatch.setattr("eason_one.services.execution.get_provider",lambda key:called.append(True))
    with pytest.raises(ValueError): execute(e,"TEST","x")
    assert called==[]
def test_successful_paid_call_has_exactly_one_cost_event(ctx):
    _,e=people(); e.current_model.input_price_per_million=10; db.session.commit(); run=execute(e,"TEST","x")
    assert CostEvent.query.filter_by(agent_run_id=run.id,category="MODEL").count()==1
def test_known_cost_survives_downstream_failure(ctx):
    _,e=people(); e.current_model.input_price_per_million=10; db.session.commit()
    with pytest.raises(RuntimeError): execute(e,"TEST","x",postprocess=lambda run:(_ for _ in ()).throw(RuntimeError("parse")))
    run=AgentRun.query.order_by(AgentRun.id.desc()).first()
    assert run.real_cost>0 and CostEvent.query.filter_by(agent_run_id=run.id).count()==1 and remaining()<3000
def test_recording_same_run_does_not_duplicate_cost(ctx):
    from eason_one.services.costs import record
    _,e=people(); run=execute(e,"TEST","x"); record(run); db.session.commit()
    assert CostEvent.query.filter_by(agent_run_id=run.id).count()==1

def test_ceo_structured_output_changes_tasks(ctx):
    ceo,_=people(); _,research=founder_request(ceo,"Investigate consumer demand project")
    _,engineering=founder_request(ceo,"Build a software website project")
    assert research.payload_json["plan"]["tasks"][0]["assignee_slug"]=="researcher"
    assert engineering.payload_json["plan"]["tasks"][0]["assignee_slug"]=="engineer"
def test_invalid_ceo_output_creates_no_proposal(ctx,monkeypatch):
    ceo,_=people()
    class P:
        def complete(self,*args,**kwargs): return ProviderResult("not-json",1,1)
    monkeypatch.setattr("eason_one.services.execution.get_provider",lambda key:P())
    run,p=founder_request(ceo,"anything")
    assert p is None and Proposal.query.count()==0 and "validation failed" in run.error_text
def test_invalid_second_task_rolls_back_materialization(ctx):
    ceo,_=people(); _,p=founder_request(ceo,"Investigate demand project")
    second=dict(p.payload_json["plan"]["tasks"][0]); second["assignee_slug"]="missing"; p.payload_json["plan"]["tasks"].append(second)
    from sqlalchemy.orm.attributes import flag_modified
    flag_modified(p,"payload_json"); db.session.commit()
    with pytest.raises(ValueError): materialize_project_plan(p)
    assert Project.query.count()==0 and Task.query.count()==0 and db.session.get(Proposal,p.id).status=="PENDING"

def test_interview_second_turn_contains_first_turn(ctx,monkeypatch):
    _,e=people(); captured=[]
    class P:
        def complete(self,model_config,system_prompt,user_prompt,context,max_output_tokens):
            captured.append(context); return ProviderResult("answer",1,1)
    monkeypatch.setattr("eason_one.services.execution.get_provider",lambda key:P())
    i=start(e); ask(i,"FIRST UNIQUE QUESTION"); ask(i,"second question")
    assert "FIRST UNIQUE QUESTION" in captured[1] and FounderInterview.query.count()==1
def test_project_contribution_grouped_and_company_independent(ctx):
    _,e=people(); a,b=projects()
    db.session.add_all([ContributionEvent(employee_id=e.id,project_id=a.id,scope="PROJECT",event_type="X",value=12,reason="x"),
      ContributionEvent(employee_id=e.id,project_id=b.id,scope="PROJECT",event_type="X",value=7,reason="x"),
      ContributionEvent(employee_id=e.id,scope="COMPANY",event_type="X",value=18,reason="x")]); db.session.commit()
    grouped={p.name:v for p,v in project_totals(e.id)}
    assert grouped=={"Alpha":12,"Beta":7} and total(e.id,"COMPANY")==18
def test_founder_can_create_validated_learning(ctx):
    _,e=people(); row=create_learning(e,"Lesson","Evidence",source_ref="founder:1",validated=True)
    assert row.validated and row.source_ref=="founder:1"
def test_execution_cannot_create_validated_learning(ctx):
    _,e=people(); execute(e,"TEST","claim learned"); assert EmployeeLearningRecord.query.count()==0
def test_package_discovery_excludes_instance():
    text=open("pyproject.toml",encoding="utf-8").read(); assert 'include = ["eason_one*"]' in text and 'exclude = ["instance*", "tests*"]' in text
def test_english_default(app):
    with app.test_request_context("/"): assert translate("nav.projects")=="Projects"
def test_language_switch_and_session_persistence(client):
    client.post("/language/zh-TW",data={"next":"/ceo"})
    assert "專案".encode() in client.get("/ceo").data
    assert "員工".encode() in client.get("/employees").data
def test_missing_zh_translation_falls_back_to_english(app):
    from eason_one.i18n import TRANSLATIONS
    TRANSLATIONS["en"]["test.only.en"]="English fallback"
    with app.test_request_context("/"):
        from flask import session
        session["language"]="zh-TW"; assert translate("test.only.en")=="English fallback"

``

---

## FILE: tests\test_patch0041.py

``python
from decimal import Decimal
from types import SimpleNamespace
import pytest
from eason_one.extensions import db
from eason_one.models import *
from eason_one.providers import ProviderResult,OpenAIProvider
from eason_one.services.execution import execute
from eason_one.services.brain import add_knowledge,validate_basis_ids
from eason_one.services.projects import create_project
from eason_one.services.tasks import create_task,transition
from eason_one.services.task_execution import run_task

def people():
    return Employee.query.filter_by(slug="ceo").one(),Employee.query.filter_by(slug="researcher").one()
def project(name="A"):
    return create_project(name,name,people()[0],status="ACTIVE")
def task_in(status):
    p=project(); ceo,e=people(); t=create_task(p,"Task","Work",ceo,e,ceo,required_output="x",acceptance_criteria="x")
    t.status=status; db.session.commit(); return t

def test_dependency_pins_openai_capability_floor():
    text=open("pyproject.toml",encoding="utf-8").read()
    assert "openai>=2.48.0,<3" in text
def test_openai_provider_distinguishes_response_and_request_ids(monkeypatch):
    response=SimpleNamespace(output_text="{}",usage=SimpleNamespace(input_tokens=2,output_tokens=1),id="resp_123",
      _request_id="req_456",status="completed",output=[],incomplete_details=None)
    class Responses:
        def create(self,**kwargs): return response
    class Client:
        def __init__(self,**kwargs): self.responses=Responses()
    monkeypatch.setattr("openai.OpenAI",Client); monkeypatch.setenv("OPENAI_API_KEY","not-real")
    result=OpenAIProvider().complete(SimpleNamespace(model_name="x"),"s","u","c",10)
    assert result.response_id=="resp_123" and result.request_id=="req_456"
def test_agent_run_persists_and_ui_displays_both_ids(ctx,client,monkeypatch):
    _,e=people()
    class P:
        def complete(self,*args): return ProviderResult("ok",2,1,response_id="resp_x",request_id="req_x")
    monkeypatch.setattr("eason_one.services.execution.get_provider",lambda key:P())
    run=execute(e,"TEST","x"); page=client.get(f"/runs/{run.id}").data
    assert run.provider_response_id=="resp_x" and run.provider_request_id=="req_x"
    assert b"Provider request ID: req_x" in page and b"Provider response ID: resp_x" in page

@pytest.mark.parametrize("provider_key",["mock","openai"])
def test_inactive_model_never_invokes_provider(ctx,monkeypatch,provider_key):
    _,e=people(); e.current_model.provider_key=provider_key; e.current_model.active=False
    if provider_key=="openai": e.current_model.input_price_per_million=e.current_model.output_price_per_million=1
    db.session.commit(); called=[]
    monkeypatch.setattr("eason_one.services.execution.get_provider",lambda key:called.append(key))
    with pytest.raises(ValueError,match="inactive"): execute(e,"TEST","x")
    assert not called and AgentRun.query.count()==0
def test_reactivated_model_executes(ctx):
    _,e=people(); e.current_model.active=False; db.session.commit()
    with pytest.raises(ValueError): execute(e,"TEST","x")
    e.current_model.active=True; db.session.commit()
    assert execute(e,"TEST","x").status=="SUCCEEDED"

def test_killed_marker_and_historical_hypothesis_cannot_be_basis(ctx):
    p=project(); h=add_knowledge("HYPOTHESIS","H","x",project_id=p.id,founder_approved=True)
    killed=add_knowledge("KILLED","K","no",project_id=p.id,target_knowledge_id=h.id,founder_approved=True)
    for ident in (h.id,killed.id):
        with pytest.raises(ValueError,match="effective"): validate_basis_ids([ident],p.id)
def test_corrected_target_invalid_but_correction_valid_basis(ctx):
    p=project(); fact=add_knowledge("FACT","Old","x",project_id=p.id,founder_approved=True)
    correction=add_knowledge("CORRECTION","New","y",project_id=p.id,target_knowledge_id=fact.id,founder_approved=True)
    with pytest.raises(ValueError,match="effective"): validate_basis_ids([fact.id],p.id)
    assert validate_basis_ids([correction.id],p.id)==[correction]

def test_project_cannot_kill_other_project_hypothesis(ctx):
    a,b=project("A"),project("B"); h=add_knowledge("HYPOTHESIS","B H","x",project_id=b.id,founder_approved=True)
    with pytest.raises(ValueError,match="unrelated"): add_knowledge("KILLED","K","x",project_id=a.id,target_knowledge_id=h.id,founder_approved=True)
def test_project_cannot_correct_other_project_fact(ctx):
    a,b=project("A"),project("B"); fact=add_knowledge("FACT","B F","x",project_id=b.id,founder_approved=True)
    with pytest.raises(ValueError,match="unrelated"): add_knowledge("CORRECTION","C","x",project_id=a.id,target_knowledge_id=fact.id,founder_approved=True)
def test_project_may_correct_company_fact(ctx):
    a=project(); fact=add_knowledge("FACT","Company","x",founder_approved=True)
    assert add_knowledge("CORRECTION","Scoped correction","y",project_id=a.id,target_knowledge_id=fact.id,founder_approved=True)
def test_company_cannot_target_project_knowledge(ctx):
    p=project(); fact=add_knowledge("FACT","Project","x",project_id=p.id,founder_approved=True)
    with pytest.raises(ValueError,match="Company-level"): add_knowledge("CORRECTION","C","y",target_knowledge_id=fact.id,founder_approved=True)

@pytest.mark.parametrize(("result","message"),[
 (ProviderResult("",10,4,response_id="resp_i",request_id="req_i",status="incomplete",incomplete_reason="max_output_tokens"),"incomplete: max_output_tokens"),
 (ProviderResult("",10,4,response_id="resp_r",request_id="req_r",status="completed",refusal="cannot comply"),"refusal: cannot comply"),
])
def test_billable_non_normal_response_ledgers_once(ctx,monkeypatch,result,message):
    _,e=people(); e.current_model.provider_key="openai"; e.current_model.input_price_per_million=10
    e.current_model.output_price_per_million=20; e.current_model.currency="TWD"; db.session.commit()
    class P:
        def complete(self,*args): return result
    monkeypatch.setattr("eason_one.services.execution.get_provider",lambda key:P())
    run=execute(e,"TEST","x")
    assert run.status=="FAILED" and message in run.error_text
    assert run.input_tokens==10 and run.output_tokens==4 and run.real_cost>0
    assert CostEvent.query.filter_by(agent_run_id=run.id,category="MODEL").count()==1

@pytest.mark.parametrize("status",["REVIEW","DONE","BLOCKED","FAILED","CANCELLED"])
def test_non_executable_task_rejected_before_any_side_effect(ctx,monkeypatch,status):
    task=task_in(status); task.result_summary="original"; db.session.commit(); called=[]
    before=(AgentRun.query.count(),CostEvent.query.count(),Proposal.query.count())
    monkeypatch.setattr("eason_one.services.execution.get_provider",lambda key:called.append(key))
    with pytest.raises(ValueError,match="not executable"): run_task(task)
    assert not called and before==(AgentRun.query.count(),CostEvent.query.count(),Proposal.query.count())
    assert task.result_summary=="original" and task.status==status
@pytest.mark.parametrize("status",["ASSIGNED","WORKING"])
def test_executable_task_states_are_allowed(ctx,status):
    task=task_in(status); assert run_task(task).status=="SUCCEEDED"
def test_done_task_post_cannot_double_bill_or_mutate(ctx,client,monkeypatch):
    task=task_in("DONE"); task.result_summary="final"; db.session.commit(); called=[]
    before=(AgentRun.query.count(),CostEvent.query.count(),Proposal.query.count())
    monkeypatch.setattr("eason_one.services.execution.get_provider",lambda key:called.append(key))
    response=client.post(f"/tasks/{task.id}/run",follow_redirects=True)
    assert response.status_code==200 and not called
    assert before==(AgentRun.query.count(),CostEvent.query.count(),Proposal.query.count())
    assert db.session.get(Task,task.id).result_summary=="final"

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

---

## FILE: tests\test_slice003.py

``python
import json
from decimal import Decimal
import pytest
from sqlalchemy.orm.attributes import flag_modified
from eason_one.extensions import db
from eason_one.models import *
from eason_one.providers import ProviderResult
from eason_one.services.ceo import founder_request,operating_context,materialize_project_plan,generate_project_briefing
from eason_one.services.projects import create_project
from eason_one.services.tasks import create_task,transition,store_result
from eason_one.services.reviews import run_review
from eason_one.services.costs import conservative_estimate
from eason_one.services.execution import execute
from eason_one.services.employees import change_model
from eason_one.services.approvals import review_proposal

def people():
    return (Employee.query.filter_by(slug="ceo").one(),Employee.query.filter_by(slug="researcher").one(),
      Employee.query.filter_by(slug="research-director").one())
def ready_task(instruction="work"):
    ceo,researcher,director=people(); p=create_project("Beauty","Test demand",ceo,status="ACTIVE")
    t=create_task(p,"Research",instruction,ceo,researcher,director,required_output="Brief",acceptance_criteria="Evidence")
    transition(t,"WORKING"); store_result(t,"Result with evidence"); return p,t

def test_ceo_request_never_mutates_permanent_instructions(ctx):
    ceo,_,_=people(); original=ceo.system_instructions; founder_request(ceo,"Create a pilot project")
    assert db.session.get(Employee,ceo.id).system_instructions==original
def test_ceo_execution_failure_preserves_instructions(ctx,monkeypatch):
    ceo,_,_=people(); original=ceo.system_instructions
    class Broken:
        def complete(self,*a,**k): raise RuntimeError("provider failed")
    monkeypatch.setattr("eason_one.services.execution.get_provider",lambda key:Broken())
    with pytest.raises(RuntimeError): founder_request(ceo,"Create a pilot project")
    assert db.session.get(Employee,ceo.id).system_instructions==original
def test_ceo_context_contains_every_active_slug(ctx):
    text=operating_context()
    for e in Employee.query.filter_by(active=True): assert f"slug: {e.slug}" in text
def test_ceo_context_contains_project_summaries_not_raw_knowledge(ctx):
    ceo,_,_=people(); a=create_project("Alpha","A",ceo,status="ACTIVE"); b=create_project("Beta","B",ceo,status="ACTIVE")
    db.session.add_all([KnowledgeItem(project_id=a.id,kind="FACT",title="A",content="ALPHA_RAW",founder_approved=True),
      KnowledgeItem(project_id=b.id,kind="FACT",title="B",content="BETA_RAW",founder_approved=True)]); db.session.commit()
    text=operating_context(); assert f"Project #{a.id}: Alpha" in text and f"Project #{b.id}: Beta" in text
    assert "ALPHA_RAW" not in text and "BETA_RAW" not in text

def test_new_project_creates_governed_proposal(ctx):
    run,p=founder_request(people()[0],"Create a Beauty consultation project")
    assert run.parsed_output_json["mode"]=="NEW_PROJECT" and p.status=="PENDING" and Project.query.count()==0
def test_project_action_proposes_tasks_for_existing_project(ctx):
    p,_=ready_task(); run,proposal=founder_request(people()[0],"Add task and continue project")
    assert run.parsed_output_json["mode"]=="PROJECT_ACTION" and proposal.project_id==p.id
def test_status_query_creates_no_proposal_or_project(ctx):
    p,_=ready_task(); before=(Proposal.query.count(),Project.query.count())
    run,proposal=founder_request(people()[0],"How is the Beauty project going?")
    assert run.parsed_output_json["mode"]=="STATUS_QUERY" and proposal is None
    assert (Proposal.query.count(),Project.query.count())==before
def test_invalid_project_id_is_rejected(ctx,monkeypatch):
    class P:
        def complete(self,*a,**k): return ProviderResult(json.dumps({"mode":"STATUS_QUERY","executive_response":"x","project_id":999}),1,1)
    monkeypatch.setattr("eason_one.services.execution.get_provider",lambda key:P())
    run,p=founder_request(people()[0],"status"); assert p is None and "unknown Project" in run.error_text

@pytest.mark.parametrize(("instruction","expected"),[("accept","DONE"),("please revise","WORKING"),("block this","BLOCKED")])
def test_reviewer_decisions_apply_deterministic_transition(ctx,instruction,expected):
    _,task=ready_task(); run=run_review(task,instruction=instruction)
    assert task.status==expected and run.purpose=="TASK_REVIEW"
def test_review_message_links_to_reviewer_run(ctx):
    _,task=ready_task(); run=run_review(task)
    message=WorkMessage.query.filter_by(task_id=task.id,message_type="REVIEW").one()
    assert message.agent_run_id==run.id and message.sender_employee_id==task.reviewer_employee_id
def test_malformed_review_preserves_review_state(ctx,monkeypatch):
    _,task=ready_task()
    class P:
        def complete(self,*a,**k): return ProviderResult("bad",1,1)
    monkeypatch.setattr("eason_one.services.execution.get_provider",lambda key:P())
    run_review(task); assert db.session.get(Task,task.id).status=="REVIEW" and WorkMessage.query.count()==0

def test_ceo_synthesis_receives_results_and_is_stored(ctx,client):
    project,task=ready_task(); run_review(task)
    run=generate_project_briefing(people()[0],project)
    assert "Result with evidence" in run.context_snapshot and run.parsed_output_json["executive_summary"]
    report=WorkMessage.query.filter_by(project_id=project.id,message_type="REPORT").one()
    assert report.agent_run_id==run.id and client.get(f"/projects/{project.id}").data.find(report.content.encode())>=0
def test_ceo_synthesis_does_not_complete_project(ctx):
    project,_=ready_task(); generate_project_briefing(people()[0],project)
    assert project.status=="ACTIVE"

def test_manual_evidence_and_decision_ui_use_brain_validation(ctx,client):
    project,_=ready_task()
    client.post(f"/projects/{project.id}/knowledge",data={"kind":"EVIDENCE","title":"E","content":"x"})
    client.post(f"/projects/{project.id}/knowledge",data={"kind":"DECISION","title":"D","content":"x"})
    assert KnowledgeItem.query.count()==0
    client.post(f"/projects/{project.id}/knowledge",data={"kind":"EVIDENCE","title":"E","content":"x","source_ref":"source:1"})
    assert KnowledgeItem.query.one().source_ref=="source:1"
def _knowledge_proposal(payload):
    ceo,_,_=people(); run=execute(ceo,"TEST","proposal")
    p=Proposal(agent_run_id=run.id,proposed_by_employee_id=ceo.id,payload_json=payload)
    db.session.add(p); db.session.commit(); return p
def test_founder_approves_normal_knowledge_proposal(ctx):
    p=_knowledge_proposal({"kind":"FACT","title":"F","content":"true"}); review_proposal(p,"APPROVED")
    assert p.materialized_knowledge_id and KnowledgeItem.query.count()==1
def test_founder_rejects_normal_knowledge_proposal(ctx):
    p=_knowledge_proposal({"kind":"FACT","title":"F","content":"maybe"}); review_proposal(p,"REJECTED")
    assert KnowledgeItem.query.count()==0 and p.status=="REJECTED"
def test_founder_corrects_normal_knowledge_proposal(ctx):
    p=_knowledge_proposal({"kind":"FACT","title":"Wrong","content":"wrong"})
    review_proposal(p,"CORRECTED",corrected={"kind":"FACT","title":"Right","content":"correct"})
    assert db.session.get(KnowledgeItem,p.materialized_knowledge_id).title=="Right"

def test_agent_run_page_uses_historical_snapshot(ctx,client):
    _,employee,_=people(); run=execute(employee,"TEST","x"); old=run.model_name_snapshot
    employee.current_model.model_name="live-changed"; db.session.commit(); page=client.get(f"/runs/{run.id}").data
    assert old.encode() in page and b"Historical Execution Identity" in page
def test_unicode_estimation_uses_utf8_upper_bound(ctx):
    model=people()[0].current_model; model.input_price_per_million=1
    english=conservative_estimate(model,"abcd",0); chinese=conservative_estimate(model,"中文中文",0); mixed=conservative_estimate(model,"a中🙂",0)
    assert chinese>=english and mixed>english
def test_paid_currency_mismatch_rejected_before_provider(ctx,monkeypatch):
    _,employee,_=people(); employee.current_model.input_price_per_million=1; employee.current_model.currency="USD"; db.session.commit(); called=[]
    monkeypatch.setattr("eason_one.services.execution.get_provider",lambda key:called.append(True))
    with pytest.raises(ValueError,match="currency"): execute(employee,"TEST","x")
    assert called==[]
def test_model_change_preserves_identity_history_and_run_snapshot(ctx):
    _,employee,_=people(); eid=employee.id; run=execute(employee,"TEST","before"); old=run.model_name_snapshot
    new=ModelConfig(label="Mock Two",provider_key="mock",model_name="mock-v2",input_price_per_million=0,output_price_per_million=0,currency="TWD")
    db.session.add(new); db.session.commit(); change_model(employee,new,"Founder test")
    assert employee.id==eid and EmployeeModelHistory.query.filter_by(employee_id=eid).count()==2 and run.model_name_snapshot==old
def test_fault_after_first_task_rolls_back_everything(ctx):
    ceo,_,_=people(); _,proposal=founder_request(ceo,"Create a rollback project")
    second=dict(proposal.payload_json["plan"]["tasks"][0]); proposal.payload_json["plan"]["tasks"].append(second)
    flag_modified(proposal,"payload_json"); db.session.commit()
    with pytest.raises(RuntimeError): materialize_project_plan(proposal,fault_after_task=0)
    assert Project.query.count()==0 and Task.query.count()==0 and db.session.get(Proposal,proposal.id).status=="PENDING"

``

---

## FILE: tests\test_slice004.py

``python
import sys,json
from decimal import Decimal
from types import SimpleNamespace
import pytest
from eason_one.extensions import db
from eason_one.models import *
from eason_one.providers import OpenAIProvider,ProviderResult
from eason_one.schemas import CEO_SCHEMA,REVIEW_SCHEMA,SYNTHESIS_SCHEMA,TASK_EXECUTION_SCHEMA
from eason_one.services.projects import create_project
from eason_one.services.tasks import create_task,transition
from eason_one.services.ceo import founder_request,generate_project_briefing
from eason_one.services.reviews import run_review
from eason_one.services.task_execution import run_task
from eason_one.services.execution import execute
from eason_one.services.brain import add_knowledge,current
from eason_one.services.approvals import review_proposal
from eason_one.services.model_configs import create as create_model
from eason_one.services.costs import conservative_estimate
from eason_one.services.company import get_company,remaining

def people():
    return Employee.query.filter_by(slug="ceo").one(),Employee.query.filter_by(slug="researcher").one(),Employee.query.filter_by(slug="research-director").one()
def project_task():
    ceo,e,r=people(); p=create_project("Beauty","Assess",ceo,status="ACTIVE")
    return p,create_task(p,"Research","Investigate",ceo,e,r,required_output="Brief",acceptance_criteria="Clear")
def capture_openai(monkeypatch):
    seen={}
    class Responses:
        def create(self,**kwargs):
            seen.update(kwargs); return SimpleNamespace(output_text="{}",usage=SimpleNamespace(input_tokens=1,output_tokens=1),id="fake")
    class Client:
        def __init__(self,**kwargs): self.responses=Responses()
    monkeypatch.setitem(sys.modules,"openai",SimpleNamespace(OpenAI=Client))
    monkeypatch.setenv("OPENAI_API_KEY","test-not-real")
    model=SimpleNamespace(model_name="configured-model")
    return seen,model
@pytest.mark.parametrize("schema",[CEO_SCHEMA,REVIEW_SCHEMA,SYNTHESIS_SCHEMA,TASK_EXECUTION_SCHEMA])
def test_openai_provider_passes_strict_schema(monkeypatch,schema):
    seen,model=capture_openai(monkeypatch)
    OpenAIProvider().complete(model,"system","user","context",100,schema)
    fmt=seen["text"]["format"]
    assert fmt["type"]=="json_schema" and fmt["strict"] is True and fmt["schema"]==schema["schema"]

def test_openai_zero_pricing_rejected_before_invocation(ctx,monkeypatch):
    _,e,_=people(); e.current_model.provider_key="openai"; e.current_model.input_price_per_million=0; e.current_model.output_price_per_million=1; db.session.commit(); called=[]
    monkeypatch.setattr("eason_one.services.execution.get_provider",lambda key:called.append(key))
    with pytest.raises(ValueError,match="greater than zero"): execute(e,"TEST","x")
    assert not called
def test_negative_prices_rejected_by_service(ctx):
    with pytest.raises(ValueError,match="negative"): create_model("bad","openai","x",-1,2,"TWD",10)
def test_mock_zero_pricing_is_valid(ctx):
    assert create_model("mock ok","mock","mock",0,0,"TWD",10).id
def test_model_route_cannot_bypass_validation(ctx,client):
    before=ModelConfig.query.count()
    client.post("/models",data={"label":"bad","provider_key":"openai","model_name":"x","input_price_per_million":"0",
      "output_price_per_million":"0","currency":"TWD","max_output_tokens":"100"})
    assert ModelConfig.query.count()==before
def test_near_budget_margin_rejects_before_provider(ctx,monkeypatch):
    _,e,_=people(); e.current_model.provider_key="openai"; e.current_model.input_price_per_million=1_000_000
    e.current_model.output_price_per_million=1; e.current_model.currency="TWD"; e.current_model.max_output_tokens=1; db.session.commit()
    # Leave less than the fixed framing overhead estimate.
    db.session.add(CostEvent(company_id=get_company().id,category="ADJUSTMENT",description="near cap",internal_credits_delta=0,
      real_cost_delta=Decimal("2800"),currency="TWD")); db.session.commit(); called=[]
    monkeypatch.setattr("eason_one.services.execution.get_provider",lambda key:called.append(key))
    with pytest.raises(ValueError,match="exhausted"): execute(e,"TEST","tiny")
    assert not called and conservative_estimate(e.current_model,"tiny",1)>remaining()

def test_valid_status_query_is_successful_ceo_ui(ctx,client):
    p,_=project_task(); response=client.post("/ceo",data={"request":"How is the Beauty project going?"},follow_redirects=True)
    run=AgentRun.query.filter_by(purpose="CEO_FOUNDER_REQUEST").one()
    assert run.parsed_output_json["mode"]=="STATUS_QUERY" and b"failed validation" not in response.data
    assert run.parsed_output_json["executive_response"].encode() in client.get("/ceo").data and Proposal.query.count()==0

def test_task_execution_stores_result_and_pending_proposal_only(ctx):
    p,t=project_task(); run=run_task(t)
    assert t.result_summary==run.parsed_output_json["result_summary"]
    assert Proposal.query.filter_by(project_id=p.id,status="PENDING").count()==1
    assert KnowledgeItem.query.count()==0
def candidate_result(item):
    return json.dumps({"result_summary":"done","knowledge_proposals":[item]})
@pytest.mark.parametrize("item",[
 {"kind":"EVIDENCE","title":"E","content":"x","source_ref":None,"rationale":None,"basis_knowledge_ids":[]},
 {"kind":"DECISION","title":"D","content":"x","source_ref":None,"rationale":None,"basis_knowledge_ids":[]},
 {"kind":"CORRECTION","title":"C","content":"x","source_ref":None,"rationale":None,"basis_knowledge_ids":[]},
 {"kind":"KILLED","title":"K","content":"x","source_ref":None,"rationale":None,"basis_knowledge_ids":[]},
])
def test_invalid_task_knowledge_candidate_is_skipped(ctx,monkeypatch,item):
    _,t=project_task()
    class P:
        def complete(self,*a): return ProviderResult(candidate_result(item),1,1)
    monkeypatch.setattr("eason_one.services.execution.get_provider",lambda key:P())
    run=run_task(t)
    assert run.parsed_output_json and t.result_summary=="done" and Proposal.query.count()==0 and KnowledgeItem.query.count()==0

def test_decision_basis_same_project_and_company_are_accepted(ctx):
    p,_=project_task()
    local=add_knowledge("EVIDENCE","Local","x",source_ref="s",project_id=p.id,founder_approved=True)
    company=add_knowledge("FACT","Company","x",founder_approved=True)
    decision=add_knowledge("DECISION","Choose","yes",rationale="why",project_id=p.id,founder_approved=True,basis_knowledge_ids=[local.id,company.id])
    refs=KnowledgeReference.query.filter_by(to_knowledge_id=decision.id,relation_type="BASIS_FOR").all()
    assert {r.from_knowledge_id for r in refs}=={local.id,company.id}
def test_unrelated_project_basis_is_rejected(ctx):
    p,_=project_task(); other=create_project("Other","x",people()[0],status="ACTIVE")
    basis=add_knowledge("FACT","Other fact","x",project_id=other.id,founder_approved=True)
    with pytest.raises(ValueError,match="unrelated"): add_knowledge("DECISION","D","x",rationale="r",project_id=p.id,founder_approved=True,basis_knowledge_ids=[basis.id])
def test_decision_proposal_approval_creates_provenance_atomically(ctx):
    p,_=project_task(); basis=add_knowledge("EVIDENCE","E","x",source_ref="s",project_id=p.id,founder_approved=True)
    run=execute(people()[1],"TEST","proposal",p)
    proposal=Proposal(project_id=p.id,agent_run_id=run.id,proposed_by_employee_id=people()[1].id,
      payload_json={"kind":"DECISION","title":"D","content":"yes","rationale":"because","source_ref":None,"target_knowledge_id":None,"basis_knowledge_ids":[basis.id]})
    db.session.add(proposal); db.session.commit(); review_proposal(proposal,"APPROVED")
    assert proposal.materialized_knowledge_id and KnowledgeReference.query.filter_by(to_knowledge_id=proposal.materialized_knowledge_id).count()==1
def test_provenance_failure_rolls_back_decision_and_refs(ctx,monkeypatch):
    p,_=project_task(); basis=add_knowledge("FACT","F","x",project_id=p.id,founder_approved=True)
    original=db.session.add
    def broken(obj):
        if isinstance(obj,KnowledgeReference): raise RuntimeError("reference failure")
        return original(obj)
    monkeypatch.setattr(db.session,"add",broken)
    with pytest.raises(RuntimeError): add_knowledge("DECISION","D","x",rationale="r",project_id=p.id,founder_approved=True,basis_knowledge_ids=[basis.id])
    assert KnowledgeItem.query.filter_by(kind="DECISION").count()==0 and KnowledgeReference.query.count()==0
def test_effective_ui_excludes_targets_and_keeps_killed_marker(ctx,client):
    p,_=project_task(); h=add_knowledge("HYPOTHESIS","OLD_TARGET","old",project_id=p.id,founder_approved=True)
    killed=add_knowledge("KILLED","KILLED_MARKER","no",project_id=p.id,target_knowledge_id=h.id,founder_approved=True)
    effective=current(p.id); assert h not in effective and killed in effective
    page=client.get(f"/projects/{p.id}").data
    current_section=page.split(b"Historical Knowledge")[0]
    assert b"KILLED_MARKER" in current_section and b"OLD_TARGET" not in current_section

``

---

## FILE: tests\test_slice005.py

``python
from decimal import Decimal
import pytest

from eason_one.extensions import db
from eason_one.models import (
    AgentRun, ContributionEvent, CostEvent, Employee, FounderFeedbackEvent,
    KnowledgeItem, Meeting, MeetingMessage, Project, Task,
)
from eason_one.services import meetings
from eason_one.services.ceo import operating_context
from eason_one.services.projects import classify, create_project


def people():
    return (
        Employee.query.filter_by(slug="ceo").one(),
        Employee.query.filter_by(slug="researcher").one(),
        Employee.query.filter_by(slug="critic").one(),
    )


def planned(participants=None, **limits):
    ceo, researcher, critic = people()
    participants = participants or [ceo, researcher, critic]
    return meetings.create(
        "Beauty pilot review", "Decide the next bounded action",
        "Positions, evidence, risks, and next action", ceo, participants,
        max_rounds=limits.get("max_rounds", 3),
        token_limit=limits.get("token_limit", 30000),
        real_cost_limit_twd=limits.get("real_cost_limit_twd", 100),
    )


def test_project_hygiene_filters_ceo_context_and_preserves_history(ctx):
    ceo, _, _ = people()
    live = create_project("Live", "Operate", ceo, status="ACTIVE")
    smoke = create_project("Smoke", "Test only", ceo, status="ACTIVE", environment="SMOKE")
    archived = create_project("Old", "Historical", ceo, status="ACTIVE", environment="ARCHIVED")
    run = AgentRun(
        employee_id=ceo.id, project_id=smoke.id, model_config_id=ceo.current_model.id,
        purpose="HISTORICAL", user_request="x", system_prompt_snapshot="x",
        context_snapshot="x", provider_key_snapshot="mock",
        model_name_snapshot="deterministic-mock", input_price_snapshot=0,
        output_price_snapshot=0, currency_snapshot="TWD",
    )
    db.session.add(run); db.session.commit()
    text = operating_context()
    assert live.name in text
    assert smoke.name not in text and archived.name not in text
    classify(smoke, "ARCHIVED")
    assert db.session.get(AgentRun, run.id).project_id == smoke.id


def test_existing_project_intake_creates_no_work_or_knowledge(client, ctx):
    ceo, _, _ = people()
    response = client.post("/projects/existing", data={
        "name": "Committed Beauty Work", "objective": "Launch responsibly",
        "current_state_summary": "Packaging is selected.", "status": "ACTIVE",
        "priority": "HIGH", "owner_id": ceo.id,
        "known_constraints": "No unverified claims", "next_milestone": "Founder review",
    })
    assert response.status_code == 302
    project = Project.query.filter_by(name="Committed Beauty Work").one()
    assert (project.environment, project.origin) == ("LIVE", "EXISTING")
    assert Task.query.filter_by(project_id=project.id).count() == 0
    assert KnowledgeItem.query.filter_by(project_id=project.id).count() == 0
    assert "Packaging is selected." in operating_context()


def test_meeting_lifecycle_refresh_is_read_only_and_terminal_blocks_calls(client, ctx, monkeypatch):
    meeting = planned()
    assert len(meeting.participants) == 3
    meetings.start(meeting)
    before = (MeetingMessage.query.count(), AgentRun.query.count())
    assert client.get(f"/meetings/{meeting.id}").status_code == 200
    assert client.get(f"/meetings/{meeting.id}").status_code == 200
    assert (MeetingMessage.query.count(), AgentRun.query.count()) == before
    meetings.stop(meeting, "Founder ended discussion")
    called = {"n": 0}
    monkeypatch.setattr("eason_one.services.execution.get_provider",
                        lambda key: called.__setitem__("n", called["n"] + 1))
    with pytest.raises(ValueError, match="not ACTIVE"):
        meetings.advance(meeting)
    assert called["n"] == 0
    assert meeting.status == "TERMINATED_BY_FOUNDER"


def test_only_valid_active_employees_may_participate(ctx):
    ceo, researcher, _ = people()
    invalid = Employee(name="Not persisted", slug="not-persisted")
    with pytest.raises(ValueError, match="valid persisted"):
        meetings.create("x", "y", "z", ceo, [invalid])
    researcher.active = False
    db.session.commit()
    with pytest.raises(ValueError, match="active"):
        meetings.create("x", "y", "z", ceo, [researcher])


def test_founder_join_intervention_commands_and_compact_context(ctx):
    meeting = planned()
    fact = KnowledgeItem(kind="FACT", title="Company constraint", content="No fabricated evidence",
                         founder_approved=True)
    db.session.add(fact)
    meetings.start(meeting)
    assert meeting.founder_joined_at is None
    meetings.join_founder(meeting)
    meetings.intervene(meeting, "Do not assume retailer demand.")
    meetings.command(meeting, "FOCUS", "Resolve packaging risk.")
    meetings.command(meeting, "REQUEST_EVIDENCE", "State evidence availability.")
    for round_number in range(1, 4):
        db.session.add(MeetingMessage(
            meeting_id=meeting.id, speaker_type="SYSTEM", round_number=round_number,
            message_type="SYSTEM", content=f"round-{round_number}-material",
        ))
    meeting.current_round = 3
    meeting.current_summary_json = {"disagreement": "Packaging risk remains."}
    db.session.commit()
    context = meetings.compact_context(meeting, people()[1])
    assert "Do not assume retailer demand." in context
    assert "Resolve packaging risk." in context
    assert "Packaging risk remains." in context
    assert "round-3-material" in context
    assert "round-1-material" not in context
    assert f"#{fact.id} [FACT] Company constraint" in context


def test_mock_meeting_round_usage_feedback_and_no_authoritative_mutation(ctx):
    meeting = planned()
    meetings.start(meeting)
    meetings.advance(meeting)
    assert meeting.current_round == 1
    messages = MeetingMessage.query.filter_by(
        meeting_id=meeting.id, speaker_type="EMPLOYEE").all()
    assert len(messages) == 3
    tokens, cost = meetings.usage(meeting)
    assert tokens > 0 and cost == Decimal("0")
    contributions = ContributionEvent.query.count()
    knowledge = KnowledgeItem.query.count()
    row = meetings.feedback(messages[0], "KEY_INSIGHT", "Useful distinction")
    assert row.employee_id == messages[0].employee_id
    assert FounderFeedbackEvent.query.count() == 1
    assert ContributionEvent.query.count() == contributions
    assert KnowledgeItem.query.count() == knowledge


def test_meeting_usage_aggregates_linked_actual_cost_events(ctx):
    meeting = planned()
    ceo, _, _ = people()
    run = AgentRun(
        employee_id=ceo.id, meeting_id=meeting.id, model_config_id=ceo.current_model.id,
        purpose="MEETING_TEST", user_request="x", system_prompt_snapshot="x",
        context_snapshot="x", input_tokens=12, output_tokens=8,
        provider_key_snapshot="mock", model_name_snapshot="deterministic-mock",
        input_price_snapshot=0, output_price_snapshot=0, currency_snapshot="TWD",
    )
    db.session.add(run); db.session.flush()
    db.session.add(CostEvent(
        company_id=meeting.company_id, employee_id=ceo.id, agent_run_id=run.id,
        category="MODEL_USAGE", description="linked actual", real_cost_delta=Decimal("1.250000"),
    ))
    db.session.commit()
    assert meetings.usage(meeting) == (20, Decimal("1.250000"))


@pytest.mark.parametrize("limit_kind", ["rounds", "tokens", "cost"])
def test_meeting_limits_stop_before_provider(ctx, monkeypatch, limit_kind):
    ceo, _, _ = people()
    meeting = planned([ceo], max_rounds=1, token_limit=30000)
    meetings.start(meeting)
    if limit_kind == "rounds":
        meeting.current_round = 1
    elif limit_kind == "tokens":
        meeting.token_limit = 1
    else:
        ceo.current_model.input_price_per_million = 1
        meeting.real_cost_limit_twd = 0
    db.session.commit()
    called = {"n": 0}
    monkeypatch.setattr("eason_one.services.execution.get_provider",
                        lambda key: called.__setitem__("n", called["n"] + 1))
    with pytest.raises(ValueError):
        meetings.advance(meeting)
    assert called["n"] == 0
    assert AgentRun.query.filter_by(meeting_id=meeting.id).count() == 0
    assert CostEvent.query.count() == 0


def test_inactive_participant_model_stops_before_provider(ctx, monkeypatch):
    ceo, _, _ = people()
    meeting = planned([ceo])
    meetings.start(meeting)
    ceo.current_model.active = False
    db.session.commit()
    called = {"n": 0}
    monkeypatch.setattr("eason_one.services.execution.get_provider",
                        lambda key: called.__setitem__("n", called["n"] + 1))
    with pytest.raises(ValueError, match="inactive"):
        meetings.advance(meeting)
    assert called["n"] == 0


def test_end_synthesis_persists_minutes_and_creates_no_brain_item(ctx):
    meeting = planned()
    meetings.start(meeting)
    meetings.advance(meeting)
    instructions = meeting.chair.system_instructions
    knowledge = KnowledgeItem.query.count()
    meetings.end_and_synthesize(meeting)
    meeting_id = meeting.id
    db.session.remove()
    restored = db.session.get(Meeting, meeting_id)
    assert restored.status == "ENDED"
    assert restored.minutes_json["participants"]
    assert "agreements" in restored.minutes_json
    assert KnowledgeItem.query.count() == knowledge
    assert restored.chair.system_instructions == instructions
    with pytest.raises(ValueError, match="not ACTIVE"):
        meetings.advance(restored)


def test_meeting_ui_exposes_room_controls_and_audit(client, ctx):
    meeting = planned()
    meetings.start(meeting)
    meetings.advance(meeting)
    page = client.get(f"/meetings/{meeting.id}").get_data(as_text=True)
    assert "LIVE SUMMARY" in page
    assert "TRANSCRIPT" in page
    assert "FOUNDER CONTROL" in page
    assert "ACTUAL COST" not in page  # room header reports exact TWD/tokens directly
    lobby = client.get("/meetings").get_data(as_text=True)
    assert "Meeting Lobby" in lobby and "ACTUAL COST" in lobby

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
    assert captured["client"] == {"api_key": "environment-only-test-key"}
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
