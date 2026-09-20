from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

_ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _decode_value(raw: str) -> str:
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        inner = value[1:-1]
        if value[0] == '"':
            inner = (
                inner.replace(r"\\", "\0")
                .replace(r"\n", "\n")
                .replace(r"\r", "\r")
                .replace(r"\t", "\t")
                .replace(r'\"', '"')
                .replace("\0", "\\")
            )
        return inner

    # Match common .env behavior without treating a # inside a key/value as a
    # comment unless whitespace precedes it.
    value = re.split(r"\s+#", value, maxsplit=1)[0]
    return value.strip()


def project_env_path() -> Path:
    configured = (os.getenv("EASON_ONE_ENV_FILE") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(__file__).resolve().parents[1] / ".env"


def load_project_env(path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """Load the repository .env without exposing or overriding secrets.

    Process-level environment variables remain authoritative. The function is
    idempotent and intentionally returns metadata only, never secret values.
    """
    env_path = Path(path).expanduser().resolve() if path else project_env_path()
    if not env_path.is_file():
        return {"found": False, "path": str(env_path), "loaded": 0, "preserved": 0}

    loaded = 0
    preserved = 0
    for index, raw_line in enumerate(env_path.read_text(encoding="utf-8-sig").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if not _ENV_KEY.fullmatch(key):
            continue
        if key in os.environ:
            preserved += 1
            continue
        os.environ[key] = _decode_value(raw_value)
        loaded += 1

    return {"found": True, "path": str(env_path), "loaded": loaded, "preserved": preserved}
