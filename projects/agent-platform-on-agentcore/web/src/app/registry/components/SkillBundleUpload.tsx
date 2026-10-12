"use client";

import { useCallback, useRef, useState } from "react";
import {
  AlertTriangle,
  FileCode2,
  FileText,
  FolderArchive,
  Loader2,
  Pencil,
  Upload,
  XCircle,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { cn } from "@/lib/utils";
import {
  SKILL_UPLOAD_ACCEPT,
  formatBytes,
  skillMarkdownFile,
  validateSkillBundle,
  type SkillBundleInfo,
} from "@/lib/skills";

/**
 * Picks a skill bundle and reports whether it satisfies the AgentSkills spec.
 *
 * The check runs server-side against the same validator that publishing uses, so
 * the report shown here is exactly what the create call will decide on — no
 * client-side approximation that can disagree with it.
 *
 * Two entry styles, one code path: a file (a `.zip` for a real skill directory, or
 * a single `.md`) or pasted markdown, which is wrapped as a `SKILL.md` file before
 * it is sent. Pasting exists because a one-file skill should not need a zip, and
 * because it is what the old inline field was good at.
 */
export interface SkillBundleSelection {
  file: File;
  info: SkillBundleInfo;
}

interface SkillBundleUploadProps {
  /** Called with the validated selection, or null when it is cleared or invalid. */
  onChange: (selection: SkillBundleSelection | null) => void;
  /** Locks the name: set when replacing an existing record's bundle. */
  expectedName?: string;
  disabled?: boolean;
}

export function SkillBundleUpload({
  onChange,
  expectedName,
  disabled,
}: SkillBundleUploadProps) {
  const fileInput = useRef<HTMLInputElement>(null);
  const [mode, setMode] = useState<"file" | "paste">("file");
  const [markdown, setMarkdown] = useState("");
  const [filename, setFilename] = useState<string | null>(null);
  const [info, setInfo] = useState<SkillBundleInfo | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const [checking, setChecking] = useState(false);
  const [dragging, setDragging] = useState(false);

  const check = useCallback(
    async (file: File) => {
      setChecking(true);
      setFailure(null);
      setFilename(file.name);
      try {
        const result = await validateSkillBundle(file);
        setInfo(result);
        // A replace is keyed on the record's name, so a bundle that renames the
        // skill is unusable here even though the bundle itself is valid. Held
        // back rather than left for the server to reject on submit.
        const usable =
          result.errors.length === 0 &&
          (!expectedName || result.name === expectedName);
        onChange(usable ? { file, info: result } : null);
      } catch (e) {
        setInfo(null);
        setFailure(e instanceof Error ? e.message : String(e));
        onChange(null);
      } finally {
        setChecking(false);
      }
    },
    [onChange, expectedName]
  );

  const clear = () => {
    setFilename(null);
    setInfo(null);
    setFailure(null);
    setMarkdown("");
    if (fileInput.current) fileInput.current.value = "";
    onChange(null);
  };

  const switchMode = (next: "file" | "paste") => {
    if (next === mode) return;
    clear();
    setMode(next);
  };

  // A mismatch here is fatal for a replace even though the bundle itself is
  // valid, so it is surfaced next to the spec errors rather than only on submit.
  const nameMismatch =
    expectedName && info && info.name && info.name !== expectedName
      ? `이 레코드의 이름은 '${expectedName}' 인데 업로드한 SKILL.md의 name은 '${info.name}' 입니다. 이름을 바꾸려면 새 스킬로 등록해주세요.`
      : null;

  const errors = [...(info?.errors ?? []), ...(nameMismatch ? [nameMismatch] : [])];

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <p className="caps-label-xs text-muted-foreground">Skill bundle</p>
        <Button
          type="button"
          variant="ghost"
          size="sm"
          className="h-6 gap-1 px-1.5 text-xs"
          disabled={disabled}
          onClick={() => switchMode(mode === "file" ? "paste" : "file")}
        >
          {mode === "file" ? (
            <>
              <Pencil className="size-3" />
              직접 작성
            </>
          ) : (
            <>
              <Upload className="size-3" />
              파일 올리기
            </>
          )}
        </Button>
      </div>

      {mode === "file" ? (
        <>
          <input
            ref={fileInput}
            type="file"
            className="hidden"
            accept={SKILL_UPLOAD_ACCEPT}
            onChange={(e) => {
              const file = e.target.files?.[0];
              if (file) void check(file);
            }}
          />
          <button
            type="button"
            disabled={disabled || checking}
            onClick={() => fileInput.current?.click()}
            onDragOver={(e) => {
              e.preventDefault();
              setDragging(true);
            }}
            onDragLeave={() => setDragging(false)}
            onDrop={(e) => {
              e.preventDefault();
              setDragging(false);
              const file = e.dataTransfer.files?.[0];
              if (file) void check(file);
            }}
            className={cn(
              "flex w-full flex-col items-center gap-1.5 rounded-md border border-dashed px-4 py-6 text-center transition-colors",
              dragging
                ? "border-primary bg-primary-tint"
                : "border-border hover:border-primary/40 hover:bg-muted/50",
              (disabled || checking) && "pointer-events-none opacity-60"
            )}
          >
            {checking ? (
              <Loader2 className="size-5 animate-spin text-muted-foreground" />
            ) : (
              <FolderArchive className="size-5 text-muted-foreground" />
            )}
            {/* An uploaded filename is arbitrary text with no guaranteed break
                opportunity, so it has to be allowed to break mid-word. */}
            <span className="max-w-full break-all text-sm font-medium">
              {filename ?? "zip 또는 md 파일을 끌어다 놓기"}
            </span>
            <span className="text-xs leading-relaxed text-muted-foreground">
              SKILL.md 한 개면 <code>.md</code>, references/ · scripts/ 같은 폴더가
              있으면 폴더를 <code>.zip</code> 으로 압축해 올려주세요. 업로드하면
              S3에 저장되고 레코드가 그 위치를 가리킵니다.
            </span>
          </button>
        </>
      ) : (
        <>
          <Textarea
            rows={10}
            className="font-mono text-xs"
            placeholder={
              "---\nname: my-skill\ndescription: 무엇을 하고 언제 쓰는지 적어주세요.\n---\n\n# My Skill\n"
            }
            value={markdown}
            disabled={disabled}
            onChange={(e) => {
              setMarkdown(e.target.value);
              // Editing invalidates the last check; re-validate explicitly.
              setInfo(null);
              setFailure(null);
              onChange(null);
            }}
          />
          <Button
            type="button"
            variant="outline"
            size="sm"
            className="w-full"
            disabled={disabled || checking || !markdown.trim()}
            onClick={() => void check(skillMarkdownFile(markdown))}
          >
            {checking && <Loader2 className="size-3.5 animate-spin" />}
            형식 검사
          </Button>
        </>
      )}

      {failure && (
        <p className="break-words rounded-md border border-destructive/50 bg-destructive/10 p-2.5 text-xs text-destructive">
          {failure}
        </p>
      )}

      {info && (
        <div className="space-y-2.5 rounded-md border border-border p-3">
          {errors.length > 0 ? (
            <div className="space-y-1.5">
              <p className="flex items-center gap-1.5 text-xs font-medium text-destructive">
                <XCircle className="size-3.5 shrink-0" />
                AgentSkills 형식을 만족하지 않습니다
              </p>
              {/* Messages quote back what the user wrote — a rejected name, a
                  path from the archive — so they carry unbreakable runs. */}
              <ul className="ml-5 list-disc space-y-1 break-words text-xs leading-relaxed text-destructive">
                {errors.map((error) => (
                  <li key={error}>{error}</li>
                ))}
              </ul>
            </div>
          ) : (
            <div className="space-y-1">
              {/* A skill name may be 64 unbroken characters, and a description
                  1024 — both are the user's text, not ours to assume wraps. */}
              <p className="break-all font-mono text-xs font-medium">{info.name}</p>
              <p className="break-words text-xs leading-relaxed text-muted-foreground">
                {info.description}
              </p>
            </div>
          )}

          {info.files.length > 0 && (
            <div>
              <p className="mb-1.5 caps-label-xs text-muted-foreground">
                Files ({info.files.length}) · {formatBytes(info.total_bytes)}
              </p>
              <ul className="divide-y divide-border overflow-hidden rounded-md border border-border">
                {info.files.map((file) => (
                  <li
                    key={file.path}
                    className="flex items-center gap-2 px-2.5 py-1.5"
                  >
                    {file.path.endsWith(".md") ? (
                      <FileText className="size-3 shrink-0 text-muted-foreground" />
                    ) : (
                      <FileCode2 className="size-3 shrink-0 text-muted-foreground" />
                    )}
                    {/* Truncated rather than wrapped, so the list stays one row
                        per file; `title` keeps the full path reachable. */}
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
            </div>
          )}

          {info.warnings.length > 0 && (
            <ul className="space-y-1">
              {info.warnings.map((warning) => (
                <li
                  key={warning}
                  className="flex gap-1.5 text-xs leading-relaxed text-warning"
                >
                  <AlertTriangle className="mt-0.5 size-3 shrink-0" />
                  <span className="min-w-0 break-words">{warning}</span>
                </li>
              ))}
            </ul>
          )}

          {(filename || info.files.length > 0) && (
            <Button
              type="button"
              variant="ghost"
              size="sm"
              className="h-6 px-1.5 text-xs text-muted-foreground"
              disabled={disabled}
              onClick={clear}
            >
              지우고 다시 선택
            </Button>
          )}
        </div>
      )}
    </div>
  );
}
