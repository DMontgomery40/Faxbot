// Partners → Find partners, the Recommendations section and the Recipients list's "Runs Faxbot":
// suggestions, the lookup settings, introductions and publishing. Synthetic numbers, domains and fingerprints only.
import { describe, expect, it } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import AdminAPIClient from '../api/client';
import DeliveryRoutes from '../components/DeliveryRoutes';
import FindPartners, { DiscoveryRecommendations, INTRODUCE_TEXT, NO_SUGGESTIONS } from '../components/delivery/FindPartners';
import { RUNS_FAXBOT } from '../components/delivery/Destinations';
import { emptyDiscovery, server } from '../test/server';

const client = () => new AdminAPIClient({ kind: 'key', key: 'synthetic-key' });
const FINDING = 'This recipient runs Faxbot. Enroll as a direct partner to send to them without a phone call.';
const OWN_NETWORK = 'This Faxbot is on your own network and uses its own certificate, not one from a trusted authority. '
  + 'The challenge fax still confirms who it is before anything is sent.';
const FINGERPRINT = Array.from({ length: 32 }, (_, index) => (index * 7 % 256).toString(16).padStart(2, '0').toUpperCase()).join(':');

function discovery(change: (value: ReturnType<typeof emptyDiscovery>) => void = () => {}) {
  const value = emptyDiscovery();
  value.direct_delivery = true;
  value.texts.well_known = 'Faxbots that fax you can read your partner card and suggest enrolling you as a partner.';
  value.suggestions = [
    { id: 's1', number: '+15550100001', organization: 'Valley Hospital', source: 'call', endpoint: 'https://valley.example',
      directory: null, created_at: '2026-10-07T15:00:00Z', sentence: FINDING,
      source_text: 'Found from a fax call with this number.', certificate: null, network_text: null },
    { id: 's2', number: '+15550100003', organization: 'Lab Clinic', source: 'call', endpoint: 'https://lan.example',
      directory: null, created_at: '2026-10-07T15:05:00Z', sentence: FINDING,
      source_text: 'Found from a fax call with this number.', certificate: FINGERPRINT, network_text: OWN_NETWORK },
  ];
  value.partners = [
    { id: 'p1', organization: 'North Clinic', fax_number: '+15550100011', verified: true, may_introduce: true,
      certificate: null, certificate_text: null },
    { id: 'p2', organization: 'South Clinic', fax_number: '+15550100012', verified: true, may_introduce: false,
      certificate: null, certificate_text: null },
  ];
  value.publishable = { number: '+15550006666', receives: true,
    sentence: 'You can publish +15550006666, the number on your partner card.' };
  change(value);
  return value;
}

describe('Find partners', () => {
  it('shows each suggestion with how it was found, and a Faxbot on your network with its certificate', async () => {
    server.use(http.get('/direct/discovery', () => HttpResponse.json(discovery())));
    render(<FindPartners client={client()} canWrite />);
    const cards = await screen.findAllByTestId('discovery-suggestion');
    expect(cards).toHaveLength(2);
    expect(within(cards[0]).getByText('Valley Hospital')).toBeTruthy();
    expect(within(cards[0]).getByText(FINDING)).toBeTruthy();
    expect(within(cards[0]).getByText('Found from a fax call with this number.')).toBeTruthy();
    expect(within(cards[1]).getByText(OWN_NETWORK, { exact: false })).toBeTruthy();
    expect(within(cards[1]).getByText(`Certificate fingerprint: ${FINGERPRINT}`)).toBeTruthy();
    expect(screen.getByText(INTRODUCE_TEXT)).toBeTruthy();
    expect(screen.getByText('You can publish +15550006666, the number on your partner card.')).toBeTruthy();
  });

  it('enrolls a suggestion and says what comes next', async () => {
    let enrolled = '';
    server.use(
      http.get('/direct/discovery', () => HttpResponse.json(discovery(enrolled ? (value) => { value.suggestions = value.suggestions.slice(1); } : undefined))),
      http.post('/direct/discovery/suggestions/:id/enroll', ({ params }) => {
        enrolled = String(params.id);
        return HttpResponse.json({ id: 'p9', organization: 'Valley Hospital', state: 'pending',
          detail: 'Valley Hospital added. Send them a code by fax to confirm their number.' });
      }),
    );
    render(<FindPartners client={client()} canWrite />);
    const card = (await screen.findAllByTestId('discovery-suggestion'))[0];
    fireEvent.click(within(card).getByRole('button', { name: 'Enroll as partner' }));
    expect(await screen.findByText('Valley Hospital added. Send them a code by fax to confirm their number.')).toBeTruthy();
    expect(enrolled).toBe('s1');
    await waitFor(() => expect(screen.getAllByTestId('discovery-suggestion')).toHaveLength(1));
  });

  it('saves the lookup settings and trusted directories', async () => {
    const saved: unknown[] = [];
    server.use(
      http.get('/direct/discovery', () => HttpResponse.json(discovery())),
      http.put('/direct/discovery/settings', async ({ request }) => {
        const body = await request.json();
        saved.push(body);
        return HttpResponse.json({ ...discovery(), detail: 'Saved.' });
      }),
    );
    render(<FindPartners client={client()} canWrite />);
    const settings = await screen.findByTestId('discovery-settings');
    fireEvent.click(within(settings).getByRole('checkbox', { name: 'Look for Faxbot on calls' }));
    await screen.findByText('Saved.');
    fireEvent.change(within(settings).getByLabelText('Directories, one per line'),
      { target: { value: 'faxdirectory.example.org\nother.example.org' } });
    fireEvent.click(within(settings).getByRole('button', { name: 'Save directories' }));
    await waitFor(() => expect(saved).toEqual([{ from_calls: false },
      { directories: ['faxdirectory.example.org', 'other.example.org'] }]));
  });

  it('turns "may be introduced" on and introduces two partners', async () => {
    const calls: unknown[] = [];
    server.use(
      http.get('/direct/discovery', () => HttpResponse.json(discovery())),
      http.post('/direct/discovery/partners/:id/may-introduce', async ({ params, request }) => {
        calls.push([params.id, await request.json()]);
        return HttpResponse.json({ may_introduce: true, detail: 'South Clinic may be introduced to your other partners.' });
      }),
      http.post('/direct/discovery/introductions', async ({ request }) => {
        calls.push(await request.json());
        return HttpResponse.json({ detail: 'North Clinic and South Clinic were introduced. Each can now enroll the other '
          + "and confirm the other's number with a code by fax." });
      }),
    );
    render(<FindPartners client={client()} canWrite />);
    const part = await screen.findByTestId('discovery-introductions');
    fireEvent.click(within(part).getByRole('checkbox', { name: 'South Clinic may be introduced' }));
    expect(await screen.findByText('South Clinic may be introduced to your other partners.')).toBeTruthy();
    fireEvent.mouseDown(within(part).getByLabelText('Introduce'));
    fireEvent.click(await screen.findByRole('option', { name: 'North Clinic' }));
    fireEvent.mouseDown(within(part).getByLabelText('To'));
    fireEvent.click(await screen.findByRole('option', { name: 'South Clinic' }));
    fireEvent.click(within(part).getByRole('button', { name: 'Introduce' }));
    expect(await screen.findByText(/North Clinic and South Clinic were introduced/)).toBeTruthy();
    expect(calls).toEqual([['p2', { allowed: true }], { first: 'p1', second: 'p2' }]);
  });

  it('publishes the number and shows the record to add to the directory', async () => {
    const zone = '_faxbot.6.6.6.6.0.0.0.5.5.5.1.faxdirectory.example.org. 3600 IN TXT "v=faxbot1; n=+15550006666"';
    let published = false;
    server.use(
      http.get('/direct/discovery', () => HttpResponse.json(discovery((value) => {
        if (published) {
          value.publications = [{ id: 'd1', number: '+15550006666', directory: 'faxdirectory.example.org',
            name: '_faxbot.6.6.6.6.0.0.0.5.5.5.1.faxdirectory.example.org', value: 'v=faxbot1; n=+15550006666', zone,
            expires_at: '2027-10-07T00:00:00Z', expires_text: '7 October 2027', expired: false,
            sentence: 'Add this record to the DNS for faxdirectory.example.org. Senders who trust '
              + 'faxdirectory.example.org then find this Faxbot for +15550006666.' }];
        }
      }))),
      http.post('/direct/discovery/publications', async ({ request }) => {
        expect(await request.json()).toEqual({ number: '+15550006666', directory: 'faxdirectory.example.org' });
        published = true;
        return HttpResponse.json({ id: 'd1', detail: 'Add this record to the DNS for faxdirectory.example.org.' });
      }, ),
      http.post('/direct/discovery/publications/:id/check', () => HttpResponse.json({ state: 'missing',
        detail: 'The record is not in the DNS for faxdirectory.example.org yet.' })),
    );
    render(<FindPartners client={client()} canWrite />);
    const part = await screen.findByTestId('discovery-publish');
    fireEvent.change(within(part).getByLabelText('Directory you control'), { target: { value: 'faxdirectory.example.org' } });
    fireEvent.click(within(part).getByRole('button', { name: 'Publish +15550006666' }));
    expect(await screen.findByText(zone)).toBeTruthy();
    expect(screen.getByText('Valid until 7 October 2027.')).toBeTruthy();
    fireEvent.click(within(await screen.findByTestId('discovery-publication')).getByRole('button', { name: 'Check' }));
    expect(await screen.findByText('The record is not in the DNS for faxdirectory.example.org yet.')).toBeTruthy();
  });

  it('says to turn on direct delivery when it is off, and shows no suggestion yet', async () => {
    render(<FindPartners client={client()} canWrite={false} />);
    expect(await screen.findAllByText('Turn on "Use direct delivery" under Recipients → Partners → Direct delivery to '
      + 'answer lookups and to find partners.')).toHaveLength(1);
    expect(screen.getByText(NO_SUGGESTIONS)).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Save directories' })).toBeNull();
  });
});

describe('Recipients that run Faxbot elsewhere in the console', () => {
  it('lists them in Recommendations and counts them', async () => {
    server.use(http.get('/direct/discovery', () => HttpResponse.json(discovery())));
    const counts: Array<number | null> = [];
    render(<DiscoveryRecommendations client={client()} onCount={(count) => counts.push(count)} />);
    expect(await screen.findAllByTestId('advice-discovery-item')).toHaveLength(2);
    expect(screen.getAllByText(FINDING)).toHaveLength(2);
    expect(counts).toEqual([2]);
  });

  it('says so in the Recipients list for a number that is no partner yet', async () => {
    server.use(
      http.get('/direct/discovery', () => HttpResponse.json(discovery())),
      http.get('/routing/destinations', () => HttpResponse.json({ destinations: [{
        number: '+15550100001', display_name: null, notes: null, preferred_route: null, accepts_references: false,
        version: 0, routes: [], estimated_cost_30_days: [] }] })),
    );
    render(<DeliveryRoutes client={client()} canWrite section="numbers" />);
    expect(await screen.findByText(RUNS_FAXBOT)).toBeTruthy();
  });
});
