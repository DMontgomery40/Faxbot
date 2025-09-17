# Faxbot VPN Tunnel Implementation Plan

## Overview
Add VPN tunnel support to Faxbot with GUI-first configuration following AGENTS.md mandates. Support Cloudflare (auto), WireGuard, Tailscale, and None options.

## 1. Backend Implementation (Python FastAPI)

### New Environment Variables (Auto-managed by GUI)
```env
# Tunnel Configuration
TUNNEL_PROVIDER=cloudflare  # cloudflare|wireguard|tailscale|none
TUNNEL_ENABLED=true

# Cloudflare (auto-generated)
CLOUDFLARE_TUNNEL_ENABLED=true
CLOUDFLARE_TUNNEL_URL=  # Auto-discovered from logs
CLOUDFLARE_TUNNEL_STATUS=  # connected|connecting|error|disabled

# WireGuard Client
WIREGUARD_ENABLED=false
WIREGUARD_ENDPOINT=  # router.home.net:51820
WIREGUARD_SERVER_PUBLIC_KEY=
WIREGUARD_CLIENT_PRIVATE_KEY=  # Auto-generated
WIREGUARD_CLIENT_IP=  # 10.0.0.100/24
WIREGUARD_DNS=

# Tailscale
TAILSCALE_ENABLED=false
TAILSCALE_AUTHKEY=
TAILSCALE_HOSTNAME=faxbot-server
TAILSCALE_TAILNET_URL=
```

### New API Endpoints (`api/app/tunnel.py`)
```python
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional, Dict, Any
import subprocess
import json
import re
import docker
from datetime import datetime

router = APIRouter(prefix="/admin/tunnel", tags=["tunnel"])

class TunnelStatus(BaseModel):
    enabled: bool
    provider: str  # cloudflare, wireguard, tailscale, none
    status: str    # connected, connecting, error, disabled
    public_url: Optional[str] = None
    local_ip: Optional[str] = None
    last_connected: Optional[datetime] = None
    error_message: Optional[str] = None
    connection_count: Optional[int] = None

class CloudflareTunnelConfig(BaseModel):
    enabled: bool = True
    custom_domain: Optional[str] = None
    use_custom_domain: bool = False

class WireGuardConfig(BaseModel):
    enabled: bool
    endpoint: str
    server_public_key: str
    client_private_key: Optional[str] = None  # Auto-generated
    client_ip: str
    dns: Optional[str] = None

class TailscaleConfig(BaseModel):
    enabled: bool
    auth_key: str
    hostname: str = "faxbot-server"

@router.get("/status", response_model=TunnelStatus)
async def get_tunnel_status():
    """Get current tunnel status and connection info"""
    return await _get_tunnel_status()

@router.post("/cloudflare/setup")
async def setup_cloudflare_tunnel(config: CloudflareTunnelConfig):
    """Setup or reconfigure Cloudflare tunnel"""
    return await _setup_cloudflare_tunnel(config)

@router.post("/wireguard/setup")
async def setup_wireguard_tunnel(config: WireGuardConfig):
    """Setup WireGuard client tunnel"""
    return await _setup_wireguard_tunnel(config)

@router.post("/tailscale/setup")
async def setup_tailscale_tunnel(config: TailscaleConfig):
    """Setup Tailscale tunnel"""
    return await _setup_tailscale_tunnel(config)

@router.post("/test")
async def test_tunnel_connection():
    """Test current tunnel connectivity"""
    return await _test_tunnel_connection()

@router.get("/qrcode")
async def get_setup_qrcode():
    """Generate QR code for iOS app setup"""
    tunnel_status = await _get_tunnel_status()
    setup_data = {
        "local_url": f"http://{_get_local_ip()}:8080",
        "tunnel_url": tunnel_status.public_url,
        "tunnel_provider": tunnel_status.provider,
        "api_key": "will_be_provided_separately",  # Security
        "server_name": os.getenv("FAXBOT_SERVER_NAME", "Faxbot Server")
    }
    qr_data = base64.b64encode(json.dumps(setup_data).encode()).decode()
    return {"qr_data": qr_data, "setup_data": setup_data}

async def _get_tunnel_status() -> TunnelStatus:
    """Get current tunnel status from Docker containers and environment"""
    provider = os.getenv("TUNNEL_PROVIDER", "cloudflare")

    if provider == "cloudflare":
        return await _get_cloudflare_status()
    elif provider == "wireguard":
        return await _get_wireguard_status()
    elif provider == "tailscale":
        return await _get_tailscale_status()
    else:
        return TunnelStatus(
            enabled=False,
            provider="none",
            status="disabled"
        )

async def _get_cloudflare_status() -> TunnelStatus:
    """Check Cloudflare tunnel status from Docker logs"""
    try:
        client = docker.from_env()
        container = client.containers.get("faxbot-cloudflared-1")

        if container.status != "running":
            return TunnelStatus(
                enabled=True,
                provider="cloudflare",
                status="error",
                error_message="Container not running"
            )

        # Get last 50 lines of logs to find the tunnel URL
        logs = container.logs(tail=50).decode('utf-8')

        # Extract trycloudflare.com URL from logs
        url_match = re.search(r'https://[a-zA-Z0-9-]+\.trycloudflare\.com', logs)
        tunnel_url = url_match.group(0) if url_match else None

        # Check for connection errors
        if "error" in logs.lower() or "failed" in logs.lower():
            return TunnelStatus(
                enabled=True,
                provider="cloudflare",
                status="error",
                public_url=tunnel_url,
                error_message="Check container logs for details"
            )

        return TunnelStatus(
            enabled=True,
            provider="cloudflare",
            status="connected" if tunnel_url else "connecting",
            public_url=tunnel_url,
            last_connected=datetime.now()
        )

    except Exception as e:
        return TunnelStatus(
            enabled=True,
            provider="cloudflare",
            status="error",
            error_message=str(e)
        )
```

### Docker Compose Updates
```yaml
# Add to existing docker-compose.yml

services:
  # Existing services...

  # Cloudflare Tunnel (enabled by default)
  cloudflared:
    image: cloudflare/cloudflared:latest
    container_name: faxbot-cloudflared
    restart: unless-stopped
    command: tunnel --no-autoupdate --url http://api:8080
    profiles:
      - cloudflare
    depends_on:
      - api
    networks:
      - faxbot-net

  # WireGuard Client
  wireguard:
    image: linuxserver/wireguard:latest
    container_name: faxbot-wireguard
    cap_add:
      - NET_ADMIN
      - SYS_MODULE
    environment:
      - PUID=1000
      - PGID=1000
      - TZ=Etc/UTC
    volumes:
      - ./wireguard-config:/config
      - /lib/modules:/lib/modules
    profiles:
      - wireguard
    restart: unless-stopped
    networks:
      - faxbot-net

  # Tailscale
  tailscale:
    image: tailscale/tailscale:latest
    container_name: faxbot-tailscale
    hostname: faxbot-server
    environment:
      - TS_AUTHKEY=${TAILSCALE_AUTHKEY}
      - TS_HOSTNAME=${TAILSCALE_HOSTNAME:-faxbot-server}
      - TS_ROUTES=10.0.0.0/8,192.168.0.0/16,172.16.0.0/12
    volumes:
      - tailscale-data:/var/lib/tailscale
      - /dev/net/tun:/dev/net/tun
    cap_add:
      - NET_ADMIN
      - SYS_MODULE
    profiles:
      - tailscale
    restart: unless-stopped
    networks:
      - faxbot-net

volumes:
  tailscale-data:
```

## 2. Admin Console UI Implementation

### New Component: `TunnelSettings.tsx`
```typescript
import React, { useState, useEffect } from 'react';
import {
  Box, Card, CardContent, Typography, Button, Alert,
  FormControlLabel, Radio, RadioGroup, TextField,
  Accordion, AccordionSummary, AccordionDetails,
  Chip, CircularProgress, Grid, Switch,
  Dialog, DialogTitle, DialogContent, QRCodeSVG
} from '@mui/material';
import {
  ExpandMore as ExpandMoreIcon,
  Vpn as VpnIcon,
  Cloud as CloudIcon,
  Router as RouterIcon,
  Security as SecurityIcon,
  QrCode as QrCodeIcon,
  CheckCircle as CheckCircleIcon,
  Error as ErrorIcon,
  Warning as WarningIcon
} from '@mui/icons-material';

interface TunnelStatus {
  enabled: boolean;
  provider: string;
  status: string;
  public_url?: string;
  error_message?: string;
  last_connected?: string;
}

export default function TunnelSettings({ client }: { client: AdminAPIClient }) {
  const [tunnelStatus, setTunnelStatus] = useState<TunnelStatus | null>(null);
  const [loading, setLoading] = useState(false);
  const [provider, setProvider] = useState('cloudflare');
  const [showQRCode, setShowQRCode] = useState(false);
  const [qrData, setQRData] = useState('');

  const fetchTunnelStatus = async () => {
    try {
      setLoading(true);
      const status = await client.getTunnelStatus();
      setTunnelStatus(status);
      setProvider(status.provider);
    } catch (error) {
      console.error('Failed to fetch tunnel status:', error);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchTunnelStatus();
  }, []);

  const StatusIcon = ({ status }: { status: string }) => {
    switch (status) {
      case 'connected':
        return <CheckCircleIcon color="success" />;
      case 'error':
        return <ErrorIcon color="error" />;
      case 'connecting':
        return <WarningIcon color="warning" />;
      default:
        return <VpnIcon />;
    }
  };

  const generateQRCode = async () => {
    try {
      const response = await client.getTunnelQRCode();
      setQRData(response.qr_data);
      setShowQRCode(true);
    } catch (error) {
      console.error('Failed to generate QR code:', error);
    }
  };

  return (
    <Box>
      <Typography variant="h5" gutterBottom>
        <VpnIcon sx={{ mr: 1, verticalAlign: 'middle' }} />
        VPN Tunnel Settings
      </Typography>

      {/* Current Status */}
      <Card sx={{ mb: 3 }}>
        <CardContent>
          <Grid container spacing={2} alignItems="center">
            <Grid item>
              <StatusIcon status={tunnelStatus?.status || 'disabled'} />
            </Grid>
            <Grid item xs>
              <Typography variant="h6">
                Status: {tunnelStatus?.status || 'Unknown'}
              </Typography>
              {tunnelStatus?.public_url && (
                <Typography variant="body2" color="textSecondary">
                  Public URL: {tunnelStatus.public_url}
                </Typography>
              )}
            </Grid>
            <Grid item>
              <Button
                variant="outlined"
                startIcon={<QrCodeIcon />}
                onClick={generateQRCode}
                disabled={!tunnelStatus?.public_url}
              >
                iOS Setup QR
              </Button>
            </Grid>
          </Grid>
        </CardContent>
      </Card>

      {/* Provider Selection */}
      <Card sx={{ mb: 3 }}>
        <CardContent>
          <Typography variant="h6" gutterBottom>
            Tunnel Provider
          </Typography>
          <RadioGroup
            value={provider}
            onChange={(e) => setProvider(e.target.value)}
          >
            <FormControlLabel
              value="cloudflare"
              control={<Radio />}
              label={
                <Box>
                  <Typography variant="body1">
                    <CloudIcon sx={{ mr: 1, verticalAlign: 'middle' }} />
                    Cloudflare (Recommended)
                  </Typography>
                  <Typography variant="body2" color="textSecondary">
                    Zero-configuration tunnel with automatic HTTPS
                  </Typography>
                </Box>
              }
            />
            <FormControlLabel
              value="wireguard"
              control={<Radio />}
              label={
                <Box>
                  <Typography variant="body1">
                    <RouterIcon sx={{ mr: 1, verticalAlign: 'middle' }} />
                    WireGuard
                  </Typography>
                  <Typography variant="body2" color="textSecondary">
                    Connect to your existing WireGuard server (e.g., Firewalla)
                  </Typography>
                </Box>
              }
            />
            <FormControlLabel
              value="tailscale"
              control={<Radio />}
              label={
                <Box>
                  <Typography variant="body1">
                    <SecurityIcon sx={{ mr: 1, verticalAlign: 'middle' }} />
                    Tailscale
                  </Typography>
                  <Typography variant="body2" color="textSecondary">
                    Join your Tailnet for secure access
                  </Typography>
                </Box>
              }
            />
            <FormControlLabel
              value="none"
              control={<Radio />}
              label={
                <Box>
                  <Typography variant="body1">None</Typography>
                  <Typography variant="body2" color="textSecondary">
                    Local network only (requires your own VPN)
                  </Typography>
                </Box>
              }
            />
          </RadioGroup>
        </CardContent>
      </Card>

      {/* Provider-Specific Configuration */}
      {provider === 'cloudflare' && (
        <CloudflareConfig tunnelStatus={tunnelStatus} client={client} />
      )}
      {provider === 'wireguard' && (
        <WireGuardConfig client={client} />
      )}
      {provider === 'tailscale' && (
        <TailscaleConfig client={client} />
      )}

      {/* QR Code Dialog */}
      <Dialog open={showQRCode} onClose={() => setShowQRCode(false)}>
        <DialogTitle>iOS App Setup</DialogTitle>
        <DialogContent>
          <Box sx={{ textAlign: 'center', p: 2 }}>
            <QRCodeSVG value={qrData} size={256} />
            <Typography variant="body2" sx={{ mt: 2 }}>
              Scan this QR code with the Faxbot iOS app to automatically
              configure your connection settings.
            </Typography>
          </Box>
        </DialogContent>
      </Dialog>
    </Box>
  );
}

function CloudflareConfig({ tunnelStatus, client }: any) {
  const [customDomain, setCustomDomain] = useState('');
  const [useCustomDomain, setUseCustomDomain] = useState(false);

  return (
    <Card>
      <CardContent>
        <Typography variant="h6" gutterBottom>
          Cloudflare Configuration
        </Typography>

        {tunnelStatus?.status === 'connected' && (
          <Alert severity="success" sx={{ mb: 2 }}>
            Tunnel is active! Public URL: {tunnelStatus.public_url}
          </Alert>
        )}

        <Accordion>
          <AccordionSummary expandIcon={<ExpandMoreIcon />}>
            <Typography>Advanced Settings</Typography>
          </AccordionSummary>
          <AccordionDetails>
            <Box sx={{ mb: 2 }}>
              <FormControlLabel
                control={
                  <Switch
                    checked={useCustomDomain}
                    onChange={(e) => setUseCustomDomain(e.target.checked)}
                  />
                }
                label="Use custom domain"
              />
            </Box>

            {useCustomDomain && (
              <TextField
                fullWidth
                label="Custom domain"
                value={customDomain}
                onChange={(e) => setCustomDomain(e.target.value)}
                placeholder="fax.yourdomain.com"
                helperText="Requires Cloudflare account and domain configuration"
                sx={{ mb: 2 }}
              />
            )}

            <Button variant="contained" disabled>
              Apply Configuration
            </Button>
          </AccordionDetails>
        </Accordion>
      </CardContent>
    </Card>
  );
}

// Similar implementations for WireGuardConfig and TailscaleConfig...
```

### Integration into Settings.tsx
Add a new section in the existing Settings component:

```typescript
// Add to existing Settings.tsx imports
import TunnelSettings from './TunnelSettings';

// Add to the main Settings component JSX:
<ResponsiveSettingSection
  title="VPN Tunnel"
  icon={<VpnIcon />}
  description="Configure secure remote access to your Faxbot server"
>
  <TunnelSettings client={client} />
</ResponsiveSettingSection>
```

## 3. Auto-Setup Logic

### Startup Script (`scripts/tunnel-setup.sh`)
```bash
#!/bin/bash
# Auto-setup tunnel based on environment variables

TUNNEL_PROVIDER=${TUNNEL_PROVIDER:-cloudflare}

case $TUNNEL_PROVIDER in
  "cloudflare")
    echo "Starting Cloudflare tunnel..."
    docker-compose --profile cloudflare up -d cloudflared

    # Wait for tunnel URL and capture it
    timeout=30
    while [ $timeout -gt 0 ]; do
      URL=$(docker logs faxbot-cloudflared-1 2>&1 | grep -o 'https://[a-zA-Z0-9-]*\.trycloudflare\.com' | tail -1)
      if [ ! -z "$URL" ]; then
        echo "Tunnel URL: $URL"
        # Save to environment or database
        break
      fi
      sleep 1
      ((timeout--))
    done
    ;;

  "wireguard")
    echo "Starting WireGuard client..."
    if [ -f "./wireguard-config/wg0.conf" ]; then
      docker-compose --profile wireguard up -d wireguard
    else
      echo "WireGuard config not found. Please configure via Admin Console."
    fi
    ;;

  "tailscale")
    echo "Starting Tailscale..."
    if [ ! -z "$TAILSCALE_AUTHKEY" ]; then
      docker-compose --profile tailscale up -d tailscale
    else
      echo "Tailscale auth key not set. Please configure via Admin Console."
    fi
    ;;

  "none")
    echo "No tunnel configured. Using local network only."
    ;;
esac
```

## 4. Implementation Notes

### HIPAA Compliance
- All tunnel providers ensure TLS encryption
- Cloudflare provides automatic certificate management
- WireGuard and Tailscale use modern cryptography
- No PHI is stored at tunnel providers

### GUI-First Approach (AGENTS.md compliance)
- Every setting has Admin Console UI
- No CLI-only configuration
- Helpful tooltips and documentation links
- Mobile-responsive design
- Real-time status monitoring

### iOS App Integration
- QR code contains all necessary connection info
- Auto-discovery for local network
- Smart connection selection (local vs tunnel)
- Graceful fallback between connection types

This implementation provides the "just works" experience like Scrypted while maintaining Faxbot's GUI-first philosophy and HIPAA compliance requirements.