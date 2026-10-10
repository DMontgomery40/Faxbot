// Administration → Audit log: who did what in Faxbot, newest first, from GET /access/audit.
import { useCallback, useEffect, useState } from 'react';
import {
  Alert, Box, Button, Chip, CircularProgress, FormControl, InputLabel, MenuItem, Paper, Select, Stack, Table, TableBody,
  TableCell, TableContainer, TableHead, TableRow, Typography,
} from '@mui/material';
import AdminAPIClient from '../api/client';
import type { AccessUser, AuditEntry } from '../api/types';
import { formatServerTime } from '../api/time';
import { ScreenHeader } from './access/AccessViews';

// Each action in plain words; an action not listed here is shown with its own name made readable.
export const AUDIT_ACTIONS: Record<string, string> = {
  issue_password_session: 'Signed in with a password',
  issue_key_session: 'Signed in with an API key',
  issue_bootstrap_session: 'Signed in with the installation key',
  logout_session: 'Signed out',
  change_password: 'Changed their password',
  revoke_session: 'Ended a session',
  'authentication.admit': 'Tried to sign in',
  'owner.recover': 'Recovered owner access',
  create_user: 'Added a user',
  update_user: 'Changed a user',
  reset_password: 'Reset a password',
  enroll_owner: 'Made someone the owner',
  create_integration: 'Added a connected system',
  update_integration: 'Changed a connected system',
  create_group: 'Added a group',
  update_group: 'Changed a group',
  add_membership: 'Added someone to a group',
  remove_membership: 'Removed someone from a group',
  create_custom_role: 'Added a role',
  update_custom_role: 'Changed a role',
  create_assignment: 'Gave access',
  remove_assignment: 'Removed access',
  issue_key: 'Created an API key',
  issue_integration_key: 'Created a key for a connected system',
  update_key_metadata: "Changed an API key's details",
  rotate_key: 'Replaced an API key',
  revoke_key: 'Revoked an API key',
  approve_pending_key: "Approved a phone's key",
  create_mailbox: 'Added a mailbox',
  update_mailbox: 'Changed a mailbox',
  create_inbound_rule: 'Gave a fax number a mailbox',
  update_inbound_rule: "Changed a fax number's mailbox",
  'settings.update': 'Changed settings',
  'providers.configure': 'Changed providers',
  'providers.write': 'Changed providers',
  'providers.install': 'Installed a provider plugin',
  'configuration.bootstrap.activate': 'Applied the first settings',
  'configuration.environment': 'Read the settings Faxbot was installed with',
  'routing.rate_cards_added': 'Added prices',
  'fax.accept': 'Submitted a fax',
  'fax.reconcile': 'Checked a fax whose result was unclear',
  'inbound.receive': 'Received a fax',
  'inbound.backfill': 'Added earlier received faxes',
  'capability.issue': 'Made a one-time code',
  'capability.consume': 'Used a one-time code',
  'host.restart': 'Restarted Faxbot',
  'telnyx.t38_gateway': 'Turned on T.38 at Telnyx',
  'telnyx.caller_name_lookup': 'Turned off caller-name lookup at Telnyx',
  'routing.toll_free_approval': "Recorded a recipient's toll-free number",
  'host.terminal': 'Opened the terminal',
  'host.actions': 'Ran a server action',
  'access.retire_permissions': 'Removed permissions that no longer do anything',
  access_migration: 'Kept access from an earlier version of Faxbot',
};

export function auditAction(operation: string): string {
  if (AUDIT_ACTIONS[operation]) return AUDIT_ACTIONS[operation];
  const words = operation.replace(/[._:]+/g, ' ').trim();
  return words ? words[0].toUpperCase() + words.slice(1) : operation;
}

// One entry's action. The terminal records two: asking to open it, then each session it starts.
// A change to Telnyx's T.38 setting names the number and what Telnyx did.
const TELNYX_T38_RESULTS: Record<string, string> = {
  turned_on: 'Turned on T.38 at Telnyx for {number}',
  still_off: 'Tried to turn on T.38 at Telnyx for {number}; Telnyx still shows it off',
  refused: 'Tried to turn on T.38 at Telnyx for {number}; Telnyx refused the change',
  not_found: 'Tried to turn on T.38 at Telnyx for {number}; the number is not on the Telnyx account',
  unreachable: 'Tried to turn on T.38 at Telnyx for {number}; Telnyx could not be reached',
};

// Turning off caller-name lookup names the number and what Telnyx did.
const TELNYX_NAME_RESULTS: Record<string, string> = {
  turned_off: 'Turned off caller-name lookup at Telnyx for {number}',
  still_on: 'Tried to turn off caller-name lookup at Telnyx for {number}; Telnyx still shows it on',
  refused: 'Tried to turn off caller-name lookup at Telnyx for {number}; Telnyx refused the change',
  not_found: 'Tried to turn off caller-name lookup at Telnyx for {number}; the number is not on the Telnyx account',
  unreachable: 'Tried to turn off caller-name lookup at Telnyx for {number}; Telnyx could not be reached',
};

// A toll-free change names the recipient's number and what was recorded.
const TOLL_FREE_ACTIONS: Record<string, string> = {
  noted: 'Put a toll-free number on file for {number}',
  approved: "Recorded the recipient's approval of a toll-free number for {number}",
  withdrawn: 'Withdrew the toll-free number for {number}',
};

export function entryAction(entry: Pick<AuditEntry, 'operation' | 'details'>): string {
  if (entry.operation === 'host.terminal' && entry.details?.session !== 'started') return 'Asked for access to the server terminal';
  if (entry.operation === 'telnyx.t38_gateway') {
    const number = String(entry.details?.shown ?? entry.details?.number ?? 'a number');
    const template = TELNYX_T38_RESULTS[String(entry.details?.result)] ?? 'Tried to turn on T.38 at Telnyx for {number}';
    return template.replace('{number}', number);
  }
  if (entry.operation === 'telnyx.caller_name_lookup') {
    const number = String(entry.details?.shown ?? entry.details?.number ?? 'a number');
    const template = TELNYX_NAME_RESULTS[String(entry.details?.result)]
      ?? 'Tried to turn off caller-name lookup at Telnyx for {number}';
    return template.replace('{number}', number);
  }
  if (entry.operation === 'routing.toll_free_approval') {
    const template = TOLL_FREE_ACTIONS[String(entry.details?.action)] ?? AUDIT_ACTIONS[entry.operation];
    return template.replace('{number}', String(entry.details?.number ?? 'a recipient'));
  }
  return auditAction(entry.operation);
}

const SIGNED_IN_WITH: Record<AuditEntry['credential_kind'], string> = {
  session: 'Signed in',
  key: 'API key',
  bootstrap: 'Installation key',
  system: 'Faxbot itself',
};

const TARGET_KINDS: Record<string, string> = {
  principal: 'Person', group: 'Group', role: 'Role', mailbox: 'Mailbox', inbound_rule: 'Fax number',
  binding: 'API key', key_binding: 'API key', resource: 'Fax', installation: 'This installation', session: 'Session',
  assignment: 'Access', membership: 'Group membership', authentication: 'Sign-in',
};

const CODE_KINDS: Record<string, string> = { terminal: 'Terminal access code', pairing: 'Phone pairing code' };

function changed(entry: AuditEntry): string {
  if (!entry.target) return '-';
  if (entry.target.kind === 'capability') return CODE_KINDS[String(entry.details.kind)] ?? 'One-time code';
  const kind = TARGET_KINDS[entry.target.kind] ?? 'Item';
  if (entry.target.kind === 'installation') return kind;
  return entry.target.name ? `${kind}: ${entry.target.name}` : kind;
}

function who(entry: AuditEntry): string {
  if (!entry.actor) return 'Faxbot';
  return entry.actor.display_name || 'Someone no longer listed';
}

export default function AuditLog({ client, canListPeople }: {
  client: AdminAPIClient;
  // May this person list users (users:read)? The Who filter needs it.
  canListPeople: boolean;
}) {
  const [items, setItems] = useState<AuditEntry[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [actor, setActor] = useState('');
  const [operation, setOperation] = useState('');
  const [people, setPeople] = useState<AccessUser[]>([]);

  const load = useCallback(async (more: string | null = null) => {
    setBusy(true);
    setError(null);
    try {
      const page = await client.listAudit({ cursor: more, actor_id: actor || undefined, operation: operation || undefined });
      setItems((current) => (more ? [...current, ...page.items] : page.items));
      setCursor(page.next_cursor);
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : 'The audit log could not be read.');
    } finally {
      setBusy(false);
    }
  }, [client, actor, operation]);

  useEffect(() => { void load(); }, [load]);

  useEffect(() => {
    if (!canListPeople) return undefined;
    let live = true;
    client.listUsers({ kind: 'all', limit: 200 }).then((page) => { if (live) setPeople(page.items); }).catch(() => undefined);
    return () => { live = false; };
  }, [client, canListPeople]);

  return (
    <Box>
      <ScreenHeader title="Audit log" subtitle="Who did what in Faxbot, newest first. Entries are never changed or removed."
        onRefresh={() => void load()} busy={busy} />
      <Paper variant="outlined" sx={{ p: 2, borderRadius: 2, mb: 2 }}>
        <Stack direction={{ xs: 'column', sm: 'row' }} spacing={2}>
          {canListPeople && (
            <FormControl size="small" sx={{ minWidth: 220 }}>
              <InputLabel id="audit-who-label">Who</InputLabel>
              <Select labelId="audit-who-label" label="Who" value={actor} onChange={(event) => setActor(String(event.target.value))}>
                <MenuItem value="">Everyone</MenuItem>
                {people.map((person) => <MenuItem key={person.id} value={person.id}>{person.display_name}</MenuItem>)}
              </Select>
            </FormControl>
          )}
          <FormControl size="small" sx={{ minWidth: 260 }}>
            <InputLabel id="audit-what-label">Action</InputLabel>
            <Select labelId="audit-what-label" label="Action" value={operation} onChange={(event) => setOperation(String(event.target.value))}>
              <MenuItem value="">Everything</MenuItem>
              {Object.entries(AUDIT_ACTIONS).filter(([key]) => key !== 'providers.write')
                .sort((a, b) => a[1].localeCompare(b[1]))
                .map(([key, label]) => <MenuItem key={key} value={key}>{label}</MenuItem>)}
            </Select>
          </FormControl>
        </Stack>
      </Paper>
      {error && <Alert severity="error" sx={{ mb: 2 }}>{error}</Alert>}
      <TableContainer component={Paper} sx={{ borderRadius: 2 }}>
        <Table size="small" aria-label="Audit log">
          <TableHead>
            <TableRow>
              <TableCell>When</TableCell>
              <TableCell>Who</TableCell>
              <TableCell>Signed in with</TableCell>
              <TableCell>Action</TableCell>
              <TableCell>Changed</TableCell>
              <TableCell>Result</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {items.length === 0 && !busy ? (
              <TableRow><TableCell colSpan={6}><Typography variant="body2" color="text.secondary">Nothing matches these filters.</Typography></TableCell></TableRow>
            ) : items.map((entry) => (
              <TableRow key={entry.id}>
                <TableCell sx={{ whiteSpace: 'nowrap' }}>{formatServerTime(entry.at)}</TableCell>
                <TableCell>{who(entry)}</TableCell>
                <TableCell>{SIGNED_IN_WITH[entry.credential_kind] ?? '-'}</TableCell>
                <TableCell>{entryAction(entry)}</TableCell>
                <TableCell>{changed(entry)}</TableCell>
                <TableCell>
                  <Chip size="small" variant="outlined" label={entry.outcome === 'denied' ? 'Refused' : 'Done'}
                    color={entry.outcome === 'denied' ? 'warning' : 'default'} />
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </TableContainer>
      <Box sx={{ mt: 2, display: 'flex', alignItems: 'center', gap: 2 }}>
        {cursor && <Button variant="outlined" onClick={() => void load(cursor)} disabled={busy}>Show older entries</Button>}
        {busy && <CircularProgress size={20} />}
      </Box>
    </Box>
  );
}
