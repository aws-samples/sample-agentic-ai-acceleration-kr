"""Tests for caller-identity extraction from gateway Lambda events."""

import base64
import json

from _shared.caller_identity import extract_caller_identity


def _jwt(payload: dict) -> str:
    """Build an unsigned JWT-shaped string (header.payload.sig)."""
    def b64(obj: dict) -> str:
        raw = json.dumps(obj).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")
    return f"{b64({'alg': 'RS256'})}.{b64(payload)}.sig"


def test_reads_claims_from_authorizer_context():
    event = {"requestContext": {"authorizer": {"claims": {"sub": "user-123", "client_id": "app-client"}}}}
    out = extract_caller_identity(event)
    assert out == {"sub": "user-123", "client_id": "app-client", "raw_present": True}


def test_decodes_bearer_jwt_payload():
    token = _jwt({"sub": "user-456", "client_id": "m2m-client"})
    event = {"headers": {"authorization": f"Bearer {token}"}}
    out = extract_caller_identity(event)
    assert out["sub"] == "user-456"
    assert out["client_id"] == "m2m-client"
    assert out["raw_present"] is True


def test_handles_cid_claim_alias():
    token = _jwt({"sub": "u", "cid": "client-from-cid"})
    event = {"headers": {"Authorization": f"Bearer {token}"}}
    assert extract_caller_identity(event)["client_id"] == "client-from-cid"


def test_returns_none_on_missing_identity():
    out = extract_caller_identity({"input": {"query": "x"}})
    assert out == {"sub": None, "client_id": None, "raw_present": False}


def test_never_raises_on_garbage_token():
    event = {"headers": {"authorization": "Bearer not-a-jwt"}}
    out = extract_caller_identity(event)
    assert out["sub"] is None
    assert out["raw_present"] is False
