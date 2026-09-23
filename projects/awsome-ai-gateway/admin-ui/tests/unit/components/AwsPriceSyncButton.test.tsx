// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * AwsPriceSyncButton — AWS 단가 자동연동 다이얼로그 (fetch ≠ apply).
 *
 * 고정하는 성질:
 *  1. preview 는 열 때 자동 호출되고, drift(변경 있는 matched) 행만 기본 선택된다.
 *  2. 반영은 명시적 — syncAwsPricingAction 에 **선택된 alias 만** 넘어간다(버튼 하나로 전부
 *     덮어쓰지 않는다). 가격은 과금·차단이라 자동 반영 금지의 UI 측 계약이다.
 *  3. matched 이지만 변경 없는 행은 기본 선택에서 빠지고, unmatched(AWS 소스 없음) 행은
 *     반영 대상이 아니다.
 */

import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, vi, beforeEach } from 'vitest';

vi.mock('next-intl', () => ({
  useTranslations: () => (key: string, values?: Record<string, unknown>) =>
    values ? `${key}:${Object.values(values).join(',')}` : key,
}));

const refresh = vi.fn();
vi.mock('next/navigation', () => ({ useRouter: () => ({ refresh }) }));

const previewAwsPricingAction = vi.fn();
const syncAwsPricingAction = vi.fn();
vi.mock('@/lib/actions/models', () => ({
  previewAwsPricingAction: (...a: unknown[]) => previewAwsPricingAction(...a),
  syncAwsPricingAction: (...a: unknown[]) => syncAwsPricingAction(...a),
}));

import { AwsPriceSyncButton } from '@/components/models/AwsPriceSyncButton';

const PREVIEW = {
  success: true as const,
  data: {
    region_code: 'us-east-1',
    items: [
      {
        alias: 'gpt-5.6-terra',
        provider_model_id: 'us.openai.gpt-5.6-terra',
        matched: true,
        aws_region_code: 'us-gov-west-1',
        aws_endpoint: 'mantle',
        changes: [{ field: 'input_price_per_1k_tokens', current: '0.002000', aws: '0.002640' }],
        note: '',
      },
      {
        alias: 'claude-sonnet',
        provider_model_id: 'anthropic.claude-3-5-sonnet',
        matched: false,
        aws_region_code: null,
        aws_endpoint: null,
        changes: [],
        note: 'no AWS source',
      },
    ],
  },
};

describe('AwsPriceSyncButton', () => {
  beforeEach(() => {
    previewAwsPricingAction.mockReset();
    syncAwsPricingAction.mockReset();
    refresh.mockReset();
    previewAwsPricingAction.mockResolvedValue(PREVIEW);
    syncAwsPricingAction.mockResolvedValue({ success: true, data: { synced: ['gpt-5.6-terra'], skipped: [] } });
  });

  it('opens, auto-loads preview, and preselects only drifted matched rows', async () => {
    const user = userEvent.setup();
    render(<AwsPriceSyncButton />);
    await user.click(screen.getByText('button'));

    await waitFor(() => expect(previewAwsPricingAction).toHaveBeenCalledWith('us-east-1'));
    const checkbox = await screen.findByRole('checkbox');
    expect(checkbox).toBeChecked();
    expect(screen.getByText('gpt-5.6-terra')).toBeInTheDocument();
    // unmatched 는 요약 라인에 표시(반영 대상 아님) — 체크박스는 하나뿐이어야 한다
    expect(screen.getAllByRole('checkbox')).toHaveLength(1);
  });

  it('applies only selected aliases and refreshes', async () => {
    const user = userEvent.setup();
    render(<AwsPriceSyncButton />);
    await user.click(screen.getByText('button'));
    await screen.findByRole('checkbox');

    // apply 버튼 라벨은 apply:{count} — 선택 1건
    await user.click(screen.getByText('apply:1'));

    await waitFor(() =>
      expect(syncAwsPricingAction).toHaveBeenCalledWith(['gpt-5.6-terra'], 'us-east-1')
    );
    expect(refresh).toHaveBeenCalled();
  });

  it('does not preselect a matched row that has no changes', async () => {
    previewAwsPricingAction.mockResolvedValue({
      success: true,
      data: {
        region_code: 'us-east-1',
        items: [
          {
            alias: 'in-sync-model',
            provider_model_id: 'openai.gpt-5.6-luna',
            matched: true,
            aws_region_code: 'us-gov-west-1',
            aws_endpoint: 'mantle',
            changes: [],
            note: '',
          },
        ],
      },
    });
    const user = userEvent.setup();
    render(<AwsPriceSyncButton />);
    await user.click(screen.getByText('button'));

    await waitFor(() => expect(previewAwsPricingAction).toHaveBeenCalled());
    // 변경 없는 행은 체크박스 자체가 없고(drift 목록에서 빠짐), noDrift 안내가 뜬다
    expect(screen.queryByRole('checkbox')).toBeNull();
    expect(screen.getByText('noDrift')).toBeInTheDocument();
    // 선택 0건이면 apply 버튼은 disabled (apply:0)
    expect(screen.getByText('apply:0')).toBeDisabled();
  });
});
