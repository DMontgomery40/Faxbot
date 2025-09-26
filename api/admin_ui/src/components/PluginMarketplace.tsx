import React, { useState } from 'react';
import { Box, Typography, TextField, InputAdornment, Grid, Card, CardContent } from '@mui/material';
import SearchIcon from '@mui/icons-material/Search';

/**
 * Minimal placeholder for the Admin Console Plugin Marketplace.
 * - Strict TypeScript compliant (no unused vars)
 * - Not wired into the App shell yet; safe to compile
 * - No provider name checks; trait‑gated wiring will come in later PRs
 */
export default function PluginMarketplace(): JSX.Element {
  const [query, setQuery] = useState('');

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

      <Grid container spacing={2}>
        {/* Empty state placeholder; results will be populated in later PRs */}
        <Grid item xs={12}>
          <Card variant="outlined">
            <CardContent>
              <Typography color="text.secondary">
                Marketplace results will appear here. Use traits to gate provider‑specific UI.
              </Typography>
            </CardContent>
          </Card>
        </Grid>
      </Grid>
    </Box>
  );
}

