// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

// i18n.spec.ts — locale switching via the Header toggle button.
//
// Locale mechanism (confirmed from source):
//   - Header.tsx renders a <button aria-label="Switch language"> that shows "EN" when locale=ko
//     and "KO" when locale=en. Clicking it calls setLocale (server action) then router.refresh().
//   - setLocale writes a cookie named `locale` (path=/, maxAge=1yr, sameSite=lax).
//   - i18n/request.ts reads that cookie on every request; default = 'ko' if absent.
//   - No URL prefix used — same paths, cookie-only locale selection.
//
// i18n catalog keys asserted in this file:
//   ko: dashboard.title = "대시보드"
//   en: dashboard.title = "Dashboard"
//   ko: models.title    = "모델 관리"
//   en: models.title    = "Model Management"

import { test, expect } from '@playwright/test';

test.describe('i18n locale switching', () => {
  test.beforeEach(async ({ page }) => {
    // dev-login으로 인증 (DEV_LOGIN_ENABLED=true 필요)
    await page.goto('/api/auth/dev-login');
    await page.selectOption('select[name="role"]', 'ADMIN');
    await page.click('button[type="submit"]');
    await page.waitForURL('/');
  });

  test('dashboard heading renders in ko by default', async ({ page }) => {
    // Default locale is 'ko' (no locale cookie set yet).
    // dashboard.title ko = "대시보드"
    await expect(page.getByRole('heading', { name: '대시보드', level: 1 })).toBeVisible();
  });

  test('toggling locale from ko to en changes heading text', async ({ page }) => {
    // Confirm ko baseline (dashboard.title ko = "대시보드")
    await expect(page.getByRole('heading', { name: '대시보드', level: 1 })).toBeVisible();

    // The locale toggle button has aria-label="Switch language" and shows "EN" when locale=ko.
    const toggleBtn = page.getByRole('button', { name: 'Switch language' });
    await expect(toggleBtn).toBeVisible();
    await expect(toggleBtn).toContainText('EN');

    // Click to switch to English — server action sets cookie, router.refresh() re-renders.
    await toggleBtn.click();

    // dashboard.title en = "Dashboard"
    await expect(page.getByRole('heading', { name: 'Dashboard', level: 1 })).toBeVisible();

    // After switching the button should now show "KO" (ready to switch back).
    await expect(toggleBtn).toContainText('KO');
  });

  test('locale persists to a different page after toggle', async ({ page }) => {
    // Switch to English on the dashboard.
    await page.getByRole('button', { name: 'Switch language' }).click();
    // Wait for the English heading to confirm the switch is complete.
    await expect(page.getByRole('heading', { name: 'Dashboard', level: 1 })).toBeVisible();

    // Navigate to /models — locale cookie should still be 'en'.
    await page.goto('/models');

    // models.title en = "Model Management"
    await expect(page.getByRole('heading', { name: 'Model Management', level: 1 })).toBeVisible();
  });

  test('switching back to ko from en restores Korean heading', async ({ page }) => {
    const toggleBtn = page.getByRole('button', { name: 'Switch language' });

    // Switch to 'en' first.
    await toggleBtn.click();
    await expect(page.getByRole('heading', { name: 'Dashboard', level: 1 })).toBeVisible();

    // ⚠️ 두 번째 클릭 전에 '정착'을 반드시 기다린다.
    // Header 의 toggleLocale 은 next 를 useLocale() 값으로 계산하므로, RSC refresh 가
    // 아직 Header 에 반영되지 않은 상태에서 다시 클릭하면 stale locale('ko')로 계산돼
    // 'en' 을 또 세팅하고 heading 이 안 바뀐다. 라벨이 'KO'로 뒤집힌 것이 client locale
    // 이 'en'으로 갱신됐다는 증거다. (병렬 워커 부하에서 실제로 이 레이스로 실패했음)
    await expect(toggleBtn).toContainText('KO');

    // Toggle again — switches back to 'ko'.
    await toggleBtn.click();

    // dashboard.title ko = "대시보드"
    await expect(page.getByRole('heading', { name: '대시보드', level: 1 })).toBeVisible();
  });
});
