// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

// Feature flag: nav item + routes are dormant unless explicitly enabled.
export const TOOL_GATEWAY_ENABLED =
  process.env.NEXT_PUBLIC_TOOL_GATEWAY_ENABLED === 'true' &&
  !!process.env.NEXT_PUBLIC_TOOL_GATEWAY_URL;

export const AWS_REGION = process.env.NEXT_PUBLIC_TOOL_GATEWAY_REGION || 'us-east-1';
export const GATEWAY_ID = process.env.NEXT_PUBLIC_TOOL_GATEWAY_ID || '';
export const GATEWAY_URL = process.env.NEXT_PUBLIC_TOOL_GATEWAY_URL || '';
export const GATEWAY_ARN =
  process.env.TOOL_GATEWAY_ARN ||
  (GATEWAY_ID && process.env.AWS_ACCOUNT_ID
    ? `arn:aws:bedrock-agentcore:${AWS_REGION}:${process.env.AWS_ACCOUNT_ID}:gateway/${GATEWAY_ID}`
    : '');

export const TIME_RANGES = {
  '1h': { label: '1시간', minutes: 60 },
  '6h': { label: '6시간', minutes: 360 },
  '24h': { label: '24시간', minutes: 1440 },
  '7d': { label: '7일', minutes: 10080 },
} as const;
export const TRACE_TIME_RANGES = {
  '1h': { label: '1시간', minutes: 60 },
  '6h': { label: '6시간', minutes: 360 },
  '24h': { label: '24시간', minutes: 1440 },
} as const;
export type TimeRangeKey = keyof typeof TIME_RANGES;
export type TraceTimeRangeKey = keyof typeof TRACE_TIME_RANGES;
