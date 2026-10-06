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
  updateRegistryRecord,
  type RegistryRecordDetail,
} from "@/lib/registry";
import { NAME_PATTERN } from "./shared";

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

interface RecordEditDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  detail: RegistryRecordDetail | null;
  onSaved: (updated: RegistryRecordDetail) => void;
  onError: (message: string) => void;
}

export function RecordEditDialog({
  open,
  onOpenChange,
  detail,
  onSaved,
  onError,
}: RecordEditDialogProps) {
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (open && detail) {
      setName(detail.name);
      setDescription(detail.description ?? "");
      setError(null);
    }
  }, [open, detail]);

  if (!detail) return null;

  const notice = revisionNotice(detail.status);
  const changed =
    name !== detail.name || description !== (detail.description ?? "");

  const save = async () => {
    setSaving(true);
    setError(null);
    try {
      const updated = await updateRegistryRecord(detail.record_id, {
        ...(name !== detail.name ? { name } : {}),
        ...(description !== (detail.description ?? "") ? { description } : {}),
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
            이름과 설명을 수정합니다. 저장하면 상태가 DRAFT로 바뀝니다.
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
