#!/usr/bin/env python3
"""OtterGate installer — wire SeaOtter's OtterScore critic into a coding harness so
external validation runs automatically at the end of every task.

Run via `seaotter.ai/install.sh` (which downloads the hook + this script first), or
directly once `~/.otter/validate.py` and `~/.otter/standing/*` are present:

    python3 ~/.otter/otter_install.py <claude|codex|openclaw|hermes|cursor|git|all> [opts]

It is stdlib-only and idempotent — re-running updates the same managed blocks in place.
Each harness gets, where its design allows: (a) the MCP `otter_score` tool, (b) an
end-of-task hook that runs the validator and blocks the finish until the work clears the
bar, and (c) a standing-instruction block that makes "validate before done" a habit.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
from pathlib import Path

OTTER_HOME = Path(os.environ.get("OTTER_HOME", str(Path.home() / ".otter")))
HOOK = OTTER_HOME / "validate.py"
STANDING = OTTER_HOME / "standing"
MCP_URL = "https://mcp.seaotter.ai/mcp"
# The env var every harness reads at RUNTIME for the MCP bearer token. The hosted
# gateway accepts a plain `sk-otter-…` key as a bearer (no interactive OAuth), so an
# autonomous agent only needs this one env var exported to USE the otter_score tool.
MCP_BEARER_ENV = "OTTER_API_KEY"
# Authorization header most MCP clients (Claude Code, Cursor, OpenClaw) expand from the
# environment at runtime via ${VAR}. Stored literally so the secret never lands on disk.
MCP_AUTH_HEADER = {"Authorization": f"Bearer ${{{MCP_BEARER_ENV}}}"}
BEGIN, END = "otter:begin external-validation", "otter:end external-validation"


# --------------------------------------------------------------------------- util
def info(msg: str) -> None:
    print(f"  • {msg}")


def py_cmd() -> str:
    return f'python3 "{HOOK}"'


def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def save_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2) + "\n")


def upsert_block(target: Path, block: str) -> str:
    """Insert/replace the managed standing-instruction block in a markdown-ish file,
    matched by the otter:begin/otter:end markers. Returns added|updated|unchanged."""
    block = block.strip("\n")
    existing = target.read_text() if target.exists() else ""
    if BEGIN in existing and END in existing:
        b = existing.index(BEGIN)
        line_start = existing.rfind("\n", 0, b) + 1           # start of the begin-marker line
        e = existing.index(END, b)
        nl = existing.find("\n", e)
        line_end = len(existing) if nl == -1 else nl + 1       # end of the end-marker line
        new = existing[:line_start] + block + "\n" + existing[line_end:]
        if new.strip() == existing.strip():
            return "unchanged"
        target.write_text(new)
        return "updated"
    target.parent.mkdir(parents=True, exist_ok=True)
    sep = "" if not existing else ("\n" if existing.endswith("\n") else "\n\n")
    target.write_text(existing + sep + block + "\n")
    return "added"


def standing(name: str) -> str:
    p = STANDING / name
    if p.exists():
        return p.read_text()
    raise SystemExit(f"otter: missing standing asset {p} — re-run via seaotter.ai/install.sh")


def make_executable(path: Path) -> None:
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def hook_cmd(harness: str, opts: argparse.Namespace) -> str:
    # shell-quote caller-supplied values: the command string is embedded in a git
    # pre-push shell script, a JSON hook entry, and a TOML string — never let a
    # policy id with spaces/metacharacters break (or inject into) any of those.
    extra = ""
    if opts.policy_id:
        extra += f" --policy-id {shlex.quote(opts.policy_id)}"
    if opts.min_band:
        extra += f" --min-band {shlex.quote(opts.min_band)}"
    return f"{py_cmd()} --harness {harness}{extra}"


def _toml_str(s: str) -> str:
    """A TOML basic (double-quoted) string literal that safely encodes any command —
    backslashes and double quotes are escaped per the TOML spec."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def upsert_codex_mcp(cfg: Path) -> str:
    """Write/repair the managed `[mcp_servers.otterscore]` table in Codex's config.toml,
    INCLUDING the bearer auth Codex needs to actually reach the hosted gateway. Codex
    rejects an inline `bearer_token` and requires `bearer_token_env_var`, so we point it
    at OTTER_API_KEY (read at runtime). Self-heals a legacy url-only block written before
    auth was wired — without that, the agent's every otter_score call 401s. Returns
    added|updated|unchanged."""
    begin, end = "# otter:begin mcp otterscore (managed)", "# otter:end mcp otterscore (managed)"
    block = (f'{begin}\n[mcp_servers.otterscore]\nurl = "{MCP_URL}"\n'
             f'bearer_token_env_var = "{MCP_BEARER_ENV}"\n{end}\n')
    text = cfg.read_text() if cfg.exists() else ""
    if begin in text and end in text:                                  # replace managed region
        b = text.index(begin)
        ls = text.rfind("\n", 0, b) + 1
        e = text.index(end) + len(end)
        nl = text.find("\n", e)
        e = len(text) if nl == -1 else nl + 1
        new = text[:ls] + block + text[e:]
        action = "unchanged" if new == text else "updated"
    elif "[mcp_servers.otterscore]" in text:                           # legacy unmarked -> replace table
        hb = text.index("[mcp_servers.otterscore]")
        ls = text.rfind("\n", 0, hb) + 1
        prev = text.rfind("\n", 0, ls - 1) + 1 if ls > 0 else 0        # absorb a leading "# otter:" comment line
        if ls > 0 and text[prev:ls].strip().startswith("# otter:"):
            ls = prev
        ne = text.find("\n[", hb + len("[mcp_servers.otterscore]"))    # table ends at next table or EOF
        te = len(text) if ne == -1 else ne + 1
        new = text[:ls].rstrip("\n") + ("\n\n" if text[:ls].strip() else "") + block + text[te:]
        action = "updated"
    else:                                                              # fresh append
        new = text + ("" if not text or text.endswith("\n") else "\n") + "\n" + block
        action = "added"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(new)
    return action


def _upsert_marked_toml(cfg: Path, begin: str, end: str, block: str) -> str:
    """Insert/replace a marker-delimited region in a TOML file. Returns added|updated|
    unchanged. Coexists with other managed regions (e.g. the MCP block) in the same file."""
    text = cfg.read_text() if cfg.exists() else ""
    if begin in text and end in text:
        b = text.index(begin)
        ls = text.rfind("\n", 0, b) + 1
        e = text.index(end) + len(end)
        nl = text.find("\n", e)
        e = len(text) if nl == -1 else nl + 1
        new = text[:ls] + block + text[e:]
        action = "unchanged" if new == text else "updated"
    else:
        new = text + ("" if not text or text.endswith("\n") else "\n") + "\n" + block
        action = "added"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(new)
    return action


def upsert_codex_hooks(cfg: Path, command: str) -> str:
    """Write/repair a managed `[hooks.Stop]` block in config.toml so Codex runs the
    validator when the agent tries to finish and BLOCKS the finish (hook exit 2) until the
    work ships. This is the inline-hooks schema Codex 0.142 actually loads — the old
    standalone `otter-hooks.toml` used a top-level `[[Stop]]` Codex never reads."""
    begin, end = "# otter:begin hooks (managed)", "# otter:end hooks (managed)"
    block = (f'{begin}\n[[hooks.Stop]]\nmatcher = ""\n\n[[hooks.Stop.hooks]]\n'
             f'type = "command"\ncommand = {_toml_str(command)}\ntimeout = 180\n{end}\n')
    return _upsert_marked_toml(cfg, begin, end, block)


# ------------------------------------------------------------------------- claude
def install_claude(opts: argparse.Namespace) -> None:
    root = Path.home() / ".claude" if opts.is_global else Path(opts.project) / ".claude"
    settings = root / "settings.json"
    cfg = load_json(settings)
    cmd = hook_cmd("claude", opts)
    hooks = cfg.setdefault("hooks", {})
    stop = hooks.setdefault("Stop", [])
    # Idempotent by a STABLE marker (the validator + harness), independent of options
    # like --policy-id, so re-running with different flags REPLACES our hook in place
    # instead of appending a duplicate. Never touches the user's other hooks.
    replaced = False
    for g in stop:
        for h in g.get("hooks", []):
            c = h.get("command", "")
            if "validate.py" in c and "--harness claude" in c:
                h["command"] = cmd
                replaced = True
    if not replaced:
        stop.append({"matcher": "", "hooks": [{"type": "command", "command": cmd, "timeout": 180}]})
    save_json(settings, cfg)
    info(f"Stop hook {'updated' if replaced else 'added'} -> {settings} (validates when Claude tries to finish)")

    # MCP: durable project .mcp.json + best-effort CLI registration.
    mcp_path = (Path.home() / ".mcp.json") if opts.is_global else (Path(opts.project) / ".mcp.json")
    mcp = load_json(mcp_path)
    # Wire the bearer header so a HEADLESS Claude Code agent reaches the gateway without
    # the interactive OAuth dance. Claude Code expands ${OTTER_API_KEY} from the env at
    # runtime, so the secret is never written to .mcp.json.
    mcp.setdefault("mcpServers", {})["otterscore"] = {
        "type": "http", "url": MCP_URL, "headers": dict(MCP_AUTH_HEADER)}
    save_json(mcp_path, mcp)
    info(f"MCP otter_score tool (bearer via ${MCP_BEARER_ENV}) -> {mcp_path}")
    if shutil.which("claude"):
        subprocess.run(["claude", "mcp", "remove", "otterscore"], capture_output=True, text=True)
        subprocess.run(["claude", "mcp", "add", "--transport", "http",
                        "--header", f"Authorization: Bearer ${{{MCP_BEARER_ENV}}}",
                        "otterscore", MCP_URL], capture_output=True, text=True)

    # /otter-validate slash command.
    cmd_md = root / "commands" / "otter-validate.md"
    cmd_md.parent.mkdir(parents=True, exist_ok=True)
    cmd_md.write_text(
        "---\ndescription: Grade the current work with OtterScore and fix any flaws\n---\n\n"
        f"Run SeaOtter's external validation and act on the verdict:\n\n"
        f"!`{cmd} --source ${{1:-diff}}`\n\n"
        "If it reports flaws, fix each one and run this again until the band clears.\n")
    info(f"/otter-validate slash command -> {cmd_md}")

    target = (Path.home() / ".claude" / "CLAUDE.md") if opts.is_global else (Path(opts.project) / "CLAUDE.md")
    info(f"standing instruction ({upsert_block(target, standing('CLAUDE.md'))}) -> {target}")


# -------------------------------------------------------------------------- codex
def install_codex(opts: argparse.Namespace) -> None:
    cfg = Path.home() / ".codex" / "config.toml"
    action = upsert_codex_mcp(cfg)
    info(f"MCP otter_score tool ({action}; bearer via ${MCP_BEARER_ENV}) -> {cfg} [mcp_servers.otterscore]")

    # Remove the historical, malformed otter-hooks.toml: it used a top-level [[Stop]]
    # in a standalone file that Codex never loads, so it enforced nothing.
    legacy = Path.home() / ".codex" / "otter-hooks.toml"
    if legacy.exists():
        legacy.unlink()
        info("removed stale, non-functional otter-hooks.toml (wrong schema; never loaded)")

    if opts.enforce:
        h_action = upsert_codex_hooks(cfg, hook_cmd("codex", opts))
        info(f"HARD gate: blocking [hooks.Stop] ({h_action}) -> {cfg} — Codex can't finish "
             "until OtterScore ships")
        info("  approve it once in interactive `codex`, or run autonomous lanes with "
             "`codex exec --dangerously-bypass-hook-trust`.")
    else:
        info("for a HARD end-of-task gate (Codex blocks the finish until the work ships), "
             "re-run: otter install codex --enforce")

    target = (Path.home() / ".codex" / "AGENTS.md") if opts.is_global else (Path(opts.project) / "AGENTS.md")
    info(f"standing instruction ({upsert_block(target, standing('AGENTS.md'))}) -> {target}")


# ----------------------------------------------------------------------- openclaw
def install_openclaw(opts: argparse.Namespace) -> None:
    cfg = Path.home() / ".openclaw" / "openclaw.json"
    obj = load_json(cfg)
    agents = obj.setdefault("agents", {}).setdefault("defaults", {})
    agents.setdefault("mcpServers", {})["otterscore"] = {"url": MCP_URL, "headers": dict(MCP_AUTH_HEADER)}
    save_json(cfg, obj)
    info(f"MCP otter_score tool (bearer via ${MCP_BEARER_ENV}) -> {cfg} (agents.defaults.mcpServers)")

    ws = Path.home() / ".openclaw" / "workspace"
    for name in ("SOUL.md", "AGENTS.md"):
        target = ws / name
        info(f"standing instruction ({upsert_block(target, standing(name))}) -> {target}")
    info("advanced: a native `agent_end` plugin template is available at "
         "seaotter.ai/otter/openclaw/ (openclaw.plugin.json + plugin.ts) for hard enforcement.")


# ------------------------------------------------------------------------- hermes
def install_hermes(opts: argparse.Namespace) -> None:
    out = Path(opts.project) / "otter"
    out.mkdir(parents=True, exist_ok=True)
    shutil.copy(STANDING / "otter-tools.json", out / "otter-tools.json")
    shutil.copy(STANDING / "hermes-system-prompt.txt", out / "otter-system-prompt.txt")
    shutil.copy(HOOK, out / "validate.py")
    info(f"OpenAI tools spec + system-prompt fragment + grader -> {out}/")
    print("\n  Hermes wiring (no hook system — wire it in your agent loop):")
    print("    1. Load otter/otter-tools.json into your chat.completions `tools=[...]`.")
    print("    2. Prepend otter/otter-system-prompt.txt to your system prompt.")
    print("    3. When the model emits an otter_grade tool_call, run the grader and")
    print("       return its verdict as the tool result:")
    print(f"         echo \"$WORK\" | python3 {out}/validate.py --source stdin --strict")
    print("       (exit 0 = ship; exit 2 = blocked, with flaws on stderr).")


# ------------------------------------------------------------------------- cursor
def install_cursor(opts: argparse.Namespace) -> None:
    proj = Path(opts.project)
    mcp_path = proj / ".cursor" / "mcp.json"
    mcp = load_json(mcp_path)
    mcp.setdefault("mcpServers", {})["otterscore"] = {"url": MCP_URL, "headers": dict(MCP_AUTH_HEADER)}
    save_json(mcp_path, mcp)
    info(f"MCP otter_score tool (bearer via ${MCP_BEARER_ENV}) -> {mcp_path}")
    rule = proj / ".cursor" / "rules" / "otter-validate.mdc"
    rule.parent.mkdir(parents=True, exist_ok=True)
    body = standing("AGENTS.md")
    rule.write_text("---\ndescription: External validation before finishing\nalwaysApply: true\n---\n\n" + body)
    info(f"always-on rule -> {rule}")


# ---------------------------------------------------------------------------- git
def install_git(opts: argparse.Namespace) -> None:
    proj = Path(opts.project)
    git_dir = proj / ".git"
    if not git_dir.is_dir():
        raise SystemExit(f"otter: {proj} is not a git repo — run inside one, or use --project DIR")
    hook = git_dir / "hooks" / "pre-push"
    hook.parent.mkdir(parents=True, exist_ok=True)
    line = f'{hook_cmd("git", opts)} --strict --source diff || exit 1'
    if hook.exists() and "validate.py" in hook.read_text():
        info(f"pre-push gate already present -> {hook}")
    else:
        prefix = hook.read_text() if hook.exists() else "#!/bin/sh\n"
        if not prefix.startswith("#!"):
            prefix = "#!/bin/sh\n" + prefix
        hook.write_text(prefix.rstrip("\n") + "\n# otter: block a push whose work fails OtterScore\n" + line + "\n")
        make_executable(hook)
        info(f"pre-push gate -> {hook} (blocks a push whose diff fails the bar)")
    target = proj / "AGENTS.md"
    info(f"standing instruction ({upsert_block(target, standing('AGENTS.md'))}) -> {target}")


INSTALLERS = {
    "claude": install_claude, "codex": install_codex, "openclaw": install_openclaw,
    "hermes": install_hermes, "cursor": install_cursor, "git": install_git,
}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="otter install")
    p.add_argument("harness", choices=[*INSTALLERS, "all"])
    p.add_argument("--global", dest="is_global", action="store_true",
                   help="install at the user level (~/.claude, ~/.codex) instead of the project")
    p.add_argument("--project", default=os.getcwd(), help="project dir (default: cwd)")
    p.add_argument("--policy-id", default=os.environ.get("OTTER_POLICY_ID"))
    p.add_argument("--min-band", default=None, choices=["ship", "route_to_fix"])
    p.add_argument("--enforce", action="store_true",
                   help="wire a HARD blocking end-of-task gate where the harness needs it "
                        "to be opt-in (Codex [hooks.Stop]); Claude's Stop hook and the git "
                        "pre-push gate already block by default")
    opts = p.parse_args(argv)

    if not HOOK.exists():
        raise SystemExit(f"otter: {HOOK} not found — install via seaotter.ai/install.sh")

    targets = list(INSTALLERS) if opts.harness == "all" else [opts.harness]
    print(f"OtterGate — wiring automatic external validation into: {', '.join(targets)}")
    for name in targets:
        print(f"\n[{name}]")
        try:
            INSTALLERS[name](opts)
        except SystemExit as exc:
            print(f"  ! {exc}")
        except Exception as exc:  # noqa: BLE001
            print(f"  ! {name}: {exc}")

    key = os.environ.get("OTTER_API_KEY") or os.environ.get("SEAOTTER_API_KEY")
    print("\nDone. One key powers BOTH the end-of-task hook AND the otter_score MCP tool.")
    print("It must be exported in the environment your AGENT runs in (the harness reads it")
    print("at runtime), so add it to your shell profile to make it stick:")
    if key:
        print(f"  ✓ OTTER_API_KEY is set ({key[:12]}…).")
    else:
        print("    export OTTER_API_KEY=$(curl -s https://api.seaotter.ai/api/v1/agent-keys/signup \\")
        print("      -H 'content-type: application/json' -d '{\"email\":\"you@example.com\"}' \\")
        print("      | python3 -c 'import sys,json;print(json.load(sys.stdin)[\"api_key\"])')")
    print("\nFrom now on, your agent validates its work with the hostile OtterScore critic")
    print("before it can call a task done — via the hook automatically, or the otter_score")
    print("tool any time. Verify:  python3 ~/.otter/validate.py --help")
    return 0


if __name__ == "__main__":
    sys.exit(main())
