"""Constants for search handlers."""

# Gateway Lambda environment and configuration
DEFAULT_AWS_REGION = "us-east-1"
DEFAULT_NUM_RESULTS = 10
MAX_NUM_RESULTS = 20
MIN_NUM_RESULTS = 1

# Search result schema fields (required and optional)
REQUIRED_RESULT_FIELDS = ["title", "url", "snippet"]
OPTIONAL_RESULT_FIELDS = ["score", "published_at", "favicon"]

# Normalized freshness values accepted on the unified tool input schema.
FRESHNESS_VALUES = ["day", "week", "month", "year"]

# HTTP timeouts (seconds)
DEFAULT_HTTP_TIMEOUT = 10
PERPLEXITY_HTTP_TIMEOUT = 30
