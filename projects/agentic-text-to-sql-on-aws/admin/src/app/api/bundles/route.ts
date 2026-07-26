// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * GET  /api/bundles — ListConfigurationBundles + SSM 활성 포인터 (§9.6)
 * POST /api/bundles — CreateConfigurationBundle (최초 생성, 현재 프롬프트 스냅샷)
 *
 * components 의 키는 runtime ARN 이 아니라 **논리 키 `"orchestrator"`** 다(§9.1 — 자기 ARN
 * 자기참조 회피, 문서 예제와 의도적 편차). orchestrator 는 SSM 포인터로 이 키를 읽는다.
 *
 * 활성 포인터(SSM)는 별도 리소스라 bundle 목록 조회 실패와 독립적으로 처리한다 —
 * 승격 이력이 없으면 `active: null`.
 */

import {
  CreateConfigurationBundleCommand,
  ListConfigurationBundlesCommand,
} from '@aws-sdk/client-bedrock-agentcore-control';
import { handle, jsonError, readJson } from '@/lib/api';
import { requireManager } from '@/lib/auth';
import { agentCoreControlClient } from '@/lib/aws-clients';
import { CONFIG_BUNDLE_COMPONENT_KEY, CONFIG_BUNDLE_NAME } from '@/lib/env';
import { isoOrUndefined, mapBundle, readActiveBundle, upstreamError } from '@/lib/eval';
import type { ConfigurationBundleItem } from '@/lib/types';

export const dynamic = 'force-dynamic';

export async function GET(request: Request) {
  return handle(async () => {
    await requireManager(request);

    const bundles: ConfigurationBundleItem[] = [];
    try {
      const client = agentCoreControlClient();
      let nextToken: string | undefined;
      do {
        const out = await client.send(
          new ListConfigurationBundlesCommand({ nextToken, maxResults: 50 })
        );
        for (const summary of out.bundles ?? []) bundles.push(mapBundle(summary));
        nextToken = out.nextToken;
      } while (nextToken);
    } catch (error) {
      return upstreamError(error, 'ListConfigurationBundles (Preview)');
    }

    const active = await readActiveBundle();

    return Response.json(
      {
        status: 'ok',
        bundle_name: CONFIG_BUNDLE_NAME,
        component_key: CONFIG_BUNDLE_COMPONENT_KEY,
        bundles,
        active,
      },
      { status: 200 }
    );
  });
}

export async function POST(request: Request) {
  return handle(async () => {
    const principal = await requireManager(request);
    const body = await readJson(request);

    const systemPrompt = typeof body.systemPrompt === 'string' ? body.systemPrompt : '';
    const modelId = typeof body.modelId === 'string' ? body.modelId.trim() : '';
    const description = typeof body.description === 'string' ? body.description : undefined;

    if (!systemPrompt.trim()) return jsonError('systemPrompt 가 필요합니다', 400);
    if (!modelId) return jsonError('modelId 가 필요합니다', 400);

    try {
      const out = await agentCoreControlClient().send(
        new CreateConfigurationBundleCommand({
          bundleName: CONFIG_BUNDLE_NAME,
          description: description ?? 'orchestrator 시스템 프롬프트·모델 설정 번들',
          components: {
            [CONFIG_BUNDLE_COMPONENT_KEY]: {
              configuration: { system_prompt: systemPrompt, model_id: modelId },
            },
          },
          commitMessage: 'admin panel 최초 생성 (현재 프롬프트 스냅샷)',
          createdBy: { name: principal.username },
        })
      );
      return Response.json(
        {
          status: 'ok',
          bundle_id: out.bundleId,
          bundle_arn: out.bundleArn,
          version_id: out.versionId,
          created_at: isoOrUndefined(out.createdAt),
        },
        { status: 201 }
      );
    } catch (error) {
      return upstreamError(error, 'CreateConfigurationBundle (Preview)');
    }
  });
}
