"use client";

import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Loader2 } from "lucide-react";
import {
  compactMetadata,
  editableMetadataFields,
  createRegistryRecord,
  listAgentRuntimes,
  listGateways,
  metadataSchemaFor,
  type AgentRuntimeSummary,
  type CustomMetadataValue,
  type DescriptorType,
  type GatewaySummary,
  type RegistryInfo,
} from "@/lib/registry";
import { createSkillRecord } from "@/lib/skills";
import { NAME_PATTERN } from "./shared";
import { CustomMetadataFields } from "./CustomMetadataFields";
import {
  SkillBundleUpload,
  type SkillBundleSelection,
} from "./SkillBundleUpload";

interface RegisterDialogProps {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  onCreated: () => void;
  onError: (msg: string) => void;
  /** Registry-wide facts: the metadata schema and whether a sync role exists. */
  info: RegistryInfo | null;
}

type SyncCredential = "none" | "iam";

export function RegisterDialog({
  open,
  onOpenChange,
  onCreated,
  onError,
  info,
}: RegisterDialogProps) {
  const [type, setType] = useState<DescriptorType>("A2A");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [version, setVersion] = useState("1.0");
  const [runtimeArn, setRuntimeArn] = useState("");
  const [runtimes, setRuntimes] = useState<AgentRuntimeSummary[]>([]);
  const [gateways, setGateways] = useState<GatewaySummary[]>([]);
  const [gatewayArn, setGatewayArn] = useState("");
  const [remoteUrl, setRemoteUrl] = useState("");
  const [meta, setMeta] = useState<CustomMetadataValue>({});
  const [syncOn, setSyncOn] = useState(false);
  const [syncUrl, setSyncUrl] = useState("");
  const [syncCred, setSyncCred] = useState<SyncCredential>("none");
  const [contentText, setContentText] = useState("");
  const [bundle, setBundle] = useState<SkillBundleSelection | null>(null);
  const [saving, setSaving] = useState(false);

  // A skill's name and description are its SKILL.md frontmatter, and the S3
  // prefix is keyed on that name — so the form's own fields would only ever be a
  // second copy free to disagree with the bundle.
  const isSkill = type === "AGENT_SKILLS";

  // Sync only exists for the two types whose definition lives at a URL.
  const syncAvailable = type === "MCP" || type === "A2A";
  const syncActive = syncAvailable && syncOn;
  const syncMissingUrl = syncActive && !syncUrl.trim();

  // The skill path posts its own body and has no metadata field, so the section
  // is hidden there rather than shown and then silently dropped.
  // `owner` is stamped by the server from the session, so it is not asked for.
  const metaFields = isSkill
    ? []
    : editableMetadataFields(metadataSchemaFor(info?.custom_metadata_schema, type));

  useEffect(() => {
    if (!open) return;
    listAgentRuntimes()
      .then(setRuntimes)
      .catch((e) => onError(e instanceof Error ? e.message : String(e)));
    // Gateways are optional here — a plain MCP endpoint needs none.
    listGateways()
      .then(setGateways)
      .catch(() => setGateways([]));
  }, [open, onError]);

  // Registering an agent almost always mirrors the runtime's own name.
  const pickRuntime = (arn: string) => {
    setRuntimeArn(arn);
    const runtime = runtimes.find((r) => r.agent_runtime_arn === arn);
    if (runtime && !name.trim()) {
      setName(runtime.name);
    }
  };

  // Picking a gateway fills in everything the record needs, so the endpoint and
  // name never have to be transcribed by hand.
  const pickGateway = (arn: string) => {
    setGatewayArn(arn);
    const gateway = gateways.find((g) => g.gateway_arn === arn);
    if (!gateway) return;
    setRemoteUrl(gateway.gateway_url ?? "");
    // The gateway's own endpoint is also the natural sync target for it, and an
    // AWS_IAM gateway only answers a SigV4-signed fetch — so the platform's sync
    // role is the default credential whenever the server offers one.
    setSyncUrl(gateway.gateway_url ?? "");
    if (gateway.authorizer_type === "AWS_IAM" && info?.sync_role_arn) {
      setSyncCred("iam");
    }
    if (!name.trim()) setName(gateway.name);
  };

  // A gateway picked for sync without IAM credentials will fail at fetch time
  // (CREATE_FAILED with "Authorization error"), so say so before Register.
  const gatewayNeedsIam =
    type === "MCP" &&
    syncActive &&
    Boolean(gatewayArn) &&
    syncCred !== "iam";

  // A metadata map is keyed by one type's schema, so it cannot survive a type change.
  const changeType = (next: DescriptorType) => {
    setType(next);
    setMeta({});
    setSyncOn(false);
    setSyncCred("none");
  };

  const reset = () => {
    setName("");
    setDescription("");
    setRuntimeArn("");
    setGatewayArn("");
    setRemoteUrl("");
    setMeta({});
    setSyncOn(false);
    setSyncUrl("");
    setSyncCred("none");
    setContentText("");
    setBundle(null);
  };

  const submit = async () => {
    if (type === "A2A" && !runtimeArn) {
      onError("Select the deployed agent runtime to register.");
      return;
    }
    if (isSkill && !bundle) {
      onError("형식 검사를 통과한 스킬 번들을 올려주세요.");
      return;
    }

    let content: unknown;
    if (!isSkill && contentText.trim()) {
      try {
        content = JSON.parse(contentText);
      } catch {
        onError("Descriptor content must be valid JSON.");
        return;
      }
    }

    setSaving(true);
    try {
      if (isSkill && bundle) {
        // A separate endpoint: the bundle has to reach S3 before the record can
        // point at it, and both halves have to succeed or neither should.
        await createSkillRecord(bundle.file, { version: version || undefined });
      } else {
        await createRegistryRecord({
          name,
          description: description || undefined,
          descriptor_type: type,
          version: version || undefined,
          agent_runtime_arn: type === "A2A" ? runtimeArn : undefined,
          qualifier: type === "A2A" ? "DEFAULT" : undefined,
          remote_url: type === "MCP" ? remoteUrl.trim() || undefined : undefined,
          gateway_arn: type === "MCP" ? gatewayArn || undefined : undefined,
          content,
          submit_for_approval: true,
          custom_metadata: compactMetadata(meta),
          sync_url: syncActive ? syncUrl.trim() : undefined,
          // Only the platform's own sync role is offered; a public endpoint sends none.
          sync_role_arn:
            syncActive && syncCred === "iam" && info?.sync_role_arn
              ? info.sync_role_arn
              : undefined,
        });
      }
      onOpenChange(false);
      reset();
      onCreated();
    } catch (e) {
      onError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      {/* overflow-*y*-auto: plain `overflow-auto` also opts into a horizontal
          scrollbar, which turns any content that does overflow into a sideways
          scroll of the whole dialog rather than a visible layout bug. */}
      <DialogContent className="max-h-[85vh] overflow-y-auto sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Register to registry</DialogTitle>
          <DialogDescription>
            레코드를 생성하고 승인 요청까지 제출합니다.
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-4 py-2">
          <div className="grid gap-2">
            <Label htmlFor="rec-type">Type</Label>
            <Select value={type} onValueChange={(v) => changeType(v as DescriptorType)}>
              <SelectTrigger id="rec-type">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="A2A">Agent (A2A)</SelectItem>
                <SelectItem value="AGENT_SKILLS">Agent Skills</SelectItem>
                <SelectItem value="MCP">MCP</SelectItem>
                <SelectItem value="CUSTOM">Custom</SelectItem>
              </SelectContent>
            </Select>
          </div>

          {type === "A2A" && (
            <div className="grid gap-2">
              <Label htmlFor="rec-runtime">
                Agent Runtime <span className="text-destructive">*</span>
              </Label>
              <Select value={runtimeArn} onValueChange={pickRuntime}>
                <SelectTrigger id="rec-runtime">
                  <SelectValue placeholder="배포된 runtime 선택" />
                </SelectTrigger>
                <SelectContent>
                  {runtimes.map((runtime) => (
                    <SelectItem
                      key={runtime.agent_runtime_arn}
                      value={runtime.agent_runtime_arn}
                    >
                      {runtime.name}
                      {runtime.status && runtime.status !== "READY"
                        ? ` (${runtime.status})`
                        : ""}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <p className="text-xs text-muted-foreground">
                AgentCore에 배포된 runtime 목록입니다. 선택한 runtime이 채팅
                대상이 됩니다.
              </p>
            </div>
          )}

          {isSkill ? (
            <SkillBundleUpload onChange={setBundle} disabled={saving} />
          ) : (
            <>
              <div className="grid gap-2">
                <Label htmlFor="rec-name">Name</Label>
                <Input
                  id="rec-name"
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                />
                {name.trim() && !NAME_PATTERN.test(name.trim()) && (
                  <p className="text-xs text-destructive">
                    영문/숫자로 시작하고 영문, 숫자, <code>_ - . /</code> 만 사용할
                    수 있습니다 (공백 불가).
                  </p>
                )}
              </div>

              <div className="grid gap-2">
                <Label htmlFor="rec-desc">Description</Label>
                <Input
                  id="rec-desc"
                  value={description}
                  onChange={(e) => setDescription(e.target.value)}
                />
              </div>
            </>
          )}

          <div className="grid gap-2">
            <Label htmlFor="rec-version">Version</Label>
            <Input
              id="rec-version"
              value={version}
              onChange={(e) => setVersion(e.target.value)}
            />
          </div>

          {metaFields.length > 0 && (
            <div className="grid gap-2">
              <div>
                <p className="caps-label-xs text-muted-foreground">Custom metadata</p>
                <p className="text-xs text-muted-foreground">
                  검색 필터와 분류에 쓰입니다. 비워 둔 항목은 전송되지 않습니다.
                </p>
              </div>
              <CustomMetadataFields
                fields={metaFields}
                value={meta}
                onChange={setMeta}
                disabled={saving}
              />
            </div>
          )}

          {type === "MCP" && (
            <>
              {gateways.length > 0 && (
                <div className="grid gap-2">
                  <Label htmlFor="rec-gateway">
                    AgentCore Gateway{" "}
                    <span className="text-muted-foreground">(optional)</span>
                  </Label>
                  <Select value={gatewayArn} onValueChange={pickGateway}>
                    <SelectTrigger id="rec-gateway">
                      <SelectValue placeholder="직접 호스팅하는 MCP 서버면 비워두세요" />
                    </SelectTrigger>
                    <SelectContent>
                      {gateways.map((gateway) => (
                        <SelectItem
                          key={gateway.gateway_arn}
                          value={gateway.gateway_arn}
                          // Harness는 실행 역할의 SigV4로만 호출하므로 CUSTOM_JWT
                          // gateway는 첫 도구 호출에서 401이 납니다.
                          disabled={gateway.authorizer_type !== "AWS_IAM"}
                        >
                          {gateway.name}
                          {gateway.status && gateway.status !== "READY"
                            ? ` (${gateway.status})`
                            : ""}
                          {gateway.authorizer_type !== "AWS_IAM"
                            ? ` — ${gateway.authorizer_type ?? "unknown"} 인증`
                            : ""}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                  <p className="text-xs text-muted-foreground">
                    Gateway는 SigV4 인증이 필요해 일반 MCP 엔드포인트로는 호출할
                    수 없습니다. 선택하면 Harness가 인증을 처리하는 전용 gateway
                    도구로 붙습니다. AWS_IAM이 아닌 gateway는 Harness가 인증할 수
                    없어 선택할 수 없습니다.
                  </p>
                </div>
              )}

              <div className="grid gap-2">
                <Label htmlFor="rec-url">Endpoint URL</Label>
                <Input
                  id="rec-url"
                  placeholder="https://example.com/mcp"
                  value={remoteUrl}
                  onChange={(e) => setRemoteUrl(e.target.value)}
                />
                <p className="text-xs text-muted-foreground">
                  {gatewayArn
                    ? "선택한 gateway의 엔드포인트입니다. 조합은 ARN으로 이뤄지므로 참고용입니다."
                    : "Compose 화면에서 이 MCP 서버를 도구로 붙이려면 엔드포인트가 필요합니다. 비워두면 조합 대상에서 제외됩니다."}
                </p>
              </div>
            </>
          )}

          {syncAvailable && (
            <div className="grid gap-3">
              <div className="flex items-center justify-between gap-3 rounded-md border border-border px-3 py-2">
                <Label htmlFor="rec-sync">엔드포인트에서 동기화</Label>
                <Switch
                  id="rec-sync"
                  checked={syncOn}
                  onCheckedChange={setSyncOn}
                  disabled={saving}
                />
              </div>

              {syncOn && (
                <>
                  <div className="grid gap-2">
                    <Label htmlFor="rec-sync-url">
                      {type === "MCP" ? "MCP server URL" : "Agent card URL"}{" "}
                      <span className="text-destructive">*</span>
                    </Label>
                    <Input
                      id="rec-sync-url"
                      placeholder={
                        type === "MCP"
                          ? "https://example.com/mcp"
                          : "https://example.com/.well-known/agent-card.json"
                      }
                      value={syncUrl}
                      onChange={(e) => setSyncUrl(e.target.value)}
                      disabled={saving}
                    />
                  </div>

                  <div className="grid gap-2">
                    <Label htmlFor="rec-sync-cred">Credential</Label>
                    <Select
                      value={syncCred}
                      onValueChange={(v) => setSyncCred(v as SyncCredential)}
                      disabled={saving}
                    >
                      <SelectTrigger id="rec-sync-cred">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="none">None (public)</SelectItem>
                        {info?.sync_role_arn && (
                          <SelectItem value="iam">IAM — 플랫폼 동기화 역할</SelectItem>
                        )}
                      </SelectContent>
                    </Select>
                  </div>

                  <p className="text-xs leading-normal text-warning">
                    동기화는 서버가 광고하는 이름·설명·버전으로 레코드를 덮어쓰고,
                    같은 서버를 동기화한 다른 레코드와 이름이 충돌하면 생성이
                    실패합니다.
                  </p>
                  {gatewayNeedsIam && (
                    <p className="text-xs leading-normal text-destructive">
                      AgentCore Gateway 는 SigV4 서명이 필요합니다.
                      {info?.sync_role_arn
                        ? " Credential 을 IAM 으로 바꾸지 않으면 동기화가 실패합니다."
                        : " 이 서버에는 동기화 역할(REGISTRY_SYNC_ROLE_ARN)이 없어 게이트웨이 동기화를 할 수 없습니다. 동기화를 끄고 등록하세요."}
                    </p>
                  )}
                </>
              )}
            </div>
          )}

          {/* Skills build their descriptor from the bundle, so a free-form JSON
              payload here could only fight with it. */}
          {!isSkill && (
            <div className="grid gap-2">
              <Label htmlFor="rec-content">
                Descriptor content{" "}
                <span className="text-muted-foreground">(JSON, optional)</span>
              </Label>
              <Textarea
                id="rec-content"
                rows={5}
                className="font-mono text-xs"
                placeholder="{}"
                value={contentText}
                onChange={(e) => setContentText(e.target.value)}
              />
            </div>
          )}
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button
            disabled={
              saving ||
              syncMissingUrl ||
              (isSkill ? !bundle : !NAME_PATTERN.test(name.trim()))
            }
            onClick={submit}
          >
            {saving && <Loader2 className="size-4 animate-spin" />}
            Register
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
