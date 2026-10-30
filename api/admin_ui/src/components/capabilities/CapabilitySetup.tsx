// This checklist writes only tab-local review progress. Canonical editors own all settings.
import { useState } from 'react';
import { Alert, Button, Dialog, DialogActions, DialogContent, DialogTitle, Stack, Typography } from '@mui/material';
import type { Capability } from '../../api/capabilityTypes';
import { AddressLink, type Navigate } from './text';

export default function CapabilitySetup({ item, onNavigate }: { item: Capability; onNavigate?: Navigate }) {
  const guide = item.setup;
  const storageKey = `faxbot.setup.${item.key}.v1`;
  const [open, setOpen] = useState(false);
  const [storageFailed, setStorageFailed] = useState(false);
  const [index, setIndex] = useState(() => {
    try {
      const value = JSON.parse(sessionStorage.getItem(storageKey) ?? '0');
      return Number.isInteger(value) && value >= 0 && value < (guide?.steps.length ?? 0) ? value : 0;
    } catch { return 0; }
  });
  if (!guide) return null;
  const step = guide.steps[index];
  const move = (next: number) => {
    setIndex(next);
    try { sessionStorage.setItem(storageKey, JSON.stringify(next)); }
    catch { setStorageFailed(true); }
  };
  return <>
    <Button onClick={() => setOpen(true)}>{index ? 'Resume guided setup' : 'Guided setup'}</Button>
    <Dialog open={open} onClose={() => setOpen(false)} fullWidth maxWidth="md" aria-labelledby="setup-title">
      <DialogTitle id="setup-title">{index + 1} of {guide.steps.length}: {step.title}</DialogTitle>
      <DialogContent>
        <Stack spacing={2}>
          <Alert severity="info">Saved review progress does not verify or enable this capability. Current settings are read when you open the capability page.</Alert>
          {storageFailed && <Alert severity="warning">Review progress could not be kept. Keep this guide open or start again next time.</Alert>}
          <Typography>{item.sentence}</Typography>
          <Typography>{item.enabled.label} · {item.works.label} · {item.evidence.label}</Typography>
          {item.example && <Typography>For example: {item.example}</Typography>}
          <Typography>{step.sentence}</Typography>
          {step.key === 'prerequisites' && item.prerequisites.map((entry, n) => <Typography key={n}>
            {entry.label}: {entry.sentence}{' '}
            <AddressLink address={entry.address} onNavigate={onNavigate}>{entry.address_label}</AddressLink>
          </Typography>)}
          {step.address && step.address !== item.setting.address && <AddressLink address={step.address} onNavigate={onNavigate}>Open {step.title.toLowerCase()} settings</AddressLink>}
          <AddressLink address={item.setting.address} onNavigate={onNavigate}>Open direct settings</AddressLink>
          <Typography fontWeight="bold">Saved route state (read now)</Typography>
          {guide.routes.length === 0 && <Typography>No delivery routes are configured.</Typography>}
          {guide.routes.map((route) => <Typography key={route.route}>{route.label}: {route.long_pages ? 'On' : 'Off'} — {route.sentence}</Typography>)}
          <Typography>{guide.cancel}</Typography>
          {step.key === 'status' && <Typography>{item.here.sentence}</Typography>}
        </Stack>
      </DialogContent>
      <DialogActions>
        <Button onClick={() => setOpen(false)}>Leave setup</Button>
        <Button disabled={index === 0} onClick={() => move(index - 1)}>Back</Button>
        {index < guide.steps.length - 1
          ? <Button onClick={() => move(index + 1)}>Next</Button>
          : <Button onClick={() => { move(0); setOpen(false); }}>Return to capability status</Button>}
      </DialogActions>
    </Dialog>
  </>;
}
