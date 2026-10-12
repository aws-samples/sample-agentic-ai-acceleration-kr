/**
 * Turning an MCP tool's JSON Schema into form fields, and back into arguments.
 *
 * Shared by the MCP Inspector page and the registry panel's tool try-out so the
 * two cannot disagree about what a schema means — they call the same tools and
 * would otherwise send differently-typed arguments for the same input.
 */

export interface ArgumentField {
  key: string;
  /** The property's schema, with `required` folded in from the parent. */
  schema: any;
  value: any;
}

export interface McpToolInfo {
  name: string;
  description?: string;
  input_schema?: Record<string, any>;
  /** `_meta`; carries the MCP Apps `ui` block when the tool has an app. */
  meta?: Record<string, any>;
}

/**
 * Pull `{properties, required}` out of the shapes servers actually send.
 *
 * Standard JSON Schema is `{type, properties, required}`, but real servers also
 * send a bare property map with no `type`, or nest it under `parameters`. Guessing
 * wrong renders an empty form for a tool that does take arguments.
 */
function readProperties(
  inputSchema: Record<string, any> | undefined
): { properties?: Record<string, any>; required: string[] } {
  if (!inputSchema || typeof inputSchema !== "object") return { required: [] };

  if (inputSchema.properties) {
    return {
      properties: inputSchema.properties,
      required: Array.isArray(inputSchema.required) ? inputSchema.required : [],
    };
  }

  if (inputSchema.parameters) {
    return {
      properties: inputSchema.parameters,
      required: Array.isArray(inputSchema.required) ? inputSchema.required : [],
    };
  }

  if (!inputSchema.type) {
    // A flat property map: accepted only when the values look like schemas,
    // so an unrelated object isn't rendered as a form.
    const keys = Object.keys(inputSchema);
    const looksLikeSchemas = keys.some(
      (k) => inputSchema[k] && typeof inputSchema[k] === "object"
    );
    if (keys.length > 0 && looksLikeSchemas) {
      return { properties: inputSchema, required: [] };
    }
  }

  return { required: [] };
}

/** The empty value a type starts at. Objects and arrays are edited as JSON text. */
function initialValue(schema: any): any {
  if (schema.default !== undefined) return schema.default;
  if (schema.type === "boolean") return false;
  return "";
}

/** Form fields for a tool's input schema, in schema order. */
export function buildArgumentFields(
  inputSchema: Record<string, any> | undefined
): ArgumentField[] {
  const { properties, required } = readProperties(inputSchema);
  if (!properties) return [];

  return Object.keys(properties).map((key) => {
    const prop = properties[key];
    // A non-object property value is a literal default, not a schema.
    const propSchema =
      prop && typeof prop === "object" ? prop : { type: typeof prop, default: prop };
    return {
      key,
      schema: { ...propSchema, required: required.includes(key) },
      value: initialValue(propSchema),
    };
  });
}

/** How many arguments a tool takes, for the collapsed row's label. */
export function countArguments(
  inputSchema: Record<string, any> | undefined
): number {
  const { properties } = readProperties(inputSchema);
  return properties ? Object.keys(properties).length : 0;
}

/**
 * Form fields back into a `tools/call` arguments object.
 *
 * Empty optional fields are dropped rather than sent as `""`: a server that
 * treats absent and empty differently (a filter, a path) would otherwise get a
 * value the user never typed. Required fields are always sent, so the server's
 * own validation reports what is missing instead of this code guessing.
 */
export function buildArguments(fields: ArgumentField[]): Record<string, any> {
  const args: Record<string, any> = {};

  for (const field of fields) {
    const { schema } = field;
    const empty = field.value === undefined || field.value === "";
    if (empty && schema.required !== true) continue;

    let value = field.value;
    if (schema.type === "number" || schema.type === "integer") {
      if (typeof value === "string" && value.trim() !== "") {
        const parsed =
          schema.type === "integer" ? parseInt(value, 10) : parseFloat(value);
        // A number the server rejects is a clearer error than a silent NaN.
        value = Number.isNaN(parsed) ? field.value : parsed;
      }
    } else if (schema.type === "boolean") {
      value = Boolean(value);
    } else if (schema.type === "array" && typeof value === "string") {
      try {
        value = JSON.parse(value);
      } catch {
        // Unparseable text is taken as a single item, which is what someone
        // typing one value into an array field means.
        value = value ? [value] : [];
      }
    } else if (schema.type === "object" && typeof value === "string") {
      try {
        value = JSON.parse(value);
      } catch {
        value = {};
      }
    }

    args[field.key] = value;
  }

  return args;
}

/** The `text` of every text block in a `CallToolResult`, in order. */
export function textBlocks(result: unknown): string[] {
  const content = (result as { content?: unknown })?.content;
  if (!Array.isArray(content)) return [];
  return content
    .filter(
      (block): block is { type: string; text: string } =>
        Boolean(block) &&
        typeof block === "object" &&
        (block as { type?: string }).type === "text" &&
        typeof (block as { text?: string }).text === "string"
    )
    .map((block) => block.text);
}

/** A tool that ran but reported failure. Its text is an error message, not data. */
export function resultIsError(result: unknown): boolean {
  return (result as { isError?: unknown })?.isError === true;
}

/**
 * How a tool's return value should be presented.
 *
 * `raw` is what the server actually sent, kept alongside the parsed value so the
 * panel can offer it: a pretty tree of re-serialised JSON is not evidence of the
 * bytes on the wire, which is exactly what someone probing a server wants to see.
 */
export type ResultView =
  | { kind: "json"; value: object; raw: string }
  | { kind: "text"; text: string }
  | { kind: "empty" };

/** Only objects and arrays get the tree; a lone `42` or `"ok"` reads as text. */
function isTreeable(value: unknown): value is object {
  return typeof value === "object" && value !== null;
}

function parseJson(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return undefined;
  }
}

/*
 * A strong marker is enough on its own: none of these appear in output that was
 * not written as Markdown.
 */
const MD_FENCE = /^ {0,3}(```|~~~)/m;
const MD_HEADING = /^ {0,3}#{1,6}[ \t]+\S/m;
const MD_TABLE_ROW = /^ {0,3}\|.*\|[ \t]*$/m;
/* The delimiter row is what makes a pipe-separated block a table rather than
   ASCII art, so both it and a body row are required. */
const MD_TABLE_RULE =
  /^ {0,3}\|?[ \t]*:?-{3,}:?[ \t]*(\|[ \t]*:?-{3,}:?[ \t]*)+\|?[ \t]*$/m;

/* A weak marker can occur by accident, so two are needed. */
const MD_WEAK = [
  /^ {0,3}[-*+][ \t]+\S/gm, // bullet item
  /^ {0,3}\d+[.)][ \t]+\S/gm, // ordered item
  /^ {0,3}>[ \t]?\S/gm, // blockquote
  /^ {0,3}([-*_])[ \t]*(\1[ \t]*){2,}$/gm, // thematic break
  /\*\*[^*\n]+\*\*/g, // bold
  /`[^`\n]+`/g, // inline code
  /!?\[[^\]\n]*\]\([^)\s]+\)/g, // link or image
];

/**
 * Whether a text result reads better rendered as Markdown than printed.
 *
 * Deliberately conservative, because the two mistakes are not symmetrical:
 * Markdown joins single newlines into one paragraph, so guessing wrong on a
 * stack trace, a log tail or aligned CLI output destroys the line structure that
 * made it readable — while a missed document only loses formatting it never had
 * on screen anyway. So the test is for structure a writer put there on purpose,
 * not for a stray `*`.
 */
export function looksLikeMarkdown(text: string): boolean {
  if (!text.trim()) return false;

  if (MD_FENCE.test(text) || MD_HEADING.test(text)) return true;
  if (MD_TABLE_RULE.test(text) && MD_TABLE_ROW.test(text)) return true;

  let weak = 0;
  for (const pattern of MD_WEAK) {
    weak += text.match(pattern)?.length ?? 0;
    if (weak >= 2) return true;
  }
  return false;
}

/**
 * Decide how to render a tool's return value.
 *
 * Almost every MCP tool answers with JSON stuffed into a text block, so treating
 * `content[].text` as prose showed a wall of one-line JSON — the thing that
 * prompted this. Order of preference:
 *
 *   1. `structuredContent`, the spec's typed field, when the server sets it.
 *   2. Text blocks that all parse as JSON — several blocks become an array,
 *      which is how gateways return one JSON document per upstream result.
 *   3. The text as-is: genuine prose, or JSON too broken to parse. Both are
 *      better shown verbatim than guessed at.
 *   4. Anything else (image or resource blocks only): the whole result object,
 *      so nothing is silently dropped.
 *
 * A bare string counts as a single text block, which is what lets step 2 reach
 * the chat: a tool result there is not a `CallToolResult` but one string — the
 * harness path joins its `toolResult` deltas and `json.dumps` them (see
 * harness_event_adapter._tool_result_chunk), so the chat receives exactly the
 * one-line JSON this function exists to unwrap.
 */
export function resultView(result: unknown): ResultView {
  const blocks = (typeof result === "string" ? [result] : textBlocks(result))
    .map((text) => text.trim())
    .filter(Boolean);
  const raw = blocks.join("\n");

  const structured = (result as { structuredContent?: unknown })
    ?.structuredContent;
  if (isTreeable(structured)) {
    return {
      kind: "json",
      value: structured,
      raw: raw || JSON.stringify(structured, null, 2),
    };
  }

  if (blocks.length > 0) {
    const parsed = blocks.map(parseJson);
    if (parsed.every(isTreeable)) {
      return {
        kind: "json",
        value: parsed.length === 1 ? parsed[0] : (parsed as object[]),
        raw,
      };
    }
    return { kind: "text", text: raw };
  }

  if (isTreeable(result)) {
    return { kind: "json", value: result, raw: JSON.stringify(result, null, 2) };
  }
  // Whitespace is treated as no answer at all, alongside null: a tool that
  // returned "" or "\n" has nothing to show, and rendering it as text left an
  // empty framed box claiming otherwise.
  const text = result === undefined || result === null ? "" : String(result);
  return text.trim() ? { kind: "text", text } : { kind: "empty" };
}
