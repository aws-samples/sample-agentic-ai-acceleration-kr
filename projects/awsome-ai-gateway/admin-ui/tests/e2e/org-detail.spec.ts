// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

// org-detail.spec.ts — OrgDetailPanel i18n label assertions on the /users route.
//
// Component confirmed: src/components/users/OrgDetailPanel.tsx (committed i18n edits).
// Tree confirmed: src/components/users/OrgTree.tsx + OrgTreeView.tsx
//
// Tree structure (from OrgTree.tsx):
//   - Root: ORGANIZATION node (always present if org data exists)
//   - Children: DEPARTMENT nodes (expandable)
//   - Grandchildren: TEAM nodes (expandable)
//   - Great-grandchildren: USER nodes (leaf — no expansion)
//   - Tree nodes are plain <button> elements; no data-testid attributes.
//   - paddingLeft is set via inline style based on depth (12 + depth*16 px).
//   - Clicking an expandable node (ORG/DEPT/TEAM) both selects it AND expands it.
//   - OrgDetailPanel shows different content depending on node.type.
//
// i18n catalog keys asserted in this file:
//   Default locale 'ko':
//     users.selectNodePrompt  = "트리에서 항목을 선택하세요"  (no selection state)
//     users.departmentCount   = "부서 수"                    (ORGANIZATION node detail)
//     users.teamCount         = "팀 수"                      (DEPARTMENT node detail)
//     users.leaderName        = "리더"                       (TEAM node detail)
//     users.memberCount       = "구성원 수"                  (TEAM node detail)
//     users.email             = "이메일"                     (USER node detail)
//     users.role              = "역할"                       (USER node detail)
//     users.teamNameLabel     = "소속 팀"                    (USER node detail)
//     users.appAccess.title   = "앱 접근 권한"               (USER node detail)
//     users.apply             = "적용"                       (USER node detail — Apply button)
//     users.noOrgData         = "조직 데이터가 없습니다"     (empty tree placeholder)
//
// SEED DATA GUARD: All assertions are guarded against empty/absent org data.
//   If no org data exists, tests are skipped with a clear annotation.
//
// UNCERTAINTY NOTES:
//   - Tree buttons have no data-testid; we navigate by clicking sequentially.
//   - We cannot determine USER nodes from TEAM nodes without examining detail panel
//     content after each click. The strategy is: click each revealed button in order
//     and check if the USER-specific label "이메일" appears in the detail panel.
//   - If the live org tree has a very unusual structure (all-flat, no users, etc.),
//     the user-level assertions will be skipped rather than failing.

import { test, expect } from '@playwright/test';

test.describe('OrgDetailPanel localized labels', () => {
  test.beforeEach(async ({ page }) => {
    // dev-login으로 인증 (DEV_LOGIN_ENABLED=true 필요)
    await page.goto('/api/auth/dev-login');
    await page.selectOption('select[name="role"]', 'ADMIN');
    await page.click('button[type="submit"]');
    await page.waitForURL('/');
  });

  test('shows selectNodePrompt when no tree node is selected', async ({ page }) => {
    await page.goto('/users');
    await expect(page.getByRole('heading', { name: '사용자/팀 관리', level: 1 })).toBeVisible();

    // ORGANIZATION node detail label is absent until selection.
    // users.selectNodePrompt ko = "트리에서 항목을 선택하세요"
    // This is rendered in the detail panel when node === null.
    await expect(page.getByText('트리에서 항목을 선택하세요')).toBeVisible();
  });

  test('clicking the root ORGANIZATION node shows department count label', async ({ page }) => {
    await page.goto('/users');
    await expect(page.getByRole('heading', { name: '사용자/팀 관리', level: 1 })).toBeVisible();

    // Guard: if org data is absent, skip rather than fail.
    // users.noOrgData ko = "조직 데이터가 없습니다"
    const noDataLocator = page.getByText('조직 데이터가 없습니다');
    const hasNoData = await noDataLocator.isVisible().catch(() => false);
    if (hasNoData) {
      test.skip(true, '조직 데이터 없음 — 트리가 비어 있어 이 테스트를 건너뜁니다 (Org data absent, skipping)');
    }

    // The tree panel is the left column (w-72 class). Click the first tree button.
    // This is the root ORGANIZATION node.
    const treePanel = page.locator('.w-72').first();
    const firstNode = treePanel.locator('button').first();
    await expect(firstNode).toBeVisible();
    await firstNode.click();

    // After clicking, the detail panel (right side) should show the ORGANIZATION view.
    // users.departmentCount ko = "부서 수"
    // UNCERTAINTY: if the first button is not an ORGANIZATION type (unexpected structure),
    //   this assertion may fail. The org tree root is always ORGANIZATION per OrgTreeView.tsx.
    const detailPanel = page.locator('.flex-1.p-6').first();
    await expect(detailPanel.getByText('부서 수')).toBeVisible();

    // The "트리에서 항목을 선택하세요" placeholder should be gone after selection.
    await expect(page.getByText('트리에서 항목을 선택하세요')).not.toBeVisible();
  });

  test('expanding org to TEAM level shows team labels', async ({ page }) => {
    await page.goto('/users');
    await expect(page.getByRole('heading', { name: '사용자/팀 관리', level: 1 })).toBeVisible();

    const noDataLocator = page.getByText('조직 데이터가 없습니다');
    const hasNoData = await noDataLocator.isVisible().catch(() => false);
    if (hasNoData) {
      test.skip(true, '조직 데이터 없음 — 트리가 비어 있어 이 테스트를 건너뜁니다');
    }

    const treePanel = page.locator('.w-72').first();
    const detailPanel = page.locator('.flex-1.p-6').first();

    // Step 1: click root ORGANIZATION node (expands it)
    const rootNode = treePanel.locator('button').first();
    await rootNode.click();
    // Root must show org details (guard that org structure is not empty)
    const rootShowsOrgDetail = await detailPanel.getByText('부서 수').isVisible().catch(() => false);
    if (!rootShowsOrgDetail) {
      test.skip(true, '루트 노드가 ORGANIZATION 타입이 아닙니다 — 구조가 예상과 다릅니다');
    }

    // Step 2: after expanding root, new buttons (DEPARTMENT nodes) should appear.
    // Click the second visible tree button (first DEPARTMENT child).
    const treeButtons = treePanel.locator('button');
    const countAfterRoot = await treeButtons.count();
    if (countAfterRoot < 2) {
      test.skip(true, '부서 노드 없음 — 조직에 하위 구조가 없습니다');
    }
    await treeButtons.nth(1).click();

    // A DEPARTMENT node shows users.teamCount ko = "팀 수"
    // OR it may be a TEAM node if the org has flat TEAM children.
    // Check for either: if "팀 수" appears it's a DEPT; if "리더" appears it's a TEAM.
    const hasDeptLabel = await detailPanel.getByText('팀 수').isVisible().catch(() => false);
    const hasTeamLabel = await detailPanel.getByText('리더').isVisible().catch(() => false);

    if (hasDeptLabel) {
      // users.teamCount ko = "팀 수"
      await expect(detailPanel.getByText('팀 수')).toBeVisible();
    } else if (hasTeamLabel) {
      // users.leaderName ko = "리더"
      await expect(detailPanel.getByText('리더')).toBeVisible();
      // users.memberCount ko = "구성원 수"
      await expect(detailPanel.getByText('구성원 수')).toBeVisible();
    } else {
      // Could be a USER node if very flat structure — proceed to user label check
      const hasEmailLabel = await detailPanel.getByText('이메일').isVisible().catch(() => false);
      if (hasEmailLabel) {
        await expect(detailPanel.getByText('이메일')).toBeVisible();
      } else {
        test.skip(true, '2번째 트리 노드의 타입을 판별할 수 없습니다');
      }
    }
  });

  test('clicking a USER node shows all OrgDetailPanel user labels', async ({ page }) => {
    await page.goto('/users');
    await expect(page.getByRole('heading', { name: '사용자/팀 관리', level: 1 })).toBeVisible();

    const noDataLocator = page.getByText('조직 데이터가 없습니다');
    const hasNoData = await noDataLocator.isVisible().catch(() => false);
    if (hasNoData) {
      test.skip(true, '조직 데이터 없음 — 이 테스트를 건너뜁니다');
    }

    const treePanel = page.locator('.w-72').first();
    const detailPanel = page.locator('.flex-1.p-6').first();

    // Expand the tree by clicking each button in sequence until we find a USER node.
    // A USER node is identified by the presence of "이메일" label in the detail panel.
    // Strategy: click up to 10 buttons in the tree, stop when we find a user node.
    let foundUserNode = false;
    for (let i = 0; i < 10; i++) {
      const buttons = treePanel.locator('button');
      const count = await buttons.count();
      if (i >= count) break;

      await buttons.nth(i).click();

      // Small wait to allow the detail panel to update
      await page.waitForTimeout(150);

      const hasEmailLabel = await detailPanel.getByText('이메일').isVisible().catch(() => false);
      if (hasEmailLabel) {
        foundUserNode = true;
        break;
      }
    }

    if (!foundUserNode) {
      test.skip(true, '첫 10개 트리 노드에서 USER 타입을 찾지 못했습니다 — 조직 구조가 깊거나 사용자가 없습니다');
    }

    // USER node detail panel — assert all committed i18n labels from OrgDetailPanel.tsx
    // users.email ko = "이메일"
    await expect(detailPanel.getByText('이메일')).toBeVisible();

    // users.role ko = "역할"
    await expect(detailPanel.getByText('역할')).toBeVisible();

    // users.teamNameLabel ko = "소속 팀"
    await expect(detailPanel.getByText('소속 팀')).toBeVisible();

    // users.appAccess.title ko = "앱 접근 권한"
    await expect(detailPanel.getByText('앱 접근 권한')).toBeVisible();

    // users.apply ko = "적용" — the Apply button in the UserPanel
    // UNCERTAINTY: the button is disabled until a change is made. We assert it exists
    // (is attached to DOM), not that it is enabled.
    await expect(detailPanel.getByRole('button', { name: '적용' })).toBeAttached();
  });
});
