"""Drive the produce → grade → revise loop until the critic says ship.

Replace ``my_agent_revise`` with a call to your own model/agent — it receives the
current Verdict and returns the revised work.

    AGENT_EVAL_API_KEY=sk-otter-... python examples/loop.py
"""

from __future__ import annotations

from agent_eval_kit import EvalClient, Verdict


def my_agent_revise(verdict: Verdict) -> str:
    """Stand-in for your agent. Use verdict.flaws + verdict.upgrades to revise."""
    fixes = "\n".join(f"- addressed: {u.action}" for u in verdict.upgrades if u.action)
    return f"# Q3 Incident Postmortem (revised)\n\n{fixes or 'expanded the analysis'}\n"


def main() -> None:
    critic = EvalClient()

    def on_round(i: int, v: Verdict) -> None:
        print(f"round {i}: {v.band} (score {v.score:.0f}, {len(v.flaws)} flaws)")

    final = critic.loop(
        produce=my_agent_revise,
        work="# Q3 Incident Postmortem\n\nWe had an outage. It is fixed.\n",
        modality="document",
        prompt="Draft the Q3 incident postmortem",
        max_rounds=5,
        target_band="ship",
        on_round=on_round,
    )

    print()
    print("final band:", final.band, "| score:", f"{final.score:.0f}",
          "| ready to ship:", final.ready_to_ship)


if __name__ == "__main__":
    main()
