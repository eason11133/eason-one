import json
from pathlib import Path
from types import SimpleNamespace

from flask import Flask

from eason_one.services import codex_connector


def test_wsl_readiness_requires_version_and_chatgpt_login(monkeypatch):
    monkeypatch.setenv("EASON_ONE_CODEX_TRANSPORT", "wsl")
    monkeypatch.setenv("EASON_ONE_WSL_PATH", r"C:\Windows\System32\wsl.exe")
    monkeypatch.setenv("EASON_ONE_CODEX_WSL_DISTRO", "Ubuntu")
    monkeypatch.setenv("EASON_ONE_CODEX_WSL_PATH", "/home/eason/.local/bin/codex")
    codex_connector._READINESS_CACHE.update({"at": 0.0, "value": None})

    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[-1] == "--version":
            return SimpleNamespace(returncode=0, stdout="codex-cli 0.146.0\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="Logged in using ChatGPT\n", stderr="")

    monkeypatch.setattr(codex_connector.subprocess, "run", fake_run)
    descriptor = codex_connector.codex_runtime_descriptor(force_probe=True)
    assert descriptor["ready"] is True
    assert descriptor["transport"] == "wsl"
    assert descriptor["distro"] == "Ubuntu"
    assert descriptor["path"] == "/home/eason/.local/bin/codex"
    assert calls[0] == [
        r"C:\Windows\System32\wsl.exe", "-d", "Ubuntu", "--",
        "/home/eason/.local/bin/codex", "--version",
    ]
    assert calls[1][-2:] == ["login", "status"]


def test_windows_paths_map_to_wsl_mounts():
    assert codex_connector._to_wsl_path(Path(r"D:\school\eason-one")) == "/mnt/d/school/eason-one"
    assert codex_connector._to_wsl_path(Path(r"C:\Users\eason\AppData\Local\Temp\x.json")) == "/mnt/c/Users/eason/AppData/Local/Temp/x.json"


def test_wsl_execution_uses_linux_codex_and_linux_paths(monkeypatch, tmp_path):
    import io

    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(
        codex_connector,
        "codex_runtime_descriptor",
        lambda force_probe=False: {
            "ready": True,
            "transport": "wsl",
            "host_executable": r"C:\Windows\System32\wsl.exe",
            "distro": "Ubuntu",
            "path": "/home/eason/.local/bin/codex",
        },
    )
    captured = {}

    class FakePopen:
        def __init__(self, command, **kwargs):
            captured["command"] = command
            output_value = command[command.index("-o") + 1]
            if output_value.startswith("/mnt/") and len(output_value) > 7 and output_value[5].isalpha() and output_value[6] == "/":
                drive = output_value[5].upper()
                remainder = output_value[7:].replace("/", "\\")
                output_path = Path(f"{drive}:\\{remainder}")
            else:
                output_path = Path(output_value)
            output_path.write_text(json.dumps({
                "summary": "Read-only evidence collected.",
                "changed_files": [],
                "tests": [{"command": "pwd", "status": "PASSED", "detail": "repo visible"}],
                "acceptance": [{"criterion": "evidence", "status": "PASSED", "evidence": "pwd"}],
                "risks": [],
                "needs_founder": False,
                "founder_reason": None,
            }), encoding="utf-8")
            self.stdout = io.StringIO(
                '{"type":"thread.started","thread_id":"t1"}\n'
                '{"type":"turn.completed","usage":{"input_tokens":10,"output_tokens":20}}\n'
            )
            self.stderr = io.StringIO("")
            self.returncode = 0

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            return self.returncode

        def kill(self):
            self.returncode = -9

    monkeypatch.setattr(codex_connector.subprocess, "Popen", FakePopen)
    app = Flask(__name__)
    app.config["TESTING"] = False
    with app.app_context():
        result = codex_connector._run_subprocess(repo, "bounded job", True)

    command = captured["command"]
    assert command[:6] == [
        r"C:\Windows\System32\wsl.exe", "-d", "Ubuntu", "--",
        "/home/eason/.local/bin/codex", "exec",
    ]
    assert "--ask-for-approval" not in command
    assert "sandbox windows" not in " ".join(command)
    # The Codex session/output surface must be writable. The repository's
    # read-only contract is enforced by before/after delta restoration.
    assert command[command.index("--sandbox") + 1] == "workspace-write"
    assert command[command.index("--cd") + 1] == codex_connector._to_wsl_path(repo)
    assert result.returncode == 0
    assert "Read-only evidence collected" in result.final_output



def test_wsl_readiness_accepts_cli_api_key_auth(monkeypatch):
    monkeypatch.setenv("EASON_ONE_CODEX_TRANSPORT", "wsl")
    monkeypatch.setenv("EASON_ONE_WSL_PATH", r"C:\Windows\System32\wsl.exe")
    monkeypatch.setenv("EASON_ONE_CODEX_WSL_DISTRO", "Ubuntu")
    monkeypatch.setenv("EASON_ONE_CODEX_WSL_PATH", "/home/eason/.local/bin/codex")
    codex_connector._READINESS_CACHE.update({"at": 0.0, "value": None})

    def fake_run(command, **kwargs):
        if command[-1] == "true":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if command[-1] == "--version":
            return SimpleNamespace(returncode=0, stdout="codex-cli 0.150.0\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="Logged in using an API key - sk-...\n", stderr="")

    monkeypatch.setattr(codex_connector.subprocess, "run", fake_run)
    descriptor = codex_connector.codex_runtime_descriptor(force_probe=True)
    assert descriptor["ready"] is True
    assert "API key" in descriptor["login"]


def test_preapproval_repository_readiness_checks_exact_git_boundary(ctx, tmp_path, monkeypatch):
    repo = tmp_path / "project"
    repo.mkdir()
    (repo / ".git").mkdir()
    monkeypatch.setenv("EASON_ONE_CODEX_ALLOWED_REPOS", str(tmp_path))
    readiness = codex_connector.preapproval_repository_readiness(
        constraints=[f"REPO_PATH={repo}"]
    )
    assert readiness["ready"] is True
    assert readiness["path"] == str(repo.resolve())

    not_git = tmp_path / "not-git"
    not_git.mkdir()
    blocked = codex_connector.preapproval_repository_readiness(
        constraints=[f"REPO_PATH={not_git}"]
    )
    assert blocked["ready"] is False
    assert "Git working tree" in blocked["error"]
