// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { MetricsCharts } from '@/components/tools/MetricsCharts';

export default function ObservabilityPage() {
  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold">Tool Gateway — 메트릭</h1>
      <MetricsCharts />
    </div>
  );
}
