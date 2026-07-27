// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * GET /api/recommendations/{id} — GetRecommendation.
 *
 * 완료(COMPLETED) 상태면 추천 텍스트(시스템 프롬프트 / 도구 설명)를 평탄화해 전달한다.
 * Manager 는 그 텍스트를 Configuration Bundle 새 버전으로 반영한 뒤 승격한다(사람 승인).
 */

import { GetRecommendationCommand } from '@aws-sdk/client-bedrock-agentcore';
import { handle } from '@/lib/api';
import { requireManager } from '@/lib/auth';
import { agentCoreDataClient } from '@/lib/aws-clients';
import { mapRecommendationDetail, upstreamError } from '@/lib/eval';

export const dynamic = 'force-dynamic';

export async function GET(request: Request, { params }: { params: { id: string } }) {
  return handle(async () => {
    await requireManager(request);

    try {
      const out = await agentCoreDataClient().send(
        new GetRecommendationCommand({ recommendationId: decodeURIComponent(params.id) })
      );
      return Response.json(
        { status: 'ok', recommendation: mapRecommendationDetail(out) },
        { status: 200 }
      );
    } catch (error) {
      return upstreamError(error, 'GetRecommendation (Preview)');
    }
  });
}
