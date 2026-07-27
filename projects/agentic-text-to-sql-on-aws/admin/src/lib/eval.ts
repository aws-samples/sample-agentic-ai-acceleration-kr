// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * 평가·개선 파이프라인 공통 헬퍼.
 *
 * 설계 근거
 * --------
 * - **Preview API 방어**: Evaluations 는 GA 지만 Configuration Bundle·Recommendations 는
 *   Preview 다. SDK 응답 형태가 바뀌어도 화면이 죽지 않도록 route 는 여기 매퍼를 거쳐
 *   snake_case 로 정규화하고, 알 수 없는 필드는 조용히 버린다.
 * - **활성 bundle 은 SSM 포인터가 단일 원천**. 승격/롤백은 SSM PutParameter 하나로
 *   끝나고, orchestrator 는 TTL 캐시로 그 포인터를 읽는다. admin 은 A/B 트래픽 분할을
 *   제공하지 않으므로 수동 전환 폴백임을 화면에 명시한다.
 * - **로그 그룹 ARN 은 DescribeLogGroups 로 조회**한다. 계정 ID 를 env 로 받거나 STS 를
 *   추가로 호출하지 않기 위해(권한·env 표면 최소화) 이미 쓰는 logs 권한을 재사용한다.
 */

import {
  GetConfigurationBundleVersionCommand,
  type ConfigurationBundleSummary,
  type ConfigurationBundleVersionSummary,
  type EvaluatorSummary as ControlEvaluatorSummary,
} from '@aws-sdk/client-bedrock-agentcore-control';
import type {
  BatchEvaluationSummary,
  GetBatchEvaluationCommandOutput,
  GetRecommendationCommandOutput,
  RecommendationSummary,
} from '@aws-sdk/client-bedrock-agentcore';
import { DescribeLogGroupsCommand } from '@aws-sdk/client-cloudwatch-logs';
import { GetParameterCommand } from '@aws-sdk/client-ssm';
import { agentCoreControlClient, cloudWatchLogsClient, ssmClient } from './aws-clients';
import { ACTIVE_BUNDLE_PARAM, CONFIG_BUNDLE_COMPONENT_KEY, ORCHESTRATOR_LOG_GROUP } from './env';
import type {
  ActiveBundlePointer,
  BatchEvaluationItem,
  ConfigurationBundleItem,
  ConfigurationBundleVersionItem,
  EvaluatorSummaryItem,
  RecommendationDetail,
  RecommendationItem,
} from './types';

/** 조회 창(시간) 정규화 — 기본 24h, 최대 7일(168h). */
export function normalizeHours(raw: unknown, fallback = 24): number {
  const value = Number(raw);
  if (!Number.isFinite(value) || value <= 0) return fallback;
  return Math.min(Math.floor(value), 168);
}

/** 리소스 이름 규칙: 언더스코어만 사용 — 하이픈은 서비스 검증에서 거부된다. */
export function underscoreName(prefix: string, epochMs: number): string {
  return `${prefix}_${Math.floor(epochMs / 1000)}`;
}

export function isoOrUndefined(date?: Date): string | undefined {
  return date ? date.toISOString() : undefined;
}

/** ListEvaluators 항목 → 화면용 요약. */
export function mapEvaluator(summary: ControlEvaluatorSummary): EvaluatorSummaryItem {
  return {
    evaluator_id: summary.evaluatorId ?? '',
    evaluator_arn: summary.evaluatorArn,
    evaluator_name: summary.evaluatorName,
    description: summary.description,
    evaluator_type: summary.evaluatorType,
    level: summary.level,
    status: summary.status,
  };
}

/** 배치 평가(목록 요약 / 상세 응답 공용) → 화면용 항목. */
export function mapBatchEvaluation(
  source: BatchEvaluationSummary | GetBatchEvaluationCommandOutput
): BatchEvaluationItem {
  const results = source.evaluationResults;
  return {
    batch_evaluation_id: source.batchEvaluationId ?? '',
    batch_evaluation_name: source.batchEvaluationName,
    status: source.status,
    created_at: isoOrUndefined(source.createdAt),
    updated_at: isoOrUndefined(source.updatedAt),
    description: source.description,
    evaluator_ids: (source.evaluators ?? [])
      .map((e) => e.evaluatorId)
      .filter((id): id is string => Boolean(id)),
    sessions: results
      ? {
          total: results.totalNumberOfSessions,
          completed: results.numberOfSessionsCompleted,
          in_progress: results.numberOfSessionsInProgress,
          failed: results.numberOfSessionsFailed,
          ignored: results.numberOfSessionsIgnored,
        }
      : undefined,
    scores: (results?.evaluatorSummaries ?? []).map((summary) => ({
      evaluator_id: summary.evaluatorId,
      average_score: summary.statistics?.averageScore,
      total_evaluated: summary.totalEvaluated,
      total_failed: summary.totalFailed,
    })),
    error_details: source.errorDetails,
  };
}

/** ListRecommendations 항목 → 화면용 요약. */
export function mapRecommendation(summary: RecommendationSummary): RecommendationItem {
  return {
    recommendation_id: summary.recommendationId ?? '',
    name: summary.name,
    description: summary.description,
    type: summary.type,
    status: summary.status,
    created_at: isoOrUndefined(summary.createdAt),
    updated_at: isoOrUndefined(summary.updatedAt),
  };
}

/** GetRecommendation → 추천 텍스트를 화면이 바로 보여줄 수 있는 형태로 평탄화. */
export function mapRecommendationDetail(
  output: GetRecommendationCommandOutput
): RecommendationDetail {
  const detail: RecommendationDetail = {
    recommendation_id: output.recommendationId ?? '',
    name: output.name,
    description: output.description,
    type: output.type,
    status: output.status,
    created_at: isoOrUndefined(output.createdAt),
    updated_at: isoOrUndefined(output.updatedAt),
  };

  const result = output.recommendationResult;
  if (!result) return detail;

  if ('systemPromptRecommendationResult' in result && result.systemPromptRecommendationResult) {
    const prompt = result.systemPromptRecommendationResult;
    detail.recommended_system_prompt = prompt.recommendedSystemPrompt;
    detail.explanation = prompt.explanation;
    detail.error_code = prompt.errorCode;
    detail.error_message = prompt.errorMessage;
  } else if (
    'toolDescriptionRecommendationResult' in result &&
    result.toolDescriptionRecommendationResult
  ) {
    const tools = result.toolDescriptionRecommendationResult;
    detail.recommended_tools = (tools.tools ?? []).map((tool) => ({
      tool_name: tool.toolName,
      recommended_tool_description: tool.recommendedToolDescription,
      explanation: tool.explanation,
    }));
    detail.error_code = tools.errorCode;
    detail.error_message = tools.errorMessage;
  }
  return detail;
}

export function mapBundle(summary: ConfigurationBundleSummary): ConfigurationBundleItem {
  return {
    bundle_id: summary.bundleId ?? '',
    bundle_arn: summary.bundleArn,
    bundle_name: summary.bundleName,
    description: summary.description,
    created_at: isoOrUndefined(summary.createdAt),
  };
}

export function mapBundleVersion(
  summary: ConfigurationBundleVersionSummary
): ConfigurationBundleVersionItem {
  return {
    version_id: summary.versionId ?? '',
    bundle_id: summary.bundleId,
    bundle_arn: summary.bundleArn,
    branch_name: summary.lineageMetadata?.branchName,
    commit_message: summary.lineageMetadata?.commitMessage,
    created_by: summary.lineageMetadata?.createdBy?.name,
    parent_version_ids: summary.lineageMetadata?.parentVersionIds,
    version_created_at: isoOrUndefined(summary.versionCreatedAt),
  };
}

/**
 * SSM 활성 bundle 포인터를 읽는다. 미설정(ParameterNotFound)·형식 오류는 **null** —
 * 아직 승격 이력이 없는 상태이므로 오류가 아니다(orchestrator 도 코드 기본값으로 폴백).
 */
export async function readActiveBundle(): Promise<ActiveBundlePointer | null> {
  try {
    const out = await ssmClient().send(new GetParameterCommand({ Name: ACTIVE_BUNDLE_PARAM }));
    const raw = out.Parameter?.Value;
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<ActiveBundlePointer>;
    if (!parsed?.bundleId || !parsed?.versionId) return null;
    return { bundleId: parsed.bundleId, versionId: parsed.versionId };
  } catch (error) {
    console.warn('[admin-eval] 활성 bundle 포인터 조회 실패(미설정 가능):', error);
    return null;
  }
}

/** 활성 bundle 버전의 `components["orchestrator"].configuration` (논리 키). 없으면 null. */
export async function readActiveBundleConfiguration(): Promise<Record<string, unknown> | null> {
  const pointer = await readActiveBundle();
  if (!pointer) return null;
  try {
    const out = await agentCoreControlClient().send(
      new GetConfigurationBundleVersionCommand({
        bundleId: pointer.bundleId,
        versionId: pointer.versionId,
      })
    );
    const component = out.components?.[CONFIG_BUNDLE_COMPONENT_KEY];
    const configuration = component?.configuration;
    if (configuration && typeof configuration === 'object' && !Array.isArray(configuration)) {
      return configuration as Record<string, unknown>;
    }
    return null;
  } catch (error) {
    console.warn('[admin-eval] 활성 bundle 버전 조회 실패:', error);
    return null;
  }
}

/** 활성 bundle 의 system_prompt (없으면 빈 문자열). */
export async function readActiveSystemPrompt(): Promise<string> {
  const configuration = await readActiveBundleConfiguration();
  const prompt = configuration?.system_prompt;
  return typeof prompt === 'string' ? prompt : '';
}

/**
 * orchestrator 로그 그룹의 ARN. Recommendations 는 이름이 아니라 ARN 을 요구하므로
 * DescribeLogGroups 로 확인해 가져온다(계정 ID env·STS 호출 회피). 못 찾으면 null.
 */
export async function resolveOrchestratorLogGroupArn(): Promise<string | null> {
  if (!ORCHESTRATOR_LOG_GROUP) return null;
  try {
    const out = await cloudWatchLogsClient().send(
      new DescribeLogGroupsCommand({ logGroupNamePrefix: ORCHESTRATOR_LOG_GROUP, limit: 5 })
    );
    const match = (out.logGroups ?? []).find((g) => g.logGroupName === ORCHESTRATOR_LOG_GROUP);
    return match?.arn ?? null;
  } catch (error) {
    console.warn('[admin-eval] 로그 그룹 ARN 조회 실패:', error);
    return null;
  }
}

/** Preview API 실패를 502 로 정규화 — 메시지는 그대로 노출해 화면이 폴백 안내를 띄운다. */
export function upstreamError(error: unknown, hint?: string): Response {
  const message = error instanceof Error ? error.message : String(error);
  console.error('[admin-eval] 업스트림 호출 실패:', error);
  return Response.json(
    { status: 'error', message: hint ? `${message} (${hint})` : message },
    { status: 502 }
  );
}
