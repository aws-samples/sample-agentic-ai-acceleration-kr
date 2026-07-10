// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

interface FormErrorProps {
  error?: string | null;
}

export function FormError({ error }: FormErrorProps) {
  if (!error) return null;
  return (
    <p className="text-sm text-destructive mt-1" role="alert" aria-live="polite">
      {error}
    </p>
  );
}
