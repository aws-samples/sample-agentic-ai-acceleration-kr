/**
 * Browser OIDC (Authorization Code + PKCE) — provider-agnostic.
 *
 * /api/auth/config lists this deployment's login options. The password entry
 * (kind "password") is Cognito and never comes here; each kind "oidc" entry
 * (Microsoft Entra ID) is a button that calls `beginLogin(providerId)`. The
 * chosen provider id rides along in sessionStorage and on the stored tokens so
 * the callback exchange and later refreshes hit the same token endpoint.
 *
 * Pure logic (PKCE, URLs) is in oidcCore.mjs; only the browser coupling is here.
 */
import {
  authConfigUrl,
  buildAuthorizeUrl,
  parseCallbackParams,
  sha256Challenge,
  base64UrlEncode,
} from "@/lib/oidcCore.mjs";
import { type AuthTokens, expiresAtFrom } from "@/lib/auth";

export interface LoginProvider {
  id: string;
  kind: "password" | "oidc";
  label: string;
  issuer_url?: string;
  client_id?: string;
  scopes?: string;
  redirect_uri?: string;
}

interface Endpoints {
  authorization_endpoint: string;
  token_endpoint: string;
}

const VERIFIER_KEY = "oidc-code-verifier";
const STATE_KEY = "oidc-state";
const NONCE_KEY = "oidc-nonce";
const RETURN_KEY = "oidc-return-to";
const PROVIDER_KEY = "oidc-provider-id";

let providersCache: LoginProvider[] | null = null;
const endpointsCache = new Map<string, Endpoints>();

/** Every login option the server offers, in display order. */
export async function getLoginProviders(): Promise<LoginProvider[]> {
  if (providersCache) return providersCache;
  const apiBase = process.env.NEXT_PUBLIC_API_URL ?? "";
  // The page origin goes along explicitly — see authConfigUrl for why the
  // Origin header cannot be relied on here.
  const origin = typeof window === "undefined" ? undefined : window.location.origin;
  const res = await fetch(authConfigUrl(apiBase, origin));
  if (!res.ok) throw new Error("로그인 설정을 불러오지 못했습니다.");
  const data = (await res.json()) as { providers?: LoginProvider[] };
  providersCache = Array.isArray(data.providers) ? data.providers : [];
  return providersCache;
}

async function getOidcProvider(providerId: string): Promise<Required<LoginProvider>> {
  const providers = (await getLoginProviders()).filter((p) => p.kind === "oidc");
  const provider = providers.find((p) => p.id === providerId) ?? providers[0];
  if (!provider || !provider.issuer_url || !provider.client_id || !provider.redirect_uri) {
    throw new Error("사용 가능한 SSO 로그인 제공자가 없습니다.");
  }
  return provider as Required<LoginProvider>;
}

async function getEndpoints(issuer: string): Promise<Endpoints> {
  const key = issuer.replace(/\/$/, "");
  const cached = endpointsCache.get(key);
  if (cached) return cached;
  const res = await fetch(`${key}/.well-known/openid-configuration`);
  if (!res.ok) throw new Error("OIDC discovery에 실패했습니다.");
  const data = await res.json();
  const endpoints: Endpoints = {
    authorization_endpoint: data.authorization_endpoint,
    token_endpoint: data.token_endpoint,
  };
  endpointsCache.set(key, endpoints);
  return endpoints;
}

function randomString(bytes = 32): string {
  const arr = new Uint8Array(bytes);
  // getRandomValues exists on plain-HTTP origins too; only `subtle` and
  // `randomUUID` are secure-context-only.
  globalThis.crypto.getRandomValues(arr);
  return base64UrlEncode(arr);
}

/** Redirect the browser to the provider's authorize endpoint. Never returns on success. */
export async function beginLogin(providerId: string, returnTo = "/"): Promise<void> {
  const provider = await getOidcProvider(providerId);
  const endpoints = await getEndpoints(provider.issuer_url);

  const codeVerifier = randomString(48);
  const state = randomString();
  const nonce = randomString();
  const codeChallenge = await sha256Challenge(codeVerifier);

  sessionStorage.setItem(VERIFIER_KEY, codeVerifier);
  sessionStorage.setItem(STATE_KEY, state);
  sessionStorage.setItem(NONCE_KEY, nonce);
  sessionStorage.setItem(RETURN_KEY, returnTo);
  sessionStorage.setItem(PROVIDER_KEY, provider.id);

  window.location.assign(
    buildAuthorizeUrl({
      authorizationEndpoint: endpoints.authorization_endpoint,
      clientId: provider.client_id,
      redirectUri: provider.redirect_uri,
      scopes: provider.scopes || "openid profile email",
      state,
      nonce,
      codeChallenge,
    })
  );
}

function tokensFrom(
  data: {
    access_token?: string;
    id_token?: string;
    refresh_token?: string;
    expires_in?: number;
  },
  providerId: string,
  fallbackRefresh = ""
): AuthTokens {
  return {
    accessToken: data.access_token ?? "",
    idToken: data.id_token ?? "",
    refreshToken: data.refresh_token ?? fallbackRefresh,
    expiresAt: expiresAtFrom(data.expires_in),
    providerId,
  };
}

/** Exchange the authorization code on /auth/callback for tokens. */
export async function completeLogin(): Promise<AuthTokens> {
  const { code, state, error } = parseCallbackParams(window.location.search);
  if (error) throw new Error(`로그인이 취소되었거나 실패했습니다: ${error}`);
  if (!code) throw new Error("인가 코드가 없습니다.");
  if (state !== sessionStorage.getItem(STATE_KEY)) {
    throw new Error("state 불일치 — 로그인을 다시 시도해주세요.");
  }

  const providerId = sessionStorage.getItem(PROVIDER_KEY) ?? "";
  const provider = await getOidcProvider(providerId);
  const endpoints = await getEndpoints(provider.issuer_url);
  const codeVerifier = sessionStorage.getItem(VERIFIER_KEY) ?? "";

  const body = new URLSearchParams({
    grant_type: "authorization_code",
    code,
    client_id: provider.client_id,
    redirect_uri: provider.redirect_uri,
    code_verifier: codeVerifier,
  });
  const res = await fetch(endpoints.token_endpoint, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: body.toString(),
  });
  if (!res.ok) throw new Error(`토큰 교환 실패: ${res.status}`);

  sessionStorage.removeItem(VERIFIER_KEY);
  sessionStorage.removeItem(STATE_KEY);
  sessionStorage.removeItem(NONCE_KEY);
  sessionStorage.removeItem(PROVIDER_KEY);

  return tokensFrom(await res.json(), provider.id);
}

/** Where the user was before login; consumed once. */
export function consumeReturnTo(): string {
  const value = sessionStorage.getItem(RETURN_KEY) ?? "/";
  sessionStorage.removeItem(RETURN_KEY);
  return value;
}

/**
 * Refresh against the provider's token endpoint. `null` when the IdP rejected
 * the refresh token (4xx → session over); throws on 5xx/network (transient).
 */
export async function refreshTokens(
  refreshToken: string,
  providerId: string
): Promise<AuthTokens | null> {
  const provider = await getOidcProvider(providerId);
  const endpoints = await getEndpoints(provider.issuer_url);
  const body = new URLSearchParams({
    grant_type: "refresh_token",
    refresh_token: refreshToken,
    client_id: provider.client_id,
  });
  const res = await fetch(endpoints.token_endpoint, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: body.toString(),
  });
  if (res.status >= 400 && res.status < 500) return null;
  if (!res.ok) throw new Error(`refresh 실패: ${res.status}`);
  return tokensFrom(await res.json(), provider.id, refreshToken);
}
