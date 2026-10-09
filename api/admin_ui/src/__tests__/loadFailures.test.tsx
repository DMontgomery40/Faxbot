// Reads that used to fail in silence: each one now says a real failure (a server error or a lost connection) in one
// short sentence where its data would be, stays quiet for someone without permission (403), and stays quiet when
// the answer is simply "nothing here".
import { describe, expect, it } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import AdminAPIClient from '../api/client';
import DeliveryRoutes from '../components/DeliveryRoutes';
import { CostsUnread, FaxCostItem, useFaxCosts, useInboundCosts } from '../components/delivery/FaxCost';
import { ReceivedEncodedPages } from '../components/delivery/EncodedPages';
import OriginRateRows from '../components/delivery/OriginRateRows';
import { ReceivedNotice } from '../components/delivery/PartnerActivity';
import { FaxTogetherItem } from '../components/delivery/SendingTogether';
import { server } from '../test/server';

const client = () => new AdminAPIClient({ kind: 'key', key: 'synthetic-key' });
const JOB = 'a'.repeat(32);
const settle = () => new Promise((resolve) => setTimeout(resolve, 50));

function answer(path: string, status: number, body: unknown = { detail: 'Synthetic failure' }) {
  server.use(http.get(path, () => (status === 0 ? HttpResponse.error() : HttpResponse.json(body as object, { status }))));
}

function SentCosts() {
  return <CostsUnread costs={useFaxCosts(client(), [{ id: JOB }])} />;
}

function ReceivedCosts() {
  return <CostsUnread costs={useInboundCosts(client(), [{ id: JOB }])} />;
}

describe('Sent and Received details say a read that failed', () => {
  it("says when a fax's sending-together details could not be loaded, and not without permission", async () => {
    const together = { state: 'together' as const, reference: 'BATCH-1', documents: 2, document_number: 1, others: 1 };
    answer('/batching/faxes/:id', 500);
    const shown = render(<ul><FaxTogetherItem client={client()} jobId={JOB} together={together} /></ul>);
    expect((await screen.findByTestId('together-unread')).textContent)
      .toBe('The details of sending together could not be loaded. Try again.');
    expect(screen.getByText('Sent in one call with 1 other fax.')).toBeTruthy(); // the summary still shows
    shown.unmount();
    answer('/batching/faxes/:id', 403);
    render(<ul><FaxTogetherItem client={client()} jobId={JOB} together={together} /></ul>);
    await settle();
    expect(screen.queryByTestId('together-unread')).toBeNull();
  });

  it("says when a fax's cost could not be loaded, and nothing for a fax with no cost", async () => {
    answer('/routing/faxes/:id/cost', 0);
    const failed = render(<ul><FaxCostItem client={client()} jobId={JOB} /></ul>);
    expect((await screen.findByTestId('cost-unread')).textContent).toBe('The cost could not be loaded. Try again.');
    failed.unmount();
    answer('/routing/faxes/:id/cost', 200, { summary: null });
    const none = render(<ul><FaxCostItem client={client()} jobId={JOB} /></ul>);
    await settle();
    expect(none.container.textContent).toBe('');
    none.unmount();
    answer('/routing/faxes/:id/cost', 403);
    const denied = render(<ul><FaxCostItem client={client()} jobId={JOB} /></ul>);
    await settle();
    expect(denied.container.textContent).toBe('');
  });

  it('says once above Sent and Received when their costs could not be loaded', async () => {
    answer('/routing/fax-costs', 500);
    answer('/routing/inbound-costs', 500);
    render(<><SentCosts /><ReceivedCosts /></>);
    await waitFor(() => expect(screen.getAllByTestId('costs-unread').map((item) => item.textContent))
      .toEqual(['Costs could not be loaded. Try again.', 'Costs could not be loaded. Try again.']));
  });

  it('keeps the cost lists quiet when they load or without permission', async () => {
    answer('/routing/fax-costs', 200, { costs: {} });
    answer('/routing/inbound-costs', 403);
    const { container } = render(<><SentCosts /><ReceivedCosts /></>);
    await settle();
    expect(container.textContent).toBe('');
  });

  it('says when it could not check whether a received fax is a notice or holds encoded pages', async () => {
    answer('/direct/notices', 500);
    answer('/codec/received/:id', 500);
    const failed = render(<><ReceivedNotice client={client()} faxId="in-1" />
      <ReceivedEncodedPages client={client()} faxId="in-1" canDownload /></>);
    expect((await screen.findByTestId('received-notice-unread')).textContent)
      .toBe('Whether this fax is a notice from a partner could not be checked. Try again.');
    expect((await screen.findByTestId('received-encoded-pages-unread')).textContent)
      .toBe('Whether this fax holds encoded pages could not be checked. Try again.');
    failed.unmount();
    // A fax that is no notice and holds no encoded pages: nothing to say.
    answer('/direct/notices', 200, { notices: [], notice_text: null });
    answer('/codec/received/:id', 200, { state: 'none', sentence: null });
    const none = render(<><ReceivedNotice client={client()} faxId="in-1" />
      <ReceivedEncodedPages client={client()} faxId="in-1" canDownload /></>);
    await settle();
    expect(none.container.textContent).toBe('');
    none.unmount();
    answer('/direct/notices', 403);
    answer('/codec/received/:id', 403);
    const denied = render(<><ReceivedNotice client={client()} faxId="in-1" />
      <ReceivedEncodedPages client={client()} faxId="in-1" canDownload /></>);
    await settle();
    expect(denied.container.textContent).toBe('');
  });
});

describe('Costs and Delivery routes say a read that failed', () => {
  it('says when your sites could not be loaded for a price by where calls start', async () => {
    answer('/admin/providers/accounts', 500);
    render(<OriginRateRows client={client()} route="sip" label="Telnyx" existing={[]} onSaved={() => undefined} />);
    fireEvent.click(screen.getByRole('button', { name: 'Add a price by where calls start' }));
    expect((await screen.findByTestId('origin-sites-unread')).textContent)
      .toBe('Your sites could not be loaded, so only Anywhere and A country are offered. Try again.');
  });

  it('keeps the sites quiet without permission', async () => {
    answer('/admin/providers/accounts', 403);
    render(<OriginRateRows client={client()} route="sip" label="Telnyx" existing={[]} onSaved={() => undefined} />);
    fireEvent.click(screen.getByRole('button', { name: 'Add a price by where calls start' }));
    await settle();
    expect(screen.queryByTestId('origin-sites-unread')).toBeNull();
  });

  it('says when your direct partners could not be loaded, instead of showing none', async () => {
    answer('/direct/peers', 500);
    render(<DeliveryRoutes client={client()} canWrite section="partners" />);
    expect((await screen.findByTestId('partners-unread')).textContent)
      .toBe('Your direct partners could not be loaded. Try again.');
  });

  it('says on Numbers when partners or suggestions could not be loaded, and not without permission', async () => {
    answer('/routing/destinations', 200, { window_days: 30, destinations: [] });
    answer('/direct/peers', 500);
    answer('/direct/discovery', 0);
    const failed = render(<DeliveryRoutes client={client()} canWrite section="numbers" />);
    expect((await screen.findByTestId('numbers-partners-unread')).textContent)
      .toBe('Your direct partners could not be loaded, so numbers are shown without them. Try again.');
    expect(screen.getByTestId('numbers-suggestions-unread').textContent)
      .toBe('Numbers whose recipient runs Faxbot could not be loaded. Try again.');
    failed.unmount();
    answer('/direct/peers', 403);
    answer('/direct/discovery', 403);
    render(<DeliveryRoutes client={client()} canWrite section="numbers" />);
    await settle();
    await waitFor(() => expect(screen.queryByText('Loading…')).toBeNull());
    expect(screen.queryByTestId('numbers-partners-unread')).toBeNull();
    expect(screen.queryByTestId('numbers-suggestions-unread')).toBeNull();
  });
});
