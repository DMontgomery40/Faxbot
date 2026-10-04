import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import AdminAPIClient from '../api/client';
import Settings from '../components/Settings';
import SetupWizard from '../components/SetupWizard';
import { server } from '../test/server';

type Json = Record<string, any>;

const client = () => new AdminAPIClient({ kind: 'key', key: 'synthetic-key' });

function settingsFixture(overrides: (data: Json) => void = () => undefined): Json {
  const data: Json = {
    backend: { type: 'phaxio', disabled: false },
    hybrid: { outbound_backend: 'phaxio', inbound_backend: 'phaxio', outbound_override: '', inbound_override: '' },
    phaxio: { api_key: '***', api_secret: '***', callback_token: '', callback_url: '', verify_signature: true, configured: true },
    documo: { api_key: '', base_url: 'https://api.documo.com', sandbox: false, configured: false },
    humblefax: { access_key: '***', secret_key: '***', from_number: '', configured: true },
    sinch: { project_id: '', base_url: '', api_key: '', api_secret: '', configured: false },
    signalwire: { space_url: '', project_id: '', api_token: '', from_fax: '', from_sms: '', callback_url: '',
      webhook_signing_key: '', status_poll_seconds: 0, configured: false },
    fs: { esl_host: '127.0.0.1', esl_port: 8021, esl_password: '***', gateway_name: 'gw', caller_id_number: '', t38_enable: true },
    sip: { ami_host: 'asterisk', ami_port: 5038, ami_username: 'api', ami_password: '***', ami_password_is_default: false,
      station_id: '***', configured: true },
    security: { api_key: '', require_api_key: false, enforce_https: true, audit_enabled: false, public_api_url: 'https://fax.example' },
    audit: { enabled: false, format: 'json', file: '', syslog: false, syslog_address: '/dev/log' },
    mcp: { sse_enabled: false, sse_path: '/mcp/sse', http_enabled: false, http_path: '/mcp/http', require_oauth: false,
      oauth: { issuer: '', audience: '', jwks_url: '' } },
    persisted: { enabled: false, path: '/faxdata/faxbot.env' },
    features: { v3_plugins: false, fax_disabled: false, inbound_enabled: false, plugin_install: false },
    storage: { backend: 'local', s3_bucket: '', s3_prefix: '', s3_region: '', s3_endpoint_url: '', s3_kms_key_id: '', s3_kms_enabled: false },
    database: { url: '***', scheme: 'sqlite', persistent: true, editable: false, maintenance_required: true },
    routing: { outbound_routes: 'sip, humblefax', min_success_percent: 80 },
    intake: { email_enabled: true, smtp_host: 'smtp.example.org', smtp_port: 587, smtp_security: 'starttls', smtp_username: 'fax',
      smtp_password: '***', email_from: 'fax@example.org', email_to: 'desk@example.org', email_subject: 'Fax from {from_number}' },
    direct: { enabled: false, organization: 'County Clinic', fax_number: '+12025550123' },
    inbound: { enabled: false, retention_days: 30, token_ttl_minutes: 60, sip: { asterisk_secret: '', configured: false },
      phaxio: { verify_signature: true }, sinch: { verify_signature: true, basic_auth_configured: false, hmac_configured: false } },
    limits: { max_file_size_mb: 10, pdf_token_ttl_minutes: 60, rate_limit_rpm: 0, inbound_list_rpm: 30, inbound_get_rpm: 60,
      artifact_ttl_days: 0, cleanup_interval_minutes: 1440 },
    _meta: { active_revision_id: 'rev-a', desired_revision_id: 'rev-a', generation: 4, apply_state: 'applied', pending_fields: [] },
  };
  overrides(data);
  return data;
}

const receipt = { ok: true, changed: true, _meta: {
  active_revision_id: 'rev-b', desired_revision_id: 'rev-b', generation: 5, apply_state: 'applied', restart_recommended: false,
} };

function settingsHandlers(data: Json, put: (body: Json) => Response | null = () => null) {
  const writes: Json[] = [];
  server.use(
    http.get('/admin/settings', () => HttpResponse.json(data)),
    http.get('/admin/tunnel/status', () => HttpResponse.json({ enabled: false, provider: 'none', status: 'disabled' })),
    http.put('/admin/settings', async ({ request }) => {
      const body = await request.json() as Json;
      writes.push(body);
      return put(body) ?? HttpResponse.json(receipt);
    }),
    http.get('/direct/card', () => HttpResponse.json({ card: {
      faxbot_direct: 1, organization: 'County Clinic', fax_number: '+12025550123', endpoint: 'https://fax.example',
      signing_key: 'public-signing', exchange_key: 'public-exchange', signature: 'signed',
    } })),
  );
  return writes;
}

const apply = () => fireEvent.click(screen.getByRole('button', { name: 'Apply settings' }));
const section = async (title: string) => (await screen.findByText(title)).closest('.MuiPaper-root') as HTMLElement;

describe('Settings delivery routes', () => {
  it('shows the saved routes in order and does not count loading as a change', async () => {
    settingsHandlers(settingsFixture());
    render(<Settings client={client()} />);
    const routes = within(await section('Delivery routes')).getByRole('list', { name: 'Extra outbound routes' });
    expect(within(routes).getAllByRole('listitem').map((item) => item.textContent)).toEqual([
      expect.stringContaining('1. Your SIP trunk (Asterisk)'), expect.stringContaining('2. HumbleFax'),
    ]);
    expect((screen.getByRole('button', { name: 'Apply settings' }) as HTMLButtonElement).disabled).toBe(true);
  });

  it('reorders, adds and removes routes and saves the list with the minimum delivery rate', async () => {
    const writes = settingsHandlers(settingsFixture());
    render(<Settings client={client()} />);
    const routes = await section('Delivery routes');
    fireEvent.click(within(routes).getByRole('button', { name: 'Move HumbleFax up' }));
    fireEvent.click(within(routes).getByRole('button', { name: 'Remove Your SIP trunk (Asterisk)' }));
    // The outbound provider itself and unconfigured providers are not offered.
    const add = within(routes).getByLabelText('Add a route');
    expect([...add.querySelectorAll('option')].map((option) => option.textContent)).toEqual(['Choose a provider…', 'Your SIP trunk (Asterisk)']);
    fireEvent.change(add, { target: { value: 'sip' } });
    fireEvent.change(within(routes).getByLabelText('Minimum delivery rate (%)'), { target: { value: '90' } });
    apply();
    expect(await screen.findByText('Settings saved.')).toBeTruthy();
    expect(writes[0]).toEqual({ expected_revision_id: 'rev-a', outbound_routes: 'humblefax,sip', route_min_success_percent: 90 });
  });
});

describe('Settings direct delivery', () => {
  it('saves the direct delivery identity and shows our card', async () => {
    const writes = settingsHandlers(settingsFixture());
    render(<Settings client={client()} />);
    const direct = await section('Direct delivery');
    expect(within(direct).getByText(/Kept on this server in a private file in the fax data folder/)).toBeTruthy();
    fireEvent.click(within(direct).getByRole('button', { name: 'Show our card' }));
    const dialog = await screen.findByRole('dialog', { name: 'Our direct delivery card' });
    expect((within(dialog).getByRole('textbox') as HTMLTextAreaElement).value).toContain('"organization": "County Clinic"');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());

    fireEvent.click(within(direct).getByRole('checkbox', { name: 'Use direct delivery' }));
    fireEvent.change(within(direct).getByLabelText('Organization name'), { target: { value: 'County Clinic East' } });
    expect((within(direct).getByRole('button', { name: 'Show our card' }) as HTMLButtonElement).disabled).toBe(true);
    expect(within(direct).getByText('Apply your changes to see them on the card.')).toBeTruthy();
    apply();
    await screen.findByText('Settings saved.');
    expect(writes[0]).toEqual({ expected_revision_id: 'rev-a', direct_delivery_enabled: true, direct_organization: 'County Clinic East' });
  });

  it('shows the plain reason when the card cannot be made yet', async () => {
    settingsHandlers(settingsFixture());
    server.use(http.get('/direct/card', () => HttpResponse.json(
      { detail: 'Add your organization name for direct delivery to the installation settings first.' }, { status: 409 })));
    render(<Settings client={client()} />);
    fireEvent.click(within(await section('Direct delivery')).getByRole('button', { name: 'Show our card' }));
    expect(await screen.findByText('Add your organization name for direct delivery to the installation settings first.')).toBeTruthy();
  });
});

describe('A new installation with no fax provider', () => {
  const noProvider = () => settingsFixture((data) => {
    data.backend.type = '';
    data.hybrid = { outbound_backend: '', inbound_backend: '', outbound_override: '', inbound_override: '' };
  });

  it('says so on Settings, with the provider setup link', async () => {
    settingsHandlers(noProvider());
    render(<Settings client={client()} />);
    const notice = await screen.findByTestId('no-provider');
    expect(notice.textContent).toBe('No fax provider set up yet. Provider setup');
    expect(within(notice).getByRole('link', { name: 'Provider setup' }).getAttribute('href')).toMatch(/\/setup\/$/);
    expect(screen.queryByText(/Inherit default provider \(\)/)).toBeNull();
  });

  it('says so in the Setup Wizard', async () => {
    settingsHandlers(noProvider());
    server.use(http.get('/plugins', () => HttpResponse.json({ items: [] })));
    render(<SetupWizard client={client()} />);
    expect((await screen.findByTestId('no-provider')).textContent).toBe('No fax provider set up yet. Provider setup');
    expect(screen.queryByText(/Outbound: ·/)).toBeNull();
  });
});

describe('Plain wording on provider and security settings', () => {
  const ENV_NAME = /[A-Z]{3,}_[A-Z_]{2,}/;

  it.each(['phaxio', 'documo', 'humblefax', 'sip', 'signalwire'])(
    'shows no environment variable names and no switch to turn authentication off (%s)', async (provider) => {
      settingsHandlers(settingsFixture((data) => {
        data.backend.type = provider;
        data.hybrid = { outbound_backend: provider, inbound_backend: provider === 'humblefax' ? 'phaxio' : provider,
          outbound_override: '', inbound_override: '' };
        data.inbound.enabled = true;
      }));
      const { container } = render(<Settings client={client()} />);
      await screen.findByText('Security Settings');
      for (const input of Array.from(container.querySelectorAll('input, textarea'))) {
        expect(input.getAttribute('placeholder') ?? '').not.toMatch(ENV_NAME);
      }
      for (const label of Array.from(container.querySelectorAll('label'))) {
        expect(label.textContent ?? '').not.toMatch(ENV_NAME);
      }
      expect(screen.queryByRole('checkbox', { name: /API Key Required|Require API Key/i })).toBeNull();
      expect(screen.queryByText(/API Key Required|Require API Key/i)).toBeNull();
    });
});

describe('MCP OAuth wording', () => {
  it('labels the OAuth fields without environment variable names', async () => {
    const data = settingsFixture((fixture) => {
      fixture.mcp = { sse_enabled: false, sse_path: '/mcp/sse', http_enabled: true, http_path: '/mcp/http',
        require_oauth: true, oauth: { issuer: 'https://id.example', audience: 'faxbot', jwks_url: '' } };
    });
    settingsHandlers(data);
    server.use(http.get('/admin/config', () => HttpResponse.json({ mcp: data.mcp })));
    const { default: MCP } = await import('../components/MCP');
    render(<MCP client={client()} />);
    expect(await screen.findByLabelText('Issuer')).toBeTruthy();
    expect(screen.getByLabelText('Audience')).toBeTruthy();
    expect(screen.getByLabelText('JWKS URL')).toBeTruthy();
    expect(screen.queryByText(/OAUTH_/)).toBeNull();
  });
});

describe('Credentials set in .env', () => {
  const fromEnvironment = (names: string[]) => settingsFixture((data) => {
    data.backend.type = 'humblefax';
    data.hybrid = { outbound_backend: 'humblefax', inbound_backend: 'humblefax', outbound_override: '', inbound_override: '' };
    data._meta.env_managed = names;
  });

  it('shows them as set in .env on Settings, disabled and without a reveal button', async () => {
    settingsHandlers(fromEnvironment(['humblefax_access_key', 'intake_smtp_password']));
    render(<Settings client={client()} />);
    const fields = await screen.findAllByDisplayValue('Set in .env');
    expect(fields.length).toBeGreaterThanOrEqual(2);
    for (const field of fields) expect((field as HTMLInputElement).disabled).toBe(true);
    expect(screen.getAllByText('Change it in .env and restart Faxbot.').length).toBeGreaterThanOrEqual(2);
    expect(screen.queryByRole('button', { name: /Show Email password/ })).toBeNull();
    // The secret key is not set in .env and stays editable.
    expect(screen.getAllByPlaceholderText('Secret key').every((input) => !(input as HTMLInputElement).disabled)).toBe(true);
    expect(screen.queryAllByPlaceholderText('Access key')).toHaveLength(0);
  });

  it('shows them as set in .env in the Setup Wizard', async () => {
    settingsHandlers(fromEnvironment(['humblefax_access_key']));
    server.use(http.get('/plugins', () => HttpResponse.json({ items: [] })));
    render(<SetupWizard client={client()} />);
    await screen.findByText('Choose Providers', { selector: 'h6' });
    fireEvent.click(screen.getByRole('button', { name: 'Next' }));
    const access = await screen.findByLabelText('Access Key');
    expect((access as HTMLInputElement).value).toBe('Set in .env');
    expect((access as HTMLInputElement).disabled).toBe(true);
    expect((screen.getByLabelText('Secret Key') as HTMLInputElement).disabled).toBe(false);
  });
});

describe('Settings outbound provider limits', () => {
  it('says HumbleFax sends only to US and Canadian numbers when it sends faxes', async () => {
    settingsHandlers(settingsFixture((data) => {
      data.hybrid.outbound_backend = 'humblefax';
      data.hybrid.outbound_override = 'humblefax';
    }));
    render(<Settings client={client()} />);
    expect((await screen.findByTestId('humblefax-countries')).textContent)
      .toBe('HumbleFax sends only to US and Canadian numbers.');
  });

  it('says nothing about countries for other outbound providers', async () => {
    settingsHandlers(settingsFixture());
    render(<Settings client={client()} />);
    await screen.findAllByText('Outbound Provider');
    expect(screen.queryByTestId('humblefax-countries')).toBeNull();
  });
});

describe('Settings intake defaults', () => {
  it('keeps the saved password unless it is replaced, and saves connector fields', async () => {
    const writes = settingsHandlers(settingsFixture());
    render(<Settings client={client()} />);
    const intake = await section('Intake defaults');
    expect(within(intake).getByRole('button', { name: 'Show Email password' })).toBeTruthy();
    expect(within(intake).getByText('Leave unchanged to keep the saved password.')).toBeTruthy();
    fireEvent.change(within(intake).getByLabelText('Email server'), { target: { value: 'mail.example.org' } });
    apply();
    await screen.findByText('Settings saved.');
    expect(writes[0]).toEqual({ expected_revision_id: 'rev-a', intake_smtp_host: 'mail.example.org' });
  });

  it('sends a replaced password', async () => {
    const writes = settingsHandlers(settingsFixture());
    render(<Settings client={client()} />);
    const intake = await section('Intake defaults');
    fireEvent.change(within(intake).getByLabelText('Email password'), { target: { value: 'new-mail-secret' } });
    apply();
    await screen.findByText('Settings saved.');
    expect(writes[0]).toEqual({ expected_revision_id: 'rev-a', intake_smtp_password: 'new-mail-secret' });
  });
});

describe('Settings save status', () => {
  it('keeps edits and says someone else changed the settings on a conflict', async () => {
    settingsHandlers(settingsFixture(), () => HttpResponse.json({ detail: 'Configuration changed.' }, { status: 409 }));
    render(<Settings client={client()} />);
    fireEvent.change(within(await section('Delivery routes')).getByLabelText('Minimum delivery rate (%)'), { target: { value: '70' } });
    apply();
    expect(await screen.findByText('Someone else changed these settings. Your edits are kept here; reload to see the current values.')).toBeTruthy();
  });

  it('checks the minimum delivery rate and email server port before saving', async () => {
    const writes = settingsHandlers(settingsFixture());
    render(<Settings client={client()} />);
    fireEvent.change(within(await section('Delivery routes')).getByLabelText('Minimum delivery rate (%)'), { target: { value: '150' } });
    apply();
    expect(await screen.findByText('Enter a minimum delivery rate from 0 to 100.')).toBeTruthy();
    fireEvent.change(within(await section('Delivery routes')).getByLabelText('Minimum delivery rate (%)'), { target: { value: '80' } });
    fireEvent.change(within(await section('Intake defaults')).getByLabelText('Port'), { target: { value: '70000' } });
    apply();
    expect(await screen.findByText('Enter an email server port from 1 to 65535.')).toBeTruthy();
    expect(writes).toEqual([]);
  });

  it('says so in one sentence when the account may not change these settings', async () => {
    settingsHandlers(settingsFixture(), () => HttpResponse.json({ detail: 'This operation is not permitted.' }, { status: 403 }));
    render(<Settings client={client()} />);
    fireEvent.change(within(await section('Intake defaults')).getByLabelText('Email server'), { target: { value: 'x.example.org' } });
    apply();
    expect(await screen.findByText('You do not have permission to change some of these settings. Your edits are kept here; reload before trying again.')).toBeTruthy();
  });
});

describe('Settings authentication and receiving', () => {
  it('states that authentication is required and never offers to turn it off', async () => {
    settingsHandlers(settingsFixture());
    render(<Settings client={client()} />);
    expect(await screen.findByText('Every request needs a signed-in person or an API key; manage them in Keys and Users.')).toBeTruthy();
    expect(screen.queryByText('API Key Required')).toBeNull();
    expect(screen.queryByText(/Yes \(Required\)/)).toBeNull();
  });

  it('warns that HumbleFax cannot receive when it would handle receiving', async () => {
    settingsHandlers(settingsFixture((data) => {
      data.backend.type = 'humblefax';
      data.hybrid = { outbound_backend: 'humblefax', inbound_backend: 'humblefax', outbound_override: '', inbound_override: '' };
      data.inbound.enabled = true;
    }));
    render(<Settings client={client()} />);
    const warning = /HumbleFax cannot receive faxes/;
    expect(await screen.findByText(warning)).toBeTruthy();
    const inbound = await section('Inbound Receiving');
    fireEvent.change(within(inbound).getByLabelText('Enable Inbound'), { target: { value: 'false' } });
    await waitFor(() => expect(screen.queryByText(warning)).toBeNull());
  });

  it('does not warn when another provider receives', async () => {
    settingsHandlers(settingsFixture((data) => {
      data.backend.type = 'humblefax';
      data.hybrid = { outbound_backend: 'humblefax', inbound_backend: 'sip', outbound_override: '', inbound_override: 'sip' };
      data.inbound.enabled = true;
    }));
    render(<Settings client={client()} />);
    await section('Delivery routes');
    expect(screen.queryByText(/HumbleFax cannot receive faxes/)).toBeNull();
  });
});

describe('Setup Wizard delivery options', () => {
  const next = () => fireEvent.click(screen.getByRole('button', { name: 'Next' }));

  it('states authentication is required and saves direct delivery and intake email from the delivery step', async () => {
    const writes = settingsHandlers(settingsFixture((data) => { data.intake.email_enabled = false; }));
    server.use(http.get('/plugins', () => HttpResponse.json({ items: [] })));
    render(<SetupWizard client={client()} />);
    await screen.findByText('Choose Providers', { selector: 'h6' });
    next();
    next();
    expect(await screen.findByText(/Authentication: required\./)).toBeTruthy();
    expect(screen.queryByLabelText('Require API Key')).toBeNull();
    next();
    expect(await screen.findByText('Delivery Options', { selector: 'h6' })).toBeTruthy();
    expect(screen.getByRole('list', { name: 'Extra outbound routes' })).toBeTruthy();
    fireEvent.change(screen.getByLabelText('Our fax number'), { target: { value: '+12025550199' } });
    fireEvent.click(screen.getByRole('checkbox', { name: 'Email each received fax' }));
    fireEvent.change(screen.getByLabelText('Port'), { target: { value: '465' } });
    next();
    fireEvent.click(screen.getByRole('button', { name: 'Apply Changes' }));
    await screen.findByText('Settings saved.');
    expect(writes[0]).toEqual({ expected_revision_id: 'rev-a', direct_fax_number: '+12025550199', intake_email_enabled: true, intake_smtp_port: 465 });
  });
});

describe('Installation country', () => {
  const US_NUMBERS = { default_country: 'US', example: { national: '(201) 555-0123', international: '+1 201-555-0123' },
    supported_countries: ['US', 'GB', 'DE', 'FR'] };
  const GB_NUMBERS = { default_country: 'GB', example: { national: '0121 234 5678', international: '+44 121 234 5678' },
    supported_countries: ['US', 'GB', 'DE', 'FR'] };
  const countryField = () => screen.findByRole('combobox', { name: 'Installation country' });

  async function chooseCountry(name: string, typed = name.slice(0, 6)) {
    const field = await countryField();
    fireEvent.change(field, { target: { value: typed } });
    fireEvent.click(within(await screen.findByRole('listbox')).getByRole('option', { name }));
  }

  it('lists countries by name and saves the United Kingdom from Settings', async () => {
    const writes = settingsHandlers(settingsFixture((data) => { data.numbers = US_NUMBERS; }));
    render(<Settings client={client()} />);
    const field = await countryField();
    await waitFor(() => expect((field as HTMLInputElement).value).toBe('United States'));
    expect(screen.getByText('Current: United States')).toBeTruthy();
    expect(screen.getByText('The number partners fax you at, for example (201) 555-0123 or +1 201-555-0123.')).toBeTruthy();

    fireEvent.mouseDown(field);
    const names = within(await screen.findByRole('listbox')).getAllByRole('option').map((option) => option.textContent);
    expect(names).toEqual(['France', 'Germany', 'United Kingdom', 'United States']);
    fireEvent.click(screen.getByRole('option', { name: 'United Kingdom' }));
    expect((field as HTMLInputElement).value).toBe('United Kingdom');
    apply();
    await screen.findByText('Settings saved.');
    expect(writes).toEqual([{ expected_revision_id: 'rev-a', fax_default_country: 'GB' }]);
  });

  it('shows UK examples for a UK installation', async () => {
    settingsHandlers(settingsFixture((data) => { data.numbers = GB_NUMBERS; }));
    render(<Settings client={client()} />);
    const field = await countryField();
    await waitFor(() => expect((field as HTMLInputElement).value).toBe('United Kingdom'));
    expect(screen.getByText('The number partners fax you at, for example 0121 234 5678 or +44 121 234 5678.')).toBeTruthy();
    expect(screen.queryByText(/E\.164|\+15551234567|\+13035551234/)).toBeNull();
  });

  it('shows the server sentence when a number cannot be read', async () => {
    const detail = 'Enter the full fax number with its area code, or with its country code starting with +.';
    settingsHandlers(settingsFixture((data) => { data.numbers = GB_NUMBERS; }),
      () => HttpResponse.json({ detail }, { status: 400 }));
    render(<Settings client={client()} />);
    const direct = await section('Direct delivery');
    fireEvent.change(within(direct).getByLabelText('Our fax number'), { target: { value: '684953' } });
    apply();
    expect(await screen.findByText(`${detail} Your edits are kept here; reload before trying again.`)).toBeTruthy();
  });

  it('chooses the country in the Setup Wizard and keeps a UK fax number as typed until the server saves it', async () => {
    const data = settingsFixture((fixture) => { fixture.numbers = US_NUMBERS; });
    const writes = settingsHandlers(data, () => {
      data.numbers = GB_NUMBERS;
      data.direct.fax_number = '+441782684953';
      return null;
    });
    server.use(http.get('/plugins', () => HttpResponse.json({ items: [] })));
    const next = () => fireEvent.click(screen.getByRole('button', { name: 'Next' }));
    render(<SetupWizard client={client()} />);
    await screen.findByText('Choose Providers', { selector: 'h6' });
    await chooseCountry('United Kingdom', 'GB');
    next();
    next();
    next();
    await screen.findByText('Delivery Options', { selector: 'h6' });
    fireEvent.change(screen.getByLabelText('Our fax number'), { target: { value: '01782 684953' } });
    next();
    fireEvent.click(screen.getByRole('button', { name: 'Apply Changes' }));
    await screen.findByText('Settings saved.');
    expect(writes[0]).toEqual({ expected_revision_id: 'rev-a', fax_default_country: 'GB', direct_fax_number: '01782 684953' });
    fireEvent.click(screen.getByRole('button', { name: 'Back' }));
    await waitFor(() => expect((screen.getByLabelText('Our fax number') as HTMLInputElement).value).toBe('+441782684953'));
    expect(screen.getByText('The number partners fax you at, for example 0121 234 5678 or +44 121 234 5678.')).toBeTruthy();
  });
});

describe('Settings email delivery', () => {
  function connectorHandlers(calls: Json[]) {
    server.use(
      http.get('/intake/connectors', () => HttpResponse.json({ connectors: [
        { id: 'c-1', kind: 'email', name: 'Front desk', enabled: true, match_number: null, host: 'smtp.example.org',
          port: 587, security: 'starttls', username: 'fax', has_password: true, from_address: 'fax@example.org',
          recipients: ['frontdesk@example.org'], subject_template: 'Fax from {from_number}', managed: false, version: 2 },
        { id: 'c-2', kind: 'email', name: 'Email from installation settings', enabled: true, match_number: null,
          host: 'smtp.example.org', port: 587, security: 'starttls', username: 'fax', has_password: true,
          from_address: 'fax@example.org', recipients: ['desk@example.org'], subject_template: 'Fax from {from_number}',
          managed: true, version: 1 },
      ] })),
      http.post('/intake/connectors', async ({ request }) => {
        calls.push(await request.json() as Json);
        return HttpResponse.json({}, { status: 201 });
      }),
      http.post('/intake/connectors/:id/test', () => HttpResponse.json({ ok: false, detail: 'Faxbot could not reach the email server.' })),
    );
  }

  it('lists email deliveries, reports a failed test in a plain sentence and adds one', async () => {
    const calls: Json[] = [];
    settingsHandlers(settingsFixture());
    connectorHandlers(calls);
    render(<Settings client={client()} canWrite />);
    const delivery = await section('Email delivery');
    expect(await within(delivery).findByText('Front desk')).toBeTruthy();
    expect(within(delivery).getByText(/Set in Intake defaults above\./)).toBeTruthy();
    fireEvent.click(within(delivery).getAllByRole('button', { name: 'Send test email' })[0]);
    expect(await screen.findByText('Faxbot could not reach the email server.')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Add email delivery' }));
    const dialog = await screen.findByRole('dialog', { name: 'Add email delivery' });
    fireEvent.change(within(dialog).getByLabelText('Name'), { target: { value: 'Billing' } });
    fireEvent.change(within(dialog).getByLabelText('Recipients'), { target: { value: 'a@example.org, b@example.org' } });
    fireEvent.change(within(dialog).getByLabelText('Email server'), { target: { value: 'smtp.example.org' } });
    fireEvent.change(within(dialog).getByLabelText('Sent from'), { target: { value: 'fax@example.org' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save' }));
    expect(await screen.findByText('Email delivery saved.')).toBeTruthy();
    expect(calls[0]).toMatchObject({
      name: 'Billing', recipients: ['a@example.org', 'b@example.org'], host: 'smtp.example.org', port: 587,
      security: 'starttls', from_address: 'fax@example.org', match_number: null,
    });
  });

  it('shows email deliveries read-only to people who cannot change settings', async () => {
    settingsHandlers(settingsFixture());
    connectorHandlers([]);
    render(<Settings client={client()} />);
    expect(await screen.findByText('Front desk')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Add email delivery' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Send test email' })).toBeNull();
  });

  it('leaves the section out when the account cannot read email deliveries', async () => {
    settingsHandlers(settingsFixture());
    server.use(http.get('/intake/connectors', () => HttpResponse.json({ detail: 'Forbidden' }, { status: 403 })));
    render(<Settings client={client()} canWrite />);
    await section('Intake defaults');
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(screen.queryByText('Email delivery')).toBeNull();
    expect(screen.queryByText(/Forbidden/)).toBeNull();
  });

  it('opens at the email delivery settings when asked', async () => {
    settingsHandlers(settingsFixture());
    const scrolled = vi.fn();
    const original = Element.prototype.scrollIntoView;
    Element.prototype.scrollIntoView = function (this: Element) { scrolled(this.id); } as typeof original;
    const focused = vi.fn();
    try {
      render(<Settings client={client()} focus="email" onFocused={focused} />);
      await waitFor(() => expect(focused).toHaveBeenCalled());
      expect(scrolled).toHaveBeenCalledWith('email-delivery');
    } finally {
      Element.prototype.scrollIntoView = original;
    }
  });
});

describe('Settings when Faxbot cannot reach its fax engine', () => {
  const sentence = "Faxbot can't sign in to its fax engine. Check that the Asterisk manager password matches.";

  it('says so in one sentence at the top', async () => {
    settingsHandlers(settingsFixture());
    server.use(http.get('/health/ready', () => HttpResponse.json({ status: 'not_ready', message: sentence }, { status: 503 })));
    render(<Settings client={client()} />);
    expect((await screen.findByTestId('engine-message')).textContent).toBe(sentence);
  });

  it('shows nothing for other readiness reasons', async () => {
    settingsHandlers(settingsFixture());
    server.use(http.get('/health/ready', () => HttpResponse.json({ status: 'not_ready', message: 'Something unexpected.' }, { status: 503 })));
    render(<Settings client={client()} />);
    await screen.findByText('Security Settings');
    expect(screen.queryByTestId('engine-message')).toBeNull();
  });
});
