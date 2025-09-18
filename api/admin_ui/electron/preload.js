const { contextBridge, ipcRenderer } = require('electron');

// Expose protected methods that allow the renderer process to use
// the ipcRenderer without exposing the entire object
contextBridge.exposeInMainWorld('electronAPI', {
  // App info
  getAppVersion: () => ipcRenderer.invoke('get-app-version'),
  
  // File operations
  selectFile: () => ipcRenderer.invoke('select-file'),
  
  // Dialog operations
  showMessageBox: (options) => ipcRenderer.invoke('show-message-box', options),
  
  // Navigation (from menu)
  onNavigate: (callback) => {
    ipcRenderer.on('navigate-to', (event, route) => callback(route));
  },
  
  // Remove navigation listener
  removeNavigateListener: () => {
    ipcRenderer.removeAllListeners('navigate-to');
  },
  
  // Platform info
  platform: process.platform,
  
  // Check if running in Electron
  isElectron: true
});

// Expose a limited set of Node.js APIs
contextBridge.exposeInMainWorld('nodeAPI', {
  platform: process.platform,
  versions: process.versions
});

