import { useEffect, useState } from 'react';
import { FormControlLabel, Switch, Typography } from '@mui/material';
import type AdminAPIClient from '../api/client';

// The answer cap's switch on the trunk page (routing/stations.py): on a trunk billed by the minute, Faxbot hangs
// up 50 seconds after answer when no fax machine has answered. The sentence beside it comes from the server for
// both positions, so it is right before the change is saved; it says when Faxbot does not use the cap and why.

export interface AnswerCapView {
  account: string;
  label: string;
  on: boolean;
  applies: boolean;
  cap_seconds: number;
  sentence: string;
  on_sentence: string;
  off_sentence: string;
}

const FALLBACK = 'Where your carrier bills calls by the whole minute, a call no fax machine answers is billed as one '
  + 'minute instead of two.';

export default function AnswerCap({ client, account = 'sip', checked, onChange }: {
  client?: AdminAPIClient;
  // The trunk account this switch belongs to; the first trunk is 'sip'.
  account?: string;
  checked: boolean;
  onChange: (on: boolean) => void;
}) {
  const [view, setView] = useState<AnswerCapView | null>(null);
  useEffect(() => {
    if (!client) return undefined;
    let alive = true;
    client.call<{ trunks: AnswerCapView[] }>({ method: 'GET', path: '/routing/stations/answer-cap' })
      .then((found) => {
        if (alive) setView((found?.trunks ?? []).find((item) => item.account === account) ?? null);
      })
      .catch(() => { if (alive) setView(null); });
    return () => { alive = false; };
  }, [client, account]);
  const sentence = view ? (checked ? view.on_sentence : view.off_sentence) : FALLBACK;
  return (
    <div data-testid="answer-cap">
      <FormControlLabel label="Hang up when no fax machine answers within 50 seconds (recommended)"
        control={<Switch checked={checked} onChange={(event) => onChange(event.target.checked)} />} />
      <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5 }}>{sentence}</Typography>
    </div>
  );
}
