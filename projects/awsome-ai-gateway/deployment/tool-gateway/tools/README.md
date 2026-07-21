# Lambda Search Handlers

Python 3.12 Lambda handlers for the WebSearch Tool Gateway. Each handler queries
its provider and returns the provider's **native response as-is**, stamped only
with `engine` and `latency_ms`. There is no common `SearchResponse` schema: the
gateway feeds the JSON to the model verbatim, so preserving each provider's own
shape keeps its richest fields intact (Perplexity's synthesized answer, Tavily's
`answer`, Exa highlights) instead of flattening everything into one schema.

## Architecture

### Shared Modules (`_shared/`)

- **identity.py** — AgentCore Identity provider for API key retrieval
  - `get_api_key(provider_name)` — Retrieves API keys using boto3 bedrock-agentcore GetResourceApiKey
  - Caches keys for the Lambda warm window
  - Raises RuntimeError if WORKLOAD_TOKEN or IDENTITY_PROVIDER_ARN is missing

- **response.py** — Response stamping utilities
  - `stamp(payload, engine, latency_ms)` — Returns the provider payload with
    `engine`/`latency_ms` added (provider shape preserved)
  - `error_response(engine, latency_ms, error)` — Uniform error envelope

- **otel.py** — Optional OpenTelemetry exporter
  - `get_tracer()` — Initializes OTEL tracer if OTEL_EXPORTER_OTLP_ENDPOINT is set
  - `create_span()` — Context manager for creating OTEL spans
  - No-op if OTEL is not configured

### Handlers

Each handler follows the same event signature:

```python
def lambda_handler(event, context):
    """
    Handles both Gateway Lambda invocation and direct testing.

    Event shape:
        {"query": str, "num_results": int, "country": str (optional)}

    Returns the provider's native JSON, stamped with metadata:
        { ...provider payload..., "engine": "<name>", "latency_ms": int }
    On failure: { "engine": "<name>", "latency_ms": int, "error": str }
    """
```

#### Serper Handler (`serper/handler.py`)

- API: `https://google.serper.dev/search`
- Requires: `WORKLOAD_TOKEN`, `IDENTITY_PROVIDER_ARN`
- Supports: `query`, `num_results`, `country` (GL parameter)

#### Exa Handler (`exa/handler.py`)

- API: `https://api.exa.ai/search`
- Requires: `WORKLOAD_TOKEN`, `IDENTITY_PROVIDER_ARN`
- Deterministic: `useAutoprompt: false`
- Supports: `query`, `num_results`

#### DuckDuckGo Handler (`duckduckgo/handler.py`)

- Library: `duckduckgo-search`
- No API key required
- Supports: `query`, `num_results`

#### Perplexity Handler (`perplexity/handler.py`)

- API: `https://api.perplexity.ai/chat/completions`
- Model: `sonar-medium-online`
- Requires: `WORKLOAD_TOKEN`, `IDENTITY_PROVIDER_ARN`
- Supports: `query`, `num_results`
- Returns: the native chat-completion response (synthesized answer + citations)

#### Anthropic Handler (`anthropic/handler.py`)

- API: `https://api.anthropic.com/v1/messages` (server-side `web_search` tool)
- Model: `claude-haiku-4-5` (override via `ANTHROPIC_MODEL`)
- Requires: `ANTHROPIC_API_KEY`
- Supports: `query`
- Returns: the native Messages response (content blocks: text + web_search_tool_result)

#### Firecrawl Handler (`firecrawl/handler.py`)

- API: `https://api.firecrawl.dev/v1/search`
- Requires: `FIRECRAWL_API_KEY`
- Supports: `query`, `num_results`

#### You.com Handler (`you/handler.py`)

- API: `https://ydc-index.io/v1/search`
- Requires: `YOU_API_KEY`
- Supports: `query`, `num_results`

> Tavily is served by the hosted Tavily MCP server target (`tavily`), not a
> Lambda handler.

## Environment Variables

**Lambda Runtime:**

- `AWS_REGION` — AWS region (us-east-1)
- `WORKLOAD_TOKEN` — AgentCore workload identity token (injected by Gateway)
- `IDENTITY_PROVIDER_ARN` — Credential provider ARN (from Terraform module)
- `OTEL_EXPORTER_OTLP_ENDPOINT` — Optional OTEL endpoint for tracing

## Testing

### Unit Tests

Run all tests with coverage:

```bash
pytest tests/ --cov=.
```

Test specific handler:

```bash
pytest tests/test_handlers.py::TestSerperHandler -v
```

### Mocked HTTP

Tests use the `responses` library to mock HTTP calls without hitting real APIs:

```python
@responses.activate
@patch("_shared.identity.get_api_key")
def test_success(self, mock_get_api_key):
    mock_get_api_key.return_value = "test-api-key"
    responses.add(responses.POST, "https://...", json={...}, status=200)
    # Test handler
```

### Coverage

- Handler success path
- Malformed input (missing query, invalid num_results)
- Upstream errors (5xx responses)
- Missing API keys
- Metadata stamping (`engine`/`latency_ms` added to the native payload)

## Development

### Setup

```bash
# Install dependencies
pip install -r requirements-dev.txt

# Or with Poetry
poetry install
```

### Code Quality

Format with Black:

```bash
black . --line-length 100
```

Lint with Ruff:

```bash
ruff check .
```

### Adding a New Handler

1. Create `<engine>/handler.py` with `lambda_handler(event, context)` function
2. Import shared utilities: `from _shared.identity import get_api_key`
3. Handle both Gateway event shape and direct invocation
4. Return `stamp(provider_payload, engine, latency_ms)` (preserve the provider's native shape)
5. Add unit tests in `tests/test_handlers.py`
6. Update `requirements.txt` if new dependencies

## Gateway Event Contract

**Input Schema** (passed by Gateway):

The tool is named `web_search` and accepts the following properties. Each engine advertises only the properties it supports (capability-gated schema, generated per-engine in the Terraform gateway module):

**Property Vocabulary:**
- `query` (string, required) — Search query; always present
- `num_results` (integer 1-20, optional) — Number of results to return; omitted for perplexity and anthropic
- `country` (ISO 3166-1 alpha-2 string, optional) — Filter results by country
- `freshness` (string: `day|week|month|year`, optional) — Filter results by recency; omitted for anthropic
- `include_domains` (array of string, optional) — Include results from specific domains; advertised for exa, perplexity, anthropic, firecrawl
- `exclude_domains` (array of string, optional) — Exclude results from specific domains; advertised for exa, perplexity, anthropic, firecrawl

**Example input** (with all optional fields; engines advertise only supported properties):

```json
{
  "query": "search query string",
  "num_results": 10,
  "country": "US",
  "freshness": "week",
  "include_domains": ["example.com"],
  "exclude_domains": ["spam.com"]
}
```

**Output** — the provider's native JSON, stamped with `engine` and `latency_ms`.
There is no shared output schema: each engine returns its own shape. For example
Serper returns its `organic`/`knowledgeGraph` fields, Perplexity returns the
chat-completion response with the synthesized `answer` and `citations`, DuckDuckGo
returns `{"results": [{title, href, body}, ...]}`. Only the two metadata keys are
guaranteed:

```json
{
  "...": "provider's native payload, preserved verbatim",
  "engine": "serper",
  "latency_ms": 234
}
```

## Error Handling

All handlers gracefully handle errors:

- Missing required parameters → Return empty results with error message
- API errors (5xx) → Return empty results with error message
- Missing API key → Return empty results with error message
- Timeout → Return empty results with error message

Error responses still include `engine`, `latency_ms`, and `error` field for observability.

## Deployment

Handlers are deployed as Lambda functions via Terraform (`infra/modules/gateway-lambda-tool`). Each handler is packaged separately with its own requirements.

**Lambda Configuration:**

- Runtime: Python 3.12
- Timeout: 30 seconds (adjust for slower APIs)
- Memory: 256 MB
- Layers: Shared modules and dependencies

## Observability

### CloudWatch Logs

Each handler logs to CloudWatch with structured format:

```
[engine] [status] [latency_ms]
```

### OTEL Tracing

If `OTEL_EXPORTER_OTLP_ENDPOINT` is configured, handlers emit spans:

- `get_<engine>_api_key` — API key retrieval
- `query_<engine>` — API query execution
