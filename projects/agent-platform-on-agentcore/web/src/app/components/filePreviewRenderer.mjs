/**
 * Decides whether a downloaded file is rendered in the browser or left to the
 * server's extracted text preview.
 *
 * The server preview is a text extraction: for .docx it throws away headings,
 * emphasis, images and the page layout, for .pptx it throws away the whole slide
 * design, and for .pdf there is nothing to extract at all. All three carry their
 * own rendering instructions inside the file, so the browser gets the bytes and
 * renders them; the extracted text stays as the fallback for when that fails.
 */

/** @typedef {"docx" | "pdf" | "pptx" | "image" | null} FileRenderer */

/**
 * Raster images the browser draws natively. A harness browser screenshot swept
 * out of the sandbox arrives as one of these, and the server has no text to
 * extract from it, so without this the panel showed only the download card.
 */
const IMAGE_EXTENSIONS = new Set(["png", "jpg", "jpeg", "gif", "webp"]);

/**
 * @param {string | undefined} filename
 * @returns {FileRenderer} null when the server preview is the best available view.
 */
export function fileRendererFor(filename) {
  const extension = (filename ?? "").toLowerCase().split(".").pop();
  if (extension === "docx") return "docx";
  if (extension === "pdf") return "pdf";
  if (extension === "pptx") return "pptx";
  if (IMAGE_EXTENSIONS.has(extension)) return "image";
  return null;
}

/**
 * Swept files the panel shows in a sandboxed iframe rather than as flowing text.
 * An iframe has no height of its own: inside the panel's ScrollArea it collapses
 * to its min-height, so these files take the whole Preview tab like the `html`
 * artifact kind does.
 * @param {string | undefined} filename
 * @returns {boolean}
 */
export function fileFillsPanel(filename) {
  const extension = filename?.split(".").pop()?.toLowerCase() ?? "";
  return extension === "html" || extension === "htm";
}
