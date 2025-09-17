// Electron-specific API client that handles both dev and production modes
import AdminAPIClient from './client';

class ElectronAPIClient extends AdminAPIClient {
  private electronBaseURL: string;

  constructor(apiKey: string = '') {
    super(apiKey);
    this.electronBaseURL = ElectronAPIClient.getAPIBaseURL();
    // Override the baseURL to use Electron's API endpoint
    this.baseURL = this.electronBaseURL;
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
