import React from 'react';
import { Box, CircularProgress, LinearProgress, Fade } from '@mui/material';

interface SmoothLoaderProps {
  loading: boolean;
  variant?: 'circular' | 'linear' | 'dots';
  size?: 'small' | 'medium' | 'large';
  fullScreen?: boolean;
}

export function SmoothLoader({ 
  loading, 
  variant = 'circular', 
  size = 'medium',
  fullScreen = false 
}: SmoothLoaderProps) {
  const sizeMap = {
    small: 24,
    medium: 40,
    large: 56
  };

  const content = () => {
    if (variant === 'circular') {
      return (
        <CircularProgress 
          size={sizeMap[size]}
          thickness={4}
          sx={{
            color: theme => theme.palette.mode === 'dark' 
              ? theme.palette.primary.light 
              : theme.palette.primary.main
          }}
        />
      );
    }

    if (variant === 'linear') {
      return (
        <Box sx={{ width: '100%', maxWidth: 400 }}>
          <LinearProgress 
            sx={{
              height: size === 'small' ? 2 : size === 'medium' ? 4 : 6,
              borderRadius: 2,
              backgroundColor: theme => theme.palette.mode === 'dark'
                ? 'rgba(255, 255, 255, 0.08)'
                : 'rgba(0, 0, 0, 0.08)',
              '& .MuiLinearProgress-bar': {
                borderRadius: 2
              }
            }}
          />
        </Box>
      );
    }

    if (variant === 'dots') {
      return (
        <Box sx={{ display: 'flex', gap: 1 }}>
          {[0, 1, 2].map((i) => (
            <Box
              key={i}
              sx={{
                width: sizeMap[size] / 3,
                height: sizeMap[size] / 3,
                borderRadius: '50%',
                backgroundColor: 'primary.main',
                animation: 'pulse 1.4s ease-in-out infinite',
                animationDelay: `${i * 0.2}s`,
                '@keyframes pulse': {
                  '0%, 60%, 100%': {
                    opacity: 0.3,
                    transform: 'scale(0.8)'
                  },
                  '30%': {
                    opacity: 1,
                    transform: 'scale(1.2)'
                  }
                }
              }}
            />
          ))}
        </Box>
      );
    }

    return null;
  };

  if (fullScreen) {
    return (
      <Fade in={loading} unmountOnExit timeout={300}>
        <Box
          sx={{
            position: 'fixed',
            top: 0,
            left: 0,
            right: 0,
            bottom: 0,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            backgroundColor: theme => theme.palette.mode === 'dark'
              ? 'rgba(0, 0, 0, 0.8)'
              : 'rgba(255, 255, 255, 0.8)',
            backdropFilter: 'blur(4px)',
            zIndex: 9999
          }}
        >
          {content()}
        </Box>
      </Fade>
    );
  }

  return (
    <Fade in={loading} unmountOnExit timeout={300}>
      <Box
        sx={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          p: 4
        }}
      >
        {content()}
      </Box>
    </Fade>
  );
}

// Inline loader for buttons and small areas
export function InlineLoader({ loading, size = 16 }: { loading: boolean; size?: number }) {
  if (!loading) return null;
  
  return (
    <CircularProgress
      size={size}
      thickness={3}
      sx={{
        color: 'inherit',
        ml: 1
      }}
    />
  );
}

// Page transition wrapper
export function PageTransition({ children, in: inProp = true }: { children: React.ReactNode; in?: boolean }) {
  return (
    <Fade in={inProp} timeout={400}>
      <Box sx={{ width: '100%' }}>
        {children}
      </Box>
    </Fade>
  );
}

