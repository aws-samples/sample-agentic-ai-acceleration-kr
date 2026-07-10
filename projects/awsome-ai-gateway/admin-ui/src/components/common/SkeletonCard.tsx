// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

interface SkeletonCardProps {
  count?: number;
}

function SingleSkeletonCard({ index }: { index: number }) {
  return (
    <div
      className="glass rounded-apple p-6 flex flex-col gap-4"
      style={{ animationDelay: `${index * 100}ms` }}
      aria-hidden="true"
    >
      {/* Icon + Title row */}
      <div className="flex items-center justify-between">
        <div className="h-4 w-28 animate-pulse rounded bg-muted" />
        <div className="h-8 w-8 animate-pulse rounded-md bg-muted" />
      </div>

      {/* Value */}
      <div className="h-8 w-24 animate-pulse rounded bg-muted" />

      {/* Description */}
      <div className="h-3 w-36 animate-pulse rounded bg-muted opacity-70" />
    </div>
  );
}

export function SkeletonCard({ count = 1 }: SkeletonCardProps) {
  if (count === 1) {
    return (
      <div aria-busy="true" aria-label="로딩 중">
        <SingleSkeletonCard index={0} />
      </div>
    );
  }

  return (
    <div
      aria-busy="true"
      aria-label="로딩 중"
      className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4"
    >
      {Array.from({ length: count }).map((_, i) => (
        <SingleSkeletonCard key={i} index={i} />
      ))}
    </div>
  );
}
