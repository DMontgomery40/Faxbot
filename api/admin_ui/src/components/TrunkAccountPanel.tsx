// Providers → the trunk page, with more than one trunk: a trunk after the first. Its connection with the carrier
// as Asterisk reports it, Telnyx's fax over IP setting on its numbers, its settings, and a button that changes
// them (the same fields as Add an account). The first trunk keeps the full trunk page.
import { useCallback, useEffect, useState } from 'react';
import { Alert, Box, Button, Chip, Paper, Stack, Typography } from '@mui/material';
import ProviderAccountsDialog from './ProviderAccountsDialog';
import type { AccountPatch, AccountsState, ProviderAccount, RulesApi } from './ProviderRulesApi';

export interface TrunkAccountStatus {
  message: string;
  registration_text?: string | null;
  reachability_text?: string | null;
  preset_label?: string | null;
  trunk_problems?: Record<string, string>;
  carrier_notes?: string[];
  telnyx_t38?: { text: string | null; numbers: Array<{ number: string; display: string; state: string; text: string }> } | null;
}

type Call = <T>(request: { method: string; path: string; body?: unknown }) => Promise<T>;

function settingText(account: ProviderAccount): string[] {
  const lines: string[] = [];
  const settings = account.settings;
  if (settings.host) lines.push(`Server: ${settings.host}`);
  if (settings.auth) lines.push(settings.auth === 'ip' ? 'Signs in by your internet address' : 'Signs in with a user name and password');
  if (settings.caller_id) lines.push(`Caller ID: ${settings.caller_id}`);
  if (account.numbers.length) lines.push(`Receives on ${account.numbers.join(', ')}`);
  if (account.limits.at_once) lines.push(`${account.limits.at_once} ${account.limits.at_once === 1 ? 'line' : 'lines'} at once`);
  if (settings.t38 === false) lines.push('Fax over IP (T.38) off: audio fax only');
  return lines;
}

export default function TrunkAccountPanel({ api, call, accountKey }: { api: RulesApi; call: Call; accountKey: string }) {
  const [state, setState] = useState<AccountsState | null>(null);
  const [status, setStatus] = useState<TrunkAccountStatus | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);
  const [saving, setSaving] = useState(false);

  const load = useCallback(async () => {
    try {
      const [accounts, found] = await Promise.all([
        api.accounts(),
        call<TrunkAccountStatus>({ method: 'GET', path: `/admin/sip/status?account=${encodeURIComponent(accountKey)}` })
          .catch((error: unknown) => {
            const detail = (error as { detail?: string })?.detail;
            return { message: detail ?? 'This trunk is not set up yet.' } as TrunkAccountStatus;
          }),
      ]);
      setState(accounts);
      setStatus(found);
      setProblem(null);
    } catch {
      setProblem('Faxbot could not read this trunk just now. Try again in a moment.');
    }
  }, [api, call, accountKey]);
  useEffect(() => { void load(); }, [load]);

  const account = state?.accounts.find((item) => item.key === accountKey) ?? null;
  const change = async (key: string, patch: AccountPatch) => {
    if (!state) return;
    setSaving(true);
    try {
      setState(await api.updateAccount(key, patch, state.generation));
      setEditing(false);
      await load();
    } catch (error) {
      setProblem((error as { detail?: string })?.detail ?? 'Faxbot could not save this trunk.');
    } finally {
      setSaving(false);
    }
  };

  if (problem && !account) return <Alert severity="warning">{problem}</Alert>;
  if (!account || !state) return <Typography color="text.secondary">Reading this trunk…</Typography>;
  const notLoaded = status?.trunk_problems?.[accountKey];
  return (
    <Paper variant="outlined" sx={{ p: 2, borderRadius: 2 }}>
      <Stack spacing={1.5}>
        <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap">
          <Typography variant="h6">{account.label}</Typography>
          {status?.preset_label && <Chip size="small" label={status.preset_label} />}
          <Chip size="small" color={account.health.state === 'ready' ? 'success' : 'default'}
            label={account.enabled ? account.health.sentence : 'Turned off'} />
        </Stack>
        {notLoaded && <Alert severity="warning">{notLoaded}</Alert>}
        {(status?.carrier_notes ?? []).map((note) => <Alert key={note} severity="info">{note}</Alert>)}
        {problem && <Alert severity="warning">{problem}</Alert>}
        {status && <Typography>{status.message}</Typography>}
        {status?.registration_text && <Typography color="text.secondary">{status.registration_text}</Typography>}
        {status?.reachability_text && <Typography color="text.secondary">{status.reachability_text}</Typography>}
        {status?.telnyx_t38?.numbers?.filter((entry) => entry.state !== 'on').map((entry) => (
          <Alert key={entry.number} severity="info">{entry.text}</Alert>
        ))}
        <Box component="ul" sx={{ m: 0, pl: 2.5 }}>
          {settingText(account).map((line) => <li key={line}><Typography variant="body2">{line}</Typography></li>)}
        </Box>
        <Typography variant="body2" color="text.secondary">
          This trunk shares Faxbot's connection with the first trunk: your carrier sends its calls to the same
          address and ports. After you change it, select Apply and connect on the first trunk so Asterisk loads it.
        </Typography>
        <Box>
          <Button variant="outlined" onClick={() => setEditing(true)}>Change this trunk</Button>
        </Box>
      </Stack>
      <ProviderAccountsDialog open={editing} state={state} account={account} currency="USD" saving={saving}
        onClose={() => setEditing(false)} onAdd={() => undefined} onChange={(key, patch) => void change(key, patch)} />
    </Paper>
  );
}
