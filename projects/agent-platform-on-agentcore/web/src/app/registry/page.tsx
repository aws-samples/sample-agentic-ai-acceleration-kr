"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { AlertTriangle, Blocks, Cloud, Library, Loader2, Plus, Sparkles } from "lucide-react";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { useAppShell, useRequireRole } from "@/app/components/AppShell";
import {
  EmptyState,
  LoadingState,
  Notice,
  PageBody,
  PageHeader,
} from "@/app/components/PageHeader";
import {
  deleteRegistryRecord,
  getRegistryInfo,
  getRegistryRecord,
  isChattable,
  listDeployedTargets,
  listRegistryRecords,
  metadataSchemaFor,
  searchRegistryRecords,
  updateRegistryRecordStatus,
  type DescriptorType,
  type RegistryInfo,
  type RegistryRecordDetail,
  type RegistryRecordSummary,
  type StatusAction,
} from "@/lib/registry";
import { getCapabilities } from "@/lib/capabilities";
import { RecordSearch } from "./components/RecordSearch";
import {
  getHarness,
  harnessIdFromArn,
  type HarnessSummary,
} from "@/lib/harness";
import { RecordGrid } from "./components/RecordGrid";
import { RecordDetailPanel } from "./components/RecordDetailPanel";
import { RegisterDialog } from "./components/RegisterDialog";
import { BucketSkillsDialog } from "./components/BucketSkillsDialog";
import { DeployedAgentsDialog } from "./components/DeployedAgentsDialog";
import { RecordEditDialog } from "./components/RecordEditDialog";
import { McpEndpointCard } from "./components/McpEndpointCard";
import { toast } from "sonner";

export default function RegistryPage() {
  const router = useRouter();
  const { config, saveConfig } = useAppShell();
  const { allowed: isAdmin } = useRequireRole(["admin"]);

  const [selectedTypes, setSelectedTypes] = useState<DescriptorType[]>([]);
  const [metaFilters, setMetaFilters] = useState<Record<string, string>>({});
  const [query, setQuery] = useState("");
  const [records, setRecords] = useState<RegistryRecordSummary[]>([]);
  const [info, setInfo] = useState<RegistryInfo | null>(null);
  const [registryEnabled, setRegistryEnabled] = useState(true);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [detail, setDetail] = useState<RegistryRecordDetail | null>(null);
  const [detailOpen, setDetailOpen] = useState(false);
  const [harness, setHarness] = useState<HarnessSummary | null>(null);
  const [showCreate, setShowCreate] = useState(false);
  const [showSkills, setShowSkills] = useState(false);
  const [showDeployed, setShowDeployed] = useState(false);
  const [showEdit, setShowEdit] = useState(false);
  const [unregistered, setUnregistered] = useState(0);
  const [deleteTarget, setDeleteTarget] = useState<RegistryRecordDetail | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  const selectedRecordId = config?.selectedAgent?.recordId;

  // Only enum fields of the DEFAULT schema become filters: they are the ones with a
  // fixed value set a dropdown can offer. Free text would need a search box per field.
  const metadataFields = useMemo(
    () =>
      metadataSchemaFor(info?.custom_metadata_schema).filter(
        (field) => field.kind === "enum"
      ),
    [info]
  );

  const load = useCallback(async () => {
    setLoading(true);
    setLoadError(null);
    setError(null);
    try {
      const caps = await getCapabilities();
      setRegistryEnabled(caps.registryEnabled);

      const trimmed = query.trim();
      if (trimmed) {
        // Hybrid search: approved-only, relevance-ordered, capped at 20.
        // The order is AWS's ranking, so it is rendered as returned. Metadata
        // filters are applied by the registry here.
        setRecords(await searchRegistryRecords(trimmed, selectedTypes, metaFilters));
      } else {
        // Browse: every status, paginated. ListRegistryRecords takes a single
        // descriptorType, so a multi-select fans out and merges.
        const listings = selectedTypes.length
          ? await Promise.all(
              selectedTypes.map((type) => listRegistryRecords({ type }))
            )
          : [await listRegistryRecords()];
        // ListRegistryRecords has no metadata filter, so it is applied here.
        setRecords(
          listings
            .flat()
            .filter((r) =>
              Object.entries(metaFilters).every(
                ([key, value]) => r.custom_metadata?.[key] === value
              )
            )
        );
      }
    } catch (e) {
      // No silent fallback to browse: the two paths return different sets, so
      // quietly swapping them would show the user something untrue.
      const message = e instanceof Error ? e.message : String(e);
      setLoadError(message);
      setError(message);
      setRecords([]);
    } finally {
      setLoading(false);
    }
  }, [query, selectedTypes, metaFilters]);

  // Drift count for the banner. Advisory only, so a failure stays silent — the
  // dialog surfaces the real error when it is opened.
  const loadDrift = useCallback(async () => {
    if (!isAdmin) return;
    try {
      const { unregistered: count } = await listDeployedTargets();
      setUnregistered(count);
    } catch {
      setUnregistered(0);
    }
  }, [isAdmin]);

  useEffect(() => {
    load();
  }, [load]);

  useEffect(() => {
    loadDrift();
  }, [loadDrift]);

  useEffect(() => {
    if (!registryEnabled) {
      setInfo(null);
      return;
    }
    getRegistryInfo()
      .then(setInfo)
      .catch(() => setInfo(null));
  }, [registryEnabled]);

  const refreshAll = useCallback(async () => {
    await Promise.all([load(), loadDrift()]);
  }, [load, loadDrift]);

  const openDetail = async (id: string) => {
    setDetailOpen(true);
    setDetail(null);
    setHarness(null);
    let record: RegistryRecordDetail;
    // A deployed-fallback record exists only as a summary synthesised from a
    // live AgentCore resource — there is no registry record to GET. Use the
    // summary already in hand; it has everything a read-only view shows.
    const fallback = records.find((r) => r.record_id === id);
    if (fallback?.source === "deployed") {
      record = { ...fallback };
      setDetail(record);
    } else {
      try {
        record = await getRegistryRecord(id);
        setDetail(record);
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
        setDetailOpen(false);
        return;
      }
    }

    // Harness config is supplementary; a failure here shouldn't blank the panel.
    const harnessId = record.harness_arn
      ? harnessIdFromArn(record.harness_arn)
      : null;
    if (harnessId) {
      try {
        setHarness(await getHarness(harnessId));
      } catch {
        setHarness(null);
      }
    }
  };

  const chatWithAgent = (record: RegistryRecordSummary) => {
    if (!isChattable(record)) return;
    saveConfig({
      ...(config ?? {}),
      selectedAgent: {
        recordId: record.record_id,
        name: record.name,
        description: record.description ?? undefined,
        agentRuntimeArn: record.agent_runtime_arn ?? undefined,
        harnessArn: record.harness_arn ?? undefined,
        qualifier: record.qualifier ?? undefined,
      },
    });
    router.push("/");
  };

  const doStatus = useCallback(
    async (id: string, action: StatusAction) => {
      try {
        await updateRegistryRecordStatus(id, action);
        if (action === "approve") {
          // Approval only reaches search once the index catches up, so the
          // record can look unchanged for a while after this succeeds.
          toast.success(
            "승인되었습니다. 검색 색인 반영까지 수 초에서 수 분이 걸릴 수 있습니다."
          );
        }
        setDetailOpen(false);
        await load();
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      }
    },
    [load]
  );

  // Editing drops the record to DRAFT. Submitting is the curator's call, so it
  // is offered rather than done automatically.
  const onEdited = useCallback(
    async (updated: RegistryRecordDetail) => {
      setDetail(updated);
      await load();
      toast.success(`'${updated.name}' 저장됨 — 상태: ${updated.status}`, {
        action: {
          label: "제출",
          onClick: () => doStatus(updated.record_id, "submit"),
        },
      });
    },
    [load, doStatus]
  );

  const doDelete = useCallback(async () => {
    if (!deleteTarget) return;
    setDeleting(true);
    setDeleteError(null);
    try {
      await deleteRegistryRecord(deleteTarget.record_id);
      toast.success(`'${deleteTarget.name}' 레코드가 삭제되었습니다.`);
      setDeleteTarget(null);
      setDetailOpen(false);
      await load();
    } catch (e) {
      // Kept inside the dialog: the page-level banner sits behind the overlay.
      setDeleteError(e instanceof Error ? e.message : String(e));
    } finally {
      setDeleting(false);
    }
  }, [deleteTarget, load]);

  // Approval is manual on this registry, so the count is an action the admin owes.
  // Search never returns unapproved records, so the count is only meaningful browsing.
  const pendingCount = records.filter((r) => r.status === "PENDING_APPROVAL").length;

  return (
    <>
      <PageHeader
        icon={Library}
        title="Registry"
        meta={
          info?.name && (
            <span className="truncate font-mono text-xs text-muted-foreground">
              {info.name}
            </span>
          )
        }
        actions={
          <>
            {/* Connecting an IDE is a read path, so every role gets it. */}
            {info?.mcp_endpoint && <McpEndpointCard endpoint={info.mcp_endpoint} />}
            {isAdmin && (
              <>
                {registryEnabled && (
                  <Button size="sm" variant="outline" onClick={() => setShowDeployed(true)}>
                    <Cloud className="size-3.5" />
                    Sync deployed
                    {unregistered > 0 && (
                      <Badge shape="count" variant="warning" className="ml-0.5">
                        {unregistered}
                      </Badge>
                    )}
                  </Button>
                )}
                <Button size="sm" variant="outline" onClick={() => router.push("/harness")}>
                  <Blocks className="size-3.5" />
                  Agent Harness
                </Button>
                {/* Registry-off has no Register dialog, so skill bundles have no
                    other home; this uploads them straight to the bucket the harness
                    picker reads. */}
                {!registryEnabled && (
                  <Button size="sm" variant="outline" onClick={() => setShowSkills(true)}>
                    <Sparkles className="size-3.5" />
                    Skills
                  </Button>
                )}
                {registryEnabled && (
                  <Button size="sm" onClick={() => setShowCreate(true)}>
                    <Plus className="size-3.5" />
                    Register
                  </Button>
                )}
              </>
            )}
          </>
        }
      />

      <PageBody>
        <RecordSearch
          query={query}
          onQueryChange={setQuery}
          selectedTypes={selectedTypes}
          onTypesChange={setSelectedTypes}
          metadataFields={metadataFields}
          metaFilters={metaFilters}
          onMetaFiltersChange={setMetaFilters}
          mode={query.trim() ? "search" : "browse"}
          resultCount={records.length}
          onClearQuery={() => setQuery("")}
          loading={loading}
          lastError={loadError}
        />

        {error && (
          <Notice tone="error" icon={AlertTriangle}>
            {error}
          </Notice>
        )}

        {registryEnabled && isAdmin && unregistered > 0 && (
          <Notice
            tone="warning"
            icon={AlertTriangle}
            action={
              <Button size="sm" variant="outline" onClick={() => setShowDeployed(true)}>
                확인하고 등록
              </Button>
            }
          >
            배포되었지만 Registry에 등록되지 않은 항목이 {unregistered}개
            있습니다.
          </Notice>
        )}

        {isAdmin && info?.auto_approval === false && !query.trim() && pendingCount > 0 && (
          <Notice tone="info">
            승인 대기 중인 레코드가 {pendingCount}개 있습니다.
          </Notice>
        )}

        {loading ? (
          <LoadingState label="레코드를 불러오는 중…" />
        ) : records.length === 0 ? (
          query.trim() ? null : (
            <EmptyState
              icon={Library}
              title="등록된 레코드가 없습니다"
              description={
                isAdmin
                  ? "우측 상단 Register 버튼으로 배포된 에이전트를 등록하세요."
                  : undefined
              }
            />
          )
        ) : (
          <RecordGrid
            records={records}
            selectedRecordId={selectedRecordId}
            onOpenDetail={openDetail}
            onChat={chatWithAgent}
          />
        )}
      </PageBody>

      <RecordDetailPanel
        open={detailOpen}
        onOpenChange={setDetailOpen}
        detail={detail}
        harness={harness}
        isAdmin={isAdmin && registryEnabled}
        onChat={chatWithAgent}
        onStatus={registryEnabled ? doStatus : async () => {}}
        onEdit={registryEnabled ? () => setShowEdit(true) : undefined}
        onDelete={registryEnabled ? setDeleteTarget : undefined}
        // Replacing a skill bundle drafts a new revision, so both the panel and
        // the grid behind it are stale until they are re-read.
        onRefresh={() => {
          if (detail) void openDetail(detail.record_id);
          void refreshAll();
        }}
        onOpenRecord={(id) => void openDetail(id)}
      />

      {!registryEnabled && isAdmin && (
        <BucketSkillsDialog
          open={showSkills}
          onOpenChange={setShowSkills}
          onChanged={refreshAll}
        />
      )}

      {registryEnabled && isAdmin && (
        <RegisterDialog
          open={showCreate}
          onOpenChange={(open) => {
            setShowCreate(open);
            if (!open) {
              setError(null);
            }
          }}
          onCreated={refreshAll}
          onError={setError}
          info={info}
        />
      )}

      {registryEnabled && isAdmin && (
        <DeployedAgentsDialog
          open={showDeployed}
          onOpenChange={setShowDeployed}
          onSynced={refreshAll}
        />
      )}

      {registryEnabled && isAdmin && (
        <RecordEditDialog
          open={showEdit}
          onOpenChange={(open) => {
            setShowEdit(open);
            if (!open) {
              setError(null);
            }
          }}
          detail={detail}
          onSaved={onEdited}
          onError={setError}
          info={info}
        />
      )}

      {isAdmin && (
        <Dialog
          open={deleteTarget !== null}
          onOpenChange={(open) => {
            if (!open) {
              setDeleteTarget(null);
              setDeleteError(null);
            }
          }}
        >
          <DialogContent className="sm:max-w-md">
            <DialogHeader>
              <DialogTitle>레코드 삭제</DialogTitle>
              <DialogDescription>
                &lsquo;{deleteTarget?.name}&rsquo; 레코드를 영구히 삭제합니다.
                Deprecated 상태는 AWS에서 되돌릴 수 없으므로 삭제해도 복원할
                내용이 없습니다.
              </DialogDescription>
            </DialogHeader>
            {deleteError && (
              <p className="text-sm text-destructive">{deleteError}</p>
            )}
            <DialogFooter>
              <Button
                variant="outline"
                onClick={() => setDeleteTarget(null)}
                disabled={deleting}
              >
                취소
              </Button>
              <Button
                variant="destructive"
                onClick={() => void doDelete()}
                disabled={deleting}
              >
                {deleting && <Loader2 className="size-3.5 animate-spin" />}
                삭제
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      )}
    </>
  );
}

