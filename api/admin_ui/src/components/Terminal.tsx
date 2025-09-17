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

interface TerminalProps {
  apiKey: string;
}

const Terminal: React.FC<TerminalProps> = ({ apiKey }) => {
  const terminalRef = useRef<HTMLDivElement>(null);
  const termRef = useRef<XTerm | null>(null);
  const [terminal, setTerminal] = useState<XTerm | null>(null);
  const [websocket, setWebsocket] = useState<WebSocket | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const [connected, setConnected] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [fullscreen, setFullscreen] = useState(false);
  const fitAddonRef = useRef<FitAddon | null>(null);
  const pingIntervalRef = useRef<number | null>(null);

  const theme = useTheme();
  const isMobile = useMediaQuery(theme.breakpoints.down('md'));
  const isSmallMobile = useMediaQuery(theme.breakpoints.down('sm'));

  // Adjust terminal font size based on screen size
  const getFontSize = () => {
    if (isSmallMobile) return 12;
    if (isMobile) return 13;
    return 14;
  };

  // Initialize terminal
  const initTerminal = useCallback(() => {
    if (!terminalRef.current) return;

    // Clean up existing terminal
    if (termRef.current) {
      termRef.current.dispose();
    }

    // Create new terminal instance with responsive settings
    const term = new XTerm({
      cursorBlink: true,
      cursorStyle: 'block',
      fontSize: getFontSize(),
      fontFamily: '"Cascadia Code", "JetBrains Mono", "Fira Code", Consolas, "Courier New", monospace',
      theme: {
        background: theme.palette.mode === 'dark' ? '#0B0F14' : '#1e1e1e',
        foreground: theme.palette.mode === 'dark' ? '#C9D1D9' : '#d4d4d4',
        cursor: '#58A6FF',
        black: '#0D1117',
        red: '#FF7B72',
        green: '#7EE83F',
        yellow: '#FFA657',
        blue: '#79C0FF',
        magenta: '#D2A8FF',
        cyan: '#A5D6FF',
        white: '#C9D1D9',
        brightBlack: '#6E7681',
        brightRed: '#FFA198',
        brightGreen: '#56D364',
        brightYellow: '#FFB454',
        brightBlue: '#79C0FF',
        brightMagenta: '#D2A8FF',
        brightCyan: '#56D4DD',
        brightWhite: '#FFFFFF',
        selectionBackground: '#3392FF44',
      },
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
    
    try {
      // Focus on click just in case
      terminalRef.current?.addEventListener('click', () => {
        try { term.focus(); } catch {}
      });
    } catch {}
    
    // Initial fit
    setTimeout(() => {
      fitAddon.fit();
    }, 0);

    termRef.current = term;
    setTerminal(term);
    return term;
  }, [theme.palette.mode, isSmallMobile, isMobile]);

  // Connect to WebSocket
  const connectWebSocket = useCallback(() => {
    if (websocket?.readyState === WebSocket.OPEN) return;

    setLoading(true);
    setError(null);

    // Build WebSocket URL with API key in query params
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    
    // Determine the API host - in development, the API runs on 8080
    // In production, it's the same host as the UI
    let apiHost = window.location.host;
    
    // Check if we're in development mode (common dev ports)
    const devPorts = ['3000', '3001', '5173', '5174', '4200'];
    const currentPort = window.location.port;
    if (devPorts.includes(currentPort)) {
      // In development, API runs on localhost:8080
      apiHost = `localhost:8080`;
    }
    
    const wsUrl = `${protocol}//${apiHost}/admin/terminal?api_key=${encodeURIComponent(apiKey)}`;
    
    console.log('Terminal WebSocket connecting to:', wsUrl);
    const ws = new WebSocket(wsUrl);

    ws.onopen = () => {
      console.log('Terminal WebSocket connected');
      setConnected(true);
      setLoading(false);
      setError(null);
      wsRef.current = ws;
      // Nudge the shell to print a prompt
      try { ws.send(JSON.stringify({ type: 'input', data: '\r' })); } catch {}
      // Ensure xterm has focus once the socket is open
      setTimeout(() => { try { termRef.current?.focus(); } catch {} }, 0);

      // Start ping interval
      pingIntervalRef.current = window.setInterval(() => {
        if (ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ type: 'ping' }));
        }
      }, 30000);
    };

    ws.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        
        if (data.type === 'output' && termRef.current) {
          termRef.current.write(data.data);
        } else if (data.type === 'error') {
          setError(data.message);
        } else if (data.type === 'exit') {
          termRef.current?.write('\r\n\x1b[1;31mTerminal session ended.\x1b[0m\r\n');
          setConnected(false);
        }
      } catch (e) {
        console.error('Error parsing WebSocket message:', e);
      }
    };

    ws.onerror = (event) => {
      console.error('Terminal WebSocket error:', event);
      setError('WebSocket connection error');
      setLoading(false);
    };

    ws.onclose = (ev) => {
      console.log('Terminal WebSocket disconnected');
      setConnected(false);
      setLoading(false);
      wsRef.current = null;
      // Provide a more helpful message on auth failure
      if (ev?.code === 1008) {
        setError(ev.reason || 'Unauthorized (admin scope required)');
      } else if (ev?.reason) {
        setError(ev.reason);
      }
      
      // Clear ping interval
      if (pingIntervalRef.current) {
        window.clearInterval(pingIntervalRef.current);
        pingIntervalRef.current = null;
      }
    };

    setWebsocket(ws);
    return ws;
  }, [websocket, apiKey]);

  // Initialize terminal on mount
  useEffect(() => {
    const term = initTerminal();
    if (term) {
      // Terminal input and resize handlers will be set up separately
    }
  }, [theme.palette.mode, isSmallMobile, isMobile]); // Only re-init on theme/size changes

  // Set up WebSocket and terminal handlers
  useEffect(() => {
    if (!terminal) return;

    const ws = connectWebSocket();
    if (!ws) return;

    // Handle terminal input
    const disposable = terminal.onData((data) => {
      try { console.debug('[terminal] onData', JSON.stringify(data)); } catch {}
      const current = wsRef.current;
      if (current && current.readyState === WebSocket.OPEN) {
        current.send(JSON.stringify({
          type: 'input',
          data: data
        }));
      }
    });

    // Handle terminal resize
    const resizeDisposable = terminal.onResize((size) => {
      const current = wsRef.current;
      if (current && current.readyState === WebSocket.OPEN) {
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
    };
  }, [terminal, apiKey]); // Connect when terminal is ready and apiKey changes

  // Handle window resize
  useEffect(() => {
    const handleResize = () => {
      if (fitAddonRef.current && terminal) {
        fitAddonRef.current.fit();
      }
    };

    window.addEventListener('resize', handleResize);
    return () => window.removeEventListener('resize', handleResize);
  }, [terminal]);

  // Cleanup on unmount
  useEffect(() => {
    return () => {
      if (pingIntervalRef.current) {
        window.clearInterval(pingIntervalRef.current);
      }
      if (websocket) {
        websocket.close();
      }
      if (terminal) {
        terminal.dispose();
      }
    };
  }, []);

  // Reconnect function
  const handleReconnect = () => {
    if (websocket) {
      websocket.close();
    }
    if (terminal) {
      terminal.clear();
    }
    connectWebSocket();
  };

  // Clear terminal
  const handleClear = () => {
    if (terminal) {
      terminal.clear();
    }
  };

  // Copy all terminal content
  const handleCopyAll = () => {
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
            Direct shell access to the Faxbot container
          </Typography>
        </Box>
        
        <Box>
          {isSmallMobile ? (
            <Stack direction="row" spacing={1}>
              {!connected && (
                <IconButton onClick={handleReconnect} color="primary" sx={{ borderRadius: 2 }}>
                  <RefreshIcon />
                </IconButton>
              )}
              <IconButton onClick={handleClear} sx={{ borderRadius: 2 }}>
                <ClearIcon />
              </IconButton>
              <IconButton onClick={handleCopyAll} sx={{ borderRadius: 2 }}>
                <ContentCopyIcon />
              </IconButton>
              <IconButton onClick={handleFullscreen} sx={{ borderRadius: 2 }}>
                {fullscreen ? <FullscreenExitIcon /> : <FullscreenIcon />}
              </IconButton>
            </Stack>
          ) : (
            <ButtonGroup variant="outlined" size={isMobile ? "small" : "medium"}>
              {!connected && (
                <Tooltip title="Reconnect">
                  <Button onClick={handleReconnect} startIcon={<RefreshIcon />}>
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

      {loading && (
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
              Connecting to terminal...
            </Typography>
          </Stack>
        </Paper>
      )}

      {!loading && (
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
          
          {!connected && !loading && (
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
                <DisconnectedIcon sx={{ fontSize: 48, color: 'text.secondary', mb: 2 }} />
                <Typography variant="h6" sx={{ color: 'text.primary', mb: 2 }}>
                  Terminal Disconnected
                </Typography>
                <Button
                  variant="contained"
                  onClick={handleReconnect}
                  startIcon={<RefreshIcon />}
                  size="large"
                  sx={{ borderRadius: 2 }}
                >
                  Reconnect
                </Button>
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