# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
  SeaOtter's hosted OtterScore critic and is overridable.

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
