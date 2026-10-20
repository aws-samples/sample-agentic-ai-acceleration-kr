import { NuqsAdapter } from "nuqs/adapters/next/app";
import { Toaster } from "sonner";
import { Suspense } from "react";
import "./globals.css";
import { AppShell } from "@/app/components/AppShell";
import {
  THEME_INIT_SCRIPT,
  ThemeProvider,
} from "@/providers/ThemeProvider";

export const metadata = {
  title: "Agent Platform",
  description: "Compose, register and run agents on AgentCore",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html
      lang="ko"
      // The theme script mutates <html>'s class and style before React runs, so
      // the server markup and the first client render necessarily differ here.
      suppressHydrationWarning
    >
      <head>
        {/*
          Must be inline, synchronous, and in <head>: applying the theme from an
          effect would let a dark-mode user see a white page for a frame first.
        */}
        <script
          dangerouslySetInnerHTML={{ __html: THEME_INIT_SCRIPT }}
        />
      </head>
      <body suppressHydrationWarning>
        <ThemeProvider>
          <NuqsAdapter>
            <Suspense
              fallback={
                <div className="flex h-screen items-center justify-center">
                  <p className="text-sm text-muted-foreground">Loading…</p>
                </div>
              }
            >
              <AppShell>{children}</AppShell>
            </Suspense>
          </NuqsAdapter>
          <Toaster
            position="bottom-right"
            toastOptions={{
              className:
                "!rounded-lg !border !border-border !bg-popover !text-popover-foreground !shadow-lg",
            }}
          />
        </ThemeProvider>
      </body>
    </html>
  );
}
