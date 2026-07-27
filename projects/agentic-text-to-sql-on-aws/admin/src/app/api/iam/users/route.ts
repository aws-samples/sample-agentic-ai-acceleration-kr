// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * GET  /api/iam/users — Cognito 사용자 목록 + 각 사용자의 그룹
 * POST /api/iam/users — 사용자 생성(AdminCreateUser) + 선택적 초기 그룹 지정
 *
 * **Admin 전용**. 사용자·그룹 관리는 도구 평면이 아닌 관리 평면이므로 MCP 를 거치지 않고
 * admin web task role 의 cognito-idp 직접 호출로 처리한다.
 */

import {
  AdminAddUserToGroupCommand,
  AdminCreateUserCommand,
  AdminListGroupsForUserCommand,
  ListUsersCommand,
  type UserType,
} from '@aws-sdk/client-cognito-identity-provider';
import { handle, jsonError, readJson } from '@/lib/api';
import { requireAdmin } from '@/lib/auth';
import { cognitoClient } from '@/lib/aws-clients';
import { requiredEnv } from '@/lib/env';
import type { IamUser } from '@/lib/types';

export const dynamic = 'force-dynamic';

/** 목록 1페이지 상한 — 관리 화면 용도로 충분하며 과도한 조회를 막는다. */
const USER_PAGE_LIMIT = 60;

/** Cognito UserType → 화면용 요약. 그룹은 사용자별 조회로 채운다. */
function toSummary(user: UserType, groups: string[]): IamUser {
  const attr = (name: string) => user.Attributes?.find((a) => a.Name === name)?.Value ?? undefined;
  return {
    username: user.Username ?? '',
    email: attr('email'),
    status: user.UserStatus,
    enabled: user.Enabled,
    created_at: user.UserCreateDate?.toISOString(),
    groups,
  };
}

export async function GET(request: Request) {
  return handle(async () => {
    await requireAdmin(request);
    const userPoolId = requiredEnv('COGNITO_USER_POOL_ID');
    const client = cognitoClient();

    const listed = await client.send(
      new ListUsersCommand({ UserPoolId: userPoolId, Limit: USER_PAGE_LIMIT })
    );
    const users = listed.Users ?? [];

    // 사용자별 그룹 조회는 병렬로 (개별 실패는 빈 그룹으로 degrade).
    const summaries = await Promise.all(
      users.map(async (user) => {
        if (!user.Username) return toSummary(user, []);
        try {
          const groupsOut = await client.send(
            new AdminListGroupsForUserCommand({
              UserPoolId: userPoolId,
              Username: user.Username,
            })
          );
          const groups = (groupsOut.Groups ?? [])
            .map((g) => g.GroupName)
            .filter((n): n is string => Boolean(n));
          return toSummary(user, groups);
        } catch (error) {
          console.warn(`[admin-iam] 그룹 조회 실패 (${user.Username}):`, error);
          return toSummary(user, []);
        }
      })
    );

    return Response.json({ status: 'ok', users: summaries }, { status: 200 });
  });
}

export async function POST(request: Request) {
  return handle(async () => {
    await requireAdmin(request);
    const body = await readJson(request);

    const username = typeof body.username === 'string' ? body.username.trim() : '';
    if (!username) return jsonError('username 이 필요합니다', 400);
    const email = typeof body.email === 'string' ? body.email.trim() : '';
    const group = typeof body.group === 'string' ? body.group.trim() : '';
    // 임시 비밀번호는 지정하지 않으면 Cognito 가 생성해 초대 메일로 전달한다.
    const temporaryPassword =
      typeof body.temporary_password === 'string' && body.temporary_password
        ? body.temporary_password
        : undefined;

    const userPoolId = requiredEnv('COGNITO_USER_POOL_ID');
    const client = cognitoClient();

    const created = await client.send(
      new AdminCreateUserCommand({
        UserPoolId: userPoolId,
        Username: username,
        TemporaryPassword: temporaryPassword,
        UserAttributes: email
          ? [
              { Name: 'email', Value: email },
              { Name: 'email_verified', Value: 'true' },
            ]
          : undefined,
      })
    );

    if (group) {
      await client.send(
        new AdminAddUserToGroupCommand({
          UserPoolId: userPoolId,
          Username: username,
          GroupName: group,
        })
      );
    }

    return Response.json(
      {
        status: 'ok',
        user: {
          username: created.User?.Username ?? username,
          status: created.User?.UserStatus,
          groups: group ? [group] : [],
        },
      },
      { status: 201 }
    );
  });
}
