// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

'use client';

/**
 * 브라우저 측 세션·API 호출 헬퍼.
 *
 * AccessToken 은 **sessionStorage** 에 보관한다(탭 종료 시 소멸, localStorage 보다 노출 창이 짧다).
 * 모든 API 호출에 `Authorization: Bearer` 로 실어 보내며, 401 이면 세션을 비워 로그인으로 되돌린다.
 * 토큰 페이로드는 화면 분기(그룹 탭 표시)용으로만 디코드한다 — **검증은 서버가 한다**.
 */

import type { ApiEnvelope } from './types';

const TOKEN_KEY = 'agentic-t2sql-admin-access-token';

export interface SessionInfo {
  accessToken: string;
  username: string;
  groups: string[];
  isAdmin: boolean;
  /** 만료 시각(epoch 초). */
  exp?: number;
}

/** base64url JWT 페이로드 디코드 (표시 목적 — 서명 검증 아님). */
function decodePayload(token: string): Record<string, unknown> {
  try {
    const part = token.split('.')[1];
    if (!part) return {};
    const base64 = part.replace(/-/g, '+').replace(/_/g, '/');
    const json = decodeURIComponent(
      atob(base64)
        .split('')
        .map((c) => `%${c.charCodeAt(0).toString(16).padStart(2, '0')}`)
        .join('')
    );
    return JSON.parse(json) as Record<string, unknown>;
  } catch {
    return {};
  }
}

function toGroups(claim: unknown): string[] {
  if (Array.isArray(claim)) return claim.map(String);
  if (typeof claim === 'string') {
    return claim
      .replace(/^\[|\]$/g, '')
      .split(/[,\s]+/)
      .map((g) => g.trim())
      .filter(Boolean);
  }
  return [];
}

/** 토큰 → 화면용 세션 정보. 만료된 토큰은 null. */
export function toSession(accessToken: string): SessionInfo | null {
  const payload = decodePayload(accessToken);
  const exp = typeof payload.exp === 'number' ? payload.exp : undefined;
  if (exp && exp * 1000 <= Date.now()) return null;
  const groups = toGroups(payload['cognito:groups']);
  return {
    accessToken,
    username: String(payload.username ?? payload.sub ?? '알 수 없음'),
    groups,
    isAdmin: groups.includes('Admin'),
    exp,
  };
}

export function loadSession(): SessionInfo | null {
  if (typeof window === 'undefined') return null;
  const token = window.sessionStorage.getItem(TOKEN_KEY);
  if (!token) return null;
  const session = toSession(token);
  if (!session) window.sessionStorage.removeItem(TOKEN_KEY);
  return session;
}

export function saveToken(token: string): SessionInfo | null {
  if (typeof window !== 'undefined') window.sessionStorage.setItem(TOKEN_KEY, token);
  return toSession(token);
}

export function clearToken(): void {
  if (typeof window !== 'undefined') window.sessionStorage.removeItem(TOKEN_KEY);
}

/** API 호출 실패를 상태코드와 함께 표현. */
export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

/**
 * 인증된 API 호출. 실패 시 ApiError 를 던지므로 화면은 try/catch 로 메시지를 노출한다.
 * 401 이면 저장된 토큰을 지운다(만료·무효 → 재로그인 유도).
 */
export async function apiFetch<T = ApiEnvelope>(
  path: string,
  init: RequestInit & { token: string }
): Promise<T> {
  const { token, ...rest } = init;
  const headers: Record<string, string> = {
    Authorization: `Bearer ${token}`,
    ...((rest.headers as Record<string, string>) ?? {}),
  };
  if (rest.body && !headers['Content-Type']) headers['Content-Type'] = 'application/json';

  const response = await fetch(path, { ...rest, headers, cache: 'no-store' });
  let body: ApiEnvelope = { status: 'error' };
  try {
    body = (await response.json()) as ApiEnvelope;
  } catch {
    // JSON 이 아닌 응답(프록시 오류 등).
  }
  if (!response.ok) {
    if (response.status === 401) clearToken();
    throw new ApiError(response.status, body.message ?? `요청 실패 (HTTP ${response.status})`);
  }
  return body as unknown as T;
}

/** 로그인 — 토큰 발급 후 sessionStorage 에 저장. */
export async function login(username: string, password: string): Promise<SessionInfo> {
  const response = await fetch('/api/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password }),
  });
  const body = (await response.json().catch(() => ({}))) as ApiEnvelope & { access_token?: string };
  if (!response.ok || !body.access_token) {
    throw new ApiError(response.status, body.message ?? '로그인에 실패했습니다');
  }
  const session = saveToken(body.access_token);
  if (!session) throw new ApiError(500, '발급된 토큰을 해석할 수 없습니다');
  return session;
}
