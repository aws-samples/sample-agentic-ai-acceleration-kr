'use server';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.


import { revalidatePath } from 'next/cache';
import { z } from 'zod';
import { adminAPI } from '@/lib/api-client';
import { ModelCreateSchema, ModelDeactivateSchema } from '@/types/api';
import { withRetry } from '@/lib/utils/retry';
import { APIError } from '@/lib/utils/retry';
import type { ActionResult } from './types';
import type { ModelListItem } from '@/types/entities';

// ─── createModelAction ────────────────────────────────────────────────────────

export async function createModelAction(formData: unknown): Promise<ActionResult<void>> {
  const parsed = ModelCreateSchema.safeParse(formData);

  if (!parsed.success) {
    const fieldErrors: Record<string, string> = {};
    for (const issue of parsed.error.issues) {
      const key = issue.path.join('.');
      fieldErrors[key] = issue.message;
    }
    return { success: false, error: 'Validation failed', fieldErrors };
  }

  try {
    const d = parsed.data;
    const provider = d.provider.toUpperCase();
    // provider → api_format 매핑(백엔드 model.api_format enum 과 정합).
    //   BEDROCK              → BEDROCK_NATIVE
    //   BEDROCK_MANTLE       → ANTHROPIC_MESSAGES (Cowork Mantle Opus, /anthropic/v1/messages)
    //   BEDROCK_MANTLE_OPENAI→ OPENAI_RESPONSES   (Codex Mantle GPT-5.5, /openai/v1/responses)
    //   OPENMODEL/그 외       → OPENAI_COMPATIBLE  (/v1/chat/completions)
    const apiFormatByProvider: Record<string, string> = {
      BEDROCK: 'BEDROCK_NATIVE',
      BEDROCK_MANTLE: 'ANTHROPIC_MESSAGES',
      BEDROCK_MANTLE_OPENAI: 'OPENAI_RESPONSES',
      OPENMODEL: 'OPENAI_COMPATIBLE',
    };
    await withRetry(() => adminAPI.post('/admin/models', {
      alias: d.alias,
      provider,
      provider_model_id: d.model_id,
      endpoint_url: d.endpoint_url || null,
      api_format: apiFormatByProvider[provider] ?? 'OPENAI_COMPATIBLE',
      description: d.description || null,
      display_name: d.display_name || null,
      input_price_per_1k_tokens: d.input_price_per_1k,
      output_price_per_1k_tokens: d.output_price_per_1k,
      cache_creation_5m_price_per_1k_tokens: d.cache_creation_5m_price_per_1k,
      cache_creation_1h_price_per_1k_tokens: d.cache_creation_1h_price_per_1k,
      cache_read_price_per_1k_tokens: d.cache_read_price_per_1k,
    }));
    revalidatePath('/models');
    return { success: true, data: undefined };
  } catch (err) {
    return { success: false, error: toErrorMessage(err) };
  }
}

// ─── updateModelAction ────────────────────────────────────────────────────────

export async function updateModelAction(
  alias: string,
  formData: unknown
): Promise<ActionResult<void>> {
  if (!alias) {
    return { success: false, error: 'Model alias is required' };
  }

  const parsed = ModelCreateSchema.safeParse(formData);

  if (!parsed.success) {
    const fieldErrors: Record<string, string> = {};
    for (const issue of parsed.error.issues) {
      const key = issue.path.join('.');
      fieldErrors[key] = issue.message;
    }
    return { success: false, error: 'Validation failed', fieldErrors };
  }

  try {
    const d = parsed.data;
    // Update model metadata
    await withRetry(() => adminAPI.put(`/admin/models/${alias}`, {
      provider_model_id: d.model_id,
      endpoint_url: d.endpoint_url || null,
      description: d.description || null,
      display_name: d.display_name || null,
    }));
    // Update pricing
    await withRetry(() => adminAPI.put(`/admin/models/${alias}/pricing`, {
      input_price_per_1k_tokens: d.input_price_per_1k,
      output_price_per_1k_tokens: d.output_price_per_1k,
      cache_creation_5m_price_per_1k_tokens: d.cache_creation_5m_price_per_1k,
      cache_creation_1h_price_per_1k_tokens: d.cache_creation_1h_price_per_1k,
      cache_read_price_per_1k_tokens: d.cache_read_price_per_1k,
      effective_from: new Date().toISOString(),
    }));
    revalidatePath('/models');
    return { success: true, data: undefined };
  } catch (err) {
    return { success: false, error: toErrorMessage(err) };
  }
}

// ─── deactivateModelAction ────────────────────────────────────────────────────

export async function deactivateModelAction(formData: unknown): Promise<ActionResult<void>> {
  const parsed = ModelDeactivateSchema.safeParse(formData);

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
      adminAPI.patch(`/admin/models/${parsed.data.alias}/status`, {
        active: false,
      })
    );
    revalidatePath('/models');
    return { success: true, data: undefined };
  } catch (err) {
    return { success: false, error: toErrorMessage(err) };
  }
}

// ─── activateModelAction ──────────────────────────────────────────────────────

export async function activateModelAction(alias: string): Promise<ActionResult<void>> {
  if (!alias) {
    return { success: false, error: 'Model alias is required' };
  }

  try {
    await withRetry(() =>
      adminAPI.patch(`/admin/models/${alias}/status`, { active: true })
    );
    revalidatePath('/models');
    return { success: true, data: undefined };
  } catch (err) {
    return { success: false, error: toErrorMessage(err) };
  }
}

// ─── listActiveModelsAction ───────────────────────────────────────────────────
// 활성 모델 카탈로그 조회 (client 컴포넌트에서 모델 선택 UI 용). status==='ACTIVE' 만 반환.

interface AdminModelItem {
  alias: string;
  provider: string;
  provider_model_id: string;
  endpoint_url: string | null;
  status: string;
  description: string | null;
  display_name: string | null;
  current_pricing: {
    input_price_per_1k_tokens: string;
    output_price_per_1k_tokens: string;
    cache_creation_5m_price_per_1k_tokens?: string;
    cache_creation_1h_price_per_1k_tokens?: string;
    cache_read_price_per_1k_tokens?: string;
  } | null;
}

export async function listActiveModelsAction(): Promise<ActionResult<ModelListItem[]>> {
  try {
    const res = await withRetry(() =>
      adminAPI.get<{ items: AdminModelItem[] }>('/admin/models')
    );
    const models: ModelListItem[] = (res.items ?? [])
      .filter((item) => item.status === 'ACTIVE')
      .map((item) => {
        const p = item.current_pricing;
        return {
          alias: item.alias,
          provider: item.provider,
          model_id: item.provider_model_id,
          endpoint_url: item.endpoint_url ?? null,
          is_active: item.status === 'ACTIVE',
          input_price_per_1k: p ? parseFloat(p.input_price_per_1k_tokens) : 0,
          output_price_per_1k: p ? parseFloat(p.output_price_per_1k_tokens) : 0,
          cache_creation_5m_price_per_1k: p?.cache_creation_5m_price_per_1k_tokens
            ? parseFloat(p.cache_creation_5m_price_per_1k_tokens)
            : 0,
          cache_creation_1h_price_per_1k: p?.cache_creation_1h_price_per_1k_tokens
            ? parseFloat(p.cache_creation_1h_price_per_1k_tokens)
            : 0,
          cache_read_price_per_1k: p?.cache_read_price_per_1k_tokens
            ? parseFloat(p.cache_read_price_per_1k_tokens)
            : 0,
          max_tokens: 0,
          context_window: 0,
          description: item.description,
          display_name: item.display_name,
        };
      });
    return { success: true, data: models };
  } catch (err) {
    return { success: false, error: toErrorMessage(err) };
  }
}

// ─── Price sync (AWS Price List API) ───────────────────────────────────────────

export interface PriceSyncDiff {
  alias: string;
  provider_model_id: string;
  matched: boolean;
  note: string | null;
  current: {
    input_price_per_1k_tokens: string;
    output_price_per_1k_tokens: string;
    cache_creation_5m_price_per_1k_tokens?: string;
    cache_creation_1h_price_per_1k_tokens?: string;
    cache_read_price_per_1k_tokens?: string;
  } | null;
  proposed_input_per_1k: string | null;
  proposed_output_per_1k: string | null;
  proposed_cache_5m_per_1k: string | null;
  proposed_cache_1h_per_1k: string | null;
  proposed_cache_read_per_1k: string | null;
  changed: boolean;
}

export interface PriceSyncPreview {
  source: string;
  region: string;
  diffs: PriceSyncDiff[];
  matched_count: number;
  changed_count: number;
}

/** AWS Price List 단가 vs 현재가 diff 미리보기(읽기 전용). */
export async function previewPriceSyncAction(): Promise<ActionResult<PriceSyncPreview>> {
  try {
    const data = await withRetry(() =>
      adminAPI.get<PriceSyncPreview>('/admin/models/pricing/sync-preview')
    );
    return { success: true, data };
  } catch (err) {
    return { success: false, error: toErrorMessage(err) };
  }
}

/** 승인된 alias 만 AWS 단가로 적용(자동 전체적용 아님). */
export async function applyPriceSyncAction(
  aliases: string[]
): Promise<ActionResult<{ applied: string[]; skipped: string[]; errors: string[] }>> {
  if (!aliases.length) {
    return { success: false, error: '적용할 모델을 선택하세요' };
  }
  try {
    const data = await withRetry(() =>
      adminAPI.post<{ applied: string[]; skipped: string[]; errors: string[] }>(
        '/admin/models/pricing/sync-apply',
        { aliases }
      )
    );
    revalidatePath('/models');
    return { success: true, data };
  } catch (err) {
    return { success: false, error: toErrorMessage(err) };
  }
}

// ─── Team Allowed Models ─────────────────────────────────────────────────────

export async function getTeamAllowedModelsAction(
  teamId: string
): Promise<ActionResult<{ team_id: string; model_aliases: string[] }>> {
  try {
    const data = await withRetry(() =>
      adminAPI.get<{ team_id: string; model_aliases: string[] }>(
        `/admin/teams/${teamId}/allowed-models`
      )
    );
    return { success: true, data };
  } catch (err) {
    return { success: false, error: toErrorMessage(err) };
  }
}

export async function setTeamAllowedModelsAction(
  teamId: string,
  modelAliases: string[]
): Promise<ActionResult<{ team_id: string; model_aliases: string[] }>> {
  try {
    const data = await withRetry(() =>
      adminAPI.put<{ team_id: string; model_aliases: string[] }>(
        `/admin/teams/${teamId}/allowed-models`,
        { model_aliases: modelAliases }
      )
    );
    revalidatePath('/models');
    return { success: true, data };
  } catch (err) {
    return { success: false, error: toErrorMessage(err) };
  }
}

export async function clearTeamAllowedModelsAction(
  teamId: string
): Promise<ActionResult<{ team_id: string; model_aliases: string[] }>> {
  try {
    const data = await withRetry(() =>
      adminAPI.delete<{ team_id: string; model_aliases: string[] }>(
        `/admin/teams/${teamId}/allowed-models`
      )
    );
    revalidatePath('/models');
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