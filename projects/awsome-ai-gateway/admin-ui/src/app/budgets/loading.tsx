// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { SkeletonTable } from '@/components/common/SkeletonTable';

export default function Loading() {
  return <SkeletonTable rows={8} columns={7} />;
}
