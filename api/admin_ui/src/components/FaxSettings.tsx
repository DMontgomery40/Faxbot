import {
  Accordion,
  AccordionDetails,
  AccordionSummary,
  FormControl,
  FormControlLabel,
  InputLabel,
  Link,
  MenuItem,
  Select,
  Stack,
  Switch,
  TextField,
  Typography,
} from '@mui/material';
import ExpandMoreIcon from '@mui/icons-material/ExpandMore';
import type { SipTrunkSettings as TrunkValues } from '../api/sipTypes';

// The trunk page's collapsed "Fax settings": the options other fax servers offer, for both of
// Faxbot's fax engines. Each has one plain line and starts at the recommended value; the trunk
// form saves them with its other fields (sip_t38_error_correction, sip_t38_max_datagram,
// sip_fax_max_rate, sip_fax_ecm, sip_fax_compression, sip_fax_fine, sip_fax_tune_coding, sip_sslfax_enabled,
// sip_fax_lines, sip_sslfax_listener_port, sip_trunk_max_calls, sip_trunk_calls_per_second).

interface FaxSettingsProps {
  form: TrunkValues;
  update: <K extends keyof TrunkValues>(key: K, value: TrunkValues[K]) => void;
}

const Hint = ({ children }: { children: string }) => (
  <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5 }}>{children}</Typography>
);

// A source's read date ('2026-10-06' is a calendar day) in the reader's own words.
function readDay(day: string): string {
  const [year, month, date] = day.split('-').map(Number);
  if (!year || !month || !date) return day;
  return new Date(year, month - 1, date).toLocaleDateString(undefined, { day: 'numeric', month: 'long', year: 'numeric' });
}

// One sentence each for the trunk's capacity: what the number does and what 0 means here.
export function callsAtOnceHint(form: TrunkValues): string {
  return 'The most faxes Faxbot sends or receives at the same time on your phone line. Others wait their turn. '
    + `Leave 0 to use the number of fax lines (${form.fax_lines ?? 2}).`;
}

export function callsPerSecondHint(form: TrunkValues): string {
  const limit = form.carrier_limits?.calls_per_second;
  const most = 'The most fax calls Faxbot starts in one second on your phone line. Others wait a moment.';
  return limit ? `${most} Leave 0 to use ${limit}, the most your carrier takes at no extra cost.`
    : `${most} Leave 0 for no limit.`;
}

export default function FaxSettings({ form, update }: FaxSettingsProps) {
  const number = (key: 't38_max_datagram' | 'fax_lines' | 'sslfax_listener_port' | 'max_calls' | 'calls_per_second',
    fallback: number) =>
    (event: React.ChangeEvent<HTMLInputElement>) => {
      const value = Number.parseInt(event.target.value, 10);
      update(key, Number.isFinite(value) ? value : fallback);
    };
  return (
    <Accordion disableGutters variant="outlined" data-testid="fax-settings">
      <AccordionSummary expandIcon={<ExpandMoreIcon />}>
        <Typography variant="subtitle2">Fax settings</Typography>
      </AccordionSummary>
      <AccordionDetails>
        <Stack spacing={2.5}>
          <Typography variant="body2" color="text.secondary">
            The recommended values suit almost every fax machine. Change one only for a fax that keeps failing.
          </Typography>

          <FormControl fullWidth size="small">
            <InputLabel id="fax-speed-label">Highest speed</InputLabel>
            <Select labelId="fax-speed-label" label="Highest speed" value={form.fax_max_rate ?? 14400}
              onChange={(event) => update('fax_max_rate', Number(event.target.value) as TrunkValues['fax_max_rate'])}>
              <MenuItem value={14400}>14,400 bits per second (recommended)</MenuItem>
              <MenuItem value={9600}>9,600 bits per second</MenuItem>
              <MenuItem value={7200}>7,200 bits per second</MenuItem>
              <MenuItem value={4800}>4,800 bits per second</MenuItem>
            </Select>
            <Hint>A lower speed helps with a bad line. Faxes sent as sound never go above 9,600.</Hint>
          </FormControl>

          <div>
            <FormControlLabel label="Error correction (recommended)"
              control={<Switch checked={form.fax_ecm ?? true} onChange={(event) => update('fax_ecm', event.target.checked)} />} />
            <Hint>Faxbot resends any damaged part of a page. Turn it off only for a very old fax machine.</Hint>
          </div>

          <FormControl fullWidth size="small">
            <InputLabel id="fax-compression-label">Page compression</InputLabel>
            <Select labelId="fax-compression-label" label="Page compression" value={form.fax_compression ?? 'jbig'}
              onChange={(event) => update('fax_compression', event.target.value as TrunkValues['fax_compression'])}>
              <MenuItem value="jbig">Every kind, smallest pages first (recommended)</MenuItem>
              <MenuItem value="mmr">Every kind except the newest</MenuItem>
              <MenuItem value="mr">The two simplest kinds</MenuItem>
              <MenuItem value="mh">Only the simplest kind, for very old machines</MenuItem>
            </Select>
            <Hint>Smaller pages arrive sooner. If one fax machine shows broken pages, pick a simpler choice.</Hint>
          </FormControl>

          <div>
            <FormControlLabel label="Fine resolution (recommended)"
              control={<Switch checked={form.fax_fine ?? true} onChange={(event) => update('fax_fine', event.target.checked)} />} />
            <Hint>Sharper text in the faxes you send; off sends half as many lines per page.</Hint>
          </div>

          <div>
            <FormControlLabel label="Make pages smaller without changing them (recommended)"
              control={<Switch checked={form.fax_tune_coding ?? true}
                onChange={(event) => update('fax_tune_coding', event.target.checked)} />} />
            <Hint>
              Faxbot packs each page into fewer bytes, so it arrives sooner; the other fax machine prints exactly the
              same page.
            </Hint>
          </div>

          <div>
            <FormControlLabel label="Send pages faster when the other fax machine can (recommended)"
              control={<Switch checked={form.sslfax_enabled ?? true}
                onChange={(event) => update('sslfax_enabled', event.target.checked)} />} />
            <Hint>
              When the other fax machine supports it, pages go over the internet during the call. They travel
              encrypted, but Faxbot can't confirm who is at the other end, so this is as private as an ordinary
              fax call, not more.
            </Hint>
          </div>

          <Stack direction={{ xs: 'column', sm: 'row' }} spacing={2}>
            <TextField size="small" type="number" label="Fax lines" value={form.fax_lines ?? 2}
              inputProps={{ min: 1, max: 8 }} onChange={number('fax_lines', 2)}
              helperText="How many faxes can be sent or received at the same time." />
            <TextField size="small" type="number" label="Router port for faster faxes" value={form.sslfax_listener_port ?? 10443}
              inputProps={{ min: 1024, max: 65535 }} onChange={number('sslfax_listener_port', 10443)}
              helperText="Used only after you forward this port on your router to this computer." />
          </Stack>

          <Stack direction={{ xs: 'column', sm: 'row' }} spacing={2} data-testid="trunk-capacity">
            <TextField size="small" type="number" label="Calls at once" value={form.max_calls ?? 0}
              inputProps={{ min: 0, max: 200 }} onChange={number('max_calls', 0)}
              helperText={callsAtOnceHint(form)} />
            <TextField size="small" type="number" label="New calls per second" value={form.calls_per_second ?? 0}
              inputProps={{ min: 0, max: 100 }} onChange={number('calls_per_second', 0)}
              helperText={callsPerSecondHint(form)} />
          </Stack>
          {form.carrier_limits && (
            <Typography variant="body2" color="text.secondary" data-testid="carrier-limits">
              {form.carrier_limits.note} Read {readDay(form.carrier_limits.read_on)}:{' '}
              {form.carrier_limits.sources.map((url, index) => (
                <span key={url}>{index > 0 && ', '}<Link href={url} target="_blank" rel="noopener noreferrer">{new URL(url).host}</Link></span>
              ))}.
            </Typography>
          )}

          <FormControl fullWidth size="small">
            <InputLabel id="t38-correction-label">Protection against lost fax data</InputLabel>
            <Select labelId="t38-correction-label" label="Protection against lost fax data"
              value={form.t38_error_correction ?? 'redundancy'}
              onChange={(event) => update('t38_error_correction', event.target.value as TrunkValues['t38_error_correction'])}>
              <MenuItem value="redundancy">Send everything twice (recommended)</MenuItem>
              <MenuItem value="fec">Send correction data</MenuItem>
              <MenuItem value="none">Nothing extra</MenuItem>
            </Select>
            <Hint>Keep the recommended choice unless your carrier tells you to use another.</Hint>
          </FormControl>

          <TextField size="small" type="number" label="Largest fax data size your carrier accepts" value={form.t38_max_datagram ?? 400}
            inputProps={{ min: 100, max: 1400 }} onChange={number('t38_max_datagram', 400)}
            helperText="Keep the recommended 400 unless your carrier tells you to use another." />
        </Stack>
      </AccordionDetails>
    </Accordion>
  );
}
