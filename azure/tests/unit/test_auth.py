"""Tests for app.auth.validate_pi_token using injected JWKS."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.auth import InvalidPiTokenError, PiIdentity, validate_pi_token

TENANT = "00000000-0000-0000-0000-000000000001"
AUDIENCE = "api://relay-app"


@pytest.fixture(scope="module")
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def signing_key(rsa_key):
    return rsa_key


@pytest.fixture
def jwks_client(rsa_key):
    """Stub PyJWKClient that always returns the test public key."""
    pub = rsa_key.public_key()

    class _StubKey:
        def __init__(self, k):
            self.key = k

    class _StubClient:
        def get_signing_key_from_jwt(self, _token):
            return _StubKey(pub)

    return _StubClient()


def _make_token(rsa_key, *, claims_override=None):
    now = datetime.now(UTC)
    claims = {
        "iss": f"https://login.microsoftonline.com/{TENANT}/v2.0",
        "aud": AUDIENCE,
        "tid": TENANT,
        "oid": "user-oid-123",
        "upn": "alice@example.com",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=1)).timestamp()),
    }
    if claims_override:
        claims.update(claims_override)
    pem = rsa_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return jwt.encode(claims, pem, algorithm="RS256")


def test_valid_token_returns_identity(signing_key, jwks_client):
    token = _make_token(signing_key)
    ident = validate_pi_token(token, tenant_id=TENANT, audience=AUDIENCE,
                              _jwks_client=jwks_client)
    assert isinstance(ident, PiIdentity)
    assert ident.account_id == "user-oid-123"
    assert ident.upn == "alice@example.com"


def test_missing_token_rejected(jwks_client):
    with pytest.raises(InvalidPiTokenError):
        validate_pi_token("", tenant_id=TENANT, audience=AUDIENCE, _jwks_client=jwks_client)


def test_wrong_audience_rejected(signing_key, jwks_client):
    token = _make_token(signing_key, claims_override={"aud": "api://other"})
    with pytest.raises(InvalidPiTokenError):
        validate_pi_token(token, tenant_id=TENANT, audience=AUDIENCE,
                          _jwks_client=jwks_client)


def test_wrong_issuer_rejected(signing_key, jwks_client):
    token = _make_token(signing_key, claims_override={"iss": "https://attacker.example"})
    with pytest.raises(InvalidPiTokenError):
        validate_pi_token(token, tenant_id=TENANT, audience=AUDIENCE,
                          _jwks_client=jwks_client)


def test_wrong_tenant_rejected(signing_key, jwks_client):
    other = "11111111-1111-1111-1111-111111111111"
    token = _make_token(signing_key, claims_override={"tid": other})
    with pytest.raises(InvalidPiTokenError):
        validate_pi_token(token, tenant_id=TENANT, audience=AUDIENCE,
                          _jwks_client=jwks_client)


def test_expired_token_rejected(signing_key, jwks_client):
    expired = datetime.now(UTC) - timedelta(hours=2)
    token = _make_token(signing_key, claims_override={
        "iat": int(expired.timestamp()),
        "exp": int(expired.timestamp()) + 60,
    })
    with pytest.raises(InvalidPiTokenError):
        validate_pi_token(token, tenant_id=TENANT, audience=AUDIENCE,
                          _jwks_client=jwks_client)


def test_missing_oid_rejected(signing_key, jwks_client):
    # PyJWT's required claims set will accept the token (no `sub`/`oid` is
    # required by spec), but our function adds explicit oid/sub check.
    token = _make_token(signing_key, claims_override={"oid": None, "sub": None})
    with pytest.raises(InvalidPiTokenError):
        validate_pi_token(token, tenant_id=TENANT, audience=AUDIENCE,
                          _jwks_client=jwks_client)
