import { http, HttpResponse } from 'msw';
import { demoHealth, demoJobs, demoApiKeys, demoSettings, demoDiagnostics, demoInbound, demoValidation } from './data';

let jobs = [...demoJobs];
let apiKeys = [...demoApiKeys];
let inbound = [...demoInbound];
let pluginConfigs: Record<string, { enabled?: boolean; settings?: any }> = {
  phaxio: { enabled: true, settings: { callback_url: 'https://example.com/phaxio-callback', verify_signature: true } },
  sinch: { enabled: false, settings: { project_id: '' } },
  sip: { enabled: false, settings: {} },
  s3: { enabled: false, settings: { bucket: '', region: '', prefix: '' } },
};
let logsRing: any[] = [
  { ts: new Date().toISOString(), event: 'api_started', backend: 'phaxio' },
  { ts: new Date().toISOString(), event: 'job_created', job_id: 'job_001', backend: 'phaxio', to: '+14155550111' },
  { ts: new Date().toISOString(), event: 'job_succeeded', job_id: 'job_001', backend: 'phaxio', pages: 2 },
];

export const handlers = [
  // Config
  http.get('/admin/config', () => HttpResponse.json({ 
    demo: true, 
    version: 'demo-1.0.0',
    allow_restart: true,
    persisted_settings_enabled: true,
    v3_plugins: { enabled: true },
    mcp: { sse_enabled: true, http_enabled: false, require_oauth: false, sse_path: '/mcp/sse', http_path: '/mcp/http', oauth: { issuer: '', audience: '', jwks_url: '' } }
  })),

  // Health
  http.get('/admin/health-status', () => HttpResponse.json({ ...demoHealth, timestamp: new Date().toISOString() })),

  // Health
  http.get('/mcp/sse/health', () => HttpResponse.json({ ok: true })),

  // Logs
  http.get('/admin/logs', ({ request }) => {
    const url = new URL(request.url);
    const q = (url.searchParams.get('q') || '').toLowerCase();
    const event = url.searchParams.get('event');
    const since = url.searchParams.get('since');
    const limit = parseInt(url.searchParams.get('limit') || '200', 10);
    let items = [...logsRing];
    if (since) items = items.filter(i => new Date(i.ts).getTime() >= new Date(since).getTime());
    if (event) items = items.filter(i => String(i.event) === event);
    if (q) items = items.filter(i => JSON.stringify(i).toLowerCase().includes(q));
    items = items.slice(-limit);
    return HttpResponse.json({ items, count: items.length, source: 'ring' });
  }),
  http.get('/admin/logs/tail', ({ request }) => {
    const url = new URL(request.url);
    const q = (url.searchParams.get('q') || '').toLowerCase();
    const event = url.searchParams.get('event');
    const lines = parseInt(url.searchParams.get('lines') || '2000', 10);
    let items = [...logsRing];
    if (event) items = items.filter(i => String(i.event) === event);
    if (q) items = items.filter(i => JSON.stringify(i).toLowerCase().includes(q));
    items = items.slice(-lines);
    return HttpResponse.json({ items, count: items.length, source: 'file' });
  }),

  // Jobs
  http.get('/admin/fax-jobs', ({ request }) => {
    const url = new URL(request.url);
    const status = url.searchParams.get('status');
    const filtered = status ? jobs.filter(j => j.status === status) : jobs;
    return HttpResponse.json({ total: filtered.length, jobs: filtered });
  }),
  http.get('/admin/fax-jobs/:id', ({ params }) => {
    const j = jobs.find(x => x.id === params.id);
    return j ? HttpResponse.json(j) : HttpResponse.json({ error: 'not found' }, { status: 404 });
  }),
  http.get('/admin/fax-jobs/:id/pdf', () => {
    const minimalPdf = '%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF';
    return new HttpResponse(minimalPdf, { status: 200, headers: { 'Content-Type': 'application/pdf' } });
  }),

  // API Keys
  http.get('/admin/api-keys', () => HttpResponse.json(apiKeys)),
  http.post('/admin/api-keys', async ({ request }) => {
    const body = (await request.json().catch(() => ({}))) as any;
    const key_id = 'k_' + Math.random().toString(36).slice(2, 10);
    const token = 'demo_' + Math.random().toString(36).slice(2, 16);
    const newKey = { key_id, name: body?.name ?? 'New Key', owner: body?.owner ?? 'Demo', scopes: body?.scopes ?? ['send'], created_at: new Date().toISOString() };
    apiKeys = [newKey, ...apiKeys];
    return HttpResponse.json({ key_id, token });
  }),
  http.delete('/admin/api-keys/:id', ({ params }) => {
    apiKeys = apiKeys.filter(k => k.key_id !== (params as any).id);
    return new HttpResponse(null, { status: 204 });
  }),
  http.post('/admin/api-keys/:id/rotate', () => {
    const token = 'demo_' + Math.random().toString(36).slice(2, 16);
    return HttpResponse.json({ token });
  }),

  // Settings
  http.get('/admin/settings', () => HttpResponse.json(demoSettings)),
  http.post('/admin/settings/validate', async () => {
    return HttpResponse.json(demoValidation);
  }),
  http.get('/admin/settings/export', () => {
    const env = [
      'FAX_BACKEND=phaxio',
      'REQUIRE_API_KEY=true',
      'ENFORCE_PUBLIC_HTTPS=true',
      'AUDIT_LOG_ENABLED=true',
      'PDF_TOKEN_TTL_MINUTES=60',
      '',
      '# Backend-specific configuration',
      'PHAXIO_API_KEY=***',
      'PHAXIO_API_SECRET=***',
      'PHAXIO_VERIFY_SIGNATURE=true',
    ].join('\n');
    return HttpResponse.json({ env_content: env, requires_restart: false, note: 'Demo export content' });
  }),

  http.post('/admin/settings/persist', async () => {
    return HttpResponse.json({ ok: true, path: '/faxdata/faxbot.env' });
  }),
  http.put('/admin/settings', async ({ request }) => {
    const body = (await request.json().catch(() => ({}))) as any;
    // Apply some changes to demoSettings for realism
    if (typeof body.require_api_key === 'boolean') demoSettings.security.require_api_key = body.require_api_key;
    if (typeof body.enforce_public_https === 'boolean') demoSettings.security.enforce_https = body.enforce_public_https;
    if (typeof body.audit_log_enabled === 'boolean') demoSettings.security.audit_enabled = body.audit_log_enabled;
    if (typeof body.enable_persisted_settings === 'boolean') { /* reflect in config on next /admin/config read */ }
    if (typeof body.backend === 'string') demoSettings.backend.type = body.backend;
    if (typeof body.fax_disabled === 'boolean') demoSettings.backend.disabled = body.fax_disabled;
    if (typeof body.inbound_enabled === 'boolean') demoSettings.inbound.enabled = body.inbound_enabled;
    if (typeof body.inbound_retention_days === 'number') demoSettings.inbound.retention_days = body.inbound_retention_days;
    if (typeof body.inbound_token_ttl_minutes === 'number') (demoSettings as any).inbound.token_ttl_minutes = body.inbound_token_ttl_minutes;
    if (typeof body.storage_backend === 'string') demoSettings.storage.backend = body.storage_backend;
    if (typeof body.s3_bucket === 'string') (demoSettings as any).storage.s3_bucket = body.s3_bucket;
    if (typeof body.s3_region === 'string') (demoSettings as any).storage.s3_region = body.s3_region;
    if (typeof body.s3_prefix === 'string') (demoSettings as any).storage.s3_prefix = body.s3_prefix;
    if (typeof body.s3_endpoint_url === 'string') (demoSettings as any).storage.s3_endpoint_url = body.s3_endpoint_url;
    if (typeof body.s3_kms_key_id === 'string') (demoSettings as any).storage.s3_kms_enabled = !!body.s3_kms_key_id;
    // MCP toggles
    // return meta to suggest restart sometimes
    const restart = !!(body.backend || body.storage_backend);
    return HttpResponse.json({ ok: true, _meta: { restart_recommended: restart } });
  }),
  http.post('/admin/settings/reload', async () => HttpResponse.json({ ok: true })),
  http.post('/admin/restart', async () => HttpResponse.json({ ok: true })),

  // Diagnostics
  http.post('/admin/diagnostics/run', () => HttpResponse.json(demoDiagnostics)),

  // Inbound
  http.get('/inbound', () => HttpResponse.json(inbound)),
  http.get('/admin/inbound/callbacks', () => HttpResponse.json({
    phaxio: { callback: '/phaxio-callback', verify_signature: true },
    sinch: { callback: '/sinch-inbound', verify_signature: true, basic_auth: false, hmac: false },
  })),
  http.post('/admin/inbound/simulate', async ({ request }) => {
    const body = (await request.json().catch(() => ({}))) as any;
    const id = 'in_' + Math.random().toString(36).slice(2, 8);
    const rec = { id, fr: body?.fr || '+1 (555) 777-8888', to: body?.to || '+1 (555) 222-3333', status: body?.status || 'received', backend: body?.backend || 'phaxio', pages: body?.pages || 1, received_at: new Date().toISOString() };
    inbound = [rec, ...inbound];
    logsRing.push({ ts: new Date().toISOString(), event: 'inbound_received', id });
    return HttpResponse.json({ id, status: 'queued' });
  }),
  http.get('/inbound/:id/pdf', () => {
    const minimalPdf = '%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF';
    return new HttpResponse(minimalPdf, { status: 200, headers: { 'Content-Type': 'application/pdf' } });
  }),

  // Send fax
  http.post('/fax', () => {
    const id = 'job_' + Math.random().toString(36).slice(2, 8);
    const job = { id, to_number: '+1 (555) 123‑4567', status: 'queued', backend: 'phaxio', created_at: new Date().toISOString(), updated_at: new Date().toISOString() };
    jobs = [job as any, ...jobs];
    logsRing.push({ ts: new Date().toISOString(), event: 'job_created', job_id: id, backend: 'phaxio', to: job.to_number });
    return HttpResponse.json({ id, status: 'queued' });
  }),

  // Plugins
  http.get('/plugins', () => HttpResponse.json({ items: [
    { id: 'phaxio', name: 'Phaxio (Sinch)', version: '1.0.0', categories: ['outbound'], capabilities: ['send','get_status'], enabled: true, description: 'Cloud fax backend via Phaxio (by Sinch)'} ,
    { id: 'sinch', name: 'Sinch Fax API', version: '1.0.0', categories: ['outbound'], capabilities: ['send','get_status'], enabled: false, description: 'Direct Sinch Fax API v3 backend' },
    { id: 'sip', name: 'SIP/Asterisk', version: '1.0.0', categories: ['outbound'], capabilities: ['send','get_status'], enabled: false, description: 'Self‑hosted SIP via Asterisk with T.38' },
    { id: 's3', name: 'Amazon S3', version: '1.0.0', categories: ['storage'], capabilities: ['store','retrieve'], enabled: false, description: 'S3/S3‑compatible storage for inbound artifacts' },
  ] })),
  http.get('/plugin-registry', () => HttpResponse.json({ items: [
    { id: 'phaxio', name: 'Phaxio (Sinch)', version: '1.0.0', categories: ['outbound'], capabilities: ['send','get_status'], learn_more: 'https://dmontgomery40.github.io/Faxbot/providers/phaxio/', description: 'Cloud fax backend via Phaxio (by Sinch)' },
    { id: 'sinch', name: 'Sinch Fax API', version: '1.0.0', categories: ['outbound'], capabilities: ['send','get_status'], learn_more: 'https://developers.sinch.com/docs/fax/api-reference/', description: 'Direct Sinch Fax API v3 backend' },
    { id: 'sip', name: 'SIP/Asterisk', version: '1.0.0', categories: ['outbound'], capabilities: ['send','get_status'], learn_more: 'https://dmontgomery40.github.io/Faxbot/asterisk/', description: 'Self‑hosted SIP via Asterisk with T.38' },
    { id: 's3', name: 'Amazon S3', version: '1.0.0', categories: ['storage'], capabilities: ['store','retrieve'], learn_more: 'https://aws.amazon.com/s3/', description: 'S3/S3‑compatible storage' },
  ] })),
  http.get('/plugins/:id/config', ({ params }) => {
    const id = (params as any).id as string;
    const cfg = pluginConfigs[id] || { enabled: false, settings: {} };
    return HttpResponse.json(cfg);
  }),
  http.put('/plugins/:id/config', async ({ params, request }) => {
    const id = (params as any).id as string;
    const body = (await request.json().catch(() => ({}))) as any;
    pluginConfigs[id] = { ...(pluginConfigs[id] || {}), ...body };
    return HttpResponse.json({ ok: true, path: '/config/faxbot.config.json' });
  }),
];
