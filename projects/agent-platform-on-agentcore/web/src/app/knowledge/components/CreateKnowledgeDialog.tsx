"use client";

import { useEffect, useState } from "react";
import { FolderOpen, Loader2, Upload } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { cn } from "@/lib/utils";
import {
  createKnowledgeBase,
  listSourceBuckets,
  type KnowledgeSourceType,
  type SourceBucketsInfo,
} from "@/lib/knowledge";

const SOURCES: {
  value: KnowledgeSourceType;
  label: string;
  hint: string;
  icon: typeof Upload;
}[] = [
  {
    value: "UPLOAD",
    label: "파일 업로드",
    hint: "브라우저에서 파일을 올립니다. 올리는 즉시 색인됩니다.",
    icon: Upload,
  },
  {
    value: "S3",
    label: "S3 버킷",
    hint: "전용 폴더가 자동으로 만들어집니다. 사이트에서 올리거나 S3에 직접 넣고 동기화할 수 있습니다.",
    icon: FolderOpen,
  },
];

/**
 * Create form for a knowledge base.
 *
 * Creation returns as soon as the record exists — the AWS resources take three
 * to six minutes — so this closes on success and lets the list report progress
 * rather than holding a spinner open.
 *
 * The source is chosen here and never again: AWS does not allow a data source to
 * change connector type, so the dialog says so rather than letting someone find
 * out later.
 */
export function CreateKnowledgeDialog({
  open,
  onOpenChange,
  isAdmin,
  onCreated,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  isAdmin: boolean;
  onCreated: () => void;
}) {
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [shared, setShared] = useState(false);
  const [sourceType, setSourceType] = useState<KnowledgeSourceType>("UPLOAD");
  const [bucket, setBucket] = useState("");
  const [prefix, setPrefix] = useState("");
  const [info, setInfo] = useState<SourceBucketsInfo | null>(null);
  const [external, setExternal] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Read when the dialog opens rather than on mount: the list only matters here,
  // and an administrator can register a bucket between two visits.
  useEffect(() => {
    if (!open) return;
    let active = true;
    listSourceBuckets()
      .then((found) => active && setInfo(found))
      .catch(() => {
        // As far as this form is concerned a failure is the same as "none
        // registered": either way the S3 option cannot be completed.
        if (active) setInfo({ buckets: [], platform_available: false });
      });
    return () => {
      active = false;
    };
  }, [open]);

  const reset = () => {
    setName("");
    setDescription("");
    setShared(false);
    setSourceType("UPLOAD");
    setBucket("");
    setPrefix("");
    setExternal(false);
    setError(null);
  };

  const submit = async () => {
    setSaving(true);
    setError(null);
    try {
      await createKnowledgeBase({
        name: name.trim(),
        description: description.trim() || undefined,
        shared: isAdmin ? shared : undefined,
        source_type: sourceType,
        source_config:
          sourceType === "S3" && external
            ? { bucket_name: bucket, prefix: prefix.trim() || undefined }
            : undefined,
      });
      reset();
      onOpenChange(false);
      onCreated();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  };

  const incomplete = !name.trim() || (sourceType === "S3" && external && !bucket);

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) reset();
        onOpenChange(next);
      }}
    >
      <DialogContent>
        <DialogHeader>
          <DialogTitle>새 Knowledge Base</DialogTitle>
          <DialogDescription>
            생성에는 3~6분이 걸립니다. 진행 상황은 목록에서 확인할 수 있습니다.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4">
          <div className="grid gap-2">
            <Label>소스</Label>
            <div role="radiogroup" aria-label="소스" className="grid gap-2">
              {SOURCES.map(({ value, label, hint, icon: Icon }) => {
                const active = sourceType === value;
                const unavailable = value === "S3" && info != null && !info.platform_available;
                return (
                  <button
                    key={value}
                    type="button"
                    role="radio"
                    aria-checked={active}
                    disabled={unavailable}
                    onClick={() => setSourceType(value)}
                    className={cn(
                      "flex items-start gap-3 rounded-md border p-3 text-left",
                      "transition-colors duration-150",
                      "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/50",
                      active ? "border-primary bg-primary/5" : "hover:bg-muted/50",
                      unavailable && "cursor-not-allowed opacity-60"
                    )}
                  >
                    <Icon className="mt-0.5 size-3.5 shrink-0 text-muted-foreground" />
                    <span className="min-w-0 flex-1">
                      <span className="block text-sm font-medium">{label}</span>
                      <span className="mt-0.5 block text-xs text-muted-foreground">
                        {unavailable
                          ? "이 환경에는 플랫폼 소스 버킷이 없습니다."
                          : hint}
                      </span>
                    </span>
                  </button>
                );
              })}
            </div>
            <p className="text-xs text-muted-foreground">
              소스는 생성 후 바꿀 수 없습니다.
            </p>
          </div>

          {sourceType === "S3" && (
            <div className="grid gap-3 rounded-md border bg-muted/30 p-3">
              {!external && (
                <p className="text-xs text-muted-foreground">
                  이 Knowledge Base 전용 S3 폴더가 만들어집니다. 파일은 여기서 올리거나,
                  생성 후 표시되는 경로에 직접 넣고 동기화하면 됩니다.
                </p>
              )}
              {isAdmin && (info?.buckets.length ?? 0) > 0 && (
                <div className="flex items-start justify-between gap-4">
                  <div className="space-y-1">
                    <Label htmlFor="kb-external">외부 버킷 연결</Label>
                    <p className="text-xs text-muted-foreground">
                      이미 있는 조직 버킷을 읽기 전용으로 연결합니다.
                    </p>
                  </div>
                  <Switch id="kb-external" checked={external} onCheckedChange={setExternal} />
                </div>
              )}
              {external && (
                <>
                  <div className="grid gap-2">
                    <Label htmlFor="kb-bucket">버킷</Label>
                    <Select value={bucket} onValueChange={setBucket}>
                      <SelectTrigger id="kb-bucket">
                        <SelectValue placeholder="버킷 선택" />
                      </SelectTrigger>
                      <SelectContent>
                        {(info?.buckets ?? []).map((option) => (
                          <SelectItem key={option} value={option}>
                            {option}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </div>
                  <div className="grid gap-2">
                    <Label htmlFor="kb-prefix">접두사</Label>
                    <Input
                      id="kb-prefix"
                      value={prefix}
                      onChange={(e) => setPrefix(e.target.value)}
                      placeholder="exports/"
                    />
                    <p className="text-xs text-muted-foreground">
                      비워두면 버킷 전체를 읽습니다.
                    </p>
                  </div>
                </>
              )}
            </div>
          )}

          <div className="grid gap-2">
            <Label htmlFor="kb-name">Name</Label>
            <Input
              id="kb-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="Product Docs"
            />
          </div>

          <div className="grid gap-2">
            <Label htmlFor="kb-desc">Description</Label>
            <Input
              id="kb-desc"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
            />
          </div>

          {isAdmin && (
            <div className="flex items-start justify-between gap-4 rounded-md border p-3">
              <div className="space-y-1">
                <Label htmlFor="kb-shared">공용으로 만들기</Label>
                <p className="text-xs text-muted-foreground">
                  모든 사용자가 조회하고 에이전트에 붙일 수 있습니다. 파일 추가와
                  삭제는 관리자만 할 수 있습니다.
                </p>
              </div>
              <Switch id="kb-shared" checked={shared} onCheckedChange={setShared} />
            </div>
          )}

          {error && <p className="text-sm text-destructive">{error}</p>}
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            취소
          </Button>
          <Button disabled={saving || incomplete} onClick={submit}>
            {saving && <Loader2 className="size-4 animate-spin" />}
            생성
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
