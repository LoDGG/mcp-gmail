"""Unattended wrapper tests without Gmail or Gemini calls."""

import os
from pathlib import Path
import shutil
import subprocess

import pytest

from gmail_agent import gemini, gemini_classifier


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/run_triage.sh"


@pytest.mark.parametrize("line", ["GEMINI_API_KEY=chosen", "GEMINI_API_KEY='chosen'", 'GEMINI_API_KEY="chosen"'])
def test_wrapper_loads_key_and_removes_unrelated_google_key(tmp_path, line):
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / ".secrets").mkdir()
    shutil.copy2(SCRIPT, repo / "scripts/run_triage.sh")
    (repo / ".secrets/gemini.env").write_text(line + "\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_uv = bin_dir / "uv"
    fake_uv.write_text("#!/usr/bin/env bash\n[[ $GEMINI_API_KEY == chosen && ! ${GOOGLE_API_KEY+x} ]]\n")
    fake_uv.chmod(0o755)
    env = {**os.environ, "HOME": str(tmp_path), "PATH": f"{bin_dir}:{os.environ['PATH']}", "GOOGLE_API_KEY": "wrong-secret", "GEMINI_API_KEY": "stale-secret"}
    result = subprocess.run(["bash", str(repo / "scripts/run_triage.sh")], env=env, capture_output=True, text=True)
    log = (repo / ".local/triage.log").read_text()
    assert result.returncode == 0
    assert "chosen" not in log and "wrong-secret" not in log and "stale-secret" not in log


@pytest.mark.parametrize("contents", ["", "GEMINI_API_KEY=''\n", "OTHER=value\n"])
def test_wrapper_rejects_missing_key_without_logging_secrets(tmp_path, contents):
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / ".secrets").mkdir()
    shutil.copy2(SCRIPT, repo / "scripts/run_triage.sh")
    (repo / ".secrets/gemini.env").write_text(contents)
    env = {**os.environ, "GOOGLE_API_KEY": "wrong-secret", "GEMINI_API_KEY": "stale-secret"}
    result = subprocess.run(["bash", str(repo / "scripts/run_triage.sh")], env=env, capture_output=True, text=True)
    log = (repo / ".local/triage.log").read_text()
    assert result.returncode != 0
    assert "GEMINI_API_KEY is missing" in log
    assert "wrong-secret" not in log and "stale-secret" not in log


@pytest.mark.parametrize("module,constructor", [
    (gemini, gemini.GeminiProvider),
    (gemini_classifier, gemini_classifier.GeminiBatchClassifier),
])
def test_client_receives_explicit_gemini_key(monkeypatch, module, constructor):
    received = []
    monkeypatch.setenv("GEMINI_API_KEY", "chosen")
    monkeypatch.setenv("GOOGLE_API_KEY", "wrong-secret")
    monkeypatch.setattr(module.genai, "Client", lambda **kwargs: received.append(kwargs) or object())
    constructor()
    assert received == [{"api_key": "chosen"}]
