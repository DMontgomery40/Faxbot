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
      phaxio: { verify_signature: true }, sinch: { basic_auth_configured: false } },
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
    http.put('/admin/settings', async ({ request }) => {
      const body = await request.json() as Json;
      writes.push(body);
      return put(body) ?? HttpResponse.json(receipt);
    }),
    http.get('/direct/card', () => HttpResponse.json({ card: {
      faxbot_direct: 1, organization: 'County Clinic', fax_number: '+12025550123', endpoint: 'https://fax.example',
      signing_key: 'public-signing', exchange_key: 'public-exchange', signature: 'signed',
    } })),
    // The Setup Wizard's Suggested Packs step (BE): no plan suggested yet.
    http.get('/setup/plans/latest', () => HttpResponse.json({ plan: null, mailboxes: [] })),
  );
  return writes;
}

const apply = () => fireEvent.click(screen.getByRole('button', { name: 'Apply settings' }));
const section = async (title: string) => (await screen.findByText(title)).closest('.MuiPaper-root') as HTMLElement;
// The Receiving section, found by its switch (other sections also say "Receiving").
const receivingSection = async () => {
  await screen.findByLabelText(/^Receiving is (on|off)$/);
  return screen.getByTestId('switch-inbound_enabled').closest('.MuiPaper-root') as HTMLElement;
};

describe('Settings delivery routes', () => {
  it('shows the saved routes in order and does not count loading as a change', async () => {
    settingsHandlers(settingsFixture());
    render(<Settings client={client()} />);
    const routes = within(await section('Delivery routes')).getByRole('list', { name: 'Extra outbound routes' });
    expect(within(routes).getAllByRole('listitem').map((item) => item.textContent)).toEqual([
      expect.stringContaining('1. Carrier trunk'), expect.stringContaining('2. HumbleFax'),
    ]);
    expect((screen.getByRole('button', { name: 'Apply settings' }) as HTMLButtonElement).disabled).toBe(true);
  });

  it('reorders, adds and removes routes and saves the list with the minimum delivery rate', async () => {
    const writes = settingsHandlers(settingsFixture());
    render(<Settings client={client()} />);
    const routes = await section('Delivery routes');
    fireEvent.click(within(routes).getByRole('button', { name: 'Move HumbleFax up' }));
    fireEvent.click(within(routes).getByRole('button', { name: 'Remove Carrier trunk' }));
    // The outbound provider itself and unconfigured providers are not offered.
    const add = within(routes).getByLabelText('Add a route');
    expect([...add.querySelectorAll('option')].map((option) => option.textContent)).toEqual(['Choose a provider…', 'Carrier trunk']);
    fireEvent.change(add, { target: { value: 'sip' } });
    fireEvent.change(within(routes).getByLabelText('Minimum delivery rate (%)'), { target: { value: '90' } });
    apply();
    expect(await screen.findByText('Settings saved.')).toBeTruthy();
    expect(writes[0]).toEqual({ expected_revision_id: 'rev-a', outbound_routes: 'humblefax,sip', route_min_success_percent: 90 });
  });

  it('delivers faxes to your own numbers inside Faxbot by default and saves turning it off', async () => {
    const writes = settingsHandlers(settingsFixture());
    render(<Settings client={client()} />);
    const routes = await section('Delivery routes');
    const toggle = within(routes).getByRole('checkbox', { name: 'Deliver faxes to your own numbers inside Faxbot' }) as HTMLInputElement;
    expect(toggle.checked).toBe(true);
    fireEvent.click(toggle);
    apply();
    expect(await screen.findByText('Settings saved.')).toBeTruthy();
    expect(writes[0]).toEqual({ expected_revision_id: 'rev-a', local_delivery_enabled: false });
  });

  it('lightens shaded areas where it saves time by default, explains the choices and saves another', async () => {
    const writes = settingsHandlers(settingsFixture((data) => {
      data.routing = { ...data.routing, fax_friendly_documents: 'where_it_saves' };
    }));
    render(<Settings client={client()} />);
    const routes = await section('Delivery routes');
    const choice = within(routes).getByLabelText(
      'Lighten shaded areas and remove specks on documents you send') as HTMLSelectElement;
    expect(choice.value).toBe('where_it_saves');
    expect([...choice.querySelectorAll('option')].map((option) => option.textContent)).toEqual(
      ['Where it saves time', 'Always', 'Never']);
    expect(routes.textContent).toContain('changes pages only on calls billed by time');
    expect(routes.textContent).toContain('a page with a shaded table went from 61 to 12 seconds');
    fireEvent.change(choice, { target: { value: 'never' } });
    apply();
    expect(await screen.findByText('Settings saved.')).toBeTruthy();
    expect(writes[0]).toEqual({ expected_revision_id: 'rev-a', fax_friendly_documents: 'never' });
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

describe('Provider plugins on a clean install', () => {
  const NOTICE = /Installed provider plugins could not be listed/;

  it('shows nothing in Setup when plugins are simply turned off', async () => {
    settingsHandlers(settingsFixture());
    server.use(http.get('/plugins', () => HttpResponse.json({ detail: 'v3 plugins feature disabled' }, { status: 404 })));
    render(<SetupWizard client={client()} />);
    await screen.findByText('Choose Providers', { selector: 'h6' });
    expect(screen.queryByText(NOTICE)).toBeNull();
  });

  it('treats only the plugins-off refusal as an empty list', async () => {
    server.use(http.get('/plugins', () => HttpResponse.json({ detail: 'v3 plugins feature disabled' }, { status: 404 })));
    expect(await client().listPlugins()).toEqual({ items: [] });
    server.use(http.get('/plugins', () => HttpResponse.json({ detail: 'Not Found' }, { status: 404 })));
    await expect(client().listPlugins()).rejects.toThrow();
  });
});

describe('Provider names', () => {
  it('shows each provider by its one plain name, never its id', async () => {
    settingsHandlers(settingsFixture((data) => {
      data.backend.type = 'sip';
      data.hybrid = { outbound_backend: 'sip', inbound_backend: 'phaxio', outbound_override: '', inbound_override: 'phaxio' };
      data.inbound.enabled = true;
    }));
    server.use(http.get('/admin/sip/presets', () => HttpResponse.json({ presets: [] })));
    render(<Settings client={client()} />);
    const backend = await section('Fax providers');
    expect(within(backend).getByText('In use: Sending: Carrier trunk · Receiving: Phaxio')).toBeTruthy();
    expect(within(backend).getByRole('combobox', { name: 'Sending' }).textContent).toBe('Carrier trunk');
    expect(within(backend).getByRole('combobox', { name: 'Receiving' }).textContent).toBe('Phaxio');
    expect(backend.textContent).not.toMatch(/\b(sip|phaxio|sinch|signalwire|documo|humblefax|freeswitch)\b|PHAXIO|SIP\/Asterisk|Inherit|Default Provider/);
  });

  it('saves the same two choices as the Setup Wizard', async () => {
    const writes = settingsHandlers(settingsFixture());
    server.use(http.get('/admin/sip/presets', () => HttpResponse.json({ presets: [] })));
    render(<Settings client={client()} />);
    const backend = await section('Fax providers');
    fireEvent.mouseDown(within(backend).getByRole('combobox', { name: 'Sending' }));
    fireEvent.click(within(await screen.findByRole('listbox')).getByRole('option', { name: 'HumbleFax' }));
    fireEvent.mouseDown(within(backend).getByRole('combobox', { name: 'Receiving' }));
    fireEvent.click(within(await screen.findByRole('listbox')).getByRole('option', { name: 'Carrier trunk' }));
    apply();
    await screen.findByText(/Settings saved\./);
    expect(writes[0]).toEqual({ expected_revision_id: 'rev-a', backend: 'humblefax', inbound_backend: 'sip', inbound_enabled: true });
  });

  it('refuses receiving without a sending provider in one sentence', async () => {
    const writes = settingsHandlers(settingsFixture());
    render(<Settings client={client()} />);
    const backend = await section('Fax providers');
    fireEvent.mouseDown(within(backend).getByRole('combobox', { name: 'Sending' }));
    fireEvent.click(within(await screen.findByRole('listbox')).getByRole('option', { name: 'No provider' }));
    fireEvent.mouseDown(within(backend).getByRole('combobox', { name: 'Receiving' }));
    fireEvent.click(within(await screen.findByRole('listbox')).getByRole('option', { name: 'Phaxio' }));
    apply();
    expect((await screen.findAllByText('Choose a provider for sending as well; Faxbot needs one even when it mainly receives.')).length)
      .toBeGreaterThan(0);
    expect(writes).toEqual([]);
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
      await screen.findByText('How people sign in and how this server is reached.');
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
    expect(screen.getAllByText('Change it in .env, then run docker compose up -d.').length).toBeGreaterThanOrEqual(2);
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
    await section('Fax providers');
    expect(screen.queryByTestId('humblefax-countries')).toBeNull();
  });
});

describe('Settings intake defaults', () => {
  it('keeps the saved password unless it is replaced, and saves connector fields', async () => {
    const writes = settingsHandlers(settingsFixture());
    render(<Settings client={client()} />);
    const intake = await section('Email delivery for the whole installation');
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
    const intake = await section('Email delivery for the whole installation');
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

  it('says one plain sentence when a save fails on the server, never the raw error', async () => {
    settingsHandlers(settingsFixture(), () => HttpResponse.json({ detail: 'Internal Server Error' }, { status: 500 }));
    render(<Settings client={client()} />);
    fireEvent.change(within(await section('Delivery routes')).getByLabelText('Minimum delivery rate (%)'), { target: { value: '70' } });
    apply();
    expect(await screen.findByText('The save could not be confirmed. Reload to check whether your changes were saved.')).toBeTruthy();
    expect(document.body.textContent).not.toMatch(/API Error|500/);
  });

  it('checks the minimum delivery rate and email server port before saving', async () => {
    const writes = settingsHandlers(settingsFixture());
    render(<Settings client={client()} />);
    fireEvent.change(within(await section('Delivery routes')).getByLabelText('Minimum delivery rate (%)'), { target: { value: '150' } });
    apply();
    expect(await screen.findByText('Enter a minimum delivery rate from 0 to 100.')).toBeTruthy();
    fireEvent.change(within(await section('Delivery routes')).getByLabelText('Minimum delivery rate (%)'), { target: { value: '80' } });
    fireEvent.change(within(await section('Email delivery for the whole installation')).getByLabelText('Port'), { target: { value: '70000' } });
    apply();
    expect(await screen.findByText('Enter an email server port from 1 to 65535.')).toBeTruthy();
    expect(writes).toEqual([]);
  });

  it('says so in one sentence when the account may not change these settings', async () => {
    settingsHandlers(settingsFixture(), () => HttpResponse.json({ detail: 'This operation is not permitted.' }, { status: 403 }));
    render(<Settings client={client()} />);
    fireEvent.change(within(await section('Email delivery for the whole installation')).getByLabelText('Email server'), { target: { value: 'x.example.org' } });
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

  it('receives through HumbleFax when it handles receiving', async () => {
    settingsHandlers(settingsFixture((data) => {
      data.backend.type = 'humblefax';
      data.hybrid = { outbound_backend: 'humblefax', inbound_backend: 'humblefax', outbound_override: '', inbound_override: '' };
      data.inbound.enabled = true;
    }));
    render(<Settings client={client()} />);
    expect(await screen.findByText('HumbleFax is your receiving provider, so Faxbot collects the faxes it receives.')).toBeTruthy();
    expect(screen.queryByText(/HumbleFax cannot receive faxes/)).toBeNull();
    const inbound = await receivingSection();
    expect(within(inbound).getByText('Faxes arrive through HumbleFax.')).toBeTruthy();
    fireEvent.click(within(inbound).getByLabelText('Receiving is on'));
    // Off, it can be turned on again: HumbleFax receives by Faxbot asking it for faxes.
    expect(within(inbound).getByText('Turn this on to receive faxes through HumbleFax.')).toBeTruthy();
    expect((within(inbound).getByLabelText('Receiving is off') as HTMLInputElement).disabled).toBe(false);
  });

  it('turns receiving off and on again with one switch, keeping the trunk as the receiving provider', async () => {
    const writes = settingsHandlers(settingsFixture((data) => {
      data.backend.type = 'humblefax';
      data.hybrid = { outbound_backend: 'humblefax', inbound_backend: 'sip', outbound_override: '', inbound_override: 'sip' };
      data.inbound.enabled = false;
    }));
    render(<Settings client={client()} sections={['providers', 'inbound', 'routes']} canWrite />);
    const inbound = await receivingSection();
    expect(within(inbound).getByText('Turn this on to receive faxes through Carrier trunk.')).toBeTruthy();
    expect(screen.queryByText('Enable Inbound Fax Receiving')).toBeNull();
    expect(screen.queryByText('Feature Flags')).toBeNull();
    fireEvent.click(within(inbound).getByLabelText(/^Receiving is (on|off)$/));
    expect(within(inbound).getByText('Faxes arrive through Carrier trunk.')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Apply settings' }));
    await waitFor(() => expect(writes).toHaveLength(1));
    expect(writes[0]).toEqual({ expected_revision_id: expect.any(String), inbound_enabled: true });
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
    await screen.findByText('Connect Providers', { selector: 'h6' });
    next();
    expect(await screen.findByText(/Authentication: required\./)).toBeTruthy();
    expect(screen.queryByLabelText('Require API Key')).toBeNull();
    next();
    expect(await screen.findByText('Delivery Options', { selector: 'h6' })).toBeTruthy();
    // Steps with no changes save nothing.
    expect(writes).toEqual([]);
    expect(screen.getByRole('list', { name: 'Extra outbound routes' })).toBeTruthy();
    fireEvent.change(screen.getByLabelText('Our fax number'), { target: { value: '+12025550199' } });
    fireEvent.click(screen.getByRole('checkbox', { name: 'Email each received fax' }));
    fireEvent.change(screen.getByLabelText('Port'), { target: { value: '465' } });
    // Moving on saves this step's changes.
    next();
    await screen.findByText('Settings saved.');
    await screen.findByText('Suggested Packs', { selector: 'h6' });
    next();
    expect(await screen.findByText('Finish', { selector: 'h6' })).toBeTruthy();
    expect(writes).toEqual([{ expected_revision_id: 'rev-a', direct_fax_number: '+12025550199', intake_email_enabled: true, intake_smtp_port: 465 }]);
    // The last step offers one test fax, sent only when asked; this installation does not receive.
    expect(screen.getByText('Send a test fax (optional)')).toBeTruthy();
    expect(screen.queryByText('Receive a test fax (optional)')).toBeNull();
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
    await screen.findByText('Connect Providers', { selector: 'h6' });
    expect(writes).toEqual([{ expected_revision_id: 'rev-a', fax_default_country: 'GB' }]);
    next();
    await screen.findByText('Security', { selector: 'h6' });
    next();
    await screen.findByText('Delivery Options', { selector: 'h6' });
    fireEvent.change(screen.getByLabelText('Our fax number'), { target: { value: '01782 684953' } });
    next();
    await screen.findByText('Suggested Packs', { selector: 'h6' });
    expect(writes[1]).toEqual({ expected_revision_id: 'rev-a', direct_fax_number: '01782 684953' });
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
    expect(within(delivery).getByText(/Set in Email delivery for the whole installation, above\./)).toBeTruthy();
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
    await section('Email delivery for the whole installation');
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

  it('opens at the SIP trunk when the Inbox receiving line asks for it', async () => {
    settingsHandlers(settingsFixture((data) => {
      data.backend.type = 'sip';
      data.hybrid = { outbound_backend: 'sip', inbound_backend: 'sip', outbound_override: '', inbound_override: '' };
    }));
    server.use(
      http.get('/admin/sip/presets', () => HttpResponse.json({ presets: [] })),
      http.get('/admin/sip/calls', () => HttpResponse.json({ items: [], next_cursor: null })),
    );
    const scrolled = vi.fn();
    const original = Element.prototype.scrollIntoView;
    Element.prototype.scrollIntoView = function (this: Element) { scrolled(this.id); } as typeof original;
    const focused = vi.fn();
    try {
      render(<Settings client={client()} focus="trunk" onFocused={focused} />);
      await waitFor(() => expect(focused).toHaveBeenCalled());
      expect(scrolled).toHaveBeenCalledWith('sip-trunk');
    } finally {
      Element.prototype.scrollIntoView = original;
    }
  });
});

describe('Settings pending restart', () => {
  const pending = () => settingsFixture((data) => {
    data._meta = { ...data._meta, apply_state: 'pending_restart', pending_fields: ['enable_mcp_http', 'mcp_http_path'] };
  });

  it('offers Restart now, waits for Faxbot to come back and loads the settings again', async () => {
    let loads = 0;
    const health = [false, true];
    let restarts = 0;
    server.use(
      http.get('/admin/settings', () => HttpResponse.json(loads++ === 0 ? pending() : settingsFixture())),
      http.get('/direct/card', () => HttpResponse.json({ detail: 'Not ready.' }, { status: 409 })),
      http.get('/admin/config', () => HttpResponse.json({ allow_restart: true, branding: {} })),
      http.post('/admin/restart', () => { restarts += 1; return HttpResponse.json({ ok: true }); }),
      http.get('/health', () => (health.shift() ?? true) ? HttpResponse.json({ status: 'ok' }) : HttpResponse.error()),
    );
    render(<Settings client={client()} canRestart />);
    const notice = await screen.findByTestId('restart-notice');
    expect(notice.textContent).toContain('Restart Faxbot to apply 2 pending changes.');
    fireEvent.click(within(notice).getByRole('button', { name: 'Restart now' }));
    expect(await screen.findByText('Faxbot restarted and is using the saved settings.', {}, { timeout: 8000 })).toBeTruthy();
    expect(restarts).toBe(1);
    expect(loads).toBeGreaterThanOrEqual(2);
    expect(screen.queryByTestId('restart-notice')).toBeNull();
  });

  it.each([
    ['the account may not restart the server', false, true],
    ['the installation does not allow restarts from the console', true, false],
  ])('says how to restart by hand when %s', async (_case, canRestart, allowRestart) => {
    settingsHandlers(pending());
    server.use(http.get('/admin/config', () => HttpResponse.json({ allow_restart: allowRestart, branding: {} })));
    render(<Settings client={client()} canRestart={canRestart} />);
    const notice = await screen.findByTestId('restart-notice');
    await waitFor(() => expect(notice.textContent).toBe(
      'Restart Faxbot to apply 2 pending changes. Run docker compose restart api on the server.'));
    expect(within(notice).queryByRole('button', { name: 'Restart now' })).toBeNull();
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
    await screen.findByText('How people sign in and how this server is reached.');
    expect(screen.queryByTestId('engine-message')).toBeNull();
  });
});

describe('Settings placed on their own pages', () => {
  it('applies the S3 check and console restarts separately, so one refusal does not undo the other', async () => {
    const writes = settingsHandlers(settingsFixture((data) => {
      data.storage.s3_diagnostics = false;
      data.restart = { allowed: false };
    }), (body) => ('admin_allow_restart' in body
      ? HttpResponse.json({ detail: 'Forbidden' }, { status: 403 }) : null));
    render(<Settings client={client()} sections={['diagnostics']} canWrite />);
    fireEvent.click(await screen.findByLabelText('Also check the S3 bucket'));
    fireEvent.click(screen.getByLabelText('Allow restarting Faxbot from here'));
    expect(screen.queryByRole('button', { name: 'Apply settings' })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Apply: Allow restarting Faxbot from here' }));
    expect(await screen.findByText('You do not have permission to change this setting.')).toBeTruthy();
    expect(writes[0]).toEqual({ expected_revision_id: expect.any(String), admin_allow_restart: true });
    // The S3 change is still there and can be applied on its own.
    expect((screen.getByLabelText('Also check the S3 bucket') as HTMLInputElement).checked).toBe(true);
    fireEvent.click(screen.getByRole('button', { name: 'Apply: Also check the S3 bucket' }));
    await waitFor(() => expect(writes).toHaveLength(2));
    expect(writes[1]).toEqual({ expected_revision_id: expect.any(String), enable_s3_diagnostics: true });
  });

  it('shows owner-only settings disabled, with one sentence, to people who are not the owner', async () => {
    settingsHandlers(settingsFixture((data) => {
      data.restart = { allowed: false };
      data.mobile = { local_base: '' };
      data.owner_only = ['admin_allow_restart', 'mobile_local_base', 'docs_base_url', 'feature_v3_plugins'];
    }));
    const { unmount } = render(<Settings client={client()} sections={['diagnostics']} canWrite isOwner={false} />);
    expect((await screen.findByLabelText('Allow restarting Faxbot from here') as HTMLInputElement).disabled).toBe(true);
    expect((screen.getByLabelText('Also check the S3 bucket') as HTMLInputElement).disabled).toBe(false);
    expect(within(screen.getByTestId('switch-admin_allow_restart')).getByText(/Only the owner of this installation can change this\.$/)).toBeTruthy();
    unmount();
    render(<Settings client={client()} sections={['phones']} canWrite isOwner={false} />);
    expect((await screen.findByRole('textbox') as HTMLInputElement).disabled).toBe(true);
    expect(screen.getByText(/Only the owner of this installation can change this\.$/)).toBeTruthy();
  });

  it('asks before turning sending off, and not before turning it on', async () => {
    const writes = settingsHandlers(settingsFixture());
    render(<Settings client={client()} sections={['providers', 'inbound', 'routes']} canWrite />);
    const sending = await screen.findByLabelText(/^Sending is (on|off)$/) as HTMLInputElement;
    fireEvent.click(sending);
    const dialog = await screen.findByRole('dialog', { name: 'Turn off sending?' });
    expect(within(dialog).getByText('Faxbot will stop sending, and faxes submitted while sending is off stay on hold until you turn it back on.')).toBeTruthy();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(sending.checked).toBe(true);
    fireEvent.click(sending);
    fireEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Turn off sending' }));
    await waitFor(() => expect(sending.checked).toBe(false));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    // Turning it back on needs no confirmation.
    fireEvent.click(sending);
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(sending.checked).toBe(true);
    expect(writes).toEqual([]);
  });

  it('says off beside a switch that is off, as with FAX_DISABLED (click-through, 2026-10-07)', async () => {
    settingsHandlers(settingsFixture((data) => {
      data.backend.disabled = true;
      data.features.fax_disabled = true;
    }));
    render(<Settings client={client()} sections={['providers', 'inbound', 'routes']} canWrite />);
    const sending = await screen.findByLabelText('Sending is off') as HTMLInputElement;
    expect(sending.checked).toBe(false);
    const receiving = screen.getByLabelText('Receiving is off') as HTMLInputElement;
    expect(receiving.checked).toBe(false);
    expect(screen.queryByText('Sending is on')).toBeNull();
    expect(screen.queryByText('Receiving is on')).toBeNull();
    // Turning sending on changes the words with the switch.
    fireEvent.click(sending);
    expect((await screen.findByLabelText('Sending is on') as HTMLInputElement).checked).toBe(true);
  });

  it('sets the address phones use on the local network on Keys & phones', async () => {
    const writes = settingsHandlers(settingsFixture((data) => { data.mobile = { local_base: '' }; }));
    render(<Settings client={client()} sections={['phones']} canWrite />);
    expect(await screen.findByText('Address phones use on your network')).toBeTruthy();
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'http://192.0.2.20:8080' } });
    fireEvent.click(screen.getByRole('button', { name: 'Apply settings' }));
    await waitFor(() => expect(writes).toHaveLength(1));
    expect(writes[0]).toEqual({ expected_revision_id: expect.any(String), mobile_local_base: 'http://192.0.2.20:8080' });
  });

  it('shows the documentation address and the files Faxbot reads in Developer', async () => {
    settingsHandlers(settingsFixture((data) => {
      data.developer = { docs_base_url: 'https://docs.faxbot.net/latest/' };
      data.persisted = { enabled: false, path: '/faxdata/operator.env' };
      data.plugin_files = { providers_dir: '/app/config/providers' };
    }));
    render(<Settings client={client()} sections={['developer']} />);
    expect(await screen.findByText('Documentation address')).toBeTruthy();
    expect(screen.getByDisplayValue('https://docs.faxbot.net/latest/')).toBeTruthy();
    for (const path of ['/faxdata/operator.env', '/app/config/providers']) {
      expect(screen.getByDisplayValue(path)).toBeTruthy();
    }
    expect(screen.queryByText('Plugin registry file')).toBeNull();
  });

  it('keeps the plugin switches on Provider plugins, not on In use', async () => {
    const writes = settingsHandlers(settingsFixture());
    const { unmount } = render(<Settings client={client()} sections={['providers', 'inbound', 'routes']} />);
    await receivingSection();
    expect(screen.queryByText('Use provider plugins')).toBeNull();
    expect(screen.getByLabelText(/^Sending is (on|off)$/)).toBeTruthy();
    unmount();
    render(<Settings client={client()} sections={['plugins']} title="Provider plugins" canWrite />);
    fireEvent.click(await screen.findByLabelText('Use provider plugins'));
    expect(screen.getByText(/Goes away in the next release\./)).toBeTruthy();
    expect((screen.getByLabelText('Allow remote plugin installation (advanced)') as HTMLInputElement).disabled).toBe(true);
    fireEvent.click(screen.getByRole('button', { name: 'Apply settings' }));
    await waitFor(() => expect(writes).toHaveLength(1));
    expect(writes[0]).toEqual({ expected_revision_id: expect.any(String), feature_v3_plugins: true });
  });
});

describe('System, milestone 5', () => {
  // `withheld` names values the server reports as set but never sends.
  const deployment = (values: Record<string, string>, withheld: string[] = []) => Object.fromEntries([
    'FAXBOT_ALLOW_INSECURE_HTTP_SESSIONS', 'FAXBOT_CONSOLE_ORIGINS', 'ENABLE_LOCAL_ADMIN', 'ENABLE_ADMIN_EXEC',
    'FAXBOT_ALLOW_INSECURE_LOOPBACK', 'FAXBOT_INSTALLATION_KEY_PATH', 'FAXBOT_DIRECT_KEY_PATH', 'FAXBOT_MEDIA_PORTS',
    'FAXBOT_PHONE_SYSTEM_ADDRESS', 'MCP_ALLOWED_HOSTS', 'MCP_ALLOWED_ORIGINS', 'MCP_OAUTH_SUBJECT_KEYS_FILE',
    'MCP_RESOURCE_URL', 'MCP_HTTP_PORT', 'TZ',
  ].map((name) => [name, name in values ? { set: true, value: withheld.includes(name) ? null : values[name] } : { set: false, value: null }]));

  it('shows the environment-only security settings with their meaning, and set or not set', async () => {
    settingsHandlers(settingsFixture((data) => {
      data.deployment = deployment({ FAXBOT_CONSOLE_ORIGINS: 'https://fax.clinic.example', ENABLE_LOCAL_ADMIN: 'true' });
    }));
    render(<Settings client={client()} sections={['security']} title="Security" />);
    const rows = await screen.findByTestId('deployment-rows');
    expect(within(rows).getByDisplayValue('https://fax.clinic.example')).toBeTruthy();
    expect(within(rows).getByText('Console addresses allowed to sign in')).toBeTruthy();
    expect(within(rows).getByText('Console served by this installation')).toBeTruthy();
    expect(within(rows).getByDisplayValue('On')).toBeTruthy();
    expect(within(rows).getByDisplayValue('Not set')).toBeTruthy();
    expect(within(rows).getAllByText('Set when Faxbot started.')).toHaveLength(2);
    expect(within(rows).getByText('Not set: signing in needs HTTPS.')).toBeTruthy();
    // Variable names are for Developer pages only.
    expect(within(rows).queryByText(/FAXBOT_|ENABLE_/)).toBeNull();
    expect(screen.queryByText('Audit Logging')).toBeNull();
  });

  it('names the assistant servers\' settings in Developer and never shows a withheld value', async () => {
    settingsHandlers(settingsFixture((data) => {
      data.deployment = deployment({ MCP_HTTP_PORT: '3001', MCP_OAUTH_SUBJECT_KEYS_FILE: 'hidden' },
        ['MCP_OAUTH_SUBJECT_KEYS_FILE']);
    }));
    render(<Settings client={client()} sections={['mcp']} />);
    const rows = await screen.findByTestId('deployment-rows');
    expect(within(rows).getByDisplayValue('3001')).toBeTruthy();
    expect(within(rows).getByText('Set when Faxbot started. (MCP_OAUTH_SUBJECT_KEYS_FILE)')).toBeTruthy();
    expect(within(rows).getByDisplayValue('Set')).toBeTruthy();
    // A setting that is not set names the default the assistant server then uses.
    expect(within(rows).getByText('Not set: the assistant server answers to any address. (MCP_ALLOWED_HOSTS)')).toBeTruthy();
    expect(document.body.textContent).not.toContain('hidden');
  });

  it('puts the server time zone in Diagnostics and the key locations in Storage & retention', async () => {
    settingsHandlers(settingsFixture((data) => {
      data.deployment = deployment({ TZ: 'America/Denver' });
    }));
    const { unmount } = render(<Settings client={client()} sections={['diagnostics']} />);
    expect(within(await screen.findByTestId('deployment-rows')).getByDisplayValue('America/Denver')).toBeTruthy();
    unmount();
    render(<Settings client={client()} sections={['storage', 'advanced', 'backup']} title="Storage & retention" />);
    const rows = await screen.findByTestId('deployment-rows');
    expect(within(rows).getByText('Where the installation key is kept')).toBeTruthy();
    // The recovery copy is kept one release; its button says what replaces it.
    expect(screen.getByTestId('recovery-retiring').textContent).toBe(
      'The recovery copy goes away in the next release. Make a full backup on the server instead (faxbot system backup).');
    expect(within(rows).getAllByText('Not set: Faxbot keeps it in its data folder.')).toHaveLength(2);
  });

  it('checks the bucket through the diagnostics report and says what it found', async () => {
    settingsHandlers(settingsFixture((data) => {
      data.storage = { ...data.storage, backend: 's3', s3_bucket: 'synthetic-bucket' };
    }));
    const report = (status: string, sentence: string) => ({ checked_at: null, checked_at_text: '', status, summary: null,
      sections: [{ id: 'server', title: 'Server', checks: [
        { id: 'server.storage', section: 'server', title: 'Online storage', status, sentence, fix: null }] }] });
    let reply = report('ok', 'Faxbot can reach the online storage that keeps received faxes.');
    let runs = 0;
    server.use(http.post('/admin/diagnostics/report', () => { runs += 1; return HttpResponse.json(reply); }));
    render(<Settings client={client()} sections={['storage', 'advanced', 'backup']} title="Storage & retention" />);
    fireEvent.click(await screen.findByRole('button', { name: 'Check the bucket' }));
    expect(await screen.findByText('Faxbot can reach the online storage that keeps received faxes.')).toBeTruthy();
    // The report's own wording points at Diagnostics' switch "below"; this page says where it is.
    reply = report('attention', 'Received faxes go to online storage, but Faxbot has not checked that it can reach it. '
      + 'Turn on Also check the S3 bucket below.');
    fireEvent.click(screen.getByRole('button', { name: 'Check the bucket' }));
    expect(await screen.findByText('Turn on Also check the S3 bucket under System → Diagnostics, then check again.')).toBeTruthy();
    expect(runs).toBe(2);
  });

  it('words the receiving settings plainly', async () => {
    settingsHandlers(settingsFixture());
    render(<Settings client={client()} sections={['providers', 'inbound', 'routes']} />);
    const receiving = await receivingSection();
    expect(within(receiving).getByText('Keep received faxes for (days)')).toBeTruthy();
    expect(within(receiving).getByText('Download links work for (minutes)')).toBeTruthy();
    expect(screen.queryByText(/Token TTL|Retention Days|Configure inbound/)).toBeNull();
  });

  it('keeps the five event settings on the Audit log page, in words', async () => {
    const writes = settingsHandlers(settingsFixture((data) => {
      data.audit = { enabled: false, format: 'json', file: '', syslog: false, syslog_address: '/dev/log' };
      data.security.audit_enabled = false;
    }));
    render(<Settings client={client()} sections={['audit']} canWrite />);
    fireEvent.click(await screen.findByLabelText('Record events'));
    for (const label of ['How each event is written', 'Also save events in a file on the server', 'Address of the system log']) {
      expect(screen.getByText(label)).toBeTruthy();
    }
    fireEvent.click(screen.getByLabelText('Also send events to the system log'));
    fireEvent.click(screen.getByRole('button', { name: 'Apply settings' }));
    await waitFor(() => expect(writes).toHaveLength(1));
    expect(writes[0]).toEqual({ expected_revision_id: expect.any(String), audit_log_enabled: true, audit_log_syslog: true });
  });
});

describe('Owner-only settings everywhere', () => {
  it('disables every owner-only setting on In use, Security and Storage for people who are not the owner', async () => {
    settingsHandlers(settingsFixture((data) => {
      data.owner_only = ['inbound_token_ttl_minutes', 'enforce_public_https', 'enable_persisted_settings',
        'max_requests_per_minute', 'inbound_list_rpm', 'inbound_get_rpm'];
    }));
    const { unmount } = render(<Settings client={client()} sections={['providers', 'inbound', 'routes']} canWrite isOwner={false} />);
    const receiving = await receivingSection();
    const minutes = within(receiving).getByText('Download links work for (minutes)').closest('.MuiBox-root')?.parentElement as HTMLElement;
    expect((within(receiving).getByDisplayValue('60') as HTMLInputElement).disabled).toBe(true);
    expect(within(receiving).getByText(/How long a link to download a received fax keeps working\. Only the owner/)).toBeTruthy();
    expect(minutes).toBeTruthy();
    // Receiving itself is not owner-only and stays changeable.
    expect((within(receiving).getByLabelText(/^Receiving is (on|off)$/) as HTMLInputElement).disabled).toBe(false);
    unmount();
    render(<Settings client={client()} sections={['security', 'storage', 'advanced']} canWrite isOwner={false} />);
    await screen.findByText('Require HTTPS for document links');
    // HTTPS for document links and the three request limits; the recovery-copy row is read-only for everyone.
    expect(screen.getAllByText(/Only the owner of this installation can change this\.$/).length).toBeGreaterThanOrEqual(4);
  });
});
