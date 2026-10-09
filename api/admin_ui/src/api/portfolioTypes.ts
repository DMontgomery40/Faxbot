// Stateless setup comparisons. Decimal strings stay exact in requests and results.
export interface PortfolioItem {
  id: string;
  label: string;
  cost: string | null;
  installed: boolean;
  group_id: string | null;
}
export interface PortfolioGroup { id: string; label: string; cost: string | null }
export interface PortfolioRelationship { a: string; b: string; expected: string | null; cautious: string | null }
export interface PortfolioInput {
  currency: string;
  horizon: string;
  perspective: string;
  budget: string | null;
  nodes: PortfolioItem[];
  groups: PortfolioGroup[];
  relationships: PortfolioRelationship[];
}
export interface PortfolioPlan {
  state: 'planned' | 'abstain';
  installed_ids: string[];
  new_ids: string[];
  selected_ids: string[];
  incremental_cost: string;
  expected_benefit: string;
  cautious_benefit: string;
  expected_net: string;
  cautious_net: string;
  objective: string;
}
export interface PortfolioResult {
  state: 'planned' | 'abstain' | 'incomplete';
  currency: string;
  horizon: string;
  perspective: string;
  budget: string | null;
  missing: Array<{ field: string; reason: string }>;
  plans: { expected: PortfolioPlan; cautious: PortfolioPlan } | null;
  estimate: true;
  realized: false;
  saved: false;
  note: string;
  assumptions: string[];
}
