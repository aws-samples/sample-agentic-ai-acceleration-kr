'use server';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * 전역 런타임 설정 서버 액션.
 *
 * 백엔드는 `require_admin` 이고 값은 `public.system_settings`(migration 0036)에
 * 저장된다. 이 레포의 다른 전역 스위치는 모두 env 전용이라 파드 롤링이 필요하다.
 */

import { revalidatePath } from 'next/cache';
import { z } from 'zod';

import { adminAPI } from '@/lib/api-client';
import { APIError, withRetry } from '@/lib/utils/retry';

import type { ActionResult } from './types';

export interface BodyLoggingSetting {
  enabled: boolean;
}

// ⚠️ 서버 액션은 브라우저 입장에서 하나의 HTTP 엔드포인트다 — 타입 시그니처는 런타임
//    보증이 아니다. 프라이버시에 영향을 주는 스위치이므로 boolean 임을 실제로 검증한다.
//    (예: 문자열 "false" 가 들어오면 truthy 라 그대로 켜진다.)
const BodyLoggingInput = z.boolean();

export async function getBodyLoggingAction(): Promise<ActionResult<BodyLoggingSetting>> {
  try {
    const data = await withRetry(() =>
      adminAPI.get<BodyLoggingSetting>('/admin/settings/body-logging')
    );
    return { success: true, data };
  } catch (err) {
    return { success: false, error: toErrorMessage(err) };
  }
}

export async function setBodyLoggingAction(
  enabled: boolean
): Promise<ActionResult<BodyLoggingSetting>> {
  try {
    const parsed = BodyLoggingInput.parse(enabled);
    // ⚠️ withRetry 로 감싸지 않는다. 이것은 **멱등이 아닌 것처럼 취급해야 하는** 쓰기다 —
    //    백엔드가 매 성공마다 `audit.audit_logs` 행을 남기므로, 응답을 못 받아 재시도하면
    //    한 번의 관리자 조작이 감사 로그에 여러 행으로 남는다. 값 자체는 멱등(upsert)이라
    //    상태가 어긋나지는 않지만, "누가 몇 번 켰나" 를 읽을 때 오답이 된다. 읽기(GET)만
    //    재시도한다.
    const data = await adminAPI.put<BodyLoggingSetting>('/admin/settings/body-logging', {
      enabled: parsed,
    });
    revalidatePath('/monitoring');
    return { success: true, data };
  } catch (err) {
    return { success: false, error: toErrorMessage(err) };
  }
}

// ─── Helpers ──────────────────────────────────────────────────────────────────

function toErrorMessage(err: unknown): string {
  if (err instanceof APIError) {
    return err.message;
  }
  if (err instanceof z.ZodError) {
    return err.issues[0]?.message ?? 'Validation error';
  }
  if (err instanceof Error) {
    return err.message;
  }
  return 'An unexpected error occurred';
}
