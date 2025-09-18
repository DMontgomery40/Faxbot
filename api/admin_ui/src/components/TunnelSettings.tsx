import { useEffect, useMemo, useState } from 'react';
import { Box, Button, Chip, Dialog, DialogActions, DialogContent, DialogTitle, Link, Paper, Stack, Typography } from '@mui/material';
import { Cloud, Security, VpnKey, VpnLock } from '@mui/icons-material';
import AdminAPIClient from '../api/client';
import type { TunnelStatus } from '../api/types';
import { ResponsiveFormSection, ResponsiveSelect, ResponsiveTextField } from './common/ResponsiveFormFields';
import { SmoothLoader, InlineLoader } from './common/SmoothLoader';

type Props = { client: AdminAPIClient; docsBase?: string; hipaaMode?: boolean };

export default function TunnelSettings({ client, docsBase, hipaaMode }: Props) {
  const [status, setStatus] = useState<TunnelStatus | null>(null);
  const [loading, setLoading] = useState<boolean>(false);
  const [saving, setSaving] = useState<boolean>(false);
  const [testing, setTesting] = useState<boolean>(false);
  const [pairDialog, setPairDialog] = useState<{ open: boolean; code?: string; expires_at?: string }>({ open: false });
  const [logsLoading, setLogsLoading] = useState<boolean>(false);
  const [logs, setLogs] = useState<string[]>([]);

  const [provider, setProvider] = useState<'none' | 'cloudflare' | 'wireguard' | 'tailscale'>('none');
  const [wg, setWg] = useState<{ endpoint?: string; server_key?: string; client_ip?: string; dns?: string }>({});
  const [ts, setTs] = useState<{ auth_key?: string; hostname?: string }>({});

  const learnMoreUrl = useMemo(() => `${docsBase || ''}/networking/tunnels`, [docsBase]);

  const fetchStatus = async () => {
    setLoading(true);
    try {
      const s = await client.getTunnelStatus();
      setStatus(s);
      setProvider(s.provider);
    } catch (e: any) {
      console.error('Tunnel status error', e);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { fetchStatus(); }, []);

  const applyConfig = async () => {
    setSaving(true);
    try {
      const payload: any = {
        enabled: provider !== 'none',
        provider,
        wireguard_endpoint: wg.endpoint,
        wireguard_server_public_key: wg.server_key,
        wireguard_client_ip: wg.client_ip,
        wireguard_dns: wg.dns,
        tailscale_auth_key: ts.auth_key,
        tailscale_hostname: ts.hostname,
      };
      const s = await client.setTunnelConfig(payload);
      setStatus(s);
    } catch (e: any) {
      console.error('Apply tunnel config failed', e);
    } finally {
      setSaving(false);
    }
  };

  const testConnectivity = async () => {
    setTesting(true);
    try {
      const res = await client.testTunnel();
      if (!res.ok) {
        alert(`Connectivity check failed: ${res.message || 'Unknown error'}`);
      } else {
        alert(`Connectivity OK${res.target ? ` (${res.target})` : ''}`);
      }
    } catch (e: any) {
      alert(e?.message || 'Test failed');
    } finally {
      setTesting(false);
    }
  };

  const pairIOS = async () => {
    try {
      const res = await client.createTunnelPairing();
      setPairDialog({ open: true, code: res.code, expires_at: res.expires_at });
    } catch (e: any) {
      alert(e?.message || 'Could not create pairing code');
    }
  };

  const cloudflareDisabled = Boolean(hipaaMode);

  const fetchLogs = async () => {
    setLogsLoading(true); setLogs([]);
    try {
      const res = await (client as any).runAction?.('tunnel_status_cloudflared_logs_tail');
      const out = (res?.stdout || '').split('\n').slice(-50);
      setLogs(out);
    } catch (e: any) {
      setLogs([`[error] ${(e?.message || 'Failed to run action')}`]);
    } finally {
      setLogsLoading(false);
    }
  };

  return (
    <Box>
      <ResponsiveFormSection
        title="VPN Tunnel"
        subtitle="Secure remote access required for iOS connectivity. Choose a provider that matches your security needs."
        icon={<VpnLock />}
      >
        <Stack spacing={2}>
          {/* Current status */}
          <Box>
            <Typography variant="subtitle2" fontWeight={600} sx={{ mb: 0.5 }}>Current Status</Typography>
            {status?.status === 'error' && (
              <Chip color="error" label={status.error_message || 'Error'} size="small" sx={{ mr: 1 }} />
            )}
            {status?.status === 'connected' && (
              <Chip color="success" label="Connected" size="small" sx={{ mr: 1 }} />
            )}
            {status?.status === 'connecting' && (
              <Chip color="warning" label="Connecting" size="small" sx={{ mr: 1 }} />
            )}
            {(!status || status?.status === 'disabled') && (
              <Chip color="default" label="Disabled" size="small" sx={{ mr: 1 }} />
            )}
            {status?.public_url && (
              <Chip icon={<Cloud fontSize="small" />} label={status.public_url} size="small" variant="outlined" />
            )}
          </Box>

          {/* Provider */}
          <ResponsiveSelect
            label="Tunnel Provider"
            value={provider}
            onChange={(v) => setProvider(v as any)}
            options={[
              { value: 'none', label: 'None (local network only)' },
              { value: 'cloudflare', label: cloudflareDisabled ? 'Cloudflare (dev only, disabled in HIPAA)' : 'Cloudflare (dev only)' , disabled: cloudflareDisabled },
              { value: 'wireguard', label: 'WireGuard (HIPAA‑capable)' },
              { value: 'tailscale', label: 'Tailscale (HIPAA‑capable)' },
            ]}
            helperText={cloudflareDisabled ? 'Cloudflare Quick Tunnel is not HIPAA compliant. Select WireGuard or Tailscale.' : 'Cloudflare is for non‑PHI testing only. Use WireGuard or Tailscale for PHI.'}
          />

          {/* WireGuard config */}
          {provider === 'wireguard' && (
            <Box>
              <Typography variant="subtitle2" fontWeight={600} sx={{ mb: 1 }}>WireGuard</Typography>
              <ResponsiveTextField
                label="Endpoint"
                value={wg.endpoint || ''}
                onChange={(v) => setWg({ ...wg, endpoint: v })}
                placeholder="router.example.com:51820"
                helperText="Your WireGuard server endpoint (e.g., Firewalla)."
              />
              <ResponsiveTextField
                label="Server Public Key"
                value={wg.server_key || ''}
                onChange={(v) => setWg({ ...wg, server_key: v })}
                placeholder="base64 key"
                helperText="Public key of your WireGuard server."
              />
              <ResponsiveTextField
                label="Client IP (CIDR)"
                value={wg.client_ip || ''}
                onChange={(v) => setWg({ ...wg, client_ip: v })}
                placeholder="10.0.0.100/24"
                helperText="Assigned client IP within your WG network."
              />
              <ResponsiveTextField
                label="DNS (optional)"
                value={wg.dns || ''}
                onChange={(v) => setWg({ ...wg, dns: v })}
                placeholder="1.1.1.1"
                helperText="Custom DNS server for the tunnel."
              />
            </Box>
          )}

          {/* Tailscale config */}
          {provider === 'tailscale' && (
            <Box>
              <Typography variant="subtitle2" fontWeight={600} sx={{ mb: 1 }}>Tailscale</Typography>
              <ResponsiveTextField
                label="Auth Key"
                value={ts.auth_key || ''}
                onChange={(v) => setTs({ ...ts, auth_key: v })}
                placeholder="tskey-..."
                helperText="Use an ephemeral or appropriately scoped key. Do not share."
                type="password"
              />
              <ResponsiveTextField
                label="Hostname"
                value={ts.hostname || ''}
                onChange={(v) => setTs({ ...ts, hostname: v })}
                placeholder="faxbot-server"
                helperText="Device name as it will appear in your Tailnet."
              />
            </Box>
          )}

          <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, flexWrap: 'wrap' }}>
            <Button variant="contained" onClick={applyConfig} disabled={saving} startIcon={<Security fontSize="small" />} sx={{ borderRadius: 2 }}>
              Save & Apply
              <InlineLoader loading={saving} />
            </Button>
            <Button variant="outlined" onClick={testConnectivity} disabled={testing} startIcon={<VpnKey fontSize="small" />} sx={{ borderRadius: 2 }}>
              Test Connectivity
              <InlineLoader loading={testing} />
            </Button>
            <Button variant="outlined" onClick={pairIOS} sx={{ borderRadius: 2 }}>
              Generate iOS Pairing Code
            </Button>
            <Button variant="text" onClick={fetchLogs} disabled={logsLoading} sx={{ borderRadius: 2 }}>
              View Cloudflared Logs (tail)
            </Button>
            <Box sx={{ ml: 'auto' }}>
              <Link href={learnMoreUrl} target="_blank" rel="noreferrer">Learn more</Link>
            </Box>
          </Box>
        </Stack>
        <SmoothLoader loading={loading} variant="linear" />
      </ResponsiveFormSection>

      {logs.length > 0 && (
        <Paper sx={{ p: 2, border: '1px solid', borderColor: 'divider', borderRadius: 2, mb: 2 }}>
          <Typography variant="subtitle2" sx={{ mb: 1 }}>Cloudflared logs (last 50 lines)</Typography>
          <Box component="pre" sx={{ m: 0, p: 0, whiteSpace: 'pre-wrap', wordBreak: 'break-word', fontSize: '0.8rem' }}>
            {logs.join('\n')}
          </Box>
        </Paper>
      )}

      {/* Pairing dialog */}
      <Dialog open={pairDialog.open} onClose={() => setPairDialog({ open: false })}>
        <DialogTitle>iOS Pairing</DialogTitle>
        <DialogContent>
          <Typography variant="body2" sx={{ mb: 1 }}>
            Enter this short code in the Faxbot iOS app. Codes expire quickly and contain no secrets.
          </Typography>
          <Typography variant="h4" sx={{ textAlign: 'center', letterSpacing: 4, my: 2 }}>
            {pairDialog.code}
          </Typography>
          {pairDialog.expires_at && (
            <Typography variant="caption" color="text.secondary" sx={{ display: 'block', textAlign: 'center' }}>
              Expires at {new Date(pairDialog.expires_at).toLocaleTimeString()}
            </Typography>
          )}
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setPairDialog({ open: false })}>Close</Button>
        </DialogActions>
      </Dialog>
    </Box>
  );
}
