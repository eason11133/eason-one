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
    # Fixed provider/request fee in Company currency. Search-backed providers
    # such as Perplexity Sonar charge a request component beyond token usage;
    # keeping it explicit prevents research work from silently bypassing budget.
    request_price_per_call = db.Column(db.Numeric(12, 6), default=0, nullable=False)
    currency = db.Column(db.String(8), default="TWD", nullable=False)
    max_output_tokens = db.Column(db.Integer, default=1200, nullable=False)
    active = db.Column(db.Boolean, default=True, nullable=False)
    archived = db.Column(db.Boolean, default=False, nullable=False)

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
    employment_status = db.Column(db.String(20), default="ACTIVE", nullable=False)
    probation_target_assignments = db.Column(db.Integer)
    hiring_request_id = db.Column(db.Integer)
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
    operation_id = db.Column(db.Integer, db.ForeignKey("operation.id"))
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
    # Company Core vNext compatibility pointer.  Work is the company-level
    # authoritative unit; Task remains the legacy execution/UI adapter while
    # the runtime is cut over incrementally.
    work_id = db.Column(db.Integer, db.ForeignKey("work.id"))
    project = db.relationship("Project", backref="tasks")
    assigned_employee = db.relationship("Employee", foreign_keys=[assigned_employee_id])
    reviewer = db.relationship("Employee", foreign_keys=[reviewer_employee_id])


class Work(db.Model, TimestampMixin):
    """Durable company-level work commitment.

    Work is deliberately separate from one model/tool attempt.  A Work can
    survive multiple failed Execution attempts, reassignment, restart, and
    verification without being rewritten into a provider-call status.
    """
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"), nullable=False)
    operation_id = db.Column(db.Integer, db.ForeignKey("operation.id"))
    parent_work_id = db.Column(db.Integer, db.ForeignKey("work.id"))
    title = db.Column(db.String(180), nullable=False)
    purpose = db.Column(db.Text, nullable=False)
    expected_output = db.Column(db.Text)
    acceptance_criteria = db.Column(db.Text)
    state = db.Column(db.String(20), default="PROPOSED", nullable=False)
    work_type = db.Column(db.String(30), default="DELIVERY", nullable=False)
    priority = db.Column(db.String(20), default="MEDIUM", nullable=False)
    risk_level = db.Column(db.String(20), default="NORMAL", nullable=False)
    created_by_employee_id = db.Column(db.Integer, db.ForeignKey("employee.id"))
    resource_ceiling_twd = db.Column(db.Numeric(12, 6))
    retry_limit = db.Column(db.Integer, default=1, nullable=False)
    # v0.18: Work owns its durable scheduling gate/retry checkpoint directly.
    # WaitCondition remains a legacy/history table and is not consulted by the
    # WORK_CORE_V018 scheduler.
    runtime_control_json = db.Column(db.JSON)
    accepted_at = db.Column(db.DateTime(timezone=True))
    abandoned_at = db.Column(db.DateTime(timezone=True))
    cancelled_at = db.Column(db.DateTime(timezone=True))
    updated_at = db.Column(db.DateTime(timezone=True), default=now, onupdate=now, nullable=False)
    project = db.relationship("Project", backref="works")
    operation = db.relationship("Operation", backref="works")
    created_by = db.relationship("Employee", foreign_keys=[created_by_employee_id])
    parent = db.relationship("Work", remote_side=[id])


class WorkAssignment(db.Model, TimestampMixin):
    id = db.Column(db.Integer, primary_key=True)
    work_id = db.Column(db.Integer, db.ForeignKey("work.id"), nullable=False)
    employee_id = db.Column(db.Integer, db.ForeignKey("employee.id"), nullable=False)
    responsibility = db.Column(db.String(40), default="OWNER", nullable=False)
    assigned_by_employee_id = db.Column(db.Integer, db.ForeignKey("employee.id"))
    reason = db.Column(db.Text)
    started_at = db.Column(db.DateTime(timezone=True), default=now, nullable=False)
    ended_at = db.Column(db.DateTime(timezone=True))
    work = db.relationship("Work", backref="assignments")
    employee = db.relationship("Employee", foreign_keys=[employee_id])
    assigned_by = db.relationship("Employee", foreign_keys=[assigned_by_employee_id])


class WorkDependency(db.Model, TimestampMixin):
    id = db.Column(db.Integer, primary_key=True)
    work_id = db.Column(db.Integer, db.ForeignKey("work.id"), nullable=False)
    depends_on_work_id = db.Column(db.Integer, db.ForeignKey("work.id"), nullable=False)
    dependency_type = db.Column(db.String(30), default="REQUIRES_ACCEPTED", nullable=False)
    work = db.relationship("Work", foreign_keys=[work_id], backref="dependency_edges")
    depends_on = db.relationship("Work", foreign_keys=[depends_on_work_id])
    __table_args__ = (db.UniqueConstraint("work_id", "depends_on_work_id"),)

class Operation(db.Model, TimestampMixin):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(180), nullable=False)
    objective = db.Column(db.Text, nullable=False)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"))
    proposed_by_employee_id = db.Column(db.Integer, db.ForeignKey("employee.id"), nullable=False)
    # ``status`` remains as a compatibility projection for pre-reset screens.
    # ``kernel_status`` is the authoritative lifecycle state from V0.12 onward.
    status = db.Column(db.String(30), default="PLANNED", nullable=False)
    kernel_status = db.Column(db.String(30), default="CREATED", nullable=False)
    route_type = db.Column(db.String(30), default="FULL_PROJECT", nullable=False)
    route_reason = db.Column(db.Text)
    current_stage = db.Column(db.String(40), default="CREATED", nullable=False)
    plan_json = db.Column(db.JSON, nullable=False)
    approved_budget_twd = db.Column(db.Numeric(12, 4), nullable=False)
    estimated_cost_twd = db.Column(db.Numeric(12, 6), default=0, nullable=False)
    hard_cost_cap_twd = db.Column(db.Numeric(12, 6), default=0, nullable=False)
    stage_cost_cap_twd = db.Column(db.Numeric(12, 6))
    single_call_cost_cap_twd = db.Column(db.Numeric(12, 6))
    reserved_cost_twd = db.Column(db.Numeric(12, 6), default=0, nullable=False)
    actual_cost_twd = db.Column(db.Numeric(12, 6), default=0, nullable=False)
    max_calls = db.Column(db.Integer, default=12, nullable=False)
    max_revisions = db.Column(db.Integer, default=2, nullable=False)
    max_messages = db.Column(db.Integer, default=24, nullable=False)
    max_elapsed_seconds = db.Column(db.Integer, default=3600, nullable=False)
    call_count = db.Column(db.Integer, default=0, nullable=False)
    revision_count = db.Column(db.Integer, default=0, nullable=False)
    attempt_count = db.Column(db.Integer, default=0, nullable=False)
    checkpoint_json = db.Column(db.JSON)
    lease_owner = db.Column(db.String(80))
    lease_expires_at = db.Column(db.DateTime(timezone=True))
    state_version = db.Column(db.Integer, default=0, nullable=False)
    founder_report_json = db.Column(db.JSON)
    memory_json = db.Column(db.JSON)
    waiting_reason = db.Column(db.Text)
    approved_at = db.Column(db.DateTime(timezone=True))
    ended_at = db.Column(db.DateTime(timezone=True))
    updated_at = db.Column(db.DateTime(timezone=True), default=now, onupdate=now, nullable=False)
    project = db.relationship("Project")
    proposed_by = db.relationship("Employee")
    tasks = db.relationship("Task", backref="operation", order_by="Task.id")

class OperationStep(db.Model, TimestampMixin):
    id = db.Column(db.Integer, primary_key=True)
    operation_id = db.Column(db.Integer, db.ForeignKey("operation.id"), nullable=False)
    idempotency_key = db.Column(db.String(180), nullable=False)
    logical_key = db.Column(db.String(180))
    kind = db.Column(db.String(30), nullable=False)
    status = db.Column(db.String(20), default="CLAIMED", nullable=False)
    task_id = db.Column(db.Integer, db.ForeignKey("task.id"))
    agent_run_id = db.Column(db.Integer, db.ForeignKey("agent_run.id"))
    result_json = db.Column(db.JSON)
    error_text = db.Column(db.Text)
    provider_started_at = db.Column(db.DateTime(timezone=True))
    finished_at = db.Column(db.DateTime(timezone=True))
    operation = db.relationship("Operation", backref="steps")
    task = db.relationship("Task")
    __table_args__ = (
        db.UniqueConstraint("operation_id", "idempotency_key"),
        db.UniqueConstraint("operation_id", "logical_key"),
    )

class OperationEvent(db.Model, TimestampMixin):
    """Append-only audit event for the authoritative Operation state machine."""
    id = db.Column(db.Integer, primary_key=True)
    operation_id = db.Column(db.Integer, db.ForeignKey("operation.id"), nullable=False)
    sequence = db.Column(db.Integer, nullable=False)
    event_type = db.Column(db.String(60), nullable=False)
    from_status = db.Column(db.String(30))
    to_status = db.Column(db.String(30))
    stage = db.Column(db.String(40))
    actor_type = db.Column(db.String(30), default="SYSTEM", nullable=False)
    actor_ref = db.Column(db.String(120))
    idempotency_key = db.Column(db.String(180))
    payload_json = db.Column(db.JSON)
    operation = db.relationship("Operation", backref=db.backref("events", order_by="OperationEvent.sequence"))
    __table_args__ = (
        db.UniqueConstraint("operation_id", "sequence"),
        db.UniqueConstraint("operation_id", "idempotency_key"),
    )


class CostReservation(db.Model, TimestampMixin):
    """Pre-call budget reservation. Provider calls may not bypass this ledger."""
    id = db.Column(db.Integer, primary_key=True)
    operation_id = db.Column(db.Integer, db.ForeignKey("operation.id"), nullable=False)
    agent_run_id = db.Column(db.Integer, db.ForeignKey("agent_run.id"))
    stage = db.Column(db.String(40), nullable=False)
    idempotency_key = db.Column(db.String(180), nullable=False)
    estimated_twd = db.Column(db.Numeric(12, 6), nullable=False)
    actual_twd = db.Column(db.Numeric(12, 6))
    status = db.Column(db.String(20), default="RESERVED", nullable=False)
    expires_at = db.Column(db.DateTime(timezone=True))
    resolved_at = db.Column(db.DateTime(timezone=True))
    resolution_note = db.Column(db.Text)
    operation = db.relationship("Operation", backref="cost_reservations")
    agent_run = db.relationship("AgentRun")
    __table_args__ = (db.UniqueConstraint("operation_id", "idempotency_key"),)


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
    operation_id = db.Column(db.Integer, db.ForeignKey("operation.id"))
    work_id = db.Column(db.Integer, db.ForeignKey("work.id"))
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
    provider_stop_reason = db.Column(db.String(80))
    failure_reason = db.Column(db.String(80))
    context_composition_json = db.Column(db.JSON)
    response_schema_snapshot_json = db.Column(db.JSON)
    prompt_version = db.Column(db.String(80))
    prompt_hash = db.Column(db.String(64))
    context_hash = db.Column(db.String(64))
    output_hash = db.Column(db.String(64))
    retry_of_run_id = db.Column(db.Integer, db.ForeignKey("agent_run.id"))
    attempt_number = db.Column(db.Integer, default=1, nullable=False)
    role_snapshot = db.Column(db.String(160))
    position_snapshot = db.Column(db.String(160))
    manager_snapshot = db.Column(db.String(160))
    instruction_version = db.Column(db.String(120))
    available_tools_snapshot_json = db.Column(db.JSON)
    used_tools_snapshot_json = db.Column(db.JSON)
    outcome = db.Column(db.String(30))
    failure_stage = db.Column(db.String(40))
    structured_validation_status = db.Column(db.String(30))
    structured_validation_warnings_json = db.Column(db.JSON)
    structured_validation_errors_json = db.Column(db.JSON)
    input_tokens = db.Column(db.Integer)
    output_tokens = db.Column(db.Integer)
    effective_max_output_tokens = db.Column(db.Integer)
    cache_creation_input_tokens = db.Column(db.Integer, default=0, nullable=False)
    cache_read_input_tokens = db.Column(db.Integer, default=0, nullable=False)
    real_cost = db.Column(db.Numeric(12, 6))
    currency = db.Column(db.String(8), default="TWD", nullable=False)
    provider_key_snapshot = db.Column(db.String(40), nullable=False)
    model_name_snapshot = db.Column(db.String(120), nullable=False)
    input_price_snapshot = db.Column(db.Numeric(12, 4), nullable=False)
    output_price_snapshot = db.Column(db.Numeric(12, 4), nullable=False)
    request_price_snapshot = db.Column(db.Numeric(12, 6), default=0, nullable=False)
    currency_snapshot = db.Column(db.String(8), nullable=False)
    error_text = db.Column(db.Text)
    resolution_status = db.Column(db.String(30))
    resolved_at = db.Column(db.DateTime(timezone=True))
    resolution_note = db.Column(db.Text)
    replacement_run_id = db.Column(db.Integer, db.ForeignKey("agent_run.id"))
    started_at = db.Column(db.DateTime(timezone=True), default=now, nullable=False)
    finished_at = db.Column(db.DateTime(timezone=True))
    employee = db.relationship("Employee")
    model_config = db.relationship("ModelConfig")
    work = db.relationship("Work", backref="executions")


class ExternalEffectAttempt(db.Model, TimestampMixin):
    """Durable truth for a provider/tool side effect boundary.

    Execution failure alone is insufficient to decide whether a retry is safe.
    This ledger records whether an external request was merely prepared,
    dispatched, received, persisted, and settled.
    """
    id = db.Column(db.Integer, primary_key=True)
    execution_id = db.Column(db.Integer, db.ForeignKey("agent_run.id"), nullable=False)
    work_id = db.Column(db.Integer, db.ForeignKey("work.id"))
    operation_id = db.Column(db.Integer, db.ForeignKey("operation.id"))
    provider = db.Column(db.String(40), nullable=False)
    # v0.19: replay authority is durable first-class truth. Unknown/legacy
    # effects are never guessed into a replayable class.
    effect_kind = db.Column(db.String(40))
    request_fingerprint = db.Column(db.String(64), nullable=False)
    idempotency_key = db.Column(db.String(180), nullable=False)
    state = db.Column(db.String(40), default="PREPARED", nullable=False)
    estimated_cost_twd = db.Column(db.Numeric(12, 6), default=0, nullable=False)
    actual_cost_twd = db.Column(db.Numeric(12, 6))
    cost_reservation_id = db.Column(db.Integer, db.ForeignKey("cost_reservation.id"))
    dispatched_at = db.Column(db.DateTime(timezone=True))
    response_received_at = db.Column(db.DateTime(timezone=True))
    persisted_at = db.Column(db.DateTime(timezone=True))
    settled_at = db.Column(db.DateTime(timezone=True))
    provider_request_id = db.Column(db.String(160))
    provider_response_id = db.Column(db.String(160))
    error_text = db.Column(db.Text)
    execution = db.relationship("AgentRun", backref="external_effects")
    work = db.relationship("Work")
    reservation = db.relationship("CostReservation")
    __table_args__ = (
        db.UniqueConstraint("execution_id", "idempotency_key"),
    )

class CostEvent(db.Model, TimestampMixin):
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=False)
    employee_id = db.Column(db.Integer, db.ForeignKey("employee.id"))
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"))
    task_id = db.Column(db.Integer, db.ForeignKey("task.id"))
    agent_run_id = db.Column(db.Integer, db.ForeignKey("agent_run.id"))
    operation_id = db.Column(db.Integer, db.ForeignKey("operation.id"))
    work_id = db.Column(db.Integer, db.ForeignKey("work.id"))
    stage = db.Column(db.String(40))
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


class WaitCondition(db.Model, TimestampMixin):
    id = db.Column(db.Integer, primary_key=True)
    work_id = db.Column(db.Integer, db.ForeignKey("work.id"), nullable=False)
    condition_type = db.Column(db.String(40), nullable=False)
    state = db.Column(db.String(20), default="OPEN", nullable=False)
    target_work_id = db.Column(db.Integer, db.ForeignKey("work.id"))
    reason = db.Column(db.Text, nullable=False)
    # Durable phase to resume after the wait clears. Without this, a review
    # retry can accidentally restart the Employee execution and spend tokens
    # twice. Existing rows may be NULL and fall back to READY/EXECUTING.
    resume_state = db.Column(db.String(20))
    retry_after = db.Column(db.DateTime(timezone=True))
    resolved_at = db.Column(db.DateTime(timezone=True))
    resolution_note = db.Column(db.Text)
    work = db.relationship("Work", foreign_keys=[work_id], backref="wait_conditions")
    target_work = db.relationship("Work", foreign_keys=[target_work_id])


class Artifact(db.Model, TimestampMixin):
    id = db.Column(db.Integer, primary_key=True)
    legacy_source = db.Column(db.String(40))
    legacy_source_id = db.Column(db.Integer)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"), nullable=False)
    work_id = db.Column(db.Integer, db.ForeignKey("work.id"), nullable=False)
    artifact_type = db.Column(db.String(40), default="WORK_RESULT", nullable=False)
    title = db.Column(db.String(220), nullable=False)
    project = db.relationship("Project", backref="artifacts")
    work = db.relationship("Work", backref="artifacts")
    __table_args__ = (db.UniqueConstraint("legacy_source", "legacy_source_id"),)


class ArtifactVersion(db.Model, TimestampMixin):
    id = db.Column(db.Integer, primary_key=True)
    artifact_id = db.Column(db.Integer, db.ForeignKey("artifact.id"), nullable=False)
    version = db.Column(db.Integer, nullable=False)
    producer_employee_id = db.Column(db.Integer, db.ForeignKey("employee.id"))
    execution_id = db.Column(db.Integer, db.ForeignKey("agent_run.id"))
    status = db.Column(db.String(20), default="SUBMITTED", nullable=False)
    content_text = db.Column(db.Text)
    content_location = db.Column(db.String(500))
    content_hash = db.Column(db.String(64), nullable=False)
    accepted_at = db.Column(db.DateTime(timezone=True))
    rejected_at = db.Column(db.DateTime(timezone=True))
    artifact = db.relationship("Artifact", backref="versions")
    producer = db.relationship("Employee")
    execution = db.relationship("AgentRun")
    __table_args__ = (db.UniqueConstraint("artifact_id", "version"),)


class VerificationRecord(db.Model, TimestampMixin):
    id = db.Column(db.Integer, primary_key=True)
    work_id = db.Column(db.Integer, db.ForeignKey("work.id"), nullable=False)
    artifact_version_id = db.Column(db.Integer, db.ForeignKey("artifact_version.id"))
    verifier_employee_id = db.Column(db.Integer, db.ForeignKey("employee.id"))
    agent_run_id = db.Column(db.Integer, db.ForeignKey("agent_run.id"))
    method = db.Column(db.String(30), nullable=False)
    status = db.Column(db.String(20), nullable=False)
    details_json = db.Column(db.JSON)
    work = db.relationship("Work", backref="verifications")
    artifact_version = db.relationship("ArtifactVersion")
    verifier = db.relationship("Employee")
    agent_run = db.relationship("AgentRun")


class Decision(db.Model, TimestampMixin):
    id = db.Column(db.Integer, primary_key=True)
    legacy_source = db.Column(db.String(40))
    legacy_source_id = db.Column(db.Integer)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"), nullable=False)
    work_id = db.Column(db.Integer, db.ForeignKey("work.id"))
    proposed_by_employee_id = db.Column(db.Integer, db.ForeignKey("employee.id"))
    decided_by_employee_id = db.Column(db.Integer, db.ForeignKey("employee.id"))
    question = db.Column(db.Text, nullable=False)
    decision = db.Column(db.Text, nullable=False)
    rationale = db.Column(db.Text)
    state = db.Column(db.String(20), default="PROPOSED", nullable=False)
    authority_basis = db.Column(db.Text)
    source_execution_id = db.Column(db.Integer, db.ForeignKey("agent_run.id"), unique=True)
    committed_at = db.Column(db.DateTime(timezone=True))
    project = db.relationship("Project", backref="decisions")
    work = db.relationship("Work", backref="decisions")
    proposed_by = db.relationship("Employee", foreign_keys=[proposed_by_employee_id])
    decided_by = db.relationship("Employee", foreign_keys=[decided_by_employee_id])
    source_execution = db.relationship("AgentRun", foreign_keys=[source_execution_id])
    __table_args__ = (db.UniqueConstraint("legacy_source", "legacy_source_id"),)


class Escalation(db.Model, TimestampMixin):
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"), nullable=False)
    work_id = db.Column(db.Integer, db.ForeignKey("work.id"))
    operation_id = db.Column(db.Integer, db.ForeignKey("operation.id"))
    escalation_type = db.Column(db.String(50), nullable=False)
    state = db.Column(db.String(20), default="OPEN", nullable=False)
    reason = db.Column(db.Text, nullable=False)
    options_json = db.Column(db.JSON)
    recommendation = db.Column(db.Text)
    created_by_employee_id = db.Column(db.Integer, db.ForeignKey("employee.id"))
    resolved_at = db.Column(db.DateTime(timezone=True))
    resolution = db.Column(db.Text)
    project = db.relationship("Project", backref="escalations")
    work = db.relationship("Work", backref="escalations")
    operation = db.relationship("Operation", backref="escalations")


class CompanyEvent(db.Model, TimestampMixin):
    """Append-only business/organizational event stream.

    Domain kernels may keep their own detailed events.  CompanyEvent is the
    stable observation/audit contract used by Founder surfaces and later
    organizational analytics.
    """
    id = db.Column(db.Integer, primary_key=True)
    event_type = db.Column(db.String(60), nullable=False)
    actor_type = db.Column(db.String(30), default="SYSTEM", nullable=False)
    actor_id = db.Column(db.Integer)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"))
    work_id = db.Column(db.Integer, db.ForeignKey("work.id"))
    execution_id = db.Column(db.Integer, db.ForeignKey("agent_run.id"))
    meeting_id = db.Column(db.Integer, db.ForeignKey("meeting.id"))
    artifact_id = db.Column(db.Integer, db.ForeignKey("artifact.id"))
    decision_id = db.Column(db.Integer, db.ForeignKey("decision.id"))
    causation_id = db.Column(db.Integer, db.ForeignKey("company_event.id"))
    correlation_id = db.Column(db.String(120))
    schema_version = db.Column(db.Integer, default=1, nullable=False)
    payload_json = db.Column(db.JSON)
    causation = db.relationship("CompanyEvent", remote_side=[id])

class EmployeeLearningRecord(db.Model, TimestampMixin):
    id = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.Integer, db.ForeignKey("employee.id"), nullable=False)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"))
    task_id = db.Column(db.Integer, db.ForeignKey("task.id"))
    # v0.20 Persistent Employee memory: outcome-backed experience is anchored
    # to canonical Work / Artifact / Verification truth rather than to a model
    # summary. Legacy Founder-authored learning rows may leave these NULL.
    work_id = db.Column(db.Integer, db.ForeignKey("work.id"))
    artifact_version_id = db.Column(db.Integer, db.ForeignKey("artifact_version.id"))
    verification_record_id = db.Column(db.Integer, db.ForeignKey("verification_record.id"))
    learning_type = db.Column(db.String(40), default="FOUNDER_NOTE", nullable=False)
    validation_basis = db.Column(db.String(40))
    evidence_json = db.Column(db.JSON)
    title = db.Column(db.String(180), nullable=False)
    content = db.Column(db.Text, nullable=False)
    source_ref = db.Column(db.String(300))
    validated = db.Column(db.Boolean, default=False, nullable=False)

class TalentTemplate(db.Model, TimestampMixin):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(160), nullable=False)
    role_title = db.Column(db.String(160), nullable=False)
    department_hint = db.Column(db.String(120))
    mission = db.Column(db.Text, nullable=False)
    responsibilities_json = db.Column(db.JSON, nullable=False)
    instructions = db.Column(db.Text, nullable=False)
    skills_json = db.Column(db.JSON, nullable=False)
    suggested_tools_json = db.Column(db.JSON, nullable=False)
    deliverables_json = db.Column(db.JSON, nullable=False)
    success_metrics_json = db.Column(db.JSON, nullable=False)
    source = db.Column(db.String(240), nullable=False)
    source_key = db.Column(db.String(300), unique=True, nullable=False)
    active_in_pool = db.Column(db.Boolean, default=True, nullable=False)
    suggested_model_class = db.Column(db.String(120))
    notes = db.Column(db.Text)

class HiringRequest(db.Model, TimestampMixin):
    id = db.Column(db.Integer, primary_key=True)
    source_execution_id = db.Column(db.Integer, db.ForeignKey("agent_run.id"), unique=True)
    requested_by_type = db.Column(db.String(20), nullable=False)
    requester_employee_id = db.Column(db.Integer, db.ForeignKey("employee.id"))
    operation_id = db.Column(db.Integer, db.ForeignKey("operation.id"))
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"))
    talent_template_id = db.Column(db.Integer, db.ForeignKey("talent_template.id"))
    role_needed = db.Column(db.String(160), nullable=False)
    problem = db.Column(db.Text, nullable=False)
    why_now = db.Column(db.Text, nullable=False)
    responsibilities_json = db.Column(db.JSON, nullable=False)
    capabilities_json = db.Column(db.JSON, nullable=False)
    urgency = db.Column(db.String(20), nullable=False)
    use_frequency = db.Column(db.String(30), nullable=False)
    status = db.Column(db.String(30), default="REQUESTED", nullable=False)
    hr_assessment_json = db.Column(db.JSON)
    recommended_model_config_id = db.Column(db.Integer, db.ForeignKey("model_config.id"))
    assessment_budget_twd = db.Column(db.Numeric(12, 4))
    hr_agent_run_id = db.Column(db.Integer, db.ForeignKey("agent_run.id"))
    target_department_id = db.Column(db.Integer, db.ForeignKey("department.id"))
    target_position = db.Column(db.String(160))
    manager_employee_id = db.Column(db.Integer, db.ForeignKey("employee.id"))
    resource_envelope_json = db.Column(db.JSON)
    founder_decision = db.Column(db.String(20))
    founder_decision_note = db.Column(db.Text)
    founder_decided_at = db.Column(db.DateTime(timezone=True))
    created_employee_id = db.Column(db.Integer, db.ForeignKey("employee.id"))
    updated_at = db.Column(db.DateTime(timezone=True), default=now, onupdate=now, nullable=False)
    requester = db.relationship("Employee", foreign_keys=[requester_employee_id])
    talent_template = db.relationship("TalentTemplate")
    recommended_model = db.relationship("ModelConfig")
    target_department = db.relationship("Department")
    approved_manager = db.relationship("Employee", foreign_keys=[manager_employee_id])
    created_employee = db.relationship("Employee", foreign_keys=[created_employee_id])

class MarketOrder(db.Model, TimestampMixin):
    """One durable generation of internal Eason Market demand for governed Work.

    A Work may be legitimately reassigned before settlement.  Market history is
    therefore generation-based rather than overwritten: each reassignment can
    supersede the prior unpaid order while preserving the old offers/contract as
    audit evidence.  At most one generation is ACTIVE at application level.
    """
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=False)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"), nullable=False)
    work_id = db.Column(db.Integer, db.ForeignKey("work.id"), nullable=False, index=True)
    generation = db.Column(db.Integer, default=1, nullable=False)
    supersedes_order_id = db.Column(db.Integer, db.ForeignKey("market_order.id"))
    capability = db.Column(db.String(60), nullable=False)
    buyer_type = db.Column(db.String(30), default="COMPANY", nullable=False)
    currency = db.Column(db.String(8), default="EC", nullable=False)
    status = db.Column(db.String(24), default="OPEN", nullable=False)
    policy_version = db.Column(db.String(60), default="INTERNAL_MARKET_V1_3", nullable=False)
    opened_by_employee_id = db.Column(db.Integer, db.ForeignKey("employee.id"))
    # The selected offer is canonical on MarketContract.offer_id. Keeping a
    # second order -> offer FK would form a market_order <-> market_offer cycle
    # in SQLite without adding authority truth.
    updated_at = db.Column(db.DateTime(timezone=True), default=now, onupdate=now, nullable=False)
    company = db.relationship("Company")
    project = db.relationship("Project")
    work = db.relationship("Work")
    opened_by = db.relationship("Employee", foreign_keys=[opened_by_employee_id])
    supersedes_order = db.relationship("MarketOrder", remote_side=[id], foreign_keys=[supersedes_order_id])
    __table_args__ = (db.UniqueConstraint("work_id", "generation"),)


class MarketOffer(db.Model, TimestampMixin):
    """One eligible Persistent Employee's deterministic internal-market offer."""
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey("market_order.id"), nullable=False)
    employee_id = db.Column(db.Integer, db.ForeignKey("employee.id"), nullable=False)
    quote_ec = db.Column(db.Numeric(12, 2), nullable=False)
    status = db.Column(db.String(20), default="OFFERED", nullable=False)
    rank_json = db.Column(db.JSON, nullable=False)
    rationale_json = db.Column(db.JSON, nullable=False)
    order = db.relationship("MarketOrder", backref="offers", foreign_keys=[order_id])
    employee = db.relationship("Employee")
    __table_args__ = (db.UniqueConstraint("order_id", "employee_id"),)


class MarketContract(db.Model, TimestampMixin):
    """One generation of internal labor authority bound to governed Work."""
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey("market_order.id"), nullable=False, unique=True)
    work_id = db.Column(db.Integer, db.ForeignKey("work.id"), nullable=False, index=True)
    generation = db.Column(db.Integer, default=1, nullable=False)
    supersedes_contract_id = db.Column(db.Integer, db.ForeignKey("market_contract.id"))
    seller_employee_id = db.Column(db.Integer, db.ForeignKey("employee.id"), nullable=False)
    offer_id = db.Column(db.Integer, db.ForeignKey("market_offer.id"), nullable=False, unique=True)
    agreed_ec = db.Column(db.Numeric(12, 2), nullable=False)
    currency = db.Column(db.String(8), default="EC", nullable=False)
    status = db.Column(db.String(24), default="ACTIVE", nullable=False)
    artifact_version_id = db.Column(db.Integer, db.ForeignKey("artifact_version.id"))
    verification_record_id = db.Column(db.Integer, db.ForeignKey("verification_record.id"))
    settled_at = db.Column(db.DateTime(timezone=True))
    superseded_at = db.Column(db.DateTime(timezone=True))
    settlement_note = db.Column(db.Text)
    order = db.relationship("MarketOrder", backref=db.backref("contract", uselist=False))
    work = db.relationship("Work")
    seller = db.relationship("Employee")
    offer = db.relationship("MarketOffer")
    artifact_version = db.relationship("ArtifactVersion")
    verification_record = db.relationship("VerificationRecord")
    supersedes_contract = db.relationship("MarketContract", remote_side=[id], foreign_keys=[supersedes_contract_id])
    __table_args__ = (db.UniqueConstraint("work_id", "generation"),)


class MarketLedgerEntry(db.Model, TimestampMixin):
    """Append-only internal EC ledger; never a substitute for real-cost truth."""
    id = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.Integer, db.ForeignKey("employee.id"), nullable=False)
    contract_id = db.Column(db.Integer, db.ForeignKey("market_contract.id"))
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"))
    work_id = db.Column(db.Integer, db.ForeignKey("work.id"))
    entry_type = db.Column(db.String(30), nullable=False)
    amount_ec = db.Column(db.Numeric(12, 2), nullable=False)
    reason = db.Column(db.Text, nullable=False)
    evidence_json = db.Column(db.JSON)
    employee = db.relationship("Employee")
    contract = db.relationship("MarketContract", backref="ledger_entries")
    __table_args__ = (db.UniqueConstraint("contract_id", "entry_type"),)


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


class ResearchRecord(db.Model, TimestampMixin):
    """Founder-owned research/evidence ledger entry.

    Raw execution truth remains in AgentRun, MeetingMessage, OperationStep, and
    CostEvent.  This table stores the explicit interpretation layer used for
    experiments, design decisions, failures, fixes, and demo evidence.
    """
    id = db.Column(db.Integer, primary_key=True)
    record_key = db.Column(db.String(120), unique=True, nullable=False)
    record_type = db.Column(db.String(30), nullable=False)
    title = db.Column(db.String(220), nullable=False)
    summary = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(30), default="OPEN", nullable=False)
    version_label = db.Column(db.String(80))
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"))
    operation_id = db.Column(db.Integer, db.ForeignKey("operation.id"))
    task_id = db.Column(db.Integer, db.ForeignKey("task.id"))
    meeting_id = db.Column(db.Integer, db.ForeignKey("meeting.id"))
    agent_run_id = db.Column(db.Integer, db.ForeignKey("agent_run.id"))
    source_ref = db.Column(db.String(500))
    metadata_json = db.Column(db.JSON)
    founder_approved = db.Column(db.Boolean, default=True, nullable=False)
    updated_at = db.Column(db.DateTime(timezone=True), default=now, onupdate=now, nullable=False)
    project = db.relationship("Project")
    operation = db.relationship("Operation")
    task = db.relationship("Task")
    meeting = db.relationship("Meeting")
    agent_run = db.relationship("AgentRun", foreign_keys=[agent_run_id])

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
    source_execution_id = db.Column(db.Integer, unique=True)
    related_work_id = db.Column(db.Integer, db.ForeignKey("work.id"))
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=False)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"))
    operation_id = db.Column(db.Integer, db.ForeignKey("operation.id"))
    title = db.Column(db.String(180), nullable=False)
    purpose = db.Column(db.Text, nullable=False)
    agenda = db.Column(db.Text, nullable=False)
    chair_employee_id = db.Column(db.Integer, db.ForeignKey("employee.id"), nullable=False)
    # Legacy status remains for existing pages; kernel_status is authoritative.
    status = db.Column(db.String(30), default="PLANNED", nullable=False)
    kernel_status = db.Column(db.String(30), default="PLANNED", nullable=False)
    current_stage = db.Column(db.String(40), default="PLANNING", nullable=False)
    state_version = db.Column(db.Integer, default=0, nullable=False)
    max_messages = db.Column(db.Integer, default=12, nullable=False)
    message_count = db.Column(db.Integer, default=0, nullable=False)
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
    execution_profile = db.Column(db.String(20), default="STANDARD", nullable=False)
    max_speakers_per_round = db.Column(db.Integer, default=3, nullable=False)
    contribution_output_cap = db.Column(db.Integer, default=640, nullable=False)
    router_output_cap = db.Column(db.Integer, default=256, nullable=False)
    synthesis_output_cap = db.Column(db.Integer, default=768, nullable=False)
    routing_json = db.Column(db.JSON)
    last_blocked_json = db.Column(db.JSON)
    paid_failure_json = db.Column(db.JSON)
    project = db.relationship("Project")
    operation = db.relationship("Operation")
    chair = db.relationship("Employee")

class MeetingEvent(db.Model, TimestampMixin):
    """Append-only audit event for a persistent bounded Meeting."""
    id = db.Column(db.Integer, primary_key=True)
    meeting_id = db.Column(db.Integer, db.ForeignKey("meeting.id"), nullable=False)
    sequence = db.Column(db.Integer, nullable=False)
    event_type = db.Column(db.String(60), nullable=False)
    from_status = db.Column(db.String(30))
    to_status = db.Column(db.String(30))
    stage = db.Column(db.String(40))
    actor_type = db.Column(db.String(30), default="SYSTEM", nullable=False)
    payload_json = db.Column(db.JSON)
    meeting = db.relationship("Meeting", backref=db.backref("events", order_by="MeetingEvent.sequence"))
    __table_args__ = (
        db.UniqueConstraint("meeting_id", "sequence"),
    )


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
    validation_status = db.Column(db.String(30))
    validation_warnings_json = db.Column(db.JSON)
    employee = db.relationship("Employee")
    meeting = db.relationship("Meeting", backref="messages")

class FounderFeedbackEvent(db.Model, TimestampMixin):
    id = db.Column(db.Integer, primary_key=True)
    meeting_id = db.Column(db.Integer, db.ForeignKey("meeting.id"), nullable=False)
    message_id = db.Column(db.Integer, db.ForeignKey("meeting_message.id"), nullable=False)
    employee_id = db.Column(db.Integer, db.ForeignKey("employee.id"), nullable=False)
    signal = db.Column(db.String(30), nullable=False)
    note = db.Column(db.Text)

class MeetingStep(db.Model, TimestampMixin):
    id = db.Column(db.Integer, primary_key=True)
    meeting_id = db.Column(db.Integer, db.ForeignKey("meeting.id"), nullable=False)
    logical_key = db.Column(db.String(180), nullable=False)
    kind = db.Column(db.String(40), nullable=False)
    round_number = db.Column(db.Integer, default=0, nullable=False)
    employee_id = db.Column(db.Integer, db.ForeignKey("employee.id"))
    contribution_shape = db.Column(db.String(40))
    status = db.Column(db.String(20), default="PENDING", nullable=False)
    agent_run_id = db.Column(db.Integer, db.ForeignKey("agent_run.id"))
    result_json = db.Column(db.JSON)
    error_text = db.Column(db.Text)
    finished_at = db.Column(db.DateTime(timezone=True))
    meeting = db.relationship("Meeting", backref="steps")
    employee = db.relationship("Employee")
    __table_args__=(db.UniqueConstraint("meeting_id","logical_key"),)
