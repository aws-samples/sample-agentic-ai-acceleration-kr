"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { usePathname } from "next/navigation";
import { BarChart3, Settings } from "lucide-react";
import { getConfig, saveConfig, StandaloneConfig } from "@/lib/config";
import { fetchNavVisibility, type MenuKey } from "@/lib/settings";
import { ThreadList } from "@/app/components/ThreadList";
import {
  ThreadFilterButton,
  ThreadFilterProvider,
} from "@/app/components/ThreadFilter";
import { LoginScreen } from "@/app/components/LoginScreen";
import { ClientProvider } from "@/providers/ClientProvider";
import { AuthProvider, useAuth } from "@/providers/AuthProvider";
import {
  AppSidebar,
  MessagesSquare,
  Library,
  Blocks,
  LogOut,
  BookOpen,
  type NavItem,
} from "@/app/components/AppSidebar";
import { Button } from "@/components/ui/button";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import {
  ThemeToggle,
  ThemeToggleButton,
} from "@/app/components/ThemeToggle";
import type { Role } from "@/lib/auth";

const NAV_COLLAPSED_KEY = "app-nav-collapsed";

interface AppShellContextValue {
  /** Current persisted config, or null if not yet configured. */
  config: StandaloneConfig | null;
  /** Persist a new config and update shell state. */
  saveConfig: (config: StandaloneConfig) => void;
  /**
   * Refetch the sidebar thread list.
   *
   * The list lives in the shell while the chat that appends to it lives in the
   * page, so the two can no longer be wired by props — the chat calls this
   * after a turn to keep titles and timestamps current.
   */
  revalidateThreads: () => void;
}

const AppShellContext = createContext<AppShellContextValue | null>(null);

export function useAppShell(): AppShellContextValue {
  const ctx = useContext(AppShellContext);
  if (!ctx) {
    throw new Error("useAppShell must be used within an AppShell");
  }
  return ctx;
}

/** Theme control + user info + logout, rendered in the sidebar footer. */
function UserFooter({ collapsed }: { collapsed: boolean }) {
  const { user, logout } = useAuth();
  if (!user) return null;

  const initial = (user.email || user.username || "?").charAt(0).toUpperCase();

  if (collapsed) {
    return (
      <TooltipProvider delayDuration={0}>
        <div className="flex flex-col items-center gap-1">
          {/* Cycles light → dark → system; the segmented group needs more width
              than the collapsed rail has. */}
          <ThemeToggleButton />
          <span className="flex size-6 items-center justify-center rounded-full bg-primary/[0.12] text-[10px] font-semibold text-primary">
            {initial}
          </span>
          <Button
            variant="ghost"
            size="icon"
            className="size-6"
            onClick={logout}
            aria-label="로그아웃"
          >
            <LogOut className="size-4" />
          </Button>
        </div>
      </TooltipProvider>
    );
  }

  // A single row: avatar, identity, logout. Stacking a full-width logout button
  // under the identity spent three times the height on the sidebar's least-used
  // control. The theme group sits on its own row above, where it can show that
  // "system" is a third option rather than hiding it behind a cycle.
  return (
    <TooltipProvider delayDuration={0}>
      <div className="flex flex-col gap-1">
        <div className="flex items-center justify-between gap-2 px-1">
          <span className="caps-label-xs text-muted-foreground">테마</span>
          <ThemeToggle />
        </div>
        <div className="flex items-center gap-2 px-1">
          <span className="flex size-6 flex-shrink-0 items-center justify-center rounded-full bg-primary/[0.12] text-[10px] font-semibold text-primary">
            {initial}
          </span>
          <div className="flex min-w-0 flex-1 items-center gap-1.5">
            {/* A span, not a <p>: the global `p { margin-bottom }` prose rule is
                cancelled by `p:last-child`, and the admin badge that follows
                this element takes that exemption away. The stray 12px then
                inflated the flex row and left the identity sitting 6px above
                the avatar and logout button beside it. */}
            <Tooltip>
              <TooltipTrigger asChild>
                <span className="truncate text-xs font-medium text-muted-foreground">
                  {user.email || user.username}
                </span>
              </TooltipTrigger>
              {/* Truncation is the only way a long address fits a 256px rail,
                  so the full value has to stay reachable somewhere. */}
              <TooltipContent side="top">
                {user.email || user.username}
              </TooltipContent>
            </Tooltip>
            {user.role === "admin" && (
              <span className="flex-shrink-0 rounded border border-primary/25 bg-primary/10 px-1 text-[9px] font-semibold uppercase tracking-wide text-primary">
                {user.role}
              </span>
            )}
          </div>
          <Button
            variant="ghost"
            size="icon"
            onClick={logout}
            aria-label="로그아웃"
            className="size-6 flex-shrink-0"
          >
            <LogOut className="size-4" />
          </Button>
        </div>
      </div>
    </TooltipProvider>
  );
}

/** The authenticated app: sidebar + main content. */
function AuthenticatedShell({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const { role } = useAuth();

  const [config, setConfig] = useState<StandaloneConfig | null>(null);
  const [navCollapsed, setNavCollapsed] = useState(false);
  // Menus an admin hid from plain users (Settings → 메뉴 노출). Empty until the
  // read returns, so the sidebar never flashes a menu that then disappears —
  // the wrong way round would be worse. Admins are never filtered by it.
  const [hiddenMenus, setHiddenMenus] = useState<MenuKey[] | null>(null);

  // The thread list registers its refetch here so the chat can trigger it
  // through context. A ref keeps `revalidateThreads` stable, so handing it to
  // the chat does not re-run its effects.
  const revalidateThreadsRef = useRef<(() => void) | null>(null);
  const revalidateThreads = useCallback(() => {
    revalidateThreadsRef.current?.();
  }, []);
  const registerThreadsMutate = useCallback((mutate: () => void) => {
    revalidateThreadsRef.current = mutate;
  }, []);

  // Threads stay in the sidebar on every route: they are the app's history, not
  // a panel belonging to one page, and hiding them behind the Chats route meant
  // resuming a conversation from Registry or Knowledge took two clicks. It used
  // to wait for a registry selection, but basic chat needs none and every thread
  // resolves its own pinned agent when opened, so the rail is always shown.
  const showThreads = true;

  // Load persisted config + nav state on mount.
  useEffect(() => {
    setConfig(getConfig());

    if (typeof window !== "undefined") {
      setNavCollapsed(localStorage.getItem(NAV_COLLAPSED_KEY) === "1");
    }
  }, []);

  useEffect(() => {
    let live = true;
    fetchNavVisibility()
      .then((value) => {
        if (live) setHiddenMenus(value.hidden);
      })
      .catch(() => {
        // A failed read hides nothing: the setting is a preference, and losing
        // it must never cost someone a menu the server would still serve.
        if (live) setHiddenMenus([]);
      });
    return () => {
      live = false;
    };
  }, []);

  const toggleNavCollapsed = () => {
    setNavCollapsed((prev) => {
      const next = !prev;
      if (typeof window !== "undefined") {
        localStorage.setItem(NAV_COLLAPSED_KEY, next ? "1" : "0");
      }
      return next;
    });
  };

  const handleSaveConfig = (newConfig: StandaloneConfig) => {
    saveConfig(newConfig);
    setConfig(newConfig);
  };

  const navItems: NavItem[] = [
    { key: "chats", label: "Chats", icon: MessagesSquare, href: "/" },
    { key: "knowledge", label: "Knowledge", icon: BookOpen, href: "/knowledge" },
    { key: "registry", label: "Registry", icon: Library, href: "/registry" },
    { key: "harness", label: "Agent Harness", icon: Blocks, href: "/harness" },
    {
      key: "insights",
      label: "Insights",
      icon: BarChart3,
      href: "/insights",
      // Org-wide fleet usage and spend. The server gates /summary·/telemetry·
      // /composition on admin too; this only hides the door a plain user cannot
      // walk through anyway.
      roles: ["admin"],
    },
    {
      key: "settings",
      label: "Settings",
      icon: Settings,
      href: "/settings",
      // Platform-wide configuration an admin edits: which menus plain users
      // see, the model rate card, the MCP inspector. The rate card lived as an
      // Insights widget and the inspector as its own "MCP Tools" door; a form
      // among figures read as one more chart, and a top-level menu for a tool
      // an admin opens a few times a month was a door nobody walked through.
      roles: ["admin"],
    },
  ];

  // Filter by role: items with `roles` require the current role to be included.
  // Then by the admin's menu setting, for plain users only — an admin sees
  // every menu regardless, so the setting cannot lock the Settings page away.
  const visibleItems = navItems.filter(
    (item) =>
      (!item.roles || (role != null && item.roles.includes(role))) &&
      (role === "admin" || !(hiddenMenus ?? []).includes(item.key as MenuKey))
  );

  // "Chats" is active for the root route (chat view).
  const itemsWithActive = visibleItems.map((item) =>
    item.href === "/"
      ? { ...item, active: pathname === "/" }
      : { ...item, active: !!item.href && pathname.startsWith(item.href) }
  );

  const contextValue: AppShellContextValue = {
    config,
    saveConfig: handleSaveConfig,
    revalidateThreads,
  };

  return (
    <ClientProvider>
      <AppShellContext.Provider value={contextValue}>
        {/* Above the sidebar because the filter's two halves are on either side
            of it: the trigger in the header, the filtering in the list. */}
        <ThreadFilterProvider>
          <div className="flex h-screen overflow-hidden">
            <AppSidebar
              collapsed={navCollapsed}
              onToggleCollapsed={toggleNavCollapsed}
              items={itemsWithActive}
              footer={<UserFooter collapsed={navCollapsed} />}
              headerAction={showThreads ? <ThreadFilterButton /> : undefined}
              section={
                showThreads ? (
                  <ThreadList onMutateReady={registerThreadsMutate} />
                ) : undefined
              }
            />
            <main className="flex min-w-0 flex-1 flex-col overflow-hidden">
              {children}
            </main>
          </div>
        </ThreadFilterProvider>
      </AppShellContext.Provider>
    </ClientProvider>
  );
}

/** Decides between login screen and the authenticated app. */
function ShellGate({ children }: { children: ReactNode }) {
  const { user, initializing } = useAuth();
  const pathname = usePathname();

  // The OIDC callback is outside the gate: it is the page that *creates* the
  // session, so at render time there is no user yet. Gating it would swap in
  // the LoginScreen, completeLogin() would never run, and the authorization
  // code would bounce back to the login screen unexchanged — forever.
  if (pathname === "/auth/callback") return <>{children}</>;

  if (initializing) {
    return (
      <div className="flex h-screen items-center justify-center">
        <p className="text-sm text-muted-foreground">Loading…</p>
      </div>
    );
  }

  if (!user) {
    return <LoginScreen />;
  }

  return <AuthenticatedShell>{children}</AuthenticatedShell>;
}

export function AppShell({ children }: { children: ReactNode }) {
  return (
    <AuthProvider>
      <ShellGate>{children}</ShellGate>
    </AuthProvider>
  );
}

/** Roles allowed on the current route; used by page-level guards. */
export function useRequireRole(allowed: Role[]): {
  allowed: boolean;
  role: Role | null;
} {
  const { role } = useAuth();
  return { allowed: role != null && allowed.includes(role), role };
}
