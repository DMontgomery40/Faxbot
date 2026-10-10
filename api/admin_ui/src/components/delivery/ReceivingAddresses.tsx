// The addresses a receiving provider sends received faxes to, each with Copy.
// Shown under Delivery setup → Providers & accounts to people who may read provider setup.
import { useEffect, useState } from 'react';
import { Box, Button, Paper, Snackbar, Stack, Typography } from '@mui/material';
import { ContentCopy as ContentCopyIcon } from '@mui/icons-material';
import type AdminAPIClient from '../../api/client';

interface Callback { name: string; url: string }

export default function ReceivingAddresses({ client }: { client: AdminAPIClient }) {
  const [callbacks, setCallbacks] = useState<Callback[]>([]);
  const [copied, setCopied] = useState('');

  useEffect(() => {
    let current = true;
    client.getInboundCallbacks()
      .then((info) => { if (current) setCallbacks(Array.isArray(info?.callbacks) ? info.callbacks : []); })
      .catch(() => { if (current) setCallbacks([]); });
    return () => { current = false; };
  }, [client]);

  if (callbacks.length === 0) return null;

  const copy = async (callback: Callback) => {
    setCopied('');
    try {
      await navigator.clipboard.writeText(callback.url);
      setCopied('Address copied.');
    } catch {
      setCopied('Could not copy the address. Select it above and copy it manually.');
    }
  };

  return (
    <Box component="section" sx={{ mt: 3 }} data-testid="receiving-addresses">
      <Typography variant="h6" component="h2">Addresses to give your provider</Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
        Enter these in your provider's account so received faxes reach Faxbot.
      </Typography>
      <Stack spacing={1}>
        {callbacks.map((callback) => (
          <Paper key={callback.name} variant="outlined" sx={{ p: 2, borderRadius: 2 }}>
            <Box sx={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap', gap: 1 }}>
              <Box sx={{ flex: 1 }}>
                <Typography variant="body2" fontWeight={600}>{callback.name}</Typography>
                <Typography variant="body2" sx={{ fontFamily: 'monospace', wordBreak: 'break-all', mt: 0.5 }}>{callback.url}</Typography>
              </Box>
              <Button size="small" variant="outlined" startIcon={<ContentCopyIcon />} onClick={() => void copy(callback)}>
                Copy
              </Button>
            </Box>
          </Paper>
        ))}
      </Stack>
      <Snackbar open={!!copied} autoHideDuration={2000} onClose={() => setCopied('')} message={copied} />
    </Box>
  );
}
