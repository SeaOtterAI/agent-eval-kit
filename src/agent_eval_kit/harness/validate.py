#!/usr/bin/env python3
"""OtterGate — the everyday external-validation hook.

This is the one script that turns "seek external validation" from a thing an
agent *might* remember into a thing that happens *automatically, every task*.
It is wired into a coding harness's end-of-task hook (Claude Code `Stop`, Codex
`AfterAgent`/`notify`, OpenClaw `agent_end`, or a git `pre-push`) by
`seaotter.ai/install.sh`. When the agent tries to finish, this runs:

    1. figure out the WORK the task produced (the git diff, the files you name,
       the harness's last message, or raw stdin),
    2. grade it with SeaOtter's hostile-by-default OtterScore critic over the
       cold-start-tolerant async eval API,
    3. if the verdict clears the bar (band=ship by default) -> exit 0, the agent
       finishes,
    4. if it does NOT clear the bar -> print the located flaws and BLOCK
       (exit 2 + reason on stderr), which every supported harness feeds back into
       the model so it fixes the flaws and re-validates.

Design choices that make it safe to leave on every day:
  * STDLIB ONLY (urllib/json/subprocess) — no pip install, runs on any python3.
  * FAIL-OPEN on infra/config problems (no key, critic warming, network down,
    nothing to grade) so a hiccup never wedges the agent. Pass --strict (or
    OTTER_STRICT=1) to fail-closed instead.
  * FAIL-CLOSED on a real verdict — a genuine route_to_fix/quarantine/block is
    the whole point, so that blocks.
  * LOOP-SAFE — the same failing diff is blocked at most OTTER_MAX_BLOCKS times
    (default 3) in a row, then allowed with a note, so a critic the agent truly
    cannot satisfy never traps it in an infinite stop loop.

Env (all overridable by flags):
  OTTER_API_KEY / SEAOTTER_API_KEY   sk-otter-... bearer (get one:
        curl -s https://api.seaotter.ai/api/v1/agent-keys/signup \
          -H 'content-type: application/json' -d '{"email":"you@example.com"}')
  OTTER_API_BASE     default https://api.seaotter.ai
  OTTER_POLICY_ID    grade against YOUR stored acceptance policy
  OTTER_RUBRIC_ID    per-modality rubric id
  OTTER_MIN_BAND     ship (default) | route_to_fix  — the bar to clear
  OTTER_TIMEOUT      seconds to wait for a verdict (default 120; warming-tolerant)
  OTTER_STRICT       1 = fail-closed on infra/config errors too
  OTTER_MAX_BLOCKS   consecutive blocks of the same diff before allowing (default 3)
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import mimetypes
import os
import select
import subprocess
import sys
import time
import urllib.error
import urllib.request

DEFAULT_BASE = "https://api.seaotter.ai"
PASSING = ("ship", "accept", "pass")  # bands that always clear
# Ordered weakest->strongest "block" so --min-band route_to_fix lets route_to_fix pass.
BAND_ORDER = ("block", "quarantine", "route_to_fix", "ship")
# Single source of truth for "the job finished and graded" (vs failed/cancelled).
TERMINAL_SUCCESS = ("completed", "succeeded")
TERMINAL_STATES = (*TERMINAL_SUCCESS, "failed", "errored", "error", "cancelled")

# Extension -> modality, mirroring the eval API's detector, so `--files` grades an
# image as an image, a deck as a deck, a sheet as a sheet — every file modality.
_EXT_MODALITY = {
    ".md": "text", ".txt": "text", ".rst": "text", ".csv": "spreadsheet",
    ".py": "code", ".js": "code", ".ts": "code", ".tsx": "code", ".jsx": "code",
    ".go": "code", ".rs": "code", ".java": "code", ".rb": "code", ".c": "code",
    ".cc": "code", ".cpp": "code", ".h": "code", ".sh": "code", ".sql": "code",
    ".diff": "code", ".patch": "code", ".json": "code", ".yaml": "code", ".yml": "code",
    ".png": "image", ".jpg": "image", ".jpeg": "image", ".gif": "image",
    ".webp": "image", ".bmp": "image", ".svg": "image",
    ".mp4": "video", ".mov": "video", ".webm": "video", ".mkv": "video",
    ".mp3": "audio", ".wav": "audio", ".m4a": "audio", ".flac": "audio", ".ogg": "audio",
    ".pdf": "document", ".docx": "document", ".doc": "document",
    ".pptx": "deck", ".ppt": "deck", ".key": "deck",
    ".xlsx": "spreadsheet", ".xls": "spreadsheet",
}
# modalities that win over plain text when several files are graded together.
_MODALITY_RANK = ("outcome_metric", "video", "audio", "image", "deck",
                  "spreadsheet", "document", "code", "text")


# --------------------------------------------------------------------------- io
def _eprint(*a: object) -> None:
    print(*a, file=sys.stderr)


def _read_stdin_if_any(timeout: float = 0.25) -> str:
    """Read stdin only if data is already waiting, so we never block a hook that
    was invoked without a payload (or interactively)."""
    if sys.stdin is None or sys.stdin.closed or sys.stdin.isatty():
        return ""
    try:
        ready, _, _ = select.select([sys.stdin], [], [], timeout)
    except (ValueError, OSError):
        return ""
    if not ready:
        return ""
    try:
        return sys.stdin.read()
    except Exception:  # noqa: BLE001
        return ""


def _git(args: list[str], cwd: str | None) -> str:
    try:
        out = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=20
        )
        return out.stdout if out.returncode == 0 else ""
    except Exception:  # noqa: BLE001
        return ""


# ----------------------------------------------------------------- gather work
def _harness_payload(raw: str) -> dict:
    """Parse a harness hook payload if stdin carried one (Claude Code Stop hook,
    Codex AfterAgent, etc.). Returns {} for non-JSON stdin (e.g. git ref lines)."""
    raw = (raw or "").strip()
    if not raw or raw[0] not in "{[":
        return {}
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else {}
    except json.JSONDecodeError:
        return {}


def _modality_for(path: str) -> str:
    return _EXT_MODALITY.get(os.path.splitext(path)[1].lower(), "")


def _text_part(text: str, name: str) -> dict:
    return {"mime_type": "text/plain", "text": text, "logical_name": name}


def _file_part(path: str) -> tuple[str, dict] | None:
    """Read a file into an artifact_part. Text-ish files become a `text` part;
    everything else becomes a base64 `data_b64` part the eval API renders —
    images, video, audio, pdf, docx, pptx, xlsx all flow through here."""
    path = os.path.expanduser(path)
    name = os.path.basename(path)
    modality = _modality_for(path) or "document"
    mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
    # Read as text when the content is genuinely text: a text/code file, or a
    # text-mime spreadsheet like CSV. Binary docs (pdf/xlsx/pptx) take the b64 path.
    as_text = modality in ("text", "code") or (modality == "spreadsheet" and mime.startswith("text/"))
    if as_text:
        try:
            with open(path, errors="replace") as fh:
                return modality, {"mime_type": mime if mime.startswith("text/") else "text/plain",
                                  "text": fh.read(), "logical_name": name}
        except OSError as exc:
            _eprint(f"otter: cannot read {path}: {exc}")
            return None
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError as exc:
        _eprint(f"otter: cannot read {path}: {exc}")
        return None
    return modality, {"mime_type": mime, "data_b64": base64.b64encode(data).decode(),
                      "logical_name": name}


def _dominant(modalities: list[str]) -> str:
    for m in _MODALITY_RANK:
        if m in modalities:
            return m
    return "text"


def gather_work(args: argparse.Namespace, payload: dict) -> tuple[list[dict], str, str]:
    """Return (artifact_parts, modality, label). Empty parts => nothing to grade."""
    # 1) explicit files win — any modality, mixed sets allowed.
    if args.files:
        parts: list[dict] = []
        mods: list[str] = []
        for path in args.files:
            got = _file_part(path)
            if got:
                mods.append(got[0])
                parts.append(got[1])
        if not parts:
            return [], (args.modality or "text"), "files"
        modality = args.modality or _dominant(mods)
        label = parts[0]["logical_name"] if len(parts) == 1 else f"{len(parts)} files"
        return parts, modality, label

    # 2) raw stdin text the caller asked us to grade.
    if args.source == "stdin":
        txt = args.stdin_text or ""
        return ([_text_part(txt, "stdin")] if txt.strip() else []), (args.modality or "text"), "stdin"

    cwd = payload.get("cwd") or args.cwd or os.getcwd()

    # 3) the git diff this task produced — the natural "work" for a coding harness.
    if args.source in ("auto", "diff"):
        diff = _git(["diff", "--no-color", "HEAD"], cwd)
        if not diff.strip():
            diff = _git(["diff", "--no-color", "--staged"], cwd)
        if diff.strip():
            head = _git(["log", "-1", "--format=%h %s"], cwd).strip()
            label = f"git diff @ {head}" if head else "git diff"
            return [_text_part(diff, "task.diff")], (args.modality or "code"), label

    # 4) fall back to the harness's last assistant message, if it gave us one.
    last = (
        payload.get("last_assistant_message")
        or payload.get("last_agent_message")
        or (payload.get("hook_event") or {}).get("last_assistant_message")
        or ""
    )
    if isinstance(last, str) and last.strip():
        return [_text_part(last, "message.txt")], (args.modality or "text"), "last message"

    return [], (args.modality or "text"), "nothing"


# -------------------------------------------------------------------- grading
def _http(method: str, url: str, key: str, body: dict | None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {key}")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode() or "{}")
        except Exception:  # noqa: BLE001
            payload = {}
        return exc.code, payload
    except Exception as exc:  # noqa: BLE001
        return 0, {"error": str(exc)}


def _cap_parts(parts: list[dict]) -> list[dict]:
    """Trim oversized text parts so a huge diff doesn't bloat the request."""
    out = []
    for p in parts[:16]:
        if isinstance(p.get("text"), str) and len(p["text"]) > 200_000:
            p = {**p, "text": p["text"][:200_000] + "\n…[truncated]"}
        out.append(p)
    return out


def grade(parts: list[dict], modality: str, label: str, args: argparse.Namespace) -> dict:
    """Submit work to the async eval API and poll for a verdict.

    Returns one of:
      {"ok": True, "band", "score", "flaws":[...], "run_id"}        — graded
      {"ok": False, "soft": True, "reason"}                          — fail-open
    """
    base = (args.api_base or os.environ.get("OTTER_API_BASE") or DEFAULT_BASE).rstrip("/")
    # Never send the API key + your work to a plaintext endpoint. https anywhere
    # (api.seaotter.ai, dev-api, self-hosted/BYOC) is fine; http is only allowed to
    # localhost for local testing. Blocks an OTTER_API_BASE=http://evil downgrade.
    if not (base.startswith("https://")
            or base.startswith(("http://localhost", "http://127.0.0.1"))):
        return {"ok": False, "soft": True,
                "reason": f"refusing to send your key to a non-HTTPS endpoint: {base}"}
    body: dict = {
        "submission": "async",
        "modality": modality,
        "user_prompt": (args.intent or "Validate this task's output before it ships.")[:4000],
        "artifact_parts": _cap_parts(parts),
    }
    if args.policy_id:
        body["policy_id"] = args.policy_id
    if args.rubric_id:
        body["rubric_id"] = args.rubric_id

    status, job = _http("POST", f"{base}/api/v1/eval/jobs", args.key, body)
    if status not in (200, 201, 202) or not job.get("job_id"):
        return {"ok": False, "soft": True,
                "reason": f"could not submit grade (HTTP {status}: {job.get('error') or job})"}

    job_id = job["job_id"]
    deadline = time.monotonic() + float(args.timeout)
    delay = 1.5
    while time.monotonic() < deadline:
        st, polled = _http("GET", f"{base}/api/v1/eval/jobs/{job_id}", args.key, None)
        state = str(polled.get("status") or "").lower()
        if state in TERMINAL_STATES:
            job = polled
            break
        time.sleep(min(delay, max(0.5, deadline - time.monotonic())))
        delay = min(delay * 1.4, 8.0)
    else:
        return {"ok": False, "soft": True,
                "reason": f"critic did not return within {args.timeout}s "
                          "(likely a cold-start warmup); allowing this stop"}

    if str(job.get("status") or "").lower() not in TERMINAL_SUCCESS:
        return {"ok": False, "soft": True,
                "reason": f"grade did not complete ({job.get('status')}: {job.get('error')})"}

    summary = job.get("result_summary") or {}
    band = str(summary.get("band") or summary.get("decision") or "route_to_fix").lower()
    score = summary.get("score")
    run_id = job.get("run_id")
    flaws: list[dict] = []
    if run_id:
        _st, run = _http("GET", f"{base}/api/v1/eval/runs/{run_id}", args.key, None)
        flaws = [f for f in (run.get("flaws") or []) if isinstance(f, dict)]
    return {"ok": True, "band": band, "score": score, "flaws": flaws, "run_id": run_id, "base": base}


# ----------------------------------------------------------------- loop safety
def _state_path(cwd: str) -> str:
    d = os.path.join(cwd, ".otter")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        d = cwd
    return os.path.join(d, "validate-state.json")


def _consecutive_blocks(cwd: str, work_hash: str) -> int:
    try:
        with open(_state_path(cwd)) as fh:
            st = json.load(fh)
        return int(st.get("count", 0)) if st.get("hash") == work_hash else 0
    except Exception:  # noqa: BLE001
        return 0


def _record(cwd: str, work_hash: str, count: int) -> None:
    try:
        with open(_state_path(cwd), "w") as fh:
            json.dump({"hash": work_hash, "count": count, "ts": int(time.time())}, fh)
    except OSError:
        pass


# ----------------------------------------------------------------- block forms
def _band_clears(band: str, min_band: str) -> bool:
    if band in PASSING:
        return True
    try:
        return BAND_ORDER.index(band) >= BAND_ORDER.index(min_band)
    except ValueError:
        # An unknown band from the API → fail safe (block) and surface the anomaly.
        _eprint(f"otter: warning — unrecognized band from the critic: {band!r}; treating as below the bar.")
        return False


def _flaw_line(f: dict) -> str:
    sev = str(f.get("severity") or "").upper()
    crit = f.get("criterion") or f.get("name") or ""
    detail = f.get("detail") or f.get("evidence") or f.get("description") or ""
    anchor = f.get("anchor") or {}
    loc = ""
    if isinstance(anchor, dict):
        loc = anchor.get("span") or anchor.get("path") or anchor.get("line") or ""
    head = " · ".join(x for x in [sev, str(crit)] if x)
    tail = f" [{loc}]" if loc else ""
    return f"  - {head}: {detail}{tail}".rstrip()


def block_reason(res: dict, min_band: str) -> str:
    score = res.get("score")
    sc = f" (score {score})" if isinstance(score, (int, float)) else ""
    lines = [
        f"⛔ OtterScore says NOT ready to ship — band={res['band'].upper()}{sc}, "
        f"your bar is {min_band}.",
        "External validation BLOCKED this finish. Fix the flaws below, then it "
        "re-validates automatically:",
    ]
    flaws = res.get("flaws") or []
    if flaws:
        lines += [_flaw_line(f) for f in flaws[:12]]
        if len(flaws) > 12:
            lines.append(f"  …and {len(flaws) - 12} more.")
    else:
        lines.append("  (no located flaws returned; the band itself is below your bar.)")
    if res.get("run_id"):
        lines.append(f"Full verdict: {res.get('base')}/api/v1/eval/runs/{res['run_id']}")
    return "\n".join(lines)


# ----------------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="otter-validate",
        description="Grade this task's output with OtterScore and block the finish if it fails.",
    )
    p.add_argument("--harness", default="generic",
                   choices=["generic", "claude", "codex", "openclaw", "git"],
                   help="which harness invoked us (selects the block protocol)")
    p.add_argument("--source", default="auto", choices=["auto", "diff", "stdin", "files"])
    p.add_argument("--files", nargs="*", default=None, help="explicit files to grade")
    p.add_argument("--modality", default=None)
    p.add_argument("--intent", default=os.environ.get("OTTER_INTENT"),
                   help="what the task was trying to do (conditions the critic)")
    p.add_argument("--policy-id", default=os.environ.get("OTTER_POLICY_ID"))
    p.add_argument("--rubric-id", default=os.environ.get("OTTER_RUBRIC_ID"))
    p.add_argument("--min-band", default=os.environ.get("OTTER_MIN_BAND", "ship"),
                   choices=["ship", "route_to_fix"])
    p.add_argument("--timeout", type=float, default=float(os.environ.get("OTTER_TIMEOUT", "120")))
    p.add_argument("--max-blocks", type=int, default=int(os.environ.get("OTTER_MAX_BLOCKS", "3")))
    p.add_argument("--strict", action="store_true", default=os.environ.get("OTTER_STRICT") == "1",
                   help="fail-closed on infra/config errors too")
    p.add_argument("--api-base", default=None)
    p.add_argument("--cwd", default=None)
    args = p.parse_args(argv)

    raw_stdin = _read_stdin_if_any()
    payload = _harness_payload(raw_stdin)
    args.stdin_text = "" if payload else raw_stdin
    args.key = os.environ.get("OTTER_API_KEY") or os.environ.get("SEAOTTER_API_KEY") or ""
    cwd = payload.get("cwd") or args.cwd or os.getcwd()

    # Loop-safety: Claude marks a re-entrant stop with stop_hook_active.
    stop_active = bool(payload.get("stop_hook_active"))

    if not args.key:
        msg = ("otter: no OTTER_API_KEY set — skipping validation. Get a free key: "
               "curl -s https://api.seaotter.ai/api/v1/agent-keys/signup "
               "-H 'content-type: application/json' -d '{\"email\":\"you@example.com\"}'")
        if args.strict:
            _eprint(msg)
            return 2
        _eprint(msg)
        return 0

    parts, modality, label = gather_work(args, payload)
    if not parts:
        return 0  # nothing produced this turn -> nothing to validate.

    canon = json.dumps(parts, sort_keys=True, separators=(",", ":"))
    work_hash = hashlib.sha256(canon.encode("utf-8", "replace")).hexdigest()[:16]
    prior = _consecutive_blocks(cwd, work_hash)
    if prior >= args.max_blocks or (stop_active and prior >= 1):
        _eprint(f"otter: still {args.min_band}-failing after {prior} attempt(s) on the same "
                "diff — allowing this finish so you aren't stuck. Validate manually with "
                "`otter validate` once you've addressed it.")
        _record(cwd, work_hash, 0)
        return 0

    res = grade(parts, modality, label, args)

    if not res.get("ok"):
        _record(cwd, work_hash, 0)
        if args.strict:
            _eprint(f"otter: {res.get('reason')} (strict mode -> blocking)")
            return 2
        _eprint(f"otter: {res.get('reason')} — allowing this finish (set OTTER_STRICT=1 to block).")
        return 0

    if _band_clears(res["band"], args.min_band):
        _record(cwd, work_hash, 0)
        sc = res.get("score")
        scstr = f" (score {sc})" if isinstance(sc, (int, float)) else ""
        _eprint(f"✅ OtterScore: {res['band'].upper()}{scstr} — clears your {args.min_band} bar. "
                f"Validated {label}.")
        return 0

    # Real verdict below the bar -> block, and feed the flaws back to the model.
    _record(cwd, work_hash, prior + 1)
    reason = block_reason(res, args.min_band)
    if args.harness == "claude":
        # Claude Code also honours a structured stdout decision on the Stop hook.
        print(json.dumps({"decision": "block", "reason": reason}))
    _eprint(reason)
    return 2


if __name__ == "__main__":
    sys.exit(main())
