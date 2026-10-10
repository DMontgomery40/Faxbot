// Providers → In use → Accounts: the country service rules (the UAE, Saudi Arabia; routing/country_rules.py) on
// the row of each account they concern, so a cloud-only installation sees them too. The trunk page keeps its own
// Country rules section. Nothing is blocked: an account stays usable until you confirm.
import { useCallback, useEffect, useState } from 'react';
import { Box, Button, Link, Typography } from '@mui/material';
import { AdminAPIError } from '../../api/client';
import { Field, FormDialog } from '../access/AccessViews';
import type { CountryRulesView, RulesApi } from '../ProviderRulesApi';

// The rules for every account, read once for the whole list; nothing for someone who may not read them.
export function useCountryRules(api: RulesApi) {
  const [rules, setRules] = useState<CountryRulesView | null>(null);
  const [failed, setFailed] = useState(false);
  const load = useCallback(() => {
    api.countryRules().then((found) => { setRules(found); setFailed(false); }).catch((failure) => {
      setRules(null);
      // A refusal for lack of permission, or a server without these rules, shows nothing.
      setFailed(!(failure instanceof AdminAPIError && (failure.status === 403 || failure.status === 404)));
    });
  }, [api]);
  useEffect(() => { load(); }, [load]);
  return { rules, failed, setRules };
}

export default function AccountCountryRules({ api, account, rules, canWrite, onChange }: {
  api: RulesApi; account: string; rules: CountryRulesView | null; canWrite: boolean;
  onChange: (next: CountryRulesView) => void;
}) {
  const [open, setOpen] = useState<string | null>(null);
  const [evidence, setEvidence] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const items = (rules?.accounts ?? []).filter((item) => item.account === account);
  if (items.length === 0) return null;
  const submit = async (country: string) => {
    setBusy(true);
    setError(null);
    try {
      onChange(await api.confirmCountry(account, country, evidence.trim()));
      setOpen(null);
      setEvidence('');
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Box sx={{ mt: 0.5 }} data-testid={`account-country-rules-${account}`}>
      {items.map((item) => {
        const country = rules?.countries.find((entry) => entry.country === item.country);
        return (
          <Box key={item.country}>
            <Typography variant="caption" display="block" color={item.confirmed ? 'text.secondary' : 'warning.main'}>
              {item.sentence}
            </Typography>
            {country && (
              <Typography variant="caption" display="block" color="text.secondary">
                {country.sentence}{' '}
                {country.sources.map((source) => (
                  <Link key={source.url} href={source.url} target="_blank" rel="noopener noreferrer" sx={{ mr: 1 }}>
                    {source.label}
                  </Link>
                ))}
              </Typography>
            )}
            {canWrite && !item.confirmed && (
              <Button size="small" onClick={() => { setError(null); setOpen(item.country); }}
                aria-label={`Confirm the rules for ${item.label}`}>
                Confirm
              </Button>
            )}
            <FormDialog open={open === item.country} title={`Confirm ${item.label}`} submitLabel="Confirm" busy={busy}
              error={error} canSubmit={Boolean(evidence.trim())} onSubmit={() => void submit(item.country)}
              onClose={() => setOpen(null)}>
              <Field label="How you know" value={evidence} onChange={setEvidence} multiline
                helperText="For example, your provider's licence or your contract with a licensed carrier." />
            </FormDialog>
          </Box>
        );
      })}
    </Box>
  );
}
