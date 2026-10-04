// Acknowledgement targets and backup people: the installation target and one row per mailbox.
import { useCallback, useEffect, useState } from 'react';
import {
  Alert, Box, Button, FormControl, InputLabel, MenuItem, Paper, Select, Stack, Table, TableBody, TableCell,
  TableHead, TableRow, TextField, Typography,
} from '@mui/material';
import type AdminAPIClient from '../../api/client';
import type { WorkMailboxSetting, WorkSettings } from '../../api/types';
import { deliveryErrorMessage } from '../delivery/shared';
import { OPERATIONAL_TARGET, targetLabel } from './text';

const INHERIT = 'installation';
const NO_BACKUP = '';

function MailboxRow({ row, canWrite, onSave }: {
  row: WorkMailboxSetting;
  canWrite: boolean;
  onSave: (row: WorkMailboxSetting, hours: number | null, backup: string | null) => Promise<void>;
}) {
  const [hours, setHours] = useState(row.acknowledge_hours === null ? INHERIT : String(row.acknowledge_hours));
  const [backup, setBackup] = useState(row.backup?.id ?? NO_BACKUP);
  const [saving, setSaving] = useState(false);
  const valid = hours === INHERIT || (/^\d+$/.test(hours) && Number(hours) <= 8760);
  const changed = hours !== (row.acknowledge_hours === null ? INHERIT : String(row.acknowledge_hours))
    || backup !== (row.backup?.id ?? NO_BACKUP);
  if (!canWrite) {
    return (
      <TableRow>
        <TableCell>{row.label}</TableCell>
        <TableCell>{targetLabel(row.acknowledge_hours)}</TableCell>
        <TableCell>{row.backup?.name ?? 'None'}</TableCell>
        <TableCell />
      </TableRow>
    );
  }
  return (
    <TableRow>
      <TableCell>{row.label}</TableCell>
      <TableCell>
        <Stack direction="row" spacing={1} alignItems="center">
          <Select size="small" value={hours === INHERIT ? INHERIT : 'hours'} aria-label={`Target for ${row.label}`}
            onChange={(event) => setHours(event.target.value === INHERIT ? INHERIT : (row.acknowledge_hours ?? 24).toString())}>
            <MenuItem value={INHERIT}>Installation target</MenuItem>
            <MenuItem value="hours">Hours for this mailbox</MenuItem>
          </Select>
          {hours !== INHERIT && (
            <TextField size="small" sx={{ width: 96 }} label="Hours" value={hours} error={!valid}
              onChange={(event) => setHours(event.target.value.trim())} inputProps={{ inputMode: 'numeric' }} />
          )}
        </Stack>
      </TableCell>
      <TableCell>
        <Select size="small" displayEmpty value={backup} aria-label={`Backup for ${row.label}`}
          onChange={(event) => setBackup(String(event.target.value))}>
          <MenuItem value={NO_BACKUP}>None</MenuItem>
          {row.people.map((person) => <MenuItem key={person.id} value={person.id}>{person.name}</MenuItem>)}
        </Select>
      </TableCell>
      <TableCell align="right">
        <Button size="small" variant="contained" disabled={!changed || !valid || saving}
          onClick={async () => {
            setSaving(true);
            try {
              await onSave(row, hours === INHERIT ? null : Number(hours), backup || null);
            } finally {
              setSaving(false);
            }
          }}>Save</Button>
      </TableCell>
    </TableRow>
  );
}

export default function WorkSettingsPanel({ client, canWrite }: { client: AdminAPIClient; canWrite: boolean }) {
  const [settings, setSettings] = useState<WorkSettings | null>(null);
  const [hours, setHours] = useState('');
  const [error, setError] = useState<unknown>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const next = await client.getWorkSettings();
      setSettings(next);
      setHours(String(next.acknowledge_hours ?? 0));
      setError(null);
    } catch (failure) {
      setError(failure);
    }
  }, [client]);

  useEffect(() => { void load(); }, [load]);

  const saveInstallation = async () => {
    try {
      const current = await client.getSettings();
      const revision = current._meta?.desired_revision_id;
      if (!revision) throw new Error('Settings could not be loaded.');
      await client.updateSettings({ expected_revision_id: revision, work_acknowledge_hours: Number(hours) });
      setNotice('Saved. New documents use this target.');
      await load();
    } catch (failure) {
      setError(failure);
    }
  };

  const saveMailbox = async (row: WorkMailboxSetting, target: number | null, backup: string | null) => {
    try {
      setSettings(await client.saveWorkMailbox({ mailbox_id: row.mailbox_id, acknowledge_hours: target,
        backup_principal_id: backup, version: row.version }));
      setNotice(`Saved ${row.label}. New documents use this target.`);
      setError(null);
    } catch (failure) {
      setError(failure);
    }
  };

  const validHours = /^\d+$/.test(hours) && Number(hours) <= 8760;
  return (
    <Paper variant="outlined" sx={{ p: { xs: 2, md: 3 }, borderRadius: 2 }}>
      <Typography variant="h6" sx={{ mb: 1 }}>Acknowledgement targets</Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
        {OPERATIONAL_TARGET} The clock starts when the document arrives. A new target applies to documents that arrive after you save it.
      </Typography>
      {error ? <Alert severity="error" sx={{ mb: 2 }} onClose={() => setError(null)}>{deliveryErrorMessage(error)}</Alert> : null}
      {notice ? <Alert severity="success" sx={{ mb: 2 }} onClose={() => setNotice(null)}>{notice}</Alert> : null}
      {settings && (
        <>
          <Stack direction={{ xs: 'column', sm: 'row' }} spacing={2} alignItems={{ sm: 'center' }} sx={{ mb: 3 }}>
            <FormControl>
              <TextField label="Installation target (hours)" size="small" value={hours} disabled={!canWrite}
                error={!validHours} helperText="0 sets no target." inputProps={{ inputMode: 'numeric' }}
                onChange={(event) => setHours(event.target.value.trim())} />
            </FormControl>
            {canWrite && (
              <Button variant="contained" disabled={!validHours || Number(hours) === settings.acknowledge_hours}
                onClick={() => void saveInstallation()}>Save</Button>
            )}
          </Stack>
          <Box sx={{ overflowX: 'auto' }}>
            <Table size="small" aria-label="Mailbox targets">
              <TableHead>
                <TableRow>
                  <TableCell>Mailbox</TableCell>
                  <TableCell>Target</TableCell>
                  <TableCell>Backup person</TableCell>
                  <TableCell />
                </TableRow>
              </TableHead>
              <TableBody>
                {settings.mailboxes.length === 0 && (
                  <TableRow><TableCell colSpan={4}>No mailboxes yet. Documents outside a mailbox use the installation target.</TableCell></TableRow>
                )}
                {settings.mailboxes.map((row) => (
                  <MailboxRow key={`${row.mailbox_id}-${row.version}`} row={row} canWrite={canWrite} onSave={saveMailbox} />
                ))}
              </TableBody>
            </Table>
          </Box>
          <InputLabel sx={{ mt: 2, whiteSpace: 'normal' }}>
            A backup person takes over an item nobody acknowledged in time. Only people who can see every document in the mailbox are listed.
          </InputLabel>
        </>
      )}
    </Paper>
  );
}
