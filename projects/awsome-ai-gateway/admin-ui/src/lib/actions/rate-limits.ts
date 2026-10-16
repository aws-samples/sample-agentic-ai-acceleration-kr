'use server';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.


import { revalidatePath } from 'next/cache';
import { z } from 'zod';
import { adminAPI } from '@/lib/api-client';
import { RateLimitSetSchema } from '@/types/api';
import { withRetry } from '@/lib/utils/retry';
import { APIError } from '@/lib/utils/retry';
import type { ActionResult } from './types';

// ─── setRateLimitAction ───────────────────────────────────────────────────────

export async function setRateLimitAction(formData: unknown): Promise<ActionResult<void>> {
  const parsed = RateLimitSetSchema.safeParse(formData);

  if (!parsed.success) {
    const fieldErrors: Record<string, string> = {};
    for (const issue of parsed.error.issues) {
      const key = issue.path.join('.');
      fieldErrors[key] = issue.message;
    }
    return { success: false, error: 'Validation failed', fieldErrors };
  }

  // GLOBAL 은 모델별이다(admin-api 0046) — 저장이 다른 모델의 한도를 해제하지 않으므로 '해제 동의'
  // (release_alias)를 보내지 않는다. 그 값을 보내면 admin-api 는 409 다.
  const { target_id, scope, ...limits } = parsed.data;

  try {
    // GLOBAL 의 target_id 는 UUID 가 아니라 model alias 다 — `claude-sonnet-4-6[1m]` 처럼 URL
    // 경로에서 인코딩이 필요한 문자가 실제로 들어 있다.
    await withRetry(() =>
      adminAPI.put(
        `/admin/rate-limits/${scope.toLowerCase()}/${encodeURIComponent(target_id)}`,
        limits,
      )
    );
    revalidatePath('/rate-limits');
    return { success: true, data: undefined };
  } catch (err) {
    // 409 = 서버 상태와 충돌(동시 수정, 또는 롤링 배포 중 이전 admin-api 의 단일 슬롯 409). 트리를 새로
    // 받아 패널이 지금 상태를 보여 주게 한다.
    if (err instanceof APIError && err.status === 409) revalidatePath('/rate-limits');
    return { success: false, error: toErrorMessage(err) };
  }
}

// ─── deleteUserRateLimitAction ────────────────────────────────────────────────

/**
 * USER 오버라이드 **제거** → 팀 설정 상속으로 복귀.
 *
 * PUT 은 전체 교체이므로 "입력을 비우고 저장" 은 전부 NULL 인 USER 행을 남긴다. 그 행은
 * 집행상 무해하지만 트리에서 상속 표식을 지워, 팀 한도가 걸려 있는데도 무제한처럼 보이게
 * 만든다. 되돌리려면 행 자체를 비활성화해야 한다(DELETE /admin/rate-limits/user/{id}).
 */
export async function deleteUserRateLimitAction(userId: string): Promise<ActionResult<void>> {
  if (!userId) return { success: false, error: 'User ID is required' };
  try {
    await withRetry(() =>
      adminAPI.delete(`/admin/rate-limits/user/${encodeURIComponent(userId)}`)
    );
    revalidatePath('/rate-limits');
    return { success: true, data: undefined };
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