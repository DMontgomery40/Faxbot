// Providers → the trunk page: copiers that fax over the network (copiers.py). Which makers document SIP fax with
// T.38, what to set on each, and where it was read; the rest are listed as not found so nobody assumes them.
import { useEffect, useState } from 'react';
import { Accordion, AccordionDetails, AccordionSummary, Box, Link, Typography } from '@mui/material';
import ExpandMoreIcon from '@mui/icons-material/ExpandMore';

// A source's read date in the reader's own words ('2026-10-10' is a calendar day, not a moment).
function readOn(day: string): string {
  const [year, month, date] = day.split('-').map(Number);
  if (!year || !month || !date) return day;
  return new Date(year, month - 1, date).toLocaleDateString(undefined, { day: 'numeric', month: 'long', year: 'numeric' });
}

type Call = <T>(request: { method: string; path: string; body?: unknown }) => Promise<T>;

interface CopierItem {
  id: string;
  label: string;
  sip_t38: boolean;
  summary: string;
  steps: string[];
  sources: Array<{ url: string; read_on: string }>;
}

export default function CopiersPanel({ call }: { call: Call }) {
  const [items, setItems] = useState<CopierItem[] | null>(null);
  useEffect(() => {
    let live = true;
    call<{ copiers: CopierItem[] }>({ method: 'GET', path: '/admin/sip/copiers' })
      .then((found) => { if (live) setItems(found.copiers ?? []); })
      .catch(() => { if (live) setItems(null); });
    return () => { live = false; };
  }, [call]);
  if (!items || items.length === 0) return null;
  return (
    <Accordion disableGutters variant="outlined" data-testid="copiers-panel">
      <AccordionSummary expandIcon={<ExpandMoreIcon />}>
        <Typography>Copiers that fax over the network</Typography>
      </AccordionSummary>
      <AccordionDetails>
        <Typography variant="body2" sx={{ mb: 1 }}>
          A copier with an IP fax option can send faxes over your network instead of an analog line. Faxbot does not
          yet take a copier&apos;s call as a fax to send on; these are the settings each maker documents.
        </Typography>
        {items.map((item) => (
          <Box key={item.id} sx={{ mb: 1.5 }}>
            <Typography variant="body2" fontWeight={600}>{item.label}{item.sip_t38 ? '' : ' (no SIP fax found)'}</Typography>
            <Typography variant="body2">{item.summary}</Typography>
            {item.steps.length > 0 && (
              <Box component="ol" sx={{ pl: 3, my: 0.5 }}>
                {item.steps.map((step) => <li key={step}><Typography variant="body2">{step}</Typography></li>)}
              </Box>
            )}
            {item.sources.map((source) => (
              <Typography key={source.url} variant="body2">
                <Link href={source.url} target="_blank" rel="noreferrer">{new URL(source.url).hostname}</Link>
                {`, read ${readOn(source.read_on)}`}
              </Typography>
            ))}
          </Box>
        ))}
      </AccordionDetails>
    </Accordion>
  );
}
