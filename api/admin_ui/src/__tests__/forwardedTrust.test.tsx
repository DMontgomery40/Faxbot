import { describe, expect, it } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import AdminAPIClient from '../api/client';
import { ForwardedTrustPanel, ReceivingOptionsFields } from '../components/ProviderRulesReceiving';
import type { ReceivingOptions } from '../components/ProviderRulesApi';
import { server } from '../test/server';

// Certificate authorities you trust for forwarded calls, on Numbers next to "Only calls forwarded from".
const NOTE = 'A forwarded call is verified only when the carrier signed the forwarding with a certificate from a '
  + 'certificate authority you trust.';
const anchor = {
  fingerprint: 'ab'.repeat(32), short: 'abababababababab', name: 'Synthetic STI-CA Root',
  valid_until: '2027-12-31T00:00:00Z', source: 'pasted', added_on: '2026-10-08',
};

describe('forwarded calls you can verify', () => {
  it('lists, adds pasted certificates and removes one; readers see the list without controls', async () => {
    const sent: unknown[] = [];
    server.use(
      http.get('*/admin/forwarded-trust', () => HttpResponse.json({
        anchors: [], note: NOTE,
        sentence: 'You trust no certificate authority for forwarded calls yet, so no forwarding is verified.' })),
      http.post('*/admin/forwarded-trust', async ({ request }) => {
        sent.push(await request.json());
        return HttpResponse.json({ anchors: [anchor], note: NOTE,
          sentence: 'You trust 1 certificate authority for forwarded calls.' });
      }),
      http.delete('*/admin/forwarded-trust/:fingerprint', () => HttpResponse.json({ anchors: [], note: NOTE,
        sentence: 'You trust no certificate authority for forwarded calls yet, so no forwarding is verified.' })),
    );
    const client = new AdminAPIClient({ kind: 'key', key: 'synthetic-key' });
    const told: number[] = [];
    const { unmount } = render(<ForwardedTrustPanel client={client} canWrite
      onChange={(trust) => told.push(trust.anchors.length)} />);
    expect(await screen.findByText(/You trust no certificate authority for forwarded calls yet/)).toBeTruthy();
    expect(screen.getByText(NOTE)).toBeTruthy();
    fireEvent.change(screen.getByLabelText('Certificates to trust (PEM)'),
      { target: { value: '-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----' } });
    fireEvent.click(screen.getByRole('button', { name: 'Trust these' }));
    expect(await screen.findByText('You trust 1 certificate authority for forwarded calls.')).toBeTruthy();
    expect(sent).toEqual([{ pem: '-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----' }]);
    expect(screen.getByText(/Synthetic STI-CA Root, valid until/)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Remove' }));
    expect(await screen.findByText(/You trust no certificate authority/)).toBeTruthy();
    // The rule fields' help follows each read and change.
    expect(told).toEqual([0, 1, 0]);
    unmount();
    render(<ForwardedTrustPanel client={client} canWrite={false} />);
    expect(await screen.findByText(/You trust no certificate authority/)).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Trust these' })).toBeNull();
  });

  it('says on "Only calls forwarded from" whether a forwarding can be verified', () => {
    const options = { diverted_from: '+13035550100', diversion_unsigned: false, from_numbers: [], days: [],
      start_minute: null, end_minute: null } as unknown as ReceivingOptions;
    const fields = (trustsAnchors: boolean) => (
      <ReceivingOptionsFields value={options} onChange={() => undefined} accounts={[]} connectors={[]}
        timeZone="America/Denver" trustsAnchors={trustsAnchors} />
    );
    const { rerender } = render(fields(false));
    expect(screen.getByText(/You trust no certificate authority for forwarded calls yet, so a forwarding counts here only if you tick the box below\./)).toBeTruthy();
    rerender(fields(true));
    expect(screen.getByText(/A forwarding counts here when it is verified: signed with a certificate from a certificate authority you trust\. Tick the box below to also take ones that are not verified\./)).toBeTruthy();
    expect(screen.queryByText(/cannot yet check/)).toBeNull();
  });
});
