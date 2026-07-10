// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { SkeletonTable } from '@/components/common/SkeletonTable';
import { SkeletonCard } from '@/components/common/SkeletonCard';

export default function Loading() {
  return (
    <div className="space-y-6">
      <SkeletonCard count={1} />
      <SkeletonTable rows={10} columns={6} />
    </div>
  );
}
