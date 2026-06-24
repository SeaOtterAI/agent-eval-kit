"""BYOP — the SDK forwards a caller's own evaluation policy to the eval API.

No network: a fake transport records the request body so we pin that ``policy``,
``critic_prompt``, and the stored-policy author/list methods send the right shape.
"""

from __future__ import annotations

from agent_eval_kit import EvalClient


class FakeAPI:
    def __init__(self):
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, body))
        if url.endswith("/api/v1/eval/runs"):
            return 201, {"run_id": "r1", "first_iteration": {"critic_verdict": {"score": 0.5, "band": "fix"}}}
        if url.endswith("/api/v1/eval/jobs"):
            return 202, {"job_id": "j1", "status": "queued"}
        if url.endswith("/api/v1/policies") and method == "GET":
            return 200, {"policies": [{"id": "acme", "version": 2}]}
        if url.endswith("/api/v1/policies") and method == "POST":
            return 201, {"id": body["id"], "version": 1}
        if "/api/v1/policies/" in url and method == "PUT":
            return 200, {"id": "acme", "version": 2}
        return 404, {"detail": "nf"}


_POLICY = {
    "directives": ["Block uncited claims"],
    "criteria": [{"name": "Cited", "definition": "cite it"}],
    "bands": {"ship": 0.95},
    "hard_rules": [{"kind": "forbidden_term", "params": {"terms": ["guaranteed"]}}],
}


def test_score_forwards_inline_policy_and_critic_prompt():
    api = FakeAPI()
    c = EvalClient(base_url="http://x", transport=api)
    c.score("draft", policy=_POLICY, critic_prompt="be a regulator")
    body = api.calls[-1][2]
    assert body["policy"] == _POLICY
    assert body["critic_prompt"] == "be a regulator"


def test_score_omits_policy_when_absent():
    api = FakeAPI()
    EvalClient(base_url="http://x", transport=api).score("draft")
    body = api.calls[-1][2]
    assert "policy" not in body and "critic_prompt" not in body


def test_submit_async_forwards_inline_policy():
    api = FakeAPI()
    EvalClient(base_url="http://x", transport=api).submit_async("draft", policy=_POLICY)
    body = api.calls[-1][2]
    assert body["policy"] == _POLICY and body["mode"] == "one_shot"


def test_list_stored_policies():
    api = FakeAPI()
    pols = EvalClient(base_url="http://x", transport=api).list_stored_policies()
    assert pols == [{"id": "acme", "version": 2}]
    assert api.calls[-1][1].endswith("/api/v1/policies")


def test_upsert_policy_creates_then_updates_on_conflict():
    api = FakeAPI()
    c = EvalClient(base_url="http://x", transport=api)
    out = c.upsert_policy("acme", name="Acme", conditioning={"policy_prior": "strict"},
                          bands={"ship": 0.9})
    assert out["id"] == "acme"
    assert api.calls[-1][0] == "POST" and api.calls[-1][2]["conditioning"]["policy_prior"] == "strict"


def test_upsert_policy_falls_back_to_put_on_409():
    class Conflict(FakeAPI):
        def __call__(self, method, url, headers, body):
            self.calls.append((method, url, body))
            if method == "POST" and url.endswith("/api/v1/policies"):
                return 409, {"detail": "exists"}
            if method == "PUT":
                return 200, {"id": "acme", "version": 3}
            return 404, {"detail": "nf"}

    api = Conflict()
    out = EvalClient(base_url="http://x", transport=api).upsert_policy("acme")
    assert out["version"] == 3 and api.calls[-1][0] == "PUT"
