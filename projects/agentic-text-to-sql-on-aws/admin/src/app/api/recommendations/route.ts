// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * GET  /api/recommendations       — ListRecommendations
 * POST /api/recommendations {type} — StartRecommendation (§9.6)
 *
 * **Preview API** 다(§9.0). 실패는 502 + 서비스 메시지를 그대로 내려 화면이 "추천 기능을
 * 사용할 수 없습니다 — 수동 프롬프트 편집으로 진행하세요" 폴백을 안내한다.
 *
 * 입력 프롬프트는 현재 활성 bundle 의 `components["orchestrator"].system_prompt` 를 스냅샷으로
 * 쓴다(§9.1). 활성 bundle 이 없으면 빈 문자열로 호출한다 — 서비스가 거부하면 그 메시지가
 * 그대로 폴백 안내에 실린다(승격 이력이 없는 초기 상태를 사용자에게 그대로 알림).
 */

import {
  ListRecommendationsCommand,
  StartRecommendationCommand,
  type StartRecommendationCommandInput,
} from '@aws-sdk/client-bedrock-agentcore';
import { handle, jsonError, readJson } from '@/lib/api';
import { requireManager } from '@/lib/auth';
import { agentCoreDataClient } from '@/lib/aws-clients';
import { ORCHESTRATOR_SERVICE_NAME } from '@/lib/env';
import {
  isoOrUndefined,
  mapRecommendation,
  normalizeHours,
  readActiveSystemPrompt,
  resolveOrchestratorLogGroupArn,
  underscoreName,
  upstreamError,
} from '@/lib/eval';
import { RECOMMENDATION_TYPES, type RecommendationItem } from '@/lib/types';

export const dynamic = 'force-dynamic';

const MAX_RECOMMENDATIONS = 50;

/** 화면 입력(`SYSTEM_PROMPT`) → 서비스 enum(`SYSTEM_PROMPT_RECOMMENDATION`) 매핑 (§9.6). */
const TYPE_MAP: Record<string, 'SYSTEM_PROMPT_RECOMMENDATION' | 'TOOL_DESCRIPTION_RECOMMENDATION'> =
  {
    SYSTEM_PROMPT: 'SYSTEM_PROMPT_RECOMMENDATION',
    TOOL_DESCRIPTION: 'TOOL_DESCRIPTION_RECOMMENDATION',
  };

export async function GET(request: Request) {
  return handle(async () => {
    await requireManager(request);

    const recommendations: RecommendationItem[] = [];
    try {
      const out = await agentCoreDataClient().send(
        new ListRecommendationsCommand({ maxResults: MAX_RECOMMENDATIONS })
      );
      for (const summary of out.recommendationSummaries ?? []) {
        recommendations.push(mapRecommendation(summary));
      }
    } catch (error) {
      return upstreamError(error, 'ListRecommendations (Preview)');
    }

    recommendations.sort((a, b) => (b.created_at ?? '').localeCompare(a.created_at ?? ''));

    return Response.json({ status: 'ok', recommendations }, { status: 200 });
  });
}

export async function POST(request: Request) {
  return handle(async () => {
    await requireManager(request);
    const body = await readJson(request);

    const typeKey = typeof body.type === 'string' ? body.type : '';
    if (!(RECOMMENDATION_TYPES as readonly string[]).includes(typeKey)) {
      return jsonError(`type 은 ${RECOMMENDATION_TYPES.join(' | ')} 중 하나여야 합니다`, 400);
    }

    if (!ORCHESTRATOR_SERVICE_NAME) {
      return jsonError(
        'ORCHESTRATOR_SERVICE_NAME 이 설정되지 않았습니다 — evaluation 스택 배포 후 admin 스택 env 를 갱신하세요',
        400
      );
    }
    const logGroupArn = await resolveOrchestratorLogGroupArn();
    if (!logGroupArn) {
      return jsonError(
        'orchestrator 로그 그룹 ARN 을 확인할 수 없습니다 (ORCHESTRATOR_LOG_GROUP 확인 필요)',
        400
      );
    }

    const hours = normalizeHours(body.hours);
    const endTime = new Date();
    const startTime = new Date(endTime.getTime() - hours * 3600 * 1000);
    const cloudwatchLogs = {
      logGroupArns: [logGroupArn],
      serviceNames: [ORCHESTRATOR_SERVICE_NAME],
      startTime,
      endTime,
    };
    // evaluationConfig 는 평가자 **ARN** 을 요구한다. admin env 에는 ID(§9.7)만 있으므로
    // 선택 필드인 이 블록은 생략한다 — 추천 품질 평가는 배치 평가 화면에서 별도로 수행한다.

    const systemPrompt = await readActiveSystemPrompt();
    const name = underscoreName(
      typeKey === 'SYSTEM_PROMPT' ? 'admin_reco_prompt' : 'admin_reco_tools',
      endTime.getTime()
    );

    const input: StartRecommendationCommandInput =
      typeKey === 'SYSTEM_PROMPT'
        ? {
            name,
            description: `admin panel 시스템 프롬프트 추천 (최근 ${hours}시간)`,
            type: TYPE_MAP[typeKey],
            recommendationConfig: {
              systemPromptRecommendationConfig: {
                systemPrompt: { text: systemPrompt },
                agentTraces: { cloudwatchLogs },
              },
            },
          }
        : {
            name,
            description: `admin panel 도구 설명 추천 (최근 ${hours}시간)`,
            type: TYPE_MAP[typeKey],
            recommendationConfig: {
              toolDescriptionRecommendationConfig: {
                // 도구 설명은 트레이스에서 사용된 도구를 서비스가 식별하므로 빈 목록으로 시작한다.
                toolDescription: { toolDescriptionText: { tools: [] } },
                agentTraces: { cloudwatchLogs },
              },
            },
          };

    try {
      const out = await agentCoreDataClient().send(new StartRecommendationCommand(input));
      return Response.json(
        {
          status: 'ok',
          recommendation_id: out.recommendationId,
          recommendation_arn: out.recommendationArn,
          name: out.name,
          window_hours: hours,
          created_at: isoOrUndefined(out.createdAt),
        },
        { status: 202 }
      );
    } catch (error) {
      return upstreamError(error, 'StartRecommendation (Preview)');
    }
  });
}
