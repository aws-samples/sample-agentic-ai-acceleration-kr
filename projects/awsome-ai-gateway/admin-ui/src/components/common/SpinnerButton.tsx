'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.


import { Loader2 } from 'lucide-react';

interface SpinnerButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  isLoading?: boolean;
  children: React.ReactNode;
}

export function SpinnerButton({
  isLoading = false,
  children,
  disabled,
  className,
  ...props
}: SpinnerButtonProps) {
  const isDisabled = isLoading || disabled;

  return (
    <button
      {...props}
      disabled={isDisabled}
      aria-disabled={isDisabled}
      aria-busy={isLoading}
      className={[
        'inline-flex items-center justify-center gap-2 rounded-md px-4 py-2 text-sm font-medium transition-colors',
        'bg-primary text-primary-foreground shadow hover:bg-primary/90',
        'focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring',
        'disabled:pointer-events-none disabled:opacity-50',
        className,
      ]
        .filter(Boolean)
        .join(' ')}
    >
      {isLoading && (
        <Loader2
          size={16}
          className="animate-spin flex-shrink-0"
          aria-hidden="true"
        />
      )}
      {children}
    </button>
  );
}