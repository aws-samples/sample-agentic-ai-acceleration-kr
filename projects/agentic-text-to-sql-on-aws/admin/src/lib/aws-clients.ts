// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * AWS SDK v3 클라이언트 싱글턴.
 *
 * 읽기 위주 관리 평면(Cognito 사용자·그룹, Cedar 조회, CloudWatch 메트릭·로그)은 MCP 도구가
 * 아니라 **admin web task role** 의 SDK 직접 호출로 처리한다. 도구 평면(semantic 쓰기)과
 * 분리되어 있으므로 Cedar 인가 대상이 아니고, 대신 task role IAM 이 최소 권한을 강제한다.
 *
 * 자격증명은 표준 provider chain — ECS 에서는 task role, 로컬은 `AWS_PROFILE` 등.
 * 클라이언트는 모듈 레벨에서 lazy 생성해 커넥션·자격증명 캐시를 재사용한다.
 */

import { BedrockAgentCoreClient } from '@aws-sdk/client-bedrock-agentcore';
import { BedrockAgentCoreControlClient } from '@aws-sdk/client-bedrock-agentcore-control';
import { CloudWatchClient } from '@aws-sdk/client-cloudwatch';
import { CloudWatchLogsClient } from '@aws-sdk/client-cloudwatch-logs';
import { CognitoIdentityProviderClient } from '@aws-sdk/client-cognito-identity-provider';
import { SSMClient } from '@aws-sdk/client-ssm';
import { AWS_REGION } from './env';

let cognito: CognitoIdentityProviderClient | null = null;
let agentcore: BedrockAgentCoreControlClient | null = null;
let agentcoreData: BedrockAgentCoreClient | null = null;
let cloudwatch: CloudWatchClient | null = null;
let logs: CloudWatchLogsClient | null = null;
let ssm: SSMClient | null = null;

export function cognitoClient(): CognitoIdentityProviderClient {
  if (!cognito) cognito = new CognitoIdentityProviderClient({ region: AWS_REGION });
  return cognito;
}

export function agentCoreControlClient(): BedrockAgentCoreControlClient {
  if (!agentcore) agentcore = new BedrockAgentCoreControlClient({ region: AWS_REGION });
  return agentcore;
}

/**
 * AgentCore **데이터플레인** 클라이언트 — StartBatchEvaluation / GetBatchEvaluation /
 * StartRecommendation / GetRecommendation. control 평면과 엔드포인트가 다르므로 별도 클라이언트다.
 */
export function agentCoreDataClient(): BedrockAgentCoreClient {
  if (!agentcoreData) agentcoreData = new BedrockAgentCoreClient({ region: AWS_REGION });
  return agentcoreData;
}

export function cloudWatchClient(): CloudWatchClient {
  if (!cloudwatch) cloudwatch = new CloudWatchClient({ region: AWS_REGION });
  return cloudwatch;
}

export function cloudWatchLogsClient(): CloudWatchLogsClient {
  if (!logs) logs = new CloudWatchLogsClient({ region: AWS_REGION });
  return logs;
}

/** SSM — 활성 bundle 포인터(`/agentic-t2sql/active-bundle`) 조회·승격. */
export function ssmClient(): SSMClient {
  if (!ssm) ssm = new SSMClient({ region: AWS_REGION });
  return ssm;
}
