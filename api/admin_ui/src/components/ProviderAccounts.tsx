// Providers → In use → Accounts: every account Faxbot sends and receives with, trunks included, with
// what each does, whether it is ready, its numbers and the address to give its provider. The first
// account of each provider is set up on that provider's own page; extra accounts are added here.
import { useCallback, useEffect, useState } from 'react';
import {
  Box, Button, Chip, Dialog, DialogActions, DialogContent, DialogTitle, Paper, Stack, Switch, Table, TableBody, TableCell,
  TableHead, TableRow, Tooltip, Typography,
} from '@mui/material';
import AddIcon from '@mui/icons-material/Add';
import type { AdminDestination } from '../navigation';
import { DeliveryError, formatMoney, Notice } from './delivery/shared';
import { providerPage } from './ProvidersInUse';
import type {
  AccountHealth, AccountInput, AccountPatch, AccountsState, HealthState, ProviderAccount, RulesApi,
} from './ProviderRulesApi';
import ProviderAccountsDialog from './ProviderAccountsDialog';

export const HEALTH: Record<HealthState, { label: string; color: 'success' | 'default' | 'warning' | 'error' | 'info' }> = {
  ready: { label: 'Ready', color: 'success' },
  not_set_up: { label: 'Not set up', color: 'warning' },
  off: { label: 'Turned off', color: 'default' },
  waiting: { label: 'Waiting for the first fax', color: 'info' },
  failing: { label: 'Failing', color: 'error' },
  spending_limit: { label: 'Spending limit reached', color: 'warning' },
};

export function rolesText(account: ProviderAccount, state: AccountsState): string {
  const sends = `Sends${state.default_sending === account.key ? ' (default)' : ''}`;
  const receives = `receives${state.default_receiving === account.key ? ' (default)' : ''}`;
  if (account.sends && account.receives) return `${sends} and ${receives}`;
  if (account.sends) return sends;
  if (account.receives) return receives[0].toUpperCase() + receives.slice(1);
  return 'Neither sends nor receives';
}

export function limitsText(account: ProviderAccount): string {
  const parts: string[] = [];
  const trunk = account.provider === 'sip';
  if (account.limits.at_once) parts.push(`${account.limits.at_once} ${trunk ? 'lines' : 'faxes'} at once`);
  if (account.limits.calls_per_second) parts.push(`${account.limits.calls_per_second} calls a second`);
  if (account.limits.daily_limit) parts.push(`${formatMoney(account.limits.daily_limit)} a day`);
  return parts.join(', ') || 'No limits';
}

export default function ProviderAccounts({ api, canWrite, currency = 'USD', onNavigate }: {
  api: RulesApi; canWrite: boolean; currency?: string; onNavigate?: (destination: AdminDestination) => void;
}) {
  const [state, setState] = useState<AccountsState | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [editing, setEditing] = useState<{ account: ProviderAccount | null } | null>(null);
  const [saving, setSaving] = useState(false);
  const [health, setHealth] = useState<{ account: ProviderAccount; result: AccountHealth } | null>(null);

  const load = useCallback(() => {
    api.accounts().then(setState).catch(setError);
  }, [api]);
  useEffect(() => { load(); }, [load]);

  const write = async (action: (generation: number) => Promise<AccountsState>, message: string) => {
    if (!state) return false;
    setSaving(true);
    setError(null);
    try {
      setState(await action(state.generation));
      setNotice(message);
      return true;
    } catch (failure) {
      setError(failure);
      load();
      return false;
    } finally {
      setSaving(false);
    }
  };
  const patch = (account: ProviderAccount, change: AccountPatch, message: string) =>
    write((generation) => api.updateAccount(account.key, change, generation), message);

  if (!state) return <DeliveryError error={error} />;
  const siteName = (key: string | null) => state.sites.find((site) => site.key === key)?.name ?? '-';

  return (
    <Box sx={{ mb: 3 }} aria-label="Accounts" role="region">
      <Stack direction="row" alignItems="center" justifyContent="space-between">
        <Typography variant="h6" component="h2">Accounts</Typography>
        {canWrite && <Button startIcon={<AddIcon />} onClick={() => setEditing({ account: null })}>Add an account</Button>}
      </Stack>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
        Every account here can be used at the same time. Rules on Providers → Rules choose which one sends each fax.
      </Typography>
      <Notice message={notice} onClose={() => setNotice(null)} />
      <DeliveryError error={error} onClose={() => setError(null)} />
      <Paper variant="outlined" sx={{ borderRadius: 2, overflowX: 'auto' }}>
        <Table size="small" aria-label="Provider accounts">
          <TableHead>
            <TableRow>
              <TableCell>Account</TableCell><TableCell>Site</TableCell><TableCell>Does</TableCell><TableCell>On</TableCell>
              <TableCell>Health</TableCell><TableCell>Numbers</TableCell><TableCell>Limits</TableCell><TableCell />
            </TableRow>
          </TableHead>
          <TableBody>
            {state.accounts.map((account) => {
              const isDefault = state.default_sending === account.key;
              const health = HEALTH[account.health.state];
              return (
                <TableRow key={account.key}>
                  <TableCell>
                    <Typography variant="body2">{account.label}</Typography>
                    <Typography variant="caption" color="text.secondary">
                      {state.providers.find((kind) => kind.id === account.provider)?.label ?? account.provider}
                    </Typography>
                  </TableCell>
                  <TableCell>{siteName(account.site)}</TableCell>
                  <TableCell>{rolesText(account, state)}</TableCell>
                  <TableCell>
                    <Tooltip title={isDefault && account.enabled ? 'Choose another default sending account before turning this one off.' : ''}>
                      <span>
                        <Switch checked={account.enabled} disabled={!canWrite || saving || (isDefault && account.enabled)}
                          inputProps={{ 'aria-label': `${account.label} is on` }}
                          onChange={(event) => void patch(account, { enabled: event.target.checked }, event.target.checked
                            ? `${account.label} is on.` : `${account.label} is off. Faxes already sent by it are not affected.`)} />
                      </span>
                    </Tooltip>
                  </TableCell>
                  <TableCell>
                    <Chip size="small" label={health.label} color={health.color} variant="outlined" />
                    <Typography variant="caption" display="block" color="text.secondary">{account.health.sentence}</Typography>
                    <Button size="small" aria-label={`Health details for ${account.label}`}
                      onClick={() => api.accountHealth(account.key).then((result) => setHealth({ account, result })).catch(setError)}>
                      Details
                    </Button>
                  </TableCell>
                  <TableCell>
                    {account.numbers.join(', ') || '-'}
                    {account.webhook_address && (
                      <Typography variant="caption" display="block" color="text.secondary">
                        Give {state.providers.find((kind) => kind.id === account.provider)?.label ?? 'your provider'} this
                        address for received faxes: {account.webhook_address}
                      </Typography>
                    )}
                  </TableCell>
                  <TableCell>{limitsText(account)}</TableCell>
                  <TableCell align="right" sx={{ whiteSpace: 'nowrap' }}>
                    {account.primary
                      ? onNavigate && <Button size="small" onClick={() => onNavigate(providerPage(account.provider))}>Settings</Button>
                      : canWrite && <Button size="small" onClick={() => setEditing({ account })}>Change</Button>}
                    {canWrite && account.sends && account.enabled && !isDefault && (
                      <Button size="small" onClick={() => void patch(account, { default_sending: true },
                        `Faxbot now sends by ${account.label} unless a rule says otherwise.`)}>Make default for sending</Button>
                    )}
                    {canWrite && account.receives && account.enabled && state.default_receiving !== account.key && (
                      <Button size="small" onClick={() => void patch(account, { default_receiving: true },
                        `${account.label} is the default receiving account.`)}>Make default for receiving</Button>
                    )}
                  </TableCell>
                </TableRow>
              );
            })}
          </TableBody>
        </Table>
      </Paper>
      <Dialog open={health !== null} onClose={() => setHealth(null)} fullWidth maxWidth="sm" aria-labelledby="account-health-title">
        <DialogTitle id="account-health-title">{health?.account.label}: {health ? HEALTH[health.result.state].label : ''}</DialogTitle>
        <DialogContent>
          <Typography variant="body2" sx={{ mb: 1 }}>{health?.result.sentence}</Typography>
          {health?.result.details.map((detail) => <Typography key={detail} variant="body2" sx={{ mb: 0.5 }}>{detail}</Typography>)}
        </DialogContent>
        <DialogActions><Button onClick={() => setHealth(null)}>Close</Button></DialogActions>
      </Dialog>
      {editing && (
        <ProviderAccountsDialog open state={state} account={editing.account} currency={currency} saving={saving}
          onClose={() => setEditing(null)}
          onAdd={(input: AccountInput) => void write((generation) => api.addAccount(input, generation),
            `Account ${input.label} added.`).then((done) => { if (done) setEditing(null); })}
          onChange={(key, change) => void write((generation) => api.updateAccount(key, change, generation),
            `Account ${change.label ?? key} saved.`).then((done) => { if (done) setEditing(null); })} />
      )}
    </Box>
  );
}
