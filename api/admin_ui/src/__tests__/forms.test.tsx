// Faxes → Forms: registered forms, filling one in and sending it, sent forms, and the
// received-fax line for a form a partner delivered as values.
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import AdminAPIClient from '../api/client';
import Forms from '../components/forms/Forms';
import Received from '../components/Received';
import { formSentence, valueText } from '../components/forms/ReceivedFormLine';
import { visibleNavigation } from '../navigation';
import type { FormDelivery, FormVersionDetail, ReceivedForm, RegisteredForm } from '../api/formsTypes';
import { server } from '../test/server';

const client = () => new AdminAPIClient({ kind: 'key', key: 'synthetic-key' });

const version = { id: 'v1', number: 1, title: 'Referral', address: 'a'.repeat(64), source: 'svg_positions' as const,
  source_text: 'Imported from an SVG drawing with a field-position file.', pages: 1, fields: 4, created_at: '2026-10-07T15:00:00' };
const referral: RegisteredForm = { id: 'form-1', name: 'Referral', origin: 'local', versions: [version], latest: version };
const detail: FormVersionDetail = {
  ...version, form_id: 'form-1', form_name: 'Referral', page_sizes: [{ width: 1728, height: 2156 }], has_template: true,
  renderer: 'faxbot-forms-1', field_list: [
    { name: 'patient', label: 'Patient name', type: 'text', type_text: 'Text', page: 1, required: true },
    { name: 'born', label: 'Date of birth', type: 'date', type_text: 'Date', page: 1, required: false, format: 'MM/DD/YYYY' },
    { name: 'urgent', label: 'Urgent', type: 'checkbox', type_text: 'Checkbox', page: 1, required: false },
    { name: 'clinic', label: 'Clinic', type: 'choice', type_text: 'Choice', page: 1, required: false, options: ['North', 'South'] },
  ],
};
const delivery = (overrides: Partial<FormDelivery>): FormDelivery => ({
  id: 'd1', direction: 'outbound', route: 'direct', state: 'delivered',
  status: "Delivered: the partner's Faxbot drew identical pages and filed them.", detail: null, partner: 'Valley Hospital',
  fax_number: '+15550100001', pages: 1, form_version_id: 'v1', form: 'Referral', form_version: 1, fax_id: null, can_fax: false,
  created_at: '2026-10-07T15:05:00', updated_at: '2026-10-07T15:05:00', ...overrides,
});

beforeEach(() => {
  URL.createObjectURL = vi.fn(() => 'blob:form-page');
  URL.revokeObjectURL = vi.fn();
});

function formsBackend(sent: unknown[] = [], faxed: string[] = [], deliveries: FormDelivery[] = []) {
  server.use(
    http.get('/forms', () => HttpResponse.json({ forms: [referral], renderer: 'faxbot-forms-1' })),
    http.get('/forms/versions/v1', () => HttpResponse.json(detail)),
    http.get('/forms/versions/v1/pages/1', () => new HttpResponse(new Uint8Array([137, 80, 78, 71]), {
      headers: { 'Content-Type': 'image/png' } })),
    http.get('/forms/deliveries', () => HttpResponse.json({ deliveries })),
    http.get('/forms/deliveries/:id', ({ params }) => HttpResponse.json({
      ...deliveries.find((item) => item.id === params.id), values: { patient: 'Ann Example', urgent: true },
      fields: detail.field_list.map(({ name, label, type }) => ({ name, label, type })) })),
    http.post('/forms/send', async ({ request }) => {
      const body = await request.json();
      sent.push(body);
      return HttpResponse.json(delivery({}));
    }),
    http.post('/forms/deliveries/:id/fax', ({ params }) => {
      faxed.push(String(params.id));
      return HttpResponse.json({ ...delivery({ id: String(params.id), state: 'mismatch', fax_id: 'job-9' }),
        message: 'The pages are on their way as a fax.' });
    }),
    http.get('/direct/peers', () => HttpResponse.json({ peers: [{ id: 'peer-1', organization: 'Valley Hospital',
      fax_number: '+15550100001', endpoint: 'https://a.example', state: 'verified', status: 'Verified.', code_sent: false,
      code_expires_at: null, verified_at: '2026-10-01T00:00:00', expires_at: null, version: 2 }] })),
    http.get('/forms/partners/peer-1', () => HttpResponse.json({ reached: true, message: 'Valley Hospital holds 1 form version.',
      forms: [{ address: 'a'.repeat(64), title: 'Referral', version: 1, also_here: true }] })),
  );
}

describe('Faxes → Forms', () => {
  it('is a Faxes page for people who read settings, not for a fax operator', () => {
    const pages = (permissions: string[]) => visibleNavigation(new Set(permissions),
      { send: true, jobs: true, inbox: true }, { pluginsEnabled: false })
      .find((area) => area.id === 'faxes')?.pages.map((page) => page.id);
    expect(pages(['settings:read', 'fax:send'])).toContain('forms');
    expect(pages(['fax:send', 'fax:read', 'inbound:list'])).not.toContain('forms');
  });

  it('lists forms and shows a version\'s fields and its page as it is faxed', async () => {
    formsBackend();
    render(<Forms client={client()} canWrite canSend canReadPartners />);
    const table = await screen.findByRole('table', { name: 'Registered forms' });
    expect(within(table).getByText('Referral')).toBeTruthy();
    expect(within(table).getByText('Imported from an SVG drawing with a field-position file.')).toBeTruthy();
    fireEvent.click(within(table).getByRole('button', { name: 'View' }));
    const fields = await screen.findByRole('table', { name: 'Fields' });
    expect(within(fields).getByText('Patient name')).toBeTruthy();
    expect(within(fields).getByText('North, South')).toBeTruthy();
    expect(await screen.findByAltText('Page 1 of Referral, as it is faxed')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Import a form' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'New version' })).toBeTruthy();
  });

  it('fills in a form and sends it to a partner as values', async () => {
    const sent: any[] = [];
    formsBackend(sent);
    render(<Forms client={client()} canWrite={false} canSend canReadPartners />);
    fireEvent.click(await screen.findByRole('button', { name: 'Fill in and send' }));
    fireEvent.change(await screen.findByLabelText('Patient name (required)'), { target: { value: 'Ann Example' } });
    fireEvent.click(screen.getByLabelText('Urgent'));
    fireEvent.change(screen.getByLabelText('Fax number to send to'), { target: { value: '+1 555 010 0001' } });
    expect(await screen.findByText(/Valley Hospital runs Faxbot: Faxbot sends only the filled-in values/)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Which forms do they hold?' }));
    expect(await screen.findByText(/Valley Hospital holds 1 form version\. Referral v1\./)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));
    await waitFor(() => expect(sent).toHaveLength(1));
    expect(sent[0]).toEqual({ version_id: 'v1', to: '+15550100001', values: { patient: 'Ann Example', urgent: true }, route: 'auto' });
    expect(await screen.findByText("Delivered: the partner's Faxbot drew identical pages and filed them.", { selector: '.MuiAlert-message' }))
      .toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Import a form' })).toBeNull();
  });

  it('after a mismatch, a person sends the pages as a fax from Sent forms', async () => {
    const faxed: string[] = [];
    formsBackend([], faxed, [delivery({ id: 'd2', state: 'mismatch', can_fax: true,
      status: "The partner's pages did not match, so nothing was filed. Send the pages as a fax if they are needed." })]);
    render(<Forms client={client()} canWrite canSend canReadPartners={false} />);
    fireEvent.click(await screen.findByRole('tab', { name: 'Sent forms' }));
    const table = await screen.findByRole('table', { name: 'Sent forms' });
    expect(within(table).getByText(/did not match, so nothing was filed/)).toBeTruthy();
    fireEvent.click(within(table).getByRole('button', { name: 'Values' }));
    expect((await screen.findByRole('table', { name: 'Form values' })).textContent).toContain('Ann Example');
    fireEvent.click(screen.getByRole('button', { name: 'Close' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    fireEvent.click(within(table).getByRole('button', { name: 'Send the pages as a fax' }));
    expect(await screen.findByText(/It is sent once\./)).toBeTruthy();
    expect(faxed).toEqual([]);
    fireEvent.click(screen.getByRole('button', { name: 'Send as a fax' }));
    await waitFor(() => expect(faxed).toEqual(['d2']));
    expect(await screen.findByText('The pages are on their way as a fax.')).toBeTruthy();
  });
});

describe('Received: a form a partner delivered as values', () => {
  const received: ReceivedForm = {
    ...delivery({ direction: 'inbound', state: 'matched', status: 'Received: the pages matched and were filed with their values.',
      form_version: 3 }),
    message_id: 'b'.repeat(32), intake_item_id: 'i5', inbound_fax_id: null,
    values: { patient: 'Ann Example', born: '1980-02-29', urgent: true, signature: { width: 1, height: 1, bits: 'AA==' } },
    fields: [{ name: 'patient', label: 'Patient name', type: 'text' }, { name: 'born', label: 'Date of birth', type: 'date' },
      { name: 'urgent', label: 'Urgent', type: 'checkbox' }, { name: 'signature', label: 'Signature', type: 'signature' }],
  };

  it('says which form it was drawn from and shows the values', async () => {
    server.use(
      http.get('/inbound', () => HttpResponse.json([])),
      http.get('/admin/inbound/callbacks', () => HttpResponse.json({ callbacks: [] })),
      http.get('/intake/items', () => HttpResponse.json({ items: [{
        id: 'i5', source: 'direct', inbound_fax_id: null, received_at: '2026-10-07T15:05:00', pages: 1, from_number: '+15550100001',
        to_number: '+15550100002', state: 'delivered', status: 'Delivered.', needs_action: false, attempts: 1, next_attempt_at: null,
        delivered_at: '2026-10-07T15:05:09', connector: 'Front desk', delivered_to: ['frontdesk@clinic.example'] }],
      counts: { received: 0, sending: 0, delivered: 1, failed: 0 } })),
      http.get('/forms/received', () => HttpResponse.json({ received: [received] })),
    );
    render(<Received client={client()} inboundEnabled permissions={new Set(['mailboxes:read'])} onNavigate={() => undefined} />);
    expect(await screen.findByText('Rendered from Referral v3; values attached.')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Show the values' }));
    const values = await screen.findByRole('table', { name: 'Form values' });
    expect(within(values).getByText('Ann Example')).toBeTruthy();
    expect(within(values).getByText('Yes')).toBeTruthy();
    expect(within(values).getByText('Signature picture')).toBeTruthy();
    expect(within(values).getByText(new Date(1980, 1, 29).toLocaleDateString())).toBeTruthy();
  });

  it('words values for people', () => {
    expect(formSentence({ form: 'Referral', form_version: 3 })).toBe('Rendered from Referral v3; values attached.');
    expect(valueText(false)).toBe('No');
    expect(valueText('')).toBe('-');
    expect(valueText('1234.50', 'number')).toBe('1234.50');
  });
});
