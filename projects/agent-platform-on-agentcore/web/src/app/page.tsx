"use client";

import React, {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  Suspense,
} from "react";
import { useRouter } from "next/navigation";
import { useQueryState } from "nuqs";
import { Button } from "@/components/ui/button";
import { Bot, Library, SquarePen } from "lucide-react";
import { EmptyState, PageHeader } from "@/app/components/PageHeader";
import {
  ResizableHandle,
  ResizablePanel,
  ResizablePanelGroup,
} from "@/components/ui/resizable";
import { ChatProvider, useChatContext } from "@/providers/ChatProvider";
import { ChatInterface } from "@/app/components/ChatInterface";
import { ArtifactPanel } from "@/app/components/ArtifactPanel";
import { useAppShell } from "@/app/components/AppShell";
import { useThreadAgent } from "@/app/hooks/useThreadAgent";
import { useDefaultAgent } from "@/app/hooks/useDefaultAgent";
import { useClient } from "@/providers/ClientProvider";
import type { SelectedAgent } from "@/lib/config";
import { artifactParam, parseArtifactParam } from "@/lib/artifacts";

/**
 * Chat plus the artifact panel. Lives inside ChatProvider because the panel
 * reads the artifacts the stream produced.
 */
function ChatWithArtifacts({
  readOnly,
  banner,
}: {
  /** The thread can be read but not continued — see useThreadAgent. */
  readOnly?: boolean;
  banner?: React.ReactNode;
}) {
  const { artifacts } = useChatContext();
  const [artifactParamValue, setArtifactParam] = useQueryState("artifact");
  const active = parseArtifactParam(artifactParamValue);

  const openArtifact = useCallback(
    (artifactId: string, version: number) => {
      void setArtifactParam(artifactParam(artifactId, version));
    },
    [setArtifactParam]
  );

  // The panel belongs to the thread it was opened in. `artifact` and `threadId`
  // are separate query params, and this component stays mounted across thread
  // switches, so picking another thread (or New chat) used to leave the panel
  // up with an id the new thread does not have. Only *leaving* a thread closes
  // it: a new chat gets its id mid-stream (null → id) and must keep whatever the
  // first turn has already opened, and a deep link with both params must open.
  const [threadId] = useQueryState("threadId");
  const panelThreadRef = useRef(threadId);
  useEffect(() => {
    const previous = panelThreadRef.current;
    panelThreadRef.current = threadId;
    if (previous !== null && previous !== threadId) {
      void setArtifactParam(null);
    }
  }, [threadId, setArtifactParam]);

  // Only just-streamed artifacts auto-open the panel — they carry inline
  // content. Versions loaded from storage would otherwise pop the panel open
  // every time a thread is reopened.
  const latestStreamedParam = useMemo(() => {
    const latest = artifacts?.filter((a) => a.content !== undefined).at(-1);
    return latest ? artifactParam(latest.artifactId, latest.version) : null;
  }, [artifacts]);

  // Fire once per newly streamed version. Reacting to the current param instead
  // would re-assert the newest artifact right after the user clicks an older
  // card, so only the last artifact of a thread could ever be opened. Closing
  // the panel is likewise not undone, since the ref is already marked.
  const autoOpenedRef = useRef<string | null>(null);
  useEffect(() => {
    if (!latestStreamedParam) return;
    if (autoOpenedRef.current === latestStreamedParam) return;
    autoOpenedRef.current = latestStreamedParam;
    void setArtifactParam(latestStreamedParam);
  }, [latestStreamedParam, setArtifactParam]);

  const closePanel = useCallback(() => {
    void setArtifactParam(null);
  }, [setArtifactParam]);

  return (
    <>
      <ResizablePanel id="chat" className="relative flex flex-col" order={2}>
        <ChatInterface
          controls={<></>}
          skeleton={
            <div className="flex items-center justify-center p-8">
              <p className="text-sm text-muted-foreground">Loading…</p>
            </div>
          }
          activeArtifact={active}
          onOpenArtifact={openArtifact}
          readOnly={readOnly}
          banner={banner}
        />
      </ResizablePanel>

      {active && (
        <>
          <ResizableHandle />
          <ResizablePanel
            id="artifact"
            order={3}
            defaultSize={40}
            minSize={25}
            className="flex flex-col"
          >
            <ArtifactPanel
              artifacts={artifacts ?? []}
              artifactId={active.artifactId}
              version={active.version}
              onSelectVersion={openArtifact}
              onClose={closePanel}
            />
          </ResizablePanel>
        </>
      )}
    </>
  );
}

function HomePageContent() {
  const router = useRouter();
  const { config, revalidateThreads } = useAppShell();
  // Same query param the thread list reads; clearing it starts a new chat.
  const [threadId, setThreadId] = useQueryState("threadId");
  const apiClient = useClient();

  // An open thread is pinned to one agent and the server refuses a turn from any
  // other, so the thread decides — both which agent is invoked and whose name
  // the header shows. Only a new chat follows the sidebar selection.
  const { agent, problem, loading } = useThreadAgent(
    threadId,
    config?.selectedAgent,
    apiClient
  );

  /**
   * The agent to keep the chat mounted with while a pin is resolving.
   *
   * The first turn of a new chat creates its thread mid-stream, and the id lands
   * in the URL — which sets this hook resolving and, with a bare `if (loading)`
   * below, unmounted the whole chat subtree while the agent was still answering.
   * That threw away the live stream along with everything the run had rendered;
   * what remounted read the thread from DynamoDB exactly once, so the transcript
   * froze at whatever had been written by that instant and the rest of the answer
   * — several messages of it — arrived to nobody. The tool boxes froze with it.
   *
   * Reusing the last known agent keeps the tree alive across that resolution. It
   * is provisional, never an answer: `problem` still decides read-only, and the
   * pin still decides who is actually invoked once it resolves. For the case that
   * matters this is the same agent either way — the one that started the run.
   */
  const lastAgentRef = useRef<SelectedAgent | null>(null);
  if (agent) {
    lastAgentRef.current = agent;
  }
  const resolvingAgent = loading && !problem ? lastAgentRef.current : null;
  const activeAgent = agent ?? resolvingAgent;

  // The default agent stands in when nothing is selected, so a first visit can
  // start talking. Shared with the switcher, which names it for the same reason.
  const { defaultAgent, defaultLoading } = useDefaultAgent();
  // A thread's pin always wins. In a new chat the selection wins, and no
  // selection at all falls back to the default agent.
  const effectiveAgent: SelectedAgent | null = threadId
    ? activeAgent
    : (activeAgent ?? defaultAgent ?? null);

  const newChat = (
    <Button
      size="sm"
      onClick={() => void setThreadId(null)}
      disabled={!threadId}
    >
      <SquarePen className="size-3.5" />
      New chat
    </Button>
  );

  // Only when there is nothing to keep on screen. Unmounting the chat over a
  // resolving pin is what killed the in-flight run; see resolvingAgent.
  // The default-agent lookup counts as loading too: rendering the picker before
  // it answers would flash "select an agent" at someone it is about to serve.
  if ((loading || (!threadId && !activeAgent && defaultLoading)) && !effectiveAgent) {
    return (
      <div className="flex flex-1 items-center justify-center">
        <p className="text-sm text-muted-foreground">Loading…</p>
      </div>
    );
  }

  // A thread whose agent cannot answer. The transcript still renders — it comes
  // from DynamoDB, not from the agent — so the chat stays mounted read-only and
  // a banner explains why nothing can be sent. Replacing the whole view with an
  // explanation would hide the very history the user opened the thread to read.
  const readOnlyBanner = problem ? (
    <div className="rounded-md border border-warning/40 bg-warning/10 px-3 py-2">
      <p className="text-xs font-medium text-foreground">
        {problem === "unpinned"
          ? "어떤 에이전트와 대화했는지 기록되지 않아 이어갈 수 없습니다"
          : "이 대화의 에이전트를 찾을 수 없어 이어갈 수 없습니다"}
      </p>
      <p className="mt-0.5 text-xs text-muted-foreground">
        {problem === "unpinned"
          ? "에이전트가 스레드에 기록되기 전에 만들어진 대화입니다. 이어서 보내면 어떤 에이전트의 기억에 쌓일지 알 수 없습니다."
          : "연결된 에이전트가 Registry에서 삭제되었거나 접근할 수 없습니다."}
      </p>
      <Button
        size="sm"
        className="mt-2"
        onClick={() => void setThreadId(null)}
      >
        <SquarePen className="size-3.5" />
        새 대화 시작
      </Button>
    </div>
  ) : undefined;

  // No agent and no thread to read: nothing to show but the picker. A thread with
  // a `problem` also has no agent, but it does have a transcript, so it falls
  // through to the read-only chat below.
  if (!effectiveAgent && !problem) {
    return (
      <div className="flex flex-1 items-center justify-center">
        <EmptyState
          icon={Library}
          title="에이전트를 선택하세요"
          description="Registry에서 AgentCore Runtime에 배포된 에이전트를 선택하면 그 에이전트와 채팅할 수 있습니다."
          action={
            <Button onClick={() => router.push("/registry")}>
              <Library className="size-4" />
              Browse Registry
            </Button>
          }
        />
      </div>
    );
  }

  return (
    <>
      {/* New chat sits top-right, where every other route puts its primary
          action (Register, Create agent, 새 Knowledge Base). It used to sit above
          the thread list in the sidebar, which was the one place in the app where
          a page's main action lived outside its header. */}
      <PageHeader
        icon={Bot}
        title={effectiveAgent?.name ?? "이어갈 수 없는 대화"}
        hint={effectiveAgent?.description}
        actions={newChat}
      />

      <div className="flex-1 overflow-hidden">
        <ResizablePanelGroup
          direction="horizontal"
          autoSaveId="standalone-chat"
        >
          <ChatProvider
            onHistoryRevalidate={revalidateThreads}
            agentConfig={
              effectiveAgent
                ? {
                    agentRuntimeArn: effectiveAgent.agentRuntimeArn,
                    harnessArn: effectiveAgent.harnessArn,
                    qualifier: effectiveAgent.qualifier,
                    registryRecordId: effectiveAgent.recordId,
                    registryAgentName: effectiveAgent.name,
                  }
                : // Nothing may be sent, so there is no agent to address. Leaving
                  // this unset means a stray send cannot reach the wrong one.
                  undefined
            }
          >
            <ChatWithArtifacts
              readOnly={!!problem}
              banner={readOnlyBanner}
            />
          </ChatProvider>
        </ResizablePanelGroup>
      </div>
    </>
  );
}

export default function HomePage() {
  return (
    <Suspense
      fallback={
        <div className="flex flex-1 items-center justify-center">
          <p className="text-sm text-muted-foreground">Loading…</p>
        </div>
      }
    >
      <HomePageContent />
    </Suspense>
  );
}
