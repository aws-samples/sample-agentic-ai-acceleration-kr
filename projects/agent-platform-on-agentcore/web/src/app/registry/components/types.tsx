"use client";

import { Bot, Boxes, Puzzle, Wrench } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { cn } from "@/lib/utils";
import type { DescriptorType } from "@/lib/registry";

/**
 * The four record types, with the icon and tone that identify each.
 *
 * Single source of truth for both the filter chips and the per-card marker, so a
 * type can never be drawn one way in the toolbar and another way in the grid.
 *
 * Deliberately lucide icons rather than emoji: emoji are rendered by the OS, so
 * they change shape between platforms, ignore the theme entirely, and sit on
 * their own baseline — none of which survives next to 11px metadata.
 */
export interface TypeDescriptor {
  type: DescriptorType;
  /** Filter-chip label — plural, because a filter selects a set. */
  label: string;
  /** Singular form, for the marker's hover title (a card describes one thing). */
  noun: string;
  icon: LucideIcon;
  /** Tint for the card marker. Hue-separated so the four read apart at a glance. */
  tone: string;
}

export const TYPE_OPTIONS: TypeDescriptor[] = [
  {
    type: "A2A",
    label: "Agents",
    noun: "Agent",
    icon: Bot,
    tone: "border-primary/20 bg-primary-tint text-primary",
  },
  {
    type: "AGENT_SKILLS",
    label: "Skills",
    noun: "Skill",
    icon: Puzzle,
    tone: "border-success/20 bg-success/[0.08] text-success",
  },
  {
    type: "MCP",
    label: "MCP",
    noun: "MCP",
    icon: Wrench,
    tone: "border-info/20 bg-info/[0.08] text-info",
  },
  {
    type: "CUSTOM",
    label: "Custom",
    noun: "Custom",
    icon: Boxes,
    tone: "border-warning/20 bg-warning/[0.08] text-warning",
  },
];

const BY_TYPE = new Map(TYPE_OPTIONS.map((option) => [option.type, option]));

/** The descriptor for a record's type, or null for an unrecognised one. */
export function typeDescriptor(
  type?: string | null
): TypeDescriptor | undefined {
  return type ? BY_TYPE.get(type as DescriptorType) : undefined;
}

/**
 * The square type marker that leads a record's name.
 *
 * A card's type used to be readable only from the `A2A` / `MCP` chip below the
 * description, which meant scanning a grid for "just the skills" was a
 * word-by-word read. The icon puts it at the start of the line the eye lands on
 * first.
 */
export function TypeMarker({
  type,
  className,
}: {
  type?: string | null;
  className?: string;
}) {
  const descriptor = typeDescriptor(type);
  if (!descriptor) return null;
  const Icon = descriptor.icon;

  return (
    <span
      // The type also appears as a chip in the row below, so this is decorative
      // to a screen reader rather than a second announcement of the same fact.
      aria-hidden
      title={descriptor.noun}
      className={cn(
        "flex size-5 shrink-0 items-center justify-center rounded border",
        descriptor.tone,
        className
      )}
    >
      <Icon className="size-3" />
    </span>
  );
}
