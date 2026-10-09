import { describe, expect, it } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import ProviderRules from '../components/ProviderRules';
import MailboxSendingRules from '../components/MailboxSendingRules';
import ProviderAccounts from '../components/ProviderAccounts';
import { FaxRouteItems, HeldFaxes } from '../components/ProviderRulesHeld';
import { CONDITION_FIELDS, limitActions, routeActions } from '../components/ProviderRulesEditor';
import fixture from './providerRulesSentences.json';
import { FakeRules } from './providerRulesFake';

function choose(container: HTMLElement, name: string | RegExp, option: string | RegExp) {
  fireEvent.mouseDown(within(container).getByRole('combobox', { name }));
  fireEvent.click(within(screen.getByRole('listbox')).getByRole('option', { name: option }));
}

async function openRules(fake = new FakeRules(), canWrite = true) {
  const api = fake.api();
  render(<ProviderRules api={api} canWrite={canWrite} loadNumberRules={async () => [{ to_number: '+17208565062', mailbox_label: 'Front desk' }]}
    onNavigate={() => undefined} />);
  await screen.findByText(/Version \d+ is in effect/);
  return { fake, api };
}

const lastDraft = (fake: FakeRules) => fake.sent('PUT', '/routing/rules/draft').slice(-1)[0] as { document: any; expected_version: number };

describe('Providers → Rules: sending', () => {
  it('reads each rule as a sentence, limits before routing rules, with the fixed last row', async () => {
    await openRules();
    expect(screen.getByText('When the destination country is United Kingdom, never use HumbleFax.')).toBeTruthy();
    expect(screen.getByText('When the destination country is United Kingdom, try Sinch (UK), then Telnyx.')).toBeTruthy();
    expect(screen.getByText('12 faxes in the last 30 days')).toBeTruthy();
    expect(screen.getByText('No faxes in the last 30 days')).toBeTruthy();
    expect(screen.getByText('Everything else: the cheapest reliable route, as before.')).toBeTruthy();
    expect(screen.getByText('Mandatory')).toBeTruthy();
    expect(await screen.findByText('Faxes to +17208565062 go to Front desk.')).toBeTruthy();
  });

  it('adds a limit through the editor and saves it to the draft', async () => {
    const { fake } = await openRules();
    fireEvent.click(screen.getByRole('button', { name: 'Add a limit' }));
    const dialog = await screen.findByRole('dialog', { name: 'Add a limit' });
    fireEvent.change(within(dialog).getByLabelText('Name'), { target: { value: 'Big faxes need approval' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Add a condition' }));
    choose(dialog, 'Condition', 'More pages than');
    fireEvent.change(within(dialog).getByLabelText('More pages than'), { target: { value: '20' } });
    fireEvent.click(within(dialog).getByLabelText('Hold the fax until someone approves it'));
    fireEvent.click(within(dialog).getByLabelText('The approver must be someone other than the sender'));
    expect(within(dialog).getByTestId('rule-preview').textContent)
      .toBe('When the fax has more than 20 pages, hold the fax for approval by someone other than the sender.');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save to draft' }));
    await screen.findByText('You have changes that are not in effect yet.');
    const saved = lastDraft(fake);
    expect(saved.expected_version).toBe(0);
    expect(saved.document.limits[1]).toEqual({ id: 'l-big-faxes-need-approval', name: 'Big faxes need approval', on: true,
      when: { document: { pages_over: 20 } }, then: { hold_for_approval: { separate_approver: true } } });
    expect(screen.getByText('“Big faxes need approval” added to your draft.')).toBeTruthy();
    // Saving checks the rules but replays no faxes, so it does not claim that all is well.
    expect(screen.queryByText('No problems found.')).toBeNull();
  });

  it('adds a routing rule with accounts in order, a layout and an alternate-number setting', async () => {
    const { fake } = await openRules();
    fireEvent.click(screen.getByRole('button', { name: 'Add a routing rule' }));
    const dialog = await screen.findByRole('dialog', { name: 'Add a routing rule' });
    fireEvent.change(within(dialog).getByLabelText('Name'), { target: { value: 'Legal faxes from Leeds' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Add a condition' }));
    choose(dialog, 'Condition', 'Labelled');
    choose(dialog, 'Labelled', 'legal');
    fireEvent.keyDown(screen.getByRole('listbox'), { key: 'Escape' });
    choose(dialog, 'How to send', 'Accounts in the order I choose');
    choose(dialog, 'Add an account', 'Sinch (UK)');
    choose(dialog, 'Add an account', 'Telnyx');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Move Telnyx up' }));
    choose(dialog, 'When every line is busy', 'Use the next account');
    choose(dialog, 'Pages per sheet', 'As many as the receiving machine allows');
    choose(dialog, 'Approved alternate number', 'Dial it when the recipient has one');
    expect(within(dialog).getByTestId('rule-preview').textContent).toBe('When the fax is labelled legal, try Telnyx, then Sinch (UK). '
      + 'If every line is busy, Faxbot uses the next account. Pages per sheet: as the receiving machine allows. '
      + "Faxbot dials the recipient's approved alternate number when there is one.");
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save to draft' }));
    await waitFor(() => expect(fake.sent('PUT', '/routing/rules/draft')).toHaveLength(1));
    expect(lastDraft(fake).document.routes[1]).toEqual({ id: 'r-legal-faxes-from-leeds', name: 'Legal faxes from Leeds', on: true,
      when: { labels: ['legal'] }, then: { try_in_order: ['sip', 'sinch-uk'], when_busy: 'next', page_layout: 'as_receiver_allows',
        alternate_number: 'use' } });
  });

  it('switches, moves and removes rules in the draft', async () => {
    const { fake } = await openRules();
    fireEvent.click(screen.getByRole('checkbox', { name: 'UK numbers go through Sinch is on' }));
    await screen.findByText('“UK numbers go through Sinch” is off in your draft.');
    expect(lastDraft(fake).document.routes[0].on).toBe(false);
    fireEvent.click(screen.getByRole('button', { name: 'Remove Never send UK faxes by HumbleFax' }));
    await screen.findByText('“Never send UK faxes by HumbleFax” removed from your draft.');
    expect(lastDraft(fake)).toMatchObject({ expected_version: 1, document: { limits: [] } });
  });

  it('checks the draft, then publishes it with the versions it was read at', async () => {
    const { fake } = await openRules();
    fireEvent.click(screen.getByRole('checkbox', { name: 'UK numbers go through Sinch is on' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Check' }));
    expect(await screen.findByText('The rule ‘Leeds first’ names a site that does not exist.')).toBeTruthy();
    expect(screen.getByText(/12 of your last 200 faxes would go differently\. 3 of them were sent before rules existed/)).toBeTruthy();
    expect(screen.getByRole('table', { name: 'Faxes that would go differently' })).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Publish' }));
    const dialog = await screen.findByRole('dialog', { name: 'Publish these rules' });
    fireEvent.change(within(dialog).getByLabelText('What changed and why'), { target: { value: 'Pause the UK rule' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Publish' }));
    expect(await screen.findByText('Version 2 is in effect for new faxes.')).toBeTruthy();
    expect(fake.sent('POST', '/routing/rules/publish')).toEqual([
      { expected_active_revision: 1, expected_draft_version: 1, note: 'Pause the UK rule' }]);
    fireEvent.click(await screen.findByRole('button', { name: 'Apply to waiting faxes' }));
    fireEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Apply' }));
    expect(await screen.findByText('1 of 4 waiting faxes will go differently.')).toBeTruthy();
  });

  it('says so in one sentence when someone else changed the draft or published meanwhile', async () => {
    const { fake } = await openRules();
    fireEvent.click(screen.getByRole('checkbox', { name: 'UK numbers go through Sinch is on' }));
    await screen.findByText('You have changes that are not in effect yet.');
    fake.draftRace = true;
    fireEvent.click(screen.getByRole('button', { name: 'Remove Never send UK faxes by HumbleFax' }));
    expect(await screen.findByText('Someone else changed these rules. Reload them and make your change again.')).toBeTruthy();
    fake.publishRace = true;
    fireEvent.click(screen.getByRole('button', { name: 'Publish' }));
    const dialog = await screen.findByRole('dialog', { name: 'Publish these rules' });
    fireEvent.change(within(dialog).getByLabelText('What changed and why'), { target: { value: 'Late' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Publish' }));
    expect(await screen.findByText('Someone published other rules meanwhile. Reload them and check again.')).toBeTruthy();
  });

  it('discards the draft after asking', async () => {
    const { fake } = await openRules();
    fireEvent.click(screen.getByRole('checkbox', { name: 'UK numbers go through Sinch is on' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Discard changes' }));
    fireEvent.click(within(await screen.findByRole('dialog', { name: 'Discard your changes?' })).getByRole('button', { name: 'Discard changes' }));
    expect(await screen.findByText('Your changes were thrown away. The published rules are unchanged.')).toBeTruthy();
    expect(fake.sent('DELETE', '/routing/rules/draft')).toHaveLength(1);
  });

  it('shows the rules without any change controls to someone who may only read settings', async () => {
    await openRules(new FakeRules(), false);
    expect(screen.queryByRole('button', { name: 'Add a limit' })).toBeNull();
    expect(screen.queryByRole('checkbox', { name: 'UK numbers go through Sinch is on' })).toBeNull();
    expect(screen.getByText('When the destination country is United Kingdom, never use HumbleFax.')).toBeTruthy();
  });
});

describe('Providers → Rules: the other tabs', () => {
  it('tries a fax against the draft and explains the answer in one sentence', async () => {
    const { fake } = await openRules();
    fireEvent.click(screen.getByRole('tab', { name: 'Try a fax' }));
    fireEvent.change(await screen.findByLabelText(/Fax number/), { target: { value: '+442071234567' } });
    fireEvent.change(screen.getByLabelText('Pages'), { target: { value: '3' } });
    choose(document.body, 'From mailbox', 'Leeds intake');
    choose(document.body, 'Rules to try', 'Version 1');
    fireEvent.click(screen.getByLabelText('legal'));
    fireEvent.click(screen.getByRole('button', { name: 'Try it' }));
    expect(await screen.findByText('Sinch (UK) first, because the rule ‘UK numbers go through Sinch’ matched.')).toBeTruthy();
    expect(screen.getByText('About $0.031')).toBeTruthy();
    expect(screen.getByText('In your plan')).toBeTruthy();
    expect(screen.queryByText('About $0.00')).toBeNull();
    expect(screen.getByText('Not priced yet')).toBeTruthy();
    expect(screen.getByText('Number is in recipient group did not match.')).toBeTruthy();
    expect(screen.getByText("The recipient's preferred route")).toBeTruthy();
    expect(screen.getByText('A mandatory rule chose instead.')).toBeTruthy();
    expect(screen.getByText('Pages per sheet: as the receiving machine allows.')).toBeTruthy();
    fireEvent.click(screen.getByText('Every rule Faxbot read for this fax'));
    expect(within(await screen.findByRole('table', { name: 'Every rule Faxbot read' })).getByText('Leeds intake')).toBeTruthy();
    expect(screen.getByText('The fax is not sent from the mailbox Leeds intake.')).toBeTruthy();
    expect(screen.queryByText(/as_receiver_allows/)).toBeNull();
    expect(screen.getByLabelText('Time at this installation (America/Denver)')).toBeTruthy();
    expect(fake.sent('POST', '/routing/explain')).toEqual([{ to: '+442071234567', pages: 3, size_bytes: null, as: 'me',
      mailbox: 'm-leeds', workflow: null, urgent: false, real_call: false, labels: ['legal'], at: null, scope: 'organization',
      source: { revision: 1 } }]);
  });

  it('keeps recipient groups, labels, sites, regions and workflows in the draft', async () => {
    const { fake } = await openRules();
    fireEvent.click(screen.getByRole('tab', { name: 'Lists' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Add a recipient group' }));
    let dialog = await screen.findByRole('dialog', { name: 'Add a recipient group' });
    fireEvent.change(within(dialog).getByLabelText('Name'), { target: { value: 'Hospitals' } });
    fireEvent.change(within(dialog).getByLabelText('Fax numbers'), { target: { value: '+15550100001,\n+15550100002' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save to draft' }));
    await screen.findByText('Recipient group “Hospitals” saved in your draft.');
    expect(lastDraft(fake).document.lists).toEqual({ 'uk-clinics': { name: 'UK clinics', numbers: ['+441782684953'], prefixes: ['+4420'] },
      hospitals: { name: 'Hospitals', numbers: ['+15550100001', '+15550100002'], prefixes: [] } });
    expect(lastDraft(fake).document.labels).toEqual(['legal']);
    fireEvent.change(screen.getByLabelText('New label'), { target: { value: 'clinical' } });
    fireEvent.click(screen.getByRole('button', { name: 'Add label' }));
    await screen.findByText('Label “clinical” added to your draft.');
    expect(lastDraft(fake).document.labels).toEqual(['legal', 'clinical']);

    fireEvent.click(screen.getByRole('tab', { name: 'Sites & regions' }));
    expect(await screen.findByText('Sinch (UK)')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Add a region' }));
    dialog = await screen.findByRole('dialog', { name: 'Add a region' });
    fireEvent.change(within(dialog).getByLabelText('Name'), { target: { value: 'Scotland' } });
    fireEvent.change(within(dialog).getByLabelText('Numbers starting with'), { target: { value: '+44131, +44141' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save to draft' }));
    await screen.findByText('Region “Scotland” saved in your draft.');
    expect(lastDraft(fake).document.regions.scotland).toEqual({ name: 'Scotland', countries: [], prefixes: ['+44131', '+44141'] });

    fireEvent.click(screen.getByRole('tab', { name: 'Workflows' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Remove Referrals' }));
    await screen.findByText('Workflow “Referrals” removed from your draft.');
    expect(lastDraft(fake).document.workflows).toEqual([]);
  });

  it('opens a workflow’s own rules with the organization’s above them, locked rules marked', async () => {
    const { fake } = await openRules();
    choose(document.body, 'Rules for', 'Workflow: Referrals');
    expect(await screen.findByText('Your organization\'s rules, which apply first')).toBeTruthy();
    expect(screen.getByText('Locked: mailbox and workflow rules cannot replace it')).toBeTruthy();
    expect(fake.requests.some((request) => request.path === '/routing/rules?scope=workflow%3Areferrals')).toBe(true);
    expect(screen.queryByRole('tab', { name: 'Sites & regions' })).toBeNull();
  });

  it('lists versions, compares two and restores one as the draft', async () => {
    const fake = new FakeRules();
    fake.scopes.get('organization')!.revisions.push({ number: 2, note: 'Approval for big faxes', actor_name: 'Ada Admin',
      created_at: '2026-10-07T18:00:00', document: fake.scopes.get('organization')!.revisions[0].document });
    await openRules(fake);
    fireEvent.click(screen.getByRole('tab', { name: 'History' }));
    expect(await screen.findByText('Approval for big faxes')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Compare' }));
    expect(await screen.findByText('When the fax has more than 20 pages, hold the fax for approval.')).toBeTruthy();
    expect(fake.requests.some((request) => request.path === '/routing/rules/revisions/1/diff/2?scope=organization')).toBe(true);
    fireEvent.click(screen.getAllByRole('button', { name: 'Restore as draft' })[1]);
    fireEvent.click(within(await screen.findByRole('dialog', { name: 'Restore version 1 as your draft?' })).getByRole('button', { name: 'Restore' }));
    expect(await screen.findByText('Version 1 is now your draft. Check it, then publish it.')).toBeTruthy();
  });
});

describe("a mailbox's own sending rules", () => {
  it("shows the organization's rules read-only and edits the mailbox's own", async () => {
    const fake = new FakeRules();
    render(<MailboxSendingRules api={fake.api()} mailbox={{ id: 'm-leeds', label: 'Leeds intake' }} canWrite />);
    expect(await screen.findByText("Your organization's rules, which apply first")).toBeTruthy();
    expect(screen.getByText('Leeds intake has no sending rules of its own yet, so your organization\'s rules decide.')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Add a routing rule' }));
    const dialog = await screen.findByRole('dialog', { name: 'Add a routing rule' });
    expect(within(dialog).queryByLabelText(/Mandatory/)).toBeNull();
    fireEvent.change(within(dialog).getByLabelText('Name'), { target: { value: 'Leeds uses HumbleFax' } });
    // A mailbox's rule names the organization's recipient groups.
    fireEvent.click(within(dialog).getByRole('button', { name: 'Add a condition' }));
    choose(dialog, 'Condition', 'Number is in recipient group');
    choose(dialog, 'Number is in recipient group', 'UK clinics');
    fireEvent.keyDown(screen.getByRole('listbox'), { key: 'Escape' });
    choose(dialog, 'Account', 'HumbleFax');
    expect(within(dialog).getByTestId('rule-preview').textContent).toBe('When the number is in UK clinics, use HumbleFax.');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save to draft' }));
    await waitFor(() => expect(fake.requests.some((request) => request.method === 'PUT'
      && request.path === '/routing/rules/draft?scope=mailbox%3Am-leeds')).toBe(true));
    const saved = fake.sent('PUT', '/routing/rules/draft').slice(-1)[0] as { document: { routes: Array<{ when: unknown }> } };
    expect(saved.document.routes[0].when).toEqual({ destination: { lists: ['uk-clinics'] } });
    fireEvent.click(screen.getByRole('button', { name: 'Earlier versions' }));
    expect(await screen.findByText('No version is published yet.')).toBeTruthy();
    expect(fake.requests.some((request) => request.path === '/routing/rules/revisions?scope=mailbox%3Am-leeds')).toBe(true);
  });
});

describe('held faxes and why a fax took its route', () => {
  it('approves and refuses with the version read, and says nothing was sent', async () => {
    const fake = new FakeRules();
    render(<HeldFaxes api={fake.api()} canApprove onNavigate={() => undefined} />);
    expect(await screen.findByText('Waiting for approval: the rule ‘Faxes over 20 pages need approval’ matched.')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Edit rules' })).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Approve the fax to +15550100001' }));
    expect(await screen.findByText('Approved. The fax to +15550100001 is no longer held.')).toBeTruthy();
    expect(fake.sent('POST', '/routing/holds/h-1/approve')).toEqual([{ version: 3 }]);
    fireEvent.click(screen.getByRole('button', { name: 'Check again for the fax to +442071234567' }));
    expect(await screen.findByText('Still no route your rules allow. The fax keeps waiting.')).toBeTruthy();
    expect(fake.sent('POST', '/routing/holds/h-2/check-again')).toEqual([{ version: 1 }]);
    fireEvent.click(screen.getByRole('button', { name: 'Send the fax to +442071234567 anyway' }));
    const anyway = await screen.findByRole('dialog', { name: 'Send the fax to +442071234567 anyway?' });
    expect(within(anyway).getByText('HumbleFax is not offered: the limit ‘Never send UK faxes by HumbleFax’ forbids it.')).toBeTruthy();
    fireEvent.click(within(anyway).getByLabelText('Telnyx: every line is busy'));
    fireEvent.click(within(anyway).getByRole('button', { name: 'Cancel' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Refuse the fax to +442071234567' }));
    const dialog = await screen.findByRole('dialog');
    fireEvent.change(within(dialog).getByLabelText('Reason'), { target: { value: 'Wrong recipient' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Refuse' }));
    expect(await screen.findByText('Refused. Nothing was sent to +442071234567.')).toBeTruthy();
    expect(fake.sent('POST', '/routing/holds/h-2/refuse')).toEqual([{ version: 1, reason: 'Wrong recipient' }]);
  });

  it('sends a fax with no allowed route anyway, by an account left out only by a cap or being busy', async () => {
    const fake = new FakeRules();
    render(<HeldFaxes api={fake.api()} canApprove />);
    fireEvent.click(await screen.findByRole('button', { name: 'Send the fax to +442071234567 anyway' }));
    const dialog = await screen.findByRole('dialog', { name: 'Send the fax to +442071234567 anyway?' });
    fireEvent.click(within(dialog).getByLabelText('Telnyx: every line is busy'));
    fireEvent.click(within(dialog).getByRole('button', { name: 'Send by Telnyx anyway' }));
    expect(await screen.findByText('Approved. The fax to +442071234567 goes by Telnyx.')).toBeTruthy();
    expect(fake.sent('POST', '/routing/holds/h-2/approve')).toEqual([{ version: 1, account: 'sip' }]);
  });

  it('tells someone without the permission where to give it, and keeps the sender from approving their own fax', async () => {
    const fake = new FakeRules();
    fake.holds[0].can_decide = false;
    const { unmount } = render(<HeldFaxes api={fake.api()} canApprove />);
    expect(await screen.findByText('Someone other than the sender must decide on this fax.')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Approve the fax to +15550100001' })).toBeNull();
    unmount();
    render(<HeldFaxes api={new FakeRules().api()} canApprove={false} />);
    expect(await screen.findByText('Approving a fax needs the Approve faxes permission. Give it on Access → Roles.')).toBeTruthy();
    expect(screen.queryByRole('button', { name: /anyway$/ })).toBeNull();
    expect(screen.queryByRole('button', { name: /^Approve the fax/ })).toBeNull();
  });

  it('shows why a fax took its route, attempt by attempt', async () => {
    render(<ul><FaxRouteItems api={new FakeRules().api()} jobId="job-1" /></ul>);
    expect(await screen.findByText('Sent by Sinch (UK) because the rule ‘UK numbers go through Sinch’ matched. Organization rules version 1.')).toBeTruthy();
    expect(screen.getByText('Attempt 1: Sinch (UK)')).toBeTruthy();
    expect(screen.getByText(/^Dialed the recipient's approved alternate number \+448005550100 instead of \+442071234567, approved by Jane Smith on .*2026 \(‘same intake, confirmed by phone’\)\. The recipient pays for calls to this number\.$/)).toBeTruthy();
    expect(screen.getByText('About $0.031 (estimate)')).toBeTruthy();
    // The whole trace, replayed from the fax's stored facts, not only the steps kept with the decision.
    fireEvent.click(screen.getByText('Every rule Faxbot read for this fax'));
    expect(screen.getByText('Destination country is did not match.')).toBeTruthy();
  });
});

describe('Providers → In use: accounts', () => {
  it('lists every account with what it does, its health and the address to give its provider', async () => {
    render(<ProviderAccounts api={new FakeRules().api()} canWrite onNavigate={() => undefined} />);
    expect(await screen.findByText('Sends (default) and receives (default)')).toBeTruthy();
    expect(screen.getByText('Waiting for the first fax')).toBeTruthy();
    expect(screen.getByText(/Give Sinch this address for received faxes: https:\/\/fax\.example\/sinch-inbound\/sinch-uk/)).toBeTruthy();
    expect(screen.getByText('2 lines at once, 1 calls a second')).toBeTruthy();
    expect(screen.getByText('$25.00 a day')).toBeTruthy();
    // The default sending account cannot be switched off until another is the default.
    expect((screen.getByRole('checkbox', { name: 'Telnyx is on' }) as HTMLInputElement).disabled).toBe(true);
    fireEvent.click(screen.getByRole('button', { name: 'Health details for Sinch (UK)' }));
    const dialog = await screen.findByRole('dialog', { name: 'Sinch (UK): Failing' });
    expect(within(dialog).getByText('Check the access key in your Sinch project, then save it here again.')).toBeTruthy();
  });

  it('adds a second account with its secrets, and switches one off', async () => {
    const fake = new FakeRules();
    render(<ProviderAccounts api={fake.api()} canWrite currency="USD" />);
    fireEvent.click(await screen.findByRole('button', { name: 'Add an account' }));
    const dialog = await screen.findByRole('dialog', { name: 'Add an account' });
    choose(dialog, 'Provider', 'Sinch');
    fireEvent.change(within(dialog).getByLabelText('Name'), { target: { value: 'Sinch (US)' } });
    expect((within(dialog).getByLabelText('Short name') as HTMLInputElement).value).toBe('sinch-us');
    fireEvent.change(within(dialog).getByLabelText(/Project ID/), { target: { value: 'proj-2' } });
    fireEvent.change(within(dialog).getByLabelText(/Access key/), { target: { value: 'synthetic-key' } });
    fireEvent.change(within(dialog).getByLabelText(/Access secret/), { target: { value: 'synthetic-secret' } });
    fireEvent.change(within(dialog).getByLabelText('Fax numbers it receives on'), { target: { value: '+13035550100' } });
    fireEvent.change(within(dialog).getByLabelText('Daily spending limit (USD)'), { target: { value: '10' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Add account' }));
    expect(await screen.findByText('Account Sinch (US) added.')).toBeTruthy();
    expect(fake.sent('POST', '/admin/providers/accounts')).toEqual([{ key: 'sinch-us', provider: 'sinch', label: 'Sinch (US)',
      site: null, sends: true, receives: true, numbers: ['+13035550100'],
      limits: { at_once: null, calls_per_second: null, daily_limit: { currency: 'USD', amount: '10' } },
      settings: { project_id: 'proj-2' }, credentials: { api_key: 'synthetic-key', api_secret: 'synthetic-secret' },
      expected_generation: 7 }]);
    expect(screen.queryByText('synthetic-key')).toBeNull();
    fireEvent.click(screen.getByRole('checkbox', { name: 'Sinch (UK) is on' }));
    expect(await screen.findByText('Sinch (UK) is off. Faxes already sent by it are not affected.')).toBeTruthy();
    expect(fake.sent('PATCH', '/admin/providers/accounts/sinch-uk')).toEqual([{ enabled: false, expected_generation: 8 }]);
  });

  it('keeps a saved secret when its field is left empty, and makes another account the default', async () => {
    const fake = new FakeRules();
    render(<ProviderAccounts api={fake.api()} canWrite />);
    fireEvent.click(await screen.findByRole('button', { name: 'Change' }));
    const dialog = await screen.findByRole('dialog', { name: 'Change Sinch (UK)' });
    expect(within(dialog).getByText('Short name: sinch-uk')).toBeTruthy();
    fireEvent.change(within(dialog).getByLabelText(/Access secret/), { target: { value: 'synthetic-new-secret' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save' }));
    expect(await screen.findByText('Account Sinch (UK) saved.')).toBeTruthy();
    const patch = fake.sent('PATCH', '/admin/providers/accounts/sinch-uk')[0] as Record<string, unknown>;
    expect(patch.credentials).toEqual({ api_secret: 'synthetic-new-secret' });
    fireEvent.click(screen.getByRole('button', { name: 'Make default for sending' }));
    expect(await screen.findByText('Faxbot now sends by Sinch (UK) unless a rule says otherwise.')).toBeTruthy();
  });
});

describe('console and command line offer the same rule conditions', () => {
  it('has a condition for every --when field of the faxbot command', () => {
    const console = CONDITION_FIELDS.map((field) => field.id);
    const commandLine = fixture.cli_condition_fields.map((id: string) => (['days', 'between', 'site-time'].includes(id) ? 'time' : id));
    expect([...new Set(commandLine)]).toEqual(console);
  });

  it('writes every action and route setting the faxbot command writes', () => {
    const settings = { when_busy: 'next', page_layout: 'one_per_sheet', alternate_number: 'only', subaddress: '2001' } as const;
    const written = new Set<string>();
    for (const method of ['use', 'try_in_order', 'cheapest_reliable', 'site_accounts', 'automatic'] as const) {
      Object.keys(routeActions(method, { ...settings })).forEach((key) => written.add(key));
    }
    Object.keys(limitActions({ never: ['humblefax'], require_direct: true, require_encryption: true,
      cap_cost: { currency: 'USD', amount: '0' }, hold_for_approval: { separate_approver: true },
      hold_until: { days: ['mon'] }, place_a_real_call: true }, '0.50')).forEach((key) => written.add(key));
    expect([...written].sort()).toEqual([...fixture.action_keys].sort());
  });
});
