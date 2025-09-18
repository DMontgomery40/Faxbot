import { createTheme, ThemeOptions } from '@mui/material/styles';

// Shared typography and shape configuration
const sharedTypography = {
  fontFamily: '"Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif',
  h1: { fontWeight: 600, letterSpacing: '-0.02em' },
  h2: { fontWeight: 600, letterSpacing: '-0.01em' },
  h3: { fontWeight: 600, letterSpacing: '-0.01em' },
  h4: { fontWeight: 600, letterSpacing: '-0.005em' },
  h5: { fontWeight: 600 },
  h6: { fontWeight: 600 },
  button: { fontWeight: 500, letterSpacing: '0.02em' }
};

const sharedShape = {
  borderRadius: 12 // Softer edges throughout
};

// Shared component overrides for smoother feel
const getComponentOverrides = (isDark: boolean): ThemeOptions['components'] => ({
  MuiCssBaseline: {
    styleOverrides: {
      body: {
        scrollbarColor: isDark ? '#424242 #121212' : '#bdbdbd #fafafa',
        '&::-webkit-scrollbar, & *::-webkit-scrollbar': {
          width: 8,
          height: 8
        },
        '&::-webkit-scrollbar-thumb, & *::-webkit-scrollbar-thumb': {
          borderRadius: 8,
          backgroundColor: isDark ? '#424242' : '#bdbdbd',
          border: 'none'
        },
        '&::-webkit-scrollbar-track, & *::-webkit-scrollbar-track': {
          borderRadius: 8,
          backgroundColor: isDark ? '#121212' : '#fafafa'
        }
      }
    }
  },
  MuiButton: {
    styleOverrides: {
      root: {
        borderRadius: 10,
        textTransform: 'none',
        transition: 'all 0.2s cubic-bezier(0.4, 0, 0.2, 1)',
        '&:hover': {
          transform: 'translateY(-1px)',
          boxShadow: isDark 
            ? '0 4px 20px rgba(255, 255, 255, 0.1)' 
            : '0 4px 20px rgba(0, 0, 0, 0.1)'
        }
      }
    }
  },
  MuiPaper: {
    styleOverrides: {
      root: {
        borderRadius: 16,
        transition: 'all 0.3s cubic-bezier(0.4, 0, 0.2, 1)',
        backgroundImage: 'none'
      }
    }
  },
  MuiCard: {
    styleOverrides: {
      root: {
        borderRadius: 16,
        transition: 'all 0.3s cubic-bezier(0.4, 0, 0.2, 1)',
        '&:hover': {
          transform: 'translateY(-2px)',
          boxShadow: isDark
            ? '0 8px 32px rgba(255, 255, 255, 0.08)'
            : '0 8px 32px rgba(0, 0, 0, 0.08)'
        }
      }
    }
  },
  MuiTextField: {
    styleOverrides: {
      root: {
        '& .MuiOutlinedInput-root': {
          borderRadius: 10,
          transition: 'all 0.2s cubic-bezier(0.4, 0, 0.2, 1)',
          '&:hover': {
            backgroundColor: isDark 
              ? 'rgba(255, 255, 255, 0.03)' 
              : 'rgba(0, 0, 0, 0.02)'
          },
          '&.Mui-focused': {
            backgroundColor: isDark 
              ? 'rgba(255, 255, 255, 0.05)' 
              : 'rgba(0, 0, 0, 0.03)'
          }
        }
      }
    }
  },
  MuiTab: {
    styleOverrides: {
      root: {
        textTransform: 'none',
        fontWeight: 500,
        transition: 'all 0.2s cubic-bezier(0.4, 0, 0.2, 1)',
        minHeight: 48,
        '&:hover': {
          backgroundColor: isDark 
            ? 'rgba(255, 255, 255, 0.05)' 
            : 'rgba(0, 0, 0, 0.04)'
        }
      }
    }
  },
  MuiChip: {
    styleOverrides: {
      root: {
        borderRadius: 8,
        fontWeight: 500,
        transition: 'all 0.2s cubic-bezier(0.4, 0, 0.2, 1)'
      }
    }
  },
  MuiAlert: {
    styleOverrides: {
      root: {
        borderRadius: 12
      }
    }
  },
  MuiDialog: {
    styleOverrides: {
      paper: {
        borderRadius: 20
      }
    }
  },
  MuiTooltip: {
    styleOverrides: {
      tooltip: {
        borderRadius: 8,
        fontSize: '0.875rem',
        fontWeight: 400,
        backdropFilter: 'blur(10px)',
        backgroundColor: isDark 
          ? 'rgba(33, 33, 33, 0.95)' 
          : 'rgba(66, 66, 66, 0.95)'
      }
    }
  },
  MuiLinearProgress: {
    styleOverrides: {
      root: {
        borderRadius: 4,
        height: 6
      },
      bar: {
        borderRadius: 4
      }
    }
  },
  MuiTableCell: {
    styleOverrides: {
      root: {
        borderBottom: isDark 
          ? '1px solid rgba(255, 255, 255, 0.08)' 
          : '1px solid rgba(0, 0, 0, 0.08)'
      }
    }
  }
});

// Dark theme with warmer tones
export const darkTheme = createTheme({
  palette: {
    mode: 'dark',
    primary: {
      main: '#60a5fa', // Warmer blue
      light: '#93bbfc',
      dark: '#3b82f6',
      contrastText: '#ffffff'
    },
    secondary: {
      main: '#a78bfa', // Soft purple
      light: '#c4b5fd',
      dark: '#8b5cf6',
      contrastText: '#ffffff'
    },
    error: {
      main: '#f87171',
      light: '#fca5a5',
      dark: '#dc2626'
    },
    warning: {
      main: '#fbbf24',
      light: '#fcd34d',
      dark: '#f59e0b'
    },
    info: {
      main: '#60a5fa',
      light: '#93bbfc',
      dark: '#3b82f6'
    },
    success: {
      main: '#4ade80',
      light: '#86efac',
      dark: '#22c55e'
    },
    background: {
      default: '#0f0f11', // Warmer black
      paper: '#18181b'
    },
    text: {
      primary: '#fafafa',
      secondary: '#a1a1aa',
      disabled: '#52525b'
    },
    divider: 'rgba(255, 255, 255, 0.08)'
  },
  typography: sharedTypography,
  shape: sharedShape,
  components: getComponentOverrides(true)
});

// Light theme with warm, soft colors
export const lightTheme = createTheme({
  palette: {
    mode: 'light',
    primary: {
      main: '#3b82f6', // Bright blue
      light: '#60a5fa',
      dark: '#2563eb',
      contrastText: '#ffffff'
    },
    secondary: {
      main: '#8b5cf6', // Purple
      light: '#a78bfa',
      dark: '#7c3aed',
      contrastText: '#ffffff'
    },
    error: {
      main: '#dc2626',
      light: '#f87171',
      dark: '#b91c1c'
    },
    warning: {
      main: '#f59e0b',
      light: '#fbbf24',
      dark: '#d97706'
    },
    info: {
      main: '#3b82f6',
      light: '#60a5fa',
      dark: '#2563eb'
    },
    success: {
      main: '#22c55e',
      light: '#4ade80',
      dark: '#16a34a'
    },
    background: {
      default: '#fafafa',
      paper: '#ffffff'
    },
    text: {
      primary: '#18181b',
      secondary: '#52525b',
      disabled: '#a1a1aa'
    },
    divider: 'rgba(0, 0, 0, 0.08)'
  },
  typography: sharedTypography,
  shape: sharedShape,
  components: getComponentOverrides(false)
});

