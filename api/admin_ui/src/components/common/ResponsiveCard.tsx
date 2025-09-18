import React from 'react';
import { Card, CardProps, Box, useTheme, useMediaQuery } from '@mui/material';

interface ResponsiveCardProps extends CardProps {
  children: React.ReactNode;
  animate?: boolean;
  hoverEffect?: boolean;
}

export function ResponsiveCard({ 
  children, 
  animate = true, 
  hoverEffect = true,
  sx,
  ...props 
}: ResponsiveCardProps) {
  const theme = useTheme();
  const isMobile = useMediaQuery(theme.breakpoints.down('sm'));

  return (
    <Card
      elevation={0}
      sx={{
        borderRadius: { xs: 2, md: 3 },
        border: '1px solid',
        borderColor: 'divider',
        p: { xs: 2, sm: 3, md: 4 },
        transition: animate ? 'all 0.3s cubic-bezier(0.4, 0, 0.2, 1)' : undefined,
        ...(hoverEffect && !isMobile ? {
          '&:hover': {
            transform: 'translateY(-2px)',
            boxShadow: theme.palette.mode === 'dark'
              ? '0 8px 32px rgba(255, 255, 255, 0.08)'
              : '0 8px 32px rgba(0, 0, 0, 0.08)',
          }
        } : {}),
        ...sx
      }}
      {...props}
    >
      {children}
    </Card>
  );
}

// Responsive Grid Container
export function ResponsiveGrid({ children, spacing = 3 }: { children: React.ReactNode; spacing?: number }) {
  return (
    <Box
      sx={{
        display: 'grid',
        gridTemplateColumns: {
          xs: '1fr',
          sm: 'repeat(2, 1fr)',
          md: 'repeat(3, 1fr)',
          lg: 'repeat(4, 1fr)'
        },
        gap: spacing,
        width: '100%'
      }}
    >
      {children}
    </Box>
  );
}

// Stats Card Component
export function StatCard({ 
  title, 
  value, 
  icon, 
  color = 'primary',
  onClick
}: { 
  title: string; 
  value: string | number; 
  icon?: React.ReactNode;
  color?: 'primary' | 'secondary' | 'success' | 'error' | 'warning' | 'info';
  onClick?: () => void;
}) {
  const theme = useTheme();
  
  return (
    <ResponsiveCard
      hoverEffect={!!onClick}
      onClick={onClick}
      sx={{
        cursor: onClick ? 'pointer' : 'default',
        position: 'relative',
        overflow: 'hidden',
        '&::before': {
          content: '""',
          position: 'absolute',
          top: 0,
          left: 0,
          right: 0,
          height: 4,
          background: `linear-gradient(90deg, ${theme.palette[color].main}, ${theme.palette[color].light})`,
          borderRadius: '16px 16px 0 0'
        }
      }}
    >
      <Box sx={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
        <Box>
          <Box sx={{ color: 'text.secondary', fontSize: '0.875rem', mb: 1 }}>
            {title}
          </Box>
          <Box sx={{ 
            fontSize: { xs: '1.5rem', md: '2rem' }, 
            fontWeight: 600,
            lineHeight: 1,
            color: theme.palette[color].main
          }}>
            {value}
          </Box>
        </Box>
        {icon && (
          <Box sx={{ 
            color: theme.palette[color].main, 
            opacity: 0.3,
            fontSize: { xs: '2rem', md: '2.5rem' }
          }}>
            {icon}
          </Box>
        )}
      </Box>
    </ResponsiveCard>
  );
}

// Loading Skeleton
export function LoadingSkeleton({ height = 200 }: { height?: number }) {
  return (
    <Box
      sx={{
        height,
        borderRadius: 3,
        background: theme => theme.palette.mode === 'dark'
          ? 'linear-gradient(90deg, #18181b 0%, #27272a 50%, #18181b 100%)'
          : 'linear-gradient(90deg, #f4f4f5 0%, #e4e4e7 50%, #f4f4f5 100%)',
        backgroundSize: '200% 100%',
        animation: 'shimmer 1.5s ease-in-out infinite',
        '@keyframes shimmer': {
          '0%': { backgroundPosition: '200% 0' },
          '100%': { backgroundPosition: '-200% 0' }
        }
      }}
    />
  );
}

