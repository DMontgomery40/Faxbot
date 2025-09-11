import type {
  HealthStatus,
  FaxJob,
  ApiKey,
  Settings,
  DiagnosticsResult,
  ValidationResult,
  InboundFax
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

  async getHealthStatus(): Promise<HealthStatus> {
    const res = await this.fetch('/admin/health-status');
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
