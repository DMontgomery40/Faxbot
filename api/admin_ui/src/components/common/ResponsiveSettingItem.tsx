import React from 'react';
import {
  Box,
  Typography,
  TextField,
  Stack,
  useTheme,
  useMediaQuery,
  ListItem,
  ListItemIcon,
  Paper,
  InputAdornment,
  IconButton,
  Tooltip,
} from '@mui/material';
import { Visibility, VisibilityOff, InfoOutlined } from '@mui/icons-material';

interface ResponsiveSettingItemProps {
  icon?: React.ReactNode;
  label: string;
  value?: string;
  helperText?: string;
  placeholder?: string;
  onChange?: (value: string) => void;
  type?: 'text' | 'password' | 'select' | 'number';
  options?: { value: string; label: string }[];
  required?: boolean;
  fullWidth?: boolean;
  showCurrentValue?: boolean;
  infoLink?: { text: string; url: string };
}

export function ResponsiveSettingItem({
  icon,
  label,
  value,
  helperText,
  placeholder,
  onChange,
  type = 'text',
  options,
  required = false,
  fullWidth = true,
  showCurrentValue = true,
  infoLink,
}: ResponsiveSettingItemProps) {
  const theme = useTheme();
  const isMobile = useMediaQuery(theme.breakpoints.down('sm'));
  const [showPassword, setShowPassword] = React.useState(false);

  const handleTogglePassword = () => setShowPassword(!showPassword);

  const renderInput = () => {
    if (type === 'select' && options) {
      return (
        <TextField
          select
          fullWidth={fullWidth}
          value={value || ''}
          onChange={(e) => onChange?.(e.target.value)}
          placeholder={placeholder}
          SelectProps={{
            native: true,
          }}
          size="small"
          sx={{
            '& .MuiOutlinedInput-root': {
              borderRadius: 2,
              backgroundColor: 'background.paper',
            }
          }}
        >
          {options.map((opt) => (
            <option key={opt.value} value={opt.value}>
              {opt.label}
            </option>
          ))}
        </TextField>
      );
    }

    return (
      <TextField
        fullWidth={fullWidth}
        type={type === 'password' && !showPassword ? 'password' : 'text'}
        placeholder={placeholder}
        onChange={(e) => onChange?.(e.target.value)}
        size="small"
        required={required}
        InputProps={{
          endAdornment: type === 'password' ? (
            <InputAdornment position="end">
              <IconButton
                onClick={handleTogglePassword}
                edge="end"
                size="small"
              >
                {showPassword ? <VisibilityOff /> : <Visibility />}
              </IconButton>
            </InputAdornment>
          ) : null,
        }}
        sx={{
          '& .MuiOutlinedInput-root': {
            borderRadius: 2,
            backgroundColor: 'background.paper',
          }
        }}
      />
    );
  };

  if (isMobile) {
    // Mobile layout - vertical stacking
    return (
      <Box sx={{ mb: 3, width: '100%' }}>
        <Stack spacing={1}>
          {/* Header with icon and label */}
          <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
            {icon && (
              <Box sx={{ 
                display: 'flex', 
                alignItems: 'center',
                color: 'text.secondary',
              }}>
                {icon}
              </Box>
            )}
            <Typography variant="subtitle2" fontWeight={600}>
              {label}
              {required && <span style={{ color: theme.palette.error.main }}> *</span>}
            </Typography>
            {infoLink && (
              <Tooltip title={infoLink.text}>
                <IconButton
                  size="small"
                  component="a"
                  href={infoLink.url}
                  target="_blank"
                  rel="noreferrer"
                >
                  <InfoOutlined fontSize="small" />
                </IconButton>
              </Tooltip>
            )}
          </Box>

          {/* Current value if exists */}
          {showCurrentValue && value && (
            <Typography
              variant="caption"
              sx={{
                color: 'text.secondary',
                px: 1,
                py: 0.5,
                backgroundColor: 'action.hover',
                borderRadius: 1,
                display: 'inline-block',
                fontFamily: type === 'password' ? 'monospace' : 'inherit',
                wordBreak: 'break-all',
              }}
            >
              Current: {type === 'password' ? '••••••••••••' : (value.length > 30 ? `${value.substring(0, 30)}...` : value)}
            </Typography>
          )}

          {/* Helper text */}
          {helperText && (
            <Typography variant="caption" color="text.secondary" sx={{ px: 0.5 }}>
              {helperText}
            </Typography>
          )}

          {/* Input field */}
          {renderInput()}
        </Stack>
      </Box>
    );
  }

  // Desktop layout - horizontal with better spacing
  return (
    <ListItem 
      sx={{ 
        py: 2,
        px: 0,
        display: 'flex',
        alignItems: 'flex-start',
        gap: 2,
      }}
    >
      {icon && (
        <ListItemIcon sx={{ minWidth: 40, mt: 0.5 }}>
          {icon}
        </ListItemIcon>
      )}
      
      <Stack spacing={1} sx={{ flex: 1 }}>
        <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
          <Typography variant="subtitle2" fontWeight={600}>
            {label}
            {required && <span style={{ color: theme.palette.error.main }}> *</span>}
          </Typography>
          {infoLink && (
            <Tooltip title={infoLink.text}>
              <IconButton
                size="small"
                component="a"
                href={infoLink.url}
                target="_blank"
                rel="noreferrer"
                sx={{ ml: 'auto' }}
              >
                <InfoOutlined fontSize="small" />
              </IconButton>
            </Tooltip>
          )}
        </Box>

        {showCurrentValue && value && (
          <Typography
            variant="caption"
            sx={{
              color: 'text.secondary',
              fontFamily: type === 'password' ? 'monospace' : 'inherit',
            }}
          >
            Current: {type === 'password' ? '••••••••••••' : value}
          </Typography>
        )}

        {helperText && (
          <Typography variant="caption" color="text.secondary">
            {helperText}
          </Typography>
        )}
      </Stack>

      <Box sx={{ minWidth: 300, maxWidth: 400 }}>
        {renderInput()}
      </Box>
    </ListItem>
  );
}

// Responsive section header
export function ResponsiveSettingSection({
  title,
  subtitle,
  children,
}: {
  title: string;
  subtitle?: string;
  children: React.ReactNode;
}) {
  const theme = useTheme();
  const isMobile = useMediaQuery(theme.breakpoints.down('sm'));

  return (
    <Paper
      elevation={0}
      sx={{
        p: isMobile ? 2 : 3,
        mb: 3,
        border: '1px solid',
        borderColor: 'divider',
        borderRadius: 2,
      }}
    >
      <Stack spacing={isMobile ? 2 : 3}>
        <Box>
          <Typography variant="h6" fontWeight={600}>
            {title}
          </Typography>
          {subtitle && (
            <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5 }}>
              {subtitle}
            </Typography>
          )}
        </Box>
        {children}
      </Stack>
    </Paper>
  );
}
