"""Remote (HTTPS) mode for a Claude custom connector.

This server is an OAuth *resource server*. An identity provider you configure
(Auth0, WorkOS, Stytch, ...) signs in the user and issues JWTs. We accept a
token only if all of these hold:
  - signed by that issuer's keys (JWKS), with RS256 or ES256 (never "none", never HMAC)
  - iss equals the configured issuer
  - aud contains this server's public URL
  - not expired
  - sub is on the allowlist (you)
Anything else gets a 401.

HTTP mode refuses to start unless all of that is configured. There is no
"no auth" switch.
"""
from __future__ import annotations

from urllib.parse import urlparse

import anyio
import jwt
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecuritySettings

from .config import ConfigError, Settings

ALGORITHMS = ["RS256", "ES256"]  # asymmetric only


class JwtVerifier:
    def __init__(self, issuer: str, audience: str, jwks_url: str, allowed_subjects, required_scopes=None,
                 jwks_client=None, leeway: int = 30):
        if not allowed_subjects:
            raise ConfigError("At least one allowed subject is required")
        self.issuer, self.audience = issuer, audience
        self.allowed = frozenset(allowed_subjects)
        self.required = frozenset(required_scopes or ())
        self.jwks = jwks_client or jwt.PyJWKClient(jwks_url, cache_keys=True, lifespan=3600)
        self.leeway = leeway

    def _verify(self, token: str) -> AccessToken | None:
        try:
            key = self.jwks.get_signing_key_from_jwt(token).key
            claims = jwt.decode(
                token, key, algorithms=ALGORITHMS, audience=self.audience, issuer=self.issuer,
                leeway=self.leeway, options={"require": ["exp", "iat", "sub", "aud", "iss"]},
            )
        except Exception:  # any failure (bad signature, JWKS down, malformed token) means no access
            return None
        if claims.get("sub") not in self.allowed:
            return None
        scope = claims.get("scope")
        scopes = set(scope.split() if isinstance(scope, str) else scope or [])
        scopes |= set(claims.get("permissions") or [])
        if not self.required <= scopes:
            return None
        return AccessToken(token=token, client_id=str(claims.get("azp") or claims.get("client_id") or "claude"),
                           scopes=sorted(scopes), expires_at=int(claims["exp"]), resource=self.audience,
                           subject=claims["sub"])

    async def verify_token(self, token: str) -> AccessToken | None:
        # The JWKS lookup can do blocking network I/O, so keep it off the event loop.
        return await anyio.to_thread.run_sync(self._verify, token)


def _https(name: str, value: str) -> str:
    u = urlparse(value)
    if u.scheme != "https" or not u.netloc:
        raise ConfigError(f"{name} must be an https:// URL")
    return value


def remote_config(s: Settings) -> dict:
    """Validate remote settings and build the server keyword arguments. Raises ConfigError if OAuth is not set up."""
    missing = [name for name, v in (("EBAY_MCP_PUBLIC_URL", s.public_url), ("EBAY_MCP_OAUTH_ISSUER", s.oauth_issuer),
                                    ("EBAY_MCP_JWKS_URL", s.jwks_url),
                                    ("EBAY_MCP_ALLOWED_SUBJECTS", s.allowed_subjects)) if not v]
    if missing:
        raise ConfigError("Refusing to serve HTTP without OAuth. Missing: " + ", ".join(missing)
                          + " (see README, 'Remote hosting').")
    public = _https("EBAY_MCP_PUBLIC_URL", s.public_url)
    issuer = _https("EBAY_MCP_OAUTH_ISSUER", s.oauth_issuer)
    jwks = _https("EBAY_MCP_JWKS_URL", s.jwks_url)
    host = urlparse(public).netloc
    return {
        "server": {
            "token_verifier": JwtVerifier(issuer, public, jwks, s.allowed_subjects, s.required_scopes),
            # validate_token_resource=False: the verifier above checks the audience itself.
            "auth": AuthSettings(issuer_url=issuer, resource_server_url=public, validate_token_resource=False,
                                 required_scopes=sorted(s.required_scopes) or None),
        },
        # DNS rebinding protection: only requests whose Host header is the public hostname are served.
        "security": TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=[host],
                                              allowed_origins=[f"https://{host}"]),
        "path": urlparse(public).path or "/mcp",
    }


def run_http(settings: Settings, build_service, build_server):
    cfg = remote_config(settings)  # raises before anything listens
    server = build_server(build_service(settings), **cfg["server"])
    server.run("streamable-http", host=settings.host, port=settings.port, stateless_http=True,
               streamable_http_path=cfg["path"], transport_security=cfg["security"])
