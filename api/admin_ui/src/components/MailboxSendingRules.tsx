// A mailbox's own sending rules, on that mailbox's page. They apply to faxes sent from the mailbox and
// can only narrow the organization's rules, which are shown above them, read-only.
import { useMemo } from 'react';
import { Alert, Box, Typography } from '@mui/material';
import type { RulesApi, Scope } from './ProviderRulesApi';
import { DraftBar, useRulesDraft } from './ProviderRulesDraft';
import { RulesList } from './ProviderRulesSending';
import { namesFor } from './ProviderRulesText';

export default function MailboxSendingRules({ api, mailbox, canWrite, currency = 'USD' }: {
  api: RulesApi;
  mailbox: { id: string; label: string };
  // Manages this mailbox.
  canWrite: boolean;
  currency?: string;
}) {
  const scope: Scope = useMemo(() => ({ kind: 'mailbox', id: mailbox.id }), [mailbox.id]);
  const draft = useRulesDraft(api, scope);
  const state = draft.state;
  const writable = canWrite && Boolean(state?.can_write);
  const organization = state?.organization ?? null;
  // The mailbox's rules can name the organization's lists, regions, sites and workflows.
  const names = namesFor({ ...(organization?.document ?? { format: 1 }), ...draft.document,
    lists: organization?.document.lists, regions: organization?.document.regions, sites: organization?.document.sites,
    workflows: organization?.document.workflows }, state?.choices ?? null);

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
          <RulesList document={draft.document} names={names} matches={state.matches_30_days} editable={writable}
            onSave={draft.save} choices={state.choices} scopeKind="mailbox" currency={currency} saving={draft.saving} />
        </>
      )}
      {!state && !draft.loading && <DraftBar draft={draft} canWrite={false} />}
    </Box>
  );
}
