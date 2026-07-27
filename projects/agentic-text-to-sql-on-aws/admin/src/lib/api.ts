// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * API route 공통 응답·오류 처리.
 *
 * 모든 핸들러는 `handle()` 로 감싸 AuthError → 401/403, 그 외 → 500 으로 정규화한다.
 * 응답 본문은 `{status:"ok", ...}` / `{status:"error", message}` 형태로 통일한다
 * (MCP 도구 반환 규약과 동일 — 클라이언트 분기 단순화).
 */

import { AuthError } from './auth';

export const jsonOk = (body: Record<string, unknown>, status = 200) =>
  Response.json({ status: 'ok', ...body }, { status });

export const jsonError = (message: string, status = 500) =>
  Response.json({ status: 'error', message }, { status });

/** 핸들러 실행 래퍼 — 인증 오류와 예기치 못한 예외를 HTTP 응답으로 변환. */
export async function handle(fn: () => Promise<Response>): Promise<Response> {
  try {
    return await fn();
  } catch (error) {
    if (error instanceof AuthError) {
      return jsonError(error.message, error.status);
    }
    const message = error instanceof Error ? error.message : String(error);
    // 서버 로그에는 남기고, 클라이언트에는 메시지만 노출한다.
    console.error('[admin-api] 처리 실패:', error);
    return jsonError(message, 500);
  }
}

/** 요청 본문 JSON 파싱 (비어 있거나 잘못된 JSON 은 빈 객체). */
export async function readJson(request: Request): Promise<Record<string, unknown>> {
  try {
    const parsed = await request.json();
    return parsed && typeof parsed === 'object' ? (parsed as Record<string, unknown>) : {};
  } catch {
    return {};
  }
}
