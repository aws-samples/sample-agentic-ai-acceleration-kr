'use server';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.


import { adminAPI } from '@/lib/api-client';
import { withRetry } from '@/lib/utils/retry';

export interface ProductivityData {
  total_lines_generated: number;
  total_lines_accepted: number;
  code_acceptance_rate_pct: number;
  total_commits: number;
  pr_opened: number;
  pr_merged: number;
  active_developers: number;
}

export interface ProductivityROI {
  total_cost_usd: number;
  cost_per_generated_line: number;
  cost_per_commit: number;
}

export interface ProductivityAnalyticsResponse {
  period: string;
  productivity: ProductivityData;
  roi: ProductivityROI;
}

export async function fetchProductivityAnalytics(
  period?: string
): Promise<ProductivityAnalyticsResponse> {
  return withRetry(() =>
    adminAPI.get<ProductivityAnalyticsResponse>(
      '/admin/analytics/productivity',
      period ? { period } : undefined
    )
  );
}