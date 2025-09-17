import React, { useState } from 'react';
import { IconButton, Menu, MenuItem, ListItemIcon, ListItemText, Tooltip } from '@mui/material';
import { 
  Brightness7 as LightIcon, 
  Brightness4 as DarkIcon,
  SettingsBrightness as SystemIcon
} from '@mui/icons-material';
import { useTheme } from '../theme/ThemeContext';

export function ThemeToggle() {
  const { mode, setMode, resolvedMode } = useTheme();
  const [anchorEl, setAnchorEl] = useState<null | HTMLElement>(null);

  const handleClick = (event: React.MouseEvent<HTMLElement>) => {
    setAnchorEl(event.currentTarget);
  };

  const handleClose = () => {
    setAnchorEl(null);
  };

  const handleModeChange = (newMode: 'dark' | 'light' | 'system') => {
    setMode(newMode);
    handleClose();
  };

  const getCurrentIcon = () => {
    if (mode === 'system') {
      return <SystemIcon />;
    }
    return resolvedMode === 'dark' ? <DarkIcon /> : <LightIcon />;
  };

  return (
    <>
      <Tooltip title="Theme">
        <IconButton
          onClick={handleClick}
          sx={{
            transition: 'transform 0.2s',
            '&:hover': {
              transform: 'rotate(20deg)'
            }
          }}
        >
          {getCurrentIcon()}
        </IconButton>
      </Tooltip>
      <Menu
        anchorEl={anchorEl}
        open={Boolean(anchorEl)}
        onClose={handleClose}
        PaperProps={{
          sx: {
            minWidth: 180,
            mt: 1
          }
        }}
        transformOrigin={{ horizontal: 'right', vertical: 'top' }}
        anchorOrigin={{ horizontal: 'right', vertical: 'bottom' }}
      >
        <MenuItem 
          onClick={() => handleModeChange('dark')}
          selected={mode === 'dark'}
        >
          <ListItemIcon>
            <DarkIcon fontSize="small" />
          </ListItemIcon>
          <ListItemText>Dark</ListItemText>
        </MenuItem>
        <MenuItem 
          onClick={() => handleModeChange('light')}
          selected={mode === 'light'}
        >
          <ListItemIcon>
            <LightIcon fontSize="small" />
          </ListItemIcon>
          <ListItemText>Light</ListItemText>
        </MenuItem>
        <MenuItem 
          onClick={() => handleModeChange('system')}
          selected={mode === 'system'}
        >
          <ListItemIcon>
            <SystemIcon fontSize="small" />
          </ListItemIcon>
          <ListItemText>System</ListItemText>
        </MenuItem>
      </Menu>
    </>
  );
}
