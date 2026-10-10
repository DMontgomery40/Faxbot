// Administration → System health → Sending routes: route problems, the 2-by-2 test and shared upstreams (routing/route_families_http.py).
// The server writes every sentence; the console shows them and sends back what the administrator chose.
import type AdminAPIClient from './client';

export interface RouteProblem {
  id: string;
  account: string;
  account_label: string;
  transport: 't38' | 'audio' | 'service';
  phase: string;
  open: boolean;
  change_cause: 'settings' | 'outside';
  since_text: string | null;
  closed_text: string | null;
  closed_reason: string | null;
  retired: number;
  destinations: number;
  sentence: string;
  advice: string | null;
}

export type CellState = 'not_sent' | 'pending' | 'success' | 'failed';

export interface TestCell {
  cell: 'a1' | 'a2' | 'b1' | 'b2';
  account: string;
  account_label: string;
  number: string;
  state: CellState;
  fax_id: string | null;
  sent_text: string | null;
}

export interface RouteTest {
  id: string;
  route_a: string;
  route_b: string;
  route_a_label: string;
  route_b_label: string;
  number_a: string;
  number_b: string;
  created_text: string | null;
  cells: TestCell[];
  verdict: string;
  sentence: string;
}

export interface Upstream {
  provider: string;
  upstream: string | null;
  source_url: string | null;
  source_day: string | null;
  note: string | null;
}

export interface RouteFamilies {
  incidents: RouteProblem[];
  tests: RouteTest[];
  upstreams: Upstream[];
  accounts: Array<{ key: string; label: string; provider: string }>;
  numbers: string[];
}

export interface TestPlan { route_a: string; route_b: string; number_a: string; number_b: string; incident_id?: string }

export function routeFamiliesApi(client: AdminAPIClient) {
  return {
    list: () => client.call<RouteFamilies>({ method: 'GET', path: '/routing/families' }),
    close: (id: string) => client.call<RouteProblem>({ method: 'POST', path: `/routing/families/${encodeURIComponent(id)}/close` }),
    plan: (body: TestPlan) => client.call<RouteTest>({ method: 'POST', path: '/routing/families/tests', body }),
    send: (testId: string, cell: string) => client.call<{ fax_id: string; test: RouteTest; sentence: string }>({
      method: 'POST', path: `/routing/families/tests/${encodeURIComponent(testId)}/send/${encodeURIComponent(cell)}`,
    }),
    upstream: (provider: string, body: { upstream: string | null; source_url?: string | null; source_date?: string | null }) =>
      client.call<Upstream>({ method: 'PUT', path: `/routing/upstreams/${encodeURIComponent(provider)}`, body }),
  };
}
