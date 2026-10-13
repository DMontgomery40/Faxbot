import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import CopiersPanel, { ReceivedCopier } from '../components/CopiersPanel';

// The trunk page: copiers as Faxbot's own extensions (copier_extensions.py), and the Received line.
const extension = { id: 'c'.repeat(32), name: 'Front desk Ricoh', extension: '601', username: 'copier-601',
  mailbox_id: 'm1', mailbox: 'Front desk', numbers: ['+13035550161'], receive: 'copier' as const,
  sentence: 'Front desk Ricoh sends from extension 601 as Front desk; faxes to +13035550161 go on to it.' };

function caller(extensions: unknown[]) {
  const sent: Array<{ method: string; path: string; body?: unknown }> = [];
  const call = vi.fn(async (request: { method: string; path: string; body?: unknown }) => {
    sent.push(request);
    if (request.path === '/admin/sip/copiers') return { copiers: [], extensions } as never;
    if (request.path.startsWith('/access/mailboxes')) return { items: [{ id: 'm1', label: 'Front desk' }] } as never;
    if (request.path === '/admin/sip/copiers/extensions') {
      return { ...extension, password: 'Synthetic-Once-Only', realm: 'faxbot',
        password_text: 'Enter this password on the copier now; Faxbot shows it only once and keeps only its digest.',
        apply_text: 'Press Apply on the trunk page so the fax engine loads the extension; the copier can register after that.' } as never;
    }
    if (request.path.endsWith('/remove')) return { detail: 'Front desk Ricoh is no longer an extension.' } as never;
    return {} as never;
  });
  return { call, sent };
}

describe('copiers as Faxbot extensions', () => {
  it('adds a copier, shows its password once, and takes it away', async () => {
    const { call, sent } = caller([]);
    const { rerender } = render(<CopiersPanel call={call} />);
    await waitFor(() => expect(screen.getByTestId('copiers-panel')).toBeTruthy());
    fireEvent.click(screen.getByText('Copiers that fax through Faxbot'));
    fireEvent.change(screen.getByLabelText('Copier name'), { target: { value: 'Front desk Ricoh' } });
    fireEvent.change(screen.getByTestId('copier-extension'), { target: { value: '601' } });
    fireEvent.change(screen.getByLabelText('Its fax numbers'), { target: { value: '+13035550161' } });
    fireEvent.click(screen.getByRole('button', { name: 'Add copier' }));
    expect(await screen.findByText('Password: Synthetic-Once-Only')).toBeTruthy();
    const add = sent.find((request) => request.method === 'POST')!;
    expect(add.body).toEqual({ name: 'Front desk Ricoh', extension: '601', mailbox_id: null, receive: 'copier',
      numbers: ['+13035550161'] });
    const listed = caller([extension]);
    rerender(<CopiersPanel call={listed.call} />);
    expect(await screen.findByText(extension.sentence)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Take it away' }));
    expect(await screen.findByText('Front desk Ricoh is no longer an extension.')).toBeTruthy();
  });

  it('says in Received whether a fax went on to the copier', async () => {
    const call = vi.fn(async () => ({ sentence: "Front desk Ricoh didn't answer, so this fax stays in Received." }) as never);
    render(<ReceivedCopier call={call} inboundId="in-1" />);
    expect(await screen.findByText("Front desk Ricoh didn't answer, so this fax stays in Received.")).toBeTruthy();
  });
});
