// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { SkeletonTable } from '@/components/common/SkeletonTable';

export default function Loading() {
  return (
    <div className="flex gap-6">
      <div className="w-64">
        <SkeletonTable rows={10} columns={1} />
      </div>
      <div className="flex-1">
        <SkeletonTable rows={6} columns={4} />
      </div>
    </div>
  );
}
