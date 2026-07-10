'use server';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.


import { revalidatePath } from 'next/cache';
import { z } from 'zod';
import { adminAPI } from '@/lib/api-client';
import { withRetry } from '@/lib/utils/retry';
import { APIError } from '@/lib/utils/retry';
import type { ActionResult } from './types';

// ─── revokeKeyAction ──────────────────────────────────────────────────────────

export async function revokeKeyAction(keyId: string): Promise<ActionResult<void>> {
  if (!keyId) {
    return { success: false, error: 'Key ID is required' };
  }

  try {
    await withRetry(() => adminAPI.delete(`/admin/keys/${keyId}`));
    revalidatePath('/keys');
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