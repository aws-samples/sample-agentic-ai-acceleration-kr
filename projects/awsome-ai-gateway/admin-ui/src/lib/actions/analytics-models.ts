'use server';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.


import { adminAPI } from '@/lib/api-client';
import { withRetry } from '@/lib/utils/retry';

export interface ModelCostItem {
  model_alias: string;
  request_count: number;
  total_cost_usd: number;
  input_tokens: number;
  output_tokens: number;
  avg_latency_ms: number;
  cost_per_1k_tokens: number;
}

export interface DailyModelCost {
  date: string;
  model_alias: string;
  cost_usd: number;
}

export interface ModelCostAnalyticsResponse {
  period: string;
  total_cost_usd: number;
  models: ModelCostItem[];
  daily_trend: DailyModelCost[];
}

export async function fetchModelCostAnalytics(
  period?: string
): Promise<ModelCostAnalyticsResponse> {
  return withRetry(() =>
    adminAPI.get<ModelCostAnalyticsResponse>(
      '/admin/analytics/models',
      period ? { period } : undefined
    )
  );
}