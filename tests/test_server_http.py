"""End-to-end ASGI test of the hosted gateway's OAuth HTTP wiring.

Drives the real ``agent_eval_kit.server_http:app`` (AuthMiddleware + OAuth router)
over httpx's ASGITransport: discovery -> DCR -> authorize (consent) -> 302+code ->
token, plus the unauthenticated-/mcp 401 with the WWW-Authenticate discovery hint.
The OAuth routes are served by AuthMiddleware BEFORE the MCP app, so the MCP
lifespan is never needed here.
"""

from __future__ import annotations

import base64
import hashlib

import pytest

httpx = pytest.importorskip("httpx")
pytest.importorskip("mcp")

from agent_eval_kit import oauth, server_http  # noqa: E402

_CALLBACK = "https://claude.ai/api/mcp/auth_callback"
_KEY = "sk-otter-" + "b" * 40


def _pkce():
    v = "verifier-" + "y" * 50
    c = base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()
    return v, c


def _client():
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=server_http.app),
        base_url="https://mcp.example.com",
    )


@pytest.mark.asyncio
async def test_health_endpoint():
    async with _client() as c:
        r = await c.get("/")
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True and body["service"] == "agent-eval-kit"
        assert body["mcp"] == "/mcp"


@pytest.mark.asyncio
async def test_discovery_and_full_oauth_flow(monkeypatch):
    # don't hit the real eval API when validating the pasted key
    monkeypatch.setattr(oauth, "validate_api_key", lambda key, api_url: key == _KEY)
    async with _client() as c:
        # 1. discovery
        prm = await c.get("/.well-known/oauth-protected-resource")
        assert prm.status_code == 200
        assert prm.json()["authorization_servers"] == ["https://mcp.example.com"]
        asm = (await c.get("/.well-known/oauth-authorization-server")).json()
        assert asm["code_challenge_methods_supported"] == ["S256"]

        # 2. dynamic client registration
        reg = await c.post("/oauth/register", json={"redirect_uris": [_CALLBACK]})
        assert reg.status_code == 201
        cid = reg.json()["client_id"]

        # 3. authorize -> consent page (paste-key)
        verifier, challenge = _pkce()
        q = {"response_type": "code", "client_id": cid, "redirect_uri": _CALLBACK,
             "code_challenge": challenge, "code_challenge_method": "S256", "state": "st8"}
        page = await c.get("/oauth/authorize", params=q)
        assert page.status_code == 200
        assert "api_key" in page.text          # paste-key consent form

        # 4. consent POST -> 302 back to the callback with ?code=...&state=
        form = dict(q)
        form["api_key"] = _KEY
        redir = await c.post("/oauth/authorize", data=form)
        assert redir.status_code == 302
        loc = redir.headers["location"]
        assert loc.startswith(_CALLBACK + "?") and "state=st8" in loc
        code = httpx.URL(loc).params["code"]

        # 5. token exchange
        tok = await c.post("/oauth/token", data={
            "grant_type": "authorization_code", "code": code, "code_verifier": verifier,
            "client_id": cid, "redirect_uri": _CALLBACK})
        assert tok.status_code == 200
        body = tok.json()
        assert body["token_type"] == "Bearer"
        assert oauth.resolve_bearer(body["access_token"]) == _KEY


@pytest.mark.asyncio
async def test_consent_rejects_bad_key(monkeypatch):
    monkeypatch.setattr(oauth, "validate_api_key", lambda key, api_url: False)
    async with _client() as c:
        reg = await c.post("/oauth/register", json={"redirect_uris": [_CALLBACK]})
        cid = reg.json()["client_id"]
        _, challenge = _pkce()
        form = {"response_type": "code", "client_id": cid, "redirect_uri": _CALLBACK,
                "code_challenge": challenge, "code_challenge_method": "S256", "state": "s",
                "api_key": "sk-otter-bogus"}
        r = await c.post("/oauth/authorize", data=form)
        assert r.status_code == 401 and "not accepted" in r.text  # re-renders, no redirect


@pytest.mark.asyncio
async def test_consent_page_has_security_headers():
    async with _client() as c:
        cid = (await c.post("/oauth/register", json={"redirect_uris": [_CALLBACK]})).json()["client_id"]
        _, challenge = _pkce()
        page = await c.get("/oauth/authorize", params={
            "response_type": "code", "client_id": cid, "redirect_uri": _CALLBACK,
            "code_challenge": challenge, "code_challenge_method": "S256", "state": "s"})
        assert page.status_code == 200
        assert page.headers.get("x-frame-options") == "DENY"
        assert "frame-ancestors 'none'" in page.headers.get("content-security-policy", "")
        assert page.headers.get("cache-control") == "no-store"
        assert page.headers.get("referrer-policy") == "no-referrer"


@pytest.mark.asyncio
async def test_mcp_requires_auth_and_advertises_discovery():
    async with _client() as c:
        r = await c.get("/mcp")
        assert r.status_code == 401
        assert "resource_metadata" in r.headers.get("www-authenticate", "")
