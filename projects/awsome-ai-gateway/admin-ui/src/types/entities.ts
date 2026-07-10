// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import type {
  AlertLevel,
  BudgetScope,
  GroupByType,
  KeyStatus,
  OrgNodeType,
  PeriodType,
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
  target_id: string;
  scope: RateLimitScope;
  rpm: number | null; // requests per minute
  tpm: number | null; // tokens per minute
  cpm: number | null; // cost per minute (USD)
  cph: number | null; // cost per hour (USD)
}

export interface RateLimitTreeNode {
  id: string;
  label: string;
  scope: RateLimitScope;
  is_active?: boolean;
  config: RateLimitConfig | null;
  children: RateLimitTreeNode[];
  inherited_from: string | null; // node id of config source when inherited
}

// ─── Organisation Tree ────────────────────────────────────────────────────────

export interface OrgNodeMeta {
  member_count: number | null;
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

// ─── CLI Downloads ────────────────────────────────────────────────────────────

export interface CLIDownloadItem {
  os: string;
  arch: string;
  filename: string;
  download_url: string;
  version: string;
  file_size_bytes: number;
  checksum_sha256: string;
}

// ─── Dashboard KPIs ───────────────────────────────────────────────────────────

export interface DashboardKPI {
  total_usage_usd: number;
  active_keys: number;
  active_models: number;
  budget_utilization_percent: number;
}

// ─── ROI / Analytics ─────────────────────────────────────────────────────────

export interface ROIMetrics {
  cost_per_line: number;
  cost_per_commit: number;
  productivity_gain_percent: number;
  roi_ratio: number;
}

export interface TrendDataPoint {
  date: string; // ISO 8601 date (YYYY-MM-DD)
  cost_usd: number;
}

export interface ModelBreakdown {
  model_alias: string;
  cost_usd: number;
  token_count: number;
  request_count: number;
}

export interface TeamBreakdown {
  team_id: string;
  team_name: string;
  cost_usd: number;
  token_count: number;
  request_count: number;
}

export interface CostSummary {
  total_cost_usd: number;
  period: PeriodType;
  start_date: string; // ISO 8601
  end_date: string; // ISO 8601
  trend: TrendDataPoint[];
  by_model: ModelBreakdown[];
  by_team: TeamBreakdown[];
}

export interface ProductivitySummary {
  period: PeriodType;
  start_date: string;
  end_date: string;
  roi: ROIMetrics;
  commits: number;
  lines_generated: number;
  active_developers: number;
}

export interface ROIAnalyticsResponse {
  cost: CostSummary;
  productivity: ProductivitySummary;
  group_by: GroupByType;
}

// ─── Budget Allocation ────────────────────────────────────────────────────────

export interface AllocationEntry {
  target_id: string;
  target_name: string;
  target_type: BudgetScope;
  allocated_usd: number;
  used_usd: number;
  remaining_usd: number;
  alert_level: AlertLevel;
}

export interface TeamBudgetAllocation {
  team_id: string;
  team_name: string;
  total_budget_usd: number;
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

