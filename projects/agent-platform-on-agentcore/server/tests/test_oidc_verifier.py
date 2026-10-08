"""OIDCVerifier against real RS256 signatures; discovery and JWKS are seams.

Also pins `verify_any`'s routing: the unverified `iss` only picks the provider,
and a provider is matched with or without a trailing slash.
"""
import os
import sys
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.oidc_verifier as ov  # noqa: E402
from core.config import OIDCProvider  # noqa: E402

ISSUER = "https://login.microsoftonline.com/tenant-123/v2.0"
AUDIENCE = "client-abc"
KID = "test-key-1"


@pytest.fixture
def signing():
    """An RSA key pair and a fake PyJWKClient that serves its public key."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()

    class FakeSigningKey:
        key = public_key

    class FakeJwkClient:
        def get_signing_key_from_jwt(self, token):
            header = jwt.get_unverified_header(token)
            if header.get("kid") != KID:
                raise jwt.exceptions.PyJWKClientError(f"unknown kid: {header.get('kid')}")
            return FakeSigningKey()

    return private_key, FakeJwkClient()


def _make(signing, monkeypatch):
    private_key, jwk_client = signing
    v = ov.OIDCVerifier(issuer_url=ISSUER, audience=AUDIENCE)
    # Skip discovery/JWKS network: the URI is "found" and the client is ready.
    monkeypatch.setattr(v, "_jwks_uri", "https://example.invalid/jwks")
    monkeypatch.setattr(v, "_jwks_client", jwk_client)
    return v, private_key


def _token(private_key, **overrides):
    claims = {
        "sub": "user-1",
        "iss": ISSUER,
        "aud": AUDIENCE,
        "exp": int(time.time()) + 3600,
        "iat": int(time.time()),
        "email": "a@corp.com",
        "name": "Alice",
        "groups": ["Claude"],
    }
    claims.update(overrides)
    return jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": KID})


def test_valid_token_returns_claims(signing, monkeypatch):
    v, pk = _make(signing, monkeypatch)
    claims = v.verify(_token(pk))
    assert claims["sub"] == "user-1"
    assert claims["groups"] == ["Claude"]


def test_expired_token_is_rejected(signing, monkeypatch):
    v, pk = _make(signing, monkeypatch)
    with pytest.raises(ov.OIDCVerifyError):
        v.verify(_token(pk, exp=int(time.time()) - 10))


def test_wrong_issuer_is_rejected(signing, monkeypatch):
    v, pk = _make(signing, monkeypatch)
    with pytest.raises(ov.OIDCVerifyError):
        v.verify(_token(pk, iss="https://evil.example/"))


def test_issuer_with_trailing_slash_is_accepted(signing, monkeypatch):
    """Auth0 and Entra v1 end `iss` with a slash. PyJWT compares `iss` exactly,
    so the slash-stripped issuer the verifier keeps for cache and discovery must
    not be the one it hands to jwt.decode — that rejected every such token."""
    private_key, jwk_client = signing
    slashed = "https://tenant.auth0.example/"
    for configured in (slashed, slashed.rstrip("/")):
        v = ov.OIDCVerifier(issuer_url=configured, audience=AUDIENCE)
        monkeypatch.setattr(v, "_jwks_uri", "https://example.invalid/jwks")
        monkeypatch.setattr(v, "_jwks_client", jwk_client)
        claims = v.verify(_token(private_key, iss=slashed))
        assert claims["iss"] == slashed, configured


def test_issuer_without_trailing_slash_still_accepted_when_configured_with_one(signing, monkeypatch):
    private_key, jwk_client = signing
    v = ov.OIDCVerifier(issuer_url=ISSUER + "/", audience=AUDIENCE)
    monkeypatch.setattr(v, "_jwks_uri", "https://example.invalid/jwks")
    monkeypatch.setattr(v, "_jwks_client", jwk_client)
    assert v.verify(_token(private_key, iss=ISSUER))["sub"] == "user-1"


def test_issuer_differing_beyond_the_slash_is_rejected(signing, monkeypatch):
    """Tolerance is the trailing slash only, not a prefix or a sibling path."""
    v, pk = _make(signing, monkeypatch)
    for iss in (ISSUER + "/extra", ISSUER[:-1], ISSUER.replace("v2.0", "v1.0")):
        with pytest.raises(ov.OIDCVerifyError):
            v.verify(_token(pk, iss=iss))


def test_wrong_audience_is_rejected(signing, monkeypatch):
    v, pk = _make(signing, monkeypatch)
    with pytest.raises(ov.OIDCVerifyError):
        v.verify(_token(pk, aud="someone-else"))


def test_unknown_kid_is_rejected(signing, monkeypatch):
    private_key, _ = signing
    v = ov.OIDCVerifier(issuer_url=ISSUER, audience=AUDIENCE)
    monkeypatch.setattr(v, "_jwks_uri", "https://example.invalid/jwks")

    class Empty:
        def get_signing_key_from_jwt(self, token):
            raise jwt.exceptions.PyJWKClientError("unknown kid")

    monkeypatch.setattr(v, "_jwks_client", Empty())
    with pytest.raises(ov.OIDCVerifyError):
        v.verify(_token(private_key))


def test_alg_none_is_rejected(signing, monkeypatch):
    v, pk = _make(signing, monkeypatch)
    unsigned = jwt.encode({"sub": "x", "iss": ISSUER, "aud": AUDIENCE}, key=None, algorithm="none")
    with pytest.raises(ov.OIDCVerifyError):
        v.verify(unsigned)


def test_missing_sub_is_rejected(signing, monkeypatch):
    v, pk = _make(signing, monkeypatch)
    with pytest.raises(ov.OIDCVerifyError):
        v.verify(_token(pk, sub=""))


def test_discovery_validates_issuer(monkeypatch):
    """A discovery document naming a different issuer is a configuration error."""
    v = ov.OIDCVerifier(issuer_url=ISSUER, audience=AUDIENCE)

    def fake_get_json(url, timeout):
        return {"issuer": "https://mismatch/", "jwks_uri": "https://x/jwks"}

    monkeypatch.setattr(v, "_get_json", fake_get_json)
    with pytest.raises(ov.OIDCConfigError):
        v._discover_jwks_uri()


def test_discovery_returns_jwks_uri(monkeypatch):
    v = ov.OIDCVerifier(issuer_url=ISSUER, audience=AUDIENCE)

    def fake_get_json(url, timeout):
        assert url == f"{ISSUER}/.well-known/openid-configuration"
        return {"issuer": ISSUER, "jwks_uri": "https://idp/keys"}

    monkeypatch.setattr(v, "_get_json", fake_get_json)
    assert v._discover_jwks_uri() == "https://idp/keys"


# ── routing ──────────────────────────────────────────────────────────────────

def test_verify_any_routes_by_issuer_and_tolerates_trailing_slash(signing, monkeypatch):
    private_key, jwk_client = signing
    provider = OIDCProvider(id="entra", label="Entra", issuer_url=ISSUER + "/", audience=AUDIENCE)
    other = OIDCProvider(id="other", label="Other", issuer_url="https://other.example", audience="x")
    ov.reset_verifier_cache()
    try:
        verifier = ov._verifier_for(provider)
        monkeypatch.setattr(verifier, "_jwks_uri", "https://example.invalid/jwks")
        monkeypatch.setattr(verifier, "_jwks_client", jwk_client)

        claims, matched = ov.verify_any(_token(private_key), [other, provider])
        assert matched is provider
        assert claims["sub"] == "user-1"
    finally:
        ov.reset_verifier_cache()


def test_verify_any_rejects_unknown_issuer(signing):
    private_key, _ = signing
    provider = OIDCProvider(id="entra", label="Entra", issuer_url="https://somewhere.else", audience=AUDIENCE)
    with pytest.raises(ov.OIDCVerifyError):
        ov.verify_any(_token(private_key), [provider])


def test_unverified_issuer_rejects_garbage():
    with pytest.raises(ov.OIDCVerifyError):
        ov.unverified_issuer("not.a.jwt.at.all")
    with pytest.raises(ov.OIDCVerifyError):
        ov.unverified_issuer("")
