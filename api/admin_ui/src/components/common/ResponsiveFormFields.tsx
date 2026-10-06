import React from 'react';
import {
  Box,
  Typography,
  TextField,
  Stack,
  useTheme,
  useMediaQuery,
  Paper,
  InputAdornment,
  IconButton,
  Button,
  Chip,
} from '@mui/material';
import { 
  Visibility, 
  VisibilityOff,
  CloudUpload,
  Clear,
  AttachFile,
} from '@mui/icons-material';

interface ResponsiveTextFieldProps {
  label: string;
  value?: string;
  placeholder?: string;
  helperText?: string;
  error?: boolean;
  errorMessage?: string;
  onChange?: (value: string) => void;
  type?: 'text' | 'email' | 'tel' | 'number' | 'password' | 'url';
  required?: boolean;
  disabled?: boolean;
  multiline?: boolean;
  rows?: number;
  icon?: React.ReactNode;
  endAdornment?: React.ReactNode;
  fullWidth?: boolean;
}

export function ResponsiveTextField({
  label,
  value,
  placeholder,
  helperText,
  error,
  errorMessage,
  onChange,
  type = 'text',
  required = false,
  disabled = false,
  multiline = false,
  rows = 4,
  icon,
  endAdornment,
  fullWidth = true,
}: ResponsiveTextFieldProps) {
  const theme = useTheme();
  const isMobile = useMediaQuery(theme.breakpoints.down('sm'));
  const [showPassword, setShowPassword] = React.useState(false);
  const fieldId = React.useId();
  const descriptionId = `${fieldId}-description`;
  const errorId = `${fieldId}-helper-text`;

  return (
    <Box sx={{ mb: isMobile ? 2.5 : 3, width: fullWidth ? '100%' : 'auto' }}>
      <Stack spacing={1}>
        <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
          {icon && (
            <Box sx={{ color: error ? 'error.main' : 'text.secondary' }}>
              {icon}
            </Box>
          )}
          <Typography 
            component="label"
            htmlFor={fieldId}
            variant="subtitle2" 
            fontWeight={600}
            color={error ? 'error' : 'textPrimary'}
          >
            {label}
            {required && <span aria-hidden="true" style={{ color: theme.palette.error.main }}> *</span>}
          </Typography>
        </Box>

        {helperText && !error && (
          <Typography id={descriptionId} variant="caption" color="text.secondary">
            {helperText}
          </Typography>
        )}

        <TextField
          id={fieldId}
          fullWidth={fullWidth}
          value={value || ''}
          onChange={(e) => onChange?.(e.target.value)}
          placeholder={placeholder}
          type={type === 'password' && !showPassword ? 'password' : type === 'password' ? 'text' : type}
          error={error}
          helperText={error ? errorMessage : ''}
          FormHelperTextProps={{ id: errorId }}
          inputProps={{
            'aria-describedby': error && errorMessage ? errorId : helperText && !error ? descriptionId : undefined,
          }}
          disabled={disabled}
          required={required}
          multiline={multiline}
          rows={multiline ? rows : undefined}
          size={isMobile ? 'medium' : 'small'}
          InputProps={{
            endAdornment: type === 'password' ? (
              <InputAdornment position="end">
                <IconButton
                  type="button"
                  aria-label={`${showPassword ? 'Hide' : 'Show'} ${label}`}
                  aria-pressed={showPassword}
                  onClick={() => setShowPassword((previous) => !previous)}
                  edge="end"
                  size="small"
                >
                  {showPassword ? <VisibilityOff /> : <Visibility />}
                </IconButton>
              </InputAdornment>
            ) : endAdornment ? (
              <InputAdornment position="end">{endAdornment}</InputAdornment>
            ) : null,
          }}
          sx={{
            '& .MuiOutlinedInput-root': {
              borderRadius: 2,
              backgroundColor: theme.palette.mode === 'dark' 
                ? 'rgba(255, 255, 255, 0.02)' 
                : 'rgba(0, 0, 0, 0.02)',
              '&:hover': {
                backgroundColor: theme.palette.mode === 'dark'
                  ? 'rgba(255, 255, 255, 0.04)'
                  : 'rgba(0, 0, 0, 0.04)',
              },
              '&.Mui-focused': {
                backgroundColor: 'transparent',
              }
            },
          }}
        />
      </Stack>
    </Box>
  );
}

interface ResponsiveFileUploadProps {
  label: string;
  helperText?: string;
  error?: boolean;
  errorMessage?: string;
  onFileSelect?: (file: File | null) => void;
  accept?: string;
  maxSize?: number;
  required?: boolean;
  disabled?: boolean;
  value?: File | null;
  icon?: React.ReactNode;
}

export function ResponsiveFileUpload({
  label,
  helperText,
  error,
  errorMessage,
  onFileSelect,
  accept = '*',
  maxSize,
  required = false,
  disabled = false,
  value,
  icon,
}: ResponsiveFileUploadProps) {
  const theme = useTheme();
  const isMobile = useMediaQuery(theme.breakpoints.down('sm'));
  const fileInputRef = React.useRef<HTMLInputElement>(null);

  const handleFileChange = (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0] || null;
    if (file && maxSize && file.size > maxSize) {
      onFileSelect?.(null);
      return;
    }
    onFileSelect?.(file);
  };

  const handleClear = () => {
    if (fileInputRef.current) {
      fileInputRef.current.value = '';
    }
    onFileSelect?.(null);
  };

  return (
    <Box sx={{ mb: isMobile ? 2.5 : 3 }}>
      <Stack spacing={1}>
        <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
          {icon && (
            <Box sx={{ color: error ? 'error.main' : 'text.secondary' }}>
              {icon}
            </Box>
          )}
          <Typography 
            variant="subtitle2" 
            fontWeight={600}
            color={error ? 'error' : 'textPrimary'}
          >
            {label}
            {required && <span style={{ color: theme.palette.error.main }}> *</span>}
          </Typography>
        </Box>

        {helperText && !error && (
          <Typography variant="caption" color="text.secondary">
            {helperText}
          </Typography>
        )}

        <input
          ref={fileInputRef}
          type="file"
          accept={accept}
          onChange={handleFileChange}
          style={{ display: 'none' }}
          disabled={disabled}
        />

        <Box sx={{ display: 'flex', gap: 1, alignItems: 'center', flexWrap: 'wrap' }}>
          <Button
            variant="outlined"
            startIcon={<CloudUpload />}
            onClick={() => fileInputRef.current?.click()}
            disabled={disabled}
            sx={{
              borderRadius: 2,
              textTransform: 'none',
              minHeight: isMobile ? 48 : 40,
            }}
          >
            Choose File
          </Button>

          {value && (
            <Chip
              label={value.name}
              onDelete={handleClear}
              deleteIcon={<Clear />}
              icon={<AttachFile />}
              variant="outlined"
              sx={{ maxWidth: isMobile ? '100%' : 300 }}
            />
          )}
        </Box>

        {error && errorMessage && (
          <Typography variant="caption" color="error">
            {errorMessage}
          </Typography>
        )}
      </Stack>
    </Box>
  );
}

export function ResponsiveFormSection({
  title,
  subtitle,
  children,
  icon,
}: {
  title: string;
  subtitle?: string;
  children: React.ReactNode;
  icon?: React.ReactNode;
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
        backgroundColor: theme.palette.mode === 'dark' 
          ? 'rgba(255, 255, 255, 0.01)' 
          : 'rgba(0, 0, 0, 0.01)',
      }}
    >
      <Stack spacing={isMobile ? 2 : 3}>
        <Box>
          <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.5, mb: 0.5 }}>
            {icon && (
              <Box sx={{ color: 'primary.main', display: 'flex' }}>
                {icon}
              </Box>
            )}
            <Typography variant="h6" fontWeight={600}>
              {title}
            </Typography>
          </Box>
          {subtitle && (
            <Typography variant="body2" color="text.secondary">
              {subtitle}
            </Typography>
          )}
        </Box>
        {children}
      </Stack>
    </Paper>
  );
}
