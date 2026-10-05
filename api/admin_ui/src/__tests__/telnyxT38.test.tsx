import { describe, expect, it } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import AdminAPIClient from '../api/client';
import TelnyxT38 from '../components/TelnyxT38';
import { entryAction } from '../components/AuditLog';
import { server } from '../test/server';

const OFF = 'Telnyx has fax over IP (T.38) turned off for +1 555-555-0100, so received faxes there arrive as audio.';
const REPORT = {
  applies: true, checked_at: '2026-10-05T03:50:00Z', ready: false, text: OFF, connection_texts: [],
  numbers: [
    { number: '+15555550100', display: '+1 555-555-0100', state: 'off', text: OFF, fixable: true },
    { number: '+15555550101', display: '+1 555-555-0101', state: 'on',
      text: 'Telnyx has fax over IP (T.38) turned on for +1 555-555-0101.', fixable: false },
  ],
};

function client() {
  return new AdminAPIClient({ kind: 'key', key: 'synthetic-console-key' });
}

describe('Fax over IP (T.38) at Telnyx', () => {
  it('names the number that is off and turns it on for that number only', async () => {
    const asked: string[] = [];
    server.use(
      http.get('/admin/sip/telnyx', () => HttpResponse.json(REPORT)),
      http.post('/admin/sip/telnyx/numbers/:number/t38', ({ params }) => {
        asked.push(String(params.number));
        return HttpResponse.json({ ...REPORT, ready: true, text: 'Telnyx has fax over IP (T.38) turned on for all your trunk numbers.',
          numbers: REPORT.numbers.map((entry) => ({ ...entry, state: 'on', fixable: false,
            text: `Telnyx has fax over IP (T.38) turned on for ${entry.display}.` })),
          outcome: 'on', message: 'Telnyx now has fax over IP (T.38) turned on for +1 555-555-0100.' });
      }),
    );
    render(<TelnyxT38 client={client()} />);
    const section = await screen.findByTestId('telnyx-t38');
    expect(within(section).getByText('Fax over IP (T.38) at Telnyx')).toBeTruthy();
    expect(within(section).getByText(OFF)).toBeTruthy();
    // Numbers already on are not repeated while another needs attention.
    expect(within(section).queryByText('Telnyx has fax over IP (T.38) turned on for +1 555-555-0101.')).toBeNull();
    fireEvent.click(within(section).getByRole('button', { name: 'Turn on T.38 for +1 555-555-0100' }));
    expect(await within(section).findByText('Telnyx now has fax over IP (T.38) turned on for +1 555-555-0100.')).toBeTruthy();
    expect(within(section).getByText('Telnyx has fax over IP (T.38) turned on for all your trunk numbers.')).toBeTruthy();
    expect(within(section).queryByRole('button', { name: /Turn on T\.38/ })).toBeNull();
    expect(asked).toEqual(['+15555550100']);
  });

  it('says plainly when Telnyx refuses, and shows nothing when it does not apply', async () => {
    const refused = 'Telnyx did not let Faxbot change +1 555-555-0100, because the API key may not change numbers. In the '
      + 'Telnyx portal, open Numbers → My Numbers, select the gear next to +1 555-555-0100, open Expert Configuration '
      + 'and tick Enable T.38 Fax Gateway.';
    server.use(
      http.get('/admin/sip/telnyx', () => HttpResponse.json(REPORT)),
      http.post('/admin/sip/telnyx/numbers/:number/t38', () => HttpResponse.json({ ...REPORT, outcome: 'refused',
        message: refused })),
    );
    render(<TelnyxT38 client={client()} />);
    const section = await screen.findByTestId('telnyx-t38');
    fireEvent.click(within(section).getByRole('button', { name: 'Turn on T.38 for +1 555-555-0100' }));
    expect(await within(section).findByText(refused)).toBeTruthy();
    server.use(http.get('/admin/sip/telnyx', () => HttpResponse.json({ applies: false, numbers: [], connection_texts: [],
      text: null })));
    const { container } = render(<TelnyxT38 client={client()} />);
    await waitFor(() => expect(container.textContent).toBe(''));
  });
});

describe('Audit log entries for Telnyx T.38 changes', () => {
  it('name the number and what Telnyx did', () => {
    const entry = (result: string) => ({ operation: 'telnyx.t38_gateway',
      details: { number: '+17208565062', shown: '+1 720-856-5062', result } });
    expect(entryAction(entry('turned_on'))).toBe('Turned on T.38 at Telnyx for +1 720-856-5062');
    expect(entryAction(entry('still_off'))).toBe('Tried to turn on T.38 at Telnyx for +1 720-856-5062; Telnyx still shows it off');
    expect(entryAction(entry('refused'))).toBe('Tried to turn on T.38 at Telnyx for +1 720-856-5062; Telnyx refused the change');
    expect(entryAction(entry('not_found')))
      .toBe('Tried to turn on T.38 at Telnyx for +1 720-856-5062; the number is not on the Telnyx account');
    expect(entryAction(entry('unreachable')))
      .toBe('Tried to turn on T.38 at Telnyx for +1 720-856-5062; Telnyx could not be reached');
    // A request refused for lack of permission reads as an attempt; its outcome chip says Refused.
    expect(entryAction(entry('forbidden'))).toBe('Tried to turn on T.38 at Telnyx for +1 720-856-5062');
  });
});
