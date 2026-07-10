// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { http, HttpResponse } from 'msw';

const BASE = 'http://admin-api:8001';

export const handlers = [
  // 헬스체크
  http.get(`${BASE}/admin/system/health`, () => {
    return HttpResponse.json({
      services: [
        { service_name: 'gateway-proxy', status: 'healthy', port: 8000, last_checked_at: new Date().toISOString() },
        { service_name: 'admin-api', status: 'healthy', port: 8001, last_checked_at: new Date().toISOString() },
      ]
    });
  }),

  // 키 목록
  http.get(`${BASE}/admin/keys`, () => {
    return HttpResponse.json({
      items: [
        { key_id: 'k1', key_prefix: 'vk_****1234', user_id: 'u1', user_email: 'user@test.com', status: 'ACTIVE', created_at: '2026-01-01T00:00:00Z', expires_at: null, last_used_at: null }
      ],
      total: 1, page: 1, page_size: 20, total_pages: 1
    });
  }),

  // 모델 목록
  http.get(`${BASE}/admin/models`, () => {
    return HttpResponse.json([
      { alias: 'claude-3-sonnet', provider: 'bedrock', model_id: 'anthropic.claude-3-sonnet-20240229-v1:0', is_active: true, input_price_per_1k: 0.003, output_price_per_1k: 0.015, max_tokens: 4096, context_window: 200000, description: 'Claude 3 Sonnet' }
    ]);
  }),

  // 예산 요약
  http.get(`${BASE}/admin/budgets/summary`, () => {
    return HttpResponse.json({
      items: [
        { target_id: 't1', target_type: 'TEAM', target_name: '개발팀', limit: 1000, used: 250, remaining: 750, usage_pct: 25, alert_level: 'NORMAL' }
      ]
    });
  }),
];
