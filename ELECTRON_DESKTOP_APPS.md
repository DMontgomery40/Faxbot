# Faxbot Desktop Applications (Electron)

The Faxbot Admin Console is now available as native desktop applications for macOS, Windows, and Linux! 🚀

## Available Branches

### 🍎 macOS (`electron_macos`)
- **Native macOS app** with proper menu bar integration
- **System tray support** with contextual menus
- **DMG installer** for easy distribution
- **Universal binary** supporting both Intel and Apple Silicon
- **Code signing ready** for distribution outside App Store

### 🪟 Windows (`electron_windows`)
- **Native Windows app** with standard title bars
- **NSIS installer** with proper uninstaller
- **Portable executable** option (no installation required)
- **System tray integration** with Windows notifications
- **Code signing ready** for SmartScreen compatibility

### 🐧 Linux (`electron_linux`)
- **Multiple package formats**: AppImage, DEB, RPM, TAR.GZ
- **Desktop environment integration** (GNOME, KDE, XFCE, etc.)
- **System tray support** (where available)
- **Native GTK dialogs** and styling
- **Distribution-ready packages** for major Linux distros

## Quick Start

### For Users

1. **Download** the appropriate package for your OS from the releases
2. **Install** using your platform's standard method
3. **Ensure** Faxbot API is running (`docker compose up -d`)
4. **Launch** the desktop app

### For Developers

```bash
# Clone and switch to the appropriate branch
git checkout electron_macos    # or electron_windows, electron_linux

# Install dependencies
cd api/admin_ui
npm install

# Run in development mode
npm run electron-dev

# Build for distribution
npm run electron-build
```

## Features

### 🎯 **Core Desktop Features**
- **Native file dialogs** for selecting fax documents
- **System tray integration** with quick actions
- **Keyboard shortcuts** for common operations
- **Native notifications** for fax status updates
- **Auto-updater ready** (when configured)

### 🔒 **Security & Integration**
- **Loads the console from the Faxbot server** at `http://127.0.0.1:8080/admin/ui/` (set `FAXBOT_URL` to use another address), so sign-in works the same as in a browser
- **Native OS security** (code signing, permissions)
- **Sandboxed execution** where supported
- **HIPAA-compliant** data handling

### 📱 **Responsive Design**
- **Same beautiful UI** as the web version
- **Mobile-responsive** design works on all screen sizes
- **Dark/Light theme** support with system integration
- **Professional Material-UI** components

## Architecture

```
┌─────────────────────────────────┐
│       Desktop Application       │
├─────────────────────────────────┤
│  Electron Main Process          │
│  - Window Management            │
│  - System Integration           │
│  - Native Dialogs               │
│  - Menu Bar / System Tray       │
├─────────────────────────────────┤
│  React Renderer Process         │
│  - Faxbot Admin Console UI      │
│  - Responsive Components        │
│  - Theme System                 │
│  - Performance Optimizations    │
├─────────────────────────────────┤
│  Electron API Bridge            │
│  - Secure IPC Communication     │
│  - File System Access           │
│  - Native OS Integration        │
└─────────────────────────────────┘
           │
           ▼ HTTP API Calls
┌─────────────────────────────────┐
│     Faxbot API (Docker)         │
│     http://localhost:8080       │
└─────────────────────────────────┘
```

## Development Workflow

### Branch Strategy

Each OS has its own branch with optimized configurations:

- **`electron_macos`** - macOS-specific builds and documentation
- **`electron_windows`** - Windows-specific builds and documentation  
- **`electron_linux`** - Linux-specific builds and documentation

### Building for Multiple Platforms

```bash
# Build for your current platform
npm run electron-build

# Build for all platforms (requires additional setup)
npm run electron-build-all

# Platform-specific builds
npm run electron-build -- --mac
npm run electron-build -- --win
npm run electron-build -- --linux
```

### Code Signing

Each platform supports code signing for professional distribution:

- **macOS**: Apple Developer Certificate
- **Windows**: Authenticode Certificate
- **Linux**: GPG signing for packages

## Distribution

### Release Packages

Each platform produces different package types:

**macOS:**
- `Faxbot Admin Console.dmg` - Drag-and-drop installer
- `Faxbot Admin Console.app` - Application bundle

**Windows:**
- `Faxbot Admin Console Setup.exe` - NSIS installer
- `Faxbot Admin Console.exe` - Portable executable

**Linux:**
- `Faxbot-Admin-Console.AppImage` - Universal executable
- `faxbot-admin.deb` - Debian/Ubuntu package
- `faxbot-admin.rpm` - Red Hat/Fedora package
- `faxbot-admin.tar.gz` - Manual installation archive

### Auto-Updates

The desktop apps are configured for auto-updates using electron-updater:

1. **Configure update server** in `package.json`
2. **Sign releases** for security
3. **Publish updates** to your distribution channel
4. **Apps automatically check** for updates on startup

## Benefits Over Web Version

### 🚀 **Performance**
- **Faster startup** - no browser overhead
- **Better memory management** - dedicated process
- **Native rendering** - smoother animations
- **Offline capabilities** - cached resources

### 🔧 **Native Integration**
- **File system access** - direct file selection
- **System notifications** - native toast messages
- **Keyboard shortcuts** - OS-standard shortcuts
- **Menu bar integration** - familiar navigation

### 🛡️ **Security**
- **No CORS issues** - direct API communication
- **Sandboxed execution** - isolated from browser
- **Code signing** - verified authenticity
- **Update verification** - signed update packages

### 👥 **User Experience**
- **Always accessible** - dock/taskbar icon
- **System tray** - quick access without opening
- **Native look** - matches OS design language
- **Professional feel** - standalone application

## Requirements

### System Requirements

**macOS:**
- macOS 10.15 (Catalina) or later
- 64-bit processor (Intel or Apple Silicon)

**Windows:**
- Windows 10 or later
- 64-bit processor (x64 or ARM64)

**Linux:**
- Modern Linux distribution (Ubuntu 18.04+, Fedora 30+, etc.)
- 64-bit processor
- GTK 3.0+ (for native dialogs)

### Dependencies

**All Platforms:**
- **Docker** - For running Faxbot API
- **Network access** - To localhost:8080
- **Disk space** - ~200MB for application

## Troubleshooting

### Common Issues

**App won't connect to API:**
1. Ensure Docker is running: `docker ps`
2. Verify API is accessible: `curl http://localhost:8080/health`
3. Check firewall settings

**Build failures:**
1. Update Node.js to 18+
2. Clear npm cache: `npm cache clean --force`
3. Reinstall dependencies: `rm -rf node_modules && npm install`

**Platform-specific issues:**
- See individual branch READMEs for detailed troubleshooting

## Contributing

### Adding Features

1. **Implement in web version first** - maintain UI parity
2. **Test across all platforms** - ensure compatibility
3. **Update all branches** - keep features synchronized
4. **Document changes** - update platform-specific docs

### Platform-Specific Development

1. **Switch to platform branch**: `git checkout electron_macos`
2. **Make platform-specific changes**
3. **Test thoroughly** on target platform
4. **Update documentation**
5. **Submit PR** for that specific branch

## Future Enhancements

### Planned Features

- **Auto-launch on startup** - system service integration
- **Multiple API endpoints** - connect to remote Faxbot instances
- **Plugin system** - extend functionality
- **Advanced notifications** - detailed fax status updates
- **Batch operations** - send multiple faxes at once

### Distribution Channels

- **Mac App Store** - for broader macOS distribution
- **Microsoft Store** - for Windows users
- **Flathub** - universal Linux app store
- **Homebrew Cask** - macOS package manager
- **Chocolatey** - Windows package manager
- **Snap Store** - Ubuntu app store

## Support

For desktop app issues:

1. **Check platform-specific README** in the appropriate branch
2. **Review common troubleshooting** steps above
3. **Open an issue** on the main Faxbot repository
4. **Include platform details** and error logs

---

**The Faxbot desktop applications bring the power of the Admin Console directly to your desktop with native OS integration, better performance, and a professional user experience!** 🎉
