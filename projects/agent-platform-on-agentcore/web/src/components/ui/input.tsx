import * as React from "react";

import { cn } from "@/lib/utils";

/**
 * Inputs sit *below* the card surface (muted fill, not white-on-white), so a
 * form reads as a set of wells rather than a set of outlines. The focus ring is
 * a 2px brand ring; the tailwind forms plugin's own ring is suppressed in
 * globals.css.
 */
function Input({ className, type, ...props }: React.ComponentProps<"input">) {
  return (
    <input
      type={type}
      data-slot="input"
      className={cn(
        "flex h-9 w-full min-w-0 rounded-md border border-input bg-background px-3 text-sm",
        "transition-[border-color,box-shadow,background-color] duration-150 ease-snap",
        "placeholder:text-muted-foreground/70",
        "selection:bg-primary/20 selection:text-foreground",
        "file:inline-flex file:h-7 file:border-0 file:bg-transparent file:text-sm file:font-medium file:text-foreground",
        "hover:border-border-strong",
        "focus-visible:border-primary focus-visible:bg-card focus-visible:ring-2 focus-visible:ring-ring/25",
        "disabled:cursor-not-allowed disabled:opacity-50",
        "aria-invalid:border-destructive aria-invalid:ring-2 aria-invalid:ring-destructive/20",
        "outline-none",
        className
      )}
      {...props}
    />
  );
}

export { Input };
