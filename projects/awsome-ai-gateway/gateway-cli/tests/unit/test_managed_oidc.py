# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""managed-settings OIDC env block + setup 의 OIDC 값 해석.

회귀 대상 결함: ``gateway-cli setup`` 이 OIDC_ISSUER_URL / OIDC_CLIENT_ID 를 쓰지 않아
api-key-helper 가 STS(IAM) 모드로 조용히 떨어졌다. 결과는 두 갈래인데 둘 다 고객이
겪었다 — (1) AWS ARN 기준으로 *다른 사용자* 에게 VK 발급, (2) SSO 세션이 없으면 VK
자체를 못 받아 Claude Code 가 1P 로그인으로 되돌아감.
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
        """issuer 는 토큰 iss 와 정확히 일치해야 하고, 캐시 키에도 쓰인다."""
        env = _base(
            oidc_issuer_url="https://idp.example.com/tenant/",
            oidc_client_id="c",
        )["env"]
        assert env["OIDC_ISSUER_URL"] == "https://idp.example.com/tenant"

    def test_partial_config_writes_neither(self) -> None:
        """하나만 있으면 STS 로 떨어지므로, 반쪽 설정을 기록해 오해를 남기지 않는다."""
        env = _base(oidc_issuer_url="https://idp.example.com")["env"]
        assert "OIDC_ISSUER_URL" not in env
        assert "OIDC_CLIENT_ID" not in env

    def test_audience_omitted_when_blank(self) -> None:
        """빈 audience 를 기록하면 IDP 별 해석 차이로 401 을 유발할 수 있다."""
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
        """OIDC 없이도 기존 2개 키 + helper 경로는 그대로 (무회귀)."""
        s = _base()
        assert s["env"] == {
            "ANTHROPIC_BASE_URL": "https://gw.example.com",
            "GATEWAY_CLI_GATEWAY_URL": "https://admin.example.com",
        }
        assert s["apiKeyHelper"] == "/usr/local/bin/api-key-helper"

    def test_admin_api_url_not_duplicated(self) -> None:
        """ADMIN_API_URL 은 GATEWAY_CLI_GATEWAY_URL 로 폴백되므로 중복 기록하지 않는다."""
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
        """login 을 한 뒤 새 셸에서 setup 을 실행하는 문서화된 순서를 지원."""
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
        """토큰 캐시가 깨져 있어도 setup 은 진행돼야 한다 (STS 모드로)."""
        monkeypatch.delenv("OIDC_ISSUER_URL", raising=False)
        monkeypatch.delenv("OIDC_CLIENT_ID", raising=False)
        with patch(
            "gateway_cli_oidc.oidc_client.load_tokens", side_effect=OSError("boom")
        ):
            assert _resolve_oidc(None, None) == ("", "", "")

    def test_option_issuer_plus_env_client(self, monkeypatch) -> None:
        """부분 조합도 합쳐서 완성되면 OIDC 로 인정."""
        monkeypatch.delenv("OIDC_ISSUER_URL", raising=False)
        monkeypatch.setenv("OIDC_CLIENT_ID", "env-client")
        issuer, client, _src = _resolve_oidc("https://opt.example.com", None)
        assert (issuer, client) == ("https://opt.example.com", "env-client")

    def test_cache_client_borrowed_only_for_same_idp(self, monkeypatch) -> None:
        """다른 IDP 의 캐시 client_id 를 빌려오면 존재하지 않는 조합이 된다.

        그 조합이면 api-key-helper 는 OIDC 모드로 진입한 뒤 'different IDP' 로 죽어
        VK 를 아예 못 받는다 (STS 폴백보다 나쁨). 따라서 빌려오지 않아야 한다.
        """
        monkeypatch.delenv("OIDC_ISSUER_URL", raising=False)
        monkeypatch.delenv("OIDC_CLIENT_ID", raising=False)

        class _Tok:
            issuer_url = "https://other-idp.example.com"
            client_id = "other-client"

        with patch("gateway_cli_oidc.oidc_client.load_tokens", return_value=_Tok()):
            assert _resolve_oidc("https://my-idp.example.com", None) == ("", "", "")

    def test_cache_client_borrowed_when_issuer_matches(self, monkeypatch) -> None:
        """같은 IDP 면 client_id 만 캐시에서 빌려오는 것이 맞다."""
        monkeypatch.delenv("OIDC_ISSUER_URL", raising=False)
        monkeypatch.delenv("OIDC_CLIENT_ID", raising=False)

        class _Tok:
            issuer_url = "https://my-idp.example.com/"  # 캐시 쪽에 slash
            client_id = "cached-client"

        with patch("gateway_cli_oidc.oidc_client.load_tokens", return_value=_Tok()):
            issuer, client, src = _resolve_oidc("https://my-idp.example.com", None)
        assert (issuer, client, src) == (
            "https://my-idp.example.com", "cached-client", "login cache",
        )

    def test_env_issuer_reported_as_env_source(self, monkeypatch) -> None:
        """issuer 는 env, client_id 만 캐시에서 온 경우 라벨이 캐시를 가리켜야 한다."""
        monkeypatch.setenv("OIDC_ISSUER_URL", "https://my-idp.example.com")
        monkeypatch.delenv("OIDC_CLIENT_ID", raising=False)

        class _Tok:
            issuer_url = "https://my-idp.example.com"
            client_id = "cached-client"

        with patch("gateway_cli_oidc.oidc_client.load_tokens", return_value=_Tok()):
            _issuer, client, src = _resolve_oidc(None, None)
        assert (client, src) == ("cached-client", "login cache")
