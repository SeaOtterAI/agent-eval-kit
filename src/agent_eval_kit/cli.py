"""agent-eval — make external validation an automatic, everyday habit in your harness.

    agent-eval init <claude|codex|openclaw|cursor|hermes|git|all> [--global] [--project DIR]
                    [--enforce] [--policy-id ID] [--min-band ship|route_to_fix]
        Wire OtterScore into the harness's MCP tool (bearer-auth'd) + end-of-task hook +
        a standing instruction, so the work an agent produces is graded and the finish is
        blocked until it passes. --enforce adds Codex's blocking [hooks.Stop] gate.

    agent-eval validate [--source diff|files|stdin] [--files F...] [--policy-id ID]
                        [--min-band ship|route_to_fix] [--strict]
        Grade work NOW with the hostile critic. Exit 0 = ship, exit 2 = blocked (flaws on
        stderr). Reads the git diff / named files / a harness hook payload on stdin.

    agent-eval mcp        Run the MCP server (stdio) so an MCP client gets the otter_score tools.
    agent-eval version    Print the version.

It needs a key — set OTTER_API_KEY (get one free:
  curl -s https://api.seaotter.ai/api/v1/agent-keys/signup -H 'content-type: application/json' \
    -d '{"email":"you@example.com"}').
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

HARNESS_DIR = Path(__file__).parent / "harness"
OTTER_HOME = Path(os.environ.get("OTTER_HOME", str(Path.home() / ".otter")))


def _ensure_assets() -> None:
    """Materialize the bundled, canonical hook + installer + standing templates into
    OTTER_HOME so `init`/`validate` behave identically to seaotter.ai/install.sh."""
    (OTTER_HOME / "standing").mkdir(parents=True, exist_ok=True)
    shutil.copy(HARNESS_DIR / "validate.py", OTTER_HOME / "validate.py")
    shutil.copy(HARNESS_DIR / "otter_install.py", OTTER_HOME / "otter_install.py")
    for f in (HARNESS_DIR / "standing").iterdir():
        if f.is_file():
            shutil.copy(f, OTTER_HOME / "standing" / f.name)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    cmd, rest = argv[0], argv[1:]

    if cmd == "version":
        from agent_eval_kit import __version__
        print(__version__)
        return 0
    if cmd == "validate":
        _ensure_assets()
        return subprocess.call([sys.executable, str(OTTER_HOME / "validate.py"), *rest])
    if cmd == "init":
        _ensure_assets()
        env = {**os.environ, "OTTER_HOME": str(OTTER_HOME)}
        return subprocess.call([sys.executable, str(OTTER_HOME / "otter_install.py"), *rest], env=env)
    if cmd == "mcp":
        from agent_eval_kit.mcp_server import main as mcp_main
        mcp_main()
        return 0

    print(f"agent-eval: unknown command {cmd!r}\n", file=sys.stderr)
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
