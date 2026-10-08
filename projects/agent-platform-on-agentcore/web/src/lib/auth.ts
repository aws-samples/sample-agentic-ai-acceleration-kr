/**
 * Client-side auth helpers: token storage, JWT parsing, and role resolution.
 *
 * Two logins produce the same AuthTokens shape:
 *
 * - **Cognito** (password form → backend /api/auth/login). The id_token's
 *   `cognito:groups` claim determines the role shown in the UI; the *access*
 *   token authorises API calls and is verified on the backend (core/auth.py).
 * - **OIDC** (Microsoft Entra ID, Authorization Code + PKCE in the browser,
 *   lib/oidc.ts). There the *id_token* is the API bearer — it carries the
 *   groups and the client-id audience the server checks — and the role is
 *   whatever the server's admin rules say, so it is fetched from
 *   /api/auth/session rather than parsed here.
 *
 * `providerId` records which one a session came from; it decides which token
 * goes in the Authorization header and which refresh path to use. Here we only
 * read payloads for display and routing — the role check in the UI is a
 * convenience, not a control.
 */

export type Role = "admin" | "user";

/** The provider id the server uses for the Cognito password login. */
export const COGNITO_PROVIDER_ID = "cognito";

export interface AuthTokens {
  accessToken: string;
  idToken: string;
  refreshToken: string;
  /** Absolute epoch ms when the access token expires. */
  expiresAt: number;
  /** "cognito" or an OIDC provider id ("entra"). Missing = legacy Cognito session. */
  providerId?: string;
}

export interface AuthUser {
  sub?: string;
  username: string;
  email?: string;
  name?: string;
  role: Role;
  groups: string[];
  /** Which login produced this session. */
  provider?: string;
}

/** True for a session that signed in through an OIDC provider, not Cognito. */
export function isOidcSession(tokens: AuthTokens | null): boolean {
  return !!tokens?.providerId && tokens.providerId !== COGNITO_PROVIDER_ID;
}

/** The token the API expects as bearer for this session. */
export function apiToken(tokens: AuthTokens | null): string {
  if (!tokens) return "";
  return isOidcSession(tokens) ? tokens.idToken : tokens.accessToken;
}

const STORAGE_KEY = "deep-agent-auth";

/** Decode a JWT payload (no signature check). Returns null on malformed input. */
export function decodeJwtPayload(token: string): Record<string, any> | null {
  try {
    const payload = token.split(".")[1];
    if (!payload) return null;
    const base64 = payload.replace(/-/g, "+").replace(/_/g, "/");
    const json = decodeURIComponent(
      atob(base64)
        .split("")
        .map((c) => "%" + ("00" + c.charCodeAt(0).toString(16)).slice(-2))
        .join("")
    );
    return JSON.parse(json);
  } catch {
    return null;
  }
}

/**
 * Resolve the app user (role, email) from an id_token.
 *
 * Authoritative for Cognito, whose admin group is always `admin`. For an OIDC
 * session this is only the fallback when /api/auth/session is unreachable: the
 * deployment names its own admin group there, so the role here may be wrong
 * (it can only ever under-report; the server enforces the real one).
 */
export function userFromIdToken(idToken: string, providerId?: string): AuthUser | null {
  const claims = decodeJwtPayload(idToken);
  if (!claims) return null;

  const groups: string[] = Array.isArray(claims["cognito:groups"])
    ? claims["cognito:groups"]
    : Array.isArray(claims["groups"])
      ? claims["groups"]
      : [];
  const role: Role = groups.includes("admin") ? "admin" : "user";
  const email = claims["email"] || claims["preferred_username"];

  return {
    sub: typeof claims["sub"] === "string" ? claims["sub"] : "",
    username:
      claims["cognito:username"] || claims["username"] || claims["preferred_username"] || claims["sub"] || "",
    email: typeof email === "string" && email.includes("@") ? email : undefined,
    name: typeof claims["name"] === "string" ? claims["name"] : undefined,
    role,
    groups,
    provider: providerId ?? COGNITO_PROVIDER_ID,
  };
}

export function loadTokens(): AuthTokens | null {
  if (typeof window === "undefined") return null;
  const raw = localStorage.getItem(STORAGE_KEY);
  if (!raw) return null;
  try {
    return JSON.parse(raw) as AuthTokens;
  } catch {
    return null;
  }
}

export function saveTokens(tokens: AuthTokens): void {
  if (typeof window === "undefined") return;
  localStorage.setItem(STORAGE_KEY, JSON.stringify(tokens));
}

export function clearTokens(): void {
  if (typeof window === "undefined") return;
  localStorage.removeItem(STORAGE_KEY);
}

/** Build an expiresAt (epoch ms) from a Cognito expires_in (seconds). */
export function expiresAtFrom(expiresInSeconds?: number): number {
  const seconds = expiresInSeconds && expiresInSeconds > 0 ? expiresInSeconds : 3600;
  return Date.now() + seconds * 1000;
}

/** True when the token is expired or within `skewMs` of expiring. */
export function isExpiring(tokens: AuthTokens, skewMs = 60_000): boolean {
  return Date.now() >= tokens.expiresAt - skewMs;
}

/**
 * Authorization header for API calls, or {} when there is no session.
 *
 * Cognito: the access token, not the id_token — Cognito puts cognito:groups on
 * both, but the access token is the API authorisation token. OIDC: the
 * id_token, which is the one carrying groups and the audience the server checks.
 */
export function authHeaders(): Record<string, string> {
  const token = apiToken(loadTokens());
  if (!token) return {};
  return { Authorization: `Bearer ${token}` };
}

/**
 * Notified whenever the stored session changes underneath React — either
 * refreshed by `refreshSession` or cleared because the refresh failed. The
 * AuthProvider subscribes so a session that dies mid-request still lands on the
 * login screen instead of leaving the UI on a dead-token error.
 */
type SessionListener = (tokens: AuthTokens | null) => void;
const sessionListeners = new Set<SessionListener>();

export function onSessionChange(listener: SessionListener): () => void {
  sessionListeners.add(listener);
  return () => sessionListeners.delete(listener);
}

function emitSessionChange(tokens: AuthTokens | null): void {
  for (const listener of sessionListeners) listener(tokens);
}

/**
 * In-flight refresh, shared by every caller.
 *
 * A 401 rarely arrives alone: the thread list, the artifact panel and the
 * registry all revalidate together, so each would otherwise spend the refresh
 * token on its own call. Cognito hands out a new access token per call but the
 * responses race, and the losers write a stale token back to storage. Holding
 * one promise means the first 401 refreshes and the rest await that result.
 */
let inFlightRefresh: Promise<AuthTokens | null> | null = null;

/**
 * Exchange the refresh token for a new access token, or clear the session when
 * that fails. Returns the new tokens, or null when re-login is required.
 *
 * Deliberately fetches directly rather than going through ApiClient: this is
 * called *from* ApiClient's 401 handler, and routing back through it would make
 * a failing refresh recurse.
 */
export async function refreshSession(): Promise<AuthTokens | null> {
  if (inFlightRefresh) return inFlightRefresh;

  const current = loadTokens();
  if (!current?.refreshToken) {
    clearTokens();
    emitSessionChange(null);
    return null;
  }

  inFlightRefresh = (async () => {
    try {
      if (isOidcSession(current)) {
        // Refresh against the IdP's own token endpoint. Dynamic import keeps
        // auth.ts ↔ oidc.ts from being a static cycle (oidc.ts imports this).
        const { refreshTokens } = await import("@/lib/oidc");
        const next = await refreshTokens(current.refreshToken, current.providerId!);
        if (!next || !next.idToken) {
          // null = the IdP rejected the refresh token (4xx). The session is over.
          clearTokens();
          emitSessionChange(null);
          return null;
        }
        saveTokens(next);
        emitSessionChange(next);
        return next;
      }

      const apiBase = process.env.NEXT_PUBLIC_API_URL ?? "";
      const response = await fetch(`${apiBase}/api/auth/refresh`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ refresh_token: current.refreshToken }),
      });
      // 4xx means Cognito rejected the refresh token itself: the session is
      // over and no retry will change that, so clear it and let the UI fall
      // back to the login screen. A 5xx or a transport failure is transient
      // (the backend may just be restarting), so the tokens are kept and the
      // caller's retry can still succeed.
      if (response.status >= 400 && response.status < 500) {
        clearTokens();
        emitSessionChange(null);
        return null;
      }
      if (!response.ok) throw new Error(`refresh failed: ${response.status}`);

      const data = (await response.json()) as {
        success?: boolean;
        access_token?: string;
        id_token?: string;
        expires_in?: number;
      };
      if (!data.success || !data.access_token || !data.id_token) {
        clearTokens();
        emitSessionChange(null);
        return null;
      }

      // Cognito does not return a new refresh token here; keep the existing one.
      const next: AuthTokens = {
        accessToken: data.access_token,
        idToken: data.id_token,
        refreshToken: current.refreshToken,
        expiresAt: expiresAtFrom(data.expires_in),
        providerId: COGNITO_PROVIDER_ID,
      };
      saveTokens(next);
      emitSessionChange(next);
      return next;
    } catch {
      return null;
    } finally {
      inFlightRefresh = null;
    }
  })();

  return inFlightRefresh;
}
