"use client";

import { useCallback, useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Loader2, Trash2 } from "lucide-react";

import {
  deleteBucketSkill,
  listBucketSkills,
  publishBucketSkill,
  type BucketSkill,
} from "@/lib/skills";
import {
  SkillBundleUpload,
  type SkillBundleSelection,
} from "./SkillBundleUpload";

interface BucketSkillsDialogProps {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  /** Called after an upload or delete, so a consumer (e.g. the harness catalog)
   *  can re-read the bucket. */
  onChanged?: () => void;
}

/**
 * Skill management when the registry is off.
 *
 * With no registry there is no AGENT_SKILLS record to create — but the bundle
 * still belongs in the skills bucket, which is exactly what the Agent Harness's
 * skill picker reads (`catalog.bucket_skills`). So this uploads/lists/deletes the
 * bucket directly, reusing the same `SkillBundleUpload` validation the registry-on
 * Register dialog uses.
 */
export function BucketSkillsDialog({
  open,
  onOpenChange,
  onChanged,
}: BucketSkillsDialogProps) {
  const [skills, setSkills] = useState<BucketSkill[]>([]);
  const [bundle, setBundle] = useState<SkillBundleSelection | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setSkills(await listBucketSkills());
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    if (open) {
      setError(null);
      void load();
    }
  }, [open, load]);

  const upload = async () => {
    if (!bundle) return;
    setBusy(true);
    setError(null);
    try {
      await publishBucketSkill(bundle.file);
      setBundle(null);
      await load();
      onChanged?.();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const remove = async (uri: string) => {
    setBusy(true);
    setError(null);
    try {
      await deleteBucketSkill(uri);
      await load();
      onChanged?.();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[85vh] overflow-y-auto sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Skills</DialogTitle>
          <DialogDescription>
            스킬 번들을 버킷에 업로드합니다. Registry 없이도 Agent Harness의 스킬
            선택에서 바로 사용할 수 있습니다.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4 py-2">
          {error && <p className="text-xs text-destructive">{error}</p>}

          {skills.length === 0 ? (
            <p className="text-xs text-muted-foreground">
              업로드된 스킬이 없습니다.
            </p>
          ) : (
            <ul className="divide-y rounded border">
              {skills.map((skill) => (
                <li
                  key={skill.uri}
                  className="flex items-center justify-between gap-2 px-3 py-2"
                >
                  <div className="min-w-0">
                    <p className="truncate text-sm">{skill.name}</p>
                    {skill.description && (
                      <p className="truncate text-xs text-muted-foreground">
                        {skill.description}
                      </p>
                    )}
                  </div>
                  <Button
                    size="icon-sm"
                    variant="ghost"
                    disabled={busy}
                    onClick={() => remove(skill.uri)}
                    aria-label={`${skill.name} 삭제`}
                  >
                    <Trash2 className="size-4" />
                  </Button>
                </li>
              ))}
            </ul>
          )}

          <SkillBundleUpload onChange={setBundle} disabled={busy} />
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Close
          </Button>
          <Button disabled={busy || !bundle} onClick={upload}>
            {busy && <Loader2 className="size-4 animate-spin" />}
            Upload
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
