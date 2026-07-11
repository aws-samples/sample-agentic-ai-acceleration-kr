"""API key retrieval for Lambda search tools.

Lambda tools retrieve keys from AWS Secrets Manager (primary path, via GetSecretValue
using the Lambda's own IAM role). Environment variables (e.g. SERPER_API_KEY) serve as
a fallback for backward compatibility. The AgentCore Identity vault (GetResourceApiKey)
is a further fallback when a workload token is present. This is distinct from MCP
*server* targets (Tavily/Brave), where the gateway injects credentials from the token
vault using the workload identity token.
"""

import os
from typing import Optional

_api_key_cache = {}

# Maps a provider name to the environment variable Terraform injects.
_ENV_VAR_BY_PROVIDER = {
    "serper": "SERPER_API_KEY",
    "exa": "EXA_API_KEY",
    "perplexity": "PERPLEXITY_API_KEY",
    "tavily": "TAVILY_API_KEY",
    "tavily_lambda": "TAVILY_API_KEY",
    "brave": "BRAVE_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "firecrawl": "FIRECRAWL_API_KEY",
    "you": "YOU_API_KEY",
}

# Maps a provider name to the env var holding its Secrets Manager secret ARN.
_SECRET_ARN_ENV_BY_PROVIDER = {
    "serper": "SERPER_SECRET_ARN",
    "exa": "EXA_SECRET_ARN",
    "perplexity": "PERPLEXITY_SECRET_ARN",
    "tavily": "TAVILY_SECRET_ARN",
    "tavily_lambda": "TAVILY_SECRET_ARN",
    "brave": "BRAVE_SECRET_ARN",
    "anthropic": "ANTHROPIC_SECRET_ARN",
    "firecrawl": "FIRECRAWL_SECRET_ARN",
    "you": "YOU_SECRET_ARN",
}


def _from_env(provider_name: str) -> Optional[str]:
    """Read the engine-specific API key env var, if present."""
    env_var = _ENV_VAR_BY_PROVIDER.get(provider_name)
    if env_var:
        value = os.environ.get(env_var)
        if value:
            return value
    return None


def _from_secrets_manager(provider_name: str) -> Optional[str]:
    """Primary path: fetch the key from AWS Secrets Manager.

    The Lambda's own IAM role is authorized for GetSecretValue on this ARN, so
    no workload token is required (unlike the AgentCore Identity vault path).
    The secret may be a raw string or a JSON object with an "api_key" field.
    """
    secret_arn_env = _SECRET_ARN_ENV_BY_PROVIDER.get(provider_name)
    if not secret_arn_env:
        return None
    secret_arn = os.environ.get(secret_arn_env)
    if not secret_arn:
        return None

    import boto3  # lazy import; tests patch boto3.client

    try:
        client = boto3.client(
            "secretsmanager",
            region_name=os.environ.get("AWS_REGION", "us-east-1"),
        )
        response = client.get_secret_value(SecretId=secret_arn)
    except Exception:
        # On any AWS error (AccessDenied, ResourceNotFound, throttling, etc.),
        # fall back to env var or identity provider resolution.
        return None

    raw = response.get("SecretString")
    if not raw:
        return None
    raw = raw.strip()
    if raw.startswith("{"):
        import json

        try:
            parsed = json.loads(raw)
            return parsed.get("api_key") or parsed.get(provider_name)
        except (ValueError, TypeError):
            return raw
    return raw


def _from_identity_provider(provider_name: str) -> Optional[str]:
    """Fallback: fetch the key from the AgentCore Identity token vault.

    Only usable when a workload token is available (MCP-style invocation).
    """
    workload_token = os.environ.get("WORKLOAD_TOKEN")
    identity_provider_arn = os.environ.get("IDENTITY_PROVIDER_ARN")

    if not workload_token or not identity_provider_arn:
        return None

    # Imported lazily so the primary env-var path doesn't require boto3.
    import boto3

    client = boto3.client(
        "bedrock-agentcore",
        region_name=os.environ.get("AWS_REGION", "us-east-1"),
    )
    response = client.get_resource_api_key(
        identityProviderArn=identity_provider_arn,
        workloadToken=workload_token,
        resourceIdentifier=provider_name,
    )
    return response.get("apiKey")


def get_api_key(provider_name: str) -> Optional[str]:
    """Retrieve the API key for a search provider.

    Resolution order:
      1. Cached value (per warm Lambda).
      2. AWS Secrets Manager (primary, requires *_SECRET_ARN env var).
      3. Engine-specific environment variable (fallback for Lambda targets).
      4. AgentCore Identity GetResourceApiKey (fallback, requires workload token).

    Args:
        provider_name: Provider identifier (e.g. 'serper', 'exa', 'perplexity').

    Returns:
        The API key string, or None if it cannot be resolved.

    Raises:
        RuntimeError: If the identity-provider fallback is attempted and fails.
    """
    if provider_name in _api_key_cache:
        return _api_key_cache[provider_name]

    api_key = _from_secrets_manager(provider_name)

    if not api_key:
        api_key = _from_env(provider_name)

    if not api_key:
        try:
            api_key = _from_identity_provider(provider_name)
        except Exception as e:
            raise RuntimeError(
                f"Failed to retrieve API key for {provider_name}: {str(e)}"
            )

    if api_key:
        _api_key_cache[provider_name] = api_key

    return api_key
