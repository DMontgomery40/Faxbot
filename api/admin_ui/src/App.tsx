import React, { useState, useEffect } from 'react';
import {
  Box,
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
  ListItemIcon,
  Divider,
  useMediaQuery,
  useTheme as useMuiTheme,
  Fade,
  Slide,
  Zoom,
  Chip
} from '@mui/material';
import MenuIcon from '@mui/icons-material/Menu';
import SettingsIcon from '@mui/icons-material/Settings';
import DashboardIcon from '@mui/icons-material/Dashboard';
import SendIcon from '@mui/icons-material/Send';
import ListAltIcon from '@mui/icons-material/ListAlt';
import InboxIcon from '@mui/icons-material/Inbox';
import VpnKeyIcon from '@mui/icons-material/VpnKey';
import CodeIcon from '@mui/icons-material/Code';
import TuneIcon from '@mui/icons-material/Tune';
import TerminalIcon from '@mui/icons-material/Terminal';
import AssessmentIcon from '@mui/icons-material/Assessment';
import DescriptionIcon from '@mui/icons-material/Description';
import ExtensionIcon from '@mui/icons-material/Extension';
import ScienceIcon from '@mui/icons-material/Science';
import LogoutIcon from '@mui/icons-material/Logout';
import HelpIcon from '@mui/icons-material/Help';
import { Tooltip } from '@mui/material';
import VpnLockIcon from '@mui/icons-material/VpnLock';
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
import TunnelSettings from './components/TunnelSettings';
import ProviderSetupWizard from './components/ProviderSetupWizard';
import InboundWebhookTester from './components/InboundWebhookTester';
import OutboundSmokeTests from './components/OutboundSmokeTests';
import ConfigurationManager from './components/ConfigurationManager';
import { ThemeProvider } from './theme/ThemeContext';
import { ThemeToggle } from './components/ThemeToggle';

interface TabPanelProps {
  children?: React.ReactNode;
  index: number;
  value: number;
}

function TabPanel(props: TabPanelProps) {
  const { children, value, index, ...other } = props;

  return (
    <Fade in={value === index} timeout={300}>
      <div
        role="tabpanel"
        hidden={value !== index}
        id={`admin-tabpanel-${index}`}
        aria-labelledby={`admin-tab-${index}`}
        {...other}
      >
        {value === index && <Box sx={{ py: { xs: 2, md: 3 } }}>{children}</Box>}
      </div>
    </Fade>
  );
}

function AppContent() {
  const muiTheme = useMuiTheme();
  const isMobile = useMediaQuery(muiTheme.breakpoints.down('md'));
  const isTablet = useMediaQuery(muiTheme.breakpoints.down('lg'));
  
  const [apiKey, setApiKey] = useState<string>(() => {
    // Load from localStorage (temporary storage, cleared on logout)
    return localStorage.getItem('faxbot_admin_key') || '';
  });
  const [client, setClient] = useState<AdminAPIClient | null>(null);
  const [adminConfig, setAdminConfig] = useState<any | null>(null);
  const [uiConfig, setUiConfig] = useState<any | null>(null);
  const [userTraits, setUserTraits] = useState<string[] | null>(null);
  const [authenticated, setAuthenticated] = useState(false);
  const [error, setError] = useState('');
  const [tabValue, setTabValue] = useState(0);
  const [settingsTab, setSettingsTab] = useState(0); // 0: Setup, 1: Settings, 2: Keys, 3: MCP
  const [toolsTab, setToolsTab] = useState(0); // 0: Terminal, 1: Diagnostics, 2: Logs, 3: Plugins, 4: Scripts & Tests
  const [mobileOpen, setMobileOpen] = useState(false);
  const [providerWizardOpen, setProviderWizardOpen] = useState(false);

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
      // Fetch UI config (non-fatal)
      try {
        const uic = await testClient.getUiConfig();
        setUiConfig(uic);
      } catch { /* ignore if not available */ }
      // Fetch user traits (admin-only endpoint)
      try {
        const ut = await testClient.getUserTraits();
        setUserTraits(ut?.traits || null);
      } catch { /* ignore if not available */ }
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

  const handleTabChange = (newValue: number) => {
    setTabValue(newValue);
    if (isMobile) {
      setMobileOpen(false);
    }
  };

  // Support simple sub-tab routing by string codes
  const handleNavigate = (to: number | string) => {
    if (typeof to === 'number') {
      handleTabChange(to);
      return;
    }
    switch (to) {
      case 'tools/diagnostics':
        setTabValue(5);
        setToolsTab(1);
        break;
      case 'tools/logs':
        setTabValue(5);
        setToolsTab(2);
        break;
      case 'tools/terminal':
        setTabValue(5);
        setToolsTab(0);
        break;
      case 'tools/plugins':
        setTabValue(5);
        setToolsTab(3);
        break;
      case 'tools/scripts':
        setTabValue(5);
        setToolsTab(4);
        break;
      case 'tools/tunnels':
        setTabValue(5);
        setToolsTab(5);
        break;
      case 'settings/setup':
        setTabValue(4);
        setSettingsTab(0);
        break;
      case 'settings/settings':
        setTabValue(4);
        setSettingsTab(1);
        break;
      case 'settings/security':
        setTabValue(4);
        setSettingsTab(1);
        setTimeout(() => {
          const el = document.querySelector('#settings-security');
          (el as any)?.scrollIntoView?.({ behavior: 'smooth' });
        }, 150);
        break;
      case 'settings/phaxio':
        setTabValue(4);
        setSettingsTab(1);
        setTimeout(() => {
          const el = document.querySelector('#settings-phaxio');
          (el as any)?.scrollIntoView?.({ behavior: 'smooth' });
        }, 150);
        break;
      case 'settings/sinch':
        setTabValue(4);
        setSettingsTab(1);
        setTimeout(() => {
          const el = document.querySelector('#settings-sinch');
          (el as any)?.scrollIntoView?.({ behavior: 'smooth' });
        }, 150);
        break;
      case 'settings/sip':
        setTabValue(4);
        setSettingsTab(1);
        setTimeout(() => {
          const el = document.querySelector('#settings-sip');
          (el as any)?.scrollIntoView?.({ behavior: 'smooth' });
        }, 150);
        break;
      case 'settings/inbound':
        setTabValue(4);
        setSettingsTab(1);
        setTimeout(() => {
          const el = document.querySelector('#settings-inbound');
          (el as any)?.scrollIntoView?.({ behavior: 'smooth' });
        }, 150);
        break;
      case 'settings/storage':
        setTabValue(4);
        setSettingsTab(1);
        setTimeout(() => {
          const el = document.querySelector('#settings-storage');
          (el as any)?.scrollIntoView?.({ behavior: 'smooth' });
        }, 150);
        break;
      case 'settings/mcp':
        setTabValue(4);
        setSettingsTab(3);
        break;
      case 'jobs':
        setTabValue(2);
        break;
      case 'send':
        setTabValue(1);
        break;
      case 'inbound':
        setTabValue(3);
        break;
      case 'dashboard':
        setTabValue(0);
        break;
      default:
        setTabValue(0);
    }
    if (isMobile) setMobileOpen(false);
  };

  // Auto-login if key exists
  useEffect(() => {
    if (apiKey && !authenticated) {
      handleLogin(apiKey);
    }
  }, [apiKey, authenticated]);

  const tabIcons = [
    <DashboardIcon />,
    <SendIcon />,
    <ListAltIcon />,
    <InboxIcon />,
    <SettingsIcon />,
    <ScienceIcon />
  ];

  const drawerItems = [
    { label: 'Dashboard', icon: <DashboardIcon />, tab: 0 },
    { label: 'Send', icon: <SendIcon />, tab: 1 },
    { label: 'Jobs', icon: <ListAltIcon />, tab: 2 },
    { label: 'Inbox', icon: <InboxIcon />, tab: 3 },
  ];

  const settingsItems = [
    { label: 'Setup', icon: <HelpIcon /> },
    { label: 'Settings', icon: <SettingsIcon /> },
    { label: 'Configuration', icon: <TuneIcon /> },
    { label: 'Keys', icon: <VpnKeyIcon /> },
    { label: 'MCP', icon: <CodeIcon /> },
  ];

  // Always include Plugins tab; component will guide when feature disabled
  const toolsItems = [
    { label: 'Terminal', icon: <TerminalIcon /> },
    { label: 'Diagnostics', icon: <AssessmentIcon /> },
    { label: 'Logs', icon: <DescriptionIcon /> },
    { label: 'Plugins', icon: <ExtensionIcon /> },
    { label: 'Scripts & Tests', icon: <ScienceIcon /> },
    { label: 'Tunnels', icon: <VpnLockIcon /> },
  ];

  const hasTrait = (t: string) => !!(userTraits && userTraits.includes(t));
  const terminalDisabled = !hasTrait('ui.terminal');
  const scriptsDisabled = !hasTrait('role.admin');
  const canSend = hasTrait('ui.send') || hasTrait('role.admin');
  const isAdmin = hasTrait('role.admin');

  useEffect(() => {
    if (tabValue === 5) {
      if (toolsTab === 0 && terminalDisabled) setToolsTab(1);
      if (toolsTab === 4 && scriptsDisabled) setToolsTab(1);
    }
  }, [tabValue, toolsTab, terminalDisabled, scriptsDisabled]);

  if (!authenticated) {
    return (
      <Zoom in={true} timeout={500}>
        <Box sx={{ 
          minHeight: '100vh',
          display: 'flex',
          flexDirection: 'column',
          background: muiTheme.palette.mode === 'dark' 
            ? 'linear-gradient(180deg, #0f0f11 0%, #18181b 100%)'
            : 'linear-gradient(180deg, #ffffff 0%, #fafafa 100%)'
        }}>
          {/* Hero Section */}
          <Box
            sx={{
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
              <Fade in={true} timeout={800}>
                <Box
                  sx={{
                    width: { xs: '320px', sm: '420px', md: '560px' },
                    maxWidth: '95%',
                    height: 'auto',
                    mb: { xs: 3, md: 4 },
                    mx: 'auto',
                    position: 'relative',
                  }}
                >
                  <img
                    src={muiTheme.palette.mode === 'dark' ? '/admin/ui/faxbot_mini_banner_dark.png' : '/admin/ui/faxbot_mini_banner_light.png'}
                    alt="Faxbot"
                    onError={(e) => {
                      console.error('Logo failed to load:', e);
                      (e.target as HTMLImageElement).style.display = 'none';
                      // Show fallback text
                      const fallback = document.createElement('div');
                      fallback.innerHTML = '<h1 style="color: #3BA0FF; font-size: 3rem; margin: 0;">FAXBOT</h1>';
                      (e.target as HTMLImageElement).parentNode?.appendChild(fallback);
                    }}
                    style={{
                      width: '100%',
                      height: 'auto',
                      filter: muiTheme.palette.mode === 'dark' 
                        ? 'drop-shadow(0 12px 40px rgba(96, 165, 250, 0.2))' 
                        : 'drop-shadow(0 12px 40px rgba(59, 130, 246, 0.15))',
                      display: 'block',
                    }}
                  />
                </Box>
              </Fade>
              <Slide direction="up" in={true} timeout={600}>
                <Box>
                  <Typography
                    variant="h3"
                    component="h1"
                    sx={{
                      fontWeight: 600,
                      letterSpacing: '-0.02em',
                      mb: 1.5,
                      fontSize: { xs: '1.8rem', md: '2.5rem' },
                      background: muiTheme.palette.mode === 'dark'
                        ? 'linear-gradient(135deg, #60a5fa 0%, #a78bfa 100%)'
                        : 'linear-gradient(135deg, #3b82f6 0%, #8b5cf6 100%)',
                      backgroundClip: 'text',
                      WebkitBackgroundClip: 'text',
                      WebkitTextFillColor: 'transparent',
                    }}
                  >
                    Admin Console
                  </Typography>
                  <Typography
                    variant="h6"
                    color="text.secondary"
                    sx={{ fontSize: { xs: '1rem', md: '1.2rem' }, mb: 2 }}
                  >
                    Local‑only tools for complete fax management
                  </Typography>
                  <Box sx={{ display: 'flex', gap: 1, justifyContent: 'center', flexWrap: 'wrap' }}>
                    {['Keys', 'Diagnostics', 'Jobs', 'Inbound', 'Setup'].map((item, idx) => (
                      <Chip 
                        key={item}
                        label={item} 
                        size="small"
                        sx={{ 
                          animation: `fadeIn 0.5s ease-out ${idx * 0.1}s both`,
                          '@keyframes fadeIn': {
                            from: { opacity: 0, transform: 'translateY(10px)' },
                            to: { opacity: 1, transform: 'translateY(0)' }
                          }
                        }}
                      />
                    ))}
                  </Box>
                </Box>
              </Slide>
            </Container>
          </Box>

          <Container maxWidth="sm" sx={{ flex: 1, display: 'flex', alignItems: 'center', pb: 8 }}>
            <Fade in={true} timeout={1000}>
              <Paper 
                elevation={0}
                sx={{ 
                  p: 4, 
                  width: '100%',
                  borderRadius: 4,
                  background: muiTheme.palette.background.paper,
                  backdropFilter: 'blur(10px)',
                  border: '1px solid',
                  borderColor: muiTheme.palette.divider,
                }}
              >
                <Typography variant="h4" gutterBottom sx={{ fontWeight: 600 }}>
                  Admin Login
                </Typography>
                <Typography variant="body2" color="text.secondary" gutterBottom>
                  Local access only (127.0.0.1)
                </Typography>
                
                {error && (
                  <Slide direction="down" in={true} timeout={300}>
                    <Alert 
                      severity="error" 
                      sx={{ 
                        mt: 2, 
                        mb: 2,
                        borderRadius: 2
                      }}
                    >
                      {error}
                    </Alert>
                  </Slide>
                )}
                
                <TextField
                  fullWidth
                  label="API Key"
                  type="password"
                  value={apiKey}
                  onChange={(e) => setApiKey(e.target.value)}
                  onKeyPress={handleKeyPress}
                  placeholder="fbk_live_... or bootstrap key"
                  sx={{ mt: 3 }}
                  autoFocus
                />
                
                <Button
                  fullWidth
                  variant="contained"
                  onClick={() => handleLogin(apiKey)}
                  sx={{ 
                    mt: 3,
                    py: 1.5,
                    fontSize: '1rem',
                    fontWeight: 600,
                    borderRadius: 2
                  }}
                  disabled={!apiKey}
                >
                  Login
                </Button>
                
                <Typography variant="caption" sx={{ mt: 2, display: 'block', opacity: 0.8 }}>
                  Use an API key with 'keys:manage' scope or the bootstrap API_KEY from your .env
                </Typography>
              </Paper>
            </Fade>
          </Container>
        </Box>
      </Zoom>
    );
  }

  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', minHeight: '100vh' }}>
      <AppBar 
        position="sticky" 
        elevation={0} 
        sx={{ 
          backdropFilter: 'blur(10px)',
          background: muiTheme.palette.mode === 'dark' 
            ? 'rgba(15, 15, 17, 0.9)' 
            : 'rgba(255, 255, 255, 0.9)',
          borderBottom: '1px solid',
          borderColor: 'divider'
        }}
      >
        <Toolbar sx={{ minHeight: { xs: 56, sm: 64 }, px: { xs: 1, sm: 2 } }}>
          <IconButton 
            color="inherit" 
            edge="start" 
            onClick={() => setMobileOpen(true)} 
            sx={{ mr: 1, display: { xs: 'inline-flex', md: 'none' } }} 
            aria-label="open navigation"
          >
            <MenuIcon />
          </IconButton>
          <Box
            component="img"
            src={muiTheme.palette.mode === 'dark' ? '/admin/ui/faxbot_mini_banner_dark.png' : '/admin/ui/faxbot_mini_banner_light.png'}
            alt="Faxbot"
            onClick={() => setTabValue(0)}
            onError={(e) => {
              console.error('Header logo failed to load:', e);
              // Try relative path as fallback
              (e.target as HTMLImageElement).src = muiTheme.palette.mode === 'dark' ? '/assets/faxbot_mini_banner_dark.png' : '/assets/faxbot_mini_banner_light.png';
            }}
            sx={{ 
              height: { xs: 30, sm: 36 }, 
              mr: { xs: 1, sm: 2 }, 
              borderRadius: 1,
              cursor: 'pointer',
              transition: 'all 0.3s cubic-bezier(0.4, 0, 0.2, 1)',
              '&:hover': {
                opacity: 0.8,
                transform: 'scale(1.05)',
              }
            }}
          />
          <Typography 
            variant="h6" 
            sx={{ 
              flexGrow: 1,
              fontSize: { xs: '0.95rem', sm: '1.2rem' },
              fontWeight: 600,
              display: { xs: 'none', md: 'block' }
            }}
          >
            Admin Console
          </Typography>
          <Chip
            label="LOCAL ONLY"
            size="small"
            color="warning"
            sx={{ 
              mr: 2,
              display: { xs: 'none', sm: 'flex' },
              fontWeight: 600,
              letterSpacing: '0.02em'
            }}
          />
          {uiConfig?.features?.sessions_enabled && (
            <Chip label="Sessions" size="small" color="success" sx={{ mr: 1, display: { xs: 'none', sm: 'flex' } }} />
          )}
          {uiConfig?.features?.csrf_enabled && (
            <Chip label="CSRF" size="small" color="info" sx={{ mr: 1, display: { xs: 'none', sm: 'flex' } }} />
          )}
          <ThemeToggle />
          <Tooltip title="Open Settings">
            <IconButton 
              color="inherit" 
              onClick={() => { setTabValue(4); setSettingsTab(1); }} 
              sx={{ mx: 1 }} 
              aria-label="open settings"
            >
              <SettingsIcon />
            </IconButton>
          </Tooltip>
          <Button 
            color="inherit" 
            onClick={handleLogout}
            startIcon={<LogoutIcon />}
            size={isMobile ? 'small' : 'medium'}
            sx={{ 
              fontSize: { xs: '0.75rem', sm: '0.875rem' },
              borderRadius: 2,
              px: { xs: 1.5, sm: 2 }
            }}
          >
            Logout
          </Button>
        </Toolbar>
      </AppBar>
      
      <Container maxWidth="xl" sx={{ flex: 1, px: { xs: 1, sm: 2, md: 3 } }}>
        {!isMobile && (
          <Box sx={{ borderBottom: 1, borderColor: 'divider', mt: { xs: 1, md: 2 } }}>
            <Tabs
              value={tabValue}
              onChange={(_, newValue) => handleTabChange(newValue)}
              variant={isTablet ? "scrollable" : "standard"}
              scrollButtons={isTablet ? "auto" : false}
              allowScrollButtonsMobile
              sx={{
                '& .MuiTab-root': {
                  minWidth: { xs: 'auto', sm: 90 },
                  fontSize: { xs: '0.75rem', sm: '0.875rem' },
                  px: { xs: 1, sm: 2 },
                  transition: 'all 0.2s',
                  borderRadius: '8px 8px 0 0',
                  '&:hover': {
                    backgroundColor: muiTheme.palette.action.hover,
                  }
                },
                '& .MuiTabs-indicator': {
                  height: 3,
                  borderRadius: '3px 3px 0 0'
                }
              }}
            >
              <Tab icon={tabIcons[0]} iconPosition="start" label="Dashboard" />
              <Tab icon={tabIcons[1]} iconPosition="start" label="Send" />
              <Tab icon={tabIcons[2]} iconPosition="start" label="Jobs" />
              <Tab icon={tabIcons[3]} iconPosition="start" label="Inbox" />
              <Tab icon={tabIcons[4]} iconPosition="start" label="Settings" />
              <Tab icon={tabIcons[5]} iconPosition="start" label="Tools" />
            </Tabs>
          </Box>
        )}

        <Drawer 
          anchor="left" 
          open={mobileOpen} 
          onClose={() => setMobileOpen(false)} 
          sx={{ 
            display: { xs: 'block', md: 'none' },
            '& .MuiDrawer-paper': {
              width: 280,
              borderRadius: '0 16px 16px 0'
            }
          }}
        >
          <Box 
            sx={{ width: 280, pt: 2 }} 
            role="presentation" 
            onClick={() => setMobileOpen(false)} 
            onKeyDown={() => setMobileOpen(false)}
          >
            <Box sx={{ px: 2, pb: 2 }}>
              <Typography variant="h6" fontWeight={600}>Navigation</Typography>
            </Box>
            <List>
              {drawerItems.map((item) => (
                <ListItem 
                  button 
                  key={item.label}
                  onClick={() => handleTabChange(item.tab)}
                  selected={tabValue === item.tab}
                  sx={{
                    borderRadius: '0 24px 24px 0',
                    mx: 1,
                    mb: 0.5,
                    '&.Mui-selected': {
                      backgroundColor: muiTheme.palette.action.selected,
                    }
                  }}
                >
                  <ListItemIcon>{item.icon}</ListItemIcon>
                  <ListItemText primary={item.label} />
                </ListItem>
              ))}
              <Divider sx={{ my: 2 }} />
              <ListItem sx={{ px: 3 }}>
                <ListItemText primary="Settings" primaryTypographyProps={{ fontWeight: 600 }} />
              </ListItem>
              {settingsItems.map((item, idx) => (
                <ListItem 
                  button 
                  key={item.label}
                  onClick={() => { handleTabChange(4); setSettingsTab(idx); }}
                  sx={{
                    borderRadius: '0 24px 24px 0',
                    mx: 1,
                    mb: 0.5,
                    pl: 4
                  }}
                >
                  <ListItemIcon>{item.icon}</ListItemIcon>
                  <ListItemText primary={item.label} />
                </ListItem>
              ))}
              <Divider sx={{ my: 2 }} />
              <ListItem sx={{ px: 3 }}>
                <ListItemText primary="Tools" primaryTypographyProps={{ fontWeight: 600 }} />
              </ListItem>
              {toolsItems.map((item, idx) => (
                <ListItem 
                  button 
                  key={item.label}
                  onClick={() => { handleTabChange(5); setToolsTab(idx); }}
                  sx={{
                    borderRadius: '0 24px 24px 0',
                    mx: 1,
                    mb: 0.5,
                    pl: 4
                  }}
                >
                  <ListItemIcon>{item.icon}</ListItemIcon>
                  <ListItemText primary={item.label} />
                </ListItem>
              ))}
            </List>
          </Box>
        </Drawer>
        
        <TabPanel value={tabValue} index={0}>
          <Dashboard client={client!} onNavigate={handleNavigate} />
        </TabPanel>
        <TabPanel value={tabValue} index={1}>
          <SendFax client={client!} />
        </TabPanel>
        <TabPanel value={tabValue} index={2}>
          <JobsList client={client!} />
        </TabPanel>
        <TabPanel value={tabValue} index={3}>
          <Inbound client={client!} docsBase={uiConfig?.docs_base || adminConfig?.branding?.docs_base} />
        </TabPanel>
        {/* Settings group */}
        <TabPanel value={tabValue} index={4}>
          <Paper 
            elevation={0}
            sx={{ 
              borderRadius: 3,
              overflow: 'hidden',
              border: '1px solid',
              borderColor: 'divider'
            }}
          >
            <Box sx={{ borderBottom: 1, borderColor: 'divider', backgroundColor: muiTheme.palette.action.hover }}>
              <Tabs
                value={settingsTab}
                onChange={(_, v) => setSettingsTab(v)}
                variant={isMobile ? 'scrollable' : 'standard'}
                scrollButtons={isMobile ? 'auto' : false}
                sx={{ px: 2 }}
              >
                {settingsItems.map((item) => (
                  <Tab key={item.label} icon={item.icon} iconPosition="start" label={item.label} />
                ))}
              </Tabs>
            </Box>
            <Box sx={{ p: { xs: 2, md: 3 } }}>
              {settingsTab === 0 && <SetupWizard client={client!} onDone={() => handleTabChange(0)} docsBase={uiConfig?.docs_base || adminConfig?.branding?.docs_base} />}
              {settingsTab === 1 && (
                <Box>
                  <Box sx={{ mb: 3, display: 'flex', justifyContent: 'flex-end' }}>
                    <Button
                      variant="outlined"
                      startIcon={<SettingsIcon />}
                      onClick={() => setProviderWizardOpen(true)}
                      sx={{ borderRadius: 2 }}
                      disabled={!hasTrait('role.admin')}
                    >
                      Provider Setup Wizard
                    </Button>
                  </Box>
                  <Settings client={client!} readOnly={!hasTrait('role.admin')} />
                </Box>
              )}
              {settingsTab === 2 && <ConfigurationManager client={client!} docsBase={uiConfig?.docs_base || adminConfig?.branding?.docs_base} />}
              {settingsTab === 3 && <ApiKeys client={client!} readOnly={!hasTrait('role.admin')} />}
              {settingsTab === 4 && <MCP client={client!} />}
            </Box>
          </Paper>
        </TabPanel>
        {/* Tools group */}
        <TabPanel value={tabValue} index={5}>
          <Paper 
            elevation={0}
            sx={{ 
              borderRadius: 3,
              overflow: 'hidden',
              border: '1px solid',
              borderColor: 'divider'
            }}
          >
            <Box sx={{ borderBottom: 1, borderColor: 'divider', backgroundColor: muiTheme.palette.action.hover }}>
              <Tabs
                value={toolsTab}
                onChange={(_, v) => setToolsTab(v)}
                variant={isMobile ? 'scrollable' : 'standard'}
                scrollButtons={isMobile ? 'auto' : false}
                sx={{ px: 2 }}
              >
                {toolsItems.map((item, idx) => (
                  <Tab 
                    key={item.label} 
                    icon={item.icon} 
                    iconPosition="start" 
                    label={item.label} 
                    disabled={(idx === 0 && terminalDisabled) || (idx === 4 && scriptsDisabled)}
                  />
                ))}
              </Tabs>
            </Box>
            <Box sx={{ p: { xs: 2, md: 3 } }}>
              {toolsTab === 0 && <Terminal apiKey={apiKey} />}
              {toolsTab === 1 && (
                <Box>
                  <Diagnostics client={client!} onNavigate={handleNavigate} docsBase={uiConfig?.docs_base || adminConfig?.branding?.docs_base} />
                  <Box sx={{ mt: 4 }}>
                    <OutboundSmokeTests client={client!} canSend={canSend} />
                  </Box>
                  <Box sx={{ mt: 4 }}>
                    <InboundWebhookTester client={client!} />
                  </Box>
                </Box>
              )}
              {toolsTab === 2 && <Logs client={client!} />}
              {toolsTab === 3 && <Plugins client={client!} readOnly={!hasTrait('role.admin')} />}
              {toolsTab === 4 && <ScriptsTests client={client!} docsBase={uiConfig?.docs_base || adminConfig?.branding?.docs_base} canSend={canSend} readOnly={!isAdmin} />}
              {toolsTab === 5 && (
                <TunnelSettings
                  client={client!}
                  docsBase={uiConfig?.docs_base || adminConfig?.branding?.docs_base}
                  hipaaMode={Boolean(adminConfig?.security?.enforce_https)}
                  inboundBackend={adminConfig?.hybrid?.inbound_backend}
                  sinchConfigured={Boolean(adminConfig?.backend_configured?.sinch)}
                  readOnly={!isAdmin}
                />
              )}
            </Box>
          </Paper>
        </TabPanel>
      </Container>

      {/* Provider Setup Wizard */}
      <ProviderSetupWizard
        client={client!}
        open={providerWizardOpen}
        onClose={() => setProviderWizardOpen(false)}
        onComplete={() => {
          setProviderWizardOpen(false);
          // Refresh the page or reload config
          window.location.reload();
        }}
      />
    </Box>
  );
}

function App() {
  return (
    <ThemeProvider>
      <AppContent />
    </ThemeProvider>
  );
}

export default App;
