import { useEffect, useState } from 'react';
import { Alert, Box, Button, Typography } from '@mui/material';
import type AdminAPIClient from '../api/client';
import type { AccessMailbox, AuthMe } from '../api/types';
import type { AdminDestination } from '../navigation';
import ResourceAccess from './ResourceAccess';
import MailboxSendingRules from './MailboxSendingRules';
import { rulesApiFor } from './ProviderRulesApi';
import WorkSettingsPanel from './work/WorkSettingsPanel';
import { deliveryErrorMessage } from './delivery/shared';

export default function MailboxDetail({ client, me, mailboxId, currency, onNavigate }: {
  client: AdminAPIClient; me: AuthMe; mailboxId: string; currency: string;
  onNavigate: (destination: AdminDestination) => void;
}) {
  const [mailbox, setMailbox] = useState<AccessMailbox | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<unknown>(null);
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    let live = true;
    setLoading(true); setError(null); setMailbox(null);
    client.listMailboxes().then((page) => {
      if (live) setMailbox(page.items.find((item) => item.id === mailboxId) ?? null);
    }).catch((failure) => { if (live) setError(failure); })
      .finally(() => { if (live) setLoading(false); });
    return () => { live = false; };
  }, [client, mailboxId, retry]);
  const permissions = new Set(me.permissions);
  return <Box>
    <Button href="#/delivery/mailboxes">All mailboxes</Button>
    {loading ? <Alert severity="info">Loading mailbox…</Alert>
      : error ? <Alert severity="error" action={<Button onClick={() => setRetry(retry + 1)}>Retry</Button>}>
        {deliveryErrorMessage(error)}</Alert>
      : !mailbox ? <Alert severity="warning">Mailbox not found or no longer accessible.</Alert>
      : <>
        <Typography variant="h4" component="h1">{mailbox.label}</Typography>
        <ResourceAccess client={client} me={me} section="mailboxes" mailboxId={mailboxId} onMailboxSaved={() => setRetry(retry + 1)} />
        <Box sx={{ mt: 3 }}><ResourceAccess client={client} me={me} section="numbers" mailboxId={mailboxId}
          onNavigate={onNavigate} /></Box>
        <Box sx={{ mt: 3 }}><MailboxSendingRules api={rulesApiFor(client)} mailbox={mailbox}
          canWrite={permissions.has('mailboxes:manage')} currency={currency} /></Box>
        {permissions.has('settings:read') && <Box sx={{ mt: 3 }}>
          <WorkSettingsPanel client={client} mailboxId={mailboxId} canWrite={permissions.has('settings:write')} />
        </Box>}
      </>}
  </Box>;
}
