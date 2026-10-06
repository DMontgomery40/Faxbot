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
