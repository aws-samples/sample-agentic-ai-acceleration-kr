// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * 환경 변수 접근 지점 (§8.5 admin-web env).
 *
 * 시크릿은 하드코딩하지 않는다 — 값은 ECS task definition(환경 변수) 또는 로컬 `.env.local`
 * 에서 주입되며, 여기서는 읽기·검증만 담당한다.
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
