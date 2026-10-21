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
  description?: string;
  display_name?: string;
}

/**
 * 단가 상한 — `model.model_pricings` 의 단가 컬럼은 `NUMERIC(12,8)`(0040 이 넓혔다)이라 정수부가
 * 4자리다. admin-api `schemas/models.py` 의 `MAX_PRICE_PER_1K` 와 **같은 값**이어야 한다 — 서버는
 * 이미 `le=` 로 422 를 내지만, 여기서 막으면 그 칸 아래에 필드 단위 메시지가 붙고 왕복이 없다.
 */
export const MAX_PRICE_PER_1K = 9999.99999999;

export const ModelCreateSchema = z.object({
  // ★ 문자열 상한은 admin-api `schemas/models.py`(= DB 컬럼 폭)와 **같아야** 한다:
  //   alias VARCHAR(128) / provider_model_id VARCHAR(512). 예전 alias 는 64 였다 — 서버가
  //   받아들이는 65~128자 alias 를 콘솔이 막는 막다른 길이었고, 이 스키마는 편집 저장에도 쓰이므로
  //   그런 alias 는 단가만 고치는 저장조차 영구히 불가능했다.
  alias: z.string().min(1, 'Alias is required').max(128),
  provider: z.string().min(1, 'Provider is required'),
  model_id: z.string().min(1, 'Model ID is required').max(512),
  endpoint_url: z.string().optional(),
  input_price_per_1k: z.number().nonnegative().max(MAX_PRICE_PER_1K),
  output_price_per_1k: z.number().nonnegative().max(MAX_PRICE_PER_1K),
  cache_creation_5m_price_per_1k: z.number().nonnegative().max(MAX_PRICE_PER_1K).default(0),
  // 1h 캐시쓰기 단가 — 과거 스키마 누락으로 폼 값이 strip 되어 백엔드 default 0 으로
  // 박혀 1시간 캐시 사용분 청구가 누락되던 버그 수정(deepdive Q-pricing).
  cache_creation_1h_price_per_1k: z.number().nonnegative().max(MAX_PRICE_PER_1K).default(0),
  cache_read_price_per_1k: z.number().nonnegative().max(MAX_PRICE_PER_1K).default(0),
  // NOTE: max_tokens / context_window 를 여기에 두면 안 된다. 모델 추가/편집 폼에는 두 필드의
  // 입력란이 없고(CreateModelDialog 의 FormState 에도 없음), 백엔드 ModelCreateRequest 와
  // model.model_aliases 테이블에도 대응 필드가 없다. required 로 남겨두면 폼이 값을 보낼 방법이
  // 없어 safeParse 가 항상 {max_tokens: Required, context_window: Required} 로 실패하고,
  // 화면에는 필드 단위 안내 없이 "Validation failed" 만 떠서 모델 등록·편집이 100% 막힌다.
  // (두 필드는 ModelListItem 표시용 타입에만 존재하며 항상 0 으로 채워진다.)
  description: z.string().max(512).optional(),
  display_name: z.string().max(128).optional(),
  // 대상 AWS 계정(0044) — 레지스트리 이름. null = 기본값(백엔드에서 해제로 처리된다).
  account_name: z.string().max(64).nullable().optional(),
  // 같은 모델 id 의 다른 ACTIVE alias 에도 같은 계정을 같은 저장에서 건다(admin-api 409 의 해소).
  apply_to_siblings: z.boolean().optional(),
})
  // BEDROCK_RUNTIME_OPENAI 은 endpoint_url 이 **필수**다. 게이트웨이 어댑터가 SigV4 서명
  // 리전을 endpoint 호스트(bedrock-runtime.{region}.amazonaws.com)에서 뽑아내므로, 비어
  // 있으면 서명 자체가 불가능하고 모든 호출이 502 로 죽는다. 등록 시점에 막지 않으면
  // 화면상 정상으로 보이는 모델이 런타임에만 실패한다 — 원인 추적이 가장 어려운 형태다.
  // (Mantle/BEDROCK 계열은 기존대로 optional 이므로 이 refine 은 그들에게 영향이 없다.)
  .refine(
    (d) => d.provider.toUpperCase() !== 'BEDROCK_RUNTIME_OPENAI' || !!d.endpoint_url?.trim(),
    {
      path: ['endpoint_url'],
      message: 'Endpoint URL is required for BEDROCK_RUNTIME_OPENAI (e.g. https://bedrock-runtime.us-east-2.amazonaws.com/openai)',
    },
  )
  // 같은 이유로 host 형태도 검사한다. 오타(bedrock-runtime.us-east-2.amazonaws.com 대신
  // bedrock.us-east-2… 등)는 리전 추출을 실패시켜 ValueError 로 502 가 된다.
  .refine(
    (d) =>
      d.provider.toUpperCase() !== 'BEDROCK_RUNTIME_OPENAI' ||
      /^https:\/\/bedrock-runtime\.[a-z0-9-]+\.amazonaws\.com/i.test(d.endpoint_url?.trim() ?? ''),
    {
      path: ['endpoint_url'],
      message: 'Must be https://bedrock-runtime.{region}.amazonaws.com/openai',
    },
  )
  // provider_model_id 는 cross-region inference profile ID 여야 한다. 접두사 없는
  // openai.gpt-5.6-* 는 inferenceTypesSupported=[INFERENCE_PROFILE] 이라 호출 불가이며,
  // Bedrock 이 400 "The provided model identifier is invalid" 를 돌려준다(실측).
  .refine(
    (d) =>
      d.provider.toUpperCase() !== 'BEDROCK_RUNTIME_OPENAI' ||
      /^(us|global|eu|apac|us-gov)\./.test(d.model_id.trim()),
    {
      path: ['model_id'],
      message: 'Must be a cross-region inference profile ID (e.g. us.openai.gpt-5.6-terra)',
    },
  );

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

// ─── AWS Price List 자동연동 (fetch ≠ apply) ─────────────────────────────────────

export interface AwsPriceChange {
  field: string;
  current: string | null;
  aws: string | null;
}

export interface AwsPricePreviewItem {
  alias: string;
  provider_model_id: string;
  matched: boolean;
  aws_region_code: string | null;
  aws_endpoint: string | null;
  changes: AwsPriceChange[];
  note: string;
}

export interface AwsPricePreviewResponse {
  region_code: string;
  items: AwsPricePreviewItem[];
}

export interface AwsPriceSyncResponse {
  synced: string[];
  skipped: string[];
}


// ─── Inferred Zod Types ───────────────────────────────────────────────────────

export type BudgetSetInput = z.infer<typeof BudgetSetSchema>;
// ─── AWS Accounts (migration 0044) ───────────────────────────────────────────
// admin-api(schemas/aws_accounts.py)·DB CHECK 와 같은 규칙. 여기서 먼저 막아 필드 단위로 보여 준다.
const AWS_ACCOUNT_NAME_RE = /^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$/;
const RESERVED_AWS_ACCOUNT_NAMES = ['default', 'gateway', 'inherit', 'none'];
// admin-api(schemas/models.py)·DB CHECK·terraform 과 같은 문법: aws / aws-<소문자> 파티션만, 역할
// 경로·이름은 IAM 문자 집합만(와일드카드 없음). ExternalId 는 STS 문자 집합의 ASCII 판.
const ROLE_ARN_RE = /^arn:aws(-[a-z]+)*:iam::[0-9]{12}:role\/[A-Za-z0-9+=,.@_/-]+$/;
const EXTERNAL_ID_RE = /^[A-Za-z0-9_+=,.@:/-]{2,1224}$/;

export const AwsAccountSchema = z.object({
  name: z
    .string()
    .regex(AWS_ACCOUNT_NAME_RE, '3-64 lowercase letters, digits and hyphens')
    .refine((v) => !RESERVED_AWS_ACCOUNT_NAMES.includes(v), 'This name is reserved'),
  display_name: z.string().min(1).max(128),
  role_arn: z
    .string()
    .regex(ROLE_ARN_RE, 'Must be an IAM role ARN: arn:aws:iam::<12-digit account>:role/<name>'),
  external_id: z
    .string()
    .regex(EXTERNAL_ID_RE, '2-1224 chars of A-Z a-z 0-9 + = , . @ : / _ -')
    .nullable()
    .optional(),
  description: z.string().max(1000).nullable().optional(),
});

export type AwsAccountInput = z.infer<typeof AwsAccountSchema>;

export type ModelCreateInput = z.infer<typeof ModelCreateSchema>;
export type ModelDeactivateInput = z.infer<typeof ModelDeactivateSchema>;
export type AnalyticsFilterInput = z.infer<typeof AnalyticsFilterSchema>;
export type ExportConfigInput = z.infer<typeof ExportConfigSchema>;
export type DepartmentCreateInput = z.infer<typeof DepartmentCreateSchema>;
export type TeamCreateInput = z.infer<typeof TeamCreateSchema>;
export type UserTeamAssignInput = z.infer<typeof UserTeamAssignSchema>;
export type RateLimitSetInput = z.infer<typeof RateLimitSetSchema>;
