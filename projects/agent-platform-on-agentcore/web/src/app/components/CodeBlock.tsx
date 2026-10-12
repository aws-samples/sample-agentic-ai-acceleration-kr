"use client";

import React from "react";
import { Prism as SyntaxHighlighter } from "react-syntax-highlighter";
import { oneDark } from "react-syntax-highlighter/dist/esm/styles/prism";

export const CodeBlock = React.memo<{
  content: string;
  language?: string;
}>(({ content, language }) => (
  <SyntaxHighlighter
    language={language || "text"}
    style={oneDark}
    customStyle={{ margin: 0, borderRadius: "0.5rem", fontSize: "0.8125rem" }}
    wrapLongLines
  >
    {content}
  </SyntaxHighlighter>
));

CodeBlock.displayName = "CodeBlock";
