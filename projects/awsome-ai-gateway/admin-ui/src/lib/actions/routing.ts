// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
'use server';

import { revalidatePath } from 'next/cache';
import { adminAPI } from '@/lib/api-client';
import { withRetry } from '@/lib/utils/retry';
import type { ActionResult } from './types';

function toErrorMessage(err: unknown): string {
  if (err && typeof err === 'object' && 'message' in err) {
    return String((err as { message: unknown }).message);
  }
  return typeof err === 'string' ? err : 'Unknown error';
}

export interface RoutingProfileItem {
  client: string; // 'claude-code' | 'cowork' | 'codex'
  web_search_enabled: boolean;
  backend: string;
  enabled: boolean;
}

// ─── list routing profiles (per-client web_search flag) ──────────────────────
export async function getRoutingProfilesAction(): Promise<
  ActionResult<{ items: RoutingProfileItem[] }>
> {
  try {
    const res = await withRetry(() =>
      adminAPI.get<{ items: RoutingProfileItem[] }>('/admin/routing-profiles'),
    );
    return { success: true, data: { items: res.items ?? [] } };
  } catch (err) {
    return { success: false, error: toErrorMessage(err) };
  }
}

// ─── toggle per-client web search ────────────────────────────────────────────
export async function setClientWebSearchAction(
  client: string,
  enabled: boolean,
): Promise<ActionResult<{ client: string; web_search_enabled: boolean }>> {
  if (!client) return { success: false, error: 'client is required' };
  try {
    const res = await withRetry(() =>
      adminAPI.put<{ client: string; web_search_enabled: boolean }>(
        `/admin/routing-profiles/${client}/web-search`,
        { enabled },
      ),
    );
    revalidatePath('/models');
    return { success: true, data: res };
  } catch (err) {
    return { success: false, error: toErrorMessage(err) };
  }
}
