import { describe, expect, it } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import DialDestinations from '../components/DialDestinations';
import ProviderAccounts from '../components/ProviderAccounts';
import { AdminAPIError } from '../api/client';
import { rulesApi } from '../components/ProviderRulesApi';
import { FakeRules } from './providerRulesFake';

describe('Providers → In use: where Faxbot may dial', () => {
  it('shows each kind of number and each country with why, and allows premium-rate numbers', async () => {
    const fake = new FakeRules();
    render(<DialDestinations api={fake.api()} canWrite />);
    const kinds = await screen.findByRole('table', { name: 'Kinds of numbers' });
    const premium = within(kinds).getByText('Premium-rate numbers').closest('tr')!;
    expect(within(premium).getByText('No')).toBeTruthy();
    expect(within(premium).getByText('Blocked: Faxbot never dials these unless you allow them.')).toBeTruthy();
    const countries = screen.getByRole('table', { name: 'Countries' });
    expect(within(countries).getByText('Allowed: the routing rule ‘UK numbers go through Sinch’ names it.')).toBeTruthy();
    expect(within(countries).getByText(/Allowed: Faxbot had already delivered faxes there before it started checking\. First delivered/)).toBeTruthy();
    fireEvent.click(within(premium).getByRole('button', { name: 'Allow' }));
    expect(await screen.findByText('Changed Premium-rate numbers.')).toBeTruthy();
    expect(fake.sent('PUT', '/routing/dialing/premium')).toEqual([{ state: 'allowed' }]);
  });

  it('sets a price ceiling that keeps the class’s own choice, and allows a country by its code', async () => {
    const fake = new FakeRules();
    render(<DialDestinations api={fake.api()} canWrite />);
    const countries = await screen.findByRole('table', { name: 'Countries' });
    const uk = within(countries).getByText('United Kingdom').closest('tr')!;
    fireEvent.click(within(uk).getByRole('button', { name: 'Price ceiling' }));
    const dialog = await screen.findByRole('dialog', { name: 'Price ceiling: United Kingdom' });
    fireEvent.change(within(dialog).getByLabelText('Highest price a minute'), { target: { value: '0.25' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(fake.sent('PUT', '/routing/dialing/country%3AGB')).toEqual([{ state: 'default', ceiling: '0.25' }]));
    expect(await screen.findByText('$0.25 a minute')).toBeTruthy();
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    fireEvent.change(screen.getByLabelText('Country to allow'), { target: { value: 'AU' } });
    fireEvent.click(screen.getByRole('button', { name: 'Allow this country' }));
    await waitFor(() => expect(fake.sent('PUT', '/routing/dialing/AU')).toEqual([{ state: 'allowed' }]));
  });

  it('shows the list without changes to someone who may only read settings', async () => {
    render(<DialDestinations api={new FakeRules().api()} canWrite={false} />);
    await screen.findByRole('table', { name: 'Kinds of numbers' });
    expect(screen.queryByRole('button', { name: 'Allow' })).toBeNull();
    expect(screen.queryByLabelText('Country to allow')).toBeNull();
  });

  it('hides itself from someone who may not read settings, and says when the list cannot be read', async () => {
    const forbidden = rulesApi(async () => { throw new AdminAPIError(403, 'Forbidden', 'Forbidden'); });
    const { container } = render(<DialDestinations api={forbidden} canWrite />);
    await waitFor(() => expect(container.textContent).toBe(''));
    const failing = rulesApi(async () => { throw new AdminAPIError(503, 'Unavailable', 'Where Faxbot may dial is unavailable right now. Try again in a moment.'); });
    render(<DialDestinations api={failing} canWrite />);
    expect(await screen.findByRole('alert')).toBeTruthy();
  });

  it('appears under the accounts on Providers → In use', async () => {
    render(<ProviderAccounts api={new FakeRules().api()} canWrite />);
    expect(await screen.findByRole('region', { name: 'Where Faxbot may dial' })).toBeTruthy();
  });
});
