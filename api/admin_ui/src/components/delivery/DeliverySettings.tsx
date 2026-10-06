// Installation settings for delivery routes, direct delivery and the intake
// email connector. Settings and the Setup Wizard share these editors; both
// save through the ordinary settings write path.
import { useState } from 'react';
import {
  Alert, Box, Button, FormControlLabel, IconButton, List, ListItem, ListItemText, Stack, Switch, TextField, Tooltip,
  Typography,
} from '@mui/material';
import {
  AltRoute as AltRouteIcon,
  ArrowDownward as ArrowDownwardIcon,
  ArrowUpward as ArrowUpwardIcon,
  Close as CloseIcon,
  Handshake as HandshakeIcon,
  MoveToInbox as MoveToInboxIcon,
  Settings as SettingsIcon,
  VpnKey as VpnKeyIcon,
} from '@mui/icons-material';
import AdminAPIClient from '../../api/client';
import type { Settings } from '../../api/types';
import { ResponsiveFormSection } from '../common/ResponsiveFormFields';
import { ResponsiveSettingItem } from '../common/ResponsiveSettingItem';
import SecretInput from '../common/SecretInput';
import EnvSetField, { environmentManaged } from '../common/EnvSetField';
import { numberHint, settingsNumberFormat } from '../common/numbers';
import { providerLabel } from '../../providerLabels';
import DirectCardDialog from './DirectCardDialog';
import EmailDelivery from './EmailDelivery';
import { DeliveryError } from './shared';

type FormValue = string | number | boolean;
type Values = Record<string, FormValue>;

const routeLabel = (id: string) => providerLabel(id);

export function parseRoutes(value: FormValue | undefined): string[] {
  const result: string[] = [];
  for (const part of String(value ?? '').split(',')) {
    const id = part.trim().toLowerCase();
    if (id && !result.includes(id)) result.push(id);
  }
  return result;
}

// Editor values for these settings, by settings patch name. The route list is
// stored in one canonical spelling so loading alone never counts as a change.
export function deliveryEditorValues(data: Settings): Values {
  const values: Values = {};
  if (data.routing) {
    values.outbound_routes = parseRoutes(data.routing.outbound_routes).join(',');
    values.route_min_success_percent = data.routing.min_success_percent;
  }
  if (data.direct) {
    values.direct_delivery_enabled = data.direct.enabled;
    values.direct_organization = data.direct.organization;
    values.direct_fax_number = data.direct.fax_number;
    values.direct_allow_private_peers = data.direct.allow_private_peers ?? false;
  }
  if (data.intake) {
    values.intake_email_enabled = data.intake.email_enabled;
    values.intake_smtp_host = data.intake.smtp_host;
    values.intake_smtp_port = data.intake.smtp_port;
    values.intake_smtp_security = data.intake.smtp_security;
    values.intake_smtp_username = data.intake.smtp_username;
    values.intake_smtp_password = data.intake.smtp_password;
    values.intake_email_from = data.intake.email_from;
    values.intake_email_to = data.intake.email_to;
    values.intake_email_subject = data.intake.email_subject;
  }
  return values;
}

// Providers whose credentials are filled in (a saved secret shows as a mask).
// The SIP trunk counts once its manager password is no longer the default.
export function providersSetUp(values: Values, sipPasswordIsDefault: boolean): string[] {
  const filled = (...fields: string[]) => fields.every((field) => Boolean(values[field]));
  const result: string[] = [];
  if (filled('phaxio_api_key', 'phaxio_api_secret')) result.push('phaxio');
  if (filled('sinch_project_id', 'sinch_api_key', 'sinch_api_secret')) result.push('sinch');
  if (filled('signalwire_space_url', 'signalwire_project_id', 'signalwire_api_token')) result.push('signalwire');
  if (filled('documo_api_key')) result.push('documo');
  if (filled('humblefax_access_key', 'humblefax_secret_key')) result.push('humblefax');
  if (filled('efax_app_id', 'efax_api_key', 'efax_user_id')) result.push('efax');
  if (filled('ami_username', 'ami_password') && !sipPasswordIsDefault) result.push('sip');
  for (const field of ['backend', 'outbound_backend', 'inbound_backend']) {
    if (values[field] === 'freeswitch' && !result.includes('freeswitch')) result.push('freeswitch');
  }
  return result;
}

// An ordered list of extra outbound providers, saved as FAX_OUTBOUND_ROUTES.
export function RouteOrderEditor({ value, onChange, available, outbound, disabled }: {
  value: FormValue | undefined;
  onChange: (next: string) => void;
  available: string[];
  outbound: string;
  disabled?: boolean;
}) {
  const chosen = parseRoutes(value);
  const save = (next: string[]) => onChange(next.join(','));
  const move = (index: number, offset: number) => {
    const next = [...chosen];
    [next[index], next[index + offset]] = [next[index + offset], next[index]];
    save(next);
  };
  const addable = available.filter((id) => id !== outbound && !chosen.includes(id));
  const note = (id: string) => id === outbound ? 'Already your outbound provider.'
    : available.includes(id) ? null : 'Not set up yet; Faxbot skips it until it is.';

  return (
    <Box>
      <Typography variant="subtitle2" fontWeight={600}>Extra outbound routes</Typography>
      <Typography variant="caption" color="text.secondary" display="block" sx={{ mb: 1 }}>
        Faxbot uses the cheapest route that works reliably for each number; when prices are equal or unknown it follows this order after {routeLabel(outbound)}.
      </Typography>
      {chosen.length === 0 ? (
        <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
          No extra routes. Faxes go through {routeLabel(outbound)} only.
        </Typography>
      ) : (
        <List dense aria-label="Extra outbound routes" sx={{ border: 1, borderColor: 'divider', borderRadius: 2, mb: 1 }}>
          {chosen.map((id, index) => (
            <ListItem key={id} secondaryAction={
              <Box>
                <Tooltip title="Move up"><span>
                  <IconButton size="small" aria-label={`Move ${routeLabel(id)} up`} disabled={disabled || index === 0}
                    onClick={() => move(index, -1)}><ArrowUpwardIcon fontSize="small" /></IconButton>
                </span></Tooltip>
                <Tooltip title="Move down"><span>
                  <IconButton size="small" aria-label={`Move ${routeLabel(id)} down`} disabled={disabled || index === chosen.length - 1}
                    onClick={() => move(index, 1)}><ArrowDownwardIcon fontSize="small" /></IconButton>
                </span></Tooltip>
                <Tooltip title="Remove"><span>
                  <IconButton size="small" aria-label={`Remove ${routeLabel(id)}`} disabled={disabled}
                    onClick={() => save(chosen.filter((other) => other !== id))}><CloseIcon fontSize="small" /></IconButton>
                </span></Tooltip>
              </Box>
            }>
              <ListItemText primary={`${index + 1}. ${routeLabel(id)}`} secondary={note(id)} />
            </ListItem>
          ))}
        </List>
      )}
      <TextField select size="small" label="Add a route" value="" disabled={disabled || addable.length === 0}
        onChange={(event) => { if (event.target.value) save([...chosen, event.target.value]); }}
        SelectProps={{ native: true }} InputLabelProps={{ shrink: true }} sx={{ minWidth: 260 }}
        helperText={addable.length === 0 ? 'Set up another provider to add it as a route.' : undefined}>
        <option value="">Choose a provider…</option>
        {addable.map((id) => <option key={id} value={id}>{routeLabel(id)}</option>)}
      </TextField>
    </Box>
  );
}

function SwitchField({ label, helper, checked, onChange }: {
  label: string; helper: string; checked: boolean; onChange: (checked: boolean) => void;
}) {
  return (
    <Box>
      <FormControlLabel control={<Switch checked={checked} onChange={(event) => onChange(event.target.checked)} />} label={label} />
      <Typography variant="caption" color="text.secondary" display="block" sx={{ ml: 6 }}>{helper}</Typography>
    </Box>
  );
}

const KEY_LOCATION = 'Kept on this server in a private file in the fax data folder, unless the installation names another file. It is never shown or exported, so include it in server backups.';
const PRIVATE_PEERS_HELP = 'Off: Faxbot only sends documents to partners on the public internet. Turn this on only for partners on a network you control.';
const SUBJECT_HELP = 'Can include {from_number}, {to_number}, {pages} and {received_at}.';
const SECURITY_OPTIONS = [
  { value: 'starttls', label: 'STARTTLS' }, { value: 'tls', label: 'TLS' }, { value: 'none', label: 'None' },
];

interface SectionsProps {
  client: AdminAPIClient;
  settings: Settings;
  form: Values;
  loaded: Values;
  onChange: (field: string, value: FormValue) => void;
  showCurrentValue: boolean;
  outbound: string;
  canWrite: boolean;
  // Show only these sections (a console page shows its own part); all four when absent.
  only?: DeliverySection[];
}

// The anchor the Inbox's "Email delivery settings" link opens.
export const EMAIL_DELIVERY_SECTION = 'email-delivery';

export const DELIVERY_SECTIONS = ['routes', 'direct', 'intake', 'email'] as const;
export type DeliverySection = typeof DELIVERY_SECTIONS[number];

// The Delivery routes, Direct delivery, Intake defaults and Email delivery sections of Settings.
export function DeliverySettingsSections({ client, settings, form, loaded, onChange, showCurrentValue, outbound, canWrite, only }: SectionsProps) {
  const shows = (section: DeliverySection) => !only || only.includes(section);
  const [card, setCard] = useState<string | null>(null);
  const [cardError, setCardError] = useState<unknown>(null);
  const [cardBusy, setCardBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  const text = (label: string, field: string, helperText: string, type: 'text' | 'number' = 'text') => (
    <ResponsiveSettingItem icon={<SettingsIcon />} label={label} value={loaded[field] ?? ''} editValue={form[field] ?? ''}
      onChange={(value) => onChange(field, type === 'number' && value !== '' ? Number(value) : value)}
      helperText={helperText} type={type} showCurrentValue={showCurrentValue} />
  );

  const identityEdited = ['direct_organization', 'direct_fax_number'].some((field) => form[field] !== loaded[field]);
  const showCard = async () => {
    setCardBusy(true);
    setCardError(null);
    setNotice(null);
    try {
      const result = await client.getDirectCard();
      setCard(JSON.stringify(result.card, null, 2));
    } catch (failure) {
      setCardError(failure);
    } finally {
      setCardBusy(false);
    }
  };

  return (
    <>
      {settings.routing && shows('routes') && (
        <ResponsiveFormSection title="Delivery routes" subtitle="Other providers a fax may use, and how reliable a route must be."
          icon={<AltRouteIcon />}>
          <RouteOrderEditor value={form.outbound_routes} onChange={(next) => onChange('outbound_routes', next)}
            available={providersSetUp(form, settings.sip.ami_password_is_default && form.ami_password === loaded.ami_password)}
            outbound={outbound} />
          {text('Minimum delivery rate (%)', 'route_min_success_percent',
            'A route that delivers less than this share of recent faxes to a number is tried last for that number.', 'number')}
        </ResponsiveFormSection>
      )}

      {settings.direct && shows('direct') && (
        <ResponsiveFormSection title="Direct delivery" subtitle="Exchange documents with other Faxbot installations, with no fax call."
          icon={<HandshakeIcon />}>
          <SwitchField label="Use direct delivery" checked={Boolean(form.direct_delivery_enabled)}
            onChange={(checked) => onChange('direct_delivery_enabled', checked)}
            helper="Verified partners can send documents straight to this Faxbot, and faxes to them go directly." />
          {text('Organization name', 'direct_organization', 'The name partners see on your card and on code faxes.')}
          {text('Our fax number', 'direct_fax_number', numberHint(settingsNumberFormat(settings), 'The number partners fax you at'))}
          <ResponsiveSettingItem icon={<VpnKeyIcon />} label="Private key" editValue="On this server"
            helperText={KEY_LOCATION} showCurrentValue={false} />
          <SwitchField label="Allow partners on private networks (advanced)" checked={Boolean(form.direct_allow_private_peers)}
            onChange={(checked) => onChange('direct_allow_private_peers', checked)}
            helper={PRIVATE_PEERS_HELP} />
          {notice && <Alert severity="success" onClose={() => setNotice(null)}>{notice}</Alert>}
          {cardError ? <DeliveryError error={cardError} onClose={() => setCardError(null)} /> : null}
          <Box display="flex" alignItems="center" gap={2} flexWrap="wrap">
            <Button variant="outlined" onClick={() => void showCard()} disabled={cardBusy || identityEdited} sx={{ borderRadius: 2 }}>
              Show our card
            </Button>
            {identityEdited && <Typography variant="caption" color="text.secondary">Apply your changes to see them on the card.</Typography>}
          </Box>
        </ResponsiveFormSection>
      )}

      {(shows('intake') || shows('email')) && (
      <Box id={EMAIL_DELIVERY_SECTION} sx={{ scrollMarginTop: 80 }}>
      {settings.intake && shows('intake') && (
        <ResponsiveFormSection title="Email delivery for the whole installation"
          subtitle="Email delivery for received faxes, set for the whole installation. It appears under Email delivery below and is changed only here. Changes take effect as soon as you apply them."
          icon={<MoveToInboxIcon />}>
          <SwitchField label="Email received faxes" checked={Boolean(form.intake_email_enabled)}
            onChange={(checked) => onChange('intake_email_enabled', checked)}
            helper="Faxbot emails each received document, with the original PDF attached." />
          {text('Email server', 'intake_smtp_host', 'For example, smtp.example.org.')}
          {text('Port', 'intake_smtp_port', 'Usually 587, or 465. Your email provider says which.', 'number')}
          <ResponsiveSettingItem icon={<SettingsIcon />} label="Security" value={loaded.intake_smtp_security ?? ''}
            editValue={form.intake_smtp_security ?? 'starttls'} onChange={(value) => onChange('intake_smtp_security', value)}
            type="select" options={SECURITY_OPTIONS} showCurrentValue={showCurrentValue} />
          {text('User name', 'intake_smtp_username', 'Leave empty if the server needs no sign-in.')}
          <Box>
            {environmentManaged(settings).has('intake_smtp_password') ? <EnvSetField fullWidth size="small" label="Email password" /> :
            <SecretInput fullWidth size="small" label="Email password" value={String(form.intake_smtp_password ?? '')}
              onChange={(value) => onChange('intake_smtp_password', value)}
              helperText={loaded.intake_smtp_password ? 'Leave unchanged to keep the saved password.' : 'Leave empty if the server needs no sign-in.'} />}
          </Box>
          {text('Sent from', 'intake_email_from', 'For example, fax@example.org.')}
          {text('Recipients', 'intake_email_to', 'Email addresses, separated by commas.')}
          {text('Subject', 'intake_email_subject', SUBJECT_HELP)}
        </ResponsiveFormSection>
      )}
      {shows('email') && <EmailDelivery client={client} canWrite={canWrite} />}
      </Box>
      )}

      <DirectCardDialog card={card} onClose={() => setCard(null)} onCopied={() => { setCard(null); setNotice('Card copied.'); }} />
    </>
  );
}

// The Setup Wizard's delivery step: the same settings, in the wizard's plain fields.
export function DeliveryWizardFields({ settings, config, baseline, onChange, outbound, disabled }: {
  settings: Settings;
  config: Values;
  baseline: Values;
  onChange: (field: string, value: FormValue) => void;
  outbound: string;
  disabled?: boolean;
}) {
  const field = (label: string, name: string, helperText?: string, number = false) => (
    <TextField fullWidth disabled={disabled} label={label} value={config[name] ?? ''} type={number ? 'number' : 'text'}
      helperText={helperText} sx={{ mt: 2 }}
      onChange={(event) => onChange(name, number && event.target.value !== '' ? Number(event.target.value) : event.target.value)} />
  );
  if (!settings.routing && !settings.direct && !settings.intake) {
    return <Alert severity="info">This server has no delivery options to set up here.</Alert>;
  }
  return (
    <Stack spacing={3}>
      {settings.routing && (
        <Box>
          <Typography variant="subtitle1" gutterBottom>Delivery routes</Typography>
          <RouteOrderEditor value={config.outbound_routes} onChange={(next) => onChange('outbound_routes', next)} disabled={disabled}
            available={providersSetUp(config, settings.sip.ami_password_is_default && config.ami_password === baseline.ami_password)}
            outbound={outbound} />
        </Box>
      )}
      {settings.direct && (
        <Box>
          <Typography variant="subtitle1">Direct delivery</Typography>
          <FormControlLabel control={<Switch disabled={disabled} checked={Boolean(config.direct_delivery_enabled)}
            onChange={(event) => onChange('direct_delivery_enabled', event.target.checked)} />} label="Use direct delivery with other Faxbot installations" />
          {field('Organization name', 'direct_organization', 'The name partners see on your card.')}
          {field('Our fax number', 'direct_fax_number', numberHint(settingsNumberFormat(settings), 'The number partners fax you at'))}
        </Box>
      )}
      {settings.intake && (
        <Box>
          <Typography variant="subtitle1">Email received faxes</Typography>
          <FormControlLabel control={<Switch disabled={disabled} checked={Boolean(config.intake_email_enabled)}
            onChange={(event) => onChange('intake_email_enabled', event.target.checked)} />} label="Email each received fax" />
          {config.intake_email_enabled && <>
            {field('Email server', 'intake_smtp_host')}
            {field('Port', 'intake_smtp_port', 'Usually 587, or 465. Your email provider says which.', true)}
            <TextField select fullWidth disabled={disabled} label="Security" value={config.intake_smtp_security ?? 'starttls'} sx={{ mt: 2 }}
              onChange={(event) => onChange('intake_smtp_security', event.target.value)} SelectProps={{ native: true }} InputLabelProps={{ shrink: true }}>
              {SECURITY_OPTIONS.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
            </TextField>
            {field('User name', 'intake_smtp_username')}
            {environmentManaged(settings).has('intake_smtp_password') ? <EnvSetField fullWidth label="Email password" sx={{ mt: 2 }} /> :
            <SecretInput fullWidth disabled={disabled} label="Email password" value={String(config.intake_smtp_password ?? '')} sx={{ mt: 2 }}
              onChange={(value) => onChange('intake_smtp_password', value)}
              helperText={baseline.intake_smtp_password ? 'Leave unchanged to keep the saved password.' : undefined} />}
            {field('Sent from', 'intake_email_from', 'For example, fax@example.org.')}
            {field('Recipients', 'intake_email_to', 'Email addresses, separated by commas.')}
          </>}
        </Box>
      )}
    </Stack>
  );
}
