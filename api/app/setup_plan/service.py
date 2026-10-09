"""``SetupPlan``: preview a plan from what Faxbot knows, and apply a previewed plan deliberately.

``preview(context, snapshot=...)`` gathers the facts (reading only), compiles
the packs, checks every scope's resulting rules with the rules' own check and
replay, and keeps the plan. It sends nothing, starts no external action and
changes no setting or rule; the plan's own record is the only row it writes.

``apply(number, expected_revision, ...)`` applies the chosen items of a kept
plan:

1. The plan must be the one the caller previewed (``expected_revision``).
2. Before anything is written, it checks authority for every part (settings,
   and each scope's rules) and that nothing the plan builds on changed since
   the preview: the configuration revision, and each scope's active rules
   with no draft in between. Any of these refuses the whole apply.
3. Settings go through the configuration's own authorized, validated and
   audited write (``ConfigurationManager.patch_authorized``), all in one
   change.
4. Each scope's rules go through the rules' own draft, check and publish
   (``RuleStore.save_draft`` and ``publish``), so the published revision is
   exactly the previewed document.
5. Each apply is recorded with what each part did. A part that is done is
   never done again from the same plan; a part refused on the way leaves the
   apply partial, and it says why.
"""
import hashlib
import json
import logging

from ..config_store import ConfigurationConflict
from ..config_activation import ConfigurationActivationError
from ..config_values import ConfigurationValueError
from ..routing.database import utcnow
from ..rules import model
from ..rules.check import CheckContext, check, compile_for_replay, replay
from ..rules.store import RuleStore, RulesConflict, RulesInputError
from . import facts as facts_module
from .context import clean_context
from .packs import APPLIES, compile_plan, pack_saving, target_document
from .store import PlanStore, encode


log = logging.getLogger(__name__)
REPLAY = 200
SETTINGS = 'settings'


class PlanNotFound(LookupError):
    """There is no plan with that number."""


class PlanStale(RuntimeError):
    """Something the plan builds on changed since the preview; the sentence says what to do."""


class PlanRefused(ValueError):
    """The apply can't be done as asked; the sentence says why."""


class PlanForbidden(PermissionError):
    """The caller may not change one of the parts this plan changes."""


def basis_token(basis):
    """A digest of what the plan was built on: the configuration revision and each scope's rules."""
    return hashlib.sha256(encode(basis).encode('ascii')).hexdigest()


def all_items(plan):
    return [item for pack in plan['packs'] for item in pack['items']]


def check_context(facts):
    """What a rules check knows here: accounts and mailboxes; other names are not checked (None)."""
    return CheckContext(accounts=facts.accounts, mailboxes=frozenset(box['id'] for box in facts.mailboxes))


class SetupPlan:
    def __init__(self, engine, *, manager=None, access=None, relay=None, bound=None, price=None, clock=utcnow):
        self.engine = engine
        self.manager = manager            # ConfigurationManager: the settings write path
        self.access = access              # the access runtime: store, control and configuration_access
        self.relay = relay                # a RelayService, for relay offers from stored prices
        self.bound = bound                # the active outbound provider, as the route planner names it
        self.price = price                # a stand-in price for tests; the shared predictor otherwise
        self.clock = clock
        self.plans = PlanStore(engine)
        self.rules = RuleStore(engine, access_store=getattr(access, 'store', None))

    # Preview ------------------------------------------------------------------------------------------------

    def facts(self, snapshot):
        return facts_module.gather(self.engine, snapshot, bound=self.bound, relay=self.relay, now=self.clock())

    def preview(self, context, *, snapshot, actor_principal_id=None, actor_name=None):
        found = self.facts(snapshot)
        cleaned = clean_context(context, {box['id'] for box in found.mailboxes})
        plan = compile_plan(found, cleaned, price=self.price)
        plan['checks'] = self._checks(plan, found)
        return self.plans.create(basis=basis_token(plan['basis']), context=cleaned, plan=plan,
                                 actor_principal_id=actor_principal_id, actor_name=actor_name, now=found.now)

    def _organization_document(self):
        found = self.rules.draft(model.ORGANIZATION) or self.rules.active(model.ORGANIZATION)
        return json.loads(found['document']) if found else None

    def _replay_entries(self):
        entries = []
        for row in self.rules.recent_decisions(REPLAY):
            try:
                before = model.Decision.from_json(row['decision'])
                facts = model.Facts.from_json(row['facts'])
            except (ValueError, TypeError) as error:
                log.warning('setup plan replay skipped a stored decision it could not read: %s', type(error).__name__)
                continue
            entries.append((row['job_id'], row['to_number'], str(row['created_at']), facts, before))
        return entries

    def _checks(self, plan, found):
        """Each touched scope's resulting rules, checked and replayed; an item the check refuses is blocked."""
        results = {}
        items = all_items(plan)
        active = None
        for scope, target in sorted(plan['targets'].items()):
            kind, scope_id = target['kind'], target['scope_id']
            organization = self._organization_document() if kind != model.ORGANIZATION else None
            chosen = [item for item in items if item['kind'] == 'rule' and item['scope'] == scope
                      and not item['blocked']]
            problems = check(kind, scope_id, target_document(target['base'], chosen), check_context(found),
                             organization=organization)
            errors = [problem for problem in problems if problem.level == 'error']
            for item in chosen:
                own = [problem for problem in errors if problem.rule_id in (None, item['rule']['id'])]
                if own:
                    item['blocked'] = f'Faxbot can’t add this rule: {own[0].message}'
                    item['selected'] = False
            chosen = [item for item in chosen if not item['blocked']]
            document = target_document(target['base'], chosen)
            warnings = [problem for problem in check(kind, scope_id, document, check_context(found),
                                                     organization=organization) if problem.level == 'warning']
            sentence = None
            entries = self._replay_entries()
            if entries and chosen:
                if active is None:
                    active = self.rules.compiled_active()
                compiled = compile_for_replay(kind, scope_id, document, active)
                checked, changed = replay(entries, compiled, found.accounts)
                sentence = (f'{len(changed)} of your last {checked} faxes would have gone another way with these '
                            'rules.' if changed else f'None of your last {checked} faxes would have gone another way.')
            results[scope] = {'warnings': [{'rule_id': problem.rule_id, 'message': problem.message}
                                           for problem in warnings], 'replay': sentence}
        for pack in plan['packs']:
            pack['saving'] = pack_saving(pack['items'])  # an item the check blocked no longer counts
        return results

    # Reading ------------------------------------------------------------------------------------------------

    def get(self, number):
        found = self.plans.get(number)
        if found is None:
            raise PlanNotFound(f'There is no setup plan {number}.')
        found['applications'] = self.plans.applications_of(found['id'])
        return found

    # Apply --------------------------------------------------------------------------------------------------

    def _done(self, applications):
        return {step['part'] for application in applications for step in application['steps']
                if step['outcome'] == 'done'}

    def _chosen(self, plan, keys, done):
        items = {item['key']: item for item in all_items(plan)}
        if keys is None:
            keys = [key for key, item in items.items() if item['selected']]
        chosen = []
        for key in dict.fromkeys(keys):
            item = items.get(key)
            if item is None:
                raise PlanRefused('One of the chosen suggestions is not in this plan. Reload it and choose again.')
            if item['kind'] not in APPLIES:
                raise PlanRefused(f'“{item["title"]}” is a step you take on its own page; Faxbot can’t apply it.')
            if item['blocked']:
                raise PlanRefused(item['blocked'])
            part = SETTINGS if item['kind'] == 'setting' else item['scope']
            if part not in done:
                chosen.append(item)
        if not chosen:
            raise PlanRefused('Choose at least one suggestion that is not applied yet.')
        return chosen

    def _authorized(self, actor, permission):
        from ..access.types import ResourceRef
        store, control = self.access.store, self.access.control
        with store.transaction() as connection:
            store.require_lock_on(connection)
            return control.authorize_on(connection, actor, permission, ResourceRef('installation'),
                                        now=utcnow()).allowed

    def _settings_changes(self, chosen):
        changes = {}
        for item in chosen:
            if item['kind'] == 'setting':
                changes.update(item['changes'])
        return changes

    def apply(self, number, expected_revision, *, snapshot, actor, actor_name=None, items=None):
        found = self.get(number)
        if expected_revision != found['basis']:
            raise PlanStale('This plan changed since you opened it. Reload it and apply again.')
        plan = found['plan']
        chosen = self._chosen(plan, items, self._done(found['applications']))
        changes = self._settings_changes(chosen)
        scopes = sorted({item['scope'] for item in chosen if item['kind'] == 'rule'})

        # Authority and freshness for every part, before anything is written.
        if changes:
            self.access.configuration_access.prepare_settings_write(actor, snapshot,
                                                                    plan['basis']['configuration'])
        for scope in scopes:
            target = plan['targets'][scope]
            kind, scope_id = target['kind'], target['scope_id']
            permission = 'mailboxes:manage' if kind == model.MAILBOX else 'settings:write'
            if not self._authorized(actor, permission):
                raise PlanForbidden('You don’t have permission to change the rules this plan changes.')
            active = self.rules.active(kind, scope_id)
            if (active['number'] if active else None) != target['base_revision'] \
                    or self.rules.draft(kind, scope_id) is not None:
                raise PlanStale('Your sending rules changed since this preview. Preview again to see the plan '
                                'against your current rules.')

        steps, restart = [], False
        if changes:
            step, restart = self._apply_settings(snapshot, changes, actor)
            steps.append(step)
        for scope in scopes:
            if steps and steps[-1]['outcome'] != 'done':
                steps.append({'part': scope, 'outcome': 'not_tried',
                              'sentence': 'Not tried, because the change before it was refused.'})
                continue
            rule_items = [item for item in chosen if item['kind'] == 'rule' and item['scope'] == scope]
            steps.append(self._apply_rules(number, plan['targets'][scope], scope, rule_items, actor, actor_name,
                                            snapshot.desired.values))
        applied = [item['key'] for item in chosen
                   if any(step['outcome'] == 'done' and step['part'] == (SETTINGS if item['kind'] == 'setting'
                                                                          else item['scope']) for step in steps)]
        outcome = ('applied' if all(step['outcome'] == 'done' for step in steps)
                   else 'partial' if applied else 'refused')
        self.plans.record_application(found['id'], outcome=outcome, items=applied, steps=steps,
                                      restart_required=restart, actor_principal_id=getattr(actor, 'principal_id', None),
                                      actor_name=actor_name)
        return {'outcome': outcome, 'items': applied, 'steps': steps, 'restart_required': restart,
                'sentence': _outcome_sentence(outcome, len(applied), restart)}

    def _apply_settings(self, snapshot, changes, actor):
        try:
            after = self.manager.patch_authorized(snapshot, changes, principal=actor, control=self.access.control)
        except ConfigurationConflict:
            return {'part': SETTINGS, 'outcome': 'refused',
                    'sentence': 'Your settings changed at the same moment, so none of these settings were saved. '
                                'Preview again.'}, False
        except (ConfigurationValueError, ConfigurationActivationError) as error:
            return {'part': SETTINGS, 'outcome': 'refused',
                    'sentence': f'These settings were not saved: {error}'}, False
        pending = after.pending is not None
        return {'part': SETTINGS, 'outcome': 'done', 'fields': sorted(changes),
                'sentence': ('Settings saved; they take effect when Faxbot restarts.' if pending
                             else 'Settings saved and in use.')}, pending

    def _apply_rules(self, number, target, scope, items, actor, actor_name, values):
        kind, scope_id = target['kind'], target['scope_id']
        document = target_document(target['base'], items)
        organization = self._organization_document() if kind != model.ORGANIZATION else None
        from ..accounts import sending_accounts
        context = CheckContext(accounts=tuple(sending_accounts(values)),
                               mailboxes=frozenset(box['id'] for box in facts_module.mailboxes(self.engine, values)))
        errors = [problem for problem in check(kind, scope_id, document, context, organization=organization)
                  if problem.level == 'error']
        if errors:
            return {'part': scope, 'outcome': 'refused', 'sentence': f'These rules were not published: '
                                                                     f'{errors[0].message}'}
        try:
            draft = self.rules.save_draft(kind, scope_id, document, expected_version=0, actor=actor,
                                          actor_name=actor_name)
        except RulesConflict:
            return {'part': scope, 'outcome': 'refused',
                    'sentence': 'Someone started changing these rules at the same moment. Preview again.'}
        try:
            revision = self.rules.publish(kind, scope_id, expected_active_revision=target['base_revision'],
                                          expected_draft_version=draft['version'], note=f'From setup plan {number}',
                                          actor=actor, actor_name=actor_name)
        except (RulesConflict, RulesInputError) as error:
            current = self.rules.draft(kind, scope_id)
            if current is not None and current['version'] == draft['version'] \
                    and json.loads(current['document']) == document:
                self.rules.discard_draft(kind, scope_id, actor=actor)
            return {'part': scope, 'outcome': 'refused', 'sentence': f'These rules were not published: {error}'}
        return {'part': scope, 'outcome': 'done', 'revision': revision['number'], 'rules': [item['key'] for item in items],
                'sentence': f'Rules published as version {revision["number"]}.'}


def _outcome_sentence(outcome, count, restart):
    if outcome == 'applied':
        text = f"Applied {count} {'suggestion' if count == 1 else 'suggestions'}."
    elif outcome == 'partial':
        text = (f"Applied {count} {'suggestion' if count == 1 else 'suggestions'}; the rest were refused, and each "
                'part says why.')
    else:
        text = 'Nothing was applied; each part says why.'
    return text + (' Restart Faxbot so the saved settings take effect.' if restart else '')

