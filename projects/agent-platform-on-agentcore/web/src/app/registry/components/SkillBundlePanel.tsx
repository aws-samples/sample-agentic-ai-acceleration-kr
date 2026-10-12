"use client";

import { useCallback, useEffect, useState } from "react";
import {
  FileCode2,
  FileText,
  Loader2,
  RefreshCw,
  Upload,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import type { RegistryRecordDetail } from "@/lib/registry";
import {
  formatBytes,
  listSkillFiles,
  replaceSkillBundle,
  type SkillFile,
} from "@/lib/skills";
import {
  SkillBundleUpload,
  type SkillBundleSelection,
} from "./SkillBundleUpload";

/**
 * A skill record's published bundle: what is actually in S3, plus a replace flow.
 *
 * The file list is read from the prefix rather than from the record's own
 * snapshot, so it tells the truth about what a harness will fetch even if the two
 * ever diverge.
 */
export function SkillBundlePanel({
  record,
  isAdmin,
  onReplaced,
}: {
  record: RegistryRecordDetail;
  isAdmin: boolean;
  /** Lets the parent refresh the record after a new revision is drafted. */
  onReplaced: () => void;
}) {
  const [files, setFiles] = useState<SkillFile[]>([]);
  const [uri, setUri] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [replacing, setReplacing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [bundle, setBundle] = useState<SkillBundleSelection | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const response = await listSkillFiles(record.record_id);
      setUri(response.uri);
      setFiles(response.files);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [record.record_id]);

  useEffect(() => {
    void load();
  }, [load]);

  const replace = async () => {
    if (!bundle) return;
    setSaving(true);
    setError(null);
    try {
      await replaceSkillBundle(record.record_id, bundle.file);
      setReplacing(false);
      setBundle(null);
      await load();
      onReplaced();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  };

  // Deprecation is terminal in AWS, so there is no revision to draft.
  const editable = isAdmin && record.status !== "DEPRECATED";

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between gap-2">
        <p className="caps-label-xs text-muted-foreground">
          Bundle{files.length > 0 ? ` (${files.length})` : ""}
        </p>
        {editable && !replacing && (
          <Button
            size="sm"
            variant="ghost"
            className="h-6 gap-1 px-1.5 text-xs"
            onClick={() => setReplacing(true)}
          >
            <RefreshCw className="size-3" />
            번들 교체
          </Button>
        )}
      </div>

      {error && (
        <p className="break-words rounded-md border border-destructive/50 bg-destructive/10 p-2.5 text-xs text-destructive">
          {error}
        </p>
      )}

      {loading ? (
        <Loader2 className="size-4 animate-spin text-muted-foreground" />
      ) : uri === null ? (
        <p className="text-xs leading-relaxed text-muted-foreground">
          이 스킬은 SKILL.md를 직접 입력해 등록되어 번들 저장소가 없습니다. 번들을
          올리면 references/ · scripts/ 같은 폴더까지 함께 배포됩니다.
        </p>
      ) : (
        <>
          <ul className="divide-y divide-border overflow-hidden rounded-md border border-border">
            {files.map((file) => (
              <li key={file.path} className="flex items-center gap-2 px-2.5 py-1.5">
                {file.path.endsWith(".md") ? (
                  <FileText className="size-3 shrink-0 text-muted-foreground" />
                ) : (
                  <FileCode2 className="size-3 shrink-0 text-muted-foreground" />
                )}
                <span
                  title={file.path}
                  className="min-w-0 flex-1 truncate font-mono text-xs"
                >
                  {file.path}
                </span>
                <span className="shrink-0 text-xs text-muted-foreground">
                  {formatBytes(file.size_bytes)}
                </span>
              </li>
            ))}
          </ul>
          <p className="break-all font-mono text-xs text-muted-foreground">{uri}</p>
        </>
      )}

      {replacing && (
        <div className="space-y-2 rounded-md border border-border p-3">
          <SkillBundleUpload
            onChange={setBundle}
            expectedName={record.name}
            disabled={saving}
          />
          <p className="text-xs leading-relaxed text-muted-foreground">
            교체하면 S3의 번들이 새 내용으로 대체되고, 이전 번들에만 있던 파일은
            삭제됩니다. 승인된 레코드는 새 초안 리비전이 만들어집니다.
          </p>
          <div className="flex gap-1.5">
            <Button size="sm" disabled={saving || !bundle} onClick={replace}>
              {saving ? (
                <Loader2 className="size-3.5 animate-spin" />
              ) : (
                <Upload className="size-3.5" />
              )}
              교체
            </Button>
            <Button
              size="sm"
              variant="outline"
              disabled={saving}
              onClick={() => {
                setReplacing(false);
                setBundle(null);
              }}
            >
              취소
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
