# MCP setup

`agent-eval-kit` ships an MCP server that exposes the agent ↔ critic loop as
first-class tools. Any MCP-speaking agent gets `eval_score`, `eval_iterate`,
`eval_score_async`, `eval_job_result`, `eval_score_stream`, `eval_score_workflow`,
`eval_feedback_artifact`, and `eval_list_policies`.

All grading tools are read-only (no side effects) so they auto-approve in
non-interactive runs.

## Install

```bash
pip install "agent-eval-kit[mcp]"
```

## Claude Desktop / Claude Code / Cursor (`.mcp.json`)

```json
{ "mcpServers": { "agent-eval": {
    "command": "python",
    "args": ["-m", "agent_eval_kit.mcp_server"],
    "env": {
      "AGENT_EVAL_API_URL": "https://api.seaotter.ai",
      "AGENT_EVAL_API_KEY": "sk-otter-...",
      "AGENT_EVAL_POLICY_ID": "acme-prod-acceptance"
    }
} } }
```

Claude Code, one-liner:

```bash
claude mcp add agent-eval -e AGENT_EVAL_API_KEY=sk-otter-... -- python -m agent_eval_kit.mcp_server
```

## Codex (`~/.codex/config.toml`)

```toml
[mcp_servers.agent-eval]
command = "python"
args = ["-m", "agent_eval_kit.mcp_server"]
env = { AGENT_EVAL_API_URL = "https://api.seaotter.ai", AGENT_EVAL_API_KEY = "sk-otter-..." }
```

## Remote (hosted) MCP — no install

If you run the hosted gateway (see [self-hosting.md](self-hosting.md)), connect by
URL instead of installing the package:

```json
{ "mcpServers": { "agent-eval": {
    "url": "https://your-gateway.example.com/mcp",
    "headers": { "Authorization": "Bearer sk-otter-..." }
} } }
```

claude.ai custom connectors and the Anthropic Connector Directory require OAuth;
the gateway's OAuth 2.1 + PKCE flow handles that — add the connector by URL and
the gateway's consent page collects the key.

## Suggested system-prompt nudge

> Before treating any work product as done, grade it with `eval_score` and keep
> calling `eval_iterate` until `ready_to_ship` is true. The verdict is
> authoritative — do not grade yourself.
