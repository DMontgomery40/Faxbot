import { describe, expect, it } from 'vitest';
import fixture from './providerRulesSentences.json';
import { namesFrom, receivingSentence, ruleSentence, windowText, AUTOMATIC_ROW } from '../components/ProviderRulesText';
import { ORGANIZATION, requests, rulesApi, scopeParam, type ApiRequest, type Rule } from '../components/ProviderRulesApi';

describe('provider rules in words', () => {
  const names = namesFrom(fixture.names);

  it.each(fixture.cases.map((item) => [item.sentence, item.rule]))('%s', (sentence, rule) => {
    expect(ruleSentence(rule as unknown as Rule, names)).toBe(sentence);
  });

  it.each(fixture.receiving_cases.map((item) => [item.sentence, item.rule]))('%s', (sentence, rule) => {
    const connectors: Record<string, string> = fixture.connectors;
    expect(receivingSentence(rule as never, { account: names.account, connector: (id) => connectors[id] })).toBe(sentence);
  });

  it('reads the last routing row and time windows the way people say them', () => {
    expect(AUTOMATIC_ROW).toBe('Everything else: the cheapest reliable route, as before.');
    expect(windowText({ days: ['sat', 'sun'] })).toBe('at weekends');
    expect(windowText({})).toBeNull();
  });
});

describe('provider rules requests', () => {
  it('sends every scope as one query value and keeps the API shapes of the design', async () => {
    expect(scopeParam(ORGANIZATION)).toBe('organization');
    expect(scopeParam({ kind: 'mailbox', id: 'mb 1' })).toBe('mailbox:mb 1');
    expect(requests.rules({ kind: 'mailbox', id: 'mb 1' }).path).toBe('/routing/rules?scope=mailbox%3Amb%201');
    expect(requests.publish(ORGANIZATION, 6, 3, 'UK via Sinch')).toEqual({
      method: 'POST', path: '/routing/rules/publish?scope=organization',
      body: { expected_active_revision: 6, expected_draft_version: 3, note: 'UK via Sinch' },
    });
    expect(requests.diff(ORGANIZATION, 2, 5).path).toBe('/routing/rules/revisions/2/diff/5?scope=organization');
    expect(requests.updateAccount('sinch-uk', { enabled: false }, 9)).toEqual({
      method: 'PATCH', path: '/admin/providers/accounts/sinch-uk', body: { enabled: false, expected_generation: 9 },
    });
    const sent: ApiRequest[] = [];
    const api = rulesApi(async <T,>(request: ApiRequest) => { sent.push(request); return {} as T; });
    await api.refuse({ id: 'h1', job_id: 'j1', kind: 'approval', to_number: '+15550100', pages: 1, sender_name: null,
      requested_at: '2026-10-07T12:00:00', until: null, reason: 'Waiting for approval.', can_decide: true, version: 2 },
    'Wrong recipient');
    expect(sent).toEqual([{ method: 'POST', path: '/routing/holds/h1/refuse', body: { version: 2, reason: 'Wrong recipient' } }]);
  });
});
