// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * 서버 컴포넌트용 세션 — admin_jwt 와 역할 쿠키를 함께 읽는다(lib/auth.ts resolveSession).
 *
 * ⚠️ 서버 컴포넌트가 parseJWT 만 부르면 middleware 와 판정이 갈린다. middleware 는 역할 쿠키로
 *    들여보냈는데 레이아웃은 역할을 몰라 사이드바에 대시보드만 남고, 이메일 관리자는 예산 화면에서
 *    관리자 표 대신 "팀 없음" 을 본다.
 */

import { cookies } from 'next/headers';
import { resolveSession } from '@/lib/auth';
import { ROLE_COOKIE_NAME } from '@/lib/roleCookie';
import type { AdminSession } from '@/types/entities';

/** 토큰이 없거나 못 읽으면 null — 그런 요청은 middleware 가 로그인으로 보낸다. */
export async function getServerSession(): Promise<AdminSession | null> {
  const cookieStore = cookies();
  const token = cookieStore.get('admin_jwt')?.value;
  if (!token) return null;
  try {
    return await resolveSession(token, cookieStore.get(ROLE_COOKIE_NAME)?.value);
  } catch {
    return null;
  }
}
