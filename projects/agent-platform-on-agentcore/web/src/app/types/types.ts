export interface ToolCall {
  id: string;
  name: string;
  args: Record<string, unknown>;
  result?: string;
  status: "pending" | "completed" | "error" | "interrupted";
  /**
   * How much answer text had been written when this call started.
   *
   * A whole turn is one message — the runtime keeps the same messageId across
   * its tool loop — so `content` and `tool_calls` alone say nothing about the
   * order the two interleaved in. Without this the UI can only paint all text
   * then all calls, which puts the final answer *above* the calls that produced
   * it. Both the live stream and the stored transcript derive it from the text
   * they have accumulated, so no new wire field is needed.
   *
   * Absent on records written before this existed; such calls sort last, which
   * is exactly the old behaviour.
   */
  contentOffset?: number;
}

/**
 * A chart the agent produced, in whichever form it produced it.
 *
 * `spec` is the durable form and is preferred: it redraws on every render, in
 * either theme, and never expires. `url` is a presigned image good for about five
 * minutes — far shorter than the object behind it lives — so it is a fallback, never
 * the primary; a thread reopened later has only the spec to go on.
 *
 * The server drops a frame with neither, so at least one is always present. That is
 * also why there is no `s3Uri` here: a chart drawn in a code sandbox reports only an
 * `s3://` URI, which no browser can load and which the server therefore does not
 * forward. Those charts do not render today; making them render means adding a
 * presign route, not a field.
 */
export interface ChartSpec {
  kind: "bar" | "line" | "area" | "pie" | "table" | "kpi";
  data: Record<string, unknown>[];
  encoding: { x?: string; y?: string | string[]; color?: string | null };
  title?: string | null;
}

export interface Chart {
  spec: ChartSpec | null;
  url?: string;
  source?: string;
}

/**
 * Evidence for the numbers in an answer.
 *
 * Produced when the agent answers by executing several candidate queries and
 * adopting the result most of them agree on, so `agreement` below 1 means the
 * candidates diverged and the figure deserves a second look. Advisory only.
 */
export interface Verification {
  method?: string;
  k?: number;
  n_valid?: number;
  agreement?: number;
  n_clusters?: number;
  tie?: boolean;
  verdict?: string;
  order_sensitive?: boolean;
}

export interface SubAgent {
  id: string;
  name: string;
  subAgentName: string;
  input: Record<string, unknown>;
  output?: Record<string, unknown>;
  status: "pending" | "active" | "completed" | "error";
}

export interface FileItem {
  path: string;
  content: string;
}

export interface TodoItem {
  id: string;
  content: string;
  status: "pending" | "in_progress" | "completed";
  updatedAt?: Date;
}

export interface Thread {
  id: string;
  title: string;
  createdAt: Date;
  updatedAt: Date;
}
