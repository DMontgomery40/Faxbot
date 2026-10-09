import { afterAll, afterEach, beforeAll, beforeEach } from 'vitest';
import { cleanup } from '@testing-library/react';
import { fetch, Headers, Request, Response } from 'undici';
import { setProviderNames } from '../providerLabels';

// Use one test-only fetch implementation across Node releases. Node 24's
// built-in fetch rejects jsdom's FormData/File brands; Undici 6 accepts the
// browser objects and still performs real multipart encoding and parsing.
Object.assign(globalThis, { fetch, Headers, Request, Response });
// MSW subclasses Response at import time, so initialize its fetch realm first.
const { backend, server } = await import('./server');

if (!window.matchMedia) {
  window.matchMedia = (query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => undefined,
    removeListener: () => undefined,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
    dispatchEvent: () => false,
  });
}

// jsdom 24's Blob/File supports FileReader but lacks stream(). Node fetch's
// multipart encoder needs that method; otherwise consuming an uploaded file
// hangs before an MSW handler can read request.formData() or request.text().
// Keep jsdom's File identity (used by FileReader) and supply the missing API.
if (!Blob.prototype.arrayBuffer) {
  Blob.prototype.arrayBuffer = function () {
    return new Promise<ArrayBuffer>((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result as ArrayBuffer);
      reader.onerror = () => reject(reader.error);
      reader.readAsArrayBuffer(this);
    });
  };
}
if (!Blob.prototype.stream) {
  Blob.prototype.stream = function () {
    const blob = this;
    let reader: FileReader | null = null;
    return new ReadableStream<Uint8Array<ArrayBuffer>>({
      start(controller) {
        reader = new FileReader();
        reader.onload = () => {
          controller.enqueue(new Uint8Array(reader!.result as ArrayBuffer));
          controller.close();
        };
        reader.onerror = () => controller.error(reader!.error);
        reader.readAsArrayBuffer(blob);
      },
      cancel() { reader?.abort(); },
    });
  };
}

// The console scrolls to the top when it opens a page; jsdom does not scroll.
window.scrollTo = (() => undefined) as typeof window.scrollTo;

// xterm probes a canvas at import time; jsdom has none.
HTMLCanvasElement.prototype.getContext = (() => null) as typeof HTMLCanvasElement.prototype.getContext;

beforeAll(() => server.listen({ onUnhandledRequest: 'error' }));
beforeEach(() => backend.reset());
afterEach(() => {
  cleanup();
  server.resetHandlers();
  try { window.localStorage.clear(); } catch { /* ignore */ }
  // Each test starts at the console's bare address, not the page the last one opened.
  window.history.replaceState(null, '', '/');
  setProviderNames({});
});
afterAll(() => server.close());
