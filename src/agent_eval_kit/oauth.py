"""Stateless OAuth 2.1 + PKCE for the hosted MCP gateway.

WHY: claude.ai custom connectors + the Anthropic Connector Directory (and ChatGPT
custom connectors) only accept OAuth (no static bearer / custom header). This wraps
the eval API key in a real OAuth 2.1 authorization-code + PKCE flow so the gateway
can be added as a remote connector — without a database and without touching the
eval API's auth: the OAuth access token is a Fernet token that ENCRYPTS the user's
API key, so the gateway decrypts it back to the key and forwards it to the eval API
exactly as before. JWTs are NOT used for the access token because their payload is
not encrypted (it would leak the key); a Fernet token is encrypted + authenticated
+ carries its own TTL.

Endpoints (served by ``agent_eval_kit.server_http``):
  GET  /.well-known/oauth-protected-resource   (RFC 9728)
  GET  /.well-known/oauth-authorization-server (RFC 8414)
  POST /oauth/register                          (RFC 7591 Dynamic Client Reg)
  GET  /oauth/authorize  + POST                 (consent: paste API key)
  POST /oauth/token                             (code+PKCE -> token; refresh)

Security model:
  * redirect_uri is allow-listed (claude.ai / claude.com / chatgpt.com callbacks +
    localhost for dev) — prevents open-redirect / code exfiltration.
  * PKCE S256 is REQUIRED (plain is rejected).
  * client_id is a Fernet blob of the registered redirect_uris (stateless DCR);
    /authorize re-checks the redirect_uri against it AND the global allow-list.
  * authorization code TTL 90s; access token TTL 1h; refresh token TTL 30d.
  * the API key is validated against the eval API before a code is issued.

Configuration (env):
  AGENT_EVAL_OAUTH_SECRET        — required; any string seeds the Fernet key.
  AGENT_EVAL_OAUTH_ALLOWED_HOSTS — extra redirect hosts, comma-separated.
  AGENT_EVAL_API_KEY_PREFIX      — raw-key bearer prefix (default ``sk-otter-``).
  AGENT_EVAL_AUTH_CHECK_PATH     — authed, side-effect-free GET used to validate a
                                   pasted key (default ``/api/v1/billing/status``).
  AGENT_EVAL_DOCS_URL            — documentation link advertised in metadata.
  AGENT_EVAL_OAUTH_SCOPE         — scope label in metadata/tokens (default ``eval``).
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

# --- TTLs -------------------------------------------------------------------
_CODE_TTL = 90          # authorization code: seconds
_ACCESS_TTL = 3600      # access token: 1 hour
_REFRESH_TTL = 30 * 24 * 3600  # refresh token: 30 days

def _scope() -> str:
    """OAuth scope label advertised in metadata + token responses
    (``AGENT_EVAL_OAUTH_SCOPE``, default ``eval``)."""
    return os.getenv("AGENT_EVAL_OAUTH_SCOPE", "eval")


# Connector OAuth callbacks we trust (host-based allow-list — prevents open
# redirect / code exfiltration). Anthropic (claude.ai/.com) + OpenAI ChatGPT
# (custom connectors + Apps SDK use chatgpt.com / openai.com callbacks). Extra
# hosts via AGENT_EVAL_OAUTH_ALLOWED_HOSTS (comma-separated).
_DEFAULT_ALLOWED_HOSTS = {
    "claude.ai", "www.claude.ai", "claude.com", "www.claude.com",
    "chatgpt.com", "www.chatgpt.com", "chat.openai.com", "platform.openai.com",
}
_LOCAL_HOSTS = {"localhost", "127.0.0.1"}


class OAuthError(Exception):
    """An OAuth protocol error -> rendered as the spec's JSON error response."""

    def __init__(self, error: str, description: str = "", status: int = 400) -> None:
        super().__init__(error)
        self.error = error
        self.description = description
        self.status = status

    def body(self) -> dict[str, str]:
        d = {"error": self.error}
        if self.description:
            d["error_description"] = self.description
        return d


def _fernet() -> Fernet:
    """Fernet from ``AGENT_EVAL_OAUTH_SECRET`` (any string -> a valid key)."""
    secret = os.getenv("AGENT_EVAL_OAUTH_SECRET", "")
    if not secret:
        raise OAuthError("server_error", "OAuth signing secret not configured", 500)
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
    return Fernet(key)


def _key_prefix() -> str:
    return os.getenv("AGENT_EVAL_API_KEY_PREFIX", "sk-otter-")


def _docs_url() -> str:
    return os.getenv("AGENT_EVAL_DOCS_URL", "https://github.com/SeaOtterAI/agent-eval-kit")


def _allowed_hosts() -> set[str]:
    extra = os.getenv("AGENT_EVAL_OAUTH_ALLOWED_HOSTS", "")
    hosts = set(_DEFAULT_ALLOWED_HOSTS) | _LOCAL_HOSTS
    hosts |= {h.strip().lower() for h in extra.split(",") if h.strip()}
    return hosts


def _redirect_uri_ok(redirect_uri: str) -> bool:
    # Reject userinfo (``@``) and backslashes outright: they cause parser
    # differentials (the host Python extracts can differ from where a
    # WHATWG/browser actually navigates), which is an open-redirect vector. No
    # legitimate connector callback contains either.
    if "\\" in redirect_uri:
        return False
    try:
        u = urllib.parse.urlparse(redirect_uri)
    except ValueError:
        return False
    if "@" in (u.netloc or ""):
        return False
    host = (u.hostname or "").lower()
    if host in _LOCAL_HOSTS:
        return u.scheme in ("http", "https")
    return u.scheme == "https" and host in _allowed_hosts()


# --- metadata ---------------------------------------------------------------


def protected_resource_metadata(issuer: str) -> dict[str, Any]:
    """RFC 9728 — the MCP server as an OAuth-protected resource."""
    return {
        "resource": issuer,
        "authorization_servers": [issuer],
        "bearer_methods_supported": ["header"],
        "resource_documentation": _docs_url(),
    }


def authorization_server_metadata(issuer: str) -> dict[str, Any]:
    """RFC 8414 — the authorization-server metadata the client discovers."""
    return {
        "issuer": issuer,
        "authorization_endpoint": f"{issuer}/oauth/authorize",
        "token_endpoint": f"{issuer}/oauth/token",
        "registration_endpoint": f"{issuer}/oauth/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": [_scope()],
    }


# --- dynamic client registration (RFC 7591, stateless) ----------------------


def register_client(body: dict[str, Any]) -> dict[str, Any]:
    """Validate redirect_uris, return a client_id that ENCODES them (no store)."""
    redirect_uris = body.get("redirect_uris") or []
    if not isinstance(redirect_uris, list) or not redirect_uris:
        raise OAuthError("invalid_redirect_uri", "redirect_uris required")
    for ru in redirect_uris:
        if not isinstance(ru, str) or not _redirect_uri_ok(ru):
            # Log the exact rejected uri — DCR failures are otherwise invisible
            # (access log shows only "400"), making "incorrect redirect URL" in a
            # connector client hard to diagnose.
            logging.getLogger(__name__).warning(
                "oauth register rejected redirect_uri=%r (allowed hosts=%s)",
                ru, sorted(_allowed_hosts()),
            )
            raise OAuthError("invalid_redirect_uri", f"redirect_uri not allowed: {ru}")
    blob = json.dumps({"r": redirect_uris}).encode()
    client_id = "cid_" + _fernet().encrypt(blob).decode()
    return {
        "client_id": client_id,
        "redirect_uris": redirect_uris,
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
    }


def _client_redirect_uris(client_id: str) -> list[str]:
    """Decode a registered client_id back to its redirect_uris (stateless DCR)."""
    if not client_id.startswith("cid_"):
        raise OAuthError("invalid_client", "unknown client_id")
    try:
        blob = _fernet().decrypt(client_id[4:].encode())
    except (InvalidToken, ValueError) as exc:
        raise OAuthError("invalid_client", "unknown client_id") from exc
    return list(json.loads(blob).get("r") or [])


# --- authorize (PKCE) -------------------------------------------------------


def validate_authorize(params: dict[str, str]) -> dict[str, str]:
    """Validate an /authorize request. Returns the fields the consent page needs."""
    if params.get("response_type") != "code":
        raise OAuthError("unsupported_response_type", "only response_type=code")
    client_id = params.get("client_id", "")
    redirect_uri = params.get("redirect_uri", "")
    registered = _client_redirect_uris(client_id)  # raises invalid_client
    if redirect_uri not in registered or not _redirect_uri_ok(redirect_uri):
        # never redirect to an unvalidated uri — render an error instead.
        raise OAuthError("invalid_request", "redirect_uri mismatch")
    challenge = params.get("code_challenge", "")
    if not challenge or params.get("code_challenge_method") != "S256":
        raise OAuthError("invalid_request", "PKCE code_challenge with S256 required")
    return {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": challenge,
        "state": params.get("state", ""),
        "scope": params.get("scope", _scope()),
    }


def issue_code(api_key: str, ctx: dict[str, str]) -> str:
    """Mint a Fernet authorization code binding the key + client + PKCE challenge."""
    blob = json.dumps({
        "k": api_key,
        "cid": ctx["client_id"],
        "ru": ctx["redirect_uri"],
        "cc": ctx["code_challenge"],
    }).encode()
    return _fernet().encrypt(blob).decode()


def _verify_pkce(code_verifier: str, code_challenge: str) -> bool:
    digest = hashlib.sha256(code_verifier.encode()).digest()
    expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return expected == code_challenge


# --- token ------------------------------------------------------------------


def _access_token(api_key: str) -> str:
    return _fernet().encrypt(json.dumps({"k": api_key, "t": "access"}).encode()).decode()


def _refresh_token(api_key: str) -> str:
    return _fernet().encrypt(json.dumps({"k": api_key, "t": "refresh"}).encode()).decode()


def exchange_code(form: dict[str, str]) -> dict[str, Any]:
    """authorization_code grant: verify code + PKCE + binding -> tokens."""
    code = form.get("code", "")
    verifier = form.get("code_verifier", "")
    client_id = form.get("client_id", "")
    redirect_uri = form.get("redirect_uri", "")
    if not code or not verifier:
        raise OAuthError("invalid_request", "code and code_verifier required")
    try:
        payload = json.loads(_fernet().decrypt(code.encode(), ttl=_CODE_TTL))
    except (InvalidToken, ValueError) as exc:
        raise OAuthError("invalid_grant", "authorization code invalid or expired") from exc
    if payload.get("cid") != client_id or payload.get("ru") != redirect_uri:
        raise OAuthError("invalid_grant", "code does not match client/redirect_uri")
    if not _verify_pkce(verifier, payload.get("cc", "")):
        raise OAuthError("invalid_grant", "PKCE verification failed")
    key = payload["k"]
    return {
        "access_token": _access_token(key),
        "token_type": "Bearer",
        "expires_in": _ACCESS_TTL,
        "refresh_token": _refresh_token(key),
        "scope": _scope(),
    }


def refresh_token_grant(form: dict[str, str]) -> dict[str, Any]:
    token = form.get("refresh_token", "")
    if not token:
        raise OAuthError("invalid_request", "refresh_token required")
    try:
        payload = json.loads(_fernet().decrypt(token.encode(), ttl=_REFRESH_TTL))
    except (InvalidToken, ValueError) as exc:
        raise OAuthError("invalid_grant", "refresh token invalid or expired") from exc
    if payload.get("t") != "refresh":
        raise OAuthError("invalid_grant", "not a refresh token")
    key = payload["k"]
    return {
        "access_token": _access_token(key),
        "token_type": "Bearer",
        "expires_in": _ACCESS_TTL,
        "refresh_token": _refresh_token(key),
        "scope": _scope(),
    }


def resolve_bearer(token: str) -> str | None:
    """Map an inbound Bearer -> the API key the eval API expects.

    Accepts BOTH a raw API key (prefix ``AGENT_EVAL_API_KEY_PREFIX``, returned
    as-is) and an OAuth access token (a Fernet blob -> decrypt -> the key,
    enforcing the 1h TTL). Returns None if neither (the caller emits 401).
    """
    if not token:
        return None
    prefix = _key_prefix()
    if prefix and token.startswith(prefix):
        return token
    try:
        payload = json.loads(_fernet().decrypt(token.encode(), ttl=_ACCESS_TTL))
    except (InvalidToken, ValueError, OAuthError):
        return None
    if payload.get("t") != "access":
        return None
    key = payload.get("k")
    if not isinstance(key, str):
        return None
    return key if (not prefix or key.startswith(prefix)) else None


def validate_api_key(api_key: str, api_url: str) -> bool:
    """Cheap liveness check that a pasted key is real, before issuing a code.

    GETs ``AGENT_EVAL_AUTH_CHECK_PATH`` (an authed, side-effect-free endpoint).
    """
    prefix = _key_prefix()
    if prefix and not api_key.startswith(prefix):
        return False
    check_path = os.getenv("AGENT_EVAL_AUTH_CHECK_PATH", "/api/v1/billing/status")
    req = urllib.request.Request(
        f"{api_url.rstrip('/')}{check_path}",
        headers={"Authorization": f"Bearer {api_key}"}, method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310 (trusted host)
            return 200 <= r.status < 300
    except urllib.error.HTTPError:
        return False
    except (urllib.error.URLError, TimeoutError, OSError):
        # Fail closed: if we cannot verify the key, do not issue a code. The eval
        # API re-checks the key on every call (it is the real auth boundary), but
        # minting a token for an unverified key is worse than asking the user to
        # retry — so a transient check-endpoint outage blocks onboarding, by design.
        return False
