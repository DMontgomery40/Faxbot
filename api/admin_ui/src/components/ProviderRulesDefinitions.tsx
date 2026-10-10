// The names rules use, kept in the organization's rules so they are versioned with them: sites and
// regions, recipient groups and labels, and workflows. Each change goes into the draft like a rule.
import { useEffect, useState, type ReactNode } from 'react';
import {
  Autocomplete, Box, Button, Chip, Dialog, DialogActions, DialogContent, DialogTitle, IconButton, Paper, Stack,
  Table, TableBody, TableCell, TableHead, TableRow, TextField, Typography,
} from '@mui/material';
import AddIcon from '@mui/icons-material/Add';
import EditIcon from '@mui/icons-material/Edit';
import DeleteOutlineIcon from '@mui/icons-material/DeleteOutline';
import { countryName } from './common/numbers';
import { timeZones } from './common/TimeZoneField';
import type { Choices, RecipientList, Region, RulesDocument, Site, Workflow } from './ProviderRulesApi';
import { documentLabels, recipientLists, withLists } from './ProviderRulesApi';

type Save = (document: RulesDocument, notice: string) => Promise<boolean>;

const splitList = (text: string) => text.split(/[,\n]/).map((item) => item.trim()).filter(Boolean);

// A short key from a name, unique among the keys already used.
export function keyFor(name: string, taken: Iterable<string>): string {
  const used = new Set(taken);
  const base = name.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 32).replace(/-+$/, '') || 'item';
  let candidate = base;
  for (let count = 2; used.has(candidate); count += 1) candidate = `${base}-${count}`;
  return candidate;
}

function Section({ title, help, addLabel, onAdd, editable, children }: {
  title: string; help: string; addLabel: string; onAdd: () => void; editable: boolean; children: ReactNode;
}) {
  return (
    <Box sx={{ mb: 4 }}>
      <Stack direction="row" alignItems="center" justifyContent="space-between">
        <Typography variant="h6" component="h2">{title}</Typography>
        {editable && <Button size="small" startIcon={<AddIcon />} onClick={onAdd}>{addLabel}</Button>}
      </Stack>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>{help}</Typography>
      {children}
    </Box>
  );
}

function RowActions({ name, editable, onEdit, onRemove }: { name: string; editable: boolean; onEdit: () => void; onRemove: () => void }) {
  if (!editable) return null;
  return (
    <TableCell align="right" sx={{ whiteSpace: 'nowrap' }}>
      <IconButton size="small" aria-label={`Change ${name}`} onClick={onEdit}><EditIcon fontSize="small" /></IconButton>
      <IconButton size="small" aria-label={`Remove ${name}`} onClick={onRemove}><DeleteOutlineIcon fontSize="small" /></IconButton>
    </TableCell>
  );
}

function NamesPicker({ label, options, value, onChange }: {
  label: string; options: Array<{ id: string; name: string }>; value: string[]; onChange: (value: string[]) => void;
}) {
  const chosen = options.filter((option) => value.includes(option.id));
  return (
    <Autocomplete multiple options={options} value={chosen} getOptionLabel={(option) => option.name}
      isOptionEqualToValue={(option, item) => option.id === item.id}
      onChange={(_, next) => onChange(next.map((option) => option.id))}
      renderInput={(params) => <TextField {...params} size="small" label={label} />} />
  );
}

function EditDialog({ title, open, onClose, onSave, valid, children }: {
  title: string; open: boolean; onClose: () => void; onSave: () => void; valid: boolean; children: ReactNode;
}) {
  return (
    <Dialog open={open} onClose={onClose} fullWidth maxWidth="sm" aria-labelledby="definition-dialog-title">
      <DialogTitle id="definition-dialog-title">{title}</DialogTitle>
      <DialogContent><Stack spacing={2} sx={{ mt: 1 }}>{children}</Stack></DialogContent>
      <DialogActions>
        <Button onClick={onClose}>Cancel</Button>
        <Button variant="contained" disabled={!valid} onClick={onSave}>Save to draft</Button>
      </DialogActions>
    </Dialog>
  );
}

const nameOf = (items: Array<{ id: string; name: string }>, id: string) => items.find((item) => item.id === id)?.name ?? 'an unknown name';

// A site's accounts: those it lists, then those whose own site names it.
export function siteAccounts(site: Site, choices: Choices): string[] {
  const label = (key: string) => choices.accounts.find((account) => account.key === key)?.label ?? key;
  const listed = site.accounts ?? [];
  const own = choices.accounts.filter((account) => account.site === site.key && !listed.includes(account.key)).map((account) => account.key);
  return [...listed, ...own].map(label);
}

// -- sites and regions -----------------------------------------------------------------------------

export function SitesAndRegions({ document, choices, editable, onSave }: {
  document: RulesDocument; choices: Choices; editable: boolean; onSave: Save;
}) {
  const [site, setSite] = useState<{ original: Site | null; value: Site } | null>(null);
  const [region, setRegion] = useState<{ key: string | null; value: Region; countries: string; prefixes: string } | null>(null);
  const sites = document.sites ?? [];
  const regions = Object.entries(document.regions ?? {});
  const zones = timeZones();

  const saveSite = () => {
    if (!site) return;
    const value = { ...site.value, country: site.value.country?.toUpperCase() || undefined };
    const next = site.original ? sites.map((item) => (item.key === site.original!.key ? value : item))
      : [...sites, { ...value, key: keyFor(value.name, sites.map((item) => item.key)) }];
    void onSave({ ...document, sites: next }, `Site “${value.name}” saved in your draft.`).then((done) => { if (done) setSite(null); });
  };
  const saveRegion = () => {
    if (!region) return;
    const key = region.key ?? keyFor(region.value.name, Object.keys(document.regions ?? {}));
    const value: Region = { name: region.value.name, countries: splitList(region.countries).map((code) => code.toUpperCase()),
      prefixes: splitList(region.prefixes) };
    void onSave({ ...document, regions: { ...(document.regions ?? {}), [key]: value } },
      `Region “${value.name}” saved in your draft.`).then((done) => { if (done) setRegion(null); });
  };

  return (
    <Box>
      <Section title="Sites" editable={editable} addLabel="Add a site"
        help="The places your organization sends from. A fax's site comes from its sending mailbox, else from the sender's groups. Give an account its site on Providers → In use."
        onAdd={() => setSite({ original: null, value: { key: '', name: '', mailboxes: [], groups: [] } })}>
        {sites.length === 0 ? <Typography variant="body2" color="text.secondary">No sites yet.</Typography> : (
          <Paper variant="outlined" sx={{ borderRadius: 2, overflowX: 'auto' }}>
            <Table size="small" aria-label="Sites">
              <TableHead><TableRow><TableCell>Site</TableCell><TableCell>Country</TableCell><TableCell>Time zone</TableCell>
                <TableCell>Mailboxes</TableCell><TableCell>Groups</TableCell><TableCell>Accounts</TableCell>{editable && <TableCell />}</TableRow></TableHead>
              <TableBody>
                {sites.map((item) => (
                  <TableRow key={item.key}>
                    <TableCell>{item.name}</TableCell>
                    <TableCell>{item.country ? countryName(item.country) : '-'}</TableCell>
                    <TableCell>{item.time_zone || "Faxbot's time zone"}</TableCell>
                    <TableCell>{(item.mailboxes ?? []).map((id) => nameOf(choices.mailboxes, id)).join(', ') || '-'}</TableCell>
                    <TableCell>{(item.groups ?? []).map((id) => nameOf(choices.groups, id)).join(', ') || '-'}</TableCell>
                    <TableCell>{siteAccounts(item, choices).join(', ') || '-'}</TableCell>
                    <RowActions name={item.name} editable={editable} onEdit={() => setSite({ original: item, value: { ...item } })}
                      onRemove={() => void onSave({ ...document, sites: sites.filter((other) => other.key !== item.key) },
                        `Site “${item.name}” removed from your draft.`)} />
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </Paper>
        )}
      </Section>
      <Section title="Regions" editable={editable} addLabel="Add a region"
        help="Named sets of countries and number prefixes, such as Northern England: +44113, +44114, +44161."
        onAdd={() => setRegion({ key: null, value: { name: '' }, countries: '', prefixes: '' })}>
        {regions.length === 0 ? <Typography variant="body2" color="text.secondary">No regions yet.</Typography> : (
          <Paper variant="outlined" sx={{ borderRadius: 2, overflowX: 'auto' }}>
            <Table size="small" aria-label="Regions">
              <TableHead><TableRow><TableCell>Region</TableCell><TableCell>Countries</TableCell><TableCell>Numbers starting with</TableCell>{editable && <TableCell />}</TableRow></TableHead>
              <TableBody>
                {regions.map(([key, item]) => (
                  <TableRow key={key}>
                    <TableCell>{item.name}</TableCell>
                    <TableCell>{(item.countries ?? []).map(countryName).join(', ') || '-'}</TableCell>
                    <TableCell>{(item.prefixes ?? []).join(', ') || '-'}</TableCell>
                    <RowActions name={item.name} editable={editable}
                      onEdit={() => setRegion({ key, value: item, countries: (item.countries ?? []).join(', '), prefixes: (item.prefixes ?? []).join(', ') })}
                      onRemove={() => {
                        const next = { ...(document.regions ?? {}) };
                        delete next[key];
                        void onSave({ ...document, regions: next }, `Region “${item.name}” removed from your draft.`);
                      }} />
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </Paper>
        )}
      </Section>
      {site && (
        <EditDialog open title={site.original ? `Change ${site.original.name}` : 'Add a site'} valid={site.value.name.trim() !== ''}
          onClose={() => setSite(null)} onSave={saveSite}>
          <TextField size="small" label="Name" value={site.value.name} placeholder="Leeds office"
            onChange={(event) => setSite({ ...site, value: { ...site.value, name: event.target.value } })} />
          <TextField size="small" label="Country code" value={site.value.country ?? ''} placeholder="GB"
            helperText={site.value.country ? countryName(site.value.country.toUpperCase()) : 'Two letters, such as GB.'}
            onChange={(event) => setSite({ ...site, value: { ...site.value, country: event.target.value } })} />
          {(site.value.country ?? '').toUpperCase() === 'US' && (
            <TextField size="small" label="State" value={site.value.state ?? ''} placeholder="CO"
              helperText="Two letters. Some carriers charge differently for calls within one state, so Faxbot prices each call from its site's state."
              inputProps={{ maxLength: 2 }}
              onChange={(event) => setSite({ ...site, value: { ...site.value,
                state: event.target.value.toUpperCase() || undefined } })} />
          )}
          {(site.value.country ?? '').toUpperCase() === 'FR' && (
            <TextField size="small" label="Commune code (INSEE)" value={site.value.commune ?? ''} placeholder="75056"
              helperText="Five characters, such as 75056 for Paris. Faxbot shows when copper, and the phone lines on it, close there."
              inputProps={{ maxLength: 5 }}
              onChange={(event) => setSite({ ...site, value: { ...site.value,
                commune: event.target.value.toUpperCase().trim() || undefined } })} />
          )}
          <Autocomplete options={zones} value={site.value.time_zone || null}
            onChange={(_, zone) => setSite({ ...site, value: { ...site.value, time_zone: zone ?? undefined } })}
            renderInput={(params) => <TextField {...params} size="small" label="Time zone"
              helperText="Rules about times of day can use this site's clock." />} />
          <NamesPicker label="Mailboxes that send from this site" options={choices.mailboxes} value={site.value.mailboxes ?? []}
            onChange={(mailboxes) => setSite({ ...site, value: { ...site.value, mailboxes } })} />
          <NamesPicker label="Groups that send from this site" options={choices.groups} value={site.value.groups ?? []}
            onChange={(groups) => setSite({ ...site, value: { ...site.value, groups } })} />
          <NamesPicker label="Accounts its calls start from"
            options={choices.accounts.map((account) => ({ id: account.key, name: account.label }))} value={site.value.accounts ?? []}
            onChange={(accounts) => setSite({ ...site, value: { ...site.value, accounts } })} />
        </EditDialog>
      )}
      {region && (
        <EditDialog open title={region.key ? `Change ${region.value.name}` : 'Add a region'}
          valid={region.value.name.trim() !== '' && (splitList(region.countries).length + splitList(region.prefixes).length) > 0}
          onClose={() => setRegion(null)} onSave={saveRegion}>
          <TextField size="small" label="Name" value={region.value.name} placeholder="Northern England"
            onChange={(event) => setRegion({ ...region, value: { ...region.value, name: event.target.value } })} />
          <TextField size="small" label="Country codes" value={region.countries} placeholder="GB, IE"
            helperText={splitList(region.countries).map((code) => countryName(code.toUpperCase())).join(', ') || 'Separate several with commas.'}
            onChange={(event) => setRegion({ ...region, countries: event.target.value })} />
          <TextField size="small" label="Numbers starting with" value={region.prefixes} placeholder="+44113, +44114"
            helperText="Separate several with commas." onChange={(event) => setRegion({ ...region, prefixes: event.target.value })} />
        </EditDialog>
      )}
    </Box>
  );
}

// -- recipient groups and labels ---------------------------------------------------------------------

export function ListsAndLabels({ document, editable, onSave }: { document: RulesDocument; editable: boolean; onSave: Save }) {
  const lists = recipientLists(document);
  const labels = documentLabels(document);
  const [editing, setEditing] = useState<{ key: string | null; name: string; numbers: string; prefixes: string } | null>(null);
  const [label, setLabel] = useState('');

  const save = () => {
    if (!editing) return;
    const key = editing.key ?? keyFor(editing.name, Object.keys(lists));
    const value: RecipientList = { name: editing.name.trim(), numbers: splitList(editing.numbers), prefixes: splitList(editing.prefixes) };
    void onSave(withLists(document, { ...lists, [key]: value }, labels), `Recipient group “${value.name}” saved in your draft.`)
      .then((done) => { if (done) setEditing(null); });
  };

  return (
    <Box>
      <Section title="Recipient groups" editable={editable} addLabel="Add a recipient group"
        help="Numbers and number prefixes that rules can name together, such as your clinics."
        onAdd={() => setEditing({ key: null, name: '', numbers: '', prefixes: '' })}>
        {Object.keys(lists).length === 0 ? <Typography variant="body2" color="text.secondary">No recipient groups yet.</Typography> : (
          <Paper variant="outlined" sx={{ borderRadius: 2, overflowX: 'auto' }}>
            <Table size="small" aria-label="Recipient groups">
              <TableHead><TableRow><TableCell>Group</TableCell><TableCell>Numbers</TableCell><TableCell>Numbers starting with</TableCell>{editable && <TableCell />}</TableRow></TableHead>
              <TableBody>
                {Object.entries(lists).map(([key, item]) => (
                  <TableRow key={key}>
                    <TableCell>{item.name}</TableCell>
                    <TableCell>{(item.numbers ?? []).join(', ') || '-'}</TableCell>
                    <TableCell>{(item.prefixes ?? []).join(', ') || '-'}</TableCell>
                    <RowActions name={item.name} editable={editable}
                      onEdit={() => setEditing({ key, name: item.name, numbers: (item.numbers ?? []).join(', '), prefixes: (item.prefixes ?? []).join(', ') })}
                      onRemove={() => {
                        const next = { ...lists };
                        delete next[key];
                        void onSave(withLists(document, next, labels), `Recipient group “${item.name}” removed from your draft.`);
                      }} />
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </Paper>
        )}
      </Section>
      <Section title="Labels" editable={false} addLabel="" onAdd={() => undefined}
        help="Labels senders can put on a fax, such as legal or clinical. Rules can match them.">
        <Stack direction="row" spacing={1} flexWrap="wrap" useFlexGap sx={{ mb: 1 }}>
          {labels.length === 0 && <Typography variant="body2" color="text.secondary">No labels yet.</Typography>}
          {labels.map((item) => (
            <Chip key={item} label={item} onDelete={editable ? () => void onSave(withLists(document, lists, labels.filter((other) => other !== item)),
              `Label “${item}” removed from your draft.`) : undefined} />
          ))}
        </Stack>
        {editable && (
          <Stack direction="row" spacing={1}>
            <TextField size="small" label="New label" value={label} onChange={(event) => setLabel(event.target.value)} />
            <Button disabled={!label.trim() || labels.includes(label.trim())}
              onClick={() => void onSave(withLists(document, lists, [...labels, label.trim()]), `Label “${label.trim()}” added to your draft.`)
                .then((done) => { if (done) setLabel(''); })}>
              Add label
            </Button>
          </Stack>
        )}
      </Section>
      {editing && (
        <EditDialog open title={editing.key ? `Change ${editing.name}` : 'Add a recipient group'}
          valid={editing.name.trim() !== '' && splitList(editing.numbers).length + splitList(editing.prefixes).length > 0}
          onClose={() => setEditing(null)} onSave={save}>
          <TextField size="small" label="Name" value={editing.name} placeholder="UK clinics"
            onChange={(event) => setEditing({ ...editing, name: event.target.value })} />
          <TextField size="small" label="Fax numbers" value={editing.numbers} multiline minRows={2}
            helperText="Separate several with commas or new lines." onChange={(event) => setEditing({ ...editing, numbers: event.target.value })} />
          <TextField size="small" label="Numbers starting with" value={editing.prefixes} placeholder="+4420"
            helperText="Separate several with commas." onChange={(event) => setEditing({ ...editing, prefixes: event.target.value })} />
        </EditDialog>
      )}
    </Box>
  );
}

// -- workflows ------------------------------------------------------------------------------------------

export function Workflows({ document, choices, editable, onSave, onOpenWorkflow }: {
  document: RulesDocument; choices: Choices; editable: boolean; onSave: Save;
  // Opens a workflow's own rules.
  onOpenWorkflow?: (workflow: Workflow) => void;
}) {
  const workflows = document.workflows ?? [];
  const labels = documentLabels(document);
  const [editing, setEditing] = useState<{ original: Workflow | null; value: Workflow } | null>(null);
  useEffect(() => { if (!editable) setEditing(null); }, [editable]);

  const save = () => {
    if (!editing) return;
    const value = { ...editing.value, name: editing.value.name.trim() };
    const next = editing.original ? workflows.map((item) => (item.key === editing.original!.key ? value : item))
      : [...workflows, { ...value, key: keyFor(value.name, workflows.map((item) => item.key)) }];
    void onSave({ ...document, workflows: next }, `Workflow “${value.name}” saved in your draft.`).then((done) => { if (done) setEditing(null); });
  };

  return (
    <Box>
      <Section title="Workflows" editable={editable} addLabel="Add a workflow"
        help="Kinds of work, such as referrals, that can have their own sending rules. A fax is part of a workflow when the sender chooses it, when it is sent from one of its mailboxes, or when it carries one of its labels."
        onAdd={() => setEditing({ original: null, value: { key: '', name: '', mailboxes: [], labels: [] } })}>
        {workflows.length === 0 ? <Typography variant="body2" color="text.secondary">No workflows yet.</Typography> : (
          <Paper variant="outlined" sx={{ borderRadius: 2, overflowX: 'auto' }}>
            <Table size="small" aria-label="Workflows">
              <TableHead><TableRow><TableCell>Workflow</TableCell><TableCell>Mailboxes</TableCell><TableCell>Labels</TableCell><TableCell />{editable && <TableCell />}</TableRow></TableHead>
              <TableBody>
                {workflows.map((item) => (
                  <TableRow key={item.key}>
                    <TableCell>{item.name}</TableCell>
                    <TableCell>{(item.mailboxes ?? []).map((id) => nameOf(choices.mailboxes, id)).join(', ') || '-'}</TableCell>
                    <TableCell>{(item.labels ?? []).join(', ') || '-'}</TableCell>
                    <TableCell>{onOpenWorkflow && <Button size="small" onClick={() => onOpenWorkflow(item)}>Its own rules</Button>}</TableCell>
                    <RowActions name={item.name} editable={editable} onEdit={() => setEditing({ original: item, value: { ...item } })}
                      onRemove={() => void onSave({ ...document, workflows: workflows.filter((other) => other.key !== item.key) },
                        `Workflow “${item.name}” removed from your draft.`)} />
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </Paper>
        )}
      </Section>
      {editing && (
        <EditDialog open title={editing.original ? `Change ${editing.original.name}` : 'Add a workflow'}
          valid={editing.value.name.trim() !== ''} onClose={() => setEditing(null)} onSave={save}>
          <TextField size="small" label="Name" value={editing.value.name} placeholder="Referrals"
            onChange={(event) => setEditing({ ...editing, value: { ...editing.value, name: event.target.value } })} />
          <NamesPicker label="Mailboxes whose faxes are part of it" options={choices.mailboxes} value={editing.value.mailboxes ?? []}
            onChange={(mailboxes) => setEditing({ ...editing, value: { ...editing.value, mailboxes } })} />
          <Autocomplete multiple options={labels} value={editing.value.labels ?? []}
            onChange={(_, next) => setEditing({ ...editing, value: { ...editing.value, labels: next } })}
            renderInput={(params) => <TextField {...params} size="small" label="Labels that put a fax in it"
              helperText={labels.length === 0 ? 'Add labels on the Lists tab first.' : undefined} />} />
        </EditDialog>
      )}
    </Box>
  );
}
