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
        # Repair the operational HR role for existing V1 databases without
        # fabricating candidates or creating a new provider configuration.
        from .seed import ensure_hr
        ensure_hr()
    return app

def _upgrade_v1_database():
    """Small SQLite compatibility migration for pre-Fix-002 local databases."""
    from sqlalchemy import inspect, text
    inspector=inspect(db.engine)
    if "model_config" in inspector.get_table_names():
        cols={x["name"] for x in inspector.get_columns("model_config")}
        if "max_output_tokens" not in cols: db.session.execute(text("ALTER TABLE model_config ADD COLUMN max_output_tokens INTEGER NOT NULL DEFAULT 1200"))
        if "archived" not in cols: db.session.execute(text("ALTER TABLE model_config ADD COLUMN archived BOOLEAN NOT NULL DEFAULT 0"))
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
        if "effective_max_output_tokens" not in cols: db.session.execute(text("ALTER TABLE agent_run ADD COLUMN effective_max_output_tokens INTEGER"))
        if "provider_stop_reason" not in cols: db.session.execute(text("ALTER TABLE agent_run ADD COLUMN provider_stop_reason VARCHAR(80)"))
        if "failure_reason" not in cols: db.session.execute(text("ALTER TABLE agent_run ADD COLUMN failure_reason VARCHAR(80)"))
        if "context_composition_json" not in cols: db.session.execute(text("ALTER TABLE agent_run ADD COLUMN context_composition_json JSON"))
        if "response_schema_snapshot_json" not in cols: db.session.execute(text("ALTER TABLE agent_run ADD COLUMN response_schema_snapshot_json JSON"))
        if "structured_validation_status" not in cols: db.session.execute(text("ALTER TABLE agent_run ADD COLUMN structured_validation_status VARCHAR(30)"))
        if "structured_validation_warnings_json" not in cols: db.session.execute(text("ALTER TABLE agent_run ADD COLUMN structured_validation_warnings_json JSON"))
        if "structured_validation_errors_json" not in cols: db.session.execute(text("ALTER TABLE agent_run ADD COLUMN structured_validation_errors_json JSON"))
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
          "operation_id":"INTEGER REFERENCES operation(id)"}
        for name,definition in additions.items():
            if name not in cols: db.session.execute(text(f"ALTER TABLE meeting ADD COLUMN {name} {definition}"))
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
    db.session.commit()
