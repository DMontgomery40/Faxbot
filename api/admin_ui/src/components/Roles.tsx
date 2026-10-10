import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Box,
  Button,
  Card,
  CardContent,
  Checkbox,
  FormControlLabel,
  Stack,
  Typography,
} from '@mui/material';
import AddIcon from '@mui/icons-material/Add';
import EditIcon from '@mui/icons-material/Edit';
import BadgeIcon from '@mui/icons-material/Badge';
import AdminAPIClient from '../api/client';
import type { AccessRole, AuthMe } from '../api/types';
import {
  EmptyState,
  ErrorBanner,
  Field,
  FormDialog,
  LoadStateView,
  PermissionChips,
  PermissionPicker,
  ScreenHeader,
  StatusChip,
  loadFailure,
  useCatalogue,
  type LoadState,
} from './access/AccessViews';
import { grantable } from './access/permissions';

interface RoleDraft {
  roleId: string | null;
  name: string;
  description: string;
  enabled: boolean;
  permissions: string[];
}

const sameSet = (a: string[], b: string[]) => a.length === b.length && a.every((item) => b.includes(item));

export default function Roles({ client, me }: { client: AdminAPIClient; me: AuthMe }) {
  const catalogue = useCatalogue(client);
  const allowed = useMemo(() => grantable(me), [me]);
  const canManage = me.permissions.includes('roles:manage');

  const [roles, setRoles] = useState<AccessRole[]>([]);
  const [state, setState] = useState<LoadState>('loading');
  const [draft, setDraft] = useState<RoleDraft | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setState((current) => (current === 'ready' ? current : 'loading'));
    try {
      setRoles((await client.listRoles()).items);
      setState('ready');
    } catch (failure) {
      setState(loadFailure(failure));
    }
  }, [client]);

  useEffect(() => { void load(); }, [load]);

  const reload = async () => {
    setError(null);
    await client.me().catch(() => undefined);
    await load();
  };

  const save = async () => {
    if (!draft) return;
    setBusy(true);
    setError(null);
    try {
      const role = draft.roleId ? roles.find((r) => r.id === draft.roleId) : undefined;
      if (role) {
        await client.updateRole(role.id, {
          ...(draft.name.trim() !== role.name ? { name: draft.name.trim() } : {}),
          ...(draft.description.trim() !== (role.description ?? '') ? { description: draft.description.trim() } : {}),
          ...(!sameSet(draft.permissions, role.permissions) ? { permissions: draft.permissions } : {}),
          ...(draft.enabled !== role.enabled ? { enabled: draft.enabled } : {}),
          version: role.version,
        });
      } else {
        await client.createRole({ name: draft.name.trim(), description: draft.description.trim(), permissions: draft.permissions, enabled: draft.enabled });
      }
      setDraft(null);
      await load();
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(false);
    }
  };

  const builtIn = roles.filter((role) => role.builtin);
  const custom = roles.filter((role) => !role.builtin);

  const roleCard = (role: AccessRole) => (
    <Card key={role.id} sx={{ borderRadius: 2 }} variant="outlined">
      <CardContent>
        <Box display="flex" justifyContent="space-between" alignItems="flex-start" gap={1} mb={1}>
          <Box>
            <Typography variant="h6" fontWeight={600}>{role.name}</Typography>
            {role.description && <Typography variant="body2" color="text.secondary">{role.description}</Typography>}
          </Box>
          <Box display="flex" gap={1} alignItems="center">
            {role.builtin ? <StatusChip label="Built-in" tone="info" /> : <StatusChip label={role.enabled ? 'Active' : 'Disabled'} tone={role.enabled ? 'success' : 'default'} />}
            {!role.builtin && canManage && (
              <Button size="small" startIcon={<EditIcon />} aria-label={`Edit ${role.name}`}
                onClick={() => { setError(null); setDraft({ roleId: role.id, name: role.name, description: role.description ?? '', enabled: role.enabled, permissions: role.permissions }); }}>
                Edit
              </Button>
            )}
          </Box>
        </Box>
        <PermissionChips permissions={role.permissions} limit={role.builtin ? 8 : 40} />
      </CardContent>
    </Card>
  );

  return (
    <Box>
      <ScreenHeader title="Roles" subtitle="A role is a set of permissions. Give roles to people and groups under Who has access."
        onRefresh={() => void reload()} busy={state === 'loading'}>
        {canManage && (
          <Button variant="contained" startIcon={<AddIcon />} disabled={state !== 'ready'}
            onClick={() => { setError(null); setDraft({ roleId: null, name: '', description: '', enabled: true, permissions: [] }); }}>
            Create role
          </Button>
        )}
      </ScreenHeader>

      {!draft && <ErrorBanner error={error} onReload={() => void reload()} onClose={() => setError(null)} />}

      {state !== 'ready' ? <LoadStateView state={state} onRetry={() => void load()} /> : (
        <Stack spacing={3}>
          <Box>
            <Typography variant="h6" gutterBottom>Built-in roles</Typography>
            <Stack spacing={2}>{builtIn.map(roleCard)}</Stack>
          </Box>
          <Box>
            <Typography variant="h6" gutterBottom>Custom roles</Typography>
            {custom.length === 0
              ? <EmptyState icon={<BadgeIcon />} title="No custom roles" text="Create a role when the built-in ones do not fit." />
              : <Stack spacing={2}>{custom.map(roleCard)}</Stack>}
          </Box>
        </Stack>
      )}

      <FormDialog client={client} open={draft !== null} title={draft?.roleId ? 'Edit role' : 'Create role'} submitLabel={draft?.roleId ? 'Save' : 'Create role'}
        busy={busy} error={error} canSubmit={Boolean(draft?.name.trim() && draft.permissions.length > 0)}
        onSubmit={() => void save()} onClose={() => setDraft(null)} onReload={() => void reload()}>
        {draft && (
          <>
            <Field label="Role name" value={draft.name} onChange={(name) => setDraft({ ...draft, name })} autoFocus />
            <Field label="Description" value={draft.description} onChange={(description) => setDraft({ ...draft, description })} />
            <FormControlLabel label="Enabled" control={<Checkbox checked={draft.enabled} onChange={(e) => setDraft({ ...draft, enabled: e.target.checked })} />} />
            <Typography variant="subtitle1" sx={{ mt: 2, mb: 1 }}>Permissions</Typography>
            <PermissionPicker catalogue={catalogue} selected={draft.permissions} allowed={allowed}
              onChange={(permissions) => setDraft({ ...draft, permissions })} />
          </>
        )}
      </FormDialog>
    </Box>
  );
}
