// Partners → what happened with them beyond ordinary direct deliveries: notice faxes (with pairing by hand),
// documents sent in pieces, and fax calls that broke and were completed directly. Every status is one sentence
// from the server.
import { useCallback, useEffect, useState } from 'react';
import {
  Box, Button, FormControlLabel, Radio, RadioGroup, Stack, Table, TableBody, TableCell, TableContainer, TableHead,
  TableRow, TextField, Typography, Paper,
} from '@mui/material';
import AdminAPIClient from '../../api/client';
import type { DirectNotice, DirectNoticeCandidate, DirectRepair, DirectTransfer } from '../../api/deliveryTypes';
import { formatServerTime } from '../../api/time';
import { FormDialog, useSmallScreens } from '../access/AccessViews';
import { DeliveryError, Notice } from './shared';
import LoadFailed, { saysFailure } from '../common/LoadFailed';

const DIRECTION = { outbound: 'Sent', inbound: 'Received' } as const;
const WITHOUT = 'without';

type Row = { key: string; when: string; direction: 'outbound' | 'inbound'; partner: string | null; status: string;
  action?: React.ReactNode };

function ActivityTable({ title, rows, empty }: { title: string; rows: Row[]; empty: string }) {
  const { isMobile } = useSmallScreens();
  return (
    <Box mt={3}>
      <Typography variant="subtitle1" sx={{ mb: 1 }}>{title}</Typography>
      {rows.length === 0 ? (
        <Typography variant="body2" color="text.secondary">{empty}</Typography>
      ) : isMobile ? (
        <Stack spacing={1}>
          {rows.map((row) => (
            <Paper key={row.key} variant="outlined" sx={{ p: 1.5, borderRadius: 2 }}>
              <Typography variant="body2" color="text.secondary">
                {DIRECTION[row.direction]} · {row.partner ?? 'A partner'} · {formatServerTime(row.when)}
              </Typography>
              <Typography variant="body2">{row.status}</Typography>
              {row.action}
            </Paper>
          ))}
        </Stack>
      ) : (
        <TableContainer component={Paper} sx={{ borderRadius: 2 }}>
          <Table size="small">
            <TableHead>
              <TableRow>
                <TableCell>When</TableCell>
                <TableCell>Sent or received</TableCell>
                <TableCell>Partner</TableCell>
                <TableCell>Status</TableCell>
                <TableCell align="right" />
              </TableRow>
            </TableHead>
            <TableBody>
              {rows.map((row) => (
                <TableRow key={row.key}>
                  <TableCell>{formatServerTime(row.when)}</TableCell>
                  <TableCell>{DIRECTION[row.direction]}</TableCell>
                  <TableCell>{row.partner ?? 'A partner'}</TableCell>
                  <TableCell>{row.status}</TableCell>
                  <TableCell align="right">{row.action}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </TableContainer>
      )}
    </Box>
  );
}

export default function PartnerActivity({ client, canWrite, refresh }: {
  client: AdminAPIClient;
  canWrite: boolean;
  refresh: number;
}) {
  const [notices, setNotices] = useState<DirectNotice[]>([]);
  const [transfers, setTransfers] = useState<DirectTransfer[]>([]);
  const [repairs, setRepairs] = useState<DirectRepair[]>([]);
  const [pairing, setPairing] = useState<DirectNotice | null>(null);
  const [candidates, setCandidates] = useState<DirectNoticeCandidate[]>([]);
  const [chosen, setChosen] = useState('');
  const [code, setCode] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const load = useCallback(async () => {
    const [n, t, r] = await Promise.allSettled([
      client.listDirectNotices(), client.listDirectTransfers(), client.listDirectRepairs(),
    ]);
    if (n.status === 'fulfilled') setNotices(n.value.notices);
    if (t.status === 'fulfilled') setTransfers(t.value.transfers);
    if (r.status === 'fulfilled') setRepairs(r.value.repairs);
  }, [client]);

  useEffect(() => { void load(); }, [load, refresh]);

  const openPairing = async (item: DirectNotice) => {
    setError(null);
    setCode('');
    setChosen('');
    setCandidates([]);
    setPairing(item);
    try {
      setCandidates((await client.listDirectNoticeFaxes(item.id)).faxes);
    } catch (failure) {
      setError(failure);
    }
  };

  const pair = async () => {
    if (!pairing) return;
    setBusy(true);
    setError(null);
    try {
      const body: { code?: string; fax_id?: string } = {};
      if (code.trim()) body.code = code;
      if (chosen && chosen !== WITHOUT) body.fax_id = chosen;
      const result = await client.pairDirectNotice(pairing.id, body);
      setNotice(result.detail);
      setPairing(null);
      await load();
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(false);
    }
  };

  const digits = code.replace(/\D/g, '').length;
  const canPair = Boolean(chosen) && (chosen === WITHOUT ? digits === 0 : (digits === 0 || digits === 20));

  return (
    <Box>
      <Notice message={notice} onClose={() => setNotice(null)} />
      <ActivityTable
        title="Notice faxes"
        empty="No notice faxes yet. Turn on a notice fax for a partner whose fax intake needs a fax event."
        rows={notices.map((item) => ({
          key: item.id, when: item.created_at, direction: item.direction, partner: item.partner,
          status: `${item.status} Code ${item.code}.`,
          action: canWrite && item.direction === 'inbound' && item.state === 'waiting'
            ? <Button size="small" onClick={() => void openPairing(item)}>Pair</Button> : undefined,
        }))}
      />
      <ActivityTable
        title="Documents sent in pieces"
        empty="No documents sent in pieces yet. Large documents go to partners in pieces, so a dropped connection only resends what is missing."
        rows={transfers.map((item) => ({
          key: `${item.direction}-${item.message_id}`, when: item.created_at, direction: item.direction,
          partner: item.partner, status: item.status,
        }))}
      />
      <ActivityTable
        title="Broken calls completed directly"
        empty="No fax calls with partners have broken part way."
        rows={repairs.map((item) => ({
          key: item.id, when: item.created_at, direction: item.direction, partner: item.partner, status: item.status,
        }))}
      />

      <FormDialog open={pairing !== null} title={`Pair the document from ${pairing?.partner ?? 'the partner'}`}
        submitLabel="Pair" busy={busy} error={null} canSubmit={canPair} onSubmit={() => void pair()}
        onClose={() => setPairing(null)}>
        <DeliveryError error={error} />
        <Typography variant="body2" sx={{ mb: 1 }}>
          Choose the received fax that is its notice page. Its code is {pairing?.code}.
        </Typography>
        <RadioGroup value={chosen} onChange={(event) => setChosen(event.target.value)}>
          {candidates.map((fax) => (
            <FormControlLabel key={fax.id} value={fax.id} control={<Radio size="small" />}
              label={`From ${fax.from_number ?? 'an unknown number'}, ${fax.pages ?? 1} page${fax.pages === 1 || fax.pages === null ? '' : 's'}, ${formatServerTime(fax.received_at)}`} />
          ))}
          <FormControlLabel value={WITHOUT} control={<Radio size="small" />}
            label="File it in Received without a notice fax" />
        </RadioGroup>
        {chosen && chosen !== WITHOUT && (
          <TextField fullWidth sx={{ mt: 1 }} label="Code on the page (optional)" value={code}
            onChange={(event) => setCode(event.target.value)} inputProps={{ inputMode: 'numeric' }}
            helperText="Type the 20 digits printed on the page to check that it is the right fax." />
        )}
      </FormDialog>
    </Box>
  );
}

// Received → a fax's details: when the fax is a notice page, which document it announced.
export function ReceivedNotice({ client, faxId }: { client: AdminAPIClient; faxId: string | null | undefined }) {
  const [text, setText] = useState<string | null>(null);
  // A fax that is no notice page answers with no text; only a failed read is said.
  const [unread, setUnread] = useState(false);
  useEffect(() => {
    let live = true;
    setText(null);
    setUnread(false);
    if (!faxId) return undefined;
    client.getDirectNoticeForFax(faxId).then((value) => { if (live) setText(value.notice_text); })
      .catch((failure) => { if (live && saysFailure(failure)) setUnread(true); });
    return () => { live = false; };
  }, [client, faxId]);
  if (!text && !unread) return null;
  return (
    <Box my={1} data-testid="received-notice">
      <Typography variant="caption" color="text.secondary">Notice</Typography>
      {text ? <Typography variant="body2">{text}</Typography>
        : <LoadFailed testId="received-notice-unread"
          text="Whether this fax is a notice from a partner could not be checked. Try again." />}
    </Box>
  );
}
