# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **`agent-eval` CLI — make external validation an everyday habit.**
  - `agent-eval init <claude|codex|openclaw|cursor|hermes|git|all>` wires OtterScore into a
    harness's end-of-task hook (Claude Code/Codex `Stop`, OpenClaw `agent_end`, git `pre-push`),
    registers the MCP `otter_score` tool, and adds a standing-instruction block to
    `AGENTS.md`/`CLAUDE.md`/`SOUL.md`. Idempotent.
  - `agent-eval validate` grades the git diff / named files / a harness hook payload with the
    hostile critic and exits `0` (ship) or `2` with located flaws (block) — the universal block
    signal every supported harness feeds back into the loop. Multimodal (code/text/image/deck/
    sheet/doc/audio/video). Fails open on infra errors, closed on a real verdict.
  - Bundles the canonical stdlib validator + installer + standing templates (the same files
    hosted at `seaotter.ai/install.sh`).

## [0.1.0] — 2026-06-24

Initial public release. Extracted and generalized from SeaOtter's internal
`otterloop` package into a backend-agnostic, open-source kit.

### Added
- `EvalClient` — the agent ↔ critic loop SDK (`score`, `iterate`, `loop`,
  async `submit_async`/`poll_job`, `score_stream`, `score_workflow`, discovery).
- Typed verdict contract: `Verdict`, `Flaw`, `Anchor`, `Upgrade`, `Delta`,
  `FeedbackArtifact`, `IterationResult`.
- Modality auto-detection / artifact-part normalization for text, code, image,
  document, deck, spreadsheet, video, and audio.
- MCP server (`agent_eval_kit.mcp_server`) exposing the loop as `eval_*` tools.
- Hosted multi-tenant streamable-HTTP MCP gateway (`agent_eval_kit.server_http`)
  with stateless OAuth 2.1 + PKCE and a paste-key consent page.
- Configuration via `AGENT_EVAL_*` environment variables; backend defaults to
  SeaOtter's hosted OtterScore critic and is overridable. The hosted gateway's
  OAuth scope label is configurable via `AGENT_EVAL_OAUTH_SCOPE` (default `eval`),
  so an overlay deployment can preserve a product-specific scope.

### Changed from the internal `otterloop` package
- Renamed package `otterloop` → `agent_eval_kit`, client `OtterLoopClient` →
  `EvalClient`, env prefix `OTTERLOOP_*` → `AGENT_EVAL_*`, MCP tools `otter_*` →
  `eval_*`.
- Relicensed from Proprietary to Apache-2.0; adopted a `src/` package layout.

### Removed
- The SeaOtter-specific Google/Firebase sign-in path in the OAuth consent flow
  (paste-key consent only; key prefix + auth-check endpoint are configurable).
- The SeaOtter-platform community / leaderboard / Raft MCP tools and the legacy
  `otterscore_grade` back-compat alias — out of scope for a backend-agnostic kit.
