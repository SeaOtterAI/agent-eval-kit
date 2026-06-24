"""Make ``import agent_eval_kit`` resolve to ``src/`` when the package is not
installed, so ``pytest`` works from a fresh clone without ``pip install -e .``."""

import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

os.environ.setdefault("AGENT_EVAL_OAUTH_SECRET", "test-secret-do-not-use-in-prod")
