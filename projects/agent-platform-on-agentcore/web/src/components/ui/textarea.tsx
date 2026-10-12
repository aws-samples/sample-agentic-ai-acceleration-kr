import * as React from "react";

import { cn } from "@/lib/utils";

type TextareaProps = React.TextareaHTMLAttributes<HTMLTextAreaElement>;

/** Matches Input's well treatment; see input.tsx. */
const Textarea = React.forwardRef<HTMLTextAreaElement, TextareaProps>(
  ({ className, ...props }, ref) => {
    return (
      <textarea
        className={cn(
          "flex min-h-[76px] w-full rounded-md border border-input bg-background px-3 py-2 text-sm",
          "transition-[border-color,box-shadow,background-color] duration-150 ease-snap",
          "placeholder:text-muted-foreground/70",
          "selection:bg-primary/20 selection:text-foreground",
          "hover:border-border-strong",
          "focus-visible:border-primary focus-visible:bg-card focus-visible:ring-2 focus-visible:ring-ring/25",
          "disabled:cursor-not-allowed disabled:opacity-50",
          "aria-invalid:border-destructive aria-invalid:ring-2 aria-invalid:ring-destructive/20",
          "outline-none",
          className
        )}
        ref={ref}
        {...props}
      />
    );
  }
);
Textarea.displayName = "Textarea";

export { Textarea };
