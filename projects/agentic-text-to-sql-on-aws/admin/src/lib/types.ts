// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/** admin web 클라이언트·서버가 공유하는 도메인 타입. */

/** semantic 엔티티 타입 (§8.3 — repository VALID_ENTITY_TYPES + M4 additive `datasource`). */
export const ENTITY_TYPES = ['term', 'fewshot', 'table', 'column', 'join', 'datasource'] as const;
export type EntityType = (typeof ENTITY_TYPES)[number];

/** 큐레이션 화면에서 다루는 지식 엔티티 (datasource 는 전용 화면에서 관리). */
export const CURATION_TYPES = ['term', 'fewshot', 'join', 'table', 'column'] as const;

/** 엔티티 상태 — candidate(후보) → published(발행). */
export const STATUSES = ['candidate', 'published'] as const;
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
