import { useState } from 'react';
import { describe, expect, it } from 'vitest';
import { fireEvent, render, screen, within } from '@testing-library/react';
import { ReceivedTry, ReceivingOptionsFields } from '../components/ProviderRulesReceiving';
import ProviderRulesSendFields, { NO_SEND_OPTIONS, sendBody, type SendOptions } from '../components/ProviderRulesSendFields';
import { AddAsRuleButton } from '../components/ProviderRulesSuggest';
import { WaitingForYouCard } from '../components/ProviderRulesHeld';
import { NO_RECEIVING_OPTIONS, type ReceivingOptions } from '../components/ProviderRulesApi';
import { FakeRules } from './providerRulesFake';

function choose(name: string, option: string) {
  fireEvent.mouseDown(screen.getByRole('combobox', { name }));
  fireEvent.click(within(screen.getByRole('listbox')).getByRole('option', { name: option }));
}

const accounts = [{ key: 'sip', label: 'Telnyx' }, { key: 'sinch-uk', label: 'Sinch (UK)' }];

describe('receiving rules on Numbers', () => {
  it('turns each option into the fields of the number rule', () => {
    let latest: ReceivingOptions = NO_RECEIVING_OPTIONS;
    function Harness() {
      const [value, setValue] = useState<ReceivingOptions>(NO_RECEIVING_OPTIONS);
      latest = value;
      return <ReceivingOptionsFields value={value} onChange={setValue} accounts={accounts} timeZone="America/Denver"
        connectors={[{ key: 'c-night', label: 'Night inbox' }]} />;
    }
    render(<Harness />);
    choose('Only faxes received on', 'Sinch (UK)');
    fireEvent.change(screen.getByLabelText('Only faxes from'), { target: { value: '+13035550100, +1303*' } });
    fireEvent.click(screen.getByLabelText('Only at certain times (America/Denver)'));
    fireEvent.change(screen.getByLabelText('From'), { target: { value: '18:00' } });
    fireEvent.change(screen.getByLabelText('Until'), { target: { value: '07:00' } });
    choose('Email', 'No email');
    fireEvent.click(screen.getByLabelText('Mark these faxes urgent'));
    fireEvent.change(screen.getByLabelText('Keep for (days)'), { target: { value: '30' } });
    expect(screen.getByText(/It is not a legal hold/)).toBeTruthy();
    expect(latest).toEqual({ ...NO_RECEIVING_OPTIONS, account_key: 'sinch-uk', from_numbers: ['+13035550100', '+1303*'],
      days: ['mon', 'tue', 'wed', 'thu', 'fri'], start_minute: 1080, end_minute: 420, email_off: true, urgent: true, keep_days: 30 });
    choose('Email', 'Through Night inbox');
    expect(latest).toMatchObject({ email_off: false, email_connector_id: 'c-night' });
  });

  it('tries a received fax and says where it would go', async () => {
    const fake = new FakeRules();
    render(<ReceivedTry api={fake.api()} accounts={accounts} timeZone="America/Denver" />);
    fireEvent.change(screen.getByLabelText('Sent to your number'), { target: { value: '+17208565062' } });
    fireEvent.change(screen.getByLabelText('From'), { target: { value: '+13035550100' } });
    choose('Received on', 'Telnyx');
    fireEvent.click(screen.getByRole('button', { name: 'Try it' }));
    expect(await screen.findByText(/It would go to Front desk, marked urgent/)).toBeTruthy();
    expect(fake.sent('POST', '/access/inbound-rules/explain')).toEqual([
      { to_number: '+17208565062', from_number: '+13035550100', account_key: 'sip', at: null }]);
  });
});

describe('Send a fax: mailbox, workflow and labels', () => {
  it('shows nothing when the organization uses none of them', () => {
    const { container } = render(<ProviderRulesSendFields choices={{ mailboxes: [], workflows: [], labels: [] }}
      value={NO_SEND_OPTIONS} onChange={() => undefined} />);
    expect(container.textContent).toBe('');
  });

  it('adds only what was chosen to the fax', () => {
    let latest: SendOptions = NO_SEND_OPTIONS;
    function Harness() {
      const [value, setValue] = useState<SendOptions>(NO_SEND_OPTIONS);
      latest = value;
      return <ProviderRulesSendFields value={value} onChange={setValue} choices={{
        mailboxes: [{ id: 'm-leeds', label: 'Leeds intake' }], workflows: [{ key: 'referrals', name: 'Referrals' }],
        labels: ['legal', 'clinical'] }} />;
    }
    render(<Harness />);
    expect(sendBody(latest)).toEqual({});
    choose('Send from mailbox', 'Leeds intake');
    fireEvent.click(screen.getByLabelText('clinical'));
    expect(sendBody(latest)).toEqual({ mailbox: 'm-leeds', labels: ['clinical'] });
    choose('Workflow', 'Referrals');
    expect(sendBody(latest)).toEqual({ mailbox: 'm-leeds', workflow: 'referrals', labels: ['clinical'] });
  });
});

describe('Costs → Recommendations: Add as rule', () => {
  it('puts the suggested rule first in the draft and never publishes', async () => {
    const fake = new FakeRules();
    let notice = '';
    render(<AddAsRuleButton api={fake.api()} onDone={(sentence) => { notice = sentence; }} onError={() => undefined}
      onNavigate={() => undefined}
      suggestion={{ name: 'Faxes to +44 numbers go through Sinch (UK)', when: { destination: { prefixes: ['+44'] } },
        then: { use: 'sinch-uk' } }} />);
    fireEvent.click(screen.getByRole('button', { name: 'Add as rule' }));
    expect(await screen.findByRole('button', { name: 'Open your draft rules' })).toBeTruthy();
    const saved = fake.sent('PUT', '/routing/rules/draft')[0] as { document: { routes: Array<{ id: string }> }; expected_version: number };
    expect(saved.expected_version).toBe(0);
    expect(saved.document.routes.map((rule) => rule.id)).toEqual(['r-faxes-to-44-numbers-go-through-sinch-uk', 'r-uk']);
    expect(fake.sent('POST', '/routing/rules/publish')).toEqual([]);
    expect(notice).toBe('“Faxes to +44 numbers go through Sinch (UK)” is in your draft on Providers → Rules. It takes effect when you publish it.');
  });
});

describe('Overview: faxes waiting for you', () => {
  it('counts held faxes by why they wait', async () => {
    let opened = false;
    render(<WaitingForYouCard api={new FakeRules().api()} onOpen={() => { opened = true; }} />);
    expect(await screen.findByText('2 faxes are waiting for you')).toBeTruthy();
    expect(screen.getByText('1 waiting for approval, 1 with no route your rules allow. Nothing has been sent for them.')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Open Sent' }));
    expect(opened).toBe(true);
  });
});
