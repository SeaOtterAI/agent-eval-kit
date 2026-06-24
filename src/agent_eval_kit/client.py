"""EvalClient — the agent's handle on an adversarial work-quality critic.

    from agent_eval_kit import EvalClient
    critic = EvalClient(policy_id="acme-prod-acceptance", locale="ja")
    verdict = critic.score(my_draft, references=["file://brand.pdf"])
    if not verdict.ready_to_ship:
        result = critic.iterate(verdict.run_id, my_revised_draft)
        print(result.delta.score_change, result.delta.ready_to_ship)

Or drive the whole loop in one call:

    final = critic.loop(produce=lambda v: agent.revise(v), work=agent.draft(),
                        modality="document", max_rounds=5)

Talks to an eval API over HTTP (``AGENT_EVAL_API_URL``; defaults to SeaOtter's
hosted OtterScore critic). No hard third-party dependency: uses ``httpx`` when
present, else ``urllib``. A ``transport`` can be injected for testing.
"""

from __future__ import annotations

import hashlib
import json as _json
import os
from collections.abc import Callable, Iterator, Sequence
from typing import Any

from . import modality as _modality
from .types import BANDS, Delta, IterationResult, Verdict

# The reference hosted backend. Override with AGENT_EVAL_API_URL or base_url=.
DEFAULT_BASE_URL = "https://api.seaotter.ai"
DEFAULT_RUBRIC = "enterprise-acceptance-default"

# (method, url, headers, json_body) -> (status_code, parsed_json)
Transport = Callable[[str, str, dict[str, str], dict[str, Any] | None], "tuple[int, Any]"]


class EvalError(RuntimeError):
    def __init__(self, status: int, detail: Any):
        self.status = status
        self.detail = detail
        super().__init__(f"eval API error {status}: {detail}")


def _content_ref(parts: Sequence[dict[str, Any]]) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update((p.get("text") or "").encode("utf-8", "replace"))
        h.update((p.get("data_b64") or "").encode("ascii", "replace"))
        h.update((p.get("uri") or "").encode("utf-8", "replace"))
    return f"inline:sha256:{h.hexdigest()}"


def _band_index(band: str) -> int:
    try:
        return BANDS.index(band)
    except ValueError:
        return len(BANDS)


def _default_transport(timeout: float) -> Transport:
    try:
        import httpx  # noqa: PLC0415

        client = httpx.Client(timeout=timeout)

        def _t(method: str, url: str, headers: dict[str, str], body: dict[str, Any] | None):
            r = client.request(method, url, headers=headers, json=body)
            try:
                data = r.json()
            except Exception:  # noqa: BLE001
                data = {"detail": r.text}
            return r.status_code, data

        return _t
    except ImportError:  # pragma: no cover - fallback path
        import urllib.error  # noqa: PLC0415
        import urllib.request  # noqa: PLC0415

        def _t(method: str, url: str, headers: dict[str, str], body: dict[str, Any] | None):
            data = _json.dumps(body).encode() if body is not None else None
            req = urllib.request.Request(url, data=data, method=method, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    return resp.status, _json.loads(resp.read() or b"{}")
            except urllib.error.HTTPError as e:  # noqa: PERF203
                try:
                    return e.code, _json.loads(e.read() or b"{}")
                except Exception:  # noqa: BLE001
                    return e.code, {"detail": str(e)}

        return _t


class EvalClient:
    """Submit work to an adversarial critic and iterate on the feedback.

    Reads sensible defaults from the environment:
      ``AGENT_EVAL_API_URL``   — eval API base url (default: SeaOtter hosted)
      ``AGENT_EVAL_API_KEY``   — bearer key for the eval API
      ``AGENT_EVAL_POLICY_ID`` — org acceptance policy to condition on
      ``AGENT_EVAL_RUBRIC_ID`` — default rubric
      ``AGENT_EVAL_LOCALE``    — feedback language (default ``en``)
    """

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        *,
        policy_id: str | None = None,
        rubric_id: str | None = None,
        locale: str | None = None,
        timeout: float = 120.0,
        transport: Transport | None = None,
    ) -> None:
        self.base_url = (base_url or os.environ.get("AGENT_EVAL_API_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.api_key = api_key or os.environ.get("AGENT_EVAL_API_KEY")
        self.policy_id = policy_id or os.environ.get("AGENT_EVAL_POLICY_ID")
        self.rubric_id = rubric_id or os.environ.get("AGENT_EVAL_RUBRIC_ID")
        self.locale = locale or os.environ.get("AGENT_EVAL_LOCALE") or "en"
        self._transport = transport or _default_transport(timeout)

    # -- low level ---------------------------------------------------------
    def _req(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        status, data = self._transport(method, f"{self.base_url}{path}", headers, body)
        if status >= 400:
            raise EvalError(status, (data or {}).get("detail", data) if isinstance(data, dict) else data)
        return data

    # -- discovery ---------------------------------------------------------
    def list_rubrics(self) -> list[dict[str, Any]]:
        return (self._req("GET", "/api/v1/eval/rubrics") or {}).get("rubrics", [])

    def get_rubric(self, rubric_id: str) -> dict[str, Any]:
        return self._req("GET", f"/api/v1/eval/rubrics/{rubric_id}")

    def list_policies(self) -> list[dict[str, Any]]:
        """Org acceptance policies. Returns [] if the deployment has none."""
        try:
            return (self._req("GET", "/api/v1/eval/policies") or {}).get("policies", [])
        except EvalError as e:
            if e.status == 404:
                return []
            raise

    def list_stored_policies(self) -> list[dict[str, Any]]:
        """YOUR tenant's authored, reusable acceptance policies (``/api/v1/policies``).

        These are the versioned, tenant-scoped policies created via
        :meth:`upsert_policy`; reference one by its id as ``policy_id`` in
        :meth:`score`. Returns [] when the deployment has none or the route is
        unavailable (requires an authenticated key)."""
        try:
            return (self._req("GET", "/api/v1/policies") or {}).get("policies", [])
        except EvalError as e:
            if e.status in (401, 403, 404):
                return []
            raise

    def upsert_policy(
        self,
        policy_id: str,
        *,
        name: str | None = None,
        hard_rules: list[dict[str, Any]] | None = None,
        conditioning: dict[str, Any] | None = None,
        bands: dict[str, Any] | None = None,
        modality_scope: list[str] | None = None,
        visibility: str = "team",
    ) -> dict[str, Any]:
        """Author (or update) a reusable, versioned acceptance policy for your tenant.

        Creates the policy on first call (POST) and bumps a new version on
        subsequent calls (PUT). Reference it by ``policy_id`` in :meth:`score`.
        ``conditioning`` is the soft critic layer (``policy_prior`` /
        ``refuse_to_flatter`` / ``pushback_required`` / ``min_high_weight_criteria``);
        ``hard_rules`` are deterministic gates; ``bands`` are the decision
        thresholds. All only ever TIGHTEN the hostile-by-default critic."""
        body: dict[str, Any] = {
            "id": policy_id,
            "name": name or policy_id,
            "visibility": visibility,
            "modality_scope": modality_scope or ["*"],
            "hard_rules": hard_rules or [],
            "conditioning": conditioning or {},
            "bands": bands or {},
        }
        try:
            return self._req("POST", "/api/v1/policies", body)
        except EvalError as e:
            if e.status == 409:  # already exists -> update (new version)
                return self._req("PUT", f"/api/v1/policies/{policy_id}", body)
            raise

    # -- the loop ----------------------------------------------------------
    def score(
        self,
        work: Any,
        *,
        modality: str | None = None,
        rubric_id: str | None = None,
        policy_id: str | None = None,
        policy: dict[str, Any] | None = None,
        critic_prompt: str | None = None,
        intent: str | None = None,
        prompt: str | None = None,
        references: list[Any] | None = None,
        locale: str | None = None,
        return_artifacts: bool = True,
        cost_cap_usd: float | None = None,
        mime: str | None = None,
        name: str | None = None,
    ) -> Verdict:
        """Submit work (any modality) → Verdict. Conditioned on org (``policy_id``/
        ``rubric_id``), prompt (``prompt``/``intent``), and files (``references``).

        Bring Your Own Policy (BYOP): pass ``policy`` to grade against your OWN
        acceptance bar in a single call — a dict of
        ``{directives[], criteria[], instructions, bands, hard_rules[], extends}``,
        composed onto the hostile base rubric (tighten-only). ``critic_prompt`` is a
        free-text hostile-critic instruction override. Both are additive to the
        defaults; neither can make the critic lenient.
        """
        mod, parts = _modality.normalize(work, modality=modality, mime=mime, name=name)
        body = {
            "modality": mod,
            "rubric_id": rubric_id or self.rubric_id or DEFAULT_RUBRIC,
            "artifact_ref": _content_ref(parts),
            "artifact_parts": parts,
            "intent_text": intent or "",
            "user_prompt": prompt or "",
            "references": _normalize_refs(references),
            "locale": locale or self.locale,
            "return_feedback_artifacts": bool(return_artifacts),
        }
        pid = policy_id or self.policy_id
        if pid:
            body["policy_id"] = pid
        if policy:
            body["policy"] = policy
        if critic_prompt:
            body["critic_prompt"] = critic_prompt
        if cost_cap_usd is not None:
            body["cost_cap_usd"] = cost_cap_usd
        resp = self._req("POST", "/api/v1/eval/runs", body)
        run_id = resp.get("run_id")
        verdict_dict = (resp.get("first_iteration") or {}).get("critic_verdict") or {}
        return Verdict.from_dict(verdict_dict, run_id=run_id)

    # ------------------------------------------------------------------
    # Async grading (submit -> poll). Non-blocking: the submit returns a job
    # id immediately, so a slow/cold critic never blocks or times out the caller.
    # ------------------------------------------------------------------
    def submit_async(
        self,
        work: Any,
        *,
        mode: str = "one_shot",
        modality: str | None = None,
        rubric_id: str | None = None,
        policy_id: str | None = None,
        policy: dict[str, Any] | None = None,
        critic_prompt: str | None = None,
        intent: str | None = None,
        prompt: str | None = None,
        references: list[Any] | None = None,
        locale: str | None = None,
        return_artifacts: bool = True,
        cost_cap_usd: float | None = None,
        mime: str | None = None,
        name: str | None = None,
    ) -> dict[str, Any]:
        """Submit work for grading as an async job. Returns the job envelope
        ``{job_id, status, poll_url, ...}`` immediately (HTTP 202). Poll with
        :meth:`poll_job` until ``status == 'completed'``, then :meth:`run_verdict`.

        Supports BYOP (``policy`` / ``critic_prompt``) — same semantics as
        :meth:`score`."""
        mod, parts = _modality.normalize(work, modality=modality, mime=mime, name=name)
        body: dict[str, Any] = {
            "mode": mode,
            "submission": "async",
            "modality": mod,
            "rubric_id": rubric_id or self.rubric_id or DEFAULT_RUBRIC,
            "artifact_ref": _content_ref(parts),
            "artifact_parts": parts,
            "intent_text": intent or "",
            "user_prompt": prompt or "",
            "references": _normalize_refs(references),
            "locale": locale or self.locale,
            "return_feedback_artifacts": bool(return_artifacts),
        }
        pid = policy_id or self.policy_id
        if pid:
            body["policy_id"] = pid
        if policy:
            body["policy"] = policy
        if critic_prompt:
            body["critic_prompt"] = critic_prompt
        if cost_cap_usd is not None:
            body["cost_cap_usd"] = cost_cap_usd
        return self._req("POST", "/api/v1/eval/jobs", body)

    def poll_job(self, job_id: str) -> dict[str, Any]:
        """Poll an async job: ``{status, run_id, progress, cost, result_summary, error}``."""
        from urllib.parse import quote

        return self._req("GET", f"/api/v1/eval/jobs/{quote(job_id, safe='')}")

    def run_verdict(self, run_id: str) -> Verdict:
        """Fetch the full verdict (flaws + upgrades) for a completed run."""
        from urllib.parse import quote

        resp = self._req("GET", f"/api/v1/eval/runs/{quote(run_id, safe='')}")
        its = resp.get("iterations") or []
        vd = (its[-1].get("critic_verdict") if its else {}) or {}
        return Verdict.from_dict(vd, run_id=run_id)

    def score_stream(
        self,
        work: Any,
        *,
        modality: str | None = None,
        rubric_id: str | None = None,
        policy_id: str | None = None,
        policy: dict[str, Any] | None = None,
        critic_prompt: str | None = None,
        intent: str | None = None,
        prompt: str | None = None,
        references: list[Any] | None = None,
        locale: str | None = None,
        return_artifacts: bool = True,
        cost_cap_usd: float | None = None,
        mime: str | None = None,
        name: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Stream the grade as it is produced → an iterator of SSE events.

        Yields the raw event dicts the API emits, in order::

            {"event": "received", "data": {run_id, modality, rubric_id}}
            {"event": "scanning", "data": {criterion, idx, total}}   # per criterion
            {"event": "flaw",     "data": {...}}                     # per flaw
            {"event": "verdict",  "data": {...}}                     # full verdict
            {"event": "done",     "data": {run_id}}

        Lower time-to-first-byte than :meth:`score`: the agent sees ``received``
        immediately and each ``flaw`` as the critic surfaces it, instead of
        blocking for the whole verdict. To get the final :class:`Verdict`, read
        the ``verdict`` event::

            for evt in critic.score_stream(work):
                if evt["event"] == "flaw":
                    print("blocker:", evt["data"]["criterion"])
                elif evt["event"] == "verdict":
                    v = Verdict.from_dict(evt["data"], run_id=evt["data"].get("run_id"))

        Requires ``httpx`` for true streaming; without it this transparently
        falls back to a blocking call and replays the result as a single
        ``received`` + ``verdict`` + ``done`` triple (no partial events, same
        final answer) so the SDK never hard-fails on a missing optional dep.
        """
        mod, parts = _modality.normalize(work, modality=modality, mime=mime, name=name)
        body: dict[str, Any] = {
            "modality": mod,
            "rubric_id": rubric_id or self.rubric_id or DEFAULT_RUBRIC,
            "artifact_ref": _content_ref(parts),
            "artifact_parts": parts,
            "intent_text": intent or "",
            "user_prompt": prompt or "",
            "references": _normalize_refs(references),
            "locale": locale or self.locale,
            "return_feedback_artifacts": bool(return_artifacts),
        }
        pid = policy_id or self.policy_id
        if pid:
            body["policy_id"] = pid
        if policy:
            body["policy"] = policy
        if critic_prompt:
            body["critic_prompt"] = critic_prompt
        if cost_cap_usd is not None:
            body["cost_cap_usd"] = cost_cap_usd
        yield from self._stream_sse("/api/v1/eval/stream", body)

    def _stream_sse(self, path: str, body: dict[str, Any]) -> Iterator[dict[str, Any]]:
        """POST ``body`` and yield parsed SSE event dicts. httpx → urllib fallback.

        Each yielded item is ``{"event": <name>, "data": <parsed json>}``. A
        ``: comment`` heartbeat line is skipped. On a missing ``httpx`` the method
        degrades to the blocking :meth:`score`-equivalent call and synthesizes the
        terminal events so a caller's event loop still terminates correctly.
        """
        url = f"{self.base_url}{path}"
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        try:
            import httpx  # noqa: PLC0415
        except ImportError:  # pragma: no cover - exercised only without httpx
            yield from self._stream_fallback(body)
            return
        with httpx.Client(timeout=None) as client:
            with client.stream("POST", url, headers=headers, json=body) as resp:
                if resp.status_code >= 400:
                    resp.read()
                    try:
                        detail = resp.json().get("detail")
                    except Exception:  # noqa: BLE001
                        detail = resp.text
                    raise EvalError(resp.status_code, detail)
                yield from _parse_sse_lines(resp.iter_lines())

    def _stream_fallback(self, body: dict[str, Any]) -> Iterator[dict[str, Any]]:
        """No httpx: call the blocking endpoint and replay it as SSE events."""
        resp = self._req("POST", "/api/v1/eval/runs", body)
        run_id = resp.get("run_id")
        verdict = (resp.get("first_iteration") or {}).get("critic_verdict") or {}
        yield {"event": "received",
               "data": {"run_id": run_id, "modality": body.get("modality"),
                        "rubric_id": body.get("rubric_id")}}
        for fl in verdict.get("flaws") or []:
            yield {"event": "flaw", "data": fl}
        vd = dict(verdict)
        vd["run_id"] = run_id
        yield {"event": "verdict", "data": vd}
        yield {"event": "done", "data": {"run_id": run_id}}

    def iterate(
        self,
        run_id: str,
        work: Any,
        *,
        prompt: str | None = None,
        modality: str | None = None,
        locale: str | None = None,
        return_artifacts: bool = True,
        prev: Verdict | None = None,
    ) -> IterationResult:
        """Submit a revision → new Verdict + delta (resolved/new flaws, score change)."""
        _mod, parts = _modality.normalize(work, modality=modality)
        body = {
            "decision": "re_prompt",
            "user_prompt": prompt or "",
            "new_artifact_ref": _content_ref(parts),
            "new_artifact_parts": parts,
            "locale": locale or self.locale,
            "return_feedback_artifacts": bool(return_artifacts),
        }
        resp = self._req("POST", f"/api/v1/eval/runs/{run_id}/iterate", body)
        it = resp.get("iteration") or {}
        verdict = Verdict.from_dict(it.get("critic_verdict") or {}, run_id=run_id)
        if isinstance(resp.get("delta"), dict):
            delta = Delta.from_dict(resp["delta"])
        elif prev is not None:
            delta = Delta.between(prev, verdict)
        else:
            delta = Delta(new_flaws=verdict.flaws, ready_to_ship=verdict.ready_to_ship)
        return IterationResult(run_id=run_id, verdict=verdict, delta=delta, idx=int(it.get("idx") or 0))

    def accept(self, run_id: str) -> dict[str, Any]:
        return self._req("POST", f"/api/v1/eval/runs/{run_id}/iterate",
                         {"decision": "accept", "user_prompt": ""})

    def reject(self, run_id: str) -> dict[str, Any]:
        return self._req("POST", f"/api/v1/eval/runs/{run_id}/iterate",
                         {"decision": "reject", "user_prompt": ""})

    def score_workflow(
        self,
        steps: list[dict[str, Any]],
        *,
        topology: list[dict[str, Any]] | None = None,
        policy_id: str | None = None,
        locale: str | None = None,
    ) -> dict[str, Any]:
        """Score an end-to-end agent trajectory: per-step modality critics + a
        topology-aware composite + chain critique. ``steps`` are
        ``[{step_id, modality, rubric_id, artifact_ref|artifact_parts, parents?}]``.
        Returns the raw workflow-score response (composite + per-step + chain critique)."""
        body: dict[str, Any] = {"steps": steps}
        if topology is not None:
            body["topology"] = topology
        if locale:
            body["locale"] = locale
        pid = policy_id or self.policy_id
        if pid:
            body["policy_id"] = pid
        return self._req("POST", "/api/v1/eval/workflows/agent-trajectory/topology", body)

    def loop(
        self,
        produce: Callable[[Verdict], Any],
        work: Any,
        *,
        max_rounds: int = 5,
        target_band: str = "ship",
        on_round: Callable[[int, Verdict], None] | None = None,
        **score_kwargs: Any,
    ) -> Verdict:
        """Drive produce → grade → revise until the critic clears ``target_band``.

        ``produce`` receives the current Verdict and returns the revised work
        (return ``None`` to stop early). This is the whole product in one call:
        good agentic work is the work that survived the critic's pushback.
        """
        target = _band_index(target_band)
        verdict = self.score(work, **score_kwargs)
        if on_round:
            on_round(0, verdict)
        rounds = 0
        while _band_index(verdict.band) > target and rounds < max_rounds:
            revised = produce(verdict)
            if revised is None:
                break
            result = self.iterate(
                verdict.run_id, revised,
                prompt=score_kwargs.get("prompt"),
                return_artifacts=score_kwargs.get("return_artifacts", True),
                prev=verdict,
            )
            verdict = result.verdict
            rounds += 1
            if on_round:
                on_round(rounds, verdict)
        return verdict


def _parse_sse_lines(lines: Iterator[str]) -> Iterator[dict[str, Any]]:
    """Parse an SSE line stream into ``{"event": name, "data": parsed}`` dicts.

    Handles multi-line ``data:`` blocks, ``:`` comment heartbeats (skipped), and
    a blank line as the event terminator — the wire format the eval API emits.
    ``data`` is JSON-decoded when possible, else passed through as a string.
    """
    event = "message"
    data_lines: list[str] = []
    for raw in lines:
        line = raw.rstrip("\r")
        if line == "":
            if data_lines:
                blob = "\n".join(data_lines)
                try:
                    data: Any = _json.loads(blob)
                except Exception:  # noqa: BLE001
                    data = blob
                yield {"event": event, "data": data}
            event = "message"
            data_lines = []
            continue
        if line.startswith(":"):
            continue  # comment / heartbeat
        field, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if field == "event":
            event = value
        elif field == "data":
            data_lines.append(value)
    # Flush a trailing event with no terminating blank line.
    if data_lines:
        blob = "\n".join(data_lines)
        try:
            data = _json.loads(blob)
        except Exception:  # noqa: BLE001
            data = blob
        yield {"event": event, "data": data}


def _normalize_refs(references: list[Any] | None) -> list[dict[str, Any]]:
    """Accept references as plain strings (paths/urls) or ref dicts."""
    out: list[dict[str, Any]] = []
    for r in references or []:
        if isinstance(r, dict):
            out.append(r)
            continue
        s = str(r)
        kind = "text"
        low = s.lower()
        if any(low.endswith(e) for e in (".png", ".jpg", ".jpeg", ".gif", ".webp")):
            kind = "image"
        elif any(low.endswith(e) for e in (".mp4", ".mov", ".webm")):
            kind = "video"
        elif any(low.endswith(e) for e in (".pptx", ".ppt", ".key", ".pdf")):
            kind = "deck"
        out.append({"kind": kind, "artifact_ref": s, "label": "good_example"})
    return out[:5]
