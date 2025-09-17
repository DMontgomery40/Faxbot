# Faxbot Admin Console - Linux Desktop App

This branch contains the Linux-specific Electron build configuration for the Faxbot Admin Console.

## Features

- **Native Linux App** - Runs as a standalone desktop application
- **Multiple Package Formats** - AppImage, DEB, RPM, and TAR.GZ
- **System Tray Integration** - Works with most Linux desktop environments
- **Native File Dialogs** - GTK-style file selection
- **Desktop Integration** - Proper .desktop file and icon integration
- **Auto-updater Ready** - Built-in update mechanism (when configured)

## Building for Linux

### Prerequisites

- Node.js 18+
- npm or yarn
- Linux development environment
- Build tools: `build-essential` (Ubuntu/Debian) or equivalent

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
# Build all Linux packages
npm run electron-build

# Build specific format
npx electron-builder --linux appimage  # AppImage only
npx electron-builder --linux deb       # DEB package only
npx electron-builder --linux rpm       # RPM package only
```

### Build Outputs

The build process creates:

- **AppImage** (`Faxbot-Admin-Console-1.0.0.AppImage`) - Universal Linux executable
- **DEB Package** (`faxbot-admin_1.0.0_amd64.deb`) - Debian/Ubuntu installer
- **RPM Package** (`faxbot-admin-1.0.0.x86_64.rpm`) - Red Hat/Fedora installer
- **TAR.GZ** (`faxbot-admin-1.0.0.tar.gz`) - Archive for manual installation

## Linux Distribution Support

### Tested Distributions

- **Ubuntu** 20.04+ (DEB package recommended)
- **Debian** 11+ (DEB package recommended)
- **Fedora** 35+ (RPM package recommended)
- **CentOS/RHEL** 8+ (RPM package recommended)
- **Arch Linux** (AppImage or manual installation)
- **openSUSE** (RPM package recommended)

### Desktop Environment Support

- **GNOME** - Full integration including system tray
- **KDE Plasma** - Full integration with native styling
- **XFCE** - Good integration, basic system tray
- **Cinnamon** - Full integration
- **MATE** - Good integration
- **i3/Sway** - Basic window management, no system tray

## Installation Methods

### AppImage (Recommended for most users)

```bash
# Download and make executable
chmod +x Faxbot-Admin-Console-1.0.0.AppImage

# Run directly
./Faxbot-Admin-Console-1.0.0.AppImage

# Optional: Integrate with desktop
./Faxbot-Admin-Console-1.0.0.AppImage --appimage-extract-and-run
```

### DEB Package (Ubuntu/Debian)

```bash
# Install
sudo dpkg -i faxbot-admin_1.0.0_amd64.deb

# Fix dependencies if needed
sudo apt-get install -f

# Run from applications menu or terminal
faxbot-admin
```

### RPM Package (Fedora/RHEL/CentOS)

```bash
# Install
sudo rpm -i faxbot-admin-1.0.0.x86_64.rpm

# Or using dnf/yum
sudo dnf install faxbot-admin-1.0.0.x86_64.rpm

# Run from applications menu or terminal
faxbot-admin
```

### Manual Installation (TAR.GZ)

```bash
# Extract
tar -xzf faxbot-admin-1.0.0.tar.gz

# Move to system location
sudo mv faxbot-admin /opt/

# Create symlink
sudo ln -s /opt/faxbot-admin/faxbot-admin /usr/local/bin/

# Run
faxbot-admin
```

## Linux-Specific Features

### System Integration

- **Desktop Entry** - Appears in application menus
- **MIME Type Associations** - Can open PDF/TXT files
- **System Tray** - Minimize to system tray (DE dependent)
- **Native Notifications** - libnotify-based notifications

### Security

- **Sandboxing** - AppImage runs in user space
- **Package Signing** - DEB/RPM packages can be signed
- **Permission Model** - Standard Linux file permissions

## Configuration

### API Connection

The Linux app automatically connects to:
- **Development**: `http://localhost:8080` (Docker API)
- **Production**: Configurable API endpoint

### Linux-Specific Settings

Edit `electron/main.js` to customize:

```javascript
// Linux-specific window options
const windowOptions = {
  icon: path.join(__dirname, 'assets', 'icon.png'),
  titleBarStyle: 'default',
  // ... other Linux options
};
```

### System Tray Configuration

The system tray behavior varies by desktop environment:

```javascript
// Check for system tray support
if (process.platform === 'linux') {
  // Some Linux DEs don't support system tray
  const hasTraySupport = /* check DE */;
  if (hasTraySupport) {
    createTray();
  }
}
```

## Docker Integration

### Running with Docker

The Linux app expects the Faxbot API to be running in Docker:

```bash
# Start Faxbot API
cd /path/to/faxbot
docker compose up -d

# Verify API is accessible
curl http://localhost:8080/health

# Run Faxbot Admin Console
faxbot-admin
```

### Firewall Configuration

Ensure the Docker API port is accessible:

```bash
# UFW (Ubuntu)
sudo ufw allow 8080

# firewalld (Fedora/RHEL)
sudo firewall-cmd --add-port=8080/tcp --permanent
sudo firewall-cmd --reload

# iptables (manual)
sudo iptables -A INPUT -p tcp --dport 8080 -j ACCEPT
```

## Troubleshooting

### Common Issues

**App won't start:**
- Check dependencies: `ldd Faxbot-Admin-Console-1.0.0.AppImage`
- Ensure Docker is running: `docker ps`
- Verify API accessibility: `curl http://localhost:8080/health`

**System tray not working:**
- Check desktop environment support
- Install system tray extensions (GNOME)
- Use alternative window management

**Permission denied:**
- Make AppImage executable: `chmod +x *.AppImage`
- Check file ownership and permissions
- Run with `--no-sandbox` if needed (not recommended)

**Build fails:**
- Install build dependencies: `sudo apt install build-essential`
- Check Node.js version: `node --version`
- Clear npm cache: `npm cache clean --force`

### Debug Mode

Run with debug logging:

```bash
# Enable Electron debug logs
DEBUG=electron* ./Faxbot-Admin-Console-1.0.0.AppImage

# Or for installed version
DEBUG=electron* faxbot-admin
```

### Log Files

Application logs are stored in:
- **AppImage**: `~/.config/Faxbot Admin Console/logs/`
- **Installed**: `~/.config/faxbot-admin/logs/`

## Architecture

```
┌─────────────────────────────────┐
│         Linux Desktop App       │
├─────────────────────────────────┤
│  Electron Main Process          │
│  - Window Management            │
│  - System Tray (DE dependent)   │
│  - Native Dialogs (GTK)         │
│  - Desktop Integration          │
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

## Distribution

### Package Repositories

Consider submitting to:
- **Flathub** - Universal Linux app store
- **Snap Store** - Ubuntu's app store
- **AUR** - Arch User Repository
- **Distribution-specific repos**

### Signing Packages

```bash
# Sign DEB packages
dpkg-sig --sign builder faxbot-admin_1.0.0_amd64.deb

# Sign RPM packages
rpm --addsign faxbot-admin-1.0.0.x86_64.rpm
```

## Support

For Linux-specific issues:
1. Check this README
2. Review distribution-specific documentation
3. Check desktop environment compatibility
4. Open an issue on the Faxbot repository
