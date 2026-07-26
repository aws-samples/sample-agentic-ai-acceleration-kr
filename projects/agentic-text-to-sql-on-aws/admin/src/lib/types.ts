// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/** admin web 클라이언트·서버가 공유하는 도메인 타입. */

/** semantic 엔티티 타입 (§8.3 — repository VALID_ENTITY_TYPES + M4 additive `datasource`). */
export const ENTITY_TYPES = ['term', 'fewshot', 'table', 'column', 'join', 'datasource'] as const;
export type EntityType = (typeof ENTITY_TYPES)[number];

/** 큐레이션 화면에서 다루는 지식 엔티티 (datasource 는 전용 화면에서 관리). */
export const CURATION_TYPES = ['term', 'fewshot', 'join', 'table', 'column'] as const;

/**
 * 엔티티 상태 — candidate(후보) → published(발행) / rejected(반려, M5 §9.1 additive).
 * rejected 는 파생 저장소(OpenSearch·Neptune)에 노출되지 않는다(`status != published` 경로).
 */
export const STATUSES = ['candidate', 'published', 'rejected'] as const;
export type EntityStatus = (typeof STATUSES)[number];

/** entity_type 한국어 라벨. */
export const ENTITY_TYPE_LABEL: Record<string, string> = {
  term: '용어',
  fewshot: 'Few-shot 예시',
  join: '관계(join)',
  table: '테이블',
  column: '컬럼',
  datasource: '데이터 소스',
};

/** status 한국어 라벨. */
export const STATUS_LABEL: Record<string, string> = {
  candidate: '후보',
  published: '발행됨',
  rejected: '반려됨',
};

/** DynamoDB semantic 엔티티 (§8.3 list_entities 반환 항목). */
export interface SemanticEntity {
  pk: string;
  sk: string;
  entity_type: string;
  entity_id: string;
  status: string;
  version?: number;
  updated_at?: string;
  updated_by?: string;
  /** 나머지는 entity_type 별 payload 필드. */
  [key: string]: unknown;
}

/** 데이터 소스 엔진 (§8.3 register_datasource). */
export const DATASOURCE_ENGINES = ['aurora-postgresql', 'redshift-serverless'] as const;
export type DatasourceEngine = (typeof DATASOURCE_ENGINES)[number];

/** Cognito 사용자 요약 (GET /api/iam/users). */
export interface IamUser {
  username: string;
  email?: string;
  status?: string;
  enabled?: boolean;
  created_at?: string;
  groups: string[];
}

/** Cedar 정책 요약 (GET /api/cedar/policies — read-only). */
export interface CedarPolicySummary {
  policy_id: string;
  name?: string;
  description?: string;
  status?: string;
  enforcement_mode?: string;
  statement?: string;
  updated_at?: string;
}

/** 메트릭 카드 1개 (GET /api/metrics/summary). */
export interface MetricSummaryItem {
  key: string;
  label: string;
  value: number | null;
  unit?: string;
}

/** 트레이스 세션 요약 (GET /api/traces/sessions). */
export interface TraceSession {
  /** 로그 스트림 이름 = 세션 식별자 (URL 세그먼트로 인코딩해 전달). */
  id: string;
  log_group: string;
  runtime: string;
  first_event_at?: string;
  last_event_at?: string;
}

/** 트레이스 이벤트 1건 (GET /api/traces/{id}). */
export interface TraceEvent {
  timestamp: string;
  message: string;
}

/** API 공통 응답 (성공/실패 정규화). */
export interface ApiEnvelope {
  status: 'ok' | 'error';
  message?: string;
  [key: string]: unknown;
}

// ----------------------------------------------------------------------------
// M5 — 평가·개선 파이프라인 (§9.6)
// ----------------------------------------------------------------------------

/** 평가자 요약 (GET /api/eval/evaluators — builtin + custom). */
export interface EvaluatorSummaryItem {
  evaluator_id: string;
  evaluator_arn?: string;
  evaluator_name?: string;
  description?: string;
  evaluator_type?: string;
  level?: string;
  status?: string;
}

/** 평가자별 스코어 (GET /api/eval/runs/{id}). */
export interface EvaluatorScore {
  evaluator_id?: string;
  average_score?: number;
  total_evaluated?: number;
  total_failed?: number;
}

/** 배치 평가 요약 (GET /api/eval/runs). */
export interface BatchEvaluationItem {
  batch_evaluation_id: string;
  batch_evaluation_name?: string;
  status?: string;
  created_at?: string;
  updated_at?: string;
  description?: string;
  evaluator_ids: string[];
  /** 진행/완료 세션 수 요약 (evaluationResults 발췌). */
  sessions?: {
    total?: number;
    completed?: number;
    in_progress?: number;
    failed?: number;
    ignored?: number;
  };
  scores: EvaluatorScore[];
  error_details?: string[];
}

/** 개선 추천 종류 (§9.6 POST /api/recommendations). */
export const RECOMMENDATION_TYPES = ['SYSTEM_PROMPT', 'TOOL_DESCRIPTION'] as const;
export type RecommendationTypeKey = (typeof RECOMMENDATION_TYPES)[number];

export const RECOMMENDATION_TYPE_LABEL: Record<string, string> = {
  SYSTEM_PROMPT: '시스템 프롬프트',
  TOOL_DESCRIPTION: '도구 설명',
  SYSTEM_PROMPT_RECOMMENDATION: '시스템 프롬프트',
  TOOL_DESCRIPTION_RECOMMENDATION: '도구 설명',
};

/** 추천 요약 (GET /api/recommendations). */
export interface RecommendationItem {
  recommendation_id: string;
  name?: string;
  description?: string;
  type?: string;
  status?: string;
  created_at?: string;
  updated_at?: string;
}

/** 추천 상세 (GET /api/recommendations/{id}) — 결과 텍스트를 화면에 그대로 노출. */
export interface RecommendationDetail extends RecommendationItem {
  /** SYSTEM_PROMPT 추천 결과. */
  recommended_system_prompt?: string;
  /** TOOL_DESCRIPTION 추천 결과. */
  recommended_tools?: Array<{
    tool_name?: string;
    recommended_tool_description?: string;
    explanation?: string;
  }>;
  explanation?: string;
  error_code?: string;
  error_message?: string;
}

/** Configuration Bundle 요약 (GET /api/bundles). */
export interface ConfigurationBundleItem {
  bundle_id: string;
  bundle_arn?: string;
  bundle_name?: string;
  description?: string;
  created_at?: string;
}

/** Bundle 버전 요약 (GET /api/bundles/{id}/versions). */
export interface ConfigurationBundleVersionItem {
  version_id: string;
  bundle_id?: string;
  bundle_arn?: string;
  branch_name?: string;
  commit_message?: string;
  created_by?: string;
  parent_version_ids?: string[];
  version_created_at?: string;
}

/** 활성 bundle 포인터 (SSM `/agentic-t2sql/active-bundle`). */
export interface ActiveBundlePointer {
  bundleId: string;
  versionId: string;
}

/** online eval 상태 요약 (GET /api/eval/online). */
export interface OnlineEvalStatus {
  configured: boolean;
  online_evaluation_config_id?: string;
  online_evaluation_config_name?: string;
  config_status?: string;
  execution_status?: string;
  sampling_percentage?: number;
  evaluator_ids?: string[];
  log_group_names?: string[];
  output_log_group?: string;
  failure_reason?: string;
  updated_at?: string;
  note?: string;
}
