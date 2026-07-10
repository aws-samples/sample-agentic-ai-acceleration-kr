// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { test, expect } from '@playwright/test';

test.describe('Dashboard', () => {
  test.beforeEach(async ({ page }) => {
    // dev-login으로 인증 (DEV_LOGIN_ENABLED=true 필요)
    await page.goto('/api/auth/dev-login');
    await page.selectOption('select[name="role"]', 'ADMIN');
    await page.click('button[type="submit"]');
    await page.waitForURL('/');
  });

  test('shows KPI cards', async ({ page }) => {
    await expect(page.getByText('이번 달 사용량')).toBeVisible();
    await expect(page.getByText('활성 API Keys')).toBeVisible();
  });

  test('navigates to keys page', async ({ page }) => {
    await page.click('text=API Keys');
    await expect(page).toHaveURL('/keys');
  });
});
