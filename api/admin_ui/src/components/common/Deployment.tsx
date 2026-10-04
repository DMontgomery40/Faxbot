// Environment-only settings, shown read-only where they matter: each with its plain meaning
// and "Set when Faxbot started" or "Not set" (with the default it then uses). The server sees
// its own environment only, so it cannot say whether .env or the compose file set a value.
import { useEffect, useState } from 'react';
import { Box, Typography } from '@mui/material';
import { Lock as LockIcon } from '@mui/icons-material';
import type AdminAPIClient from '../../api/client';
import type { DeploymentValue, Settings } from '../../api/types';
import { ResponsiveFormSection } from './ResponsiveFormFields';
import { ResponsiveSettingItem } from './ResponsiveSettingItem';

type Kind = 'switch' | 'text' | 'secret';

export const DEPLOYMENT_MEANINGS: Record<string, { label: string; kind: Kind; unset?: string }> = {
  FAXBOT_ALLOW_INSECURE_HTTP_SESSIONS: { label: 'Sign-in without HTTPS on a private network', kind: 'switch',
    unset: 'Not set: signing in needs HTTPS.' },
  FAXBOT_CONSOLE_ORIGINS: { label: 'Console addresses allowed to sign in', kind: 'text',
    unset: 'Not set: only the public address of this server.' },
  ENABLE_LOCAL_ADMIN: { label: 'Console served by this installation', kind: 'switch',
    unset: 'Not set: this installation does not serve the console.' },
  ENABLE_ADMIN_EXEC: { label: 'Terminal is on', kind: 'switch',
    unset: 'Not set: the terminal is on when this installation serves the console.' },
  FAXBOT_ALLOW_INSECURE_LOOPBACK: { label: 'Plain HTTP on this computer only, for development', kind: 'switch',
    unset: 'Not set: off.' },
  FAXBOT_INSTALLATION_KEY_PATH: { label: 'Where the installation key is kept', kind: 'text',
    unset: "Not set: Faxbot keeps it in its data folder." },
  FAXBOT_DIRECT_KEY_PATH: { label: 'Where the direct-delivery key is kept', kind: 'text',
    unset: "Not set: Faxbot keeps it in its data folder." },
  FAXBOT_MEDIA_PORTS: { label: 'Ports the fax engine uses for call sound', kind: 'text',
    unset: 'Not set: the fax engine uses ports 4000–4999.' },
  FAXBOT_PHONE_SYSTEM_ADDRESS: { label: 'Address Faxbot gives your phone system', kind: 'text',
    unset: 'Not set: no phone system on your network sends calls to Faxbot.' },
  MCP_ALLOWED_HOSTS: { label: 'Addresses the assistant server answers to', kind: 'text',
    unset: 'Not set: the assistant server answers to any address.' },
  MCP_ALLOWED_ORIGINS: { label: 'Web pages allowed to use the assistant server', kind: 'text',
    unset: 'Not set: no web page may use it.' },
  MCP_OAUTH_SUBJECT_KEYS_FILE: { label: 'File of keys assistants sign in with', kind: 'text',
    unset: 'Not set: assistants sign in with a Faxbot API key.' },
  MCP_RESOURCE_URL: { label: 'Public address of the assistant server', kind: 'text', unset: 'Not set.' },
  MCP_HTTP_PORT: { label: 'Assistant server port', kind: 'text', unset: 'Not set: port 3001.' },
  MCP_WS_PORT: { label: 'Assistant server port for live connections', kind: 'text', unset: 'Not set: port 3004.' },
  MCP_WS_API_KEY: { label: 'Key for live assistant connections', kind: 'secret', unset: 'Not set.' },
  TZ: { label: "Server's time zone", kind: 'text', unset: 'Not set, so the server uses world standard time (UTC).' },
};

export const SET = 'Set';
export const SET_WHEN_STARTED = 'Set when Faxbot started.';
export const NOT_SET = 'Not set';
const TRUE = new Set(['1', 'true', 'yes', 'on']);

// What a row shows in its box: the value, On or Off, "Set" for a secret, or "Not set".
export function deploymentText(name: string, entry: DeploymentValue | undefined): string {
  const meaning = DEPLOYMENT_MEANINGS[name];
  if (name === 'ENABLE_ADMIN_EXEC' && entry?.effective !== undefined) return entry.effective ? 'On' : 'Off';
  if (!entry?.set) return NOT_SET;
  if (meaning?.kind === 'secret' || entry.value === null) return SET;
  if (meaning?.kind === 'switch') return TRUE.has(entry.value.toLowerCase()) ? 'On' : 'Off';
  return entry.value;
}

function helper(name: string, entry: DeploymentValue | undefined, showName: boolean): string {
  const meaning = DEPLOYMENT_MEANINGS[name];
  const where = entry?.set ? SET_WHEN_STARTED : (meaning?.unset ?? `${NOT_SET}.`);
  return showName ? `${where} (${name})` : where;
}

// Read-only rows for these variables, from a loaded settings document.
export function DeploymentRows({ settings, names, showNames = false }: {
  settings: Settings;
  names: string[];
  // Variable names are developer vocabulary: shown only on System → Developer pages.
  showNames?: boolean;
}) {
  const deployment = settings.deployment;
  if (!deployment) return null;
  return (
    <Box data-testid="deployment-rows">
      {names.map((name) => (
        <ResponsiveSettingItem key={name} icon={<LockIcon fontSize="small" />} label={DEPLOYMENT_MEANINGS[name]?.label ?? name}
          editValue={deploymentText(name, deployment[name])} helperText={helper(name, deployment[name], showNames)}
          showCurrentValue={false} />
      ))}
    </Box>
  );
}

// The same rows on a page that does not load settings itself (the Terminal page); nothing when settings cannot be read.
export function DeploymentSection({ client, names, title, showNames = false }: {
  client: AdminAPIClient;
  names: string[];
  title: string;
  showNames?: boolean;
}) {
  const [settings, setSettings] = useState<Settings | null>(null);
  useEffect(() => {
    let live = true;
    client.getSettings().then((data) => { if (live) setSettings(data); }).catch(() => undefined);
    return () => { live = false; };
  }, [client]);
  if (!settings?.deployment) return null;
  return (
    <Box sx={{ mt: 3 }}>
      <ResponsiveFormSection title={title} icon={<LockIcon />}>
        <Typography variant="body2" color="text.secondary" sx={{ px: 2 }}>
          These are set when Faxbot is installed and can't be changed here.
        </Typography>
        <DeploymentRows settings={settings} names={names} showNames={showNames} />
      </ResponsiveFormSection>
    </Box>
  );
}
