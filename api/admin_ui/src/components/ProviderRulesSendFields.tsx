// Send a fax: "Send from mailbox", "Workflow" and "Labels", which rules can match. Each appears only when the
// organization uses it: mailboxes this person may send from, workflows and labels defined in the rules.
import {
  Checkbox, FormControl, FormControlLabel, FormGroup, FormLabel, InputLabel, MenuItem, Select, Stack,
} from '@mui/material';

export interface SendOptions { mailbox: string | null; workflow: string | null; labels: string[] }
export const NO_SEND_OPTIONS: SendOptions = { mailbox: null, workflow: null, labels: [] };

export interface SendChoices {
  mailboxes: Array<{ id: string; label: string }>;
  workflows: Array<{ key: string; name: string }>;
  labels: string[];
}

// The fields of POST /fax for these choices, leaving out what was not chosen.
export function sendBody(value: SendOptions): Record<string, unknown> {
  return {
    ...(value.mailbox ? { mailbox: value.mailbox } : {}),
    ...(value.workflow ? { workflow: value.workflow } : {}),
    ...(value.labels.length > 0 ? { labels: value.labels } : {}),
  };
}

export default function ProviderRulesSendFields({ choices, value, onChange, disabled }: {
  choices: SendChoices; value: SendOptions; onChange: (value: SendOptions) => void; disabled?: boolean;
}) {
  if (choices.mailboxes.length === 0 && choices.workflows.length === 0 && choices.labels.length === 0) return null;
  return (
    <Stack spacing={2}>
      {choices.mailboxes.length > 0 && (
        <FormControl size="small" fullWidth disabled={disabled}>
          <InputLabel id="send-from-mailbox">Send from mailbox</InputLabel>
          <Select labelId="send-from-mailbox" label="Send from mailbox" value={value.mailbox ?? ''}
            inputProps={{ 'aria-label': 'Send from mailbox' }}
            onChange={(event) => onChange({ ...value, mailbox: event.target.value || null })}>
            <MenuItem value="">No mailbox</MenuItem>
            {choices.mailboxes.map((mailbox) => <MenuItem key={mailbox.id} value={mailbox.id}>{mailbox.label}</MenuItem>)}
          </Select>
        </FormControl>
      )}
      {choices.workflows.length > 0 && (
        <FormControl size="small" fullWidth disabled={disabled}>
          <InputLabel id="send-workflow">Workflow</InputLabel>
          <Select labelId="send-workflow" label="Workflow" value={value.workflow ?? ''} inputProps={{ 'aria-label': 'Workflow' }}
            onChange={(event) => onChange({ ...value, workflow: event.target.value || null })}>
            <MenuItem value="">None</MenuItem>
            {choices.workflows.map((workflow) => <MenuItem key={workflow.key} value={workflow.key}>{workflow.name}</MenuItem>)}
          </Select>
        </FormControl>
      )}
      {choices.labels.length > 0 && (
        <FormControl component="fieldset" disabled={disabled}>
          <FormLabel component="legend">Labels</FormLabel>
          <FormGroup row>
            {choices.labels.map((label) => (
              <FormControlLabel key={label} label={label} control={(
                <Checkbox checked={value.labels.includes(label)} onChange={(event) => onChange({
                  ...value, labels: event.target.checked ? [...value.labels, label] : value.labels.filter((item) => item !== label),
                })} />
              )} />
            ))}
          </FormGroup>
        </FormControl>
      )}
    </Stack>
  );
}
