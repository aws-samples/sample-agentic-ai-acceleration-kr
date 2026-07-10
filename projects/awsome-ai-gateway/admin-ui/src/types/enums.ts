// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

// Union type + const object pattern (tree-shaking friendly, avoids TypeScript enum pitfalls)

// User role within the organization
export const UserRole = {
  ADMIN: 'ADMIN',
  TEAM_LEADER: 'TEAM_LEADER',
  DEVELOPER: 'DEVELOPER',
} as const;
export type UserRole = (typeof UserRole)[keyof typeof UserRole];

// Virtual API key lifecycle status
export const KeyStatus = {
  ACTIVE: 'ACTIVE',
  EXPIRED: 'EXPIRED',
  REVOKED: 'REVOKED',
} as const;
export type KeyStatus = (typeof KeyStatus)[keyof typeof KeyStatus];

// Budget allocation scope
export const BudgetScope = {
  TEAM: 'TEAM',
  USER: 'USER',
} as const;
export type BudgetScope = (typeof BudgetScope)[keyof typeof BudgetScope];

// Budget / cost alert severity level
export const AlertLevel = {
  NORMAL: 'NORMAL',
  WARNING: 'WARNING',
  CRITICAL: 'CRITICAL',
} as const;
export type AlertLevel = (typeof AlertLevel)[keyof typeof AlertLevel];

// Rate-limit enforcement scope
export const RateLimitScope = {
  USER: 'USER',
  TEAM: 'TEAM',
  GLOBAL: 'GLOBAL',
} as const;
export type RateLimitScope = (typeof RateLimitScope)[keyof typeof RateLimitScope];

// Org-tree node type
export const OrgNodeType = {
  ORGANIZATION: 'ORGANIZATION',
  DEPARTMENT: 'DEPARTMENT',
  TEAM: 'TEAM',
  USER: 'USER',
} as const;
export type OrgNodeType = (typeof OrgNodeType)[keyof typeof OrgNodeType];

// Analytics time period selector
export const PeriodType = {
  '7d': '7d',
  '30d': '30d',
  '90d': '90d',
  custom: 'custom',
} as const;
export type PeriodType = (typeof PeriodType)[keyof typeof PeriodType];

// Analytics group-by dimension
export const GroupByType = {
  model: 'model',
  team: 'team',
  user: 'user',
} as const;
export type GroupByType = (typeof GroupByType)[keyof typeof GroupByType];
