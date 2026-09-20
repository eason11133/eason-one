"""In-place SQLite migration for Eason One V0.12 Architecture/Founder UX Reset.

Uses only the Python standard library so it can run before the virtual
environment is installed.  The migration is additive and idempotent.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import sqlite3

OPERATION_COLUMNS = {
    "kernel_status": "VARCHAR(30) NOT NULL DEFAULT 'CREATED'",
    "route_type": "VARCHAR(30) NOT NULL DEFAULT 'FULL_PROJECT'",
    "route_reason": "TEXT",
    "current_stage": "VARCHAR(40) NOT NULL DEFAULT 'CREATED'",
    "estimated_cost_twd": "NUMERIC(12,6) NOT NULL DEFAULT 0",
    "hard_cost_cap_twd": "NUMERIC(12,6) NOT NULL DEFAULT 0",
    "stage_cost_cap_twd": "NUMERIC(12,6)",
    "single_call_cost_cap_twd": "NUMERIC(12,6)",
    "reserved_cost_twd": "NUMERIC(12,6) NOT NULL DEFAULT 0",
    "max_calls": "INTEGER NOT NULL DEFAULT 12",
    "max_revisions": "INTEGER NOT NULL DEFAULT 2",
    "max_messages": "INTEGER NOT NULL DEFAULT 24",
    "max_elapsed_seconds": "INTEGER NOT NULL DEFAULT 3600",
    "call_count": "INTEGER NOT NULL DEFAULT 0",
    "revision_count": "INTEGER NOT NULL DEFAULT 0",
    "attempt_count": "INTEGER NOT NULL DEFAULT 0",
    "checkpoint_json": "JSON",
    "lease_owner": "VARCHAR(80)",
    "lease_expires_at": "DATETIME",
    "state_version": "INTEGER NOT NULL DEFAULT 0",
}
MEETING_COLUMNS = {
    "kernel_status": "VARCHAR(30) NOT NULL DEFAULT 'PLANNED'",
    "current_stage": "VARCHAR(40) NOT NULL DEFAULT 'PLANNING'",
    "state_version": "INTEGER NOT NULL DEFAULT 0",
    "max_messages": "INTEGER NOT NULL DEFAULT 12",
    "message_count": "INTEGER NOT NULL DEFAULT 0",
}


def columns(con: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in con.execute(f"PRAGMA table_info({table})")}


def add_columns(con: sqlite3.Connection, table: str, additions: dict[str, str]) -> None:
    existing=columns(con,table)
    for name, definition in additions.items():
        if name not in existing:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def migrate(db_path: Path, *, backup: bool=True) -> Path | None:
    db_path=db_path.resolve()
    if not db_path.exists():
        raise FileNotFoundError(db_path)
    backup_path=None
    if backup:
        stamp=datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_path=db_path.with_name(f"{db_path.stem}.pre-v0120-{stamp}{db_path.suffix}")
        shutil.copy2(db_path,backup_path)
    con=sqlite3.connect(db_path)
    con.execute("PRAGMA foreign_keys=ON")
    try:
        con.execute("BEGIN IMMEDIATE")
        add_columns(con,"operation",OPERATION_COLUMNS)
        add_columns(con,"meeting",MEETING_COLUMNS)
        if "stage" not in columns(con,"cost_event"):
            con.execute("ALTER TABLE cost_event ADD COLUMN stage VARCHAR(40)")

        con.executescript("""
        CREATE TABLE IF NOT EXISTS operation_event (
          id INTEGER NOT NULL PRIMARY KEY,
          operation_id INTEGER NOT NULL REFERENCES operation(id),
          sequence INTEGER NOT NULL,
          event_type VARCHAR(60) NOT NULL,
          from_status VARCHAR(30), to_status VARCHAR(30), stage VARCHAR(40),
          actor_type VARCHAR(30) NOT NULL DEFAULT 'SYSTEM', actor_ref VARCHAR(120),
          idempotency_key VARCHAR(180), payload_json JSON,
          created_at DATETIME NOT NULL,
          UNIQUE(operation_id, sequence), UNIQUE(operation_id, idempotency_key)
        );
        CREATE TABLE IF NOT EXISTS cost_reservation (
          id INTEGER NOT NULL PRIMARY KEY,
          operation_id INTEGER NOT NULL REFERENCES operation(id),
          agent_run_id INTEGER REFERENCES agent_run(id),
          stage VARCHAR(40) NOT NULL, idempotency_key VARCHAR(180) NOT NULL,
          estimated_twd NUMERIC(12,6) NOT NULL, actual_twd NUMERIC(12,6),
          status VARCHAR(20) NOT NULL DEFAULT 'RESERVED', expires_at DATETIME,
          resolved_at DATETIME, resolution_note TEXT, created_at DATETIME NOT NULL,
          UNIQUE(operation_id, idempotency_key)
        );
        CREATE TABLE IF NOT EXISTS meeting_event (
          id INTEGER NOT NULL PRIMARY KEY,
          meeting_id INTEGER NOT NULL REFERENCES meeting(id),
          sequence INTEGER NOT NULL, event_type VARCHAR(60) NOT NULL,
          from_status VARCHAR(30), to_status VARCHAR(30), stage VARCHAR(40),
          actor_type VARCHAR(30) NOT NULL DEFAULT 'SYSTEM', payload_json JSON,
          created_at DATETIME NOT NULL,
          UNIQUE(meeting_id, sequence)
        );
        CREATE INDEX IF NOT EXISTS ix_operation_event_operation ON operation_event(operation_id, sequence);
        CREATE INDEX IF NOT EXISTS ix_cost_reservation_operation ON cost_reservation(operation_id, status);
        CREATE INDEX IF NOT EXISTS ix_meeting_event_meeting ON meeting_event(meeting_id, sequence);
        """)
        con.execute("""UPDATE operation SET
          kernel_status=CASE status
            WHEN 'PLANNED' THEN 'WAITING_APPROVAL'
            WHEN 'WAITING_FOR_FOUNDER' THEN 'WAITING_APPROVAL'
            WHEN 'RUNNING' THEN 'RUNNING' WHEN 'PAUSED' THEN 'WAITING_INPUT'
            WHEN 'COMPLETED' THEN 'COMPLETED' WHEN 'FAILED' THEN 'FAILED'
            WHEN 'TERMINATED_BY_FOUNDER' THEN 'CANCELLED'
            WHEN 'SUPERSEDED' THEN 'CANCELLED' ELSE kernel_status END,
          hard_cost_cap_twd=CASE WHEN hard_cost_cap_twd IS NULL OR hard_cost_cap_twd=0
            THEN approved_budget_twd ELSE hard_cost_cap_twd END,
          current_stage=CASE WHEN current_stage IS NULL OR current_stage='CREATED'
            THEN CASE status WHEN 'PLANNED' THEN 'APPROVAL'
              WHEN 'WAITING_FOR_FOUNDER' THEN 'FOUNDER_GATE'
              WHEN 'RUNNING' THEN 'EXECUTION' WHEN 'PAUSED' THEN 'WAITING_INPUT'
              WHEN 'COMPLETED' THEN 'COMPLETED' WHEN 'FAILED' THEN 'FAILED'
              ELSE 'CANCELLED' END ELSE current_stage END
        """)
        con.execute("""UPDATE operation SET route_type=CASE
          WHEN EXISTS(SELECT 1 FROM meeting m WHERE m.operation_id=operation.id) THEN 'SHORT_MEETING'
          WHEN (SELECT COUNT(*) FROM task t WHERE t.operation_id=operation.id)>1 THEN 'FULL_PROJECT'
          ELSE 'SINGLE_WORKER' END
          WHERE route_type IS NULL OR route_type='' OR route_type='FULL_PROJECT'
        """)
        con.execute("""UPDATE cost_event SET stage=(
          SELECT ar.purpose FROM agent_run ar WHERE ar.id=cost_event.agent_run_id
        ) WHERE stage IS NULL AND agent_run_id IS NOT NULL""")
        con.execute("""UPDATE operation SET call_count=(
          SELECT COUNT(*) FROM agent_run ar WHERE ar.operation_id=operation.id
            AND ar.purpose!='CEO_FOUNDER_REQUEST'
            AND (ar.provider_response_id IS NOT NULL OR ar.input_tokens IS NOT NULL
              OR ar.output_tokens IS NOT NULL OR COALESCE(ar.real_cost,0)>0)
        )""")
        con.execute("""UPDATE meeting SET
          kernel_status=CASE status WHEN 'PLANNED' THEN 'READY'
            WHEN 'ACTIVE' THEN 'ACTIVE' WHEN 'RUNNING' THEN 'ACTIVE'
            WHEN 'PAUSED' THEN 'WAITING_FOR_INPUTS'
            WHEN 'WAITING_FOR_FOUNDER' THEN 'WAITING_FOR_INPUTS'
            WHEN 'ENDED' THEN 'COMPLETED'
            WHEN 'TERMINATED_BY_FOUNDER' THEN 'CANCELLED' ELSE kernel_status END,
          current_stage=CASE status WHEN 'PLANNED' THEN 'READY'
            WHEN 'ACTIVE' THEN 'ACTIVE' WHEN 'RUNNING' THEN 'ACTIVE'
            WHEN 'PAUSED' THEN 'WAITING_FOR_INPUTS'
            WHEN 'WAITING_FOR_FOUNDER' THEN 'WAITING_FOR_INPUTS'
            WHEN 'ENDED' THEN 'COMPLETED' ELSE 'CANCELLED' END,
          max_messages=CASE WHEN max_messages IS NULL OR max_messages<=0
            THEN MAX(2,max_rounds*max_speakers_per_round+2) ELSE max_messages END,
          message_count=(SELECT COUNT(*) FROM meeting_message mm WHERE mm.meeting_id=meeting.id)
        """)

        timestamp=utcnow()
        terminal={"COMPLETED","FAILED","CANCELLED"}
        for project_id,status,origin in con.execute("SELECT id,status,origin FROM project"):
            if origin!="CEO_OPERATION":
                continue
            states=[row[0] for row in con.execute(
                "SELECT kernel_status FROM operation WHERE project_id=?",(project_id,)).fetchall()]
            if not states or any(state not in terminal for state in states):
                continue
            new_status="REVIEW" if "COMPLETED" in states else "CANCELLED"
            summary=("All governing Operations are terminal; completed delivery awaits Founder review."
              if new_status=="REVIEW" else "All governing Operations ended without an accepted delivery.")
            con.execute("UPDATE project SET status=?,current_state_summary=? WHERE id=?",
              (new_status,summary,project_id))

        for row in con.execute("SELECT id,status,kernel_status,route_type,current_stage FROM operation ORDER BY id"):
            operation_id,legacy,kernel,route,stage=row
            exists=con.execute("SELECT 1 FROM operation_event WHERE operation_id=? LIMIT 1",(operation_id,)).fetchone()
            if not exists:
                payload=json.dumps({"legacy_status":legacy,"route_type":route,"migration":"v0.12.0"},separators=(",",":"))
                con.execute("""INSERT INTO operation_event
                  (operation_id,sequence,event_type,to_status,stage,actor_type,idempotency_key,payload_json,created_at)
                  VALUES(?,1,'OPERATION_IMPORTED',?,?, 'SYSTEM','kernel-bootstrap',?,?)""",
                  (operation_id,kernel,stage,payload,timestamp))
        for row in con.execute("SELECT id,status,kernel_status,current_stage FROM meeting ORDER BY id"):
            meeting_id,legacy,kernel,stage=row
            exists=con.execute("SELECT 1 FROM meeting_event WHERE meeting_id=? LIMIT 1",(meeting_id,)).fetchone()
            if not exists:
                payload=json.dumps({"legacy_status":legacy,"migration":"v0.12.0"},separators=(",",":"))
                con.execute("""INSERT INTO meeting_event
                  (meeting_id,sequence,event_type,to_status,stage,actor_type,payload_json,created_at)
                  VALUES(?,1,'MEETING_IMPORTED',?,?, 'SYSTEM',?,?)""",
                  (meeting_id,kernel,stage,payload,timestamp))
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
    return backup_path


def main() -> None:
    parser=argparse.ArgumentParser()
    parser.add_argument("db",nargs="?",default="instance/eason_one.db")
    parser.add_argument("--no-backup",action="store_true")
    args=parser.parse_args()
    backup=migrate(Path(args.db),backup=not args.no_backup)
    print(f"Migrated: {Path(args.db).resolve()}")
    if backup: print(f"Backup:   {backup}")

if __name__=="__main__":
    main()
