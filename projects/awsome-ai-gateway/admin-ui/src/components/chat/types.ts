// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

export type ChatRole = 'user' | 'assistant' | 'tool';

export type ChartKind = 'bar' | 'line' | 'area' | 'pie' | 'table' | 'kpi' | 'image';

export interface ChartSpec {
  kind: ChartKind;
  data: Record<string, unknown>[];
  encoding: {
    x: string;
    y: string | string[];
    color?: string;
  };
  title?: string | null;
}

export interface ToolCall {
  tool: string;
  args?: Record<string, unknown>;
  result?: Record<string, unknown>;
  status?: 'running' | 'done' | 'failed';
}

export interface ValidatorResult {
  verdict: 'PASS' | 'WARN' | 'FAIL';
  reason: string;
  suggested_fix?: string | null;
  confidence?: number;
}

// L3 실행기반 후보선택 검증 메타(§58, deep 모드만). k개 후보를 실제 실행해
// 결과셋 합의로 정답을 고른 과정을 "검증됨" 카드로 노출(설명가능성=신뢰).
export interface VerificationResult {
  method: string; // "execution_self_consistency"
  k: number; // 생성·실행한 후보 수
  n_valid: number; // 실행 성공 후보 수
  agreement: number; // 다수파 크기 / 유효 후보 (0~1)
  n_clusters: number; // 서로 다른 결과셋 수
  tie: boolean; // 최대 클러스터 복수(결과 갈림)
  verdict: 'PASS' | 'WARN' | 'FAIL';
  chosen_sql?: string;
}

// L5 답변 감사 결과(§60, deep 모드만). validator(L2 SQL 의미)·verification(L3
// 실행합의)와 다른 차원 — 최종 산문의 수치가 실행 결과에서 유래했는지 회의적으로
// 재검(citation 무결성). 비파괴 advisory: 수치를 고치지 않고 카드로만 경고.
export interface AuditResult {
  verdict: 'PASS' | 'RETRY' | 'NEEDS_REVIEW';
  defects: Array<{
    type: 'A' | 'B' | 'C'; // A: uncited(근거없음) B: drift(값 어긋남) C: stale(의도 표류)
    body_excerpt?: string; // 문제된 산문 조각
    body_value?: number; // 산문이 주장한 값
    ground_values?: number[]; // 실행 결과의 가장 가까운 후보값
    suggested_fix?: string;
  }>;
  confidence: number;
  reason: string;
  model?: string; // "claude" | "gpt"
}

// 다운로드 리포트 카드(report 이벤트). s3_uri 는 클릭 시 presign 요청에 사용
// (URL 을 미리 굽지 않음 — 만료·검증 우회 방지).
export interface ReportFile {
  s3_uri: string;
  file_name: string;
  format: string; // pdf | pptx | xlsx
  summary: string;
  page_count?: number | null;
}

// heartbeat 단계(공백 없는 스트리밍 생존신호). reasoning(의미 텍스트)과 다른
// 레인 — 파이프라인 진행 타임라인. 같은 phase 재진입(SQL 재시도)은 누적 카운트.
export interface HeartbeatPhase {
  phase: string; // think | sql | analyze | validate | viz | report | work
  label: string; // 사람이 읽는 단계명("데이터 조회·SQL 생성 중")
  elapsedMs: number; // 단계 진입 시점 전체 경과
  count: number; // 같은 phase 재진입 횟수(≥1)
}

export interface ChatMessage {
  id: string;
  role: ChatRole;
  content: string;
  toolCalls?: ToolCall[];
  charts?: ChartSpec[];
  validator?: ValidatorResult;
  costUsd?: number;
  durationMs?: number;
  createdAt?: string;
  // streaming 중인지
  pending?: boolean;
  // 첫 토큰 전 "작업 중" 표시 (thinking 이벤트). 본문 도착하면 무시.
  thinkingText?: string;
  // 추론 요약 누적(orchestrator display:summarized). 침묵 구간을 메우는 연속
  // "사고 과정" 스트림. 답변(content)과 분리 — 별도 접이식 영역에 표시.
  reasoning?: string;
  // heartbeat 진행 타임라인(공백 없는 스트리밍). 단계가 하나씩 채워지며, 마지막
  // 단계의 elapsedMs 로 라이브 카운터를 보간. 본문 첫 토큰 도착 시 fade-out.
  heartbeats?: HeartbeatPhase[];
  // 마지막 heartbeat 수신 시점(클라 Date.now) — 라이브 경과 카운터 보간 기준.
  heartbeatAt?: number;
  // 다운로드 리포트 카드(report 이벤트). 다운로드 가능한 파일 링크.
  reports?: ReportFile[];
  // 턴별 후속질문 칩(§55) — 응답 끝 [SUGGESTIONS]q1|q2|q3[/SUGGESTIONS] 마커를
  // done 시점에 추출(본문에서는 제거). 마지막 assistant 메시지에서만 렌더.
  suggestions?: string[];
  // deep 모드 분석 계획(§57 PlanCard) — plan 이벤트로 수신한 구조화 계획.
  // 사용자가 [진행] 버튼 또는 수정 메시지로 응답(HITL 은 일반 대화 턴).
  plan?: AnalysisPlan;
  // L3 실행기반 후보선택 검증(§58, deep 모드만) — "검증됨" 신뢰 배지/카드.
  verifications?: VerificationResult[];
  // L5 답변 감사(§60, deep 모드만) — 최종 산문 수치 cite 무결성. validator 와 별개
  // 레이어(비파괴 advisory). PASS 면 미발행 → RETRY/NEEDS_REVIEW 일 때만 존재.
  audit?: AuditResult;
}

// deep 모드 plan-first 의 분석 계획(```plan 펜스 → plan 이벤트).
export interface AnalysisPlan {
  title?: string;
  steps: Array<{ id?: number; label: string; tool?: string }>;
}

export interface ChatSession {
  id: string;
  title: string | null;
  status: 'active' | 'expired' | 'archived';
  updatedAt: string;
  messageCount: number;
}

// SSE event types (admin-api → admin-ui)
export type StreamEvent =
  | { type: 'thinking'; text: string }
  | { type: 'heartbeat'; phase: string; label: string; elapsed_ms: number }
  | { type: 'reasoning'; chunk: string }
  | { type: 'tool_call'; tool: string; args?: Record<string, unknown> }
  | { type: 'tool_result'; tool: string; result: Record<string, unknown> }
  | { type: 'chart'; spec: ChartSpec; strip?: string }
  | { type: 'report'; s3_uri: string; file_name: string; format: string; summary: string; page_count?: number | null }
  | { type: 'text'; chunk: string }
  | { type: 'validator'; result: ValidatorResult }
  | { type: 'verification'; result: VerificationResult }
  | { type: 'audit'; result: AuditResult }
  | { type: 'error'; error: string }
  | { type: 'session_warning'; expiresInSeconds: number }
  | { type: 'done'; totalTokens?: number; costUsd?: number; durationMs?: number };
