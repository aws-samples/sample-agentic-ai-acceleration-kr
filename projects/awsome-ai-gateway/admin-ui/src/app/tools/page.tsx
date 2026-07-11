// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { ToolList } from '@/components/tools/ToolList';

export default function ToolsPage() {
  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold">Tool Gateway — Tool 카탈로그</h1>
      <ToolList />
    </div>
  );
}
