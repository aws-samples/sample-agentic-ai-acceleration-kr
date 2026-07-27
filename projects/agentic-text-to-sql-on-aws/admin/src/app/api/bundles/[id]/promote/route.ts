// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * POST /api/bundles/{id}/promote {versionId} — 활성 bundle 승격.
 *
 * 승격은 SSM 파라미터(`/agentic-t2sql/active-bundle`) 값을 JSON `{bundleId, versionId}` 로
 * 갱신하는 **한 번의 쓰기**다. orchestrator 는 세션 시작 시 TTL 60s 캐시로 이 값을 읽어
 * system_prompt/model_id 를 오버라이드하고, 실패·빈 값이면 코드 기본값으로 폴백한다.
 *
 * A/B 트래픽 분할은 안정 API 표면이 없어 미구현이므로, 승격은 전량 전환이고
 * **롤백도 같은 API 로 예전 versionId 를 다시 승격**하는 방식이다(수동 전환 폴백).
 */

import { PutParameterCommand } from '@aws-sdk/client-ssm';
import { handle, jsonError, readJson } from '@/lib/api';
import { requireManager } from '@/lib/auth';
import { ssmClient } from '@/lib/aws-clients';
import { ACTIVE_BUNDLE_PARAM } from '@/lib/env';
import { upstreamError } from '@/lib/eval';

export const dynamic = 'force-dynamic';

export async function POST(request: Request, { params }: { params: { id: string } }) {
  return handle(async () => {
    const principal = await requireManager(request);
    const bundleId = decodeURIComponent(params.id);
    const body = await readJson(request);

    const versionId = typeof body.versionId === 'string' ? body.versionId.trim() : '';
    if (!versionId) return jsonError('versionId 가 필요합니다', 400);

    const pointer = { bundleId, versionId };
    try {
      await ssmClient().send(
        new PutParameterCommand({
          Name: ACTIVE_BUNDLE_PARAM,
          Value: JSON.stringify(pointer),
          Type: 'String',
          Overwrite: true,
          // 승격 이력은 SSM 파라미터 버전으로도 남는다(감사). 값은 ASCII 로 유지한다.
          Description: `promoted by ${principal.username}`,
        })
      );
    } catch (error) {
      return upstreamError(error, 'SSM PutParameter');
    }

    return Response.json(
      {
        status: 'ok',
        parameter_name: ACTIVE_BUNDLE_PARAM,
        active: pointer,
        promoted_by: principal.username,
        note: 'orchestrator 는 TTL 60초 캐시로 반영합니다 (전량 전환 — A/B 분할 미제공)',
      },
      { status: 200 }
    );
  });
}
