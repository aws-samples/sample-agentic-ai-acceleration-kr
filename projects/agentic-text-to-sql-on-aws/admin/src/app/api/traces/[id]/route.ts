// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * GET /api/traces/{id} — 세션 이벤트 타임라인.
 *
 * `{id}` 는 `/api/traces/sessions` 가 준 합성 키(`<로그그룹>|<스트림>`)를 URL 인코딩한 값이다.
 * 임의 로그 그룹 조회를 막기 위해 **프리픽스(RUNTIME_LOG_GROUP_PREFIX) 검증**을 반드시 통과시킨다.
 */

import { GetLogEventsCommand } from '@aws-sdk/client-cloudwatch-logs';
import { handle, jsonError } from '@/lib/api';
import { requireManager } from '@/lib/auth';
import { cloudWatchLogsClient } from '@/lib/aws-clients';
import { RUNTIME_LOG_GROUP_PREFIX } from '@/lib/env';
import type { TraceEvent } from '@/lib/types';

export const dynamic = 'force-dynamic';

const SEPARATOR = '|';
/** 타임라인 1회 조회 이벤트 상한. */
const EVENT_LIMIT = 200;

export async function GET(request: Request, { params }: { params: { id: string } }) {
  return handle(async () => {
    await requireManager(request);

    const decoded = decodeURIComponent(params.id);
    const separatorAt = decoded.indexOf(SEPARATOR);
    if (separatorAt <= 0) {
      return jsonError('세션 ID 형식이 올바르지 않습니다 (<로그그룹>|<스트림>)', 400);
    }
    const logGroupName = decoded.slice(0, separatorAt);
    const logStreamName = decoded.slice(separatorAt + 1);
    if (!logStreamName) {
      return jsonError('로그 스트림 이름이 비어 있습니다', 400);
    }
    // 관리 대상 프리픽스 밖의 로그 그룹 접근 차단 (경로 파라미터 신뢰 금지).
    if (!logGroupName.startsWith(RUNTIME_LOG_GROUP_PREFIX)) {
      return jsonError('조회할 수 없는 로그 그룹입니다', 403);
    }

    const out = await cloudWatchLogsClient().send(
      new GetLogEventsCommand({
        logGroupName,
        logStreamName,
        limit: EVENT_LIMIT,
        startFromHead: true,
      })
    );

    const events: TraceEvent[] = (out.events ?? []).map((event) => ({
      timestamp: event.timestamp ? new Date(event.timestamp).toISOString() : '',
      message: event.message ?? '',
    }));

    return Response.json(
      {
        status: 'ok',
        session: { id: decoded, log_group: logGroupName, log_stream: logStreamName },
        events,
      },
      { status: 200 }
    );
  });
}
