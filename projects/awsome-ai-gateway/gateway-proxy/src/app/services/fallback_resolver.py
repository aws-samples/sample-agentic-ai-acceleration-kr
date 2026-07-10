# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
from __future__ import annotations

from collections.abc import Callable
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


def make_same_provider(
    alias_provider_map: dict[str, Any],
    original_provider: Any,
) -> Callable[[str], bool]:
    """Return a predicate that admits only aliases whose provider matches *original_provider*.

    The predicate uses a strict deny-by-default policy: if an alias is absent
    from *alias_provider_map* the predicate returns ``False`` (excluded).  This
    prevents cross-provider candidates (e.g. Bedrock aliases when the original
    is BEDROCK_MANTLE) from silently entering the fallback try_order.

    Type-safety note: ``ProviderType`` is a ``str``-``Enum``, so comparing a
    ``ProviderType`` value to a plain DB string (e.g. ``"BEDROCK"``) yields
    ``True`` without any explicit casting.  Both storing ``ProviderType`` enums
    and raw DB strings in *alias_provider_map* are therefore safe.

    Args:
        alias_provider_map: Mapping of alias → provider value (ProviderType or
            plain string from DB).  Built at startup from ``model.model_aliases``.
        original_provider: The provider of the originally-requested model
            (``model_config.provider``, a ``ProviderType`` enum).

    Returns:
        A callable ``(alias: str) -> bool`` suitable for passing to
        ``FallbackResolver.resolve(same_provider=...)``.
    """

    def _predicate(alias: str) -> bool:
        return alias_provider_map.get(alias) == original_provider

    return _predicate


class FallbackResolver:
    """Compute the ordered list of models to try, starting with the original.

    chain: dict of from_alias -> to_alias (quality-descending), sourced from
    budget.downgrade_policies (threshold ignored). Fallback CANDIDATES must be in the
    same provider as the original (never cross account) and in the key's allowed pool.
    The ORIGINAL is always first regardless of the allowed pool (it is the user's
    requested model; allowed filtering governs fallback targets, not the request itself).
    Cycle-guarded (max_depth).
    """

    def __init__(self, chain: dict[str, str], max_depth: int = 5) -> None:
        self.chain = chain
        self.max_depth = max_depth

    def resolve(
        self,
        *,
        original: str,
        allowed: set[str] | None,
        same_provider: Callable[[str], bool],
    ) -> list[str]:
        order = [original]
        visited = {original}
        cur = original
        for _ in range(self.max_depth):
            nxt = self.chain.get(cur)
            if nxt is None or nxt in visited:
                break
            visited.add(nxt)
            cur = nxt
            if not same_provider(nxt):
                continue  # never cross provider/account
            if allowed is not None and nxt not in allowed:
                continue  # not in this key's allowed pool
            order.append(nxt)
        logger.debug("fallback_chain_resolved", original=original, order=order)
        return order
