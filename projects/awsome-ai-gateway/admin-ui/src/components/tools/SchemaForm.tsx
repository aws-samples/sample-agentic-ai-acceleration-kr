'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

// A minimal JSON-schema-driven form so operators can fill in tool inputs
// without hand-authoring JSON. Anything we can't render as a simple field
// (nested objects, arrays of objects, …) falls back to a JSON textarea via
// isFullyRenderable().

interface JsonSchema {
  type?: string;
  properties?: Record<string, JsonSchema>;
  required?: string[];
  description?: string;
  enum?: unknown[];
  default?: unknown;
  items?: JsonSchema;
  title?: string;
}

interface SchemaFormProps {
  schema: JsonSchema | null | undefined;
  value: Record<string, unknown>;
  onChange: (next: Record<string, unknown>) => void;
}

const inputClass =
  'flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2';
const textareaClass =
  'flex w-full rounded-md border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2';

// Decide whether a property can be rendered as a friendly field at all.
function isSimple(prop: JsonSchema): boolean {
  const t = prop.type;
  return (
    Array.isArray(prop.enum) ||
    t === 'string' ||
    t === 'number' ||
    t === 'integer' ||
    t === 'boolean' ||
    (t === 'array' &&
      (prop.items?.type === 'string' ||
        prop.items?.type === 'number' ||
        prop.items?.type === 'integer'))
  );
}

export function SchemaForm({ schema, value, onChange }: SchemaFormProps) {
  const props = schema?.properties ?? {};
  const required = schema?.required ?? [];
  const keys = Object.keys(props);

  if (keys.length === 0) {
    return (
      <p className="text-sm text-muted-foreground">이 도구는 입력 파라미터가 없습니다.</p>
    );
  }

  const setField = (key: string, fieldValue: unknown) => {
    const next = { ...value };
    if (fieldValue === undefined || fieldValue === '') {
      delete next[key];
    } else {
      next[key] = fieldValue;
    }
    onChange(next);
  };

  return (
    <div className="space-y-4">
      {keys.map((key) => {
        const prop = props[key];
        const isRequired = required.includes(key);
        const current = value[key];

        const labelEl = (
          <label htmlFor={`field-${key}`} className="flex items-center gap-1 text-sm">
            <span className="font-medium">{prop.title ?? key}</span>
            {isRequired && <span className="text-destructive">*</span>}
            {prop.type && (
              <span className="text-xs font-normal text-muted-foreground">({prop.type})</span>
            )}
          </label>
        );

        // ---- enum -> dropdown ----
        if (Array.isArray(prop.enum)) {
          return (
            <div key={key} className="space-y-1.5">
              {labelEl}
              <select
                id={`field-${key}`}
                value={current === undefined ? '' : String(current)}
                onChange={(e) => setField(key, e.target.value || undefined)}
                className={inputClass}
              >
                <option value="">선택하세요…</option>
                {prop.enum.map((opt) => (
                  <option key={String(opt)} value={String(opt)}>
                    {String(opt)}
                  </option>
                ))}
              </select>
              {prop.description && (
                <p className="text-xs text-muted-foreground">{prop.description}</p>
              )}
            </div>
          );
        }

        // ---- boolean -> checkbox ----
        if (prop.type === 'boolean') {
          return (
            <div key={key} className="space-y-1.5">
              <label className="flex items-center gap-2 text-sm">
                <input
                  id={`field-${key}`}
                  type="checkbox"
                  checked={Boolean(current)}
                  onChange={(e) => setField(key, e.target.checked)}
                  className="h-4 w-4 rounded border-input"
                />
                <span className="font-medium">{prop.title ?? key}</span>
                {isRequired && <span className="text-destructive">*</span>}
              </label>
              {prop.description && (
                <p className="text-xs text-muted-foreground">{prop.description}</p>
              )}
            </div>
          );
        }

        // ---- number / integer -> numeric input ----
        if (prop.type === 'number' || prop.type === 'integer') {
          return (
            <div key={key} className="space-y-1.5">
              {labelEl}
              <input
                id={`field-${key}`}
                type="number"
                step={prop.type === 'integer' ? 1 : 'any'}
                value={current === undefined ? '' : String(current)}
                placeholder={prop.description}
                className={inputClass}
                onChange={(e) => {
                  const raw = e.target.value;
                  if (raw === '') return setField(key, undefined);
                  const num = Number(raw);
                  setField(key, Number.isNaN(num) ? raw : num);
                }}
              />
              {prop.description && (
                <p className="text-xs text-muted-foreground">{prop.description}</p>
              )}
            </div>
          );
        }

        // ---- array of scalars -> newline-separated textarea ----
        if (prop.type === 'array') {
          const asText = Array.isArray(current) ? current.join('\n') : '';
          const isNumeric =
            prop.items?.type === 'number' || prop.items?.type === 'integer';
          return (
            <div key={key} className="space-y-1.5">
              {labelEl}
              <textarea
                id={`field-${key}`}
                value={asText}
                placeholder="한 줄에 하나씩 입력"
                rows={3}
                className={textareaClass}
                onChange={(e) => {
                  const items = e.target.value
                    .split('\n')
                    .map((s) => s.trim())
                    .filter((s) => s.length > 0)
                    .map((s) => (isNumeric ? Number(s) : s));
                  setField(key, items.length > 0 ? items : undefined);
                }}
              />
              <p className="text-xs text-muted-foreground">
                {prop.description ? `${prop.description} · ` : ''}한 줄에 하나씩 입력하세요.
              </p>
            </div>
          );
        }

        // ---- default: string (long strings get a textarea) ----
        const isLong = /body|content|text|prompt|query|description/i.test(key);
        return (
          <div key={key} className="space-y-1.5">
            {labelEl}
            {isLong ? (
              <textarea
                id={`field-${key}`}
                value={current === undefined ? '' : String(current)}
                placeholder={prop.description}
                rows={3}
                className={textareaClass}
                onChange={(e) => setField(key, e.target.value || undefined)}
              />
            ) : (
              <input
                id={`field-${key}`}
                value={current === undefined ? '' : String(current)}
                placeholder={prop.description}
                className={inputClass}
                onChange={(e) => setField(key, e.target.value || undefined)}
              />
            )}
            {prop.description && (
              <p className="text-xs text-muted-foreground">{prop.description}</p>
            )}
          </div>
        );
      })}
    </div>
  );
}

// True when every property in the schema can be rendered as a friendly field.
// If not, the page should offer the raw JSON editor instead.
export function isFullyRenderable(schema: JsonSchema | null | undefined): boolean {
  const props = schema?.properties;
  if (!props || Object.keys(props).length === 0) return true;
  return Object.values(props).every(isSimple);
}
