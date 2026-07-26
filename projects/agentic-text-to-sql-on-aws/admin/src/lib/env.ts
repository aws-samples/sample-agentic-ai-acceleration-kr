// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * 환경 변수 접근 지점 (§8.5 / §9.7 admin-web env).
 *
 * 시크릿은 하드코딩하지 않는다 — 값은 ECS task definition(환경 변수) 또는 로컬 `.env.local`
 * 에서 주입되며, 여기서는 읽기·검증만 담당한다.
 *
 * M5(§9.7) 로 추가된 평가·개선 관련 env 는 **전부 optional** 이다. evaluation 스택이 아직
 * 배포되지 않은 환경에서도 헬스체크·기존 화면이 그대로 떠야 하므로 미설정 시 던지지 않고
 * 빈 문자열로 두고, 각 route 가 "미구성" 안내를 내려 화면이 graceful 하게 degrade 한다.
 */

/** 필수 env 를 읽고, 없으면 명확한 오류를 던진다(핸들러가 500 으로 변환). */
export function requiredEnv(name: string): string {
  const value = process.env[name];
  if (!value) {
    throw new Error(`환경 변수 ${name} 가 설정되지 않았습니다 (.env.example 참고)`);
  }
  return value;
}

/** AWS 리전 — 설계 확정값 us-west-2 를 기본값으로 둔다. */
export const AWS_REGION = process.env.AWS_REGION ?? 'us-west-2';

/** Gateway MCP target 이름 — 도구명 프리픽스 `<target>___<tool>` 구성용 (§8.2). */
export const ADMIN_MCP_TARGET = process.env.ADMIN_MCP_TARGET ?? 'datasource-admin-mcp';

/** AgentCore Runtime 로그 그룹 프리픽스 (트레이스 탐색기). */
export const RUNTIME_LOG_GROUP_PREFIX =
  process.env.RUNTIME_LOG_GROUP_PREFIX ?? '/aws/bedrock-agentcore/runtimes/';

/** Cedar PolicyEngine ID (read-only 조회). 미설정 시 빈 문자열 → 핸들러가 graceful 안내. */
export const POLICY_ENGINE_ID = process.env.POLICY_ENGINE_ID ?? '';

// ----------------------------------------------------------------------------
// M5 (§9.7) — 평가·개선 파이프라인. 전부 optional (미설정 시 화면에 "미구성" 안내).
// ----------------------------------------------------------------------------

/** EX(Execution Accuracy) custom evaluator ID — evaluation 스택 출력 `ExecutionEvaluatorId`. */
export const EXECUTION_EVALUATOR_ID = process.env.EXECUTION_EVALUATOR_ID ?? '';

/** OnlineEvaluationConfig ID — evaluation 스택 출력 `OnlineEvalConfigId`. */
export const ONLINE_EVAL_CONFIG_ID = process.env.ONLINE_EVAL_CONFIG_ID ?? '';

/** 활성 bundle 포인터 SSM 파라미터명 (§9.1 — 승격·롤백의 단일 원천). */
export const ACTIVE_BUNDLE_PARAM =
  process.env.ACTIVE_BUNDLE_PARAM ?? '/agentic-t2sql/active-bundle';

/** orchestrator Runtime 로그 그룹 — 배치 평가·추천의 트레이스 소스. */
export const ORCHESTRATOR_LOG_GROUP = process.env.ORCHESTRATOR_LOG_GROUP ?? '';

/** 트레이스 필터용 서비스명 (OTel service.name). */
export const ORCHESTRATOR_SERVICE_NAME = process.env.ORCHESTRATOR_SERVICE_NAME ?? '';

/** 배치 평가 실행 role ARN — `agentic-t2sql-eval-exec-role` (§9.3). */
export const EVAL_EXECUTION_ROLE_ARN = process.env.EVAL_EXECUTION_ROLE_ARN ?? '';

/** Configuration Bundle 이름 (§9.3 — admin 이 최초 생성). */
export const CONFIG_BUNDLE_NAME = 'agentic_t2sql_orchestrator';

/** bundle components 의 논리 키 (§9.1 — runtime ARN 자기참조 회피, 의도적 편차). */
export const CONFIG_BUNDLE_COMPONENT_KEY = 'orchestrator';
