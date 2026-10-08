/**
 * A cancelled deck render must not take the live render's slides with it.
 *
 * The panel rendered a .pptx into the container node directly, so two overlapping
 * renders shared one container that belonged to neither. Whichever render was
 * cancelled first cleared the container — and the render still in flight then
 * appended its slides into a node that was no longer in the document, leaving the
 * panel blank with no spinner and no fallback. React runs mount effects twice in
 * development, so that overlap was the normal case: measured 0 of 9 loads
 * rendered, against 4 of 4 in a production build.
 *
 * Run: node --test src/app/components/pptxDeckRender.test.mjs
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import { renderPptxDeck } from "./pptxDeckRender.mjs";

/** Just enough of a DOM for the parent/child bookkeeping under test. */
function fakeNode(name = "div") {
  const node = {
    name,
    children: [],
    parent: null,
    style: {},
    appendChild(child) {
      child.parent = node;
      node.children.push(child);
      return child;
    },
    remove() {
      if (!node.parent) return;
      node.parent.children = node.parent.children.filter((c) => c !== node);
      node.parent = null;
    },
    get isConnected() {
      return node.parent !== null;
    },
  };
  node.ownerDocument = { createElement: (tag) => fakeNode(tag) };
  return node;
}

/** A previewer that appends a slide per call, like pptx-preview does. */
function fakeLibrary() {
  const calls = [];
  const init = (host) => {
    calls.push(host);
    return {
      async preview() {
        host.appendChild(fakeNode("slide"));
        return host;
      },
    };
  };
  return { init, calls };
}

const source = { arrayBuffer: async () => new ArrayBuffer(8) };

test("a cancelled render leaves the live render's slides in place", async () => {
  const container = fakeNode();
  const library = fakeLibrary();
  const load = async () => library.init;

  // Both renders start before either finishes — a React mount effect running
  // twice, or a version switched while the first deck is still parsing.
  const first = renderPptxDeck({ container, load, source });
  const second = renderPptxDeck({ container, load, source });

  first.cancel();
  await first.done;
  const host = await second.done;

  assert.equal(host, second.host, "the live render reports its own node");
  assert.equal(container.children.length, 1, "only the live render's node remains");
  assert.equal(container.children[0], second.host);
  assert.equal(second.host.children.length, 1, "the slide survived the cancellation");
  assert.equal(second.host.isConnected, true);
});

test("a render cancelled before the library loads never parses the file", async () => {
  const container = fakeNode();
  const library = fakeLibrary();
  let released;
  const load = () => new Promise((resolve) => {
    released = () => resolve(library.init);
  });

  const render = renderPptxDeck({ container, load, source });
  render.cancel();
  released();

  assert.equal(await render.done, null, "the caller is told there is nothing to measure");
  assert.deepEqual(library.calls, [], "the 4MB parse is skipped, not just discarded");
  assert.equal(container.children.length, 0);
});

test("a render cancelled mid-parse resolves to null", async () => {
  const container = fakeNode();
  let finish;
  const load = async () => (host) => ({
    preview: () =>
      new Promise((resolve) => {
        finish = () => {
          host.appendChild(fakeNode("slide"));
          resolve(host);
        };
      }),
  });

  const render = renderPptxDeck({ container, load, source });
  // Let the load and the preview call start.
  await new Promise((resolve) => setTimeout(resolve, 0));
  render.cancel();
  finish();

  assert.equal(await render.done, null, "a detached deck is never handed back to be measured");
  assert.equal(container.children.length, 0);
});

test("a cancelled render does not report its failure", async () => {
  const container = fakeNode();
  let reject;
  const load = () => new Promise((_, r) => {
    reject = r;
  });

  const render = renderPptxDeck({ container, load, source });
  render.cancel();
  reject(new Error("unsupported deck"));

  // Not a rejection: the caller would switch the panel to the extracted text of
  // the deck it just stopped showing.
  assert.equal(await render.done, null);
});

test("a failing render still rejects so the caller can fall back", async () => {
  const container = fakeNode();
  const load = async () => {
    throw new Error("unsupported deck");
  };

  const render = renderPptxDeck({ container, load, source });
  await assert.rejects(render.done, /unsupported deck/);
});

test("the deck is laid out at the requested size", async () => {
  const container = fakeNode();
  const options = [];
  const load = async () => (host, opts) => {
    options.push(opts);
    return { preview: async () => host };
  };

  const render = renderPptxDeck({ container, load, source, width: 960, height: 540 });
  await render.done;

  assert.deepEqual(options, [{ width: 960, height: 540, mode: "list" }]);
});
