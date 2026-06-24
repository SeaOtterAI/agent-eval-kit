"""agent-eval-kit — wire any agent into an adversarial work-quality critic.

Submit work in any modality, get conditional adversarial feedback (conditioned on
your acceptance policy, the prompt the agent was given, and reference files), and
iterate until the work passes the gate.

    from agent_eval_kit import EvalClient

    critic = EvalClient(policy_id="acme-prod-acceptance")
    verdict = critic.score(my_work)            # any modality, auto-detected
    if not verdict.ready_to_ship:
        for flaw in verdict.flaws:
            print(flaw.human())                # what's wrong + where (anchor)

See ``docs/protocol.md`` for the full agent ↔ critic protocol.
"""

from __future__ import annotations

from .client import DEFAULT_BASE_URL, EvalClient, EvalError
from .types import (
    Anchor,
    Delta,
    FeedbackArtifact,
    Flaw,
    IterationResult,
    Upgrade,
    Verdict,
)

__version__ = "0.1.0"

__all__ = [
    "EvalClient",
    "EvalError",
    "DEFAULT_BASE_URL",
    "Verdict",
    "Flaw",
    "Anchor",
    "Upgrade",
    "Delta",
    "FeedbackArtifact",
    "IterationResult",
    "__version__",
]
