import React, { useEffect, useState } from 'react';
import {
  Alert,
  Box,
  Button,
  Chip,
  CircularProgress,
  Container,
  Fade,
  Link,
  Paper,
  Slide,
  TextField,
  Typography,
  Zoom,
  useTheme,
} from '@mui/material';
import AdminAPIClient from '../api/client';

interface LoginScreenProps {
  notice?: string;
  onPasswordSignIn: (login: string, password: string) => Promise<void>;
  onKeySignIn: (apiKey: string) => Promise<void>;
}

export default function LoginScreen({ notice, onPasswordSignIn, onKeySignIn }: LoginScreenProps) {
  const theme = useTheme();
  const [mode, setMode] = useState<'password' | 'key'>('password');
  const [login, setLogin] = useState('');
  const [password, setPassword] = useState('');
  const [apiKey, setApiKey] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // A new installation has no owner yet: the first sign-in uses the installation key.
  const [firstOwner, setFirstOwner] = useState(false);

  useEffect(() => {
    let live = true;
    void AdminAPIClient.needsFirstOwner().then((needed) => {
      if (!live || !needed) return;
      setFirstOwner(true);
      setMode('key');
    });
    return () => { live = false; };
  }, []);

  const switchMode = (next: 'password' | 'key') => {
    setMode(next);
    setPassword('');
    setApiKey('');
    setError(null);
  };

  const submit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      if (mode === 'password') await onPasswordSignIn(login.trim(), password);
      else await onKeySignIn(apiKey.trim());
    } catch (failure) {
      setError(failure instanceof Error && failure.message ? failure.message : 'Sign-in failed. Try again.');
      setBusy(false);
    }
  };

  const message = error ?? notice ?? null;
  const ready = mode === 'password' ? Boolean(login.trim() && password) : Boolean(apiKey.trim());

  return (
    <Zoom in timeout={500}>
      <Box sx={{
        minHeight: '100vh',
        display: 'flex',
        flexDirection: 'column',
        background: theme.palette.mode === 'dark'
          ? 'linear-gradient(180deg, #0f0f11 0%, #18181b 100%)'
          : 'linear-gradient(180deg, #ffffff 0%, #fafafa 100%)',
      }}>
        <Box sx={{
          py: { xs: 6, md: 8 },
          textAlign: 'center',
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'center',
          justifyContent: 'center',
          minHeight: { xs: '40vh', md: '50vh' },
        }}>
          <Container maxWidth="lg">
            <Fade in timeout={800}>
              <Box sx={{ width: { xs: '320px', sm: '420px', md: '560px' }, maxWidth: '95%', mb: { xs: 3, md: 4 }, mx: 'auto' }}>
                <img
                  src={theme.palette.mode === 'dark' ? '/admin/ui/faxbot_mini_banner_dark.png' : '/admin/ui/faxbot_mini_banner_light.png'}
                  alt="Faxbot"
                  onError={(e) => { (e.target as HTMLImageElement).style.display = 'none'; }}
                  style={{
                    width: '100%',
                    height: 'auto',
                    display: 'block',
                    filter: theme.palette.mode === 'dark'
                      ? 'drop-shadow(0 12px 40px rgba(96, 165, 250, 0.2))'
                      : 'drop-shadow(0 12px 40px rgba(59, 130, 246, 0.15))',
                  }}
                />
              </Box>
            </Fade>
            <Slide direction="up" in timeout={600}>
              <Box>
                <Typography
                  variant="h3"
                  component="h1"
                  sx={{
                    fontWeight: 600,
                    letterSpacing: '-0.02em',
                    mb: 1.5,
                    fontSize: { xs: '1.8rem', md: '2.5rem' },
                    background: theme.palette.mode === 'dark'
                      ? 'linear-gradient(135deg, #60a5fa 0%, #a78bfa 100%)'
                      : 'linear-gradient(135deg, #3b82f6 0%, #8b5cf6 100%)',
                    backgroundClip: 'text',
                    WebkitBackgroundClip: 'text',
                    WebkitTextFillColor: 'transparent',
                  }}
                >
                  Admin Console
                </Typography>
                <Typography variant="h6" color="text.secondary" sx={{ fontSize: { xs: '1rem', md: '1.2rem' }, mb: 2 }}>
                  Send, receive and manage faxes for this installation
                </Typography>
                <Box sx={{ display: 'flex', gap: 1, justifyContent: 'center', flexWrap: 'wrap' }}>
                  {['Send', 'Jobs', 'Inbox', 'Users', 'Keys'].map((item) => <Chip key={item} label={item} size="small" />)}
                </Box>
              </Box>
            </Slide>
          </Container>
        </Box>

        <Container maxWidth="sm" sx={{ flex: 1, display: 'flex', alignItems: 'flex-start', pb: 8 }}>
          <Fade in timeout={1000}>
            <Paper
              component="form"
              onSubmit={submit}
              noValidate
              elevation={0}
              sx={{ p: 4, width: '100%', borderRadius: 4, border: '1px solid', borderColor: 'divider' }}
            >
              <Typography variant="h4" component="h2" gutterBottom sx={{ fontWeight: 600 }}>
                Sign in
              </Typography>
              <Typography variant="body2" color="text.secondary">
                {mode === 'password' ? 'Use your Faxbot username and password.'
                  : firstOwner ? 'Use the installation key.' : 'Use an API key issued for this installation.'}
              </Typography>

              {firstOwner && mode === 'key' && (
                <Alert severity="info" sx={{ mt: 2, borderRadius: 2 }} data-testid="first-owner">
                  This installation has no owner yet: sign in with the installation key (API_KEY in .env) to create the first owner.
                </Alert>
              )}

              {message && (
                <Alert severity={error ? 'error' : 'info'} sx={{ mt: 2, borderRadius: 2 }} role="alert">
                  {message}
                </Alert>
              )}

              {mode === 'password' ? (
                <>
                  <TextField
                    fullWidth
                    label="Username"
                    autoComplete="username"
                    value={login}
                    onChange={(e) => setLogin(e.target.value)}
                    sx={{ mt: 3 }}
                    autoFocus
                    inputProps={{ maxLength: 128 }}
                  />
                  <TextField
                    fullWidth
                    label="Password"
                    type="password"
                    autoComplete="current-password"
                    value={password}
                    onChange={(e) => setPassword(e.target.value)}
                    sx={{ mt: 2 }}
                  />
                </>
              ) : (
                <TextField
                  fullWidth
                  label={firstOwner ? 'Installation key' : 'API key'}
                  type="password"
                  autoComplete="off"
                  value={apiKey}
                  onChange={(e) => setApiKey(e.target.value)}
                  sx={{ mt: 3 }}
                  autoFocus
                />
              )}

              <Button
                fullWidth
                variant="contained"
                type="submit"
                disabled={!ready || busy}
                startIcon={busy ? <CircularProgress size={18} color="inherit" /> : undefined}
                sx={{ mt: 3, py: 1.5, fontSize: '1rem', fontWeight: 600, borderRadius: 2 }}
              >
                Sign in
              </Button>

              <Box sx={{ mt: 2, textAlign: 'center' }}>
                <Link
                  component="button"
                  type="button"
                  variant="body2"
                  onClick={() => switchMode(mode === 'password' ? 'key' : 'password')}
                >
                  {mode === 'password' ? 'Sign in with API key' : 'Sign in with username and password'}
                </Link>
              </Box>
            </Paper>
          </Fade>
        </Container>
      </Box>
    </Zoom>
  );
}
