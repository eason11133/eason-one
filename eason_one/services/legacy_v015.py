"""Forensic-safe import of the rolled-back v0.15 Runtime Spine.

The old tables remain untouched under ``*_v015_legacy`` names.  This importer
copies only facts that can be mapped unambiguously onto Batch A Work truth.
Anything that cannot be mapped stays available in the preserved legacy table
rather than being guessed into the new runtime.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from sqlalchemy import inspect, text

from ..extensions import db
from ..models import (
    Artifact, ArtifactVersion, Decision, Task, VerificationRecord, Work, now,
)
from .company_events import emit


def _legacy_work_item_task_id(work_item_id):
    if work_item_id is None:
        return None
    tables=set(inspect(db.engine).get_table_names())
    if "work_item" not in tables:
        return None
    row=db.session.execute(text(
        "SELECT legacy_task_id FROM work_item WHERE id=:id"
    ),{"id":int(work_item_id)}).mappings().first()
    return row["legacy_task_id"] if row else None


def _work_for_legacy(task_id, work_item_id=None):
    task_id=task_id or _legacy_work_item_task_id(work_item_id)
    task=db.session.get(Task,int(task_id)) if task_id is not None else None
    return db.session.get(Work,task.work_id) if task and task.work_id else None


def _content_hash(*values):
    payload="\n".join(str(value or "") for value in values)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _dt(value):
    if value is None or isinstance(value, datetime):
        return value
    text_value=str(value).strip()
    if not text_value:
        return None
    try:
        return datetime.fromisoformat(text_value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _import_artifacts():
    if "artifact_v015_legacy" not in inspect(db.engine).get_table_names():
        return {"imported":0,"unmapped":0}
    rows=db.session.execute(text(
        "SELECT id,project_id,work_item_id,legacy_task_id,source_agent_run_id,"
        "artifact_type,title,summary,version,status,creator_employee_id,content_ref,"
        "content_json,verification_status,verification_note,acceptance_status,"
        "acceptance_note,verified_at,accepted_at,created_at "
        "FROM artifact_v015_legacy ORDER BY id"
    )).mappings().all()
    imported=unmapped=0
    for row in rows:
        if Artifact.query.filter_by(
            legacy_source="v015_artifact",legacy_source_id=row["id"]
        ).first():
            continue
        work=_work_for_legacy(row["legacy_task_id"],row["work_item_id"])
        if not work:
            unmapped+=1
            continue
        artifact=Artifact(
            legacy_source="v015_artifact",legacy_source_id=row["id"],
            project_id=row["project_id"],work_id=work.id,
            artifact_type=row["artifact_type"] or "WORK_RESULT",
            title=row["title"] or f"Imported v0.15 Artifact #{row['id']}",
            created_at=_dt(row["created_at"]) or now(),
        )
        db.session.add(artifact); db.session.flush()
        acceptance=str(row["acceptance_status"] or "").upper()
        legacy_status=str(row["status"] or "").upper()
        if acceptance == "ACCEPTED" or legacy_status == "ACCEPTED":
            status="ACCEPTED"
        elif acceptance == "REJECTED" or legacy_status == "REJECTED":
            status="REJECTED"
        else:
            status="SUBMITTED"
        content_json=row["content_json"]
        if isinstance(content_json,(dict,list)):
            content_json=json.dumps(content_json,ensure_ascii=False,sort_keys=True)
        version=ArtifactVersion(
            artifact_id=artifact.id,
            version=max(1,int(row["version"] or 1)),
            producer_employee_id=row["creator_employee_id"],
            execution_id=row["source_agent_run_id"],
            status=status,
            content_text=row["summary"],
            content_location=row["content_ref"],
            content_hash=_content_hash(row["summary"],content_json,row["content_ref"]),
            accepted_at=(_dt(row["accepted_at"]) if status=="ACCEPTED" else None),
            rejected_at=None,
            created_at=_dt(row["created_at"]) or now(),
        )
        db.session.add(version); db.session.flush()
        verification=str(row["verification_status"] or "").upper()
        if verification not in {"", "PENDING", "NONE"} or status in {"ACCEPTED","REJECTED"}:
            db.session.add(VerificationRecord(
                work_id=work.id,artifact_version_id=version.id,
                verifier_employee_id=None,agent_run_id=None,
                method="LEGACY_V015_IMPORT",
                status=("ACCEPTED" if status=="ACCEPTED" else
                        "REJECTED" if status=="REJECTED" else "RECORDED"),
                details_json={
                    "legacy_verification_status":row["verification_status"],
                    "verification_note":row["verification_note"],
                    "acceptance_status":row["acceptance_status"],
                    "acceptance_note":row["acceptance_note"],
                },
            ))
        emit(
            "ARTIFACT_IMPORTED",actor_type="SYSTEM",project_id=row["project_id"],
            work_id=work.id,execution_id=row["source_agent_run_id"],artifact_id=artifact.id,
            correlation_id=f"work:{work.id}",
            payload={"legacy_table":"artifact_v015_legacy","legacy_id":row["id"],"status":status},
        )
        imported+=1
    return {"imported":imported,"unmapped":unmapped}


def _import_decisions():
    if "decision_v015_legacy" not in inspect(db.engine).get_table_names():
        return {"imported":0,"unmapped_work":0}
    rows=db.session.execute(text(
        "SELECT id,project_id,meeting_id,source_work_item_id,title,question,status,"
        "decision_maker_type,decision_maker_employee_id,authority_source,chosen_action,"
        "reason,outcome_summary,made_at,created_at FROM decision_v015_legacy ORDER BY id"
    )).mappings().all()
    imported=unmapped=0
    for row in rows:
        if Decision.query.filter_by(
            legacy_source="v015_decision",legacy_source_id=row["id"]
        ).first():
            continue
        work=_work_for_legacy(None,row["source_work_item_id"])
        if row["source_work_item_id"] is not None and not work:
            unmapped+=1
        maker=(row["decision_maker_employee_id"] if
               str(row["decision_maker_type"] or "").upper()=="EMPLOYEE" else None)
        decision=Decision(
            legacy_source="v015_decision",legacy_source_id=row["id"],
            project_id=row["project_id"],work_id=getattr(work,"id",None),
            proposed_by_employee_id=maker,decided_by_employee_id=maker,
            question=row["question"] or row["title"] or f"Imported v0.15 Decision #{row['id']}",
            decision=row["chosen_action"],
            rationale="\n".join(filter(None,[row["reason"],row["outcome_summary"]])) or None,
            state="COMMITTED",
            authority_basis=row["authority_source"],
            committed_at=_dt(row["made_at"]) or _dt(row["created_at"]) or now(),
            created_at=_dt(row["created_at"]) or now(),
        )
        db.session.add(decision); db.session.flush()
        emit(
            "DECISION_IMPORTED",actor_type=row["decision_maker_type"] or "SYSTEM",
            actor_id=maker,project_id=row["project_id"],work_id=getattr(work,"id",None),
            decision_id=decision.id,
            correlation_id=(f"work:{work.id}" if work else f"project:{row['project_id']}"),
            payload={"legacy_table":"decision_v015_legacy","legacy_id":row["id"],"action":row["chosen_action"]},
        )
        imported+=1
    return {"imported":imported,"unmapped_work":unmapped}


def import_legacy_v015_truth():
    artifacts=_import_artifacts()
    decisions=_import_decisions()
    db.session.commit()
    return {"artifacts":artifacts,"decisions":decisions}
