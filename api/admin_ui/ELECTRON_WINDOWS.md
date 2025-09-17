# Faxbot Admin Console - Windows Desktop App

This branch contains the Windows-specific Electron build configuration for the Faxbot Admin Console.

## Features

- **Native Windows App** - Runs as a standalone desktop application
- **System Tray Integration** - Minimize to system tray with quick actions
- **Native File Dialogs** - Windows-style file selection for fax documents
- **Auto-updater Ready** - Built-in update mechanism (when configured)
- **NSIS Installer** - Professional Windows installer package
- **Portable Version** - No-install executable option

## Building for Windows

### Prerequisites

- Node.js 18+ 
- npm or yarn
- Windows 10/11 (for native builds)

### Development

```bash
# Install dependencies
npm install

# Run in development mode (with hot reload)
npm run electron-dev

# Run built version
npm run electron
```

### Production Build

```bash
# Build Windows installer and portable app
npm run electron-build

# Build all platforms (requires additional setup)
npm run electron-build-all
```

### Build Outputs

The build process creates:

- **NSIS Installer** (`Faxbot Admin Console Setup.exe`) - Full installer with uninstaller
- **Portable App** (`Faxbot Admin Console.exe`) - Single executable, no installation required

## Windows-Specific Features

### System Integration

- **Start Menu Entry** - Appears in Windows Start Menu
- **Desktop Shortcut** - Optional desktop icon during installation
- **File Associations** - Can be configured to open PDF/TXT files
- **Windows Notifications** - Native toast notifications for fax status

### Security

- **Code Signing** - Ready for Authenticode signing (certificate required)
- **SmartScreen Compatible** - Properly configured for Windows Defender
- **UAC Aware** - Handles Windows User Account Control properly

## Configuration

### API Connection

The Windows app automatically connects to:
- **Development**: `http://localhost:8080` (Docker API)
- **Production**: Configurable API endpoint

### Windows-Specific Settings

Edit `electron/main.js` to customize:

```javascript
// Windows-specific window options
const windowOptions = {
  titleBarStyle: 'default', // Windows standard title bar
  autoHideMenuBar: false,   // Show menu bar by default
  // ... other Windows options
};
```

## Deployment

### For Development/Testing

1. Build the app: `npm run electron-build`
2. Distribute the installer or portable app
3. Users run the installer or executable

### For Production

1. **Code Signing** (Recommended):
   - Obtain an Authenticode certificate
   - Configure signing in `package.json`
   - Build with signing enabled

2. **Distribution**:
   - Upload to your website
   - Use Windows Package Manager (winget)
   - Microsoft Store (requires additional setup)

## Troubleshooting

### Common Issues

**App won't start:**
- Ensure Docker is running with Faxbot API on port 8080
- Check Windows Defender/antivirus isn't blocking the app

**Build fails:**
- Run `npm install` to ensure all dependencies are installed
- Check Node.js version (requires 18+)
- Ensure you have sufficient disk space

**Connection issues:**
- Verify Faxbot API is accessible at `http://localhost:8080`
- Check Windows Firewall settings
- Ensure Docker Desktop is running

### Debug Mode

Run with debug logging:

```bash
# Enable Electron debug logs
set DEBUG=electron*
npm run electron
```

## Architecture

```
┌─────────────────────────────────┐
│        Windows Desktop App      │
├─────────────────────────────────┤
│  Electron Main Process          │
│  - Window Management            │
│  - System Tray                  │
│  - Native Dialogs               │
│  - Auto-updater                 │
├─────────────────────────────────┤
│  React Renderer Process         │
│  - Faxbot Admin UI              │
│  - Material-UI Components       │
│  - Responsive Design            │
├─────────────────────────────────┤
│  API Client                     │
│  - HTTP to localhost:8080       │
│  - File Upload Handling         │
│  - Error Management             │
└─────────────────────────────────┘
           │
           ▼
┌─────────────────────────────────┐
│     Faxbot API (Docker)         │
│     http://localhost:8080       │
└─────────────────────────────────┘
```

## Support

For Windows-specific issues:
1. Check this README
2. Review the main Faxbot documentation
3. Open an issue on the Faxbot repository
