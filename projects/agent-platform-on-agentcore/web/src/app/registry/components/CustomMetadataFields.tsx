"use client";

import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import type { CustomMetadataValue, MetadataField } from "@/lib/registry";

/**
 * Radix Select rejects an empty-string item value, so the "clear" option carries
 * a sentinel instead. It never reaches the value map.
 */
const CLEAR = "__clear__";

interface CustomMetadataFieldsProps {
  fields: MetadataField[];
  value: CustomMetadataValue;
  onChange: (next: CustomMetadataValue) => void;
  disabled?: boolean;
}

/**
 * One control per schema field of a record type. Renders nothing when the type
 * has no custom metadata, so callers can drop it in unconditionally.
 */
export function CustomMetadataFields({
  fields,
  value,
  onChange,
  disabled,
}: CustomMetadataFieldsProps) {
  if (fields.length === 0) return null;

  // "Not set" is an absent key. Clearing a control removes the key, so the map
  // never accumulates empty strings that the server would have to reject.
  const set = (name: string, next: string | boolean | undefined) => {
    const copy: CustomMetadataValue = { ...value };
    if (next === undefined || next === "") delete copy[name];
    else copy[name] = next;
    onChange(copy);
  };

  return (
    <div className="grid gap-3">
      {fields.map((field) => {
        const id = `meta-${field.name}`;
        const current = value[field.name];
        const label = (
          <Label htmlFor={id}>
            {field.name}
            {field.required && <span className="text-destructive"> *</span>}
          </Label>
        );

        if (field.kind === "boolean") {
          return (
            <div
              key={field.name}
              className="flex items-center justify-between gap-3 rounded-md border border-border px-3 py-2"
            >
              {label}
              <Switch
                id={id}
                checked={current === true}
                onCheckedChange={(checked) => set(field.name, checked)}
                disabled={disabled}
              />
            </div>
          );
        }

        if (field.kind === "enum") {
          return (
            <div key={field.name} className="grid gap-1.5">
              {label}
              <Select
                value={typeof current === "string" ? current : ""}
                onValueChange={(v) => set(field.name, v === CLEAR ? undefined : v)}
                disabled={disabled}
              >
                <SelectTrigger id={id}>
                  <SelectValue placeholder="— 선택 안 함 —" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value={CLEAR}>— 선택 안 함 —</SelectItem>
                  {field.options?.map((option) => (
                    <SelectItem key={option} value={option}>
                      {option}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          );
        }

        // text and url share the input; the url kind only changes the keyboard and
        // validation the browser applies.
        return (
          <div key={field.name} className="grid gap-1.5">
            {label}
            <Input
              id={id}
              type={field.kind === "url" ? "url" : "text"}
              maxLength={128}
              placeholder={field.kind === "url" ? "https://" : undefined}
              value={typeof current === "string" ? current : ""}
              onChange={(e) => set(field.name, e.target.value)}
              disabled={disabled}
            />
          </div>
        );
      })}
    </div>
  );
}
