// Costs → Recommendations → Sending: numbers where another route would have cost less than the
// one Faxbot uses first now, counting every attempt (failed ones too) over the last 30 days.
// "Use this route" makes it the number's preferred route; nothing is sent again.
import { useCallback, useEffect, useState } from 'react';
import { Box, Button, Chip, CircularProgress, Paper, Stack, Typography } from '@mui/material';
import AdminAPIClient from '../../api/client';
import type { SendingRecommendation, SendingRecommendations as Result } from '../../api/deliveryTypes';
import { DeliveryError, Notice, formatMoney } from './shared';
import type { AdminDestination } from '../../navigation';
import { AddAsRuleButton } from '../ProviderRulesSuggest';
import { rulesApiFor } from '../ProviderRulesApi';

function Item({ item, canWrite, busy, onUse, client, onRuleAdded, onError, onNavigate }: {
  item: SendingRecommendation;
  canWrite: boolean;
  busy: boolean;
  onUse: (item: SendingRecommendation) => void;
  client: AdminAPIClient;
  onRuleAdded: (sentence: string) => void;
  onError: (error: unknown) => void;
  onNavigate?: (destination: AdminDestination) => void;
}) {
  const { current, suggested } = item;
  return (
    <Paper variant="outlined" sx={{ p: 2, borderRadius: 2 }} data-testid="sending-recommendation">
      <Typography variant="subtitle1">{item.display_name || item.number}</Typography>
      {item.display_name && <Typography variant="body2" color="text.secondary">{item.number}</Typography>}
      <Typography variant="body2" sx={{ mt: 1 }}>{item.sentence}</Typography>
      <Box display="flex" gap={1} flexWrap="wrap" sx={{ mt: 1 }}>
        {current && <Chip size="small" variant="outlined"
          label={`${current.label}: ${current.delivered} of ${current.attempts} faxes delivered`} />}
        <Chip size="small" variant="outlined"
          label={`${suggested.label}: ${suggested.delivered} of ${suggested.attempts} faxes delivered`} />
        {item.saving_per_fax && <Chip size="small" label={`Saves about ${formatMoney(item.saving_per_fax)} a fax`} />}
      </Box>
      <Box sx={{ mt: 1.5 }}>
        <Button variant="contained" size="small" disabled={!canWrite || busy} onClick={() => onUse(item)}
          aria-label={`Use ${suggested.label} for ${item.number}`} sx={{ borderRadius: 2 }}>
          Use this route
        </Button>
        {canWrite && item.rule_suggestion && (
          <AddAsRuleButton api={rulesApiFor(client)} suggestion={item.rule_suggestion} onDone={onRuleAdded} onError={onError}
            onNavigate={onNavigate} />
        )}
      </Box>
    </Paper>
  );
}

export default function SendingRecommendations({ client, canWrite, onCount, onNavigate }: {
  client: AdminAPIClient;
  canWrite: boolean;
  onNavigate?: (destination: AdminDestination) => void;
  // How many recommendations this section shows, or null until it knows.
  onCount?: (count: number | null) => void;
}) {
  const [data, setData] = useState<Result | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const loaded = await client.getSendingRecommendations();
      setData(loaded);
      onCount?.(loaded.items.length);
    } catch (failure) {
      setError(failure);
      onCount?.(null);
    } finally {
      setLoading(false);
    }
  }, [client, onCount]);

  useEffect(() => { void load(); }, [load]);

  const use = async (item: SendingRecommendation) => {
    setBusy(item.number);
    setError(null);
    try {
      await client.updateDestination(item.number, { preferred_route: item.suggested.route, version: item.version });
      setMessage(`${item.suggested.label} is now the first choice for faxes to ${item.display_name || item.number}.`);
      await load();
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(null);
    }
  };

  const items = data?.items ?? [];
  return (
    <Box>
      <DeliveryError error={error} onClose={() => setError(null)} />
      <Notice message={message} onClose={() => setMessage(null)} />
      {loading && !data && <CircularProgress size={24} />}
      {items.length > 0 && (
        <Box data-testid="sending-recommendations">
          <Typography variant="h6" component="h2" gutterBottom>Sending</Typography>
          <Stack spacing={2}>
            {items.map((item) => (
              <Item key={item.number} item={item} canWrite={canWrite} busy={busy === item.number} onUse={(chosen) => void use(chosen)}
                client={client} onRuleAdded={setMessage} onError={setError} onNavigate={onNavigate} />
            ))}
          </Stack>
        </Box>
      )}
    </Box>
  );
}
