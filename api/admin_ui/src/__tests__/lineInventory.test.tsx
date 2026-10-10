// Numbers → Advice and moves → line inventory and carrier dates (N19). Synthetic data only.
import { describe, expect, it } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import AdminAPIClient from '../api/client';
import LineInventory, { type LineInventoryView } from '../components/delivery/LineInventory';
import { server } from '../test/server';

const client = () => new AdminAPIClient({ kind: 'key', key: 'synthetic-key' });
const WORKBOOK = 'https://clec.att.com/clec_documents/unrestr/clec/common/PrimeAccess_Model-Discontinued_Service_Areas.xlsx';
const EMPTY: LineInventoryView = {
  sentence: 'No line inventory yet. Import one to match your fax lines to carrier lists and contract dates.',
  lines: [], inventory: null, lists: [], keyed: "AT&T's workbook lists wire centers, not street addresses.",
  note: 'Faxbot only advises: it never orders, ports or cancels a line.',
  help: { inventory: 'One line per row; only the number is required.', list: 'For another carrier, a CSV.' },
  sources: { att_workbook: WORKBOOK },
};
const LISTED = 'AT&T\'s Discontinued TDM Service Areas workbook lists wire center ZZTSILAA, distribution area 1410ZA as discontinued from 21 August 2024.';
const FILLED: LineInventoryView = {
  ...EMPTY,
  sentence: '3 lines in your inventory, 1 with a date set by a carrier or a contract.',
  inventory: { file_name: 'lines.csv', lines: 3, imported_by: 'Ada', imported_at: '2026-10-10T12:00:00' },
  lists: [{ carrier: 'att', kind: 'discontinued', label: "AT&T's Discontinued TDM Service Areas workbook",
    source_url: WORKBOOK, file_date: '2026-08-17', areas: 100512, wire_centers: 3994, first: '2016-11-05', last: '2026-03-16' }],
  lines: [
    { number: '+13035550110', display: '+1 303-555-0110', carrier: 'AT&T', wire_center: 'ZZTSILAA', use_label: 'Fax',
      monthly: '$34.42', account: null, match: { state: 'listed', sentence: LISTED },
      dates: [{ kind: 'carrier_list', date: '2024-08-21', state: 'passed', sentence: LISTED,
        source: "AT&T's Discontinued TDM Service Areas workbook", source_url: WORKBOOK }] },
    { number: '+13035550111', display: '+1 303-555-0111', carrier: null, wire_center: null, use_label: 'Not known',
      monthly: null, account: null, dates: [],
      match: { state: 'no_wire_center', sentence: "Faxbot cannot check this line against AT&T's list without its wire center." } },
    { number: '+13035550113', display: '+1 303-555-0113', carrier: 'AT&T', wire_center: 'ZZTSILBB', use_label: 'Alarm',
      monthly: null, account: null, match: null, dates: [] },
  ],
};

describe('Line inventory and carrier dates', () => {
  it('says how matching works and where to download AT&T\'s workbook before anything is imported', async () => {
    server.use(http.get('/routing/line-inventory', () => HttpResponse.json(EMPTY)));
    render(<LineInventory client={client()} canWrite />);
    const section = await screen.findByTestId('line-inventory');
    expect(section.textContent).toContain('No line inventory yet.');
    expect(section.textContent).toContain('wire centers, not street addresses');
    expect(within(section).getByRole('link', { name: /Discontinued TDM Service Areas workbook/ }).getAttribute('href')).toBe(WORKBOOK);
    expect(screen.getByRole('button', { name: 'Import line inventory' })).toBeTruthy();
  });

  it('imports an inventory with its date order and shows dated lines and what it could not read', async () => {
    const sent: Array<Record<string, string>> = [];
    server.use(
      http.get('/routing/line-inventory', () => HttpResponse.json(EMPTY)),
      http.post('/routing/line-inventory/files', async ({ request }) => {
        const form = await request.formData();
        sent.push({ date_order: String(form.get('date_order')), file: (form.get('file') as File).name });
        return HttpResponse.json({ ...FILLED, imported: 3, skipped: ['Row 5: not a number is not a phone number Faxbot can read.'], skipped_count: 1 });
      }),
    );
    render(<LineInventory client={client()} canWrite />);
    fireEvent.click(await screen.findByRole('button', { name: 'Import line inventory' }));
    const dialog = await screen.findByRole('dialog');
    const file = new File(['number\n+13035550110\n'], 'lines.csv', { type: 'text/csv' });
    fireEvent.change(within(dialog).getByLabelText('Inventory file'), { target: { files: [file] } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Import' }));
    await waitFor(() => expect(sent).toEqual([{ date_order: 'mdy', file: 'lines.csv' }]));
    const section = await screen.findByTestId('line-inventory');
    await waitFor(() => expect(section.textContent).toContain('Imported 3 lines. 1 row was not read.'));
    expect(section.textContent).toContain('Row 5: not a number is not a phone number Faxbot can read.');
    expect(section.textContent).toContain(LISTED);
    expect(section.textContent).toContain('$34.42 a month');
    expect(section.textContent).toContain("without its wire center");
    // The alarm line has nothing to say; it is counted, not listed.
    expect(section.textContent).toContain('1 other line has no date and no match.');
    expect(section.textContent).toContain("AT&T's Discontinued TDM Service Areas workbook: 100,512 areas in 3,994 wire centers");
  });

  it('sends a named carrier\'s list with its kind and date', async () => {
    const sent: Array<Record<string, string>> = [];
    server.use(
      http.get('/routing/line-inventory', () => HttpResponse.json(FILLED)),
      http.post('/routing/carrier-lists/files', async ({ request }) => {
        const form = await request.formData();
        sent.push(Object.fromEntries([...form.entries()].filter(([key]) => key !== 'file').map(([key, value]) => [key, String(value)])));
        return HttpResponse.json({ ...FILLED, imported: 2, skipped: [], skipped_count: 0 });
      }),
    );
    render(<LineInventory client={client()} canWrite />);
    fireEvent.click(await screen.findByRole('button', { name: 'Import carrier list' }));
    const dialog = await screen.findByRole('dialog');
    fireEvent.change(within(dialog).getByLabelText('Carrier list file'),
      { target: { files: [new File(['x'], 'lumen.csv', { type: 'text/csv' })] } });
    fireEvent.change(within(dialog).getByLabelText('Carrier'), { target: { value: 'Lumen' } });
    fireEvent.change(within(dialog).getByLabelText("The list's own date"), { target: { value: '2026-09-01' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Import' }));
    await waitFor(() => expect(sent).toEqual([{ date_order: 'mdy', carrier: 'Lumen', kind: 'discontinued', file_date: '2026-09-01' }]));
  });

  it('gives readers the dates without import buttons, and an older server shows nothing', async () => {
    server.use(http.get('/routing/line-inventory', () => HttpResponse.json(FILLED)));
    const { unmount } = render(<LineInventory client={client()} canWrite={false} />);
    expect((await screen.findByTestId('line-inventory')).textContent).toContain(LISTED);
    expect(screen.queryByRole('button', { name: 'Import line inventory' })).toBeNull();
    unmount();
    server.use(http.get('/routing/line-inventory', () => HttpResponse.json({ detail: 'old' })));
    render(<LineInventory client={client()} canWrite />);
    await waitFor(() => expect(screen.queryByTestId('line-inventory')).toBeNull());
  });
});
