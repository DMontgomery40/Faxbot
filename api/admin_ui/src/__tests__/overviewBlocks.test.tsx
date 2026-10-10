// The value-first Overview: what Faxbot is doing, the next improvements, everyday faxes and Needs attention, each
// with its own state and time, from the same answers `faxbot overview` is tested with (overviewBlocks.json,
// api/tests/test_cli_overview.py). The console must say what the command prints.
import { describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import AdminAPIClient from '../api/client';
import type { HealthStatus } from '../api/types';
import Dashboard from '../components/Dashboard';
import { clockText, STALE_AFTER_MS } from '../components/overview/blocks';
import { server } from '../test/server';
import fixture from './overviewBlocks.json';

type Scenario = (typeof fixture.scenarios)[number];
type Reply = { status: number; body: unknown };

const client = () => new AdminAPIClient({ kind: 'key', key: 'synthetic-key' });
const NORMAL = ['doing', 'next', 'everyday', 'attention'];
const VALUE_READS = ['/routing/capabilities', '/routing/savings', '/routing/recommendations/sending',
  '/routing/recommendations/facts'];

function answer(scenario: Scenario): string[] {
  const asked: string[] = [];
  const replies = { ...fixture.base, ...scenario.responses } as Record<string, Reply>;
  server.use(...Object.entries(replies).map(([path, reply]) => http.get(path, () => {
    asked.push(path);
    return HttpResponse.json(reply.body as object, { status: reply.status });
  })));
  return asked;
}

const scenario = (name: string) => fixture.scenarios.find((item) => item.name === name)!;
const cli = (item: Scenario) => item.cli.map((line) => line.trim());
const part = (node: Element, which: 'title' | 'detail') => node.querySelector(`[data-part="${which}"]`)?.textContent;

async function loaded() {
  await waitFor(() => {
    for (const id of ['doing', 'next', 'everyday']) {
      expect(screen.getByTestId(`overview-${id}`).getAttribute('data-state')).not.toBe('loading');
    }
    expect(screen.getByTestId('needs-attention').getAttribute('aria-busy')).toBe('false');
  });
}

function order(container: HTMLElement): string[] {
  return [...container.querySelectorAll('[data-block]')].map((node) => node.getAttribute('data-block') as string)
    .filter((block) => block !== 'map');
}

// What each scenario must show, by key; the sentences themselves are compared with the command's lines.
const USED = ['fax_over_ip', 'cheapest_route', 'blocked_senders'];
const READY = ['capability-fax_friendly', 'capability-case_packets', 'capability-separator_pages', 'capability-encoded_pages'];
const NEXT = ['capability-fax_friendly', 'sending-+13035550124', 'fact-+13035550199-route_price', 'capability-case_packets',
  'capability-separator_pages', 'capability-encoded_pages'];
const EXPECT: Record<string, { states: [string, string, string]; used: string[]; next: string[]; results: string[] }> = {
  healthy: { states: ['ready', 'ready', 'ready'], used: USED, next: NEXT, results: ['money', 'pages', 'seconds', 'calls', 'bytes'] },
  new: { states: ['ready', 'ready', 'ready'], used: [], next: [], results: [] },
  quiet: { states: ['ready', 'ready', 'ready'], used: [], next: READY, results: [] },
  'receive-only': { states: ['ready', 'ready', 'ready'], used: USED, next: NEXT, results: ['money', 'pages', 'seconds', 'calls', 'bytes'] },
  incident: { states: ['ready', 'ready', 'ready'], used: USED, next: NEXT, results: ['money', 'pages', 'seconds', 'calls', 'bytes'] },
  'failed source': { states: ['ready', 'ready', 'ready'], used: USED, next: NEXT.filter((key) => !key.startsWith('sending-')), results: [] },
  restricted: { states: ['denied', 'denied', 'ready'], used: [], next: [], results: [] },
};

describe('Overview blocks', () => {
  for (const item of fixture.scenarios) {
    it(`shows the ${item.name} installation's blocks in order, in the command's own sentences`, async () => {
      const asked = answer(item);
      const navigate = vi.fn();
      const { container } = render(<Dashboard client={client()} onNavigate={navigate} canReadSettings={item.settings}
        canSetUp onSendFax={vi.fn()} />);
      await loaded();
      const expected = EXPECT[item.name];
      const lines = cli(item);
      expect(order(container)).toEqual(item.order);
      expect(['doing', 'next', 'everyday'].map((id) => screen.getByTestId(`overview-${id}`).getAttribute('data-state')))
        .toEqual(expected.states);

      // What Faxbot is doing: each capability that acted here, as the command names it, opening its own page.
      const used = within(screen.getByTestId('overview-doing')).queryAllByTestId(/^overview-used-/);
      expect(used.map((node) => node.getAttribute('data-testid'))).toEqual(expected.used.map((key) => `overview-used-${key}`));
      for (const node of used) {
        expect(lines).toContain(`${part(node, 'title')}: ${part(node, 'detail')}`);
        fireEvent.click(node);
        expect(navigate).toHaveBeenLastCalledWith(`savings/capabilities?key=${node.getAttribute('data-testid')!.slice('overview-used-'.length)}`);
      }
      // Results keep their units; nothing is shown for a unit with no result, and no grand total.
      const results = within(screen.getByTestId('overview-doing')).queryAllByTestId(/^overview-result-/);
      expect(results.map((node) => node.getAttribute('data-testid'))).toEqual(expected.results.map((unit) => `overview-result-${unit}`));
      for (const node of results) expect(lines).toContain(node.textContent);
      expect(screen.getByTestId('overview-doing').textContent).not.toMatch(/total/i);

      // Next improvements: each says what kind of step it is, as the command does, and opens its page.
      const next = within(screen.getByTestId('overview-next')).queryAllByTestId(/^overview-next-(capability|sending|fact)-/);
      expect(next.map((node) => node.getAttribute('data-testid'))).toEqual(expected.next.map((key) => `overview-next-${key}`));
      for (const node of next) {
        const kind = within(node).getByTestId('overview-next-kind').textContent;
        expect(lines).toContain(`${part(node, 'title')}: ${kind}`);
        expect(lines).toContain(part(node, 'detail'));
        fireEvent.click(node);
        const key = node.getAttribute('data-testid')!.slice('overview-next-'.length);
        expect(navigate).toHaveBeenLastCalledWith(key.startsWith('capability-')
          ? `savings/capabilities?key=${key.slice('capability-'.length)}`
          : key.startsWith('sending-') ? 'savings/opportunities?section=sending' : 'savings/facts');
      }

      // Everyday faxes: the same counts as the command.
      for (const id of ['received', 'sent', 'expected']) {
        const node = screen.getByTestId(`overview-everyday-${id}`);
        expect(lines).toContain(`${part(node, 'title')}: ${part(node, 'detail')}`);
      }

      // A person who may not read settings: the value blocks say so, nothing is asked for and no map is drawn.
      if (!item.settings) {
        expect(within(screen.getByTestId('overview-doing')).getByText('Not available to this account.')).toBeTruthy();
        expect(asked.filter((path) => VALUE_READS.includes(path))).toEqual([]);
        expect(container.querySelector('[data-block="map"]')).toBeNull();
      }
    });
  }

  it('shows a new installation what to connect next and which capabilities then work right away', async () => {
    answer(scenario('new'));
    const navigate = vi.fn();
    render(<Dashboard client={client()} onNavigate={navigate} canReadSettings canSetUp />);
    await loaded();
    const doing = screen.getByTestId('overview-doing');
    expect(doing.textContent).toContain('No fax provider is set up yet, so Faxbot has nothing to improve so far.');
    const trunk = within(doing).getByTestId('overview-connect-delivery/trunk');
    expect(cli(scenario('new'))).toContain(part(trunk, 'title'));
    expect(cli(scenario('new'))).toContain(part(trunk, 'detail'));
    fireEvent.click(trunk);
    expect(navigate).toHaveBeenLastCalledWith('delivery/trunk');
    fireEvent.click(within(doing).getByRole('button', { name: 'Set up a fax provider' }));
    expect(navigate).toHaveBeenLastCalledWith('setup');
    // No saving is made up to fill the page.
    expect(doing.textContent).not.toMatch(/\$|saved/);
  });

  it('never shows a saving for a quiet installation, and says why there are no results', async () => {
    answer(scenario('quiet'));
    render(<Dashboard client={client()} canReadSettings />);
    await loaded();
    const doing = screen.getByTestId('overview-doing');
    expect(within(doing).getByTestId('overview-doing-empty').textContent)
      .toBe('Nothing Faxbot can improve acted on your faxes in the last 30 days, so there are no results yet.');
    expect(doing.textContent).not.toMatch(/\$/);
  });

  it('says a receive-only installation receives only, with nothing marked as not ready', async () => {
    answer(scenario('receive-only'));
    render(<Dashboard client={client()} canReadSettings />);
    await loaded();
    expect(screen.getByTestId('overview-everyday-sent').textContent)
      .toContain('This installation receives faxes only; sending is not set up.');
    expect(screen.queryByTestId('attention-not-ready')).toBeNull();
    expect(screen.getByTestId('needs-attention').getAttribute('data-serious')).toBe('false');
  });

  it('says what could not be read, and still shows what could', async () => {
    answer(scenario('failed source'));
    render(<Dashboard client={client()} canReadSettings />);
    await loaded();
    expect(screen.getByTestId('overview-results-state').textContent).toBe('The results could not be read. Select Refresh to try again.');
    expect(screen.getByTestId('overview-next-partial').textContent).toBe('Some advice could not be checked. Select Refresh to try again.');
  });

  it('links prominently to Capabilities, and keeps Send a fax with the everyday faxes', async () => {
    answer(scenario('healthy'));
    const navigate = vi.fn();
    const send = vi.fn();
    render(<Dashboard client={client()} onNavigate={navigate} canReadSettings onSendFax={send} />);
    await loaded();
    fireEvent.click(screen.getByRole('button', { name: 'Capabilities' }));
    expect(navigate).toHaveBeenLastCalledWith('savings/capabilities');
    fireEvent.click(within(screen.getByTestId('overview-everyday')).getByRole('button', { name: 'Send a fax' }));
    expect(send).toHaveBeenCalled();
    fireEvent.click(screen.getByTestId('overview-everyday-expected'));
    expect(navigate).toHaveBeenLastCalledWith('faxes/expected');
    expect(screen.queryByText(/Auto-refreshing/)).toBeNull();
  });
});

describe('Overview freshness and order', () => {
  // A client whose status poll the test runs by hand.
  function polled() {
    const api = client();
    let poll: ((data: HealthStatus) => void) | null = null;
    vi.spyOn(api, 'startPolling').mockImplementation((callback) => { poll = callback; return () => undefined; });
    return { api, poll: (data: HealthStatus) => act(() => { poll!(data); }) };
  }
  const healthBody = (name: string) => ({ ...fixture.base['/admin/health-status'].body,
    ...((scenario(name).responses as Record<string, Reply>)['/admin/health-status']?.body as object) }) as unknown as HealthStatus;

  it('moves only the status time on a health poll, and says when the other blocks have gone stale', async () => {
    answer(scenario('healthy'));
    const start = new Date(2026, 9, 9, 10, 0, 0).getTime();
    let time = start;
    const { api, poll } = polled();
    render(<Dashboard client={api} canReadSettings clock={() => time} />);
    await loaded();
    const loadedAt = `Checked ${clockText(start)}.`;
    expect(screen.getByTestId('overview-doing-checked').textContent).toBe(loadedAt);
    expect(screen.getByTestId('overview-everyday-checked').textContent).toBe(loadedAt);

    time = start + 3 * 60 * 1000;
    poll(healthBody('healthy'));
    expect(screen.getByTestId('overview-status-checked').textContent).toBe(`Status checked ${clockText(time)}.`);
    expect(screen.getByTestId('overview-doing-checked').textContent).toBe(loadedAt);
    expect(screen.getByTestId('overview-next-checked').textContent).toBe(loadedAt);
    expect(screen.getByTestId('overview-everyday-checked').textContent).toBe(loadedAt);
    expect(screen.getByTestId('needs-attention-checked').textContent).toBe(loadedAt);

    time = start + STALE_AFTER_MS + 60 * 1000;
    poll(healthBody('healthy'));
    const stale = `Last checked at ${clockText(start)}. Select Refresh to check again.`;
    for (const id of ['doing', 'next', 'everyday']) {
      expect(screen.getByTestId(`overview-${id}`).getAttribute('data-state')).toBe('stale');
      expect(screen.getByTestId(`overview-${id}-state`).textContent).toBe(stale);
    }
    expect(screen.getByTestId('needs-attention-stale').textContent).toBe(stale);
    expect(screen.getByTestId('overview-status-checked').textContent).toBe(`Status checked ${clockText(time)}.`);
  });

  it('puts Needs attention first while a serious problem lasts, and back in its place after', async () => {
    answer(scenario('incident'));
    const { api, poll } = polled();
    const { container } = render(<Dashboard client={api} canReadSettings />);
    await loaded();
    expect(order(container)).toEqual(['attention', 'doing', 'next', 'everyday']);
    expect(screen.getByTestId('needs-attention').getAttribute('data-serious')).toBe('true');
    poll(healthBody('healthy'));
    expect(order(container)).toEqual(NORMAL);
    expect(screen.getByTestId('needs-attention').getAttribute('data-serious')).toBe('false');
  });
});
