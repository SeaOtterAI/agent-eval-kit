# Hosted agent-eval-kit MCP gateway — streamable-HTTP, multi-tenant
# (agent_eval_kit.server_http:app). Lightweight: the package + the MCP SDK +
# httpx + cryptography + uvicorn. No private deps, no build args, no secrets baked in.
FROM python:3.12-slim

WORKDIR /srv
COPY . /srv/agent-eval-kit
RUN pip install --no-cache-dir "/srv/agent-eval-kit[server]"

ENV PORT=8080 \
    AGENT_EVAL_API_URL=https://api.seaotter.ai
EXPOSE 8080

# AGENT_EVAL_OAUTH_SECRET must be supplied at runtime (the OAuth flow fails closed
# without it). Your platform injects $PORT; uvicorn serves the MCP at /mcp.
CMD ["sh", "-c", "uvicorn agent_eval_kit.server_http:app --host 0.0.0.0 --port ${PORT}"]
