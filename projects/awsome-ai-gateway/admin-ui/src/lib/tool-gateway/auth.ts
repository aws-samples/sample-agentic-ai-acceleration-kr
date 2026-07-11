/**
 * Server-side Cognito M2M (client_credentials) token provider.
 *
 * NOTE: This module reads server-only secrets and must only be imported from
 * route handlers (the `app/api/**` tree), never from client components.
 *
 * The gateway is protected by a CUSTOM_JWT authorizer that validates Cognito
 * access tokens. API routes call the gateway on the browser's behalf, so they
 * mint and cache an M2M token here. The client secret never leaves the server
 * (no NEXT_PUBLIC_ prefix).
 */

interface CachedToken {
  accessToken: string;
  // epoch ms when the token should be considered expired (with safety margin)
  expiresAt: number;
}

let cached: CachedToken | null = null;
let inflight: Promise<string> | null = null;

// Refresh this many ms before the real expiry to avoid mid-request 401s.
const EXPIRY_MARGIN_MS = 60_000;

function requireEnv(name: string): string {
  const value = process.env[name];
  if (!value) {
    throw new Error(
      `Missing required server environment variable: ${name}. ` +
        `Set it in admin-ui/.env.local (written by provision_tool_gateway.sh).`
    );
  }
  return value;
}

async function fetchToken(): Promise<string> {
  const tokenEndpoint = requireEnv('COGNITO_TOOL_TOKEN_ENDPOINT');
  const clientId = requireEnv('COGNITO_TOOL_M2M_CLIENT_ID');
  const clientSecret = requireEnv('COGNITO_TOOL_M2M_CLIENT_SECRET');
  const scope = process.env.COGNITO_TOOL_M2M_SCOPE || 'agentcore/invoke';

  const basic = Buffer.from(`${clientId}:${clientSecret}`).toString('base64');

  const body = new URLSearchParams({
    grant_type: 'client_credentials',
    scope,
  });

  const res = await fetch(tokenEndpoint, {
    method: 'POST',
    headers: {
      Authorization: `Basic ${basic}`,
      'Content-Type': 'application/x-www-form-urlencoded',
    },
    body,
    // Token endpoint responses must never be cached by the fetch layer.
    cache: 'no-store',
  });

  if (!res.ok) {
    const detail = await res.text().catch(() => '');
    throw new Error(
      `Cognito token request failed (${res.status}): ${detail.slice(0, 300)}`
    );
  }

  const json = (await res.json()) as {
    access_token?: string;
    expires_in?: number;
    error?: string;
  };

  if (!json.access_token) {
    throw new Error(`Cognito token response missing access_token: ${json.error ?? 'unknown'}`);
  }

  const expiresInMs = (json.expires_in ?? 3600) * 1000;
  cached = {
    accessToken: json.access_token,
    expiresAt: Date.now() + expiresInMs - EXPIRY_MARGIN_MS,
  };

  return json.access_token;
}

/**
 * Returns a valid M2M access token, reusing the cached one when possible and
 * de-duplicating concurrent refreshes.
 */
export async function getGatewayToken(): Promise<string> {
  if (cached && Date.now() < cached.expiresAt) {
    return cached.accessToken;
  }

  // Collapse concurrent refreshes into a single request.
  if (!inflight) {
    inflight = fetchToken().finally(() => {
      inflight = null;
    });
  }

  return inflight;
}
