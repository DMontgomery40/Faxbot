// Editing rules always works on a draft: each change is saved to the scope's draft at once, Check
// shows problems and which recent faxes would go differently, and Publish puts the draft into effect
// for new faxes. The organization's page and a mailbox's own sending rules share this.
import { useCallback, useEffect, useState } from 'react';
import {
  Alert, Box, Button, Dialog, DialogActions, DialogContent, DialogTitle, Paper, Stack, Table, TableBody, TableCell,
  TableHead, TableRow, TextField, Typography,
} from '@mui/material';
import { formatServerTime } from '../api/time';
import { DeliveryError, Notice } from './delivery/shared';
import type { CheckResult, RulesApi, RulesDocument, RulesState, Scope } from './ProviderRulesApi';
import { emptyDocument } from './ProviderRulesApi';

export interface RulesDraft {
  state: RulesState | null;
  // The document being edited: the draft's, else the published one, else an empty one.
  document: RulesDocument;
  loading: boolean;
  saving: boolean;
  error: unknown;
  notice: string | null;
  check: CheckResult | null;
  setError: (error: unknown) => void;
  setNotice: (notice: string | null) => void;
  reload: () => Promise<void>;
  save: (document: RulesDocument, notice?: string) => Promise<boolean>;
  runCheck: () => Promise<void>;
  publish: (note: string) => Promise<boolean>;
  discard: () => Promise<void>;
}

// With enabled false, nothing is read (a workflow's rules before one is chosen).
export function useRulesDraft(api: RulesApi, scope: Scope, enabled = true): RulesDraft {
  const [state, setState] = useState<RulesState | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [check, setCheck] = useState<CheckResult | null>(null);
  const scopeKey = `${scope.kind}:${scope.id ?? ''}`;

  const reload = useCallback(async () => {
    setLoading(true);
    try {
      const next = await api.rules(scope);
      setState(next);
      setCheck(next.draft?.check ?? null);
    } catch (failure) {
      setError(failure);
    } finally {
      setLoading(false);
    }
    // The scope is compared by its key, so a new object for the same scope does not reload.
  }, [api, scopeKey]);

  useEffect(() => { if (enabled) void reload(); }, [reload, enabled]);

  const document = state?.draft?.document ?? state?.active?.document ?? emptyDocument();

  const save = async (next: RulesDocument, message?: string) => {
    if (!state) return false;
    setSaving(true);
    setError(null);
    try {
      const draft = await api.saveDraft(scope, next, state.draft?.version ?? 0);
      setState({ ...state, draft });
      setCheck(draft.check);
      setNotice(message ?? null);
      return true;
    } catch (failure) {
      setError(failure);
      return false;
    } finally {
      setSaving(false);
    }
  };

  const runCheck = async () => {
    setSaving(true);
    setError(null);
    try {
      setCheck(await api.checkDraft(scope, 200));
    } catch (failure) {
      setError(failure);
    } finally {
      setSaving(false);
    }
  };

  const publish = async (note: string) => {
    if (!state?.draft) return false;
    setSaving(true);
    setError(null);
    try {
      const revision = await api.publish(scope, state.active?.number ?? null, state.draft.version, note);
      await reload();
      setNotice(`Version ${revision.number} is in effect for new faxes.`);
      return true;
    } catch (failure) {
      setError(failure);
      return false;
    } finally {
      setSaving(false);
    }
  };

  const discard = async () => {
    setSaving(true);
    setError(null);
    try {
      await api.discardDraft(scope);
      await reload();
      setNotice('Your changes were thrown away. The published rules are unchanged.');
    } catch (failure) {
      setError(failure);
    } finally {
      setSaving(false);
    }
  };

  return { state, document, loading, saving, error, notice, check, setError, setNotice, reload, save, runCheck, publish,
    discard };
}

// What is in effect, and whether there are changes that are not.
export function versionSentence(state: RulesState | null): string {
  const active = state?.active;
  if (!active && state && state.scope.kind !== 'organization') {
    return `${state.scope.name} has no sending rules of its own yet, so your organization's rules decide.`;
  }
  if (!active) return 'No rules are published yet, so Faxbot sends each fax by the cheapest reliable route, as before.';
  const by = active.actor_name ? ` by ${active.actor_name}` : '';
  return `Version ${active.number} is in effect, published ${formatServerTime(active.created_at)}${by}.`;
}

export function CheckPanel({ check }: { check: CheckResult | null }) {
  if (!check) return null;
  const replay = check.replay;
  // Saving checks the rules themselves; only Check also tries recent faxes, so only Check says all is well.
  if (!replay && check.errors.length === 0 && check.warnings.length === 0) return null;
  return (
    <Paper variant="outlined" sx={{ p: 2, mb: 2, borderRadius: 2 }} data-testid="rules-check">
      {check.errors.length === 0 && check.warnings.length === 0 && (
        <Alert severity="success" sx={{ mb: replay ? 1 : 0 }}>No problems found.</Alert>
      )}
      {check.errors.length > 0 && (
        <Alert severity="error" sx={{ mb: 1 }}>
          <Typography variant="subtitle2">Fix these before you publish</Typography>
          {check.errors.map((issue, index) => <Typography key={index} variant="body2">{issue.message}</Typography>)}
        </Alert>
      )}
      {check.warnings.length > 0 && (
        <Alert severity="warning" sx={{ mb: 1 }}>
          <Typography variant="subtitle2">Worth a look</Typography>
          {check.warnings.map((issue, index) => <Typography key={index} variant="body2">{issue.message}</Typography>)}
        </Alert>
      )}
      {replay && (
        <Box>
          <Typography variant="body2" sx={{ mb: 1 }}>
            {replay.changed === 0
              ? `All of your last ${replay.checked} faxes would go the same way.`
              : `${replay.changed} of your last ${replay.checked} faxes would go differently.`}
            {replay.approximate > 0 && ` ${replay.approximate} of them were sent before rules existed, so Faxbot used `
              + "today's groups and preferences for them."}
          </Typography>
          {replay.items.length > 0 && (
            <Table size="small" aria-label="Faxes that would go differently">
              <TableHead>
                <TableRow><TableCell>To</TableCell><TableCell>Sent</TableCell><TableCell>Went by</TableCell>
                  <TableCell>Would go by</TableCell></TableRow>
              </TableHead>
              <TableBody>
                {replay.items.map((item) => (
                  <TableRow key={item.job_id}>
                    <TableCell>{item.to_number}</TableCell>
                    <TableCell>{formatServerTime(item.accepted_at)}</TableCell>
                    <TableCell>{item.before}</TableCell>
                    <TableCell>{item.after}{item.approximate ? ' (approximate)' : ''}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </Box>
      )}
    </Paper>
  );
}

function PublishDialog({ open, saving, onClose, onPublish }: {
  open: boolean; saving: boolean; onClose: () => void; onPublish: (note: string) => void;
}) {
  const [note, setNote] = useState('');
  useEffect(() => { if (open) setNote(''); }, [open]);
  return (
    <Dialog open={open} onClose={onClose} fullWidth maxWidth="sm" aria-labelledby="publish-rules-title">
      <DialogTitle id="publish-rules-title">Publish these rules</DialogTitle>
      <DialogContent>
        <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
          New faxes follow these rules. Faxes already waiting keep the rules they were accepted under.
        </Typography>
        <TextField label="What changed and why" value={note} onChange={(event) => setNote(event.target.value)}
          fullWidth autoFocus inputProps={{ maxLength: 200 }} helperText="Kept in the history with your name." />
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose}>Cancel</Button>
        <Button variant="contained" disabled={saving || note.trim() === ''} onClick={() => onPublish(note.trim())}>
          Publish
        </Button>
      </DialogActions>
    </Dialog>
  );
}

function ConfirmDialog({ open, title, text, action, onClose, onConfirm }: {
  open: boolean; title: string; text: string; action: string; onClose: () => void; onConfirm: () => void;
}) {
  return (
    <Dialog open={open} onClose={onClose} aria-labelledby="rules-confirm-title">
      <DialogTitle id="rules-confirm-title">{title}</DialogTitle>
      <DialogContent><Typography variant="body2">{text}</Typography></DialogContent>
      <DialogActions>
        <Button onClick={onClose}>Cancel</Button>
        <Button variant="contained" onClick={onConfirm}>{action}</Button>
      </DialogActions>
    </Dialog>
  );
}

// The bar above the rules: what is in effect, and Check, Publish and Discard while there is a draft.
export function DraftBar({ draft, canWrite, onApplyToWaiting }: {
  draft: RulesDraft;
  canWrite: boolean;
  // Offered once a version is published: decide faxes still waiting again by it.
  onApplyToWaiting?: () => Promise<string>;
}) {
  const [publishing, setPublishing] = useState(false);
  const [discarding, setDiscarding] = useState(false);
  const [applying, setApplying] = useState(false);
  const hasDraft = Boolean(draft.state?.draft);
  return (
    <Box sx={{ mb: 2 }}>
      <DeliveryError error={draft.error} onClose={() => draft.setError(null)} />
      <Notice message={draft.notice} onClose={() => draft.setNotice(null)} />
      <Paper variant="outlined" sx={{ p: 2, borderRadius: 2 }}>
        <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1} alignItems={{ sm: 'center' }} justifyContent="space-between">
          <Box>
            <Typography variant="body2">{versionSentence(draft.state)}</Typography>
            {hasDraft && (
              <Typography variant="body2" color="warning.main">
                You have changes that are not in effect yet.
              </Typography>
            )}
          </Box>
          {canWrite && (
            <Stack direction="row" spacing={1} flexWrap="wrap">
              {hasDraft && <Button onClick={() => void draft.runCheck()} disabled={draft.saving}>Check</Button>}
              {hasDraft && (
                <Button variant="contained" onClick={() => setPublishing(true)} disabled={draft.saving}>Publish</Button>
              )}
              {hasDraft && <Button color="inherit" onClick={() => setDiscarding(true)} disabled={draft.saving}>Discard changes</Button>}
              {!hasDraft && draft.state?.active && onApplyToWaiting && (
                <Button onClick={() => setApplying(true)} disabled={draft.saving}>Apply to waiting faxes</Button>
              )}
            </Stack>
          )}
        </Stack>
      </Paper>
      <Box sx={{ mt: 2 }}><CheckPanel check={hasDraft ? draft.check : null} /></Box>
      <PublishDialog open={publishing} saving={draft.saving} onClose={() => setPublishing(false)}
        onPublish={(note) => { void draft.publish(note).then((done) => { if (done) setPublishing(false); }); }} />
      <ConfirmDialog open={discarding} title="Discard your changes?" action="Discard changes"
        text="The draft is thrown away. The published rules stay as they are."
        onClose={() => setDiscarding(false)} onConfirm={() => { setDiscarding(false); void draft.discard(); }} />
      {onApplyToWaiting && (
        <ConfirmDialog open={applying} title="Apply the rules to waiting faxes?" action="Apply"
          text="Faxes still waiting to go are decided again by the rules in effect now. Faxes already sent, or being sent, stay as they are."
          onClose={() => setApplying(false)}
          onConfirm={() => {
            setApplying(false);
            onApplyToWaiting().then((sentence) => draft.setNotice(sentence)).catch((failure) => draft.setError(failure));
          }} />
      )}
    </Box>
  );
}
