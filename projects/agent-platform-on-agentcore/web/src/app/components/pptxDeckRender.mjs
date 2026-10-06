/**
 * One .pptx render, in a node it owns.
 *
 * pptx-preview appends into whatever node it is handed, and its `destroy()` only
 * disposes chart instances — it leaves the DOM alone. So a container shared by two
 * overlapping renders belongs to neither: clearing it on cancellation takes the
 * other render's slides too, and the render still in flight then appends into a
 * node that is no longer in the document. The panel ends up blank with no spinner
 * and no fallback, because as far as the component is concerned the render
 * succeeded.
 *
 * Giving each render its own child node makes cancellation local: a cancelled
 * render removes its node and nothing else.
 */

/**
 * @param {object} args
 * @param {HTMLElement} args.container node the deck is rendered inside
 * @param {() => Promise<Function>} args.load resolves pptx-preview's `init`
 * @param {{ arrayBuffer: () => Promise<ArrayBuffer> }} args.source the file
 * @param {number} [args.width] layout width in CSS pixels
 * @param {number} [args.height] layout height of one slide in CSS pixels
 * @returns {{ host: HTMLElement, done: Promise<HTMLElement | null>, cancel: () => void }}
 *   `done` resolves the host once the deck is in it, or null if the render was
 *   cancelled — a cancelled render reports neither a deck to measure nor a
 *   failure to fall back from.
 */
export function renderPptxDeck({ container, load, source, width, height }) {
  let cancelled = false;
  const host = container.ownerDocument.createElement("div");
  container.appendChild(host);

  const done = (async () => {
    try {
      const init = await load();
      // Checked before the parse, not after: a 4MB deck takes seconds to read,
      // and a render nobody is waiting for should not spend them.
      if (cancelled) return null;
      const previewer = init(host, { width, height, mode: "list" });
      await previewer.preview(await source.arrayBuffer());
      // No `previewer.destroy()` on the way out. It disposes chart instances
      // through a registry the library keeps at module scope, shared by every
      // previewer on the page, so destroying one deck blanks the charts of any
      // other deck currently on screen. Dropping our node is the cleanup that
      // matters; the next `preview()` call disposes the charts anyway, since it
      // fires the same registry.
      return cancelled ? null : host;
    } catch (error) {
      // The panel falls back to the extracted text when a render throws, so a
      // cancelled render must stay quiet: it would replace the deck the live
      // render is about to show with the outline of the one being abandoned.
      if (cancelled) return null;
      throw error;
    }
  })();

  return {
    host,
    done,
    cancel() {
      cancelled = true;
      host.remove();
    },
  };
}
