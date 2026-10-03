import { useState } from 'react';
import {
  TextField,
  InputAdornment,
  IconButton,
  TextFieldProps,
} from '@mui/material';
import {
  Visibility,
  VisibilityOff,
} from '@mui/icons-material';

interface SecretInputProps extends Omit<TextFieldProps, 'onChange'> {
  onChange: (value: string) => void;
}

function SecretInput({ onChange, ...props }: SecretInputProps) {
  const [showPassword, setShowPassword] = useState(false);

  const handleToggleVisibility = () => {
    setShowPassword((previous) => !previous);
  };

  return (
    <TextField
      {...props}
      type={showPassword ? 'text' : 'password'}
      onChange={(e) => onChange(e.target.value)}
      InputProps={{
        ...props.InputProps,
        endAdornment: (
          <InputAdornment position="end">
            <IconButton
              type="button"
              aria-label={`${showPassword ? 'Hide' : 'Show'} ${typeof props.label === 'string' ? props.label : 'secret'}`}
              aria-pressed={showPassword}
              onClick={handleToggleVisibility}
              edge="end"
            >
              {showPassword ? <VisibilityOff /> : <Visibility />}
            </IconButton>
          </InputAdornment>
        ),
      }}
    />
  );
}

export default SecretInput;
