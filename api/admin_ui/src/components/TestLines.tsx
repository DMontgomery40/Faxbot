// System → Diagnostics → Public test lines (test_lines.py, research N10): send one test fax to a service whose
// operator invites test faxes, at your request only, and see what came back. The dialing guard is never bypassed:
// a line in a country Faxbot may not dial yet offers to allow that country, with your confirmation, through the
// guard's own change (Providers → In use → Where Faxbot may dial).
import { useCallback, useEffect, useState } from 'react';
import { Alert, Box, Button, Link, Paper, Stack, Typography } from '@mui/material';
import AdminAPIClient from '../api/client';
import { formatLocalDate } from '../api/time';
import { ConfirmDialog } from './access/AccessViews';
import LoadFailed, { saysFailure } from './common/LoadFailed';
import { DeliveryError } from './delivery/shared';

interface Guard {
  allowed: boolean;
  class: string | null;
  class_label: string | null;
  // 'numbers in the United Kingdom'
  class_text: string | null;
  fenced: boolean;
  sentence: string | null;
}

interface TestLine {
  id: string;
  number: string;
  country: string;
  operator: string;
  kind: 'public' | 'reply' | 'echo';
  shows: string;
  source_url: string;
  read_on: string;
  invitation: string | null;
  invitation_en: string | null;
  reply_minutes: number | null;
  max_pages: number | null;
  public_page: string | null;
  note: string | null;
  faxbeep_receipt: boolean;
  guard: Guard;
}

interface Candidate { inbound_id: string; from_number: string | null; pages: number | null; received_at_text: string }

interface TestSend {
  id: string;
  line_id: string;
  operator: string;
  number: string;
  fax_id: string;
  sent_at_text: string;
  actor_name: string | null;
  fax_state: string;
  fax_sentence: string;
  reply: { state: string; inbound_id: string | null; candidates: Candidate[]; sentence: string | null } | null;
  public_page: string | null;
  public_sentence?: string;
  receipt: string | null;
}

interface TestLinesView {
  lines: TestLine[];
  sends: TestSend[];
  reply: { caller_id: string | null; header: string | null; reaches: boolean | null; sentence: string } | null;
}

interface SendAnswer {
  sent: boolean;
  sentence: string;
  needs_allow?: Guard;
  send?: TestSend | null;
}

type Ask = { kind: 'public' | 'allow'; line: TestLine; guard?: Guard; sentence?: string };

export default function TestLines({ client }: { client: AdminAPIClient }) {
  const [view, setView] = useState<TestLinesView | null>(null);
  const [failed, setFailed] = useState<unknown>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [ask, setAsk] = useState<Ask | null>(null);
  const [receipts, setReceipts] = useState<Record<string, { sentence: string; url: string | null; list_url?: string }>>({});

  const load = useCallback(async () => {
    try {
      setView(await client.call<TestLinesView>({ method: 'GET', path: '/diagnostics/test-lines' }));
      setFailed(null);
    } catch (failure) {
      setFailed(failure);
    }
  }, [client]);

  useEffect(() => { void load(); }, [load]);

  const send = async (line: TestLine) => {
    setBusy(line.id);
    setError(null);
    setNotice(null);
    try {
      const answer = await client.call<SendAnswer>({ method: 'POST', path: `/diagnostics/test-lines/${encodeURIComponent(line.id)}/send` });
      if (!answer.sent && answer.needs_allow) {
        if (answer.needs_allow.fenced || !answer.needs_allow.class) setNotice(answer.sentence);
        else setAsk({ kind: 'allow', line, guard: answer.needs_allow, sentence: answer.sentence });
      } else {
        setNotice(answer.sentence);
      }
      await load();
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(null);
    }
  };

  const confirm = async () => {
    if (!ask) return;
    const current = ask;
    setAsk(null);
    if (current.kind === 'public') {
      await send(current.line);
      return;
    }
    setBusy(current.line.id);
    try {
      // The guard's own change, recorded as yours: Faxbot may dial this country from now on.
      await client.call({ method: 'PUT', path: `/routing/dialing/${encodeURIComponent(current.guard!.class!)}`,
        body: { state: 'allowed' } });
    } catch (failure) {
      setError(failure);
      setBusy(null);
      return;
    }
    await send(current.line);
  };

  const start = (line: TestLine) => {
    if (line.kind === 'public') setAsk({ kind: 'public', line });
    else void send(line);
  };

  const findReceipt = async (item: TestSend) => {
    setBusy(item.id);
    try {
      const found = await client.call<{ sentence: string; url: string | null; list_url?: string }>({
        method: 'GET', path: `/diagnostics/test-lines/sends/${encodeURIComponent(item.id)}/receipt` });
      setReceipts((current) => ({ ...current, [item.id]: found }));
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(null);
    }
  };

  const markReply = async (item: TestSend, inboundId: string) => {
    setBusy(item.id);
    try {
      await client.call({ method: 'POST', path: `/diagnostics/test-lines/sends/${encodeURIComponent(item.id)}/reply`,
        body: { inbound_id: inboundId } });
      await load();
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(null);
    }
  };

  if (failed) {
    return saysFailure(failed)
      ? <LoadFailed testId="test-lines-unread" text="Public test lines could not be loaded. Try again." /> : null;
  }
  if (!view) return null;
  const replySeverity = view.reply?.reaches === true ? 'success' : view.reply?.reaches === false ? 'warning' : 'info';
  return (
    <Paper variant="outlined" sx={{ px: 2, py: 1.5, mb: 3 }} data-testid="test-lines">
      <Typography variant="h6" component="h2">Public test lines</Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 1.5 }}>
        Send one test fax to a service that invites test faxes. Faxbot never sends one by itself.
      </Typography>
      {view.reply && <Alert severity={replySeverity} sx={{ mb: 1.5 }} data-testid="test-lines-reply">{view.reply.sentence}</Alert>}
      <DeliveryError error={error} onClose={() => setError(null)} />
      {notice && <Alert severity="info" sx={{ mb: 1.5 }} onClose={() => setNotice(null)}>{notice}</Alert>}
      <Stack spacing={1.5}>
        {view.lines.map((line) => (
          <Box key={line.id} data-testid={`test-line-${line.id}`}
            sx={{ display: 'flex', gap: 1.5, alignItems: 'flex-start', flexWrap: { xs: 'wrap', sm: 'nowrap' } }}>
            <Box sx={{ flex: 1, minWidth: 0 }}>
              <Typography variant="subtitle2" fontWeight={600}>{line.operator}, {line.number}</Typography>
              <Typography variant="body2" color="text.secondary">{line.shows}</Typography>
              {line.note && <Typography variant="body2" color="text.secondary">{line.note}</Typography>}
              {!line.guard.allowed && line.guard.sentence && (
                <Typography variant="body2" color="warning.main">{line.guard.sentence}</Typography>
              )}
              <Typography variant="caption" color="text.secondary">
                <Link href={line.source_url} target="_blank" rel="noopener noreferrer">The operator's invitation</Link>
                {`, read ${formatLocalDate(line.read_on)}`}
              </Typography>
            </Box>
            <Button size="small" variant="outlined" sx={{ flexShrink: 0 }} disabled={busy !== null}
              onClick={() => start(line)} aria-label={`Send a test fax to ${line.operator}, ${line.number}`}>
              Send a test fax
            </Button>
          </Box>
        ))}
      </Stack>
      {view.sends.length > 0 && (
        <Box sx={{ mt: 2 }} data-testid="test-line-results">
          <Typography variant="subtitle2" sx={{ mb: 0.5 }}>Recent test faxes</Typography>
          {view.sends.map((item) => (
            <Box key={item.id} sx={{ py: 1, borderTop: '1px solid', borderColor: 'divider' }}>
              <Typography variant="body2">
                {`${item.operator}, sent ${item.sent_at_text}${item.actor_name ? ` by ${item.actor_name}` : ''}. ${item.fax_sentence}`}
              </Typography>
              {item.reply?.sentence && <Typography variant="body2" color="text.secondary">{item.reply.sentence}</Typography>}
              {(item.reply?.candidates ?? []).map((found) => (
                <Box key={found.inbound_id} display="flex" gap={1} alignItems="center" flexWrap="wrap">
                  <Typography variant="body2" color="text.secondary">
                    {`Received ${found.received_at_text} from ${found.from_number || 'an unknown number'}.`}
                  </Typography>
                  <Button size="small" disabled={busy !== null} onClick={() => void markReply(item, found.inbound_id)}>
                    Mark as the test reply
                  </Button>
                </Box>
              ))}
              {item.public_sentence && item.public_page && (
                <Typography variant="body2" color="text.secondary">
                  {item.public_sentence}{' '}
                  <Link href={item.public_page} target="_blank" rel="noopener noreferrer">Open its page</Link>
                </Typography>
              )}
              {item.receipt === 'faxbeep' && (
                <Box display="flex" gap={1} alignItems="center" flexWrap="wrap">
                  <Button size="small" disabled={busy !== null} onClick={() => void findReceipt(item)}>Find it on Faxbeep</Button>
                  {receipts[item.id] && (
                    <Typography variant="body2" color="text.secondary">
                      {receipts[item.id].sentence}{' '}
                      {(receipts[item.id].url || receipts[item.id].list_url) && (
                        <Link href={(receipts[item.id].url || receipts[item.id].list_url)!} target="_blank"
                          rel="noopener noreferrer">{receipts[item.id].url ? 'Open your test page' : 'Open Faxbeep'}</Link>
                      )}
                    </Typography>
                  )}
                </Box>
              )}
            </Box>
          ))}
        </Box>
      )}
      <ConfirmDialog open={ask !== null}
        title={ask?.kind === 'allow' ? `Allow calls to ${ask.guard?.class_label ?? 'this country'}?` : `Send to ${ask?.line.operator ?? ''}?`}
        text={ask?.kind === 'allow'
          ? `${ask.sentence ?? ''} Allow Faxbot to dial ${ask.guard?.class_text ?? 'this country'} from now on, then send the test fax?`
          : `${ask?.line.operator ?? ''} shows every fax it receives on a public web page, including the line at the top of each page with your organization's name and reply number. Faxbot sends only its own test page.`}
        confirmLabel={ask?.kind === 'allow' ? 'Allow and send' : 'Send the test fax'}
        onConfirm={() => void confirm()} onCancel={() => setAsk(null)} />
    </Paper>
  );
}
