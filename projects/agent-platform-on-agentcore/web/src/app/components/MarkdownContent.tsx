"use client";

import React from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Prism as SyntaxHighlighter } from "react-syntax-highlighter";
import { oneDark } from "react-syntax-highlighter/dist/esm/styles/prism";
import { MermaidDiagram } from "@/app/components/MermaidDiagram";
import { cn } from "@/lib/utils";

interface MarkdownContentProps {
  content: string;
  className?: string;
  /**
   * True while `content` is still arriving token by token. Mermaid blocks hold
   * off rendering until then, since a partial diagram cannot be parsed.
   */
  isStreaming?: boolean;
}

export const MarkdownContent = React.memo<MarkdownContentProps>(
  ({ content, className = "", isStreaming = false }) => {
    return (
      <div
        className={cn(
          "prose min-w-0 max-w-full overflow-hidden break-words text-sm leading-relaxed text-inherit [&_p:last-child]:mb-0 [&_p]:mb-4",
          className
        )}
      >
        <ReactMarkdown
          remarkPlugins={[remarkGfm]}
          components={{
            h1: ({ node: _node, children, ...props }) => (
              <h1
                className="mb-6 mt-8 text-2xl font-bold text-foreground first:mt-0 border-b border-border pb-3"
                {...props}
              >
                {children}
              </h1>
            ),
            h2: ({ node: _node, children, ...props }) => (
              <h2
                className="mb-5 mt-7 text-xl font-bold text-foreground first:mt-0 border-b border-border/70 pb-2"
                {...props}
              >
                {children}
              </h2>
            ),
            h3: ({ node: _node, children, ...props }) => (
              <h3
                className="mb-2 mt-5 border-l-2 border-primary pl-2.5 text-base font-semibold text-foreground first:mt-0"
                {...props}
              >
                {children}
              </h3>
            ),
            h4: ({ node: _node, children, ...props }) => (
              <h4
                className="mb-3 mt-5 text-base font-semibold text-foreground/90 first:mt-0"
                {...props}
              >
                {children}
              </h4>
            ),
            h5: ({ node: _node, children, ...props }) => (
              <h5
                className="mb-3 mt-4 text-sm font-semibold text-foreground/80 first:mt-0"
                {...props}
              >
                {children}
              </h5>
            ),
            h6: ({ node: _node, children, ...props }) => (
              <h6
                className="mb-2 mt-4 text-sm font-medium text-foreground/70 first:mt-0 uppercase tracking-wide"
                {...props}
              >
                {children}
              </h6>
            ),
            p: ({ node: _node, className, ...props }) => (
              <p
                className={cn(
                  "mb-5 mt-5 leading-7 first:mt-0 last:mb-0",
                  className
                )}
                {...props}
              />
            ),
            code({
              inline,
              className,
              children,
              ...props
            }: {
              inline?: boolean;
              className?: string;
              children?: React.ReactNode;
            }) {
              const match = /language-(\w+)/.exec(className || "");
              if (!inline && match?.[1] === "mermaid") {
                return (
                  <MermaidDiagram
                    source={String(children).replace(/\n$/, "")}
                    settled={!isStreaming}
                  />
                );
              }
              return !inline && match ? (
                <SyntaxHighlighter
                  style={oneDark}
                  language={match[1]}
                  PreTag="div"
                  className="my-4 max-w-full overflow-x-auto rounded-md"
                  wrapLines={true}
                  wrapLongLines={true}
                  lineProps={{
                    style: {
                      wordBreak: "break-all",
                      whiteSpace: "pre-wrap",
                      overflowWrap: "break-word",
                    },
                  }}
                  customStyle={{
                    margin: 0,
                    padding: "1rem",
                    maxWidth: "100%",
                    fontSize: "0.875rem",
                    borderRadius: "0.375rem",
                  }}
                >
                  {String(children).replace(/\n$/, "")}
                </SyntaxHighlighter>
              ) : (
                <code
                  className="bg-muted rounded-sm px-1 py-0.5 font-mono text-[0.9em]"
                  {...props}
                >
                  {children}
                </code>
              );
            },
            pre: ({ node: _node, children, ...props }) => {
              if (React.isValidElement(children) && children.type === "code") {
                const codeChild = (children.props as { children?: React.ReactNode }).children;
                return React.isValidElement(codeChild) ? <>{codeChild}</> : <>{children}</>;
              }
              return <pre className="overflow-x-auto rounded-md bg-muted p-4 m-2 text-sm" {...props}>{children}</pre>;
            },
            a({
              href,
              children,
            }: {
              href?: string;
              children?: React.ReactNode;
            }) {
              return (
                <a
                  href={href}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="font-medium text-primary underline underline-offset-2"
                >
                  {children}
                </a>
              );
            },
            blockquote({ children }: { children?: React.ReactNode }) {
              return (
                <blockquote className="my-3 border-l-2 border-primary/40 pl-3 text-muted-foreground">
                  {children}
                </blockquote>
              );
            },
            ul: ({ node: _node, className, ...props }) => (
              <ul
                className={cn("my-4 ml-1 list-disc [&>li]:mt-2", className)}
                {...props}
              />
            ),
            ol: ({ node: _node, className, ...props }) => (
              <ol
                className={cn("my-4 ml-1 list-decimal [&>li]:mt-2", className)}
                {...props}
              />
            ),
            hr: ({ node: _node, className, ...props }) => (
              <hr className={cn("my-5 border-b", className)} {...props} />
            ),
            table({ children }: { children?: React.ReactNode }) {
              return (
                <div className="my-4 overflow-x-auto">
                  <table className="[&_th]:bg-muted w-full border-collapse [&_td]:border [&_td]:border-border [&_td]:p-2 [&_th]:border [&_th]:border-border [&_th]:p-2 [&_th]:text-left [&_th]:font-semibold">
                    {children}
                  </table>
                </div>
              );
            },
            th: ({ node: _node, className, ...props }) => (
              <th
                className={cn(
                  "bg-muted px-4 py-2 text-left font-bold first:rounded-tl-lg last:rounded-tr-lg [&[align=center]]:text-center [&[align=right]]:text-right",
                  className
                )}
                {...props}
              />
            ),
            td: ({ node: _node, className, ...props }) => (
              <td
                className={cn(
                  "border-b border-l px-4 py-2 text-left last:border-r [&[align=center]]:text-center [&[align=right]]:text-right",
                  className
                )}
                {...props}
              />
            ),
            tr: ({ node: _node, className, ...props }) => (
              <tr
                className={cn(
                  "m-0 border-b p-0 first:border-t [&:last-child>td:first-child]:rounded-bl-lg [&:last-child>td:last-child]:rounded-br-lg",
                  className
                )}
                {...props}
              />
            ),
            sup: ({ node: _node, className, ...props }) => (
              <sup
                className={cn("[&>a]:text-xs [&>a]:no-underline", className)}
                {...props}
              />
            ),
          }}
        >
          {content}
        </ReactMarkdown>
      </div>
    );
  }
);

MarkdownContent.displayName = "MarkdownContent";
