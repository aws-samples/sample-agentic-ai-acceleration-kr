// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * GET /api/traces/sessions — 최근 세션(로그 스트림) 목록 (§8.4).
 *
 * AgentCore Runtime 은 세션 단위로 로그 스트림을 만든다. `RUNTIME_LOG_GROUP_PREFIX` 하위
 * 로그 그룹들을 훑어 최근 스트림을 모아 "세션 목록"으로 제시한다. X-Ray BatchGetTraces 는
 * optional 이라 도입하지 않고(권한 표면 최소화) 로그만으로 타임라인을 구성한다.
 *
 * 세션 ID 는 `<로그그룹>|<스트림>` 을 URL 인코딩한 합성 키다 — 상세 조회(`/api/traces/{id}`)가
 * 두 값을 모두 필요로 하기 때문.
 */

import {
  DescribeLogGroupsCommand,
  DescribeLogStreamsCommand,
} from '@aws-sdk/client-cloudwatch-logs';
import { handle } from '@/lib/api';
import { requireManager } from '@/lib/auth';
import { cloudWatchLogsClient } from '@/lib/aws-clients';
import { RUNTIME_LOG_GROUP_PREFIX } from '@/lib/env';
import type { TraceSession } from '@/lib/types';

export const dynamic = 'force-dynamic';

/** 로그 그룹당 가져올 최근 스트림 수 / 전체 상한. */
const STREAMS_PER_GROUP = 10;
const MAX_GROUPS = 10;
const MAX_SESSIONS = 40;

/** 합성 세션 ID 구분자 (그룹|스트림) — `/api/traces/{id}` 가 같은 규약으로 분해한다. */
const SESSION_ID_SEPARATOR = '|';

export async function GET(request: Request) {
  return handle(async () => {
    await requireManager(request);
    const client = cloudWatchLogsClient();

    const groupsOut = await client.send(
      new DescribeLogGroupsCommand({
        logGroupNamePrefix: RUNTIME_LOG_GROUP_PREFIX,
        limit: MAX_GROUPS,
      })
    );
    const groups = (groupsOut.logGroups ?? [])
      .map((g) => g.logGroupName)
      .filter((n): n is string => Boolean(n));

    const sessions: TraceSession[] = [];
    for (const logGroup of groups) {
      try {
        const streamsOut = await client.send(
          new DescribeLogStreamsCommand({
            logGroupName: logGroup,
            orderBy: 'LastEventTime',
            descending: true,
            limit: STREAMS_PER_GROUP,
          })
        );
        for (const stream of streamsOut.logStreams ?? []) {
          if (!stream.logStreamName) continue;
          sessions.push({
            id: `${logGroup}${SESSION_ID_SEPARATOR}${stream.logStreamName}`,
            log_group: logGroup,
            // 프리픽스를 제거해 런타임 이름만 표시.
            runtime: logGroup.replace(RUNTIME_LOG_GROUP_PREFIX, ''),
            first_event_at: stream.firstEventTimestamp
              ? new Date(stream.firstEventTimestamp).toISOString()
              : undefined,
            last_event_at: stream.lastEventTimestamp
              ? new Date(stream.lastEventTimestamp).toISOString()
              : undefined,
          });
        }
      } catch (error) {
        // 개별 로그 그룹 실패는 건너뛴다 (전체 목록은 계속 제공).
        console.warn(`[admin-traces] 스트림 조회 실패 (${logGroup}):`, error);
      }
    }

    // 최신 이벤트 순으로 정렬 후 상한 적용.
    sessions.sort((a, b) => (b.last_event_at ?? '').localeCompare(a.last_event_at ?? ''));

    return Response.json(
      {
        status: 'ok',
        log_group_prefix: RUNTIME_LOG_GROUP_PREFIX,
        sessions: sessions.slice(0, MAX_SESSIONS),
      },
      { status: 200 }
    );
  });
}
