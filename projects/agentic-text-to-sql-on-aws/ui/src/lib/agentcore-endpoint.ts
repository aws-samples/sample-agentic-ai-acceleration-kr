// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * AgentCore Runtime 호출 엔드포인트/설정을 환경 변수에서 조립한다.
 * 브라우저는 이 값을 알 필요가 없다 — 서버 사이드(route handler)에서만 사용한다.
 */

/** AgentCore Runtime 세션 헤더 이름 (동일 microVM 라우팅 affinity 유지). */
export const AGENTCORE_SESSION_HEADER = 'X-Amzn-Bedrock-AgentCore-Runtime-Session-Id';

/** SigV4 서명 시 사용할 AWS 서비스 이름. */
export const AGENTCORE_SERVICE = 'bedrock-agentcore';

export interface AgentCoreConfig {
  region: string;
  runtimeArn: string;
  qualifier: string;
  /** SSE POST 대상 전체 URL. */
  invocationsUrl: string;
}

/**
 * 환경 변수를 읽어 AgentCore 설정을 만든다. 필수 값 누락 시 명확한 에러.
 * 엔드포인트 규격:
 *   https://bedrock-agentcore.{region}.amazonaws.com
 *     /runtimes/{URL-encoded ARN}/invocations?qualifier={qualifier}
 */
export function getAgentCoreConfig(): AgentCoreConfig {
  const region = process.env.AWS_REGION ?? 'us-west-2';
  const runtimeArn = process.env.AGENT_RUNTIME_ARN;
  const qualifier = process.env.AGENT_RUNTIME_QUALIFIER ?? 'DEFAULT';

  if (!runtimeArn) {
    throw new Error(
      'AGENT_RUNTIME_ARN 환경 변수가 설정되지 않았습니다. .env.local 또는 ECS task 환경에 설정하세요.'
    );
  }

  // ARN 은 ':' 와 '/' 를 포함하므로 반드시 URL 인코딩해야 한다.
  const encodedArn = encodeURIComponent(runtimeArn);
  const invocationsUrl =
    `https://${AGENTCORE_SERVICE}.${region}.amazonaws.com` +
    `/runtimes/${encodedArn}/invocations?qualifier=${encodeURIComponent(qualifier)}`;

  return { region, runtimeArn, qualifier, invocationsUrl };
}
