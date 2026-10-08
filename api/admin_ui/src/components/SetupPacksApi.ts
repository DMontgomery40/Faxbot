// Guided setup's suggested packs: the shapes System → Setup reads, and its four requests.
// The server compiles every suggestion, explanation and sentence; the console shows them and sends back
// which suggestions to apply, with the plan's revision so a plan built on older settings is refused.
import type AdminAPIClient from '../api/client';
import type { AdminDestination } from '../navigation';

export interface Amount { amount: string; currency: string }
export interface Saving extends Amount { faxes?: number | null; estimate?: boolean }
export interface Source { name: string; detail: string }

export type ItemKind = 'rule' | 'setting' | 'step' | 'in_effect';

export interface PlanItem {
  key: string;
  pack: string;
  kind: ItemKind;
  title: string;
  sentence: string;
  sources: Source[];
  saving: Saving | null;
  selected: boolean;
  blocked: string | null;
  scope: string;
  applies_to: string[] | null;
  link: AdminDestination | null;
  cli: string | null;
}

export interface Pack { key: string; title: string; sentence: string; items: PlanItem[]; saving: Amount | null }

export interface MissingEntry {
  key: string;
  sentence: string;
  owner: 'you' | 'faxbot';
  operation: string;
  status: 'blocking' | 'warning';
  scope: string;
  link: AdminDestination | null;
}

export interface Choice { label: string; value: string; source: string }
export interface MailboxView {
  id: string;
  name: string;
  country: string | null;
  country_source: 'stated' | 'organization' | 'not_stated';
  choices: Choice[];
  items: string[];
  missing: string[];
}
export interface WorkflowView { key: string; name: string; choices: Choice[] }

export interface Step { part: string; outcome: 'done' | 'refused' | 'not_tried'; sentence: string }
export interface Application {
  outcome: 'applied' | 'partial' | 'refused';
  items: string[];
  steps: Step[];
  restart_required: boolean;
  actor_name: string | null;
  created_at: string;
}

export interface SetupContext {
  organization_name: string;
  country: string;
  mailboxes: Record<string, { country: string }>;
}

export interface Plan {
  number: number;
  revision: string;
  created_at: string;
  actor_name: string | null;
  context: SetupContext;
  packs: Pack[];
  missing: MissingEntry[];
  mailboxes: MailboxView[];
  workflows: WorkflowView[];
  checks: Record<string, { warnings: Array<{ rule_id: string | null; message: string }>; replay: string | null }>;
  applications: Application[];
}

export interface MailboxChoice { id: string; name: string; numbers: string[]; numbers_country: string | null }
export interface Latest { plan: Plan | null; mailboxes: MailboxChoice[] }
export interface ApplyResult {
  outcome: Application['outcome'];
  items: string[];
  steps: Step[];
  restart_required: boolean;
  sentence: string;
  plan: Plan;
}

export interface SetupPacksApi {
  latest(): Promise<Latest>;
  preview(context: SetupContext): Promise<Plan>;
  apply(plan: Plan, items: string[]): Promise<ApplyResult>;
}

export function setupPacksApi(client: Pick<AdminAPIClient, 'call'>): SetupPacksApi {
  return {
    latest: () => client.call<Latest>({ method: 'GET', path: '/setup/plans/latest' }),
    preview: (context) => client.call<Plan>({ method: 'POST', path: '/setup/plans', body: context }),
    apply: (plan, items) => client.call<ApplyResult>({
      method: 'POST', path: `/setup/plans/${plan.number}/apply`, body: { expected_revision: plan.revision, items },
    }),
  };
}

// Suggestions Faxbot can apply: sending rules and settings that nothing blocks.
export const applicable = (item: PlanItem) => (item.kind === 'rule' || item.kind === 'setting') && !item.blocked;

export function appliedKeys(plan: Plan): Set<string> {
  return new Set(plan.applications.flatMap((application) => application.items));
}
