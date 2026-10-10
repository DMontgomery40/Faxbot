// Costs → Recommendations → "Add as rule": a suggested routing rule goes to the top of the routing rules in
// the organization's draft. It never publishes; the administrator checks and publishes it on Providers → Rules.
import { useState } from 'react';
import { Button } from '@mui/material';
import type { AdminDestination } from '../navigation';
import type { Actions, Conditions, Rule, RulesApi } from './ProviderRulesApi';
import { emptyDocument, ORGANIZATION } from './ProviderRulesApi';
import { newRuleId } from './ProviderRulesEditor';

// What a recommendation proposes, such as "Faxes to +44 numbers go through Sinch (UK)".
export interface RuleSuggestion { name: string; when: Conditions; then: Actions }

export async function addSuggestedRule(api: RulesApi, suggestion: RuleSuggestion): Promise<Rule> {
  const state = await api.rules(ORGANIZATION);
  const document = state.draft?.document ?? state.active?.document ?? emptyDocument();
  const rule: Rule = { id: newRuleId(document, 'routes', suggestion.name), name: suggestion.name, on: true,
    when: suggestion.when, then: suggestion.then };
  // A suggestion is specific (a country, a prefix, a number), so it goes first; Check warns if it hides another rule.
  await api.saveDraft(ORGANIZATION, { ...document, routes: [rule, ...(document.routes ?? [])] }, state.draft?.version ?? 0);
  return rule;
}

export function AddAsRuleButton({ api, suggestion, onDone, onError, onNavigate }: {
  api: RulesApi;
  suggestion: RuleSuggestion;
  onDone: (sentence: string) => void;
  onError: (error: unknown) => void;
  onNavigate?: (destination: AdminDestination) => void;
}) {
  const [added, setAdded] = useState(false);
  const [busy, setBusy] = useState(false);
  if (added && onNavigate) {
    return <Button size="small" onClick={() => onNavigate('providers/rules')}>Open your draft rules</Button>;
  }
  return (
    <Button size="small" disabled={busy || added} onClick={() => {
      setBusy(true);
      addSuggestedRule(api, suggestion)
        .then((rule) => {
          setAdded(true);
          onDone(`“${rule.name}” is in your draft on Delivery setup → Routing rules. It takes effect when you publish it.`);
        })
        .catch(onError)
        .finally(() => setBusy(false));
    }}>
      Add as rule
    </Button>
  );
}
