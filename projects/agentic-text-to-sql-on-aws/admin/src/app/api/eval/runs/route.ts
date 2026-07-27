// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * GET  /api/eval/runs  — ListBatchEvaluations
 * POST /api/eval/runs  — StartBatchEvaluation
 *
 * 데이터 소스는 orchestrator Runtime 로그 그룹(CloudWatch Logs)이다 — 별도 데이터셋 업로드 없이
 * 실제 트래픽 트레이스를 평가한다. 기본 평가자는 EX(custom) + Builtin.Correctness.
 *
 * 이름 규칙: `admin_eval_<epoch>` — 서비스가 **언더스코어만** 허용하므로 하이픈을 쓰지 않는다.
 * 비동기 작업이라 응답은 batchEvaluationId·status(PENDING) 까지이고, 진행 상황은 목록·상세로 본다.
 */

import {
  ListBatchEvaluationsCommand,
  StartBatchEvaluationCommand,
  type StartBatchEvaluationCommandInput,
} from '@aws-sdk/client-bedrock-agentcore';
import { handle, jsonError, readJson } from '@/lib/api';
import { requireManager } from '@/lib/auth';
import { agentCoreDataClient } from '@/lib/aws-clients';
import {
  EVAL_EXECUTION_ROLE_ARN,
  EXECUTION_EVALUATOR_ID,
  ORCHESTRATOR_LOG_GROUP,
  ORCHESTRATOR_SERVICE_NAME,
} from '@/lib/env';
import {
  isoOrUndefined,
  mapBatchEvaluation,
  normalizeHours,
  underscoreName,
  upstreamError,
} from '@/lib/eval';
import type { BatchEvaluationItem } from '@/lib/types';

export const dynamic = 'force-dynamic';

/** 목록 상한 — 관리 화면 규모상 최근 50건이면 충분하다(페이지네이션 UI 미도입). */
const MAX_RUNS = 50;

/** 기본 builtin 평가자 (EX + Correctness). */
const DEFAULT_BUILTIN_EVALUATOR = 'Builtin.Correctness';

export async function GET(request: Request) {
  return handle(async () => {
    await requireManager(request);

    const runs: BatchEvaluationItem[] = [];
    try {
      const out = await agentCoreDataClient().send(
        new ListBatchEvaluationsCommand({ maxResults: MAX_RUNS })
      );
      for (const summary of out.batchEvaluations ?? []) runs.push(mapBatchEvaluation(summary));
    } catch (error) {
      return upstreamError(error, 'ListBatchEvaluations');
    }

    // 최신순 정렬 (서비스 정렬 순서에 의존하지 않는다).
    runs.sort((a, b) => (b.created_at ?? '').localeCompare(a.created_at ?? ''));

    return Response.json({ status: 'ok', runs }, { status: 200 });
  });
}

export async function POST(request: Request) {
  return handle(async () => {
    await requireManager(request);
    const body = await readJson(request);

    if (!ORCHESTRATOR_LOG_GROUP || !ORCHESTRATOR_SERVICE_NAME) {
      return jsonError(
        'ORCHESTRATOR_LOG_GROUP / ORCHESTRATOR_SERVICE_NAME 이 설정되지 않았습니다 — ' +
          'evaluation 스택 배포 후 admin 스택 env 를 갱신하세요',
        400
      );
    }

    const hours = normalizeHours(body.hours);
    const requested = Array.isArray(body.evaluators)
      ? body.evaluators.filter((id): id is string => typeof id === 'string' && id.trim().length > 0)
      : [];
    const evaluatorIds = requested.length
      ? requested
      : [EXECUTION_EVALUATOR_ID, DEFAULT_BUILTIN_EVALUATOR].filter(Boolean);

    if (!evaluatorIds.length) {
      return jsonError(
        '평가자가 없습니다 — EXECUTION_EVALUATOR_ID 미설정 시 evaluators 를 직접 지정하세요',
        400
      );
    }

    const endTime = new Date();
    const startTime = new Date(endTime.getTime() - hours * 3600 * 1000);

    const input: StartBatchEvaluationCommandInput = {
      batchEvaluationName: underscoreName('admin_eval', endTime.getTime()),
      description: `admin panel 배치 평가 (최근 ${hours}시간)`,
      evaluators: evaluatorIds.map((evaluatorId) => ({ evaluatorId })),
      dataSourceConfig: {
        cloudWatchLogs: {
          logGroupNames: [ORCHESTRATOR_LOG_GROUP],
          serviceNames: [ORCHESTRATOR_SERVICE_NAME],
          filterConfig: { timeRange: { startTime, endTime } },
        },
      },
    };

    // 실행 role 은 온라인 평가와 동일한 role 을 재사용한다. 설치된 SDK 의 데이터플레인
    // 요청 스키마에는 이 멤버가 없어 직렬화 시 무시되지만, 서비스가 추가하면 그대로 전달되도록
    // additive 로 붙여 둔다(미설정이어도 호출은 성공해야 한다).
    const payload: StartBatchEvaluationCommandInput = EVAL_EXECUTION_ROLE_ARN
      ? ({
          ...input,
          evaluationExecutionRoleArn: EVAL_EXECUTION_ROLE_ARN,
        } as StartBatchEvaluationCommandInput)
      : input;

    try {
      const out = await agentCoreDataClient().send(new StartBatchEvaluationCommand(payload));
      return Response.json(
        {
          status: 'ok',
          batch_evaluation_id: out.batchEvaluationId,
          batch_evaluation_arn: out.batchEvaluationArn,
          batch_evaluation_name: out.batchEvaluationName,
          batch_status: out.status,
          created_at: isoOrUndefined(out.createdAt),
          window_hours: hours,
          evaluator_ids: evaluatorIds,
        },
        { status: 202 }
      );
    } catch (error) {
      return upstreamError(error, 'StartBatchEvaluation');
    }
  });
}
