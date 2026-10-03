import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Box,
  Button,
  Card,
  CardContent,
  IconButton,
  Paper,
  Stack,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  Tooltip,
  Typography,
} from '@mui/material';
import AddIcon from '@mui/icons-material/Add';
import RotateIcon from '@mui/icons-material/Cached';
import DeleteIcon from '@mui/icons-material/Delete';
import EditIcon from '@mui/icons-material/Edit';
import ApproveIcon from '@mui/icons-material/TaskAlt';
import KeyIcon from '@mui/icons-material/VpnKey';
import AdminAPIClient from '../api/client';
import type { AccessKey, AccessUser, AuthMe } from '../api/types';
import { endOfLocalDay, formatServerTime, isPast, localDay } from '../api/time';
import SecretDialog, { type SecretReveal } from './access/SecretDialog';
import {
  ConfirmDialog,
  EmptyState,
  ErrorBanner,
  Field,
  FormDialog,
  LoadStateView,
  PermissionChips,
  PermissionPicker,
  ScreenHeader,
  SelectField,
  StatusChip,
  loadFailure,
  useCatalogue,
  useSmallScreens,
  type LoadState,
} from './access/AccessViews';
import { grantable } from './access/permissions';

interface ApiKeysProps {
  client: AdminAPIClient;
  me: AuthMe;
}

type Principal = Pick<AccessUser, 'id' | 'kind' | 'display_name' | 'version'>;

interface KeyDraft {
  principalId: string;
  name: string;
  note: string;
  expires: string;
  permissions: string[];
}

type Editor =
  | { mode: 'create' }
  | { mode: 'edit'; keyId: string }
  | { mode: 'approve'; keyId: string };

type Pending = { action: 'rotate' | 'revoke'; keyId: string };

const KIND_LABEL: Record<string, string> = { user: 'Person', integration: 'Integration', bootstrap: 'Installation key' };

function keyStatus(key: AccessKey): { label: string; tone: 'success' | 'default' | 'warning' | 'error' } {
  if (key.revoked_at) return { label: 'Revoked', tone: 'default' };
  if (key.pending_review) return { label: 'Needs review', tone: 'warning' };
  if (isPast(key.expires_at)) return { label: 'Expired', tone: 'error' };
  return { label: 'Active', tone: 'success' };
}

const emptyDraft = (permissions: string[]): KeyDraft => ({ principalId: '', name: '', note: '', expires: '', permissions });

const keyName = (key: AccessKey) => key.name || 'Unnamed key';

export default function ApiKeys({ client, me }: ApiKeysProps) {
  const { isMobile } = useSmallScreens();
  const catalogue = useCatalogue(client);
  const allowed = useMemo(() => grantable(me), [me]);
  const defaultPermissions = useMemo(() => ['fax:send', 'fax:read'].filter((p) => allowed.has(p)), [allowed]);

  const [keys, setKeys] = useState<AccessKey[]>([]);
  const [principals, setPrincipals] = useState<Principal[]>([]);
  const [installationId, setInstallationId] = useState<string | null>(null);
  const [state, setState] = useState<LoadState>('loading');
  const [error, setError] = useState<unknown>(null);

  const [editor, setEditor] = useState<Editor | null>(null);
  const [draft, setDraft] = useState<KeyDraft>(() => emptyDraft(defaultPermissions));
  const [formError, setFormError] = useState<unknown>(null);
  const [pending, setPending] = useState<Pending | null>(null);
  const [pendingError, setPendingError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const [reveal, setReveal] = useState<SecretReveal | null>(null);

  const load = useCallback(async () => {
    setState((current) => (current === 'ready' ? current : 'loading'));
    try {
      const [keyPage, userPage, installation] = await Promise.all([
        client.listKeys(),
        client.listUsers({ kind: 'all', limit: 200 }).catch(() => null),
        client.listResources('installation').catch(() => null),
      ]);
      setKeys(keyPage.items);
      const known = new Map<string, Principal>();
      if (me.principal.kind !== 'bootstrap') known.set(me.principal.id, me.principal);
      for (const user of userPage?.items ?? []) if (user.kind !== 'bootstrap') known.set(user.id, user);
      setPrincipals([...known.values()]);
      setInstallationId(installation?.items[0]?.id ?? null);
      setState('ready');
    } catch (failure) {
      setState(loadFailure(failure));
    }
  }, [client, me.principal]);

  useEffect(() => { void load(); }, [load]);

  const reload = async () => {
    setError(null);
    setFormError(null);
    setPendingError(null);
    await client.me().catch(() => undefined);
    await load();
  };

  const keyById = (keyId: string) => keys.find((key) => key.id === keyId);
  const principalById = (principalId: string) => principals.find((principal) => principal.id === principalId);

  const openCreate = () => {
    setDraft(emptyDraft(defaultPermissions));
    setFormError(null);
    setEditor({ mode: 'create' });
  };

  const openEdit = (key: AccessKey) => {
    setDraft({ principalId: key.principal.id, name: key.name ?? '', note: key.note ?? '', expires: localDay(key.expires_at), permissions: [] });
    setFormError(null);
    setEditor({ mode: 'edit', keyId: key.id });
  };

  const openApprove = (key: AccessKey) => {
    setDraft({ principalId: key.principal.kind === 'bootstrap' ? '' : key.principal.id, name: key.name ?? '', note: key.note ?? '', expires: '', permissions: defaultPermissions });
    setFormError(null);
    setEditor({ mode: 'approve', keyId: key.id });
  };

  const ceiling = () => draft.permissions.map((permission) => ({ permission, resource_id: installationId as string }));

  const submit = async () => {
    if (!editor) return;
    setBusy(true);
    setFormError(null);
    try {
      if (editor.mode === 'create') {
        const principal = principalById(draft.principalId);
        if (!principal) throw new Error('Choose who this key belongs to.');
        const result = await client.createKey({
          principal: { id: principal.id, version: principal.version },
          name: draft.name.trim(),
          note: draft.note.trim(),
          expires_at: draft.expires ? endOfLocalDay(draft.expires) : null,
          ceiling: ceiling(),
        });
        setEditor(null);
        setReveal({
          title: 'Key created',
          message: 'Copy this key now. It is shown only once.',
          label: 'API key',
          secret: result.token,
        });
      } else if (editor.mode === 'edit') {
        const key = keyById(editor.keyId);
        if (!key) throw new Error('This key no longer exists. Reload and try again.');
        const expiresAt = draft.expires ? endOfLocalDay(draft.expires) : null;
        await client.updateKey(key.id, {
          ...(draft.name.trim() !== (key.name ?? '') ? { name: draft.name.trim() } : {}),
          ...(draft.note.trim() !== (key.note ?? '') ? { note: draft.note.trim() } : {}),
          ...(draft.expires !== localDay(key.expires_at) ? { expires_at: expiresAt } : {}),
          version: key.version,
        });
        setEditor(null);
      } else {
        const key = keyById(editor.keyId);
        const principal = principalById(draft.principalId);
        if (!key || !principal) throw new Error('Choose who this key belongs to.');
        await client.approveKey(key.id, { principal: { id: principal.id, version: principal.version }, ceiling: ceiling(), version: key.version });
        setEditor(null);
      }
      await load();
    } catch (failure) {
      setFormError(failure);
    } finally {
      setBusy(false);
    }
  };

  const confirmPending = async () => {
    if (!pending) return;
    const key = keyById(pending.keyId);
    if (!key) return;
    setBusy(true);
    setPendingError(null);
    try {
      if (pending.action === 'rotate') {
        const result = await client.rotateKey(key.id, key.version);
        setPending(null);
        setReveal({
          title: 'Key replaced',
          message: `The old key for "${keyName(key)}" no longer works. Copy the new key now. It is shown only once.`,
          label: 'New API key',
          secret: result.token,
        });
      } else {
        await client.revokeKey(key.id, key.version);
        setPending(null);
      }
      await load();
    } catch (failure) {
      setPendingError(failure);
    } finally {
      setBusy(false);
    }
  };

  const principalOptions = principals.map((principal) => ({
    value: principal.id,
    label: `${principal.display_name} (${KIND_LABEL[principal.kind] ?? principal.kind})`,
  }));

  const pendingKey = pending ? keyById(pending.keyId) : undefined;
  const canSubmit = editor?.mode === 'edit'
    ? Boolean(draft.name.trim())
    : Boolean(draft.principalId && draft.permissions.length > 0 && installationId && (editor?.mode === 'approve' || draft.name.trim()));

  const actions = (key: AccessKey) => {
    if (key.revoked_at) return null;
    return (
      <>
        {key.pending_review && (
          <Tooltip title="Approve">
            <IconButton aria-label={`Approve ${keyName(key)}`} size="small" color="primary" onClick={() => openApprove(key)}><ApproveIcon /></IconButton>
          </Tooltip>
        )}
        <Tooltip title="Edit">
          <IconButton aria-label={`Edit ${keyName(key)}`} size="small" onClick={() => openEdit(key)}><EditIcon /></IconButton>
        </Tooltip>
        <Tooltip title="Replace key">
          <IconButton aria-label={`Rotate ${keyName(key)}`} size="small" onClick={() => { setPendingError(null); setPending({ action: 'rotate', keyId: key.id }); }}>
            <RotateIcon />
          </IconButton>
        </Tooltip>
        <Tooltip title="Revoke">
          <IconButton aria-label={`Revoke ${keyName(key)}`} size="small" color="error" onClick={() => { setPendingError(null); setPending({ action: 'revoke', keyId: key.id }); }}>
            <DeleteIcon />
          </IconButton>
        </Tooltip>
      </>
    );
  };

  const permissionsOf = (key: AccessKey) => [...new Set(key.ceiling.map((entry) => entry.permission))];

  return (
    <Box>
      <ScreenHeader title="API Keys" subtitle="Keys let apps, scanners and phones use Faxbot." onRefresh={() => void reload()} busy={state === 'loading'}>
        <Button variant="contained" startIcon={<AddIcon />} onClick={openCreate} disabled={state !== 'ready'}>
          Create key
        </Button>
      </ScreenHeader>

      <ErrorBanner error={error} onReload={() => void reload()} onClose={() => setError(null)} />

      {state !== 'ready' ? <LoadStateView state={state} onRetry={() => void load()} />
        : keys.length === 0 ? (
          <EmptyState icon={<KeyIcon />} title="No API keys" text="Create a key for an app, scanner or phone that sends faxes."
            action={<Button variant="contained" startIcon={<AddIcon />} onClick={openCreate} sx={{ borderRadius: 2 }}>Create key</Button>} />
        ) : isMobile ? (
          <Stack spacing={2}>
            {keys.map((key) => {
              const status = keyStatus(key);
              return (
                <Card key={key.id} sx={{ borderRadius: 2 }}>
                  <CardContent>
                    <Stack spacing={1.5}>
                      <Box display="flex" justifyContent="space-between" alignItems="center" gap={1}>
                        <Typography variant="h6" fontWeight={600}>{keyName(key)}</Typography>
                        <StatusChip label={status.label} tone={status.tone} />
                      </Box>
                      <Typography variant="body2">Belongs to {key.principal.display_name}</Typography>
                      <Typography variant="body2" color="text.secondary">Expires: {formatServerTime(key.expires_at, 'Never')}</Typography>
                      <PermissionChips permissions={permissionsOf(key)} />
                      <Box>{actions(key)}</Box>
                    </Stack>
                  </CardContent>
                </Card>
              );
            })}
          </Stack>
        ) : (
          <TableContainer component={Paper} sx={{ borderRadius: 2 }}>
            <Table>
              <TableHead>
                <TableRow>
                  <TableCell>Name</TableCell>
                  <TableCell>Belongs to</TableCell>
                  <TableCell>Permissions</TableCell>
                  <TableCell>Expires</TableCell>
                  <TableCell>Last used</TableCell>
                  <TableCell>Status</TableCell>
                  <TableCell align="right">Actions</TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {keys.map((key) => {
                  const status = keyStatus(key);
                  return (
                    <TableRow key={key.id} hover>
                      <TableCell>
                        <Typography variant="body2" fontWeight={600}>{keyName(key)}</Typography>
                        {key.note && <Typography variant="caption" color="text.secondary">{key.note}</Typography>}
                      </TableCell>
                      <TableCell>
                        <Typography variant="body2">{key.principal.display_name}</Typography>
                        <Typography variant="caption" color="text.secondary">{KIND_LABEL[key.principal.kind] ?? key.principal.kind}</Typography>
                      </TableCell>
                      <TableCell><PermissionChips permissions={permissionsOf(key)} limit={4} /></TableCell>
                      <TableCell>{formatServerTime(key.expires_at, 'Never')}</TableCell>
                      <TableCell>{formatServerTime(key.last_used_at, 'Never')}</TableCell>
                      <TableCell><StatusChip label={status.label} tone={status.tone} /></TableCell>
                      <TableCell align="right" sx={{ whiteSpace: 'nowrap' }}>{actions(key)}</TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          </TableContainer>
        )}

      <FormDialog
        open={editor !== null}
        title={editor?.mode === 'edit' ? 'Edit key' : editor?.mode === 'approve' ? 'Approve key' : 'Create key'}
        submitLabel={editor?.mode === 'edit' ? 'Save' : editor?.mode === 'approve' ? 'Approve' : 'Create key'}
        busy={busy}
        error={formError}
        canSubmit={canSubmit}
        onSubmit={() => void submit()}
        onClose={() => setEditor(null)}
        onReload={() => void reload()}
      >
        {editor?.mode === 'approve' && (
          <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
            This older key could do anything. Choose who it belongs to and what it may do.
          </Typography>
        )}
        {editor?.mode !== 'edit' && (
          <SelectField label="Belongs to" value={draft.principalId} onChange={(principalId) => setDraft({ ...draft, principalId })}
            options={principalOptions} helperText="A person or an integration. The key can never do more than its owner." />
        )}
        {editor?.mode !== 'approve' && (
          <>
            <Field label="Name" value={draft.name} onChange={(name) => setDraft({ ...draft, name })} autoFocus={editor?.mode === 'edit'} />
            <Field label="Note" value={draft.note} onChange={(note) => setDraft({ ...draft, note })} />
            <Field label="Expires" type="date" value={draft.expires} onChange={(expires) => setDraft({ ...draft, expires })}
              helperText="Leave empty for a key that does not expire." />
          </>
        )}
        {editor?.mode !== 'edit' && (
          <Box sx={{ mt: 2 }}>
            <Typography variant="subtitle1" sx={{ mb: 1 }}>What this key may do</Typography>
            {!installationId && <Typography color="error" variant="body2">Keys cannot be created on this server yet.</Typography>}
            <PermissionPicker catalogue={catalogue} selected={draft.permissions} allowed={allowed}
              onChange={(permissions) => setDraft({ ...draft, permissions })} />
          </Box>
        )}
      </FormDialog>

      <ConfirmDialog
        open={pending !== null}
        title={pending?.action === 'rotate' ? 'Replace this key?' : 'Revoke this key?'}
        text={pending?.action === 'rotate'
          ? `The current key for "${pendingKey ? keyName(pendingKey) : 'this key'}" stops working immediately and a new one is shown once.`
          : `"${pendingKey ? keyName(pendingKey) : 'This key'}" stops working immediately. This cannot be undone.`}
        confirmLabel={pending?.action === 'rotate' ? 'Replace key' : 'Revoke'}
        danger={pending?.action === 'revoke'}
        busy={busy}
        error={pendingError}
        onConfirm={() => void confirmPending()}
        onCancel={() => setPending(null)}
        onReload={() => void reload()}
      />

      <SecretDialog reveal={reveal} onClose={() => setReveal(null)} />
    </Box>
  );
}
