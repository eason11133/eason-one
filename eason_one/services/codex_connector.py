"""Bounded local Codex execution for the Engineering Employee.

Engineer remains the accountable Employee. Codex is a local execution tool,
not an Employee or a silent provider fallback. Every attempt is persisted as an
AgentRun with the exact Job Spec, CLI output, repository, diff/test evidence,
and retry lineage.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import queue
import threading
import tempfile
import time
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flask import current_app

from ..extensions import db
from ..models import AgentRun, ModelConfig, Operation, Work, now

CODEX_PROVIDER_KEY = "codex"
CODEX_MODEL_NAME = "codex-cli"
CODEX_CONFIG_LABEL = "Codex CLI · Local Engineering Tool"
DEFAULT_TIMEOUT_SECONDS = 1800
DEFAULT_RETRY_LIMIT = 1
DEFAULT_WSL_DISTRO = "Ubuntu"
DEFAULT_WSL_CODEX_PATH = "/home/eason/.local/bin/codex"
_READINESS_CACHE: dict[str, Any] = {"at": 0.0, "value": None}

OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "summary", "changed_files", "tests", "acceptance", "risks",
        "needs_founder", "founder_reason",
    ],
    "properties": {
        "summary": {"type": "string"},
        "changed_files": {"type": "array", "items": {"type": "string"}},
        "tests": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["command", "status", "detail"],
                "properties": {
                    "command": {"type": "string"},
                    "status": {"type": "string", "enum": ["PASSED", "FAILED", "NOT_RUN"]},
                    "detail": {"type": "string"},
                },
            },
        },
        "acceptance": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["criterion", "status", "evidence"],
                "properties": {
                    "criterion": {"type": "string"},
                    "status": {"type": "string", "enum": ["PASSED", "FAILED", "UNKNOWN"]},
                    "evidence": {"type": "string"},
                },
            },
        },
        "risks": {"type": "array", "items": {"type": "string"}},
        "needs_founder": {"type": "boolean"},
        "founder_reason": {"type": ["string", "null"]},
    },
}


@dataclass
class CodexResult:
    returncode: int
    stdout: str
    stderr: str
    final_output: str
    elapsed_ms: float
    command: list[str]


def ensure_codex_model_config() -> ModelConfig:
    row = ModelConfig.query.filter_by(
        provider_key=CODEX_PROVIDER_KEY, model_name=CODEX_MODEL_NAME
    ).first()
    if row:
        if row.archived:
            row.archived = False
        row.active = True
        row.input_price_per_million = 0
        row.output_price_per_million = 0
        row.currency = "TWD"
        row.max_output_tokens = max(int(row.max_output_tokens or 1), 1)
        db.session.commit()
        return row
    row = ModelConfig(
        label=CODEX_CONFIG_LABEL,
        provider_key=CODEX_PROVIDER_KEY,
        model_name=CODEX_MODEL_NAME,
        input_price_per_million=0,
        output_price_per_million=0,
        currency="TWD",
        max_output_tokens=1,
        active=True,
        archived=False,
    )
    db.session.add(row)
    db.session.commit()
    return row


def _project_root() -> Path:
    return Path(current_app.root_path).resolve().parent


def _configured_repo(task) -> Path:
    work = db.session.get(Work, getattr(task, "work_id", None)) if getattr(task, "work_id", None) else None
    boundary = dict((getattr(work, "runtime_control_json", None) or {}).get("codex_execution_boundary") or {}) if work else {}
    if boundary.get("repo_path"):
        return Path(str(boundary["repo_path"])).expanduser().resolve()
    operation = getattr(task, "operation", None)
    memory = dict(getattr(operation, "memory_json", None) or {})
    engineering = dict(memory.get("engineering") or {})
    configured = engineering.get("repo_path")
    if not configured and getattr(task, "project", None):
        project = task.project
        contracts = __import__(
            "eason_one.services.project_contract", fromlist=["is_vnext_governed", "governing_terms"]
        )
        if contracts.is_vnext_governed(project):
            constraints = "\n".join(contracts.governing_terms(project).get("constraints") or [])
        else:
            constraints = project.known_constraints or ""
        for line in constraints.splitlines():
            if line.strip().upper().startswith("REPO_PATH="):
                configured = line.split("=", 1)[1].strip()
                break
    configured = configured or os.getenv("EASON_ONE_CODEX_DEFAULT_REPO")
    return Path(configured).expanduser().resolve() if configured else _project_root()


_DEFAULT_ALLOWED_PATHS = ["Repository files required for this bounded Work"]
_DEFAULT_FORBIDDEN_PATHS = [
    ".env and credential files",
    "instance/*.db and production data",
    ".git internals",
    "deployment/infrastructure outside the approved Work",
]

_WRITE_SCOPE_VERSION = "CODEX_WRITE_SCOPE_V1"
_FORBIDDEN_WRITE_ROOTS = {".git", "instance", ".eason-one-runtime", ".eason-one-backups"}


def normalize_write_scope(scope: dict) -> dict:
    """Validate explicit structured write authority without reading prose."""
    if not isinstance(scope, dict) or set(scope) != {"version", "paths"}:
        raise ValueError("CODEX_WRITE_SCOPE_INVALID")
    if scope.get("version") != _WRITE_SCOPE_VERSION:
        raise ValueError("CODEX_WRITE_SCOPE_VERSION_INVALID")
    raw_paths = scope.get("paths")
    if not isinstance(raw_paths, list) or not 1 <= len(raw_paths) <= 20:
        raise ValueError("CODEX_WRITE_SCOPE_PATHS_INVALID")
    paths: list[str] = []
    for raw in raw_paths:
        if not isinstance(raw, str) or not raw.strip() or "\\" in raw:
            raise ValueError("CODEX_WRITE_SCOPE_PATH_INVALID")
        value = raw.strip()
        path = Path(value)
        parts = value.split("/")
        if (
            path.is_absolute() or value.startswith(("/", "./")) or ":" in value
            or any(part in {"", ".", ".."} for part in parts)
            or parts[0].casefold() in _FORBIDDEN_WRITE_ROOTS
            or parts[0].casefold().startswith(".env")
            or value.endswith("/")
        ):
            raise ValueError("CODEX_WRITE_SCOPE_PATH_INVALID")
        if value not in paths:
            paths.append(value)
    return {"version": _WRITE_SCOPE_VERSION, "paths": paths}


def freeze_write_scope(scope: dict, *, source: str, authority_ref: str) -> dict:
    normalized = normalize_write_scope(scope)
    body = {
        **normalized,
        "source": str(source),
        "authority_ref": str(authority_ref),
    }
    body["scope_hash"] = hashlib.sha256(
        json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return body


def _contract_allowed_paths(work: Work, task) -> list[str]:
    """Read only hash-bound structured Work authority; prose is never parsed."""
    frozen = dict((work.runtime_control_json or {}).get("codex_write_scope") or {})
    if not frozen:
        return []
    body = {key: value for key, value in frozen.items() if key != "scope_hash"}
    expected = hashlib.sha256(
        json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if frozen.get("scope_hash") != expected:
        raise ValueError("CODEX_WRITE_SCOPE_HASH_MISMATCH")
    normalized = normalize_write_scope({
        "version": frozen.get("version"), "paths": frozen.get("paths")
    })
    if not frozen.get("source") or not frozen.get("authority_ref"):
        raise ValueError("CODEX_WRITE_SCOPE_AUTHORITY_MISSING")
    return normalized["paths"]


def _allowed_delta_paths(boundary: dict, changed: list[str]) -> list[str]:
    allowed = {str(row).replace("\\", "/").strip("./") for row in (boundary.get("allowed_paths") or [])}
    return [row for row in changed if row.replace("\\", "/").strip("./") not in allowed]


def _governed_repo(task) -> Path:
    """Resolve vNext repository authority without mutable Operation memory."""
    project = getattr(task, "project", None)
    if project:
        contracts = __import__(
            "eason_one.services.project_contract", fromlist=["is_vnext_governed", "governing_terms"]
        )
        if contracts.is_vnext_governed(project):
            constraints = contracts.governing_terms(project).get("constraints") or []
            for value in constraints:
                line = str(value or "").strip()
                if line.upper().startswith("REPO_PATH="):
                    configured = line.split("=", 1)[1].strip()
                    if configured:
                        return Path(configured).expanduser().resolve()
            configured = os.getenv("EASON_ONE_CODEX_DEFAULT_REPO")
            return Path(configured).expanduser().resolve() if configured else _project_root()
    return _configured_repo(task)


def ensure_execution_boundary(work: Work, task) -> dict:
    """Freeze Codex repository/tool scope on authoritative Work before execution.

    Retries and restarts consume this immutable boundary instead of re-reading
    mutable Operation.memory_json. A Project terms amendment is handled by the
    Work acceptance contract; the boundary also records that execution-terms
    hash so audit can prove which Project terms authorized this tool scope.
    """
    control = dict(work.runtime_control_json or {})
    existing = dict(control.get("codex_execution_boundary") or {})
    exact_paths = _contract_allowed_paths(work, task)
    if existing:
        body = {key: value for key, value in existing.items() if key != "boundary_hash"}
        expected = hashlib.sha256(
            json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        if existing.get("boundary_hash") != expected:
            raise ValueError("CODEX_EXECUTION_BOUNDARY_HASH_MISMATCH")
        if existing.get("version") != "CODEX_EXECUTION_BOUNDARY_V2":
            if AgentRun.query.filter_by(work_id=work.id, provider_key_snapshot="codex").count():
                raise ValueError("LEGACY_CODEX_BOUNDARY_REQUIRES_RECONCILIATION")
            if not exact_paths and not bool(_is_read_only(task)):
                raise ValueError("CODEX_STRUCTURED_WRITE_SCOPE_MISSING")
            existing["version"] = "CODEX_EXECUTION_BOUNDARY_V2"
            existing["allowed_paths"] = exact_paths
            existing["max_changed_files"] = len(exact_paths)
            body = {key: value for key, value in existing.items() if key != "boundary_hash"}
            existing["boundary_hash"] = hashlib.sha256(
                json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            control["codex_execution_boundary"] = existing
            work.runtime_control_json = control
        elif existing.get("allowed_paths") != exact_paths:
            raise ValueError("CODEX_EXECUTION_BOUNDARY_SCOPE_MISMATCH")
        return existing

    project = work.project
    terms_hash = None
    if project:
        contracts = __import__(
            "eason_one.services.project_contract", fromlist=["is_vnext_governed", "execution_terms_hash"]
        )
        if contracts.is_vnext_governed(project):
            terms_hash = contracts.execution_terms_hash(project)
    body = {
        "version": "CODEX_EXECUTION_BOUNDARY_V2",
        "project_id": work.project_id,
        "work_id": work.id,
        "repo_path": str(_governed_repo(task)),
        "allowed_paths": exact_paths,
        "forbidden_paths": list(_DEFAULT_FORBIDDEN_PATHS),
        "max_changed_files": len(exact_paths),
        "read_only": bool(_is_read_only(task)),
        "project_execution_terms_hash": terms_hash,
    }
    body["boundary_hash"] = hashlib.sha256(
        json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    control["codex_execution_boundary"] = body
    work.runtime_control_json = control
    if not body["read_only"] and not exact_paths:
        raise ValueError("CODEX_STRUCTURED_WRITE_SCOPE_MISSING")
    return body


def _allowed_repos() -> list[Path]:
    roots = [_project_root()]
    raw = os.getenv("EASON_ONE_CODEX_ALLOWED_REPOS", "")
    for value in raw.split(os.pathsep):
        if value.strip():
            roots.append(Path(value.strip()).expanduser().resolve())
    return roots


def preapproval_repository_readiness(*, constraints=None) -> dict[str, Any]:
    """Resolve the exact repository boundary without needing a Task/Work row.

    New Project approval happens before Work materialization, so Engineering
    readiness cannot rely on `_configured_repo(task)`. Use the same governing
    precedence as execution: Contract `REPO_PATH=` -> configured default ->
    Eason One project root, then verify existence, allowlist and Git identity.
    This is a local deterministic check only; it never invokes Codex.
    """
    configured = None
    for value in (constraints or []):
        line = str(value or "").strip()
        if line.upper().startswith("REPO_PATH="):
            configured = line.split("=", 1)[1].strip() or None
            if configured:
                break
    configured = configured or os.getenv("EASON_ONE_CODEX_DEFAULT_REPO")
    repo = Path(configured).expanduser().resolve() if configured else _project_root()
    allowed_roots = _allowed_repos()
    exists = repo.exists() and repo.is_dir()
    allowlisted = exists and any(_is_within(repo, root) for root in allowed_roots)
    git_working_tree = exists and (repo / ".git").exists()
    error = None
    if not exists:
        error = f"Codex repository does not exist: {repo}"
    elif not allowlisted:
        error = (
            f"Codex repository is outside the allowlist: {repo}. "
            "Configure EASON_ONE_CODEX_ALLOWED_REPOS."
        )
    elif not git_working_tree:
        error = f"Codex repository must be a Git working tree: {repo}"
    return {
        "ready": bool(exists and allowlisted and git_working_tree),
        "path": str(repo),
        "exists": bool(exists),
        "allowlisted": bool(allowlisted),
        "git_working_tree": bool(git_working_tree),
        "allowed_roots": [str(root) for root in allowed_roots],
        "error": error,
    }


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _codex_transport() -> str:
    configured = (os.getenv("EASON_ONE_CODEX_TRANSPORT") or "").strip().casefold()
    if configured in {"wsl", "wsl2"}:
        return "wsl"
    if configured in {"native", "windows"}:
        return "native"
    # The native Windows sandbox is not reliable on every Codex standalone
    # installation. Prefer the Linux sandbox when WSL is available.
    if os.name == "nt" and (shutil.which("wsl.exe") or shutil.which("wsl")):
        return "wsl"
    return "native"


def _resolve_native_codex_command() -> str:
    configured = os.getenv("CODEX_PATH")
    command = configured or shutil.which("codex") or shutil.which("codex.exe")
    if not command:
        raise ValueError(
            "Native Codex CLI is unavailable. Install/sign in or configure CODEX_PATH."
        )
    return command


def _resolve_wsl_executable() -> str:
    command = os.getenv("EASON_ONE_WSL_PATH") or shutil.which("wsl.exe") or shutil.which("wsl")
    if not command:
        raise ValueError("WSL executable is unavailable. Install/repair WSL2 before resuming Engineering.")
    return command


def _wsl_distro() -> str:
    return (os.getenv("EASON_ONE_CODEX_WSL_DISTRO") or DEFAULT_WSL_DISTRO).strip()


def _wsl_codex_path() -> str:
    return (os.getenv("EASON_ONE_CODEX_WSL_PATH") or DEFAULT_WSL_CODEX_PATH).strip()


def _to_wsl_path(path: Path) -> str:
    raw = str(path)
    match = re.match(r"^([A-Za-z]):[\\/](.*)$", raw)
    if not match:
        raw = str(path.resolve())
        match = re.match(r"^([A-Za-z]):[\\/](.*)$", raw)
    if match:
        drive = match.group(1).lower()
        rest = re.sub(r"/+", "/", match.group(2).replace("\\", "/"))
        return f"/mnt/{drive}/{rest.lstrip('/')}"
    return raw.replace("\\", "/")

def _readiness_snapshot_path() -> Path:
    configured = (os.getenv("EASON_ONE_CODEX_READINESS_FILE") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(__file__).resolve().parents[2] / "instance" / "codex_readiness.json"


def _readiness_cache_key(transport: str) -> str:
    if transport == "wsl":
        return "|".join([
            "wsl",
            _wsl_distro(),
            _wsl_codex_path(),
        ])
    return "|".join([
        "native",
        os.getenv("CODEX_PATH", ""),
    ])


def _load_persisted_readiness(cache_key: str) -> dict[str, Any] | None:
    path = _readiness_snapshot_path()
    try:
        if not path.exists():
            return None
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        if payload.get("cache_key") not in {None, "", cache_key}:
            return None
        payload["cache_key"] = cache_key
        payload["source"] = "persisted"
        checked_at = payload.get("checked_at")
        if checked_at:
            try:
                checked = datetime.fromisoformat(str(checked_at).replace("Z", "+00:00"))
                if checked.tzinfo is None:
                    checked = checked.replace(tzinfo=timezone.utc)
                payload["stale"] = (datetime.now(timezone.utc) - checked).total_seconds() > 86400
            except (TypeError, ValueError):
                payload["stale"] = True
        else:
            payload["stale"] = True
        return payload
    except (OSError, ValueError, TypeError):
        return None


def _persist_readiness(descriptor: dict[str, Any]) -> None:
    path = _readiness_snapshot_path()
    payload = dict(descriptor)
    payload["checked_at"] = datetime.now(timezone.utc).isoformat()
    payload["source"] = "probe"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
    except OSError:
        return


def codex_runtime_descriptor(*, force_probe: bool = False) -> dict[str, Any]:
    """Return Codex readiness without blocking Founder page renders.

    Normal UI projections use an in-memory or installer-persisted readiness
    snapshot. Only an actual Engineering execution (``force_probe=True``) may
    launch WSL/Codex subprocesses.

    An explicitly configured readiness file takes precedence over Flask's
    TESTING shortcut. This lets installer verification exercise the exact
    persisted-snapshot path while still guaranteeing that no subprocess runs.
    """
    transport = _codex_transport()
    now_monotonic = time.monotonic()
    cache_key = _readiness_cache_key(transport)

    # Installer/regression verification supplies an explicit snapshot path.
    # Read it before the TESTING shortcut so the tested path matches the one
    # Founder pages use after installation. This branch never probes WSL.
    explicit_snapshot = bool((os.getenv("EASON_ONE_CODEX_READINESS_FILE") or "").strip())
    if not force_probe and explicit_snapshot:
        cached = _READINESS_CACHE.get("value")
        if (
            cached
            and cached.get("cache_key") == cache_key
            and now_monotonic - float(_READINESS_CACHE.get("at") or 0) < 300
        ):
            return dict(cached)
        persisted = _load_persisted_readiness(cache_key)
        if persisted:
            _READINESS_CACHE["at"] = now_monotonic
            _READINESS_CACHE["value"] = dict(persisted)
            return dict(persisted)

    try:
        testing_without_real_probe = bool(
            current_app.config.get("TESTING")
            and not current_app.config.get("CODEX_REAL_READINESS_PROBE")
        )
    except RuntimeError:
        testing_without_real_probe = False
    if testing_without_real_probe:
        distro = _wsl_distro()
        codex = _wsl_codex_path()
        return {
            "transport": transport,
            "ready": True,
            "version": "TESTING / no real CLI probe",
            "login": "TESTING / simulated signed-in local tool",
            "error": None,
            "host_executable": "wsl.exe" if transport == "wsl" else None,
            "distro": distro if transport == "wsl" else None,
            "path": codex if transport == "wsl" else (os.getenv("CODEX_PATH") or "codex"),
            "display_path": (
                f"wsl.exe -d {distro} -- {codex}" if transport == "wsl" else
                (os.getenv("CODEX_PATH") or "codex")
            ),
            "cache_key": "TESTING",
            "source": "testing",
            "stale": False,
        }

    cached = _READINESS_CACHE.get("value")
    if (
        not force_probe
        and cached
        and cached.get("cache_key") == cache_key
        and now_monotonic - float(_READINESS_CACHE.get("at") or 0) < 300
    ):
        return dict(cached)

    if not force_probe:
        persisted = _load_persisted_readiness(cache_key)
        if persisted:
            _READINESS_CACHE["at"] = now_monotonic
            _READINESS_CACHE["value"] = dict(persisted)
            return dict(persisted)
        distro = _wsl_distro()
        codex = _wsl_codex_path()
        descriptor = {
            "transport": transport,
            "ready": False,
            "version": None,
            "login": None,
            "error": "Codex readiness has not been verified yet. Start an Engineering task or refresh readiness in System.",
            "host_executable": "wsl.exe" if transport == "wsl" else None,
            "distro": distro if transport == "wsl" else None,
            "path": codex if transport == "wsl" else (os.getenv("CODEX_PATH") or "codex"),
            "display_path": (
                f"wsl.exe -d {distro} -- {codex}" if transport == "wsl" else
                (os.getenv("CODEX_PATH") or "codex")
            ),
            "cache_key": cache_key,
            "source": "unverified",
            "stale": True,
        }
        _READINESS_CACHE["at"] = now_monotonic
        _READINESS_CACHE["value"] = dict(descriptor)
        return descriptor

    descriptor: dict[str, Any] = {
        "transport": transport,
        "ready": False,
        "version": None,
        "login": None,
        "error": None,
        "source": "probe",
        "stale": False,
    }
    try:
        if transport == "wsl":
            wsl = _resolve_wsl_executable()
            distro = _wsl_distro()
            codex = _wsl_codex_path()
            descriptor.update({
                "host_executable": wsl,
                "distro": distro,
                "path": codex,
                "display_path": f"{wsl} -d {distro} -- {codex}",
            })
            # WSL can take longer than 12 seconds to cold-start even when Codex is
            # installed and signed in. Readiness is an infrastructure probe, not a
            # paid model attempt, so warm the distro and retry once before blocking
            # Engineering or escalating to the Founder.
            readiness_timeout = max(15, min(120, int(
                os.getenv("EASON_ONE_CODEX_READINESS_TIMEOUT_SECONDS", "30")
            )))
            last_probe_error = None
            version_run = login_run = None
            for probe_attempt in range(2):
                try:
                    warmup = subprocess.run(
                        [wsl, "-d", distro, "--", "true"],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=readiness_timeout,
                        check=False,
                    )
                    if warmup.returncode != 0:
                        raise ValueError(warmup.stderr.strip() or "WSL warm-up failed")
                    version_run = subprocess.run(
                        [wsl, "-d", distro, "--", codex, "--version"],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=readiness_timeout,
                        check=False,
                    )
                    if version_run.returncode != 0:
                        raise ValueError(version_run.stderr.strip() or "WSL Codex version probe failed")
                    login_run = subprocess.run(
                        [wsl, "-d", distro, "--", codex, "login", "status"],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=readiness_timeout,
                        check=False,
                    )
                    break
                except (subprocess.TimeoutExpired, ValueError) as exc:
                    last_probe_error = exc
                    if probe_attempt == 0:
                        time.sleep(1.0)
                        continue
                    raise
            if version_run is None or login_run is None:
                raise ValueError(str(last_probe_error or "WSL Codex readiness probe failed"))
            login_text = (login_run.stdout + "\n" + login_run.stderr).strip()
            descriptor["version"] = version_run.stdout.strip()
            descriptor["login"] = login_text
            # `codex login status` is the CLI authority for authentication.
            # Current Codex supports multiple valid auth modes (ChatGPT, API key,
            # Agent Identity, headers); do not reject a successful CLI status just
            # because its human-readable text is not the ChatGPT-specific phrase.
            descriptor["ready"] = version_run.returncode == 0 and login_run.returncode == 0
            if not descriptor["ready"]:
                descriptor["error"] = login_text or "WSL Codex is not signed in"
        else:
            codex = _resolve_native_codex_command()
            descriptor.update({"path": codex, "display_path": codex})
            version_run = subprocess.run(
                [codex, "--version"], stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                encoding="utf-8", errors="replace", timeout=12, check=False,
            )
            login_run = subprocess.run(
                [codex, "login", "status"], stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                encoding="utf-8", errors="replace", timeout=12, check=False,
            )
            login_text = (login_run.stdout + "\n" + login_run.stderr).strip()
            descriptor["version"] = version_run.stdout.strip()
            descriptor["login"] = login_text
            descriptor["ready"] = (
                version_run.returncode == 0
                and login_run.returncode == 0
            )
            if not descriptor["ready"]:
                descriptor["error"] = login_text or version_run.stderr.strip() or "Native Codex probe failed"
    except Exception as exc:
        descriptor["error"] = str(exc)

    descriptor["cache_key"] = cache_key
    descriptor["checked_at"] = datetime.now(timezone.utc).isoformat()
    _READINESS_CACHE["at"] = now_monotonic
    _READINESS_CACHE["value"] = dict(descriptor)
    _persist_readiness(descriptor)
    return descriptor

_EXPLICIT_READ_ONLY_MARKERS = (
    "read-only task", "read only task", "read-only analysis", "read only analysis",
    "do not modify files", "do not modify the repository", "without modifying files",
    "without modifying the repository", "no file changes", "analysis only",
    "只讀分析", "唯讀分析", "唯讀檢查", "唯讀任務", "不修改任何檔案",
    "不要修改檔案", "不得修改檔案", "不得修改 repository", "不要修改 repository",
    "不修改 repository", "不修改程式碼", "不要修改程式碼", "不得修改程式碼",
)


def explicit_read_only_text(*values) -> bool:
    """Recognize only an explicit non-writing engineering contract.

    This helper may remove write authority; it never grants it.  Proposal
    validation and Codex execution consume the same detector so a Task cannot
    be approved as read-only and later become writable because two layers parsed
    its prose differently.
    """
    text = " ".join(str(value or "") for value in values).casefold()
    return any(marker in text for marker in _EXPLICIT_READ_ONLY_MARKERS)


def plan_task_is_read_only(item: dict) -> bool:
    if not isinstance(item, dict):
        return False
    return explicit_read_only_text(
        item.get("title"),
        item.get("objective"),
        " ".join(item.get("acceptance_criteria") or []),
    )


def _is_read_only(task) -> bool:
    """Return True only from durable Work authority for vNext engineering jobs.

    Task/Operation text is a compatibility projection and may not widen a frozen
    Work from READ ONLY to repository write. Historical Tasks retain the legacy
    text fallback. Product requirements such as "read-only GET endpoint" still
    do not count unless the Work explicitly says the engineering task itself is
    non-writing.
    """
    work = db.session.get(Work, getattr(task, "work_id", None)) if getattr(task, "work_id", None) else None
    if work is not None:
        return explicit_read_only_text(
            work.title, work.purpose, work.acceptance_criteria,
        )
    operation = getattr(task, "operation", None)
    return explicit_read_only_text(
        task.title, task.objective, task.acceptance_criteria,
        getattr(operation, "objective", ""),
    )


def _criteria(task) -> list[str]:
    rows = [line.strip(" -\t") for line in (task.acceptance_criteria or "").splitlines()]
    return [row for row in rows if row]


def _persistent_engineer_learning(task) -> tuple[str, dict]:
    """Return canonical Engineer precedent for the exact governed Codex Task.

    Codex remains a tool. The memory belongs to the accountable Persistent
    Engineer and may guide implementation method only; it never widens the
    frozen repository/Founder/budget boundary or proves the current result.
    """
    employee = getattr(task, "assigned_employee", None)
    if employee is None:
        return "", {}
    work = __import__(
        "eason_one.services.work_runtime", fromlist=["work_for_task"]
    ).work_for_task(task)
    capability = None
    if work is not None:
        capability = __import__(
            "eason_one.services.team_formation", fromlist=["infer_primary_capability"]
        ).infer_primary_capability(work)[0]
    return __import__(
        "eason_one.services.employee_memory", fromlist=["execution_learning_context"]
    ).execution_learning_context(
        employee, getattr(task, "project", None), task,
        purpose="TASK_EXECUTION", capability=capability,
    )


def build_job_spec(task, *, repair_context: str | None = None, execution_repo: Path | None = None) -> str:
    operation = getattr(task, "operation", None)
    work = db.session.get(Work, getattr(task, "work_id", None)) if getattr(task, "work_id", None) else None
    boundary = ensure_execution_boundary(work, task) if work is not None else None
    repo = execution_repo.resolve() if execution_repo is not None else (
        Path(boundary["repo_path"]).expanduser().resolve() if boundary else _configured_repo(task)
    )
    founder_receipt = None
    if getattr(task, "project", None) and getattr(task, "work_id", None):
        work = db.session.get(Work, task.work_id)
        if work:
            founder_receipt = __import__(
                "eason_one.services.governance", fromlist=["latest_execution_receipt"]
            ).latest_execution_receipt(
                project=task.project, work=work, authority_type="CODEX_RISK_APPROVAL"
            )
    read_only = bool(boundary.get("read_only")) if boundary else _is_read_only(task)
    if work is not None:
        task_title = work.title or task.title
        task_objective = work.purpose or task.objective
        criteria = [row.strip(" -\t") for row in (work.acceptance_criteria or "").splitlines() if row.strip(" -\t")]
    else:
        task_title = task.title
        task_objective = task.objective
        criteria = _criteria(task)
    company_context = ""
    if getattr(task, "assigned_employee", None) is not None:
        try:
            company_context = __import__(
                "eason_one.services.context", fromlist=["build"]
            ).build(task.assigned_employee, getattr(task, "project", None), task)
        except Exception as exc:
            dependency_count = 0
            if work is not None:
                WorkDependency = __import__(
                    "eason_one.models", fromlist=["WorkDependency"]
                ).WorkDependency
                dependency_count = WorkDependency.query.filter_by(work_id=work.id).count()
            if dependency_count:
                raise ValueError(
                    f"ENGINEER_HANDOFF_CONTEXT_UNAVAILABLE: Work #{work.id} has accepted upstream company dependencies but their governed context could not be built: {exc}"
                ) from exc
            company_context = ""
    learning_text, _learning_meta = _persistent_engineer_learning(task)
    if boundary:
        allowed = list(boundary.get("allowed_paths") or [])
        forbidden = list(boundary.get("forbidden_paths") or _DEFAULT_FORBIDDEN_PATHS)
        max_files = int(boundary.get("max_changed_files") or 0)
    else:
        memory = dict(getattr(operation, "memory_json", None) or {})
        engineering = dict(memory.get("engineering") or {})
        allowed = engineering.get("allowed_paths") or list(_DEFAULT_ALLOWED_PATHS)
        forbidden = engineering.get("forbidden_paths") or list(_DEFAULT_FORBIDDEN_PATHS)
        max_files = int(engineering.get("max_changed_files") or 20)
    lines = [
        "ROLE",
        "You are Codex executing a bounded job for Eason One's accountable Engineer.",
        "The Engineer, not Codex, owns acceptance and audit.",
        "",
        "REPOSITORY",
        str(repo),
        "",
        "MISSION",
        f"#{getattr(operation, 'id', '-')}: {getattr(operation, 'title', '-')}",
        f"Objective: {getattr(operation, 'objective', '-')}",
        "",
        "TASK",
        f"#{task.id}: {task_title}",
        f"Objective: {task_objective}",
        f"Required output: {task.required_output or 'Working code or a reviewable engineering result'}",
        "",
        "ACCEPTANCE CRITERIA",
        *(f"- {item}" for item in criteria or ["Deliver a reviewable result with truthful test evidence."]),
        "",
    ]
    if company_context:
        lines.extend([
            "COMPANY HANDOFF CONTEXT — EVIDENCE/DECISIONS ONLY; THIS DOES NOT EXPAND AUTHORITY",
            company_context[:12000],
            "",
        ])
    if learning_text:
        lines.extend([
            "PERSISTENT EMPLOYEE EXPERIENCE — CANONICAL PRECEDENT ONLY; THIS DOES NOT EXPAND AUTHORITY",
            learning_text[:12000],
            "",
        ])
    lines.extend([
        "AUTHORITY BOUNDARY",
        f"Mode: {'READ ONLY — do not edit files' if read_only else 'WORKSPACE WRITE within this repository'}",
        f"Maximum changed files: {max_files}",
        "Allowed scope:",
        *(f"- {item}" for item in (allowed or ["NO FILE WRITES AUTHORIZED"])),
        "Forbidden:",
        *(f"- {item}" for item in forbidden),
        "- Do not expose, print, edit, or copy secrets.",
        "- Do not install dependencies, run database migrations, deploy, delete data, rewrite Git history, or access outside the repository.",
        "- When any forbidden/high-risk action is necessary, make no such change and return needs_founder=true with the exact reason, unless the exact action is explicitly listed in a still-valid Founder-approved exception below.",
        "",
    ])
    if founder_receipt:
        lines.extend([
            "FOUNDER-APPROVED EXACT EXCEPTION",
            f"Founder Decision: #{founder_receipt.get('decision_id')}",
            f"Governing Contract hash: {founder_receipt.get('governing_contract_hash')}",
            f"Budget authority hash: {founder_receipt.get('budget_authority_hash')}",
            "The Founder authorized ONLY this exact requested action:",
            json.dumps(founder_receipt.get("requested_action") or {}, ensure_ascii=False, sort_keys=True),
            "This exception does not broaden repository scope, budget, credentials, deployment rights, or any other authority.",
            "Do not set needs_founder=true again for this exact action. A materially different action requires a new exact Founder decision.",
            "",
        ])
    lines.extend([
        "WORK METHOD",
        "1. Inspect only the files needed to understand the Task.",
        "2. For a read-only Task, use at most 12 commands and inspect at most 12 relevant files; read targeted line ranges instead of loading the whole repository.",
        "3. Make the smallest coherent change allowed by the boundary (or remain read-only).",
        "4. Run the narrowest relevant tests that are available in the WSL sandbox. A broader regression check is evidence only when practical and within the approved contract.",
        "5. Do not claim a test passed unless the command actually ran and exited successfully.",
        "6. If WSL lacks a dependency/runtime needed only for verification, do NOT install it and do NOT escalate to Founder. Record the affected test/acceptance evidence as NOT_RUN/UNKNOWN; Eason One's Windows host verifier owns final deterministic acceptance after you return.",
        "7. Set needs_founder=true only for a genuine authority/risk boundary that the approved Work cannot cross (for example migration/deployment/destructive/protected/secret/out-of-repo action), not because your WSL sandbox cannot run verification.",
        "8. Stop and report uncertainty instead of expanding scope or ingesting the entire repository.",
        "9. Return the final structured result required by the supplied output schema.",
    ])
    if repair_context:
        lines.extend([
            "", "ENGINEER RETRY NOTE",
            "The previous bounded Codex attempt did not meet acceptance. Correct only the documented failure without expanding scope:",
            repair_context[:5000],
        ])
    return "\n".join(lines)


def _protected_files(repo: Path) -> list[Path]:
    # The live SQLite database is intentionally not byte-snapshotted here.
    # Eason One itself persists AgentRun heartbeats while Codex is running, so
    # comparing/restoring the database would misclassify normal runtime writes
    # as a Codex boundary violation and could roll back authoritative history.
    candidates = [repo / ".env"]
    return [path for path in candidates if path.exists() and path.is_file()]


def _snapshot_protected(repo: Path) -> dict[Path, bytes]:
    return {path: path.read_bytes() for path in _protected_files(repo)}


def _restore_protected(snapshot: dict[Path, bytes]) -> list[str]:
    restored = []
    for path, content in snapshot.items():
        current = path.read_bytes() if path.exists() else None
        if current != content:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            restored.append(str(path))
    return restored



_REPO_SNAPSHOT_EXCLUDED_PARTS = {
    ".git", ".venv", ".python313", ".eason-one-backups",
    ".eason-one-test-temp", ".eason-one-runtime", "instance",
    "__pycache__", ".pytest_cache", "node_modules",
}


def _snapshot_candidate(path: Path, repo: Path) -> bool:
    try:
        relative = path.relative_to(repo)
    except ValueError:
        return False
    if any(part in _REPO_SNAPSHOT_EXCLUDED_PARTS or part.startswith(".test-") for part in relative.parts):
        return False
    if (
        relative.name.casefold().startswith(".env")
        or relative.suffix.casefold() in {".pyc", ".pyo", ".db", ".sqlite", ".sqlite3"}
        or relative.name.casefold().endswith((".db-wal", ".db-shm", ".sqlite-wal", ".sqlite-shm"))
    ):
        return False
    return path.is_file() and not path.is_symlink()


def _snapshot_repository(repo: Path) -> dict[str, bytes]:
    """Capture the governed live source surface, excluding protected/runtime state."""
    snapshot: dict[str, bytes] = {}
    for path in repo.rglob("*"):
        if not _snapshot_candidate(path, repo):
            continue
        try:
            snapshot[path.relative_to(repo).as_posix()] = path.read_bytes()
        except (OSError, PermissionError):
            continue
    return snapshot


def _snapshot_isolated_workspace(repo: Path) -> dict[str, bytes]:
    """Capture every file-like path Codex created or changed in its disposable tree.

    Unlike the live-source snapshot this intentionally does *not* exclude .git,
    instance, .env, database files, caches, or other protected names.  The live
    workspace is built without those surfaces, so any such path appearing after
    execution is a Codex-created delta and must participate in exact-scope
    validation.  Symlinks are recorded without following their targets.
    """
    snapshot: dict[str, bytes] = {}
    for path in repo.rglob("*"):
        try:
            relative = path.relative_to(repo).as_posix()
        except ValueError:
            continue
        try:
            if path.is_symlink():
                snapshot[relative] = b"__EASON_SYMLINK__\0" + os.readlink(path).encode("utf-8", "surrogateescape")
            elif path.is_file():
                snapshot[relative] = path.read_bytes()
        except (OSError, PermissionError, UnicodeError):
            # An unreadable file is still a changed path.  Preserve a sentinel so
            # scope validation fails closed instead of silently ignoring it.
            snapshot[relative] = b"__EASON_UNREADABLE__"
    return snapshot


def _isolated_workspace_delta(repo: Path, before: dict[str, bytes]) -> list[str]:
    after = _snapshot_isolated_workspace(repo)
    return sorted({
        relative for relative in set(before) | set(after)
        if before.get(relative) != after.get(relative)
    })


def _create_isolated_workspace(before: dict[str, bytes]) -> tuple[Path, Path]:
    """Materialize source truth into a disposable tree with no protected state."""
    root = Path(tempfile.mkdtemp(prefix="eason-one-codex-isolated-"))
    workspace = root / "workspace"
    workspace.mkdir()
    for relative, content in sorted(before.items()):
        candidate = Path(relative)
        if candidate.is_absolute() or ".." in candidate.parts:
            shutil.rmtree(root, ignore_errors=True)
            raise ValueError("CODEX_ISOLATION_SNAPSHOT_PATH_INVALID")
        target = workspace / candidate
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    return root, workspace


def _copy_back_approved_paths(
    source_repo: Path,
    isolated_repo: Path,
    source_before: dict[str, bytes],
    changed: list[str],
    boundary: dict,
) -> dict[str, Any]:
    """Copy a completely validated isolated delta back as one bounded unit."""
    outside = _allowed_delta_paths(boundary, changed)
    if outside:
        return {"applied": False, "reason": "EXTRA_CHANGED_PATH", "outside": outside, "copied": []}
    approved = sorted(set(changed))
    current = _snapshot_repository(source_repo)
    conflicts = [path for path in approved if current.get(path) != source_before.get(path)]
    if conflicts:
        return {"applied": False, "reason": "CONCURRENT_SOURCE_CHANGE", "conflicts": conflicts, "copied": []}
    backups = {path: current.get(path) for path in approved}
    copied: list[str] = []
    try:
        for relative in approved:
            source_path = source_repo / Path(relative)
            isolated_path = isolated_repo / Path(relative)
            if isolated_path.is_symlink():
                raise ValueError(f"CODEX_COPYBACK_SYMLINK_FORBIDDEN:{relative}")
            if isolated_path.exists() and isolated_path.is_file():
                source_path.parent.mkdir(parents=True, exist_ok=True)
                temp_path = source_path.with_name(f".{source_path.name}.codex-{uuid.uuid4().hex}.tmp")
                temp_path.write_bytes(isolated_path.read_bytes())
                os.replace(temp_path, source_path)
            elif source_path.exists():
                source_path.unlink()
            copied.append(relative)
    except Exception:
        for relative, content in backups.items():
            source_path = source_repo / Path(relative)
            if content is None:
                if source_path.exists():
                    source_path.unlink()
            else:
                source_path.parent.mkdir(parents=True, exist_ok=True)
                source_path.write_bytes(content)
        raise
    return {"applied": True, "reason": None, "outside": [], "copied": copied}


def _repository_delta(repo: Path, before: dict[str, bytes]) -> list[str]:
    """Return the source delta introduced after ``before`` was captured.

    This is independent of Git status, so pre-existing dirty or untracked files
    cannot hide what the current bounded Codex job actually changed.
    """
    after = _snapshot_repository(repo)
    return sorted({
        relative for relative in set(before) | set(after)
        if before.get(relative) != after.get(relative)
    })


def _restore_repository(repo: Path, before: dict[str, bytes]) -> list[str]:
    """Restore only the delta introduced by this read-only job."""
    changed = _repository_delta(repo, before)
    for relative in changed:
        path = repo / Path(relative)
        if relative not in before:
            try:
                if path.exists() or path.is_symlink():
                    path.unlink()
            except OSError:
                pass
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(before[relative])
    return changed


def _restore_repository_verified(repo: Path, before: dict[str, bytes]) -> tuple[list[str], bool, list[str]]:
    """Restore the bounded source delta and prove the governed surface matches.

    Retry authority must never infer restoration merely because a rollback was
    attempted.  Residual source delta keeps LOCAL_REVERSIBLE work non-replayable.
    """
    changed = _restore_repository(repo, before)
    remaining = _repository_delta(repo, before)
    return changed, not remaining, remaining


def _hash_relative_paths(repo: Path, paths: list[str]) -> dict[str, str | None]:
    rows: dict[str, str | None] = {}
    for relative in paths:
        path = repo / Path(relative)
        try:
            if path.is_symlink():
                value = b"__EASON_SYMLINK__\0" + os.readlink(path).encode("utf-8", "surrogateescape")
                rows[relative] = hashlib.sha256(value).hexdigest()
            else:
                rows[relative] = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() and path.is_file() else None
        except (OSError, PermissionError, UnicodeError):
            rows[relative] = None
    return rows


def _restore_paths_verified(
    repo: Path,
    before: dict[str, bytes],
    paths: list[str],
    *,
    expected_current_hashes: dict[str, str | None] | None = None,
) -> tuple[list[str], bool, list[str]]:
    unique = sorted(set(paths))
    if expected_current_hashes is not None:
        current = _hash_relative_paths(repo, unique)
        conflicts = [
            relative for relative in unique
            if current.get(relative) != expected_current_hashes.get(relative)
        ]
        if conflicts:
            return [], False, [f"CONCURRENT_CHANGE:{relative}" for relative in conflicts]
    restored: list[str] = []
    for relative in unique:
        path = repo / Path(relative)
        if relative not in before:
            try:
                if path.exists() or path.is_symlink():
                    path.unlink()
            except OSError:
                pass
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(before[relative])
        restored.append(relative)
    remaining = []
    for relative in unique:
        path = repo / Path(relative)
        expected = before.get(relative)
        try:
            if path.is_symlink():
                current = b"__EASON_SYMLINK__\0" + os.readlink(path).encode("utf-8", "surrogateescape")
            else:
                current = path.read_bytes() if path.exists() and path.is_file() else None
        except (OSError, UnicodeError):
            current = b"__READ_ERROR__"
        if current != expected:
            remaining.append(relative)
    return restored, not remaining, remaining


def _file_hash(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() and path.is_file() else None
    except (OSError, PermissionError):
        return None


def _git_head(repo: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except Exception:
        return None
    value = (result.stdout or "").strip()
    return value if result.returncode == 0 and value else None


def _git_boundary(repo: Path) -> dict[str, str | None]:
    return {
        "head": _git_head(repo),
        "index_hash": _file_hash(repo / ".git" / "index"),
        "head_file_hash": _file_hash(repo / ".git" / "HEAD"),
    }


def _protected_hashes(repo: Path) -> dict[str, str | None]:
    # Persist hashes only: crash recovery must never copy secret contents.
    return {".env": _file_hash(repo / ".env")}


def _recovery_snapshot_path(repo: Path, run_id: int) -> Path:
    root = repo / ".eason-one-runtime" / "codex-recovery"
    root.mkdir(parents=True, exist_ok=True)
    return root / f"run-{run_id}.zip"


def _persist_recovery_snapshot(repo: Path, run_id: int, before: dict[str, bytes]) -> Path:
    """Persist the governed source surface before Codex can mutate it.

    The archive deliberately excludes .git, .env, databases, venvs and runtime
    state. It exists only so a process restart can restore a known source state
    without asking the Founder to reconcile a local Codex process.
    """
    path = _recovery_snapshot_path(repo, run_id)
    temp = path.with_suffix(".tmp")
    with zipfile.ZipFile(temp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for relative, content in sorted(before.items()):
            archive.writestr(relative, content)
    temp.replace(path)
    return path


def _load_recovery_snapshot(path: Path) -> dict[str, bytes]:
    snapshot: dict[str, bytes] = {}
    with zipfile.ZipFile(path, "r") as archive:
        for info in archive.infolist():
            relative = Path(info.filename)
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("Unsafe path in Codex recovery snapshot")
            snapshot[relative.as_posix()] = archive.read(info)
    return snapshot


def _recovery_metadata(run: AgentRun) -> dict[str, Any]:
    return dict((run.context_composition_json or {}).get("codex_recovery") or {})


def _cleanup_recovery_snapshot(run: AgentRun) -> None:
    value = _recovery_metadata(run).get("snapshot_path")
    if not value:
        return
    try:
        Path(value).unlink(missing_ok=True)
    except OSError:
        pass


def reconcile_interrupted_run(run: AgentRun) -> dict[str, Any]:
    """Reconcile a RUNNING Codex attempt after the Eason One process restarted.

    Auto-recovery is permitted only when Eason One can prove that the Git and
    protected-file boundaries are unchanged and can restore the governed source
    tree from the durable pre-dispatch snapshot. Otherwise the caller must keep
    the Work behind a hard reconciliation gate.
    """
    metadata = _recovery_metadata(run)
    if metadata.get("isolation_version") == "CODEX_DISPOSABLE_WORKSPACE_V1":
        workspace_root = metadata.get("workspace_root")
        if workspace_root:
            shutil.rmtree(Path(workspace_root), ignore_errors=True)
        run.status = "FAILED"
        run.outcome = "FAILED_SAFE"
        run.failure_reason = "CODEX_ISOLATED_PROCESS_RESTARTED"
        run.failure_stage = "CODEX_TOOL"
        run.error_text = (
            "Eason One restarted during isolated Codex work; the live repository was never exposed "
            "and no isolated delta was copied back."
        )
        run.finished_at = now()
        run.real_cost = 0
        run.context_composition_json = {
            **(run.context_composition_json or {}),
            "restart_recovery": {"restored": True, "copyback": False, "at": _iso_now()},
        }
        effect = next((row for row in reversed(list(run.external_effects or [])) if row.provider == CODEX_PROVIDER_KEY), None)
        if effect is not None:
            effect.state = "SETTLED"
            effect.actual_cost_twd = 0
            effect.error_text = run.error_text
            effect.settled_at = now()
        __import__("eason_one.services.external_effects", fromlist=["emit_execution_event"]).emit_execution_event(
            run, "EXECUTION_FAILED_SAFE", payload={"failure_reason": run.failure_reason, "copyback": False}
        )
        return {"safe": True, "reason": run.error_text, "reverted_files": []}
    repo_value = metadata.get("repository")
    snapshot_value = metadata.get("snapshot_path")
    if not repo_value or not snapshot_value:
        return {"safe": False, "reason": "Codex recovery metadata is missing."}
    try:
        repo = Path(repo_value).expanduser().resolve()
        snapshot_path = Path(snapshot_value).expanduser().resolve()
    except Exception as exc:
        return {"safe": False, "reason": f"Codex recovery paths are invalid: {exc}"}
    if not repo.exists() or not repo.is_dir():
        return {"safe": False, "reason": f"Codex repository is unavailable: {repo}"}
    if not any(_is_within(repo, root) for root in _allowed_repos()):
        return {"safe": False, "reason": "Codex repository is no longer inside the configured allowlist."}
    if not snapshot_path.exists() or not _is_within(snapshot_path, repo / ".eason-one-runtime"):
        return {"safe": False, "reason": "Durable Codex recovery snapshot is unavailable."}

    before_git = dict(metadata.get("git_boundary") or {})
    current_git = _git_boundary(repo)
    if not before_git.get("head") or before_git != current_git:
        return {
            "safe": False,
            "reason": "Git metadata changed while Codex owned the repository; automatic rollback would be ambiguous.",
            "git_before": before_git,
            "git_current": current_git,
        }
    before_protected = dict(metadata.get("protected_hashes") or {})
    current_protected = _protected_hashes(repo)
    if before_protected != current_protected:
        return {
            "safe": False,
            "reason": "A protected file changed while Codex owned the repository; automatic rollback is blocked.",
        }
    try:
        before = _load_recovery_snapshot(snapshot_path)
        reverted, restore_verified, restore_remaining = _restore_repository_verified(repo, before)
        if not restore_verified:
            return {"safe": False, "reason": "Codex rollback left a residual source delta.", "remaining": restore_remaining}
    except Exception as exc:
        return {"safe": False, "reason": f"Codex source rollback failed: {exc}"}

    run.status = "FAILED"
    run.outcome = "FAILED_SAFE"
    run.failure_reason = "CODEX_PROCESS_RESTART_RESTORED"
    run.failure_stage = "CODEX_TOOL"
    run.error_text = "Eason One restarted during Codex work, restored the pre-dispatch source snapshot, and will retry safely."
    run.finished_at = now()
    run.real_cost = 0
    run.context_composition_json = {
        **(run.context_composition_json or {}),
        "restart_recovery": {"restored": True, "restore_verified": True, "reverted_files": reverted, "at": _iso_now()},
    }
    effect = next((row for row in reversed(list(run.external_effects or [])) if row.provider == CODEX_PROVIDER_KEY), None)
    if effect is not None:
        effect.state = "SETTLED"
        effect.actual_cost_twd = 0
        effect.error_text = run.error_text
        effect.settled_at = now()
    __import__("eason_one.services.external_effects", fromlist=["emit_execution_event"]).emit_execution_event(
        run, "EXECUTION_FAILED_SAFE",
        payload={"failure_reason": run.failure_reason, "reverted_files": reverted},
    )
    return {"safe": True, "reason": run.error_text, "reverted_files": reverted}


def cleanup_recovery_snapshot(run: AgentRun) -> None:
    _cleanup_recovery_snapshot(run)


def rollback_completed_write(
    run: AgentRun, *, reason: str = "ARTIFACT_REJECTED"
) -> dict[str, Any]:
    """Rollback a successful but not-authoritatively-accepted Codex write.

    Only the exact files changed by that execution are touched, and only when
    their current hashes still match the post-execution hashes. A concurrent
    modification therefore becomes reconciliation instead of collateral damage.

    This is used not only for reviewer rejection, but also when a successful
    execution discovers that Founder authority is still required before the
    change may become Company Truth.
    """
    metadata = _recovery_metadata(run)
    repo_value = metadata.get("repository")
    snapshot_value = metadata.get("snapshot_path")
    composition = dict(run.context_composition_json or {})
    changed = list(composition.get("repository_delta") or [])
    post_hashes = dict(composition.get("post_execution_changed_file_hashes") or {})
    if not changed:
        _cleanup_recovery_snapshot(run)
        run.context_composition_json = {
            **composition,
            "completed_write_restore_verified": True,
            "completed_write_restore_reason": reason,
            "completed_write_reverted_files": [],
        }
        return {"safe": True, "reverted_files": [], "reason": "NO_REPOSITORY_DELTA"}
    if not repo_value or not snapshot_value:
        return {"safe": False, "reason": "Successful Codex rollback metadata is incomplete."}
    try:
        repo = Path(repo_value).expanduser().resolve()
        snapshot = Path(snapshot_value).expanduser().resolve()
        if not snapshot.exists():
            return {"safe": False, "reason": "Successful Codex rollback snapshot is unavailable."}
        before = _load_recovery_snapshot(snapshot)
        restored, verified, remaining = _restore_paths_verified(
            repo, before, changed, expected_current_hashes=post_hashes,
        )
    except Exception as exc:
        return {"safe": False, "reason": f"Successful Codex rollback failed: {exc}"}
    if not verified:
        return {"safe": False, "reason": "Successful Codex rollback is ambiguous because source changed after execution.", "remaining": remaining}
    run.context_composition_json = {
        **composition,
        "completed_write_restore_verified": True,
        "completed_write_restore_reason": reason,
        "completed_write_reverted_files": restored,
        # Compatibility keys retained for the first v0.19 reviewer-rejection
        # recovery path; new authority checks read the generic fields above.
        "review_rejection_restore_verified": reason == "ARTIFACT_REJECTED",
        "review_rejection_reverted_files": restored if reason == "ARTIFACT_REJECTED" else [],
    }
    _cleanup_recovery_snapshot(run)
    return {"safe": True, "reverted_files": restored}


def _runtime_output_dir(repo: Path) -> Path:
    # Transport plumbing must live outside the disposable repository so the
    # post-run full-delta observer can treat every in-repository path as Codex
    # output.  The directory is removed by _run_subprocess() in all cases.
    return Path(tempfile.mkdtemp(prefix="eason-one-codex-io-"))


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _short_command(value: Any, limit: int = 180) -> str:
    if isinstance(value, list):
        text = " ".join(str(item) for item in value)
    else:
        text = str(value or "")
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _test_command(command: str) -> bool:
    lowered = command.casefold()
    markers = (
        "pytest", "unittest", "npm test", "pnpm test", "yarn test",
        "vitest", "jest", "cargo test", "go test", "dotnet test",
    )
    return any(marker in lowered for marker in markers)


def _read_command(command: str) -> bool:
    lowered = command.casefold().lstrip()
    markers = (
        "rg ", "grep ", "cat ", "sed ", "head ", "tail ", "find ",
        "git status", "git diff", "ls", "pwd", "get-content", "select-string",
    )
    return any(lowered.startswith(marker) or marker in lowered for marker in markers)


def _record_live_execution(
    operation_id: int | None,
    *,
    run_id: int | None = None,
    task_id: int | None = None,
    state: str | None = None,
    phase: str | None = None,
    detail: str | None = None,
    kind: str = "STATUS",
    current_action: str | None = None,
    counters: dict[str, Any] | None = None,
    append_event: bool = True,
) -> None:
    """Persist a bounded Founder-visible Codex heartbeat.

    Only summaries are stored here. Full stdout/stderr and structured output stay
    on AgentRun so the live monitor never becomes a second audit ledger.
    """
    if not operation_id:
        return
    operation = db.session.get(Operation, operation_id)
    if not operation:
        return
    stamp = _iso_now()
    memory = dict(operation.memory_json or {})
    runtime = dict(memory.get("runtime") or {})
    live = dict(runtime.get("live_execution") or {})
    if run_id is not None:
        live["run_id"] = run_id
    if task_id is not None:
        live["task_id"] = task_id
    if state is not None:
        live["state"] = state
    if phase is not None:
        if phase not in {"FAILED", "TIMEOUT", "CONNECTOR_ERROR", "ARCHIVED"}:
            live["last_progress_phase"] = phase
        live["phase"] = phase
    if detail is not None:
        live["detail"] = detail
    if current_action is not None or state in {"COMPLETED", "FAILED", "ARCHIVED"}:
        live["current_action"] = current_action
    if counters:
        merged = dict(live.get("counters") or {})
        for key, value in counters.items():
            if key in {"files_changed", "files_seen"}:
                previous = list(merged.get(key) or [])
                for item in value or []:
                    if item and item not in previous:
                        previous.append(item)
                merged[key] = previous[-100:]
            else:
                merged[key] = value
        live["counters"] = merged
    live["last_activity_at"] = stamp
    live.setdefault("started_at", stamp)
    if state in {"COMPLETED", "FAILED", "ARCHIVED"}:
        live["finished_at"] = stamp
    if append_event and (detail or phase):
        events = list(live.get("events") or [])
        event = {
            "at": stamp,
            "kind": kind,
            "phase": phase or live.get("phase") or "WORKING",
            "detail": detail or live.get("detail") or "Runtime activity recorded.",
        }
        if current_action:
            event["action"] = current_action
        events.append(event)
        live["events"] = events[-50:]
    runtime["live_execution"] = live
    runtime["heartbeat_at"] = stamp
    memory["runtime"] = runtime
    operation.memory_json = memory
    db.session.commit()


def _summarize_codex_event(event: dict[str, Any]) -> dict[str, Any] | None:
    event_type = str(event.get("type") or "")
    if not event_type:
        return None
    if event_type == "thread.started":
        thread_id = event.get("thread_id") or event.get("id")
        return {
            "phase": "CODEX_SESSION_STARTED",
            "detail": f"Codex session {thread_id or 'started'} is connected through WSL2.",
            "kind": "SESSION",
        }
    if event_type == "turn.started":
        return {
            "phase": "ANALYZING_TASK",
            "detail": "Codex is analyzing the bounded Engineer Job Spec.",
            "kind": "TURN",
        }
    if event_type in {"turn.completed", "turn.failed"}:
        usage = event.get("usage") or (event.get("turn") or {}).get("usage") or {}
        detail = "Codex finished the model turn; Eason One is validating the structured result."
        if event_type == "turn.failed":
            detail = "Codex reported a failed turn; Eason One is preserving the failure evidence."
        return {
            "phase": "VALIDATING_OUTPUT" if event_type == "turn.completed" else "CODEX_ERROR",
            "detail": detail,
            "kind": "TURN",
            "counters": {
                "input_tokens": usage.get("input_tokens"),
                "output_tokens": usage.get("output_tokens"),
            },
        }
    if event_type in {"error", "item.failed"}:
        message = event.get("message") or event.get("error") or "Codex emitted an error event."
        return {
            "phase": "CODEX_ERROR",
            "detail": _short_command(message, 240),
            "kind": "ERROR",
        }
    if event_type not in {"item.started", "item.completed", "item.updated"}:
        return None
    item = event.get("item") or {}
    item_type = str(item.get("type") or event.get("item_type") or "")
    command = item.get("command") or item.get("cmd")
    if item_type == "command_execution" or command:
        command_text = _short_command(command)
        completed = event_type == "item.completed"
        status = item.get("status") or ("completed" if completed else "running")
        exit_code = item.get("exit_code")
        is_test = _test_command(command_text)
        phase = "RUNNING_TESTS" if is_test else "READING_REPOSITORY" if _read_command(command_text) else "RUNNING_COMMAND"
        detail = ("Completed" if completed else "Running") + f": {command_text or 'repository command'}"
        if completed and exit_code is not None:
            detail += f" · exit {exit_code}"
        counters = {
            "current_command": None if completed else command_text,
            "commands_completed" if completed else "commands_started": None,
        }
        counters.pop("commands_completed" if completed else "commands_started")
        counters["last_command_status"] = str(status)
        if is_test:
            counters["last_test_command"] = command_text
            counters["last_test_status"] = str(status)
        return {
            "phase": phase,
            "detail": detail,
            "kind": "TEST" if is_test else "COMMAND",
            "current_action": None if completed else command_text,
            "counter_increment": "tests_completed" if completed and is_test else "tests_started" if is_test else "commands_completed" if completed else "commands_started",
            "counters": counters,
        }
    if item_type in {"file_change", "file_changes", "patch"}:
        changes = item.get("changes") or item.get("files") or []
        files: list[str] = []
        if isinstance(changes, list):
            for change in changes:
                if isinstance(change, dict):
                    value = change.get("path") or change.get("file")
                else:
                    value = change
                if value:
                    files.append(str(value))
        return {
            "phase": "APPLYING_CHANGES",
            "detail": f"Codex recorded {len(files) or 1} bounded file change(s).",
            "kind": "FILE",
            "counters": {"files_changed": files},
        }
    if item_type in {"agent_message", "message"}:
        return {
            "phase": "SYNTHESIZING_RESULT",
            "detail": "Codex is preparing the final structured engineering result.",
            "kind": "MESSAGE",
        }
    if item_type in {"reasoning", "analysis"}:
        return {
            "phase": "ANALYZING_TASK",
            "detail": "Codex is reasoning about the bounded task.",
            "kind": "REASONING",
        }
    return None


def _apply_event_summary(operation_id: int | None, run_id: int | None, task_id: int | None, summary: dict[str, Any]) -> None:
    counters = dict(summary.get("counters") or {})
    increment_key = summary.get("counter_increment")
    if increment_key and operation_id:
        operation = db.session.get(Operation, operation_id)
        live = dict(((operation.memory_json or {}).get("runtime") or {}).get("live_execution") or {}) if operation else {}
        existing = dict(live.get("counters") or {})
        counters[increment_key] = int(existing.get(increment_key) or 0) + 1
    _record_live_execution(
        operation_id,
        run_id=run_id,
        task_id=task_id,
        state="WORKING",
        phase=summary.get("phase"),
        detail=summary.get("detail"),
        kind=summary.get("kind") or "EVENT",
        current_action=summary.get("current_action"),
        counters=counters,
    )

def _run_subprocess(
    repo: Path,
    job_spec: str,
    read_only: bool,
    *,
    operation_id: int | None = None,
    task_id: int | None = None,
    run_id: int | None = None,
) -> CodexResult:
    runner = current_app.config.get("CODEX_RUNNER")
    if runner:
        _record_live_execution(
            operation_id, run_id=run_id, task_id=task_id, state="WORKING",
            phase="CODEX_SESSION_STARTED",
            detail="The configured Codex runner accepted the bounded Engineer Job Spec.",
            kind="SESSION",
        )
        value = runner(repo=repo, prompt=job_spec, read_only=read_only, schema=OUTPUT_SCHEMA)
        return value if isinstance(value, CodexResult) else CodexResult(**value)

    if current_app.config.get("TESTING"):
        _record_live_execution(
            operation_id, run_id=run_id, task_id=task_id, state="WORKING",
            phase="RUNNING_TESTS",
            detail="Deterministic TESTING-mode Codex evidence is being produced.",
            kind="TEST",
        )
        payload = {
            "summary": "Bounded Codex test execution completed with deterministic evidence.",
            "changed_files": [] if read_only else ["eason_one/example.py"],
            "tests": [{
                "command": "python -m pytest tests/test_v0109_engineering_runtime.py",
                "status": "PASSED",
                "detail": "Deterministic TESTING-mode Codex connector evidence.",
            }],
            "acceptance": [{
                "criterion": "Deliver a reviewable result with truthful test evidence.",
                "status": "PASSED",
                "evidence": "TESTING-mode connector returned the governed schema.",
            }],
            "risks": [], "needs_founder": False, "founder_reason": None,
        }
        return CodexResult(
            returncode=0,
            stdout=json.dumps({"type": "turn.completed", "usage": {"input_tokens": 0, "output_tokens": 0}}),
            stderr="", final_output=json.dumps(payload), elapsed_ms=1.0,
            command=["codex", "exec", "<TESTING>"],
        )

    descriptor = codex_runtime_descriptor(force_probe=True)
    if not descriptor.get("ready"):
        raise ValueError("Codex runtime is not ready: " + str(descriptor.get("error") or "unknown readiness failure"))

    configured_timeout = int(os.getenv("EASON_ONE_CODEX_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS))
    timeout = min(configured_timeout, 120) if read_only else configured_timeout
    runtime_dir = _runtime_output_dir(repo)
    try:
        schema_path = runtime_dir / "output-schema.json"
        output_path = runtime_dir / "final-output.json"
        schema_path.write_text(json.dumps(OUTPUT_SCHEMA, ensure_ascii=False), encoding="utf-8")
        # A read-only repository contract still needs writable Codex session/output
        # space.  The source boundary is enforced by before/after snapshot and
        # automatic restoration in _attempt(), not by freezing the whole process.
        sandbox = "workspace-write"
        if descriptor["transport"] == "wsl":
            command = [
                descriptor["host_executable"], "-d", descriptor["distro"], "--",
                descriptor["path"], "exec", "--json", "--ephemeral",
                "--skip-git-repo-check",
                "--sandbox", sandbox, "--cd", _to_wsl_path(repo),
                "--output-schema", _to_wsl_path(schema_path),
                "-o", _to_wsl_path(output_path), job_spec,
            ]
        else:
            command = [
                descriptor["path"], "exec", "--json", "--ephemeral",
                "--skip-git-repo-check",
                "--sandbox", sandbox, "--cd", str(repo),
                "--output-schema", str(schema_path), "-o", str(output_path),
                job_spec,
            ]
        _record_live_execution(
            operation_id, run_id=run_id, task_id=task_id, state="STARTING",
            phase="STARTING_CODEX",
            detail=(
                f"Starting Codex through WSL2 {descriptor.get('distro')} in {repo}."
                if descriptor.get("transport") == "wsl" else f"Starting local Codex in {repo}."
            ),
            kind="TRANSPORT", current_action="Launching bounded Codex process",
            counters={
                "transport": descriptor.get("transport"), "repository": str(repo),
                "read_only": read_only, "commands_started": 0,
                "commands_completed": 0, "tests_started": 0,
                "tests_completed": 0, "files_changed": [],
            },
        )
        started_monotonic = time.monotonic()
        process_env = os.environ.copy()
        process_env["PYTHONDONTWRITEBYTECODE"] = "1"
        existing_pytest_opts = process_env.get("PYTEST_ADDOPTS", "").strip()
        if "no:cacheprovider" not in existing_pytest_opts:
            process_env["PYTEST_ADDOPTS"] = (existing_pytest_opts + " -p no:cacheprovider").strip()
        process = subprocess.Popen(
            command, cwd=str(repo), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding="utf-8", errors="replace", env=process_env, bufsize=1,
        )
        line_queue: queue.Queue[tuple[str, str | None]] = queue.Queue()

        def read_stream(stream, name: str) -> None:
            try:
                for line in iter(stream.readline, ""):
                    line_queue.put((name, line))
            finally:
                line_queue.put((name, None))
                try:
                    stream.close()
                except Exception:
                    pass

        stdout_thread = threading.Thread(target=read_stream, args=(process.stdout, "stdout"), daemon=True)
        stderr_thread = threading.Thread(target=read_stream, args=(process.stderr, "stderr"), daemon=True)
        stdout_thread.start(); stderr_thread.start()
        stdout_parts: list[str] = []; stderr_parts: list[str] = []
        closed_streams: set[str] = set(); last_heartbeat = 0.0
        try:
            while len(closed_streams) < 2 or process.poll() is None:
                elapsed = time.monotonic() - started_monotonic
                if elapsed > timeout:
                    process.kill(); raise subprocess.TimeoutExpired(command, timeout)
                try:
                    stream_name, line = line_queue.get(timeout=0.5)
                except queue.Empty:
                    now_tick = time.monotonic()
                    if now_tick - last_heartbeat >= 5:
                        _record_live_execution(
                            operation_id, run_id=run_id, task_id=task_id,
                            state="WORKING", phase=None, detail=None, append_event=False,
                        )
                        last_heartbeat = now_tick
                    continue
                if line is None:
                    closed_streams.add(stream_name); continue
                if stream_name == "stdout":
                    stdout_parts.append(line)
                    try:
                        event = json.loads(line)
                    except Exception:
                        event = None
                    if isinstance(event, dict):
                        summary = _summarize_codex_event(event)
                        if summary:
                            _apply_event_summary(operation_id, run_id, task_id, summary)
                else:
                    stderr_parts.append(line)
                    stripped = " ".join(line.split())
                    if stripped:
                        _record_live_execution(
                            operation_id, run_id=run_id, task_id=task_id,
                            state="WORKING", phase="CODEX_DIAGNOSTIC",
                            detail="Codex diagnostic: " + _short_command(stripped, 220),
                            kind="DIAGNOSTIC",
                        )
            returncode = process.wait(timeout=5)
        finally:
            if process.poll() is None:
                process.kill()
            stdout_thread.join(timeout=2); stderr_thread.join(timeout=2)
        elapsed_ms = (time.monotonic() - started_monotonic) * 1000
        final_output = output_path.read_text(encoding="utf-8") if output_path.exists() else ""
        return CodexResult(
            returncode=returncode, stdout="".join(stdout_parts),
            stderr="".join(stderr_parts), final_output=final_output,
            elapsed_ms=elapsed_ms, command=command,
        )
    finally:
        shutil.rmtree(runtime_dir, ignore_errors=True)

def _event_usage(stdout: str) -> tuple[int | None, int | None, str | None]:
    input_tokens = output_tokens = None
    thread_id = None
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except Exception:
            continue
        thread_id = thread_id or event.get("thread_id")
        usage = event.get("usage") or (event.get("turn") or {}).get("usage") or {}
        if usage:
            input_tokens = usage.get("input_tokens", input_tokens)
            output_tokens = usage.get("output_tokens", output_tokens)
    return input_tokens, output_tokens, thread_id


def _validate_payload(
    payload: Any,
    task,
    *,
    read_only: bool,
    host_validation: dict | None = None,
) -> tuple[dict, list[str]]:
    """Validate Codex transport/schema without making Codex the acceptance authority.

    For write jobs, the accountable Engineer owns acceptance through independent
    host verification. Codex's tests/acceptance rows remain evidence, but an
    omitted or pessimistic self-assessment must not veto repository truth that
    the host has independently proven.
    """
    errors: list[str] = []
    if not isinstance(payload, dict):
        return {}, ["Codex final output is not a JSON object"]
    if set(payload) != set(OUTPUT_SCHEMA["required"]):
        errors.append("Codex final output fields do not match the governed schema")
    if not isinstance(payload.get("summary"), str) or not payload.get("summary", "").strip():
        errors.append("Codex summary is missing")
    for key in ("changed_files", "tests", "acceptance", "risks"):
        if not isinstance(payload.get(key), list):
            errors.append(f"Codex {key} must be a list")
    if not isinstance(payload.get("needs_founder"), bool):
        errors.append("Codex needs_founder must be boolean")
    if read_only and payload.get("changed_files"):
        errors.append("Read-only Task reported file changes")

    # Read-only work has no independent repository delta to prove the outcome, so
    # its explicit Codex acceptance rows still matter. Write work is governed by
    # host_validation below and by Project closure, not by Codex grading itself.
    acceptance_rows = payload.get("acceptance") or []
    if read_only and not current_app.config.get("TESTING"):
        def norm(value: Any) -> str:
            return " ".join(str(value or "").casefold().split()).strip(" .")
        expected = [row for row in _criteria(task) if row.strip()]
        indexed = {
            norm(row.get("criterion")): row
            for row in acceptance_rows if isinstance(row, dict)
        }
        for criterion in expected:
            row = indexed.get(norm(criterion))
            if not row:
                errors.append(f"Codex omitted approved acceptance criterion: {criterion}")
            elif row.get("status") != "PASSED":
                errors.append(f"Approved acceptance criterion did not pass: {criterion}")

    if not read_only and host_validation is not None and not host_validation.get("success"):
        errors.append(host_validation.get("error") or "Independent Engineer host validation did not pass")
    return payload, errors


def _create_run(task, model: ModelConfig, prompt: str, retry_of_run=None) -> AgentRun:
    operation = task.operation
    work=__import__("eason_one.services.work_runtime",fromlist=["work_for_task"]).work_for_task(task)
    attempt_number=(AgentRun.query.filter_by(work_id=getattr(work,"id",None),purpose="TASK_EXECUTION").count()+1) if work else (int(getattr(retry_of_run,"attempt_number",0) or 0)+1 if retry_of_run else 1)
    company_composition = {}
    if getattr(task, "assigned_employee", None) is not None:
        try:
            _, company_composition = __import__(
                "eason_one.services.context", fromlist=["build_with_composition"]
            ).build_with_composition(task.assigned_employee, getattr(task, "project", None), task)
        except Exception:
            company_composition = {}
    input_lineage = list(((company_composition.get("operation_context") or {}).get("handoff_lineage") or []))
    _learning_text, learning_meta = _persistent_engineer_learning(task)
    run = AgentRun(
        employee_id=task.assigned_employee_id,
        project_id=task.project_id,
        task_id=task.id,
        operation_id=operation.id if operation else None,
        work_id=getattr(work,"id",None),
        model_config_id=model.id,
        purpose="TASK_EXECUTION",
        user_request=task.objective,
        system_prompt_snapshot=(
            "ENGINEER_CODEX_JOB_SPEC\nEngineer creates and governs a bounded Codex job. "
            "Codex is a tool, not an Employee."
        ),
        context_snapshot=prompt,
        context_composition_json={
            "executor": "codex-cli",
            "job_spec_chars": len(prompt),
            "company_handoff": company_composition,
            "persistent_employee_memory": dict(learning_meta or {}),
            "input_artifact_lineage": input_lineage,
        },
        prompt_version="engineer_codex_job-v1",
        prompt_hash=hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        context_hash=hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        retry_of_run_id=getattr(retry_of_run, "id", None),
        attempt_number=attempt_number,
        role_snapshot=task.assigned_employee.role_description if task.assigned_employee else None,
        position_snapshot=(task.assigned_employee.position.name if task.assigned_employee and task.assigned_employee.position else None),
        manager_snapshot=(task.assigned_employee.manager.name if task.assigned_employee and task.assigned_employee.manager else None),
        instruction_version="engineer_codex_job-v1",
        available_tools_snapshot_json=["codex-cli"],
        used_tools_snapshot_json=["codex-cli"],
        outcome="RUNNING",
        provider_key_snapshot=CODEX_PROVIDER_KEY,
        model_name_snapshot=CODEX_MODEL_NAME,
        input_price_snapshot=0,
        output_price_snapshot=0,
        currency_snapshot="TWD",
        currency="TWD",
        effective_max_output_tokens=0,
        status="CREATED",
    )
    db.session.add(run)
    db.session.flush()
    effects = __import__("eason_one.services.external_effects", fromlist=["prepare", "emit_execution_event"])
    effects.prepare(
        run, provider=CODEX_PROVIDER_KEY, system_prompt=run.system_prompt_snapshot,
        user_request=run.user_request, context=prompt, estimated_cost_twd=0,
        effect_kind="LOCAL_REVERSIBLE",
    )
    effects.emit_execution_event(
        run, "EXECUTION_STARTED", payload={"attempt": attempt_number, "provider": "codex", "model": CODEX_MODEL_NAME}
    )
    if retry_of_run:
        retry_of_run.replacement_run_id = run.id
        retry_of_run.resolution_status = "RETRY_STARTED"
        retry_of_run.resolved_at = now()
    db.session.commit()
    return run


def _attempt(task, *, retry_of_run=None, repair_context: str | None = None) -> AgentRun:
    operation = task.operation
    model = ensure_codex_model_config()
    source_repo = _configured_repo(task)
    if not source_repo.exists() or not source_repo.is_dir():
        raise ValueError(f"Codex repository does not exist: {source_repo}")
    if not any(_is_within(source_repo, root) for root in _allowed_repos()):
        raise ValueError(
            f"Codex repository is outside the allowlist: {source_repo}. Configure EASON_ONE_CODEX_ALLOWED_REPOS."
        )
    if not (source_repo / ".git").exists():
        raise ValueError(f"Codex repository must be a Git working tree: {source_repo}")

    work = db.session.get(Work, getattr(task, "work_id", None)) if getattr(task, "work_id", None) else None
    boundary = ensure_execution_boundary(work, task) if work is not None else {}
    read_only = bool(boundary.get("read_only")) if boundary else _is_read_only(task)
    source_before = _snapshot_repository(source_repo)
    isolation_root, repo = _create_isolated_workspace(source_before)
    prompt = build_job_spec(task, repair_context=repair_context, execution_repo=repo)
    try:
        run = _create_run(task, model, prompt, retry_of_run=retry_of_run)
    except Exception:
        shutil.rmtree(isolation_root, ignore_errors=True)
        raise
    protected = _snapshot_protected(repo)
    repository_before = _snapshot_isolated_workspace(repo)
    run.context_composition_json = {
        **(run.context_composition_json or {}),
        "repository": str(source_repo),
        "isolated_repository": str(repo),
        "read_only": read_only,
        "codex_recovery": {
            "isolation_version": "CODEX_DISPOSABLE_WORKSPACE_V1",
            "repository": str(source_repo),
            "workspace_root": str(isolation_root),
            "snapshot_path": None,
            "git_boundary": _git_boundary(source_repo),
            "protected_hashes": _protected_hashes(source_repo),
            "source_file_count": len(repository_before),
            "prepared_at": _iso_now(),
        },
    }
    effect = next((row for row in reversed(list(run.external_effects or [])) if row.provider == CODEX_PROVIDER_KEY), None)
    if effect is None:
        raise RuntimeError("Codex ExternalEffectAttempt was not prepared")
    effects = __import__("eason_one.services.external_effects", fromlist=["mark_dispatching", "mark_response", "mark_persisted", "mark_settled"])
    effects.mark_dispatching(effect)
    run.status = "RUNNING"
    db.session.commit()
    _record_live_execution(
        operation.id if operation else None,
        run_id=run.id,
        task_id=task.id,
        state="STARTING",
        phase="JOB_SPEC_PREPARED",
        detail="Engineer prepared the bounded Codex Job Spec and repository boundary.",
        kind="ENGINEER",
        current_action="Preparing WSL2 Codex execution",
        counters={"job_spec_chars": len(prompt), "repository": str(source_repo), "read_only": read_only,
                  "isolated_workspace": True},
    )
    try:
        result = _run_subprocess(
            repo,
            prompt,
            read_only,
            operation_id=operation.id if operation else None,
            task_id=task.id,
            run_id=run.id,
        )
        effects.mark_response(effect, result)
        db.session.commit()
        _record_live_execution(
            operation.id if operation else None,
            run_id=run.id,
            task_id=task.id,
            state="WORKING",
            phase="VALIDATING_OUTPUT",
            detail="Engineer is validating Codex output, acceptance evidence, and safety boundaries.",
            kind="VALIDATION",
            current_action="Validating structured Codex result",
        )
        input_tokens, output_tokens, thread_id = _event_usage(result.stdout)
        restored = _restore_protected(protected)
        repository_delta = _isolated_workspace_delta(repo, repository_before)
        post_execution_changed_file_hashes = _hash_relative_paths(repo, repository_delta)
        work = db.session.get(Work, getattr(task, "work_id", None)) if getattr(task, "work_id", None) else None
        boundary = dict((work.runtime_control_json or {}).get("codex_execution_boundary") or {}) if work else {}
        symlink_deltas = sorted(
            relative for relative in repository_delta
            if (repo / Path(relative)).is_symlink()
        )
        outside_allowed = _allowed_delta_paths(boundary, repository_delta) if not read_only else []
        outside_allowed = sorted(set(outside_allowed) | set(symlink_deltas))
        outside_restore_verified = None
        outside_restore_remaining = []
        if outside_allowed:
            _, outside_restore_verified, outside_restore_remaining = _restore_paths_verified(
                repo, repository_before, outside_allowed,
                expected_current_hashes=post_execution_changed_file_hashes,
            )
        read_only_deltas = []
        read_only_restore_verified = None
        read_only_restore_remaining = []
        if read_only:
            read_only_deltas, read_only_restore_verified, read_only_restore_remaining = _restore_paths_verified(
                repo, repository_before, repository_delta,
                expected_current_hashes=post_execution_changed_file_hashes,
            )
            repository_delta = read_only_deltas
        run.finished_at = now()
        run.input_tokens = input_tokens
        run.output_tokens = output_tokens
        run.real_cost = 0
        run.provider_request_id = thread_id
        run.provider_response_id = f"codex-exit-{result.returncode}"
        run.provider_stop_reason = "completed" if result.returncode == 0 else "process_error"
        run.context_composition_json = {
            **(run.context_composition_json or {}),
            "repository": str(source_repo),
            "isolated_repository": str(repo),
            "read_only": read_only,
            "elapsed_ms": round(result.elapsed_ms, 2),
            "command": [*result.command[:-1], "<JOB_SPEC>"],
            "transport": codex_runtime_descriptor().get("transport"),
            "protected_files_restored": restored,
            "read_only_deltas_restored": read_only_deltas,
            "read_only_restore_verified": read_only_restore_verified,
            "read_only_restore_remaining": read_only_restore_remaining,
            "repository_delta": repository_delta,
            "outside_allowed_paths": outside_allowed,
            "symlink_delta_paths": symlink_deltas,
            "outside_allowed_restore_verified": outside_restore_verified,
            "outside_allowed_restore_remaining": outside_restore_remaining,
            "post_execution_changed_file_hashes": post_execution_changed_file_hashes,
            "stdout_tail": result.stdout[-12000:],
            "stderr_tail": result.stderr[-12000:],
        }
        run.raw_output = result.final_output or result.stdout[-20000:]
        run.output_hash = hashlib.sha256((run.raw_output or "").encode("utf-8")).hexdigest()
        if outside_allowed:
            run.status = "FAILED"
            run.failure_reason = "CODEX_SCOPE_VIOLATION"
            run.structured_validation_status = "FAILED"
            run.structured_validation_errors_json = [
                "Codex changed or created paths outside the exact Founder-approved path set; the disposable attempt is rejected and no output is copied to the live repository.",
                *outside_allowed,
            ]
            run.error_text = "Codex crossed the exact allowed-path boundary."
        elif restored:
            run.status = "FAILED"
            run.failure_reason = "CODEX_BOUNDARY_VIOLATION"
            run.structured_validation_status = "FAILED"
            run.structured_validation_errors_json = [
                "Codex modified protected files; Eason One restored them automatically.", *restored
            ]
            run.error_text = "Codex crossed the protected-data boundary."
        elif read_only_deltas:
            run.status = "FAILED"
            run.failure_reason = "CODEX_READ_ONLY_ATTEMPTED_CHANGE"
            run.structured_validation_status = "FAILED"
            run.structured_validation_errors_json = [
                "The read-only job attempted repository changes; Eason One restored only this job's delta.",
                *read_only_deltas,
            ]
            run.error_text = "Read-only Codex work attempted a repository change and was safely restored."
        elif result.returncode != 0:
            run.status = "FAILED"
            run.failure_reason = "CODEX_PROCESS_FAILED"
            run.structured_validation_status = "FAILED"
            run.structured_validation_errors_json = [result.stderr[-2000:] or f"exit {result.returncode}"]
            run.error_text = f"Codex CLI exited with code {result.returncode}."
        else:
            host_validation = None
            try:
                payload = json.loads(result.final_output)
            except Exception as exc:
                payload = {}
                errors = [f"Codex final output JSON could not be parsed: {exc}"]
            else:
                if not read_only:
                    _record_live_execution(
                        operation.id if operation else None, run_id=run.id, task_id=task.id,
                        state="WORKING", phase="RUNNING_TESTS",
                        detail="Windows host is running the approved repository validation suite.",
                        kind="TEST", current_action="Running Windows .venv validation",
                    )
                    host_validation = __import__(
                        "eason_one.services.host_validation", fromlist=["validate_codex_result"]
                    ).validate_codex_result(
                        task, repo, payload, actual_changed_files=repository_delta
                    )
                    for host_test in host_validation.get("tests") or [host_validation.get("test")]:
                        if host_test:
                            payload.setdefault("tests", []).append(host_test)
                    run.context_composition_json = {
                        **(run.context_composition_json or {}),
                        "host_validation": host_validation,
                    }
                payload, errors = _validate_payload(
                    payload, task, read_only=read_only, host_validation=host_validation
                )
                if not errors and not read_only:
                    copyback = _copy_back_approved_paths(
                        source_repo, repo, source_before, repository_delta, boundary
                    )
                    run.context_composition_json = {
                        **(run.context_composition_json or {}),
                        "isolated_copyback": copyback,
                    }
                    if not copyback.get("applied"):
                        errors = [
                            "Validated isolated Codex output was not copied back: "
                            + str(copyback.get("reason") or "copyback failed")
                        ]
            if errors:
                run.status = "FAILED"
                run.failure_reason = (host_validation or {}).get("failure_reason") or "CODEX_VALIDATION_FAILED"
                run.structured_validation_status = "FAILED"
                run.structured_validation_errors_json = errors
                run.error_text = "; ".join(errors)
                if payload:
                    run.parsed_output_json = {
                        "result_summary": payload.get("summary") or "Codex returned a result that did not pass host validation.",
                        "knowledge_proposals": [],
                        "codex": payload,
                    }
                    task.result_summary = payload.get("summary") or task.result_summary
            else:
                partial_rows = [
                    row for row in (payload.get("acceptance") or [])
                    if row.get("status") in {"FAILED", "UNKNOWN"}
                ]
                run.status = "SUCCEEDED"
                run.structured_validation_status = "PARTIAL" if partial_rows else "PASSED"
                run.structured_validation_errors_json = (
                    ["One or more read-only checks are incomplete; useful evidence was preserved."]
                    if partial_rows else []
                )
                if partial_rows:
                    run.resolution_status = "PARTIAL_RESULT"
                    run.resolution_note = "Useful read-only evidence exists, but one or more validation checks could not complete."
                run.parsed_output_json = {
                    "result_summary": payload["summary"],
                    "knowledge_proposals": [{
                        "kind": "EVIDENCE",
                        "title": "Codex engineering execution evidence",
                        "content": json.dumps({
                            "changed_files": payload["changed_files"],
                            "tests": payload["tests"],
                            "acceptance": payload["acceptance"],
                            "risks": payload["risks"],
                            "needs_founder": payload["needs_founder"],
                            "founder_reason": payload["founder_reason"],
                        }, ensure_ascii=False),
                        "source_ref": f"codex:run:{run.id}",
                        "rationale": None,
                        "basis_knowledge_ids": [],
                    }],
                    "codex": payload,
                }
                task.result_summary = payload["summary"]
        if run.status == "FAILED" and not read_only:
            reverted, restore_verified, restore_remaining = _restore_paths_verified(
                repo, repository_before, repository_delta,
                expected_current_hashes=post_execution_changed_file_hashes,
            )
            run.context_composition_json = {
                **(run.context_composition_json or {}),
                "failed_write_delta_reverted": reverted,
                "failed_write_restore_verified": restore_verified,
                "failed_write_restore_remaining": restore_remaining,
            }
        run.outcome="SUCCEEDED" if run.status=="SUCCEEDED" else "FAILED_KNOWN"
        run.failure_stage=None if run.status=="SUCCEEDED" else "CODEX_VALIDATION"
        effects.mark_persisted(effect)
        effects.mark_settled(effect, actual_cost_twd=0)
        __import__("eason_one.services.external_effects",fromlist=["emit_execution_event"]).emit_execution_event(
            run,"EXECUTION_SUCCEEDED" if run.status=="SUCCEEDED" else "EXECUTION_FAILED_KNOWN",
            payload={"failure_reason":run.failure_reason},
        )
        db.session.commit()
        if run.status != "SUCCEEDED" or read_only:
            _cleanup_recovery_snapshot(run)
        if run.status == "SUCCEEDED":
            codex_payload = (run.parsed_output_json or {}).get("codex") or {}
            _record_live_execution(
                operation.id if operation else None,
                run_id=run.id,
                task_id=task.id,
                state="COMPLETED",
                phase="EVIDENCE_PERSISTED",
                detail="Engineer accepted the bounded Codex evidence and persisted the reviewable result.",
                kind="RESULT",
                current_action=None,
                counters={
                    "files_changed": codex_payload.get("changed_files") or [],
                    "tests": codex_payload.get("tests") or [],
                    "acceptance": codex_payload.get("acceptance") or [],
                    "risks": codex_payload.get("risks") or [],
                },
            )
        else:
            _record_live_execution(
                operation.id if operation else None,
                run_id=run.id,
                task_id=task.id,
                state="FAILED",
                phase="FAILED",
                detail=run.error_text or run.failure_reason or "Codex execution failed.",
                kind="ERROR",
                current_action=None,
            )
        shutil.rmtree(isolation_root, ignore_errors=True)
        return run
    except subprocess.TimeoutExpired as exc:
        _restore_protected(protected)
        failure_delta = _isolated_workspace_delta(repo, repository_before)
        failure_hashes = _hash_relative_paths(repo, failure_delta)
        reverted, restore_verified, restore_remaining = _restore_paths_verified(
            repo, repository_before, failure_delta, expected_current_hashes=failure_hashes,
        )
        run.status = "FAILED"
        run.failure_reason = "CODEX_TIMEOUT"
        run.structured_validation_status = "FAILED"
        run.structured_validation_errors_json = [str(exc)]
        run.error_text = "Codex CLI exceeded the bounded timeout."
        run.finished_at = now()
        run.real_cost = 0
        run.outcome="FAILED_SAFE"
        run.failure_stage="CODEX_TOOL"
        run.context_composition_json = {
            **(run.context_composition_json or {}),
            "failed_write_delta_reverted": reverted,
            "failed_write_restore_verified": restore_verified,
            "failed_write_restore_remaining": restore_remaining,
        }
        effect.state = "SETTLED"
        effect.actual_cost_twd = 0
        effect.error_text = run.error_text
        effect.settled_at = now()
        __import__("eason_one.services.external_effects",fromlist=["emit_execution_event"]).emit_execution_event(
            run,"EXECUTION_FAILED_SAFE",payload={"failure_reason":run.failure_reason}
        )
        db.session.commit()
        _cleanup_recovery_snapshot(run)
        _record_live_execution(
            operation.id if operation else None,
            run_id=run.id,
            task_id=task.id,
            state="FAILED",
            phase="TIMEOUT",
            detail="Codex exceeded the bounded execution timeout and was stopped.",
            kind="ERROR",
            current_action=None,
        )
        shutil.rmtree(isolation_root, ignore_errors=True)
        return run
    except Exception as exc:
        _restore_protected(protected)
        failure_delta = _isolated_workspace_delta(repo, repository_before)
        failure_hashes = _hash_relative_paths(repo, failure_delta)
        reverted, restore_verified, restore_remaining = _restore_paths_verified(
            repo, repository_before, failure_delta, expected_current_hashes=failure_hashes,
        )
        run.status = "FAILED"
        run.failure_reason = "CODEX_CONNECTOR_ERROR"
        run.structured_validation_status = "FAILED"
        run.structured_validation_errors_json = [str(exc)]
        run.error_text = str(exc)
        run.finished_at = now()
        run.real_cost = 0
        run.outcome="FAILED_SAFE"
        run.failure_stage="CODEX_TOOL"
        run.context_composition_json = {
            **(run.context_composition_json or {}),
            "failed_write_delta_reverted": reverted,
            "failed_write_restore_verified": restore_verified,
            "failed_write_restore_remaining": restore_remaining,
        }
        effect.state = "SETTLED"
        effect.actual_cost_twd = 0
        effect.error_text = run.error_text
        effect.settled_at = now()
        __import__("eason_one.services.external_effects",fromlist=["emit_execution_event"]).emit_execution_event(
            run,"EXECUTION_FAILED_SAFE",payload={"failure_reason":run.failure_reason}
        )
        db.session.commit()
        _cleanup_recovery_snapshot(run)
        _record_live_execution(
            operation.id if operation else None,
            run_id=run.id,
            task_id=task.id,
            state="FAILED",
            phase="CONNECTOR_ERROR",
            detail=str(exc),
            kind="ERROR",
            current_action=None,
        )
        shutil.rmtree(isolation_root, ignore_errors=True)
        return run


def run_codex_task(task) -> AgentRun:
    project = getattr(task, "project", None)
    if project is not None and __import__(
        "eason_one.services.work_runtime", fromlist=["project_is_terminal"]
    ).project_is_terminal(project):
        raise ValueError(
            f"PROJECT_TERMINAL_CODEX_EXECUTION_FORBIDDEN:{str(project.status or '').upper()}"
        )
    operation = task.operation
    memory = dict(getattr(operation, "memory_json", None) or {})
    engineering = dict(memory.get("engineering") or {})
    retry_limit = max(0, min(int(engineering.get("codex_retry_limit", DEFAULT_RETRY_LIMIT)), 2))
    run = _attempt(task)
    for _ in range(retry_limit):
        if run.status == "SUCCEEDED":
            break
        if run.failure_reason in {
            "CODEX_BOUNDARY_VIOLATION", "CODEX_TIMEOUT", "CODEX_CONNECTOR_ERROR",
            "CODEX_SCOPE_VIOLATION", "VALIDATION_ENVIRONMENT_FAILURE",
        }:
            break
        repair = json.dumps({
            "failure_reason": run.failure_reason,
            "validation_errors": run.structured_validation_errors_json or [],
            "previous_output": (run.raw_output or "")[-5000:],
        }, ensure_ascii=False)
        run = _attempt(task, retry_of_run=run, repair_context=repair)
    return run
