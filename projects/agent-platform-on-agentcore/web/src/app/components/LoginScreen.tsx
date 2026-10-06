"use client";

import { useEffect, useState, type FormEvent } from "react";
import { Bot, KeyRound, LogIn } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { TooltipProvider } from "@/components/ui/tooltip";
import { ThemeToggle } from "@/app/components/ThemeToggle";
import { useAuth } from "@/providers/AuthProvider";
import type { LoginProvider } from "@/lib/oidc";

/**
 * What the screen shows is decided by /api/auth/config: the Cognito password
 * form when the deployment has a pool + app client, one SSO button per OIDC
 * provider (Microsoft Entra ID), or both. If that request fails the password
 * form is shown anyway — a config hiccup must not lock everyone out of the
 * login that worked yesterday.
 */
const FALLBACK: LoginProvider[] = [{ id: "cognito", kind: "password", label: "아이디 · 비밀번호" }];

export function LoginScreen() {
  const { login, loginWithProvider } = useAuth();
  const [providers, setProviders] = useState<LoginProvider[] | null>(null);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  // The provider id in progress ("cognito" for the form); disables the rest.
  const [submitting, setSubmitting] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const { getLoginProviders } = await import("@/lib/oidc");
        const list = await getLoginProviders();
        if (!cancelled) setProviders(list.length > 0 ? list : FALLBACK);
      } catch {
        if (!cancelled) setProviders(FALLBACK);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const passwordLogin = providers?.some((p) => p.kind === "password") ?? false;
  const ssoProviders = providers?.filter((p) => p.kind === "oidc") ?? [];

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    setError(null);
    setSubmitting("cognito");
    const result = await login(username.trim(), password);
    setSubmitting(null);
    if (!result.ok) {
      setError(result.error || "로그인에 실패했습니다.");
    }
  };

  const handleSso = async (providerId: string) => {
    setError(null);
    setSubmitting(providerId);
    try {
      await loginWithProvider(providerId, window.location.pathname + window.location.search);
      // On success the browser has left for the IdP; nothing below runs.
    } catch (e) {
      setSubmitting(null);
      setError(e instanceof Error ? e.message : "로그인을 시작하지 못했습니다.");
    }
  };

  return (
    <div className="relative flex h-screen items-center justify-center bg-background px-4">
      {/* The login screen renders outside AppShell, so it needs its own control —
          otherwise the theme is unreachable until after signing in. */}
      <TooltipProvider delayDuration={0}>
        <div className="absolute right-4 top-4">
          <ThemeToggle />
        </div>
      </TooltipProvider>

      <div className="w-full max-w-sm">
        <div className="mb-8 flex flex-col items-center text-center">
          <div className="mb-4 flex size-11 items-center justify-center rounded-lg bg-primary text-primary-foreground shadow-raised">
            <Bot className="size-5" />
          </div>
          <h1 className="display-lg">Agent Platform</h1>
          <p className="mt-1 text-sm text-muted-foreground">계속하려면 로그인하세요</p>
        </div>

        <div className="flex flex-col gap-3.5 rounded-lg border border-border bg-card p-5 shadow-md">
          {providers === null && (
            <p className="text-center text-sm text-muted-foreground">불러오는 중…</p>
          )}

          {passwordLogin && (
            <form onSubmit={handleSubmit} className="flex flex-col gap-3.5">
              <div className="flex flex-col gap-1.5">
                <Label htmlFor="username">아이디</Label>
                <Input
                  id="username"
                  autoComplete="username"
                  value={username}
                  onChange={(e) => setUsername(e.target.value)}
                  placeholder="사용자 이름 또는 이메일"
                  disabled={submitting !== null}
                  required
                />
              </div>

              <div className="flex flex-col gap-1.5">
                <Label htmlFor="password">비밀번호</Label>
                <Input
                  id="password"
                  type="password"
                  autoComplete="current-password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  placeholder="비밀번호"
                  disabled={submitting !== null}
                  required
                />
              </div>

              <Button
                type="submit"
                disabled={submitting !== null || !username || !password}
                className="mt-1"
              >
                <LogIn className="size-4" />
                {submitting === "cognito" ? "로그인 중..." : "로그인"}
              </Button>
            </form>
          )}

          {passwordLogin && ssoProviders.length > 0 && (
            <div className="flex items-center gap-3 text-xs text-muted-foreground">
              <span className="h-px flex-1 bg-border" />
              또는
              <span className="h-px flex-1 bg-border" />
            </div>
          )}

          {ssoProviders.map((provider) => (
            <Button
              key={provider.id}
              type="button"
              variant={passwordLogin ? "outline" : "default"}
              onClick={() => handleSso(provider.id)}
              disabled={submitting !== null}
              data-provider={provider.id}
            >
              <KeyRound className="size-4" />
              {submitting === provider.id ? "이동 중…" : `${provider.label}으로 로그인`}
            </Button>
          ))}

          {error && (
            <p
              role="alert"
              className="rounded-md border border-destructive/30 bg-destructive/[0.07] px-3 py-2 text-xs text-destructive"
            >
              {error}
            </p>
          )}
        </div>
      </div>
    </div>
  );
}
