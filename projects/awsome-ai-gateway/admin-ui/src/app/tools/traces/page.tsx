// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { TraceTable } from '@/components/tools/TraceTable';

export default function TracesPage() {
  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold">Tool Gateway — Trace</h1>
      <TraceTable />
    </div>
  );
}
