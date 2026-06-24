"""Hosted, multi-tenant streamable-HTTP MCP gateway (``agent_eval_kit.server_http:app``).

Serves the SAME tools as the stdio server (``agent_eval_kit.mcp_server``) over MCP
Streamable HTTP at ``/mcp`` — so any agent connects by URL with NO install and no
``agent_eval_kit`` package:

    { "mcpServers": { "agent-eval": {
        "url": "https://your-gateway.example.com/mcp",
        "headers": { "Authorization": "Bearer sk-otter-..." } } } }

Auth is PER-REQUEST and multi-tenant. The caller's credential is resolved by
:func:`agent_eval_kit.oauth.resolve_bearer`, which accepts BOTH:
  * a raw API key (Claude Code / the Messages API MCP connector), and
  * an OAuth 2.1 access token (claude.ai custom connectors + the Anthropic
    Connector Directory only accept OAuth) — see the ``/oauth/*`` + well-known
    endpoints below.
Either way it resolves to the tenant's API key, stamped on the request contextvar
that ``mcp_server._client()`` reads, so every tool call authenticates + bills THAT
tenant via the eval API. A request to ``/mcp`` with no/invalid credential gets 401
+ a ``WWW-Authenticate`` pointing at the protected-resource metadata, so an OAuth
client can discover the authorization server.

Stateless streamable-HTTP, so it scales to zero and across instances with no
session affinity / in-memory session store. The OAuth flow is also stateless
(Fernet-encrypted codes/tokens — see ``agent_eval_kit.oauth``).

Run:  AGENT_EVAL_API_URL=https://api.seaotter.ai uvicorn agent_eval_kit.server_http:app
"""

from __future__ import annotations

import html
import json
import os
import urllib.parse

from mcp.server.transport_security import TransportSecuritySettings

from . import oauth
from .mcp_server import _REQUEST_API_KEY, mcp

# Stateless: each request is self-contained — required for serverless / multi-
# instance hosting (no shared MCP session state to coordinate).
mcp.settings.stateless_http = True

# MCP's DNS-rebinding protection defaults to a localhost-only Host allow-list, so
# behind a load balancer / public host every request would fail "Invalid Host
# header". That protection guards browser-reachable localhost servers from
# malicious pages; this is a Bearer-authenticated server-to-server API (the
# credential is the real auth boundary, enforced by AuthMiddleware below), so the
# host allow-list is the wrong control here. Disable it.
mcp.settings.transport_security = TransportSecuritySettings(
    enable_dns_rebinding_protection=False
)

_inner = mcp.streamable_http_app()
_API_URL = os.environ.get("AGENT_EVAL_API_URL", "https://api.seaotter.ai")
_DEFAULT_HOST = os.environ.get("AGENT_EVAL_GATEWAY_HOST", "localhost")
_DOCS_URL = os.environ.get("AGENT_EVAL_DOCS_URL", "https://github.com/SeaOtterAI/agent-eval-kit")
_SIGNUP_URL = os.environ.get("AGENT_EVAL_SIGNUP_URL", "https://seaotter.ai/developers")

_CORS = {
    "access-control-allow-origin": "*",
    "access-control-allow-headers": "authorization, content-type, mcp-protocol-version",
    "access-control-allow-methods": "GET, POST, OPTIONS",
}


# --- ASGI helpers -----------------------------------------------------------


def _headers(scope) -> dict[str, str]:
    return {k.decode().lower(): v.decode() for k, v in (scope.get("headers") or [])}


def _bearer(headers: dict[str, str]) -> str:
    key = headers.get("x-api-key", "").strip()
    if key:
        return key
    auth = headers.get("authorization", "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return ""


def _issuer(scope) -> str:
    """External base URL of this server (a TLS-terminating proxy means we force
    https for non-local hosts) — the OAuth metadata must advertise https endpoints."""
    h = _headers(scope)
    host = h.get("host", _DEFAULT_HOST)
    proto = h.get("x-forwarded-proto") or ("http" if host.split(":")[0] in ("localhost", "127.0.0.1") else "https")
    return f"{proto}://{host}"


async def _read_body(receive) -> bytes:
    chunks = []
    while True:
        msg = await receive()
        chunks.append(msg.get("body", b"") or b"")
        if not msg.get("more_body"):
            break
    return b"".join(chunks)


async def _send_json(send, status, payload, extra=None) -> None:
    body = json.dumps(payload).encode()
    hdrs = {"content-type": "application/json", "content-length": str(len(body)), **_CORS}
    if extra:
        hdrs.update(extra)
    await send({"type": "http.response.start", "status": status,
                "headers": [(k.encode(), v.encode()) for k, v in hdrs.items()]})
    await send({"type": "http.response.body", "body": body})


# The only HTML we serve is the credential-harvesting consent page, so lock it
# down: never framable (clickjacking), no caching, no Referer leak of the auth
# code, and a tight CSP (the page uses only inline styles + a same-origin form POST).
_HTML_SECURITY_HEADERS = {
    "x-frame-options": "DENY",
    "content-security-policy": (
        "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
        "base-uri 'none'; frame-ancestors 'none'"
    ),
    "referrer-policy": "no-referrer",
    "cache-control": "no-store",
}


async def _send_html(send, status, page) -> None:
    body = page.encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"text/html; charset=utf-8"),
                            (b"content-length", str(len(body)).encode()),
                            *[(k.encode(), v.encode()) for k, v in _HTML_SECURITY_HEADERS.items()]]})
    await send({"type": "http.response.body", "body": body})


async def _send_text(send, status, text) -> None:
    body = text.encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"text/plain; charset=utf-8"),
                            (b"content-length", str(len(body)).encode()),
                            *[(k.encode(), v.encode()) for k, v in _CORS.items()]]})
    await send({"type": "http.response.body", "body": body})


async def _redirect(send, location) -> None:
    await send({"type": "http.response.start", "status": 302,
                "headers": [(b"location", location.encode()), (b"content-length", b"0")]})
    await send({"type": "http.response.body", "body": b""})


async def _send_oauth_error(send, exc: oauth.OAuthError) -> None:
    await _send_json(send, exc.status, exc.body())


def _form(body: bytes) -> dict[str, str]:
    return {k: v[-1] for k, v in urllib.parse.parse_qs(body.decode(), keep_blank_values=True).items()}


# --- consent page -----------------------------------------------------------


def _hidden_inputs(ctx: dict[str, str]) -> str:
    fields = {
        "response_type": "code",
        "client_id": ctx["client_id"],
        "redirect_uri": ctx["redirect_uri"],
        "code_challenge": ctx["code_challenge"],
        "code_challenge_method": "S256",
        "state": ctx["state"],
        "scope": ctx["scope"],
    }
    return "".join(
        f'<input type="hidden" name="{html.escape(k)}" value="{html.escape(v)}">'
        for k, v in fields.items()
    )


def _consent_page(ctx: dict[str, str], error: str = "") -> str:
    """Authorize/consent page: paste an API key. All reflected values are
    validated upstream + HTML-escaped here."""
    hidden = _hidden_inputs(ctx)
    err = f'<p style="color:#c0392b">{html.escape(error)}</p>' if error else ""
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Authorize</title></head>
<body style="font-family:system-ui,sans-serif;max-width:32rem;margin:3rem auto;padding:0 1rem;line-height:1.5">
<h1 style="font-size:1.4rem">Authorize this client to grade your work</h1>
<p>Connect your account so this client can grade work on your behalf.</p>
{err}
<form method="POST" action="/oauth/authorize" style="margin-top:.8rem">{hidden}
<label>API key (<a href="{html.escape(_SIGNUP_URL)}" target="_blank" rel="noopener">get one</a>)<br>
<input name="api_key" type="password" autocomplete="off" placeholder="sk-otter-..." required
 style="width:100%;padding:.6rem;margin:.4rem 0 1rem;font-family:monospace"></label>
<button type="submit" style="padding:.6rem 1.2rem;font-size:1rem;cursor:pointer">Authorize</button>
</form>
<p style="color:#666;font-size:.85rem;margin-top:1.5rem">Your credential is encrypted into a short-lived
token; it is never stored by the gateway and never exposed to the client in plaintext.</p>
</body></html>"""


# --- OAuth + well-known router ----------------------------------------------


async def _handle_oauth(scope, receive, send, path, method) -> bool:
    """Handle the OAuth + discovery endpoints. Returns True if it served the
    request, False if the path is not ours (caller falls through to MCP)."""
    issuer = _issuer(scope)

    if method == "OPTIONS" and (path.startswith("/oauth/") or path.startswith("/.well-known/")):
        await send({"type": "http.response.start", "status": 204,
                    "headers": [(k.encode(), v.encode()) for k, v in _CORS.items()]})
        await send({"type": "http.response.body", "body": b""})
        return True

    if path == "/.well-known/oauth-protected-resource" and method == "GET":
        await _send_json(send, 200, oauth.protected_resource_metadata(issuer))
        return True
    if path == "/.well-known/oauth-authorization-server" and method == "GET":
        await _send_json(send, 200, oauth.authorization_server_metadata(issuer))
        return True

    # OpenAI ChatGPT Apps domain-verification challenge — served pre-auth, like
    # the discovery endpoints above. The token is issued by the OpenAI Apps
    # dashboard (Apps -> MCP -> Domain verification) and supplied via env so it is
    # not baked into source; unset -> fall through (route inactive).
    if path == "/.well-known/openai-apps-challenge" and method == "GET":
        token = os.environ.get("OPENAI_APPS_CHALLENGE_TOKEN", "").strip()
        if token:
            await _send_text(send, 200, token)
            return True
        return False

    if path == "/oauth/register" and method == "POST":
        try:
            body = json.loads(await _read_body(receive) or b"{}")
            await _send_json(send, 201, oauth.register_client(body))
        except oauth.OAuthError as e:
            await _send_oauth_error(send, e)
        except (json.JSONDecodeError, ValueError):
            await _send_json(send, 400, {"error": "invalid_request"})
        return True

    if path == "/oauth/authorize" and method == "GET":
        params = {k: v[-1] for k, v in urllib.parse.parse_qs(
            scope.get("query_string", b"").decode(), keep_blank_values=True).items()}
        try:
            ctx = oauth.validate_authorize(params)
        except oauth.OAuthError as e:
            await _send_json(send, e.status, e.body())  # cannot safely redirect
            return True
        await _send_html(send, 200, _consent_page(ctx))
        return True

    if path == "/oauth/authorize" and method == "POST":
        form = _form(await _read_body(receive))
        try:
            ctx = oauth.validate_authorize(form)
        except oauth.OAuthError as e:
            await _send_json(send, e.status, e.body())
            return True
        api_key = form.get("api_key", "").strip()
        if not oauth.validate_api_key(api_key, _API_URL):
            await _send_html(send, 401, _consent_page(ctx, "That key was not accepted. Check it and try again."))
            return True
        code = oauth.issue_code(api_key, ctx)
        q = urllib.parse.urlencode({"code": code, "state": ctx["state"]})
        await _redirect(send, f"{ctx['redirect_uri']}?{q}")
        return True

    if path == "/oauth/token" and method == "POST":
        form = _form(await _read_body(receive))
        try:
            grant = form.get("grant_type")
            if grant == "authorization_code":
                tok = oauth.exchange_code(form)
            elif grant == "refresh_token":
                tok = oauth.refresh_token_grant(form)
            else:
                raise oauth.OAuthError("unsupported_grant_type", f"grant_type={grant}")
            await _send_json(send, 200, tok, extra={"cache-control": "no-store"})
        except oauth.OAuthError as e:
            await _send_oauth_error(send, e)
        return True

    return False


class AuthMiddleware:
    """Pure-ASGI: route OAuth/discovery, then gate /mcp on a resolved credential."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = (scope.get("path") or "").rstrip("/") or "/"
        method = scope.get("method", "GET")

        # Health/landing — no auth (load-balancer probes, humans hitting the root).
        if path in ("/", "/healthz", "/health") and method == "GET":
            return await _send_json(
                send, 200,
                {"ok": True, "service": "agent-eval-kit", "mcp": "/mcp",
                 "oauth": "/.well-known/oauth-authorization-server",
                 "docs": _DOCS_URL},
            )

        # OAuth + discovery endpoints.
        if await _handle_oauth(scope, receive, send, path, method):
            return

        # Everything else (/mcp ...) requires a credential.
        token = _bearer(_headers(scope))
        api_key = oauth.resolve_bearer(token)
        if not api_key:
            issuer = _issuer(scope)
            www = (f'Bearer resource_metadata="{issuer}/.well-known/oauth-protected-resource"')
            return await _send_json(
                send, 401,
                {"error": "unauthorized",
                 "detail": "Authenticate with OAuth (claude.ai custom connector / the "
                           "Connector Directory) or an API key (Claude Code / the API). "
                           f"Get a key at {_SIGNUP_URL}."},
                extra={"www-authenticate": www},
            )
        reset = _REQUEST_API_KEY.set(api_key)
        try:
            await self.app(scope, receive, send)
        finally:
            _REQUEST_API_KEY.reset(reset)


app = AuthMiddleware(_inner)


def main() -> None:
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))


if __name__ == "__main__":
    main()
