import { useState } from 'react';
import { describe, expect, it } from 'vitest';
import { fireEvent, render, screen, within } from '@testing-library/react';
import { ReceivedTry, ReceivingOptionsFields, SUBADDRESS_HELP } from '../components/ProviderRulesReceiving';
import { NO_RECEIVING_OPTIONS, type ReceivingOptions } from '../components/ProviderRulesApi';
import { receivingSentence } from '../components/ProviderRulesText';
import { FakeRules } from './providerRulesFake';
import AdminAPIClient from '../api/client';
import ResourceAccess from '../components/ResourceAccess';
import { backend } from '../test/server';

function choose(name: string, option: string) {
  fireEvent.mouseDown(screen.getByRole('combobox', { name }));
  fireEvent.click(within(screen.getByRole('listbox')).getByRole('option', { name: option }));
}

const accounts = [{ key: 'sip', label: 'Telnyx' }, { key: 'sinch-uk', label: 'Sinch (UK)' }];

describe('a number rule by subaddress and site', () => {
  it('saves the subaddress and the site, and says a subaddress never gives anyone access', () => {
    let latest: ReceivingOptions = NO_RECEIVING_OPTIONS;
    function Harness() {
      const [value, setValue] = useState<ReceivingOptions>(NO_RECEIVING_OPTIONS);
      latest = value;
      return <ReceivingOptionsFields value={value} onChange={setValue} accounts={accounts} timeZone="America/Denver"
        connectors={[]} sites={[{ key: 'leeds', label: 'Leeds office' }]} />;
    }
    render(<Harness />);
    expect(screen.getByText(SUBADDRESS_HELP)).toBeTruthy();
    expect(SUBADDRESS_HELP).toMatch(/never gives anyone access/);
    fireEvent.change(screen.getByLabelText('Only faxes with subaddress'), { target: { value: ' 2001 ' } });
    choose('Only faxes received on an account of', 'Leeds office');
    expect(latest).toEqual({ ...NO_RECEIVING_OPTIONS, subaddress: '2001', site_key: 'leeds' });
    fireEvent.change(screen.getByLabelText('Only faxes with subaddress'), { target: { value: '' } });
    expect(latest.subaddress).toBeNull();
  });

  it('offers no site choice when the organization has no sites', () => {
    render(<ReceivingOptionsFields value={NO_RECEIVING_OPTIONS} onChange={() => undefined} accounts={accounts}
      timeZone="America/Denver" connectors={[]} />);
    expect(screen.queryByRole('combobox', { name: 'Only faxes received on an account of' })).toBeNull();
  });

  it('reads the rule as a sentence that names the subaddress and the site', () => {
    expect(receivingSentence({ to_number: '+17208565062', mailbox_label: 'Billing', subaddress: '2001', site_key: 'leeds' },
      { account: (key) => key, connector: (id) => id, site: () => 'Leeds office' }))
      .toBe('Faxes to +17208565062 with subaddress 2001 received on an account of Leeds office go to Billing.');
  });

  it('lists each rule of one number on its own row on Numbers', async () => {
    backend.state.rules.set('rule_front', { id: 'rule_front', to_number: '+17208565062', mailbox_id: 'mbx_main', version: 1 });
    backend.state.rules.set('rule_sub', { id: 'rule_sub', to_number: '+17208565062', mailbox_id: 'mbx_main', version: 1,
      subaddress: '2001', urgent: true });
    await AdminAPIClient.login('admin', 'correct horse');
    const client = new AdminAPIClient({ kind: 'session', csrf: null });
    render(<ResourceAccess client={client} me={await client.me()} section="numbers" />);
    expect(await screen.findByText(/with subaddress 2001 go to Main line, marked urgent\./)).toBeTruthy();
    expect(screen.getAllByText('+17208565062')).toHaveLength(2);
  });

  it('tries a received fax with the subaddress its sender would state', async () => {
    const fake = new FakeRules();
    render(<ReceivedTry api={fake.api()} accounts={accounts} timeZone="America/Denver" />);
    fireEvent.change(screen.getByLabelText('Sent to your number'), { target: { value: '+17208565062' } });
    fireEvent.change(screen.getByLabelText('Subaddress'), { target: { value: '2001' } });
    fireEvent.click(screen.getByRole('button', { name: 'Try it' }));
    await screen.findByText(/It would go to/);
    expect(fake.sent('POST', '/access/inbound-rules/explain')).toEqual([
      { to_number: '+17208565062', from_number: null, account_key: null, at: null, subaddress: '2001' }]);
  });
});
