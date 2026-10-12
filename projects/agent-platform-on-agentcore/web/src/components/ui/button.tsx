import * as React from "react";
import { Slot } from "@radix-ui/react-slot";
import { cva, type VariantProps } from "class-variance-authority";

import { cn } from "@/lib/utils";

const buttonVariants = cva(
  [
    "inline-flex shrink-0 items-center justify-center gap-1.5 whitespace-nowrap",
    "rounded-md text-sm font-medium tracking-snug",
    "transition-[background-color,border-color,color,box-shadow,transform] duration-150 ease-snap",
    // A 1px press keeps clicks feeling physical without shifting layout.
    "active:translate-y-px",
    "disabled:pointer-events-none disabled:opacity-45",
    "outline-none focus-visible:ring-2 focus-visible:ring-ring/60 focus-visible:ring-offset-1 focus-visible:ring-offset-background",
    "[&_svg]:pointer-events-none [&_svg]:shrink-0 [&_svg:not([class*='size-'])]:size-4",
    "aria-invalid:border-destructive aria-invalid:ring-2 aria-invalid:ring-destructive/25",
  ],
  {
    variants: {
      variant: {
        /* Filled brand. `bg-primary` used to resolve to a phantom variable and
           rendered transparent, which is why primary actions looked white. */
        default:
          "border border-primary bg-primary text-primary-foreground shadow-raised hover:border-primary-hover hover:bg-primary-hover",
        destructive:
          "border border-destructive bg-destructive text-destructive-foreground shadow-raised hover:bg-destructive/90",
        /* The workhorse secondary. Tinted rather than white so it still reads as
           a control against a white card — the old `outline` variant was a white
           button on a white surface and effectively disappeared. */
        outline:
          "border border-border-strong bg-secondary text-secondary-foreground shadow-xs hover:border-primary/45 hover:bg-primary-tint hover:text-primary",
        /*
         * Brand-tinted, filling in on hover.
         *
         * For a repeated action inside a list or grid. `default` is right for the
         * one true page action, but the registry grid renders fourteen of these
         * at once, and fourteen filled brand buttons made the page read as a
         * wall of colour with no focal point — they were also louder than
         * Register, the actual primary action in the header. This keeps them
         * legible and clearly clickable while leaving the solid fill to mean
         * "the main thing on this screen".
         */
        brand:
          "border border-primary/40 bg-primary-tint text-primary hover:border-primary hover:bg-primary hover:text-primary-foreground",
        secondary:
          "border border-transparent bg-secondary text-secondary-foreground hover:bg-border/70",
        ghost:
          "border border-transparent text-foreground/80 hover:bg-accent hover:text-accent-foreground",
        link: "text-primary underline-offset-4 hover:text-primary-hover hover:underline",
      },
      size: {
        default: "h-9 px-3.5 has-[>svg]:px-3",
        sm: "h-8 gap-1.5 px-3 text-xs has-[>svg]:px-2.5",
        lg: "h-10 px-5 text-base has-[>svg]:px-4",
        icon: "size-9",
        "icon-sm": "size-7 rounded-sm",
      },
    },
    defaultVariants: {
      variant: "default",
      size: "default",
    },
  }
);

function Button({
  className,
  variant,
  size,
  asChild = false,
  ...props
}: React.ComponentProps<"button"> &
  VariantProps<typeof buttonVariants> & {
    asChild?: boolean;
  }) {
  const Comp = asChild ? Slot : "button";

  return (
    <Comp
      data-slot="button"
      className={cn(buttonVariants({ variant, size, className }))}
      {...props}
    />
  );
}

export { Button, buttonVariants };
