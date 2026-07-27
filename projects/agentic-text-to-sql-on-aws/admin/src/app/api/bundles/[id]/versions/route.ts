// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * GET  /api/bundles/{id}/versions — ListConfigurationBundleVersions
 * POST /api/bundles/{id}/versions — UpdateConfigurationBundle (새 버전)
 *
 * bundle 버전은 **불변**이다 — 수동 편집이든 추천 반영이든 항상 새 버전이 생기고, 어떤 버전이
 * 활성인지는 SSM 포인터가 결정한다. 따라서 롤백은 "예전 버전으로 다시 승격"과 동일하다.
 */

import {
  ListConfigurationBundleVersionsCommand,
  UpdateConfigurationBundleCommand,
} from '@aws-sdk/client-bedrock-agentcore-control';
import { handle, jsonError, readJson } from '@/lib/api';
import { requireManager } from '@/lib/auth';
import { agentCoreControlClient } from '@/lib/aws-clients';
import { CONFIG_BUNDLE_COMPONENT_KEY } from '@/lib/env';
import { isoOrUndefined, mapBundleVersion, readActiveBundle, upstreamError } from '@/lib/eval';
import type { ConfigurationBundleVersionItem } from '@/lib/types';

export const dynamic = 'force-dynamic';

export async function GET(request: Request, { params }: { params: { id: string } }) {
  return handle(async () => {
    await requireManager(request);
    const bundleId = decodeURIComponent(params.id);

    const versions: ConfigurationBundleVersionItem[] = [];
    try {
      const client = agentCoreControlClient();
      let nextToken: string | undefined;
      do {
        const out = await client.send(
          new ListConfigurationBundleVersionsCommand({ bundleId, nextToken, maxResults: 50 })
        );
        for (const summary of out.versions ?? []) versions.push(mapBundleVersion(summary));
        nextToken = out.nextToken;
      } while (nextToken);
    } catch (error) {
      return upstreamError(error, 'ListConfigurationBundleVersions (Preview)');
    }

    versions.sort((a, b) => (b.version_created_at ?? '').localeCompare(a.version_created_at ?? ''));
    const active = await readActiveBundle();

    return Response.json(
      {
        status: 'ok',
        bundle_id: bundleId,
        versions,
        // 이 bundle 이 활성인 경우의 활성 버전 (다른 bundle 이 활성이면 null).
        active_version_id: active?.bundleId === bundleId ? active.versionId : null,
      },
      { status: 200 }
    );
  });
}

export async function POST(request: Request, { params }: { params: { id: string } }) {
  return handle(async () => {
    const principal = await requireManager(request);
    const bundleId = decodeURIComponent(params.id);
    const body = await readJson(request);

    const systemPrompt = typeof body.systemPrompt === 'string' ? body.systemPrompt : '';
    const modelId = typeof body.modelId === 'string' ? body.modelId.trim() : '';
    const commitMessage =
      typeof body.commitMessage === 'string' && body.commitMessage.trim()
        ? body.commitMessage.trim()
        : 'admin panel 편집';
    const parentVersionId =
      typeof body.parentVersionId === 'string' && body.parentVersionId.trim()
        ? body.parentVersionId.trim()
        : undefined;

    if (!systemPrompt.trim()) return jsonError('systemPrompt 가 필요합니다', 400);
    if (!modelId) return jsonError('modelId 가 필요합니다', 400);

    try {
      const out = await agentCoreControlClient().send(
        new UpdateConfigurationBundleCommand({
          bundleId,
          components: {
            [CONFIG_BUNDLE_COMPONENT_KEY]: {
              configuration: { system_prompt: systemPrompt, model_id: modelId },
            },
          },
          ...(parentVersionId ? { parentVersionIds: [parentVersionId] } : {}),
          commitMessage,
          createdBy: { name: principal.username },
        })
      );
      return Response.json(
        {
          status: 'ok',
          bundle_id: out.bundleId,
          bundle_arn: out.bundleArn,
          version_id: out.versionId,
          updated_at: isoOrUndefined(out.updatedAt),
        },
        { status: 201 }
      );
    } catch (error) {
      return upstreamError(error, 'UpdateConfigurationBundle (Preview)');
    }
  });
}
