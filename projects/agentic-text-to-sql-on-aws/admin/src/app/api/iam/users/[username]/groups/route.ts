// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * POST /api/iam/users/{username}/groups — 그룹 지정/해제 (Admin 전용).
 *
 * body: `{group: string, action?: "add"|"remove"}` (기본 add).
 * 그룹 클레임(`cognito:groups`)이 Cedar 인가와 화면 분기의 근거이므로, 변경은 Admin 만 가능하다.
 */

import {
  AdminAddUserToGroupCommand,
  AdminListGroupsForUserCommand,
  AdminRemoveUserFromGroupCommand,
} from '@aws-sdk/client-cognito-identity-provider';
import { handle, jsonError, readJson } from '@/lib/api';
import { requireAdmin } from '@/lib/auth';
import { cognitoClient } from '@/lib/aws-clients';
import { requiredEnv } from '@/lib/env';

export const dynamic = 'force-dynamic';

export async function POST(request: Request, { params }: { params: { username: string } }) {
  return handle(async () => {
    await requireAdmin(request);
    const body = await readJson(request);

    const group = typeof body.group === 'string' ? body.group.trim() : '';
    if (!group) return jsonError('group 이 필요합니다', 400);
    const action = body.action === 'remove' ? 'remove' : 'add';

    const userPoolId = requiredEnv('COGNITO_USER_POOL_ID');
    const username = decodeURIComponent(params.username);
    const client = cognitoClient();

    if (action === 'add') {
      await client.send(
        new AdminAddUserToGroupCommand({
          UserPoolId: userPoolId,
          Username: username,
          GroupName: group,
        })
      );
    } else {
      await client.send(
        new AdminRemoveUserFromGroupCommand({
          UserPoolId: userPoolId,
          Username: username,
          GroupName: group,
        })
      );
    }

    // 변경 후 그룹을 되읽어 화면 상태와 실제를 일치시킨다.
    const groupsOut = await client.send(
      new AdminListGroupsForUserCommand({ UserPoolId: userPoolId, Username: username })
    );
    const groups = (groupsOut.Groups ?? [])
      .map((g) => g.GroupName)
      .filter((n): n is string => Boolean(n));

    return Response.json({ status: 'ok', username, action, groups }, { status: 200 });
  });
}
