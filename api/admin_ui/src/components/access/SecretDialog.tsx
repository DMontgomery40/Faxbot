import { useState } from 'react';
import {
  Alert,
  Box,
  Button,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  IconButton,
  Snackbar,
  Tooltip,
  Typography,
  useMediaQuery,
  useTheme,
} from '@mui/material';
import CopyIcon from '@mui/icons-material/ContentCopy';

export interface SecretReveal {
  title: string;
  message: string;
  label: string;
  secret: string;
}

// Shows a secret exactly once. Closing clears it; nothing else keeps a copy.
export default function SecretDialog({ reveal, onClose }: { reveal: SecretReveal | null; onClose: () => void }) {
  const theme = useTheme();
  const fullScreen = useMediaQuery(theme.breakpoints.down('sm'));
  const [copied, setCopied] = useState(false);

  const copy = async () => {
    if (!reveal) return;
    try {
      await navigator.clipboard.writeText(reveal.secret);
      setCopied(true);
    } catch {
      setCopied(false);
    }
  };

  return (
    <>
      <Dialog open={reveal !== null} maxWidth="sm" fullWidth fullScreen={fullScreen} aria-labelledby="secret-dialog-title">
        <DialogTitle id="secret-dialog-title">{reveal?.title}</DialogTitle>
        <DialogContent>
          <Alert severity="success" sx={{ mb: 2, borderRadius: 2 }}>{reveal?.message}</Alert>
          <Typography variant="subtitle2" sx={{ mb: 1 }}>{reveal?.label}</Typography>
          <Box
            sx={{
              p: 2,
              pr: 6,
              position: 'relative',
              borderRadius: 1,
              fontFamily: 'monospace',
              wordBreak: 'break-all',
              backgroundColor: theme.palette.mode === 'dark' ? 'rgba(255, 255, 255, 0.05)' : 'rgba(0, 0, 0, 0.05)',
            }}
          >
            <Typography variant="body2" sx={{ fontFamily: 'monospace' }} data-testid="secret-value">
              {reveal?.secret}
            </Typography>
            <Tooltip title="Copy">
              <IconButton aria-label="Copy" size="small" onClick={copy} sx={{ position: 'absolute', top: 8, right: 8 }}>
                <CopyIcon fontSize="small" />
              </IconButton>
            </Tooltip>
          </Box>
        </DialogContent>
        <DialogActions sx={{ px: 3, pb: 3 }}>
          <Button onClick={copy} startIcon={<CopyIcon />} sx={{ borderRadius: 2 }}>Copy</Button>
          <Button variant="contained" onClick={onClose} sx={{ borderRadius: 2 }}>Done</Button>
        </DialogActions>
      </Dialog>
      <Snackbar open={copied} autoHideDuration={2000} onClose={() => setCopied(false)} message="Copied" />
    </>
  );
}
