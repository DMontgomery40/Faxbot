// System → Diagnostics → Receiving readiness and Power (receive_readiness_http.py, power.py).
// The server writes every sentence; the console shows them and sends back what the administrator chose.
import type AdminAPIClient from './client';

export type ReceivingStatus = 'receiving' | 'attention' | 'not_receiving' | 'off';

export interface ReceivingCheck { title: string; status: ReceivingStatus; sentence: string }

export interface ReceivingNumber {
  number: string;
  status: ReceivingStatus;
  sentence: string;
  owner: string | null;
  owner_label: string | null;
  endpoints: Array<{ key: string; label: string; provider: string }>;
  checks: ReceivingCheck[];
  last_received_text: string | null;
}

export interface Readiness { numbers: ReceivingNumber[]; checked_text: string; receiving_on: boolean }

export interface OwnerChange { number: string; owner: string | null; label?: string | null; move?: boolean }

export interface Power {
  configured: boolean;
  host: string | null;
  port: number;
  ups_name: string | null;
  reserve_minutes: number;
  status: 'ok' | 'attention' | 'off';
  sentence: string;
  on_battery: boolean;
  runtime_minutes: number | null;
  charge_percent: number | null;
}

export interface PowerChange { host: string; port?: number; ups_name?: string | null; reserve_minutes?: number }

export function siteChecksApi(client: AdminAPIClient) {
  return {
    readiness: () => client.call<Readiness>({ method: 'GET', path: '/receiving/readiness' }),
    owner: (body: OwnerChange) => client.call<{ sentence: string }>({ method: 'POST', path: '/receiving/owners', body }),
    power: () => client.call<Power>({ method: 'GET', path: '/power' }),
    setPower: (body: PowerChange) => client.call<Power>({ method: 'PUT', path: '/power', body }),
  };
}
