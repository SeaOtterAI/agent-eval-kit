"""EvalClient SDK tests — no network, a fake transport stands in for the eval API."""

from __future__ import annotations

import pytest

from agent_eval_kit import EvalClient, EvalError, Verdict
from agent_eval_kit.client import _parse_sse_lines


class FakeAPI:
    """Records requests and returns scripted verdicts per run."""

    def __init__(self, verdicts):
        self.verdicts = list(verdicts)  # one verdict dict per score/iterate call
        self.calls = []
        self._i = 0

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, body, headers))
        if url.endswith("/api/v1/eval/rubrics"):
            return 200, {"rubrics": [{"id": "text-blog", "modality": "text"}]}
        if url.endswith("/api/v1/eval/policies"):
            return 200, {"policies": [{"id": "acme", "name": "Acme"}]}
        if url.endswith("/api/v1/eval/workflows/agent-trajectory/topology"):
            return 200, {"composite_score": 72.0, "per_step": [], "chain_critique": "ok"}
        if url.endswith("/api/v1/eval/runs"):
            v = self.verdicts[self._i]
            self._i += 1
            return 201, {"run_id": "run-1", "first_iteration": {"critic_verdict": v}}
        if "/iterate" in url:
            if body.get("decision") in ("accept", "reject"):
                return 200, {"run_id": "run-1", "status": "accepted"}
            v = self.verdicts[self._i]
            self._i += 1
            return 200, {"run_id": "run-1", "iteration": {"idx": self._i, "critic_verdict": v}}
        return 404, {"detail": "not found"}


def _v(score, band, flaws):
    return {"score": score, "band": band, "decision": band,
            "flaws": flaws, "upgrades": [{"action": "fix it"}], "rationale": "because"}


def _flaw(crit, sev="high", anchor=None):
    f = {"criterion": crit, "severity": sev, "detail": f"{crit} is wrong"}
    if anchor:
        f["anchor"] = anchor
    return f


# ---- score -------------------------------------------------------------------

def test_score_builds_conditioned_request(tmp_path):
    api = FakeAPI([_v(40, "route_to_fix", [_flaw("groundedness")])])
    c = EvalClient(base_url="http://x", policy_id="acme", locale="ja", transport=api)
    # The reference must resolve to a real local file — the kit now refuses to
    # ship an unreadable local ref the hosted critic could never resolve.
    gold = tmp_path / "gold.md"
    gold.write_text("# Gold standard\nbody")
    v = c.score("some text", prompt="do the thing", references=[f"file://{gold}"])
    assert isinstance(v, Verdict) and v.run_id == "run-1"
    assert v.band == "route_to_fix" and v.score == 40.0
    assert v.flaws[0].criterion == "groundedness"
    body = api.calls[-1][2]
    assert body["policy_id"] == "acme" and body["locale"] == "ja"
    assert body["return_feedback_artifacts"] is True
    assert body["user_prompt"] == "do the thing"
    assert body["references"][0]["artifact_ref"] == f"file://{gold}"
    assert body["artifact_ref"].startswith("inline:sha256:")


def test_score_rejects_unreadable_local_reference():
    """A reference to a local file that does not exist is an actionable error,
    not a silently-unresolvable ref shipped to the hosted critic."""
    from agent_eval_kit.types import FileError

    api = FakeAPI([_v(90, "ship", [])])
    c = EvalClient(base_url="http://x", transport=api)
    with pytest.raises(FileError) as e:
        c.score("some text", references=["file://does-not-exist.pdf"])
    assert "base64" in str(e.value)  # tells the agent how to fix it


def test_score_sends_bearer_when_key_set():
    api = FakeAPI([_v(90, "ship", [])])
    c = EvalClient(base_url="http://x", api_key="sk-otter-secret", transport=api)
    c.score("hi")
    headers = api.calls[-1][3]
    assert headers["Authorization"] == "Bearer sk-otter-secret"


def test_score_error_raises():
    def boom(method, url, headers, body):
        return 422, {"detail": "modality bad"}
    c = EvalClient(base_url="http://x", transport=boom)
    with pytest.raises(EvalError) as e:
        c.score("hi")
    assert e.value.status == 422


# ---- iterate + delta ---------------------------------------------------------

def test_iterate_computes_delta():
    api = FakeAPI([
        _v(40, "route_to_fix", [_flaw("a"), _flaw("b")]),
        _v(80, "route_to_fix", [_flaw("b"), _flaw("c")]),
    ])
    c = EvalClient(base_url="http://x", transport=api)
    v0 = c.score("draft 1")
    res = c.iterate(v0.run_id, "draft 2", prev=v0)

    def keys(fl):
        return sorted(f.criterion for f in fl)

    assert keys(res.delta.resolved_flaws) == ["a"]
    assert keys(res.delta.new_flaws) == ["c"]
    assert keys(res.delta.persisted_flaws) == ["b"]
    assert res.delta.score_change == 40.0


def test_delta_prefers_server_payload():
    def t(method, url, headers, body):
        if url.endswith("/runs"):
            return 201, {"run_id": "r", "first_iteration": {"critic_verdict": _v(50, "route_to_fix", [])}}
        return 200, {"run_id": "r", "iteration": {"critic_verdict": _v(90, "ship", [])},
                     "delta": {"resolved_flaws": [_flaw("x")], "score_change": 40, "ready_to_ship": True}}
    c = EvalClient(base_url="http://x", transport=t)
    v0 = c.score("a")
    res = c.iterate(v0.run_id, "b")
    assert res.delta.ready_to_ship is True and res.delta.score_change == 40.0
    assert res.delta.resolved_flaws[0].criterion == "x"


# ---- the loop ----------------------------------------------------------------

def test_loop_stops_when_ship():
    api = FakeAPI([
        _v(30, "block", [_flaw("a")]),
        _v(60, "route_to_fix", [_flaw("b")]),
        _v(92, "ship", []),
    ])
    c = EvalClient(base_url="http://x", transport=api)
    rounds_seen = []
    final = c.loop(produce=lambda v: f"revision after {v.band}",
                   work="first draft", on_round=lambda i, v: rounds_seen.append((i, v.band)))
    assert final.ready_to_ship and final.score == 92.0
    assert rounds_seen == [(0, "block"), (1, "route_to_fix"), (2, "ship")]


def test_loop_respects_max_rounds():
    api = FakeAPI([_v(10, "block", [_flaw("a")])] * 10)
    c = EvalClient(base_url="http://x", transport=api)
    final = c.loop(produce=lambda v: "again", work="d", max_rounds=2)
    assert not final.ready_to_ship
    # 1 score + 2 iterate = 3 verdicts consumed
    assert api._i == 3


def test_loop_produce_returns_none_stops():
    api = FakeAPI([_v(10, "block", [_flaw("a")]), _v(10, "block", [_flaw("a")])])
    c = EvalClient(base_url="http://x", transport=api)
    final = c.loop(produce=lambda v: None, work="d", max_rounds=5)
    assert final.band == "block" and api._i == 1


# ---- discovery ---------------------------------------------------------------

def test_discovery():
    api = FakeAPI([])
    c = EvalClient(base_url="http://x", transport=api)
    assert c.list_rubrics()[0]["id"] == "text-blog"
    assert c.list_policies()[0]["id"] == "acme"


def test_list_policies_empty_on_404():
    def t(method, url, headers, body):
        return 404, {"detail": "no policies endpoint"}
    c = EvalClient(base_url="http://x", transport=t)
    assert c.list_policies() == []


def test_score_workflow_posts_to_topology_endpoint():
    api = FakeAPI([])
    c = EvalClient(base_url="http://x", policy_id="acme", transport=api)
    out = c.score_workflow(
        [{"step_id": "s1", "modality": "text"}],
        topology=[{"from": "s1", "to": "s2"}],
    )
    assert out["composite_score"] == 72.0
    method, url, body, _ = api.calls[-1]
    assert method == "POST" and url.endswith("/api/v1/eval/workflows/agent-trajectory/topology")
    assert body["steps"][0]["step_id"] == "s1"
    assert body["topology"][0]["from"] == "s1"
    assert body["policy_id"] == "acme"


def test_summary_human_readable():
    v = Verdict.from_dict(_v(45, "route_to_fix", [_flaw("rca", anchor={"kind": "page", "page": 2})]))
    s = v.summary()
    assert "ROUTE_TO_FIX" in s and "rca" in s and "page 2" in s


# ---- streaming (score_stream) ------------------------------------------------

def _sse_lines(*blocks):
    """Yield SSE wire lines for the given (event, data_dict) blocks + heartbeats."""
    import json as _j
    yield ": keepalive"            # a heartbeat comment is skipped
    yield ""
    for name, data in blocks:
        yield f"event: {name}"
        yield f"data: {_j.dumps(data)}"
        yield ""


def test_parse_sse_lines_order_and_skip_comments():
    blocks = [
        ("received", {"run_id": "r1", "modality": "text", "rubric_id": "rb"}),
        ("scanning", {"criterion": "groundedness", "idx": 0, "total": 2}),
        ("flaw", {"criterion": "groundedness", "severity": "high"}),
        ("verdict", {"score": 0.39, "band": "block", "flaws": [{"criterion": "groundedness"}],
                     "run_id": "r1", "latency_ms": 12}),
        ("done", {"run_id": "r1"}),
    ]
    events = list(_parse_sse_lines(_sse_lines(*blocks)))
    names = [e["event"] for e in events]
    assert names == ["received", "scanning", "flaw", "verdict", "done"]
    # heartbeat comment did not produce an event
    assert all(e["event"] != "message" for e in events)
    assert events[0]["data"]["run_id"] == "r1"
    assert events[3]["data"]["band"] == "block"


def test_parse_sse_multiline_data():
    lines = iter(["event: verdict", "data: {", 'data:   "score": 0.5', "data: }", ""])
    events = list(_parse_sse_lines(lines))
    assert events == [{"event": "verdict", "data": {"score": 0.5}}]


def test_score_stream_fallback_without_httpx():
    """No httpx → _stream_fallback replays the blocking endpoint as SSE events."""
    api = FakeAPI([_v(39, "block", [_flaw("groundedness")])])
    c = EvalClient(base_url="http://x", transport=api)
    events = list(c._stream_fallback({
        "modality": "text", "rubric_id": "rb",
        "artifact_ref": "inline:sha256:x", "artifact_parts": []}))
    names = [e["event"] for e in events]
    assert names[0] == "received"
    assert names[-1] == "done"
    assert "flaw" in names
    verdict = next(e["data"] for e in events if e["event"] == "verdict")
    assert verdict["band"] == "block" and verdict["run_id"] == "run-1"


def test_score_stream_delegates_to_stream_endpoint(monkeypatch):
    """score_stream POSTs to /api/v1/eval/stream and yields parsed events."""
    captured = {}

    def fake_stream_sse(self, path, body):
        captured["path"] = path
        captured["body"] = body
        yield {"event": "received", "data": {"run_id": "r9"}}
        yield {"event": "verdict", "data": {"score": 0.7, "band": "ship", "run_id": "r9"}}
        yield {"event": "done", "data": {"run_id": "r9"}}

    monkeypatch.setattr(EvalClient, "_stream_sse", fake_stream_sse)
    c = EvalClient(base_url="http://x")
    events = list(c.score_stream("some text", rubric_id="rb"))
    assert captured["path"] == "/api/v1/eval/stream"
    assert captured["body"]["rubric_id"] == "rb"
    assert captured["body"]["modality"] == "text"
    assert [e["event"] for e in events] == ["received", "verdict", "done"]
