'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.


import { useState, useTransition } from 'react';
import type { ModelListItem } from '@/types/entities';
import { activateModelAction } from '@/lib/actions/models';
import { useToast } from '@/components/common/ToastProvider';
import { Badge, type BadgeTone } from '@/components/common/Badge';
import { Table, THead, TBody, Tr, Th, Td, TEmpty } from '@/components/common/Table';
import { CreateModelDialog } from './CreateModelDialog';
import { DeactivateModelDialog } from './DeactivateModelDialog';

interface ModelsTableProps {
  models: ModelListItem[];
}

function ProviderBadge({ provider }: { provider: string }) {
  const toneMap: Record<string, BadgeTone> = {
    bedrock: 'sky',
    'on-prem': 'pink',
    bedrock_mantle: 'amber',       // Cowork → Mantle Opus (Tokyo)
    bedrock_mantle_openai: 'teal', // Codex → Mantle GPT-5.5 (Ohio)
  };
  return <Badge tone={toneMap[provider.toLowerCase()] ?? 'neutral'}>{provider}</Badge>;
}

function StatusBadge({ isActive }: { isActive: boolean }) {
  return <Badge tone={isActive ? 'teal' : 'neutral'}>{isActive ? '활성' : '비활성'}</Badge>;
}

function formatNumber(n: number): string {
  return new Intl.NumberFormat('ko-KR').format(n);
}

export function ModelsTable({ models }: ModelsTableProps) {
  const { toast } = useToast();
  const [isPending, startTransition] = useTransition();
  const [selectedModel, setSelectedModel] = useState<ModelListItem | null>(null);
  const [editDialogOpen, setEditDialogOpen] = useState(false);
  const [deactivateDialogOpen, setDeactivateDialogOpen] = useState(false);

  const handleEdit = (model: ModelListItem) => {
    setSelectedModel(model);
    setEditDialogOpen(true);
  };

  const handleDeactivate = (model: ModelListItem) => {
    setSelectedModel(model);
    setDeactivateDialogOpen(true);
  };

  const handleActivate = (model: ModelListItem) => {
    startTransition(async () => {
      const result = await activateModelAction(model.alias);
      if (result.success) {
        toast({
          type: 'success',
          message: `${model.alias} 모델이 활성화되었습니다.`,
          auto_dismiss_ms: 3000,
        });
      } else {
        toast({
          type: 'error',
          message: result.error,
          auto_dismiss_ms: 5000,
        });
      }
    });
  };

  return (
    <>
      <div className="w-full glass rounded-apple overflow-hidden">
        <Table>
          <THead>
            <Tr>
              <Th>Alias</Th>
              <Th>표시 이름</Th>
              <Th>Provider</Th>
              <Th>Model ID</Th>
              <Th numeric>입력단가</Th>
              <Th numeric>출력단가</Th>
              <Th numeric>캐시 5min</Th>
              <Th numeric>캐시 1h</Th>
              <Th numeric>캐시 읽기</Th>
              <Th>상태</Th>
              <Th>액션</Th>
            </Tr>
          </THead>
          <TBody>
            {models.length === 0 ? (
              <TEmpty colSpan={11}>등록된 모델이 없습니다.</TEmpty>
            ) : (
              models.map((model) => (
                <Tr key={model.alias}>
                  <Td
                    emphasis
                    className={`font-mono mono-id text-xs${!model.is_active ? ' text-muted-foreground' : ''}`}
                  >
                    {model.alias}
                  </Td>
                  <Td className={!model.is_active ? 'text-muted-foreground' : ''}>
                    {model.display_name ?? <span className="text-muted-foreground">—</span>}
                  </Td>
                  <Td>
                    <ProviderBadge provider={model.provider} />
                  </Td>
                  <Td className="text-muted-foreground font-mono mono-id text-xs">{model.model_id}</Td>
                  <Td numeric>${model.input_price_per_1k.toFixed(4)}/1K</Td>
                  <Td numeric>${model.output_price_per_1k.toFixed(4)}/1K</Td>
                  <Td numeric className="text-muted-foreground">
                    {model.cache_creation_5m_price_per_1k > 0
                      ? `$${model.cache_creation_5m_price_per_1k.toFixed(5)}/1K`
                      : '—'}
                  </Td>
                  <Td numeric className="text-muted-foreground">
                    {model.cache_creation_1h_price_per_1k > 0
                      ? `$${model.cache_creation_1h_price_per_1k.toFixed(5)}/1K`
                      : '—'}
                  </Td>
                  <Td numeric className="text-muted-foreground">
                    {model.cache_read_price_per_1k > 0
                      ? `$${model.cache_read_price_per_1k.toFixed(5)}/1K`
                      : '—'}
                  </Td>
                  <Td>
                    <StatusBadge isActive={model.is_active} />
                  </Td>
                  <Td>
                    <div className="flex items-center gap-2">
                      <button
                        onClick={() => handleEdit(model)}
                        className="inline-flex items-center justify-center rounded-md border border-border bg-background px-3 py-1.5 text-xs font-medium hover:bg-accent transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                      >
                        수정
                      </button>
                      {model.is_active ? (
                        <button
                          onClick={() => handleDeactivate(model)}
                          className="inline-flex items-center justify-center rounded-md border border-destructive/30 bg-background px-3 py-1.5 text-xs font-medium text-destructive hover:bg-destructive/10 transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                        >
                          비활성화
                        </button>
                      ) : (
                        <button
                          onClick={() => handleActivate(model)}
                          disabled={isPending}
                          className="inline-flex items-center justify-center rounded-md border border-primary/40 bg-background px-3 py-1.5 text-xs font-medium text-primary hover:bg-primary/10 transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:opacity-50"
                        >
                          활성화
                        </button>
                      )}
                    </div>
                  </Td>
                </Tr>
              ))
            )}
          </TBody>
        </Table>
      </div>

      <CreateModelDialog
        isOpen={editDialogOpen}
        onClose={() => {
          setEditDialogOpen(false);
          setSelectedModel(null);
        }}
        editModel={selectedModel ?? undefined}
      />

      <DeactivateModelDialog
        isOpen={deactivateDialogOpen}
        onClose={() => {
          setDeactivateDialogOpen(false);
          setSelectedModel(null);
        }}
        model={selectedModel}
      />
    </>
  );
}