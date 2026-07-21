// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * Best-effort parsers for the AgentCore Gateway's vended-log payloads.
 *
 * The gateway logs `requestBody`/`responseBody` as a Java-style map dump
 * (e.g. `{id=3, jsonrpc=2.0, params={name=serper, arguments={query=test}}}`),
 * not JSON. These helpers reconstruct the pieces the trace-detail view needs
 * (method, tool name, arguments, embedded error/latency, a re-indented raw
 * view). Everything degrades gracefully — callers tolerate missing fields.
 */

/**
 * Parse a Java-style map dump into a nested object. Anything unparseable
 * degrades to a string.
 */
export function parseJavaMap(text: string): Record<string, unknown> {
  const trimmed = text.trim();
  if (!trimmed.startsWith('{') || !trimmed.endsWith('}')) return {};
  return parseMapBody(trimmed.slice(1, -1));
}

function parseMapBody(body: string): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  let lastKey: string | null = null;
  for (const segment of splitTopLevel(body)) {
    const eq = segment.indexOf('=');
    if (eq === -1) {
      // Continuation of a previous value that contained a top-level comma.
      if (lastKey !== null && typeof out[lastKey] === 'string') {
        out[lastKey] = `${out[lastKey]}, ${segment.trim()}`;
      }
      continue;
    }
    const key = segment.slice(0, eq).trim();
    const rawValue = segment.slice(eq + 1).trim();
    out[key] = parseValue(rawValue);
    lastKey = key;
  }
  return out;
}

function parseValue(raw: string): unknown {
  if (raw.startsWith('{') && raw.endsWith('}')) return parseMapBody(raw.slice(1, -1));
  return raw;
}

// Split on top-level commas, respecting nested {} and [] depth.
function splitTopLevel(body: string): string[] {
  const parts: string[] = [];
  let depth = 0;
  let start = 0;
  for (let i = 0; i < body.length; i++) {
    const c = body[i];
    if (c === '{' || c === '[') depth++;
    else if (c === '}' || c === ']') depth--;
    else if (c === ',' && depth === 0) {
      parts.push(body.slice(start, i));
      start = i + 1;
    }
  }
  if (start < body.length) parts.push(body.slice(start));
  return parts;
}

// requestBody → full tool name (params.name) for tools/call, else null.
export function extractToolName(requestBody: string): string | null {
  const parsed = parseJavaMap(requestBody);
  if (parsed.method !== 'tools/call') return null;
  const params = parsed.params;
  if (params && typeof params === 'object' && 'name' in params) {
    const name = (params as Record<string, unknown>).name;
    return typeof name === 'string' ? name : null;
  }
  return null;
}

// requestBody → params.arguments object (string values), else {}.
export function extractArguments(requestBody: string): Record<string, unknown> {
  const parsed = parseJavaMap(requestBody);
  const params = parsed.params;
  if (params && typeof params === 'object' && 'arguments' in params) {
    const args = (params as Record<string, unknown>).arguments;
    if (args && typeof args === 'object') return args as Record<string, unknown>;
  }
  return {};
}

// responseBody → embedded tool-level error ("error":"..."), else null.
export function extractToolError(responseBody: string): string | null {
  const m = /"error":"((?:[^"\\]|\\.)*?)"/.exec(responseBody);
  if (m && m[1] && m[1] !== 'null') return m[1].replace(/\\"/g, '"');
  return null;
}

// responseBody → embedded "latency_ms":<number>, else null.
export function extractLatencyMs(responseBody: string): number | null {
  const m = /"latency_ms":(\d+)/.exec(responseBody);
  return m ? Number(m[1]) : null;
}

// responseBody → the tool result's `text` payload (the real JSON the tool
// returned). Logged as `content=[{type=text, text={...json...}}]`; we locate
// `text=` and balance-match the following `{...}`.
export function extractResponseText(responseBody: string): string | null {
  const marker = 'text=';
  const at = responseBody.indexOf(marker);
  if (at === -1) return null;
  const start = at + marker.length;
  if (responseBody[start] !== '{') return null;
  let depth = 0;
  for (let i = start; i < responseBody.length; i++) {
    const c = responseBody[i];
    if (c === '{') depth++;
    else if (c === '}' && --depth === 0) return responseBody.slice(start, i + 1);
  }
  return null;
}

/**
 * Pretty-print a gateway body for display. Bodies are Java-style map dumps
 * (not valid JSON) that may embed real JSON inside a `text={...}` value, so we
 * re-indent purely from structural punctuation rather than parsing to an
 * object. Characters inside quoted strings are emitted verbatim. Works for
 * plain JSON too.
 */
export function prettyPrintBody(body: string): string {
  const text = body.trim();
  if (!text) return body;

  const INDENT = '  ';
  let out = '';
  let depth = 0;
  let inString = false;
  let quote = '';

  const newline = (d: number) => '\n' + INDENT.repeat(Math.max(0, d));

  for (let i = 0; i < text.length; i++) {
    const c = text[i];

    if (inString) {
      out += c;
      if (c === '\\' && i + 1 < text.length) {
        out += text[++i];
      } else if (c === quote) {
        inString = false;
      }
      continue;
    }

    if (c === '"' || c === "'") {
      inString = true;
      quote = c;
      out += c;
      continue;
    }

    if (c === '{' || c === '[') {
      depth++;
      out += c + newline(depth);
    } else if (c === '}' || c === ']') {
      depth--;
      out += newline(depth) + c;
    } else if (c === ',') {
      out += ',' + newline(depth);
    } else if (c === ' ' && (out.endsWith('\n') || /\s$/.test(out))) {
      continue;
    } else {
      out += c;
    }
  }

  const cleaned = out
    .split('\n')
    .map((line) => line.replace(/\s+$/, ''))
    .filter((line) => line.trim().length > 0)
    .join('\n');

  return cleaned.length > 8000 ? cleaned.slice(0, 8000) + '\n…' : cleaned;
}
