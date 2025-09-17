import React, { useState, useEffect } from 'react';
import {
  Box,
  CssBaseline,
  ThemeProvider,
  createTheme,
  AppBar,
  Toolbar,
  Typography,
  Container,
  Alert,
  TextField,
  Button,
  Paper,
  Tabs,
  Tab,
  IconButton,
  Drawer,
  List,
  ListItem,
  ListItemText,
  Divider,
  useMediaQuery,
} from '@mui/material';
import MenuIcon from '@mui/icons-material/Menu';
import SettingsIcon from '@mui/icons-material/Settings';
import { Tooltip } from '@mui/material';
import AdminAPIClient from './api/client';
import Dashboard from './components/Dashboard';
import SetupWizard from './components/SetupWizard';
import JobsList from './components/JobsList';
import Plugins from './components/Plugins';
import ApiKeys from './components/ApiKeys';
import Settings from './components/Settings';
import Diagnostics from './components/Diagnostics';
import MCP from './components/MCP';
import Logs from './components/Logs';
import SendFax from './components/SendFax';
import Inbound from './components/Inbound';
import Terminal from './components/Terminal';
import ScriptsTests from './components/ScriptsTests';

const darkTheme = createTheme({
  palette: {
    mode: 'dark',
    primary: { main: '#3BA0FF' },
    success: { main: '#2EBE7E' },
    error: { main: '#FF5C5C' },
    warning: { main: '#FFB020' },
    background: {
      default: '#0B0F14',
      paper: '#121821',
    },
  },
  typography: {
    fontFamily: 'system-ui, -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif',
  },
  components: {
    MuiPaper: {
      styleOverrides: {
        root: {
          border: '1px solid #1f2937',
        },
      },
    },
    MuiTextField: {
      styleOverrides: {
        root: {
          '& .MuiOutlinedInput-root': {
            backgroundColor: '#0f141c',
            '& fieldset': {
              borderColor: '#1f2937',
            },
          },
        },
      },
    },
    MuiButton: {
      styleOverrides: {
        root: {
          textTransform: 'none',
        },
      },
    },
  },
});

interface TabPanelProps {
  children?: React.ReactNode;
  index: number;
  value: number;
}

function TabPanel(props: TabPanelProps) {
  const { children, value, index, ...other } = props;

  return (
    <div
      role="tabpanel"
      hidden={value !== index}
      id={`admin-tabpanel-${index}`}
      aria-labelledby={`admin-tab-${index}`}
      {...other}
    >
      {value === index && <Box sx={{ py: 3 }}>{children}</Box>}
    </div>
  );
}

function App() {
  const [apiKey, setApiKey] = useState<string>(() => {
    // Load from localStorage (temporary storage, cleared on logout)
    return localStorage.getItem('faxbot_admin_key') || '';
  });
  const [client, setClient] = useState<AdminAPIClient | null>(null);
  const [adminConfig, setAdminConfig] = useState<any | null>(null);
  const [authenticated, setAuthenticated] = useState(false);
  const [error, setError] = useState('');
  const [tabValue, setTabValue] = useState(0);
  const [settingsTab, setSettingsTab] = useState(0); // 0: Setup, 1: Settings, 2: Keys, 3: MCP
  const [toolsTab, setToolsTab] = useState(0); // 0: Terminal, 1: Diagnostics, 2: Logs, 3: Plugins, 4: Scripts & Tests
  const [mobileOpen, setMobileOpen] = useState(false);
  const isSmall = useMediaQuery('(max-width:900px)');

  const handleLogin = async (key: string) => {
    try {
      const testClient = new AdminAPIClient(key);
      // Test the key by fetching config
      const cfg = await testClient.getConfig();
      
      // Success
      localStorage.setItem('faxbot_admin_key', key);
      setApiKey(key);
      setClient(testClient);
      setAuthenticated(true);
      setAdminConfig(cfg);
      setError('');
    } catch (e) {
      setError('Invalid API key or insufficient permissions');
      setAuthenticated(false);
    }
  };

  const handleLogout = () => {
    localStorage.removeItem('faxbot_admin_key');
    setApiKey('');
    setClient(null);
    setAuthenticated(false);
    setTabValue(0);
  };

  const handleKeyPress = (event: React.KeyboardEvent) => {
    if (event.key === 'Enter') {
      handleLogin(apiKey);
    }
  };

  // Auto-login if key exists
  useEffect(() => {
    if (apiKey && !authenticated) {
      handleLogin(apiKey);
    }
  }, [apiKey, authenticated]);

  if (!authenticated) {
    return (
      <ThemeProvider theme={darkTheme}>
        <CssBaseline />
        
        {/* Hero Section */}
        <Box
          sx={{
            background: 'linear-gradient(180deg, #0d1626 0%, #0b0f14 100%)',
            borderBottom: '1px solid #1f2937',
            py: { xs: 6, md: 8 },
            textAlign: 'center',
            display: 'flex',
            flexDirection: 'column',
            alignItems: 'center',
            justifyContent: 'center',
            minHeight: { xs: '50vh', md: '60vh' },
          }}
        >
          <Container maxWidth="lg">
            <Box
              sx={{
                width: { xs: '420px', sm: '560px', md: '720px', lg: '900px', xl: '1000px' },
                maxWidth: '95%',
                height: 'auto',
                mb: { xs: 3, md: 4 },
                mx: 'auto',
                position: 'relative',
              }}
            >
              <img
                src={`${window.location.origin}/assets/faxbot_full_logo.png`}
                alt="Faxbot"
                onError={(e) => {
                  console.error('Logo failed to load:', e);
                  (e.target as HTMLImageElement).style.display = 'none';
                  // Show fallback text
                  const fallback = document.createElement('div');
                  fallback.innerHTML = '<h1 style="color: #3BA0FF; font-size: 3rem; margin: 0;">FAXBOT</h1>';
                  (e.target as HTMLImageElement).parentNode?.appendChild(fallback);
                }}
                onLoad={() => console.log('Logo loaded successfully')}
                style={{
                  width: '100%',
                  height: 'auto',
                  filter: 'drop-shadow(0 12px 40px rgba(0,0,0,0.5))',
                  display: 'block',
                }}
              />
            </Box>
            <Typography
              variant="h3"
              component="h1"
              sx={{
                fontWeight: 600,
                letterSpacing: '0.3px',
                mb: 1,
                fontSize: { xs: '1.8rem', md: '2.5rem' },
              }}
            >
              Faxbot Admin Console
            </Typography>
            <Typography
              variant="h6"
              color="text.secondary"
              sx={{ fontSize: { xs: '1rem', md: '1.2rem' } }}
            >
              Local‑only tools for Keys • Diagnostics • Jobs • Inbound • Setup
            </Typography>
          </Container>
        </Box>

        <Container maxWidth="sm" sx={{ mt: 4 }}>
          <Paper sx={{ p: 4 }}>
            <Typography variant="h4" gutterBottom>
              Admin Login
            </Typography>
            <Typography variant="body2" color="text.secondary" gutterBottom>
              Local access only (127.0.0.1)
            </Typography>
            
            {error && (
              <Alert severity="error" sx={{ mt: 2, mb: 2 }}>
                {error}
              </Alert>
            )}
            
            <TextField
              fullWidth
              label="API Key"
              type="password"
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              onKeyPress={handleKeyPress}
              placeholder="fbk_live_... or bootstrap key"
              sx={{ mt: 2 }}
            />
            
            <Button
              fullWidth
              variant="contained"
              onClick={() => handleLogin(apiKey)}
              sx={{ mt: 2 }}
              disabled={!apiKey}
            >
              Login
            </Button>
            
            <Typography variant="caption" sx={{ mt: 2, display: 'block' }}>
              Use an API key with 'keys:manage' scope or the bootstrap API_KEY from your .env
            </Typography>
          </Paper>
        </Container>
      </ThemeProvider>
    );
  }

  return (
    <ThemeProvider theme={darkTheme}>
      <CssBaseline />
      <Box sx={{ display: 'flex', flexDirection: 'column', minHeight: '100vh' }}>
        <AppBar position="static" elevation={0} sx={{ borderBottom: '1px solid #1f2937' }}>
          <Toolbar sx={{ minHeight: { xs: 56, sm: 64 }, px: { xs: 1, sm: 2 } }}>
            <IconButton color="inherit" edge="start" onClick={() => setMobileOpen(true)} sx={{ mr: 1, display: { xs: 'inline-flex', md: 'none' } }} aria-label="open navigation">
              <MenuIcon />
            </IconButton>
            <Box
              component="img"
              src={`${window.location.origin}/assets/faxbot_full_logo.png`}
              alt="Faxbot"
              onClick={() => setTabValue(0)}
              onError={(e) => {
                console.error('Header logo failed to load:', e);
                // Try relative path as fallback
                (e.target as HTMLImageElement).src = "/assets/faxbot_full_logo.png";
              }}
              sx={{ 
                height: { xs: 26, sm: 30 }, 
                mr: { xs: 1, sm: 2 }, 
                borderRadius: 1,
                cursor: 'pointer',
                '&:hover': {
                  opacity: 0.8,
                  transform: 'scale(1.02)',
                },
                transition: 'all 0.2s ease-in-out',
              }}
            />
            <Typography 
              variant="h6" 
              sx={{ 
                flexGrow: 1,
                fontSize: { xs: '0.95rem', sm: '1.2rem' },
                display: { xs: 'none', md: 'block' }
              }}
            >
              Admin Console
            </Typography>
            <Typography 
              variant="caption" 
              sx={{ 
                mr: { xs: 1, sm: 2 }, 
                color: 'warning.main',
                display: { xs: 'none', md: 'block' },
                fontSize: { xs: '0.65rem', sm: '0.75rem' }
              }}
            >
              LOCAL ONLY
            </Typography>
            <Tooltip title="Open Settings">
              <IconButton color="inherit" onClick={() => { setTabValue(4); setSettingsTab(1); }} sx={{ mr: { xs: 0.5, md: 1 } }} aria-label="open settings">
                <SettingsIcon />
              </IconButton>
            </Tooltip>
            <Button 
              color="inherit" 
              onClick={handleLogout}
              size={window.innerWidth < 600 ? 'small' : 'medium'}
              sx={{ fontSize: { xs: '0.75rem', sm: '0.875rem' } }}
            >
              Logout
            </Button>
          </Toolbar>
        </AppBar>
        
        <Container maxWidth="xl" sx={{ flex: 1, px: { xs: 1, sm: 2, md: 3 } }}>
          {!isSmall && (
            <Box sx={{ borderBottom: 1, borderColor: 'divider', mt: { xs: 1, md: 2 } }}>
              <Tabs
                value={tabValue}
                onChange={(_, newValue) => setTabValue(newValue)}
                variant="scrollable"
                scrollButtons="auto"
                allowScrollButtonsMobile
                sx={{
                  '& .MuiTab-root': {
                    minWidth: { xs: 'auto', sm: 90 },
                    fontSize: { xs: '0.75rem', sm: '0.875rem' },
                    px: { xs: 1, sm: 2 },
                  },
                }}
              >
                <Tab label="Dashboard" />
                <Tab label="Send" />
                <Tab label="Jobs" />
                <Tab label="Inbox" />
                <Tab label="Settings" />
                <Tab label="Tools" />
              </Tabs>
            </Box>
          )}

          <Drawer anchor="left" open={mobileOpen} onClose={() => setMobileOpen(false)} sx={{ display: { xs: 'block', md: 'none' } }}>
            <Box sx={{ width: 260 }} role="presentation" onClick={() => setMobileOpen(false)} onKeyDown={() => setMobileOpen(false)}>
              <List>
                <ListItem button onClick={() => setTabValue(0)}><ListItemText primary="Dashboard" /></ListItem>
                <ListItem button onClick={() => setTabValue(1)}><ListItemText primary="Send" /></ListItem>
                <ListItem button onClick={() => setTabValue(2)}><ListItemText primary="Jobs" /></ListItem>
                <ListItem button onClick={() => setTabValue(3)}><ListItemText primary="Inbox" /></ListItem>
                <Divider />
                <ListItem><ListItemText primary="Settings" primaryTypographyProps={{ fontWeight: 600 }} /></ListItem>
                <ListItem button onClick={() => { setTabValue(4); setSettingsTab(0); }}><ListItemText primary="Setup" /></ListItem>
                <ListItem button onClick={() => { setTabValue(4); setSettingsTab(1); }}><ListItemText primary="Settings" /></ListItem>
                <ListItem button onClick={() => { setTabValue(4); setSettingsTab(2); }}><ListItemText primary="Keys" /></ListItem>
                <ListItem button onClick={() => { setTabValue(4); setSettingsTab(3); }}><ListItemText primary="MCP" /></ListItem>
                <Divider />
                <ListItem><ListItemText primary="Tools" primaryTypographyProps={{ fontWeight: 600 }} /></ListItem>
                <ListItem button onClick={() => { setTabValue(5); setToolsTab(0); }}><ListItemText primary="Terminal" /></ListItem>
                <ListItem button onClick={() => { setTabValue(5); setToolsTab(1); }}><ListItemText primary="Diagnostics" /></ListItem>
                <ListItem button onClick={() => { setTabValue(5); setToolsTab(2); }}><ListItemText primary="Logs" /></ListItem>
                {adminConfig?.v3_plugins?.enabled && (
                  <ListItem button onClick={() => { setTabValue(5); setToolsTab(3); }}><ListItemText primary="Plugins" /></ListItem>
                )}
                <ListItem button onClick={() => { setTabValue(5); setToolsTab(4); }}><ListItemText primary="Scripts & Tests" /></ListItem>
              </List>
            </Box>
          </Drawer>
          
          <TabPanel value={tabValue} index={0}>
            <Dashboard client={client!} onNavigate={setTabValue} />
          </TabPanel>
          <TabPanel value={tabValue} index={1}>
            <SendFax client={client!} />
          </TabPanel>
          <TabPanel value={tabValue} index={2}>
            <JobsList client={client!} />
          </TabPanel>
          <TabPanel value={tabValue} index={3}>
            <Inbound client={client!} docsBase={adminConfig?.branding?.docs_base} />
          </TabPanel>
          {/* Settings group */}
          <TabPanel value={tabValue} index={4}>
            <Box sx={{ borderBottom: 1, borderColor: 'divider', mb: 2, overflowX: 'auto' }}>
              <Tabs
                value={settingsTab}
                onChange={(_, v) => setSettingsTab(v)}
                variant={isSmall ? 'scrollable' : 'standard'}
                scrollButtons={isSmall ? 'auto' : undefined}
              >
                <Tab label="Setup" />
                <Tab label="Settings" />
                <Tab label="Keys" />
                <Tab label="MCP" />
              </Tabs>
            </Box>
            {settingsTab === 0 && <SetupWizard client={client!} onDone={() => setTabValue(0)} docsBase={adminConfig?.branding?.docs_base} />}
            {settingsTab === 1 && <Settings client={client!} />}
            {settingsTab === 2 && <ApiKeys client={client!} />}
            {settingsTab === 3 && <MCP client={client!} />}
          </TabPanel>
          {/* Tools group */}
          <TabPanel value={tabValue} index={5}>
            <Box sx={{ borderBottom: 1, borderColor: 'divider', mb: 2, overflowX: 'auto' }}>
              <Tabs
                value={toolsTab}
                onChange={(_, v) => setToolsTab(v)}
                variant={isSmall ? 'scrollable' : 'standard'}
                scrollButtons={isSmall ? 'auto' : undefined}
              >
                <Tab label="Terminal" />
                <Tab label="Diagnostics" />
                <Tab label="Logs" />
                {adminConfig?.v3_plugins?.enabled ? <Tab label="Plugins" /> : null}
                <Tab label="Scripts & Tests" />
              </Tabs>
            </Box>
            {toolsTab === 0 && <Terminal apiKey={apiKey} />}
            {toolsTab === 1 && <Diagnostics client={client!} onNavigate={setTabValue} docsBase={adminConfig?.branding?.docs_base} />}
            {toolsTab === 2 && <Logs client={client!} />}
            {toolsTab === 3 && adminConfig?.v3_plugins?.enabled && <Plugins client={client!} />}
            {toolsTab === 4 && <ScriptsTests client={client!} docsBase={adminConfig?.branding?.docs_base} />}
          </TabPanel>
        </Container>
      </Box>
    </ThemeProvider>
  );
}

export default App;
