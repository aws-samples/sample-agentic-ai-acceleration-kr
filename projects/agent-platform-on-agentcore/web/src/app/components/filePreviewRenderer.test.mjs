/**
 * A .docx must not fall through to the extracted-text preview.
 *
 * The text extraction is what made the panel show a docx as one flat run of
 * paragraphs — no headings, no bold, no lists, and pipe-joined table rows that
 * markdown renders as literal text.
 *
 * Run: node --test src/app/components/filePreviewRenderer.test.mjs
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import { fileFillsPanel, fileRendererFor } from "./filePreviewRenderer.mjs";

test("docx, pdf and pptx render in the browser", () => {
  assert.equal(fileRendererFor("보고서.docx"), "docx");
  assert.equal(fileRendererFor("report.pdf"), "pdf");
  assert.equal(fileRendererFor("실적.pptx"), "pptx");
});

test("the extension is matched case-insensitively", () => {
  assert.equal(fileRendererFor("REPORT.DOCX"), "docx");
  assert.equal(fileRendererFor("Report.Pdf"), "pdf");
  assert.equal(fileRendererFor("실적.PPTX"), "pptx");
});

test("raster images render in the browser", () => {
  assert.equal(fileRendererFor("wikipedia-amazon-bedrock.png"), "image");
  assert.equal(fileRendererFor("photo.JPG"), "image");
  assert.equal(fileRendererFor("chart.webp"), "image");
});

test("formats with a server preview keep it", () => {
  assert.equal(fileRendererFor("결과.xlsx"), null);
  assert.equal(fileRendererFor("data.csv"), null);
  assert.equal(fileRendererFor("메모.txt"), null);
});

test("a missing or extensionless filename has no renderer", () => {
  assert.equal(fileRendererFor(undefined), null);
  assert.equal(fileRendererFor("Makefile"), null);
});

test("html files fill the panel like html artifacts; documents flow", () => {
  assert.equal(fileFillsPanel("agent-usage-dashboard.html"), true);
  assert.equal(fileFillsPanel("index.HTM"), true);
  assert.equal(fileFillsPanel("report.docx"), false);
  assert.equal(fileFillsPanel(undefined), false);
});
