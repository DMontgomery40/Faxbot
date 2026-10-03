import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Box,
  Button,
  Checkbox,
  Divider,
  Drawer,
  FormControlLabel,
  IconButton,
  List,
  ListItem,
  ListItemText,
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
import EditIcon from '@mui/icons-material/Edit';
import PeopleIcon from '@mui/icons-material/People';
import GroupsIcon from '@mui/icons-material/Groups';
import RemoveIcon from '@mui/icons-material/PersonRemove';
import CloseIcon from '@mui/icons-material/Close';
import AdminAPIClient from '../api/client';
import type { AccessGroup, AccessGroupDetail, AccessUser, AuthMe } from '../api/types';
import {
  EmptyState,
  ErrorBanner,
  Field,
  FormDialog,
  LoadStateView,
  ScreenHeader,
  SelectField,
  StatusChip,
  loadFailure,
  usePolicyRefresh,
  type LoadState,
} from './access/AccessViews';
import { resourceLabel } from './access/permissions';

interface GroupDraft {
  groupId: string | null;
  name: string;
  description: string;
  enabled: boolean;
}

function GroupMembers({ client, groupId, canManage, onChanged, onClose }: {
  client: AdminAPIClient;
  groupId: string;
  canManage: boolean;
  onChanged: () => void;
  onClose: () => void;
}) {
  const [detail, setDetail] = useState<AccessGroupDetail | null>(null);
  const [people, setPeople] = useState<AccessUser[]>([]);
  const [state, setState] = useState<LoadState>('loading');
  const [choice, setChoice] = useState('');
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const [group, users] = await Promise.all([
        client.getGroup(groupId),
        canManage ? client.listUsers({ kind: 'all', limit: 200 }).catch(() => null) : Promise.resolve(null),
      ]);
      setDetail(group);
      setPeople((users?.items ?? []).filter((user) => user.kind !== 'bootstrap'));
      setState('ready');
    } catch (failure) {
      setState(loadFailure(failure));
    }
  }, [client, groupId, canManage]);

  useEffect(() => { void load(); }, [load]);
  // Adding and removing members happen in this panel.
  usePolicyRefresh(client, true);

  const reload = async () => {
    setError(null);
    await client.me().catch(() => undefined);
    await load();
  };

  const add = async () => {
    const person = people.find((p) => p.id === choice);
    if (!detail || !person) return;
    setBusy(true);
    setError(null);
    try {
      await client.addGroupMember(detail.id, { principal_id: person.id, principal_version: person.version, group_version: detail.version });
      setChoice('');
      await load();
      onChanged();
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(false);
    }
  };

  const remove = async (membershipId: string, membershipVersion: number) => {
    if (!detail) return;
    setBusy(true);
    setError(null);
    try {
      await client.removeGroupMember(detail.id, membershipId, { membership_version: membershipVersion, group_version: detail.version });
      await load();
      onChanged();
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(false);
    }
  };

  const memberIds = new Set(detail?.members.map((m) => m.principal_id) ?? []);
  const candidates = people.filter((p) => !memberIds.has(p.id)).map((p) => ({ value: p.id, label: p.display_name }));

  return (
    <Box sx={{ p: 3 }}>
      <Box display="flex" justifyContent="space-between" alignItems="center" mb={2}>
        <Typography variant="h5" component="h2">{detail?.name ?? 'Members'}</Typography>
        <IconButton aria-label="Close members" onClick={onClose}><CloseIcon /></IconButton>
      </Box>
      <ErrorBanner error={error} onReload={() => void reload()} onClose={() => setError(null)} />
      {state !== 'ready' || !detail ? <LoadStateView state={state} onRetry={() => void load()} /> : (
        <Stack spacing={2}>
          <Typography variant="subtitle1">Members</Typography>
          {detail.members.length === 0 ? <Typography variant="body2" color="text.secondary">No members yet.</Typography> : (
            <List dense disablePadding>
              {detail.members.map((member) => (
                <ListItem key={member.membership_id} disableGutters secondaryAction={canManage ? (
                  <Tooltip title="Remove from group">
                    <IconButton edge="end" aria-label={`Remove ${member.display_name}`} disabled={busy}
                      onClick={() => void remove(member.membership_id, member.version)}>
                      <RemoveIcon />
                    </IconButton>
                  </Tooltip>
                ) : undefined}>
                  <ListItemText primary={member.display_name} secondary={member.kind === 'integration' ? 'Integration' : 'Person'} />
                </ListItem>
              ))}
            </List>
          )}
          {canManage && (
            <Box display="flex" gap={1} alignItems="flex-end">
              <Box flex={1}>
                <SelectField label="Add a member" value={choice} onChange={setChoice} options={candidates} />
              </Box>
              <Button variant="contained" onClick={() => void add()} disabled={!choice || busy} sx={{ mb: 1, borderRadius: 2 }}>Add</Button>
            </Box>
          )}
          <Divider />
          <Typography variant="subtitle1">Access for this group</Typography>
          {detail.assignments.length === 0 ? <Typography variant="body2" color="text.secondary">No roles given to this group.</Typography> : (
            <Stack spacing={0.5}>
              {detail.assignments.map((a) => (
                <Typography key={a.id} variant="body2">{a.role.name} on {resourceLabel(a.resource).toLowerCase()}</Typography>
              ))}
            </Stack>
          )}
        </Stack>
      )}
    </Box>
  );
}

export default function Groups({ client, me }: { client: AdminAPIClient; me: AuthMe }) {
  const canManage = useMemo(() => me.permissions.includes('groups:manage'), [me.permissions]);
  const [groups, setGroups] = useState<AccessGroup[]>([]);
  const [state, setState] = useState<LoadState>('loading');
  const [draft, setDraft] = useState<GroupDraft | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const [membersOf, setMembersOf] = useState<string | null>(null);

  const load = useCallback(async () => {
    setState((current) => (current === 'ready' ? current : 'loading'));
    try {
      setGroups((await client.listGroups()).items);
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
      const group = draft.groupId ? groups.find((g) => g.id === draft.groupId) : undefined;
      if (group) {
        await client.updateGroup(group.id, {
          ...(draft.name.trim() !== group.name ? { name: draft.name.trim() } : {}),
          ...(draft.description.trim() !== (group.description ?? '') ? { description: draft.description.trim() } : {}),
          ...(draft.enabled !== group.enabled ? { enabled: draft.enabled } : {}),
          version: group.version,
        });
      } else {
        await client.createGroup({ name: draft.name.trim(), description: draft.description.trim(), enabled: draft.enabled });
      }
      setDraft(null);
      await load();
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Box>
      <ScreenHeader title="Groups" subtitle="Give a role to a group once and everyone in it gets that access."
        onRefresh={() => void reload()} busy={state === 'loading'}>
        {canManage && (
          <Button variant="contained" startIcon={<AddIcon />} disabled={state !== 'ready'}
            onClick={() => { setError(null); setDraft({ groupId: null, name: '', description: '', enabled: true }); }}>
            Create group
          </Button>
        )}
      </ScreenHeader>

      {!draft && <ErrorBanner error={error} onReload={() => void reload()} onClose={() => setError(null)} />}

      {state !== 'ready' ? <LoadStateView state={state} onRetry={() => void load()} />
        : groups.length === 0 ? <EmptyState icon={<GroupsIcon />} title="No groups yet" text="Groups make it easy to give a team the same access." />
        : (
          <TableContainer component={Paper} sx={{ borderRadius: 2 }}>
            <Table>
              <TableHead>
                <TableRow>
                  <TableCell>Name</TableCell>
                  <TableCell>Members</TableCell>
                  <TableCell>Status</TableCell>
                  <TableCell align="right">Actions</TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {groups.map((group) => (
                  <TableRow key={group.id} hover>
                    <TableCell>
                      <Typography variant="body2" fontWeight={600}>{group.name}</Typography>
                      {group.description && <Typography variant="caption" color="text.secondary">{group.description}</Typography>}
                    </TableCell>
                    <TableCell>{group.member_count}</TableCell>
                    <TableCell><StatusChip label={group.enabled ? 'Active' : 'Disabled'} tone={group.enabled ? 'success' : 'default'} /></TableCell>
                    <TableCell align="right" sx={{ whiteSpace: 'nowrap' }}>
                      <Tooltip title="Members">
                        <IconButton aria-label={`Members of ${group.name}`} size="small" onClick={() => setMembersOf(group.id)}><PeopleIcon /></IconButton>
                      </Tooltip>
                      {canManage && (
                        <Tooltip title="Edit">
                          <IconButton aria-label={`Edit ${group.name}`} size="small"
                            onClick={() => { setError(null); setDraft({ groupId: group.id, name: group.name, description: group.description ?? '', enabled: group.enabled }); }}>
                            <EditIcon />
                          </IconButton>
                        </Tooltip>
                      )}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableContainer>
        )}

      <FormDialog client={client} open={draft !== null} title={draft?.groupId ? 'Edit group' : 'Create group'} submitLabel={draft?.groupId ? 'Save' : 'Create group'}
        busy={busy} error={error} canSubmit={Boolean(draft?.name.trim())} onSubmit={() => void save()}
        onClose={() => setDraft(null)} onReload={() => void reload()}>
        {draft && (
          <>
            <Field label="Group name" value={draft.name} onChange={(name) => setDraft({ ...draft, name })} autoFocus />
            <Field label="Description" value={draft.description} onChange={(description) => setDraft({ ...draft, description })} />
            <FormControlLabel label="Enabled" control={<Checkbox checked={draft.enabled} onChange={(e) => setDraft({ ...draft, enabled: e.target.checked })} />} />
          </>
        )}
      </FormDialog>

      <Drawer anchor="right" open={membersOf !== null} onClose={() => setMembersOf(null)} PaperProps={{ sx: { width: { xs: '100%', sm: 440 } } }}>
        {membersOf && <GroupMembers client={client} groupId={membersOf} canManage={canManage} onChanged={() => void load()} onClose={() => setMembersOf(null)} />}
      </Drawer>
    </Box>
  );
}
