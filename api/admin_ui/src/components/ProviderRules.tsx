// Providers → Rules: which provider account carries each fax. The organization's rules come first; a
// workflow's own rules can only narrow them (a mailbox's own rules are on that mailbox's page). Every
// change goes into a draft; Check and Publish put it into effect for new faxes.
import { useEffect, useMemo, useState } from 'react';
import {
  Alert, Box, Button, FormControl, InputLabel, MenuItem, Paper, Select, Stack, Tab, Tabs, Typography,
} from '@mui/material';
import type { AdminDestination } from '../navigation';
import type { RulesApi, Scope } from './ProviderRulesApi';
import { ORGANIZATION } from './ProviderRulesApi';
import { DraftBar, useRulesDraft } from './ProviderRulesDraft';
import { RulesList } from './ProviderRulesSending';
import { ListsAndLabels, SitesAndRegions, Workflows } from './ProviderRulesDefinitions';
import ProviderRulesTry from './ProviderRulesTry';
import ProviderRulesHistory from './ProviderRulesHistory';
import { namesFor } from './ProviderRulesText';

export interface NumberRuleSummary { to_number: string; mailbox_label: string }

type TabId = 'sending' | 'sites' | 'lists' | 'workflows' | 'try' | 'history';

// How received faxes are placed, read-only here: number rules are edited on Numbers.
export function ReceivingSummary({ load, onNavigate }: {
  load?: () => Promise<NumberRuleSummary[]>; onNavigate?: (destination: AdminDestination) => void;
}) {
  const [rules, setRules] = useState<NumberRuleSummary[] | null>(null);
  useEffect(() => {
    let live = true;
    load?.().then((value) => { if (live) setRules(value); }).catch(() => { if (live) setRules(null); });
    return () => { live = false; };
  }, [load]);
  if (!load) return null;
  return (
    <Paper variant="outlined" sx={{ p: 2, borderRadius: 2, mt: 3 }} aria-label="Receiving">
      <Typography variant="h6" component="h2">Receiving</Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
        Which mailbox each of your numbers delivers to is set on Numbers.
      </Typography>
      {rules && rules.length === 0 && <Typography variant="body2">No number has a mailbox yet.</Typography>}
      {rules?.slice(0, 8).map((rule) => (
        <Typography key={`${rule.to_number}-${rule.mailbox_label}`} variant="body2">
          Faxes to {rule.to_number || 'any number'} go to {rule.mailbox_label}.
        </Typography>
      ))}
      {rules && rules.length > 8 && <Typography variant="body2">And {rules.length - 8} more.</Typography>}
      {onNavigate && <Button size="small" sx={{ mt: 1 }} onClick={() => onNavigate('numbers/list')}>Open Numbers</Button>}
    </Paper>
  );
}

export default function ProviderRules({ api, canWrite, currency = 'USD', loadNumberRules, onNavigate }: {
  api: RulesApi;
  // May this person change the organization's rules (settings:write)?
  canWrite: boolean;
  currency?: string;
  loadNumberRules?: () => Promise<NumberRuleSummary[]>;
  onNavigate?: (destination: AdminDestination) => void;
}) {
  const [workflowKey, setWorkflowKey] = useState('');
  const workflowScope: Scope = useMemo(() => ({ kind: 'workflow', id: workflowKey }), [workflowKey]);
  const organizationDraft = useRulesDraft(api, ORGANIZATION);
  const workflowDraft = useRulesDraft(api, workflowScope, Boolean(workflowKey));
  const scope = workflowKey ? workflowScope : ORGANIZATION;
  const draft = workflowKey ? workflowDraft : organizationDraft;
  const [tab, setTab] = useState<TabId>('sending');
  const state = draft.state;
  const writable = canWrite && Boolean(state?.can_write);
  // Workflows are defined in the organization's rules, so the picker reads them from there.
  const workflows = organizationDraft.document.workflows ?? [];
  const isOrganization = scope.kind === 'organization';
  const tabs: Array<[TabId, string]> = isOrganization
    ? [['sending', 'Sending'], ['sites', 'Sites & regions'], ['lists', 'Lists'], ['workflows', 'Workflows'], ['try', 'Try a fax'], ['history', 'History']]
    : [['sending', 'Sending'], ['try', 'Try a fax'], ['history', 'History']];
  const shownTab = tabs.some(([id]) => id === tab) ? tab : 'sending';
  const names = namesFor(isOrganization ? draft.document : { ...organizationDraft.document, ...draft.document,
    sites: organizationDraft.document.sites, regions: organizationDraft.document.regions,
    lists: organizationDraft.document.lists, workflows: organizationDraft.document.workflows }, state?.choices ?? null);

  return (
    <Box>
      <Typography variant="h4" component="h1" sx={{ mb: 1 }}>Rules</Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
        Choose which provider account sends each fax. Faxbot follows these rules first, then picks the cheapest
        reliable route among the accounts they allow.
      </Typography>
      {workflows.length > 0 && (
        <FormControl size="small" sx={{ minWidth: 260, mb: 2 }}>
          <InputLabel id="rules-scope">Rules for</InputLabel>
          <Select labelId="rules-scope" label="Rules for" value={workflowKey} inputProps={{ 'aria-label': 'Rules for' }}
            onChange={(event) => setWorkflowKey(event.target.value)}>
            <MenuItem value="">Your organization</MenuItem>
            {workflows.map((workflow) => <MenuItem key={workflow.key} value={workflow.key}>Workflow: {workflow.name}</MenuItem>)}
          </Select>
        </FormControl>
      )}
      {draft.loading && !state && <Alert severity="info">Loading…</Alert>}
      {state && (
        <>
          <DraftBar draft={draft} canWrite={writable}
            onApplyToWaiting={isOrganization ? () => api.applyToWaiting().then((result) => result.sentence) : undefined} />
          <Tabs value={shownTab} onChange={(_, value) => setTab(value)} variant="scrollable" allowScrollButtonsMobile sx={{ mb: 2 }}>
            {tabs.map(([id, label]) => <Tab key={id} value={id} label={label} />)}
          </Tabs>
          {shownTab === 'sending' && (
            <>
              {!isOrganization && state.organization && (
                <Box sx={{ mb: 3 }}>
                  <Typography variant="h6" component="h2">Your organization's rules, which apply first</Typography>
                  <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
                    This workflow's rules can only narrow these. A locked rule can't be replaced.
                  </Typography>
                  <RulesList document={state.organization.document} names={namesFor(state.organization.document, state.choices)}
                    editable={false} scopeKind="organization" />
                </Box>
              )}
              <RulesList document={draft.document} names={names} matches={state.matches_30_days} editable={writable}
                onSave={draft.save} choices={state.choices} scopeKind={scope.kind} currency={currency} saving={draft.saving} />
              {isOrganization && <ReceivingSummary load={loadNumberRules} onNavigate={onNavigate} />}
            </>
          )}
          {shownTab === 'sites' && <SitesAndRegions document={draft.document} choices={state.choices} editable={writable} onSave={draft.save} />}
          {shownTab === 'lists' && <ListsAndLabels document={draft.document} editable={writable} onSave={draft.save} />}
          {shownTab === 'workflows' && (
            <Workflows document={draft.document} choices={state.choices} editable={writable} onSave={draft.save}
              onOpenWorkflow={(workflow) => { setWorkflowKey(workflow.key); setTab('sending'); }} />
          )}
          {shownTab === 'try' && <ProviderRulesTry api={api} scope={scope} state={state} document={draft.document} />}
          {shownTab === 'history' && (
            <ProviderRulesHistory api={api} scope={scope} scopeKind={scope.kind} choices={state.choices} canWrite={writable}
              hasDraft={Boolean(state.draft)}
              onRestored={(number) => { void draft.reload().then(() => draft.setNotice(`Version ${number} is now your draft. Check it, then publish it.`)); setTab('sending'); }} />
          )}
        </>
      )}
      {!state && !draft.loading && (
        <Stack spacing={1}>
          <DraftBar draft={draft} canWrite={false} />
          <Box><Button onClick={() => void draft.reload()}>Try again</Button></Box>
        </Stack>
      )}
    </Box>
  );
}
