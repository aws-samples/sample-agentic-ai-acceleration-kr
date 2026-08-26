// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

// models.spec.ts — Model Management page + CreateModelDialog schema-drift fix.
//
// Schema-drift fix confirmed in CreateModelDialog.tsx (committed):
//   The form's FormState type and JSX contain NO fields for max_tokens or context_window.
//   Those fields were removed because they do not exist in the backend API schema.
//   This spec asserts their absence so a regression would be immediately caught.
//
// Fields PRESENT in the dialog (confirmed from CreateModelDialog.tsx source):
//   id="alias"                       — label "Alias *" (hardcoded, not translated)
//   id="provider"                    — label "Provider *" (hardcoded, not translated)
//   id="model_id"                    — label "Model ID *" (hardcoded, not translated)
//   id="endpoint_url"                — label "Endpoint URL"
//   id="input_price_per_1k"          — label from t('priceInput') = "입력"
//   id="output_price_per_1k"         — label from t('priceOutput') = "출력"
//   id="cache_creation_5m_price_per_1k"
//   id="cache_creation_1h_price_per_1k"
//   id="cache_read_price_per_1k"
//   id="description"
//   id="display_name"
//
// Fields ABSENT (schema-drift fix):
//   id="max_tokens"                  — removed; must NOT exist in the DOM
//   id="context_window"              — removed; must NOT exist in the DOM
//
// i18n catalog keys asserted:
//   ko: models.title      = "모델 관리"            (page h1)
//   ko: models.createModel = "모델 추가"            (dialog h2 — create mode)
//   ko: models.addModelButton = "+ 모델 추가"       (trigger button text)
//   ko: common.cancel = "취소"                      (cancel button in dialog)
//
// Dialog trigger: CreateModelButton.tsx renders a <button> with text t('addModelButton').
// Dialog heading: CreateModelDialog.tsx renders <h2>{t('createModel')}</h2> in create mode.
//
// UNCERTAINTY NOTE: The trigger button has no data-testid; we select it by role + name.
// If the button text changes or additional buttons share the same text, this may need adjustment.

import { test, expect } from '@playwright/test';

test.describe('Models page', () => {
  test.beforeEach(async ({ page }) => {
    // dev-login으로 인증 (DEV_LOGIN_ENABLED=true 필요)
    await page.goto('/api/auth/dev-login');
    await page.selectOption('select[name="role"]', 'ADMIN');
    await page.click('button[type="submit"]');
    await page.waitForURL('/');
  });

  test('shows model management heading', async ({ page }) => {
    await page.goto('/models');
    // models.title ko = "모델 관리"
    await expect(page.getByRole('heading', { name: '모델 관리', level: 1 })).toBeVisible();
  });

  test('create dialog opens and contains required fields', async ({ page }) => {
    await page.goto('/models');
    await expect(page.getByRole('heading', { name: '모델 관리', level: 1 })).toBeVisible();

    // Trigger: CreateModelButton renders t('addModelButton') = "+ 모델 추가"
    const createBtn = page.getByRole('button', { name: '+ 모델 추가' });
    await expect(createBtn).toBeVisible();
    await createBtn.click();

    // Dialog heading: t('createModel') ko = "모델 추가" (create mode, not edit)
    const dialogHeading = page.getByRole('heading', { name: '모델 추가', level: 2 });
    await expect(dialogHeading).toBeVisible();

    // Required fields MUST be present (confirmed from CreateModelDialog.tsx FormState + JSX)
    await expect(page.locator('#alias')).toBeVisible();
    await expect(page.locator('#provider')).toBeVisible();
    await expect(page.locator('#model_id')).toBeVisible();
    await expect(page.locator('#input_price_per_1k')).toBeVisible();
    await expect(page.locator('#output_price_per_1k')).toBeVisible();

    // Optional fields that are still present
    await expect(page.locator('#endpoint_url')).toBeVisible();
    await expect(page.locator('#description')).toBeVisible();
    await expect(page.locator('#display_name')).toBeVisible();
  });

  test('create dialog has no max_tokens or context_window fields (schema-drift fix)', async ({ page }) => {
    await page.goto('/models');
    await page.getByRole('button', { name: '+ 모델 추가' }).click();

    // Wait for the dialog to open before asserting absences
    await expect(page.getByRole('heading', { name: '모델 추가', level: 2 })).toBeVisible();

    // These fields were removed as a schema-drift fix — they MUST NOT exist
    await expect(page.locator('#max_tokens')).not.toBeAttached();
    await expect(page.locator('#context_window')).not.toBeAttached();

    // Also verify by label text — even if id names changed, labels should not exist
    // UNCERTAINTY: if a label containing "max_tokens" or "context_window" text is re-added
    // for display purposes (not input), the locators below may need narrowing.
    await expect(page.locator('label[for="max_tokens"]')).not.toBeAttached();
    await expect(page.locator('label[for="context_window"]')).not.toBeAttached();
  });

  test('create dialog closes on cancel', async ({ page }) => {
    await page.goto('/models');
    await page.getByRole('button', { name: '+ 모델 추가' }).click();

    const dialogHeading = page.getByRole('heading', { name: '모델 추가', level: 2 });
    await expect(dialogHeading).toBeVisible();

    // common.cancel ko = "취소"
    await page.getByRole('button', { name: '취소' }).click();

    // Dialog should be gone
    await expect(dialogHeading).not.toBeVisible();
  });
});
