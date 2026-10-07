// History: every published version of the rules, with who published it and why, the differences
// between any two, and restoring an earlier version as the draft. Versions are never changed.
import { useEffect, useState } from 'react';
import {
  Box, Button, Dialog, DialogActions, DialogContent, DialogTitle, FormControl, InputLabel, MenuItem, Paper, Select,
  Stack, Table, TableBody, TableCell, TableHead, TableRow, Typography,
} from '@mui/material';
import { formatServerTime } from '../api/time';
import { DeliveryError } from './delivery/shared';
import type { Choices, Diff, DiffChange, Revision, RevisionDetail, Rule, RulesApi, Scope, ScopeKind } from './ProviderRulesApi';
import type { Names } from './ProviderRulesText';
import { namesFor, ruleSentence } from './ProviderRulesText';
import { RulesList } from './ProviderRulesSending';

const SECTION: Record<DiffChange['section'], string> = {
  limits: 'Limit', routes: 'Routing rule', lists: 'Recipient group', labels: 'Labels', regions: 'Region', sites: 'Site',
  workflows: 'Workflow',
};
const CHANGE: Record<DiffChange['change'], string> = { added: 'Added', removed: 'Removed', changed: 'Changed', moved: 'Moved' };

function describe(value: unknown, names: Names): string {
  if (value && typeof value === 'object' && 'then' in value) return ruleSentence(value as Rule, names);
  if (Array.isArray(value)) return value.join(', ') || 'none';
  if (value && typeof value === 'object') {
    const record = value as Record<string, unknown>;
    const parts = ['numbers', 'prefixes', 'countries', 'mailboxes', 'groups', 'labels']
      .flatMap((key) => (Array.isArray(record[key]) ? (record[key] as string[]) : []));
    return parts.join(', ') || String(record.name ?? '-');
  }
  return value === null || value === undefined ? '-' : String(value);
}

export function changeText(change: DiffChange, names: Names): string {
  if (change.change === 'added') return describe(change.after, names);
  if (change.change === 'removed') return describe(change.before, names);
  if (change.change === 'moved') return 'Moved to another place in the list.';
  return `Was: ${describe(change.before, names)} Now: ${describe(change.after, names)}`;
}

export default function ProviderRulesHistory({ api, scope, scopeKind, choices, canWrite, hasDraft, onRestored }: {
  api: RulesApi; scope: Scope; scopeKind: ScopeKind; choices: Choices; canWrite: boolean; hasDraft: boolean;
  onRestored: (number: number) => void;
}) {
  const [revisions, setRevisions] = useState<Revision[] | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [shown, setShown] = useState<RevisionDetail | null>(null);
  const [from, setFrom] = useState<number | ''>('');
  const [to, setTo] = useState<number | ''>('');
  const [diff, setDiff] = useState<Diff | null>(null);
  const [restoring, setRestoring] = useState<number | null>(null);

  useEffect(() => {
    let live = true;
    api.revisions(scope).then((value) => {
      if (!live) return;
      setRevisions(value.revisions);
      if (value.revisions.length >= 2) {
        setTo(value.revisions[0].number);
        setFrom(value.revisions[1].number);
      }
    }).catch((failure) => { if (live) setError(failure); });
    return () => { live = false; };
  }, [api, scope]);

  const show = (number: number) => {
    setError(null);
    api.revision(scope, number).then(setShown).catch(setError);
  };
  const compare = () => {
    if (from === '' || to === '') return;
    setError(null);
    api.diff(scope, from, to).then(setDiff).catch(setError);
  };
  const restore = (number: number) => {
    setRestoring(null);
    setError(null);
    api.restore(scope, number).then(() => onRestored(number)).catch(setError);
  };
  const names = namesFor(shown?.document ?? null, choices);

  if (revisions === null) return <><DeliveryError error={error} /><Typography variant="body2">Loading…</Typography></>;
  return (
    <Box>
      <DeliveryError error={error} onClose={() => setError(null)} />
      {revisions.length === 0 ? (
        <Typography variant="body2" color="text.secondary">No version is published yet.</Typography>
      ) : (
        <Paper variant="outlined" sx={{ borderRadius: 2, mb: 3, overflowX: 'auto' }}>
          <Table size="small" aria-label="Published versions">
            <TableHead><TableRow><TableCell>Version</TableCell><TableCell>Published</TableCell><TableCell>By</TableCell>
              <TableCell>Note</TableCell><TableCell /></TableRow></TableHead>
            <TableBody>
              {revisions.map((revision) => (
                <TableRow key={revision.number}>
                  <TableCell>{revision.number}</TableCell>
                  <TableCell>{formatServerTime(revision.created_at)}</TableCell>
                  <TableCell>{revision.actor_name ?? '-'}</TableCell>
                  <TableCell>{revision.note ?? '-'}</TableCell>
                  <TableCell align="right" sx={{ whiteSpace: 'nowrap' }}>
                    <Button size="small" onClick={() => show(revision.number)}>Show</Button>
                    {canWrite && <Button size="small" onClick={() => setRestoring(revision.number)}>Restore as draft</Button>}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </Paper>
      )}
      {revisions.length >= 2 && (
        <Box sx={{ mb: 3 }}>
          <Typography variant="h6" component="h2" sx={{ mb: 1 }}>Compare two versions</Typography>
          <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 2 }}>
            {([['Earlier', from, setFrom], ['Later', to, setTo]] as const).map(([label, value, set]) => (
              <FormControl key={label} size="small" sx={{ minWidth: 140 }}>
                <InputLabel id={`compare-${label}`}>{label}</InputLabel>
                <Select labelId={`compare-${label}`} label={label} value={value} inputProps={{ 'aria-label': `${label} version` }}
                  onChange={(event) => set(Number(event.target.value))}>
                  {revisions.map((revision) => <MenuItem key={revision.number} value={revision.number}>Version {revision.number}</MenuItem>)}
                </Select>
              </FormControl>
            ))}
            <Button onClick={compare} disabled={from === '' || to === '' || from === to}>Compare</Button>
          </Stack>
          {diff && (diff.changes.length === 0 ? (
            <Typography variant="body2">Versions {diff.from} and {diff.to} are the same.</Typography>
          ) : (
            <Paper variant="outlined" sx={{ borderRadius: 2, overflowX: 'auto' }}>
              <Table size="small" aria-label="Differences">
                <TableHead><TableRow><TableCell>Change</TableCell><TableCell>What</TableCell><TableCell>Name</TableCell>
                  <TableCell>Details</TableCell></TableRow></TableHead>
                <TableBody>
                  {diff.changes.map((change) => (
                    <TableRow key={`${change.section}-${change.id}`}>
                      <TableCell>{CHANGE[change.change]}</TableCell>
                      <TableCell>{SECTION[change.section]}</TableCell>
                      <TableCell>{change.name}</TableCell>
                      <TableCell>{changeText(change, namesFor(null, choices))}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </Paper>
          ))}
        </Box>
      )}
      {shown && (
        <Box>
          <Typography variant="h6" component="h2">Version {shown.number}</Typography>
          <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
            Published {formatServerTime(shown.created_at)}{shown.actor_name ? ` by ${shown.actor_name}` : ''}.
            {shown.note ? ` ${shown.note}` : ''}
          </Typography>
          <RulesList document={shown.document} names={names} editable={false} scopeKind={scopeKind} />
        </Box>
      )}
      <Dialog open={restoring !== null} onClose={() => setRestoring(null)} aria-labelledby="restore-title">
        <DialogTitle id="restore-title">Restore version {restoring} as your draft?</DialogTitle>
        <DialogContent>
          <Typography variant="body2">
            {hasDraft ? 'This replaces the changes you have not published. ' : ''}
            Nothing takes effect until you check and publish the draft.
          </Typography>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setRestoring(null)}>Cancel</Button>
          <Button variant="contained" onClick={() => restoring !== null && restore(restoring)}>Restore</Button>
        </DialogActions>
      </Dialog>
    </Box>
  );
}
