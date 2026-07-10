// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { clsx, type ClassValue } from 'clsx';
import { twMerge } from 'tailwind-merge';

/**
 * Tailwind 클래스 병합 유틸 (shadcn 표준). clsx 로 조건부 클래스를 합치고
 * tailwind-merge 로 충돌 클래스(px-2 vs px-4 등)를 뒤쪽 우선으로 정리한다.
 * 공통 <Table> 컴포넌트가 기본 클래스 + 호출부 className 을 안전하게 합칠 때 사용.
 */
export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}
