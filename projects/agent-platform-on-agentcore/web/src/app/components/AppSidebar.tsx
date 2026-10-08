"use client";

import { usePathname } from "next/navigation";
import Link from "next/link";
import type { ReactNode } from "react";
import {
  MessagesSquare,
  Library,
  Blocks,
  Wrench,
  LogOut,
  PanelLeftClose,
  PanelLeft,
  BookOpen,
  type LucideIcon,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";

export interface NavItem {
  key: string;
  label: string;
  icon: LucideIcon;
  /** Route to navigate to. Omit for action-only items (use onClick). */
  href?: string;
  /** Custom click handler (e.g. toggling a panel or opening a dialog). */
  onClick?: () => void;
  /** Numeric badge (e.g. threads requiring attention). */
  badge?: number;
  /** Whether the item should be rendered as active. */
  active?: boolean;
  /** Renders the item non-interactive. */
  disabled?: boolean;
  /** Roles allowed to see this item. Omit for "visible to everyone". */
  roles?: Array<"admin" | "user">;
}

interface AppSidebarProps {
  collapsed: boolean;
  onToggleCollapsed: () => void;
  /** Primary navigation groups. */
  items: NavItem[];
  /** Optional brand/title shown in the header when expanded. */
  title?: string;
  /** Optional content rendered below footer items (e.g. user info + logout). */
  footer?: ReactNode;
  /**
   * Optional control placed next to the collapse toggle, for actions belonging
   * to the section below (e.g. the thread status filter).
   *
   * It lives here so the section does not have to spend a header row of its own
   * on a single icon. Hidden while collapsed, along with the section itself.
   */
  headerAction?: ReactNode;
  /**
   * Content for the active section, e.g. the thread list while in Chats.
   *
   * Rendered below navigation, in a slot that exists on every route so that
   * appearing or disappearing never moves the nav rows: routes with no section
   * simply leave the slot empty.
   */
  section?: ReactNode;
}

function SidebarButton({
  item,
  collapsed,
  isActive,
}: {
  item: NavItem;
  collapsed: boolean;
  isActive: boolean;
}) {
  const Icon = item.icon;

  const content = (
    <>
      <span className="relative flex-shrink-0">
        <Icon className="size-4" />
        {item.badge !== undefined && item.badge > 0 && (
          <span
            className={cn(
              "absolute -right-1.5 -top-1.5 inline-flex min-h-3.5 min-w-3.5 items-center justify-center",
              "rounded-full bg-destructive px-1 text-[9px] font-bold leading-none text-destructive-foreground"
            )}
          >
            {item.badge}
          </span>
        )}
      </span>
      {!collapsed && <span className="truncate">{item.label}</span>}
    </>
  );

  // Kept compact on purpose: navigation is the least-used part of the sidebar,
  // so every row it gives up goes to the thread list above it.
  const className = cn(
    "relative flex h-8 items-center gap-2.5 rounded-md px-2 text-xs font-medium",
    "transition-colors duration-150 ease-snap outline-none",
    "focus-visible:ring-2 focus-visible:ring-ring/40",
    collapsed && "justify-center px-0",
    isActive
      ? // Tint plus weight, and nothing else. A left rail marker read as an
        // ornament stuck to the panel edge; the tint alone already separates the
        // current route from a hover, and the heavier text carries the rest.
        "bg-primary-tint font-semibold text-primary"
      : "text-muted-foreground hover:bg-accent hover:text-foreground"
  );

  const inner = item.href ? (
    <Link
      href={item.href}
      className={className}
      aria-current={isActive ? "page" : undefined}
    >
      {content}
    </Link>
  ) : (
    <button
      type="button"
      onClick={item.onClick}
      className={cn(className, "w-full text-left")}
      aria-current={isActive ? "page" : undefined}
    >
      {content}
    </button>
  );

  if (!collapsed) return inner;

  return (
    <Tooltip>
      <TooltipTrigger asChild>{inner}</TooltipTrigger>
      <TooltipContent side="right">{item.label}</TooltipContent>
    </Tooltip>
  );
}

export function AppSidebar({
  collapsed,
  onToggleCollapsed,
  items,
  title = "Agent Platform",
  footer,
  headerAction,
  section,
}: AppSidebarProps) {
  const pathname = usePathname();

  const isItemActive = (item: NavItem) =>
    item.active ?? (item.href ? pathname === item.href : false);

  const showSection = !!section && !collapsed;

  return (
    <TooltipProvider delayDuration={0}>
      <aside
        className={cn(
          // One expanded width for every route. Sizing to the section instead
          // made the whole page shift sideways on navigation.
          "flex h-full flex-col border-r border-border bg-sidebar transition-[width] duration-200 ease-snap",
          collapsed ? "w-16" : "w-64"
        )}
      >
        {/* Header: brand + collapse toggle */}
        <div
          className={cn(
            "flex h-13 flex-shrink-0 items-center border-b border-border px-3",
            collapsed ? "justify-center" : "justify-between"
          )}
        >
          {!collapsed && (
            <span className="flex min-w-0 items-center gap-2">
              <span className="flex size-5 shrink-0 items-center justify-center rounded bg-primary text-[10px] font-bold text-primary-foreground">
                A
              </span>
              <span className="truncate text-xs font-semibold tracking-tight">
                {title}
              </span>
            </span>
          )}
          <span className="flex flex-shrink-0 items-center gap-0.5">
            {!collapsed && headerAction}
            <Button
              variant="ghost"
              size="icon"
              onClick={onToggleCollapsed}
              className="h-8 w-8"
              aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
            >
              {collapsed ? (
                <PanelLeft className="h-4 w-4" />
              ) : (
                <PanelLeftClose className="h-4 w-4" />
              )}
            </Button>
          </span>
        </div>

        {/* The active section takes the top, where the work is. The slot exists
            on every route — empty ones still claim the leftover height, so
            navigating never shifts the navigation below it. */}
        <div className="flex min-h-0 flex-1 flex-col">
          {showSection && (
            <div className="flex min-h-0 flex-1 flex-col">{section}</div>
          )}
        </div>

        {/* Navigation, anchored to the bottom next to the user: switching
            sections is occasional, so it sits out of the way of the section
            above rather than on top of it. */}
        {/* `bg-sidebar` is not cosmetic: these two strips are the floor the
            thread list scrolls against, and without a background of their own a
            future break in the height chain above would let rows paint through
            the gaps between the rows here instead of failing visibly. */}
        <nav className="flex flex-shrink-0 flex-col gap-px overflow-y-auto border-t border-border bg-sidebar p-1.5">
          {items.map((item) => (
            <SidebarButton
              key={item.key}
              item={item}
              collapsed={collapsed}
              isActive={isItemActive(item)}
            />
          ))}
        </nav>

        {/* Custom footer (e.g. user info + logout) */}
        {footer && (
          <div className="flex flex-shrink-0 flex-col border-t border-border bg-sidebar p-1.5">
            {footer}
          </div>
        )}
      </aside>
    </TooltipProvider>
  );
}

// Re-export icons so callers can build nav items without extra imports.
export { MessagesSquare, Library, Blocks, Wrench, LogOut, BookOpen };
