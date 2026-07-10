// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

interface SkeletonTableProps {
  rows?: number;
  columns?: number;
}

export function SkeletonTable({ rows = 5, columns = 4 }: SkeletonTableProps) {
  return (
    <div className="w-full overflow-hidden glass rounded-apple" aria-busy="true" aria-label="로딩 중">
      {/* Header */}
      <div className="flex items-center gap-4 border-b border-border bg-muted/40 px-4 py-3">
        {Array.from({ length: columns }).map((_, colIdx) => (
          <div
            key={colIdx}
            className={[
              'h-4 animate-pulse rounded bg-muted',
              colIdx === 0 ? 'w-32' : colIdx === columns - 1 ? 'w-20' : 'flex-1',
            ].join(' ')}
          />
        ))}
      </div>

      {/* Rows */}
      {Array.from({ length: rows }).map((_, rowIdx) => (
        <div
          key={rowIdx}
          className="flex items-center gap-4 border-b border-border px-4 py-3 last:border-b-0"
        >
          {Array.from({ length: columns }).map((_, colIdx) => (
            <div
              key={colIdx}
              className={[
                'h-4 animate-pulse rounded bg-muted',
                colIdx === 0
                  ? 'w-28'
                  : colIdx === columns - 1
                  ? 'w-16'
                  : 'flex-1',
                // Vary widths slightly to look realistic
                rowIdx % 2 === 0 && colIdx === 1 ? 'opacity-70' : '',
              ].join(' ')}
              style={{
                animationDelay: `${(rowIdx * columns + colIdx) * 60}ms`,
              }}
            />
          ))}
        </div>
      ))}
    </div>
  );
}
