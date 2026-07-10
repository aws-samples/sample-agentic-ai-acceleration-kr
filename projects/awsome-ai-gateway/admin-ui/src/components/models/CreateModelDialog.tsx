'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.


import { useState, useTransition, useEffect } from 'react';
import { X } from 'lucide-react';
import type { ModelListItem } from '@/types/entities';
import { createModelAction, updateModelAction } from '@/lib/actions/models';
import { FormError } from '@/components/common/FormError';
import { SpinnerButton } from '@/components/common/SpinnerButton';
import { useToast } from '@/components/common/ToastProvider';

interface CreateModelDialogProps {
  isOpen: boolean;
  onClose: () => void;
  editModel?: ModelListItem;
}

interface FormState {
  alias: string;
  provider: string;
  model_id: string;
  endpoint_url: string;
  input_price_per_1k: string;
  output_price_per_1k: string;
  cache_creation_5m_price_per_1k: string;
  cache_creation_1h_price_per_1k: string;
  cache_read_price_per_1k: string;
  description: string;
  display_name: string;
}

function getInitialState(editModel?: ModelListItem): FormState {
  if (editModel) {
    return {
      alias: editModel.alias,
      provider: editModel.provider,
      model_id: editModel.model_id,
      endpoint_url: editModel.endpoint_url ?? '',
      input_price_per_1k: editModel.input_price_per_1k.toString(),
      output_price_per_1k: editModel.output_price_per_1k.toString(),
      cache_creation_5m_price_per_1k: editModel.cache_creation_5m_price_per_1k.toString(),
      cache_creation_1h_price_per_1k: editModel.cache_creation_1h_price_per_1k.toString(),
      cache_read_price_per_1k: editModel.cache_read_price_per_1k.toString(),
      description: editModel.description ?? '',
      display_name: editModel.display_name ?? '',
    };
  }
  return {
    alias: '',
    provider: '',
    model_id: '',
    endpoint_url: '',
    input_price_per_1k: '',
    output_price_per_1k: '',
    cache_creation_5m_price_per_1k: '0',
    cache_creation_1h_price_per_1k: '0',
    cache_read_price_per_1k: '0',
    description: '',
    display_name: '',
  };
}

export function CreateModelDialog({ isOpen, onClose, editModel }: CreateModelDialogProps) {
  const { toast } = useToast();
  const [isPending, startTransition] = useTransition();
  const [error, setError] = useState<string | null>(null);
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [form, setForm] = useState<FormState>(() => getInitialState(editModel));

  const isEditMode = !!editModel;

  useEffect(() => {
    if (editModel) {
      setForm(getInitialState(editModel));
    }
  }, [editModel]);

  const handleChange = (
    e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>
  ) => {
    const { name, value } = e.target;
    setForm((prev) => ({ ...prev, [name]: value }));
    if (fieldErrors[name]) {
      setFieldErrors((prev) => {
        const next = { ...prev };
        delete next[name];
        return next;
      });
    }
  };

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setFieldErrors({});

    const payload = {
      alias: form.alias,
      provider: form.provider,
      model_id: form.model_id,
      endpoint_url: form.endpoint_url,
      input_price_per_1k: parseFloat(form.input_price_per_1k),
      output_price_per_1k: parseFloat(form.output_price_per_1k),
      cache_creation_5m_price_per_1k: parseFloat(form.cache_creation_5m_price_per_1k || '0'),
      cache_creation_1h_price_per_1k: parseFloat(form.cache_creation_1h_price_per_1k || '0'),
      cache_read_price_per_1k: parseFloat(form.cache_read_price_per_1k || '0'),
      description: form.description || undefined,
      display_name: form.display_name || undefined,
    };

    startTransition(async () => {
      const result = isEditMode
        ? await updateModelAction(editModel.alias, payload)
        : await createModelAction(payload);

      if (result.success) {
        toast({
          type: 'success',
          message: isEditMode
            ? `${form.alias} 모델이 수정되었습니다.`
            : `${form.alias} 모델이 등록되었습니다.`,
          auto_dismiss_ms: 3000,
        });
        onClose();
      } else {
        setError(result.error);
        if (!result.success && result.fieldErrors) {
          setFieldErrors(result.fieldErrors);
        }
      }
    });
  };

  if (!isOpen) return null;

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
      <div className="bg-background rounded-lg p-6 w-full max-w-lg shadow-xl border border-border max-h-[90vh] overflow-y-auto">
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-lg font-semibold">
            {isEditMode ? '모델 수정' : '모델 추가'}
          </h2>
          <button
            onClick={onClose}
            className="rounded-sm opacity-70 hover:opacity-100 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring transition-opacity"
            aria-label="닫기"
          >
            <X size={16} aria-hidden="true" />
          </button>
        </div>

        <form onSubmit={handleSubmit} className="space-y-4">
          {/* Alias */}
          <div className="space-y-1">
            <label htmlFor="alias" className="text-sm font-medium">
              Alias <span className="text-destructive">*</span>
            </label>
            <input
              id="alias"
              name="alias"
              type="text"
              value={form.alias}
              onChange={handleChange}
              required
              disabled={isEditMode}
              className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:opacity-60 disabled:cursor-not-allowed"
              placeholder="e.g. claude-3-5-sonnet"
            />
            {isEditMode && (
              <p className="text-xs text-muted-foreground">
                Alias 는 여러 테이블에서 참조하는 식별자라 변경할 수 없습니다.
              </p>
            )}
            {fieldErrors.alias && <FormError error={fieldErrors.alias} />}
          </div>

          {/* Provider */}
          <div className="space-y-1">
            <label htmlFor="provider" className="text-sm font-medium">
              Provider <span className="text-destructive">*</span>
            </label>
            <select
              id="provider"
              name="provider"
              value={form.provider}
              onChange={handleChange}
              required
              className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
            >
              <option value="">선택하세요</option>
              <option value="BEDROCK">BEDROCK</option>
              <option value="OPENMODEL">OPENMODEL</option>
              {/* Mantle 계열은 endpoint_url·api_format 이 필요해 보통 마이그레이션으로 시드되지만,
                  기존 cowork-opus / codex-gpt 모델 편집(단가 등) 시 provider 드롭다운이 값과
                  매칭되도록 옵션을 노출한다. */}
              <option value="BEDROCK_MANTLE">BEDROCK_MANTLE (Cowork · Opus)</option>
              <option value="BEDROCK_MANTLE_OPENAI">BEDROCK_MANTLE_OPENAI (Codex · GPT)</option>
            </select>
            {fieldErrors.provider && <FormError error={fieldErrors.provider} />}
          </div>

          {/* Model ID */}
          <div className="space-y-1">
            <label htmlFor="model_id" className="text-sm font-medium">
              Model ID <span className="text-destructive">*</span>
            </label>
            <input
              id="model_id"
              name="model_id"
              type="text"
              value={form.model_id}
              onChange={handleChange}
              required
              className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
              placeholder="e.g. anthropic.claude-3-5-sonnet-20241022-v2:0"
            />
            {fieldErrors.model_id && <FormError error={fieldErrors.model_id} />}
          </div>

          {/* Endpoint URL — OPENMODEL(vLLM) 등 커스텀 엔드포인트 모델에서만 의미있음 */}
          <div className="space-y-1">
            <label htmlFor="endpoint_url" className="text-sm font-medium">
              Endpoint URL <span className="text-muted-foreground text-xs">(선택)</span>
            </label>
            <input
              id="endpoint_url"
              name="endpoint_url"
              type="text"
              value={form.endpoint_url}
              onChange={handleChange}
              className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
              placeholder="예: http://vllm.internal:8000/v1 (Bedrock/Mantle은 비워둠)"
            />
            {fieldErrors.endpoint_url && <FormError error={fieldErrors.endpoint_url} />}
          </div>

          {/* Price fields — 수직 일렬 배치 */}
          <div className="space-y-3">
            <span className="text-sm font-medium">단가 (USD/1K tokens)</span>

            <div className="space-y-1">
              <label htmlFor="input_price_per_1k" className="text-xs text-muted-foreground">입력</label>
              <input
                id="input_price_per_1k"
                name="input_price_per_1k"
                type="number"
                min={0}
                step={0.000001}
                value={form.input_price_per_1k}
                onChange={handleChange}
                required
                className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                placeholder="0.000000"
              />
              {fieldErrors.input_price_per_1k && <FormError error={fieldErrors.input_price_per_1k} />}
            </div>

            <div className="space-y-1">
              <label htmlFor="output_price_per_1k" className="text-xs text-muted-foreground">출력</label>
              <input
                id="output_price_per_1k"
                name="output_price_per_1k"
                type="number"
                min={0}
                step={0.000001}
                value={form.output_price_per_1k}
                onChange={handleChange}
                required
                className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                placeholder="0.000000"
              />
              {fieldErrors.output_price_per_1k && <FormError error={fieldErrors.output_price_per_1k} />}
            </div>

            <div className="space-y-1">
              <label htmlFor="cache_creation_5m_price_per_1k" className="text-xs text-muted-foreground">캐시 생성 5min</label>
              <input
                id="cache_creation_5m_price_per_1k"
                name="cache_creation_5m_price_per_1k"
                type="number"
                min={0}
                step={0.000001}
                value={form.cache_creation_5m_price_per_1k}
                onChange={handleChange}
                className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                placeholder="0.000000"
              />
              {fieldErrors.cache_creation_5m_price_per_1k && <FormError error={fieldErrors.cache_creation_5m_price_per_1k} />}
            </div>

            <div className="space-y-1">
              <label htmlFor="cache_creation_1h_price_per_1k" className="text-xs text-muted-foreground">캐시 생성 1h</label>
              <input
                id="cache_creation_1h_price_per_1k"
                name="cache_creation_1h_price_per_1k"
                type="number"
                min={0}
                step={0.000001}
                value={form.cache_creation_1h_price_per_1k}
                onChange={handleChange}
                className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                placeholder="0.000000"
              />
              {fieldErrors.cache_creation_1h_price_per_1k && <FormError error={fieldErrors.cache_creation_1h_price_per_1k} />}
            </div>

            <div className="space-y-1">
              <label htmlFor="cache_read_price_per_1k" className="text-xs text-muted-foreground">캐시 읽기</label>
              <input
                id="cache_read_price_per_1k"
                name="cache_read_price_per_1k"
                type="number"
                min={0}
                step={0.000001}
                value={form.cache_read_price_per_1k}
                onChange={handleChange}
                className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                placeholder="0.000000"
              />
              {fieldErrors.cache_read_price_per_1k && <FormError error={fieldErrors.cache_read_price_per_1k} />}
            </div>
          </div>

          {/* Description */}
          <div className="space-y-1">
            <label htmlFor="description" className="text-sm font-medium">
              설명 <span className="text-muted-foreground text-xs">(선택)</span>
            </label>
            <textarea
              id="description"
              name="description"
              value={form.description}
              onChange={handleChange}
              rows={3}
              maxLength={512}
              className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring resize-none"
              placeholder="모델에 대한 설명을 입력하세요"
            />
            {fieldErrors.description && <FormError error={fieldErrors.description} />}
          </div>

          {/* Display Name */}
          <div className="space-y-1">
            <label htmlFor="display_name" className="text-sm font-medium">
              표시 이름 (예: Cowork · Opus 4.8)
            </label>
            <input
              id="display_name"
              name="display_name"
              type="text"
              maxLength={128}
              value={form.display_name}
              onChange={handleChange}
              placeholder="미설정 시 alias 로 표시 (등록 시점)"
              className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
            />
            {fieldErrors.display_name && <FormError error={fieldErrors.display_name} />}
          </div>

          <FormError error={error} />

          <div className="flex items-center justify-end gap-3 pt-2">
            <button
              type="button"
              onClick={onClose}
              disabled={isPending}
              className="inline-flex items-center justify-center rounded-md border border-border bg-background px-4 py-2 text-sm font-medium hover:bg-accent transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:opacity-50"
            >
              취소
            </button>
            <SpinnerButton type="submit" isLoading={isPending}>
              {isEditMode ? '수정' : '등록'}
            </SpinnerButton>
          </div>
        </form>
      </div>
    </div>
  );
}