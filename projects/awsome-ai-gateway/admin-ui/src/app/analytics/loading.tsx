// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { SkeletonCard } from '@/components/common/SkeletonCard';

export default function Loading() {
  return (
    <div className="space-y-6">
      <SkeletonCard count={4} />
      <SkeletonCard count={2} />
      <SkeletonCard count={4} />
    </div>
  );
}
