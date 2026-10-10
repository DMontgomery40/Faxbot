// Numbers → when lines close, and Providers → trunk → country rules. Synthetic data only.
import { describe, expect, it } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import AdminAPIClient from '../api/client';
import { CountryRules, LineClosures, type Closures } from '../components/delivery/CountryLines';
import { server } from '../test/server';

const client = () => new AdminAPIClient({ kind: 'key', key: 'synthetic-key' });
const SOURCES = { orange: 'https://gallery.orange.com/element?id=410038', gouv: 'https://data.economie.gouv.fr/x',
  arcep: 'https://www.arcep.fr/x' };
const PARIS: Closures = {
  sites: [{ site: 'paris', name: 'Paris office', commune: '75056', state: 'soon',
    sentence: 'Copper in Paris closes on 31 January 2027. Before then, run a receipt test from another route, so you know faxes to this number still arrive once the line moves.' }],
  lines: [{ number: '+33142000000', account: 'Telnyx', site: 'paris', state: 'soon',
    sentences: ['Copper in Paris closes on 31 January 2027.'] }],
  files: [{ source: 'gouv', source_url: null, file_date: '2025-10-20', communes: 35305 }],
  sources: SOURCES,
};

describe('When lines close', () => {
  it('shows the file, each French site and line, and adds and removes a carrier notice', async () => {
    const sent: unknown[] = [];
    const notice = { number: '+14165550100', account: null, site: null, state: 'soon' as const,
      sentences: ['Bell says this line closes on 4 November 2026.'] };
    server.use(
      http.get('/routing/closures', () => HttpResponse.json(PARIS)),
      http.put('/routing/line-notices/:number', async ({ request }) => {
        sent.push(await request.json());
        return HttpResponse.json({ ...PARIS, lines: [...PARIS.lines, notice] });
      }),
      http.delete('/routing/line-notices/:number', () => HttpResponse.json(PARIS)),
    );
    render(<LineClosures client={client()} canWrite />);
    const section = await screen.findByTestId('line-closures');
    expect(section.textContent).toContain('Government copy: 35,305 communes');
    expect(section.textContent).toContain('Paris office: Copper in Paris closes on 31 January 2027.');
    fireEvent.click(screen.getByRole('button', { name: "Add a carrier's notice" }));
    const dialog = await screen.findByRole('dialog');
    fireEvent.change(within(dialog).getByLabelText("Line's number"), { target: { value: '+14165550100' } });
    fireEvent.change(within(dialog).getByLabelText('The line closes on'), { target: { value: '2026-11-04' } });
    fireEvent.change(within(dialog).getByLabelText('Carrier'), { target: { value: 'Bell' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(screen.getByTestId('line-closures').textContent).toContain('Bell says this line closes'));
    expect(sent).toEqual([{ closes_on: '2026-11-04', carrier: 'Bell', received_on: null, note: '' }]);
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    fireEvent.click(screen.getByRole('button', { name: 'Remove the notice for +14165550100' }));
    await waitFor(() => expect(screen.getByTestId('line-closures').textContent).not.toContain('Bell says'));
  });

  it('imports a closure file with its source and date', async () => {
    let form: FormData | null = null;
    server.use(
      http.get('/routing/closures', () => HttpResponse.json({ ...PARIS, files: [], sites: [], lines: [] })),
      http.post('/routing/closures/files', async ({ request }) => {
        form = await request.formData();
        return HttpResponse.json({ ...PARIS, imported: 3, skipped: 0 });
      }),
    );
    render(<LineClosures client={client()} canWrite />);
    expect((await screen.findByTestId('line-closures')).textContent).toContain('No closure file yet.');
    fireEvent.click(screen.getByRole('button', { name: 'Import closure dates' }));
    const dialog = await screen.findByRole('dialog');
    fireEvent.change(within(dialog).getByLabelText('Closure file'), {
      target: { files: [new File(['code_insee;nom_commune\n'], 'fermeture.csv', { type: 'text/csv' })] } });
    fireEvent.change(within(dialog).getByLabelText("The file's own date"), { target: { value: '2025-12-19' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Import' }));
    expect((await screen.findByText('Imported 3 communes.')).textContent).toBe('Imported 3 communes.');
    expect([form!.get('source'), form!.get('file_date')]).toEqual(['gouv', '2025-12-19']);
  });

  it('says when closures could not be loaded', async () => {
    server.use(http.get('/routing/closures', () => HttpResponse.json({ detail: 'x' }, { status: 500 })));
    render(<LineClosures client={client()} canWrite />);
    expect((await screen.findByTestId('closures-unread')).textContent).toBe('Line closures could not be loaded. Try again.');
  });
});

describe('Country rules', () => {
  it('shows the rules with sources for accounts there and confirms one with evidence', async () => {
    const rules = { accounts: [{ account: 'sip', label: 'Telnyx', country: 'AE', confirmed: false,
      sentence: "Telnyx: not confirmed. Faxes still go; confirm that this account's provider is a TDRA licensee." }],
    countries: [{ country: 'AE', name: 'the United Arab Emirates', regulator: 'TDRA', sentence: 'In the UAE, calls over the internet are licensed.',
      sources: [{ label: 'TDRA: frequently asked questions', url: 'https://tdra.gov.ae/en/FAQs' }] },
    { country: 'SA', name: 'Saudi Arabia', regulator: 'CST', sentence: 'In Saudi Arabia...', sources: [] }] };
    const posted: unknown[] = [];
    server.use(
      http.get('/routing/country-rules', () => HttpResponse.json(rules)),
      http.post('/routing/country-rules/confirm', async ({ request }) => {
        posted.push(await request.json());
        return HttpResponse.json({ ...rules, accounts: [{ ...rules.accounts[0], confirmed: true,
          sentence: 'Telnyx: confirmed by Anne.' }] });
      }),
    );
    render(<CountryRules client={client()} canWrite />);
    const section = await screen.findByTestId('country-rules');
    expect(section.textContent).toContain('In the UAE, calls over the internet are licensed.');
    expect(section.textContent).not.toContain('In Saudi Arabia');
    fireEvent.click(screen.getByRole('button', { name: 'Confirm Telnyx' }));
    const dialog = await screen.findByRole('dialog');
    fireEvent.change(within(dialog).getByLabelText(/How you know/), { target: { value: 'Contract with du' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Confirm' }));
    await waitFor(() => expect(screen.getByTestId('country-rules').textContent).toContain('Telnyx: confirmed by Anne.'));
    expect(posted).toEqual([{ account: 'sip', country: 'AE', evidence: 'Contract with du', evidence_url: null }]);
  });

  it('shows nothing when no account is in such a country', async () => {
    const shown = render(<CountryRules client={client()} canWrite />);
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(shown.container.textContent).toBe('');
  });
});
