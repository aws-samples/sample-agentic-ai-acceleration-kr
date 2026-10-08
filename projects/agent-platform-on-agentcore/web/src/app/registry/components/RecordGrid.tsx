"use client";

import type { RegistryRecordSummary } from "@/lib/registry";
import { RecordCard } from "./RecordCard";

interface RecordGridProps {
  records: RegistryRecordSummary[];
  selectedRecordId?: string;
  onOpenDetail: (recordId: string) => void;
  onChat: (record: RegistryRecordSummary) => void;
}

export function RecordGrid({
  records,
  selectedRecordId,
  onOpenDetail,
  onChat,
}: RecordGridProps) {
  return (
    // Tighter gutters and a 4th column past 1280px: the cards are compact
    // enough now that three columns left a lot of empty track on wide screens.
    <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3 2xl:grid-cols-4">
      {records.map((record, i) => (
        <RecordCard
          key={record.record_id}
          record={record}
          index={i}
          isSelected={record.record_id === selectedRecordId}
          onOpenDetail={onOpenDetail}
          onChat={onChat}
        />
      ))}
    </div>
  );
}
