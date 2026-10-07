// The Sending tab: limits first (they always apply when they match), then routing rules read from the
// top (the first that matches chooses the route), then the fixed last row. Each rule reads as a
// sentence, with its on/off switch, its place in the list and how many faxes it matched lately.
import { useState } from 'react';
import {
  Box, Button, Chip, IconButton, Paper, Stack, Switch, Tooltip, Typography,
} from '@mui/material';
import ArrowUpwardIcon from '@mui/icons-material/ArrowUpward';
import ArrowDownwardIcon from '@mui/icons-material/ArrowDownward';
import EditIcon from '@mui/icons-material/Edit';
import DeleteOutlineIcon from '@mui/icons-material/DeleteOutline';
import LockIcon from '@mui/icons-material/Lock';
import AddIcon from '@mui/icons-material/Add';
import type { Choices, Rule, RuleKind, RulesDocument, ScopeKind } from './ProviderRulesApi';
import type { Names } from './ProviderRulesText';
import { AUTOMATIC_ROW, ruleSentence } from './ProviderRulesText';
import ProviderRulesEditor from './ProviderRulesEditor';

function matchesText(count: number | undefined): string | null {
  if (count === undefined) return null;
  if (count === 0) return 'No faxes in the last 30 days';
  return count === 1 ? '1 fax in the last 30 days' : `${count} faxes in the last 30 days`;
}

function RuleRow({ rule, names, matches, position, count, editable, onChange, onMove, onEdit, onRemove }: {
  rule: Rule; names: Names; matches?: number; position: number; count: number; editable: boolean;
  onChange?: (rule: Rule) => void; onMove?: (offset: -1 | 1) => void; onEdit?: () => void; onRemove?: () => void;
}) {
  return (
    <Paper variant="outlined" sx={{ p: 1.5, mb: 1, borderRadius: 2, opacity: rule.on ? 1 : 0.6 }}
      aria-label={rule.name} role="group">
      <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1} alignItems={{ sm: 'center' }}>
        <Box sx={{ flex: 1, minWidth: 0 }}>
          <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap">
            <Typography variant="subtitle2">{rule.name}</Typography>
            {rule.mandatory && (
              <Chip size="small" icon={<LockIcon fontSize="small" />}
                label={editable ? 'Mandatory' : 'Locked: mailbox and workflow rules cannot replace it'} />
            )}
            {!rule.on && <Chip size="small" label="Off" />}
          </Stack>
          <Typography variant="body2">{ruleSentence(rule, names)}</Typography>
          {matchesText(matches) && (
            <Typography variant="caption" color="text.secondary">{matchesText(matches)}</Typography>
          )}
        </Box>
        {editable && (
          <Stack direction="row" spacing={0.5} alignItems="center">
            <Tooltip title={rule.on ? 'Switch off' : 'Switch on'}>
              <Switch checked={rule.on} inputProps={{ 'aria-label': `${rule.name} is on` }}
                onChange={(event) => onChange?.({ ...rule, on: event.target.checked })} />
            </Tooltip>
            <IconButton aria-label={`Move ${rule.name} up`} disabled={position === 0} onClick={() => onMove?.(-1)}>
              <ArrowUpwardIcon fontSize="small" />
            </IconButton>
            <IconButton aria-label={`Move ${rule.name} down`} disabled={position === count - 1} onClick={() => onMove?.(1)}>
              <ArrowDownwardIcon fontSize="small" />
            </IconButton>
            <IconButton aria-label={`Change ${rule.name}`} onClick={onEdit}><EditIcon fontSize="small" /></IconButton>
            <IconButton aria-label={`Remove ${rule.name}`} onClick={onRemove}><DeleteOutlineIcon fontSize="small" /></IconButton>
          </Stack>
        )}
      </Stack>
    </Paper>
  );
}

const SECTIONS: Array<{ kind: RuleKind; title: string; help: string; add: string }> = [
  { kind: 'limits', title: 'Limits',
    help: 'Every limit that matches a fax applies to it. Limits only narrow what a fax may use.', add: 'Add a limit' },
  { kind: 'routes', title: 'Routing rules',
    help: 'Read from the top. The first rule that matches a fax chooses how it is sent.', add: 'Add a routing rule' },
];

// The rules of one document, read-only or editable.
export function RulesList({ document, names, matches, editable, onSave, choices, scopeKind, currency, saving }: {
  document: RulesDocument;
  names: Names;
  matches?: Record<string, number>;
  editable: boolean;
  // Saves the whole document with a sentence for the notice.
  onSave?: (document: RulesDocument, notice: string) => Promise<boolean>;
  choices?: Choices;
  scopeKind: ScopeKind;
  currency?: string;
  saving?: boolean;
}) {
  const [editing, setEditing] = useState<{ kind: RuleKind; rule: Rule | null } | null>(null);
  const update = (kind: RuleKind, rules: Rule[], notice: string) =>
    onSave?.({ ...document, [kind]: rules }, notice) ?? Promise.resolve(false);

  return (
    <Box>
      {SECTIONS.map((section) => {
        const rules = document[section.kind] ?? [];
        return (
          <Box key={section.kind} sx={{ mb: 3 }}>
            <Stack direction="row" alignItems="center" justifyContent="space-between" sx={{ mb: 0.5 }}>
              <Typography variant="h6" component="h2">{section.title}</Typography>
              {editable && (
                <Button size="small" startIcon={<AddIcon />} onClick={() => setEditing({ kind: section.kind, rule: null })}>
                  {section.add}
                </Button>
              )}
            </Stack>
            <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>{section.help}</Typography>
            {rules.length === 0 && (
              <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
                {section.kind === 'limits' ? 'No limits.' : 'No routing rules.'}
              </Typography>
            )}
            {rules.map((rule, index) => (
              <RuleRow key={rule.id} rule={rule} names={names} matches={matches?.[rule.id]} position={index}
                count={rules.length} editable={editable}
                onChange={(changed) => void update(section.kind, rules.map((item) => (item.id === rule.id ? changed : item)),
                  `“${rule.name}” is ${changed.on ? 'on' : 'off'} in your draft.`)}
                onMove={(offset) => {
                  const next = [...rules];
                  [next[index], next[index + offset]] = [next[index + offset], next[index]];
                  void update(section.kind, next, `“${rule.name}” moved ${offset < 0 ? 'up' : 'down'} in your draft.`);
                }}
                onEdit={() => setEditing({ kind: section.kind, rule })}
                onRemove={() => void update(section.kind, rules.filter((item) => item.id !== rule.id),
                  `“${rule.name}” removed from your draft.`)} />
            ))}
            {section.kind === 'routes' && (
              <Paper variant="outlined" sx={{ p: 1.5, borderRadius: 2, bgcolor: 'action.hover' }}>
                <Typography variant="body2">{AUTOMATIC_ROW}</Typography>
              </Paper>
            )}
          </Box>
        );
      })}
      {editable && choices && editing && (
        <ProviderRulesEditor open kind={editing.kind} rule={editing.rule} document={document} choices={choices}
          names={names} scopeKind={scopeKind} currency={currency ?? 'USD'} saving={saving}
          onClose={() => setEditing(null)}
          onSave={(rule) => {
            const rules = document[editing.kind] ?? [];
            const next = editing.rule ? rules.map((item) => (item.id === editing.rule!.id ? rule : item)) : [...rules, rule];
            void update(editing.kind, next, editing.rule ? `“${rule.name}” changed in your draft.` : `“${rule.name}” added to your draft.`)
              .then((saved) => { if (saved) setEditing(null); });
          }} />
      )}
    </Box>
  );
}
