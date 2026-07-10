# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
from app.services.fallback_resolver import FallbackResolver, make_same_provider
from app.schemas.domain import ProviderType


def _chain():
    return {
        "claude-opus-4-8": "claude-sonnet-4-6",
        "claude-sonnet-4-6": "claude-haiku-4-5-20251001",
    }


def test_resolves_descending_chain():
    r = FallbackResolver(chain=_chain())
    order = r.resolve(original="claude-opus-4-8", allowed=None, same_provider=lambda a: True)
    assert order == ["claude-opus-4-8", "claude-sonnet-4-6", "claude-haiku-4-5-20251001"]


def test_intersects_allowed_pool():
    r = FallbackResolver(chain=_chain())
    order = r.resolve(
        original="claude-opus-4-8",
        allowed={"claude-opus-4-8", "claude-haiku-4-5-20251001"},  # sonnet not allowed
        same_provider=lambda a: True,
    )
    assert order == ["claude-opus-4-8", "claude-haiku-4-5-20251001"]


def test_stops_at_provider_boundary():
    r = FallbackResolver(chain={"cowork-opus": "claude-opus-4-8"})
    order = r.resolve(original="cowork-opus", allowed=None,
                      same_provider=lambda a: a == "cowork-opus")
    assert order == ["cowork-opus"]  # no same-provider fallback available


def test_cycle_guard():
    r = FallbackResolver(chain={"a": "b", "b": "a"})
    order = r.resolve(original="a", allowed=None, same_provider=lambda a: True)
    assert order == ["a", "b"]  # 'a' not revisited


def test_no_chain_edge_returns_only_original():
    r = FallbackResolver(chain={})
    order = r.resolve(original="claude-opus-4-8", allowed=None, same_provider=lambda a: True)
    assert order == ["claude-opus-4-8"]


def test_max_depth_bounds_chain():
    chain = {str(i): str(i + 1) for i in range(8)}  # 0->1->...->8
    r = FallbackResolver(chain=chain, max_depth=3)
    order = r.resolve(original="0", allowed=None, same_provider=lambda a: True)
    assert order == ["0", "1", "2", "3"]  # original + 3 hops


def test_original_always_first_even_if_not_in_allowed():
    # The original is the requested model; it is always attempted first regardless of `allowed`
    # (allowed filtering applies to FALLBACK candidates, not the user's own requested model).
    r = FallbackResolver(chain=_chain())
    order = r.resolve(original="claude-opus-4-8", allowed={"claude-haiku-4-5-20251001"},
                      same_provider=lambda a: True)
    assert order[0] == "claude-opus-4-8"
    assert "claude-haiku-4-5-20251001" in order
    assert "claude-sonnet-4-6" not in order


# ===========================================================================
# make_same_provider helper tests (STEP 1 — these fail until the helper exists)
# ===========================================================================

class TestMakeSameProvider:
    """Unit tests for the make_same_provider factory extracted from messages.py.

    The factory returns a predicate that admits only aliases whose provider
    is positively known to match the original's provider.  Absent entries
    are excluded (strict / deny-by-default).
    """

    def test_bedrock_original_admits_bedrock_candidates(self):
        """Bedrock original + Bedrock aliases in map → all admitted."""
        alias_provider_map = {
            "claude-opus-4-8": ProviderType.BEDROCK,
            "claude-sonnet-4-6": ProviderType.BEDROCK,
            "claude-haiku-4-5-20251001": ProviderType.BEDROCK,
        }
        pred = make_same_provider(alias_provider_map, ProviderType.BEDROCK)
        assert pred("claude-opus-4-8") is True
        assert pred("claude-sonnet-4-6") is True
        assert pred("claude-haiku-4-5-20251001") is True

    def test_bedrock_original_full_chain_resolves_all_three(self):
        """Bedrock original with full Bedrock chain → try_order = [opus, sonnet, haiku]."""
        alias_provider_map = {
            "claude-opus-4-8": ProviderType.BEDROCK,
            "claude-sonnet-4-6": ProviderType.BEDROCK,
            "claude-haiku-4-5-20251001": ProviderType.BEDROCK,
        }
        pred = make_same_provider(alias_provider_map, ProviderType.BEDROCK)
        r = FallbackResolver(chain=_chain())
        order = r.resolve(original="claude-opus-4-8", allowed=None, same_provider=pred)
        assert order == ["claude-opus-4-8", "claude-sonnet-4-6", "claude-haiku-4-5-20251001"]

    def test_mantle_original_excludes_bedrock_candidates(self):
        """Mantle original + Bedrock-only candidates in chain → try_order = [original only].

        The map has the Mantle original but NOT the Bedrock fallback aliases
        (as would be the case at startup: Bedrock aliases are mapped as BEDROCK,
        Mantle aliases as BEDROCK_MANTLE).
        """
        alias_provider_map = {
            "cowork-opus": ProviderType.BEDROCK_MANTLE,
            "claude-sonnet-4-6": ProviderType.BEDROCK,   # cross-provider
            "claude-haiku-4-5-20251001": ProviderType.BEDROCK,  # cross-provider
        }
        pred = make_same_provider(alias_provider_map, ProviderType.BEDROCK_MANTLE)
        r = FallbackResolver(chain={"cowork-opus": "claude-sonnet-4-6",
                                    "claude-sonnet-4-6": "claude-haiku-4-5-20251001"})
        order = r.resolve(original="cowork-opus", allowed=None, same_provider=pred)
        assert order == ["cowork-opus"]

    def test_unknown_alias_not_in_map_is_excluded(self):
        """An alias absent from the map returns False (deny-by-default / safe)."""
        alias_provider_map = {
            "claude-opus-4-8": ProviderType.BEDROCK,
        }
        pred = make_same_provider(alias_provider_map, ProviderType.BEDROCK)
        # "totally-unknown-model" not in map → excluded
        assert pred("totally-unknown-model") is False

    def test_db_string_provider_compares_correctly_with_provider_type(self):
        """DB yields plain strings; ProviderType is str-Enum so equality holds without casting."""
        # Simulate what the DB yields: plain string "BEDROCK"
        alias_provider_map = {
            "claude-sonnet-4-6": "BEDROCK",   # DB string, not ProviderType enum
            "claude-haiku-4-5-20251001": "BEDROCK",
        }
        pred = make_same_provider(alias_provider_map, ProviderType.BEDROCK)
        # ProviderType.BEDROCK == "BEDROCK" is True (str Enum)
        assert pred("claude-sonnet-4-6") is True
        assert pred("claude-haiku-4-5-20251001") is True

    def test_original_alias_always_same_provider_with_itself(self):
        """The original alias is always considered same-provider with itself."""
        alias_provider_map = {
            "claude-opus-4-8": ProviderType.BEDROCK,
        }
        pred = make_same_provider(alias_provider_map, ProviderType.BEDROCK)
        assert pred("claude-opus-4-8") is True
