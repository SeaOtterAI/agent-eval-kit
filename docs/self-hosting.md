# Self-hosting the MCP gateway

`agent_eval_kit.server_http` is a hosted, multi-tenant, streamable-HTTP MCP
gateway. Run it in front of an eval API so agents can add it as a **remote
connector** (claude.ai custom connector, the Anthropic Connector Directory, or a
ChatGPT custom connector) with no package install.

It is stateless: each request is self-contained (no in-memory session store), so
it scales to zero and across instances. Per-request auth resolves the caller's
credential and bills that tenant via the eval API.

## Run it

```bash
pip install "agent-eval-kit[server]"

export AGENT_EVAL_OAUTH_SECRET=$(openssl rand -hex 32)   # required, keep secret
export AGENT_EVAL_API_URL=https://api.seaotter.ai        # the eval API to front
uvicorn agent_eval_kit.server_http:app --host 0.0.0.0 --port 8080
```

Or with Docker:

```bash
docker build -t agent-eval-gateway .
docker run -p 8080:8080 \
  -e AGENT_EVAL_OAUTH_SECRET=$(openssl rand -hex 32) \
  -e AGENT_EVAL_API_URL=https://api.seaotter.ai \
  agent-eval-gateway
```

## Endpoints

| Path | Purpose |
|---|---|
| `GET /` | health / landing JSON |
| `POST /mcp` | the MCP streamable-HTTP transport (credential required) |
| `GET /.well-known/oauth-protected-resource` | RFC 9728 |
| `GET /.well-known/oauth-authorization-server` | RFC 8414 |
| `POST /oauth/register` | RFC 7591 dynamic client registration (stateless) |
| `GET/POST /oauth/authorize` | consent (paste an API key) |
| `POST /oauth/token` | authorization_code + refresh_token grants |
| `GET /.well-known/openai-apps-challenge` | served only when `OPENAI_APPS_CHALLENGE_TOKEN` is set |

## How auth works

The gateway accepts **both**:

1. **A raw API key** — `Authorization: Bearer <key>` (or `x-api-key: <key>`).
   Used by Claude Code and the Messages-API MCP connector.
2. **An OAuth 2.1 access token** — for claude.ai custom connectors / the Connector
   Directory, which only accept OAuth. The access token is a Fernet blob that
   *encrypts* the user's API key; the gateway decrypts it back and forwards it to
   the eval API. PKCE S256 is required; authorization codes live 90s, access
   tokens 1h, refresh tokens 30d. Nothing is stored server-side.

A request to `/mcp` without a valid credential returns `401` with a
`WWW-Authenticate` header pointing at the protected-resource metadata, so an
OAuth client can discover the authorization server.

## Configuration

| Env var | Default | Meaning |
|---|---|---|
| `AGENT_EVAL_OAUTH_SECRET` | — (**required**) | seeds the Fernet key for codes/tokens; the flow fails closed if unset |
| `AGENT_EVAL_API_URL` | `https://api.seaotter.ai` | the eval API the gateway fronts |
| `AGENT_EVAL_OAUTH_ALLOWED_HOSTS` | claude.ai, claude.com, chatgpt.com, openai.com + localhost | extra redirect-callback hosts, comma-separated |
| `AGENT_EVAL_API_KEY_PREFIX` | `sk-otter-` | bearer prefix recognised as a raw key (vs an OAuth token) |
| `AGENT_EVAL_AUTH_CHECK_PATH` | `/api/v1/billing/status` | authed, side-effect-free GET used to validate a pasted key before issuing a code |
| `AGENT_EVAL_SIGNUP_URL` | `https://seaotter.ai/developers` | "get a key" link on the consent page |
| `AGENT_EVAL_DOCS_URL` | the repo URL | documentation link in OAuth metadata |
| `AGENT_EVAL_OAUTH_SCOPE` | `eval` | scope label advertised in OAuth metadata + token responses |
| `AGENT_EVAL_GATEWAY_HOST` | `localhost` | host used to build OAuth issuer metadata when no `Host`/`x-forwarded-proto` header is forwarded |
| `OPENAI_APPS_CHALLENGE_TOKEN` | — | enables the ChatGPT Apps domain-verification route |

## Security notes

- Keep `AGENT_EVAL_OAUTH_ALLOWED_HOSTS` limited to the callbacks you actually use
  — it is the open-redirect / code-exfiltration guard. `redirect_uri`s containing
  userinfo (`@`) or backslashes are rejected (parser-differential hardening).
- The consent page is served with `X-Frame-Options: DENY`, a strict
  `Content-Security-Policy`, `Cache-Control: no-store`, and `Referrer-Policy:
  no-referrer` so the credential form is never framable, cached, or leaked.
- **Stateless tradeoff — authorization codes are replayable within their 90s TTL.**
  The flow holds no server-side state, so a code is not single-use as strict
  OAuth 2.1 requires; PKCE binds it to the client + verifier, and the TTL is short
  (90s code / 1h access / 30d refresh). For strict single-use, front the gateway
  with a shared store that records consumed code ids, or shorten exposure further.
- Sessions are stateless tokens; to invalidate **all** live tokens at once, rotate
  `AGENT_EVAL_OAUTH_SECRET`.
- Put the gateway behind TLS (a TLS-terminating proxy is fine — the gateway reads
  `x-forwarded-proto` and advertises https metadata for non-local hosts).
