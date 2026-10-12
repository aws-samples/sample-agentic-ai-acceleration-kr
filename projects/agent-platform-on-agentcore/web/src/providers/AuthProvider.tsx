"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { ApiClient } from "@/lib/api-client";
import {
  type AuthTokens,
  type AuthUser,
  type Role,
  COGNITO_PROVIDER_ID,
  apiToken,
  clearTokens,
  expiresAtFrom,
  isExpiring,
  isOidcSession,
  loadTokens,
  onSessionChange,
  refreshSession,
  saveTokens,
  userFromIdToken,
} from "@/lib/auth";

interface AuthContextValue {
  /** Current user, or null when not logged in. */
  user: AuthUser | null;
  /** True until the initial token load finishes (avoids login-flash). */
  initializing: boolean;
  role: Role | null;
  /** Cognito password login. */
  login: (
    username: string,
    password: string
  ) => Promise<{ ok: boolean; error?: string }>;
  /** Start an OIDC (Entra ID) login: redirects the browser to the provider. */
  loginWithProvider: (providerId: string, returnTo?: string) => Promise<void>;
  /** Adopt tokens from /auth/callback and resolve the profile from the server. */
  establishSession: (tokens: AuthTokens) => Promise<void>;
  logout: () => void;
}

const AuthContext = createContext<AuthContextValue | null>(null);

// Local-development auth bypass, the frontend half of the server's
// AUTH_ENFORCED=false: the server lets requests through as a local admin, so the
// UI skips the login screen and renders with a synthetic admin session instead
// of demanding Cognito credentials it has no pool to check. NEXT_PUBLIC_* is
// inlined at build time, so a production image built without it cannot be
// flipped at runtime — never set it for a deployed build.
const AUTH_DISABLED = process.env.NEXT_PUBLIC_AUTH_DISABLED === "true";
const LOCAL_DEV_USER: AuthUser = {
  username: "local",
  email: "local@dev",
  role: "admin",
  groups: ["admin"],
  teams: [],
};

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within an AuthProvider");
  return ctx;
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [tokens, setTokens] = useState<AuthTokens | null>(null);
  const [user, setUser] = useState<AuthUser | null>(
    AUTH_DISABLED ? LOCAL_DEV_USER : null
  );
  const [initializing, setInitializing] = useState(!AUTH_DISABLED);
  const refreshTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const client = useMemo(() => new ApiClient(), []);

  /**
   * Turn stored tokens into the rendered user.
   *
   * Cognito: the id_token is authoritative (admin group is always `admin`), so
   * parse it locally. OIDC: the deployment names its admin group server-side,
   * so ask /api/auth/session — parsing the id_token here would demote every
   * Entra admin on refresh. If that call fails the token parse is the fallback;
   * it can only under-report the role, and the server enforces the real one.
   */
  const establishSession = useCallback(async (next: AuthTokens) => {
    saveTokens(next);
    setTokens(next);
    if (!isOidcSession(next)) {
      setUser(userFromIdToken(next.idToken, next.providerId ?? COGNITO_PROVIDER_ID));
      return;
    }
    try {
      const apiBase = process.env.NEXT_PUBLIC_API_URL ?? "";
      const res = await fetch(`${apiBase}/api/auth/session`, {
        method: "POST",
        headers: { Authorization: `Bearer ${apiToken(next)}` },
      });
      if (res.ok) {
        const profile = (await res.json()) as {
          sub: string;
          username?: string;
          email?: string;
          name?: string;
          role: Role;
          groups: string[];
          teams?: string[];
          provider?: string;
        };
        setUser({
          sub: profile.sub,
          username: profile.email || profile.username || profile.sub,
          email: profile.email || undefined,
          name: profile.name || undefined,
          role: profile.role,
          groups: profile.groups,
          teams: profile.teams ?? [],
          provider: profile.provider,
        });
        return;
      }
    } catch {
      // fall through to the token-derived profile
    }
    setUser(userFromIdToken(next.idToken, next.providerId));
  }, []);

  const logout = useCallback(() => {
    clearTokens();
    setTokens(null);
    setUser(null);
    if (refreshTimer.current) {
      clearTimeout(refreshTimer.current);
      refreshTimer.current = null;
    }
  }, []);

  const login = useCallback(
    async (username: string, password: string) => {
      try {
        const res = await client.login({ username, password });
        if (!res.success || !res.id_token || !res.access_token) {
          return { ok: false, error: res.error || "로그인에 실패했습니다." };
        }
        await establishSession({
          accessToken: res.access_token,
          idToken: res.id_token,
          refreshToken: res.refresh_token || "",
          expiresAt: expiresAtFrom(res.expires_in),
          providerId: COGNITO_PROVIDER_ID,
        });
        return { ok: true };
      } catch (err) {
        return {
          ok: false,
          error: err instanceof Error ? err.message : "로그인에 실패했습니다.",
        };
      }
    },
    [client, establishSession]
  );

  const loginWithProvider = useCallback(async (providerId: string, returnTo = "/") => {
    const { beginLogin } = await import("@/lib/oidc");
    await beginLogin(providerId, returnTo);
  }, []);

  /**
   * Refresh via the shared single-flight helper rather than a second local
   * implementation. Two independent refresh paths would race on a shared,
   * single-use refresh token: whichever response lost would write a stale
   * access token back over the winner's. `refreshSession` also persists the
   * tokens and notifies listeners, so this only reports success.
   */
  const refresh = useCallback(async (): Promise<boolean> => {
    return (await refreshSession()) !== null;
  }, []);

  // Initial load: restore session, refreshing if the access token is stale.
  useEffect(() => {
    if (AUTH_DISABLED) return; // bypass: keep the local admin, ignore stored tokens
    const stored = loadTokens();
    if (!stored) {
      setInitializing(false);
      return;
    }
    let cancelled = false;
    (async () => {
      let next: AuthTokens | null = stored;
      if (isExpiring(stored)) {
        next = await refreshSession();
        if (!next) {
          if (!cancelled) {
            logout();
            setInitializing(false);
          }
          return;
        }
      }
      if (cancelled) return;
      await establishSession(next);
      if (!cancelled) setInitializing(false);
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Adopt refreshes that happened outside React. `authedFetch` refreshes on a
  // 401 without going through this provider, so without this the UI would keep
  // rendering as the old session — or, when the refresh fails, stay "logged in"
  // against a token the backend rejects instead of showing the login screen.
  // An OIDC refresh goes back through establishSession for the same reason as
  // the initial load: only the server knows the admin group's name.
  useEffect(() => {
    if (AUTH_DISABLED) return; // bypass: a session change must not clear the local admin
    return onSessionChange((next) => {
      if (!next) {
        setTokens(null);
        setUser(null);
        return;
      }
      void establishSession(next);
    });
  }, [establishSession]);

  // Schedule an auto-refresh 60s before expiry whenever tokens change.
  useEffect(() => {
    if (refreshTimer.current) {
      clearTimeout(refreshTimer.current);
      refreshTimer.current = null;
    }
    if (!tokens) return;

    const delay = Math.max(0, tokens.expiresAt - Date.now() - 60_000);
    refreshTimer.current = setTimeout(() => {
      refresh().then((ok) => {
        if (!ok) logout();
      });
    }, delay);

    return () => {
      if (refreshTimer.current) clearTimeout(refreshTimer.current);
    };
  }, [tokens, refresh, logout]);

  const value: AuthContextValue = {
    user,
    initializing,
    role: user?.role ?? null,
    login,
    loginWithProvider,
    establishSession,
    logout,
  };

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}
