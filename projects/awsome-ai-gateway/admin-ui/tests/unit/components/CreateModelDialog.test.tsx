// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * 모델 추가 다이얼로그 — 검증 실패 원인이 화면에 반드시 보여야 한다.
 *
 * 배경(실제 고객 장애): 스키마가 폼에 없는 키(max_tokens/context_window)를 required 로 요구해
 * 등록이 항상 실패했는데, 그 키에는 대응 <input> 이 없어 fieldErrors 가 렌더되지 못했다.
 * 사용자는 원인 없는 "Validation failed" 만 보게 되어 자력 진단이 불가능했다.
 *
 * 여기서는 서버 액션을 대역으로 바꿔 "폼에 없는 키의 필드 에러" 를 반환시키고,
 * 그 키 이름이 화면 텍스트에 노출되는지 확인한다.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { CreateModelDialog } from '@/components/models/CreateModelDialog';

const createModelAction = vi.fn();
const updateModelAction = vi.fn();

vi.mock('@/lib/actions/models', () => ({
  createModelAction: (...a: unknown[]) => createModelAction(...a),
  updateModelAction: (...a: unknown[]) => updateModelAction(...a),
}));

// next-intl 은 메시지 provider 없이는 throw 한다 — 키를 그대로 돌려주는 대역으로 대체.
vi.mock('next-intl', () => ({
  useTranslations: () => (key: string) => key,
}));

const toast = vi.fn();
vi.mock('@/components/common/ToastProvider', () => ({
  useToast: () => ({ toast }),
}));

/** 필수 입력을 채워 submit 이 브라우저 required 검증에 막히지 않게 한다. */
function fillRequiredFields() {
  fireEvent.change(screen.getByLabelText(/Alias/), {
    target: { name: 'alias', value: 'codex-gpt-5.6-sol' },
  });
  fireEvent.change(screen.getByLabelText(/Provider/), {
    target: { name: 'provider', value: 'BEDROCK_MANTLE_OPENAI' },
  });
  fireEvent.change(screen.getByLabelText(/Model ID/), {
    target: { name: 'model_id', value: 'openai.gpt-5.6-sol' },
  });
  fireEvent.change(screen.getByLabelText('priceInput'), {
    target: { name: 'input_price_per_1k', value: '0.011' },
  });
  fireEvent.change(screen.getByLabelText('priceOutput'), {
    target: { name: 'output_price_per_1k', value: '0.0495' },
  });
}

describe('CreateModelDialog — 검증 실패 관측성', () => {
  beforeEach(() => {
    createModelAction.mockReset();
    updateModelAction.mockReset();
    toast.mockReset();
  });

  it('폼에 입력란이 없는 키의 필드 에러도 화면에 노출한다 (회귀: 원인 없는 Validation failed)', async () => {
    createModelAction.mockResolvedValue({
      success: false,
      error: 'Validation failed',
      // 폼에 대응 <input> 이 없는 키 — 과거에는 조용히 버려졌다.
      fieldErrors: { max_tokens: 'Required', context_window: 'Required' },
    });

    render(<CreateModelDialog isOpen onClose={() => {}} />);
    fillRequiredFields();
    fireEvent.submit(screen.getByRole('button', { name: 'register' }).closest('form')!);

    await waitFor(() => {
      const alerts = screen.getAllByRole('alert').map((n) => n.textContent ?? '');
      const joined = alerts.join(' | ');
      expect(joined).toContain('Validation failed');
      // 핵심: 원인 키가 화면에 보여야 한다.
      expect(joined).toContain('max_tokens');
      expect(joined).toContain('context_window');
    });
  });

  it('폼에 입력란이 있는 키는 해당 필드 아래에만 표시하고 상단 메시지를 오염시키지 않는다', async () => {
    createModelAction.mockResolvedValue({
      success: false,
      error: 'Validation failed',
      fieldErrors: { alias: 'Alias is required' },
    });

    render(<CreateModelDialog isOpen onClose={() => {}} />);
    fillRequiredFields();
    fireEvent.submit(screen.getByRole('button', { name: 'register' }).closest('form')!);

    await waitFor(() => {
      const joined = screen.getAllByRole('alert').map((n) => n.textContent ?? '').join(' | ');
      expect(joined).toContain('Alias is required');
      // alias 는 대응 input 이 있으므로 상단 요약에 키 이름을 덧붙이지 않는다.
      expect(joined).not.toContain('alias (');
    });
  });

  it('성공하면 토스트를 띄우고 닫는다', async () => {
    createModelAction.mockResolvedValue({ success: true, data: undefined });
    const onClose = vi.fn();

    render(<CreateModelDialog isOpen onClose={onClose} />);
    fillRequiredFields();
    fireEvent.submit(screen.getByRole('button', { name: 'register' }).closest('form')!);

    await waitFor(() => {
      expect(toast).toHaveBeenCalledWith(
        expect.objectContaining({ type: 'success' })
      );
      expect(onClose).toHaveBeenCalled();
    });
  });

  it('전송 payload 에 폼에 없는 필드를 넣지 않는다', async () => {
    createModelAction.mockResolvedValue({ success: true, data: undefined });

    render(<CreateModelDialog isOpen onClose={() => {}} />);
    fillRequiredFields();
    fireEvent.submit(screen.getByRole('button', { name: 'register' }).closest('form')!);

    await waitFor(() => expect(createModelAction).toHaveBeenCalled());
    const payload = createModelAction.mock.calls[0][0] as Record<string, unknown>;
    expect(payload).not.toHaveProperty('max_tokens');
    expect(payload).not.toHaveProperty('context_window');
    expect(payload.alias).toBe('codex-gpt-5.6-sol');
    expect(payload.provider).toBe('BEDROCK_MANTLE_OPENAI');
  });
});
