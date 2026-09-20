import json
from pathlib import Path

from eason_one.services import codex_connector


def _clear_cache():
    codex_connector._READINESS_CACHE["at"] = 0.0
    codex_connector._READINESS_CACHE["value"] = None


def test_persisted_codex_readiness_never_spawns_subprocess(ctx, tmp_path, monkeypatch):
    snapshot = tmp_path / "codex-readiness.json"
    snapshot.write_text(json.dumps({
        "transport": "wsl",
        "ready": True,
        "version": "codex-cli 0.146.0",
        "login": "Logged in using ChatGPT",
        "error": None,
        "distro": "Ubuntu",
        "path": "/home/eason/.local/bin/codex",
        "display_path": "wsl.exe -d Ubuntu -- /home/eason/.local/bin/codex",
        "cache_key": "wsl|Ubuntu|/home/eason/.local/bin/codex",
        "checked_at": "2026-08-04T14:00:00+00:00",
    }), encoding="utf-8")
    monkeypatch.setenv("EASON_ONE_CODEX_TRANSPORT", "wsl")
    monkeypatch.setenv("EASON_ONE_CODEX_WSL_DISTRO", "Ubuntu")
    monkeypatch.setenv("EASON_ONE_CODEX_WSL_PATH", "/home/eason/.local/bin/codex")
    monkeypatch.setenv("EASON_ONE_CODEX_READINESS_FILE", str(snapshot))
    monkeypatch.setattr(
        codex_connector.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("subprocess must not run")),
    )
    _clear_cache()
    descriptor = codex_connector.codex_runtime_descriptor()
    assert descriptor["ready"] is True
    assert descriptor["source"] == "persisted"


def test_founder_pages_do_not_probe_wsl(client, ctx, tmp_path, monkeypatch):
    snapshot = tmp_path / "codex-readiness.json"
    snapshot.write_text(json.dumps({
        "transport": "wsl",
        "ready": True,
        "version": "codex-cli 0.146.0",
        "login": "Logged in using ChatGPT",
        "error": None,
        "distro": "Ubuntu",
        "path": "/home/eason/.local/bin/codex",
        "display_path": "wsl.exe -d Ubuntu -- /home/eason/.local/bin/codex",
        "cache_key": "wsl|Ubuntu|/home/eason/.local/bin/codex",
        "checked_at": "2026-08-04T14:00:00+00:00",
    }), encoding="utf-8")
    monkeypatch.setenv("EASON_ONE_CODEX_TRANSPORT", "wsl")
    monkeypatch.setenv("EASON_ONE_CODEX_WSL_DISTRO", "Ubuntu")
    monkeypatch.setenv("EASON_ONE_CODEX_WSL_PATH", "/home/eason/.local/bin/codex")
    monkeypatch.setenv("EASON_ONE_CODEX_READINESS_FILE", str(snapshot))
    monkeypatch.setattr(
        codex_connector.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("Founder UI launched WSL")),
    )
    _clear_cache()
    for path in ("/headquarters", "/headquarters/missions", "/headquarters/people"):
        assert client.get(path).status_code == 200


def test_testing_fallback_remains_nonblocking_without_explicit_snapshot(ctx, monkeypatch):
    monkeypatch.delenv("EASON_ONE_CODEX_READINESS_FILE", raising=False)
    monkeypatch.setattr(
        codex_connector.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("subprocess must not run")),
    )
    _clear_cache()
    descriptor = codex_connector.codex_runtime_descriptor()
    assert descriptor["ready"] is True
    assert descriptor["source"] == "testing"
