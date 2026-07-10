// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

// 실시간 RPM 사용량(§60.9) 클라이언트 폴링 — RateLimitConfigPanel 에서 사용.
// gateway-proxy 가 Redis 에 적재하는 sliding-window 카운터를 /api/rate-limits/usage
// 프록시 경유로 읽는다. fail-soft: 실패 시 available:false.

export interface RateLimitUsage {
  available: boolean;
  scope?: string;
  scope_id?: string;
  window_sec: number;
  rpm_used_total: number;
  by_model: { model_alias: string; rpm_used: number }[];
  reason?: string;
}

export async function fetchRateLimitUsage(scope: string, scopeId: string): Promise<RateLimitUsage> {
  const params = new URLSearchParams({ scope, scope_id: scopeId });
  try {
    const res = await fetch(`/api/rate-limits/usage?${params}`, { cache: 'no-store' });
    const data = await res.json();
    return {
      available: !!data.available,
      scope: data.scope,
      scope_id: data.scope_id,
      window_sec: data.window_sec ?? 60,
      rpm_used_total: data.rpm_used_total ?? 0,
      by_model: Array.isArray(data.by_model) ? data.by_model : [],
      reason: data.reason,
    };
  } catch {
    return { available: false, window_sec: 60, rpm_used_total: 0, by_model: [] };
  }
}
