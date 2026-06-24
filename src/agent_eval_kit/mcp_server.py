#!/usr/bin/env python3
"""MCP server — the agent's iteration loop with an adversarial critic.

Any MCP-speaking agent (Claude, Codex, Cursor, a custom harness) wires this in
and gets the whole loop as first-class tools:

    eval_list_policies     discover the rubrics / org acceptance policies
    eval_score             submit work (any modality) -> verdict (+ rich feedback)
    eval_iterate           submit a revision -> verdict + delta (resolved/new)
    eval_score_async       submit work, return a job id immediately (non-blocking)
    eval_job_result        poll an async grading job
    eval_score_stream      grade via the low-latency streaming endpoint
    eval_score_workflow    score a multi-step agent trajectory
    eval_feedback_artifact fetch a rendered annotated artifact by ref

All tools are read-only (no side effects) so codex/Claude auto-approve them in
non-interactive runs. The verdict is the critic's, authoritatively — the agent
acts on it, it does not grade itself.

Run (stdio):
    AGENT_EVAL_API_URL=https://api.seaotter.ai AGENT_EVAL_API_KEY=sk-otter-... \
    AGENT_EVAL_POLICY_ID=acme-prod-acceptance python -m agent_eval_kit.mcp_server

Streamable-HTTP (hosted): AGENT_EVAL_MCP_TRANSPORT=streamable-http python -m agent_eval_kit.mcp_server
"""

from __future__ import annotations

import contextvars
import os
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from .client import EvalClient, EvalError
from .types import Verdict

mcp = FastMCP("agent-eval-kit")

# Per-request eval API key for the HOSTED (streamable-http, multi-tenant) server:
# the auth middleware in agent_eval_kit.server_http stamps each request's caller
# key here so every tool bills the right tenant. In stdio mode it stays None and
# the client falls back to AGENT_EVAL_API_KEY from the env (single-tenant local).
_REQUEST_API_KEY: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "agent_eval_request_api_key", default=None
)


def _client() -> EvalClient:
    key = _REQUEST_API_KEY.get()
    return EvalClient(api_key=key) if key else EvalClient()


def _verdict_payload(v: Verdict) -> dict[str, Any]:
    out = dict(v.raw)
    out["run_id"] = v.run_id
    out["ready_to_ship"] = v.ready_to_ship
    out["summary"] = v.summary()
    return out


def _err(exc: Exception) -> dict[str, Any]:
    # Fail CLOSED: a critic outage is never a silent pass.
    if isinstance(exc, EvalError):
        return {"error": "eval_error", "status": exc.status, "detail": str(exc.detail),
                "band": "quarantine", "ready_to_ship": False}
    return {"error": "eval_unavailable", "detail": str(exc),
            "band": "quarantine", "ready_to_ship": False}


# Grading + read tools: read-only (no user-data mutation), but they reach an
# external service (the eval API) -> openWorldHint=True. ChatGPT Apps review
# checks that these three flags match real behaviour.
_RO = ToolAnnotations(
    readOnlyHint=True, openWorldHint=True, destructiveHint=False,
    title="agent-eval-kit — adversarial critic iteration",
)


@mcp.tool(annotations=ToolAnnotations(
    readOnlyHint=True, openWorldHint=True, destructiveHint=False,
    title="agent-eval-kit — discover policies"))
def eval_list_policies() -> dict[str, Any]:
    """List the rubrics and org acceptance policies you can be graded against."""
    c = _client()
    try:
        return {"rubrics": c.list_rubrics(), "policies": c.list_policies()}
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool(annotations=_RO)
def eval_score(
    work: str,
    modality: str | None = None,
    rubric_id: str | None = None,
    policy_id: str | None = None,
    prompt: str = "",
    intent: str = "",
    references: list[str] | None = None,
    locale: str | None = None,
    return_artifacts: bool = True,
) -> dict[str, Any]:
    """Grade a work artifact with a hostile-by-default critic.

    `work` may be inline text/code, a local file path, or a base64 data: URL —
    images, video, pdf, docx, pptx, xlsx, audio are all accepted; the modality is
    auto-detected when omitted. Feedback is conditioned on the organisation
    (`policy_id`/`rubric_id`), the `prompt`/`intent` you were given, and reference
    files. Returns {run_id, score, band, flaws[ {criterion,severity,detail,anchor} ],
    upgrades[], feedback_artifacts[], ready_to_ship}. Keep the `run_id` to call
    eval_iterate. Treat the verdict as authoritative — do NOT grade yourself.
    """
    try:
        v = _client().score(
            work, modality=modality, rubric_id=rubric_id, policy_id=policy_id,
            prompt=prompt, intent=intent, references=references, locale=locale,
            return_artifacts=return_artifacts)
        return _verdict_payload(v)
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool(annotations=_RO)
def eval_iterate(
    run_id: str,
    work: str,
    prompt: str = "",
    locale: str | None = None,
    return_artifacts: bool = True,
) -> dict[str, Any]:
    """Submit a revised artifact against a prior run_id and get fresh feedback.

    Returns {run_id, verdict, delta:{resolved_flaws,new_flaws,persisted_flaws,
    score_change,ready_to_ship}}. Keep iterating while ready_to_ship is false —
    that is how good agentic work is produced: by surviving the critic's pushback.
    """
    try:
        res = _client().iterate(run_id, work, prompt=prompt, locale=locale,
                                return_artifacts=return_artifacts)
        return {"run_id": run_id, "verdict": _verdict_payload(res.verdict),
                "delta": res.delta.to_dict(), "ready_to_ship": res.verdict.ready_to_ship}
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool(annotations=_RO)
def eval_score_async(
    work: str,
    modality: str | None = None,
    rubric_id: str | None = None,
    policy_id: str | None = None,
    prompt: str = "",
    references: list[str] | None = None,
    locale: str | None = None,
    mode: str = "one_shot",
) -> dict[str, Any]:
    """Submit work for grading and return IMMEDIATELY with a job_id — NON-BLOCKING.

    Use this instead of `eval_score` when a grade may be slow (a cold critic, a
    large/multimodal artifact, or `mode="agentic"` deep grading) so your call
    never blocks or times out. Same inputs as `eval_score`. Returns
    `{job_id, status}` right away; then poll `eval_job_result(job_id)` every few
    seconds until `status` is `completed` (verdict included) or `failed`.
    `mode`: `one_shot` (fast single pass) or `agentic` (deeper, multi-pass).
    """
    try:
        env = _client().submit_async(
            work, mode=mode, modality=modality, rubric_id=rubric_id,
            policy_id=policy_id, prompt=prompt, references=references, locale=locale)
        return {
            "job_id": env.get("job_id"),
            "status": env.get("status") or "queued",
            "poll": "call eval_job_result(job_id) every ~5s until status is 'completed' or 'failed'",
        }
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool(annotations=_RO)
def eval_job_result(job_id: str) -> dict[str, Any]:
    """Poll an async grading job (submitted via `eval_score_async`).

    While `status` is `queued` / `preparing` / `grading`, poll again in a few
    seconds. On `status="completed"` the full verdict
    `{run_id, score, band, flaws[], upgrades[], ready_to_ship}` is included. On
    `status="failed"` an `error` explains why. Treat the verdict as authoritative.
    """
    try:
        c = _client()
        job = c.poll_job(job_id)
        status = job.get("status")
        out: dict[str, Any] = {
            "job_id": job_id, "status": status,
            "progress": job.get("progress"), "cost": job.get("cost"),
        }
        if status == "completed" and job.get("run_id"):
            try:
                v = c.run_verdict(str(job["run_id"]))
                out["verdict"] = _verdict_payload(v)
                out["ready_to_ship"] = v.ready_to_ship
            except Exception:  # noqa: BLE001 - verdict fetch failed; fall back to the summary
                out["result_summary"] = job.get("result_summary")
        elif status == "failed":
            out["error"] = job.get("error")
            out["ready_to_ship"] = False
        else:
            out["hint"] = "still grading — call eval_job_result(job_id) again in a few seconds"
        return out
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool(annotations=_RO)
def eval_score_stream(
    work: str,
    modality: str | None = None,
    rubric_id: str | None = None,
    policy_id: str | None = None,
    prompt: str = "",
    references: list[str] | None = None,
    locale: str | None = None,
    return_artifacts: bool = True,
) -> dict[str, Any]:
    """Grade work via the low-latency STREAMING critic endpoint (SSE under the hood).

    Same inputs + verdict as `eval_score`, but it consumes the server's
    `received -> scanning -> flaw -> verdict -> done` event stream, so for large
    files / slow critics the underlying call has a lower time-to-first-byte. MCP
    is a request/response protocol, so this tool DRAINS the stream server-side and
    returns the final verdict PLUS a `progress` trace of the criteria scanned and
    the order flaws were surfaced — the agent gets the same authoritative
    `{run_id, score, band, flaws[], upgrades[], ready_to_ship}` it would from
    `eval_score`, with the incremental ordering preserved for inspection.

    For a genuinely incremental, token-by-token UX, call the streaming endpoint
    directly with the SDK (`EvalClient.score_stream(...)`).
    """
    try:
        scanned: list[str] = []
        flaw_order: list[str] = []
        verdict_data: dict[str, Any] | None = None
        run_id: str | None = None
        for evt in _client().score_stream(
            work, modality=modality, rubric_id=rubric_id, policy_id=policy_id,
            prompt=prompt, references=references, locale=locale,
            return_artifacts=return_artifacts,
        ):
            name, data = evt.get("event"), evt.get("data") or {}
            if name == "received":
                run_id = data.get("run_id")
            elif name == "scanning":
                scanned.append(str(data.get("criterion")))
            elif name == "flaw":
                flaw_order.append(str(data.get("criterion") or ""))
            elif name == "verdict":
                verdict_data = data
        if verdict_data is None:
            return _err(EvalError(502, "stream ended without a verdict"))
        v = Verdict.from_dict(verdict_data, run_id=run_id or verdict_data.get("run_id"))
        out = _verdict_payload(v)
        out["progress"] = {"criteria_scanned": scanned, "flaw_order": flaw_order}
        return out
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool(annotations=_RO)
def eval_score_workflow(
    steps: list[dict[str, Any]],
    topology: list[dict[str, Any]] | None = None,
    policy_id: str | None = None,
    locale: str | None = None,
) -> dict[str, Any]:
    """Score an end-to-end agent trajectory: per-step modality critics + a
    topology-aware composite + chain critique. `steps` are
    [{step_id, modality, rubric_id, artifact_ref|artifact_parts, parents?}]."""
    try:
        return _client().score_workflow(steps, topology=topology, policy_id=policy_id, locale=locale)
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool(annotations=_RO)
def eval_feedback_artifact(ref: str) -> dict[str, Any]:
    """Fetch a rendered rich-feedback artifact (annotated image / pdf / video
    marker track / markdown) by its ref. Returns {mime, data_url, caption, anchors}."""
    try:
        from urllib.parse import quote
        return _client()._req("GET", f"/api/v1/eval/feedback-artifacts/{quote(ref, safe='')}")
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


def main() -> None:
    transport = os.environ.get("AGENT_EVAL_MCP_TRANSPORT", "stdio")
    if transport in ("streamable-http", "http", "sse"):
        mcp.run(transport="streamable-http" if transport in ("streamable-http", "http") else "sse")
    else:
        mcp.run()


if __name__ == "__main__":
    main()
