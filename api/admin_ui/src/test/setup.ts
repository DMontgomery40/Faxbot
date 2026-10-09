import { afterAll, afterEach, beforeAll, beforeEach } from 'vitest';
import { cleanup } from '@testing-library/react';
import { backend, server } from './server';
import { setProviderNames } from '../providerLabels';

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
