// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import type {
  AlertLevel,
  BudgetScope,
  KeyStatus,
  OrgNodeType,
  RateLimitScope,
  UserRole,
} from './enums';

// ─── Auth / Session ───────────────────────────────────────────────────────────

export interface AdminSession {
  user_id: string;
  email: string;
  display_name: string;
  role: UserRole;
  team_id: string | null;
  department_id: string | null;
  issued_at: string; // ISO 8601
  expires_at: string; // ISO 8601
}

// ─── Virtual Keys ─────────────────────────────────────────────────────────────

export interface VirtualKeyListItem {
  key_id: string;
  key_prefix: string;
  user_id: string;
  user_email: string | null;
  status: KeyStatus;
  created_at: string; // ISO 8601
  expires_at: string | null; // ISO 8601
  last_used_at: string | null; // ISO 8601
}

export interface VirtualKeyDetail extends VirtualKeyListItem {
  /** Full key value — only returned immediately after creation */
  key_value: string;
}

// ─── Budgets ──────────────────────────────────────────────────────────────────

export interface BudgetSummaryItem {
  target_id: string;
  target_type: BudgetScope;
  target_name: string;
  team_id?: string | null;
  is_active?: boolean;
  limit: number | null;  // null = 개인 예산 미설정 (팀 예산 적용)
  used: number;
  remaining: number | null;
  usage_pct: number | null;
  alert_level: AlertLevel;
  // 저장된 알림 임계값. ⚠️ 예전에는 서버가 이 값을 돌려주지 않아서 SetBudgetDialog 가
  // 항상 [80,90,100] 으로 초기화됐다 — 50% 를 저장한 뒤 다시 열면 저장한 값이 사라진
  // 것처럼 보였다. null = 예산 미설정, [] = "알림 없음"(유효한 설정).
  alert_thresholds?: number[] | null;
}

// ─── Models ───────────────────────────────────────────────────────────────────

export interface ModelListItem {
  alias: string;
  provider: string;
  model_id: string;
  endpoint_url: string | null;
  is_active: boolean;
  input_price_per_1k: number;
  output_price_per_1k: number;
  cache_creation_5m_price_per_1k: number;
  cache_creation_1h_price_per_1k: number;
  cache_read_price_per_1k: number;
  max_tokens: number;
  context_window: number;
  description: string | null;
  display_name: string | null;
  /**
   * 대상 AWS 계정(마이그레이션 0044) — 레지스트리 이름. null/없음 = 기본값(앱 라우팅 프로필
   * 계정, 없으면 게이트웨이 계정). 편집 다이얼로그가 이 값으로 prefill 한다 — 빠지면 계정 칸을
   * 건드리지 않은 저장도 선택을 지운다.
   */
  account_name?: string | null;
  /** 게이트웨이가 실제로 서명에 쓰는 역할(트리거가 유지하는 사본). 읽기 전용 표시용. */
  account_role_arn?: string | null;
}

// ─── AWS Accounts (migration 0044) ───────────────────────────────────────────

/** `/admin/aws-accounts` 항목 — 한 번 등록하고 모델에서 이름으로 고른다. */
export interface AwsAccount {
  name: string;
  display_name: string;
  role_arn: string;
  account_id: string;
  external_id: string | null;
  description: string | null;
  /** 이 계정을 고른 모델 alias — 그 호출이 이 계정으로 서명된다. */
  aliases: string[];
  /** 같은 역할을 앱 라우팅 프로필 계정으로도 쓰는 앱(정보용). */
  app_profiles: string[];
  created_at?: string | null;
  updated_at?: string | null;
}

/** 앱 단위(라우팅 프로필) 계정 — 모델 계정이 "기본값" 일 때 무엇인지 설명하는 데 쓴다. */
export interface AppAccount {
  client: string;
  backend: string;
  role_arn: string;
  account_id: string;
  registered_as: string | null;
}

// ─── Team Model Access ───────────────────────────────────────────────────────

export interface TeamAllowedModels {
  team_id: string;
  model_aliases: string[];
}

// ─── Auto-Downgrade ──────────────────────────────────────────────────────────

export interface DowngradeRule {
  id: string;
  from_model_alias: string;
  to_model_alias: string;
  threshold_pct: number;
  is_active: boolean;
  created_at: string;
}

export interface AutoDowngradeConfig {
  scope: string;
  scope_id: string;
  enabled: boolean;
  rules: DowngradeRule[];
}

// ─── Rate Limits ──────────────────────────────────────────────────────────────

export interface RateLimitConfig {
  // ⚠️ 이 객체의 두 절반은 JSON 타입이 다르다. admin-api 는 rpm/tpm 을 `int`, cpm/cph 를
  // pydantic `Decimal`(NUMERIC(10,4)) 로 선언하고, Decimal 은 JSON **문자열**로 직렬화된다
  // — 와이어 실측: {"rpm":60,"cpm":"0.5000"}. 그래서 cpm/cph 는 string 이다. 산술이 필요하면
  // 소비 지점에서 Number()/parseFloat. `number` 로 두면 `.toFixed()` 가 타입 검사를 통과하고
  // 런타임에 "toFixed is not a function" 으로 죽는다.
  // 고정: tests/fixtures/rate-limit-tree.json (admin-api test_rate_limit_tree.py 와 공유).
  target_id: string;
  scope: RateLimitScope; // config 의 **출처** scope. 노드 scope 와 다르면 상속된 값이다.
  rpm: number | null; // requests per minute (JSON number)
  tpm: number | null; // tokens per minute (JSON number)
  cpm: string | null; // cost per minute (USD) — JSON string (Decimal)
  cph: string | null; // cost per hour (USD) — JSON string (Decimal)
}

export interface RateLimitTreeNode {
  // GLOBAL 노드의 id 는 UUID 가 아니라 **model alias** 다(PUT /admin/rate-limits/global/{model_alias}
  // 의 URL 슬롯으로 그대로 들어간다 — `claude-sonnet-4-6[1m]` 처럼 인코딩이 필요한 문자를 품는다).
  id: string;
  label: string;
  scope: RateLimitScope;
  is_active?: boolean;
  // 실효 한도. `inherited_from != null` 이면 이 값은 **상위(TEAM)에서 빌려온 것**이지 이 노드
  // 자신의 저장된 설정이 아니다 → 폼에 프리필하면 저장 시 USER 행으로 굳는다.
  config: RateLimitConfig | null;
  children: RateLimitTreeNode[];
  inherited_from: string | null; // node id of config source when inherited
}

// ─── Organisation Tree ────────────────────────────────────────────────────────

/**
 * 조직 트리 검색창의 사용자 결과 1건.
 *
 * `UserResponse` 와 별개인 이유: 검색은 트리에서 노드를 찾아 선택하는 용도라
 * created_at/is_active 가 불필요하고, 반대로 `team_id` 가 **반드시** 필요하다 —
 * 팀 멤버는 트리에서 lazy-load 되므로 조상 경로를 펼치려면 팀 id 를 알아야 한다.
 */
export interface UserSearchItem {
  id: string;
  email: string;
  display_name: string;
  role: UserRole;
  team_id: string | null;
  team_name: string | null;
}

export interface OrgNodeMeta {
  /** 항상 **사람 수**. 노드 타입과 무관하다(admin-api OrgNodeMeta 주석 참조). */
  member_count: number | null;
  /** 하위 팀 수. DEPARTMENT / ORGANIZATION 만 채워지고 TEAM·USER 는 null. */
  team_count: number | null;
  leader_name: string | null;
  email: string | null;
  role: UserRole | null;
  team_name: string | null;
}

export interface OrgTreeNode {
  id: string;
  name: string;
  type: OrgNodeType;
  children: OrgTreeNode[];
  meta: OrgNodeMeta;
}

// ─── Dashboard KPIs ───────────────────────────────────────────────────────────

export interface DashboardKPI {
  total_usage_usd: number;
  active_keys: number;
  active_models: number;
  budget_utilization_percent: number;
}

// ─── Budget Allocation ────────────────────────────────────────────────────────

export interface AllocationEntry {
  target_id: string;
  target_name: string;
  target_type: BudgetScope;
  // ⚠️ pydantic Decimal 은 JSON **문자열**("60.0000")로 나간다 — 숫자가 아니다. 소비 지점에서
  //    Number() 로 바꾼 뒤 산술/.toFixed() 할 것. `number` 로 선언했던 동안 TeamAllocationView 는
  //    `.toFixed is not a function` 으로 죽었고, reduce 합계는 문자열 연결("0100.0000…")이 됐다.
  //    고정: admin-api tests/integration/test_budget_wire_contract.py.
  allocated_usd: string;
  used_usd: string;
  remaining_usd: string;
  alert_level: AlertLevel;
}

export interface TeamBudgetAllocation {
  team_id: string;
  team_name: string;
  // Decimal → JSON string (AllocationEntry 와 같은 규약). 쓸 때 Number(...).
  total_budget_usd: string;
  // 첫 항목은 **팀 합계 행**(target_type="TEAM", allocated_usd = 팀 총예산)이고 그 뒤가 멤버다.
  entries: AllocationEntry[];
}

// ─── UI State ─────────────────────────────────────────────────────────────────

export interface ToastNotification {
  type: 'success' | 'error' | 'warning' | 'info';
  message: string;
  auto_dismiss_ms: number | null;
}

export interface FormFieldError {
  field: string;
  message: string;
}

