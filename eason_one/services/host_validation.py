"""Host-side engineering verification after Codex returns.

Codex output is a claim, not authority.  The frozen Work acceptance contract
owns acceptance scope.  This module produces deterministic host evidence for
only the criteria whose proof ownership was frozen as HOST_*.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from types import SimpleNamespace
from typing import Any

from flask import current_app

from ..extensions import db
from ..models import Work

_HTTP_METHOD_PATH_RE = re.compile(r"\b(GET|POST|PUT|PATCH|DELETE)\s+(/[A-Za-z0-9_./<>:-]+)", re.IGNORECASE)


FOCUSED_V0111_TESTS = [
    "tests/test_v01102_nonblocking_readiness.py",
    "tests/test_v0110_stabilization.py",
    "tests/test_v01012_live_execution.py",
    "tests/test_v01011_wsl_codex.py",
    "tests/test_v01010_truthful_engineering.py",
    "tests/test_v0109_engineering_runtime.py",
    "tests/test_v0108_runtime_ui.py",
    "tests/test_v01103_env_loading.py",
    "tests/test_v01104_ceo_dialogue_persistence.py",
    "tests/test_v01105_main_flow.py",
]


def _task_text(task) -> str:
    operation = getattr(task, "operation", None)
    project = getattr(task, "project", None)
    return " ".join(filter(None, [
        getattr(project, "objective", None),
        getattr(operation, "objective", None),
        getattr(task, "title", None),
        getattr(task, "objective", None),
        getattr(task, "acceptance_criteria", None),
    ]))


def _display(command: list[str]) -> str:
    return " ".join(f'"{item}"' if " " in item else item for item in command)


def _is_windows_host() -> bool:
    return os.name == "nt"


def _normalize_path(value: Any, repo: Path) -> str | None:
    text = str(value or "").strip().replace("\\", "/")
    if not text:
        return None
    try:
        path = Path(text)
        if path.is_absolute():
            text = path.resolve().relative_to(repo.resolve()).as_posix()
    except (OSError, ValueError):
        pass
    while text.startswith("./"):
        text = text[2:]
    return text.strip("/") or None


def _extract_exact_replacements(text: str) -> list[tuple[str, str]]:
    """Extract explicit `from <quoted old> to <quoted new>` contracts.

    We intentionally keep this narrow. If the Founder provided exact strings,
    deterministic code can and should prove them rather than asking a model to
    judge whether a paraphrase is close enough.
    """
    patterns = [
        r"\bfrom\s+[\"“](?P<old>.*?)[\"”]\s+to\s+[\"“](?P<new>.*?)[\"”]",
        r"\bfrom\s+`(?P<old>.*?)`\s+to\s+`(?P<new>.*?)`",
        r"\bchange\s*:\s*[\"“](?P<old>.*?)[\"”]\s+to\s*:\s*[\"“](?P<new>.*?)[\"”]",
        r"\bchange\s*:\s*`(?P<old>.*?)`\s+to\s*:\s*`(?P<new>.*?)`",
    ]
    rows: list[tuple[str, str]] = []
    for pattern in patterns:
        for match in re.finditer(pattern, text or "", flags=re.IGNORECASE | re.DOTALL):
            old = " ".join(match.group("old").split())
            new = " ".join(match.group("new").split())
            if old and new and old != new and (old, new) not in rows:
                rows.append((old, new))
    return rows


def _read_changed_texts(repo: Path, changed_files: list[str]) -> dict[str, str]:
    rows: dict[str, str] = {}
    for relative in changed_files:
        path = repo / Path(relative)
        try:
            resolved = path.resolve()
            resolved.relative_to(repo.resolve())
        except (OSError, ValueError):
            continue
        if not path.exists() or not path.is_file():
            continue
        try:
            rows[relative] = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
    return rows


def _focused_suite(task, repo: Path) -> dict[str, Any] | None:
    # Legacy focused regression has veto authority only when the approved
    # acceptance text explicitly requests that scope.  Implementation clues in
    # title/objective (for example query.get) are not Founder acceptance power.
    text = str(getattr(task, "acceptance_criteria", None) or "").casefold()
    needs_focused = any(marker in text for marker in (
        "focused v0.11.1", "v0.11.1 focused", "v0111 focused",
        "test_v01102_nonblocking_readiness", "test_v0110_stabilization",
    ))
    if not needs_focused:
        return None

    python = repo / ".venv" / "Scripts" / "python.exe"
    if not _is_windows_host() or not python.exists():
        return {
            "success": False,
            "failure_reason": "VALIDATION_ENVIRONMENT_FAILURE",
            "error": (
                "Windows host validation is unavailable. The repository .venv\\Scripts\\python.exe "
                "must run the approved focused suite after WSL2 Codex returns."
            ),
            "test": {
                "command": "Windows .venv focused V0.11 suite",
                "status": "NOT_RUN",
                "detail": "Host validation environment was unavailable; no boundary violation occurred.",
            },
        }

    base_temp = repo / ".eason-one-test-temp" / "codex-host-validation"
    command = [
        str(python), "-m", "pytest", "-q",
        "-W", "error::sqlalchemy.exc.LegacyAPIWarning",
        "-p", "no:cacheprovider",
        "--basetemp", str(base_temp),
        *FOCUSED_V0111_TESTS,
    ]
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        completed = subprocess.run(
            command, cwd=repo, capture_output=True, text=True,
            timeout=420, env=env, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "success": False,
            "failure_reason": "VALIDATION_ENVIRONMENT_FAILURE",
            "error": str(exc),
            "test": {
                "command": _display(command),
                "status": "NOT_RUN",
                "detail": f"Windows host validation could not complete: {exc}",
            },
        }

    output = "\n".join(filter(None, [completed.stdout, completed.stderr])).strip()
    tail = output[-6000:]
    success = completed.returncode == 0
    return {
        "success": success,
        "failure_reason": None if success else "HOST_VALIDATION_FAILED",
        "error": None if success else tail,
        "test": {
            "command": _display(command),
            "status": "PASSED" if success else "FAILED",
            "detail": tail or f"pytest exited {completed.returncode}",
        },
    }



def _python_for_repo(repo: Path) -> Path | None:
    candidates = [
        repo / ".python313" / "python.exe",
        repo / ".python313" / "bin" / "python",
        repo / ".venv" / "Scripts" / "python.exe",
        repo / ".venv" / "bin" / "python",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _norm_criterion(value: Any) -> str:
    return " ".join(str(value or "").casefold().split()).strip(" .")


def _criterion_rows(task) -> list[str]:
    return [
        row.strip(" -\t")
        for row in (getattr(task, "acceptance_criteria", None) or "").splitlines()
        if row.strip(" -\t")
    ]


def _scalar(value: str) -> Any:
    text = value.strip().strip("`")
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {chr(34), chr(39)}:
        return text[1:-1]
    lowered = text.casefold()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered == "null":
        return None
    try:
        return int(text)
    except ValueError:
        try:
            return float(text)
        except ValueError:
            return text


def _extract_http_contract(task) -> dict[str, Any] | None:
    text = _task_text(task)
    match = _HTTP_METHOD_PATH_RE.search(text)
    if not match:
        return None
    method = match.group(1).upper()
    path = match.group(2).rstrip("`'\".,，。；;:)]}")
    expected: dict[str, Any] = {}
    pair_pattern = re.compile(
        r"\b([A-Za-z_][A-Za-z0-9_-]*)\s*=\s*(true|false|null|-?\d+(?:\.\d+)?|\"[^\"]*\"|'[^']*'|`[^`]*`)",
        flags=re.IGNORECASE,
    )
    for pair in pair_pattern.finditer(text):
        expected[pair.group(1)] = _scalar(pair.group(2))
    lowered = text.casefold()
    status_match = re.search(r"\bHTTP\s*(?:status\s*)?(?:=|:)?\s*(\d{3})\b", text, flags=re.IGNORECASE)
    expected_status = int(status_match.group(1)) if status_match else None
    require_current_version = any(marker in lowered for marker in (
        "current application version", "current app version",
        "目前 application version", "目前 app version", "目前版本",
    ))
    db_markers = ("database", " db ", "sqlite", "migration", "alembic", "資料庫", "遷移")
    no_effect_markers = (
        "no db", "no database", "without db", "without database",
        "no side effect", "no side-effect", "without side effect",
        "must not write", "does not write", "do not write",
        "無 db", "無db", "不寫入資料庫", "不得寫入資料庫", "無資料庫副作用", "不影響資料庫",
    )
    require_no_db_effect = (
        any(marker in lowered for marker in no_effect_markers)
        or (any(marker in lowered for marker in db_markers) and any(marker in lowered for marker in ("read-only", "read only", "唯讀", "只讀", "不得", "不能")))
    )
    require_health_check = (
        path != "/api/healthz"
        and any(marker in lowered for marker in (
            "health check", "health endpoint", "healthz", "健康檢查", "健康檢查", "健康端點",
        ))
    )
    return {
        "method": method, "path": path, "expected_json": expected,
        "expected_status": expected_status,
        "require_current_version": require_current_version,
        "version_source": "pyproject.toml:[project].version" if require_current_version else None,
        "require_health_check": require_health_check,
        "health_check": {"method": "GET", "path": "/api/healthz", "expected_status": 200} if require_health_check else None,
        "require_no_db_effect": require_no_db_effect,
    }


def _run_http_contract(task, repo: Path) -> dict[str, Any] | None:
    """Run the approved HTTP contract through a real loopback TCP server.

    Founder criteria that say GET/POST/... are network behavior contracts. A
    Flask ``test_client`` call is useful unit evidence but is not equivalent to
    starting the application and issuing an HTTP request.  The verifier starts
    an isolated Werkzeug server on an ephemeral 127.0.0.1 port inside the
    repository Python, performs one real urllib request, then shuts the server
    down.  It never reuses the live Headquarters port or database.
    """
    contract = _extract_http_contract(task)
    if not contract:
        return None
    python = _python_for_repo(repo)
    if not python:
        return {
            "success": False,
            "failure_reason": "VALIDATION_ENVIRONMENT_FAILURE",
            "error": "Repository virtualenv Python was not found for live HTTP endpoint validation.",
            "contract": contract,
            "test": {
                "command": "isolated loopback HTTP endpoint validation",
                "status": "NOT_RUN",
                "detail": "Repository virtualenv Python was not found.",
            },
        }
    script = r"""import json, sys, tempfile, threading, urllib.request, urllib.error, tomllib
from pathlib import Path
from werkzeug.serving import make_server
from eason_one import create_app

method, path, expected = sys.argv[1], sys.argv[2], json.loads(sys.argv[3])
expected_status = None if sys.argv[4] == "" else int(sys.argv[4])
require_current_version = sys.argv[5] == "1"
require_health_check = sys.argv[6] == "1"
require_no_db_effect = sys.argv[7] == "1"
root = Path(tempfile.mkdtemp(prefix="eason-one-http-verify-"))
db_path = (root / "validation.db").as_posix()
app = create_app({
    "TESTING": True,
    "AUTO_START_COMPANY_RUNTIME": False,
    "AUTO_START_OPERATION_RUNTIME": False,
    "SQLALCHEMY_DATABASE_URI": "sqlite:///" + db_path,
})
server = make_server("127.0.0.1", 0, app, threaded=True)
port = int(server.server_port)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
url = f"http://127.0.0.1:{port}{path}"
status = None
payload = None
body = ""
health_status = None
health_body = ""
problems = []
try:
    request = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(request, timeout=15) as response:
        status = int(response.status)
        body = response.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(body)
        except Exception:
            payload = None
    if require_health_check:
        health_url = f"http://127.0.0.1:{port}/api/healthz"
        try:
            with urllib.request.urlopen(urllib.request.Request(health_url, method="GET"), timeout=15) as health_response:
                health_status = int(health_response.status)
                health_body = health_response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            health_status = int(exc.code)
            health_body = exc.read().decode("utf-8", errors="replace")
        except Exception as exc:
            problems.append(f"health check request failed: {type(exc).__name__}: {exc}")
except urllib.error.HTTPError as exc:
    status = int(exc.code)
    body = exc.read().decode("utf-8", errors="replace")
except Exception as exc:
    problems.append(f"request failed: {type(exc).__name__}: {exc}")
finally:
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)

if expected_status is not None:
    if status != expected_status:
        problems.append(f"HTTP status expected {expected_status}, got {status}")
elif status is None or not (200 <= status < 300):
    problems.append(f"HTTP {status}")
if require_health_check and health_status != 200:
    problems.append(f"GET /api/healthz expected HTTP 200, got {health_status}")
if expected and not isinstance(payload, dict):
    problems.append("response is not a JSON object")
if isinstance(payload, dict):
    for key, value in expected.items():
        if payload.get(key) != value:
            problems.append(f"{key} expected {value!r}, got {payload.get(key)!r}")

current_version = None
if require_current_version:
    try:
        current_version = str(tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))["project"]["version"])
    except Exception as exc:
        problems.append(f"application version source unavailable: {type(exc).__name__}: {exc}")
    else:
        if not isinstance(payload, dict):
            problems.append("version comparison requires a JSON object")
        elif payload.get("version") != current_version:
            problems.append(f"version expected current application version {current_version!r}, got {payload.get('version')!r}")

# The live TCP request above is the endpoint acceptance proof. For an explicit
# no-database-effect criterion, retain a second deterministic in-process DB
# mutation check; this is supplementary evidence and does not replace real HTTP.
db_unchanged = None
if require_no_db_effect:
    from eason_one.extensions import db
    client = app.test_client()
    with app.app_context():
        raw = db.session.connection().connection
        driver = getattr(raw, "driver_connection", raw)
        before_changes = getattr(driver, "total_changes", None)
    response = client.open(path, method=method)
    with app.app_context():
        raw = db.session.connection().connection
        driver = getattr(raw, "driver_connection", raw)
        after_changes = getattr(driver, "total_changes", None)
    delta = None if before_changes is None or after_changes is None else after_changes - before_changes
    db_unchanged = delta == 0
    if delta is None:
        problems.append("database side-effect counter unavailable")
    elif delta != 0:
        problems.append(f"database total_changes delta expected 0, got {delta}")

print(json.dumps({
    "status": status,
    "json": payload,
    "body": body[-2000:],
    "problems": problems,
    "url": url,
    "transport": "REAL_LOOPBACK_HTTP",
    "server": "WERKZEUG_EPHEMERAL",
    "db_unchanged": db_unchanged,
    "expected_status": expected_status,
    "current_application_version": current_version,
    "version_source": "pyproject.toml:[project].version" if require_current_version else None,
    "health_check": {
        "required": require_health_check,
        "status": health_status,
        "body": health_body[-1000:] if health_body else "",
        "url": f"http://127.0.0.1:{port}/api/healthz" if require_health_check else None,
    },
}, ensure_ascii=False))
raise SystemExit(1 if problems else 0)
"""
    command = [
        str(python), "-c", script, contract["method"], contract["path"],
        json.dumps(contract["expected_json"], ensure_ascii=False),
        "" if contract.get("expected_status") is None else str(contract["expected_status"]),
        "1" if contract.get("require_current_version") else "0",
        "1" if contract.get("require_health_check") else "0",
        "1" if contract.get("require_no_db_effect") else "0",
    ]
    try:
        completed = subprocess.run(
            command, cwd=repo, capture_output=True, text=True, timeout=120,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "success": False,
            "failure_reason": "VALIDATION_ENVIRONMENT_FAILURE",
            "error": str(exc),
            "contract": contract,
            "test": {
                "command": f"{contract['method']} {contract['path']} via isolated live loopback HTTP",
                "status": "NOT_RUN",
                "detail": str(exc),
            },
        }
    output = "\n".join(filter(None, [completed.stdout, completed.stderr])).strip()
    success = completed.returncode == 0
    observation = {}
    try:
        observation = json.loads((completed.stdout or "").strip().splitlines()[-1])
    except Exception:
        observation = {}
    return {
        "success": success,
        "failure_reason": None if success else "HTTP_ACCEPTANCE_FAILED",
        "error": None if success else output[-4000:],
        "contract": contract,
        "observation": observation,
        "test": {
            "command": f"{contract['method']} {contract['path']} via isolated live loopback HTTP",
            "status": "PASSED" if success else "FAILED",
            "detail": output[-4000:] or f"live endpoint validation exited {completed.returncode}",
        },
    }

def _http_acceptance_checks(task, acceptance_contract: dict, repo: Path) -> dict[str, Any]:
    """Run every frozen HTTP contract, not whichever endpoint appears first.

    A Work may legitimately mention a new endpoint plus an existing health/API
    regression endpoint. Older code collapsed every key=value expectation onto
    the first path in the combined task text. This groups frozen criteria by
    exact method/path and binds response-only/no-effect criteria only when one
    primary non-health endpoint is unambiguous.
    """
    items = [
        item for item in (acceptance_contract.get("criteria") or [])
        if item.get("proof_owner") in {"HOST_HTTP_CONTRACT", "HOST_DATABASE_NO_EFFECT"}
    ]
    endpoints: list[tuple[str, str]] = []
    for text in [
        getattr(task, "title", None), getattr(task, "objective", None),
        *[item.get("text") for item in items],
    ]:
        for match in _HTTP_METHOD_PATH_RE.finditer(str(text or "")):
            key = (match.group(1).upper(), match.group(2).rstrip("`'\".,，。；;:)]}"))
            if key not in endpoints:
                endpoints.append(key)
    non_health = [key for key in endpoints if key[1] != "/api/healthz"]
    primary = non_health[0] if len(non_health) == 1 else (endpoints[0] if len(endpoints) == 1 else None)

    groups: dict[tuple[str, str], list[dict]] = {}
    target_by_id: dict[str, tuple[str, str] | None] = {}
    for item in items:
        text = str(item.get("text") or "")
        explicit = []
        for match in _HTTP_METHOD_PATH_RE.finditer(text):
            key = (match.group(1).upper(), match.group(2).rstrip("`'\".,，。；;:)]}"))
            if key not in explicit:
                explicit.append(key)
        target = explicit[0] if len(explicit) == 1 else (primary if not explicit else None)
        criterion_id = str(item.get("id") or "")
        target_by_id[criterion_id] = target
        if target is not None:
            groups.setdefault(target, []).append(item)

    executions: dict[tuple[str, str], dict] = {}
    for (method, path), grouped_items in groups.items():
        text = "\n".join([
            f"{method} {path}",
            *[str(item.get("text") or "") for item in grouped_items],
        ])
        probe = SimpleNamespace(
            project=None, operation=None, title="", objective="", acceptance_criteria=text,
        )
        result = _run_http_contract(probe, repo)
        if result is not None:
            executions[(method, path)] = result

    return {
        "executions": executions,
        "target_by_criterion_id": target_by_id,
        "primary_target": primary,
        "ambiguous_criterion_ids": [
            criterion_id for criterion_id, target in target_by_id.items() if target is None
        ],
    }


def _requires_repository_regression(task) -> bool:
    """Return True only when the approved acceptance contract explicitly asks
    for repository-wide regression evidence.

    v0.18 Work acceptance must be scoped to the Founder-approved outcome. A
    generic word such as ``test`` or ``驗證`` is not authority to run the
    entire historical repository suite and let unrelated legacy debt veto a
    bounded Work. Endpoint/file-specific host checks remain authoritative.
    """
    text = " ".join(filter(None, [
        getattr(task, "objective", None),
        getattr(task, "acceptance_criteria", None),
    ])).casefold()
    explicit = (
        "full test suite", "entire test suite", "complete test suite",
        "repository test suite", "repository-wide test", "repository wide test",
        "all tests", "all existing tests", "existing tests must pass",
        "regression suite", "full regression", "repository pytest", "pytest -q",
        "完整測試", "全套測試", "全部測試", "所有測試", "回歸測試",
        "不破壞既有測試",
    )
    return any(marker in text for marker in explicit)


def _generic_regression_suite(task, repo: Path) -> dict[str, Any] | None:
    if not _requires_repository_regression(task):
        return None
    python = _python_for_repo(repo)
    if not python:
        return {
            "success": False,
            "failure_reason": "VALIDATION_ENVIRONMENT_FAILURE",
            "error": "Repository virtualenv Python was not found for regression validation.",
            "test": {
                "command": "repository pytest",
                "status": "NOT_RUN",
                "detail": "Repository virtualenv Python was not found.",
            },
        }
    base_temp = repo / ".eason-one-test-temp" / "codex-generic-validation"
    command = [
        str(python), "-m", "pytest", "-q", "--maxfail=1", "-p", "no:cacheprovider",
        "--basetemp", str(base_temp),
    ]
    try:
        completed = subprocess.run(
            command, cwd=repo, capture_output=True, text=True, timeout=420,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "success": False,
            "failure_reason": "VALIDATION_ENVIRONMENT_FAILURE",
            "error": str(exc),
            "test": {"command": _display(command), "status": "NOT_RUN", "detail": str(exc)},
        }
    output = "\n".join(filter(None, [completed.stdout, completed.stderr])).strip()
    success = completed.returncode == 0
    return {
        "success": success,
        "failure_reason": None if success else "HOST_VALIDATION_FAILED",
        "error": None if success else output[-6000:],
        "test": {
            "command": _display(command),
            "status": "PASSED" if success else "FAILED",
            "detail": output[-6000:] or f"pytest exited {completed.returncode}",
        },
    }

def _changed_file_hashes(repo: Path, changed_files: list[str]) -> dict[str, str | None]:
    rows: dict[str, str | None] = {}
    for relative in changed_files:
        path = repo / Path(relative)
        try:
            resolved = path.resolve()
            resolved.relative_to(repo.resolve())
        except (OSError, ValueError):
            rows[relative] = None
            continue
        if not path.exists() or not path.is_file():
            rows[relative] = None
            continue
        try:
            rows[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            rows[relative] = None
    return rows


def _repository_delta_hash(rows: dict[str, str | None]) -> str:
    payload = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def validate_codex_result(
    task,
    repo: Path,
    payload: dict[str, Any],
    *,
    actual_changed_files: list[str] | None = None,
    acceptance_contract: dict | None = None,
) -> dict[str, Any]:
    """Independently verify a write result on the host.

    A PASSED Codex schema is not sufficient. For write jobs, the actual source
    delta must match the files Codex claimed it changed. When the approved
    contract contains an exact old/new string replacement, the host reads the
    changed files and proves the requested new string exists and the old string
    is gone.
    """
    if current_app.config.get("TESTING") and os.getenv("EASON_ONE_TEST_HOST_VALIDATION") != "1":
        return {
            "attempted": False,
            "success": True,
            "failure_reason": None,
            "test": None,
            "verification_mode": "TEST_FIXTURE_SKIP",
            "actual_changed_files": list(actual_changed_files or []),
            "reported_changed_files": list(payload.get("changed_files") or []),
            "repository_delta_match": None,
            "exact_replacements": [],
        }

    repo = repo.resolve()
    reported = sorted({
        value for value in (
            _normalize_path(item, repo) for item in (payload.get("changed_files") or [])
        ) if value
    })
    actual = sorted({
        value for value in (
            _normalize_path(item, repo) for item in (actual_changed_files or [])
        ) if value
    })
    changed_file_hashes = _changed_file_hashes(repo, actual)
    repository_delta_hash = _repository_delta_hash(changed_file_hashes)
    checks: list[dict[str, Any]] = []
    errors: list[str] = []
    failure_reason = None

    if reported != actual:
        failure_reason = "CODEX_CHANGED_FILE_MISMATCH"
        errors.append(
            "Codex changed-file claim does not match the host-observed repository delta. "
            f"reported={reported}; actual={actual}"
        )
    checks.append({
        "kind": "REPOSITORY_DELTA",
        "status": "PASSED" if reported == actual else "FAILED",
        "reported": reported,
        "actual": actual,
    })

    replacements = []  # populated from frozen acceptance contract below
    changed_texts = _read_changed_texts(repo, actual)
    source_texts = {
        name: content for name, content in changed_texts.items()
        if not name.casefold().startswith(("tests/", "test/"))
    } or changed_texts
    focused = _focused_suite(task, repo)
    if focused:
        checks.append({
            "kind": "FOCUSED_TEST_SUITE",
            "status": "PASSED" if focused["success"] else "FAILED",
            "detail": focused.get("test"),
        })
        if not focused["success"]:
            failure_reason = focused.get("failure_reason") or failure_reason or "HOST_VALIDATION_FAILED"
            errors.append(focused.get("error") or "Focused host validation failed")

    if acceptance_contract is None:
        work = db.session.get(Work, getattr(task, "work_id", None)) if getattr(task, "work_id", None) else None
        if not work:
            failure_reason = failure_reason or "ACCEPTANCE_CONTRACT_MISSING"
            errors.append("Engineering Work has no durable Work acceptance contract owner.")
            acceptance_contract = {"version": None, "contract_hash": None, "criteria": []}
        else:
            try:
                acceptance_contract = __import__(
                    "eason_one.services.acceptance_contract", fromlist=["ensure_for_work"]
                ).ensure_for_work(work, task=task)
            except ValueError as exc:
                failure_reason = failure_reason or "ACCEPTANCE_CONTRACT_MISMATCH"
                errors.append(str(exc))
                acceptance_contract = dict((work.runtime_control_json or {}).get("acceptance_contract") or {})

    frozen_replacement_text = "\n".join(
        str(item.get("text") or "")
        for item in (acceptance_contract or {}).get("criteria", [])
        if item.get("proof_owner") == "HOST_EXACT_REPLACEMENT"
    )
    replacements = _extract_exact_replacements(frozen_replacement_text)
    replacement_rows = []
    for old, new in replacements:
        files_with_new = [name for name, content in source_texts.items() if new in content]
        files_with_old = [name for name, content in source_texts.items() if old in content]
        passed = bool(files_with_new) and not files_with_old
        replacement_rows.append({
            "old": old, "new": new,
            "status": "PASSED" if passed else "FAILED",
            "files_with_new": files_with_new, "files_with_old": files_with_old,
        })
        if not passed:
            failure_reason = failure_reason or "EXACT_ACCEPTANCE_MISMATCH"
            errors.append(
                "Exact frozen replacement criterion was not satisfied on disk: "
                f"expected new={new!r}, forbidden old={old!r}, "
                f"files_with_new={files_with_new}, files_with_old={files_with_old}."
            )
    checks.extend({"kind": "EXACT_REPLACEMENT", **row} for row in replacement_rows)

    http_bundle = _http_acceptance_checks(task, acceptance_contract, repo)
    http_executions = dict(http_bundle.get("executions") or {})
    for (method, path), http_check in http_executions.items():
        checks.append({
            "kind": "HTTP_CONTRACT",
            "status": "PASSED" if http_check["success"] else "FAILED",
            "detail": http_check.get("test"),
            "contract": http_check.get("contract"),
            "method": method, "path": path,
        })
        if not http_check["success"]:
            failure_reason = http_check.get("failure_reason") or failure_reason or "HTTP_ACCEPTANCE_FAILED"
            errors.append(http_check.get("error") or f"Approved HTTP contract did not pass: {method} {path}")

    regression = _generic_regression_suite(task, repo)
    if regression:
        checks.append({
            "kind": "REGRESSION_SUITE",
            "status": "PASSED" if regression["success"] else "FAILED",
            "detail": regression.get("test"),
        })
        if not regression["success"]:
            failure_reason = regression.get("failure_reason") or failure_reason or "HOST_VALIDATION_FAILED"
            errors.append(regression.get("error") or "Repository regression validation failed")

    criterion_results: dict[str, dict[str, Any]] = {}
    target_by_criterion = dict(http_bundle.get("target_by_criterion_id") or {})
    for item in acceptance_contract.get("criteria") or []:
        criterion_id = str(item.get("id") or "")
        criterion = str(item.get("text") or "")
        owner = item.get("proof_owner")
        lowered = criterion.casefold()

        if owner == "INDEPENDENT_REVIEW":
            criterion_results[criterion_id] = {
                "criterion_id": criterion_id,
                "criterion": criterion,
                "status": "UNPROVEN",
                "evidence": "Semantic criterion is reserved for the frozen independent reviewer.",
                "source": "INDEPENDENT_REVIEW_PENDING",
            }
            continue

        if owner == "HOST_HTTP_CONTRACT":
            target = target_by_criterion.get(criterion_id)
            http_check = http_executions.get(target) if target is not None else None
            passed = bool(http_check and http_check.get("success"))
            criterion_results[criterion_id] = {
                "criterion_id": criterion_id,
                "criterion": criterion,
                "status": "PASSED" if passed else "FAILED",
                "evidence": (
                    (http_check or {}).get("test", {}).get("detail")
                    if http_check
                    else "The frozen HTTP criterion is ambiguous across multiple endpoint contracts and was not guessed."
                ),
                "source": "HOST_HTTP_CONTRACT",
                "http_target": list(target) if target is not None else None,
            }
            continue

        if owner == "HOST_EXACT_REPLACEMENT":
            replacement_match = next((
                row for row in replacement_rows
                if row.get("old") in criterion and row.get("new") in criterion
            ), None)
            passed = bool(replacement_match and replacement_match.get("status") == "PASSED")
            criterion_results[criterion_id] = {
                "criterion_id": criterion_id,
                "criterion": criterion,
                "status": "PASSED" if passed else "FAILED",
                "evidence": json.dumps(replacement_match, ensure_ascii=False) if replacement_match else "No exact replacement proof matched this criterion.",
                "source": "HOST_EXACT_REPLACEMENT",
            }
            continue

        if owner == "HOST_REPOSITORY_REGRESSION":
            passed = bool(regression and regression.get("success"))
            criterion_results[criterion_id] = {
                "criterion_id": criterion_id,
                "criterion": criterion,
                "status": "PASSED" if passed else "FAILED",
                "evidence": (regression or {}).get("test", {}).get("detail") if regression else "Repository-wide regression was approved but no regression check ran.",
                "source": "HOST_REPOSITORY_REGRESSION",
            }
            continue

        if owner == "HOST_DATABASE_NO_EFFECT":
            target = target_by_criterion.get(criterion_id)
            http_check = http_executions.get(target) if target is not None else None
            unchanged = ((http_check or {}).get("observation") or {}).get("db_unchanged") if http_check else None
            passed = bool(http_check and http_check.get("success") and unchanged is True)
            criterion_results[criterion_id] = {
                "criterion_id": criterion_id,
                "criterion": criterion,
                "status": "PASSED" if passed else "FAILED",
                "evidence": f"Isolated live-HTTP database mutation check unchanged: {unchanged}",
                "source": "HOST_DATABASE_NO_EFFECT",
                "http_target": list(target) if target is not None else None,
            }
            continue

        criterion_results[criterion_id] = {
            "criterion_id": criterion_id,
            "criterion": criterion,
            "status": "UNPROVEN",
            "evidence": f"No governed verifier exists for proof owner {owner!r}.",
            "source": "NO_GOVERNED_PROOF",
        }

    unproven_host = [
        item for item in (acceptance_contract.get("criteria") or [])
        if item.get("proof_owner") != "INDEPENDENT_REVIEW"
        and (criterion_results.get(str(item.get("id") or "")) or {}).get("status") != "PASSED"
    ]
    if unproven_host:
        failure_reason = failure_reason or "ACCEPTANCE_EVIDENCE_INCOMPLETE"
        errors.append(
            "Deterministic host proof is incomplete for approved criteria: "
            + "; ".join(item.get("text") or item.get("id") for item in unproven_host)
        )

    success = not errors
    detail = {
        "repository_delta_match": reported == actual,
        "actual_changed_files": actual,
        "changed_file_hashes": changed_file_hashes,
        "repository_delta_hash": repository_delta_hash,
        "reported_changed_files": reported,
        "exact_replacements": replacement_rows,
        "checks": checks,
        "criterion_results": criterion_results,
        "acceptance_contract": acceptance_contract,
        "acceptance_contract_hash": acceptance_contract.get("contract_hash"),
        "host_criterion_coverage_complete": not unproven_host,
        "independent_review_required": any(
            item.get("proof_owner") == "INDEPENDENT_REVIEW"
            for item in (acceptance_contract.get("criteria") or [])
        ),
    }
    host_tests = [
        row.get("test") for row in ([focused] + list(http_executions.values()) + [regression])
        if row and row.get("test")
    ]
    test = {
        "command": "Eason One Engineer host acceptance",
        "status": "PASSED" if success else "FAILED",
        "detail": json.dumps(detail, ensure_ascii=False)[-6000:],
    }
    return {
        "attempted": True,
        "success": success,
        "failure_reason": failure_reason,
        "error": "; ".join(errors) if errors else None,
        "test": test,
        "tests": host_tests,
        "verification_mode": "HOST_REPOSITORY_ACCEPTANCE",
        **detail,
    }
