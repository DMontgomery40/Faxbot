// The signed-in person's own menu in the lower-left corner: name and role,
// Change password, My sessions, My API keys, Appearance and Sign out.
import { useEffect, useState } from 'react';
import {
  Avatar, Box, ButtonBase, Divider, ListItemIcon, ListItemText, ListSubheader, Menu, MenuItem, Typography,
} from '@mui/material';
import LockResetIcon from '@mui/icons-material/LockReset';
import DevicesIcon from '@mui/icons-material/Devices';
import VpnKeyIcon from '@mui/icons-material/VpnKey';
import LightModeIcon from '@mui/icons-material/LightMode';
import DarkModeIcon from '@mui/icons-material/DarkMode';
import SettingsBrightnessIcon from '@mui/icons-material/SettingsBrightness';
import CheckIcon from '@mui/icons-material/Check';
import LogoutIcon from '@mui/icons-material/Logout';
import UnfoldMoreIcon from '@mui/icons-material/UnfoldMore';
import type AdminAPIClient from '../../api/client';
import type { AccessUserDetail, AuthMe } from '../../api/types';
import type { AdminDestination } from '../../navigation';
import { useTheme as useThemeMode } from '../../theme/ThemeContext';
import { PasswordChangeDialog } from '../PasswordChange';

// The second line under the person's name: Owner, their roles, their groups,
// or what kind of sign-in this is. Empty when nothing is known.
export function roleLine(me: AuthMe, detail: AccessUserDetail | null): string {
  if (me.is_owner) return 'Owner';
  if (me.principal.kind === 'bootstrap') return 'Installation key';
  const unique = (values: string[]) => [...new Set(values.filter(Boolean))];
  const assignments = detail?.assignments ?? [];
  const installationRoles = unique(assignments.filter((a) => a.resource.kind === 'installation').map((a) => a.role.name));
  if (installationRoles.length) return installationRoles.join(', ');
  const roles = unique(assignments.map((a) => a.role.name));
  if (roles.length) return roles.join(', ');
  const groups = unique((detail?.memberships ?? []).map((m) => m.group_name));
  if (groups.length === 1) return `In the ${groups[0]} group`;
  if (groups.length) return `In the ${groups.slice(0, -1).join(', ')} and ${groups[groups.length - 1]} groups`;
  if (me.principal.kind === 'integration') return 'Connected system';
  return '';
}

function initials(name: string): string {
  const parts = name.trim().split(/\s+/).filter(Boolean);
  return (parts.length > 1 ? parts[0][0] + parts[parts.length - 1][0] : (parts[0] ?? '?').slice(0, 2)).toUpperCase();
}

const APPEARANCE: Array<{ mode: 'light' | 'dark' | 'system'; label: string; icon: React.ReactElement }> = [
  { mode: 'light', label: 'Light', icon: <LightModeIcon fontSize="small" /> },
  { mode: 'dark', label: 'Dark', icon: <DarkModeIcon fontSize="small" /> },
  { mode: 'system', label: 'Match my system', icon: <SettingsBrightnessIcon fontSize="small" /> },
];

interface UserMenuProps {
  client: AdminAPIClient;
  me: AuthMe;
  onNavigate: (destination: AdminDestination) => void;
  onSignOut: () => void;
  onIdentityChanged: () => void;
}

export default function UserMenu({ client, me, onNavigate, onSignOut, onIdentityChanged }: UserMenuProps) {
  const { mode, setMode } = useThemeMode();
  const [anchor, setAnchor] = useState<HTMLElement | null>(null);
  const [detail, setDetail] = useState<AccessUserDetail | null>(null);
  const [changingPassword, setChangingPassword] = useState(false);
  const name = me.principal.display_name;
  const canChangePassword = me.source === 'session' && me.principal.kind === 'user';
  const canSeeKeys = me.permissions.includes('keys:manage');

  // A person may always read their own account, including the roles it holds.
  useEffect(() => {
    if (me.is_owner || me.principal.kind === 'bootstrap') return;
    let current = true;
    client.getUser(me.principal.id).then((found) => { if (current) setDetail(found); }).catch(() => undefined);
    return () => { current = false; };
  }, [client, me.principal.id, me.principal.kind, me.is_owner]);

  const role = roleLine(me, detail);
  const close = () => setAnchor(null);
  const go = (destination: AdminDestination) => { close(); onNavigate(destination); };

  return (
    <>
      <ButtonBase onClick={(event) => setAnchor(event.currentTarget)} aria-haspopup="menu" aria-expanded={Boolean(anchor)}
        aria-controls={anchor ? 'user-menu' : undefined} data-testid="user-menu-button"
        sx={{ width: '100%', justifyContent: 'flex-start', gap: 1.5, px: 2, py: 1.5, borderRadius: 2, textAlign: 'left',
          '&:hover': { backgroundColor: 'action.hover' } }}>
        <Avatar sx={{ width: 34, height: 34, fontSize: '0.875rem', bgcolor: 'primary.main' }} aria-hidden>{initials(name)}</Avatar>
        <Box sx={{ minWidth: 0, flex: 1 }}>
          <Typography variant="body2" fontWeight={600} noWrap>{name}</Typography>
          {role && <Typography variant="caption" color="text.secondary" noWrap component="div" data-testid="user-role">{role}</Typography>}
        </Box>
        <UnfoldMoreIcon fontSize="small" color="action" />
      </ButtonBase>
      <Menu id="user-menu" anchorEl={anchor} open={Boolean(anchor)} onClose={close}
        anchorOrigin={{ vertical: 'top', horizontal: 'left' }} transformOrigin={{ vertical: 'bottom', horizontal: 'left' }}
        PaperProps={{ sx: { minWidth: 240 } }}>
        {canChangePassword && (
          <MenuItem onClick={() => { close(); setChangingPassword(true); }}>
            <ListItemIcon><LockResetIcon fontSize="small" /></ListItemIcon>
            <ListItemText>Change password</ListItemText>
          </MenuItem>
        )}
        <MenuItem onClick={() => go('access/sessions')}>
          <ListItemIcon><DevicesIcon fontSize="small" /></ListItemIcon>
          <ListItemText>My sessions</ListItemText>
        </MenuItem>
        {canSeeKeys && (
          <MenuItem onClick={() => go('access/keys?mine=1')}>
            <ListItemIcon><VpnKeyIcon fontSize="small" /></ListItemIcon>
            <ListItemText>My API keys</ListItemText>
          </MenuItem>
        )}
        <Divider />
        <ListSubheader sx={{ lineHeight: '32px', backgroundColor: 'transparent' }}>Appearance</ListSubheader>
        {APPEARANCE.map((option) => (
          <MenuItem key={option.mode} role="menuitemradio" aria-checked={mode === option.mode} selected={mode === option.mode}
            onClick={() => setMode(option.mode)}>
            <ListItemIcon>{option.icon}</ListItemIcon>
            <ListItemText>{option.label}</ListItemText>
            {mode === option.mode && <CheckIcon fontSize="small" sx={{ ml: 1 }} />}
          </MenuItem>
        ))}
        <Divider />
        <MenuItem onClick={() => { close(); onSignOut(); }}>
          <ListItemIcon><LogoutIcon fontSize="small" /></ListItemIcon>
          <ListItemText>Sign out</ListItemText>
        </MenuItem>
      </Menu>
      <PasswordChangeDialog client={client} open={changingPassword} onClose={() => setChangingPassword(false)}
        onChanged={onIdentityChanged} />
    </>
  );
}
