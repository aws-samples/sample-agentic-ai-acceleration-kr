import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

const TONE: Record<string, string> = {
  ok: "border-emerald-500/40 text-emerald-700 dark:text-emerald-300",
  warn: "border-amber-500/40 text-amber-700 dark:text-amber-300",
  error: "border-destructive/50 text-destructive",
  muted: "border-border text-muted-foreground",
};

/** A status word with one of four tones; the tone names come from orgTable.mjs. */
export function ToneBadge({ label, tone }: { label: string; tone: string }) {
  return (
    <Badge variant="outline" className={cn("font-normal", TONE[tone] ?? TONE.muted)}>
      {label}
    </Badge>
  );
}
