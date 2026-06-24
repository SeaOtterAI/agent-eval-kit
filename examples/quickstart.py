"""Grade one work product and print the verdict.

    AGENT_EVAL_API_KEY=sk-otter-... python examples/quickstart.py
"""

from __future__ import annotations

from agent_eval_kit import EvalClient

critic = EvalClient()  # reads AGENT_EVAL_API_URL / AGENT_EVAL_API_KEY from env

work = """\
# Q3 Incident Postmortem

We had an outage on the 12th. It is fixed now. Sorry for the inconvenience.
"""

verdict = critic.score(work, prompt="Draft the Q3 incident postmortem")

print(verdict.summary())
print()
print("ready to ship:", verdict.ready_to_ship)
for flaw in verdict.flaws:
    print(" -", flaw.human())
