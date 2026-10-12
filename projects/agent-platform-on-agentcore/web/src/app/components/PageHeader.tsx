"use client";

import type { ComponentType, ReactNode } from "react";
import type { LucideIcon } from "lucide-react";
import { cn } from "@/lib/utils";

interface PageHeaderProps {
  icon: LucideIcon | ComponentType<{ className?: string }>;
  title: string;
  /** One-line explanation, shown beside the title on wide screens. */
  hint?: string;
  /** Secondary label pinned next to the title (e.g. the registry name). */
  meta?: ReactNode;
  /** Right-aligned actions. */
  actions?: ReactNode;
}

/**
 * The route header.
 *
 * Every page had its own copy of this markup at `h-16` with an `h-5 w-5` icon
 * and an `text-xl` title — a 64px bar for one line of text, which on a console
 * is a lot of chrome to carry on every screen. 52px with a smaller title reads
 * the same and gives the row back to the content.
 */
export function PageHeader({
  icon: Icon,
  title,
  hint,
  meta,
  actions,
}: PageHeaderProps) {
  return (
    <header
      className={cn(
        "flex h-13 flex-shrink-0 items-center justify-between gap-3",
        "border-b border-border bg-card/60 px-4 backdrop-blur-sm"
      )}
    >
      <div className="flex min-w-0 items-center gap-2">
        <Icon className="size-4 shrink-0 text-muted-foreground" />
        <h1 className="shrink-0 text-sm font-semibold tracking-tight">
          {title}
        </h1>
        {meta}
        {hint && (
          <span className="hidden min-w-0 truncate border-l border-border pl-2 text-xs text-muted-foreground lg:block">
            {hint}
          </span>
        )}
      </div>
      {actions && (
        <div className="flex shrink-0 items-center gap-1.5">{actions}</div>
      )}
    </header>
  );
}

/** Standard scrolling body for a route. */
export function PageBody({
  className,
  children,
}: {
  className?: string;
  children: ReactNode;
}) {
  return (
    <div className={cn("flex-1 overflow-auto p-4", className)}>{children}</div>
  );
}

/** Centred empty / zero-state block. */
export function EmptyState({
  icon: Icon,
  title,
  description,
  action,
}: {
  icon: LucideIcon | ComponentType<{ className?: string }>;
  title: string;
  description?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="flex flex-col items-center justify-center px-6 py-16 text-center">
      <div className="mb-3 flex size-10 items-center justify-center rounded-lg border border-border bg-muted">
        <Icon className="size-5 text-muted-foreground" />
      </div>
      <p className="text-sm font-medium">{title}</p>
      {description && (
        <p className="mt-1 max-w-sm text-xs leading-relaxed text-muted-foreground">
          {description}
        </p>
      )}
      {action && <div className="mt-4">{action}</div>}
    </div>
  );
}

/** Inline alert strip, for route-level errors and warnings. */
export function Notice({
  tone = "error",
  icon: Icon,
  children,
  action,
}: {
  tone?: "error" | "warning" | "info";
  icon?: LucideIcon | ComponentType<{ className?: string }>;
  children: ReactNode;
  action?: ReactNode;
}) {
  const tones = {
    error: "border-destructive/30 bg-destructive/[0.07] text-destructive",
    warning: "border-warning/30 bg-warning/[0.07] text-warning",
    info: "border-info/30 bg-info/[0.07] text-info",
  } as const;

  return (
    <div
      className={cn(
        "mb-3 flex flex-wrap items-center justify-between gap-2 rounded-md border px-3 py-2 text-xs",
        tones[tone]
      )}
    >
      <span className="flex min-w-0 items-start gap-2">
        {Icon && <Icon className="mt-px size-3.5 shrink-0" />}
        <span className="min-w-0 whitespace-pre-wrap leading-normal">
          {children}
        </span>
      </span>
      {action}
    </div>
  );
}

/** Centred loading block, matching EmptyState's proportions. */
export function LoadingState({ label }: { label: string }) {
  return (
    <div className="flex flex-col items-center justify-center px-6 py-16 text-center">
      <span className="mb-3 size-5 animate-spin rounded-full border-2 border-border border-t-primary" />
      <p className="text-xs text-muted-foreground">{label}</p>
    </div>
  );
}
