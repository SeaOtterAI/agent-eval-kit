# The agent ↔ critic protocol

The contract the SDK, the MCP server, and the eval API all build against. Any
agent can submit its work to an adversarial critic, get back conditional,
multimodal, localized feedback, and iterate until the work passes the gate.

## 1. Why this exists

A frontier agent left to its own judgement ships its first answer. An acceptance
loop only works if the agent can *actually call the critic, understand what's
wrong, and fix it* — in the agent's own runtime, in whatever modality it
produces, conditioned on the organisation's policy, the prompt it was given, and
the reference files it must respect.

- **For the agent:** discover the policy → submit work (any modality) → read
  structured + rich feedback → submit a revision → repeat until `ship`.
- **For the org:** feedback is **conditional** — on the org's acceptance policy,
  the agent's prompt/intent, and reference files (exemplars, brand, source of
  truth). Not a generic "is this good" score; *your* bar.
- **Not finance-only.** Code, support replies, claims decisions, research memos,
  documents, decks, spreadsheets, images, video, marketing, design, outcome
  metrics — any work an agent produces, in any industry.

## 2. The loop (the whole product, in five calls)

```
produce ──▶ eval_score(work, policy, intent, references) ──▶ verdict
                                                              │
        ┌─────────────────────────────────────────────────────┘
        ▼
   band == "ship"?  ── yes ──▶ done (signed acceptance evidence)
        │ no
        ▼
   revise using verdict.flaws + verdict.upgrades + feedback artifacts
        │
        ▼
   eval_iterate(run_id, revised_work) ──▶ verdict + delta(resolved/new) ──┐
        ▲                                                                  │
        └──────────────────────────────────────────────────────────────┘
```

The critic is **hostile-by-default**: aligned to find reasons to *block*, never
to flatter. The verdict is authoritative — the harness agent never invents or
inflates a grade.

## 3. Three transports, one contract

| Transport | Who | How |
|---|---|---|
| **MCP (stdio)** | Any MCP-speaking agent (Claude, Codex, Cursor, custom) | `python -m agent_eval_kit.mcp_server` |
| **MCP (HTTP/SSE)** | Remote/hosted agents | the hosted gateway's `/mcp` (streamable-HTTP), same tools |
| **HTTP/REST** | Any runtime that speaks HTTP | `POST {AGENT_EVAL_API_URL}/api/v1/eval/*` |
| **SDK** | Python agents | `pip install agent-eval-kit` → `EvalClient(...).loop(produce_fn)` |

The MCP server and the SDK are thin, well-tested wrappers over the same HTTP eval
API, which owns the critic call, the cost gate, conditioning, rich-return
rendering, localization, and the audit record. **One implementation of the
contract; everything else delegates.**

## 4. MCP tool surface (`agent_eval_kit.mcp_server`)

All tools are `readOnlyHint=True` (grading has no side effects → codex/Claude
auto-approve in non-interactive runs) and return JSON the agent can act on.

### `eval_list_policies()`
Discover what the agent can be graded against. Returns rubrics (per-modality
acceptance criteria) **and** org acceptance policies (the org-conditioned bar).

### `eval_score(work, modality?, rubric_id?, policy_id?, intent?, prompt?, references?, locale?, return_artifacts?)`
Submit work of **any modality** and get the verdict. `work` accepts inline
text/code, a file path (the SDK reads + converts pdf/docx/pptx/xlsx/png/mp4/wav…),
a base64 data URL, or a list of parts. `modality` is auto-detected when omitted.
Conditioning:
- **organisation** → `policy_id` (org acceptance policy) and/or `rubric_id`,
- **prompt** → `prompt`/`intent` (what the agent was asked to do),
- **files** → `references` (labelled exemplars / brand / source-of-truth).

Returns a **Verdict** (§6). `return_artifacts=true` adds rendered, localized
rich-feedback artifacts (annotated image, annotated PDF, video marker track,
markdown report).

### `eval_iterate(run_id, work, prompt?, return_artifacts?)`
Submit a revision against a prior run. Returns the new Verdict **plus a delta**
(`resolved_flaws`, `new_flaws`, `persisted_flaws`, `score_change`,
`ready_to_ship`). `ready_to_ship` is the agent's stop signal.

### `eval_score_async(...)` / `eval_job_result(job_id)`
Non-blocking grading: submit returns a `job_id` immediately; poll until
`completed`. Use when a grade may be slow (cold critic, large/multimodal artifact,
or deep `mode="agentic"` grading) so the call never blocks or times out.

### `eval_score_stream(...)`
Grade via the low-latency streaming endpoint. Drains the
`received → scanning → flaw → verdict → done` event stream server-side and returns
the final verdict plus a progress trace.

### `eval_score_workflow(steps, topology?, policy_id?, locale?)`
Score an end-to-end agent **trajectory** (per-step modality critics + topology-
aware composite + chain critique).

### `eval_feedback_artifact(ref)`
Fetch a rendered rich-feedback artifact by ref: returns the bytes (base64) + mime
+ caption + anchors.

## 5. HTTP contract

`POST {AGENT_EVAL_API_URL}/api/v1/eval/runs` and
`POST .../api/v1/eval/runs/{id}/iterate` accept the conditioning fields:

| Field | Type | Meaning |
|---|---|---|
| `policy_id` | `str?` | Org acceptance policy id — conditions rubric, thresholds, references, locale on the **organisation** |
| `locale` | `str` (default `en`) | Localise feedback text (en, ja, zh-CN, ko, de, fr, es, pt, it, ar, …) |
| `return_feedback_artifacts` | `bool` (default false) | Render annotated rich artifacts into the response |

Responses carry:
- `verdict.feedback_artifacts: [{kind, modality, mime, ref, data_url?, caption, anchors[]}]`
- `iterate` responses carry `verdict` + `delta` (resolved/new/persisted flaws, `score_change`, `ready_to_ship`).

## 6. Verdict schema (the thing the agent acts on)

```json
{
  "score": 0-100,                       // lower = more flawed
  "band": "ship | route_to_fix | quarantine | block",
  "decision": "ship | route_to_fix | quarantine | block",
  "flaws": [
    { "criterion": "string",            // which acceptance criterion
      "severity": "high | med | low",
      "evidence": "string",             // the offending span / quote
      "detail": "string",               // what's wrong, localized
      "anchor": {                       // WHERE — spatial localization
        "kind": "bbox|point|span|cell|slide|timestamp|page",
        "bbox": [x0,y0,x1,y1],          // normalized [0,1] for image/page
        "timestamp": 12.5,              // seconds, for video/audio
        "cell": "B7", "slide": 3, "page": 2, "span": [start,end] } } ],
  "upgrades": [ { "action": "string", "target_criterion": "string", "draft": "string" } ],
  "rationale": "string",                // localized
  "feedback_artifacts": [ {...} ],      // §5, when requested
  "model_id": "string", "source": "string", "cost_usd": 0.0, "locale": "en"
}
```

**Localization is two things, both first-class:**
1. **Spatial** — every flaw carries an `anchor` ("where": bbox / timestamp /
   cell / slide / page / span) so the agent can target its fix.
2. **Language** — `locale` translates `detail` / `rationale` / `upgrades` so the
   feedback lands in the org's working language.

## 7. Rich multimodal returns

When `return_artifacts=true`, the verdict is rendered into artifacts an agent can
both consume and hand to a human:

| Input modality | Rich return |
|---|---|
| image | annotated PNG with flaw bounding boxes + a markdown report |
| pdf / deck / document | annotated pages (PNG/PDF) + per-page markdown |
| spreadsheet | flagged-cell map + markdown |
| video | timestamped marker track (WebVTT/JSON) + markdown |
| audio | timestamped marker track + markdown |
| text / code | inline-anchored markdown review (span highlights) |

Every return also includes the canonical Verdict (`feedback.json`) so a model
that can't render media still gets every anchor.
