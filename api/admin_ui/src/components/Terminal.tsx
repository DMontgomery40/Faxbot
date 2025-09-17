import React, { useEffect, useRef, useState, useCallback } from 'react';
import { Box, Paper, Typography, Alert, CircularProgress, IconButton, Tooltip } from '@mui/material';
import RefreshIcon from '@mui/icons-material/Refresh';
import FullscreenIcon from '@mui/icons-material/Fullscreen';
import FullscreenExitIcon from '@mui/icons-material/FullscreenExit';
import ContentCopyIcon from '@mui/icons-material/ContentCopy';
import ClearIcon from '@mui/icons-material/Clear';
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

  // Initialize terminal
  const initTerminal = useCallback(() => {
    if (!terminalRef.current) return;

    // Clean up existing terminal
    if (terminal) {
      terminal.dispose();
    }

    // Create new terminal instance
    const term = new XTerm({
      cursorBlink: true,
      cursorStyle: 'block',
      fontSize: 14,
      fontFamily: '"Cascadia Code", "JetBrains Mono", "Fira Code", Consolas, "Courier New", monospace',
      theme: {
        background: '#0B0F14',
        foreground: '#C9D1D9',
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
      scrollback: 10000,
      convertEol: true
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
  }, [terminal]);

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
        
        if (data.type === 'output' && terminal) {
          terminal.write(data.data);
        } else if (data.type === 'error') {
          setError(data.message);
        } else if (data.type === 'exit') {
          terminal?.write('\r\n\x1b[1;31mTerminal session ended.\x1b[0m\r\n');
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
  }, [websocket, apiKey, terminal]);

  // Set up terminal and WebSocket
  useEffect(() => {
    const term = initTerminal();
    const ws = connectWebSocket();

    // Handle terminal input
    if (term && ws) {
      const disposable = term.onData((data) => {
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
      const resizeDisposable = term.onResize((size) => {
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
    }
  }, []); // Empty deps, run once on mount

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

  return (
    <Box sx={{ height: '100%', display: 'flex', flexDirection: 'column' }}>
      <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', mb: 2 }}>
        <Box>
          <Typography variant="h5" gutterBottom>
            Terminal
          </Typography>
          <Typography variant="body2" color="text.secondary">
            Direct shell access to the Faxbot container
          </Typography>
        </Box>
        <Box sx={{ display: 'flex', gap: 1 }}>
          {!connected && (
            <Tooltip title="Reconnect">
              <IconButton onClick={handleReconnect} size="small" color="primary">
                <RefreshIcon />
              </IconButton>
            </Tooltip>
          )}
          <Tooltip title="Clear Terminal">
            <IconButton onClick={handleClear} size="small">
              <ClearIcon />
            </IconButton>
          </Tooltip>
          <Tooltip title="Copy All">
            <IconButton onClick={handleCopyAll} size="small">
              <ContentCopyIcon />
            </IconButton>
          </Tooltip>
          <Tooltip title={fullscreen ? "Exit Fullscreen" : "Fullscreen"}>
            <IconButton onClick={handleFullscreen} size="small">
              {fullscreen ? <FullscreenExitIcon /> : <FullscreenIcon />}
            </IconButton>
          </Tooltip>
        </Box>
      </Box>

      {error && (
        <Alert severity="error" sx={{ mb: 2 }}>
          {error}
        </Alert>
      )}

      {loading && (
        <Box sx={{ display: 'flex', justifyContent: 'center', alignItems: 'center', minHeight: 200 }}>
          <CircularProgress />
        </Box>
      )}

      <Paper 
        sx={{ 
          flex: 1, 
          p: 2, 
          bgcolor: '#0B0F14',
          position: fullscreen ? 'fixed' : 'relative',
          top: fullscreen ? 0 : 'auto',
          left: fullscreen ? 0 : 'auto',
          right: fullscreen ? 0 : 'auto',
          bottom: fullscreen ? 0 : 'auto',
          zIndex: fullscreen ? 1300 : 'auto',
          height: fullscreen ? '100vh' : '600px',
          display: 'flex',
          flexDirection: 'column'
        }}
      >
        {fullscreen && (
          <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', mb: 1, px: 1 }}>
            <Typography variant="h6" sx={{ color: '#C9D1D9' }}>
              Faxbot Terminal
            </Typography>
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
              padding: '10px',
              height: '100%'
            },
            '& .xterm-viewport': {
              backgroundColor: '#0B0F14'
            }
          }}
          tabIndex={0}
          onClick={() => { try { termRef.current?.focus(); } catch {} }}
        />
        {!connected && !loading && (
          <Box sx={{ 
            position: 'absolute', 
            top: '50%', 
            left: '50%', 
            transform: 'translate(-50%, -50%)',
            textAlign: 'center'
          }}>
            <Typography variant="h6" sx={{ color: '#6E7681', mb: 2 }}>
              Terminal Disconnected
            </Typography>
            <IconButton onClick={handleReconnect} size="large" color="primary">
              <RefreshIcon fontSize="large" />
            </IconButton>
          </Box>
        )}
      </Paper>

      {connected && (
        <Alert severity="info" icon={false} sx={{ mt: 2, py: 1 }}>
          <Typography variant="caption">
            <strong>Shortcuts:</strong> Ctrl+C: Interrupt • Ctrl+D: Exit • Ctrl+L: Clear • Ctrl+A/E: Line start/end
          </Typography>
        </Alert>
      )}
    </Box>
  );
};

export default Terminal;
