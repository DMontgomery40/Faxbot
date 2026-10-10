import { describe, expect, it } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { RecipientAfterAnswer, SentAfterAnswer } from '../components/delivery/AfterAnswer';
import { AdminAPIError } from '../api/client';

const BILLED = 'The carrier bills from the moment the call is answered, so the seconds in the phone menu are part of the call.';

function fakeClient({ failRead = false, forbidden = false } = {}) {
  const requests: Array<{ method: string; path: string; body?: unknown }> = [];
  let digits: string | null = null;
  const view = () => ({
    number: '+13035550150', digits, spoken: digits ? '2, pause, 105' : null,
    sentence: digits ? `After this number answers, Faxbot presses 2, pause, 105 and then starts the fax. ${BILLED}`
      : 'Faxbot starts the fax as soon as this number answers.',
  });
  const client = {
    async call<T>(request: { method: string; path: string; body?: unknown }): Promise<T> {
      requests.push(request);
      if (request.path.startsWith('/routing/after-answer/faxes/')) {
        return { sentences: [`After the call was answered, Faxbot pressed 2, pause, 105 to reach the fax machine. ${BILLED}`] } as T;
      }
      if (request.path.startsWith('/routing/after-answer/')) {
        if (request.method === 'PUT') {
          if (forbidden) throw new AdminAPIError(403, 'Forbidden', 'Forbidden');
          const body = (request.body ?? {}) as { digits: string | null };
          if (body.digits === 'p9') throw new AdminAPIError(400, 'Bad Request', 'Use the keys 0 to 9, * and #, with w for a short pause or W for a one-second pause, at most 32 in all.');
          digits = body.digits ? '2w105' : null;
          return { ...view(), saved: digits
            ? `Saved. Faxbot will press 2, pause, 105 after this number answers, only on calls over your trunk. ${BILLED}`
            : 'Saved. Faxbot no longer presses any keys after this number answers.' } as T;
        }
        if (failRead) throw new AdminAPIError(503, 'Unavailable', 'Unavailable');
        return view() as T;
      }
      throw new AdminAPIError(404, 'Not Found', 'Not Found');
    },
  };
  return { client, requests };
}

describe('keys to press after answer', () => {
  it('saves keys for a phone menu, says the menu seconds are billed, and clears them', async () => {
    const fake = fakeClient();
    render(<RecipientAfterAnswer client={fake.client} number="+13035550150" canWrite />);
    const region = await screen.findByRole('region', { name: 'Keys to press after answer' });
    expect(within(region).getByText('Faxbot starts the fax as soon as this number answers.')).toBeTruthy();
    fireEvent.change(within(region).getByLabelText('Keys to press after it answers'), { target: { value: '2w105' } });
    fireEvent.click(within(region).getByRole('button', { name: 'Save' }));
    expect(await screen.findByText(/Saved\. Faxbot will press 2, pause, 105 after this number answers/)).toBeTruthy();
    fireEvent.click(within(region).getByRole('button', { name: 'Press none' }));
    expect(await screen.findByText('Saved. Faxbot no longer presses any keys after this number answers.')).toBeTruthy();
    await waitFor(() => expect(fake.requests.filter((item) => item.method === 'PUT').map((item) => item.body))
      .toEqual([{ digits: '2w105' }, { digits: null }]));
  });

  it('shows the refusal sentence, permission, and a load failure; read-only shows no field', async () => {
    const fake = fakeClient();
    const { unmount } = render(<RecipientAfterAnswer client={fake.client} number="+13035550150" canWrite />);
    const region = await screen.findByRole('region', { name: 'Keys to press after answer' });
    fireEvent.change(within(region).getByLabelText('Keys to press after it answers'), { target: { value: 'p9' } });
    fireEvent.click(within(region).getByRole('button', { name: 'Save' }));
    expect(await screen.findByText(/Use the keys 0 to 9, \* and #/)).toBeTruthy();
    unmount();
    const denied = fakeClient({ forbidden: true });
    const second = render(<RecipientAfterAnswer client={denied.client} number="+13035550150" canWrite />);
    const secondRegion = await screen.findByRole('region', { name: 'Keys to press after answer' });
    fireEvent.change(within(secondRegion).getByLabelText('Keys to press after it answers'), { target: { value: '2' } });
    fireEvent.click(within(secondRegion).getByRole('button', { name: 'Save' }));
    expect(await screen.findByText('You do not have permission to do this.')).toBeTruthy();
    second.unmount();
    const readOnly = render(<RecipientAfterAnswer client={fakeClient().client} number="+13035550150" canWrite={false} />);
    await screen.findByRole('region', { name: 'Keys to press after answer' });
    expect(screen.queryByLabelText('Keys to press after it answers')).toBeNull();
    readOnly.unmount();
    render(<RecipientAfterAnswer client={fakeClient({ failRead: true }).client} number="+13035550150" canWrite />);
    expect(await screen.findByText(/could not be loaded/)).toBeTruthy();
  });

  it('says in Sent details which keys a call pressed', async () => {
    render(<SentAfterAnswer client={fakeClient().client} jobId="job-1" />);
    expect(await screen.findByText(/Faxbot pressed 2, pause, 105 to reach the fax machine/)).toBeTruthy();
  });
});
