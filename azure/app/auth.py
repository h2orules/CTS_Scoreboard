"""Pi → Azure authentication.

The Pi obtains an Entra ID access token via the device-code flow (msal on the
Pi side) and presents it on the relay's `/pi` namespace at handshake time.
This module validates the token: signature against Entra's JWKS, audience
matches the relay app registration, tenant matches the configured tenant.

Phase 3: full implementation backed by PyJWT + Entra JWKS, with a small
in-process JWKS cache (keys rotate rarely).
"""
from __future__ import annotations

from dataclasses import dataclass
from threading import Lock
from typing import Any

import jwt
from jwt import PyJWKClient

_JWKS_URI_TMPL = (
    "https://login.microsoftonline.com/{tenant_id}/discovery/v2.0/keys"
)


@dataclass(frozen=True)
class PiIdentity:
    account_id: str
    tenant_id: str
    upn: str | None = None


class InvalidPiTokenError(Exception):
    """Raised when a Pi-presented access token fails validation."""


_jwks_clients: dict[str, PyJWKClient] = {}
_jwks_lock = Lock()


def _jwks_client_for(tenant_id: str, *, jwks_uri: str | None = None) -> PyJWKClient:
    with _jwks_lock:
        c = _jwks_clients.get(tenant_id)
        if c is None:
            uri = jwks_uri or _JWKS_URI_TMPL.format(tenant_id=tenant_id)
            c = PyJWKClient(uri, cache_keys=True, lifespan=3600)
            _jwks_clients[tenant_id] = c
        return c


def validate_pi_token(
    token: str,
    *,
    tenant_id: str,
    audience: str,
    leeway_s: int = 30,
    _jwks_client: PyJWKClient | None = None,
) -> PiIdentity:
    """Validate an Entra ID access token from the Pi.

    Raises ``InvalidPiTokenError`` if anything is wrong; otherwise returns the
    PiIdentity drawn from the ``oid``/``upn`` claims.

    The ``_jwks_client`` parameter is for testing only.
    """
    if not token or not isinstance(token, str):
        raise InvalidPiTokenError("missing token")

    client = _jwks_client or _jwks_client_for(tenant_id)
    try:
        signing_key = client.get_signing_key_from_jwt(token).key
    except Exception as exc:
        raise InvalidPiTokenError(f"jwks lookup failed: {exc}") from exc

    issuers = (
        f"https://login.microsoftonline.com/{tenant_id}/v2.0",
        f"https://sts.windows.net/{tenant_id}/",
    )
    try:
        claims: dict[str, Any] = jwt.decode(
            token,
            signing_key,
            algorithms=["RS256"],
            audience=audience,
            issuer=issuers,
            leeway=leeway_s,
            options={"require": ["exp", "iat", "iss", "aud"]},
        )
    except jwt.PyJWTError as exc:
        raise InvalidPiTokenError(f"jwt validation failed: {exc}") from exc

    if claims.get("tid") and claims["tid"] != tenant_id:
        raise InvalidPiTokenError("tenant mismatch")

    oid = claims.get("oid") or claims.get("sub")
    if not oid:
        raise InvalidPiTokenError("missing oid/sub")

    return PiIdentity(
        account_id=str(oid),
        tenant_id=tenant_id,
        upn=claims.get("upn") or claims.get("preferred_username"),
    )


def reset_jwks_cache() -> None:
    """Test helper."""
    with _jwks_lock:
        _jwks_clients.clear()
