import { useEffect, useMemo, useState } from 'react';
import { Alert, Box, Button, Chip, Dialog, DialogActions, DialogContent, DialogTitle, Link, Paper, Stack, Typography } from '@mui/material';
import { Link as LinkIcon, Security, VpnKey, VpnLock } from '@mui/icons-material';
import AdminAPIClient from '../api/client';
import type { TunnelStatus } from '../api/types';
import { useTraits } from '../hooks/useTraits';
import { ResponsiveFormSection, ResponsiveSelect, ResponsiveTextField, ResponsiveFileUpload } from './common/ResponsiveFormFields';
import { SmoothLoader, InlineLoader } from './common/SmoothLoader';

type Props = { client: AdminAPIClient; docsBase?: string; hipaaMode?: boolean; inboundBackend?: string; sinchConfigured?: boolean };

export default function TunnelSettings({ client, docsBase, hipaaMode }: Props) {
  const [status, setStatus] = useState<TunnelStatus | null>(null);
  const [loading, setLoading] = useState<boolean>(false);
  const [saving, setSaving] = useState<boolean>(false);
  const [testing, setTesting] = useState<boolean>(false);
  const [registering, setRegistering] = useState<boolean>(false);
  const [pairDialog, setPairDialog] = useState<{ open: boolean; code?: string; expires_at?: string }>({ open: false });
  const [logsLoading, setLogsLoading] = useState<boolean>(false);
  const [logs, setLogs] = useState<string[]>([]);
  const [isSinchActive, setIsSinchActive] = useState<boolean>(false);
  const [inboundEnabled, setInboundEnabled] = useState<boolean>(false);
  const [notice, setNotice] = useState<{ severity: 'success' | 'error' | 'info'; message: string } | null>(null);

  const [provider, setProvider] = useState<'none' | 'cloudflare' | 'wireguard' | 'tailscale'>('none');
  const [wg, setWg] = useState<{ endpoint?: string; server_key?: string; client_ip?: string; dns?: string }>({});
  const [ts, setTs] = useState<{ auth_key?: string; hostname?: string }>({});
  const [wgFile, setWgFile] = useState<File | null>(null);
  const [wgHasConf, setWgHasConf] = useState<boolean>(false);
  const [wgQr, setWgQr] = useState<{ open: boolean; png?: string }>({ open: false });

  const learnMoreUrl = useMemo(() => `${docsBase || ''}/networking/tunnels`, [docsBase]);

  const fetchStatus = async () => {
    setLoading(true);
    try {
      const s = await client.getTunnelStatus();
      setStatus(s);
      setProvider(s.provider);
    } catch (e: any) {
      setNotice({ severity: 'error', message: e?.message || 'Failed to load tunnel status' });
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { fetchStatus(); }, []);
  // Load persisted provider from admin settings (round-trip across restarts)
  useEffect(() => {
    (async () => {
      try {
        const s = await client.getSettings();
        const persistedProvider = (s as any)?.tunnel?.provider as string | undefined;
        if (persistedProvider && ['none','cloudflare','wireguard','tailscale'].includes(persistedProvider)) {
          setProvider(persistedProvider as any);
        }
      } catch {}
    })();
  }, [client]);

  // Probe if a WireGuard conf is stored
  useEffect(() => {
    const probe = async () => {
      if (provider !== 'wireguard') { setWgHasConf(false); return; }
      try {
        // Try a HEAD by attempting to fetch and discarding body
        const blob = await client.wgDownloadConf();
        setWgHasConf(blob.size > 0);
      } catch (_) {
        setWgHasConf(false);
      }
    };
    probe();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [provider]);
  useEffect(() => {
    (async () => {
      try {
        const s = await client.getSettings();
        // Trait-gated detection: consider Sinch-like when OAuth2 is available for the active inbound provider
        const methods = (traitValue('inbound', 'auth.methods') || []) as string[];
        setIsSinchActive(Array.isArray(methods) && methods.includes('oauth2'));
        setInboundEnabled(Boolean(s?.inbound?.enabled));
      } catch {
        // no-op
      }
    })();
  }, [client, traitValue]);

  // Auto-register Sinch webhook when tunnel URL changes (non-HIPAA only)
  useEffect(() => {
    const maybeAutoRegister = async () => {
      try {
        if (!status?.public_url) return;
        if (!isSinchActive || !inboundEnabled) return;
        if (hipaaMode) return;
        const key = 'last_registered_sinch_url';
        const last = localStorage.getItem(key) || '';
        if (last === status.public_url) return;
        setRegistering(true);
        const res = await (client as any).registerSinchWebhook?.();
        if (res?.success) {
          localStorage.setItem(key, status.public_url);
          setNotice({ severity: 'success', message: 'Sinch webhook auto-registered to current tunnel URL.' });
        } else if (res?.error) {
          setNotice({ severity: 'error', message: `Auto-registration failed: ${res.error}` });
        }
      } catch (e: any) {
        setNotice({ severity: 'error', message: e?.message || 'Auto-registration failed' });
      } finally {
        setRegistering(false);
      }
    };
    maybeAutoRegister();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status?.public_url, isSinchActive, inboundEnabled, hipaaMode]);

  const applyConfig = async () => {
    setSaving(true);
    try {
      // Persist provider via admin settings so it survives restarts
      await client.updateSettings({ tunnel_provider: provider });
      // Also update in-memory state for immediate UX
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
      if (!res.ok) setNotice({ severity: 'error', message: `Connectivity check failed: ${res.message || 'Unknown error'}` });
      else setNotice({ severity: 'success', message: `Connectivity OK${res.target ? ` (${res.target})` : ''}` });
    } catch (e: any) {
      setNotice({ severity: 'error', message: e?.message || 'Test failed' });
    } finally {
      setTesting(false);
    }
  };

  const pairIOS = async () => {
    try {
      const res = await client.createTunnelPairing();
      setPairDialog({ open: true, code: res.code, expires_at: res.expires_at });
    } catch (e: any) {
      setNotice({ severity: 'error', message: e?.message || 'Could not create pairing code' });
    }
  };

  const registerSinch = async () => {
    setRegistering(true);
    try {
      const res = await (client as any).registerSinchWebhook?.();
      if (!res?.success) setNotice({ severity: 'error', message: `Registration failed: ${res?.error || 'Unknown error'}` });
      else setNotice({ severity: 'success', message: `Registered webhook: ${res.webhook_url || ''}` });
    } catch (e: any) {
      setNotice({ severity: 'error', message: e?.message || 'Registration failed' });
    } finally {
      setRegistering(false);
    }
  };

  const cloudflareDisabled = Boolean(hipaaMode);

  const fetchLogs = async () => {
    setLogsLoading(true); setLogs([]);
    try {
      const res = await client.getTunnelCloudflaredLogs(50);
      setLogs(res.items || []);
    } catch (e: any) {
      setLogs([`[error] ${(e?.message || 'Failed to read Cloudflared logs')}`]);
      setNotice({ severity: 'error', message: e?.message || 'Failed to read Cloudflared logs' });
    } finally {
      setLogsLoading(false);
    }
  };

  return (
    <Box>
      {notice && (
        <Alert severity={notice.severity} onClose={() => setNotice(null)} sx={{ mb: 2 }}>
          {notice.message}
        </Alert>
      )}
      <ResponsiveFormSection
        title="VPN Tunnel"
        subtitle="Secure external access for non‑HIPAA setups and private VPN for HIPAA. Choose a provider that matches your security needs."
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
            {/* Intentionally do not display the public URL to keep implementation details abstracted. */}
            {status?.status === 'connected' && (
              <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 0.5 }}>
                Connected via public URL
              </Typography>
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
              <ResponsiveFileUpload
                label="Import .conf"
                helperText="Upload your device-specific WireGuard .conf to display as a QR. The file is stored securely and can be deleted."
                accept=".conf,text/plain"
                onFileSelect={(f) => setWgFile(f)}
              />
              <Box sx={{ display: 'flex', gap: 1, flexWrap: 'wrap', mb: 1 }}>
                <Button
                  variant="outlined"
                  disabled={!wgFile}
                  onClick={async () => {
                    if (!wgFile) return;
                    try {
                      setSaving(true);
                      await client.wgImportConfFile(wgFile);
                      setWgHasConf(true);
                      setWgFile(null);
                      setNotice({ severity: 'success', message: 'WireGuard configuration imported' });
                    } catch (e: any) {
                      setNotice({ severity: 'error', message: e?.message || 'Import failed' });
                    } finally { setSaving(false); }
                  }}
                  sx={{ borderRadius: 2 }}
                >
                  Upload
                </Button>
                <Button
                  variant="outlined"
                  disabled={!wgHasConf}
                  onClick={async () => {
                    try {
                      const res = await client.wgGetQr();
                      const dataUrl = res?.svg_base64
                        ? `data:image/svg+xml;base64,${res.svg_base64}`
                        : res?.png_base64
                        ? `data:image/png;base64,${res.png_base64}`
                        : '';
                      if (!dataUrl) throw new Error('QR not available');
                      setWgQr({ open: true, png: dataUrl });
                    } catch (e: any) {
                      setNotice({ severity: 'error', message: e?.message || 'Could not generate QR' });
                    }
                  }}
                  sx={{ borderRadius: 2 }}
                >
                  Show QR
                </Button>
                <Button
                  variant="outlined"
                  disabled={!wgHasConf}
                  onClick={async () => {
                    try {
                      const blob = await client.wgDownloadConf();
                      const url = URL.createObjectURL(blob);
                      const a = document.createElement('a');
                      a.href = url;
                      a.download = 'wg.conf';
                      document.body.appendChild(a);
                      a.click();
                      a.remove();
                      URL.revokeObjectURL(url);
                    } catch (e: any) {
                      setNotice({ severity: 'error', message: e?.message || 'Download failed' });
                    }
                  }}
                  sx={{ borderRadius: 2 }}
                >
                  Download .conf
                </Button>
                <Button
                  variant="text"
                  color="error"
                  disabled={!wgHasConf}
                  onClick={async () => {
                    try {
                      await client.wgDeleteConf();
                      setWgHasConf(false);
                      setNotice({ severity: 'success', message: 'WireGuard configuration deleted' });
                    } catch (e: any) {
                      setNotice({ severity: 'error', message: e?.message || 'Delete failed' });
                    }
                  }}
                  sx={{ borderRadius: 2 }}
                >
                  Delete configuration
                </Button>
              </Box>
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
          {isSinchActive && inboundEnabled && (
            <Button variant="outlined" onClick={registerSinch} disabled={registering} startIcon={<LinkIcon fontSize="small" />} sx={{ borderRadius: 2 }}>
              Register with Sinch
              <InlineLoader loading={registering} />
            </Button>
          )}
          {provider === 'cloudflare' && !cloudflareDisabled && (
            <Button variant="text" onClick={fetchLogs} disabled={logsLoading} sx={{ borderRadius: 2 }}>
              View Cloudflared Logs (tail)
            </Button>
          )}
            <Box sx={{ ml: 'auto' }}>
              <Link href={learnMoreUrl} target="_blank" rel="noreferrer">Learn more</Link>
            </Box>
          </Box>
        </Stack>
        <SmoothLoader loading={loading} variant="linear" />
      </ResponsiveFormSection>

      {provider === 'cloudflare' && !cloudflareDisabled && logs.length > 0 && (
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

      {/* WireGuard QR dialog */}
      <Dialog open={wgQr.open} onClose={() => setWgQr({ open: false })}>
        <DialogTitle>WireGuard QR</DialogTitle>
        <DialogContent>
          <Typography variant="body2" sx={{ mb: 1 }}>
            Scan this QR in the WireGuard app to import the configuration.
          </Typography>
          {wgQr.png && (
            <Box sx={{ display: 'flex', justifyContent: 'center', my: 1 }}>
              <img src={wgQr.png} alt="WireGuard QR" style={{ maxWidth: 320, width: '100%' }} />
            </Box>
          )}
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setWgQr({ open: false })}>Close</Button>
        </DialogActions>
      </Dialog>
    </Box>
  );
}
  const { traitValue } = useTraits();
