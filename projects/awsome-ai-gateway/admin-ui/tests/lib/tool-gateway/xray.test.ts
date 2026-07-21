// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { clampTraceRange, extractToolName, normalizeSummary } from '@/lib/tool-gateway/xray';

describe('clampTraceRange', () => {
  it('defaults invalid to 24h', () => {
    expect(clampTraceRange('nonsense')).toBe('24h');
    expect(clampTraceRange('1h')).toBe('1h');
  });
});

describe('extractToolName', () => {
  it('extracts tool name from span (returns whole string if it contains ___)', () => {
    expect(extractToolName('serper___web_search')).toBe('serper___web_search');
    expect(extractToolName(undefined)).toBeNull();
  });
});

describe('normalizeSummary', () => {
  it('flattens an X-Ray summary', () => {
    const item = normalizeSummary({
      Id: 't1',
      Duration: 1.5,
      ResponseTime: 1.5,
      HasFault: false,
      HasError: true,
    } as never);
    expect(item.id).toBe('t1');
    expect(item.hasError).toBe(true);
    expect(item.duration).toBe(1.5);
    expect(item.hasFault).toBe(false);
  });
});
