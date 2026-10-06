// A fax the trunk received but could not hand to Faxbot is named in Recent
// calls and on the Dashboard, and can be brought in from the trunk screen.
import { describe, expect, it } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import AdminAPIClient from '../api/client';
import Dashboard from '../components/Dashboard';
import SipTrunkSettings from '../components/SipTrunkSettings';
import { server } from '../test/server';

const client = () => new AdminAPIClient({ kind: 'key', key: 'synthetic-console-key' });
const SENTENCE = 'A fax was received but could not be handed to Faxbot: the fax engine has no inbound secret yet; select Apply and connect.';

function call(recovered: boolean) {
  const started = new Date(Date.now() - 60 * 60 * 1000).toISOString();
  return {
    id: 'c1', direction: 'inbound', job_id: recovered ? 'f'.repeat(32) : null, attempt_id: null, trunk_preset: 'telnyx',
    did: '+15555550100', caller: '+13035550100', called: '+15555550100', started_at: started, answered_at: started,
    ended_at: started, disposition: 'answered', connected_seconds: 40, t38: 'no', pages: 2, fax_status: 'SUCCESS',
    remote_station_id: null, error_cause: 'not_handed_over: no_secret', fax_preference: false,
    verdict: recovered ? 'received' : 'not_handed_over', summary: recovered ? 'Received: 2 pages.' : SENTENCE,
  };
}

function trunkScreen(recover: () => Response) {
  let recovered = false;
  const posts: string[] = [];
  server.use(
    http.get('/admin/sip/presets', () => HttpResponse.json({ presets: [] })),
    http.get('/admin/settings', () => HttpResponse.json({ _meta: { desired_revision_id: 'rev-1' }, sip: { trunk: {} } })),
    http.get('/admin/sip/calls', () => HttpResponse.json({ items: [call(recovered)], next_cursor: null })),
    http.post('/admin/inbound/recover', ({ request }) => {
      posts.push(new URL(request.url).pathname);
      const response = recover();
      recovered = response.status === 200;
      return response;
    }),
  );
  render(<SipTrunkSettings client={client()} />);
  return posts;
}

describe('Faxes that were received but not handed over', () => {
  it('names the reason in Recent calls and brings the fax in on request', async () => {
    const posts = trunkScreen(() => HttpResponse.json({ found: 1, imported: 1, waiting: 0, message: 'Brought in 1 received fax.' }));
    expect(await screen.findAllByText(SENTENCE)).not.toHaveLength(0);
    fireEvent.click(screen.getByRole('button', { name: 'Bring in faxes that were received but not handed over' }));
    expect((await screen.findByTestId('inbound-recovery')).textContent).toBe('Brought in 1 received fax.');
    await waitFor(() => expect(screen.queryAllByText(SENTENCE)).toHaveLength(0));
    expect(screen.getAllByText('Received: 2 pages.').length).toBeGreaterThan(0);
    expect(posts).toEqual(['/admin/inbound/recover']);
  });

  it('says plainly when receiving over the trunk is off', async () => {
    trunkScreen(() => HttpResponse.json({ detail: 'Turn on receiving over the SIP trunk first.' }, { status: 409 }));
    fireEvent.click(await screen.findByRole('button', { name: 'Bring in faxes that were received but not handed over' }));
    expect((await screen.findByTestId('inbound-recovery')).textContent).toBe('Turn on receiving over the SIP trunk first.');
  });

  it('shows the reason on the Dashboard receiving card', async () => {
    server.use(http.get('/admin/sip/calls', () => HttpResponse.json({ items: [call(false)], next_cursor: null })));
    render(<Dashboard client={client()} />);
    expect((await screen.findByTestId('missed-inbound-call')).textContent).toBe(SENTENCE);
  });
});
