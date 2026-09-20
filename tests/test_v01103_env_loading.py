import os
from pathlib import Path

from eason_one.env_loader import load_project_env


def test_project_env_loader_reads_values_without_overriding_process_env(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\ufeff# comment\n"
        "OPENAI_API_KEY=from-file\n"
        "ANTHROPIC_API_KEY=\"quoted-value\"\n"
        "export GEMINI_API_KEY='single-quoted'\n"
        "PERPLEXITY_API_KEY=value-with-#-inside\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-process")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("PERPLEXITY_API_KEY", raising=False)

    metadata = load_project_env(env_file)

    assert metadata["found"] is True
    assert metadata["loaded"] == 3
    assert metadata["preserved"] == 1
    assert os.environ["OPENAI_API_KEY"] == "from-file"
    assert os.environ["ANTHROPIC_API_KEY"] == "from-process"
    assert os.environ["GEMINI_API_KEY"] == "single-quoted"
    assert os.environ["PERPLEXITY_API_KEY"] == "value-with-#-inside"


def test_project_env_loader_is_idempotent(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("OPENAI_API_KEY=first\n", encoding="utf-8")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    first = load_project_env(env_file)
    env_file.write_text("OPENAI_API_KEY=second\n", encoding="utf-8")
    second = load_project_env(env_file)

    assert first["loaded"] == 1
    assert second["loaded"] == 0
    assert second["preserved"] == 1
    assert os.environ["OPENAI_API_KEY"] == "first"
