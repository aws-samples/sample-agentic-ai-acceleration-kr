// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * 본문 로깅 토글 — **켜는 쪽**이 보호돼 있는지.
 *
 * 왜 이 파일이 있나
 * -----------------
 * 이 스위치를 켜면 게이트웨이가 요청·응답 본문을 **마스킹 없이** S3 로 보낸다. 즉
 * 사용자가 프롬프트에 붙여 넣은 것이 durable 저장소로 나간다. UI 실수의 비용이
 * 비대칭이라(켜는 실수는 되돌릴 수 없고, 끄는 실수는 데이터가 조금 비는 것) 다음
 * 세 가지를 구조로 못 박는다:
 *
 *   1. **양방향 확인.** 원본 구현은 OFF 로 내릴 때만 물었다 — 위험한 방향은 그
 *      반대다. 확인이 안전한 액션에만 붙어 있으면 정확히 거꾸로 붙어 있는 것이다.
 *   2. **켜는 확인은 파괴적 스타일.** 빨간 버튼이 "지금 되돌릴 수 없는 일을 한다" 를
 *      전달하는 유일한 시각 신호다.
 *   3. **실패하면 스위치가 움직이지 않는다.** 낙관적으로 먼저 뒤집으면 화면은 ON 인데
 *      실제로는 꺼진 채라, 운영자가 수집되고 있다고 믿는 최악의 오답이 된다.
 */

import { readFileSync } from 'node:fs';
import { basename, resolve } from 'node:path';

import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi, beforeEach } from 'vitest';

const setBodyLoggingAction = vi.fn();

vi.mock('@/lib/actions/settings', () => ({
  setBodyLoggingAction: (enabled: boolean) => setBodyLoggingAction(enabled),
}));

const toast = vi.fn();
vi.mock('@/components/common/ToastProvider', () => ({
  useToast: () => ({ toast }),
}));

vi.mock('next-intl', () => ({
  // 키를 그대로 돌려준다 — 문구가 아니라 **어느 키**가 쓰였는지를 검증한다.
  useTranslations: () => (key: string) => key,
}));

const { BodyLoggingToggle } = await import('@/components/monitoring/BodyLoggingToggle');

function uiRoot(): string {
  const cwd = process.cwd();
  if (basename(cwd) !== 'admin-ui') {
    throw new Error(`cwd 가 admin-ui 가 아니다(${cwd})`);
  }
  return cwd;
}

function read(rel: string): string {
  const text = readFileSync(resolve(uiRoot(), rel), 'utf-8');
  expect(text.length).toBeGreaterThan(100); // 대조군 — 경로 오타를 일치로 오판하지 않는다
  return text;
}

beforeEach(() => {
  setBodyLoggingAction.mockReset();
  toast.mockReset();
});

describe('양방향 확인', () => {
  it('OFF → ON 클릭은 즉시 적용하지 않고 확인을 띄운다', async () => {
    render(<BodyLoggingToggle initialEnabled={false} />);
    await userEvent.click(screen.getByRole('switch'));
    // ⚠️ 이 단정이 이 파일의 핵심이다. 확인 없이 켜지면 클릭 한 번에 사용자 프롬프트
    //    수집이 시작된다.
    expect(setBodyLoggingAction).not.toHaveBeenCalled();
    expect(screen.getByText('confirmOnTitle')).toBeTruthy();
  });

  it('ON → OFF 클릭도 확인을 띄운다 (그 구간 감사 데이터가 영구히 없어진다)', async () => {
    render(<BodyLoggingToggle initialEnabled />);
    await userEvent.click(screen.getByRole('switch'));
    expect(setBodyLoggingAction).not.toHaveBeenCalled();
    expect(screen.getByText('confirmOffTitle')).toBeTruthy();
  });

  it('켜는 확인과 끄는 확인의 문구 키가 서로 다르다', async () => {
    // 같은 키를 쓰면 "끄시겠습니까?" 가 켜는 순간에 떠서 경고가 무의미해진다.
    const { unmount } = render(<BodyLoggingToggle initialEnabled={false} />);
    await userEvent.click(screen.getByRole('switch'));
    expect(screen.queryByText('confirmOffTitle')).toBeNull();
    unmount();

    render(<BodyLoggingToggle initialEnabled />);
    await userEvent.click(screen.getByRole('switch'));
    expect(screen.queryByText('confirmOnTitle')).toBeNull();
  });

  it('확인을 누르면 그 목표 상태로 호출한다', async () => {
    setBodyLoggingAction.mockResolvedValue({ success: true, data: { enabled: true } });
    render(<BodyLoggingToggle initialEnabled={false} />);
    await userEvent.click(screen.getByRole('switch'));
    await userEvent.click(screen.getByText('confirmOnLabel'));
    await waitFor(() => expect(setBodyLoggingAction).toHaveBeenCalledWith(true));
  });

  it('취소하면 아무 일도 일어나지 않는다', async () => {
    render(<BodyLoggingToggle initialEnabled={false} />);
    await userEvent.click(screen.getByRole('switch'));
    await userEvent.keyboard('{Escape}');
    await waitFor(() => expect(screen.queryByText('confirmOnTitle')).toBeNull());
    expect(setBodyLoggingAction).not.toHaveBeenCalled();
    expect(screen.getByRole('switch').getAttribute('aria-checked')).toBe('false');
  });
});

describe('실패 처리', () => {
  it('액션이 실패하면 스위치가 움직이지 않는다', async () => {
    setBodyLoggingAction.mockResolvedValue({ success: false, error: '403 Forbidden' });
    render(<BodyLoggingToggle initialEnabled={false} />);
    await userEvent.click(screen.getByRole('switch'));
    await userEvent.click(screen.getByText('confirmOnLabel'));
    await waitFor(() => expect(toast).toHaveBeenCalled());
    // ⚠️ 여기서 'true' 가 되면 화면은 수집 중이라고 말하는데 실제로는 꺼져 있다.
    expect(screen.getByRole('switch').getAttribute('aria-checked')).toBe('false');
    expect(toast.mock.calls[0][0].type).toBe('error');
  });

  it('성공하면 **서버가 돌려준** 값으로 맞춘다 (요청한 값이 아니라)', async () => {
    // 서버가 거부하거나 다른 값으로 정착했을 때 화면이 거짓말하지 않게 한다.
    setBodyLoggingAction.mockResolvedValue({ success: true, data: { enabled: false } });
    render(<BodyLoggingToggle initialEnabled={false} />);
    await userEvent.click(screen.getByRole('switch'));
    await userEvent.click(screen.getByText('confirmOnLabel'));
    await waitFor(() => expect(setBodyLoggingAction).toHaveBeenCalled());
    expect(screen.getByRole('switch').getAttribute('aria-checked')).toBe('false');
  });
});

describe('켜져 있는 동안의 경고', () => {
  it('ON 이면 상시 경고를 렌더한다', () => {
    render(<BodyLoggingToggle initialEnabled />);
    expect(screen.getByText('activeWarning')).toBeTruthy();
  });

  it('OFF 면 경고가 없다 (대조군)', () => {
    render(<BodyLoggingToggle initialEnabled={false} />);
    expect(screen.queryByText('activeWarning')).toBeNull();
  });
});

describe('배선', () => {
  it('켜는 확인이 파괴적 스타일이다', () => {
    const src = read('src/components/monitoring/BodyLoggingToggle.tsx');
    // isDestructive 가 turningOn 에 묶여 있어야 한다. 상수 true/false 로 두면
    // 두 방향이 같은 색이 되어 위험한 방향이 구분되지 않는다.
    expect(src).toMatch(/isDestructive=\{turningOn\}/);
  });

  it('/monitoring 페이지가 토글을 렌더한다', () => {
    const src = read('src/app/monitoring/page.tsx');
    expect(src).toContain('BodyLoggingToggle');
    expect(src).toContain('getBodyLoggingAction');
  });

  it('읽기 실패 시 OFF 로 렌더한다 — 모를 때 ON 으로 보이면 안 된다', () => {
    const src = read('src/app/monitoring/page.tsx');
    expect(src).toMatch(/result\.success \?\s*result\.data\.enabled\s*:\s*false/);
  });

  it('쓰기 액션은 재시도하지 않는다 — 감사 로그가 중복된다', () => {
    const src = read('src/lib/actions/settings.ts');
    // GET 은 withRetry, PUT 은 직접 호출이어야 한다.
    expect(src).toMatch(/withRetry\(\(\) =>\s*\n?\s*adminAPI\.get/);
    const putIdx = src.indexOf('adminAPI.put');
    expect(putIdx).toBeGreaterThan(-1);
    const before = src.slice(Math.max(0, putIdx - 120), putIdx);
    expect(before).not.toContain('withRetry');
  });

  it('쓰기 액션이 입력을 boolean 으로 검증한다', () => {
    const src = read('src/lib/actions/settings.ts');
    // 서버 액션은 임의 페이로드를 받을 수 있다. 문자열 "false" 는 truthy 다.
    expect(src).toMatch(/z\.boolean\(\)/);
    expect(src).toMatch(/\.parse\(enabled\)/);
  });

  it('두 로케일에 컴포넌트가 쓰는 모든 키가 있다', () => {
    const src = read('src/components/monitoring/BodyLoggingToggle.tsx');
    const keys = [...src.matchAll(/t\('([a-zA-Z]+)'\)/g)].map((m) => m[1]);
    expect(keys.length, 't() 호출을 찾지 못했다 — 이 검사가 공허하다').toBeGreaterThan(8);
    for (const locale of ['ko', 'en']) {
      const msgs = JSON.parse(read(`messages/${locale}.json`)) as Record<string, unknown>;
      const ns = (msgs['monitoring'] as Record<string, Record<string, string>>)['bodyLogging'];
      expect(ns, `${locale}: monitoring.bodyLogging 네임스페이스가 없다`).toBeTruthy();
      const missing = [...new Set(keys)].filter((k) => !(k in ns));
      expect(missing, `${locale} 에 없는 키: ${missing.join(', ')}`).toEqual([]);
    }
  });

  it('두 로케일의 키 집합이 동일하다', () => {
    const sets = ['ko', 'en'].map((locale) => {
      const msgs = JSON.parse(read(`messages/${locale}.json`)) as Record<string, unknown>;
      const ns = (msgs['monitoring'] as Record<string, Record<string, string>>)['bodyLogging'];
      return Object.keys(ns).sort();
    });
    expect(sets[0]).toEqual(sets[1]);
  });

  it('경고 문구가 마스킹하지 않는다는 사실을 실제로 말한다', () => {
    // ⚠️ 이 단정은 문구 취향이 아니다. 현재 구현은 마스킹하지 않으며, 그 사실을
    //    운영자에게 알리지 않으면 켜는 결정이 잘못된 전제 위에서 이뤄진다.
    for (const locale of ['ko', 'en']) {
      const msgs = JSON.parse(read(`messages/${locale}.json`)) as Record<string, unknown>;
      const ns = (msgs['monitoring'] as Record<string, Record<string, string>>)['bodyLogging'];
      const text = `${ns.description} ${ns.activeWarning} ${ns.confirmOnMessage}`;
      expect(text).toMatch(locale === 'ko' ? /마스킹/ : /mask/i);
    }
  });
});
