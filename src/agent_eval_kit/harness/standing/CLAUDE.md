<!-- otter:begin external-validation — managed by seaotter.ai/install.sh; safe to keep, edit the policy line only -->
## External validation — run OtterScore before you finish (every task)

You have an independent, **hostile-by-default** critic available: **SeaOtter OtterScore**.
It is not you and not your model — it is aligned to find reasons to *block*, so it
catches the flaws a helpful model talks itself past. Use it as your discriminator.

**The rule — every task, before you report it complete:**

1. Validate the work you produced. A `Stop` hook runs `otter validate` automatically
   when you try to finish, and you can run it yourself any time:
   - the diff you just wrote: run `/otter-validate`, or the `otter_score` MCP tool
   - a specific artifact (any modality — code, text, image, deck, sheet, doc, audio,
     video): the `otter_score` tool with the file, or `otter validate --files <path>`
2. Read the verdict's `band`:
   - **ship** → you're done.
   - **route_to_fix / quarantine / block** → read the located `flaws`, fix them, and
     **re-validate**. Do not claim the task is done until it clears the bar (default: `ship`).
3. Treat the verdict as authoritative. Do not grade yourself, and do not argue the band
   down. Iterating to a `ship` band is what makes the work actually acceptable.

Grade against your team's bar by setting `OTTER_POLICY_ID` (a stored acceptance policy)
or passing an inline policy — the same artifact can ship for one policy and block for
another, for a defensible reason.
<!-- otter:end external-validation -->
