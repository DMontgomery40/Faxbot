import { describe, expect, it } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import AdminAPIClient from '../api/client';
import type { DigitalAccountsState, DigitalAddress, DigitalRecipient } from '../api/digitalTypes';
import RecipientDigitalPanel, { DIGITAL_HELP } from '../components/delivery/RecipientDigital';
import DigitalAccounts from '../components/DigitalAccounts';
import { DigitalFaxOutcome, DigitalReceived } from '../components/delivery/DigitalMessages';
import { server } from '../test/server';

const client = () => new AdminAPIClient({ kind: 'key', key: 'synthetic-key' });
const NUMBER = '+13035550142';

function address(state: DigitalAddress['state'], extra: Partial<DigitalAddress> = {}): DigitalAddress {
  return {
    id: 'a'.repeat(32), kind: 'direct', kind_label: 'Direct message', address: 'records@direct.hospital.example.net',
    organization: null, account_key: null, source: 'entered', npi: null, evidence: null, state,
    sentence: state === 'confirmed' ? 'Confirmed; faxes to this number may go this way.'
      : 'Suggested; Faxbot does not use it until you confirm it.',
    label: 'Direct message to records@direct.hospital.example.net', route_key: `dsm:${'a'.repeat(32)}`,
    created_at: '2026-10-08T15:00:00Z',
    history: [{ action: state, note: null, recorded_by_name: 'Anne Admin', recorded_at: '2026-10-08T15:00:00Z' }],
    ...extra,
  };
}

const EMPTY: DigitalRecipient = {
  number: NUMBER, addresses: [], accounts: [{ key: 'hisp', label: 'Synthetic HISP', kind: 'hisp' }],
  sentence: 'Faxes to this number go only by fax until you confirm a Direct address or FHIR endpoint.',
};

describe('Recipients → Details: Direct message and FHIR', () => {
  it('puts an address on file, confirms it and withdraws it, each as a new entry', async () => {
    const bodies: unknown[] = [];
    let current: DigitalRecipient = EMPTY;
    server.use(
      http.get('/digital/recipients/:number', () => HttpResponse.json(current)),
      http.post('/digital/recipients/:number', async ({ request }) => {
        bodies.push(await request.json());
        current = { ...EMPTY, addresses: [address('suggested')] };
        return HttpResponse.json(current);
      }),
      http.post('/digital/recipients/:number/addresses/:id', async ({ request }) => {
        const body = await request.json() as { action: string };
        bodies.push(body);
        current = { ...EMPTY, sentence: body.action === 'confirm'
          ? 'Faxes to this number may go as Direct message to records@direct.hospital.example.net when that is the better route.'
          : EMPTY.sentence,
        addresses: [address(body.action === 'confirm' ? 'confirmed' : 'withdrawn')] };
        return HttpResponse.json(current);
      }),
    );
    render(<RecipientDigitalPanel client={client()} number={NUMBER} canWrite />);
    const panel = await screen.findByTestId('recipient-digital');
    expect(within(panel).getByText(EMPTY.sentence)).toBeTruthy();
    expect(within(panel).getByText(DIGITAL_HELP)).toBeTruthy();
    fireEvent.click(within(panel).getByRole('button', { name: 'Add a Direct address or FHIR endpoint' }));
    fireEvent.change(within(panel).getByLabelText('Direct address'), {
      target: { value: 'Records@Direct.Hospital.Example.net' } });
    fireEvent.click(within(panel).getByRole('button', { name: 'Save without confirming' }));
    expect(await within(panel).findByText('Not confirmed')).toBeTruthy();
    fireEvent.click(within(panel).getByRole('button', { name: 'Confirm' }));
    expect(await within(panel).findByText(/may go as Direct message to records@direct/)).toBeTruthy();
    fireEvent.click(within(panel).getByRole('button', { name: 'Withdraw' }));
    await waitFor(() => expect(within(panel).getByText('Withdrawn')).toBeTruthy());
    expect(bodies).toEqual([
      { kind: 'direct', address: 'Records@Direct.Hospital.Example.net', account_key: null, organization: null,
        confirm: false },
      { action: 'confirm', note: null },
      { action: 'withdraw', note: null },
    ]);
  });

  it('files NPPES suggestions that say where they came from, and only reads for someone who may not change', async () => {
    const suggested = address('suggested', { source: 'nppes', npi: '1234567893',
      evidence: 'NPPES record NPI 1234567893 lists this fax number and this Direct address, read 8 October 2026.' });
    server.use(
      http.get('/digital/recipients/:number', () => HttpResponse.json(EMPTY)),
      http.post('/digital/recipients/:number/nppes', () => HttpResponse.json({ ...EMPTY, addresses: [suggested],
        nppes_sentence: 'NPPES lists these for the provider whose record has this fax number. Check each one with the '
          + 'recipient, then confirm it before Faxbot uses it.' })),
    );
    const { unmount } = render(<RecipientDigitalPanel client={client()} number={NUMBER} canWrite />);
    const panel = await screen.findByTestId('recipient-digital');
    fireEvent.change(within(panel).getByLabelText('NPI'), { target: { value: '1234567893' } });
    fireEvent.click(within(panel).getByRole('button', { name: 'Look up in NPPES' }));
    expect(await within(panel).findByText(suggested.evidence as string)).toBeTruthy();
    expect(within(panel).getByText(/NPPES lists these/)).toBeTruthy();
    unmount();
    render(<RecipientDigitalPanel client={client()} number={NUMBER} canWrite={false} />);
    const readOnly = await screen.findByTestId('recipient-digital');
    expect(within(readOnly).queryByRole('button')).toBeNull();
  });
});

const ACCOUNTS: DigitalAccountsState = {
  generation: 4,
  accounts: [{
    key: 'hisp', provider: 'hisp', label: 'Synthetic HISP', enabled: true,
    settings: { direct_address: 'faxes@direct.clinic.example.org', smtp_host: 'smtp.hisp.example' },
    secrets_set: ['password'], missing: [], health: { state: 'not_set_up',
      sentence: "Load a trust bundle before Faxbot can check recipients' certificates." },
    plan: '$16.58 a month. Source: https://hdirect.inpriva.com/, read 08 October 2026.', certificate: null,
    trust_bundle: null,
  }],
  kinds: [{ id: 'hisp', label: 'Direct messages (HISP)', fields: [
    { name: 'direct_address', label: 'Your Direct address', kind: 'text', secret: false, required: true, default: null,
      choices: [], help: null },
    { name: 'password', label: 'Password', kind: 'text', secret: true, required: true, default: null, choices: [],
      help: null },
  ] }, { id: 'fhir', label: 'FHIR client', fields: [
    { name: 'client_id', label: 'Client ID', kind: 'text', secret: false, required: true, default: null, choices: [],
      help: null },
  ] }],
  presets: [],
};

describe('Providers → In use: Direct messages and FHIR', () => {
  it('shows each account with its health and plan, never a secret, and loads a trust bundle', async () => {
    const bundles: unknown[] = [];
    server.use(
      http.get('/digital/accounts', () => HttpResponse.json(ACCOUNTS)),
      http.post('/digital/accounts/:key/trust-bundle', async ({ request }) => {
        bundles.push(await request.json());
        return HttpResponse.json({ ...ACCOUNTS, accounts: [{ ...ACCOUNTS.accounts[0],
          health: { state: 'ready', sentence: 'Ready to send.' },
          trust_bundle: { anchors: 12, loaded_at: '2026-10-08T15:00:00Z', source_url: null, loaded_by: 'Anne' } }] });
      }),
    );
    render(<DigitalAccounts client={client()} canWrite />);
    const section = await screen.findByTestId('digital-accounts');
    expect(within(section).getByText(ACCOUNTS.accounts[0].health.sentence)).toBeTruthy();
    expect(within(section).getByText(ACCOUNTS.accounts[0].plan as string)).toBeTruthy();
    expect(within(section).getByText('No trust bundle is loaded yet.')).toBeTruthy();
    fireEvent.click(within(section).getByRole('button', { name: 'Load trust bundle' }));
    fireEvent.change(within(section).getByLabelText('Trust bundle address, or the bundle itself'), {
      target: { value: 'https://bundles.example.org/community.p7b' } });
    fireEvent.click(within(section).getByRole('button', { name: 'Load' }));
    expect(await within(section).findByText('Ready to send.')).toBeTruthy();
    expect(bundles).toEqual([{ url: 'https://bundles.example.org/community.p7b' }]);
  });

  it('adds a FHIR client from its fields', async () => {
    const added: unknown[] = [];
    server.use(
      http.get('/digital/accounts', () => HttpResponse.json(ACCOUNTS)),
      http.post('/digital/accounts', async ({ request }) => {
        added.push(await request.json());
        return HttpResponse.json(ACCOUNTS);
      }),
    );
    render(<DigitalAccounts client={client()} canWrite />);
    const section = await screen.findByTestId('digital-accounts');
    fireEvent.click(within(section).getByRole('button', { name: 'Add a FHIR client' }));
    const dialog = await screen.findByRole('dialog');
    fireEvent.change(within(dialog).getByLabelText('Key'), { target: { value: 'fhir-hospital' } });
    fireEvent.change(within(dialog).getByLabelText('Client ID'), { target: { value: 'faxbot-county-clinic' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(added).toEqual([{ key: 'fhir-hospital', provider: 'fhir', label: null,
      settings: { client_id: 'faxbot-county-clinic' }, credentials: {}, expected_generation: 4 }]));
  });
});

describe('Sent and Received: messages without a call', () => {
  it("shows a sent fax's Direct message and what came back", async () => {
    server.use(http.get('/digital/faxes/:job', () => HttpResponse.json({ job_id: 'job-1', messages: [{
      id: 'm1', direction: 'out', kind: 'direct', account_key: 'hisp', job_id: 'job-1',
      counterpart: 'records@direct.hospital.example.net', state: 'dispatched',
      sentence: "Delivered: the recipient's system confirmed it.",
      label: 'Direct message to records@direct.hospital.example.net', pages: 2, created_at: '2026-10-08T15:00:00Z',
      updated_at: '2026-10-08T15:05:00Z', settled_at: '2026-10-08T15:05:00Z',
      events: [{ kind: 'submitted', at: '2026-10-08T15:00:00Z', details: {} },
        { kind: 'dispatched', at: '2026-10-08T15:05:00Z', details: {} }],
    }] })));
    render(<DigitalFaxOutcome client={client()} jobId="job-1" />);
    const outcome = await screen.findByTestId('digital-fax-outcome');
    expect(within(outcome).getByText('Delivered')).toBeTruthy();
    expect(within(outcome).getByText("Delivered: the recipient's system confirmed it.")).toBeTruthy();
    expect(within(outcome).getByText(/The recipient's system confirmed delivery/)).toBeTruthy();
  });

  it('lists received Direct messages and shows nothing to someone who may not read them', async () => {
    server.use(http.get('/digital/messages', () => HttpResponse.json({ messages: [{
      id: 'm2', direction: 'in', kind: 'direct', account_key: 'hisp', job_id: null,
      counterpart: 'records@direct.hospital.example.net', state: 'not_filed',
      sentence: 'It carried no PDF or TIFF document, so there was nothing to file.',
      label: 'Direct message from records@direct.hospital.example.net', pages: null,
      created_at: '2026-10-08T15:00:00Z', updated_at: '2026-10-08T15:00:00Z', settled_at: null,
    }] })));
    const { unmount } = render(<DigitalReceived client={client()} />);
    const section = await screen.findByTestId('digital-received');
    expect(within(section).getByText('Not filed')).toBeTruthy();
    expect(within(section).getByText(/nothing to file/)).toBeTruthy();
    unmount();
    server.use(http.get('/digital/messages', () => HttpResponse.json({ detail: 'Forbidden' }, { status: 403 })));
    const { container } = render(<DigitalReceived client={client()} />);
    await waitFor(() => expect(container.textContent).toBe(''));
  });
});
