// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import type { ReactNode } from 'react';

export type BadgeTone = 'teal' | 'sky' | 'pink' | 'amber' | 'neutral';

interface BadgeProps {
  tone?: BadgeTone;
  children: ReactNode;
  className?: string;
}

const TONE_CLASS: Record<BadgeTone, string> = {
  teal: 'badge-teal',
  sky: 'badge-sky',
  pink: 'badge-pink',
  amber: 'badge-amber',
  neutral: 'badge-neutral',
};

/**
 * 상태/역할/프로바이더 등 라벨 배지. teal/sky/pink/amber/neutral 톤을
 * globals.css 의 그라데이션 유틸로 통일(핑크/틸/스카이/화이트 계열).
 */
export function Badge({ tone = 'neutral', children, className = '' }: BadgeProps) {
  return <span className={`badge ${TONE_CLASS[tone]} ${className}`}>{children}</span>;
}
