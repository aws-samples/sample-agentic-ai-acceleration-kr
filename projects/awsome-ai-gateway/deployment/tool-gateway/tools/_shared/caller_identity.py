"""Extract caller identity (sub / client_id) from a gateway Lambda event.

AgentCore Gateway validates the inbound JWT but does NOT forward its claims to
the Lambda's clientContext (see memory: agentcore-gateway-identity-quirk). This
helper recovers identity best-effort from whatever IS present on the event so a
tool can log "who called me". The gateway already verified the token, so we
decode the payload WITHOUT signature verification, purely to read claims.
"""

import base64
import json
from typing import Any, Dict, Optional

_EMPTY = {"sub": None, "client_id": None, "raw_present": False}


def _decode_jwt_payload(token: str) -> Optional[Dict[str, Any]]:
    parts = token.split(".")
    if len(parts) != 3:
        return None
    payload_b64 = parts[1]
    padding = "=" * (-len(payload_b64) % 4)
    try:
        raw = base64.urlsafe_b64decode(payload_b64 + padding)
        decoded = json.loads(raw)
        return decoded if isinstance(decoded, dict) else None
    except (ValueError, TypeError):
        return None


def extract_caller_identity(event: Dict[str, Any]) -> Dict[str, Any]:
    """Return {"sub", "client_id", "raw_present"} — never raises."""
    # 1. A future Request Interceptor may inject verified claims here.
    ctx = (
        event.get("requestContext", {})
        .get("authorizer", {})
        .get("claims")
        if isinstance(event.get("requestContext"), dict)
        else None
    )
    if isinstance(ctx, dict) and (ctx.get("sub") or ctx.get("client_id")):
        return {
            "sub": ctx.get("sub"),
            "client_id": ctx.get("client_id") or ctx.get("cid"),
            "raw_present": True,
        }

    # 2. Fall back to decoding the Bearer token payload (gateway already verified it).
    headers = event.get("headers") or {}
    auth = headers.get("authorization") or headers.get("Authorization") or ""
    if auth.lower().startswith("bearer "):
        payload = _decode_jwt_payload(auth[7:].strip())
        if payload:
            return {
                "sub": payload.get("sub"),
                "client_id": payload.get("client_id") or payload.get("cid"),
                "raw_present": True,
            }

    return dict(_EMPTY)
