import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Alert,
  Box,
  Button,
  Card,
  CardContent,
  Checkbox,
  CircularProgress,
  Divider,
  Drawer,
  FormControlLabel,
  IconButton,
  InputAdornment,
  Paper,
  Stack,
  Tab,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  Tabs,
  TextField,
  Tooltip,
  Typography,
} from '@mui/material';
import PersonAddIcon from '@mui/icons-material/PersonAdd';
import AddLinkIcon from '@mui/icons-material/AddLink';
import PersonIcon from '@mui/icons-material/Person';
import SearchIcon from '@mui/icons-material/Search';
import InfoIcon from '@mui/icons-material/InfoOutlined';
import BlockIcon from '@mui/icons-material/Block';
import CheckIcon from '@mui/icons-material/CheckCircleOutline';
import PasswordIcon from '@mui/icons-material/Password';
import CloseIcon from '@mui/icons-material/Close';
import AdminAPIClient from '../api/client';
import type { AccessUser, AccessUserDetail, AuthMe } from '../api/types';
import { formatServerTime, isPast } from '../api/time';
import SecretDialog, { type SecretReveal } from './access/SecretDialog';
import {
  ConfirmDialog,
  EmptyState,
  ErrorBanner,
  Field,
  FormDialog,
  LoadStateView,
  PermissionChips,
  ScreenHeader,
  StatusChip,
  loadFailure,
  useSmallScreens,
  type LoadState,
} from './access/AccessViews';

type Filter = 'all' | 'user' | 'integration';
type Creating = { kind: 'user' | 'integration'; login: string; displayName: string; enabled: boolean };
type Pending = { action: 'disable' | 'reset'; userId: string };

const KIND_LABEL: Record<string, string> = { user: 'Person', integration: 'Integration', bootstrap: 'Installation key' };

export function principalStatus(user: AccessUser): { label: string; tone: 'success' | 'default' | 'warning' } {
  if (!user.enabled) return { label: 'Disabled', tone: 'default' };
  if (user.password_change_required) return { label: 'Needs new password', tone: 'warning' };
  return { label: 'Active', tone: 'success' };
}

function UserDetail({ client, userId, onClose }: { client: AdminAPIClient; userId: string; onClose: () => void }) {
  const [detail, setDetail] = useState<AccessUserDetail | null>(null);
  const [state, setState] = useState<LoadState>('loading');

  useEffect(() => {
    let current = true;
    setState('loading');
    client.getUser(userId).then((result) => {
      if (!current) return;
      setDetail(result);
      setState('ready');
    }).catch((failure) => { if (current) setState(loadFailure(failure)); });
    return () => { current = false; };
  }, [client, userId]);

  return (
    <Box sx={{ p: 3 }}>
      <Box display="flex" justifyContent="space-between" alignItems="center" mb={2}>
        <Typography variant="h5" component="h2">{detail?.display_name ?? 'Details'}</Typography>
        <IconButton aria-label="Close details" onClick={onClose}><CloseIcon /></IconButton>
      </Box>
      {state !== 'ready' || !detail ? <LoadStateView state={state} /> : (
        <Stack spacing={2.5}>
          <Box>
            <Typography variant="body2" color="text.secondary">
              {KIND_LABEL[detail.kind] ?? detail.kind}{detail.login ? ` · signs in as ${detail.login}` : ''}
            </Typography>
            <Box mt={1}><StatusChip {...principalStatus(detail)} /></Box>
          </Box>
          <Divider />
          <Box>
            <Typography variant="subtitle1" gutterBottom>Groups</Typography>
            {detail.memberships.length === 0 ? <Typography variant="body2" color="text.secondary">Not in any group.</Typography>
              : <Typography variant="body2">{detail.memberships.map((m) => m.group_name).join(', ')}</Typography>}
          </Box>
          <Box>
            <Typography variant="subtitle1" gutterBottom>Access</Typography>
            {detail.assignments.length === 0 ? <Typography variant="body2" color="text.secondary">No roles given directly.</Typography> : (
              <Stack spacing={0.5}>
                {detail.assignments.map((a) => (
                  <Typography key={a.id} variant="body2">
                    {a.role.name} on {a.resource.kind === 'installation' ? 'everything' : a.resource.name}
                  </Typography>
                ))}
              </Stack>
            )}
          </Box>
          <Box>
            <Typography variant="subtitle1" gutterBottom>API keys</Typography>
            {detail.keys.length === 0 ? <Typography variant="body2" color="text.secondary">No keys.</Typography> : (
              <Stack spacing={0.5}>
                {detail.keys.map((key) => (
                  <Typography key={key.id} variant="body2">
                    {key.name || 'Unnamed key'} · {key.revoked_at ? 'Revoked' : isPast(key.expires_at) ? 'Expired' : 'Active'}
                  </Typography>
                ))}
              </Stack>
            )}
          </Box>
          <Box>
            <Typography variant="subtitle1" gutterBottom>Can do everywhere</Typography>
            <PermissionChips permissions={detail.effective.installation} limit={40} />
          </Box>
          {detail.effective.personal.length > 0 && (
            <Box>
              <Typography variant="subtitle1" gutterBottom>Can do with their own faxes</Typography>
              <PermissionChips permissions={detail.effective.personal} limit={40} />
            </Box>
          )}
        </Stack>
      )}
    </Box>
  );
}

export default function Users({ client, me }: { client: AdminAPIClient; me: AuthMe }) {
  const { isMobile } = useSmallScreens();
  const canManage = useMemo(() => me.permissions.includes('users:manage'), [me.permissions]);

  const [filter, setFilter] = useState<Filter>('all');
  const [search, setSearch] = useState('');
  const [query, setQuery] = useState('');
  const [users, setUsers] = useState<AccessUser[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [state, setState] = useState<LoadState>('loading');
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const [creating, setCreating] = useState<Creating | null>(null);
  const [formError, setFormError] = useState<unknown>(null);
  const [pending, setPending] = useState<Pending | null>(null);
  const [pendingError, setPendingError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const [reveal, setReveal] = useState<SecretReveal | null>(null);
  const [detailId, setDetailId] = useState<string | null>(null);

  useEffect(() => {
    const timer = window.setTimeout(() => setQuery(search.trim()), 300);
    return () => window.clearTimeout(timer);
  }, [search]);

  const load = useCallback(async () => {
    setState((current) => (current === 'ready' ? current : 'loading'));
    try {
      const result = await client.listUsers({ kind: filter, q: query || undefined });
      setUsers(result.items);
      setCursor(result.next_cursor);
      setState('ready');
    } catch (failure) {
      setState(loadFailure(failure));
    }
  }, [client, filter, query]);

  useEffect(() => { void load(); }, [load]);

  const loadMore = async () => {
    if (!cursor) return;
    setLoadingMore(true);
    try {
      const result = await client.listUsers({ kind: filter, q: query || undefined, cursor });
      setUsers((current) => [...current, ...result.items]);
      setCursor(result.next_cursor);
    } catch (failure) {
      setError(failure);
    } finally {
      setLoadingMore(false);
    }
  };

  const reload = async () => {
    setError(null);
    setFormError(null);
    setPendingError(null);
    await client.me().catch(() => undefined);
    await load();
  };

  const userById = (userId: string) => users.find((user) => user.id === userId);

  const submitCreate = async () => {
    if (!creating) return;
    setBusy(true);
    setFormError(null);
    try {
      if (creating.kind === 'user') {
        const result = await client.createUser({ login: creating.login.trim(), display_name: creating.displayName.trim(), enabled: creating.enabled });
        setReveal({
          title: 'Person added',
          message: `${result.user.display_name} signs in as ${result.user.login ?? creating.login.trim()} with this temporary password and then chooses a new one. It is shown only once.`,
          label: 'Temporary password',
          secret: result.temporary_password,
        });
      } else {
        await client.createIntegration({ display_name: creating.displayName.trim(), enabled: creating.enabled });
        setNotice('Integration added. Create a key for it under Keys.');
      }
      setCreating(null);
      await load();
    } catch (failure) {
      setFormError(failure);
    } finally {
      setBusy(false);
    }
  };

  const setEnabled = async (user: AccessUser, enabled: boolean) => {
    setBusy(true);
    setError(null);
    try {
      await client.updateUser(user.id, { enabled, version: user.version });
      setPending(null);
      await load();
    } catch (failure) {
      if (pending) setPendingError(failure); else setError(failure);
    } finally {
      setBusy(false);
    }
  };

  const confirmPending = async () => {
    if (!pending) return;
    const user = userById(pending.userId);
    if (!user) return;
    if (pending.action === 'disable') {
      await setEnabled(user, false);
      return;
    }
    setBusy(true);
    setPendingError(null);
    try {
      const result = await client.resetPassword(user.id, user.version);
      setPending(null);
      setReveal({
        title: 'Password reset',
        message: `${user.display_name} signs in with this temporary password and then chooses a new one. It is shown only once.`,
        label: 'Temporary password',
        secret: result.temporary_password,
      });
      await load();
    } catch (failure) {
      setPendingError(failure);
    } finally {
      setBusy(false);
    }
  };

  const actions = (user: AccessUser) => (
    <>
      <Tooltip title="Details">
        <IconButton aria-label={`Details for ${user.display_name}`} size="small" onClick={() => setDetailId(user.id)}><InfoIcon /></IconButton>
      </Tooltip>
      {canManage && user.kind !== 'bootstrap' && (user.enabled ? (
        <Tooltip title="Disable">
          <IconButton aria-label={`Disable ${user.display_name}`} size="small" onClick={() => { setPendingError(null); setPending({ action: 'disable', userId: user.id }); }}>
            <BlockIcon />
          </IconButton>
        </Tooltip>
      ) : (
        <Tooltip title="Enable">
          <IconButton aria-label={`Enable ${user.display_name}`} size="small" disabled={busy} onClick={() => void setEnabled(user, true)}><CheckIcon /></IconButton>
        </Tooltip>
      ))}
      {canManage && user.kind === 'user' && (
        <Tooltip title="Reset password">
          <IconButton aria-label={`Reset password for ${user.display_name}`} size="small" onClick={() => { setPendingError(null); setPending({ action: 'reset', userId: user.id }); }}>
            <PasswordIcon />
          </IconButton>
        </Tooltip>
      )}
    </>
  );

  const pendingUser = pending ? userById(pending.userId) : undefined;
  const canCreate = creating ? Boolean(creating.displayName.trim() && (creating.kind === 'integration' || creating.login.trim())) : false;

  return (
    <Box>
      <ScreenHeader title="Users" subtitle="People sign in with a password. Integrations use API keys." onRefresh={() => void reload()} busy={state === 'loading'}>
        {canManage && (
          <Button variant="contained" startIcon={<PersonAddIcon />} disabled={state !== 'ready'}
            onClick={() => { setFormError(null); setCreating({ kind: 'user', login: '', displayName: '', enabled: true }); }}>
            Add person
          </Button>
        )}
        {canManage && (
          <Button variant="outlined" startIcon={<AddLinkIcon />} disabled={state !== 'ready'}
            onClick={() => { setFormError(null); setCreating({ kind: 'integration', login: '', displayName: '', enabled: true }); }}>
            Add integration
          </Button>
        )}
      </ScreenHeader>

      {notice && <Alert severity="success" sx={{ mb: 3, borderRadius: 2 }} onClose={() => setNotice(null)}>{notice}</Alert>}
      <ErrorBanner error={error} onReload={() => void reload()} onClose={() => setError(null)} />

      <Box display="flex" flexDirection={{ xs: 'column', sm: 'row' }} gap={2} mb={2} alignItems={{ sm: 'center' }}>
        <Tabs value={filter} onChange={(_, value: Filter) => setFilter(value)} sx={{ minHeight: 40 }}>
          <Tab value="all" label="All" />
          <Tab value="user" label="People" />
          <Tab value="integration" label="Integrations" />
        </Tabs>
        <TextField size="small" placeholder="Search by name" value={search} onChange={(e) => setSearch(e.target.value)}
          inputProps={{ 'aria-label': 'Search users' }}
          InputProps={{ startAdornment: <InputAdornment position="start"><SearchIcon fontSize="small" /></InputAdornment> }} />
      </Box>

      {state !== 'ready' ? <LoadStateView state={state} onRetry={() => void load()} />
        : users.length === 0 ? (
          <EmptyState icon={<PersonIcon />} title={query ? 'No matches' : 'No users yet'}
            text={query ? 'Try a different name.' : 'Add the people and integrations that use Faxbot.'} />
        ) : isMobile ? (
          <Stack spacing={2}>
            {users.map((user) => (
              <Card key={user.id} sx={{ borderRadius: 2 }}>
                <CardContent>
                  <Box display="flex" justifyContent="space-between" alignItems="center" gap={1}>
                    <Box>
                      <Typography variant="h6" fontWeight={600}>{user.display_name}</Typography>
                      <Typography variant="caption" color="text.secondary">{KIND_LABEL[user.kind] ?? user.kind}{user.login ? ` · ${user.login}` : ''}</Typography>
                    </Box>
                    <StatusChip {...principalStatus(user)} />
                  </Box>
                  <Box mt={1}>{actions(user)}</Box>
                </CardContent>
              </Card>
            ))}
          </Stack>
        ) : (
          <TableContainer component={Paper} sx={{ borderRadius: 2 }}>
            <Table>
              <TableHead>
                <TableRow>
                  <TableCell>Name</TableCell>
                  <TableCell>Type</TableCell>
                  <TableCell>Status</TableCell>
                  <TableCell>Last sign-in</TableCell>
                  <TableCell align="right">Actions</TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {users.map((user) => (
                  <TableRow key={user.id} hover>
                    <TableCell>
                      <Typography variant="body2" fontWeight={600}>{user.display_name}</Typography>
                      {user.login && <Typography variant="caption" color="text.secondary">{user.login}</Typography>}
                    </TableCell>
                    <TableCell>{KIND_LABEL[user.kind] ?? user.kind}</TableCell>
                    <TableCell><StatusChip {...principalStatus(user)} /></TableCell>
                    <TableCell>{user.kind === 'user' ? formatServerTime(user.last_login_at, 'Never') : '-'}</TableCell>
                    <TableCell align="right" sx={{ whiteSpace: 'nowrap' }}>{actions(user)}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableContainer>
        )}

      {state === 'ready' && cursor && (
        <Box textAlign="center" mt={2}>
          <Button onClick={() => void loadMore()} disabled={loadingMore} startIcon={loadingMore ? <CircularProgress size={16} /> : undefined}>
            Load more
          </Button>
        </Box>
      )}

      <FormDialog
        open={creating !== null}
        title={creating?.kind === 'integration' ? 'Add integration' : 'Add person'}
        submitLabel={creating?.kind === 'integration' ? 'Add integration' : 'Add person'}
        busy={busy}
        error={formError}
        canSubmit={canCreate}
        onSubmit={() => void submitCreate()}
        onClose={() => setCreating(null)}
        onReload={() => void reload()}
      >
        {creating?.kind === 'user' && (
          <Field label="Username" value={creating.login} onChange={(login) => setCreating({ ...creating, login })} autoFocus
            helperText="What they type to sign in." />
        )}
        {creating && (
          <Field label="Display name" value={creating.displayName} onChange={(displayName) => setCreating({ ...creating, displayName })}
            autoFocus={creating.kind === 'integration'}
            helperText={creating.kind === 'integration' ? 'For example, the scanner or app that will use a key.' : undefined} />
        )}
        {creating && (
          <FormControlLabel sx={{ mt: 1 }} label={creating.kind === 'user' ? 'Can sign in now' : 'Enabled'}
            control={<Checkbox checked={creating.enabled} onChange={(e) => setCreating({ ...creating, enabled: e.target.checked })} />} />
        )}
        {creating?.kind === 'user' && (
          <Typography variant="body2" color="text.secondary">New people start with no access. Give them a role under Access.</Typography>
        )}
      </FormDialog>

      <ConfirmDialog
        open={pending !== null}
        title={pending?.action === 'reset' ? 'Reset password?' : 'Disable this account?'}
        text={pending?.action === 'reset'
          ? `${pendingUser?.display_name ?? 'This person'} gets a temporary password and must choose a new one at next sign-in.`
          : `${pendingUser?.display_name ?? 'This account'} is signed out and its keys stop working immediately.`}
        confirmLabel={pending?.action === 'reset' ? 'Reset password' : 'Disable'}
        danger={pending?.action === 'disable'}
        busy={busy}
        error={pendingError}
        onConfirm={() => void confirmPending()}
        onCancel={() => setPending(null)}
      />

      <Drawer anchor="right" open={detailId !== null} onClose={() => setDetailId(null)}
        PaperProps={{ sx: { width: { xs: '100%', sm: 440 } } }}>
        {detailId && <UserDetail client={client} userId={detailId} onClose={() => setDetailId(null)} />}
      </Drawer>

      <SecretDialog reveal={reveal} onClose={() => setReveal(null)} />
    </Box>
  );
}
