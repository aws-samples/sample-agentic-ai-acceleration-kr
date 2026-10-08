"use client";

import { Monitor, Moon, Sun } from "lucide-react";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { useTheme, type ThemePreference } from "@/providers/ThemeProvider";
import { cn } from "@/lib/utils";

const OPTIONS: Array<{
  value: ThemePreference;
  label: string;
  icon: typeof Sun;
}> = [
  { value: "light", label: "라이트", icon: Sun },
  { value: "dark", label: "다크", icon: Moon },
  { value: "system", label: "시스템 설정", icon: Monitor },
];

/**
 * Theme control.
 *
 * Two shapes for two places: a segmented group where there is room to show that
 * "system" is a distinct third choice, and a single cycling button for the
 * collapsed sidebar rail. Both write the same preference.
 */
export function ThemeToggle({ className }: { className?: string }) {
  const { preference, setPreference } = useTheme();

  return (
    <div
      role="radiogroup"
      aria-label="테마"
      className={cn(
        "flex items-center rounded-md border border-border bg-muted p-0.5",
        className
      )}
    >
      {OPTIONS.map(({ value, label, icon: Icon }) => {
        const active = preference === value;
        return (
          <Tooltip key={value}>
            <TooltipTrigger asChild>
              <button
                type="button"
                role="radio"
                aria-checked={active}
                aria-label={label}
                onClick={() => setPreference(value)}
                className={cn(
                  "inline-flex size-6 items-center justify-center rounded-sm",
                  "transition-colors duration-150 ease-snap",
                  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/50",
                  active
                    ? "bg-card text-foreground shadow-xs"
                    : "text-muted-foreground hover:text-foreground"
                )}
              >
                <Icon className="size-3.5" />
              </button>
            </TooltipTrigger>
            <TooltipContent side="top">{label}</TooltipContent>
          </Tooltip>
        );
      })}
    </div>
  );
}

/**
 * Icon-only variant for the collapsed sidebar: cycles light → dark → system.
 *
 * Shows the *resolved* icon while following the system, so the button always
 * depicts what is on screen rather than the abstract preference.
 */
export function ThemeToggleButton({ className }: { className?: string }) {
  const { preference, resolved, cycle } = useTheme();

  const Icon =
    preference === "system" ? Monitor : resolved === "dark" ? Moon : Sun;
  const label =
    preference === "system"
      ? `시스템 설정 (현재 ${resolved === "dark" ? "다크" : "라이트"})`
      : preference === "dark"
        ? "다크"
        : "라이트";

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <button
          type="button"
          onClick={cycle}
          aria-label={`테마: ${label}. 클릭해서 변경`}
          className={cn(
            "inline-flex size-7 items-center justify-center rounded-md",
            "text-muted-foreground transition-colors duration-150 ease-snap",
            "hover:bg-accent hover:text-foreground",
            "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/50",
            className
          )}
        >
          <Icon className="size-4" />
        </button>
      </TooltipTrigger>
      <TooltipContent side="right">테마: {label}</TooltipContent>
    </Tooltip>
  );
}
