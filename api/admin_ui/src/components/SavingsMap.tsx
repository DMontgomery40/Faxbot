// Overview → How Faxbot saves money: every way Faxbot saves money, drawn along the path a fax takes.
// The stages run left to right (top to bottom on a phone), each with its mechanisms as small cards; the advice
// that saves money once you act on it sits below the path. A card shows three statuses at a glance (On or Off,
// Works here or Not here, and how it is tested) with the reason where a status is negative. Selecting a card
// opens its own page in Savings & optimization → Capabilities, which links on to its figures; the map never shows
// money. Every sentence comes from the server (GET /routing/savings/mechanisms), and `faxbot savings mechanisms`
// prints the same ones (__tests__/savingsMap.json). Given the capabilities read, Turn on leads to each setting's
// page under its six-area name.
import { Box, Button, Chip, Paper, Stack, Typography } from '@mui/material';
import {
  ArrowDownward as ArrowDownIcon,
  ArrowForward as ArrowRightIcon,
  Science as TestedIcon,
} from '@mui/icons-material';
import type { SavingsMechanism, SavingsMechanisms } from '../api/deliveryTypes';
import type { Capabilities, Capability } from '../api/capabilityTypes';
import type { AdminDestination } from '../navigation';

const STATUS_CHIP = { size: 'small', variant: 'outlined', sx: { height: 22, fontSize: '0.75rem' } } as const;

type Navigate = (destination: AdminDestination) => void;

// On a wide screen a stage with many mechanisms takes two columns (three on the widest), so no stage runs far
// below the others.
function span(count: number, wide: boolean): number {
  if (count <= 6) return 1;
  return wide ? 3 : 2;
}

// How far each mechanism is proven, at a glance; the legend says what each means.
const TESTED_COLOR = { live: 'info', lab: 'secondary', built: 'default' } as const;

// Each mechanism's own page in Capabilities.
export function capabilityAddress(key: string): AdminDestination {
  return `savings/capabilities?key=${key}`;
}

function MechanismCard({ item, capability, onNavigate }: {
  item: SavingsMechanism; capability?: Capability; onNavigate?: Navigate;
}) {
  const open = onNavigate ? () => onNavigate(capabilityAddress(item.key)) : undefined;
  // Where Turn on leads: the setting's page as Capabilities names it, else the catalogue's own page.
  const setting = capability
    ? { address: capability.setting.address, label: capability.setting.label }
    : { address: item.page, label: item.page_label };
  const reasons = [item.enabled.sentence, item.works.sentence].filter((sentence): sentence is string => Boolean(sentence));
  return (
    <Paper variant="outlined" data-testid={`savings-map-${item.key}`}
      role={open ? 'link' : undefined} tabIndex={open ? 0 : undefined}
      aria-label={open ? `${item.name}: open it in Capabilities` : undefined}
      onClick={open}
      onKeyDown={open ? (event) => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); open(); } } : undefined}
      sx={{
        p: 1.5, borderRadius: 2, display: 'flex', flexDirection: 'column', gap: 0.75, minWidth: 0,
        ...(open ? {
          cursor: 'pointer', transition: 'all 0.2s ease-in-out',
          '&:hover, &:focus-visible': { borderColor: 'primary.main', backgroundColor: 'rgba(59, 160, 255, 0.08)' },
        } : {}),
      }}>
      <Typography variant="subtitle2" component="h4" sx={{ lineHeight: 1.3, overflowWrap: 'anywhere' }}>{item.name}</Typography>
      <Box display="flex" flexWrap="wrap" gap={0.5}>
        <Chip {...STATUS_CHIP} label={item.enabled.label} color={item.enabled.on ? 'success' : 'default'} />
        <Chip {...STATUS_CHIP} label={item.works.label} color={item.works.here ? 'success' : 'warning'} />
        <Chip {...STATUS_CHIP} label={item.evidence.label} color={TESTED_COLOR[item.evidence.level]}
          icon={<TestedIcon />} data-testid={`savings-map-tested-${item.key}`}
          sx={{ ...STATUS_CHIP.sx, '& .MuiChip-icon': { fontSize: 14 } }} />
      </Box>
      {/* The reasons a status is negative are the useful text; then one short line about this installation. */}
      {reasons.map((sentence) => (
        <Typography key={sentence} variant="caption" color="text.secondary" sx={{ lineHeight: 1.35 }}>{sentence}</Typography>
      ))}
      {item.here.sentence && (
        <Typography variant="caption" sx={{ lineHeight: 1.35 }}>{item.here.sentence}</Typography>
      )}
      {item.turn_on && onNavigate && (
        <Button size="small" variant="text" sx={{ alignSelf: 'flex-start', px: 0.5, minWidth: 0 }}
          onClick={(event) => { event.stopPropagation(); onNavigate(setting.address as AdminDestination); }}
          onKeyDown={(event) => event.stopPropagation()}
          aria-label={`Turn on ${item.name} in ${setting.label}`}>
          Turn on
        </Button>
      )}
    </Paper>
  );
}

function StageTitle({ number, title }: { number: number; title: string }) {
  return (
    <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, px: 1.5, py: 1, borderRadius: 2,
      bgcolor: 'action.selected', border: 1, borderColor: 'divider' }}>
      <Box component="span" aria-hidden sx={{ flex: '0 0 auto', width: 22, height: 22, borderRadius: '50%', display: 'grid',
        placeItems: 'center', bgcolor: 'primary.main', color: 'primary.contrastText', fontSize: '0.75rem', fontWeight: 700 }}>
        {number}
      </Box>
      <Typography variant="subtitle2" component="h3" sx={{ lineHeight: 1.2 }}>{title}</Typography>
    </Box>
  );
}

export default function SavingsMap({ data, capabilities, onNavigate }: {
  data: SavingsMechanisms;
  // GET /routing/capabilities, when the page has it: its setting addresses and names for Turn on.
  capabilities?: Capabilities | null;
  onNavigate?: Navigate;
}) {
  const byKey = new Map((capabilities?.outcomes ?? []).flatMap((outcome) => outcome.capabilities)
    .map((capability) => [capability.key, capability]));
  const path = data.stages.filter((stage) => stage.path && stage.mechanisms.length > 0);
  const beside = data.stages.filter((stage) => !stage.path && stage.mechanisms.length > 0);
  if (path.length === 0 && beside.length === 0) return null;
  return (
    <Box component="section" aria-labelledby="savings-map-title" data-testid="savings-map" sx={{ mt: { xs: 3, md: 4 } }}>
      <Typography id="savings-map-title" variant="h5" component="h2" gutterBottom>{data.title}</Typography>
      <Typography variant="body2" color="text.secondary">
        {data.sentence} Select one to see what it needs and what it did here.
      </Typography>
      <Stack direction={{ xs: 'column', md: 'row' }} spacing={{ xs: 0.5, md: 2 }} sx={{ mt: 1.5, mb: 2 }}
        data-testid="savings-map-legend">
        {data.legend.map((entry) => (
          <Typography key={entry.label} variant="caption" color="text.secondary">
            <Box component="span" fontWeight="bold" color="text.primary">{entry.label}:</Box> {entry.sentence}
          </Typography>
        ))}
      </Stack>
      <Box sx={{ display: 'grid', gap: { xs: 0, md: 2 },
        gridTemplateColumns: { xs: 'minmax(0, 1fr)', md: `repeat(${path.length}, minmax(0, 1fr))`,
          lg: path.map((stage) => `minmax(0, ${span(stage.mechanisms.length, false)}fr)`).join(' '),
          xl: path.map((stage) => `minmax(0, ${span(stage.mechanisms.length, true)}fr)`).join(' ') } }}>
        {path.map((stage, index) => {
          const last = index === path.length - 1;
          const count = stage.mechanisms.length;
          return (
            <Box key={stage.key} data-testid={`savings-map-stage-${stage.key}`} sx={{ minWidth: 0 }}>
              <Box sx={{ position: 'relative', mb: 1.5 }}>
                <StageTitle number={index + 1} title={stage.title} />
                {!last && (
                  // The path goes on to the next stage: to the right on a wide screen.
                  <ArrowRightIcon aria-hidden fontSize="small" color="action"
                    sx={{ display: { xs: 'none', md: 'block' }, position: 'absolute', top: '50%', right: -18,
                      transform: 'translateY(-50%)' }} />
                )}
              </Box>
              <Box sx={{ display: 'grid', gap: 1, alignItems: 'start',
                gridTemplateColumns: { xs: 'minmax(0, 1fr)', lg: `repeat(${span(count, false)}, minmax(0, 1fr))`,
                  xl: `repeat(${span(count, true)}, minmax(0, 1fr))` } }}>
                {stage.mechanisms.map((item) => <MechanismCard key={item.key} item={item} capability={byKey.get(item.key)} onNavigate={onNavigate} />)}
              </Box>
              {!last && (
                // And downwards on a narrow one.
                <Box sx={{ display: { xs: 'flex', md: 'none' }, justifyContent: 'center', py: 1 }}>
                  <ArrowDownIcon aria-hidden color="action" />
                </Box>
              )}
            </Box>
          );
        })}
      </Box>
      {beside.map((stage) => (
        <Box key={stage.key} data-testid={`savings-map-stage-${stage.key}`} sx={{ mt: 3 }}>
          <Box sx={{ mb: 1.5, maxWidth: { md: 360 } }}><StageTitle number={path.length + 1} title={stage.title} /></Box>
          {stage.sentence && (
            <Typography variant="body2" color="text.secondary" sx={{ mb: 1.5 }}>{stage.sentence}</Typography>
          )}
          <Box sx={{ display: 'grid', gap: 1,
            gridTemplateColumns: { xs: 'minmax(0, 1fr)', sm: 'repeat(2, minmax(0, 1fr))', md: 'repeat(3, minmax(0, 1fr))',
              lg: 'repeat(4, minmax(0, 1fr))' } }}>
            {stage.mechanisms.map((item) => <MechanismCard key={item.key} item={item} capability={byKey.get(item.key)} onNavigate={onNavigate} />)}
          </Box>
        </Box>
      ))}
    </Box>
  );
}
