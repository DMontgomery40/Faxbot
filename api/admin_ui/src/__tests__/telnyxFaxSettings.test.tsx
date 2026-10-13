import { describe, expect, it } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import AdminAPIClient from '../api/client';
import Settings from '../components/Settings';
import { TELNYX_FAX_NO_PERMISSION, TELNYX_FAX_PRICE_SENTENCE } from '../components/TelnyxFaxSettings';
import { server } from '../test/server';
import { settingsFixture } from '../test/settingsFixture';

type Json = Record<string, any>;

const client = () => new AdminAPIClient({ kind: 'key', key: 'synthetic-key' });
const ENV_NAME = /[A-Z]{3,}_[A-Z_]{2,}/;
const CREATED = 'Faxbot created the Fax Application 1293384261075731499 in your Telnyx account and saved its ID.';

function telnyx(fields: Json = {}): Json {
  return settingsFixture((data) => {
    data.telnyx_fax = { api_key: '***', connection_id: '', from_number: '+13035550100', public_key: '',
      results_url: 'https://fax.example/telnyx-fax-callback', configured: false, ...fields };
  });
}

function backend(data: Json, create: (body: Json) => Response | Promise<Response>) {
  const posts: Json[] = [];
  let reads = 0;
  server.use(
    http.get('/admin/settings', () => {
      reads += 1;
      return HttpResponse.json(data);
    }),
    http.get('/admin/providers/accounts', () => HttpResponse.json({ generation: 7, accounts: [], providers: [] })),
    http.post('/admin/providers/accounts/telnyx_fax/telnyx-fax-application', async ({ request }) => {
      const body = await request.json() as Json;
      posts.push(body);
      return create(body);
    }),
  );
  return { posts, reads: () => reads };
}

const page = (canWrite = true) => render(<Settings client={client()} canWrite={canWrite} sections={['telnyx_fax']}
  title="Telnyx Fax API" />);

describe('The Telnyx Fax API page', () => {
  it('shows the saved account, the price and the address for fax results in plain words', async () => {
    backend(telnyx({ connection_id: '1293384261075731499', public_key: 'c2lnbmluZy1rZXk=', configured: true }),
      () => HttpResponse.json({}));
    page();
    expect(await screen.findByTestId('telnyx-fax-price')).toHaveProperty('textContent', TELNYX_FAX_PRICE_SENTENCE);
    expect(screen.getByText('Fax Application ID')).toBeTruthy();
    expect(screen.getByDisplayValue('1293384261075731499')).toBeTruthy();
    expect(screen.getByDisplayValue('+13035550100')).toBeTruthy();
    const results = screen.getByTestId('telnyx-fax-results-url');
    expect(within(results).getByText('https://fax.example/telnyx-fax-callback')).toBeTruthy();
    // With a Fax Application already saved there is nothing to create.
    expect(screen.queryByRole('button', { name: 'Create a Fax Application' })).toBeNull();
    expect(screen.getByTestId('provider-use').textContent).toBe(
      'Faxbot does not use Telnyx Fax API now. To use it, choose Add or change a provider.');
    expect(document.body.textContent).not.toMatch(ENV_NAME);
  });

  it('creates a Fax Application with the account list it read, then shows what Faxbot did', async () => {
    const data = telnyx();
    const seen = backend(data, () => {
      data.telnyx_fax.connection_id = '1293384261075731499';
      return HttpResponse.json({ generation: 8, accounts: [], created: { key: 'telnyx_fax',
        connection_id: '1293384261075731499', sentence: CREATED } });
    });
    page();
    const create = await screen.findByRole('button', { name: 'Create a Fax Application' });
    await waitFor(() => expect(create).toHaveProperty('disabled', false));
    fireEvent.click(create);
    expect(await screen.findByText(CREATED)).toBeTruthy();
    expect(seen.posts).toEqual([{ expected_generation: 7 }]);
    // The settings are read again, so the new ID shows and the button goes away.
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Create a Fax Application' })).toBeNull());
    expect(seen.reads()).toBeGreaterThan(1);
  });

  it("shows Telnyx's refusal in Faxbot's own sentence and keeps the button", async () => {
    const refusal = 'Telnyx did not accept the API key. Check it on the Telnyx Fax API provider page.';
    backend(telnyx(), () => HttpResponse.json({ detail: refusal }, { status: 502 }));
    page();
    const create = await screen.findByRole('button', { name: 'Create a Fax Application' });
    await waitFor(() => expect(create).toHaveProperty('disabled', false));
    fireEvent.click(create);
    expect(await screen.findByText(refusal)).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Create a Fax Application' })).toHaveProperty('disabled', false);
  });

  it('says when the person may not change provider settings', async () => {
    const seen = backend(telnyx(), () => HttpResponse.json({}, { status: 403 }));
    page(false);
    const create = await screen.findByRole('button', { name: 'Create a Fax Application' });
    expect(create).toHaveProperty('disabled', true);
    expect(screen.getByText(TELNYX_FAX_NO_PERMISSION)).toBeTruthy();
    expect(seen.posts).toEqual([]);
  });

  it('asks for the API key before it can create a Fax Application', async () => {
    backend(telnyx({ api_key: '' }), () => HttpResponse.json({}));
    page();
    const create = await screen.findByRole('button', { name: 'Create a Fax Application' });
    await screen.findByText('Save the Telnyx API key first; Faxbot then can create the Fax Application.');
    expect(create).toHaveProperty('disabled', true);
  });
});
