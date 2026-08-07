// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

'use client';

import { useTheme } from 'next-themes';
import { useTranslations } from 'next-intl';
import { Sun, Moon, Laptop } from 'lucide-react';
import { useEffect, useState } from 'react';

const OPTIONS = [
  { value: 'light', key: 'light' as const, icon: Sun },
  { value: 'dark', key: 'dark' as const, icon: Moon },
  { value: 'system', key: 'system' as const, icon: Laptop },
] as const;

export function ThemeToggle() {
  const { theme, setTheme } = useTheme();
  const t = useTranslations('common.theme');
  const [mounted, setMounted] = useState(false);

  // hydration mismatch 방지: client mount 후에만 active state 표시
  useEffect(() => setMounted(true), []);

  return (
    <div
      className="flex items-center gap-1 rounded-md border border-sidebar-border bg-sidebar-background p-1"
      role="radiogroup"
      aria-label={t('label')}
    >
      {OPTIONS.map(({ value, key, icon: Icon }) => {
        const active = mounted && theme === value;
        const label = t(key);
        return (
          <button
            key={value}
            type="button"
            role="radio"
            aria-checked={active}
            aria-label={label}
            title={label}
            onClick={() => setTheme(value)}
            className={[
              'flex h-7 w-7 items-center justify-center rounded transition-colors',
              active
                ? 'bg-sidebar-accent text-sidebar-accent-foreground'
                : 'text-sidebar-foreground hover:bg-sidebar-accent/50',
            ].join(' ')}
          >
            <Icon size={14} aria-hidden="true" />
          </button>
        );
      })}
    </div>
  );
}
