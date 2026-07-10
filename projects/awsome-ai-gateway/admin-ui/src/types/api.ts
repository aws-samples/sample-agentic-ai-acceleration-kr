// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { z } from 'zod';
import type { GroupByType, PeriodType, RateLimitScope, UserRole } from './enums';

// ─── Generic Response Wrappers ────────────────────────────────────────────────

export interface PaginatedResponse<T> {
  items: T[];
  total: number;
  page: number;
  page_size: number;
  total_pages: number;
}

export interface APIError {
  status_code: number;
  error_code: string;
  message: string;
  details: unknown;
}

// ─── Virtual Keys ─────────────────────────────────────────────────────────────

export interface VirtualKeyCreateForm {
  user_id: string;
  expires_at: string | null; // ISO 8601 or null for no expiry
}

// ─── Budgets ──────────────────────────────────────────────────────────────────

export interface BudgetSetForm {
  target_id: string;
  target_type: 'TEAM' | 'USER';
  max_budget_usd: number;
  policy: 'HARD_BLOCK' | 'SOFT_WARNING' | 'THROTTLE';
  alert_thresholds: number[];
}

export const BudgetSetSchema = z.object({
  target_id: z.string().min(1, 'Target ID is required'),
  target_type: z.enum(['TEAM', 'USER']),
  max_budget_usd: z.number().nonnegative('Budget must be 0 or greater'),
  policy: z.enum(['HARD_BLOCK', 'SOFT_WARNING', 'THROTTLE']),
  alert_thresholds: z.array(z.number().int().min(1).max(100)).min(1),
});

// ─── Models ───────────────────────────────────────────────────────────────────

export interface ModelCreateForm {
  alias: string;
  provider: string;
  model_id: string;
  // OPENMODEL(vLLM) 등 커스텀 엔드포인트 모델용. Bedrock/Mantle 은 비워둔다(null 전송).
  endpoint_url?: string;
  input_price_per_1k: number;
  output_price_per_1k: number;
  cache_creation_5m_price_per_1k: number;
  cache_creation_1h_price_per_1k: number;
  cache_read_price_per_1k: number;
  max_tokens: number;
  context_window: number;
  description?: string;
  display_name?: string;
}

export const ModelCreateSchema = z.object({
  alias: z.string().min(1, 'Alias is required').max(64),
  provider: z.string().min(1, 'Provider is required'),
  model_id: z.string().min(1, 'Model ID is required'),
  endpoint_url: z.string().optional(),
  input_price_per_1k: z.number().nonnegative(),
  output_price_per_1k: z.number().nonnegative(),
  cache_creation_5m_price_per_1k: z.number().nonnegative().default(0),
  // 1h 캐시쓰기 단가 — 과거 스키마 누락으로 폼 값이 strip 되어 백엔드 default 0 으로
  // 박혀 1시간 캐시 사용분 청구가 누락되던 버그 수정(deepdive Q-pricing).
  cache_creation_1h_price_per_1k: z.number().nonnegative().default(0),
  cache_read_price_per_1k: z.number().nonnegative().default(0),
  max_tokens: z.number().int().positive(),
  context_window: z.number().int().positive(),
  description: z.string().max(512).optional(),
  display_name: z.string().max(128).optional(),
});

export interface ModelDeactivateForm {
  alias: string;
}

// 즉시 비활성화만 지원(예약/유예 기능 없음). 백엔드 StatusPatchRequest 는 {active: bool} 만 받아
// 즉시 전환한다 — UI 도 이에 맞춘다.
export const ModelDeactivateSchema = z.object({
  alias: z.string().min(1),
});

// ─── Analytics ────────────────────────────────────────────────────────────────

export interface AnalyticsFilterForm {
  period: PeriodType;
  start_date?: string | null; // ISO 8601 date — required when period === 'custom'
  end_date?: string | null; // ISO 8601 date — required when period === 'custom'
  group_by: GroupByType;
  scope?: string | null;
}

export const AnalyticsFilterSchema = z
  .object({
    period: z.enum(['7d', '30d', '90d', 'custom']),
    start_date: z.string().date().nullable().optional(),
    end_date: z.string().date().nullable().optional(),
    group_by: z.enum(['model', 'team', 'user']),
    scope: z.string().nullable().optional(),
  })
  .refine(
    (data) => {
      if (data.period === 'custom') {
        return !!data.start_date && !!data.end_date;
      }
      return true;
    },
    { message: 'start_date and end_date are required for custom period', path: ['start_date'] }
  );

export interface ExportConfig {
  format: 'csv' | 'json';
  filters: AnalyticsFilterForm;
}

export const ExportConfigSchema = z.object({
  format: z.enum(['csv', 'json']),
  filters: AnalyticsFilterSchema,
});

// ─── Organisation ─────────────────────────────────────────────────────────────

export interface DepartmentCreateForm {
  name: string;
}

export const DepartmentCreateSchema = z.object({
  name: z.string().min(1, 'Department name is required').max(128),
});

export interface TeamCreateForm {
  name: string;
  department_id: string;
}

export const TeamCreateSchema = z.object({
  name: z.string().min(1, 'Team name is required').max(128),
  department_id: z.string().min(1, 'Department ID is required'),
});

export interface UserTeamAssignForm {
  user_id: string;
  team_id: string;
}

export const UserTeamAssignSchema = z.object({
  user_id: z.string().min(1, 'User ID is required'),
  team_id: z.string().min(1, 'Team ID is required'),
});

// ─── Rate Limits ──────────────────────────────────────────────────────────────

export interface RateLimitSetForm {
  target_id: string;
  scope: RateLimitScope;
  rpm?: number | null;
  tpm?: number | null;
  cpm?: number | null;
  cph?: number | null;
}

export const RateLimitSetSchema = z.object({
  target_id: z.string().min(1),
  scope: z.enum(['USER', 'TEAM', 'GLOBAL']),
  rpm: z.number().int().positive().nullable().optional(),
  tpm: z.number().int().positive().nullable().optional(),
  cpm: z.number().positive().nullable().optional(),
  cph: z.number().positive().nullable().optional(),
});

// ─── Page Permission Map ──────────────────────────────────────────────────────

/**
 * Maps URL pathname patterns to the array of UserRole values that may access them.
 * Evaluated by checkPagePermission in src/lib/auth.ts.
 */
export type PagePermissionMap = Record<string, UserRole[]>;

// ─── Inferred Zod Types ───────────────────────────────────────────────────────

export type BudgetSetInput = z.infer<typeof BudgetSetSchema>;
export type ModelCreateInput = z.infer<typeof ModelCreateSchema>;
export type ModelDeactivateInput = z.infer<typeof ModelDeactivateSchema>;
export type AnalyticsFilterInput = z.infer<typeof AnalyticsFilterSchema>;
export type ExportConfigInput = z.infer<typeof ExportConfigSchema>;
export type DepartmentCreateInput = z.infer<typeof DepartmentCreateSchema>;
export type TeamCreateInput = z.infer<typeof TeamCreateSchema>;
export type UserTeamAssignInput = z.infer<typeof UserTeamAssignSchema>;
export type RateLimitSetInput = z.infer<typeof RateLimitSetSchema>;
