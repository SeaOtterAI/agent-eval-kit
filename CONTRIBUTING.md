# Contributing to agent-eval-kit

Thanks for your interest in improving the kit. This project is small and aims to
stay that way — a thin, well-tested SDK + MCP surface over an eval API.

## Development setup

```bash
git clone https://github.com/SeaOtterAI/agent-eval-kit
cd agent-eval-kit
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest -q
ruff check .
```

The test suite requires **no network** — a fake transport stands in for the eval
API. New behaviour must come with a test that exercises it through that fake.

## Guidelines

- **Keep it thin.** The eval API owns the critic call, conditioning, rich render,
  localization, cost gate, and audit. The SDK and MCP server are thin, typed
  wrappers over the same HTTP contract — one implementation of the contract,
  everything else delegates. Don't add a second source of truth.
- **Backend-agnostic.** Nothing should hardcode a single hosted backend's
  internal details. Endpoints, key prefixes, and auth checks are configurable.
- **No required third-party deps for the SDK.** It works on stdlib `urllib`;
  `httpx` is an optional accelerator. The MCP server and gateway may require
  `mcp` / `cryptography` / `uvicorn` (declared as extras).
- **Fail closed.** A critic outage is never a silent pass — surface the error and
  a non-shipping band.
- **Style.** `ruff` is the linter/formatter of record; run `ruff check .` before
  pushing. Match the surrounding code.

## Pull requests

1. Branch off `main`.
2. Add or update tests; keep `pytest -q` and `ruff check .` green.
3. Update `CHANGELOG.md` under an "Unreleased" heading.
4. Open a PR with a clear description of the change and its motivation.

By contributing, you agree that your contributions are licensed under the
[Apache License 2.0](LICENSE).
