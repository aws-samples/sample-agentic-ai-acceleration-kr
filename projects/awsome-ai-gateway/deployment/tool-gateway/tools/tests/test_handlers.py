"""Unit tests for search handlers with mocked HTTP."""

import json
import os
from unittest.mock import patch, MagicMock

import pytest
import responses


class TestSecretsManagerResolution:
    """get_api_key() resolves from Secrets Manager when *_SECRET_ARN is set."""

    def setup_method(self):
        # Clear the warm-Lambda cache between tests.
        from _shared import identity
        identity._api_key_cache.clear()

    def teardown_method(self):
        from _shared import identity
        identity._api_key_cache.clear()
        for k in ["SERPER_SECRET_ARN"]:
            os.environ.pop(k, None)

    def test_reads_plain_string_secret(self):
        os.environ["SERPER_SECRET_ARN"] = (
            "arn:aws:secretsmanager:us-east-1:913524902871:secret:websearch-gw/dev/tool/serper-AbCdEf"
        )
        fake_client = MagicMock()
        fake_client.get_secret_value.return_value = {"SecretString": "sk-plain-123"}
        with patch("boto3.client", return_value=fake_client):
            from _shared.identity import get_api_key
            key = get_api_key("serper")
        assert key == "sk-plain-123"
        fake_client.get_secret_value.assert_called_once_with(
            SecretId="arn:aws:secretsmanager:us-east-1:913524902871:secret:websearch-gw/dev/tool/serper-AbCdEf"
        )

    def test_reads_json_secret_with_api_key_field(self):
        os.environ["SERPER_SECRET_ARN"] = (
            "arn:aws:secretsmanager:us-east-1:913524902871:secret:websearch-gw/dev/tool/serper-AbCdEf"
        )
        fake_client = MagicMock()
        fake_client.get_secret_value.return_value = {"SecretString": '{"api_key": "sk-json-456"}'}
        with patch("boto3.client", return_value=fake_client):
            from _shared.identity import get_api_key
            key = get_api_key("serper")
        assert key == "sk-json-456"

    def test_falls_back_to_env_when_no_secret_arn(self):
        os.environ.pop("SERPER_SECRET_ARN", None)
        os.environ["SERPER_API_KEY"] = "sk-env-789"
        try:
            from _shared.identity import get_api_key
            key = get_api_key("serper")
            assert key == "sk-env-789"
        finally:
            os.environ.pop("SERPER_API_KEY", None)

    def test_falls_back_to_env_on_secrets_manager_error(self):
        os.environ["SERPER_SECRET_ARN"] = (
            "arn:aws:secretsmanager:us-east-1:913524902871:secret:websearch-gw/dev/tool/serper-AbCdEf"
        )
        os.environ["SERPER_API_KEY"] = "sk-env-fallback"
        fake_client = MagicMock()
        fake_client.get_secret_value.side_effect = Exception("AccessDenied")
        try:
            with patch("boto3.client", return_value=fake_client):
                from _shared.identity import get_api_key
                key = get_api_key("serper")
            assert key == "sk-env-fallback"
        finally:
            os.environ.pop("SERPER_API_KEY", None)


@pytest.fixture(autouse=True)
def setup_env():
    """Setup environment variables for tests."""
    os.environ["AWS_REGION"] = "us-east-1"
    os.environ["WORKLOAD_TOKEN"] = "test-token"
    os.environ["IDENTITY_PROVIDER_ARN"] = "arn:aws:bedrock-agentcore:us-east-1:123456789012:identity-provider/test"
    yield
    # Cleanup
    for key in ["AWS_REGION", "WORKLOAD_TOKEN", "IDENTITY_PROVIDER_ARN"]:
        if key in os.environ:
            del os.environ[key]


class TestSerperHandler:
    """Test Serper handler."""

    @responses.activate
    @patch("_shared.identity.get_api_key")
    def test_success(self, mock_get_api_key):
        """Test successful Serper query."""
        mock_get_api_key.return_value = "test-api-key"

        responses.add(
            responses.POST,
            "https://google.serper.dev/search",
            json={
                "organic": [
                    {
                        "title": "Result 1",
                        "link": "https://example.com/1",
                        "snippet": "Snippet 1",
                    },
                    {
                        "title": "Result 2",
                        "link": "https://example.com/2",
                        "snippet": "Snippet 2",
                    },
                ]
            },
            status=200,
        )

        from serper.handler import lambda_handler

        event = {"query": "test search"}
        result = lambda_handler(event, None)

        assert result["engine"] == "serper"
        assert len(result["organic"]) == 2
        assert result["organic"][0]["title"] == "Result 1"
        assert "latency_ms" in result

    @patch("_shared.identity.get_api_key")
    def test_missing_query(self, mock_get_api_key):
        """Test handler with missing query parameter."""
        mock_get_api_key.return_value = "test-api-key"

        from serper.handler import lambda_handler

        event = {}
        result = lambda_handler(event, None)

        assert result["engine"] == "serper"
        assert "error" in result

    @responses.activate
    @patch("_shared.identity.get_api_key")
    def test_api_error(self, mock_get_api_key):
        """Test handler with upstream API error."""
        mock_get_api_key.return_value = "test-api-key"

        responses.add(
            responses.POST,
            "https://google.serper.dev/search",
            status=500,
        )

        from serper.handler import lambda_handler

        event = {"query": "test search"}
        result = lambda_handler(event, None)

        assert result["engine"] == "serper"
        assert "error" in result


class TestExaHandler:
    """Test Exa handler."""

    @responses.activate
    @patch("_shared.identity.get_api_key")
    def test_success(self, mock_get_api_key):
        """Test successful Exa query."""
        mock_get_api_key.return_value = "test-api-key"

        responses.add(
            responses.POST,
            "https://api.exa.ai/search",
            json={
                "results": [
                    {
                        "title": "Result 1",
                        "url": "https://example.com/1",
                        "text": "Snippet 1",
                    },
                ]
            },
            status=200,
        )

        from exa.handler import lambda_handler

        event = {"query": "test search"}
        result = lambda_handler(event, None)

        assert result["engine"] == "exa"
        assert len(result["results"]) == 1

    @patch("_shared.identity.get_api_key")
    def test_missing_api_key(self, mock_get_api_key):
        """Test handler with missing API key."""
        mock_get_api_key.return_value = None

        from exa.handler import lambda_handler

        event = {"query": "test search"}
        result = lambda_handler(event, None)

        assert result["engine"] == "exa"
        assert "error" in result


class TestDuckDuckGoHandler:
    """Test DuckDuckGo handler."""

    @patch("ddgs.DDGS")
    def test_success(self, mock_ddgs):
        """Test successful DuckDuckGo query."""
        mock_instance = MagicMock()
        mock_instance.text.return_value = [
            {
                "title": "Result 1",
                "href": "https://example.com/1",
                "body": "Snippet 1",
            },
        ]
        mock_ddgs.return_value = mock_instance

        from duckduckgo.handler import lambda_handler

        event = {"query": "test search"}
        result = lambda_handler(event, None)

        assert result["engine"] == "duckduckgo"
        assert len(result["results"]) == 1

    def test_missing_query(self):
        """Test handler with missing query parameter."""
        from duckduckgo.handler import lambda_handler

        event = {}
        result = lambda_handler(event, None)

        assert result["engine"] == "duckduckgo"
        assert "error" in result


class TestPerplexityHandler:
    """Test Perplexity handler."""

    @responses.activate
    @patch("_shared.identity.get_api_key")
    def test_success(self, mock_get_api_key):
        """Test successful Perplexity query."""
        mock_get_api_key.return_value = "test-api-key"

        responses.add(
            responses.POST,
            "https://api.perplexity.ai/chat/completions",
            json={
                "citations": [
                    "https://example.com/1",
                    "https://example.com/2",
                ]
            },
            status=200,
        )

        from perplexity.handler import lambda_handler

        event = {"query": "test search"}
        result = lambda_handler(event, None)

        assert result["engine"] == "perplexity"
        assert result["citations"] == ["https://example.com/1", "https://example.com/2"]

    @responses.activate
    @patch("_shared.identity.get_api_key")
    def test_upstream_error(self, mock_get_api_key):
        """Test handler with upstream API error."""
        mock_get_api_key.return_value = "test-api-key"

        responses.add(
            responses.POST,
            "https://api.perplexity.ai/chat/completions",
            status=503,
        )

        from perplexity.handler import lambda_handler

        event = {"query": "test search"}
        result = lambda_handler(event, None)

        assert result["engine"] == "perplexity"
        assert "error" in result


class TestBraveHandler:
    """Test Brave handler."""

    @responses.activate
    @patch("_shared.identity.get_api_key")
    def test_success(self, mock_get_api_key):
        """Test successful Brave query."""
        mock_get_api_key.return_value = "test-api-key"

        responses.add(
            responses.GET,
            "https://api.search.brave.com/res/v1/web/search",
            json={
                "web": {
                    "results": [
                        {
                            "title": "Result 1",
                            "url": "https://example.com/1",
                            "description": "Snippet 1",
                        },
                        {
                            "title": "Result 2",
                            "url": "https://example.com/2",
                            "description": "Snippet 2",
                        },
                    ]
                }
            },
            status=200,
        )

        from brave.handler import lambda_handler

        event = {"query": "test search"}
        result = lambda_handler(event, None)

        assert result["engine"] == "brave"
        assert len(result["web"]["results"]) == 2
        assert result["web"]["results"][0]["title"] == "Result 1"
        assert result["web"]["results"][0]["description"] == "Snippet 1"

    @patch("_shared.identity.get_api_key")
    def test_missing_query(self, mock_get_api_key):
        """Test handler with missing query parameter."""
        mock_get_api_key.return_value = "test-api-key"

        from brave.handler import lambda_handler

        event = {}
        result = lambda_handler(event, None)

        assert result["engine"] == "brave"
        assert "error" in result

    @responses.activate
    @patch("_shared.identity.get_api_key")
    def test_api_error(self, mock_get_api_key):
        """Test handler with upstream API error."""
        mock_get_api_key.return_value = "test-api-key"

        responses.add(
            responses.GET,
            "https://api.search.brave.com/res/v1/web/search",
            status=500,
        )

        from brave.handler import lambda_handler

        event = {"query": "test search"}
        result = lambda_handler(event, None)

        assert result["engine"] == "brave"
        assert "error" in result


class TestAnthropicHandler:
    """Test Anthropic Claude built-in web search handler."""

    @responses.activate
    @patch("_shared.identity.get_api_key")
    def test_success(self, mock_get_api_key):
        """Test successful Anthropic query (native Messages content blocks)."""
        mock_get_api_key.return_value = "test-api-key"

        responses.add(
            responses.POST,
            "https://api.anthropic.com/v1/messages",
            json={
                "content": [
                    {
                        "type": "web_search_tool_result",
                        "content": [
                            {
                                "type": "web_search_result",
                                "title": "Result 1",
                                "url": "https://example.com/1",
                                "page_age": "2 days ago",
                            },
                        ],
                    },
                    {"type": "text", "text": "Here is a summary."},
                ]
            },
            status=200,
        )

        from anthropic.handler import lambda_handler

        event = {"query": "test search"}
        result = lambda_handler(event, None)

        assert result["engine"] == "anthropic"
        assert result["content"][0]["type"] == "web_search_tool_result"
        assert result["content"][0]["content"][0]["title"] == "Result 1"
        assert result["content"][1]["text"] == "Here is a summary."

    @patch("_shared.identity.get_api_key")
    def test_missing_query(self, mock_get_api_key):
        """Test handler with missing query parameter."""
        mock_get_api_key.return_value = "test-api-key"

        from anthropic.handler import lambda_handler

        result = lambda_handler({}, None)

        assert result["engine"] == "anthropic"
        assert "error" in result

    @responses.activate
    @patch("_shared.identity.get_api_key")
    def test_api_error(self, mock_get_api_key):
        """Test handler with upstream API error."""
        mock_get_api_key.return_value = "test-api-key"

        responses.add(
            responses.POST,
            "https://api.anthropic.com/v1/messages",
            status=500,
        )

        from anthropic.handler import lambda_handler

        result = lambda_handler({"query": "test search"}, None)

        assert result["engine"] == "anthropic"
        assert "error" in result


class TestFirecrawlHandler:
    """Test Firecrawl handler."""

    @responses.activate
    @patch("_shared.identity.get_api_key")
    def test_success(self, mock_get_api_key):
        """Test successful Firecrawl query."""
        mock_get_api_key.return_value = "test-api-key"

        responses.add(
            responses.POST,
            "https://api.firecrawl.dev/v1/search",
            json={
                "data": [
                    {
                        "title": "Result 1",
                        "url": "https://example.com/1",
                        "description": "Snippet 1",
                    },
                ]
            },
            status=200,
        )

        from firecrawl.handler import lambda_handler

        result = lambda_handler({"query": "test search"}, None)

        assert result["engine"] == "firecrawl"
        assert len(result["data"]) == 1
        assert result["data"][0]["description"] == "Snippet 1"

    @patch("_shared.identity.get_api_key")
    def test_missing_api_key(self, mock_get_api_key):
        """Test handler with missing API key."""
        mock_get_api_key.return_value = None

        from firecrawl.handler import lambda_handler

        result = lambda_handler({"query": "test search"}, None)

        assert result["engine"] == "firecrawl"
        assert "error" in result


class TestYouHandler:
    """Test You.com handler."""

    @responses.activate
    @patch("_shared.identity.get_api_key")
    def test_success(self, mock_get_api_key):
        """Test successful You.com query."""
        mock_get_api_key.return_value = "test-api-key"

        responses.add(
            responses.GET,
            "https://ydc-index.io/v1/search",
            json={
                "results": {
                    "web": [
                        {
                            "title": "Result 1",
                            "url": "https://example.com/1",
                            "snippets": ["Snippet 1", "Snippet 1b"],
                        },
                    ]
                }
            },
            status=200,
        )

        from you.handler import lambda_handler

        result = lambda_handler({"query": "test search"}, None)

        assert result["engine"] == "you"
        assert len(result["results"]["web"]) == 1
        assert result["results"]["web"][0]["snippets"] == ["Snippet 1", "Snippet 1b"]

    @responses.activate
    @patch("_shared.identity.get_api_key")
    def test_api_error(self, mock_get_api_key):
        """Test handler with upstream API error."""
        mock_get_api_key.return_value = "test-api-key"

        responses.add(
            responses.GET,
            "https://ydc-index.io/v1/search",
            status=429,
        )

        from you.handler import lambda_handler

        result = lambda_handler({"query": "test search"}, None)

        assert result["engine"] == "you"
        assert "error" in result


class TestResponseStamping:
    """stamp() preserves the provider's native shape; error_response() envelopes."""

    def test_stamp_preserves_native_payload(self):
        """stamp() adds engine/latency_ms without touching provider keys."""
        from _shared.response import stamp

        payload = {"organic": [{"title": "Test"}], "knowledgeGraph": {"x": 1}}
        response = stamp(payload, "serper", 100)

        assert response["engine"] == "serper"
        assert response["latency_ms"] == 100
        assert response["organic"] == [{"title": "Test"}]
        assert response["knowledgeGraph"] == {"x": 1}

    def test_stamp_does_not_mutate_input(self):
        """stamp() returns a copy, leaving the caller's dict untouched."""
        from _shared.response import stamp

        payload = {"results": []}
        stamp(payload, "exa", 10)

        assert "engine" not in payload

    def test_error_response_shape(self):
        """error_response() returns only engine/latency_ms/error."""
        from _shared.response import error_response

        response = error_response("perplexity", 50, "boom")

        assert response == {"engine": "perplexity", "latency_ms": 50, "error": "boom"}


class TestCallerIdentityLogging:
    """serper handler logs caller identity at entry (for audit join)."""

    def test_logs_caller_identity_line(self, capsys):
        import base64, json as _json
        def _jwt(payload):
            b = lambda o: base64.urlsafe_b64encode(_json.dumps(o).encode()).decode().rstrip("=")
            return f"{b({'alg':'RS256'})}.{b(payload)}.sig"
        os.environ["SERPER_API_KEY"] = "sk-test"
        event = {
            "input": {"query": "python"},
            "headers": {"authorization": f"Bearer {_jwt({'sub': 'user-9', 'client_id': 'web'})}"},
        }
        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.POST,
                "https://google.serper.dev/search",
                json={"organic": []},
                status=200,
            )
            from serper.handler import lambda_handler
            lambda_handler(event, None)
        logged = capsys.readouterr().out
        assert '"event": "caller_identity"' in logged
        assert '"sub": "user-9"' in logged
        os.environ.pop("SERPER_API_KEY", None)


class TestExaDomainForwarding:
    @responses.activate
    @patch("_shared.identity.get_api_key")
    def test_domains_land_in_payload(self, mock_get_api_key):
        mock_get_api_key.return_value = "test-api-key"
        responses.add(responses.POST, "https://api.exa.ai/search",
                      json={"results": []}, status=200)
        from exa.handler import lambda_handler
        event = {"query": "q", "include_domains": ["a.com"], "exclude_domains": ["b.com"]}
        lambda_handler(event, None)
        body = json.loads(responses.calls[0].request.body)
        assert body["includeDomains"] == ["a.com"]
        assert body["excludeDomains"] == ["b.com"]


class TestPerplexityDomainForwarding:
    @responses.activate
    @patch("_shared.identity.get_api_key")
    def test_domain_filter_in_payload(self, mock_get_api_key):
        mock_get_api_key.return_value = "test-api-key"
        responses.add(responses.POST, "https://api.perplexity.ai/chat/completions",
                      json={"choices": []}, status=200)
        from perplexity.handler import lambda_handler
        event = {"query": "q", "include_domains": ["a.com"], "exclude_domains": ["b.com"]}
        lambda_handler(event, None)
        body = json.loads(responses.calls[0].request.body)
        assert body["search_domain_filter"] == ["a.com", "-b.com"]


class TestFirecrawlDomainForwarding:
    @responses.activate
    @patch("_shared.identity.get_api_key")
    def test_include_wins_over_exclude(self, mock_get_api_key):
        mock_get_api_key.return_value = "test-api-key"
        responses.add(responses.POST, "https://api.firecrawl.dev/v1/search",
                      json={"data": []}, status=200)
        from firecrawl.handler import lambda_handler
        event = {"query": "q", "include_domains": ["a.com"], "exclude_domains": ["b.com"]}
        lambda_handler(event, None)
        body = json.loads(responses.calls[0].request.body)
        assert body["includeDomains"] == ["a.com"]
        assert "excludeDomains" not in body


class TestAnthropicCountryAndDomains:
    @responses.activate
    @patch("_shared.identity.get_api_key")
    def test_country_sets_user_location(self, mock_get_api_key):
        mock_get_api_key.return_value = "test-api-key"
        responses.add(responses.POST, "https://api.anthropic.com/v1/messages",
                      json={"content": []}, status=200)
        from anthropic.handler import lambda_handler
        event = {"query": "q", "country": "KR"}
        lambda_handler(event, None)
        body = json.loads(responses.calls[0].request.body)
        tool = body["tools"][0]
        assert tool["user_location"] == {"type": "approximate", "country": "KR"}

    @responses.activate
    @patch("_shared.identity.get_api_key")
    def test_include_domains_sets_allowed(self, mock_get_api_key):
        mock_get_api_key.return_value = "test-api-key"
        responses.add(responses.POST, "https://api.anthropic.com/v1/messages",
                      json={"content": []}, status=200)
        from anthropic.handler import lambda_handler
        event = {"query": "q", "include_domains": ["a.com"], "exclude_domains": ["b.com"]}
        lambda_handler(event, None)
        body = json.loads(responses.calls[0].request.body)
        tool = body["tools"][0]
        assert tool["allowed_domains"] == ["a.com"]
        assert "blocked_domains" not in tool
