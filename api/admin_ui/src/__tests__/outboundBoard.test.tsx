// The live outbound board (GET /outbound/now) on Overview and Sent: every tile and empty state, sections the server
// could not read, the engine banner, the held-fax decisions through the existing holds API, the big-send bars, and
// the one shared poll (paused while hidden, never overlapping, backing off when busy without signing out).
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { delay, http, HttpResponse } from 'msw';
import AdminAPIClient, { AdminAPIError } from '../api/client';
import type { OutboundNow, OutboundNowReply } from '../api/outboundTypes';
import Dashboard from '../components/Dashboard';
import JobsList from '../components/JobsList';
import OutboundBoard, { CompactOutboundBoard } from '../components/overview/OutboundBoard';
import {
  BOARD_TEXT, durationText, needsYouLine, sendingLine, todayLine, waitingLine, whenText,
} from '../components/overview/outboundText';
import { OUTBOUND_POLL, OutboundPoller, type PollerDeps } from '../hooks/useOutboundNow';
import { emptyOutboundNow, server } from '../test/server';

const keyClient = (onUnauthorized?: () => void) => new AdminAPIClient({ kind: 'key', key: 'synthetic-key' }, { onUnauthorized });

const CADENCE = { ...OUTBOUND_POLL };
afterEach(() => {
  Object.assign(OUTBOUND_POLL, CADENCE);
  Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'visible' });
});

const later = (minutes: number) => new Date(Date.now() + minutes * 60 * 1000).toISOString();
const earlier = (seconds: number) => new Date(Date.now() - seconds * 1000).toISOString();

function busyBoard(): OutboundNow {
  return {
    as_of: earlier(1), not_checked: [],
    sending: { count: 2, items: [
      { id: 'job-s1', to: '+1 303 555 ••12', recipient: 'Riverside Clinic', pages: 4, page: 2,
        route: 'Telnyx (your phone line)', engine: 'SSL Fax engine', started_at: earlier(125) },
      { id: 'job-s2', to: '+1 720 555 ••34', recipient: null, pages: 3, page: null, route: 'Phaxio', engine: null,
        started_at: null },
    ] },
    waiting: { count: 41, more: 5, reasons: [
      { key: 'recipient_hours', count: 4, sentence: "Waiting for the recipient's fax hours." },
      { key: 'lines_busy', count: 30, sentence: "Waiting for a free line: 2 of 2 calls in use, your carrier's limit at once." },
      { key: 'retry', count: 0, sentence: "Waiting to try again after an attempt that didn't get through." },
      { key: 'number_busy', count: 2, sentence: 'Waiting: another fax is calling the same number.' },
    ], items: [
      { id: 'job-w1', to: '+1 303 555 ••56', recipient: 'Mesa Cardiology', pages: 3, reason: 'lines_busy',
        sentence: 'Waiting for a free line: 2 of 2 calls in use.', since: earlier(300), until: null },
      { id: 'job-w2', to: '+1 303 555 ••78', recipient: null, pages: 1, reason: 'recipient_hours',
        sentence: "Waiting for the recipient's fax hours.", since: earlier(600), until: later(90) },
    ] },
    needs_you: { count: 4, items: [
      { id: 'job-n1', kind: 'approval', to: '+1 303 555 ••90', recipient: 'Front Range Labs', pages: 24, since: earlier(900),
        sentence: 'A rule asks for approval before this fax goes.', hold: { id: 'hold-1', version: 3 }, actions: ['approve', 'refuse'] },
      { id: 'job-n2', kind: 'no_route', to: '+1 303 555 ••11', recipient: 'Boulder Imaging', pages: 2, since: earlier(1200),
        sentence: 'No sending rule allows a route for this fax.', hold: { id: 'hold-2', version: 5 },
        actions: ['send_by_account', 'check_again', 'refuse'] },
      { id: 'job-n3', kind: 'uncertain', to: '+1 303 555 ••22', recipient: null, pages: 5, since: earlier(1500),
        sentence: "Faxbot couldn't confirm whether this fax arrived.", hold: null, actions: ['settle'] },
      { id: 'job-n4', kind: 'other', to: '+1 303 555 ••33', recipient: 'Aurora Clinic', pages: 1, since: earlier(1800),
        sentence: 'This fax needs a look in Sent.', hold: null, actions: [] },
    ] },
    today: { since: earlier(36000), accepted: 260, delivered: 231, failed: 12, failures: [
      { key: 'no_answer', label: 'No one answered', count: 3 },
      { key: 'busy', label: 'The line was busy', count: 5 },
      { key: 'other', label: 'Another problem (see Sent)', count: 4 },
    ] },
    lines: [{ key: 'trunk:telnyx', label: 'Your phone line', in_use: 2, limit: 2, incoming: 1,
      sentence: '2 of 2 calls in use (1 incoming). Your carrier allows 2 at once.' }],
    engine: null,
    groups: [{ who: 'EHR outbound (API key)', total: 240, delivered: 180, sending: 2, waiting: 40, needs_you: 3, failed: 15 }],
  };
}

function answer(board: OutboundNow | ReturnType<typeof emptyOutboundNow>) {
  server.use(http.get('/outbound/now', () => HttpResponse.json(board)));
}

async function renderBoard(board: OutboundNow | ReturnType<typeof emptyOutboundNow>, props: { onOpenSentFax?: (id: string) => void } = {}) {
  answer(board);
  const client = keyClient();
  render(<OutboundBoard client={client} {...props} />);
  await waitFor(() => expect(screen.getByTestId('outbound-board').getAttribute('data-state')).toBe('ok'));
  return client;
}

const tile = (key: string) => screen.getByTestId(`tile-${key}`);
const count = (key: string) => within(tile(key)).getByTestId(`tile-${key}-count`).textContent;
const line = (key: string) => within(tile(key)).getByTestId(`tile-${key}-line`).textContent;

describe('the outbound board on Overview', () => {
  it('shows the four tiles with a number and one short line, and the lines meter', async () => {
    await renderBoard(busyBoard());
    expect(count('sending')).toBe('2');
    expect(line('sending')).toBe('Riverside Clinic and 1 more');
    expect(count('waiting')).toBe('41');
    // The line is at its carrier's limit, so that is why faxes wait.
    expect(line('waiting')).toBe('Your phone line is full: 2 of 2 calls in use.');
    expect(count('needs_you')).toBe('4');
    expect(line('needs_you')).toBe('1 waiting for approval, 1 with no route your rules allow, and 2 more');
    expect(tile('needs_you').getAttribute('data-urgent')).toBe('true');
    expect(count('today')).toBe('231');
    expect(tile('today').textContent).toContain('delivered');
    // 231 delivered and 12 failed of 260: 17 are still on their way.
    expect(line('today')).toBe('12 failed, 17 still on their way, of 260 faxes today.');

    const meter = screen.getByTestId('outbound-line');
    expect(meter.getAttribute('data-full')).toBe('true');
    expect(within(meter).getByTestId('line-in-use').textContent).toBe('2 of 2 in use');
    expect(within(meter).getByText('Full')).toBeTruthy();
    expect(within(meter).getByText('2 of 2 calls in use (1 incoming). Your carrier allows 2 at once.')).toBeTruthy();
    expect([...meter.querySelectorAll('[data-slot]')].map((slot) => slot.getAttribute('data-slot'))).toEqual(['out', 'in']);
    expect(screen.getByTestId('outbound-checked').textContent).toMatch(/^Updated \d{1,2}:\d{2}:\d{2}\b[^.]*$/);
    expect(screen.queryByTestId('outbound-engine')).toBeNull();
  });

  it('says each empty state in plain words, and Needs you is calm at zero', async () => {
    await renderBoard({ ...emptyOutboundNow(), lines: [{ key: 'trunk:t', label: 'Your phone line', in_use: 0, limit: 2,
      incoming: 0, sentence: '0 of 2 calls in use. Your carrier allows 2 at once.' }] });
    expect(count('sending')).toBe('0');
    expect(line('sending')).toBe('Nothing is sending right now.');
    expect(line('waiting')).toBe('Nothing is waiting.');
    expect(line('needs_you')).toBe('Nothing needs you.');
    expect(tile('needs_you').getAttribute('data-urgent')).toBe('false');
    expect(line('today')).toBe('No faxes sent yet today.');
    expect(screen.getByTestId('outbound-line').getAttribute('data-full')).toBe('false');
    // No big send today: no section for it.
    expect(screen.queryByTestId('outbound-groups')).toBeNull();

    for (const [key, text] of [['sending', 'Nothing is sending right now.'], ['waiting', 'Nothing is waiting.'],
      ['needs_you', 'Nothing needs you.'], ['today', 'Nothing failed today.']]) {
      fireEvent.click(tile(key));
      expect(tile(key).getAttribute('aria-expanded')).toBe('true');
      expect(within(await screen.findByTestId(`outbound-list-${key}`)).getByText(text)).toBeTruthy();
    }
  });

  it("says Couldn't check this just now for a section the server could not read, never a 0", async () => {
    await renderBoard({ ...busyBoard(), not_checked: ['sending', 'needs_you', 'lines', 'engine', 'groups'] });
    for (const key of ['sending', 'needs_you']) {
      expect(within(tile(key)).getByTestId(`tile-${key}-not-checked`).textContent).toBe("Couldn't check this just now");
      expect(within(tile(key)).queryByTestId(`tile-${key}-count`)).toBeNull();
    }
    // A section that couldn't be read is never shown as calm or urgent.
    expect(tile('needs_you').getAttribute('data-urgent')).toBe('false');
    expect(count('waiting')).toBe('41');
    // Without the lines, the Waiting tile falls back to the biggest reason.
    expect(line('waiting')).toBe("Waiting for a free line: 2 of 2 calls in use, your carrier's limit at once.");
    expect(screen.getByTestId('lines-not-checked').textContent).toBe("Couldn't check this just now");
    expect(screen.getByTestId('engine-not-checked').textContent).toBe("Couldn't check the fax engine just now.");
    expect(within(screen.getByTestId('outbound-groups')).getByText("Couldn't check this just now")).toBeTruthy();
    fireEvent.click(tile('sending'));
    expect(within(await screen.findByTestId('outbound-list-sending')).getByText("Couldn't check this just now")).toBeTruthy();
  });

  it('shows the engine banner only while Faxbot has switched new faxes to audio', async () => {
    await renderBoard({ ...busyBoard(), engine: { audio: true, since: earlier(3600),
      sentence: 'New faxes over your phone line go as audio, because the last three calls failed the other way.',
      fix: 'Faxbot switches back by itself when a test call gets through.' } });
    const banner = screen.getByTestId('outbound-engine');
    expect(banner.textContent).toContain('New faxes over your phone line go as audio, because the last three calls failed the other way.');
    expect(banner.textContent).toContain('Faxbot switches back by itself when a test call gets through.');
  });

  it('says when Telnyx has stopped your calls, or your Telnyx balance is low, and nothing when it is fine', async () => {
    const stopped = 'Telnyx has turned your account off (your balance is -$1.38), so faxes cannot be sent or received '
      + 'through Telnyx. Faxbot sends by your other routes until you add funds in the Telnyx portal under Billing.';
    await renderBoard({ ...busyBoard(), carrier_balance: { state: 'stopped', sentence: stopped,
      available: { amount: '-1.38', currency: 'USD' }, read_at: earlier(60) } });
    expect(screen.getByTestId('outbound-carrier-balance').textContent).toBe(stopped);
    cleanup();
    await renderBoard({ ...busyBoard(), carrier_balance: null });
    expect(screen.queryByTestId('outbound-carrier-balance')).toBeNull();
  });

  it('lists what is sending with its page, route, engine and ticking time on the line', async () => {
    await renderBoard(busyBoard());
    fireEvent.click(tile('sending'));
    const items = await within(await screen.findByTestId('outbound-list-sending')).findAllByTestId('sending-item');
    expect(items[0].textContent).toContain('Riverside Clinic');
    expect(items[0].textContent).toContain('+1 303 555 ••12, page 2 of 4, Telnyx (your phone line), SSL Fax engine');
    expect(within(items[0]).getByTestId('on-the-line').textContent).toMatch(/^2:0\d on the line$/);
    // No progress reported: the page count, never an estimated page.
    expect(items[1].textContent).toContain('+1 720 555 ••34');
    expect(items[1].textContent).toContain('3 pages, Phaxio');
    expect(items[1].textContent).not.toContain('page 1');
    expect(within(items[1]).queryByTestId('on-the-line')).toBeNull();
    const first = within(items[0]).getByTestId('on-the-line').textContent;
    await waitFor(() => expect(within(items[0]).getByTestId('on-the-line').textContent).not.toBe(first), { timeout: 2500 });
  });

  it('lists why faxes wait, largest first, then the first in line with their own sentence and time', async () => {
    const board = busyBoard();
    await renderBoard(board);
    fireEvent.click(tile('waiting'));
    const list = await screen.findByTestId('outbound-list-waiting');
    expect(within(list).getAllByTestId('waiting-reason').map((node) => node.textContent)).toEqual([
      "30Waiting for a free line: 2 of 2 calls in use, your carrier's limit at once.",
      "4Waiting for the recipient's fax hours.",
      '2Waiting: another fax is calling the same number.',
    ]);
    expect(within(list).getByTestId('waiting-more').textContent).toBe('+5 more further back in line.');
    const items = within(list).getAllByTestId('waiting-item');
    expect(items[0].textContent).toContain('Mesa Cardiology');
    expect(within(items[0]).queryByTestId('waiting-until')).toBeNull();
    expect(within(items[1]).getByTestId('waiting-until').textContent).toBe(`until ${whenText(board.waiting.items[1].until, Date.now())}`);
    expect(items[1].textContent).toContain("Waiting for the recipient's fax hours.");
    expect(within(list).getByText('Showing the first 2 of 41.')).toBeTruthy();
  });

  it("lists today's failures as a short ranked list of plain reasons", async () => {
    await renderBoard(busyBoard());
    fireEvent.click(tile('today'));
    const list = await screen.findByTestId('outbound-list-today');
    expect(within(list).getByTestId('today-delivered').textContent).toBe('231');
    expect(within(list).getByTestId('today-failed').textContent).toBe('12');
    expect(within(list).getAllByTestId('today-failure').map((node) => node.textContent)).toEqual([
      '5The line was busy', '4Another problem (see Sent)', '3No one answered',
    ]);
  });

  it('shows each big send as one stacked bar with its delivered count and submitter', async () => {
    await renderBoard(busyBoard());
    const group = screen.getByTestId('outbound-group');
    expect(group.textContent).toContain('EHR outbound (API key)');
    expect(within(group).getByTestId('group-delivered').textContent).toBe('180 of 240 delivered');
    const segments = [...group.querySelectorAll('[data-segment]')] as HTMLElement[];
    expect(segments.map((node) => [node.getAttribute('data-segment'), node.getAttribute('data-count')])).toEqual([
      ['delivered', '180'], ['sending', '2'], ['waiting', '40'], ['needs_you', '3'], ['failed', '15'],
    ]);
    expect(segments[0].style.width).toBe('75%');
    expect(group.textContent).toContain('40 waiting');
    expect(group.textContent).toContain('3 needs you');
  });
});

describe('Needs you decisions use the existing holds API', () => {
  type Seen = { path: string; body: unknown };
  function holdsApi(seen: Seen[]) {
    const record = (name: string) => http.post(`/routing/holds/:id/${name}`, async ({ request, params }) => {
      seen.push({ path: `/routing/holds/${params.id}/${name}`, body: await request.json() });
      return HttpResponse.json({ id: params.id, job_id: 'job', kind: 'approval', to_number: '+1', pages: 1, sender_name: null,
        requested_at: earlier(10), until: null, reason: '', can_decide: true, version: 9, sentence: null });
    });
    server.use(record('approve'), record('refuse'), record('check-again'));
  }

  it('approves and checks again with the hold and its version, and offers only the actions the server gave', async () => {
    const seen: Seen[] = [];
    holdsApi(seen);
    await renderBoard(busyBoard());
    fireEvent.click(tile('needs_you'));
    const list = await screen.findByTestId('outbound-list-needs_you');
    const items = within(list).getAllByTestId('needs-you-item');
    expect(within(items[0]).getAllByRole('button').map((button) => button.textContent)).toEqual(['Approve', 'Refuse']);
    expect(within(items[1]).getAllByRole('button').map((button) => button.textContent))
      .toEqual(['Send by another account', 'Check again', 'Refuse']);

    fireEvent.click(within(items[0]).getByRole('button', { name: 'Approve: the fax to Front Range Labs' }));
    expect(await within(list).findByText('Approved. The fax to Front Range Labs is no longer held.')).toBeTruthy();
    fireEvent.click(within(items[1]).getByRole('button', { name: 'Check again: the fax to Boulder Imaging' }));
    await waitFor(() => expect(seen).toHaveLength(2));
    expect(seen).toEqual([
      { path: '/routing/holds/hold-1/approve', body: { version: 3 } },
      { path: '/routing/holds/hold-2/check-again', body: { version: 5 } },
    ]);
  });

  it('looks again at once after a decision, not at the next tick', async () => {
    Object.assign(OUTBOUND_POLL, { activeMs: 60 * 1000, quietMs: 60 * 1000 });
    const seen: Seen[] = [];
    holdsApi(seen);
    let asked = 0;
    server.use(http.get('/outbound/now', () => { asked += 1; return HttpResponse.json(busyBoard()); }));
    render(<OutboundBoard client={keyClient()} />);
    await waitFor(() => expect(screen.getByTestId('outbound-board').getAttribute('data-state')).toBe('ok'));
    expect(asked).toBe(1);
    fireEvent.click(tile('needs_you'));
    fireEvent.click(await screen.findByRole('button', { name: 'Approve: the fax to Front Range Labs' }));
    await waitFor(() => expect(seen).toHaveLength(1));
    await waitFor(() => expect(asked).toBe(2));
  });

  it('asks for a reason before refusing, as the held-fax control does', async () => {
    const seen: Seen[] = [];
    holdsApi(seen);
    await renderBoard(busyBoard());
    fireEvent.click(tile('needs_you'));
    fireEvent.click(await screen.findByRole('button', { name: 'Refuse: the fax to Front Range Labs' }));
    const dialog = await screen.findByRole('dialog', { name: 'Refuse the fax to Front Range Labs?' });
    const refuse = within(dialog).getByRole('button', { name: 'Refuse' }) as HTMLButtonElement;
    expect(refuse.disabled).toBe(true);
    expect(seen).toEqual([]);
    fireEvent.change(within(dialog).getByLabelText('Reason'), { target: { value: 'Wrong recipient' } });
    fireEvent.click(refuse);
    await waitFor(() => expect(seen).toEqual([{ path: '/routing/holds/hold-1/refuse', body: { version: 3, reason: 'Wrong recipient' } }]));
    expect(await screen.findByText('Refused. Nothing was sent to Front Range Labs.')).toBeTruthy();
  });

  it('reads the accounts a fax may still go by when asked, then sends it by the one chosen', async () => {
    const seen: Seen[] = [];
    holdsApi(seen);
    server.use(http.get('/routing/holds', () => HttpResponse.json({ holds: [{
      id: 'hold-2', job_id: 'job-n2', kind: 'no_route', to_number: '+1 303 555 ••11', pages: 2, sender_name: null,
      requested_at: earlier(1200), until: null, reason: 'No sending rule allows a route for this fax.', can_decide: true,
      version: 6, options: [{ account: 'phaxio-main', label: 'Phaxio', reason: 'over the $0.50 cap: estimated $0.90' }],
    }] })));
    await renderBoard(busyBoard());
    fireEvent.click(tile('needs_you'));
    fireEvent.click(await screen.findByRole('button', { name: 'Send by another account: the fax to Boulder Imaging' }));
    const dialog = await screen.findByRole('dialog', { name: 'Send the fax to Boulder Imaging anyway?' });
    expect(within(dialog).getByText('Phaxio: over the $0.50 cap: estimated $0.90')).toBeTruthy();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Send by Phaxio anyway' }));
    // The hold's latest version, read with its accounts.
    await waitFor(() => expect(seen).toEqual([{ path: '/routing/holds/hold-2/approve', body: { version: 6, account: 'phaxio-main' } }]));
    expect(await screen.findByText('Approved. The fax to Boulder Imaging goes by Phaxio.')).toBeTruthy();
  });

  it('opens an unknown result, and a fax with no decision here, in Sent', async () => {
    const open = vi.fn();
    await renderBoard(busyBoard(), { onOpenSentFax: open });
    fireEvent.click(tile('needs_you'));
    fireEvent.click(await screen.findByRole('button', { name: 'Open in Sent: the fax to +1 303 555 ••22' }));
    fireEvent.click(screen.getByRole('button', { name: 'Open in Sent: the fax to Aurora Clinic' }));
    expect(open.mock.calls).toEqual([['job-n3'], ['job-n4']]);
  });

  it('opens an unknown result in Sent from Overview, through the shell', async () => {
    answer(busyBoard());
    const open = vi.fn();
    render(<Dashboard client={keyClient()} onOpenSentFax={open} />);
    fireEvent.click(await screen.findByTestId('tile-needs_you'));
    fireEvent.click(await screen.findByRole('button', { name: 'Open in Sent: the fax to +1 303 555 ••22' }));
    expect(open).toHaveBeenCalledWith('job-n3');
  });
});

describe('a fax the server sent without its own sentence', () => {
  it('says its reason or the kind of decision it waits for, never nothing', async () => {
    const board = busyBoard();
    board.waiting.items = [
      { ...board.waiting.items[0], sentence: null },
      { ...board.waiting.items[1], reason: 'unknown_later', sentence: null },
    ];
    board.needs_you.items = board.needs_you.items.map((item) => ({ ...item, sentence: null }));
    await renderBoard(board);
    fireEvent.click(tile('waiting'));
    const waiting = within(await screen.findByTestId('outbound-list-waiting')).getAllByTestId('waiting-item');
    expect(waiting[0].textContent).toContain("Waiting for a free line: 2 of 2 calls in use, your carrier's limit at once.");
    expect(waiting[1].textContent).toContain('Waiting to go.');
    fireEvent.click(tile('needs_you'));
    const needs = within(await screen.findByTestId('outbound-list-needs_you')).getAllByTestId('needs-you-item');
    expect(needs.map((node) => node.querySelectorAll('p')[1]?.textContent ?? node.textContent)).toEqual([
      'Waiting for approval.', 'No route your rules allow.', 'The result is unknown.', 'Needs a look in Sent.',
    ]);
    // The refuse dialog keeps working with the kind's sentence.
    fireEvent.click(screen.getByRole('button', { name: 'Refuse: the fax to Front Range Labs' }));
    expect(await screen.findByRole('dialog', { name: 'Refuse the fax to Front Range Labs?' })).toBeTruthy();
  });
});

describe("the tiles' lines", () => {
  it('reads past the 20 listed faxes, full lines, a day of late deliveries and long calls plainly', () => {
    const board = busyBoard();
    const many = { ...board, needs_you: { count: 25, items: Array.from({ length: 20 }, (_, index) => ({
      ...board.needs_you.items[index % 2], id: `job-${index}` })) } };
    expect(needsYouLine(many)).toBe('10 waiting for approval, 10 with no route your rules allow, and 5 more');
    const lines = [board.lines[0], { ...board.lines[0], key: 'acct:p', label: 'Phaxio', in_use: 5, limit: 5, incoming: 0 }];
    expect(waitingLine({ ...board, lines })).toBe('2 lines are full. Faxes wait for a free line.');
    // No limit known: never called full.
    expect(waitingLine({ ...board, lines: [{ ...board.lines[0], limit: null }] }))
      .toBe("Waiting for a free line: 2 of 2 calls in use, your carrier's limit at once.");
    expect(todayLine({ ...board, today: { ...board.today, accepted: 0, delivered: 3, failed: 0 } })).toBe('None failed today.');
    expect(todayLine({ ...board, today: { ...board.today, accepted: 20, delivered: 20, failed: 0 } })).toBe('None failed, of 20 faxes today.');
    expect(todayLine({ ...board, today: { ...board.today, accepted: 20, delivered: 18, failed: 0 } }))
      .toBe('None failed, 2 still on their way, of 20 faxes today.');
    // One fax needing you is said in its own sentence.
    expect(needsYouLine({ ...board, needs_you: { count: 1, items: [board.needs_you.items[1]] } }))
      .toBe('No sending rule allows a route for this fax.');
    const one = (pages: number | null, page: number | null) => sendingLine({ ...board, sending: { count: 1,
      items: [{ ...board.sending.items[0], pages, page }] } });
    expect(one(4, 2)).toBe('Riverside Clinic, page 2 of 4');
    expect(one(1, null)).toBe('Riverside Clinic, 1 page');
    expect(one(null, null)).toBe('Riverside Clinic');
    expect(durationText(3725000)).toBe('1:02:05');
    expect(durationText(-500)).toBe('0:00');
  });
});

describe('the compact board on Sent', () => {
  it('shows the four tiles and the lines, and Waiting and Needs you filter the list below', async () => {
    answer(busyBoard());
    const asked: string[] = [];
    server.use(http.get('/admin/fax-jobs', ({ request }) => {
      asked.push(new URL(request.url).searchParams.get('status') ?? '');
      return HttpResponse.json({ total: 0, jobs: [] });
    }));
    const statusChange = vi.fn();
    render(<JobsList client={keyClient()} onStatusChange={statusChange} />);
    const board = await screen.findByTestId('outbound-compact');
    await waitFor(() => expect(board.getAttribute('data-state')).toBe('ok'));
    expect(['sending', 'waiting', 'needs_you', 'today'].map(count)).toEqual(['2', '41', '4', '231']);
    expect(within(board).getByTestId('outbound-line').getAttribute('data-full')).toBe('true');
    // Sending now and Today only inform; the compact board opens no lists.
    expect(tile('sending').tagName).not.toBe('BUTTON');
    expect(tile('today').tagName).not.toBe('BUTTON');
    expect(screen.queryByTestId('outbound-list-waiting')).toBeNull();

    fireEvent.click(tile('waiting'));
    await waitFor(() => expect(asked).toContain('ready'));
    fireEvent.click(tile('needs_you'));
    await waitFor(() => expect(asked).toContain('reconciliation_required'));
    expect(statusChange.mock.calls).toEqual([['ready'], ['reconciliation_required']]);
  });

  it("says the board couldn't be checked rather than showing zeros, and shows nothing to someone it is not for", async () => {
    server.use(http.get('/outbound/now', () => HttpResponse.json({ detail: 'Unexpected.' }, { status: 500 })));
    const { unmount } = render(<CompactOutboundBoard client={keyClient()} />);
    expect((await screen.findByTestId('outbound-no-reading')).textContent).toBe(BOARD_TEXT.failedEmpty);
    expect(screen.queryByTestId('tile-waiting')).toBeNull();
    unmount();
    server.use(http.get('/outbound/now', () => HttpResponse.json({ detail: 'This operation is not permitted.' }, { status: 403 })));
    render(<OutboundBoard client={keyClient()} />);
    await waitFor(() => expect(screen.queryByTestId('outbound-board')?.getAttribute('data-state') ?? 'gone').toBe('gone'));
  });
});

// -- the one poll ---------------------------------------------------------------------------------------------

// A poller on a hand-run clock: each test starts what is due and sees what was asked.
function harness(replies: Array<OutboundNowReply | Error>) {
  const timers = new Map<number, { run: () => void; ms: number }>();
  let next = 1;
  let visible = true;
  let clock = 0;
  const listeners = new Set<() => void>();
  const pending: Array<(value: OutboundNowReply) => void> = [];
  let calls = 0;
  const deps: PollerDeps = {
    fetch: () => {
      calls += 1;
      const reply = replies.shift();
      if (reply instanceof Error) return Promise.reject(reply);
      if (reply) return Promise.resolve(reply);
      return new Promise((resolve) => pending.push(resolve));
    },
    now: () => clock,
    setTimer: (run, ms) => { const id = next++; timers.set(id, { run, ms }); return id; },
    clearTimer: (id) => { timers.delete(id as number); },
    visible: () => visible,
    onVisibility: (listener) => { listeners.add(listener); return () => listeners.delete(listener); },
  };
  const poller = new OutboundPoller(deps);
  return {
    poller,
    get calls() { return calls; },
    timers: () => [...timers.values()].map((timer) => timer.ms),
    // Run every timer due now and let the requests they start settle.
    async fire() {
      const due = [...timers.entries()];
      timers.clear();
      for (const [, timer] of due) { clock += timer.ms; timer.run(); }
      await new Promise((resolve) => setTimeout(resolve, 0));
    },
    answerPending(board: OutboundNow) { pending.shift()?.({ kind: 'ready', board }); return new Promise((resolve) => setTimeout(resolve, 0)); },
    setVisible(value: boolean) { visible = value; listeners.forEach((listener) => listener()); },
  };
}

const ready = (): OutboundNowReply => ({ kind: 'ready', board: busyBoard() });

describe('the one outbound poll', () => {
  it('asks only after the last answer arrived, and a refresh during a poll asks once more after it', async () => {
    const run = harness([]);
    const stop = run.poller.subscribe(() => undefined);
    await run.fire();
    expect(run.calls).toBe(1);
    // A request is out: no timer waits, and refreshing does not start a second request.
    expect(run.timers()).toEqual([]);
    run.poller.refresh();
    run.poller.refresh();
    expect(run.calls).toBe(1);
    await run.answerPending(busyBoard());
    expect(run.poller.getView().state).toBe('ok');
    expect(run.timers()).toEqual([0]);
    await run.fire();
    expect(run.calls).toBe(2);
    await run.answerPending(busyBoard());
    expect(run.timers()).toEqual([5000]);
    stop();
    expect(run.timers()).toEqual([]);
  });

  it('asks every 5 seconds while faxes are sending or waiting, every 15 seconds otherwise', async () => {
    const quiet = { ...emptyOutboundNow(), as_of: earlier(1) } as OutboundNow;
    const sendingOnly = { ...quiet, sending: busyBoard().sending };
    const waitingOnly = { ...quiet, waiting: busyBoard().waiting };
    const unread = { ...quiet, not_checked: ['waiting'] };
    const run = harness([quiet, sendingOnly, quiet, waitingOnly, unread, quiet]
      .map((board) => ({ kind: 'ready', board }) as OutboundNowReply));
    run.poller.subscribe(() => undefined);
    const waits: number[] = [];
    for (let poll = 0; poll < 6; poll += 1) {
      await run.fire();
      waits.push(...run.timers());
    }
    // Nothing moving: 15 s. Sending, waiting, or a count the server could not read: 5 s.
    expect(waits).toEqual([15000, 5000, 15000, 5000, 5000, 15000]);
    // A decision on a held fax: at once, whatever the cadence.
    run.poller.refresh();
    expect(run.timers()).toEqual([0]);
  });

  it('pauses while the tab is hidden and looks again when it is shown', async () => {
    const run = harness([ready(), ready()]);
    run.poller.subscribe(() => undefined);
    await run.fire();
    expect(run.calls).toBe(1);
    expect(run.timers()).toEqual([5000]);
    run.setVisible(false);
    expect(run.timers()).toEqual([]);
    await run.fire();
    expect(run.calls).toBe(1);
    run.setVisible(true);
    expect(run.timers()).toHaveLength(1);
    await run.fire();
    expect(run.calls).toBe(2);
  });

  it('backs off when the server is busy, honouring Retry-After, and keeps the last reading', async () => {
    const run = harness([ready(), { kind: 'busy', retryAfterMs: 30000 }, { kind: 'busy', retryAfterMs: null },
      { kind: 'busy', retryAfterMs: null }, new TypeError('Failed to fetch'), ready()]);
    run.poller.subscribe(() => undefined);
    await run.fire();
    expect(run.timers()).toEqual([5000]);
    await run.fire();
    expect(run.poller.getView()).toMatchObject({ state: 'busy', board: { waiting: { count: 41 } } });
    expect(run.timers()).toEqual([30000]);
    await run.fire();
    expect(run.timers()).toEqual([20000]);
    // Refresh honours the back-off rather than asking a busy server again at once.
    run.poller.refresh();
    expect(run.timers()).toEqual([0 + 20000]);
    await run.fire();
    expect(run.timers()).toEqual([40000]);
    await run.fire();
    expect(run.poller.getView().state).toBe('failed');
    expect(run.timers()).toEqual([60000]);
    await run.fire();
    expect(run.poller.getView().state).toBe('ok');
    expect(run.timers()).toEqual([5000]);
  });

  it('stops for good when the board is not permitted', async () => {
    const run = harness([new AdminAPIError(403, 'Forbidden')]);
    run.poller.subscribe(() => undefined);
    await run.fire();
    expect(run.poller.getView().state).toBe('denied');
    expect(run.timers()).toEqual([]);
    run.poller.refresh();
    expect(run.timers()).toEqual([]);
  });

  it('runs one poll with Overview and Sent both open, never two requests at once', async () => {
    Object.assign(OUTBOUND_POLL, { activeMs: 10, quietMs: 10 });
    let asked = 0;
    let out = 0;
    let most = 0;
    server.use(http.get('/outbound/now', async () => {
      asked += 1;
      out += 1;
      most = Math.max(most, out);
      await delay(40);
      out -= 1;
      return HttpResponse.json(busyBoard());
    }));
    const client = keyClient();
    render(<><Dashboard client={client} /><JobsList client={client} /></>);
    await waitFor(() => {
      expect(screen.getByTestId('outbound-board').getAttribute('data-state')).toBe('ok');
      expect(screen.getByTestId('outbound-compact').getAttribute('data-state')).toBe('ok');
    });
    await waitFor(() => expect(asked).toBeGreaterThanOrEqual(4));
    expect(most).toBe(1);
  });

  it('shows a busy server as busy, waits as asked, and keeps the session', async () => {
    Object.assign(OUTBOUND_POLL, { activeMs: 10, quietMs: 10 });
    const signedOut = vi.fn();
    let asked = 0;
    let probed = 0;
    server.use(
      http.get('/outbound/now', () => {
        asked += 1;
        return asked === 1
          ? HttpResponse.json({ detail: 'Busy.' }, { status: 503, headers: { 'Retry-After': '1' } })
          : HttpResponse.json(busyBoard());
      }),
      http.get('/auth/me', () => { probed += 1; return HttpResponse.json({ detail: 'Not signed in.' }, { status: 401 }); }),
    );
    const started = Date.now();
    render(<CompactOutboundBoard client={keyClient(signedOut)} />);
    expect((await screen.findByTestId('outbound-no-reading')).textContent).toBe(BOARD_TEXT.busyEmpty);
    await waitFor(() => expect(screen.getByTestId('outbound-compact').getAttribute('data-state')).toBe('ok'));
    expect(Date.now() - started).toBeGreaterThanOrEqual(900);
    expect(signedOut).not.toHaveBeenCalled();
    expect(probed).toBe(0);
  });
});

// A reply the API itself produced (api/tests/test_outbound_now.py writes it and checks it keeps every field), so the
// console and the server can't drift apart.
import fromApi from './outboundNowFromApi.json';

describe('the outbound board with a reply from the API', () => {
  it('shows every section of a real reply with no missing words', async () => {
    const board = { ...(fromApi as unknown as OutboundNow), as_of: earlier(1) };
    await renderBoard(board);
    expect(count('sending')).toBe('2');
    expect(count('waiting')).toBe('4');
    expect(count('needs_you')).toBe('1');
    expect(line('needs_you')).toBe('No account is allowed for this fax.');
    expect(screen.getByTestId('outbound-engine').textContent).toContain('Faxbot sends audio fax until the network is fixed.');
    expect(within(screen.getByTestId('outbound-line')).getByTestId('line-in-use').textContent).toBe('2 of 2 in use');
    expect(screen.getByTestId('outbound-groups').textContent).toContain('olive');
    const expected: Record<string, string> = {
      sending: 'Telnyx', waiting: 'Waiting for a free line: all 2 lines are in use.',
      needs_you: 'No account is allowed for this fax.', today: 'Another problem (see Sent)',
    };
    for (const [key, words] of Object.entries(expected)) {
      fireEvent.click(tile(key));
      const list = await screen.findByTestId(`outbound-list-${key}`);
      expect(list.textContent).toContain(words);
      const text = screen.getByTestId('outbound-board').textContent ?? '';
      for (const broken of ['undefined', 'NaN', 'null', '[object Object]', 'Invalid Date']) expect(text).not.toContain(broken);
    }
  });
});
