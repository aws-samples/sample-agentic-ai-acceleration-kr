// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * POST /api/auth/login — Cognito USER_PASSWORD_AUTH 로그인.
 *
 * 비밀번호는 서버(이 route)에서만 다루고 저장하지 않는다. 발급된 AccessToken 은 클라이언트가
 * sessionStorage 에 보관하고 이후 모든 API 호출의 `Authorization: Bearer` 로 전달한다
 * (그 토큰이 그대로 Gateway MCP OBO 에 재사용된다.)
 *
 * NEW_PASSWORD_REQUIRED 등 challenge 응답은 그대로 전달해 클라이언트가 안내한다.
 */

import {
  InitiateAuthCommand,
  type InitiateAuthCommandOutput,
} from '@aws-sdk/client-cognito-identity-provider';
import { handle, jsonError, readJson } from '@/lib/api';
import { cognitoClient } from '@/lib/aws-clients';
import { requiredEnv } from '@/lib/env';

export const dynamic = 'force-dynamic';

export async function POST(request: Request) {
  return handle(async () => {
    const body = await readJson(request);
    const username = typeof body.username === 'string' ? body.username.trim() : '';
    const password = typeof body.password === 'string' ? body.password : '';
    if (!username || !password) {
      return jsonError('username 과 password 가 필요합니다', 400);
    }

    let output: InitiateAuthCommandOutput;
    try {
      output = await cognitoClient().send(
        new InitiateAuthCommand({
          AuthFlow: 'USER_PASSWORD_AUTH',
          ClientId: requiredEnv('COGNITO_CLIENT_ID'),
          AuthParameters: { USERNAME: username, PASSWORD: password },
        })
      );
    } catch (error) {
      // 자격증명 오류는 상세를 노출하지 않고 401 로 정규화 (사용자 열거 방지).
      const name = (error as { name?: string }).name ?? '';
      if (name === 'NotAuthorizedException' || name === 'UserNotFoundException') {
        return jsonError('아이디 또는 비밀번호가 올바르지 않습니다', 401);
      }
      throw error;
    }

    if (output.ChallengeName) {
      // 예: NEW_PASSWORD_REQUIRED — 콘솔/CLI 로 비밀번호를 확정한 뒤 재로그인해야 한다.
      return Response.json(
        {
          status: 'error',
          message: `추가 인증 단계가 필요합니다: ${output.ChallengeName}`,
          challenge: output.ChallengeName,
        },
        { status: 409 }
      );
    }

    const auth = output.AuthenticationResult;
    if (!auth?.AccessToken) {
      return jsonError('토큰 발급에 실패했습니다', 502);
    }

    return Response.json(
      {
        status: 'ok',
        access_token: auth.AccessToken,
        id_token: auth.IdToken,
        expires_in: auth.ExpiresIn,
        token_type: auth.TokenType,
      },
      { status: 200 }
    );
  });
}
