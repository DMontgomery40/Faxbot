// Words and links the Capabilities pages share. Every sentence comes from GET /routing/capabilities;
// `faxbot costs capabilities` prints the same ones (cli/commands/capabilities.py, __tests__/capabilities.json).
import type { MouseEvent, ReactNode } from 'react';
import { Link } from '@mui/material';
import type { Capability, CapabilityFilterKey } from '../../api/capabilityTypes';
import type { AdminDestination } from '../../navigation';

export const CAPABILITIES_ADDRESS = 'savings/capabilities';

export type Navigate = (destination: AdminDestination) => void;

// The filters the page keeps in its address (?show=ready); anything else shows them all.
export const CAPABILITY_FILTERS: CapabilityFilterKey[] = ['on', 'off', 'ready', 'needs', 'experimental'];

export function readCapabilityFilter(value: string | null | undefined): CapabilityFilterKey | null {
  return CAPABILITY_FILTERS.includes(value as CapabilityFilterKey) ? value as CapabilityFilterKey : null;
}

export function capabilitiesAddress(show: CapabilityFilterKey | null): string {
  return show ? `${CAPABILITIES_ADDRESS}?show=${show}` : CAPABILITIES_ADDRESS;
}

// The labels shown as chips, in the command's order: On or Off, Works here or Not here, how far it is proven,
// then Ready to turn on and Experimental where they apply.
export function capabilityStates(item: Capability): string[] {
  return [item.enabled.label, item.works.label, item.evidence.label,
    ...(item.ready ? ['Ready to turn on'] : []), ...(item.experimental ? ['Experimental'] : [])];
}

// "Missing: Partner, Setting", or null when nothing is missing.
export function missingLine(item: Capability): string | null {
  const kinds = item.prerequisites.filter((prerequisite) => !prerequisite.met).map((prerequisite) => prerequisite.kind_label);
  return kinds.length ? `Missing: ${kinds.join(', ')}` : null;
}

// A real link to a console address: it opens in a new tab or can be copied, and a plain click navigates in place.
export function AddressLink({ address, onNavigate, children, label }: {
  address: string; onNavigate?: Navigate; children: ReactNode; label?: string;
}) {
  const open = (event: MouseEvent) => {
    if (!onNavigate || event.metaKey || event.ctrlKey || event.shiftKey || event.button !== 0) return;
    event.preventDefault();
    onNavigate(address as AdminDestination);
  };
  return <Link href={`#/${address}`} onClick={open} aria-label={label} underline="hover">{children}</Link>;
}
