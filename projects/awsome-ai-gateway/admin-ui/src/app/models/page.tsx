// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { adminAPI } from '@/lib/api-client';
import type { ModelListItem } from '@/types/entities';
import { ModelsTable } from '@/components/models/ModelsTable';
import { CreateModelButton } from '@/components/models/CreateModelButton';
import { PriceSyncButton } from '@/components/models/PriceSyncButton';
import { TeamModelPermissionPanel } from '@/components/models/TeamModelPermissionPanel';
import { WebSearchTogglePanel } from '@/components/models/WebSearchTogglePanel';
import type { RoutingProfileItem } from '@/lib/actions/routing';

interface APIModelItem {
  alias: string;
  provider: string;
  provider_model_id: string;
  endpoint_url: string | null;
  status: string;
  description: string | null;
  display_name: string | null;
  current_pricing: {
    input_price_per_1k_tokens: string;
    output_price_per_1k_tokens: string;
    cache_creation_5m_price_per_1k_tokens?: string;
    cache_creation_1h_price_per_1k_tokens?: string;
    cache_read_price_per_1k_tokens?: string;
  } | null;
}

interface APITeamItem {
  id: string;
  name: string;
  member_count?: number;
}

function mapToModelListItem(item: APIModelItem): ModelListItem {
  const p = item.current_pricing;
  return {
    alias: item.alias,
    provider: item.provider,
    model_id: item.provider_model_id,
    endpoint_url: item.endpoint_url ?? null,
    is_active: item.status === 'ACTIVE',
    input_price_per_1k: p ? parseFloat(p.input_price_per_1k_tokens) : 0,
    output_price_per_1k: p ? parseFloat(p.output_price_per_1k_tokens) : 0,
    cache_creation_5m_price_per_1k: p?.cache_creation_5m_price_per_1k_tokens
      ? parseFloat(p.cache_creation_5m_price_per_1k_tokens)
      : 0,
    cache_creation_1h_price_per_1k: p?.cache_creation_1h_price_per_1k_tokens
      ? parseFloat(p.cache_creation_1h_price_per_1k_tokens)
      : 0,
    cache_read_price_per_1k: p?.cache_read_price_per_1k_tokens
      ? parseFloat(p.cache_read_price_per_1k_tokens)
      : 0,
    max_tokens: 0,
    context_window: 0,
    description: item.description,
    display_name: item.display_name,
  };
}

export default async function ModelsPage() {
  const [modelsRes, teamsRes, routingRes] = await Promise.allSettled([
    adminAPI.get<{ items: APIModelItem[] }>('/admin/models'),
    adminAPI.get<{ items: APITeamItem[] }>('/admin/users/teams'),
    adminAPI.get<{ items: RoutingProfileItem[] }>('/admin/routing-profiles'),
  ]);

  const models = (modelsRes.status === 'fulfilled' && modelsRes.value?.items ? modelsRes.value.items : []).map(mapToModelListItem);
  const allTeams = teamsRes.status === 'fulfilled' && teamsRes.value?.items ? teamsRes.value.items : [];
  const routingProfiles = routingRes.status === 'fulfilled' && routingRes.value?.items ? routingRes.value.items : [];
  const teams = allTeams
    .filter(t => (t.member_count ?? 0) > 0)
    .map(t => ({ id: t.id, name: t.name }));

  return (
    <div className="space-y-8">
      <div>
        <div className="flex items-center justify-between mb-6">
          <h1 className="text-2xl font-bold">모델 관리</h1>
          <div className="flex items-center gap-2">
            <PriceSyncButton />
            <CreateModelButton />
          </div>
        </div>
        <ModelsTable models={models} />
      </div>

      <div>
        <h2 className="text-lg font-semibold mb-4">팀별 모델 접근 관리</h2>
        <TeamModelPermissionPanel
          teams={teams}
          allTeams={allTeams.map(t => ({ id: t.id, name: t.name }))}
          models={models}
        />
      </div>

      <div>
        <h2 className="text-lg font-semibold mb-4">앱별 웹서치 허용</h2>
        <WebSearchTogglePanel initial={routingProfiles} />
      </div>
    </div>
  );
}
