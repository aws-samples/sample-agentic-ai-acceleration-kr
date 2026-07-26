// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * GET /api/iam/groups — 사용자 풀의 그룹 목록 (Admin 전용).
 *
 * 그룹 지정 드롭다운을 실제 풀 상태로 채우기 위한 보조 경로다(§8.4 iam/* 계열).
 */

import { ListGroupsCommand } from '@aws-sdk/client-cognito-identity-provider';
import { handle } from '@/lib/api';
import { requireAdmin } from '@/lib/auth';
import { cognitoClient } from '@/lib/aws-clients';
import { requiredEnv } from '@/lib/env';

export const dynamic = 'force-dynamic';

export async function GET(request: Request) {
  return handle(async () => {
    await requireAdmin(request);
    const listed = await cognitoClient().send(
      new ListGroupsCommand({ UserPoolId: requiredEnv('COGNITO_USER_POOL_ID'), Limit: 60 })
    );
    const groups = (listed.Groups ?? []).map((g) => ({
      name: g.GroupName ?? '',
      description: g.Description,
    }));
    return Response.json({ status: 'ok', groups }, { status: 200 });
  });
}
