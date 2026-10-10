import { describe, expect, it } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import ProviderAccounts from '../components/ProviderAccounts';
import { AdminAPIError } from '../api/client';
import { rulesApi, type ApiRequest } from '../components/ProviderRulesApi';
import { FakeRules } from './providerRulesFake';

const UAE = { country: 'AE', name: 'the United Arab Emirates', regulator: 'TDRA',
  sentence: 'In the UAE, calls over the internet are licensed telecommunications services: they must come from a TDRA licensee (e& or du), from a provider working with one, or from one TDRA approved. International providers are not licensed there.',
  sources: [{ label: 'TDRA: frequently asked questions (VoIP Regulatory Policy 2.0)', url: 'https://tdra.gov.ae/en/FAQs' }] };

// The accounts list from the shared fake, with the country rules answered here: Telnyx is in the UAE.
function withRules(countryRules: (request: ApiRequest) => unknown) {
  const fake = new FakeRules();
  const requests: ApiRequest[] = [];
  const api = rulesApi(async <T,>(request: ApiRequest) => {
    if (request.path.startsWith('/routing/country-rules')) {
      requests.push(request);
      return countryRules(request) as T;
    }
    return JSON.parse(JSON.stringify(await fake.handle(request) ?? {})) as T;
  });
  return { api, requests };
}

describe('Providers → In use: country rules on each account they concern', () => {
  it('shows the rules with their source on the account row and confirms them with your evidence', async () => {
    let confirmed = false;
    const { api, requests } = withRules((request) => {
      if (request.method === 'POST') confirmed = true;
      return { countries: [UAE, { ...UAE, country: 'SA' }], accounts: [{ account: 'sip', label: 'Telnyx', country: 'AE', confirmed,
        sentence: confirmed ? 'Telnyx: confirmed by Ada Admin.'
          : "Telnyx: not confirmed. Faxes still go; confirm that this account's provider is a TDRA licensee, works with one, or is approved by TDRA." }] };
    });
    render(<ProviderAccounts api={api} canWrite />);
    const row = await screen.findByTestId('account-country-rules-sip');
    expect(within(row).getByText(/Telnyx: not confirmed\. Faxes still go/)).toBeTruthy();
    expect(within(row).getByRole('link', { name: 'TDRA: frequently asked questions (VoIP Regulatory Policy 2.0)' })
      .getAttribute('href')).toBe('https://tdra.gov.ae/en/FAQs');
    expect(screen.getAllByTestId(/account-country-rules-/)).toHaveLength(1);
    fireEvent.click(within(row).getByRole('button', { name: 'Confirm the rules for Telnyx' }));
    fireEvent.change(screen.getByLabelText('How you know'), { target: { value: 'Our contract is with du.' } });
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Confirm' }));
    expect(await within(row).findByText('Telnyx: confirmed by Ada Admin.')).toBeTruthy();
    expect(requests.filter((item) => item.method === 'POST').map((item) => item.body)).toEqual([
      { account: 'sip', country: 'AE', evidence: 'Our contract is with du.', evidence_url: null }]);
    expect(within(row).queryByRole('button', { name: 'Confirm the rules for Telnyx' })).toBeNull();
  });

  it('shows nothing where no account is in such a country, and says so when the rules cannot be read', async () => {
    const quiet = withRules(() => ({ countries: [UAE], accounts: [] }));
    const { unmount } = render(<ProviderAccounts api={quiet.api} canWrite />);
    await screen.findByRole('table', { name: 'Provider accounts' });
    await waitFor(() => expect(quiet.requests).toHaveLength(1));
    expect(screen.queryByTestId(/account-country-rules-/)).toBeNull();
    unmount();
    const broken = withRules(() => { throw new AdminAPIError(503, 'Unavailable', 'down'); });
    render(<ProviderAccounts api={broken.api} canWrite />);
    expect(await screen.findByTestId('account-country-rules-unread')).toBeTruthy();
  });
});
