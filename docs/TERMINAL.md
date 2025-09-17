# Faxbot Terminal - Admin Console

The Faxbot Admin Console now includes a built-in terminal feature that provides direct shell access to the Faxbot container or server environment, similar to popular self-hosted applications like BirdNET-Pi, Scrypted, and Pi-hole.

## Features

- **Direct TTY Access**: Full terminal interface via WebSocket connection
- **No Additional Auth**: Uses existing admin authentication - no separate login required
- **Rich Terminal Experience**: 
  - Full color support with xterm-256color
  - Command history and tab completion
  - Standard terminal shortcuts (Ctrl+C, Ctrl+D, Ctrl+L, etc.)
  - Clickable links
  - Copy/paste support
- **Container-Aware**: Automatically detects Docker environment
- **Responsive**: Works on desktop and mobile devices
- **Fullscreen Mode**: Expand terminal to full browser window

## Installation

### Docker Deployment (Recommended)
If you're using Docker Compose, the terminal feature is automatically available. The required Python dependencies (`websockets` and `pexpect`) are included in the container image.

### Manual Installation
For local development or non-Docker deployments:

```bash
# From the faxbot root directory
cd api
pip install websockets==12.0 pexpect==4.9.0

# Install Admin UI dependencies
cd admin_ui
npm install
```

Or use the provided installation script:
```bash
./scripts/install-terminal-deps.sh
```

## Usage

1. **Access the Admin Console**: Navigate to `http://localhost:8080/admin`

2. **Login**: Use your admin API key to authenticate

3. **Open Terminal**: Click on the "Terminal" tab in the navigation

4. **Start Using**: The terminal will automatically connect and provide a shell prompt

## Terminal Controls

- **Clear Terminal**: Click the Clear button or press Ctrl+L
- **Copy All**: Click Copy All button to copy entire terminal output
- **Fullscreen**: Click Fullscreen button for distraction-free mode
- **Reconnect**: If disconnected, click the Refresh button to reconnect

## Keyboard Shortcuts

| Shortcut | Action |
|----------|--------|
| Ctrl+C | Interrupt current command |
| Ctrl+D | Exit shell (will disconnect) |
| Ctrl+L | Clear screen |
| Ctrl+A | Move to line start |
| Ctrl+E | Move to line end |
| Tab | Auto-complete |
| ↑/↓ | Command history |

## Security Considerations

- **Admin Only**: Terminal access requires admin authentication (API key with `keys:manage` scope or bootstrap env key)
- **Local Access**: By default, admin console is only accessible from localhost (127.0.0.1)
- **No Privilege Escalation**: Terminal runs with same privileges as the API service
- **Container Isolation**: In Docker deployments, terminal is isolated within the container

## Use Cases

- **Debugging**: Check logs, environment variables, and service status
- **Configuration**: Edit configuration files directly
- **Maintenance**: Run maintenance scripts and database operations
- **Monitoring**: Check resource usage with `top`, `df`, `ps`
- **File Management**: Browse and manage fax data files
- **Testing**: Run test commands and verify integrations

## Troubleshooting

### Terminal Won't Connect
- Verify the API server is running
- Check WebSocket support isn't blocked by proxy/firewall
- Ensure you have admin privileges (correct API key)
- Check browser console for errors
- If you see it spin then fail: on older code, the server tried to `decode()` the WebSocket query which is already a string in Starlette ≥0.27. Update to the latest Faxbot or patch `admin_terminal_websocket` to parse `websocket.url.query` as a string.

### No Shell Available
- The container/server needs bash or sh installed
- For minimal containers, ensure a shell is available in the image

### Terminal Disconnects
- Check for network issues
- The server may have restarted
- Session timeout after prolonged inactivity (30+ minutes)
- Click Reconnect button to establish new session

### Permission Denied Errors
- Terminal runs as the same user as the API service
- Cannot perform operations requiring root/sudo
- For elevated operations, use docker exec from host

## Environment Details

The terminal environment includes:
- Shell: `/bin/bash` (fallback to `/bin/sh` if not available)
- Terminal Type: `xterm-256color`
- Working Directory: `/app` (in Docker) or API directory
- User: Same as API service user

## Browser Compatibility

The terminal works best in modern browsers:
- Chrome/Edge 90+
- Firefox 88+  
- Safari 14+
- Mobile browsers with WebSocket support

## Technical Architecture

The terminal feature uses:
- **Backend**: FastAPI WebSocket endpoint with pexpect for pseudo-terminal
- **Frontend**: React component with xterm.js for terminal emulation
- **Protocol**: JSON over WebSocket for bidirectional communication
- **Auth**: API key validation via query parameter or header

## Customization

### Terminal Theme
The terminal uses a dark theme optimized for the Faxbot Admin Console. Colors can be customized in `Terminal.tsx`:

```javascript
theme: {
  background: '#0B0F14',
  foreground: '#C9D1D9',
  cursor: '#58A6FF',
  // ... additional color settings
}
```

### Shell Prompt
The default prompt shows `user@faxbot:path$`. Customize via PS1 environment variable in `terminal.py`.

## Comparison with Other Self-Hosted Apps

| Feature | Faxbot | BirdNET-Pi | Scrypted | Pi-hole |
|---------|--------|------------|----------|---------|
| Built-in Terminal | ✅ | ✅ | ✅ | ✅ |
| No Extra Auth | ✅ | ✅ | ❌ | ✅ |
| Full TTY | ✅ | ✅ | ✅ | Limited |
| Mobile Support | ✅ | ✅ | ✅ | ✅ |
| Fullscreen | ✅ | ❌ | ✅ | ❌ |

## Future Enhancements

Potential future improvements:
- [ ] Multiple terminal sessions/tabs
- [ ] Terminal sharing/collaboration
- [ ] Command snippets/macros
- [ ] File upload/download via terminal
- [ ] Persistent session reconnection
- [ ] Custom shell configurations
- [ ] Terminal recording/playback
