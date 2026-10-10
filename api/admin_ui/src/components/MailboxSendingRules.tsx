// A mailbox's own sending rules, on that mailbox's page. They apply to faxes sent from the mailbox and
// can only narrow the organization's rules, which are shown above them, read-only.
import { useEffect, useMemo, useState } from 'react';
import { Alert, Box, Button, FormControl, InputLabel, MenuItem, Select, Typography } from '@mui/material';
import { isForbidden } from '../api/client';
import { DeliveryError } from './delivery/shared';
import type { RulesApi, Scope } from './ProviderRulesApi';
import { DraftBar, useRulesDraft } from './ProviderRulesDraft';
import { RulesList } from './ProviderRulesSending';
import { namesFor } from './ProviderRulesText';
import ProviderRulesHistory from './ProviderRulesHistory';

export default function MailboxSendingRules({ api, mailbox, canWrite, currency = 'USD' }: {
  api: RulesApi;
  mailbox: { id: string; label: string };
  // Manages this mailbox.
  canWrite: boolean;
  currency?: string;
}) {
  const scope: Scope = useMemo(() => ({ kind: 'mailbox', id: mailbox.id }), [mailbox.id]);
  const draft = useRulesDraft(api, scope);
  const [history, setHistory] = useState(false);
  const state = draft.state;
  const writable = canWrite && Boolean(state?.can_write);
  const organization = state?.organization ?? null;
  // The mailbox's rules can name the organization's lists, regions, sites and workflows.
  const names = namesFor(organization?.document ?? null, state?.choices ?? null);

  return (
    <Box aria-label={`Sending rules for ${mailbox.label}`} role="region">
      <Typography variant="h6" component="h2">Sending rules</Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
        These apply to faxes sent from {mailbox.label}. They can only narrow your organization's rules.
      </Typography>
      {draft.loading && !state && <Alert severity="info">Loading…</Alert>}
      {state && (
        <>
          {organization ? (
            <Box sx={{ mb: 3 }}>
              <Typography variant="subtitle1">Your organization's rules, which apply first</Typography>
              <RulesList document={organization.document} names={namesFor(organization.document, state.choices)}
                editable={false} scopeKind="organization" />
            </Box>
          ) : (
            <Typography variant="body2" sx={{ mb: 2 }}>
              Your organization has no published rules, so these are the only rules for this mailbox's faxes.
            </Typography>
          )}
          <DraftBar draft={draft} canWrite={writable} />
          <RulesList document={draft.document} definitions={organization?.document} names={names}
            matches={state.matches_30_days} editable={writable} onSave={draft.save} choices={state.choices}
            scopeKind="mailbox" currency={currency} saving={draft.saving} />
          <Button size="small" onClick={() => setHistory(!history)} sx={{ mt: 1 }}>
            {history ? 'Hide earlier versions' : 'Earlier versions'}
          </Button>
          {history && (
            <Box sx={{ mt: 2 }}>
              <ProviderRulesHistory api={api} scope={scope} scopeKind="mailbox" choices={state.choices} canWrite={writable}
                hasDraft={Boolean(state.draft)}
                onRestored={(number) => { void draft.reload().then(() => draft.setNotice(`Version ${number} is now your draft. Check it, then publish it.`)); }} />
            </Box>
          )}
        </>
      )}
      {!state && !draft.loading && <DraftBar draft={draft} canWrite={false} />}
    </Box>
  );
}

// Delivery setup → Mailboxes: choose a mailbox to see and change its own sending rules.
export function MailboxSendingRulesPicker({ api, loadMailboxes, canWrite, currency }: {
  api: RulesApi;
  loadMailboxes: () => Promise<Array<{ id: string; label: string }>>;
  canWrite: boolean;
  currency?: string;
}) {
  const [mailboxes, setMailboxes] = useState<Array<{ id: string; label: string }> | null>(null);
  const [chosen, setChosen] = useState('');
  const [error, setError] = useState<unknown>(null);
  useEffect(() => {
    let live = true;
    loadMailboxes().then((items) => { if (live) setMailboxes(items); }).catch((failure) => {
      if (!live) return;
      setMailboxes([]);
      // Someone who may not see mailboxes has none to choose; any other failure is said.
      if (!isForbidden(failure)) setError(failure);
    });
    return () => { live = false; };
  }, [loadMailboxes]);
  if (error) return <DeliveryError error={error} onClose={() => setError(null)} />;
  if (!mailboxes || mailboxes.length === 0) return null;
  const mailbox = mailboxes.find((item) => item.id === chosen);
  return (
    <Box>
      <Typography variant="h5" component="h2" sx={{ mb: 1 }}>Sending rules for a mailbox</Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
        A mailbox can narrow your organization's sending rules for the faxes sent from it.
      </Typography>
      <FormControl size="small" sx={{ minWidth: 260, mb: 2 }}>
        <InputLabel id="mailbox-rules-picker">Mailbox</InputLabel>
        <Select labelId="mailbox-rules-picker" label="Mailbox" value={chosen} inputProps={{ 'aria-label': 'Mailbox' }}
          onChange={(event) => setChosen(event.target.value)}>
          {mailboxes.map((item) => <MenuItem key={item.id} value={item.id}>{item.label}</MenuItem>)}
        </Select>
      </FormControl>
      {mailbox && <MailboxSendingRules key={mailbox.id} api={api} mailbox={mailbox} canWrite={canWrite} currency={currency} />}
    </Box>
  );
}
