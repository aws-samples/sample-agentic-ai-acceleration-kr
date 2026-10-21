# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Token counts for models Bedrock CountTokens refuses — counted on Mantle, same region.

Bedrock CountTokens is enabled per model, not per API. Measured 2026-10-19 in
ap-northeast-2, us-east-1 and us-west-2 (bare model id, ``global.`` profile, and the
``converse`` input form all give the same answer):

    counts            Haiku 4.5, Sonnet 4.5, Sonnet 4.6, Opus 4.5, Opus 4.6
    refuses           Opus 4.7, Opus 4.8, Sonnet 5, Opus 5, Fable 5, Fable 5.1,
                      Sonnet 5.5, Opus 5.5, Haiku 5.5
                      -> ValidationException "The provided model doesn't support
                         counting tokens." — a model capability, not a permission

Granting ``bedrock:CountTokens`` changes nothing for the second row. For those
models the only free, exact count is Mantle's Anthropic endpoint
(``bedrock-mantle.<region>.api.aws/anthropic/v1/messages/count_tokens``), which
returned exactly the ``usage.input_tokens`` of a real invoke for the same model
(sonnet-5 547/497/582/494, opus-5 2073). But Mantle serves only some models per
region — ap-northeast-2 serves sonnet-5 and opus-5 and nothing else in the row above.

So the count is tried on the model itself first and then, only for the models in
``_FAMILY``, on a sibling that tokenizes the same way. Measured against real invoke
usage (English, code, Korean, system + 1/10/30 tools): text agrees to ±2 tokens and
each model adds a constant tool-template offset that does not grow with the number of
tools — relative to opus-5: opus-4-8 +4, the 5.5 models and fable-5-1 +2, sonnet-5
+68. Opus 4.7 (+26% with tools) and Fable 5 (+8–10% on code and Korean) tokenize
differently and are deliberately absent: they get a same-model Mantle count where
Mantle serves them, and an honest error otherwise. Add a model here only after the
same measurement.

A ``/v1/messages/count_tokens`` count runs with the account and in the region the
request itself would use (the cut-stream estimate uses the pod account and region, like
the Bedrock call it backs up). It sends the whole prompt, so counting anywhere else
would move prompt data across accounts or regions. For the same reason fable-5-1 is
never a sibling for another model: Fable traffic runs under the account's
``aws_review`` data-retention mode, which was approved for Fable alone.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

import structlog

from app.schemas.routing import RoutingProfileSchema
from app.services.account_credentials import ACCOUNT_UNAVAILABLE_CODE, AccountTarget
from app.services.thinking_normalizer import normalize_thinking

logger = structlog.get_logger(__name__)

#: Bedrock's whole ValidationException message for a model CountTokens is not enabled
#: for. Compared in full: validation messages quote request field names, so a substring
#: test would let one crafted request mark a countable model refused for everyone.
_UNSUPPORTED_MESSAGE = "the provided model doesn't support counting tokens"

#: Models that may borrow a sibling's count — the same-tokenizer family.
_FAMILY: frozenset[str] = frozenset({
    "anthropic.claude-opus-5-5",
    "anthropic.claude-sonnet-5-5",
    "anthropic.claude-haiku-5-5",
    "anthropic.claude-opus-5",
    "anthropic.claude-opus-4-8",
    "anthropic.claude-fable-5-1",
    "anthropic.claude-sonnet-5",
})
#: Models that may lend their count, closest-first for the 5.5 models. fable-5-1 is
#: absent on purpose (module docstring): it counts only for itself.
_SIBLINGS: tuple[str, ...] = (
    "anthropic.claude-opus-5-5",
    "anthropic.claude-sonnet-5-5",
    "anthropic.claude-haiku-5-5",
    "anthropic.claude-opus-5",
    "anthropic.claude-opus-4-8",
    "anthropic.claude-sonnet-5",
)

#: Content block types the sibling measurements did not cover. A body that carries
#: any of them is counted on the requested model only.
_UNMEASURED_BLOCKS = frozenset({"image", "document"})

#: What changes the input count. ``max_tokens``, sampling, ``metadata`` and
#: ``output_config`` are generation-only (effort low/high/max measured identical);
#: ``anthropic_version`` is Bedrock's body field.
_MANTLE_COUNT_FIELDS = frozenset({"messages", "system", "tools", "tool_choice", "thinking"})


class _TTLSet:
    """A bounded set whose members expire. Process-local on purpose: a stale entry
    costs one extra upstream call, never a wrong count."""

    def __init__(self, ttl_seconds: float, max_entries: int = 1024) -> None:
        self._ttl = ttl_seconds
        self._max = max_entries
        self._expiry: dict[object, float] = {}

    def add(self, key: object) -> None:
        now = time.monotonic()
        if len(self._expiry) >= self._max:
            self._expiry = {k: v for k, v in self._expiry.items() if v > now}
            if len(self._expiry) >= self._max:
                self._expiry.pop(next(iter(self._expiry)))
        self._expiry[key] = now + self._ttl

    def __contains__(self, key: object) -> bool:
        expiry = self._expiry.get(key)
        if expiry is None:
            return False
        if expiry <= time.monotonic():
            del self._expiry[key]
            return False
        return True

    def clear(self) -> None:
        self._expiry.clear()


#: Models Bedrock refused to count. Skips the doomed call; re-probed after an hour in
#: case AWS enables the model.
BEDROCK_UNSUPPORTED = _TTLSet(3600)
#: (region, model) that Mantle does not serve (404).
MANTLE_MISSING = _TTLSet(600)
#: (account, region, model) the account is not entitled to on Mantle (403 — model
#: specific; Mantle reports an IAM denial as 401).
MANTLE_DENIED = _TTLSet(300)
#: (account, region) whose Mantle credentials failed (401 or no usable account).
MANTLE_BLOCKED = _TTLSet(300)
#: (account, region) where Mantle could not be reached. Short: it is a network state,
#: and without it every count would wait out a connect timeout again.
MANTLE_UNREACHABLE = _TTLSet(60)
_ALL_CACHES = (
    BEDROCK_UNSUPPORTED, MANTLE_MISSING, MANTLE_DENIED, MANTLE_BLOCKED, MANTLE_UNREACHABLE,
)


def clear_caches() -> None:
    for cache in _ALL_CACHES:
        cache.clear()


def count_base_model_id(provider_model_id: str) -> str:
    """The id CountTokens and Mantle accept: no routing prefix, no inference-profile ARN.

    Cut at the vendor segment rather than matching a prefix list, as the thinking
    normalizer does — a list misses ``jp.``/``au.``/``in.``/``ca.`` and ARNs.
    """
    i = provider_model_id.find("anthropic.")
    if i > 0:
        return provider_model_id[i:]
    return provider_model_id


def is_count_unsupported(error_code: str | None, error_message: str | None) -> bool:
    """True when Bedrock refused because the model cannot be counted at all."""
    message = (error_message or "").strip().rstrip(".").lower()
    return error_code == "ValidationException" and message == _UNSUPPORTED_MESSAGE


def is_bedrock_unsupported(result) -> bool:
    return is_count_unsupported(
        getattr(result, "error_code", None), getattr(result, "error_message", None)
    )


#: Nesting this deep is treated as "unmeasured": no real request goes there, and the
#: walk must not recurse on client data.
_MAX_BLOCK_DEPTH = 32


def _has_unmeasured_blocks(body: dict) -> bool:
    messages = body.get("messages")
    if not isinstance(messages, list):
        return True
    stack = [(m.get("content"), 0) for m in messages if isinstance(m, dict)]
    while stack:
        content, depth = stack.pop()
        if not isinstance(content, list):
            continue
        if depth >= _MAX_BLOCK_DEPTH:
            return True
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if isinstance(block_type, str) and block_type in _UNMEASURED_BLOCKS:
                return True
            stack.append((block.get("content"), depth + 1))
    return False


def mantle_candidates(base_model: str, body: dict | None = None) -> list[str]:
    if base_model in _FAMILY and not (body and _has_unmeasured_blocks(body)):
        return [base_model, *(m for m in _SIBLINGS if m != base_model)]
    return [base_model]


def _mantle_body(body: dict, model: str) -> bytes:
    # Shallow: normalize_thinking and the "model" key only touch top-level keys.
    b = {k: v for k, v in body.items() if k in _MANTLE_COUNT_FIELDS}
    # The request would be normalised for the model that runs it; count the same body.
    normalize_thinking(b, model)
    b["model"] = model
    return json.dumps(b).encode()


@dataclass(frozen=True)
class MantleCount:
    input_tokens: int
    model: str  # the model the count came from; differs from the request's for a sibling


async def count_on_mantle(
    adapter,
    *,
    body: dict,
    base_model: str,
    region: str,
    account: AccountTarget | None = None,
    profile_role_arn: str | None = None,
    profile_external_id: str | None = None,
    client: str | None = None,
) -> MantleCount | None:
    """Count ``body`` on Mantle in ``region`` with the account that serves the model.

    Account precedence matches the native path: the alias account, else the app
    profile's cross-account role, else the pod. Returns None when no candidate counts.
    """
    if adapter is None:
        return None
    blocked_key = (account.role_arn if account else profile_role_arn or "in-account", region)
    if blocked_key in MANTLE_BLOCKED or blocked_key in MANTLE_UNREACHABLE:
        return None
    profile = RoutingProfileSchema(
        client=client or "count_tokens",
        backend="mantle",
        region=region,
        account_role_arn=profile_role_arn,
        external_id=profile_external_id,
    )
    endpoint = f"https://bedrock-mantle.{region}.api.aws/anthropic"
    kwargs = {"account": account} if account is not None else {}
    for model in mantle_candidates(base_model, body):
        if (region, model) in MANTLE_MISSING or (*blocked_key, model) in MANTLE_DENIED:
            continue
        result = await adapter.count_tokens(
            _mantle_body(body, model), model, profile=profile, endpoint=endpoint, **kwargs
        )
        status = result[0]
        code = getattr(result, "error_code", None)
        if status == 200:
            return MantleCount(result[1], model)
        if status == 404:
            MANTLE_MISSING.add((region, model))
            continue
        if status == 403:
            # Model entitlement, not the account: the next candidate may well count.
            MANTLE_DENIED.add((*blocked_key, model))
            continue
        if status in (400, 422):
            # A refusal of the requested model itself (e.g. data retention not enabled)
            # leaves the siblings worth asking. A sibling's 400 is most likely this
            # request's body, which every other candidate would refuse too. Not cached
            # either way: the body is per request.
            if model == base_model:
                continue
            return None
        if status == 401 or code in (ACCOUNT_UNAVAILABLE_CODE, "MantleAuthFailed"):
            MANTLE_BLOCKED.add(blocked_key)
        elif code == "MantleUnreachable":
            MANTLE_UNREACHABLE.add(blocked_key)
        # 429, upstream 5xx, and a call that connected but failed or timed out (a large
        # prompt, a full connection pool): stop for this request, remember nothing.
        return None
    return None


def make_text_counter(adapter, region: str):
    """``(text, model_id) -> int | None`` for TokenizerService (cut-stream estimates).

    Pod account and pod region, like the Bedrock CountTokens call it backs up.
    """

    async def _count(text: str, model_id: str) -> int | None:
        result = await count_on_mantle(
            adapter,
            body={"messages": [{"role": "user", "content": text}]},
            base_model=count_base_model_id(model_id),
            region=region,
            client="tokenizer",
        )
        return result.input_tokens if result is not None else None

    return _count
