"use client";

import { useEffect, useState } from "react";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { AlertTriangle, Loader2 } from "lucide-react";
import {
  compactMetadata,
  editableMetadataFields,
  metadataSchemaFor,
  updateRegistryRecord,
  type CustomMetadataValue,
  type RegistryInfo,
  type RegistryRecordDetail,
} from "@/lib/registry";
import { NAME_PATTERN } from "./shared";
import { CustomMetadataFields } from "./CustomMetadataFields";

/**
 * What an edit does to the record depends on its current status: AWS keeps the
 * approved revision in search and opens a new DRAFT alongside it. Spelling that
 * out per status is the difference between "my edit vanished" and understanding
 * the dual-revision model.
 */
function revisionNotice(status?: string | null): string | null {
  switch (status) {
    case "DRAFT":
      return null;
    case "PENDING_APPROVAL":
      return "심사 대기 중인 리비전이 폐기되고 새 DRAFT가 만들어집니다. 다시 제출해야 합니다.";
    case "APPROVED":
      return "검색에는 기존 승인본이 계속 노출됩니다. 새 리비전은 승인 후 반영됩니다.";
    case "REJECTED":
      return "새 DRAFT가 만들어지고, 제출·승인 절차를 다시 거칩니다.";
    default:
      return null;
  }
}

/** Two compacted metadata maps are equal when they hold the same keys and values. */
function sameMetadata(a: CustomMetadataValue, b: CustomMetadataValue): boolean {
  const keys = Object.keys(a);
  return (
    keys.length === Object.keys(b).length && keys.every((key) => a[key] === b[key])
  );
}

interface RecordEditDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  detail: RegistryRecordDetail | null;
  onSaved: (updated: RegistryRecordDetail) => void;
  onError: (message: string) => void;
  /** Supplies the metadata schema the form is built from. */
  info: RegistryInfo | null;
}

export function RecordEditDialog({
  open,
  onOpenChange,
  detail,
  onSaved,
  onError,
  info,
}: RecordEditDialogProps) {
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [meta, setMeta] = useState<CustomMetadataValue>({});
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (open && detail) {
      setName(detail.name);
      setDescription(detail.description ?? "");
      setMeta(detail.custom_metadata ?? {});
      setError(null);
    }
  }, [open, detail]);

  if (!detail) return null;

  const notice = revisionNotice(detail.status);
  const metaFields = metadataSchemaFor(
    info?.custom_metadata_schema,
    detail.descriptor_type
  );
  // The form edits a subset of the schema: `owner` belongs to the server (it
  // keeps the stored value whatever is sent) and retired fields are hidden.
  // `known` below stays the full schema so those values ride along unchanged
  // and an untouched form does not count as a change.
  const formFields = editableMetadataFields(metaFields);
  const nameChanged = name !== detail.name;
  const descriptionChanged = description !== (detail.description ?? "");
  // Compared in compacted form, so an untouched field that is blank on both sides
  // does not count as a change.
  // Keys the schema no longer defines are dropped on save: the map is a full
  // replacement, and a stale key would otherwise be invisible in the form yet
  // keep the record NON_COMPLIANT with no way to clear it. Only applied when
  // the schema is known — without one, nothing is trimmed.
  const known = metaFields.length
    ? new Set(metaFields.map((field) => field.name))
    : null;
  const trim = (value: Record<string, string | boolean>) =>
    known
      ? Object.fromEntries(Object.entries(value).filter(([key]) => known.has(key)))
      : value;
  const nextMeta = compactMetadata(trim(meta)) ?? {};
  const metaChanged = !sameMetadata(
    nextMeta,
    compactMetadata(detail.custom_metadata ?? {}) ?? {}
  );
  const changed = nameChanged || descriptionChanged || metaChanged;

  const save = async () => {
    setSaving(true);
    setError(null);
    try {
      const updated = await updateRegistryRecord(detail.record_id, {
        ...(nameChanged ? { name } : {}),
        ...(descriptionChanged ? { description } : {}),
        // A full replacement map, so `{}` is how the last field gets cleared.
        ...(metaChanged ? { custom_metadata: nextMeta } : {}),
      });
      onSaved(updated);
      onOpenChange(false);
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      setError(msg);
      onError(msg);
    } finally {
      setSaving(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-[560px]">
        <DialogHeader>
          <DialogTitle>Edit record</DialogTitle>
          <DialogDescription>
            이름·설명·메타데이터를 수정합니다. 저장하면 새 DRAFT 리비전이 만들어지며,
            승인본이 있으면 검색과 채팅에 계속 제공됩니다.
          </DialogDescription>
        </DialogHeader>

        {notice && (
          <div className="flex items-start gap-2 rounded-md border border-warning/30 bg-warning/[0.07] px-3 py-2 text-xs text-warning">
            <AlertTriangle className="mt-px size-3.5 shrink-0" />
            <span className="leading-normal">{notice}</span>
          </div>
        )}

        {error && (
          <div className="flex items-start gap-2 rounded-md border border-destructive/30 bg-destructive/[0.07] px-3 py-2 text-xs text-destructive">
            <AlertTriangle className="mt-0.5 h-4 w-4 flex-shrink-0" />
            <span className="whitespace-pre-wrap">{error}</span>
          </div>
        )}

        <div className="grid gap-4 py-2">
          <div className="grid gap-2">
            <Label htmlFor="edit-name">Name</Label>
            <Input
              id="edit-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
            {name.trim() && !NAME_PATTERN.test(name.trim()) && (
              <p className="text-xs text-destructive">
                영문/숫자로 시작하고 영문, 숫자, <code>_ - . /</code> 만 사용할 수
                있습니다 (공백 불가).
              </p>
            )}
            <p className="text-xs text-muted-foreground">
              공백은 쓸 수 없습니다. 검색 관련도에서 이름의 비중이 가장 큽니다.
            </p>
          </div>
          <div className="grid gap-2">
            <Label htmlFor="edit-description">Description</Label>
            <Textarea
              id="edit-description"
              rows={4}
              value={description}
              onChange={(e) => setDescription(e.target.value)}
            />
            <p className="text-xs text-muted-foreground">
              설명은 시맨틱 검색 관련도를 결정합니다. 무엇을 해결하는지 자연어로
              쓰세요.
            </p>
          </div>
          {formFields.length > 0 && (
            <div className="grid gap-2">
              <div>
                <p className="caps-label-xs text-muted-foreground">Custom metadata</p>
                <p className="text-xs text-muted-foreground">
                  검색 필터와 분류에 쓰입니다. 지운 값은 기록에서도 사라집니다.
                  owner 는 등록한 사용자로 고정됩니다.
                </p>
              </div>
              <CustomMetadataFields
                fields={formFields}
                value={meta}
                onChange={setMeta}
                disabled={saving}
              />
            </div>
          )}
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button
            onClick={save}
            disabled={saving || !changed || !NAME_PATTERN.test(name.trim())}
          >
            {saving && <Loader2 className="size-4 animate-spin" />}
            Save
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
