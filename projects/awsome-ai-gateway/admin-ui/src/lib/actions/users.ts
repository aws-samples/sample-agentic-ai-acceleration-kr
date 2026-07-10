'use server';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.


// NOTE: 아래 action 들은 admin UI 에서 호출 지점이 제거되었습니다.
// 부서/팀/사용자 이전/팀 리더는 Cognito 그룹을 원천으로 동기화됩니다
// (Claude_<team>, Claude_<dept>_<team> 패턴 → OIDC exchange 시 자동 생성).
// admin-api 엔드포인트는 safety margin 으로 살아있지만 UI 호출은 없습니다.
// 필요시 kubectl/curl 로 직접 호출 가능 (긴급 운영 용도).

import { revalidatePath } from 'next/cache';
import { z } from 'zod';
import { adminAPI } from '@/lib/api-client';
import {
  DepartmentCreateSchema,
  TeamCreateSchema,
  UserTeamAssignSchema,
} from '@/types/api';
import { withRetry } from '@/lib/utils/retry';
import { APIError } from '@/lib/utils/retry';
import type { ActionResult } from './types';

// ─── createDepartmentAction ───────────────────────────────────────────────────

export async function createDepartmentAction(
  formData: unknown
): Promise<ActionResult<void>> {
  const parsed = DepartmentCreateSchema.safeParse(formData);

  if (!parsed.success) {
    const fieldErrors: Record<string, string> = {};
    for (const issue of parsed.error.issues) {
      const key = issue.path.join('.');
      fieldErrors[key] = issue.message;
    }
    return { success: false, error: 'Validation failed', fieldErrors };
  }

  try {
    await withRetry(() => adminAPI.post('/admin/departments', parsed.data));
    revalidatePath('/users');
    return { success: true, data: undefined };
  } catch (err) {
    return { success: false, error: toErrorMessage(err) };
  }
}

// ─── createTeamAction ─────────────────────────────────────────────────────────

export async function createTeamAction(formData: unknown): Promise<ActionResult<void>> {
  const parsed = TeamCreateSchema.safeParse(formData);

  if (!parsed.success) {
    const fieldErrors: Record<string, string> = {};
    for (const issue of parsed.error.issues) {
      const key = issue.path.join('.');
      fieldErrors[key] = issue.message;
    }
    return { success: false, error: 'Validation failed', fieldErrors };
  }

  try {
    await withRetry(() => adminAPI.post('/admin/teams', parsed.data));
    revalidatePath('/users');
    return { success: true, data: undefined };
  } catch (err) {
    return { success: false, error: toErrorMessage(err) };
  }
}

// ─── assignUserTeamAction ─────────────────────────────────────────────────────

export async function assignUserTeamAction(
  formData: unknown
): Promise<ActionResult<void>> {
  const parsed = UserTeamAssignSchema.safeParse(formData);

  if (!parsed.success) {
    const fieldErrors: Record<string, string> = {};
    for (const issue of parsed.error.issues) {
      const key = issue.path.join('.');
      fieldErrors[key] = issue.message;
    }
    return { success: false, error: 'Validation failed', fieldErrors };
  }

  try {
    await withRetry(() =>
      adminAPI.put(`/admin/users/${parsed.data.user_id}/team`, {
        team_id: parsed.data.team_id,
      })
    );
    revalidatePath('/users');
    return { success: true, data: undefined };
  } catch (err) {
    return { success: false, error: toErrorMessage(err) };
  }
}

// ─── setTeamLeaderAction ──────────────────────────────────────────────────────

export async function setTeamLeaderAction(
  userId: string,
  teamId: string
): Promise<ActionResult<void>> {
  if (!userId) {
    return { success: false, error: 'User ID is required' };
  }
  if (!teamId) {
    return { success: false, error: 'Team ID is required' };
  }

  try {
    await withRetry(() =>
      adminAPI.put(`/admin/teams/${teamId}/leader`, { user_id: userId })
    );
    revalidatePath('/users');
    return { success: true, data: undefined };
  } catch (err) {
    return { success: false, error: toErrorMessage(err) };
  }
}

// ─── forceReauthTeamAction ────────────────────────────────────────────────────
// 팀 멤버 전원의 ACTIVE VK 일괄 revoke. 오프보딩/보안 사고/즉시 정책 반영 용도.
// 사용자는 다음 호출 시 401 → Claude Code 재실행 필요 (UI 에서 명시).

export async function forceReauthTeamAction(
  teamId: string
): Promise<ActionResult<{ revoked_count: number }>> {
  if (!teamId) {
    return { success: false, error: 'Team ID is required' };
  }
  try {
    const res = await withRetry(() =>
      adminAPI.post<{ revoked_count: number }>(
        `/admin/teams/${teamId}/force-reauth`,
        {}
      )
    );
    revalidatePath('/users');
    return { success: true, data: res };
  } catch (err) {
    return { success: false, error: toErrorMessage(err) };
  }
}

// ─── syncCognitoAction ─────────────────────────────────────────────────────
// Cognito User Pool 에서 그룹/사용자를 가져와 DB 동기화.

export async function syncCognitoAction(): Promise<
  ActionResult<{
    groups_synced: number;
    users_created: number;
    users_updated: number;
    users_deactivated: number;
    errors: string[];
  }>
> {
  try {
    const res = await withRetry(() =>
      adminAPI.post<{
        groups_synced: number;
        users_created: number;
        users_updated: number;
        users_deactivated: number;
        errors: string[];
      }>('/admin/users/sync-cognito', {})
    );
    revalidatePath('/users');
    return { success: true, data: res };
  } catch (err) {
    return { success: false, error: toErrorMessage(err) };
  }
}

// ─── getUserAllowedClientsAction ──────────────────────────────────────────────

export async function getUserAllowedClientsAction(
  userId: string,
): Promise<ActionResult<{ clients: string[] }>> {
  if (!userId) return { success: false, error: 'User ID is required' };
  try {
    const res = await withRetry(() =>
      adminAPI.get<{ user_id: string; clients: string[] }>(
        `/admin/users/${userId}/allowed-clients`,
      ),
    );
    return { success: true, data: { clients: res.clients ?? [] } };
  } catch (err) {
    return { success: false, error: toErrorMessage(err) };
  }
}

// ─── setUserAllowedClientsAction ──────────────────────────────────────────────
// empty array = both allowed → DELETE (clears policy). non-empty → PUT allowlist.

export async function setUserAllowedClientsAction(
  userId: string,
  clients: string[],
): Promise<ActionResult<{ clients: string[] }>> {
  if (!userId) return { success: false, error: 'User ID is required' };
  try {
    if (clients.length === 0) {
      await withRetry(() => adminAPI.delete(`/admin/users/${userId}/allowed-clients`));
      revalidatePath('/users');
      return { success: true, data: { clients: [] } };
    }
    const res = await withRetry(() =>
      adminAPI.put<{ user_id: string; clients: string[] }>(
        `/admin/users/${userId}/allowed-clients`,
        { clients },
      ),
    );
    revalidatePath('/users');
    return { success: true, data: { clients: res.clients ?? clients } };
  } catch (err) {
    return { success: false, error: toErrorMessage(err) };
  }
}

// ─── getUserAllowedModelsAction ───────────────────────────────────────────────
// per-user model whitelist (overrides team). [] = no override → falls back to team.

export async function getUserAllowedModelsAction(
  userId: string,
): Promise<ActionResult<{ modelAliases: string[] }>> {
  if (!userId) return { success: false, error: 'User ID is required' };
  try {
    const res = await withRetry(() =>
      adminAPI.get<{ user_id: string; model_aliases: string[] }>(
        `/admin/users/${userId}/allowed-models`,
      ),
    );
    return { success: true, data: { modelAliases: res.model_aliases ?? [] } };
  } catch (err) {
    return { success: false, error: toErrorMessage(err) };
  }
}

// ─── setUserAllowedModelsAction ───────────────────────────────────────────────
// empty array = clear override → DELETE (falls back to team policy).
// non-empty = PUT whitelist (overrides team). ★ empty ≠ "deny all".

export async function setUserAllowedModelsAction(
  userId: string,
  modelAliases: string[],
): Promise<ActionResult<{ modelAliases: string[] }>> {
  if (!userId) return { success: false, error: 'User ID is required' };
  try {
    if (modelAliases.length === 0) {
      await withRetry(() => adminAPI.delete(`/admin/users/${userId}/allowed-models`));
      revalidatePath('/users');
      return { success: true, data: { modelAliases: [] } };
    }
    const res = await withRetry(() =>
      adminAPI.put<{ user_id: string; model_aliases: string[] }>(
        `/admin/users/${userId}/allowed-models`,
        { model_aliases: modelAliases },
      ),
    );
    revalidatePath('/users');
    return { success: true, data: { modelAliases: res.model_aliases ?? modelAliases } };
  } catch (err) {
    return { success: false, error: toErrorMessage(err) };
  }
}

// ─── getUserClientBudgetsAction ───────────────────────────────────────────────
// per-app(client) 예산 현재값 조회 — UI prefill 용. Decimal 은 JSON 에서 string.

export async function getUserClientBudgetsAction(
  userId: string,
): Promise<ActionResult<{ apps: Array<{ client: string; max_budget_usd: string; policy: string }> }>> {
  if (!userId) return { success: false, error: 'User ID is required' };
  try {
    const res = await withRetry(() =>
      adminAPI.get<{ user_id: string; apps: Array<{ client: string; max_budget_usd: string; policy: string }> }>(
        `/admin/budgets/user/${userId}/apps`,
      ),
    );
    return { success: true, data: { apps: res.apps ?? [] } };
  } catch (err) {
    return { success: false, error: toErrorMessage(err) };
  }
}

// ─── setUserClientBudgetAction ────────────────────────────────────────────────

export async function setUserClientBudgetAction(
  userId: string,
  client: string,
  body: { max_budget_usd: string; policy?: string; alert_thresholds?: number[] },
): Promise<ActionResult<void>> {
  if (!userId) return { success: false, error: 'User ID is required' };
  try {
    await withRetry(() => adminAPI.put(`/admin/budgets/user/${userId}/app/${client}`, body));
    revalidatePath('/users');
    return { success: true, data: undefined };
  } catch (err) {
    return { success: false, error: toErrorMessage(err) };
  }
}

// ─── clearUserClientBudgetAction ──────────────────────────────────────────────

export async function clearUserClientBudgetAction(
  userId: string,
  client: string,
): Promise<ActionResult<void>> {
  if (!userId) return { success: false, error: 'User ID is required' };
  try {
    await withRetry(() => adminAPI.delete(`/admin/budgets/user/${userId}/app/${client}`));
    revalidatePath('/users');
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