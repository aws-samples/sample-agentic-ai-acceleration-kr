// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

// navigation.spec.ts — smoke navigation across all confirmed main routes.
//
// Routes confirmed from: find src/app -name "page.tsx" (only these paths have page.tsx):
//   /            → dashboard    h1: t('dashboard.title')   ko="대시보드"
//   /keys        → API Keys     h1: t('keys.pageTitle')    ko="API Keys" (same in en)
//   /models      → Model Mgmt   h1: t('models.title')      ko="모델 관리"
//   /budgets     → Budgets      h1: t('budgets.title')     ko="예산 관리"
//   /analytics   → Analytics    h1: t('analytics.pageTitle') ko="분석 (ROI 대시보드)"
//   /users       → Users/Teams  h1: t('users.title')       ko="사용자/팀 관리"
//   /monitoring  → Monitoring   h1: t('monitoring.title')  ko="실시간 모니터링"
//   /rate-limits → Rate Limits  h1: t('rateLimits.title')  ko="Rate Limits" (same in en)
//
// Note: /chat, /my, /cli, /analytics/models routes exist but are not covered here —
//   /chat requires active admin-chat-agent; /my is personal usage (role-scoped);
//   /cli depends on release artifacts being seeded.
//   /403 is an error page, not a main nav route.
//
// Error boundary detection: 기대 h1 이 보이는 것 + 에러 바운더리가 안 떴는지를 함께 본다.
//   이 앱의 바운더리는 src/app/error.tsx 의 h2 "오류가 발생했습니다" 이므로 그것을 판정에 쓴다.
//   Next.js 기본 500 페이지는 heading 정확매칭으로만 확인한다(부분문자열 금지 — 아래 주석 참고).
//
// All headings are asserted against the default locale 'ko'. Since no locale cookie is
//   set by the test, next-intl defaults to 'ko' per i18n/request.ts.

import { test, expect } from '@playwright/test';

const ROUTES = [
  { path: '/',            heading: '대시보드',              level: 1 as const },
  { path: '/keys',        heading: 'API Keys',             level: 1 as const },
  { path: '/models',      heading: '모델 관리',             level: 1 as const },
  { path: '/budgets',     heading: '예산 관리',             level: 1 as const },
  { path: '/analytics',   heading: '분석 (ROI 대시보드)',   level: 1 as const },
  { path: '/users',       heading: '사용자/팀 관리',        level: 1 as const },
  { path: '/monitoring',  heading: '실시간 모니터링',       level: 1 as const },
  { path: '/rate-limits', heading: 'Rate Limits',          level: 1 as const },
] as const;

test.describe('Main route smoke navigation', () => {
  test.beforeEach(async ({ page }) => {
    // dev-login으로 인증 (DEV_LOGIN_ENABLED=true 필요)
    await page.goto('/api/auth/dev-login');
    await page.selectOption('select[name="role"]', 'ADMIN');
    await page.click('button[type="submit"]');
    await page.waitForURL('/');
  });

  for (const { path, heading, level } of ROUTES) {
    test(`${path} renders "${heading}" and has no error boundary`, async ({ page }) => {
      await page.goto(path);

      // The page's real h1 heading must be visible.
      // Some pages use Suspense — allow up to the default Playwright timeout (30s).
      await expect(page.getByRole('heading', { name: heading, level })).toBeVisible();

      // 앱의 실제 에러 바운더리(src/app/error.tsx)는 h2 "오류가 발생했습니다"를 렌더한다.
      // 이것이 라우트가 에러로 넘어갔는지 판정하는 진짜 신호다.
      await expect(
        page.getByRole('heading', { name: '오류가 발생했습니다' })
      ).not.toBeVisible();

      // Next.js 기본 에러 페이지(커스텀 바운더리를 못 타는 경우) 백업 판정.
      // ⚠️ 부분문자열 매칭 금지: getByText('500') 은 정상 예산 금액 "$5000.00" 에도 걸려
      //    /budgets 에서 거짓 실패를 냈다. 반드시 heading + 정확매칭으로 좁힌다.
      await expect(
        page.getByRole('heading', { name: /^(500|Internal Server Error)$/ })
      ).not.toBeVisible();
    });
  }

  test('clicking the API Keys sidebar link navigates to /keys', async ({ page }) => {
    // Dashboard is the landing page — verify sidebar navigation works.
    // The sidebar nav item renders t('nav.keys') = "API Keys" (same in ko/en).
    // UNCERTAINTY: the sidebar may render nav items as links or buttons. We click by text.
    await page.getByRole('link', { name: 'API Keys' }).first().click();
    await expect(page).toHaveURL('/keys');
    await expect(page.getByRole('heading', { name: 'API Keys', level: 1 })).toBeVisible();
  });
});
