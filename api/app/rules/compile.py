"""Read a rules document, report what is wrong with its shape, and compile it once per revision.

``document_problems`` checks the shape of one scope's document and returns
plain-sentence problems; ``compile_document`` turns a sound document into
predicates over a fax's facts. Compiling happens once per revision (cached by
revision ID, 32 entries); a decision then only calls the predicates. Every
trace line a rule can produce is built here too, so deciding allocates almost
nothing per rule.
"""
from collections import OrderedDict
from dataclasses import dataclass
import re
import threading

from . import model
from .model import (CONDITIONS, DAYS, LIMIT_ACTIONS, ROUTE_ACTIONS, ROUTE_SETTINGS, SITE_ACCOUNT_MODES, Step)


_NUMBER = re.compile(r'\+[1-9][0-9]{1,14}')
_PREFIX = re.compile(r'\+[1-9][0-9]{0,14}')
_COUNTRY = re.compile(r'[A-Z]{2}')
# A French commune's INSEE code: five characters, Corsica's starting 2A or 2B (routing/closures.py).
_COMMUNE = re.compile(r'(?:[0-9]{2}|2[AB])[0-9]{3}')
# A US site's state, for pricing calls by where they really start (routing/jurisdiction.py).
_US_STATE = re.compile(r'A[KLRSZ]|C[AOT]|D[CE]|FL|G[AU]|HI|I[ADLN]|K[SY]|LA|M[ADEINOPST]|N[CDEHJMVY]|O[HKR]|P[AR]'
                       r'|RI|S[CD]|T[NX]|UT|V[AIT]|W[AIVY]')
_CURRENCY = re.compile(r'[A-Z]{3}')
_CLOCK = re.compile(r'([01][0-9]|2[0-3]):([0-5][0-9])')
_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}')


class DocumentError(ValueError):
    """The document cannot be compiled; ``problems`` says why."""

    def __init__(self, problems):
        super().__init__(problems[0].message if problems else 'The rules could not be read.')
        self.problems = tuple(problems)


@dataclass(frozen=True)
class Problem:
    """One finding of a check. ``level`` is 'error' (blocks publishing) or 'warning'."""
    level: str
    code: str
    message: str
    rule_id: str | None = None
    path: str | None = None


def _rule_label(rule, index, kind):
    name = rule.get('name') if isinstance(rule, dict) else None
    if isinstance(name, str) and name.strip():
        return f'‘{name.strip()}’'
    return f'{"limit" if kind == "limits" else "routing rule"} {index + 1}'


class _Problems:
    def __init__(self):
        self.items = []

    def error(self, code, message, rule_id=None, path=None):
        self.items.append(Problem('error', code, message, rule_id, path))


def _strings(value, pattern=None, *, limit=10_000):
    return (isinstance(value, list) and len(value) <= limit
            and all(isinstance(item, str) and (pattern is None or pattern.fullmatch(item)) for item in value)
            and len(set(value)) == len(value))


def _window_problem(window):
    """None when ``window`` is a valid time window, else a short reason."""
    if not isinstance(window, dict) or set(window) - {'days', 'from', 'until', 'time_zone'}:
        return 'a time window has days, from, until and time zone only'
    days = window.get('days')
    if days is not None and (not _strings(days) or not set(days) <= set(DAYS) or not days):
        return 'days are mon, tue, wed, thu, fri, sat and sun'
    start, end = window.get('from'), window.get('until')
    if (start is None) != (end is None):
        return 'a time window has both a start and an end, or neither'
    for value in (start, end):
        if value is not None and (not isinstance(value, str) or _CLOCK.fullmatch(value) is None):
            return 'times are written HH:MM, from 00:00 to 23:59'
    if start is not None and start == end:
        return 'a time window cannot start and end at the same time'
    if window.get('time_zone', 'installation') not in model.TIME_ZONES:
        return 'the clock is the installation’s or the sender site’s'
    return None


def _check_conditions(block, where, label, rule_id, problems, path):
    if not isinstance(block, dict):
        problems.error('shape', f'The {where} part of {label} must list conditions.', rule_id, path)
        return 0
    count = 0
    for group, fields in block.items():
        spec = CONDITIONS.get(group)
        if spec is None:
            problems.error('unknown_condition', f'{label} has a condition Faxbot does not know: {group}.', rule_id,
                           f'{path}.{group}')
            continue
        if spec == 'list':
            count += 1
            if not _strings(fields) or not fields:
                problems.error('shape', f'{label}: {group} must list at least one value.', rule_id, f'{path}.{group}')
            continue
        if spec == 'yes_no':
            count += 1
            if not isinstance(fields, bool):
                problems.error('shape', f'{label}: {group.replace("_", " ")} is yes or no.', rule_id, f'{path}.{group}')
            continue
        if spec == 'window':
            count += 1
            reason = _window_problem(fields)
            if reason:
                problems.error('shape', f'{label}: {reason}.', rule_id, f'{path}.{group}')
            continue
        if not isinstance(fields, dict) or not fields:
            problems.error('shape', f'{label}: {group} must hold at least one condition.', rule_id, f'{path}.{group}')
            continue
        for name, value in fields.items():
            kind = spec.get(name)
            here = f'{path}.{group}.{name}'
            count += 1
            if kind is None:
                problems.error('unknown_condition', f'{label} has a condition Faxbot does not know: {group} {name}.',
                               rule_id, here)
            elif kind == 'yes_no' and not isinstance(value, bool):
                problems.error('shape', f'{label}: {group} {name} is yes or no.', rule_id, here)
            elif kind == 'count' and (not isinstance(value, int) or isinstance(value, bool) or value < 0):
                problems.error('shape', f'{label}: {group} {name} is a whole number.', rule_id, here)
            elif kind == 'list':
                pattern = {'numbers': _NUMBER, 'prefixes': _PREFIX, 'countries': _COUNTRY}.get(name)
                if not _strings(value, pattern) or not value:
                    what = {'numbers': 'full numbers starting with + and the country code',
                            'prefixes': 'number beginnings starting with + and the country code',
                            'countries': 'two-letter country codes such as GB'}.get(name, 'values')
                    problems.error('shape', f'{label}: {group} {name} must list {what}.', rule_id, here)
    return count


def cap_micros(cap):
    """``(micros, currency)`` of a cap written as money ({"currency", "amount"}), or None when it is not."""
    from ..routing.costs import InvalidRateCard, parse_amount
    if not isinstance(cap, dict) or set(cap) != {'amount', 'currency'} or not isinstance(cap['currency'], str) \
            or _CURRENCY.fullmatch(cap['currency']) is None or not isinstance(cap['amount'], str):
        return None
    try:
        return parse_amount(cap['amount'], whole_digits=6), cap['currency']
    except InvalidRateCard:
        return None


def _account_keys(value, *, allow_reserved=False):
    return _strings(value) and value and all(
        model.route_key(key) and (allow_reserved or key not in model.RESERVED_KEYS) for key in value)


def _check_then(then, kind, label, rule_id, problems, path, *, scope):
    if not isinstance(then, dict) or not then:
        problems.error('shape', f'{label} must say what to do.', rule_id, path)
        return
    if kind == 'limits':
        unknown = set(then) - set(LIMIT_ACTIONS)
        if unknown:
            problems.error('unknown_action', f'{label} has an action a limit cannot take: {", ".join(sorted(unknown))}.',
                           rule_id, path)
        for action in ('require_direct', 'require_encryption', 'place_a_real_call'):
            if action in then and then[action] is not True:
                problems.error('shape', f'{label}: {action.replace("_", " ")} is turned on with yes.', rule_id, path)
        if 'never' in then and not _account_keys(then['never'], allow_reserved=True):
            problems.error('shape', f'{label}: never must list accounts by their keys.', rule_id, f'{path}.never')
        if 'cap_cost' in then and cap_micros(then['cap_cost']) is None:
            problems.error('shape', f'{label}: a cost cap is an amount such as 0.50 and a three-letter currency.',
                           rule_id, f'{path}.cap_cost')
        if 'hold_for_approval' in then:
            hold = then['hold_for_approval']
            if not isinstance(hold, dict) or set(hold) - {'separate_approver'} or not isinstance(
                    hold.get('separate_approver', False), bool):
                problems.error('shape', f'{label}: approval says only whether the approver must be someone else.',
                               rule_id, f'{path}.hold_for_approval')
        if 'hold_until' in then:
            reason = _window_problem(then['hold_until'])
            if reason or then['hold_until'].get('from') is None and then['hold_until'].get('days') is None:
                problems.error('shape', f'{label}: {reason or "a waiting window names days or times"}.', rule_id,
                               f'{path}.hold_until')
        if 'alternate_number' in then and then['alternate_number'] != 'never':
            problems.error('shape', f'{label}: as a limit, the approved alternate number can only be never used.',
                           rule_id, f'{path}.alternate_number')
        return
    actions = [key for key in then if key in ROUTE_ACTIONS]
    unknown = set(then) - set(ROUTE_ACTIONS) - set(ROUTE_SETTINGS)
    if unknown:
        problems.error('unknown_action', f'{label} has an action a routing rule cannot take: '
                       f'{", ".join(sorted(unknown))}.', rule_id, path)
    if len(actions) != 1:
        problems.error('shape', f'{label} must choose exactly one way to send.', rule_id, path)
        return
    action = actions[0]
    value = then[action]
    if action == 'use' and not (isinstance(value, str) and model.route_key(value)
                                and value not in model.RESERVED_KEYS):
        problems.error('shape', f'{label}: use names one account by its key.', rule_id, f'{path}.use')
    if action in ('try_in_order', 'cheapest_reliable') and not _account_keys(value):
        problems.error('shape', f'{label}: list the accounts by their keys, each once.', rule_id, f'{path}.{action}')
    if action == 'site_accounts' and not (isinstance(value, str) and (value == 'sender'
                                                                        or model.DEFINITION_KEY.fullmatch(value))):
        problems.error('shape', f'{label}: site accounts are the sender’s site’s or a named site’s.', rule_id,
                       f'{path}.site_accounts')
    if action == 'automatic' and value is not True:
        problems.error('shape', f'{label}: the automatic choice is turned on with yes.', rule_id, f'{path}.automatic')
    if 'mode' in then and (action != 'site_accounts' or then['mode'] not in SITE_ACCOUNT_MODES):
        problems.error('shape', f'{label}: the order applies to site accounts only, in order or cheapest first.',
                       rule_id, f'{path}.mode')
    for setting, allowed in (('when_busy', model.WHEN_BUSY), ('page_layout', model.PAGE_LAYOUTS),
                             ('alternate_number', model.ALTERNATE_SETTINGS)):
        if setting in then and then[setting] not in allowed:
            problems.error('shape', f'{label}: {setting.replace("_", " ")} must be one of {", ".join(allowed)}.',
                           rule_id, f'{path}.{setting}')
    if 'subaddress' in then and not (isinstance(then['subaddress'], str)
                                     and model.SUBADDRESS.fullmatch(then['subaddress'])):
        problems.error('shape', f'{label}: a subaddress is up to 20 digits, such as 2001; it may also use +, # and *.',
                       rule_id, f'{path}.subaddress')


def _check_definitions(document, problems):
    lists = document.get('lists', {})
    numbers = 0
    if not isinstance(lists, dict):
        problems.error('shape', 'Recipient groups must be named lists.', path='lists')
        lists = {}
    for key, item in lists.items():
        here = f'lists.{key}'
        if not model.DEFINITION_KEY.fullmatch(str(key)) or not isinstance(item, dict) \
                or set(item) - {'name', 'numbers', 'prefixes'} or not isinstance(item.get('name'), str) \
                or not item['name'].strip() or not _strings(item.get('numbers', []), _NUMBER) \
                or not _strings(item.get('prefixes', []), _PREFIX):
            problems.error('shape', f'The recipient group {key} needs a name and numbers or number beginnings '
                           'starting with + and the country code.', path=here)
            continue
        numbers += len(item.get('numbers', [])) + len(item.get('prefixes', []))
    labels = document.get('labels', [])
    if not _strings(labels) or any(not label.strip() or len(label) > 64 for label in labels):
        problems.error('shape', 'Labels are short names, each listed once.', path='labels')
    regions = document.get('regions', {})
    if not isinstance(regions, dict):
        problems.error('shape', 'Regions must be named lists.', path='regions')
        regions = {}
    for key, item in regions.items():
        if not model.DEFINITION_KEY.fullmatch(str(key)) or not isinstance(item, dict) \
                or set(item) - {'name', 'countries', 'prefixes'} or not isinstance(item.get('name'), str) \
                or not item['name'].strip() or not _strings(item.get('countries', []), _COUNTRY) \
                or not _strings(item.get('prefixes', []), _PREFIX) \
                or not (item.get('countries') or item.get('prefixes')):
            problems.error('shape', f'The region {key} needs a name and countries or number beginnings.',
                           path=f'regions.{key}')
            continue
        numbers += len(item.get('prefixes', []))
    for name, fields in (('sites', {'key', 'name', 'country', 'state', 'time_zone', 'mailboxes', 'groups',
                                    'accounts', 'commune'}),
                         ('workflows', {'key', 'name', 'mailboxes', 'labels'})):
        items = document.get(name, [])
        if not isinstance(items, list):
            problems.error('shape', f'{name.capitalize()} must be a list.', path=name)
            continue
        keys = [item.get('key') for item in items if isinstance(item, dict)]
        if len(set(keys)) != len(keys):
            problems.error('duplicate', f'Two {name} have the same key.', path=name)
        for index, item in enumerate(items):
            here = f'{name}[{index}]'
            if not isinstance(item, dict) or set(item) - fields or not isinstance(item.get('key'), str) \
                    or not model.DEFINITION_KEY.fullmatch(item['key']) or not isinstance(item.get('name'), str) \
                    or not item['name'].strip():
                problems.error('shape', f'Each of the {name} needs a key and a name.', path=here)
                continue
            for listed in ('mailboxes', 'groups', 'accounts', 'labels'):
                if listed in item and not _strings(item[listed]):
                    problems.error('shape', f'{item["name"]}: {listed} must be a list, each listed once.', path=here)
            if name == 'sites':
                if item.get('country') is not None and (not isinstance(item['country'], str)
                                                        or _COUNTRY.fullmatch(item['country']) is None):
                    problems.error('shape', f'{item["name"]}: the country is a two-letter code such as GB.', path=here)
                if item.get('state') is not None and (not isinstance(item['state'], str)
                                                      or _US_STATE.fullmatch(item['state']) is None
                                                      or (item.get('country') or 'US') != 'US'):
                    problems.error('shape', f'{item["name"]}: the state is a two-letter US state code such as CO, '
                                   'for a site in the US.', path=here)
                if item.get('commune') is not None and (not isinstance(item['commune'], str)
                                                        or _COMMUNE.fullmatch(item['commune']) is None
                                                        or (item.get('country') or 'FR') != 'FR'):
                    problems.error('shape', f'{item["name"]}: the commune is a French INSEE code such as 75056, '
                                   'for a site in France.', path=here)
                if item.get('time_zone') is not None and not _valid_zone(item['time_zone']):
                    problems.error('shape', f'{item["name"]}: the time zone is not one Faxbot knows, such as '
                                   'Europe/London.', path=here)
    return numbers


def _valid_zone(name):
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    if not isinstance(name, str) or not name or len(name) > 64:
        return False
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


def document_problems(kind, document):
    """Problems with the shape of one scope's document (errors only; references are checked in check.py)."""
    problems = _Problems()
    if kind not in model.SCOPE_KINDS:
        raise ValueError('Unknown scope.')
    if not isinstance(document, dict):
        problems.error('shape', 'The rules must be a JSON object.')
        return problems.items
    allowed = model.ORGANIZATION_KEYS if kind == model.ORGANIZATION else model.SCOPE_DOCUMENT_KEYS
    unknown = set(document) - set(allowed)
    if unknown:
        where = 'the organization’s rules' if kind == model.ORGANIZATION else 'a mailbox’s or workflow’s rules'
        problems.error('shape', f'These parts don’t belong in {where}: {", ".join(sorted(unknown))}.')
    if document.get('format') != model.FORMAT:
        problems.error('format', 'This rules file is from a different version of Faxbot.', path='format')
    numbers = _check_definitions(document, problems) if kind == model.ORGANIZATION else 0
    ids = []
    for section in ('limits', 'routes'):
        rules = document.get(section, [])
        if not isinstance(rules, list):
            problems.error('shape', f'{"Limits" if section == "limits" else "Routing rules"} must be a list.',
                           path=section)
            continue
        if len(rules) > model.MAX_RULES:
            problems.error('too_many', f'A scope can hold at most {model.MAX_RULES} '
                           f'{"limits" if section == "limits" else "routing rules"}.', path=section)
        for index, rule in enumerate(rules):
            label = _rule_label(rule, index, section)
            path = f'{section}[{index}]'
            if not isinstance(rule, dict):
                problems.error('shape', f'{label} must be an object.', path=path)
                continue
            rule_id = rule.get('id')
            if not isinstance(rule_id, str) or not model.DEFINITION_KEY.fullmatch(rule_id):
                problems.error('shape', f'{label} needs an ID of lower-case letters, digits and dashes.', path=path)
                rule_id = None
            else:
                ids.append(rule_id)
            if set(rule) - set(model.RULE_KEYS):
                problems.error('shape', f'{label} has parts Faxbot does not know: '
                               f'{", ".join(sorted(set(rule) - set(model.RULE_KEYS)))}.', rule_id, path)
            if not isinstance(rule.get('name'), str) or not rule['name'].strip() or len(rule['name']) > 200:
                problems.error('shape', f'{label} needs a name of up to 200 characters.', rule_id, path)
            if not isinstance(rule.get('on', True), bool):
                problems.error('shape', f'{label}: on is yes or no.', rule_id, path)
            if 'mandatory' in rule and (kind != model.ORGANIZATION or not isinstance(rule['mandatory'], bool)):
                problems.error('shape', f'{label}: only the organization’s rules can be mandatory.', rule_id, path)
            count = _check_conditions(rule.get('when', {}), 'when', label, rule_id, problems, f'{path}.when')
            if 'unless' in rule:
                count += _check_conditions(rule['unless'], 'unless', label, rule_id, problems, f'{path}.unless')
            if count > model.MAX_CONDITIONS:
                problems.error('too_many', f'{label} has more than {model.MAX_CONDITIONS} conditions.', rule_id, path)
            for group in (rule.get('when', {}), rule.get('unless', {})):
                if isinstance(group, dict):
                    destination = group.get('destination')
                    if isinstance(destination, dict):
                        numbers += sum(len(destination.get(name) or []) for name in ('numbers', 'prefixes')
                                       if isinstance(destination.get(name), list))
            _check_then(rule.get('then'), section, label, rule_id, problems, f'{path}.then', scope=kind)
    if len(set(ids)) != len(ids):
        problems.error('duplicate', 'Two rules have the same ID.')
    if numbers > model.MAX_LISTED_NUMBERS:
        problems.error('too_many', f'The rules list more than {model.MAX_LISTED_NUMBERS:,} numbers.')
    return problems.items


# Compiling -----------------------------------------------------------------------------------------------------

def _prefixes(destination):
    """Every beginning of a number, so a set of prefixes is matched with at most 16 lookups."""
    return {destination[:end] for end in range(2, len(destination) + 1)}


def _minutes(text):
    hours, minutes = text.split(':')
    return int(hours) * 60 + int(minutes)


@dataclass(frozen=True)
class Window:
    days: frozenset              # weekday numbers, Monday 0
    start: int | None            # minutes after local midnight; None is the whole day
    end: int | None
    zone: str                    # model.TIME_ZONES

    @classmethod
    def read(cls, data):
        days = frozenset(DAYS.index(day) for day in data.get('days') or DAYS)
        start = _minutes(data['from']) if data.get('from') else None
        end = _minutes(data['until']) if data.get('until') else None
        return cls(days, start, end, data.get('time_zone', 'installation'))

    def contains(self, weekday, minute):
        if self.start is None:
            return weekday in self.days
        if self.start < self.end:
            return weekday in self.days and self.start <= minute < self.end
        # Wraps past midnight: the window belongs to the day it starts on.
        return (weekday in self.days and minute >= self.start) or ((weekday - 1) % 7 in self.days
                                                                  and minute < self.end)


@dataclass(frozen=True)
class Definitions:
    """The organization document's lists, regions, sites, workflows and labels."""
    lists: dict
    regions: dict
    sites: tuple
    workflows: tuple
    labels: tuple

    @classmethod
    def read(cls, document):
        lists = {key: model.RecipientList(key, item['name'], tuple(item.get('numbers', ())),
                                          tuple(item.get('prefixes', ())))
                 for key, item in (document.get('lists') or {}).items()}
        regions = {key: model.Region(key, item['name'], tuple(item.get('countries', ())),
                                     tuple(item.get('prefixes', ())))
                   for key, item in (document.get('regions') or {}).items()}
        sites = tuple(model.Site(item['key'], item['name'], item.get('country'), item.get('time_zone'),
                                 tuple(item.get('mailboxes', ())), tuple(item.get('groups', ())),
                                 tuple(item.get('accounts', ())))
                      for item in document.get('sites') or ())
        workflows = tuple(model.Workflow(item['key'], item['name'], tuple(item.get('mailboxes', ())),
                                         tuple(item.get('labels', ())))
                          for item in document.get('workflows') or ())
        return cls(lists, regions, sites, workflows, tuple(document.get('labels') or ()))

    def site(self, key):
        return next((site for site in self.sites if site.key == key), None)


EMPTY_DEFINITIONS = Definitions({}, {}, (), (), ())


class Context:
    """One fax's facts plus what the organization document derives from them, worked out once per decision."""
    __slots__ = ('facts', 'definitions', 'site', 'workflow', '_prefixes', '_clock')

    def __init__(self, facts, definitions):
        self.facts = facts
        self.definitions = definitions
        self.site = sender_site(definitions, facts)
        self.workflow = fax_workflow(definitions, facts)
        self._prefixes = None
        self._clock = {}

    @property
    def prefixes(self):
        if self._prefixes is None:
            self._prefixes = _prefixes(self.facts.destination)
        return self._prefixes

    def zone_name(self, zone):
        if zone == 'sender_site' and self.site is not None and self.site.time_zone:
            return self.site.time_zone
        return self.facts.time_zone

    def clock(self, zone):
        """``(weekday, minute)`` of acceptance on the zone's clock."""
        name = self.zone_name(zone)
        found = self._clock.get(name)
        if found is None:
            local = local_time(self.facts.accepted_at, name)
            found = self._clock[name] = (local.weekday(), local.hour * 60 + local.minute)
        return found


def local_time(accepted_at, zone_name):
    from datetime import datetime, timezone
    from ..people_time import zone
    moment = datetime.fromisoformat(accepted_at).replace(tzinfo=timezone.utc)
    return moment.astimezone(zone(zone_name))


def sender_site(definitions, facts):
    """The first site whose mailboxes include the sending mailbox, else the first sharing one of the sender's groups."""
    if facts.mailbox_id is not None:
        for site in definitions.sites:
            if facts.mailbox_id in site.mailboxes:
                return site
    groups = set(facts.sender.groups)
    if groups:
        for site in definitions.sites:
            if groups.intersection(site.groups):
                return site
    return None


def fax_workflow(definitions, facts):
    """The send form's workflow, else the sending mailbox's, else the first whose labels the fax carries."""
    if facts.workflow is not None:
        return facts.workflow
    if facts.mailbox_id is not None:
        for workflow in definitions.workflows:
            if facts.mailbox_id in workflow.mailboxes:
                return workflow.key
    labels = set(facts.labels)
    if labels:
        for workflow in definitions.workflows:
            if labels.intersection(workflow.labels):
                return workflow.key
    return None


def _predicate(group, name, value, definitions):
    """``(field name, test(context) -> bool)`` for one condition field."""
    field = group if name is None else f'{group}.{name}'
    if group == 'workflows':
        wanted = frozenset(value)
        return field, lambda ctx: ctx.workflow in wanted
    if group == 'labels':
        wanted = frozenset(value)
        return field, lambda ctx: not wanted.isdisjoint(ctx.facts.labels)
    if group == 'time':
        window = Window.read(value)
        return field, lambda ctx: window.contains(*ctx.clock(window.zone))
    if group == 'destination':
        if name == 'numbers':
            wanted = frozenset(value)
            return field, lambda ctx: ctx.facts.destination in wanted
        if name == 'prefixes':
            wanted = frozenset(value)
            return field, lambda ctx: not wanted.isdisjoint(ctx.prefixes)
        if name == 'lists':
            numbers, prefixes = set(), set()
            for key in value:
                listed = definitions.lists.get(key)
                if listed is not None:
                    numbers.update(listed.numbers)
                    prefixes.update(listed.prefixes)
            numbers, prefixes = frozenset(numbers), frozenset(prefixes)
            return field, lambda ctx: ctx.facts.destination in numbers or not prefixes.isdisjoint(ctx.prefixes)
        if name == 'countries':
            wanted = frozenset(value)
            return field, lambda ctx: ctx.facts.country in wanted
        if name == 'regions':
            countries, prefixes = set(), set()
            for key in value:
                region = definitions.regions.get(key)
                if region is not None:
                    countries.update(region.countries)
                    prefixes.update(region.prefixes)
            countries, prefixes = frozenset(countries), frozenset(prefixes)
            return field, lambda ctx: ctx.facts.country in countries or not prefixes.isdisjoint(ctx.prefixes)
        if name == 'recipients':
            wanted = frozenset(value)
            return field, lambda ctx: ctx.facts.recipient_id in wanted
        if name == 'partner':
            return field, lambda ctx: ctx.facts.partner is value
        if name == 'own_number':
            return field, lambda ctx: ctx.facts.own_number is value
        if name == 'approved_alternate':
            return field, lambda ctx: (ctx.facts.alternate is not None) is value
        if name == 'in_sender_country':
            return field, lambda ctx: (ctx.site is not None and ctx.site.country is not None
                                       and ctx.site.country == ctx.facts.country) is value
    if group == 'sender':
        wanted = frozenset(value)
        if name == 'people':
            return field, lambda ctx: ctx.facts.sender.principal_id in wanted
        if name == 'keys':
            return field, lambda ctx: ctx.facts.sender.kind == 'key' and ctx.facts.sender.key_id in wanted
        if name == 'groups':
            return field, lambda ctx: not wanted.isdisjoint(ctx.facts.sender.groups)
        if name == 'mailboxes':
            return field, lambda ctx: ctx.facts.mailbox_id in wanted
        if name == 'sites':
            return field, lambda ctx: ctx.site is not None and ctx.site.key in wanted
    if group == 'document':
        if name == 'pages_over':
            return field, lambda ctx: ctx.facts.pages > value
        if name == 'pages_under':
            return field, lambda ctx: ctx.facts.pages < value
        if name == 'size_over':
            return field, lambda ctx: ctx.facts.size_bytes > value
        if name == 'case_packet':
            return field, lambda ctx: ctx.facts.case_packet is value
    if group == 'urgent':
        return field, lambda ctx: ctx.facts.urgent is value
    if group == 'real_call':
        return field, lambda ctx: ctx.facts.by_call is value
    raise ValueError('Unknown condition.')


def _predicates(block, definitions):
    found = []
    for group, fields in (block or {}).items():
        if CONDITIONS[group] in ('list', 'window', 'yes_no'):
            found.append(_predicate(group, None, fields, definitions))
        else:
            found.extend(_predicate(group, name, value, definitions) for name, value in fields.items())
    return tuple(found)


@dataclass(frozen=True)
class Rule:
    """One enabled rule, compiled; ``evaluate`` returns its prebuilt trace line."""
    id: str
    name: str
    kind: str                   # 'limit' or 'route'
    then: dict
    when: tuple
    unless: tuple
    mandatory: bool
    matched: Step
    unless_step: Step
    not_reached: Step
    failed: dict                # field name -> Step
    source: model.Source

    def evaluate(self, ctx):
        for name, test in self.when:
            if not test(ctx):
                return self.failed[name]
        if self.unless:
            for _, test in self.unless:
                if not test(ctx):
                    return self.matched
            return self.unless_step
        return self.matched

    def step(self, result, note=None):
        """A trace line for a result decided outside the rule's own conditions."""
        return Step(self.matched.kind, result, self.matched.scope, self.matched.scope_id, self.matched.revision,
                    self.id, self.name, None, note)


@dataclass(frozen=True)
class Compiled:
    """One scope's document at one revision, ready to decide with."""
    ref: model.RevisionRef
    definitions: Definitions
    limits: tuple
    routes: tuple
    document: dict | None = None      # the document as compiled, for recompiling lower scopes

    @property
    def scope(self):
        return self.ref.scope

    @property
    def scope_id(self):
        return self.ref.scope_id


def _compile_rule(rule, kind, ref, definitions):
    step_kind = 'limit' if kind == 'limits' else 'route'
    base = dict(kind=step_kind, scope=ref.scope, scope_id=ref.scope_id, revision=ref.number, rule_id=rule['id'],
                rule_name=rule['name'].strip())
    when = _predicates(rule.get('when'), definitions)
    unless = _predicates(rule.get('unless'), definitions)
    return Rule(
        id=rule['id'], name=rule['name'].strip(), kind=step_kind, then=rule['then'], when=when, unless=unless,
        mandatory=bool(rule.get('mandatory', False)), matched=Step(result='matched', **base),
        unless_step=Step(result='unless', field='unless', **base), not_reached=Step(result='not_reached', **base),
        failed={name: Step(result='not_matched', field=name, **base) for name, _ in when},
        source=model.Source('rule', ref.scope, ref.scope_id, ref.number, rule['id'], rule['name'].strip()))


def compile_document(ref, document, definitions=None):
    """Compile one scope's document. ``definitions`` are the organization's, needed by mailbox and workflow scopes."""
    problems = [problem for problem in document_problems(ref.scope, document) if problem.level == 'error']
    if problems:
        raise DocumentError(problems)
    if ref.scope == model.ORGANIZATION:
        definitions = Definitions.read(document)
    definitions = definitions or EMPTY_DEFINITIONS
    limits = tuple(_compile_rule(rule, 'limits', ref, definitions)
                   for rule in document.get('limits', ()) if rule.get('on', True))
    routes = tuple(_compile_rule(rule, 'routes', ref, definitions)
                   for rule in document.get('routes', ()) if rule.get('on', True))
    return Compiled(ref, definitions, limits, routes, document)


_CACHE = OrderedDict()
_CACHE_LOCK = threading.Lock()
CACHE_SIZE = 32


def compiled_revision(ref, document_text, definitions=None, *, definitions_key=None):
    """``compile_document`` cached by revision ID (and, for a lower scope, the organization revision it used)."""
    import json
    key = (ref.revision_id, definitions_key)
    with _CACHE_LOCK:
        found = _CACHE.get(key)
        if found is not None:
            _CACHE.move_to_end(key)
            return found
    compiled = compile_document(ref, json.loads(document_text), definitions)
    with _CACHE_LOCK:
        _CACHE[key] = compiled
        while len(_CACHE) > CACHE_SIZE:
            _CACHE.popitem(last=False)
    return compiled
