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
} from '@mui/material';
import AdminAPIClient from './api/client';
import Dashboard from './components/Dashboard';
import SetupWizard from './components/SetupWizard';
import JobsList from './components/JobsList';
import ApiKeys from './components/ApiKeys';
import Settings from './components/Settings';
import Diagnostics from './components/Diagnostics';
import SendFax from './components/SendFax';
import Inbound from './components/Inbound';

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
  const [authenticated, setAuthenticated] = useState(false);
  const [error, setError] = useState('');
  const [tabValue, setTabValue] = useState(0);

  const handleLogin = async (key: string) => {
    try {
      const testClient = new AdminAPIClient(key);
      // Test the key by fetching config
      await testClient.getConfig();
      
      // Success
      localStorage.setItem('faxbot_admin_key', key);
      setApiKey(key);
      setClient(testClient);
      setAuthenticated(true);
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
            <Box
              component="img"
              src={`${window.location.origin}/assets/faxbot_full_logo.png`}
              alt="Faxbot"
              onClick={() => setTabValue(0)}
              sx={{ 
                height: { xs: 28, sm: 32 }, 
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
                fontSize: { xs: '1rem', sm: '1.25rem' },
                display: { xs: 'none', sm: 'block' }
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
              <Tab label="Keys" />
              <Tab label="Setup" />
              <Tab label="Settings" />
              <Tab label="Diagnostics" />
            </Tabs>
          </Box>
          
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
            <Inbound client={client!} />
          </TabPanel>
          <TabPanel value={tabValue} index={4}>
            <ApiKeys client={client!} />
          </TabPanel>
          <TabPanel value={tabValue} index={5}>
            <SetupWizard client={client!} />
          </TabPanel>
          <TabPanel value={tabValue} index={6}>
            <Settings client={client!} />
          </TabPanel>
          <TabPanel value={tabValue} index={7}>
            <Diagnostics client={client!} />
          </TabPanel>
        </Container>
      </Box>
    </ThemeProvider>
  );
}

export default App;
