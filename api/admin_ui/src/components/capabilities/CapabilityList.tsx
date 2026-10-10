// Savings & optimization → Capabilities: every capability, grouped by the outcome it serves, with its state
// here. The filter is kept in the address (?show=ready), so a copied link opens the same view.
import type { MouseEvent } from 'react';
import { Box, Button, Chip, Paper, Stack, Typography } from '@mui/material';
import { Science as TestedIcon } from '@mui/icons-material';
import type { Capabilities, Capability, CapabilityFilterKey } from '../../api/capabilityTypes';
import type { AdminDestination } from '../../navigation';
import { AddressLink, CAPABILITY_FILTERS, capabilitiesAddress, missingLine, type Navigate } from './text';

const CHIP = { size: 'small', variant: 'outlined', sx: { height: 22, fontSize: '0.75rem' } } as const;
const TESTED_COLOR = { live: 'info', lab: 'secondary', built: 'default' } as const;

export function StateChips({ item }: { item: Capability }) {
  return (
    <Box display="flex" flexWrap="wrap" gap={0.5} data-testid={`capability-states-${item.key}`}>
      <Chip {...CHIP} label={item.enabled.label} color={item.enabled.on ? 'success' : 'default'} />
      <Chip {...CHIP} label={item.works.label} color={item.works.here ? 'success' : 'warning'} />
      <Chip {...CHIP} label={item.evidence.label} color={TESTED_COLOR[item.evidence.level]} icon={<TestedIcon />}
        sx={{ ...CHIP.sx, '& .MuiChip-icon': { fontSize: 14 } }} />
      {item.ready && <Chip {...CHIP} label="Ready to turn on" color="primary" variant="filled" />}
      {item.experimental && <Chip {...CHIP} label="Experimental" color="secondary" variant="filled" />}
    </Box>
  );
}

function CapabilityCard({ item, onNavigate }: { item: Capability; onNavigate?: Navigate }) {
  const missing = missingLine(item);
  return (
    <Paper variant="outlined" data-testid={`capability-${item.key}`}
      sx={{ p: 1.5, borderRadius: 2, display: 'flex', flexDirection: 'column', gap: 0.75, minWidth: 0 }}>
      <Typography variant="subtitle1" component="h3" sx={{ lineHeight: 1.3, overflowWrap: 'anywhere' }}>
        <AddressLink address={item.address} onNavigate={onNavigate}>{item.name}</AddressLink>
      </Typography>
      <StateChips item={item} />
      <Typography variant="body2">{item.sentence}</Typography>
      {[item.enabled.sentence, item.works.sentence, missing, item.here.sentence]
        .filter((sentence): sentence is string => Boolean(sentence))
        .map((sentence) => (
          <Typography key={sentence} variant="caption" color="text.secondary" sx={{ lineHeight: 1.35 }}>{sentence}</Typography>
        ))}
      {item.ready && (
        <Button size="small" variant="text" href={`#/${item.setting.address}`}
          onClick={(event) => { if (onNavigate) { event.preventDefault(); onNavigate(item.setting.address as AdminDestination); } }}
          aria-label={`Turn on ${item.name} in ${item.setting.label}`} sx={{ alignSelf: 'flex-start', px: 0.5, minWidth: 0 }}>
          Turn on
        </Button>
      )}
    </Paper>
  );
}

function count(data: Capabilities, filter: CapabilityFilterKey | null): number {
  return data.outcomes.reduce((total, outcome) => total + outcome.capabilities
    .filter((item) => !filter || item.filters.includes(filter)).length, 0);
}

export default function CapabilityList({ data, show, onNavigate }: {
  data: Capabilities; show: CapabilityFilterKey | null; onNavigate?: Navigate;
}) {
  const labels = new Map(data.filters.map((filter) => [filter.key, filter.label]));
  const outcomes = data.outcomes
    .map((outcome) => ({ ...outcome, capabilities: outcome.capabilities.filter((item) => !show || item.filters.includes(show)) }))
    .filter((outcome) => outcome.capabilities.length > 0);
  const chosen = show ? data.filters.find((filter) => filter.key === show) : undefined;
  return (
    <Box>
      <Box component="nav" aria-label="Show capabilities" sx={{ display: 'flex', flexWrap: 'wrap', gap: 1, mb: 1 }}>
        {[null, ...CAPABILITY_FILTERS].map((filter) => {
          const selected = filter === show;
          const address = capabilitiesAddress(filter);
          return (
            <Chip key={filter ?? 'all'} component="a" href={`#/${address}`} clickable
              label={`${filter ? labels.get(filter) : 'All'} (${count(data, filter)})`}
              color={selected ? 'primary' : 'default'} variant={selected ? 'filled' : 'outlined'}
              aria-current={selected ? 'page' : undefined} data-testid={`capability-filter-${filter ?? 'all'}`}
              onClick={(event: MouseEvent) => { if (onNavigate) { event.preventDefault(); onNavigate(address as AdminDestination); } }} />
          );
        })}
      </Box>
      {chosen && <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>{chosen.sentence}</Typography>}
      <Stack direction={{ xs: 'column', md: 'row' }} spacing={{ xs: 0.5, md: 2 }} sx={{ mb: 3, flexWrap: 'wrap' }}
        data-testid="capabilities-legend">
        {[...data.legend, ...data.filters.filter((filter) => ['ready', 'needs', 'experimental'].includes(filter.key))]
          .map((entry) => (
            <Typography key={entry.label} variant="caption" color="text.secondary">
              <Box component="span" fontWeight="bold" color="text.primary">{entry.label}:</Box> {entry.sentence}
            </Typography>
          ))}
      </Stack>
      {outcomes.length === 0 && (
        <Typography variant="body1" data-testid="capabilities-none">Nothing matches {chosen?.label}.</Typography>
      )}
      <Stack spacing={4}>
        {outcomes.map((outcome) => (
          <Box key={outcome.key} component="section" aria-labelledby={`capability-outcome-${outcome.key}`}
            data-testid={`capability-outcome-${outcome.key}`}>
            <Typography id={`capability-outcome-${outcome.key}`} variant="h5" component="h2">{outcome.title}</Typography>
            <Typography variant="body2" color="text.secondary" sx={{ mb: 1.5 }}>{outcome.sentence}</Typography>
            <Box sx={{ display: 'grid', gap: 1.5, alignItems: 'start',
              gridTemplateColumns: { xs: 'minmax(0, 1fr)', sm: 'repeat(2, minmax(0, 1fr))', lg: 'repeat(3, minmax(0, 1fr))' } }}>
              {outcome.capabilities.map((item) => <CapabilityCard key={item.key} item={item} onNavigate={onNavigate} />)}
            </Box>
          </Box>
        ))}
      </Stack>
    </Box>
  );
}
