# Copyright 2026 © Amazon.com and Affiliates.
"""SigV4Signer — the hand-rolled signing the bedrock-runtime OpenAI wires need.

These paths are not botocore operations, so nothing validates the signature for us until
AWS returns 403. The tests therefore pin the things a 403 would be blamed on later: that
the signature is bound to the exact body bytes and host, that the service name is
``bedrock``, and that the cross-account path never degrades to the pod's own identity.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from botocore.credentials import Credentials

from app.services.sigv4_signer import SigV4Signer

URL = "https://bedrock-runtime.us-east-2.amazonaws.com/openai/v1/responses"
BODY = b'{"model":"us.openai.gpt-5.6-terra","input":"hi"}'


def _static_session(access="AKIDIRSA", secret="SECRETIRSA", token="TOKENIRSA"):
    """Stand-in for the pod's botocore session (IRSA)."""
    frozen = Credentials(access, secret, token)
    raw = MagicMock()
    raw.get_frozen_credentials.return_value = frozen
    session = MagicMock()
    session.get_credentials.return_value = raw
    return session


def _signer(sts=None, now=None):
    s = SigV4Signer(sts_client=sts, now=now or (lambda: 1_000_000.0))
    s._session = _static_session()
    return s


async def _sign(signer, **kw):
    return await signer.sign(method="POST", url=URL, body=BODY, region="us-east-2", **kw)


@pytest.mark.asyncio
async def test_signs_with_bedrock_service_and_host_region():
    headers = await _sign(_signer())
    auth = headers["Authorization"]
    # Credential scope encodes date/region/service; getting any of the three wrong is a
    # 403 that looks like a credentials problem.
    assert "/us-east-2/bedrock/aws4_request" in auth
    assert auth.startswith("AWS4-HMAC-SHA256 ")
    assert "X-Amz-Date" in headers
    assert headers["X-Amz-Security-Token"] == "TOKENIRSA"
    assert headers["content-type"] == "application/json"


@pytest.mark.asyncio
async def test_signature_is_bound_to_the_body_bytes():
    """One byte of difference must change the signature.

    This is why the adapter has to POST the same ``content=request_body`` it signed: any
    re-serialisation of the JSON (key order, whitespace) invalidates it.
    """
    a = await _sign(_signer())
    b = await _signer().sign(
        method="POST", url=URL, body=BODY + b" ", region="us-east-2"
    )
    assert a["Authorization"] != b["Authorization"]


@pytest.mark.asyncio
async def test_signature_is_bound_to_the_region():
    a = await _sign(_signer())
    b = await _signer().sign(method="POST", url=URL, body=BODY, region="us-east-1")
    assert a["Authorization"] != b["Authorization"]
    assert "/us-east-1/bedrock/aws4_request" in b["Authorization"]


@pytest.mark.asyncio
async def test_signature_is_bound_to_the_path():
    """The two wires sign different canonical requests, so a shared signature is invalid."""
    a = await _sign(_signer())
    b = await _signer().sign(
        method="POST",
        url="https://bedrock-runtime.us-east-2.amazonaws.com/openai/v1/chat/completions",
        body=BODY,
        region="us-east-2",
    )
    assert a["Authorization"] != b["Authorization"]


@pytest.mark.asyncio
async def test_extra_headers_are_merged_and_signed():
    """Any header we add must end up in SignedHeaders or AWS rejects the request.

    Relevant to request-metadata tagging: the docs warn that omitting
    ``X-Amzn-Bedrock-Request-Metadata`` from the signed list is an InvalidSignatureException.
    """
    headers = await _sign(
        _signer(), headers={"X-Amzn-Bedrock-Request-Metadata": '{"team":"gw"}'}
    )
    assert headers["X-Amzn-Bedrock-Request-Metadata"] == '{"team":"gw"}'
    signed = headers["Authorization"].split("SignedHeaders=")[1].split(",")[0]
    assert "x-amzn-bedrock-request-metadata" in signed


@pytest.mark.asyncio
async def test_in_account_path_never_calls_sts():
    sts = MagicMock()
    await _sign(_signer(sts=sts))
    sts.assume_role.assert_not_called()


@pytest.mark.asyncio
async def test_cross_account_without_sts_client_raises_instead_of_signing():
    """No silent fallback to the pod's identity.

    Signing with in-account creds when a cross-account role was configured would bill the
    wrong account AND hide the misconfiguration behind a working request — strictly worse
    than a loud failure.
    """
    with pytest.raises(RuntimeError, match="no STS client"):
        await _sign(_signer(sts=None), role_arn="arn:aws:iam::333344445555:role/x")


def _sts_returning(access="AKIDXACCT", expires_in=3600):
    sts = MagicMock()
    sts.assume_role.return_value = {
        "Credentials": {
            "AccessKeyId": access,
            "SecretAccessKey": "SECRETXACCT",
            "SessionToken": "TOKENXACCT",
            "Expiration": datetime.now(timezone.utc) + timedelta(seconds=expires_in),
        }
    }
    return sts


@pytest.mark.asyncio
async def test_cross_account_uses_assumed_credentials():
    sts = _sts_returning()
    headers = await _sign(_signer(sts=sts), role_arn="arn:aws:iam::333344445555:role/x")
    assert "AKIDXACCT" in headers["Authorization"]
    assert headers["X-Amz-Security-Token"] == "TOKENXACCT"
    kwargs = sts.assume_role.call_args.kwargs
    assert kwargs["RoleArn"] == "arn:aws:iam::333344445555:role/x"
    assert kwargs["DurationSeconds"] == 3600
    assert "ExternalId" not in kwargs  # omitted, not sent as None


@pytest.mark.asyncio
async def test_external_id_is_forwarded_when_present():
    sts = _sts_returning()
    await _sign(
        _signer(sts=sts), role_arn="arn:aws:iam::333344445555:role/x", external_id="ext-1"
    )
    assert sts.assume_role.call_args.kwargs["ExternalId"] == "ext-1"


@pytest.mark.asyncio
async def test_assumed_credentials_are_cached_per_role():
    sts = _sts_returning()
    signer = _signer(sts=sts)
    for _ in range(3):
        await _sign(signer, role_arn="arn:aws:iam::333344445555:role/x")
    assert sts.assume_role.call_count == 1
    # A different role is a different cache entry, not a reuse of the first one.
    await _sign(signer, role_arn="arn:aws:iam::222233334444:role/y")
    assert sts.assume_role.call_count == 2


@pytest.mark.asyncio
async def test_credentials_refresh_before_hard_expiry():
    """Refresh at expiry-minus-skew, not at expiry.

    Signing with a credential that expires mid-flight is a 403 on a request the user
    already paid for in latency, so the 300 s skew has to be honoured.
    """
    now = 1_000_000.0
    sts = _sts_returning()
    signer = SigV4Signer(sts_client=sts, now=lambda: now)
    signer._session = _static_session()
    role = "arn:aws:iam::333344445555:role/x"
    await _sign(signer, role_arn=role)
    assert sts.assume_role.call_count == 1

    expires_at = signer._creds[(role, "", "gw-bedrock-openai")].expires_at
    # Just inside the skew window: still cached.
    now = expires_at - 301
    await _sign(signer, role_arn=role)
    assert sts.assume_role.call_count == 1
    # Inside the skew window: re-assume even though the credential has not expired yet.
    now = expires_at - 299
    await _sign(signer, role_arn=role)
    assert sts.assume_role.call_count == 2


@pytest.mark.asyncio
async def test_concurrent_cross_account_signs_assume_once():
    """The lock exists so a cold start does not fire N AssumeRole calls at once."""
    sts = _sts_returning()
    signer = _signer(sts=sts)
    role = "arn:aws:iam::333344445555:role/x"
    await asyncio.gather(*[_sign(signer, role_arn=role) for _ in range(8)])
    assert sts.assume_role.call_count == 1


@pytest.mark.asyncio
async def test_missing_in_account_credentials_raises_clearly():
    signer = SigV4Signer()
    session = MagicMock()
    session.get_credentials.return_value = None
    signer._session = session
    with pytest.raises(RuntimeError, match="IRSA"):
        await _sign(signer)


@pytest.mark.asyncio
async def test_frozen_credentials_are_refetched_each_call():
    """botocore's session owns refresh, so caching the frozen copy would pin an expired one."""
    signer = _signer()
    await _sign(signer)
    await _sign(signer)
    assert signer._session.get_credentials.return_value.get_frozen_credentials.call_count == 2
    # ...but the session itself is built once, or every request re-runs the IRSA chain.
    assert signer._session.get_credentials.call_count == 2
