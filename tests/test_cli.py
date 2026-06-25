"""agent-eval CLI tests — no network. They exercise init (offline file wiring) and the
validator's fail-open behaviour (no key / nothing to grade => exit 0, never wedges)."""

from __future__ import annotations

import json
import subprocess

import pytest

from agent_eval_kit import cli


@pytest.fixture
def otter_home(tmp_path, monkeypatch):
    home = tmp_path / "otter"
    monkeypatch.setattr(cli, "OTTER_HOME", home)
    return home


def test_help_and_version(capsys):
    assert cli.main(["--help"]) == 0
    assert cli.main(["version"]) == 0
    out = capsys.readouterr().out
    assert out.strip()  # version printed


def test_unknown_command():
    assert cli.main(["frobnicate"]) == 2


def test_ensure_assets_materializes_bundled_scripts(otter_home):
    cli._ensure_assets()
    assert (otter_home / "validate.py").exists()
    assert (otter_home / "otter_install.py").exists()
    assert (otter_home / "standing" / "CLAUDE.md").exists()
    assert (otter_home / "standing" / "AGENTS.md").exists()


def test_init_claude_writes_hook_and_standing(otter_home, tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    rc = cli.main(["init", "claude", "--project", str(proj)])
    assert rc == 0
    settings = json.loads((proj / ".claude" / "settings.json").read_text())
    cmds = [h["command"] for g in settings["hooks"]["Stop"] for h in g["hooks"]]
    assert any("validate.py" in c and "--harness claude" in c for c in cmds)
    mcp = json.loads((proj / ".mcp.json").read_text())
    assert mcp["mcpServers"]["otterscore"]["url"].startswith("https://mcp.seaotter.ai")
    assert "otter:begin external-validation" in (proj / "CLAUDE.md").read_text()


def test_init_git_writes_pre_push(otter_home, tmp_path):
    proj = tmp_path / "repo"
    proj.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=proj, check=True)
    assert cli.main(["init", "git", "--project", str(proj)]) == 0
    hook = proj / ".git" / "hooks" / "pre-push"
    assert hook.exists() and "validate.py" in hook.read_text()


def test_validate_fail_open_without_key(otter_home, tmp_path, monkeypatch):
    # No key + nothing to grade => fail open (exit 0), never wedges the agent.
    monkeypatch.delenv("OTTER_API_KEY", raising=False)
    monkeypatch.delenv("SEAOTTER_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["validate", "--source", "diff"]) == 0


def test_init_idempotent(otter_home, tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    cli.main(["init", "claude", "--project", str(proj)])
    cli.main(["init", "claude", "--project", str(proj)])
    settings = json.loads((proj / ".claude" / "settings.json").read_text())
    assert len(settings["hooks"]["Stop"]) == 1  # no duplicate hook entries
