import { describe, expect, it } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import AnalogLinePanel from '../components/AnalogLinePanel';
import { providerChoices } from '../components/common/ProviderDirections';
import { AdminAPIError } from '../api/client';

const BEFORE = {
  analog: true, account: 'sip-line', label: 'Denver office line', preset_label: 'Grandstream HT813 (analog line)',
  calls_at_once: 1, local_prefixes: 0, imported_at: null, sources: [], toll_rate: null, monthly_fee: null,
  sentence: 'No local calling area yet: import the list of local prefixes for this line, so local numbers go out on it at no extra cost.',
  save_page_help: 'https://www.localcallingguide.com/saq.php',
};
const AFTER = {
  ...BEFORE, local_prefixes: 3, imported_at: '2026-10-10T15:00:00Z', toll_rate: '$0.10 a minute', monthly_fee: '$45.00',
  sources: ['https://www.localcallingguide.com/lca_prefix.php?npa=303&nxx=426'],
  sentence: '3 local prefixes go out on this line at no extra cost; other numbers cost $0.10 a minute on it, and Faxbot sends them the cheapest way.',
};

function fakeCall({ analog = true, refuse = false, fail = false } = {}) {
  const requests: Array<{ method: string; path: string; body?: any }> = [];
  const call = async <T,>(request: { method: string; path: string; body?: unknown }): Promise<T> => {
    requests.push(request as any);
    if (fail) throw new AdminAPIError(503, 'Unavailable', 'Unavailable');
    if (request.method === 'PUT' && request.path.endsWith('/routing')) {
      const on = (request.body as { on: boolean }).on;
      return { ...AFTER, routing: on ? 'listed' : 'off',
        route_sentence: on ? 'Faxbot chooses this line by itself for the numbers it reaches for less.'
          : 'Faxbot does not choose this line by itself: turn that on here, or name the line in a sending rule under Providers → Rules.',
        saved: on ? 'Faxbot now sends local calls on this line by itself; turn that off here.'
          : 'Faxbot no longer chooses this line by itself; your other routes and sending rules are unchanged.' } as T;
    }
    if (request.method === 'PUT') {
      if (refuse) throw new AdminAPIError(400, 'Bad Request', 'Enter what your line charges a minute for calls outside the local area, from your phone bill or your carrier; enter 0 if your plan includes them.');
      return { ...AFTER, routing: 'listed', route_sentence: 'Faxbot chooses this line by itself for the numbers it reaches for less.',
        saved: `Saved. ${AFTER.sentence} Faxbot now sends local calls on this line by itself; turn that off here.` } as T;
    }
    return (analog ? BEFORE : { analog: false, account: 'sip-two' }) as T;
  };
  return { call, requests };
}

describe('an analog line through a gateway', () => {
  it('imports a saved list with the line prices and says what goes out on the line', async () => {
    const fake = fakeCall();
    render(<AnalogLinePanel call={fake.call} accountKey="sip-line" expectAnalog />);
    const region = await screen.findByRole('region', { name: 'Local calls on this line' });
    expect(within(region).getByText(/No local calling area yet/)).toBeTruthy();
    const list = new File(['303-426\n303-298\n'], 'local.txt', { type: 'text/plain' });
    fireEvent.change(within(region).getByLabelText('Saved list of local prefixes'), { target: { files: [list] } });
    await within(region).findByText('List: local.txt');
    fireEvent.change(within(region).getByLabelText("Your line's prefix (optional)"), { target: { value: '303-426' } });
    fireEvent.change(within(region).getByLabelText('Other numbers, per minute'), { target: { value: '0.10' } });
    fireEvent.change(within(region).getByLabelText('Monthly fee (optional)'), { target: { value: '45' } });
    fireEvent.click(within(region).getByRole('button', { name: 'Save the local calling area' }));
    expect(await screen.findByText(/^Saved\. 3 local prefixes go out on this line/)).toBeTruthy();
    await waitFor(() => expect(fake.requests.filter((item) => item.method === 'PUT').map((item) => item.body))
      .toEqual([{ text: '303-426\n303-298\n', filename: 'local.txt', line: '303-426', toll_per_minute: '0.10', monthly_fee: '45' }]));
    expect(fake.requests[0].path).toBe('/routing/analog-lines/sip-line');
    expect(screen.getByText(/turn that off here\.$/)).toBeTruthy();
    fireEvent.click(within(region).getByRole('button', { name: 'Turn off' }));
    expect(await screen.findByText(/^Faxbot no longer chooses this line by itself/)).toBeTruthy();
    expect(fake.requests[fake.requests.length - 1]).toEqual({ method: 'PUT', path: '/routing/analog-lines/sip-line/routing', body: { on: false } });
    expect(within(region).getByRole('button', { name: 'Turn on' })).toBeTruthy();
  });

  it('shows a refusal, nothing for another trunk, and a load failure only when the line is known', async () => {
    const refusing = fakeCall({ refuse: true });
    const first = render(<AnalogLinePanel call={refusing.call} accountKey="sip-line" />);
    const region = await screen.findByRole('region', { name: 'Local calls on this line' });
    fireEvent.change(within(region).getByLabelText('Saved list of local prefixes'),
      { target: { files: [new File(['303-426'], 'local.txt')] } });
    await within(region).findByText('List: local.txt');
    fireEvent.click(within(region).getByRole('button', { name: 'Save the local calling area' }));
    expect(await screen.findByText(/charges a minute for calls outside the local area/)).toBeTruthy();
    first.unmount();
    const other = render(<AnalogLinePanel call={fakeCall({ analog: false }).call} accountKey="sip-two" />);
    await waitFor(() => expect(screen.queryByRole('region', { name: 'Local calls on this line' })).toBeNull());
    other.unmount();
    const quiet = render(<AnalogLinePanel call={fakeCall({ fail: true }).call} accountKey="sip-two" />);
    await waitFor(() => expect(screen.queryByText(/could not be loaded/)).toBeNull());
    quiet.unmount();
    render(<AnalogLinePanel call={fakeCall({ fail: true }).call} accountKey="sip" expectAnalog />);
    expect(await screen.findByText(/could not be loaded/)).toBeTruthy();
  });

  it('lists the gateways in their own group when choosing a provider', () => {
    const groups = providerChoices([
      { id: 'telnyx', label: 'Telnyx', kind: 'carrier' },
      { id: 'avaya-ipoffice', label: 'Avaya IP Office', kind: 'phone_system' },
      { id: 'grandstream-ht813', label: 'Grandstream HT813 (analog line)', kind: 'analog_line' },
      { id: 'custom', label: 'Another carrier', kind: 'carrier' },
    ], 'US', false);
    const titles = groups.map((group) => group.title);
    expect(titles).toContain('Your analog line through a gateway');
    const carriers = groups.find((group) => group.title === 'Your own fax line through a carrier');
    expect(carriers?.options.map((option) => option.value)).toEqual(['sip:telnyx', 'sip:custom']);
    expect(groups.find((group) => group.title === 'Your analog line through a gateway')?.options)
      .toEqual([{ value: 'sip:grandstream-ht813', label: 'Grandstream HT813 (analog line)' }]);
  });
});
