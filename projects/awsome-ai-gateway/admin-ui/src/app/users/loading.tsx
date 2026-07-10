// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { SkeletonTable } from '@/components/common/SkeletonTable';

export default function Loading() {
  return (
    <div className="flex gap-6">
      <div className="w-72">
        <SkeletonTable rows={12} columns={1} />
      </div>
      <div className="flex-1">
        <SkeletonTable rows={4} columns={3} />
      </div>
    </div>
  );
}
