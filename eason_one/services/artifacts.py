"""Durable Work artifacts, verification evidence, and acceptance provenance."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from urllib.parse import urlparse

from ..extensions import db
from ..models import Artifact, ArtifactVersion, VerificationRecord, Work, now
from .company_events import correlation_for_work, emit
from . import work_runtime


def _hash(text: str | None, location: str | None) -> str:
    payload = (text or "") + "\nLOCATION:" + (location or "")
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def submit_from_execution(work: Work, run, *, artifact_type: str = "WORK_RESULT", title: str | None = None, content_text: str | None = None, content_location: str | None = None) -> ArtifactVersion:
    artifact = Artifact.query.filter_by(work_id=work.id, artifact_type=artifact_type).order_by(Artifact.id).first()
    if artifact:
        existing = ArtifactVersion.query.filter_by(
            artifact_id=artifact.id, execution_id=run.id
        ).order_by(ArtifactVersion.id.desc()).first()
        if existing:
            return existing
    if not artifact:
        artifact = Artifact(
            project_id=work.project_id,
            work_id=work.id,
            artifact_type=artifact_type,
            title=(title or work.title)[:220],
        )
        db.session.add(artifact)
        db.session.flush()
    version_no = (db.session.query(db.func.max(ArtifactVersion.version)).filter_by(artifact_id=artifact.id).scalar() or 0) + 1
    text = content_text if content_text is not None else (run.raw_output or run.error_text or "")
    version = ArtifactVersion(
        artifact_id=artifact.id,
        version=version_no,
        producer_employee_id=run.employee_id,
        execution_id=run.id,
        status="SUBMITTED",
        content_text=text,
        content_location=content_location,
        content_hash=_hash(text, content_location),
    )
    db.session.add(version)
    db.session.flush()
    emit(
        "ARTIFACT_SUBMITTED",
        actor_type="EMPLOYEE",
        actor_id=run.employee_id,
        project_id=work.project_id,
        work_id=work.id,
        execution_id=run.id,
        artifact_id=artifact.id,
        correlation_id=correlation_for_work(work.id),
        payload={"artifact_version_id": version.id, "version": version.version, "type": artifact_type},
    )
    return version




def deliverable_targets(version: ArtifactVersion) -> list[dict]:
    """Project exact Founder-approved repository outputs for one accepted ArtifactVersion.

    The UI must never turn a repository root, arbitrary content_location, or model
    prose into file authority. Deliverables come only from the frozen Work
    contract and the governed Codex repository boundary.
    """
    if version is None or version.status != "ACCEPTED":
        return []
    artifact = version.artifact
    work = getattr(artifact, "work", None) if artifact is not None else None
    if work is None:
        return []
    control = dict(work.runtime_control_json or {})
    targets = dict(control.get("deliverable_targets") or {})
    if (
        targets.get("version") != "DELIVERABLE_TARGETS_V1"
        or targets.get("kind") != "REPOSITORY_PATHS"
    ):
        return []

    # Revalidate the hash-bound Founder-approved scope instead of trusting the
    # convenience deliverable projection by itself.
    frozen_scope = dict(control.get("codex_write_scope") or {})
    if not frozen_scope:
        return []
    scope_body = {key: value for key, value in frozen_scope.items() if key != "scope_hash"}
    expected_scope_hash = hashlib.sha256(
        json.dumps(scope_body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if frozen_scope.get("scope_hash") != expected_scope_hash:
        return []
    connector = __import__(
        "eason_one.services.codex_connector", fromlist=["normalize_write_scope"]
    )
    try:
        normalized_scope = connector.normalize_write_scope({
            "version": frozen_scope.get("version"),
            "paths": frozen_scope.get("paths"),
        })
    except ValueError:
        return []
    raw_paths = list(normalized_scope.get("paths") or [])
    if list(targets.get("paths") or []) != raw_paths:
        return []

    boundary = dict(control.get("codex_execution_boundary") or {})
    if not boundary:
        return []
    boundary_body = {key: value for key, value in boundary.items() if key != "boundary_hash"}
    expected_boundary_hash = hashlib.sha256(
        json.dumps(boundary_body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if boundary.get("boundary_hash") != expected_boundary_hash:
        return []
    if list(boundary.get("allowed_paths") or []) != raw_paths:
        return []
    repo_value = str(boundary.get("repo_path") or "").strip()
    if not repo_value:
        return []
    try:
        repo = Path(repo_value).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return []

    expected_hashes = {}
    run_context = dict(getattr(version.execution, "context_composition_json", None) or {})
    host_validation = dict(run_context.get("host_validation") or {})
    expected_hashes.update({
        str(key): str(value) for key, value in dict(host_validation.get("changed_file_hashes") or {}).items()
        if value
    })
    if not expected_hashes:
        proof = (
            VerificationRecord.query.filter_by(
                artifact_version_id=version.id, method="HOST_ENGINEERING_VALIDATION", status="PASSED"
            ).order_by(VerificationRecord.id.desc()).first()
        )
        observations = dict((getattr(proof, "details_json", None) or {}).get("host_observations") or {}) if proof else {}
        expected_hashes.update({
            str(key): str(value) for key, value in dict(observations.get("changed_file_hashes") or {}).items()
            if value
        })

    rows: list[dict] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_paths):
        relative = str(raw or "").strip().replace("\\", "/")
        if not relative or relative in seen:
            continue
        seen.add(relative)
        try:
            candidate = (repo / Path(relative)).resolve()
        except (OSError, RuntimeError, ValueError):
            continue
        if candidate == repo or repo not in candidate.parents:
            continue
        current_hash = None
        if candidate.is_file():
            try:
                current_hash = hashlib.sha256(candidate.read_bytes()).hexdigest()
            except OSError:
                current_hash = None
        expected_hash = expected_hashes.get(relative)
        integrity = (
            "VERIFIED" if expected_hash and current_hash == expected_hash
            else "CHANGED" if expected_hash and current_hash
            else "MISSING" if expected_hash and not current_hash
            else "UNPROVEN"
        )
        rows.append({
            "index": index,
            "path": relative,
            "name": Path(relative).name or relative,
            "available": integrity == "VERIFIED",
            "integrity": integrity,
            "expected_sha256": expected_hash,
            "current_sha256": current_hash,
            "_resolved_path": candidate,
        })
    return rows



def readable_artifact(version: ArtifactVersion) -> dict:
    """Project one accepted DB-native ArtifactVersion into a Founder-readable view.

    This is a read projection only. It never promotes SUBMITTED output, never
    interprets model prose as verification, and rechecks the immutable content
    hash before showing accepted content as a company result.
    """
    if version is None or version.status != "ACCEPTED":
        return {"available": False, "integrity": "UNAVAILABLE", "blocks": []}
    artifact = getattr(version, "artifact", None)
    project = getattr(artifact, "project", None) if artifact is not None else None
    if project is None or project.environment != "LIVE":
        return {"available": False, "integrity": "UNAVAILABLE", "blocks": []}

    text = str(version.content_text or "")
    if not text.strip():
        return {"available": False, "integrity": "EMPTY", "blocks": []}
    integrity = "VERIFIED" if _hash(text, version.content_location) == version.content_hash else "CHANGED"
    if integrity != "VERIFIED":
        return {"available": False, "integrity": integrity, "blocks": []}

    blocks: list[dict] = []

    def label(value) -> str:
        return " ".join(part for part in str(value or "").replace("_", " " ).split()).strip().title()

    def scalar(value) -> str:
        if value is None:
            return ""
        if isinstance(value, bool):
            return "Yes" if value else "No"
        return str(value).strip()

    def walk(value, *, level: int = 2, key: str | None = None, depth: int = 0):
        if len(blocks) >= 240 or depth > 8:
            return
        if key:
            blocks.append({"kind": "heading", "level": max(2, min(level, 4)), "text": label(key)})
        if isinstance(value, dict):
            for child_key, child in value.items():
                walk(child, level=level + 1, key=str(child_key), depth=depth + 1)
            return
        if isinstance(value, list):
            if all(not isinstance(item, (dict, list)) for item in value):
                for item in value:
                    text_value = scalar(item)
                    if text_value:
                        blocks.append({"kind": "bullet", "text": text_value})
                return
            for index, item in enumerate(value, start=1):
                walk(item, level=level + 1, key=(f"Item {index}" if isinstance(item, (dict, list)) else None), depth=depth + 1)
            return
        text_value = scalar(value)
        if text_value:
            blocks.append({"kind": "paragraph", "text": text_value})

    parsed = None
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        parsed = None
    if isinstance(parsed, (dict, list)):
        walk(parsed)
        format_name = "STRUCTURED"
    else:
        # Preserve exact model-authored text while giving long prose a readable
        # paragraph rhythm. No Markdown/HTML from the model is executed.
        for paragraph in [row.strip() for row in text.replace("\r\n", "\n").split("\n\n") if row.strip()]:
            blocks.append({"kind": "paragraph", "text": paragraph})
        format_name = "TEXT"

    sources = []
    run_context = dict(getattr(version.execution, "context_composition_json", None) or {})
    for row in (run_context.get("provider_sources") or []):
        if not isinstance(row, dict):
            continue
        url = str(row.get("url") or "").strip()
        parsed_url = urlparse(url)
        if parsed_url.scheme.lower() not in {"http", "https"} or not parsed_url.netloc:
            continue
        sources.append({
            "title": str(row.get("title") or "Source").strip()[:500],
            "url": url[:2000],
            "date": str(row.get("date") or row.get("last_updated") or "").strip()[:200],
        })
        if len(sources) >= 40:
            break

    return {
        "available": bool(blocks),
        "integrity": integrity,
        "format": format_name,
        "blocks": blocks,
        "sources": sources,
        "content_hash": version.content_hash,
    }

def resolve_deliverable(version: ArtifactVersion, index: int) -> Path:
    """Resolve one exact accepted deliverable path or fail closed."""
    for row in deliverable_targets(version):
        if int(row["index"]) != int(index):
            continue
        candidate = row["_resolved_path"]
        if not row["available"] or not candidate.is_file():
            raise FileNotFoundError(str(row["path"]))
        return candidate
    raise FileNotFoundError(f"deliverable:{index}")


def record_verification(
    work: Work,
    version: ArtifactVersion,
    *,
    method: str,
    status: str,
    verifier_employee_id: int | None = None,
    agent_run_id: int | None = None,
    details: dict | None = None,
) -> VerificationRecord:
    """Persist proof without implicitly accepting the Work.

    v0.19 separates evidence production from acceptance authority.  Mixed Work
    contracts can therefore retain deterministic host proof while waiting for a
    different Employee to prove semantic criteria.
    """
    if status not in {"PASSED", "FAILED", "UNPROVEN"}:
        raise ValueError(f"Unsupported verification status {status}")
    existing = VerificationRecord.query.filter_by(
        work_id=work.id,
        artifact_version_id=version.id,
        method=method,
        agent_run_id=agent_run_id,
    ).order_by(VerificationRecord.id.desc()).first()
    if existing:
        existing.status = status
        existing.verifier_employee_id = verifier_employee_id
        existing.details_json = details or None
        return existing
    record = VerificationRecord(
        work_id=work.id,
        artifact_version_id=version.id,
        verifier_employee_id=verifier_employee_id,
        agent_run_id=agent_run_id,
        method=method,
        status=status,
        details_json=details or None,
    )
    db.session.add(record)
    db.session.flush()
    return record


def _criterion_results(details: dict | None) -> dict[str, dict]:
    raw = dict(details or {}).get("criterion_results") or {}
    if isinstance(raw, dict):
        return {str(key): dict(value or {}) for key, value in raw.items()}
    rows: dict[str, dict] = {}
    if isinstance(raw, list):
        for value in raw:
            if not isinstance(value, dict):
                continue
            key = value.get("criterion_id") or value.get("id")
            if key:
                rows[str(key)] = dict(value)
    return rows


def contract_proof_summary(work: Work, version: ArtifactVersion, contract: dict) -> dict:
    """Resolve authoritative criterion proof for one ArtifactVersion only."""
    records = VerificationRecord.query.filter_by(
        work_id=work.id, artifact_version_id=version.id
    ).order_by(VerificationRecord.id).all()
    summary: dict[str, dict] = {}
    for criterion in contract.get("criteria") or []:
        criterion_id = str(criterion.get("id") or "")
        owner = criterion.get("proof_owner")
        expected_reviewer = criterion.get("verifier_employee_id")
        selected = None
        for record in records:
            if record.method == "ACCEPTANCE_CONTRACT":
                continue
            if owner == "INDEPENDENT_REVIEW":
                if record.method != "INDEPENDENT_REVIEW":
                    continue
                if expected_reviewer is not None and record.verifier_employee_id != expected_reviewer:
                    continue
            else:
                if record.method != "HOST_ENGINEERING_VALIDATION":
                    continue
                if record.verifier_employee_id is not None:
                    continue
            result = _criterion_results(record.details_json).get(criterion_id)
            if result is None:
                continue
            selected = {
                "status": result.get("status") or "UNPROVEN",
                "evidence": result.get("evidence"),
                "source": result.get("source") or record.method,
                "proof_record_id": record.id,
                "verifier_employee_id": record.verifier_employee_id,
            }
        summary[criterion_id] = selected or {
            "status": "UNPROVEN",
            "evidence": "No authoritative proof record exists for this criterion and ArtifactVersion.",
            "source": "MISSING_PROOF",
            "proof_record_id": None,
            "verifier_employee_id": None,
        }
    return summary


def finalize_contract_acceptance(
    work: Work,
    version: ArtifactVersion,
    *,
    contract: dict,
    agent_run_id: int | None = None,
) -> VerificationRecord | None:
    """Accept only when every frozen criterion has authoritative PASS proof.

    The final acceptance record and Artifact/Work state changes are staged in
    the same database transaction.  Callers decide when to commit.
    """
    proof = contract_proof_summary(work, version, contract)
    incomplete = [
        criterion for criterion in (contract.get("criteria") or [])
        if (proof.get(str(criterion.get("id") or "")) or {}).get("status") != "PASSED"
    ]
    if incomplete:
        return None

    existing = VerificationRecord.query.filter_by(
        work_id=work.id,
        artifact_version_id=version.id,
        method="ACCEPTANCE_CONTRACT",
        status="PASSED",
    ).order_by(VerificationRecord.id.desc()).first()
    if existing:
        return existing

    record = VerificationRecord(
        work_id=work.id,
        artifact_version_id=version.id,
        verifier_employee_id=None,
        agent_run_id=agent_run_id,
        method="ACCEPTANCE_CONTRACT",
        status="PASSED",
        details_json={
            "acceptance_contract_version": contract.get("version"),
            "acceptance_contract_hash": contract.get("contract_hash"),
            "criteria_hash": contract.get("criteria_hash"),
            "criterion_results": proof,
            "artifact": {
                "artifact_id": version.artifact_id,
                "artifact_version_id": version.id,
                "version": version.version,
                "content_hash": version.content_hash,
                "content_location": version.content_location,
                "producer_employee_id": version.producer_employee_id,
                "producing_execution_id": version.execution_id,
            },
        },
    )
    db.session.add(record)
    db.session.flush()

    version.status = "ACCEPTED"
    version.accepted_at = now()
    if work.state not in {"VERIFYING", "ACCEPTED"}:
        work_runtime.transition(work, "VERIFYING", reason="Frozen acceptance contract proof is complete")
    if work.state != "ACCEPTED":
        work_runtime.transition(
            work,
            "ACCEPTED",
            actor_type="RUNTIME",
            reason="Every Founder-approved Work criterion has authoritative PASS evidence",
        )
    __import__(
        "eason_one.services.employee_memory", fromlist=["capture_accepted_work_experience"]
    ).capture_accepted_work_experience(work, version, record)
    __import__(
        "eason_one.services.market", fromlist=["settle_accepted_work"]
    ).settle_accepted_work(work, version, record)
    emit(
        "ARTIFACT_ACCEPTED",
        actor_type="RUNTIME",
        project_id=work.project_id,
        work_id=work.id,
        artifact_id=version.artifact_id,
        correlation_id=correlation_for_work(work.id),
        payload={
            "artifact_version_id": version.id,
            "verification_method": "ACCEPTANCE_CONTRACT",
            "acceptance_contract_hash": contract.get("contract_hash"),
            "criterion_proof_record_ids": [
                row.get("proof_record_id") for row in proof.values() if row.get("proof_record_id")
            ],
        },
    )
    return record


# Legacy compatibility helper.  Pre-v0.19 management/history paths still use
# the original one-step verification contract.  New delivery Work must use
# record_verification + finalize_contract_acceptance instead.
def verify_and_accept(work: Work, version: ArtifactVersion, *, method: str, verifier_employee_id: int | None = None, agent_run_id: int | None = None, details: dict | None = None) -> VerificationRecord:
    existing = VerificationRecord.query.filter_by(
        work_id=work.id, artifact_version_id=version.id, method=method, status="PASSED"
    ).order_by(VerificationRecord.id.desc()).first()
    if existing:
        return existing
    record = VerificationRecord(
        work_id=work.id,
        artifact_version_id=version.id,
        verifier_employee_id=verifier_employee_id,
        agent_run_id=agent_run_id,
        method=method,
        status="PASSED",
        details_json=details or None,
    )
    db.session.add(record)
    db.session.flush()
    version.status = "ACCEPTED"
    version.accepted_at = now()
    if work.state not in {"VERIFYING", "ACCEPTED"}:
        work_runtime.transition(work, "VERIFYING", reason=f"Verification started: {method}")
    if work.state != "ACCEPTED":
        work_runtime.transition(work, "ACCEPTED", actor_type="EMPLOYEE" if verifier_employee_id else "RUNTIME", actor_id=verifier_employee_id, reason=f"Artifact verified by {method}")
    __import__(
        "eason_one.services.employee_memory", fromlist=["capture_accepted_work_experience"]
    ).capture_accepted_work_experience(work, version, record)
    __import__(
        "eason_one.services.market", fromlist=["settle_accepted_work"]
    ).settle_accepted_work(work, version, record)
    emit(
        "ARTIFACT_ACCEPTED",
        actor_type="EMPLOYEE" if verifier_employee_id else "RUNTIME",
        actor_id=verifier_employee_id,
        project_id=work.project_id,
        work_id=work.id,
        artifact_id=version.artifact_id,
        correlation_id=correlation_for_work(work.id),
        payload={"artifact_version_id": version.id, "verification_method": method},
    )
    return record


def reject(work: Work, version: ArtifactVersion, *, method: str, verifier_employee_id: int | None = None, agent_run_id: int | None = None, details: dict | None = None) -> VerificationRecord:
    existing = VerificationRecord.query.filter_by(
        work_id=work.id, artifact_version_id=version.id, method=method, status="FAILED"
    ).order_by(VerificationRecord.id.desc()).first()
    if existing:
        return existing
    record = VerificationRecord(
        work_id=work.id,
        artifact_version_id=version.id,
        verifier_employee_id=verifier_employee_id,
        agent_run_id=agent_run_id,
        method=method,
        status="FAILED",
        details_json=details or None,
    )
    db.session.add(record)
    version.status = "REJECTED"
    version.rejected_at = now()
    if work.state != "EXECUTING":
        work_runtime.transition(work, "EXECUTING", actor_type="EMPLOYEE" if verifier_employee_id else "RUNTIME", actor_id=verifier_employee_id, reason="Artifact verification failed; Work remains active")
    emit(
        "ARTIFACT_REJECTED",
        actor_type="EMPLOYEE" if verifier_employee_id else "RUNTIME",
        actor_id=verifier_employee_id,
        project_id=work.project_id,
        work_id=work.id,
        artifact_id=version.artifact_id,
        correlation_id=correlation_for_work(work.id),
        payload={"artifact_version_id": version.id, "verification_method": method},
    )
    return record
