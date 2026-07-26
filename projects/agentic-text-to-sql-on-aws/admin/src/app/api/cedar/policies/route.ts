// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * GET /api/cedar/policies — Cedar 정책 **read-only** 조회 (§8.4).
 *
 * 정책은 CDK(gateway 스택)가 소유하는 IaC 산출물이다. 콘솔에서 편집하면 다음 배포에 덮여
 * 드리프트가 되므로 admin panel 은 조회만 제공하고 화면에서 편집 불가를 명시한다.
 * task role 에도 Get/List 만 부여한다(최소 권한).
 */

import { ListPoliciesCommand, type Policy } from '@aws-sdk/client-bedrock-agentcore-control';
import { handle, jsonError } from '@/lib/api';
import { requireManager } from '@/lib/auth';
import { agentCoreControlClient } from '@/lib/aws-clients';
import { POLICY_ENGINE_ID } from '@/lib/env';
import type { CedarPolicySummary } from '@/lib/types';

export const dynamic = 'force-dynamic';

/**
 * PolicyDefinition 은 union(cedar | policy | policyGeneration)이므로 Cedar 문장을
 * 안전하게 추출한다. 알 수 없는 멤버는 undefined 로 두고 화면에서 "표시 불가"로 처리.
 */
function extractStatement(policy: Policy): string | undefined {
  const definition = policy.definition;
  if (!definition) return undefined;
  if ('cedar' in definition && definition.cedar?.statement) return definition.cedar.statement;
  if ('policy' in definition && definition.policy?.statement) return definition.policy.statement;
  return undefined;
}

function toSummary(policy: Policy): CedarPolicySummary {
  return {
    policy_id: policy.policyId ?? '',
    name: policy.name,
    description: policy.description,
    status: policy.status,
    enforcement_mode: policy.enforcementMode,
    statement: extractStatement(policy),
    updated_at: policy.updatedAt?.toISOString(),
  };
}

export async function GET(request: Request) {
  return handle(async () => {
    await requireManager(request);
    if (!POLICY_ENGINE_ID) {
      return jsonError('POLICY_ENGINE_ID 가 설정되지 않았습니다 (.env.example 참고)', 500);
    }

    // ListPolicies 는 definition 을 포함해 반환하므로 GetPolicy 개별 호출이 불필요하다
    // (호출 수·권한 표면 최소화). 페이지네이션은 관리 화면 규모상 전량 순회한다.
    const client = agentCoreControlClient();
    const policies: CedarPolicySummary[] = [];
    let nextToken: string | undefined;
    do {
      const out = await client.send(
        new ListPoliciesCommand({ policyEngineId: POLICY_ENGINE_ID, nextToken, maxResults: 50 })
      );
      for (const policy of out.policies ?? []) policies.push(toSummary(policy));
      nextToken = out.nextToken;
    } while (nextToken);

    return Response.json(
      {
        status: 'ok',
        policy_engine_id: POLICY_ENGINE_ID,
        editable: false,
        policies,
      },
      { status: 200 }
    );
  });
}
