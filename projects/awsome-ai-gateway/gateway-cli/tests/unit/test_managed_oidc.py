# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""managed-settings OIDC env block + OIDC value resolution in setup.

Regression target: ``gateway-cli setup`` did not write OIDC_ISSUER_URL /
OIDC_CLIENT_ID, so api-key-helper silently fell back to STS (IAM) mode. That
splits into two outcomes and customers hit both — (1) a VK issued to a
*different user*, resolved from the AWS ARN, (2) with no SSO session, no VK at
all, so Claude Code falls back to 1P login.
"""

from __future__ import annotations

from unittest.mock import patch

from cli.managed import build_gateway_settings
from cli.setup import _resolve_oidc


def _base(**kw) -> dict:
    args = dict(
        gateway_url="https://gw.example.com",
        admin_api_url="https://admin.example.com",
        api_key_helper_path="/usr/local/bin/api-key-helper",
    )
    args.update(kw)
    return build_gateway_settings(**args)


class TestBuildGatewaySettingsOIDC:
    def test_writes_both_oidc_keys(self) -> None:
        env = _base(
            oidc_issuer_url="https://idp.example.com/tenant",
            oidc_client_id="client-abc",
        )["env"]
        assert env["OIDC_ISSUER_URL"] == "https://idp.example.com/tenant"
        assert env["OIDC_CLIENT_ID"] == "client-abc"

    def test_trailing_slash_stripped(self) -> None:
        """The issuer must match the token's iss exactly, and it is also used as a cache key."""
        env = _base(
            oidc_issuer_url="https://idp.example.com/tenant/",
            oidc_client_id="c",
        )["env"]
        assert env["OIDC_ISSUER_URL"] == "https://idp.example.com/tenant"

    def test_partial_config_writes_neither(self) -> None:
        """Only one of the two still falls back to STS, so don't write a misleading half config."""
        env = _base(oidc_issuer_url="https://idp.example.com")["env"]
        assert "OIDC_ISSUER_URL" not in env
        assert "OIDC_CLIENT_ID" not in env

    def test_audience_omitted_when_blank(self) -> None:
        """Writing an empty audience can cause a 401, since IDPs interpret it differently."""
        env = _base(
            oidc_issuer_url="https://idp.example.com", oidc_client_id="c",
            oidc_audience="",
        )["env"]
        assert "OIDC_AUDIENCE" not in env

    def test_audience_written_when_given(self) -> None:
        env = _base(
            oidc_issuer_url="https://idp.example.com", oidc_client_id="c",
            oidc_audience="api://gateway",
        )["env"]
        assert env["OIDC_AUDIENCE"] == "api://gateway"

    def test_no_oidc_keeps_previous_behaviour(self) -> None:
        """Without OIDC, the existing 2 keys + helper path stay unchanged (no regression)."""
        s = _base()
        assert s["env"] == {
            "ANTHROPIC_BASE_URL": "https://gw.example.com",
            "GATEWAY_CLI_GATEWAY_URL": "https://admin.example.com",
        }
        assert s["apiKeyHelper"] == "/usr/local/bin/api-key-helper"

    def test_admin_api_url_not_duplicated(self) -> None:
        """ADMIN_API_URL falls back to GATEWAY_CLI_GATEWAY_URL, so it is not written twice."""
        env = _base(
            oidc_issuer_url="https://idp.example.com", oidc_client_id="c",
        )["env"]
        assert "ADMIN_API_URL" not in env

    def test_otel_block_unaffected(self) -> None:
        env = _base(
            otel_endpoint="http://otel:4317", otel_auth_token="tok",
            oidc_issuer_url="https://idp.example.com", oidc_client_id="c",
        )["env"]
        assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://otel:4317"
        assert env["OTEL_EXPORTER_OTLP_HEADERS"] == "Authorization=Bearer tok"
        assert env["OIDC_CLIENT_ID"] == "c"


class TestResolveOIDC:
    def test_option_wins_over_env(self, monkeypatch) -> None:
        monkeypatch.setenv("OIDC_ISSUER_URL", "https://env.example.com")
        monkeypatch.setenv("OIDC_CLIENT_ID", "env-client")
        issuer, client, src = _resolve_oidc("https://opt.example.com", "opt-client")
        assert (issuer, client) == ("https://opt.example.com", "opt-client")
        assert src == "option"

    def test_env_used_when_no_option(self, monkeypatch) -> None:
        monkeypatch.setenv("OIDC_ISSUER_URL", "https://env.example.com/")
        monkeypatch.setenv("OIDC_CLIENT_ID", "env-client")
        issuer, client, src = _resolve_oidc(None, None)
        assert (issuer, client, src) == ("https://env.example.com", "env-client", "env")

    def test_falls_back_to_login_cache(self, monkeypatch) -> None:
        """Supports the documented order: run login, then run setup from a new shell."""
        monkeypatch.delenv("OIDC_ISSUER_URL", raising=False)
        monkeypatch.delenv("OIDC_CLIENT_ID", raising=False)

        class _Tok:
            issuer_url = "https://cached.example.com"
            client_id = "cached-client"

        with patch("gateway_cli_oidc.oidc_client.load_tokens", return_value=_Tok()):
            issuer, client, src = _resolve_oidc(None, None)
        assert (issuer, client, src) == (
            "https://cached.example.com", "cached-client", "login cache",
        )

    def test_empty_when_nothing_available(self, monkeypatch) -> None:
        monkeypatch.delenv("OIDC_ISSUER_URL", raising=False)
        monkeypatch.delenv("OIDC_CLIENT_ID", raising=False)
        with patch("gateway_cli_oidc.oidc_client.load_tokens", return_value=None):
            assert _resolve_oidc(None, None) == ("", "", "")

    def test_cache_error_is_not_fatal(self, monkeypatch) -> None:
        """setup must proceed even when the token cache is corrupt (in STS mode)."""
        monkeypatch.delenv("OIDC_ISSUER_URL", raising=False)
        monkeypatch.delenv("OIDC_CLIENT_ID", raising=False)
        with patch(
            "gateway_cli_oidc.oidc_client.load_tokens", side_effect=OSError("boom")
        ):
            assert _resolve_oidc(None, None) == ("", "", "")

    def test_option_issuer_plus_env_client(self, monkeypatch) -> None:
        """Partial sources count as OIDC as long as they combine into a complete pair."""
        monkeypatch.delenv("OIDC_ISSUER_URL", raising=False)
        monkeypatch.setenv("OIDC_CLIENT_ID", "env-client")
        issuer, client, _src = _resolve_oidc("https://opt.example.com", None)
        assert (issuer, client) == ("https://opt.example.com", "env-client")

    def test_cache_client_borrowed_only_for_same_idp(self, monkeypatch) -> None:
        """Borrowing a cached client_id from a different IDP produces a pair that does not exist.

        With that pair, api-key-helper enters OIDC mode and then dies on 'different IDP',
        so no VK is issued at all (worse than the STS fallback). It must not borrow.
        """
        monkeypatch.delenv("OIDC_ISSUER_URL", raising=False)
        monkeypatch.delenv("OIDC_CLIENT_ID", raising=False)

        class _Tok:
            issuer_url = "https://other-idp.example.com"
            client_id = "other-client"

        with patch("gateway_cli_oidc.oidc_client.load_tokens", return_value=_Tok()):
            assert _resolve_oidc("https://my-idp.example.com", None) == ("", "", "")

    def test_cache_client_borrowed_when_issuer_matches(self, monkeypatch) -> None:
        """For the same IDP, borrowing just the client_id from the cache is correct."""
        monkeypatch.delenv("OIDC_ISSUER_URL", raising=False)
        monkeypatch.delenv("OIDC_CLIENT_ID", raising=False)

        class _Tok:
            issuer_url = "https://my-idp.example.com/"  # trailing slash on the cache side
            client_id = "cached-client"

        with patch("gateway_cli_oidc.oidc_client.load_tokens", return_value=_Tok()):
            issuer, client, src = _resolve_oidc("https://my-idp.example.com", None)
        assert (issuer, client, src) == (
            "https://my-idp.example.com", "cached-client", "login cache",
        )

    def test_env_issuer_reported_as_env_source(self, monkeypatch) -> None:
        """Issuer from env but client_id from the cache — the label must point at the cache."""
        monkeypatch.setenv("OIDC_ISSUER_URL", "https://my-idp.example.com")
        monkeypatch.delenv("OIDC_CLIENT_ID", raising=False)

        class _Tok:
            issuer_url = "https://my-idp.example.com"
            client_id = "cached-client"

        with patch("gateway_cli_oidc.oidc_client.load_tokens", return_value=_Tok()):
            _issuer, client, src = _resolve_oidc(None, None)
        assert (client, src) == ("cached-client", "login cache")
