// Electron-specific API client that handles both dev and production modes
import AdminAPIClient from './client';

class ElectronAPIClient {
  private client: AdminAPIClient;
  private electronBaseURL: string;

  constructor(apiKey: string = '') {
    // Create a temporary client just to get the methods
    this.client = new AdminAPIClient(apiKey);
    this.electronBaseURL = ElectronAPIClient.getAPIBaseURL();
  }

  // Custom fetch method for Electron
  private async electronFetch(path: string, options: RequestInit = {}): Promise<Response> {
    const response = await fetch(`${this.electronBaseURL}${path}`, {
      ...options,
      headers: {
        'X-API-Key': this.client['apiKey'] || '',
        'Content-Type': 'application/json',
        ...options.headers,
      },
    });

    if (!response.ok) {
      throw new Error(`HTTP error! status: ${response.status}`);
    }

    return response;
  }

  // Proxy all methods from AdminAPIClient but use our custom fetch
  async checkHealth() {
    const response = await this.electronFetch('/health');
    return response.json();
  }

  async getSettings() {
    const response = await this.electronFetch('/admin/settings');
    return response.json();
  }

  async updateSettings(settings: any) {
    const response = await this.electronFetch('/admin/settings', {
      method: 'POST',
      body: JSON.stringify(settings),
    });
    return response.json();
  }

  async reloadSettings() {
    const response = await this.electronFetch('/admin/reload-settings', {
      method: 'POST',
    });
    return response.json();
  }

  async sendFax(to: string, file: File) {
    const formData = new FormData();
    formData.append('to', to);
    formData.append('file', file);

    const response = await fetch(`${this.electronBaseURL}/fax`, {
      method: 'POST',
      headers: {
        'X-API-Key': this.client['apiKey'] || '',
      },
      body: formData,
    });

    if (!response.ok) {
      throw new Error(`HTTP error! status: ${response.status}`);
    }

    return response.json();
  }

  async getFaxStatus(jobId: string) {
    const response = await this.electronFetch(`/fax/${jobId}`);
    return response.json();
  }

  async listJobs() {
    const response = await this.electronFetch('/admin/jobs');
    return response.json();
  }

  async getApiKeys() {
    const response = await this.electronFetch('/admin/api-keys');
    return response.json();
  }

  async createApiKey(name: string, scopes: string[]) {
    const response = await this.electronFetch('/admin/api-keys', {
      method: 'POST',
      body: JSON.stringify({ name, scopes }),
    });
    return response.json();
  }

  async deleteApiKey(keyId: string) {
    const response = await this.electronFetch(`/admin/api-keys/${keyId}`, {
      method: 'DELETE',
    });
    return response.json();
  }

  async runDiagnostics() {
    const response = await this.electronFetch('/admin/diagnostics');
    return response.json();
  }

  async listInbound() {
    const response = await this.electronFetch('/inbound');
    return response.json();
  }

  async getInboundPdf(id: string) {
    const response = await this.electronFetch(`/inbound/${id}/pdf`);
    return response.blob();
  }

  static getAPIBaseURL(): string {
    // Check if we're running in Electron
    if (typeof window !== 'undefined' && (window as any).electronAPI?.isElectron) {
      // In Electron, always connect to localhost:8080 (where Docker API runs)
      return 'http://localhost:8080';
    }
    
    // Fallback to current host (for web version)
    if (typeof window !== 'undefined') {
      const { protocol, hostname } = window.location;
      // If we're on a dev port, assume API is on 8080
      const devPorts = ['3000', '3001', '5173', '5174', '4200'];
      const currentPort = window.location.port;
      
      if (devPorts.includes(currentPort)) {
        return `${protocol}//localhost:8080`;
      }
      
      // Production: same host
      return `${protocol}//${hostname}:8080`;
    }
    
    // Server-side fallback
    return 'http://localhost:8080';
  }

  // Electron-specific methods
  async selectFile(): Promise<{ filePaths: string[]; canceled: boolean } | null> {
    if (typeof window !== 'undefined' && (window as any).electronAPI?.selectFile) {
      return await (window as any).electronAPI.selectFile();
    }
    return null;
  }

  async showMessageBox(options: {
    type?: 'none' | 'info' | 'error' | 'question' | 'warning';
    title?: string;
    message: string;
    detail?: string;
    buttons?: string[];
  }): Promise<{ response: number; checkboxChecked?: boolean } | null> {
    if (typeof window !== 'undefined' && (window as any).electronAPI?.showMessageBox) {
      return await (window as any).electronAPI.showMessageBox(options);
    }
    return null;
  }

  isElectron(): boolean {
    return typeof window !== 'undefined' && (window as any).electronAPI?.isElectron === true;
  }

  getPlatform(): string {
    if (typeof window !== 'undefined' && (window as any).electronAPI?.platform) {
      return (window as any).electronAPI.platform;
    }
    return 'web';
  }
}

export default ElectronAPIClient;
