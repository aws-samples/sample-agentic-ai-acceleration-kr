"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { completeLogin, consumeReturnTo } from "@/lib/oidc";
import { useAuth } from "@/providers/AuthProvider";

/**
 * OIDC redirect target. Exchanges the authorization code for tokens, hands them
 * to the AuthProvider (which asks the server for the profile) and returns the
 * user to where they started. Rendered outside the login gate — see ShellGate.
 */
export default function AuthCallbackPage() {
  const router = useRouter();
  const { establishSession } = useAuth();
  const [error, setError] = useState<string | null>(null);
  const ran = useRef(false);

  useEffect(() => {
    // StrictMode mounts effects twice in dev; the code is single-use.
    if (ran.current) return;
    ran.current = true;
    (async () => {
      try {
        const tokens = await completeLogin();
        await establishSession(tokens);
        router.replace(consumeReturnTo());
      } catch (e) {
        setError(e instanceof Error ? e.message : "로그인에 실패했습니다.");
      }
    })();
  }, [establishSession, router]);

  return (
    <div className="flex h-screen items-center justify-center bg-background px-4 text-center">
      {error ? (
        <div className="flex flex-col items-center gap-3">
          <p role="alert" className="text-sm text-destructive">
            {error}
          </p>
          <a href="/" className="text-sm underline">
            다시 시도
          </a>
        </div>
      ) : (
        <p className="text-sm text-muted-foreground">로그인 처리 중…</p>
      )}
    </div>
  );
}
