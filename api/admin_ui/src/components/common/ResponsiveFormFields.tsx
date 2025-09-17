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
  FormControl,
  FormLabel,
  RadioGroup,
  FormControlLabel,
  Radio,
  Checkbox,
  Select,
  MenuItem,
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

        <TextField
          fullWidth={fullWidth}
          value={value || ''}
          onChange={(e) => onChange?.(e.target.value)}
          placeholder={placeholder}
          type={type === 'password' && !showPassword ? 'password' : type === 'password' ? 'text' : type}
          error={error}
          helperText={error ? errorMessage : ''}
          disabled={disabled}
          required={required}
          multiline={multiline}
          rows={multiline ? rows : undefined}
          size={isMobile ? 'medium' : 'small'}
          InputProps={{
            endAdornment: type === 'password' ? (
              <InputAdornment position="end">
                <IconButton
                  onClick={() => setShowPassword(!showPassword)}
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

interface ResponsiveSelectProps {
  label: string;
  value?: string;
  options: { value: string; label: string; disabled?: boolean }[];
  helperText?: string;
  error?: boolean;
  errorMessage?: string;
  onChange?: (value: string) => void;
  required?: boolean;
  disabled?: boolean;
  icon?: React.ReactNode;
  fullWidth?: boolean;
}

export function ResponsiveSelect({
  label,
  value,
  options,
  helperText,
  error,
  errorMessage,
  onChange,
  required = false,
  disabled = false,
  icon,
  fullWidth = true,
}: ResponsiveSelectProps) {
  const theme = useTheme();
  const isMobile = useMediaQuery(theme.breakpoints.down('sm'));

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

        <Select
          fullWidth={fullWidth}
          value={value || ''}
          onChange={(e) => onChange?.(e.target.value)}
          error={error}
          disabled={disabled}
          required={required}
          size={isMobile ? 'medium' : 'small'}
          sx={{
            borderRadius: 2,
            backgroundColor: theme.palette.mode === 'dark' 
              ? 'rgba(255, 255, 255, 0.02)' 
              : 'rgba(0, 0, 0, 0.02)',
            '&:hover': {
              backgroundColor: theme.palette.mode === 'dark'
                ? 'rgba(255, 255, 255, 0.04)'
                : 'rgba(0, 0, 0, 0.04)',
            },
          }}
        >
          {options.map((option) => (
            <MenuItem key={option.value} value={option.value} disabled={option.disabled}>
              {option.label}
            </MenuItem>
          ))}
        </Select>

        {error && errorMessage && (
          <Typography variant="caption" color="error">
            {errorMessage}
          </Typography>
        )}
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

interface ResponsiveCheckboxProps {
  label: string;
  checked?: boolean;
  onChange?: (checked: boolean) => void;
  helperText?: string;
  disabled?: boolean;
  icon?: React.ReactNode;
}

export function ResponsiveCheckbox({
  label,
  checked = false,
  onChange,
  helperText,
  disabled = false,
  icon,
}: ResponsiveCheckboxProps) {
  const theme = useTheme();
  const isMobile = useMediaQuery(theme.breakpoints.down('sm'));

  return (
    <Box sx={{ mb: isMobile ? 2 : 2.5 }}>
      <FormControlLabel
        control={
          <Checkbox
            checked={checked}
            onChange={(e) => onChange?.(e.target.checked)}
            disabled={disabled}
            sx={{ mr: 1 }}
          />
        }
        label={
          <Box>
            <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
              {icon && (
                <Box sx={{ color: 'text.secondary', display: 'flex' }}>
                  {icon}
                </Box>
              )}
              <Typography variant="subtitle2" fontWeight={600}>
                {label}
              </Typography>
            </Box>
            {helperText && (
              <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 0.5 }}>
                {helperText}
              </Typography>
            )}
          </Box>
        }
        sx={{ alignItems: 'flex-start' }}
      />
    </Box>
  );
}

interface ResponsiveRadioGroupProps {
  label: string;
  value?: string;
  options: { value: string; label: string; helperText?: string }[];
  onChange?: (value: string) => void;
  helperText?: string;
  required?: boolean;
  disabled?: boolean;
  icon?: React.ReactNode;
}

export function ResponsiveRadioGroup({
  label,
  value,
  options,
  onChange,
  helperText,
  required = false,
  disabled = false,
  icon,
}: ResponsiveRadioGroupProps) {
  const theme = useTheme();
  const isMobile = useMediaQuery(theme.breakpoints.down('sm'));

  return (
    <Box sx={{ mb: isMobile ? 2.5 : 3 }}>
      <FormControl component="fieldset" fullWidth disabled={disabled}>
        <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, mb: 1 }}>
          {icon && (
            <Box sx={{ color: 'text.secondary' }}>
              {icon}
            </Box>
          )}
          <FormLabel component="legend" sx={{ fontWeight: 600 }}>
            {label}
            {required && <span style={{ color: theme.palette.error.main }}> *</span>}
          </FormLabel>
        </Box>

        {helperText && (
          <Typography variant="caption" color="text.secondary" sx={{ mb: 1, display: 'block' }}>
            {helperText}
          </Typography>
        )}

        <RadioGroup value={value || ''} onChange={(e) => onChange?.(e.target.value)}>
          {options.map((option) => (
            <FormControlLabel
              key={option.value}
              value={option.value}
              control={<Radio />}
              label={
                <Box>
                  <Typography variant="body2">{option.label}</Typography>
                  {option.helperText && (
                    <Typography variant="caption" color="text.secondary">
                      {option.helperText}
                    </Typography>
                  )}
                </Box>
              }
              sx={{ mb: 1 }}
            />
          ))}
        </RadioGroup>
      </FormControl>
    </Box>
  );
}

// Responsive form section wrapper
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
