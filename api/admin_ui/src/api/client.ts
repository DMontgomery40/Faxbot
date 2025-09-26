import type {
  HealthStatus,
  FaxJob,
  ApiKey,
  Settings,
  DiagnosticsResult,
  ValidationResult,
  InboundFax,
  ProvidersInfo
} from './types';

export class AdminAPIClient {
  private baseURL: string;
  private apiKey: string;

  constructor(apiKey: string) {
    // Always localhost since we're local-only
    this.baseURL = window.location.origin;
    this.apiKey = apiKey;
  }

  private async fetch(path: string, options: RequestInit = {}): Promise<Response> {
    const response = await fetch(`${this.baseURL}${path}`, {
      ...options,
      headers: {
        'X-API-Key': this.apiKey,
        'Content-Type': 'application/json',
        ...options.headers,
      },
    });

    if (!response.ok) {
      throw new Error(`API Error: ${response.status} ${response.statusText}`);
    }

    return response;
  }

  // Configuration
  async getConfig(): Promise<any> {
    const res = await this.fetch('/admin/config');
    return res.json();
  }

  async importEnv(prefixes?: string[]): Promise<{ ok: boolean; discovered: number; prefixes: string[] }>{
    const body = prefixes && prefixes.length ? { prefixes } : {};
    const res = await this.fetch('/admin/config/import-env', {
      method: 'POST',
      body: JSON.stringify(body)
    });
    return res.json();
  }

  async getSettings(): Promise<Settings> {
    const res = await this.fetch('/admin/settings');
    return res.json();
  }

  async validateSettings(settings: any): Promise<ValidationResult> {
    const res = await this.fetch('/admin/settings/validate', {
      method: 'POST',
      body: JSON.stringify(settings),
    });
    return res.json();
  }

  async exportSettings(): Promise<{ env_content: string; requires_restart: boolean; note: string }> {
    const res = await this.fetch('/admin/settings/export');
    return res.json();
  }

  async persistSettings(content?: string, path?: string): Promise<{ ok: boolean; path: string }> {
    const res = await this.fetch('/admin/settings/persist', {
      method: 'POST',
      body: JSON.stringify({ content, path }),
    });
    return res.json();
  }

  async updateSettings(settings: any): Promise<any> {
    const res = await this.fetch('/admin/settings', {
      method: 'PUT',
      body: JSON.stringify(settings),
    });
    return res.json();
  }

  async reloadSettings(): Promise<any> {
    const res = await this.fetch('/admin/settings/reload', { method: 'POST' });
    return res.json();
  }

  async restart(): Promise<any> {
    const res = await this.fetch('/admin/restart', { method: 'POST' });
    return res.json();
  }

  // Diagnostics
  async runDiagnostics(): Promise<DiagnosticsResult> {
    const res = await this.fetch('/admin/diagnostics/run', {
      method: 'POST',
    });
    return res.json();
  }

  // UI Config (ETag-cached on server)
  async getUiConfig(): Promise<{ schema_version: number; features: any; endpoints: any; docs_base?: string }>{
    const res = await this.fetch('/admin/ui-config');
    return res.json();
  }

  // User traits (admin-only)
  async getUserTraits(): Promise<{ schema_version: number; user: { id: string }; traits: string[] }>{
    const res = await this.fetch('/admin/user/traits');
    return res.json();
  }

  // Provider traits & active backends
  async getProviders(): Promise<ProvidersInfo> {
    const res = await this.fetch('/admin/providers');
    return res.json();
  }

  async getHealthStatus(): Promise<HealthStatus> {
    const res = await this.fetch('/admin/health-status');
    return res.json();
  }

  // MCP
  async getMcpConfig(): Promise<any> {
    const res = await this.fetch('/admin/config');
    return res.json();
  }

  async getMcpHealth(path: string = '/mcp/sse/health'): Promise<any> {
    const res = await fetch(`${this.baseURL}${path}`);
    if (!res.ok) throw new Error(`MCP not healthy (${res.status})`);
    return res.json();
  }

  // Logs
  async getLogs(params: { q?: string; event?: string; since?: string; limit?: number } = {}): Promise<{ items: any[]; count: number }>{
    const search = new URLSearchParams();
    for (const [k,v] of Object.entries(params)) {
      if (v !== undefined && v !== null && String(v).length > 0) search.append(k, String(v));
    }
    const res = await this.fetch(`/admin/logs?${search.toString()}`);
    return res.json();
  }

  async tailLogs(params: { q?: string; event?: string; lines?: number } = {}): Promise<{ items: any[]; count: number; source?: string }>{
    const search = new URLSearchParams();
    for (const [k,v] of Object.entries(params)) {
      if (v !== undefined && v !== null && String(v).length > 0) search.append(k, String(v));
    }
    const res = await this.fetch(`/admin/logs/tail?${search.toString()}`);
    return res.json();
  }

  // Jobs
  async listJobs(params: { 
    status?: string; 
    backend?: string; 
    limit?: number; 
    offset?: number 
  } = {}): Promise<{ total: number; jobs: FaxJob[] }> {
    const query = new URLSearchParams();
    Object.entries(params).forEach(([key, value]) => {
      if (value !== undefined) {
        query.append(key, String(value));
      }
    });
    const res = await this.fetch(`/admin/fax-jobs?${query}`);
    return res.json();
  }

  async getJob(id: string): Promise<FaxJob> {
    const res = await this.fetch(`/admin/fax-jobs/${id}`);
    return res.json();
  }

  async downloadJobPdf(id: string): Promise<Blob> {
    const res = await fetch(`${this.baseURL}/admin/fax-jobs/${encodeURIComponent(id)}/pdf`, {
      headers: {
        'X-API-Key': this.apiKey,
      },
    });
    if (!res.ok) {
      throw new Error(`Download failed: ${res.status}`);
    }
    return res.blob();
  }

  // API Keys
  async createApiKey(data: { 
    name?: string; 
    owner?: string; 
    scopes?: string[] 
  }): Promise<{ key_id: string; token: string }> {
    const res = await this.fetch('/admin/api-keys', {
      method: 'POST',
      body: JSON.stringify(data),
    });
    return res.json();
  }

  async listApiKeys(): Promise<ApiKey[]> {
    const res = await this.fetch('/admin/api-keys');
    return res.json();
  }

  async revokeApiKey(keyId: string): Promise<void> {
    await this.fetch(`/admin/api-keys/${keyId}`, {
      method: 'DELETE',
    });
  }

  async rotateApiKey(keyId: string): Promise<{ token: string }> {
    const res = await this.fetch(`/admin/api-keys/${keyId}/rotate`, {
      method: 'POST',
    });
    return res.json();
  }

  // Inbound
  async listInbound(): Promise<InboundFax[]> {
    const res = await this.fetch('/inbound');
    return res.json();
  }

  async downloadInboundPdf(id: string): Promise<Blob> {
    const res = await fetch(`${this.baseURL}/inbound/${encodeURIComponent(id)}/pdf`, {
      headers: {
        'X-API-Key': this.apiKey,
      },
    });
    
    if (!res.ok) {
      throw new Error(`Download failed: ${res.status}`);
    }
    
    return res.blob();
  }

  // Inbound helpers
  async getInboundCallbacks(): Promise<any> {
    const res = await this.fetch('/admin/inbound/callbacks');
    return res.json();
  }

  async purgeInboundBySid(providerSid: string): Promise<{ ok: boolean; deleted_faxes: number; deleted_events: number }>{
    const res = await this.fetch('/admin/inbound/purge-by-sid', {
      method: 'DELETE',
      body: JSON.stringify({ provider_sid: providerSid })
    });
    return res.json();
  }

  async simulateInbound(opts: { backend?: string; fr?: string; to?: string; pages?: number; status?: string } = {}): Promise<{ id: string; status: string }> {
    const res = await this.fetch('/admin/inbound/simulate', {
      method: 'POST',
      body: JSON.stringify(opts),
    });
    return res.json();
  }

  // Admin actions (container exec — allowlisted)
  async listActions(): Promise<{ enabled: boolean; items: Array<{ id: string; label: string; backend?: string[] }> }> {
    const res = await this.fetch('/admin/actions');
    return res.json();
  }

  async runAction(id: string): Promise<{ ok: boolean; id: string; code?: number; stdout?: string; stderr?: string }> {
    const res = await this.fetch('/admin/actions/run', {
      method: 'POST',
      body: JSON.stringify({ id }),
    });
    return res.json();
  }

  // Tunnel (admin-only)
  async getTunnelStatus(): Promise<any> {
    const res = await this.fetch('/admin/tunnel/status');
    return res.json();
  }

  async setTunnelConfig(payload: any): Promise<any> {
    const res = await this.fetch('/admin/tunnel/config', {
      method: 'POST',
      body: JSON.stringify(payload || {}),
    });
    return res.json();
  }

  async testTunnel(): Promise<{ ok: boolean; message?: string; target?: string }> {
    const res = await this.fetch('/admin/tunnel/test', { method: 'POST' });
    return res.json();
  }

  async createTunnelPairing(): Promise<{ code: string; expires_at: string }> {
    const res = await this.fetch('/admin/tunnel/pair', { method: 'POST' });
    return res.json();
  }

  async registerSinchWebhook(): Promise<{ success: boolean; webhook_url?: string; error?: string; provider_response?: any }>{
    const res = await this.fetch('/admin/tunnel/register-sinch', { method: 'POST' });
    return res.json();
  }

  // Cloudflared logs (admin-only)
  async getTunnelCloudflaredLogs(lines: number = 50): Promise<{ items: string[]; path?: string }>{
    const res = await this.fetch(`/admin/tunnel/cloudflared/logs?lines=${encodeURIComponent(String(lines))}`);
    return res.json();
  }

  // WireGuard config + QR
  async wgImportConfFile(file: File): Promise<{ ok: boolean; path?: string }>{
    const form = new FormData();
    form.append('file', file);
    const res = await fetch(`${this.baseURL}/admin/tunnel/wg/import`, {
      method: 'POST',
      headers: { 'X-API-Key': this.apiKey },
      body: form,
    });
    if (!res.ok) throw new Error(`Import failed: ${res.status}`);
    return res.json();
  }

  async wgImportConfText(content: string): Promise<{ ok: boolean; path?: string }>{
    const res = await this.fetch('/admin/tunnel/wg/import', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content }),
    });
    return res.json();
  }

  async wgDownloadConf(): Promise<Blob> {
    const res = await fetch(`${this.baseURL}/admin/tunnel/wg/conf`, {
      headers: { 'X-API-Key': this.apiKey },
    });
    if (!res.ok) throw new Error(`Download failed: ${res.status}`);
    return res.blob();
  }

  async wgDeleteConf(): Promise<{ ok: boolean }>{
    const res = await this.fetch('/admin/tunnel/wg/conf', { method: 'DELETE' });
    return res.json();
  }

  async wgGetQr(): Promise<{ png_base64?: string; svg_base64?: string }>{
    const res = await this.fetch('/admin/tunnel/wg/qr', { method: 'POST' });
    return res.json();
  }

  // Send test fax
  async sendFax(to: string, file: File): Promise<{ id: string; status: string }> {
    const formData = new FormData();
    formData.append('to', to);
    formData.append('file', file);

    const res = await fetch(`${this.baseURL}/fax`, {
      method: 'POST',
      headers: {
        'X-API-Key': this.apiKey,
      },
      body: formData,
    });

    if (!res.ok) {
      throw new Error(`Send failed: ${res.status}`);
    }

    return res.json();
  }

  // v3 Plugins (feature-gated)
  async listPlugins(): Promise<{ items: any[] }> {
    const res = await this.fetch('/plugins');
    return res.json();
  }

  async getPluginConfig(pluginId: string): Promise<{ enabled: boolean; settings: any }> {
    const res = await this.fetch(`/plugins/${encodeURIComponent(pluginId)}/config`);
    return res.json();
  }

  async updatePluginConfig(pluginId: string, payload: { enabled?: boolean; settings?: Record<string, any> }): Promise<{ ok: boolean; path: string }> {
    const res = await this.fetch(`/plugins/${encodeURIComponent(pluginId)}/config`, {
      method: 'PUT',
      body: JSON.stringify(payload || {}),
    });
    return res.json();
  }

  async getPluginRegistry(): Promise<{ items: any[] }> {
    const res = await this.fetch('/plugin-registry');
    return res.json();
  }

  // Manifest providers (admin-only)
  async validateHttpManifest(payload: { manifest: any; credentials?: any; settings?: any; to?: string; file_url?: string; from_number?: string; render_only?: boolean }): Promise<any> {
    const res = await this.fetch('/admin/plugins/http/validate', {
      method: 'POST',
      body: JSON.stringify(payload || {}),
    });
    return res.json();
  }

  async installHttpManifest(payload: { manifest: any }): Promise<{ ok: boolean; id: string; path: string }> {
    const res = await this.fetch('/admin/plugins/http/install', {
      method: 'POST',
      body: JSON.stringify(payload || {}),
    });
    return res.json();
  }

  // Jobs admin helpers
  async refreshJob(jobId: string): Promise<FaxJob> {
    const res = await this.fetch(`/admin/fax-jobs/${encodeURIComponent(jobId)}/refresh`, { method: 'POST' });
    return res.json();
  }

  async importHttpManifests(payload: { items?: any[]; markdown?: string }): Promise<{ ok: boolean; imported: any[]; errors: any[] }>{
    const res = await this.fetch('/admin/plugins/http/import-manifests', {
      method: 'POST',
      body: JSON.stringify(payload || {}),
    });
    return res.json();
  }

  // Polling helper
  startPolling(onUpdate: (data: HealthStatus) => void, intervalMs: number = 5000): () => void {
    let running = true;
    
    const poll = async () => {
      if (!running) return;
      try {
        const data = await this.getHealthStatus();
        onUpdate(data);
      } catch (e) {
        console.error('Polling error:', e);
      }
      if (running) {
        setTimeout(poll, intervalMs);
      }
    };
    
    poll(); // Start immediately
    
    // Return cleanup function
    return () => { running = false; };
  }
}

export default AdminAPIClient;
