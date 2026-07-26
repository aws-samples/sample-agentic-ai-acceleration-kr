// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * GET /api/eval/online — online eval config 상태 요약 (§9.6).
 *
 * `ONLINE_EVAL_CONFIG_ID` 가 없으면 evaluation 스택이 아직 배포되지 않은 환경이므로
 * `{configured:false}` 로 내려 화면이 "미구성" 안내를 띄운다(오류 아님 — 회귀 금지).
 * 최근 스코어는 online eval 이 CloudWatch 로그로 내보내므로 여기서는 샘플링률·상태·평가자
 * 구성 요약만 제공하고, 상세 스코어는 배치 평가 결과 화면에서 확인한다.
 */

import { GetOnlineEvaluationConfigCommand } from '@aws-sdk/client-bedrock-agentcore-control';
import { handle } from '@/lib/api';
import { requireManager } from '@/lib/auth';
import { agentCoreControlClient } from '@/lib/aws-clients';
import { ONLINE_EVAL_CONFIG_ID } from '@/lib/env';
import { isoOrUndefined, upstreamError } from '@/lib/eval';
import type { OnlineEvalStatus } from '@/lib/types';

export const dynamic = 'force-dynamic';

export async function GET(request: Request) {
  return handle(async () => {
    await requireManager(request);

    if (!ONLINE_EVAL_CONFIG_ID) {
      const body: OnlineEvalStatus = {
        configured: false,
        note: 'ONLINE_EVAL_CONFIG_ID 가 설정되지 않았습니다 — evaluation 스택 배포 후 사용할 수 있습니다',
      };
      return Response.json({ status: 'ok', ...body }, { status: 200 });
    }

    try {
      const out = await agentCoreControlClient().send(
        new GetOnlineEvaluationConfigCommand({
          onlineEvaluationConfigId: ONLINE_EVAL_CONFIG_ID,
        })
      );
      const dataSource = out.dataSourceConfig;
      const body: OnlineEvalStatus = {
        configured: true,
        online_evaluation_config_id: out.onlineEvaluationConfigId,
        online_evaluation_config_name: out.onlineEvaluationConfigName,
        config_status: out.status,
        execution_status: out.executionStatus,
        sampling_percentage: out.rule?.samplingConfig?.samplingPercentage,
        evaluator_ids: (out.evaluators ?? [])
          .map((e) => ('evaluatorId' in e ? e.evaluatorId : undefined))
          .filter((id): id is string => Boolean(id)),
        log_group_names:
          dataSource && 'cloudWatchLogs' in dataSource
            ? dataSource.cloudWatchLogs?.logGroupNames
            : undefined,
        output_log_group: out.outputConfig?.cloudWatchConfig?.logGroupName,
        failure_reason: out.failureReason,
        updated_at: isoOrUndefined(out.updatedAt),
      };
      return Response.json({ status: 'ok', ...body }, { status: 200 });
    } catch (error) {
      return upstreamError(error, 'GetOnlineEvaluationConfig');
    }
  });
}
