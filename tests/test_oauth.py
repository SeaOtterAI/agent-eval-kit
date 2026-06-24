"""Tests for the hosted gateway's OAuth 2.1 + PKCE flow (agent_eval_kit.oauth).

Pins the security-critical behaviour: PKCE S256 required + verified, redirect_uri
allow-listed (open-redirect / code-exfil prevention), code bound to client +
redirect + challenge, and the OAuth access token round-trips back to the API key
(so the eval API path is unchanged).
"""

from __future__ import annotations

import base64
import hashlib

import pytest

from agent_eval_kit import oauth

_KEY = "sk-otter-" + "a" * 40
_CALLBACK = "https://claude.ai/api/mcp/auth_callback"


def _pkce() -> tuple[str, str]:
    verifier = "verifier-" + "x" * 50
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def _register(redirect=_CALLBACK):
    return oauth.register_client({"redirect_uris": [redirect]})["client_id"]


# --- metadata ---------------------------------------------------------------

def test_metadata_shapes():
    prm = oauth.protected_resource_metadata("https://mcp.example.com")
    assert prm["resource"] == "https://mcp.example.com"
    assert prm["authorization_servers"] == ["https://mcp.example.com"]
    asm = oauth.authorization_server_metadata("https://mcp.example.com")
    assert asm["code_challenge_methods_supported"] == ["S256"]
    assert asm["authorization_endpoint"].endswith("/oauth/authorize")
    assert asm["token_endpoint"].endswith("/oauth/token")
    assert asm["registration_endpoint"].endswith("/oauth/register")
    assert asm["scopes_supported"] == ["eval"]


# --- dynamic client registration + redirect allow-list ----------------------

def test_register_allows_claude_callback():
    cid = _register()
    assert cid.startswith("cid_")
    assert _CALLBACK in oauth._client_redirect_uris(cid)


def test_register_allows_chatgpt_callback():
    cid = _register("https://chatgpt.com/connector_platform_oauth_redirect")
    assert cid.startswith("cid_")


@pytest.mark.parametrize("bad", [
    "https://evil.com/callback",                # not allow-listed -> open-redirect guard
    "http://claude.ai/api/mcp/auth_callback",   # non-https on a remote host
    "ftp://claude.ai/x",
])
def test_register_rejects_bad_redirect(bad):
    with pytest.raises(oauth.OAuthError) as e:
        oauth.register_client({"redirect_uris": [bad]})
    assert e.value.error == "invalid_redirect_uri"


def test_register_requires_redirect_uris():
    with pytest.raises(oauth.OAuthError):
        oauth.register_client({})


@pytest.mark.parametrize("bad", [
    "https://evil.com@claude.ai/api/mcp/auth_callback",   # userinfo: host parses to an allowed host, but @ is rejected
    "https://claude.ai\\@evil.com/cb",                    # backslash parser differential
    "https://claude.ai/cb\\@evil.com",                    # backslash anywhere in the uri
])
def test_register_rejects_userinfo_and_backslash(bad):
    with pytest.raises(oauth.OAuthError) as e:
        oauth.register_client({"redirect_uris": [bad]})
    assert e.value.error == "invalid_redirect_uri"


# --- authorize validation (PKCE) --------------------------------------------

def test_validate_authorize_happy():
    cid = _register()
    _, challenge = _pkce()
    ctx = oauth.validate_authorize({
        "response_type": "code", "client_id": cid, "redirect_uri": _CALLBACK,
        "code_challenge": challenge, "code_challenge_method": "S256", "state": "xyz",
    })
    assert ctx["redirect_uri"] == _CALLBACK and ctx["state"] == "xyz"


def test_validate_authorize_requires_s256():
    cid = _register()
    _, challenge = _pkce()
    with pytest.raises(oauth.OAuthError):  # missing PKCE
        oauth.validate_authorize({"response_type": "code", "client_id": cid, "redirect_uri": _CALLBACK})
    with pytest.raises(oauth.OAuthError):  # plain method rejected
        oauth.validate_authorize({"response_type": "code", "client_id": cid, "redirect_uri": _CALLBACK,
                                  "code_challenge": challenge, "code_challenge_method": "plain"})


def test_validate_authorize_rejects_unknown_client_and_redirect_mismatch():
    with pytest.raises(oauth.OAuthError) as e1:
        oauth.validate_authorize({"response_type": "code", "client_id": "cid_garbage",
                                  "redirect_uri": _CALLBACK, "code_challenge": "c", "code_challenge_method": "S256"})
    assert e1.value.error == "invalid_client"
    cid = _register()
    _, challenge = _pkce()
    with pytest.raises(oauth.OAuthError):  # redirect not the one registered
        oauth.validate_authorize({"response_type": "code", "client_id": cid,
                                  "redirect_uri": "https://claude.ai/other", "code_challenge": challenge,
                                  "code_challenge_method": "S256"})


# --- full code -> token (PKCE verified) -------------------------------------

def test_full_pkce_flow_round_trips_to_key():
    cid = _register()
    verifier, challenge = _pkce()
    ctx = oauth.validate_authorize({
        "response_type": "code", "client_id": cid, "redirect_uri": _CALLBACK,
        "code_challenge": challenge, "code_challenge_method": "S256", "state": "s",
    })
    code = oauth.issue_code(_KEY, ctx)
    tok = oauth.exchange_code({"code": code, "code_verifier": verifier,
                               "client_id": cid, "redirect_uri": _CALLBACK})
    assert tok["token_type"] == "Bearer" and tok["expires_in"] == 3600
    # the OAuth access token decrypts back to the API key the eval API needs
    assert oauth.resolve_bearer(tok["access_token"]) == _KEY
    # and the refresh token mints a fresh, working access token
    tok2 = oauth.refresh_token_grant({"refresh_token": tok["refresh_token"]})
    assert oauth.resolve_bearer(tok2["access_token"]) == _KEY


def test_exchange_rejects_wrong_verifier():
    cid = _register()
    _, challenge = _pkce()
    ctx = oauth.validate_authorize({"response_type": "code", "client_id": cid, "redirect_uri": _CALLBACK,
                                    "code_challenge": challenge, "code_challenge_method": "S256"})
    code = oauth.issue_code(_KEY, ctx)
    with pytest.raises(oauth.OAuthError) as e:
        oauth.exchange_code({"code": code, "code_verifier": "WRONG" * 10,
                             "client_id": cid, "redirect_uri": _CALLBACK})
    assert e.value.error == "invalid_grant"


def test_exchange_rejects_client_or_redirect_mismatch():
    cid = _register()
    verifier, challenge = _pkce()
    ctx = oauth.validate_authorize({"response_type": "code", "client_id": cid, "redirect_uri": _CALLBACK,
                                    "code_challenge": challenge, "code_challenge_method": "S256"})
    code = oauth.issue_code(_KEY, ctx)
    with pytest.raises(oauth.OAuthError):
        oauth.exchange_code({"code": code, "code_verifier": verifier,
                             "client_id": "cid_other", "redirect_uri": _CALLBACK})


def test_exchange_rejects_garbage_code():
    with pytest.raises(oauth.OAuthError) as e:
        oauth.exchange_code({"code": "not-a-real-code", "code_verifier": "v",
                             "client_id": "cid_x", "redirect_uri": _CALLBACK})
    assert e.value.error == "invalid_grant"


# --- resolve_bearer (the MCP gate) ------------------------------------------

def test_resolve_bearer_passthrough_and_reject():
    assert oauth.resolve_bearer(_KEY) == _KEY           # raw API key
    assert oauth.resolve_bearer("") is None
    assert oauth.resolve_bearer("garbage") is None
    # a refresh token must NOT be accepted as an access token at the MCP gate
    cid = _register()
    verifier, challenge = _pkce()
    ctx = oauth.validate_authorize({"response_type": "code", "client_id": cid, "redirect_uri": _CALLBACK,
                                    "code_challenge": challenge, "code_challenge_method": "S256"})
    tok = oauth.exchange_code({"code": oauth.issue_code(_KEY, ctx), "code_verifier": verifier,
                               "client_id": cid, "redirect_uri": _CALLBACK})
    assert oauth.resolve_bearer(tok["refresh_token"]) is None


def test_configurable_key_prefix(monkeypatch):
    monkeypatch.setenv("AGENT_EVAL_API_KEY_PREFIX", "ek-")
    assert oauth.resolve_bearer("ek-live-123") == "ek-live-123"
    assert oauth.resolve_bearer("sk-otter-xyz") is None  # no longer the configured prefix


def test_validate_api_key_fails_closed_on_network_error(monkeypatch):
    import urllib.error

    def boom(*a, **k):
        raise urllib.error.URLError("backend down")

    monkeypatch.setattr(oauth.urllib.request, "urlopen", boom)
    # A transient outage of the check endpoint must NOT issue a code (fail closed).
    assert oauth.validate_api_key(_KEY, "http://x") is False


def test_validate_api_key_rejects_wrong_prefix():
    assert oauth.validate_api_key("not-a-key", "http://x") is False


def test_oauth_scope_is_configurable(monkeypatch):
    monkeypatch.setenv("AGENT_EVAL_OAUTH_SCOPE", "otterscore")
    asm = oauth.authorization_server_metadata("https://mcp.example.com")
    assert asm["scopes_supported"] == ["otterscore"]
    cid = _register()
    verifier, challenge = _pkce()
    ctx = oauth.validate_authorize({"response_type": "code", "client_id": cid, "redirect_uri": _CALLBACK,
                                    "code_challenge": challenge, "code_challenge_method": "S256"})
    assert ctx["scope"] == "otterscore"
    tok = oauth.exchange_code({"code": oauth.issue_code(_KEY, ctx), "code_verifier": verifier,
                               "client_id": cid, "redirect_uri": _CALLBACK})
    assert tok["scope"] == "otterscore"
