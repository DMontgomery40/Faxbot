import type {
  HealthStatus,
  FaxJob,
  ApiKey,
  Settings,
  SettingsPatch,
  PluginConfiguration,
  PluginConfigurationPatch,
  PluginConfigurationResult,
  PluginRole,
  DiagnosticsResult,
  ValidationResult,
  InboundFax
} from './types';

// These manifest validation messages contain no paths, credentials, or provider
// responses. All other server error bodies remain opaque to the UI.
const safeManifestDetails = new Set([
  'Manifest id is required',
  'Manifest id required',
  'manifest.id missing',
  'Invalid provider id or path',
  'HTTP provider id is reserved for storage.',
  'Invalid HTTP provider manifest.',
  'No manifest candidates provided',
  'Fax sending is disabled. Validate the manifest without sending, or use Send to queue a test document.',
]);

export class AdminAPIClient {
  private baseURL: string;
  private apiKey: string;

  constructor(apiKey: string) {
    // Always localhost since we're local-only
    this.baseURL = window.location.origin;
    this.apiKey = apiKey;
  }

  private async fetch(path: string, options: RequestInit = {}, manifestValidation = false): Promise<Response> {
    const response = await fetch(`${this.baseURL}${path}`, {
      ...options,
      headers: {
        'X-API-Key': this.apiKey,
        'Content-Type': 'application/json',
        ...options.headers,
      },
    });

    if (!response.ok) {
      if (manifestValidation && (response.status === 400 || response.status === 409)) {
        const body = await response.json().catch(() => null);
        if (typeof body?.detail === 'string' && safeManifestDetails.has(body.detail)) {
          throw new Error(body.detail);
        }
      }
      throw new Error(`API Error: ${response.status} ${response.statusText}`);
    }

    return response;
  }

  // Configuration
  async getConfig(): Promise<any> {
    const res = await this.fetch('/admin/config');
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

  async exportSettings(): Promise<{ env: string }> {
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

  async updateSettings(settings: SettingsPatch): Promise<Settings> {
    const res = await this.fetch('/admin/settings', {
      method: 'PUT',
      body: JSON.stringify(settings),
    });
    return res.json();
  }

  async reloadSettings(): Promise<Settings> {
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

  async sendFax(to: string, file: File, options: { queueOnly?: boolean } = {}): Promise<{ id: string; status: string }> {
    const formData = new FormData();
    formData.append('to', to);
    formData.append('file', file);
    if (options.queueOnly) formData.append('queue_only', 'true');

    const res = await fetch(`${this.baseURL}/fax`, {
      method: 'POST',
      headers: {
        'X-API-Key': this.apiKey,
      },
      body: formData,
    });

    if (!res.ok) {
      if (options.queueOnly && res.status === 409) {
        throw new Error('Queue-only submission was refused because active settings changed. Leave and reopen Send to review the current delivery mode before trying again.');
      }
      throw new Error(`Send failed: ${res.status}`);
    }

    return res.json();
  }

  // v3 Plugins (feature-gated)
  async listPlugins(): Promise<{ items: any[] }> {
    const res = await this.fetch('/plugins');
    return res.json();
  }

  async getPluginConfig(pluginId: string, role?: PluginRole): Promise<PluginConfiguration> {
    const query = role ? `?role=${encodeURIComponent(role)}` : '';
    const res = await this.fetch(`/plugins/${encodeURIComponent(pluginId)}/config${query}`);
    return res.json();
  }

  async updatePluginConfig(pluginId: string, payload: PluginConfigurationPatch): Promise<PluginConfigurationResult> {
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
    }, true);
    return res.json();
  }

  async installHttpManifest(payload: { manifest: any }): Promise<{ ok: boolean; id: string; path: string }> {
    const res = await this.fetch('/admin/plugins/http/install', {
      method: 'POST',
      body: JSON.stringify(payload || {}),
    }, true);
    return res.json();
  }

  // Jobs admin helpers
  async refreshJob(jobId: string): Promise<FaxJob> {
    const res = await this.fetch(`/admin/fax-jobs/${encodeURIComponent(jobId)}/refresh`, { method: 'POST' });
    return res.json();
  }

  async importHttpManifests(payload: { items?: any[]; markdown?: string; source?: 'repo_scrape' }): Promise<{ ok: boolean; imported: any[]; errors: Array<{ error: string }> }>{
    const res = await this.fetch('/admin/plugins/http/import-manifests', {
      method: 'POST',
      body: JSON.stringify(payload || {}),
    }, true);
    const result = await res.json();
    return {
      ...result,
      errors: (Array.isArray(result.errors) ? result.errors : []).map((failure: any) => ({
        error: safeManifestDetails.has(failure?.error) ? failure.error : 'Manifest could not be imported.',
      })),
    };
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
