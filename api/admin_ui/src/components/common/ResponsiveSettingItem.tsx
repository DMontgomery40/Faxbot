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
  value?: string | number | boolean;
  editValue?: string | number | boolean;
  helperText?: string;
  placeholder?: string;
  onChange?: (value: string) => void;
  type?: 'text' | 'password' | 'select' | 'number';
  options?: { value: string; label: string }[];
  required?: boolean;
  fullWidth?: boolean;
  showCurrentValue?: boolean;
  infoLink?: { text: string; url: string };
  // A custom control in place of the built-in input, labelled by this row.
  renderControl?: (labels: { id: string; labelledBy: string; describedBy?: string }) => React.ReactNode;
}

export function ResponsiveSettingItem({
  icon,
  label,
  value,
  editValue,
  helperText,
  placeholder,
  onChange,
  type = 'text',
  options,
  required = false,
  fullWidth = true,
  showCurrentValue = true,
  infoLink,
  renderControl,
}: ResponsiveSettingItemProps) {
  const theme = useTheme();
  const isMobile = useMediaQuery(theme.breakpoints.down('sm'));
  const [showPassword, setShowPassword] = React.useState(false);
  const inputId = React.useId();
  const labelId = `${inputId}-label`;
  const helperId = `${inputId}-helper`;
  const inputValue = String(editValue ?? '');
  const currentValue = value === undefined ? undefined : String(value);
  const inputAccessibility = {
    'aria-labelledby': labelId,
    'aria-describedby': helperText ? helperId : undefined,
    readOnly: !onChange,
  };

  const handleTogglePassword = () => setShowPassword((previous) => !previous);

  const renderInput = () => {
    if (renderControl) {
      return renderControl({ id: inputId, labelledBy: labelId, describedBy: helperText ? helperId : undefined });
    }
    if (type === 'select' && options) {
      return (
        <TextField
          select
          id={inputId}
          fullWidth={fullWidth}
          value={inputValue}
          onChange={(e) => onChange?.(e.target.value)}
          placeholder={placeholder}
          inputProps={inputAccessibility}
          required={required}
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
          {!options.some((option) => option.value === inputValue) && (
            <option value={inputValue}>{inputValue || 'Not selected'}</option>
          )}
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
        id={inputId}
        fullWidth={fullWidth}
        type={type === 'password' ? (showPassword ? 'text' : 'password') : type}
        value={inputValue}
        placeholder={placeholder}
        onChange={(e) => onChange?.(e.target.value)}
        inputProps={inputAccessibility}
        size="small"
        required={required}
        InputProps={{
          endAdornment: type === 'password' ? (
            <InputAdornment position="end">
              <IconButton
                type="button"
                aria-label={`${showPassword ? 'Hide' : 'Show'} ${label}`}
                aria-pressed={showPassword}
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
            <Typography component="label" id={labelId} htmlFor={inputId} variant="subtitle2" fontWeight={600}>
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
                  aria-label={infoLink.text}
                >
                  <InfoOutlined fontSize="small" />
                </IconButton>
              </Tooltip>
            )}
          </Box>

          {/* Current value if exists */}
          {showCurrentValue && currentValue !== undefined && (
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
              Current: {type === 'password' && currentValue ? '••••••••••••' : (currentValue.length > 30 ? `${currentValue.substring(0, 30)}...` : currentValue || 'Not set')}
            </Typography>
          )}

          {/* Helper text */}
          {helperText && (
            <Typography id={helperId} variant="caption" color="text.secondary" sx={{ px: 0.5 }}>
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
          <Typography component="label" id={labelId} htmlFor={inputId} variant="subtitle2" fontWeight={600}>
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
                aria-label={infoLink.text}
                sx={{ ml: 'auto' }}
              >
                <InfoOutlined fontSize="small" />
              </IconButton>
            </Tooltip>
          )}
        </Box>

        {showCurrentValue && currentValue !== undefined && (
          <Typography
            variant="caption"
            sx={{
              color: 'text.secondary',
              fontFamily: type === 'password' ? 'monospace' : 'inherit',
            }}
          >
            Current: {type === 'password' && currentValue ? '••••••••••••' : currentValue || 'Not set'}
          </Typography>
        )}

        {helperText && (
          <Typography id={helperId} variant="caption" color="text.secondary">
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
