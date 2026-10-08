// The requests the Expected page makes. JSON requests go through the console client's `call`;
// the file upload and the evidence download use the client's own methods.
import type AdminAPIClient from '../../api/client';
import type {
  ExpectedCounts, ExpectedEvent, ExpectedFax, ExpectedReport, ExpectedView, ExpectInput, ImportRun, ImportSource,
  ImportSourceInput, Outage, OutageActionInput,
} from '../../api/expectedTypes';

const segment = (value: string) => encodeURIComponent(value);

function query(params: Record<string, string | number | undefined>): string {
  const search = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== '') search.set(key, String(value));
  });
  const text = search.toString();
  return text ? `?${text}` : '';
}

export function expectedApi(client: AdminAPIClient) {
  const get = <T,>(path: string) => client.call<T>({ method: 'GET', path });
  const post = <T,>(path: string, body?: unknown) => client.call<T>({ method: 'POST', path, body: body ?? {} });
  return {
    list: (view: ExpectedView, search?: string) =>
      get<{ expected: ExpectedFax[] }>(`/expected-faxes${query({ view, search, limit: 200 })}`),
    counts: () => get<ExpectedCounts>('/expected-faxes/counts'),
    report: (days: number) => get<ExpectedReport>(`/expected-faxes/report${query({ days })}`),
    mailboxes: () => get<{ mailboxes: Array<{ id: string; label: string }> }>('/expected-faxes/mailboxes'),
    add: (input: ExpectInput) => post<ExpectedFax>('/expected-faxes', input),
    detail: (code: string) => get<ExpectedFax>(`/expected-faxes/${segment(code)}`),
    history: (code: string) => get<{ events: ExpectedEvent[] }>(`/expected-faxes/${segment(code)}/history`),
    confirm: (code: string, proposalId: string, version: number) =>
      post<ExpectedFax>(`/expected-faxes/${segment(code)}/confirm`, { proposal_id: proposalId, version }),
    reject: (code: string, proposalId: string, version: number, note?: string) =>
      post<ExpectedFax>(`/expected-faxes/${segment(code)}/reject`,
        { proposal_id: proposalId, version, ...(note ? { note } : {}) }),
    match: (code: string, inboundFaxId: string, version: number, note?: string) =>
      post<ExpectedFax>(`/expected-faxes/${segment(code)}/match`,
        { inbound_fax_id: inboundFaxId, version, ...(note ? { note } : {}) }),
    close: (code: string, outcome: 'cancelled' | 'completed_elsewhere', note: string, version: number) =>
      post<ExpectedFax>(`/expected-faxes/${segment(code)}/close`, { outcome, note, version }),
    conflict: (code: string, choice: 'keep' | 'apply', version: number) =>
      post<ExpectedFax>(`/expected-faxes/${segment(code)}/conflict`, { choice, version }),
    sources: () => get<{ sources: ImportSource[] }>('/expected-faxes/sources'),
    saveSource: (input: ImportSourceInput) => post<ImportSource>('/expected-faxes/sources', input),
    imports: () => get<{ imports: ImportRun[] }>('/expected-faxes/imports'),
    importFile: (source: string, file: File, fullExport: boolean) =>
      client.importExpected<ImportRun>(source, file, fullExport),
    exportEvidence: (expectedId: string) => client.exportExpected(expectedId),
    outages: () => get<{ outages: Outage[] }>('/expected-faxes/outages'),
    outage: (code: string) => get<Outage>(`/expected-faxes/outages/${segment(code)}`),
    startOutage: (source: string, note?: string) =>
      post<Outage>('/expected-faxes/outages', { source, ...(note ? { note } : {}) }),
    endOutage: (code: string, version: number) =>
      post<Outage>(`/expected-faxes/outages/${segment(code)}/end`, { version }),
    recordAction: (code: string, input: OutageActionInput) =>
      post<Outage>(`/expected-faxes/outages/${segment(code)}/actions`, input),
    reconcile: (code: string) => post<Outage>(`/expected-faxes/outages/${segment(code)}/reconcile`),
  };
}

export type ExpectedApi = ReturnType<typeof expectedApi>;
