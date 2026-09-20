import os
from decimal import Decimal, ROUND_HALF_UP
from flask import Flask
from .extensions import db
from .env_loader import load_project_env
load_project_env()

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
    app.jinja_env.filters["money"]=lambda value: format(
      Decimal(value or 0).quantize(Decimal("0.01"),rounding=ROUND_HALF_UP),
      ".2f")
    def english_ui(value, fallback="Not available"):
        """Keep Headquarters chrome in English without corrupting source records.

        Mission names, Task text, acceptance criteria, and stored evidence may have
        been authored in Chinese.  Those are source data, not interface labels, so
        the presentation layer must preserve them exactly rather than deleting all
        non-Latin characters and leaving punctuation fragments behind.
        """
        text=" ".join(str(value or "").split())
        return text or fallback
    app.jinja_env.filters["english_ui"]=english_ui
    from .seed import seed_command
    app.cli.add_command(seed_command)
    with app.app_context():
        _configure_sqlite_company_runtime()
        _prepare_legacy_schema_collisions()
        db.create_all()
        _upgrade_v1_database()
        # Repair the operational HR role for existing V1 databases without
        # fabricating candidates or creating a new provider configuration.
        from .seed import ensure_hr, ensure_current_org_shape, ensure_research_department_roster
        ensure_hr()
        ensure_current_org_shape()
        ensure_research_department_roster()
        # Preserve already-earned vNext career history when upgrading to the
        # Persistent Employee experience projection. This stages deterministic
        # records from accepted Work/Artifact/Verification truth only; it never
        # replays execution or asks a model to invent lessons.
        from .services.employee_memory import backfill_existing_experience
        backfill_existing_experience(commit=False)
        # v0.20: historical imports/cutovers are explicit one-time migration
        # work (scripts/migrate_v020.py). Normal process restart must never
        # replay migrations or let retired OperationKernel semantics govern a
        # current Project. The Company Runtime performs only durable v0.20
        # adoption/recovery after the process-owned thread starts.
        db.session.commit()
        # Company Core v0.20: one Work-first kernel owns approved vNext
        # execution. Founder pages are read/control surfaces only. Legacy
        # Pre-v0.18 approved LIVE Project Operations are retained only as
        # audit history; they are never restarted by the legacy worker.
        if app.config.get(
            "AUTO_START_COMPANY_RUNTIME",
            app.config.get("AUTO_START_OPERATION_RUNTIME", not app.testing),
        ):
            # Flask/Werkzeug debug reloaders import the app once in a parent and
            # again in the serving child. Only the serving process may own the
            # Company Runtime, otherwise two schedulers race on one SQLite DB.
            debug_reloader_parent = (
                os.getenv("WERKZEUG_RUN_MAIN") != "true"
                and os.getenv("FLASK_DEBUG", "").strip().lower() in {"1", "true", "yes", "on"}
            )
            if not debug_reloader_parent:
                from .services.company_runtime import start_company_runtime
                start_company_runtime(app)
    return app


def _configure_sqlite_company_runtime():
    """Make the local SQLite host tolerate real multi-Employee runtime writes.

    v0.20 can dispatch independent Work branches concurrently. SQLite still has
    one writer at a time, so WAL plus a bounded busy timeout lets those short
    durable commits serialize instead of turning normal company concurrency into
    spurious ``database is locked`` recovery. This changes host mechanics only;
    it does not weaken Work/Project transaction or authority boundaries.
    """
    from sqlalchemy import event

    engine = db.engine
    if engine.dialect.name != "sqlite":
        return
    if not getattr(engine, "_eason_one_sqlite_runtime_configured", False):
        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_connection, _connection_record):
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA busy_timeout=10000")
                cursor.execute("PRAGMA foreign_keys=ON")
            finally:
                cursor.close()
        engine._eason_one_sqlite_runtime_configured = True

    # File-backed SQLite can use WAL to keep readers and one writer from
    # needlessly blocking each other. In-memory test databases report a
    # different journal mode and are intentionally left alone.
    database = str(engine.url.database or "")
    if database and database != ":memory:":
        with engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA journal_mode=WAL")
            connection.exec_driver_sql("PRAGMA busy_timeout=10000")
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")

def _prepare_legacy_schema_collisions():
    """Rename rolled-back v0.15 tables before SQLAlchemy creates vNext tables.

    This must run *before* ``db.create_all()``.  ArtifactVersion references the
    vNext ``artifact`` table; allowing create_all to see the legacy table first
    would create a foreign key to the wrong schema and a later rename would
    preserve that incorrect reference in SQLite.
    """
    from sqlalchemy import inspect, text
    inspector=inspect(db.engine)
    tables=set(inspector.get_table_names())
    collisions=(
        (
            "company_event", "company_event_v015_legacy",
            {"company_id","actor_employee_id","object_type","institution_version"},
            "actor_id",
        ),
        (
            "artifact", "artifact_v015_legacy",
            {"work_item_id","legacy_task_id","source_agent_run_id","acceptance_status"},
            "work_id",
        ),
        (
            "decision", "decision_v015_legacy",
            {"source_work_item_id","decision_maker_type","chosen_action","authority_source"},
            "work_id",
        ),
    )
    changed=False
    for table,legacy_name,legacy_signature,new_column in collisions:
        if table not in tables:
            continue
        cols={row["name"] for row in inspector.get_columns(table)}
        if not legacy_signature.issubset(cols) or new_column in cols:
            continue
        if legacy_name in tables:
            raise RuntimeError(
                f"Both {table} and {legacy_name} contain legacy v0.15 schemas; "
                "manual reconciliation is required before migration."
            )
        db.session.execute(text(f"ALTER TABLE {table} RENAME TO {legacy_name}"))
        db.session.commit()
        changed=True
        inspector=inspect(db.engine)
        tables=set(inspector.get_table_names())
    return changed


def _upgrade_v1_database():
    """Small SQLite compatibility migration for pre-Fix-002 local databases."""
    from sqlalchemy import inspect, text
    inspector=inspect(db.engine)

    # v0.15 legacy tables were renamed before create_all().  Copy the
    # compatible event envelope into the vNext stream exactly once while the
    # full original table remains preserved for forensic history.
    tables=set(inspector.get_table_names())
    legacy_name="company_event_v015_legacy"
    if legacy_name in tables and "company_event" in tables:
        db.session.execute(text(
            "INSERT OR IGNORE INTO company_event "
            "(id,event_type,actor_type,actor_id,project_id,correlation_id,causation_id,schema_version,payload_json,created_at) "
            "SELECT id,event_type,actor_type,actor_employee_id,project_id,correlation_id,caused_by_event_id,0,payload_json,created_at "
            f"FROM {legacy_name}"
        ))
        db.session.commit()
        inspector=inspect(db.engine)
    if "model_config" in inspector.get_table_names():
        cols={x["name"] for x in inspector.get_columns("model_config")}
        if "max_output_tokens" not in cols: db.session.execute(text("ALTER TABLE model_config ADD COLUMN max_output_tokens INTEGER NOT NULL DEFAULT 1200"))
        if "archived" not in cols: db.session.execute(text("ALTER TABLE model_config ADD COLUMN archived BOOLEAN NOT NULL DEFAULT 0"))
        request_price_added = "request_price_per_call" not in cols
        if request_price_added: db.session.execute(text("ALTER TABLE model_config ADD COLUMN request_price_per_call NUMERIC(12,6) NOT NULL DEFAULT 0"))
        # Hosted Web Search is priced by OpenAI separately from model tokens.
        # Official API pricing on 2026-08-29 is USD 10 / 1,000 web-search calls
        # (USD 0.01/call) plus search-content tokens at model rates. Company
        # authority is TWD and Eason One intentionally has no live FX oracle, so
        # do NOT mislabel USD 0.01 as TWD 0.01. For the current GPT-5.6 family,
        # which the official model catalog explicitly lists as Web-search capable,
        # reserve a conservative TWD 1.00/call when an upgraded local database
        # has no explicit request reserve yet. This is a local budget reserve,
        # not a claim about the provider's final billed TWD amount. Founder may
        # override it later through ModelConfig without touching the database.
        if request_price_added:
            db.session.execute(text(
              "UPDATE model_config SET request_price_per_call=1.000000 "
              "WHERE provider_key='openai' AND currency='TWD' "
              "AND model_name LIKE 'gpt-5.6%' "
              "AND (request_price_per_call IS NULL OR request_price_per_call<=0)"))
        db.session.execute(text(
          "UPDATE model_config SET max_output_tokens=4096 "
          "WHERE provider_key='openai' AND model_name='gpt-5.6-luna' "
          "AND max_output_tokens=512"))
    if "agent_run" in inspector.get_table_names():
        cols={x["name"] for x in inspector.get_columns("agent_run")}
        additions={
          "provider_key_snapshot":"VARCHAR(40) NOT NULL DEFAULT 'legacy'",
          "model_name_snapshot":"VARCHAR(120) NOT NULL DEFAULT 'legacy'",
          "input_price_snapshot":"NUMERIC(12,4) NOT NULL DEFAULT 0",
          "output_price_snapshot":"NUMERIC(12,4) NOT NULL DEFAULT 0",
          "request_price_snapshot":"NUMERIC(12,6) NOT NULL DEFAULT 0",
          "currency_snapshot":"VARCHAR(8) NOT NULL DEFAULT 'TWD'"}
        for name,definition in additions.items():
            if name not in cols: db.session.execute(text(f"ALTER TABLE agent_run ADD COLUMN {name} {definition}"))
        if "provider_response_id" not in cols: db.session.execute(text("ALTER TABLE agent_run ADD COLUMN provider_response_id VARCHAR(160)"))
        if "cache_creation_input_tokens" not in cols: db.session.execute(text("ALTER TABLE agent_run ADD COLUMN cache_creation_input_tokens INTEGER NOT NULL DEFAULT 0"))
        if "cache_read_input_tokens" not in cols: db.session.execute(text("ALTER TABLE agent_run ADD COLUMN cache_read_input_tokens INTEGER NOT NULL DEFAULT 0"))
        if "effective_max_output_tokens" not in cols: db.session.execute(text("ALTER TABLE agent_run ADD COLUMN effective_max_output_tokens INTEGER"))
        if "provider_stop_reason" not in cols: db.session.execute(text("ALTER TABLE agent_run ADD COLUMN provider_stop_reason VARCHAR(80)"))
        if "failure_reason" not in cols: db.session.execute(text("ALTER TABLE agent_run ADD COLUMN failure_reason VARCHAR(80)"))
        if "context_composition_json" not in cols: db.session.execute(text("ALTER TABLE agent_run ADD COLUMN context_composition_json JSON"))
        if "response_schema_snapshot_json" not in cols: db.session.execute(text("ALTER TABLE agent_run ADD COLUMN response_schema_snapshot_json JSON"))
        if "structured_validation_status" not in cols: db.session.execute(text("ALTER TABLE agent_run ADD COLUMN structured_validation_status VARCHAR(30)"))
        if "structured_validation_warnings_json" not in cols: db.session.execute(text("ALTER TABLE agent_run ADD COLUMN structured_validation_warnings_json JSON"))
        if "structured_validation_errors_json" not in cols: db.session.execute(text("ALTER TABLE agent_run ADD COLUMN structured_validation_errors_json JSON"))
        if "resolution_status" not in cols: db.session.execute(text("ALTER TABLE agent_run ADD COLUMN resolution_status VARCHAR(30)"))
        if "resolved_at" not in cols: db.session.execute(text("ALTER TABLE agent_run ADD COLUMN resolved_at DATETIME"))
        if "resolution_note" not in cols: db.session.execute(text("ALTER TABLE agent_run ADD COLUMN resolution_note TEXT"))
        if "replacement_run_id" not in cols: db.session.execute(text("ALTER TABLE agent_run ADD COLUMN replacement_run_id INTEGER REFERENCES agent_run(id)"))
        if "prompt_version" not in cols: db.session.execute(text("ALTER TABLE agent_run ADD COLUMN prompt_version VARCHAR(80)"))
        if "prompt_hash" not in cols: db.session.execute(text("ALTER TABLE agent_run ADD COLUMN prompt_hash VARCHAR(64)"))
        if "context_hash" not in cols: db.session.execute(text("ALTER TABLE agent_run ADD COLUMN context_hash VARCHAR(64)"))
        if "output_hash" not in cols: db.session.execute(text("ALTER TABLE agent_run ADD COLUMN output_hash VARCHAR(64)"))
        if "retry_of_run_id" not in cols: db.session.execute(text("ALTER TABLE agent_run ADD COLUMN retry_of_run_id INTEGER REFERENCES agent_run(id)"))
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
    if "meeting" in inspector.get_table_names():
        cols={x["name"] for x in inspector.get_columns("meeting")}
        additions={"execution_profile":"VARCHAR(20) NOT NULL DEFAULT 'STANDARD'",
          "max_speakers_per_round":"INTEGER NOT NULL DEFAULT 3",
          "contribution_output_cap":"INTEGER NOT NULL DEFAULT 640",
          "router_output_cap":"INTEGER NOT NULL DEFAULT 256",
          "synthesis_output_cap":"INTEGER NOT NULL DEFAULT 768",
          "routing_json":"JSON","last_blocked_json":"JSON","paid_failure_json":"JSON",
          "operation_id":"INTEGER REFERENCES operation(id)",
          "kernel_status":"VARCHAR(30) NOT NULL DEFAULT 'PLANNED'",
          "current_stage":"VARCHAR(40) NOT NULL DEFAULT 'PLANNING'",
          "state_version":"INTEGER NOT NULL DEFAULT 0",
          "max_messages":"INTEGER NOT NULL DEFAULT 12",
          "message_count":"INTEGER NOT NULL DEFAULT 0"}
        for name,definition in additions.items():
            if name not in cols: db.session.execute(text(f"ALTER TABLE meeting ADD COLUMN {name} {definition}"))
        db.session.execute(text(
          "UPDATE meeting SET kernel_status=CASE status "
          "WHEN 'PLANNED' THEN 'READY' WHEN 'ACTIVE' THEN 'ACTIVE' "
          "WHEN 'RUNNING' THEN 'ACTIVE' WHEN 'PAUSED' THEN 'WAITING_FOR_INPUTS' "
          "WHEN 'WAITING_FOR_FOUNDER' THEN 'WAITING_FOR_INPUTS' "
          "WHEN 'ENDED' THEN 'COMPLETED' WHEN 'TERMINATED_BY_FOUNDER' THEN 'CANCELLED' "
          "ELSE kernel_status END"))
        db.session.execute(text(
          "UPDATE meeting SET current_stage=kernel_status "
          "WHERE current_stage IS NULL OR current_stage='PLANNING'"))
        db.session.execute(text(
          "UPDATE meeting SET max_messages=MAX(2, max_rounds*max_speakers_per_round+2) "
          "WHERE max_messages IS NULL OR max_messages<=0"))
        db.session.execute(text(
          "UPDATE meeting SET message_count=(SELECT COUNT(*) FROM meeting_message mm WHERE mm.meeting_id=meeting.id)"))
    if "meeting_message" in inspector.get_table_names():
        cols={x["name"] for x in inspector.get_columns("meeting_message")}
        if "validation_status" not in cols: db.session.execute(text("ALTER TABLE meeting_message ADD COLUMN validation_status VARCHAR(30)"))
        if "validation_warnings_json" not in cols: db.session.execute(text("ALTER TABLE meeting_message ADD COLUMN validation_warnings_json JSON"))
    if "meeting_step" in inspector.get_table_names():
        cols={x["name"] for x in inspector.get_columns("meeting_step")}
        if "contribution_shape" not in cols: db.session.execute(text("ALTER TABLE meeting_step ADD COLUMN contribution_shape VARCHAR(40)"))
    table_additions={
      "task":{"operation_id":"INTEGER REFERENCES operation(id)"},
      "agent_run":{"operation_id":"INTEGER REFERENCES operation(id)"},
      "cost_event":{"operation_id":"INTEGER REFERENCES operation(id)"},
      "employee":{"employment_status":"VARCHAR(20) NOT NULL DEFAULT 'ACTIVE'",
        "probation_target_assignments":"INTEGER","hiring_request_id":"INTEGER"}}
    for table,additions in table_additions.items():
        if table in inspector.get_table_names():
            cols={x["name"] for x in inspector.get_columns(table)}
            for name,definition in additions.items():
                if name not in cols:
                    db.session.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {definition}"))
    slice0101_additions={
      "operation":{"memory_json":"JSON"},
      "operation_step":{"logical_key":"VARCHAR(180)","provider_started_at":"DATETIME"},
      "hiring_request":{"assessment_budget_twd":"NUMERIC(12,4)",
        "hr_agent_run_id":"INTEGER REFERENCES agent_run(id)",
        "target_department_id":"INTEGER REFERENCES department(id)",
        "target_position":"VARCHAR(160)",
        "manager_employee_id":"INTEGER REFERENCES employee(id)",
        "resource_envelope_json":"JSON"}}
    for table,additions in slice0101_additions.items():
        if table in inspector.get_table_names():
            cols={x["name"] for x in inspector.get_columns(table)}
            for name,definition in additions.items():
                if name not in cols:
                    db.session.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {definition}"))
    # V0.12 Architecture / Founder UX Reset. Existing databases are upgraded
    # in place; the original status column remains only as a compatibility
    # projection while kernel_status + operation_event become authoritative.
    if "operation" in inspector.get_table_names():
        cols={x["name"] for x in inspector.get_columns("operation")}
        additions={
          "kernel_status":"VARCHAR(30) NOT NULL DEFAULT 'CREATED'",
          "route_type":"VARCHAR(30) NOT NULL DEFAULT 'FULL_PROJECT'",
          "route_reason":"TEXT",
          "current_stage":"VARCHAR(40) NOT NULL DEFAULT 'CREATED'",
          "estimated_cost_twd":"NUMERIC(12,6) NOT NULL DEFAULT 0",
          "hard_cost_cap_twd":"NUMERIC(12,6) NOT NULL DEFAULT 0",
          "stage_cost_cap_twd":"NUMERIC(12,6)",
          "single_call_cost_cap_twd":"NUMERIC(12,6)",
          "reserved_cost_twd":"NUMERIC(12,6) NOT NULL DEFAULT 0",
          "max_calls":"INTEGER NOT NULL DEFAULT 12",
          "max_revisions":"INTEGER NOT NULL DEFAULT 2",
          "max_messages":"INTEGER NOT NULL DEFAULT 24",
          "max_elapsed_seconds":"INTEGER NOT NULL DEFAULT 3600",
          "call_count":"INTEGER NOT NULL DEFAULT 0",
          "revision_count":"INTEGER NOT NULL DEFAULT 0",
          "attempt_count":"INTEGER NOT NULL DEFAULT 0",
          "checkpoint_json":"JSON",
          "lease_owner":"VARCHAR(80)",
          "lease_expires_at":"DATETIME",
          "state_version":"INTEGER NOT NULL DEFAULT 0",
        }
        for name,definition in additions.items():
            if name not in cols:
                db.session.execute(text(f"ALTER TABLE operation ADD COLUMN {name} {definition}"))
        db.session.execute(text(
          "UPDATE operation SET hard_cost_cap_twd=approved_budget_twd "
          "WHERE hard_cost_cap_twd IS NULL OR hard_cost_cap_twd=0"))
        db.session.execute(text(
          "UPDATE operation SET kernel_status=CASE status "
          "WHEN 'PLANNED' THEN 'WAITING_APPROVAL' "
          "WHEN 'WAITING_FOR_FOUNDER' THEN 'WAITING_APPROVAL' "
          "WHEN 'RUNNING' THEN 'RUNNING' WHEN 'PAUSED' THEN 'WAITING_INPUT' "
          "WHEN 'COMPLETED' THEN 'COMPLETED' WHEN 'FAILED' THEN 'FAILED' "
          "WHEN 'TERMINATED_BY_FOUNDER' THEN 'CANCELLED' "
          "WHEN 'SUPERSEDED' THEN 'CANCELLED' ELSE kernel_status END"))
        db.session.execute(text(
          "UPDATE operation SET current_stage=kernel_status "
          "WHERE current_stage IS NULL OR current_stage='CREATED'"))
    if "cost_event" in inspector.get_table_names():
        cols={x["name"] for x in inspector.get_columns("cost_event")}
        if "stage" not in cols:
            db.session.execute(text("ALTER TABLE cost_event ADD COLUMN stage VARCHAR(40)"))
        db.session.execute(text(
          "UPDATE cost_event SET stage=(SELECT purpose FROM agent_run WHERE agent_run.id=cost_event.agent_run_id) "
          "WHERE stage IS NULL AND agent_run_id IS NOT NULL"))
    if "operation" in inspector.get_table_names() and "agent_run" in inspector.get_table_names():
        db.session.execute(text(
          "UPDATE operation SET call_count=(SELECT COUNT(*) FROM agent_run "
          "WHERE agent_run.operation_id=operation.id AND purpose!='CEO_FOUNDER_REQUEST' "
          "AND (provider_response_id IS NOT NULL OR input_tokens IS NOT NULL "
          "OR output_tokens IS NOT NULL OR COALESCE(real_cost,0)>0))"))

    if "hiring_request" in inspector.get_table_names():
        rows=db.session.execute(text(
          "SELECT id, hr_assessment_json FROM hiring_request "
          "WHERE status='FOUNDER_REVIEW' AND hr_assessment_json IS NOT NULL"
        )).mappings().all()
        import json
        for row in rows:
            assessment=row["hr_assessment_json"]
            if isinstance(assessment,str):
                try: assessment=json.loads(assessment)
                except (TypeError,ValueError): continue
            recommendation=(assessment.get("recommendation")
              if isinstance(assessment,dict) else None)
            if recommendation and recommendation!="HIRE":
                db.session.execute(text(
                  "UPDATE hiring_request SET status='ASSESSMENT_COMPLETE' "
                  "WHERE id=:id"),{"id":row["id"]})

    # Company Core vNext Batch A. db.create_all() creates the new vNext tables;
    # these ALTERs upgrade existing local databases whose legacy tables already
    # existed before Work/Execution truth was introduced.
    vnext_additions={
      "task":{"work_id":"INTEGER REFERENCES work(id)"},
      "agent_run":{
        "work_id":"INTEGER REFERENCES work(id)",
        "attempt_number":"INTEGER NOT NULL DEFAULT 1",
        "role_snapshot":"VARCHAR(160)",
        "position_snapshot":"VARCHAR(160)",
        "manager_snapshot":"VARCHAR(160)",
        "instruction_version":"VARCHAR(120)",
        "available_tools_snapshot_json":"JSON",
        "used_tools_snapshot_json":"JSON",
        "outcome":"VARCHAR(30)",
        "failure_stage":"VARCHAR(40)",
      },
      "cost_event":{"work_id":"INTEGER REFERENCES work(id)"},
      "work":{
        "work_type":"VARCHAR(30) NOT NULL DEFAULT 'DELIVERY'",
        "runtime_control_json":"JSON",
      },
      "artifact":{"legacy_source":"VARCHAR(40)","legacy_source_id":"INTEGER"},
      "decision":{"source_execution_id":"INTEGER REFERENCES agent_run(id)","legacy_source":"VARCHAR(40)","legacy_source_id":"INTEGER"},
      "meeting":{"source_execution_id":"INTEGER","related_work_id":"INTEGER REFERENCES work(id)"},
      "hiring_request":{"source_execution_id":"INTEGER REFERENCES agent_run(id)"},
      "employee_learning_record":{
        "work_id":"INTEGER REFERENCES work(id)",
        "artifact_version_id":"INTEGER REFERENCES artifact_version(id)",
        "verification_record_id":"INTEGER REFERENCES verification_record(id)",
        "learning_type":"VARCHAR(40) NOT NULL DEFAULT 'FOUNDER_NOTE'",
        "validation_basis":"VARCHAR(40)",
        "evidence_json":"JSON",
      },
    }
    refreshed=inspect(db.engine)
    for table,additions in vnext_additions.items():
        if table in refreshed.get_table_names():
            cols={x["name"] for x in refreshed.get_columns(table)}
            for name,definition in additions.items():
                if name not in cols:
                    db.session.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {definition}"))
    if db.engine.dialect.name == "sqlite" and "employee_learning_record" in inspect(db.engine).get_table_names():
        db.session.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_employee_learning_work_experience "
            "ON employee_learning_record(employee_id, work_id, artifact_version_id, learning_type) "
            "WHERE work_id IS NOT NULL AND artifact_version_id IS NOT NULL"
        ))

    # v0.17 Work-first wait checkpoints preserve the phase that should resume.
    # db.create_all() does not add columns to an existing SQLite table.
    if "wait_condition" in inspect(db.engine).get_table_names():
        cols={x["name"] for x in inspect(db.engine).get_columns("wait_condition")}
        if "resume_state" not in cols:
            db.session.execute(text("ALTER TABLE wait_condition ADD COLUMN resume_state VARCHAR(20)"))

    if db.engine.dialect.name == "sqlite" and "decision" in inspect(db.engine).get_table_names():
        # Existing databases cannot gain SQLAlchemy's table-level UNIQUE constraint
        # through ALTER COLUMN, so preserve one-management-decision-per-Execution
        # idempotency with a partial unique index. NULL legacy rows remain valid.
        db.session.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_decision_source_execution "
            "ON decision(source_execution_id) WHERE source_execution_id IS NOT NULL"
        ))
    if db.engine.dialect.name == "sqlite":
        current_tables=set(inspect(db.engine).get_table_names())
        for table,index_name in (
            ("meeting","ux_meeting_source_execution"),
            ("hiring_request","ux_hiring_request_source_execution"),
        ):
            if table in current_tables:
                db.session.execute(text(
                    f"CREATE UNIQUE INDEX IF NOT EXISTS {index_name} "
                    f"ON {table}(source_execution_id) WHERE source_execution_id IS NOT NULL"
                ))
        for table,index_name in (
            ("artifact","ux_artifact_legacy_source"),
            ("decision","ux_decision_legacy_source"),
        ):
            if table in current_tables:
                db.session.execute(text(
                    f"CREATE UNIQUE INDEX IF NOT EXISTS {index_name} "
                    f"ON {table}(legacy_source,legacy_source_id) "
                    "WHERE legacy_source IS NOT NULL AND legacy_source_id IS NOT NULL"
                ))
    db.session.commit()
