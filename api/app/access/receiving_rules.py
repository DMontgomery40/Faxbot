"""Receiving rules: which mailbox, email, urgency and cleanup age a received fax gets (provider-rules design §4.9).

A number rule ("faxes to this number go to this mailbox", ``inbound_rules`` and ``access_mailbox_routes``) may
have options (``inbound_rule_options``, migrations 0030 and 0043):

- conditions: any of your numbers instead of one, the account the fax arrived on, that account's site, the
  subaddress the sender stated (T.33 SUB), the sender's number (exact, or a beginning ending in ``*``), and the
  day and time of receipt in the installation's time zone (a window may run past midnight; it belongs to the day
  it starts on, as in the sending rules);
- actions: email through a chosen connector or not at all, mark the work item urgent, and keep the fax N days
  (cleanup age only, never a legal hold).

Rules are read in their place order; the first that matches places the fax. A rule without options behaves
exactly as number rules always have, and the match keeps their two passes: first an exact match of the number
in international form across every rule, then a match on its digits, so with no options the placement is
exactly the old "oldest rule for this number" one. A subaddress is what the sending machine says, never proof
of who sent the fax: it chooses where the fax is filed and never grants anyone access.

Each received fax records how it was placed (``inbound_fax_routing``): the rule, its version and a snapshot of
the rule as it matched, the account, site and subaddress, the mailbox and email connector, urgency, keep days,
and whether the receipt time came from the provider or from the import. Rule and mailbox IDs are kept as plain
values, so changing a rule later never rewrites how an earlier fax was placed.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import re
import weakref

import sqlalchemy as sa


OPTION_FIELDS = ('enabled', 'any_number', 'account_key', 'site_key', 'subaddress', 'from_numbers', 'days',
                 'start_minute', 'end_minute', 'email_connector_id', 'email_off', 'urgent', 'keep_days',
                 'diverted_from', 'diversion_unsigned')
CONDITION_FIELDS = ('account_key', 'site_key', 'subaddress', 'from_numbers', 'days', 'start_minute', 'end_minute',
                    'diverted_from')
# How far a received call's diversion was checked (inbound/diversion.py): the network signed it and the signature
# checked, it was signed but not checked, its signature failed, or only a header stated it.
DIVERSION_SIGNED, DIVERSION_UNCHECKED, DIVERSION_FAILED, DIVERSION_STATED = 'signed', 'unchecked', 'failed', 'stated'
DAYS = ('mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun')
MAX_FROM = 50
MAX_KEEP_DAYS = 36500
_SUBADDRESS = re.compile(r'[0-9#*+]{1,20}')
_TABLES = weakref.WeakKeyDictionary()


DIVERSION_STATES = ('signed', 'unchecked', 'failed', 'stated')


class ReceivingRuleError(ValueError):
    """One plain sentence about a rule's options."""


@dataclass(frozen=True)
class ReceivedFacts:
    """What Faxbot knows about one received fax when it places it."""
    to_number: str | None
    from_number: str | None = None
    account_key: str | None = None
    site_key: str | None = None
    subaddress: str | None = None
    # The number the call was forwarded from (X4), as the network stated it, and how far that was checked
    # (DIVERSION_*). A diversion is the network's statement about the call, never proof of who sent the fax.
    diverted_from: str | None = None
    diversion: str | None = None
    # Naive UTC: the provider's reported time when it gave one, else when Faxbot recorded the fax.
    received_at: datetime | None = None
    time_source: str = 'import'
    time_zone: str = ''

    def local(self):
        """(weekday, minute after midnight) of receipt on the installation's clock."""
        from ..people_time import zone
        moment = (self.received_at or datetime.utcnow()).replace(tzinfo=timezone.utc)
        local = moment.astimezone(zone(self.time_zone))
        return local.weekday(), local.hour * 60 + local.minute


def tables(engine):
    """{'options', 'routing'} reflected once per engine, or None before migration 0030."""
    found = _TABLES.get(engine)
    if found is None:
        try:
            metadata = sa.MetaData()
            metadata.reflect(engine, only=['inbound_rule_options', 'inbound_fax_routing'])
        except sa.exc.SQLAlchemyError:
            return None
        found = {'options': metadata.tables['inbound_rule_options'], 'routing': metadata.tables['inbound_fax_routing']}
        _TABLES[engine] = found
    return found


def normalize_subaddress(value):
    """A subaddress as Faxbot compares it: T.30's numeric characters (digits, + # *), spaces removed, at most 20;
    None for none or anything else."""
    if not isinstance(value, str):
        return None
    text = ''.join(value.split())
    return text if _SUBADDRESS.fullmatch(text) else None


def _flag(value):
    return bool(value) and value not in (0, '0')


def has_conditions(options):
    if not options:
        return False
    return any(options.get(name) not in (None, '', [], ()) for name in CONDITION_FIELDS)


def _from_list(value):
    if value in (None, ''):
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def _day_list(value):
    if not value:
        return []
    if isinstance(value, str):
        value = value.split(',')
    return [day for day in DAYS if day in set(value)]


def row_options(row):
    """The options of one stored options row as plain values (the API's ReceivingOptions without position)."""
    if row is None:
        return None
    return {
        'enabled': _flag(row['enabled']), 'any_number': _flag(row['any_number']), 'account_key': row['account_key'],
        'site_key': row['site_key'], 'subaddress': row.get('subaddress'),
        'from_numbers': _from_list(row['from_numbers']), 'days': _day_list(row['days']),
        'start_minute': row['start_minute'], 'end_minute': row['end_minute'],
        'email_connector_id': row['email_connector_id'], 'email_off': _flag(row['email_off']),
        'urgent': _flag(row['urgent']), 'keep_days': row['keep_days'],
        'diverted_from': row.get('diverted_from'), 'diversion_unsigned': _flag(row.get('diversion_unsigned')),
    }


DEFAULT_OPTIONS = {'enabled': True, 'any_number': False, 'account_key': None, 'site_key': None, 'subaddress': None,
                   'from_numbers': [], 'days': [], 'start_minute': None, 'end_minute': None,
                   'email_connector_id': None, 'email_off': False, 'urgent': False, 'keep_days': None,
                   'diverted_from': None, 'diversion_unsigned': False}


def clean_options(given, current=None, *, country='US', subaddress_column=True):
    """The options after a change from the API (only the fields given change). Raises ReceivingRuleError."""
    from ..routing.numbers import InvalidNumber, normalize_number
    options = dict(current or DEFAULT_OPTIONS)
    for name, value in given.items():
        if name not in OPTION_FIELDS:
            continue
        if name in ('enabled', 'any_number', 'email_off', 'urgent', 'diversion_unsigned'):
            if type(value) is not bool:
                raise ReceivingRuleError('Use true or false for the switches of a number rule.')
            options[name] = value
        elif name in ('account_key', 'site_key', 'email_connector_id'):
            if value is not None and (not isinstance(value, str) or not 0 < len(value.strip()) <= 64):
                raise ReceivingRuleError('Choose an account, site or email connector from the list.')
            options[name] = value.strip() if isinstance(value, str) else None
        elif name == 'subaddress':
            if value in (None, ''):
                options[name] = None
            else:
                clean = normalize_subaddress(value) if isinstance(value, str) else None
                if clean is None:
                    raise ReceivingRuleError('A subaddress is up to 20 digits, such as 2001; it may also use +, # and *.')
                if not subaddress_column:
                    raise ReceivingRuleError('Upgrade the database before saving a subaddress.')
                options[name] = clean
        elif name == 'diverted_from':
            if value in (None, ''):
                options[name] = None
            else:
                try:
                    options[name] = normalize_number(value.strip(), country=country) if isinstance(value, str) else None
                except (InvalidNumber, ValueError):
                    options[name] = None
                if options[name] is None:
                    raise ReceivingRuleError('Enter the number calls were forwarded from with its country code, such '
                                             'as +13035550100.')
        elif name == 'from_numbers':
            if value is None:
                value = []
            if not isinstance(value, list) or len(value) > MAX_FROM:
                raise ReceivingRuleError(f'List at most {MAX_FROM} sender numbers.')
            found = []
            for entry in value:
                if not isinstance(entry, str) or not entry.strip():
                    continue
                text = entry.strip()
                if text.endswith('*'):
                    digits = ''.join(character for character in text[:-1] if character.isdigit())
                    if not digits:
                        raise ReceivingRuleError(f'{text[:40]} does not start with any digits.')
                    found.append(('+' if text.startswith('+') else '') + digits + '*')
                else:
                    try:
                        found.append(normalize_number(text, country=country))
                    except (InvalidNumber, ValueError):
                        raise ReceivingRuleError(f'{text[:40]} is not a fax number Faxbot can read. End it with * to '
                                                 'match every number that starts with it.') from None
            options[name] = list(dict.fromkeys(found))
        elif name == 'days':
            if value is None:
                value = []
            if not isinstance(value, list) or any(day not in DAYS for day in value):
                raise ReceivingRuleError('Days are mon, tue, wed, thu, fri, sat and sun.')
            options[name] = _day_list(value)
        elif name in ('start_minute', 'end_minute'):
            if value is not None and (type(value) is not int or not 0 <= value <= 1439):
                raise ReceivingRuleError('Times are minutes after midnight, from 0 to 1439.')
            options[name] = value
        elif name == 'keep_days':
            if value is not None and (type(value) is not int or not 1 <= value <= MAX_KEEP_DAYS):
                raise ReceivingRuleError('Keep a fax for 1 day or more, or leave it empty for the usual cleanup.')
            options[name] = value
    if (options['start_minute'] is None) != (options['end_minute'] is None):
        raise ReceivingRuleError('Give both a start and an end time, or neither.')
    if options['email_off'] and options['email_connector_id']:
        raise ReceivingRuleError('Choose an email connector or no email, not both.')
    return options


def stored_values(options):
    """Column values for an options row."""
    return {
        'enabled': int(options['enabled']), 'any_number': int(options['any_number']),
        'account_key': options['account_key'], 'site_key': options['site_key'],
        'from_numbers': json.dumps(options['from_numbers']) if options['from_numbers'] else None,
        'days': ','.join(options['days']) or None, 'start_minute': options['start_minute'],
        'end_minute': options['end_minute'], 'email_connector_id': options['email_connector_id'],
        'email_off': int(options['email_off']), 'urgent': int(options['urgent']), 'keep_days': options['keep_days'],
        **({'subaddress': options['subaddress']} if 'subaddress' in options else {}),
        **({'diverted_from': options['diverted_from'], 'diversion_unsigned': 1 if options.get('diversion_unsigned')
            else None} if 'diverted_from' in options else {}),
    }


# -- reading rules in order ------------------------------------------------------------------------------------

def ordered_rules(connection, access_tables, receiving, *, enabled_mailboxes_only=True):
    """Every number rule with its mailbox and options, in place order.

    A rule's place is its options row's ``place``; a rule without options keeps its place in creation order,
    so the oldest rule for a number still comes first.
    """
    rules, routes = access_tables['inbound_rules'], access_tables['access_mailbox_routes']
    resources, mailboxes = access_tables['access_resources'], access_tables['mailboxes']
    columns = [rules.c.id, rules.c.to_number, rules.c.created_at, routes.c.mailbox_id, routes.c.version,
               mailboxes.c.label, resources.c.id.label('resource_id'), resources.c.enabled.label('mailbox_enabled')]
    source = (rules.join(routes, routes.c.id == rules.c.id)
              .join(mailboxes, mailboxes.c.id == routes.c.mailbox_id)
              .join(resources, sa.and_(resources.c.kind == 'mailbox', resources.c.mailbox_id == mailboxes.c.id)))
    options = receiving['options'] if receiving is not None else None
    stored_names = ('place', 'version', 'enabled', 'any_number', 'account_key', 'site_key', 'subaddress',
                    'from_numbers', 'days', 'start_minute', 'end_minute', 'email_connector_id', 'email_off', 'urgent',
                    'keep_days', 'diverted_from', 'diversion_unsigned')
    if options is not None:
        columns += [options.c[name].label('option_' + name) for name in stored_names if name in options.c]
        source = source.outerjoin(options, options.c.id == rules.c.id)
    query = sa.select(*columns).select_from(source)
    if enabled_mailboxes_only:
        query = query.where(resources.c.enabled == 1)
    found = []
    every = connection.execute(sa.select(rules.c.id).order_by(rules.c.created_at, rules.c.id)).scalars().all()
    rank = {identity: number for number, identity in enumerate(every)}
    for row in connection.execute(query).mappings():
        row = dict(row)
        option = {name: row.get('option_' + name) for name in stored_names}
        stored = row_options(option) if option['place'] is not None else None
        row['place'] = option['place']
        found.append({'id': row['id'], 'to_number': row['to_number'], 'created_at': row['created_at'],
                      'mailbox_id': row['mailbox_id'], 'mailbox_label': row['label'], 'version': row['version'],
                      'resource_id': row['resource_id'], 'options': stored,
                      'options_version': option['version'] if stored is not None else None,
                      'place': row['place'] if stored is not None else rank.get(row['id'], len(every))})
    found.sort(key=lambda rule: (rule['place'], rule['created_at'], rule['id']))
    return found


def _sender_matches(entries, number):
    if not entries:
        return True
    if not number:
        return False
    digits = ''.join(character for character in number if character.isdigit())
    for entry in entries:
        if entry.endswith('*'):
            prefix = ''.join(character for character in entry[:-1] if character.isdigit())
            if digits.startswith(prefix):
                return True
        elif entry == number:
            return True
    return False


def conditions_hold(options, facts):
    """Whether a fax meets every condition of a rule's options (a rule without options has none)."""
    if not options:
        return True
    if not options['enabled']:
        return False
    if options['account_key'] and options['account_key'] != facts.account_key:
        return False
    if options['site_key'] and options['site_key'] != facts.site_key:
        return False
    if options.get('subaddress') and options['subaddress'] != normalize_subaddress(facts.subaddress):
        return False
    if not _sender_matches(options['from_numbers'], facts.from_number):
        return False
    if options.get('diverted_from') and not diversion_matches(options, facts):
        return False
    if options['days'] or options['start_minute'] is not None:
        from ..rules.compile import Window
        window = Window(frozenset(DAYS.index(day) for day in (options['days'] or DAYS)), options['start_minute'],
                        options['end_minute'], 'installation')
        if not window.contains(*facts.local()):
            return False
    return True


def diversion_matches(options, facts):
    """Whether a call forwarded from the rule's number meets it: the network signed the diversion and the
    signature checked, or the rule also takes an unsigned or unchecked statement. A failed signature never matches."""
    if not facts.diverted_from or ''.join(filter(str.isdigit, facts.diverted_from)) != ''.join(
            filter(str.isdigit, options['diverted_from'])):
        return False
    if facts.diversion == DIVERSION_SIGNED:
        return True
    return bool(options.get('diversion_unsigned')) and facts.diversion in (DIVERSION_UNCHECKED, DIVERSION_STATED)


def diversion_rules_exist(engine):
    """Whether an enabled receiving rule depends on where a call was forwarded from (then its signature is
    checked); False before migration 0054."""
    found = tables(engine) if engine is not None else None
    if found is None or 'diverted_from' not in found['options'].c:
        return False
    options = found['options']
    with engine.connect() as connection:
        return connection.execute(sa.select(options.c.id).where(options.c.diverted_from.is_not(None),
                                                                 options.c.enabled == 1).limit(1)).first() is not None


def uses_diversion(rules):
    """Whether any enabled rule depends on where a call was forwarded from (then a diversion is checked)."""
    return any((rule.get('options') or {}).get('enabled') and (rule.get('options') or {}).get('diverted_from')
               for rule in rules)


def choose(rules, facts, country):
    """The rule that places a fax, or None for the usual unassigned placement.

    Two passes, as number rules always matched: an exact match in international form (or any number) across
    every rule in order, then a match on the number's digits for rules saved before numbers were stored in
    international form.
    """
    from ..routing.numbers import is_canonical, stored_number
    number = (facts.to_number or '').strip()[:64] or None
    canonical = stored_number(number, country=country) if number else None
    digits = (''.join(character for character in number if character.isdigit()) or number) if number else None
    candidates = [rule for rule in rules if conditions_hold(rule['options'], facts)]

    def any_number(rule):
        return bool(rule['options'] and rule['options']['any_number'])
    if number is not None or any(any_number(rule) for rule in candidates):
        for rule in candidates:
            if any_number(rule):
                return rule
            if number is not None and is_canonical(canonical) and rule['to_number'].strip() \
                    and stored_number(rule['to_number'].strip(), country=country) == canonical:
                return rule
    if number is None:
        return None
    for rule in candidates:
        if any_number(rule) or not rule['to_number'].strip():
            continue
        if (''.join(character for character in rule['to_number'] if character.isdigit())
                or rule['to_number'].strip()) == digits:
            return rule
    return None


def snapshot(rule):
    """The rule as it matched, for the placement record."""
    return json.dumps({'to_number': rule['to_number'], 'mailbox_id': rule['mailbox_id'],
                       'mailbox_label': rule['mailbox_label'], 'options': rule['options']},
                      ensure_ascii=True, separators=(',', ':'), sort_keys=True)


def record_on(connection, receiving, inbound_id, rule, facts, now, *, mailbox_id=None):
    """Write how one received fax was placed; once per fax, never changed."""
    if receiving is None:
        return
    routing = receiving['routing']
    options = (rule or {}).get('options') or {}
    values = dict(id=inbound_id, rule_id=rule['id'] if rule else None,
                  rule_version=(rule['version'] or None) if rule else None,
                  rule_snapshot=snapshot(rule) if rule else None, account_key=facts.account_key,
                  site_key=facts.site_key, mailbox_id=rule['mailbox_id'] if rule else mailbox_id,
                  connector_id=options.get('email_connector_id'),
                  urgent=1 if options.get('urgent') else (0 if rule else None),
                  keep_days=options.get('keep_days'),
                  received_time_source=facts.time_source if facts.time_source in ('provider', 'import') else None,
                  created_at=now)
    if 'subaddress' in routing.c:
        values['subaddress'] = normalize_subaddress(facts.subaddress)
    if 'diverted_from' in routing.c and facts.diverted_from:
        values['diverted_from'], values['diversion'] = facts.diverted_from[:32], facts.diversion
    connection.execute(routing.insert().values(**values))


def rule_order(connection, access_tables, receiving):
    """Every number rule's ID in place order, bound to a mailbox or not."""
    rules = access_tables['inbound_rules']
    options = receiving['options'] if receiving is not None else None
    if options is None:
        return list(connection.execute(sa.select(rules.c.id).order_by(rules.c.created_at, rules.c.id)).scalars())
    rows = connection.execute(sa.select(rules.c.id, rules.c.created_at, options.c.place)
                              .select_from(rules.outerjoin(options, options.c.id == rules.c.id))
                              .order_by(rules.c.created_at, rules.c.id)).all()
    ranked = [(row.place if row.place is not None else rank, row.created_at, row.id) for rank, row in enumerate(rows)]
    return [identity for _, _, identity in sorted(ranked)]


def options_on(connection, receiving, rule_ids):
    """{rule id: (options, options version)} for the rules that have options."""
    if receiving is None or not rule_ids:
        return {}
    options = receiving['options']
    found = {}
    for row in connection.execute(sa.select(options).where(options.c.id.in_(sorted(rule_ids)))).mappings():
        found[row['id']] = (row_options(dict(row)), row['version'])
    return found


def write_options(connection, receiving, rule_id, options, now, *, place=None):
    """Create or change one rule's options row; its version counts each change. ``place`` None keeps it."""
    table = receiving['options']
    values = stored_values({name: options[name] for name in OPTION_FIELDS if name in options})
    for name in ('subaddress', 'diverted_from', 'diversion_unsigned'):
        if name not in table.c:
            values.pop(name, None)
    current = connection.execute(sa.select(table.c.version, table.c.place).where(table.c.id == rule_id)).first()
    if current is None:
        connection.execute(table.insert().values(id=rule_id, place=place if place is not None else 0, version=1,
                                                 created_at=now, updated_at=now, **values))
    else:
        connection.execute(table.update().where(table.c.id == rule_id).values(
            version=current.version + 1, updated_at=now, **values,
            **({'place': place} if place is not None else {})))


def renumber(connection, receiving, ordered_ids, now):
    """Give every rule its place in this order; a rule without options gets a row that changes nothing else."""
    table = receiving['options']
    existing = dict(connection.execute(sa.select(table.c.id, table.c.place)).all())
    for place, rule_id in enumerate(ordered_ids):
        if rule_id not in existing:
            defaults = {name: value for name, value in stored_values(DEFAULT_OPTIONS).items() if name in table.c}
            connection.execute(table.insert().values(id=rule_id, place=place, version=1, created_at=now,
                                                     updated_at=now, **defaults))
        elif existing[rule_id] != place:
            connection.execute(table.update().where(table.c.id == rule_id).values(place=place))


def routing_for(connection, receiving, inbound_id):
    """How one received fax was placed, or None."""
    if receiving is None:
        return None
    routing = receiving['routing']
    row = connection.execute(sa.select(routing).where(routing.c.id == inbound_id)).mappings().first()
    return dict(row) if row is not None else None


def _local_moment(text, zone_name):
    """A local time at the installation ("2026-10-07T18:30") as naive UTC; now for none."""
    from ..people_time import zone
    if not text:
        return datetime.utcnow()
    try:
        moment = datetime.fromisoformat(text.strip())
    except ValueError:
        raise ReceivingRuleError('Write the time as a date and a time, such as 2026-10-07 18:30.') from None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=zone(zone_name))
    return moment.astimezone(timezone.utc).replace(tzinfo=None)


def _join(parts):
    return parts[0] if len(parts) == 1 else ', '.join(parts[:-1]) + ' and ' + parts[-1]


def explain(access_store, intake, values, *, to_number, from_number=None, account_key=None, subaddress=None,
            at=None, diverted_from=None, diversion=None):
    """Where a received fax with these facts would go (the console's "Try a received fax"); nothing is saved.

    ``intake`` is the email store (``intake.store.IntakeStore``), or None when email delivery can't be read.
    """
    from ..routing.numbers import stored_number
    country = getattr(values, 'fax_default_country', 'US') or 'US'
    zone_name = getattr(values, 'time_zone', '') or ''
    to = stored_number(to_number.strip(), country=country) if to_number and to_number.strip() else None
    sender = stored_number(from_number.strip(), country=country) if from_number and from_number.strip() else None
    site = None
    if account_key:
        from ..accounts import account_named
        account = account_named(values, account_key)
        site = account.site if account is not None else None
    moment = at if isinstance(at, datetime) else _local_moment(at, zone_name)
    facts = ReceivedFacts(to_number=to, from_number=sender, account_key=account_key, site_key=site,
                          subaddress=subaddress, received_at=moment, time_zone=zone_name,
                          diverted_from=stored_number(diverted_from.strip(), country=country)
                          if diverted_from and diverted_from.strip() else None,
                          diversion=diversion if diversion in DIVERSION_STATES else DIVERSION_SIGNED)
    with access_store.engine.connect() as connection:
        rule = choose(ordered_rules(connection, access_store.tables, tables(access_store.engine)), facts, country)
    options = (rule or {}).get('options') or {}
    connectors = intake.list_connectors() if intake is not None else []
    if options.get('email_off'):
        connector = None
    elif options.get('email_connector_id'):
        connector = next((item for item in connectors if item.id == options['email_connector_id'] and item.enabled),
                         None)
    else:
        connector = intake.connector_for(to) if intake is not None and to else None
    email = ', '.join(connector.settings.recipients) if connector is not None else None
    parts = []
    if options.get('urgent'):
        parts.append('marked urgent')
    parts.append(f'emailed to {email}' if email else 'not emailed')
    if options.get('keep_days'):
        parts.append(f"kept for {options['keep_days']} day{'s' if options['keep_days'] != 1 else ''}")
    if rule is None:
        sentence = f'No number rule matches it, so it would wait in Received with no mailbox, {_join(parts)}.'
    else:
        target = 'any of your numbers' if options.get('any_number') else rule['to_number']
        sentence = f"It would go to {rule['mailbox_label']}, {_join(parts)}, by the rule for {target}."
    return {'sentence': sentence, 'mailbox_label': rule['mailbox_label'] if rule else None, 'email': email,
            'urgent': bool(options.get('urgent')), 'keep_days': options.get('keep_days'),
            'rule_to_number': (rule['to_number'] or None) if rule else None}


def email_off_for(snapshot_text):
    """Whether the rule that placed a fax said to send no email (read from its snapshot)."""
    try:
        options = (json.loads(snapshot_text or '{}') or {}).get('options') or {}
    except ValueError:
        return False
    return bool(options.get('email_off'))
