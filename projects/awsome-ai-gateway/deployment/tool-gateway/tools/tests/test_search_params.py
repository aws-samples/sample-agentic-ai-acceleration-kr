"""Tests for _shared.search_params domain + engine param mapping."""

from _shared.search_params import normalize_domains, apply_exa, apply_firecrawl, apply_perplexity


class TestNormalizeDomains:
    def test_list_input(self):
        assert normalize_domains(["a.com", "b.com"]) == ["a.com", "b.com"]

    def test_comma_string_input(self):
        assert normalize_domains("a.com, b.com ,c.com") == ["a.com", "b.com", "c.com"]

    def test_empty_and_whitespace(self):
        assert normalize_domains("") == []
        assert normalize_domains(None) == []
        assert normalize_domains(["", "  ", "a.com"]) == ["a.com"]

    def test_non_string_items_coerced(self):
        assert normalize_domains([123, "a.com"]) == ["123", "a.com"]


class TestExaDomains:
    def test_include_and_exclude(self):
        p = apply_exa({}, "", "", include_domains=["a.com"], exclude_domains=["b.com"])
        assert p["includeDomains"] == ["a.com"]
        assert p["excludeDomains"] == ["b.com"]

    def test_absent_domains_not_set(self):
        p = apply_exa({}, "", "")
        assert "includeDomains" not in p
        assert "excludeDomains" not in p


class TestFirecrawlDomains:
    def test_include_wins_when_both_given(self):
        p = apply_firecrawl({}, "", "", include_domains=["a.com"], exclude_domains=["b.com"])
        assert p["includeDomains"] == ["a.com"]
        assert "excludeDomains" not in p

    def test_exclude_only(self):
        p = apply_firecrawl({}, "", "", exclude_domains=["b.com"])
        assert p["excludeDomains"] == ["b.com"]
        assert "includeDomains" not in p


class TestPerplexityDomains:
    def test_merges_with_minus_prefix(self):
        p = apply_perplexity({}, "", "", include_domains=["a.com"], exclude_domains=["b.com"])
        assert p["search_domain_filter"] == ["a.com", "-b.com"]

    def test_absent_domains_not_set(self):
        p = apply_perplexity({}, "", "")
        assert "search_domain_filter" not in p
