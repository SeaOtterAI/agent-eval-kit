# Security Policy

## Reporting a vulnerability

Please report security issues privately to **security@seaotter.ai**, or via
GitHub's [private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)
on this repository. Do **not** open a public issue for a vulnerability.

We aim to acknowledge a report within 3 business days and to ship a fix or
mitigation for confirmed, in-scope issues promptly.

## Scope notes

- **The OAuth gateway** (`agent_eval_kit.server_http` + `agent_eval_kit.oauth`)
  is the security-sensitive surface. It enforces PKCE S256, an allow-list of
  redirect hosts (with userinfo/backslash parser-differential hardening), and
  short-lived, encrypted (Fernet) authorization codes and tokens. The consent page
  is served `X-Frame-Options: DENY` + strict CSP + `no-store` + `no-referrer`.
  `AGENT_EVAL_OAUTH_SECRET` must be a strong, secret value; the flow fails closed
  if it is unset, and key validation fails closed on a verification-endpoint outage.
- **Known stateless tradeoff:** the gateway holds no server-side state, so an
  authorization code is replayable within its 90s TTL rather than strictly
  single-use. PKCE binds the code to the client + verifier. For strict OAuth 2.1
  single-use, front the gateway with a shared store of consumed code ids — see
  [docs/self-hosting.md](docs/self-hosting.md).
- **Credentials are never logged or persisted.** The gateway encrypts the API
  key into a short-lived token; it is never stored server-side and never returned
  to the client in plaintext.
- **The SDK fails closed:** a critic outage surfaces an error and a non-shipping
  band, never a silent pass.

If you are running the hosted gateway, keep `AGENT_EVAL_OAUTH_ALLOWED_HOSTS`
limited to the connector callbacks you actually use, and rotate
`AGENT_EVAL_OAUTH_SECRET` to invalidate all live tokens at once.
