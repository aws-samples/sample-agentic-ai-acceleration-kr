import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";

import { cn } from "@/lib/utils";

/**
 * Badges are metadata, not decoration.
 *
 * The row under a card title mixes several genuinely different kinds of fact,
 * and rendering them all as identically-shaped pills made the row read as
 * undifferentiated noise. `shape` encodes what kind of thing a badge is;
 * `variant` only chooses its tone:
 *
 *   code   — a fixed taxonomy value (A2A, MCP, AGENT_SKILLS). Monospace and
 *            sharp-cornered, because it is an identifier, not prose.
 *   chip   — a lifecycle status (Approved, Ready, Create failed). Tinted, with a
 *            leading dot that carries the colour.
 *   tag    — a capability or flag the thing has (Harness, 공용). A soft filled
 *            pill; rounder than the rest so it does not read as a state.
 *   count  — a bare number or version. Tabular, no chrome at all.
 */
const badgeVariants = cva(
  "inline-flex shrink-0 items-center whitespace-nowrap [&_svg]:size-3 [&_svg]:shrink-0",
  {
    variants: {
      shape: {
        code: "gap-1 rounded-xs border px-1.5 py-px font-mono text-xxs font-medium tracking-normal",
        chip: "gap-1 rounded-sm border px-1.5 py-0.5 text-xxs font-medium",
        tag: "gap-1 rounded-full px-2 py-0.5 text-xxs font-medium",
        count: "gap-1 font-mono text-xxs tabular-nums",
      },
      variant: {
        default: "",
        secondary: "",
        outline: "",
        success: "",
        warning: "",
        destructive: "",
        info: "",
      },
    },
    compoundVariants: [
      // `code` stays neutral whatever the tone: a type is not good or bad.
      { shape: "code", variant: "outline", class: "border-border-strong text-muted-foreground" },
      { shape: "code", variant: "secondary", class: "border-border bg-muted text-muted-foreground" },
      { shape: "code", variant: "default", class: "border-primary/25 text-primary" },

      { shape: "chip", variant: "default", class: "border-primary/20 bg-primary/[0.08] text-primary" },
      { shape: "chip", variant: "secondary", class: "border-border bg-muted text-muted-foreground" },
      { shape: "chip", variant: "outline", class: "border-border-strong text-muted-foreground" },
      { shape: "chip", variant: "success", class: "border-success/20 bg-success/[0.08] text-success" },
      { shape: "chip", variant: "warning", class: "border-warning/20 bg-warning/[0.08] text-warning" },
      { shape: "chip", variant: "destructive", class: "border-destructive/20 bg-destructive/[0.08] text-destructive" },
      { shape: "chip", variant: "info", class: "border-info/20 bg-info/[0.08] text-info" },

      // `tag` is borderless — the fill alone separates it from a status chip.
      // The brand tag uses the ramp step rather than an alpha wash: at 24%
      // saturation a 12% brand-over-white lands on #E8EAED, i.e. plain grey, so
      // the "Harness" tag lost its colour entirely.
      { shape: "tag", variant: "default", class: "bg-primary-tint text-primary" },
      { shape: "tag", variant: "secondary", class: "bg-muted text-muted-foreground" },
      { shape: "tag", variant: "outline", class: "bg-muted text-muted-foreground" },
      { shape: "tag", variant: "success", class: "bg-success/[0.12] text-success" },
      { shape: "tag", variant: "warning", class: "bg-warning/[0.12] text-warning" },
      { shape: "tag", variant: "destructive", class: "bg-destructive/[0.12] text-destructive" },
      { shape: "tag", variant: "info", class: "bg-info/[0.12] text-info" },

      { shape: "count", variant: "default", class: "text-primary" },
      { shape: "count", variant: "secondary", class: "text-muted-foreground" },
      { shape: "count", variant: "outline", class: "text-muted-foreground" },
      { shape: "count", variant: "success", class: "text-success" },
      { shape: "count", variant: "warning", class: "font-semibold text-warning" },
      { shape: "count", variant: "destructive", class: "font-semibold text-destructive" },
      { shape: "count", variant: "info", class: "text-info" },
    ],
    defaultVariants: {
      shape: "chip",
      variant: "outline",
    },
  }
);

type BadgeVariant = NonNullable<VariantProps<typeof badgeVariants>["variant"]>;
type BadgeShape = NonNullable<VariantProps<typeof badgeVariants>["shape"]>;

export interface BadgeProps
  extends React.HTMLAttributes<HTMLDivElement>,
    VariantProps<typeof badgeVariants> {}

function Badge({ className, variant, shape, ...props }: BadgeProps) {
  return (
    <div
      className={cn(badgeVariants({ variant, shape }), className)}
      {...props}
    />
  );
}

/**
 * Maps a backend lifecycle status to a badge tone.
 *
 * Registry records, harnesses and knowledge bases all report overlapping status
 * vocabularies, and each page used to carry its own copy of this returning
 * hardcoded pastel pairs. One table keeps them consistent — and means a new
 * status only has to be classified once.
 */
export function statusVariant(status?: string | null): BadgeVariant {
  switch ((status || "").toUpperCase()) {
    case "READY":
    case "APPROVED":
    case "TEXT_INDEXED":
    case "INDEXED":
      return "success";
    case "PENDING_APPROVAL":
    case "CREATING":
    case "UPDATING":
    case "PENDING":
    case "INDEXING":
      return "warning";
    case "REJECTED":
    case "CREATE_FAILED":
    case "UPDATE_FAILED":
    case "DELETE_FAILED":
    case "FAILED":
      return "destructive";
    case "DEPRECATED":
    case "NOT_FOUND":
      return "secondary";
    default:
      return "info";
  }
}

/**
 * `PENDING_APPROVAL` → `Pending approval`.
 *
 * The APIs return SCREAMING_SNAKE_CASE, and printing that verbatim is both the
 * loudest thing in the row and the widest — `PENDING_APPROVAL` at badge size ran
 * wider than the card title it was describing. The raw value is kept in a
 * `title` tooltip by StatusBadge, so nothing is actually hidden.
 */
export function humanizeStatus(status: string): string {
  const words = status.replace(/_/g, " ").trim().toLowerCase();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/** A dot echoing the status colour, for use inside a status chip. */
export function StatusDot({
  variant,
  className,
}: {
  variant: BadgeVariant;
  className?: string;
}) {
  const tone: Record<BadgeVariant, string> = {
    default: "bg-primary",
    secondary: "bg-muted-foreground",
    outline: "bg-muted-foreground",
    success: "bg-success",
    warning: "bg-warning",
    destructive: "bg-destructive",
    info: "bg-info",
  };
  return (
    <span
      aria-hidden
      className={cn("size-1.5 shrink-0 rounded-full", tone[variant], className)}
    />
  );
}

export { Badge, badgeVariants };
export type { BadgeVariant, BadgeShape };
