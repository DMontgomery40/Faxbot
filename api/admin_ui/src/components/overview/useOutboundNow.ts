// The one poll of the live outbound board (GET /outbound/now) per tab, shared by Overview and Sent.
// It asks only while the tab is visible, never starts a request before the last one ended, and backs off when the
// server is busy (429 or 503, honouring Retry-After) without treating that as a signed-out session (#83, D3).
import { useSyncExternalStore } from 'react';
import AdminAPIClient, { AdminAPIError } from '../../api/client';
import type { OutboundNow, OutboundNowReply } from '../../api/outboundTypes';

// How often the board looks again, and the longest it waits after a busy or failed answer.
export const OUTBOUND_POLL = { intervalMs: 5000, maxBackoffMs: 60 * 1000, maxRetryAfterMs: 5 * 60 * 1000 };

// What the last poll found: a reading, a busy server, no answer, or a board this account may not or cannot read.
export type OutboundPollState = 'loading' | 'ok' | 'busy' | 'failed' | 'denied' | 'unavailable';

export interface OutboundNowView {
  // The last reading, kept through a busy or failed poll so the board never drops to zero.
  board: OutboundNow | null;
  // When that reading arrived (milliseconds).
  at: number | null;
  state: OutboundPollState;
}

export interface PollerDeps {
  fetch: () => Promise<OutboundNowReply>;
  now: () => number;
  setTimer: (run: () => void, ms: number) => unknown;
  clearTimer: (timer: unknown) => void;
  visible: () => boolean;
  // Calls the listener when the tab is shown or hidden; returns how to stop.
  onVisibility: (listener: () => void) => () => void;
}

const INITIAL: OutboundNowView = { board: null, at: null, state: 'loading' };

export class OutboundPoller {
  private view: OutboundNowView = INITIAL;
  private readonly listeners = new Set<() => void>();
  private timer: unknown = null;
  // The next poll starts no earlier than this (a back-off survives the tab being hidden).
  private dueAt = 0;
  private inFlight = false;
  // Asked to look again while a poll was running: once more right after it.
  private again = false;
  private failures = 0;
  // Not permitted, or no board on this server: it never asks again.
  private stopped = false;
  private unwatch: (() => void) | null = null;

  constructor(private readonly deps: PollerDeps) {}

  getView = (): OutboundNowView => this.view;

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    if (this.listeners.size === 1) this.start();
    return () => {
      this.listeners.delete(listener);
      if (this.listeners.size === 0) this.halt();
    };
  };

  // Look again now, as after a decision or Refresh; during a poll, once more as soon as it ends.
  refresh = (): void => {
    if (this.stopped || this.listeners.size === 0) return;
    if (this.inFlight) {
      this.again = true;
      return;
    }
    if (this.failures === 0) this.dueAt = 0;
    this.wake();
  };

  private start() {
    this.unwatch = this.deps.onVisibility(this.wake);
    this.wake();
  }

  private halt() {
    this.unwatch?.();
    this.unwatch = null;
    this.cancel();
  }

  private cancel() {
    if (this.timer !== null) this.deps.clearTimer(this.timer);
    this.timer = null;
  }

  // Shown: poll when due. Hidden: nothing waits to run.
  private wake = () => {
    if (!this.deps.visible()) {
      this.cancel();
      return;
    }
    if (!this.inFlight) this.schedule(Math.max(0, this.dueAt - this.deps.now()));
  };

  private schedule(ms: number) {
    this.cancel();
    if (this.stopped || this.listeners.size === 0) return;
    this.timer = this.deps.setTimer(() => {
      this.timer = null;
      void this.poll();
    }, ms);
  }

  private set(view: OutboundNowView) {
    this.view = view;
    this.listeners.forEach((listener) => listener());
  }

  private backoff(): number {
    return Math.min(OUTBOUND_POLL.intervalMs * 2 ** this.failures, OUTBOUND_POLL.maxBackoffMs);
  }

  private async poll() {
    if (this.inFlight || this.stopped || this.listeners.size === 0 || !this.deps.visible()) return;
    this.inFlight = true;
    let wait = OUTBOUND_POLL.intervalMs;
    try {
      const reply = await this.deps.fetch();
      if (reply.kind === 'ready') {
        this.failures = 0;
        this.set({ board: reply.board, at: this.deps.now(), state: 'ok' });
      } else {
        this.failures += 1;
        wait = reply.retryAfterMs === null ? this.backoff()
          : Math.min(Math.max(reply.retryAfterMs, OUTBOUND_POLL.intervalMs), OUTBOUND_POLL.maxRetryAfterMs);
        this.set({ ...this.view, state: 'busy' });
      }
    } catch (error) {
      if (error instanceof AdminAPIError && (error.status === 401 || error.status === 403)) {
        this.stopped = true;
        this.set({ board: null, at: null, state: 'denied' });
      } else if (error instanceof AdminAPIError && (error.status === 404 || error.status === 405)) {
        this.stopped = true;
        this.set({ board: null, at: null, state: 'unavailable' });
      } else {
        this.failures += 1;
        wait = this.backoff();
        this.set({ ...this.view, state: 'failed' });
      }
    } finally {
      this.inFlight = false;
    }
    if (this.again) {
      this.again = false;
      if (this.failures === 0) wait = 0;
    }
    this.dueAt = this.deps.now() + wait;
    if (this.deps.visible()) this.schedule(wait);
  }
}

function browserDeps(client: AdminAPIClient): PollerDeps {
  return {
    fetch: () => client.outboundNow(),
    now: () => Date.now(),
    setTimer: (run, ms) => window.setTimeout(run, ms),
    clearTimer: (timer) => window.clearTimeout(timer as number),
    visible: () => document.visibilityState === 'visible',
    onVisibility: (listener) => {
      document.addEventListener('visibilitychange', listener);
      return () => document.removeEventListener('visibilitychange', listener);
    },
  };
}

// One poller per console client, so Overview and Sent mounted together still make one request at a time.
const pollers = new WeakMap<AdminAPIClient, OutboundPoller>();
export function outboundPollerFor(client: AdminAPIClient): OutboundPoller {
  let poller = pollers.get(client);
  if (!poller) {
    poller = new OutboundPoller(browserDeps(client));
    pollers.set(client, poller);
  }
  return poller;
}

export function useOutboundNow(client: AdminAPIClient): OutboundNowView & { refresh: () => void } {
  const poller = outboundPollerFor(client);
  const view = useSyncExternalStore(poller.subscribe, poller.getView);
  return { ...view, refresh: poller.refresh };
}
