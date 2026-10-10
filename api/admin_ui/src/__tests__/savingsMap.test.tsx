import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import AdminAPIClient from '../api/client';
import type { SavingsMechanisms } from '../api/deliveryTypes';
import type { Capabilities } from '../api/capabilityTypes';
import Dashboard from '../components/Dashboard';
import SavingsMap from '../components/SavingsMap';
import Recommendations, { recommendationSectionId } from '../components/delivery/Recommendations';
import Savings, { savingsPartId } from '../components/delivery/Savings';
import { emptySavings, server } from '../test/server';
import fixture from './savingsMap.json';
import capabilityFixture from './capabilities.json';

// One answer of GET /routing/savings/mechanisms, evaluated on a real synthetic installation; `faxbot costs
// mechanisms` prints exactly fixture.cli from it (api/tests/test_savings_mechanisms.py).
const answer = fixture.response as unknown as SavingsMechanisms;
const items = answer.stages.flatMap((stage) => stage.mechanisms);
const client = () => new AdminAPIClient({ kind: 'key', key: 'synthetic-key' });

describe('The savings map on Overview', () => {
  it('shows every stage and mechanism in the same sentences as the command line, and no money', () => {
    render(<SavingsMap data={answer} />);
    const map = screen.getByTestId('savings-map');
    expect(within(map).getByRole('heading', { name: answer.title })).toBeTruthy();
    const stageTitles = within(map).getAllByRole('heading', { level: 3 }).map((heading) => heading.textContent);
    expect(stageTitles).toEqual(answer.stages.map((stage) => stage.title));
    for (const entry of answer.legend) expect(map.textContent).toContain(entry.sentence);
    for (const item of items) {
      const card = screen.getByTestId(`savings-map-${item.key}`);
      // Every line the command prints for this mechanism, except its own description, is on its card.
      expect(card.textContent).toContain(item.name);
      expect(card.textContent).toContain(item.enabled.label);
      expect(card.textContent).toContain(item.works.label);
      expect(screen.getByTestId(`savings-map-tested-${item.key}`).textContent).toBe(item.evidence.label);
      if (item.here.sentence) {
        expect(card.textContent).toContain(item.here.sentence);
        expect(fixture.cli).toContain(`    ${item.here.sentence}`);
      }
      if (item.enabled.sentence) expect(card.textContent).toContain(item.enabled.sentence);
      if (item.works.sentence) expect(card.textContent).toContain(item.works.sentence);
    }
    // "Advice only" is said once, under the advice stage's title.
    const advice = answer.stages.find((stage) => stage.key === 'advice')!;
    expect(screen.getAllByText(advice.sentence as string)).toHaveLength(1);
    expect(map.textContent).not.toContain('$');
    expect(map.textContent).not.toMatch(/USD|[0-9a-f]{32}/);
  });

  it('opens each mechanism\'s page in Capabilities, and offers Turn on only where it is off and works here', () => {
    const navigate = vi.fn();
    render(<SavingsMap data={answer} onNavigate={navigate} />);
    fireEvent.click(screen.getByTestId('savings-map-sending_together'));
    expect(navigate).toHaveBeenLastCalledWith('savings/capabilities?key=sending_together');
    fireEvent.keyDown(screen.getByTestId('savings-map-blocked_senders'), { key: 'Enter' });
    expect(navigate).toHaveBeenLastCalledWith('savings/capabilities?key=blocked_senders');

    const offered = items.filter((item) => item.turn_on);
    expect(offered.length).toBeGreaterThan(0);
    expect(screen.getAllByRole('button', { name: /^Turn on / })).toHaveLength(offered.length);
    navigate.mockClear();
    const shading = items.find((item) => item.key === 'fax_friendly')!;
    fireEvent.click(screen.getByRole('button', { name: `Turn on ${shading.name} in ${shading.page_label}` }));
    // The setting's page only: the card's own link to Savings does not fire as well.
    expect(navigate.mock.calls).toEqual([['providers/sending']]);
    // Every mechanism has its page, even one with no figures of its own; advice and charge checks too.
    fireEvent.click(screen.getByTestId('savings-map-busy_hours'));
    expect(navigate).toHaveBeenLastCalledWith('savings/capabilities?key=busy_hours');
    fireEvent.click(screen.getByRole('link', { name: 'Plans worth their fee: open it in Capabilities' }));
    expect(navigate).toHaveBeenLastCalledWith('savings/capabilities?key=advice_plans');
    fireEvent.click(screen.getByTestId('savings-map-charge_checks'));
    expect(navigate).toHaveBeenLastCalledWith('savings/capabilities?key=charge_checks');
    for (const item of items) {
      expect(screen.getByTestId(`savings-map-${item.key}`).getAttribute('role')).toBe('link');
    }
  });

  it('names each Turn on by its setting\'s six-area page when it has the capabilities read', () => {
    const navigate = vi.fn();
    const capabilities = capabilityFixture.response as unknown as Capabilities;
    render(<SavingsMap data={answer} capabilities={capabilities} onNavigate={navigate} />);
    const shading = capabilities.outcomes.flatMap((outcome) => outcome.capabilities).find((item) => item.key === 'fax_friendly')!;
    fireEvent.click(screen.getByRole('button', { name: `Turn on ${shading.name} in ${shading.setting.label}` }));
    expect(navigate.mock.calls).toEqual([[shading.setting.address]]);
    // No Turn on names an older menu path.
    for (const button of screen.getAllByRole('button', { name: /^Turn on / })) {
      expect(button.getAttribute('aria-label')).not.toMatch(/Providers|Costs|Numbers|System/);
    }
  });

  it('draws the fax path with arrows and the advice beside it', () => {
    render(<SavingsMap data={answer} />);
    const advice = screen.getByTestId('savings-map-stage-advice');
    expect(within(advice).getByRole('heading', { level: 3 }).textContent).toBe('Advice you act on');
    expect(within(advice).getAllByRole('heading', { level: 4 })).toHaveLength(
      answer.stages.find((stage) => stage.key === 'advice')!.mechanisms.length);
    const path = answer.stages.filter((stage) => stage.path);
    expect(path.map((stage) => stage.key)).toEqual(['document', 'route', 'call', 'after', 'receiving']);
    for (const stage of path) expect(screen.getByTestId(`savings-map-stage-${stage.key}`)).toBeTruthy();
  });

  it('is at the bottom of Overview for a person who reads settings, loaded once with the cards', async () => {
    let reads = 0;
    server.use(http.get('/routing/savings/mechanisms', () => { reads += 1; return HttpResponse.json(answer); }));
    render(<Dashboard client={client()} canReadSettings onNavigate={vi.fn()} />);
    expect(await screen.findByTestId('savings-map')).toBeTruthy();
    expect(screen.getByText(items[0].name)).toBeTruthy();
    expect(reads).toBe(1);
  });

  it('is not there at all for a person who may not read settings', async () => {
    let reads = 0;
    server.use(http.get('/routing/savings/mechanisms', () => { reads += 1; return HttpResponse.json(answer); }));
    render(<Dashboard client={client()} canReadSettings={false} />);
    expect(await screen.findByText('Overview')).toBeTruthy();
    expect(screen.queryByTestId('savings-map')).toBeNull();
    expect(reads).toBe(0);
  });

  it('shows nothing, never an error, when the server refuses the map', async () => {
    let refused = 0;
    server.use(http.get('/routing/savings/mechanisms', () => {
      refused += 1;
      return HttpResponse.json({ detail: 'This operation is not permitted.' }, { status: 403 });
    }));
    render(<Dashboard client={client()} canReadSettings />);
    expect(await screen.findByText('Overview')).toBeTruthy();
    await waitFor(() => expect(refused).toBe(1));
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(screen.queryByTestId('savings-map')).toBeNull();
    expect(screen.queryByTestId('savings-map-error')).toBeNull();
  });
});

describe('Costs, Recommendations anchors', () => {
  it('outlines the section the address names, and says so when it has nothing yet', async () => {
    const scrolled = vi.fn();
    const original = Element.prototype.scrollIntoView;
    Element.prototype.scrollIntoView = scrolled;
    try {
      render(<Recommendations client={client()} focus="steps" />);
      const section = await screen.findByTestId(recommendationSectionId('steps'));
      expect(section.getAttribute('data-focused')).toBe('true');
      expect(await within(section).findByText('Calls just past a billed minute: nothing to suggest yet.')).toBeTruthy();
      expect(screen.getByTestId(recommendationSectionId('plans')).getAttribute('data-focused')).toBeNull();
      expect(scrolled).toHaveBeenCalled();
    } finally {
      Element.prototype.scrollIntoView = original;
    }
  });
});

describe('Costs, Savings anchors', () => {
  it('outlines and scrolls to the part the address names', async () => {
    const savings = emptySavings() as Record<string, unknown>;
    savings.blocked_calls = { estimate: false, saved: [], calls: 1,
      sentence: '1 call from blocked senders was turned away before Faxbot answered.' };
    server.use(http.get('/routing/savings', () => HttpResponse.json(savings)));
    const scrolled = vi.fn();
    const original = Element.prototype.scrollIntoView;
    Element.prototype.scrollIntoView = scrolled;
    try {
      render(<Savings client={client()} focus="blocked_calls" />);
      const part = await screen.findByTestId('savings-blocked-calls');
      expect(part.id).toBe(savingsPartId('blocked_calls'));
      expect(part.getAttribute('data-focused')).toBe('true');
      expect(part.textContent).toContain('1 call from blocked senders was turned away before Faxbot answered.');
      // A count, not an estimate.
      expect(within(part).queryByText('Estimate')).toBeNull();
      expect(screen.getByTestId('savings-together').getAttribute('data-focused')).toBeNull();
      expect(scrolled).toHaveBeenCalledTimes(1);
    } finally {
      Element.prototype.scrollIntoView = original;
    }
  });
});
