# agent-eval-kit

**Wire any agent into an adversarial work-quality critic — and iterate until the
work passes the gate.**

[![CI](https://github.com/SeaOtterAI/agent-eval-kit/actions/workflows/ci.yml/badge.svg)](https://github.com/SeaOtterAI/agent-eval-kit/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](pyproject.toml)

Agents produce work — code, support replies, research memos, documents, decks,
images, video — faster than anyone can review it. `agent-eval-kit` is the
calling surface for the missing step: submit the work to a **hostile-by-default
critic**, get **structured, multimodal, localized feedback** (conditioned on your
acceptance policy, the prompt the agent was given, and reference files), and
**iterate until it ships**.

> The only way to produce good agentic work is to iterate on the feedback a
> hostile critic imposes. This kit is that loop, made callable in one line.

One contract, three ways to call it: a **Python SDK**, an **MCP server** (stdio +
hosted HTTP), and plain **HTTP**. It is backend-agnostic — it defaults to
[SeaOtter](https://seaotter.ai)'s hosted OtterScore critic, and you can point
`AGENT_EVAL_API_URL` at any compatible eval API (including your own on-prem one).

---

## Install

```bash
pip install agent-eval-kit            # pure stdlib HTTP
pip install "agent-eval-kit[http]"    # + httpx (recommended; enables streaming)
pip install "agent-eval-kit[mcp]"     # + the MCP server
pip install "agent-eval-kit[server]"  # + the hosted HTTP gateway (OAuth)
```

## Make it automatic — validate every task (the habit, not a one-off)

The highest-leverage way to use the critic is to wire it into your harness's **end-of-task
hook**, so the work an agent produces is graded and the finish is **blocked until it clears
the bar** — no need to remember.

```bash
agent-eval init claude     # Claude Code  — Stop hook + MCP otter_score + CLAUDE.md rule
agent-eval init codex      # Codex        — MCP + AGENTS.md (+ optional blocking Stop hook)
agent-eval init openclaw   # OpenClaw     — agent_end + SOUL.md
agent-eval init cursor     # Cursor       — MCP + always-on rule
agent-eval init hermes     # Hermes/OpenAI-compatible — tools spec + system-prompt fragment
agent-eval init git        # ANY harness  — git pre-push gate

# grade work yourself any time (exit 0 = ship, exit 2 = blocked with flaws):
agent-eval validate --source diff           # the diff you just wrote
agent-eval validate --files report.pdf      # any artifact, any modality
```

No-install equivalent (stdlib python3 + curl): `curl -fsSL https://seaotter.ai/install.sh | sh -s -- claude`.

## Python SDK

```python
from agent_eval_kit import EvalClient

critic = EvalClient(
    api_key="sk-otter-...",              # or env AGENT_EVAL_API_KEY
    policy_id="acme-prod-acceptance",    # org conditioning (optional)
    locale="ja",                          # localized feedback (optional)
)

# One-shot grade — modality auto-detected; file paths / data URLs / text accepted.
verdict = critic.score(
    "path/to/postmortem.pdf",
    prompt="Draft the Q3 incident postmortem",
    references=["file://gold-postmortem.md", "file://brand-guide.pdf"],
)
print(verdict.band, verdict.score)          # "route_to_fix", 64.0
for flaw in verdict.flaws:
    print(flaw.human())                     # "[high] missing-rca (page 2): ..."

# Iterate until the critic says ship — the whole loop in one call.
final = critic.loop(
    produce=lambda v: my_agent.revise(v),   # v is the Verdict; return revised work
    work=my_agent.first_draft(),
    modality="document",
    max_rounds=5,
    target_band="ship",
)
print(final.ready_to_ship, final.score)     # True, 91.0
```

`score()` accepts inline text/code, a file path (`pdf/docx/pptx/xlsx/png/mp4/wav…`),
a `data:` URL, raw bytes, or a list of parts. The modality is auto-detected.

## MCP (Claude / Codex / Cursor)

`.mcp.json` (Claude / Cursor) or `config.toml` `[mcp_servers.agent-eval]` (Codex):

```json
{ "mcpServers": { "agent-eval": {
    "command": "python", "args": ["-m", "agent_eval_kit.mcp_server"],
    "env": {
      "AGENT_EVAL_API_URL": "https://api.seaotter.ai",
      "AGENT_EVAL_API_KEY": "sk-otter-...",
      "AGENT_EVAL_POLICY_ID": "acme-prod-acceptance"
    } } } }
```

Tools the agent gets: `eval_list_policies`, `eval_score`, `eval_iterate`,
`eval_score_async`, `eval_job_result`, `eval_score_stream`, `eval_score_workflow`,
`eval_feedback_artifact`. All read-only (grading has no side effects → auto-approved
in non-interactive runs). See [docs/mcp.md](docs/mcp.md).

## HTTP (any runtime)

```bash
curl -s https://api.seaotter.ai/api/v1/eval/runs \
  -H "Authorization: Bearer $AGENT_EVAL_API_KEY" -H 'Content-Type: application/json' \
  -d '{ "modality":"text", "policy_id":"acme-prod-acceptance", "locale":"ja",
        "user_prompt":"Draft the Q3 incident postmortem",
        "artifact_parts":[{"mime_type":"text/plain","text":"..."}],
        "return_feedback_artifacts": true }'
```

## What you get back (`Verdict`)

- `score` (0–100, lower = more flawed), `band` (`ship` / `route_to_fix` /
  `quarantine` / `block`), `ready_to_ship`.
- `flaws[]`: each with `criterion`, `severity`, `evidence`, `detail` (localized),
  and an `anchor` (**where**: bbox / timestamp / cell / slide / page / span).
- `upgrades[]`: concrete fixes.
- `feedback_artifacts[]` (when `return_artifacts=true`): rendered annotated
  image / PDF / video marker track / markdown the agent can render *and*
  machine-read.

The critic is the authority. The agent acts on the verdict; it never grades
itself or invents a pass. Full schema: [docs/protocol.md](docs/protocol.md).

## Self-host the MCP gateway

`agent-eval-kit` ships a hosted, multi-tenant streamable-HTTP MCP gateway with
OAuth 2.1 + PKCE — so you can add it as a remote connector on claude.ai or ChatGPT
without shipping a package to anyone:

```bash
AGENT_EVAL_OAUTH_SECRET=$(openssl rand -hex 32) \
AGENT_EVAL_API_URL=https://api.seaotter.ai \
uvicorn agent_eval_kit.server_http:app --host 0.0.0.0 --port 8080
```

It accepts both a raw API key (Claude Code) and the OAuth flow (claude.ai custom
connector / the Connector Directory). See [docs/self-hosting.md](docs/self-hosting.md).

## Configuration

| Env var | Default | Meaning |
|---|---|---|
| `AGENT_EVAL_API_URL` | `https://api.seaotter.ai` | eval API base URL |
| `AGENT_EVAL_API_KEY` | — | bearer key for the eval API |
| `AGENT_EVAL_POLICY_ID` | — | org acceptance policy to condition on |
| `AGENT_EVAL_RUBRIC_ID` | — | default rubric |
| `AGENT_EVAL_LOCALE` | `en` | feedback language |

Gateway-only: `AGENT_EVAL_OAUTH_SECRET` (required), `AGENT_EVAL_OAUTH_ALLOWED_HOSTS`,
`AGENT_EVAL_API_KEY_PREFIX`, `AGENT_EVAL_AUTH_CHECK_PATH` — see
[docs/self-hosting.md](docs/self-hosting.md).

## Develop

```bash
pip install -e ".[dev]"
pytest -q
ruff check .
```

No network is required for the test suite — a fake transport stands in for the
eval API.

## Learn more

- [SeaOtter](https://seaotter.ai) — the acceptance layer for enterprise AI agent work
- [What is AI agent evaluation?](https://seaotter.ai/docs/ai-agent-evaluation) — the pillar guide
- [Best AI agent evaluation tools (2026)](https://seaotter.ai/docs/best-ai-agent-evaluation-tools) — the category, ranked by the job each tool wins
- [Compare SeaOtter](https://seaotter.ai/docs/compare) — head-to-head vs DeepEval, Ragas, Arize Phoenix, Braintrust, LangSmith, Galileo, Patronus, and Langfuse
- [AI agent evaluation glossary](https://seaotter.ai/docs/glossary)
- [LLM-as-a-judge](https://seaotter.ai/docs/llm-as-a-judge) · [AI agent quality gate](https://seaotter.ai/docs/ai-agent-quality-gate)
- [llms.txt](https://seaotter.ai/llms.txt) — the machine-readable agent contract
- [Developer / agent console](https://seaotter.ai/developers) · [Live demo](https://seaotter.ai/demo/eval)

## License

[Apache-2.0](LICENSE). "SeaOtter" and "OtterScore" are trademarks of SeaOtter;
see [NOTICE](NOTICE).
