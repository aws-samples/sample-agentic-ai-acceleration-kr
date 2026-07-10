// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { checkPagePermission } from '@/lib/auth';

describe('checkPagePermission', () => {
  it('allows ADMIN to access all pages', () => {
    expect(checkPagePermission('/', 'ADMIN')).toBe(true);
    expect(checkPagePermission('/keys', 'ADMIN')).toBe(true);
    expect(checkPagePermission('/monitoring', 'ADMIN')).toBe(true);
  });

  it('allows TEAM_LEADER to access permitted pages', () => {
    expect(checkPagePermission('/', 'TEAM_LEADER')).toBe(true);
    expect(checkPagePermission('/budgets', 'TEAM_LEADER')).toBe(true);
    expect(checkPagePermission('/analytics', 'TEAM_LEADER')).toBe(true);
  });

  it('blocks TEAM_LEADER from admin-only pages', () => {
    expect(checkPagePermission('/keys', 'TEAM_LEADER')).toBe(false);
    expect(checkPagePermission('/models', 'TEAM_LEADER')).toBe(false);
    expect(checkPagePermission('/monitoring', 'TEAM_LEADER')).toBe(false);
  });

  it('blocks DEVELOPER from all pages', () => {
    expect(checkPagePermission('/', 'DEVELOPER')).toBe(false);
    expect(checkPagePermission('/analytics', 'DEVELOPER')).toBe(false);
  });
});
