import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import AdminAPIClient from '../api/client';
import type { Capabilities as CapabilitiesView, Capability } from '../api/capabilityTypes';
import Capabilities from '../components/capabilities/Capabilities';
import { resolvesTo } from '../navigation';
import { server } from '../test/server';
import fixture from './capabilities.json';

// One answer of GET /routing/capabilities, evaluated on a real synthetic installation, and the lines
// `faxbot savings capabilities` prints from it (api/tests/test_capabilities.py keeps both current). The console must
// show every one of those sentences, so the two surfaces never say different things.
const answer: CapabilitiesView = fixture.response as unknown as CapabilitiesView;
const items: Capability[] = answer.outcomes.flatMap((outcome) => outcome.capabilities);
const byKey = new Map(items.map((item) => [item.key, item]));
const client = () => new AdminAPIClient({ kind: 'key', key: 'synthetic-key' });

function serve(body: unknown = answer, status = 200) {
  server.use(http.get('/routing/capabilities', () => HttpResponse.json(body as never, { status })));
}

async function showList(show: string | null = null, onNavigate = vi.fn()) {
  serve();
  render(<Capabilities client={client()} show={show} onNavigate={onNavigate} />);
  await screen.findByTestId('capability-filter-all');
  return onNavigate;
}

describe('The capabilities read contract', () => {
  it('lists every capability once, by outcome, with its own address and no money', () => {
    const keys = items.map((item) => item.key);
    expect(new Set(keys).size).toBe(keys.length);
    expect(answer.outcomes).toHaveLength(7);
    for (const outcome of answer.outcomes) {
      for (const item of outcome.capabilities) {
        expect(item.outcome).toBe(outcome.key);
        expect(item.address).toBe(`savings/capabilities?key=${item.key}`);
      }
    }
    expect(JSON.stringify(answer)).not.toMatch(/\$|USD|"amount"|micros/);
  });

  it('carries every state the page filters on, and marks only ready capabilities as next improvements', () => {
    const filters = answer.filters.map((filter) => filter.key);
    expect(filters).toEqual(['on', 'off', 'ready', 'needs', 'experimental']);
    for (const key of filters) expect(items.some((item) => item.filters.includes(key))).toBe(true);
    for (const item of items) {
      expect(item.improvement === null).toBe(!item.ready);
      expect(item.missing).toBe(item.prerequisites.filter((prerequisite) => !prerequisite.met).length);
      // "Not here" always names what is missing.
      if (!item.works.here) expect(item.missing).toBeGreaterThan(0);
    }
  });

  it('only names console addresses that open a page', () => {
    expect(fixture.addresses.length).toBeGreaterThan(10);
    for (const address of [...fixture.addresses, ...items.map((item) => item.address)]) {
      expect(resolvesTo(address), address).not.toBeNull();
    }
  });
});

describe('Savings & optimization, Capabilities', () => {
  it('shows every outcome and capability in the same sentences as the command line, and no money', async () => {
    await showList();
    const page = document.body;
    let current: Capability | null = null;
    for (const line of fixture.cli.list) {
      const header = /^ {2}(.+) \(([a-z_]+)\): (.+)$/.exec(line);
      if (!line || line.startsWith('One capability in full:')) continue;
      if (header) {
        current = byKey.get(header[2])!;
        const card = screen.getByTestId(`capability-${current.key}`);
        expect(within(card).getByRole('link', { name: header[1] }).getAttribute('href')).toBe(`#/${current.address}`);
        const chips = screen.getByTestId(`capability-states-${current.key}`).textContent;
        for (const state of header[3].split(' · ')) expect(chips).toContain(state);
      } else if (line.startsWith('    Turn it on in ')) {
        const where = line.slice('    Turn it on in '.length, -1);
        expect(within(screen.getByTestId(`capability-${current!.key}`))
          .getByRole('link', { name: `Turn on ${current!.name} in ${where}` })).toBeTruthy();
      } else if (line.startsWith('    ')) {
        expect(screen.getByTestId(`capability-${current!.key}`).textContent).toContain(line.trim());
      } else {
        expect(page.textContent).toContain(line);
      }
    }
    for (const outcome of answer.outcomes) {
      expect(screen.getByRole('heading', { level: 2, name: outcome.title })).toBeTruthy();
    }
    expect(page.textContent).not.toMatch(/\$|USD/);
  });

  it('keeps its filter in the address and shows only what matches', async () => {
    const onNavigate = await showList('ready');
    const shown = fixture.cli.filtered.ready.map((line) => /^ {2}.+ \(([a-z_]+)\): /.exec(line)?.[1]).filter(Boolean);
    expect(shown.length).toBeGreaterThan(0);
    expect(document.querySelectorAll('[data-testid^="capability-states-"]')).toHaveLength(shown.length);
    for (const key of shown) expect(screen.getByTestId(`capability-${key}`)).toBeTruthy();
    expect(screen.getByText(answer.filters.find((filter) => filter.key === 'ready')!.sentence, { selector: 'p' }))
      .toBeTruthy();
    const ready = screen.getByTestId('capability-filter-ready');
    expect(ready.getAttribute('aria-current')).toBe('page');
    expect(ready.textContent).toBe(`Ready to turn on (${shown.length})`);
    const needs = screen.getByTestId('capability-filter-needs');
    expect(needs.getAttribute('href')).toBe('#/savings/capabilities?show=needs');
    fireEvent.click(needs);
    expect(onNavigate).toHaveBeenLastCalledWith('savings/capabilities?show=needs');
    fireEvent.click(screen.getByTestId('capability-filter-all'));
    expect(onNavigate).toHaveBeenLastCalledWith('savings/capabilities');
  });

  it('opens a capability and its setting from the list', async () => {
    const onNavigate = await showList();
    const shading = byKey.get('fax_friendly')!;
    const card = screen.getByTestId('capability-fax_friendly');
    fireEvent.click(within(card).getByRole('link', { name: shading.name }));
    expect(onNavigate).toHaveBeenLastCalledWith('savings/capabilities?key=fax_friendly');
    fireEvent.click(within(card).getByRole('link', { name: `Turn on ${shading.name} in ${shading.setting.label}` }));
    expect(onNavigate).toHaveBeenLastCalledWith(shading.setting.address);
  });
});

describe('One capability (?key=)', () => {
  for (const [key, lines] of Object.entries(fixture.cli.show)) {
    it(`shows ${key} in the same sentences as faxbot savings capabilities show`, async () => {
      const item = byKey.get(key)!;
      const onNavigate = vi.fn();
      serve();
      render(<Capabilities client={client()} capability={key} onNavigate={onNavigate} />);
      const page = await screen.findByTestId('capability-page');
      expect(within(page).getByRole('heading', { level: 1, name: item.name })).toBeTruthy();
      let prerequisite = 0;
      for (const line of (lines as string[]).slice(2)) {
        const needed = /^ {2}(.+?) · (.+?): (.+) See (.+)\.$/.exec(line);
        const figures = /^Its figures: (.+) \((.+)\)$/.exec(line);
        if (!line) continue;
        if (needed) {
          const row = within(page).getByTestId(`capability-prerequisite-${prerequisite}`);
          prerequisite += 1;
          for (const part of [needed[1], `${needed[2]}:`, needed[3]]) expect(row.textContent).toContain(part);
          expect(within(row).getByRole('link', { name: needed[4] })).toBeTruthy();
        } else if (figures) {
          expect(page.textContent).toContain(`Its figures: ${figures[1]}`);
          expect(page.textContent).not.toContain(figures[2]);
        } else if (line.startsWith('Command line: ')) {
          // The command line is the CLI's; the console names the page instead.
          expect(page.textContent).not.toContain(line.slice('Command line: '.length));
        } else if (line.startsWith('Turn it on in ')) {
          expect(within(page).getByRole('link', { name: `Turn on ${item.name} in ${line.slice(14, -1)}` })).toBeTruthy();
        } else if (line === 'What it needs') {
          expect(within(page).getByRole('heading', { level: 2, name: line })).toBeTruthy();
        } else {
          expect(page.textContent).toContain(line.trim());
        }
      }
      for (const state of lines[1].split(' · ')) expect(screen.getByTestId(`capability-states-${key}`).textContent).toContain(state);
      expect(prerequisite).toBe(item.prerequisites.length);
      // No command lines on the console page: no developer text on operator screens.
      expect(page.textContent).not.toMatch(/faxbot /);
      expect(page.textContent).not.toMatch(/\$|USD/);
    });
  }

  it('links its setting, its prerequisites and its figures to their pages', async () => {
    const item = byKey.get('encoded_pages')!;
    const onNavigate = vi.fn();
    serve();
    render(<Capabilities client={client()} capability="encoded_pages" onNavigate={onNavigate} />);
    const page = await screen.findByTestId('capability-page');
    const setting = within(screen.getByTestId('capability-setting')).getByRole('link', { name: item.setting.label });
    expect(setting.getAttribute('href')).toBe(`#/${item.setting.address}`);
    fireEvent.click(within(screen.getByTestId('capability-results')).getByRole('link', { name: item.results!.label }));
    expect(onNavigate).toHaveBeenLastCalledWith(item.results!.address);
    fireEvent.click(within(page).getByRole('link', { name: 'All capabilities' }));
    expect(onNavigate).toHaveBeenLastCalledWith('savings/capabilities');
  });

  it('says so when the address names no capability, instead of showing another page', async () => {
    serve();
    render(<Capabilities client={client()} capability="not_a_capability" />);
    expect(await screen.findByText('This capability is not in the list.')).toBeTruthy();
    expect(screen.getByRole('link', { name: 'All capabilities' }).getAttribute('href')).toBe('#/savings/capabilities');
    expect(screen.queryByTestId('capability-page')).toBeNull();
  });
});

describe('When the read is refused or fails', () => {
  it('says the role does not include reading settings', async () => {
    serve({ detail: 'This operation is not permitted.' }, 403);
    render(<Capabilities client={client()} />);
    expect(await screen.findByTestId('capabilities-denied')).toBeTruthy();
    expect(screen.queryByTestId(`capability-outcome-${answer.outcomes[0].key}`)).toBeNull();
  });

  it('shows the failure and lists nothing', async () => {
    serve({ detail: 'Delivery route storage is unavailable.' }, 503);
    render(<Capabilities client={client()} />);
    await waitFor(() => expect(screen.getByRole('alert')).toBeTruthy());
    expect(screen.queryByTestId('capabilities-denied')).toBeNull();
    expect(screen.queryByTestId(`capability-outcome-${answer.outcomes[0].key}`)).toBeNull();
  });
});
