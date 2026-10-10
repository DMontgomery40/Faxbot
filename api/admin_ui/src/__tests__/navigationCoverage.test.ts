// Every address the console had before the six areas (the 52 entries of #46 section 4),
// every legacy destination name and every conditional provider page: each opens the page
// that does its job now, with its query, behind the same gate as before; a person who may
// not open it gets the forbidden state and nothing else. The rows below are written out
// by hand from the old menu, so they check the alias table rather than repeat it.
import { describe, expect, it } from 'vitest';
import {
  LEGACY_DESTINATIONS,
  NAVIGATION,
  destinationAddress,
  parseAddress,
  resolveAddress,
  resolveHash,
  visibleNavigation,
  type Gate,
  type LegacyDestination,
} from '../navigation';
import { ALL_PERMISSIONS } from '../test/server';

const SR: Gate = { anyOf: ['settings:read'] };
const OVERVIEW: Gate = { anyOf: ['diagnostics:read', 'settings:read'] };

// [old address, its new home, the gate it had]; a third address is where an entry that
// opens another page lands.
type Row = [old: string, home: string, gate: Gate, lands?: string];
const OLD_ENTRIES: Row[] = [
  ['overview', 'overview/overview', OVERVIEW],
  ['faxes/received', 'faxes/received', { navigation: ['inbox', 'work'] }],
  ['faxes/sent', 'faxes/sent', { navigation: 'jobs' }],
  ['faxes/send', 'faxes/send', { navigation: 'send' }],
  ['faxes/forms', 'faxes/forms', { anyOf: ['settings:read', 'fax:send'] }],
  ['faxes/expected', 'faxes/expected', { anyOf: ['work:read', 'work:import'] }],
  ['numbers/list', 'delivery/numbers', { anyOf: ['mailboxes:read', 'mailboxes:manage'] }],
  ['numbers/mailboxes', 'delivery/mailboxes', { anyOf: ['mailboxes:read', 'mailboxes:manage', 'settings:read'] }],
  ['numbers/email', 'delivery/email', SR],
  ['numbers/advice', 'delivery/moves', SR],
  ['numbers/npi', 'admin/npi', SR],
  ['numbers/blocked', 'delivery/blocked', SR],
  ['numbers/connectors', 'delivery/connectors', SR],
  ['numbers/identity', 'delivery/identity', SR],
  ['recipients/list', 'recipients/list', SR],
  ['recipients/partners', 'recipients/partners', SR],
  ['recipients/cases', 'faxes/cases', SR],
  ['providers/sending', 'delivery/connections', SR],
  ['providers/rules', 'delivery/rules', SR],
  ['providers/humblefax', 'delivery/humblefax', SR],
  ['providers/efax', 'delivery/efax', SR],
  ['providers/phaxio', 'delivery/phaxio', SR],
  ['providers/sinch', 'delivery/sinch', SR],
  ['providers/signalwire', 'delivery/signalwire', SR],
  ['providers/documo', 'delivery/documo', SR],
  ['providers/trunk', 'delivery/trunk', SR],
  ['providers/change', 'delivery/change', { anyOf: ['settings:write'] }, 'admin/setup'],
  ['costs/spending', 'savings/spending', SR],
  ['costs/charges', 'savings/charges', SR],
  ['costs/invoices', 'savings/invoices', SR],
  ['costs/prices', 'savings/prices', SR],
  ['costs/savings', 'savings/results', SR],
  ['costs/recommendations', 'savings/opportunities', SR],
  ['costs/advice', 'savings/facts', SR],
  ['access/users', 'admin/users', { anyOf: ['users:read', 'users:manage'] }],
  ['access/groups', 'admin/groups', { anyOf: ['groups:read', 'groups:manage'] }],
  ['access/roles', 'admin/roles', { anyOf: ['roles:read', 'roles:manage'] }],
  ['access/who', 'admin/who', { anyOf: ['grants:read', 'grants:manage'] }],
  ['access/keys', 'admin/keys', { anyOf: ['keys:manage'] }],
  ['access/sessions', 'admin/sessions', {}],
  ['system/setup', 'admin/setup', { anyOf: ['settings:write'] }],
  ['system/analysis', 'admin/analysis', SR],
  ['system/security', 'admin/security', SR],
  ['system/storage', 'admin/retention', SR],
  ['system/audit', 'admin/audit', { anyOf: ['audit:read'] }],
  ['system/diagnostics', 'admin/health', { anyOf: ['diagnostics:read'] }],
  ['system/logs', 'admin/logs', { anyOf: ['logs:read'] }],
  ['system/api', 'admin/api', OVERVIEW],
  ['system/assistants', 'admin/assistants', SR],
  ['system/terminal', 'admin/terminal', { anyOf: ['host:terminal'] }],
  ['system/scripts', 'admin/scripts', { anyOf: ['providers:write'] }],
  ['system/plugins', 'admin/plugins', { anyOf: ['providers:read', 'settings:read'] }],
];

const LEGACY: Record<LegacyDestination, string> = {
  send: 'faxes/send', jobs: 'faxes/sent', inbox: 'faxes/received', settings: 'delivery/connections', keys: 'admin/keys',
  diagnostics: 'admin/health', routes: 'savings/spending', email: 'delivery/email', trunk: 'delivery/trunk', setup: 'admin/setup',
};

const PROVIDER_PAGES = ['humblefax', 'efax', 'phaxio', 'sinch', 'signalwire', 'documo', 'trunk'];

// Every permission the test server knows, and every one an old gate names (work:read, ...).
const everything = new Set([...ALL_PERMISSIONS.map(([permission]) => permission),
  ...OLD_ENTRIES.flatMap(([, , gate]) => gate.anyOf ?? [])]);
const allScreens = { send: true, jobs: true, inbox: true, work: true };
const noScreens = { send: false, jobs: false, inbox: false, work: false };
const permitted = visibleNavigation(everything, allScreens, { pluginsEnabled: true });
const nobody = visibleNavigation(new Set(), noScreens, { pluginsEnabled: false });

const pageAt = (key: string) => {
  const [areaId, pageId] = key.split('/');
  return NAVIGATION.find((area) => area.id === areaId)?.pages.find((page) => page.id === pageId);
};
const opened = (resolved: ReturnType<typeof resolveAddress>) =>
  resolved?.kind === 'page' ? `${resolved.area.id}/${resolved.page.id}` : resolved?.kind;
// One person for each way through a gate, holding only that permission or that kind of screen.
const throughEachOption = (gate: Gate) => {
  const people = [
    ...(gate.anyOf ?? []).map((permission) => visibleNavigation(new Set([permission]), noScreens, { pluginsEnabled: false })),
    ...[gate.navigation ?? []].flat().map((key) =>
      visibleNavigation(new Set(), { ...noScreens, [key]: true }, { pluginsEnabled: false })),
  ];
  return people.length ? people : [visibleNavigation(new Set(), noScreens, { pluginsEnabled: false })];
};

describe('the 52 old navigation entries', () => {
  it('are all here, each once, each with a new home that is a page of the table', () => {
    expect(OLD_ENTRIES).toHaveLength(52);
    expect(new Set(OLD_ENTRIES.map(([old]) => old)).size).toBe(52);
    for (const [, home] of OLD_ENTRIES) expect(pageAt(home), home).toBeTruthy();
    // Every page of the new table is the home of an old entry, except Capabilities, which is new.
    const homes = new Set(OLD_ENTRIES.map(([, home]) => home));
    const pages = NAVIGATION.flatMap((area) => area.pages.map((page) => `${area.id}/${page.id}`));
    expect(pages.filter((page) => !homes.has(page))).toEqual(['savings/capabilities']);
  });

  it.each(OLD_ENTRIES)('%s keeps its gate at %s', (_old, home, gate) => {
    expect(pageAt(home)!.gate).toEqual(gate);
  });

  it.each(OLD_ENTRIES)('%s opens %s with its query for a permitted person', (old, home, _gate, lands) => {
    const resolved = resolveAddress(permitted, parseAddress(`#/${old}?probe=kept`));
    expect(opened(resolved)).toBe(lands ?? home);
    if (resolved?.kind !== 'page') return;
    if (!lands) expect(resolved.address).toContain('probe=kept');
    // Only an address that changed says where the page used to be.
    const moved = old !== home && !(old === 'overview' && home === 'overview/overview');
    expect(Boolean(resolved.movedFrom)).toBe(moved);
  });

  it.each(OLD_ENTRIES)('%s opens for a person who holds any one option of its gate and nothing else', (old, home, gate, lands) => {
    for (const person of throughEachOption(gate)) {
      expect(opened(resolveAddress(person, parseAddress(`#/${old}`)))).toBe(lands ?? home);
    }
  });

  it.each(OLD_ENTRIES)('%s is forbidden to a person without its gate, and the address is kept', (old, home, gate) => {
    const requested = `#/${old}?record=private-value`;
    const resolved = resolveAddress(nobody, parseAddress(requested));
    if (Object.keys(gate).length === 0) {
      expect(opened(resolved)).toBe(home);  // Sessions: everyone signed in
      return;
    }
    expect(resolved).toEqual({ kind: 'forbidden', address: `#/${old === 'overview' ? 'overview' : old}?record=private-value` });
  });

  it('keeps the queries the old menu was linked with', () => {
    expect(resolveAddress(permitted, parseAddress('#/costs/savings?part=sslfax'))?.address).toBe('#/savings/results?part=sslfax');
    expect(resolveAddress(permitted, parseAddress('#/faxes/received?show=waiting'))?.address).toBe('#/faxes/received?show=waiting');
    expect(resolveAddress(permitted, parseAddress('#/faxes/work'))?.address).toBe('#/faxes/received?show=waiting');
    expect(resolveAddress(permitted, parseAddress('#/faxes/work?mailbox=front'))?.address)
      .toBe('#/faxes/received?show=waiting&mailbox=front');
    expect(resolveAddress(permitted, parseAddress('#/access/keys?mine=1'))?.address).toBe('#/admin/keys?mine=1');
    expect(resolveAddress(permitted, parseAddress('#/costs/recommendations?section=plans'))?.address)
      .toBe('#/savings/opportunities?section=plans');
  });
});

describe('addresses that are not addresses', () => {
  it.each(['', '#', '#/'])('%j opens the first page this person may open', (hash) => {
    expect(resolveHash(permitted, hash)).toMatchObject({ kind: 'page', address: '#/overview' });
  });

  it.each(['#/faxes/received/123', '#/Costs/savings', '#/costs/savings/extra', '#settings', '#/faxes/sent//',
    '#//faxes', '#/1/2'])('%s names no page, and the address is kept', (hash) => {
    expect(resolveHash(permitted, hash)).toEqual({ kind: 'unknown', address: hash });
    expect(resolveHash(nobody, hash)).toEqual({ kind: 'unknown', address: hash });
  });
});

describe('the legacy destination names', () => {
  it('are the ten the screens use', () => {
    expect(Object.keys(LEGACY_DESTINATIONS).sort()).toEqual(Object.keys(LEGACY).sort());
  });

  it.each(Object.entries(LEGACY))('%s opens %s', (name, home) => {
    expect(destinationAddress(name as LegacyDestination)).toBe(`#/${home}`);
    expect(opened(resolveAddress(permitted, parseAddress(destinationAddress(name as LegacyDestination))))).toBe(home);
    const gate = pageAt(home)!.gate;
    expect(opened(resolveAddress(nobody, parseAddress(destinationAddress(name as LegacyDestination)))))
      .toBe(Object.keys(gate).length ? 'forbidden' : home);
  });
});

describe('the conditional provider pages', () => {
  it.each(PROVIDER_PAGES)('%s leaves the panel while not in use and still opens at its old and new address', (id) => {
    const areas = visibleNavigation(everything, allScreens, { pluginsEnabled: false, providersInUse: [] });
    const page = areas.find((area) => area.id === 'delivery')!.pages.find((candidate) => candidate.id === id)!;
    expect(page.provider).toBeTruthy();
    expect(page.inPanel).toBe(false);
    expect(page.group).toBe('Connections');
    expect(opened(resolveAddress(areas, parseAddress(`#/providers/${id}`)))).toBe(`delivery/${id}`);
    expect(opened(resolveAddress(areas, parseAddress(`#/delivery/${id}`)))).toBe(`delivery/${id}`);
    expect(opened(resolveAddress(nobody, parseAddress(`#/providers/${id}`)))).toBe('forbidden');
  });

  it('lists a provider page in the panel while that provider is in use', () => {
    const areas = visibleNavigation(everything, allScreens, { pluginsEnabled: false, providersInUse: ['phaxio', 'sip'] });
    const listed = areas.find((area) => area.id === 'delivery')!.pages
      .filter((page) => page.provider && page.inPanel !== false).map((page) => page.id);
    expect(listed).toEqual(['phaxio', 'trunk']);
  });
});
