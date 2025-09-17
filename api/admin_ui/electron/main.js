const { app, BrowserWindow, Menu, shell, ipcMain, Tray, nativeImage, dialog } = require('electron');
const path = require('path');
const isDev = process.env.NODE_ENV === 'development';

// Set the app name and About panel for macOS
app.setName('Faxbot');
app.setAboutPanelOptions({
  applicationName: 'Faxbot',
  applicationVersion: app.getVersion(),
  copyright: '© Faxbot',
});

// Keep a global reference of the window object
let mainWindow;
let tray = null;

// Enable live reload for Electron in development
if (isDev) {
  require('electron-reload')(__dirname, {
    electron: path.join(__dirname, '..', 'node_modules', '.bin', 'electron'),
    hardResetMethod: 'exit'
  });
}

function createWindow() {
  // Create the browser window
  mainWindow = new BrowserWindow({
    width: 1400,
    height: 900,
    minWidth: 800,
    minHeight: 600,
    webPreferences: {
      nodeIntegration: false,
      contextIsolation: true,
      enableRemoteModule: false,
      preload: path.join(__dirname, 'preload.js')
    },
    icon: getAppIcon(),
    show: false, // Don't show until ready
    titleBarStyle: process.platform === 'darwin' ? 'hiddenInset' : 'default',
  });

  // Load the app
  const startUrl = isDev 
    ? 'http://localhost:5173' 
    : `file://${path.join(__dirname, '../dist/index.html')}`;
  
  mainWindow.loadURL(startUrl);

  // Show window when ready to prevent visual flash
  mainWindow.once('ready-to-show', () => {
    mainWindow.show();
    
    // Focus on the window
    if (isDev) {
      mainWindow.webContents.openDevTools();
    }
  });

  // Handle window closed
  mainWindow.on('closed', () => {
    mainWindow = null;
  });

  // Handle external links
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: 'deny' };
  });

  // Prevent navigation to external URLs
  mainWindow.webContents.on('will-navigate', (event, navigationUrl) => {
    const parsedUrl = new URL(navigationUrl);
    
    if (parsedUrl.origin !== startUrl.split('/').slice(0, 3).join('/')) {
      event.preventDefault();
      shell.openExternal(navigationUrl);
    }
  });
}

function getAppIcon() {
  if (process.platform === 'win32') {
    return path.join(__dirname, 'assets', 'icon.ico');
  } else if (process.platform === 'darwin') {
    return path.join(__dirname, 'assets', 'icon.icns');
  } else {
    return path.join(__dirname, 'assets', 'icon.png');
  }
}

function createTray() {
  const trayIcon = nativeImage.createFromPath(
    path.join(__dirname, 'assets', 'tray-icon.png')
  ).resize({ width: 16, height: 16 });
  
  tray = new Tray(trayIcon);
  
  const contextMenu = Menu.buildFromTemplate([
    {
      label: 'Show Faxbot Admin',
      click: () => {
        if (mainWindow) {
          mainWindow.show();
          mainWindow.focus();
        } else {
          createWindow();
        }
      }
    },
    {
      label: 'Check Fax Status',
      click: () => {
        // This could trigger an IPC event to the renderer
        if (mainWindow) {
          mainWindow.webContents.send('navigate-to', '/jobs');
          mainWindow.show();
          mainWindow.focus();
        }
      }
    },
    { type: 'separator' },
    {
      label: 'Quit',
      click: () => {
        app.isQuiting = true;
        app.quit();
      }
    }
  ]);
  
  tray.setToolTip('Faxbot Admin Console');
  tray.setContextMenu(contextMenu);
  
  // Show window on tray click
  tray.on('click', () => {
    if (mainWindow) {
      if (mainWindow.isVisible()) {
        mainWindow.hide();
      } else {
        mainWindow.show();
        mainWindow.focus();
      }
    } else {
      createWindow();
    }
  });
}

function createMenu() {
  const isMac = process.platform === 'darwin';

  const appMenu = isMac ? [{
    label: app.name || 'Faxbot',
    submenu: [
      { role: 'about', label: 'About Faxbot' },
      { type: 'separator' },
      { role: 'services' },
      { type: 'separator' },
      { role: 'hide', label: 'Hide Faxbot' },
      { role: 'hideOthers' },
      { role: 'unhide' },
      { type: 'separator' },
      { role: 'quit', label: 'Quit Faxbot' },
    ]
  }] : [];

  const faxMenu = [{
    label: 'Fax',
    submenu: [
      { label: 'Dashboard', accelerator: 'CmdOrCtrl+D', click: () => mainWindow && mainWindow.webContents.send('navigate-to', '/dashboard') },
      { label: 'Jobs', accelerator: 'CmdOrCtrl+J', click: () => mainWindow && mainWindow.webContents.send('navigate-to', '/jobs') },
      { label: 'Inbox', accelerator: 'CmdOrCtrl+I', click: () => mainWindow && mainWindow.webContents.send('navigate-to', '/inbound') },
      { type: 'separator' },
      { label: 'Diagnostics', click: () => mainWindow && mainWindow.webContents.send('navigate-to', '/diagnostics') },
    ]
  }];

  const helpMenu = [{
    role: 'help',
    submenu: [
      { label: 'Faxbot Documentation', click: () => shell.openExternal('https://dmontgomery40.github.io/Faxbot/') },
      { label: 'About Faxbot', click: () => app.showAboutPanel() },
    ]
  }];

  const template = [
    ...appMenu,
    {
      label: 'File',
      submenu: [
        {
          label: 'Send Fax',
          accelerator: 'CmdOrCtrl+N',
          click: () => { mainWindow && mainWindow.webContents.send('navigate-to', '/send'); }
        },
        { type: 'separator' },
        { role: isMac ? 'close' : 'quit' }
      ]
    },
    {
      label: 'Edit',
      submenu: [
        { role: 'undo' },
        { role: 'redo' },
        { type: 'separator' },
        { role: 'cut' },
        { role: 'copy' },
        { role: 'paste' },
        { role: 'selectAll' }
      ]
    },
    {
      label: 'View',
      submenu: [
        { role: 'reload' },
        { role: 'forceReload' },
        { role: 'toggleDevTools' },
        { type: 'separator' },
        { role: 'resetZoom' },
        { role: 'zoomIn' },
        { role: 'zoomOut' },
        { type: 'separator' },
        { role: 'togglefullscreen' }
      ]
    },
    ...faxMenu,
    { role: 'windowMenu' },
    ...helpMenu,
  ];

  const menu = Menu.buildFromTemplate(template);
  Menu.setApplicationMenu(menu);
}

// App event handlers
app.whenReady().then(() => {
  createWindow();
  createMenu();
  createTray();

  app.on('activate', () => {
    // On macOS, re-create window when dock icon is clicked
    if (BrowserWindow.getAllWindows().length === 0) {
      createWindow();
    }
  });
});

app.on('window-all-closed', () => {
  // On macOS, keep app running even when all windows are closed
  if (process.platform !== 'darwin') {
    app.quit();
  }
});

app.on('before-quit', () => {
  app.isQuiting = true;
});

// Security: Prevent new window creation
app.on('web-contents-created', (event, contents) => {
  contents.on('new-window', (event, navigationUrl) => {
    event.preventDefault();
    shell.openExternal(navigationUrl);
  });
});

// IPC handlers
ipcMain.handle('get-app-version', () => {
  return app.getVersion();
});

ipcMain.handle('show-message-box', async (event, options) => {
  const result = await dialog.showMessageBox(mainWindow, options);
  return result;
});

// Handle file selection for fax sending
ipcMain.handle('select-file', async () => {
  const result = await dialog.showOpenDialog(mainWindow, {
    properties: ['openFile'],
    filters: [
      { name: 'Documents', extensions: ['pdf', 'txt'] },
      { name: 'PDF Files', extensions: ['pdf'] },
      { name: 'Text Files', extensions: ['txt'] }
    ]
  });
  
  return result;
});
