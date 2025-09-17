import React, { createContext, useContext, useEffect, useState, useMemo } from 'react';
import { ThemeProvider as MuiThemeProvider } from '@mui/material/styles';
import CssBaseline from '@mui/material/CssBaseline';
import { darkTheme, lightTheme } from './themes';

type ThemeMode = 'dark' | 'light' | 'system';

interface ThemeContextType {
  mode: ThemeMode;
  setMode: (mode: ThemeMode) => void;
  resolvedMode: 'dark' | 'light';
}

const ThemeContext = createContext<ThemeContextType>({
  mode: 'dark',
  setMode: () => {},
  resolvedMode: 'dark'
});

export const useTheme = () => useContext(ThemeContext);

interface ThemeProviderProps {
  children: React.ReactNode;
}

export function ThemeProvider({ children }: ThemeProviderProps) {
  const [mode, setMode] = useState<ThemeMode>(() => {
    const saved = localStorage.getItem('theme-mode');
    return (saved as ThemeMode) || 'dark';
  });

  const [systemPreference, setSystemPreference] = useState<'dark' | 'light'>(() => {
    try {
      return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
    } catch {
      return 'dark'; // fallback for Electron or other environments
    }
  });

  useEffect(() => {
    try {
      const mediaQuery = window.matchMedia('(prefers-color-scheme: dark)');
      const handleChange = (e: MediaQueryListEvent) => {
        // Debounce rapid changes to prevent flickering
        setTimeout(() => {
          setSystemPreference(e.matches ? 'dark' : 'light');
        }, 100);
      };
      
      mediaQuery.addEventListener('change', handleChange);
      return () => mediaQuery.removeEventListener('change', handleChange);
    } catch {
      // Fallback for environments that don't support matchMedia
      console.warn('matchMedia not supported, using dark theme');
    }
  }, []);

  useEffect(() => {
    localStorage.setItem('theme-mode', mode);
  }, [mode]);

  const resolvedMode = useMemo(() => {
    if (mode === 'system') {
      return systemPreference;
    }
    return mode as 'dark' | 'light';
  }, [mode, systemPreference]);

  const theme = useMemo(() => {
    return resolvedMode === 'dark' ? darkTheme : lightTheme;
  }, [resolvedMode]);

  // Add smooth transition class to body
  useEffect(() => {
    document.body.style.transition = 'background-color 0.3s ease, color 0.3s ease';
    return () => {
      document.body.style.transition = '';
    };
  }, []);

  const value = useMemo(() => ({
    mode,
    setMode,
    resolvedMode
  }), [mode, resolvedMode]);

  return (
    <ThemeContext.Provider value={value}>
      <MuiThemeProvider theme={theme}>
        <CssBaseline />
        {children}
      </MuiThemeProvider>
    </ThemeContext.Provider>
  );
}
