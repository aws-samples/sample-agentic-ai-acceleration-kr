// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import type { AlertLevel } from '@/types/enums';
import { AlertLevel as AlertLevelConst } from '@/types/enums';

interface KPICardProps {
  title: string;
  value: string | number;
  icon: React.ReactNode;
  alertLevel?: AlertLevel;
  description?: string;
}

const ALERT_BORDER_CLASSES: Record<AlertLevel, string> = {
  [AlertLevelConst.CRITICAL]: 'border-destructive shadow-destructive/20',
  [AlertLevelConst.WARNING]: 'border-warning shadow-warning/20',
  [AlertLevelConst.NORMAL]: 'border-border',
};

const ALERT_ICON_WRAPPER_CLASSES: Record<AlertLevel, string> = {
  [AlertLevelConst.CRITICAL]: 'bg-destructive/10 text-destructive',
  [AlertLevelConst.WARNING]: 'bg-warning/10 text-warning',
  [AlertLevelConst.NORMAL]: 'bg-muted text-muted-foreground',
};

export function KPICard({
  title,
  value,
  icon,
  alertLevel = AlertLevelConst.NORMAL,
  description,
}: KPICardProps) {
  const borderClass = ALERT_BORDER_CLASSES[alertLevel];
  const iconWrapperClass = ALERT_ICON_WRAPPER_CLASSES[alertLevel];

  return (
    <div
      className={[
        'glass glass-hover rounded-apple p-6 flex flex-col gap-4',
        // normal 은 glass 기본 보더, alert 일 때만 강조 보더 덮어쓰기
        alertLevel === AlertLevelConst.NORMAL ? '' : borderClass,
      ].join(' ')}
      aria-label={`${title}: ${value}`}
    >
      {/* Header: title + icon */}
      <div className="flex items-center justify-between">
        <p className="text-sm font-medium text-muted-foreground">{title}</p>
        <div
          className={[
            'flex h-9 w-9 items-center justify-center rounded-md flex-shrink-0',
            iconWrapperClass,
          ].join(' ')}
          aria-hidden="true"
        >
          {icon}
        </div>
      </div>

      {/* Value */}
      <p className="text-3xl font-bold text-foreground tracking-tight">
        {typeof value === 'number' ? value.toLocaleString() : value}
      </p>

      {/* Description */}
      {description && (
        <p className="text-xs text-muted-foreground">{description}</p>
      )}
    </div>
  );
}
