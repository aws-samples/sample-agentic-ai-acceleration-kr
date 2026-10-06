"use client";

import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
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
  createRegistryRecord,
  listAgentRuntimes,
  listGateways,
  type AgentRuntimeSummary,
  type DescriptorType,
  type GatewaySummary,
} from "@/lib/registry";
import { createSkillRecord } from "@/lib/skills";
import { NAME_PATTERN } from "./shared";
import {
  SkillBundleUpload,
  type SkillBundleSelection,
} from "./SkillBundleUpload";

interface RegisterDialogProps {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  onCreated: () => void;
  onError: (msg: string) => void;
}

export function RegisterDialog({
  open,
  onOpenChange,
  onCreated,
  onError,
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
  const [contentText, setContentText] = useState("");
  const [bundle, setBundle] = useState<SkillBundleSelection | null>(null);
  const [saving, setSaving] = useState(false);

  // A skill's name and description are its SKILL.md frontmatter, and the S3
  // prefix is keyed on that name — so the form's own fields would only ever be a
  // second copy free to disagree with the bundle.
  const isSkill = type === "AGENT_SKILLS";

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
    if (!name.trim()) setName(gateway.name);
  };

  const reset = () => {
    setName("");
    setDescription("");
    setRuntimeArn("");
    setGatewayArn("");
    setRemoteUrl("");
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
            <Select value={type} onValueChange={(v) => setType(v as DescriptorType)}>
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
