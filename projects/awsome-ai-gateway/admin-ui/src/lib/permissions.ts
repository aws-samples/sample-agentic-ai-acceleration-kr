// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import type { PagePermissionMap } from '@/types/api';
import { UserRole } from '@/types/enums';

/**
 * Canonical page-permission table.
 *
 * Keys are exact pathname prefixes (matched with startsWith in auth.ts).
 * Values list the UserRole values that are ALLOWED to visit that path.
 *
 * DEVELOPER role is intentionally absent from all entries — developers are
 * redirected to a "no access" page by the middleware.
 */
export const PAGE_PERMISSIONS: PagePermissionMap = {
  // Dashboard
  '/': [UserRole.ADMIN, UserRole.TEAM_LEADER],

  // Virtual key management — admin only
  '/keys': [UserRole.ADMIN],

  // Budget overview — admin + team leader
  '/budgets': [UserRole.ADMIN, UserRole.TEAM_LEADER],

  // Model catalogue — admin only
  '/models': [UserRole.ADMIN],

  // Rate-limit configuration — admin only
  '/rate-limits': [UserRole.ADMIN],

  // User / org management — admin only
  '/users': [UserRole.ADMIN],

  // Analytics & ROI — admin + team leader
  '/analytics': [UserRole.ADMIN, UserRole.TEAM_LEADER],

  // Self-service usage — team leaders and developers only.
  // ADMIN 은 게이트웨이 전체를 관리하는 역할이므로 "내 사용량" 을 노출하면
  // 역할 경계가 흐려진다는 피드백에 따라 제외. 시스템 전체 사용량은
  // /monitoring, /analytics 가 담당.
  '/my': [UserRole.TEAM_LEADER, UserRole.DEVELOPER],

  // Real-time monitoring — admin only
  '/monitoring': [UserRole.ADMIN],

  // CLI downloads — admin + team leader
  '/cli': [UserRole.ADMIN, UserRole.TEAM_LEADER],

  // BI assistant chat — admin + team leader (운영 데이터 질의 도구, /analytics 와 동일 범위)
  '/chat': [UserRole.ADMIN, UserRole.TEAM_LEADER],
};
