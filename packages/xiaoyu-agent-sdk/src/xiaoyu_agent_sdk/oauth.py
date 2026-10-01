"""Host-mediated OAuth authorization-code/PKCE for explicit MCP resources.

Endpoints and client registration are supplied by the host. No browser is
opened, redirect listener started, or tokens written to conversation storage.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
import hashlib
import math
import secrets
import threading
import time
from typing import Any, Callable, Protocol
from urllib.parse import parse_qs, urlencode, urlsplit

from .types import ConfigurationError, OAuthError


@dataclass(frozen=True)
class OAuthTokens:
    access_token: str = field(repr=False)
    refresh_token: str = field(default="", repr=False)
    expires_at: float = 0.0
    scope: str = ""


class OAuthTokenStore(Protocol):
    def load(self) -> OAuthTokens | None: ...
    def save(self, tokens: OAuthTokens) -> None: ...
    def clear(self) -> None: ...


class MemoryTokenStore:
    def __init__(self) -> None:
        self._tokens: OAuthTokens | None = None
        self._lock = threading.Lock()

    def load(self) -> OAuthTokens | None:
        with self._lock:
            return self._tokens

    def save(self, tokens: OAuthTokens) -> None:
        with self._lock:
            self._tokens = tokens

    def clear(self) -> None:
        with self._lock:
            self._tokens = None


def _url(value: str) -> None:
    parsed = urlsplit(value)
    if (parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "::1", "localhost"))) or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ConfigurationError("OAuth URLs require HTTPS (or loopback HTTP), without userinfo or fragments")


class OAuthClient:
    """Public registered client; authorization callback returns the full redirect URL.

    Token stores belong to this exact client/resource/scope configuration and
    must bound their own I/O. HTTP redirects are rejected. A refresh failure
    invalidates local credentials and requires explicit reauthorization.
    """
    def __init__(self, *, resource: str, client_id: str, authorization_endpoint: str,
                 token_endpoint: str, redirect_uri: str, scope: str = "",
                 revocation_endpoint: str = "", issuer: str = "",
                 token_store: OAuthTokenStore | None = None, timeout: float = 15.0) -> None:
        for value in (resource, authorization_endpoint, token_endpoint, redirect_uri):
            _url(value)
        for value in (revocation_endpoint, issuer):
            if value:
                _url(value)
        if not client_id or not math.isfinite(timeout) or timeout <= 0:
            raise ConfigurationError("OAuth client ID and positive timeout required")
        self.resource, self.client_id = resource, client_id
        self.authorization_endpoint, self.token_endpoint = authorization_endpoint, token_endpoint
        self.redirect_uri, self.scope = redirect_uri, scope
        self.revocation_endpoint, self.issuer = revocation_endpoint, issuer
        self.store = token_store if token_store is not None else MemoryTokenStore()
        self.timeout = timeout
        self._lock = threading.Lock()
        self._invalid = False

    def _post(self, url: str, data: dict[str, str]) -> dict[str, Any]:
        import httpx
        try:
            with httpx.Client(timeout=self.timeout, trust_env=False, follow_redirects=False) as client:
                response = client.post(url, data=data)
                if response.status_code != 200:
                    raise OAuthError("OAuth endpoint rejected request")
                return response.json() if response.content else {}
        except Exception as exc:
            raise OAuthError("OAuth exchange failed") from exc

    def _invalidate(self) -> None:
        self._invalid = True
        try:
            self.store.clear()
        except Exception as exc:
            raise OAuthError("OAuth credential store could not be cleared") from exc

    def _save(self, data: dict[str, Any], prior: OAuthTokens | None = None) -> OAuthTokens:
        access = data.get("access_token")
        if not isinstance(access, str) or not access or data.get("token_type", "").lower() != "bearer":
            raise OAuthError("Invalid OAuth token response")
        scope = data.get("scope", prior.scope if prior else self.scope)
        if not isinstance(scope, str) or not set(scope.split()) <= set(self.scope.split()):
            raise OAuthError("OAuth token expanded requested scope")
        refresh = data.get("refresh_token", prior.refresh_token if prior else "")
        lifetime = data.get("expires_in")
        if not isinstance(refresh, str) or (lifetime is not None and (type(lifetime) not in (int, float) or not math.isfinite(lifetime) or lifetime <= 0)):
            raise OAuthError("Invalid OAuth token expiry")
        if any(c in access for c in "\r\n"):
            raise OAuthError("Invalid OAuth access token")
        tokens = OAuthTokens(access, refresh, time.time() + lifetime if lifetime is not None else 0.0, scope)
        self.store.save(tokens)
        self._invalid = False
        return tokens

    def authorize(self, callback: Callable[[str], str]) -> None:
        with self._lock:
            verifier, state = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
            params = dict(response_type="code", client_id=self.client_id, redirect_uri=self.redirect_uri,
                          resource=self.resource, state=state, code_challenge=challenge, code_challenge_method="S256")
            if self.scope:
                params["scope"] = self.scope
            separator = "&" if urlsplit(self.authorization_endpoint).query else "?"
            try:
                response_url = callback(self.authorization_endpoint + separator + urlencode(params))
                actual, expected = urlsplit(response_url), urlsplit(self.redirect_uri)
                if (actual.scheme, actual.netloc, actual.path) != (expected.scheme, expected.netloc, expected.path) or actual.fragment:
                    raise OAuthError("OAuth redirect differs")
                query = parse_qs(actual.query)
                if any(len(v) != 1 for v in query.values()) or "error" in query:
                    raise OAuthError("OAuth authorization rejected")
                if not secrets.compare_digest(query.get("state", [""])[0], state):
                    raise OAuthError("OAuth state differs")
                if self.issuer and query.get("iss", [""])[0] != self.issuer:
                    raise OAuthError("OAuth issuer differs")
                code = query.get("code", [""])[0]
                if not code:
                    raise OAuthError("OAuth authorization code missing")
                self._save(self._post(self.token_endpoint, dict(grant_type="authorization_code", code=code,
                    code_verifier=verifier, redirect_uri=self.redirect_uri, client_id=self.client_id, resource=self.resource)))
            except Exception as exc:
                self._invalidate()
                raise OAuthError("OAuth authorization failed") from exc

    def authorization(self) -> str:
        with self._lock:
            try:
                if self._invalid:
                    raise OAuthError("OAuth requires authorization")
                tokens = self.store.load()
                if tokens is None:
                    raise OAuthError("OAuth requires authorization")
                if tokens.expires_at and tokens.expires_at <= time.time() + 30:
                    if not tokens.refresh_token:
                        raise OAuthError("OAuth token expired")
                    tokens = self._save(self._post(self.token_endpoint, dict(grant_type="refresh_token",
                        refresh_token=tokens.refresh_token, client_id=self.client_id, resource=self.resource)), tokens)
                if not set(tokens.scope.split()) <= set(self.scope.split()):
                    raise OAuthError("Stored OAuth scope differs")
                return "Bearer " + tokens.access_token
            except Exception as exc:
                self._invalidate()
                raise OAuthError("OAuth authorization unavailable") from exc

    def revoke(self) -> None:
        """Invalidate locally even if the authorization server rejects revocation."""
        with self._lock:
            self._invalid = True
            try:
                tokens = self.store.load()
                if tokens is not None:
                    if not self.revocation_endpoint:
                        raise OAuthError("No revocation endpoint; only local credentials cleared")
                    # Revoke both: servers do not universally invalidate the
                    # access token when its refresh token is revoked.
                    for token, hint in ((tokens.refresh_token, "refresh_token"), (tokens.access_token, "access_token")):
                        if token:
                            self._post(self.revocation_endpoint, dict(token=token, token_type_hint=hint, client_id=self.client_id))
            except Exception as exc:
                raise OAuthError("OAuth revocation failed") from exc
            finally:
                self._invalidate()
