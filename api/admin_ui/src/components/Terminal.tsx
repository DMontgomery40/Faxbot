import React, { useEffect, useRef, useState, useCallback } from 'react';
import { 
  Box, 
  Paper, 
  Typography, 
  Alert, 
  CircularProgress, 
  IconButton, 
  Tooltip,
  useTheme,
  useMediaQuery,
  Stack,
  Fade,
  Button,
  ButtonGroup,
} from '@mui/material';
import {
  Refresh as RefreshIcon,
  Fullscreen as FullscreenIcon,
  FullscreenExit as FullscreenExitIcon,
  ContentCopy as ContentCopyIcon,
  Clear as ClearIcon,
  Terminal as TerminalIcon,
  WifiOff as DisconnectedIcon,
} from '@mui/icons-material';
import { Terminal as XTerm } from '@xterm/xterm';
import { FitAddon } from '@xterm/addon-fit';
import { WebLinksAddon } from '@xterm/addon-web-links';
import '@xterm/xterm/css/xterm.css';
import { AdminAPIError, isNotAvailable } from '../api/client';
import type AdminAPIClient from '../api/client';

interface TerminalProps {
  client: AdminAPIClient;
}

type AccessState = 'checking' | 'enabled' | 'disabled' | 'error';
type ConnectionState = 'connecting' | 'connected' | 'disconnected' | 'forbidden' | 'error';

const terminalTheme = (mode: string) => ({
  background: mode === 'dark' ? '#0B0F14' : '#1e1e1e',
  foreground: mode === 'dark' ? '#C9D1D9' : '#d4d4d4',
  cursor: '#58A6FF',
  black: '#0D1117', red: '#FF7B72', green: '#7EE83F', yellow: '#FFA657',
  blue: '#79C0FF', magenta: '#D2A8FF', cyan: '#A5D6FF', white: '#C9D1D9',
  brightBlack: '#6E7681', brightRed: '#FFA198', brightGreen: '#56D364',
  brightYellow: '#FFB454', brightBlue: '#79C0FF', brightMagenta: '#D2A8FF',
  brightCyan: '#56D4DD', brightWhite: '#FFFFFF', selectionBackground: '#3392FF44',
});

const Terminal: React.FC<TerminalProps> = ({ client }) => {
  const terminalRef = useRef<HTMLDivElement>(null);
  const termRef = useRef<XTerm | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  // The socket that has already sent its ticket; input waits for it.
  const authenticatedRef = useRef<WebSocket | null>(null);
  const connectAttemptRef = useRef(0);
  const [access, setAccess] = useState<AccessState>('checking');
  const [availabilityAttempt, setAvailabilityAttempt] = useState(0);
  const [connection, setConnection] = useState<ConnectionState>('connecting');
  const [error, setError] = useState<string | null>(null);
  const [fullscreen, setFullscreen] = useState(false);
  const fitAddonRef = useRef<FitAddon | null>(null);
  const pingIntervalRef = useRef<number | null>(null);
  const connectionTimeoutRef = useRef<number | null>(null);
  const fitTimeoutRef = useRef<number | null>(null);
  const connected = connection === 'connected';
  const loading = access === 'checking' || (access === 'enabled' && connection === 'connecting');

  const theme = useTheme();
  const isMobile = useMediaQuery(theme.breakpoints.down('md'));
  const isSmallMobile = useMediaQuery(theme.breakpoints.down('sm'));
  const displayRef = useRef({ mode: theme.palette.mode, isMobile, isSmallMobile });
  displayRef.current = { mode: theme.palette.mode, isMobile, isSmallMobile };

  // Initialize terminal
  const initTerminal = useCallback(() => {
    if (!terminalRef.current) return;

    // Clean up existing terminal
    if (termRef.current) {
      termRef.current.dispose();
    }
    const { mode, isMobile, isSmallMobile } = displayRef.current;

    // Create new terminal instance with responsive settings
    const term = new XTerm({
      cursorBlink: true,
      cursorStyle: 'block',
      fontSize: isSmallMobile ? 12 : isMobile ? 13 : 14,
      fontFamily: '"Cascadia Code", "JetBrains Mono", "Fira Code", Consolas, "Courier New", monospace',
      theme: terminalTheme(mode),
      allowTransparency: false,
      scrollback: isMobile ? 5000 : 10000, // Reduce scrollback on mobile
      convertEol: true,
      cols: isSmallMobile ? 60 : 80, // Smaller initial columns for mobile
    });

    // Add fit addon for responsive resizing
    const fitAddon = new FitAddon();
    fitAddonRef.current = fitAddon;
    term.loadAddon(fitAddon);

    // Add web links addon
    const webLinksAddon = new WebLinksAddon();
    term.loadAddon(webLinksAddon);

    // Open terminal in the DOM element
    term.open(terminalRef.current);
    
    // Ensure the terminal captures keyboard input
    try {
      term.focus();
      term.attachCustomKeyEventHandler(() => true);
    } catch {}
    
    // Initial fit
    fitTimeoutRef.current = window.setTimeout(() => {
      try { fitAddon.fit(); } catch {}
    }, 0);

    termRef.current = term;
    return term;
  }, []);

  const clearConnectionTimers = useCallback(() => {
    if (pingIntervalRef.current !== null) window.clearInterval(pingIntervalRef.current);
    if (connectionTimeoutRef.current !== null) window.clearTimeout(connectionTimeoutRef.current);
    pingIntervalRef.current = null;
    connectionTimeoutRef.current = null;
  }, []);

  const closeSocket = useCallback(() => {
    clearConnectionTimers();
    connectAttemptRef.current += 1;
    const ws = wsRef.current;
    wsRef.current = null;
    authenticatedRef.current = null;
    if (ws) {
      ws.onopen = ws.onmessage = ws.onerror = ws.onclose = null;
      ws.close();
    }
  }, [clearConnectionTimers]);

  // The settings' environment-only view says whether this installation turns the terminal on.
  // Do not initialize a shell socket until the server grants that capability.
  useEffect(() => {
    let current = true;
    setAccess('checking');
    setError(null);
    const timeout = window.setTimeout(() => {
      if (!current) return;
      current = false;
      setAccess('error');
    }, 10000);
    void client.getSettings().then((result) => {
      if (!current) return;
      window.clearTimeout(timeout);
      const effective = result.deployment?.ENABLE_ADMIN_EXEC?.effective;
      // Without that view, let the terminal ticket request decide.
      setAccess(effective === false ? 'disabled' : 'enabled');
    }).catch((failure: unknown) => {
      if (!current) return;
      window.clearTimeout(timeout);
      // Reading settings has its own permission; when it is refused, let the
      // terminal ticket request decide whether this account may connect.
      const refused = failure instanceof AdminAPIError && (failure.status === 401 || failure.status === 403);
      setAccess(refused ? 'enabled' : 'error');
    });
    return () => { current = false; window.clearTimeout(timeout); };
  }, [client, availabilityAttempt]);

  // Connect: mint a fresh single-use ticket, open the socket without any
  // credential in the URL, and send the ticket as the very first message.
  const connectWebSocket = useCallback(async () => {
    closeSocket();
    const attempt = ++connectAttemptRef.current;
    setConnection('connecting');
    setError(null);

    let ticket: string;
    try {
      ticket = (await client.createTerminalTicket()).ticket;
    } catch (failure) {
      if (attempt !== connectAttemptRef.current) return;
      if (failure instanceof AdminAPIError && (failure.status === 401 || failure.status === 403)) {
        setConnection('forbidden');
        setError('This account is not allowed to use the terminal.');
      } else if (isNotAvailable(failure)) {
        setConnection('error');
        setError('The terminal is not available on this server yet.');
      } else {
        setConnection('error');
        setError('Could not start a terminal session. Try again.');
      }
      return;
    }
    if (attempt !== connectAttemptRef.current) return;

    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${protocol}//${window.location.host}/admin/terminal`;

    let ws: WebSocket;
    try {
      ws = new WebSocket(wsUrl);
    } catch {
      setConnection('error');
      setError('Could not open the terminal connection. Check the connection and try again.');
      return;
    }
    wsRef.current = ws;
    connectionTimeoutRef.current = window.setTimeout(() => {
      if (wsRef.current !== ws) return;
      closeSocket();
      setConnection('error');
      setError('The terminal connection timed out. Check the service and try again.');
    }, 10000);

    ws.onopen = () => {
      if (wsRef.current !== ws) return;
      // Nothing may precede the ticket, including a resize from fit().
      ws.send(JSON.stringify({ type: 'auth', ticket }));
      authenticatedRef.current = ws;
      clearConnectionTimers();
      setConnection('connected');
      setError(null);
      try { fitAddonRef.current?.fit(); termRef.current?.focus(); } catch {}

      // Start ping interval
      pingIntervalRef.current = window.setInterval(() => {
        if (ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ type: 'ping' }));
        }
      }, 30000);
    };

    ws.onmessage = (event) => {
      if (wsRef.current !== ws) return;
      try {
        const data = JSON.parse(event.data);
        
        if (data.type === 'output' && termRef.current) {
          termRef.current.write(data.data);
        } else if (data.type === 'error') {
          setError(typeof data.message === 'string' ? data.message : 'The terminal session failed.');
          setConnection('error');
          closeSocket();
        } else if (data.type === 'exit') {
          termRef.current?.write('\r\n\x1b[1;31mTerminal session ended.\x1b[0m\r\n');
          setConnection('disconnected');
          closeSocket();
        }
      } catch {
        setError('The terminal returned an invalid response. Reconnect to try again.');
        setConnection('error');
        closeSocket();
      }
    };

    ws.onerror = () => {
      if (wsRef.current !== ws) return;
      setError('Could not connect to the terminal. Check service availability and administrator access.');
      setConnection('error');
      closeSocket();
    };

    ws.onclose = (ev) => {
      if (wsRef.current !== ws) return;
      clearConnectionTimers();
      wsRef.current = null;
      authenticatedRef.current = null;
      if (ev.code === 1008) {
        setConnection('forbidden');
        setError('Terminal closed: access revoked');
      } else {
        setConnection('disconnected');
        setError(ev.code === 1000
          ? 'The terminal session ended.'
          : 'The terminal connection closed. Reconnect to start a new session.');
      }
    };
  }, [client, closeSocket, clearConnectionTimers]);

  // Set up WebSocket and terminal handlers
  useEffect(() => {
    if (access !== 'enabled') return;
    const terminal = initTerminal();
    if (!terminal) {
      setConnection('error');
      setError('The terminal display could not initialize. Reload availability to try again.');
      return;
    }
    void connectWebSocket();

    // Handle terminal input
    const disposable = terminal.onData((data) => {
      const current = wsRef.current;
      if (current && current === authenticatedRef.current && current.readyState === WebSocket.OPEN) {
        current.send(JSON.stringify({
          type: 'input',
          data: data
        }));
      }
    });

    // Handle terminal resize
    const resizeDisposable = terminal.onResize((size) => {
      const current = wsRef.current;
      if (current && current === authenticatedRef.current && current.readyState === WebSocket.OPEN) {
        current.send(JSON.stringify({
          type: 'resize',
          cols: size.cols,
          rows: size.rows
        }));
      }
    });

    return () => {
      disposable.dispose();
      resizeDisposable.dispose();
      closeSocket();
      if (fitTimeoutRef.current !== null) window.clearTimeout(fitTimeoutRef.current);
      terminal.dispose();
      termRef.current = null;
      fitAddonRef.current = null;
    };
  }, [access, initTerminal, connectWebSocket, closeSocket]);

  // Display preferences update the current terminal without replacing its
  // socket or shell session.
  useEffect(() => {
    const terminal = termRef.current;
    if (!terminal) return;
    terminal.options.theme = terminalTheme(theme.palette.mode);
    terminal.options.fontSize = isSmallMobile ? 12 : isMobile ? 13 : 14;
    terminal.options.scrollback = isMobile ? 5000 : 10000;
    try { fitAddonRef.current?.fit(); } catch {}
  }, [access, theme.palette.mode, isSmallMobile, isMobile]);

  // Handle window resize
  useEffect(() => {
    const handleResize = () => {
      try { fitAddonRef.current?.fit(); } catch {}
    };

    window.addEventListener('resize', handleResize);
    return () => window.removeEventListener('resize', handleResize);
  }, []);

  // Reconnect function
  const handleReconnect = () => {
    // Recheck permission before every new session, including after failures.
    setAvailabilityAttempt((attempt) => attempt + 1);
  };

  // Clear terminal
  const handleClear = () => {
    termRef.current?.clear();
  };

  // Copy all terminal content
  const handleCopyAll = () => {
    const terminal = termRef.current;
    if (terminal) {
      const selection = terminal.getSelection();
      if (selection) {
        navigator.clipboard.writeText(selection);
      } else {
        // Select all and copy
        terminal.selectAll();
        const allContent = terminal.getSelection();
        if (allContent) {
          navigator.clipboard.writeText(allContent);
          terminal.clearSelection();
        }
      }
    }
  };

  // Toggle fullscreen
  const handleFullscreen = () => {
    setFullscreen(!fullscreen);
    setTimeout(() => {
      if (fitAddonRef.current) {
        fitAddonRef.current.fit();
      }
    }, 100);
  };

  const terminalHeight = () => {
    if (fullscreen) return '100vh';
    if (isSmallMobile) return '400px';
    if (isMobile) return '500px';
    return '600px';
  };

  return (
    <Box sx={{ height: '100%', display: 'flex', flexDirection: 'column', p: { xs: 2, sm: 0 } }}>
      <Box 
        sx={{ 
          display: 'flex', 
          justifyContent: 'space-between', 
          alignItems: { xs: 'flex-start', sm: 'center' },
          flexDirection: { xs: 'column', sm: 'row' },
          gap: 2,
          mb: 3
        }}
      >
        <Box>
          <Typography variant="h4" component="h1" gutterBottom>
            Terminal
          </Typography>
          <Typography variant="body2" color="text.secondary">
            Administrative shell access to this Faxbot installation
          </Typography>
        </Box>
        
        <Box>
          {access !== 'enabled' ? (
            <Button onClick={handleReconnect} disabled={access === 'checking'} startIcon={<RefreshIcon />}>
              Reload availability
            </Button>
          ) : isSmallMobile ? (
            <Stack direction="row" spacing={1}>
              {!connected && (
                <IconButton aria-label="Reconnect terminal" disabled={loading} onClick={handleReconnect} color="primary" sx={{ borderRadius: 2 }}>
                  <RefreshIcon />
                </IconButton>
              )}
              <IconButton aria-label="Clear terminal" onClick={handleClear} sx={{ borderRadius: 2 }}>
                <ClearIcon />
              </IconButton>
              <IconButton aria-label="Copy terminal content" onClick={handleCopyAll} sx={{ borderRadius: 2 }}>
                <ContentCopyIcon />
              </IconButton>
              <IconButton aria-label={fullscreen ? 'Exit fullscreen' : 'Fullscreen terminal'} onClick={handleFullscreen} sx={{ borderRadius: 2 }}>
                {fullscreen ? <FullscreenExitIcon /> : <FullscreenIcon />}
              </IconButton>
            </Stack>
          ) : (
            <ButtonGroup variant="outlined" size={isMobile ? "small" : "medium"}>
              {!connected && (
                <Tooltip title="Reconnect">
                  <Button disabled={loading} onClick={handleReconnect} startIcon={<RefreshIcon />}>
                    Reconnect
                  </Button>
                </Tooltip>
              )}
              <Tooltip title="Clear Terminal">
                <Button onClick={handleClear} startIcon={<ClearIcon />}>
                  Clear
                </Button>
              </Tooltip>
              <Tooltip title="Copy All">
                <Button onClick={handleCopyAll} startIcon={<ContentCopyIcon />}>
                  Copy
                </Button>
              </Tooltip>
              <Tooltip title={fullscreen ? "Exit Fullscreen" : "Fullscreen"}>
                <Button onClick={handleFullscreen} startIcon={fullscreen ? <FullscreenExitIcon /> : <FullscreenIcon />}>
                  {fullscreen ? "Exit" : "Full"}
                </Button>
              </Tooltip>
            </ButtonGroup>
          )}
        </Box>
      </Box>

      {access === 'disabled' && <Alert severity="info" sx={{ mb: 2, borderRadius: 2 }}>
        The terminal is turned off for this installation.
      </Alert>}

      {access === 'error' && <Alert severity="warning" sx={{ mb: 2, borderRadius: 2 }}>
        Could not check terminal availability. Try again.
      </Alert>}

      {error && (
        <Fade in>
          <Alert 
            severity="error" 
            sx={{ mb: 2, borderRadius: 2 }}
            onClose={() => setError(null)}
          >
            {error}
          </Alert>
        </Fade>
      )}

      {access === 'checking' && (
        <Paper sx={{ 
          display: 'flex', 
          justifyContent: 'center', 
          alignItems: 'center', 
          minHeight: 300,
          borderRadius: 3,
          border: '1px solid',
          borderColor: 'divider',
        }}>
          <Stack alignItems="center" spacing={2}>
            <CircularProgress />
            <Typography color="text.secondary">
              Checking terminal availability…
            </Typography>
          </Stack>
        </Paper>
      )}

      {access === 'enabled' && (
        <Paper 
          elevation={0}
          sx={{ 
            flex: 1, 
            p: { xs: 1, sm: 2 },
            bgcolor: theme.palette.mode === 'dark' ? '#0B0F14' : '#1e1e1e',
            border: '1px solid',
            borderColor: 'divider',
            borderRadius: 3,
            position: fullscreen ? 'fixed' : 'relative',
            top: fullscreen ? 0 : 'auto',
            left: fullscreen ? 0 : 'auto',
            right: fullscreen ? 0 : 'auto',
            bottom: fullscreen ? 0 : 'auto',
            zIndex: fullscreen ? theme.zIndex.modal + 1 : 'auto',
            height: terminalHeight(),
            display: 'flex',
            flexDirection: 'column',
            overflow: 'hidden',
          }}
        >
          {fullscreen && (
            <Box 
              sx={{ 
                display: 'flex', 
                justifyContent: 'space-between', 
                alignItems: 'center', 
                mb: 1, 
                px: 2,
                pt: 2,
                borderBottom: '1px solid rgba(255,255,255,0.1)',
                pb: 1,
              }}
            >
              <Stack direction="row" alignItems="center" spacing={1}>
                <TerminalIcon sx={{ color: '#C9D1D9' }} />
                <Typography variant="h6" sx={{ color: '#C9D1D9' }}>
                  Faxbot Terminal
                </Typography>
              </Stack>
              <IconButton onClick={handleFullscreen} sx={{ color: '#C9D1D9' }}>
                <FullscreenExitIcon />
              </IconButton>
            </Box>
          )}
          
          <Box 
            ref={terminalRef}
            sx={{ 
              flex: 1,
              '& .xterm': {
                padding: isSmallMobile ? '5px' : '10px',
                height: '100%'
              },
              '& .xterm-viewport': {
                backgroundColor: theme.palette.mode === 'dark' ? '#0B0F14' : '#1e1e1e',
              },
              cursor: connected ? 'text' : 'default',
            }}
            tabIndex={0}
            onClick={() => { try { termRef.current?.focus(); } catch {} }}
          />
          
          {!connected && (
            <Fade in>
              <Box sx={{ 
                position: 'absolute', 
                top: '50%', 
                left: '50%', 
                transform: 'translate(-50%, -50%)',
                textAlign: 'center',
                p: 3,
                borderRadius: 2,
                bgcolor: theme.palette.mode === 'dark' ? 'rgba(0,0,0,0.5)' : 'rgba(255,255,255,0.9)',
              }}>
                {loading ? <CircularProgress sx={{ mb: 2 }} /> : <DisconnectedIcon sx={{ fontSize: 48, color: 'text.secondary', mb: 2 }} />}
                <Typography variant="h6" sx={{ color: 'text.primary', mb: 2 }}>
                  {loading ? 'Connecting to terminal…' : connection === 'forbidden' ? 'Terminal closed' : connection === 'error' ? 'Terminal unavailable' : 'Terminal disconnected'}
                </Typography>
                {!loading && <Button
                  variant="contained"
                  onClick={handleReconnect}
                  startIcon={<RefreshIcon />}
                  size="large"
                  sx={{ borderRadius: 2 }}
                >
                  Reconnect
                </Button>}
              </Box>
            </Fade>
          )}
        </Paper>
      )}

      {connected && !fullscreen && (
        <Fade in>
          <Alert 
            severity="info" 
            icon={<TerminalIcon />}
            sx={{ 
              mt: 2, 
              py: 1,
              borderRadius: 2,
            }}
          >
            <Stack spacing={0.5}>
              <Typography variant="caption" fontWeight={600}>
                Terminal Shortcuts
              </Typography>
              <Typography variant="caption" component="div">
                {isSmallMobile ? (
                  <>
                    <strong>Ctrl+C:</strong> Stop • <strong>Ctrl+D:</strong> Exit<br />
                    <strong>Ctrl+L:</strong> Clear • <strong>Ctrl+A/E:</strong> Line nav
                  </>
                ) : (
                  <>
                    <strong>Ctrl+C:</strong> Interrupt • <strong>Ctrl+D:</strong> Exit • <strong>Ctrl+L:</strong> Clear • <strong>Ctrl+A/E:</strong> Line start/end • <strong>Tab:</strong> Autocomplete
                  </>
                )}
              </Typography>
            </Stack>
          </Alert>
        </Fade>
      )}
    </Box>
  );
};

export default Terminal;
