// One capability (savings/capabilities?key=<key>): what it does with an example, how it stands here, what it
// needs (each with whether it is in place, who or what satisfies it and where), where its setting lives, and what it
// did here with a link to its figures. Never money: amounts stay on Savings. Command lines stay in `faxbot savings
// capabilities show`; the console names the page instead (no developer text on operator screens).
import type { ReactNode } from 'react';
import { Box, Button, Chip, Paper, Stack, Typography } from '@mui/material';
import type { Capability, CapabilityOutcome, CapabilityPrerequisite } from '../../api/capabilityTypes';
import type { AdminDestination } from '../../navigation';
import { StateChips } from './CapabilityList';
import { AddressLink, CAPABILITIES_ADDRESS, type Navigate } from './text';

const STATE_COLOR = { in_place: 'success', missing: 'warning', not_needed: 'default', not_checked: 'default' } as const;

function Prerequisite({ prerequisite, index, onNavigate }: {
  prerequisite: CapabilityPrerequisite; index: number; onNavigate?: Navigate;
}) {
  return (
    <Box component="li" data-testid={`capability-prerequisite-${index}`}
      sx={{ display: 'flex', flexDirection: { xs: 'column', sm: 'row' }, gap: 1, alignItems: { sm: 'baseline' } }}>
      <Chip size="small" variant="outlined" label={prerequisite.label} color={STATE_COLOR[prerequisite.state]}
        sx={{ alignSelf: 'flex-start', height: 22, fontSize: '0.75rem' }} />
      <Typography variant="body2">
        <Box component="span" fontWeight="bold">{prerequisite.kind_label}:</Box> {prerequisite.sentence}{' '}
        <AddressLink address={prerequisite.address} onNavigate={onNavigate}>{prerequisite.address_label}</AddressLink>
      </Typography>
    </Box>
  );
}

function Section({ title, children, testId }: { title: string; children: ReactNode; testId: string }) {
  return (
    <Paper variant="outlined" component="section" sx={{ p: 2, borderRadius: 2 }} data-testid={testId}>
      <Typography variant="h6" component="h2" sx={{ mb: 1 }}>{title}</Typography>
      <Stack spacing={1}>{children}</Stack>
    </Paper>
  );
}

export default function CapabilityPage({ item, outcome, onNavigate }: {
  item: Capability; outcome: CapabilityOutcome; onNavigate?: Navigate;
}) {
  // Advice and charge checks keep their home on the page with what they found: that is their figures link.
  const ownSetting = !item.results || item.setting.address !== item.results.address || Boolean(item.setting.command)
    || item.ready;
  return (
    <Box data-testid="capability-page">
      <Typography variant="body2" sx={{ mb: 1 }}>
        <AddressLink address={CAPABILITIES_ADDRESS} onNavigate={onNavigate}>All capabilities</AddressLink>
      </Typography>
      <Typography variant="h4" component="h1">{item.name}</Typography>
      <Box sx={{ my: 1 }}><StateChips item={item} /></Box>
      <Stack spacing={2} sx={{ maxWidth: 900 }}>
        <Box>
          <Typography variant="body1">{item.sentence}</Typography>
          {item.example && (
            <Typography variant="body2" sx={{ mt: 1 }}>
              <Box component="span" fontWeight="bold">For example:</Box> {item.example}
            </Typography>
          )}
          {[item.enabled.sentence, item.works.sentence].filter((sentence): sentence is string => Boolean(sentence))
            .map((sentence) => <Typography key={sentence} variant="body2" color="text.secondary" sx={{ mt: 1 }}>{sentence}</Typography>)}
          <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }}>
            <Box component="span" fontWeight="bold">Helps with:</Box> {outcome.title}
          </Typography>
        </Box>

        <Section title="What it needs" testId="capability-needs">
          {item.prerequisites.length === 0 && (
            <Typography variant="body2">Nothing more than any installation has.</Typography>
          )}
          {item.prerequisites.length > 0 && (
            <Stack component="ul" spacing={1} sx={{ m: 0, p: 0, listStyle: 'none' }}>
              {item.prerequisites.map((prerequisite, index) => (
                <Prerequisite key={`${prerequisite.kind}-${index}`} prerequisite={prerequisite} index={index}
                  onNavigate={onNavigate} />
              ))}
            </Stack>
          )}
        </Section>

        {ownSetting && (
          <Section title="Where to change it" testId="capability-setting">
            <Typography variant="body2">
              Its setting: <AddressLink address={item.setting.address} onNavigate={onNavigate}>{item.setting.label}</AddressLink>
            </Typography>
            {item.ready && (
              <Button variant="contained" size="small" href={`#/${item.setting.address}`} sx={{ alignSelf: 'flex-start' }}
                onClick={(event) => { if (onNavigate) { event.preventDefault(); onNavigate(item.setting.address as AdminDestination); } }}
                aria-label={`Turn on ${item.name} in ${item.setting.label}`}>
                Turn on
              </Button>
            )}
          </Section>
        )}

        <Section title="What it did here" testId="capability-results">
          {item.here.sentence && <Typography variant="body2">On this installation: {item.here.sentence}</Typography>}
          {item.results && (
            <Typography variant="body2">
              Its figures: <AddressLink address={item.results.address} onNavigate={onNavigate}>{item.results.label}</AddressLink>
            </Typography>
          )}
          {item.affected && (
            <Typography variant="body2">
              The faxes it acted on: <AddressLink address={item.affected.address} onNavigate={onNavigate}>{item.affected.label}</AddressLink>
            </Typography>
          )}
        </Section>
      </Stack>
    </Box>
  );
}
