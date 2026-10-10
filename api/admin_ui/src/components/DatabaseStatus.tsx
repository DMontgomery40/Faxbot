// Administration → System health: the database Faxbot uses, whether it can reach it, and what it holds, in plain words.
import { useCallback, useEffect, useState } from 'react';
import { Alert, Box, Button, Card, CardContent, Stack, Typography } from '@mui/material';
import { Storage as StorageIcon } from '@mui/icons-material';
import type AdminAPIClient from '../api/client';
import type { DatabaseStatus as Status } from '../api/types';
import { formatServerTime } from '../api/time';

const KINDS: Record<Status['engine'], string> = {
  sqlite: 'A database file on this server',
  postgres: 'PostgreSQL',
  mysql: 'MySQL',
  unknown: 'Not recognized',
};

export function fileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} bytes`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function plural(count: number, word: string): string {
  return `${count} ${word}${count === 1 ? '' : 's'}`;
}

// "You can see 12 sent faxes, 4 received faxes and 2 API keys." (keys only for people who manage them)
export function countsSentence(counts: Status['counts']): string {
  const parts = [plural(counts.fax_jobs ?? 0, 'sent fax').replace(/faxs$/, 'faxes'),
    plural(counts.inbound_fax ?? 0, 'received fax').replace(/faxs$/, 'faxes'),
    ...(typeof counts.api_keys === 'number' ? [plural(counts.api_keys, 'API key')] : [])];
  return `You can see ${parts.slice(0, -1).join(', ')} and ${parts[parts.length - 1]}.`;
}

export default function DatabaseStatus({ client }: { client: AdminAPIClient }) {
  const [status, setStatus] = useState<Status | null>(null);
  const [failed, setFailed] = useState(false);

  const load = useCallback(async () => {
    try {
      setStatus(await client.getDatabaseStatus());
      setFailed(false);
    } catch {
      setFailed(true);
    }
  }, [client]);

  useEffect(() => { void load(); }, [load]);

  return (
    <Card sx={{ mb: 3, borderRadius: 2 }} data-testid="database-status">
      <CardContent>
        <Box display="flex" alignItems="center" justifyContent="space-between" gap={1} sx={{ mb: 1 }}>
          <Box display="flex" alignItems="center" gap={1}>
            <StorageIcon color="action" />
            <Typography variant="h6" component="h2">Database</Typography>
          </Box>
          <Button size="small" onClick={() => void load()}>Check again</Button>
        </Box>
        {failed && <Alert severity="warning">Faxbot could not say how its database is doing. Check again in a moment.</Alert>}
        {status && (
          <Stack spacing={0.5}>
            <Typography variant="body2">{`Kind: ${KINDS[status.engine] ?? KINDS.unknown}`}</Typography>
            {status.connected
              ? <Typography variant="body2" color="success.main">Faxbot can reach its database.</Typography>
              : (
                <Alert severity="error">
                  Faxbot cannot reach its database.
                  {status.error && <Typography variant="body2" sx={{ mt: 0.5 }}>{`Details: ${status.error}`}</Typography>}
                </Alert>
              )}
            {status.connected && (
              <Typography variant="body2">{countsSentence(status.counts)}</Typography>
            )}
            {status.sqlite && (status.sqlite.exists ? (
              <>
                <Typography variant="body2">
                  {`File: ${status.sqlite.path}${status.sqlite.size_bytes !== undefined ? `, ${fileSize(status.sqlite.size_bytes)}` : ''}`
                    + `${status.sqlite.modified ? `, last changed ${formatServerTime(status.sqlite.modified)}` : ''}`}
                </Typography>
                {status.sqlite.persistent_volume === false && (
                  <Alert severity="warning">This file is not in Faxbot's data folder, so it could be lost when Faxbot is reinstalled.</Alert>
                )}
              </>
            ) : (
              <Alert severity="error">{`The database file ${status.sqlite.path} was not found.`}</Alert>
            ))}
          </Stack>
        )}
      </CardContent>
    </Card>
  );
}
