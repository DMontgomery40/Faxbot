// Shared building blocks for the access-management screens (Users, Groups,
// Roles, Access, Sessions, Keys). They follow the existing list pattern:
// header row, dismissible Alert in Fade, table on desktop and cards on mobile.
import React, { useEffect, useState } from 'react';
import {
  Alert,
  Box,
  Button,
  Checkbox,
  Chip,
  CircularProgress,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  Fade,
  FormControlLabel,
  FormGroup,
  Paper,
  TextField,
  Typography,
  useMediaQuery,
  useTheme,
} from '@mui/material';
import RefreshIcon from '@mui/icons-material/Refresh';
import AdminAPIClient, { AdminAPIError, accessErrorMessage, isConflict, isNotAvailable } from '../../api/client';
import type { PermissionInfo } from '../../api/types';
import { FALLBACK_CATALOGUE, GROUP_LABELS, GROUP_ORDER, permissionLabel } from './permissions';

export type LoadState = 'loading' | 'ready' | 'denied' | 'unavailable' | 'error';

export function loadFailure(error: unknown): LoadState {
  if (error instanceof AdminAPIError && error.status === 403) return 'denied';
  if (isNotAvailable(error)) return 'unavailable';
  return 'error';
}

export function useSmallScreens() {
  const theme = useTheme();
  return {
    isMobile: useMediaQuery(theme.breakpoints.down('md')),
    isSmallMobile: useMediaQuery(theme.breakpoints.down('sm')),
  };
}

export function ScreenHeader({ title, subtitle, onRefresh, busy, children }: {
  title: string;
  subtitle?: string;
  onRefresh?: () => void;
  busy?: boolean;
  children?: React.ReactNode;
}) {
  const { isSmallMobile } = useSmallScreens();
  const buttonSx = { borderRadius: 2, minHeight: isSmallMobile ? 40 : 42 };
  return (
    <Box display="flex" justifyContent="space-between" alignItems={{ xs: 'flex-start', sm: 'center' }}
      flexDirection={{ xs: 'column', sm: 'row' }} gap={2} mb={3}>
      <Box>
        <Typography variant="h4" component="h1">{title}</Typography>
        {subtitle && <Typography variant="body2" color="text.secondary">{subtitle}</Typography>}
      </Box>
      <Box display="flex" gap={1} flexWrap="wrap">
        {onRefresh && (
          <Button variant="outlined" startIcon={<RefreshIcon />} onClick={onRefresh} disabled={busy}
            size={isSmallMobile ? 'medium' : 'large'} sx={buttonSx}>
            Refresh
          </Button>
        )}
        {React.Children.map(children, (child) => React.isValidElement(child)
          ? React.cloneElement(child as React.ReactElement<{ size?: string; sx?: object }>, { size: isSmallMobile ? 'medium' : 'large', sx: buttonSx })
          : child)}
      </Box>
    </Box>
  );
}

// A failure message; a policy conflict offers Reload and keeps any draft.
export function ErrorBanner({ error, onReload, onClose }: { error: unknown; onReload?: () => void; onClose?: () => void }) {
  if (!error) return null;
  const conflict = isConflict(error);
  return (
    <Fade in>
      <Alert
        severity={conflict ? 'warning' : 'error'}
        sx={{ mb: 3, borderRadius: 2 }}
        onClose={onClose}
        action={conflict && onReload ? <Button color="inherit" size="small" onClick={onReload}>Reload</Button> : undefined}
      >
        {accessErrorMessage(error)}
      </Alert>
    </Fade>
  );
}

const STATE_TEXT: Record<Exclude<LoadState, 'ready' | 'loading'>, string> = {
  denied: 'You do not have permission to view this.',
  unavailable: 'This is not available on this server yet.',
  error: 'Could not load this. Try again.',
};

export function LoadStateView({ state, onRetry }: { state: LoadState; onRetry?: () => void }) {
  if (state === 'ready') return null;
  if (state === 'loading') {
    return <Box display="flex" justifyContent="center" py={4}><CircularProgress aria-label="Loading" /></Box>;
  }
  return (
    <Alert severity={state === 'error' ? 'error' : 'info'} sx={{ borderRadius: 2 }}
      action={state === 'error' && onRetry ? <Button color="inherit" size="small" onClick={onRetry}>Try again</Button> : undefined}>
      {STATE_TEXT[state]}
    </Alert>
  );
}

export function EmptyState({ icon, title, text, action }: { icon: React.ReactNode; title: string; text: string; action?: React.ReactNode }) {
  return (
    <Fade in>
      <Paper sx={{ p: 4, textAlign: 'center', borderRadius: 2 }}>
        <Box sx={{ color: 'text.secondary', mb: 2, '& svg': { fontSize: 48 } }}>{icon}</Box>
        <Typography variant="h6" gutterBottom>{title}</Typography>
        <Typography color="text.secondary" sx={{ mb: action ? 3 : 0 }}>{text}</Typography>
        {action}
      </Paper>
    </Fade>
  );
}

// An access change starts when its dialog opens: read the current policy
// version then, so the change is refused only if someone else edits access
// while the dialog is open.
export function usePolicyRefresh(client: AdminAPIClient | undefined, open: boolean) {
  useEffect(() => {
    if (open && client) void client.refreshPolicy();
  }, [client, open]);
}

export function ConfirmDialog({ open, title, text, confirmLabel, danger, busy, error, onConfirm, onCancel, onReload, client }: {
  open: boolean;
  title: string;
  text: string;
  confirmLabel: string;
  danger?: boolean;
  busy?: boolean;
  error?: unknown;
  onConfirm: () => void;
  onCancel: () => void;
  onReload?: () => void;
  // Pass for access changes; the policy version is refreshed on open.
  client?: AdminAPIClient;
}) {
  const { isSmallMobile } = useSmallScreens();
  usePolicyRefresh(client, open);
  return (
    <Dialog open={open} onClose={() => !busy && onCancel()} maxWidth="xs" fullWidth fullScreen={isSmallMobile}>
      <DialogTitle>{title}</DialogTitle>
      <DialogContent>
        {error ? <ErrorBanner error={error} onReload={onReload} /> : null}
        <Typography>{text}</Typography>
      </DialogContent>
      <DialogActions sx={{ px: 3, pb: 3 }}>
        <Button onClick={onCancel} disabled={busy}>Cancel</Button>
        <Button variant="contained" color={danger ? 'error' : 'primary'} onClick={onConfirm} disabled={busy}>
          {confirmLabel}
        </Button>
      </DialogActions>
    </Dialog>
  );
}

// A form dialog. Errors show inside it so the typed draft stays in place.
export function FormDialog({ open, title, submitLabel, busy, error, canSubmit, onSubmit, onClose, onReload, children, client }: {
  open: boolean;
  title: string;
  submitLabel: string;
  busy?: boolean;
  error?: unknown;
  canSubmit: boolean;
  onSubmit: () => void;
  onClose: () => void;
  onReload?: () => void;
  children: React.ReactNode;
  // Pass for access changes; the policy version is refreshed on open.
  client?: AdminAPIClient;
}) {
  const { isSmallMobile } = useSmallScreens();
  usePolicyRefresh(client, open);
  return (
    <Dialog open={open} onClose={() => !busy && onClose()} maxWidth="sm" fullWidth fullScreen={isSmallMobile}>
      <DialogTitle>{title}</DialogTitle>
      <DialogContent>
        <Box sx={{ pt: 1 }}>
          {error ? <ErrorBanner error={error} onReload={onReload} /> : null}
          {children}
        </Box>
      </DialogContent>
      <DialogActions sx={{ px: 3, pb: 3 }}>
        <Button onClick={onClose} disabled={busy} sx={{ borderRadius: 2 }}>Cancel</Button>
        <Button variant="contained" onClick={onSubmit} disabled={busy || !canSubmit} sx={{ borderRadius: 2 }}>
          {submitLabel}
        </Button>
      </DialogActions>
    </Dialog>
  );
}

export function Field({ label, value, onChange, ...rest }: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  helperText?: string;
  placeholder?: string;
  type?: string;
  multiline?: boolean;
  required?: boolean;
  autoFocus?: boolean;
  disabled?: boolean;
}) {
  return (
    <TextField fullWidth margin="normal" label={label} value={value} onChange={(e) => onChange(e.target.value)}
      InputLabelProps={rest.type === 'date' ? { shrink: true } : undefined} {...rest} />
  );
}

export function SelectField({ label, value, onChange, options, helperText, disabled }: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  options: Array<{ value: string; label: string }>;
  helperText?: string;
  disabled?: boolean;
}) {
  return (
    <TextField select fullWidth margin="normal" label={label} value={value} onChange={(e) => onChange(e.target.value)}
      SelectProps={{ native: true }} InputLabelProps={{ shrink: true }} helperText={helperText} disabled={disabled}>
      <option value="">Choose…</option>
      {options.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
    </TextField>
  );
}

export function StatusChip({ label, tone }: { label: string; tone: 'success' | 'default' | 'warning' | 'error' | 'info' }) {
  return <Chip size="small" label={label} color={tone === 'default' ? undefined : tone} variant={tone === 'default' ? 'outlined' : 'filled'} />;
}

export function PermissionChips({ permissions, limit = 6 }: { permissions: string[]; limit?: number }) {
  if (permissions.length === 0) return <Typography variant="body2" color="text.secondary">None</Typography>;
  const shown = permissions.slice(0, limit);
  return (
    <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 0.5 }}>
      {shown.map((permission) => (
        <Chip key={permission} label={permissionLabel(permission)} size="small" variant="outlined" sx={{ borderRadius: 1 }} />
      ))}
      {permissions.length > limit && <Chip label={`+${permissions.length - limit} more`} size="small" sx={{ borderRadius: 1 }} />}
    </Box>
  );
}

export function useCatalogue(client: AdminAPIClient): PermissionInfo[] {
  const [items, setItems] = useState<PermissionInfo[]>(FALLBACK_CATALOGUE);
  useEffect(() => {
    let current = true;
    client.listPermissions().then((result) => {
      if (current && Array.isArray(result?.items) && result.items.length > 0) setItems(result.items);
    }).catch(() => undefined);
    return () => { current = false; };
  }, [client]);
  return items;
}

// Grouped checkboxes. Only permissions the signed-in identity may grant can
// be ticked; anything already present but not grantable stays visible.
export function PermissionPicker({ catalogue, selected, allowed, onChange }: {
  catalogue: PermissionInfo[];
  selected: string[];
  allowed: ReadonlySet<string>;
  onChange: (next: string[]) => void;
}) {
  const chosen = new Set(selected);
  const visible = catalogue.filter((item) => allowed.has(item.permission) || chosen.has(item.permission));
  if (visible.length === 0) {
    return <Alert severity="info" sx={{ borderRadius: 2 }}>You have no permissions you can give.</Alert>;
  }
  const toggle = (permission: string, on: boolean) => {
    const next = new Set(chosen);
    if (on) next.add(permission); else next.delete(permission);
    onChange(catalogue.map((item) => item.permission).filter((p) => next.has(p)));
  };
  return (
    <Box>
      {GROUP_ORDER.map((group) => {
        const items = visible.filter((item) => item.group === group);
        if (items.length === 0) return null;
        return (
          <Box key={group} sx={{ mb: 1.5 }}>
            <Typography variant="subtitle2" sx={{ mb: 0.5 }}>{GROUP_LABELS[group]}</Typography>
            <FormGroup sx={{ pl: 1 }}>
              {items.map((item) => (
                <FormControlLabel
                  key={item.permission}
                  control={<Checkbox size="small" checked={chosen.has(item.permission)} disabled={!allowed.has(item.permission)}
                    onChange={(e) => toggle(item.permission, e.target.checked)} />}
                  label={permissionLabel(item.permission)}
                />
              ))}
            </FormGroup>
          </Box>
        );
      })}
    </Box>
  );
}
