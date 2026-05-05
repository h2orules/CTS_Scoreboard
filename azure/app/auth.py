"""Pi → Azure authentication.

The Pi obtains an Entra ID access token via the device-code flow (msal on the
Pi side) and presents it on the relay's `/pi` namespace at handshake time.
This module validates the token: signature against Entra's JWKS, audience
matches the relay app registration, tenant matches the configured tenant.

Phase 1: scaffold. Phase 2 implements full validation.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PiIdentity:
    """Authenticated Pi caller.

    Attributes:
        account_id: stable identifier from the access token (oid claim).
        tenant_id: Entra tenant ID.
        upn: optional user principal name for logs.
    """

    account_id: str
    tenant_id: str
    upn: str | None = None


class InvalidPiTokenError(Exception):
    """Raised when a Pi-presented access token fails validation."""


def validate_pi_token(token: str, *, tenant_id: str, audience: str) -> PiIdentity:
    """Validate an Entra ID access token from the Pi.

    Phase 1: stub that raises NotImplementedError. Phase 2 fetches the JWKS,
    validates signature + audience + issuer + expiry, and returns the
    PiIdentity.
    """
    raise NotImplementedError("Phase 2 wires up validate_pi_token via PyJWT + JWKS.")
