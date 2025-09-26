import React, { useEffect, useState } from 'react';
import { Box, Typography, TextField, InputAdornment, Grid, Card, CardContent, Alert, CircularProgress } from '@mui/material';
import SearchIcon from '@mui/icons-material/Search';
import { AdminAPIClient } from '../api/client';

/**
 * Minimal placeholder for the Admin Console Plugin Marketplace.
 * - Strict TypeScript compliant (no unused vars)
 * - Not wired into the App shell yet; safe to compile
 * - No provider name checks; trait‑gated wiring will come in later PRs
 */
type Props = { client: AdminAPIClient; docsBase?: string };

export default function PluginMarketplace({ client, docsBase }: Props): JSX.Element {
  const [query, setQuery] = useState('');
  const [loading, setLoading] = useState(false);
  const [plugins, setPlugins] = useState<Array<{ id: string; name?: string; description?: string }>>([]);
  const [disabled, setDisabled] = useState(false);

  useEffect(() => {
    let cancelled = false;
    const run = async () => {
      setLoading(true);
      try {
        const res = await fetch(`${window.location.origin}/admin/marketplace/plugins`, {
          headers: { 'X-API-Key': (client as any).apiKey || '' },
        });
        if (res.status === 404) {
          if (!cancelled) setDisabled(true);
          return;
        }
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        if (!cancelled) setPlugins(Array.isArray(data?.plugins) ? data.plugins : []);
      } catch {
        if (!cancelled) setDisabled(true);
      } finally {
        if (!cancelled) setLoading(false);
      }
    };
    run();
    return () => { cancelled = true; };
  }, [client]);

  return (
    <Box sx={{ px: { xs: 1, sm: 2, md: 3 }, py: 2 }}>
      <Typography variant="h4" sx={{ mb: 2 }}>
        Plugin Marketplace
      </Typography>

      <TextField
        fullWidth
        placeholder="Search plugins…"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        InputProps={{
          startAdornment: (
            <InputAdornment position="start">
              <SearchIcon />
            </InputAdornment>
          ),
        }}
        sx={{ mb: 3 }}
      />

      {disabled && (
        <Alert severity="info" sx={{ mb: 2 }}>
          Plugin Marketplace is disabled. Enable by setting <code>ADMIN_MARKETPLACE_ENABLED=true</code>.
          {docsBase && (
            <>
              {' '}Learn more at <a href={`${docsBase}/admin/marketplace`} target="_blank" rel="noreferrer">docs</a>.
            </>
          )}
        </Alert>
      )}

      {loading && <CircularProgress size={20} sx={{ mb: 2 }} />}

      <Grid container spacing={2}>
        {/* Empty state placeholder; results will be populated in later PRs */}
        <Grid item xs={12}>
          <Card variant="outlined">
            <CardContent>
              {plugins.length === 0 ? (
                <Typography color="text.secondary">
                  {disabled ? 'Marketplace is currently disabled.' : 'No plugins found.'}
                </Typography>
              ) : (
                <>
                  {plugins
                    .filter(p => (query ? (p.name || '').toLowerCase().includes(query.toLowerCase()) : true))
                    .map(p => (
                      <Box key={p.id} sx={{ mb: 1 }}>
                        <Typography variant="subtitle1">{p.name || p.id}</Typography>
                        {p.description && (
                          <Typography variant="body2" color="text.secondary">{p.description}</Typography>
                        )}
                      </Box>
                    ))}
                </>
              )}
            </CardContent>
          </Card>
        </Grid>
      </Grid>
    </Box>
  );
}
