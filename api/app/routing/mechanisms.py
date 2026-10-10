"""Every way Faxbot saves money on faxes, and how each one stands on this installation.

``CATALOGUE`` is the one list of Faxbot's money-saving mechanisms. Each entry says what the mechanism does for the
administrator's money, where it sits on a fax's path (``STAGES``), how far it is proven (``EVIDENCE``), its part
on Savings & optimization → Savings results and the console page that holds its setting. ``evaluate`` reads this installation's effective
settings and stored records, never the network, and says for each entry:

- **On or off**, from the settings and each recipient's or partner's own choice;
- **Works here**, from what the installation has: its sending routes and their prices, its own SIP trunk and fast
  fax service, its partners and its receiving numbers. When something is missing, one sentence names it;
- **Tested**, as two separate facts: the product evidence level (fixed per entry, from the README roadmap) and
  what happened on this installation, from the same Savings part, so the map and Savings & optimization → Savings results never disagree.

The Overview's map and ``faxbot savings mechanisms`` show the result of GET /routing/savings/mechanisms. Neither
shows money: what each mechanism saved stays on Savings & optimization → Savings results, under the entry's ``part``.

A new mechanism adds its entry here. ``api/tests/test_savings_mechanisms.py`` fails when a Savings part has no
entry, an entry names a setting, page or Savings part that does not exist, or has no evidence level.
"""
from dataclasses import dataclass
from datetime import timedelta
from functools import cached_property
from typing import Callable

import sqlalchemy as sa

from .database import read_connection, reflect, utcnow
from .store import WINDOW_DAYS


# A fax's path, in the order a fax meets each stage, then the advice that saves money once you act on it.
STAGES = (('document', 'Preparing the document'), ('route', 'Choosing the route'), ('call', 'On the call'),
          ('after', 'After the call'), ('receiving', 'Receiving'), ('advice', 'Advice you act on'))
# The stages on a fax's own path; advice sits beside it.
PATH = ('document', 'route', 'call', 'after', 'receiving')
# Said once, under the advice stage's title.
STAGE_SENTENCES = {'advice': 'Advice only: nothing changes until you act on it.'}

# How far Faxbot has proven a mechanism; the lower level whenever the roadmap is in doubt.
EVIDENCE = {
    'live': 'Live call',
    'lab': 'Test lab',
    'built': 'Sample data',
}

TITLE = 'How Faxbot saves money'
SENTENCE = 'Every way Faxbot saves money, in the order a fax meets them.'
LEGEND = (
    ('On or Off', 'Whether it is switched on for your faxes.'),
    ('Works here or Not here', 'Whether this installation has what it needs, such as its own phone line or a '
                               'partner.'),
    ('Live call, Test lab or Sample data', 'How far Faxbot has proven it: on a live carrier call, in its own test '
                                           'lab, or with sample data only.'),
)

# Mechanisms with no part on Savings & optimization → Savings results, each with the reason. Whoever gives one a part removes it here.
# The reason is the map's "on this installation" sentence for that mechanism.
# (Each works out its wait afresh from the delivery records whenever it schedules a fax and stores no count.)
NO_PART = {
    'busy_hours': 'Faxbot keeps no count of the faxes that waited',
    'free_line': 'Faxbot keeps no count of the faxes that waited',
    'answer_cap': 'Its capped calls are not counted on Savings yet',
    'station_check': "What it caught is in each fax's Sent details, not on Savings",
    'keys_after_answer': "The keys each call pressed are in its Sent details, not on Savings",
}


@dataclass(frozen=True)
class State:
    """How one mechanism stands here: on, and works here, each with one sentence when there is more to say."""
    on: bool
    works: bool
    on_sentence: str | None = None
    works_sentence: str | None = None
    # When what turns it on is set elsewhere this time (header text for page marks): that page and its name.
    page: tuple | None = None


@dataclass(frozen=True)
class Mechanism:
    key: str
    name: str
    # What it does for the administrator's money, in one sentence.
    sentence: str
    stage: str
    evidence: str
    # Its part on Savings & optimization → Savings results: the key in GET /routing/savings and the anchor on the Savings page.
    part: str | None
    # The console page that holds its setting, and its name as the console shows it.
    page: str
    page_label: str
    check: Callable
    # The configuration values it reads (ConfigurationValues fields); per-recipient homes name only a page.
    settings: tuple = ()
    # What its Savings part counts: (key in the part, one, many).
    counts: tuple = ('faxes', 'fax', 'faxes')
    verb: str = 'Used on'
    # For an entry with no Savings part: where its own figures or advice already are (a console address, its
    # name, and the command that prints them).
    link: str | None = None
    link_label: str | None = None
    command: str | None = None

    @property
    def destination(self):
        """Where selecting it leads: its part on Savings & optimization → Savings results, else its own page, else nowhere."""
        return f'costs/savings?part={self.part}' if self.part else self.link


class Installation:
    """What this installation has, read once from its settings and stored records; no network."""

    def __init__(self, values, routes, engine, *, saved, days):
        self.values, self.routes, self.engine, self.saved, self.days = values, routes, engine, saved, days

    def count(self, table, *where):
        found = reflect(self.engine, (table,))[table]
        with read_connection(self.engine) as connection:
            return int(connection.scalar(sa.select(sa.func.count()).select_from(found).where(
                *(condition(found.c) for condition in where))) or 0)

    @cached_property
    def sending(self):
        """The accounts that send, switched on, in configured order."""
        from ..accounts import all_accounts
        return [account for account in all_accounts(self.values) if account.sends and account.enabled]

    def card(self, account):
        return self.routes.card_for_route(account.key, account.provider)

    @cached_property
    def sending_names(self):
        from .plan import route_label
        names = []
        for account in self.sending:
            name = account.label or route_label(account.provider)
            if name not in names:
                names.append(name)
        return names

    def through(self, verb='send'):
        """How this installation sends, for a sentence: 'you send through HumbleFax only'."""
        names = self.sending_names
        if not names:
            return 'no sending provider is set up yet'
        listed = names[0] if len(names) == 1 else ', '.join(names[:-1]) + ' and ' + names[-1]
        return f"you {verb} through {listed}{' only' if len(names) == 1 else ''}"

    @cached_property
    def trunk_sends(self):
        return any(account.provider == 'sip' for account in self.sending)

    @cached_property
    def trunk_receives(self):
        from ..inbound.sip_handover import receives_over_trunk
        return bool(getattr(self.values, 'inbound_enabled', False)) and receives_over_trunk(self.values)

    @cached_property
    def receiving_names(self):
        from ..accounts import receiving_accounts
        from .plan import route_label
        names = []
        for account in receiving_accounts(self.values):
            name = account.label or route_label(account.provider)
            if name not in names:
                names.append(name)
        return names

    @cached_property
    def partners(self):
        """Verified partners: (receives fax images from us, per their own statement)."""
        peers = reflect(self.engine, ('direct_peers',))['direct_peers']
        with read_connection(self.engine) as connection:
            return [bool(row.partner_receives_fax_images) for row in connection.execute(
                sa.select(peers.c.partner_receives_fax_images).where(peers.c.state == 'verified'))]

    @cached_property
    def direct_on(self):
        return bool(getattr(self.values, 'direct_delivery_enabled', False))

    def used(self, mechanism):
        """(count, sentence) for what the mechanism did here, from its Savings part."""
        key, one, many = mechanism.counts
        part = self.saved.get(mechanism.part) or {}
        count = int(part.get(key) or 0)
        if count:
            return count, f"{mechanism.verb} {count} {one if count == 1 else many} in {self.days} days"
        return 0, f'Not used in {self.days} days'


# -- each mechanism's check ---------------------------------------------------------------------------------------

def _recipients(count, who='recipients'):
    return f"{count} {who[:-1] if count == 1 else who}"


def _agreed(count, none, who='recipients', what='who agreed'):
    """On for the recipients who agreed, or off with what is still needed."""
    if count:
        return True, f'On for {_recipients(count, who)} {what}.'
    return False, none


def _needs_sending(here):
    return None if here.sending else 'Needs a way to send faxes; no sending provider is set up yet.'


def case_packets(here):
    on, sentence = _agreed(here.count('delivery_destinations', lambda c: c.accepts_references == 1),
                           'No recipient accepts a list of the documents they already have yet.',
                           what='who accept a list of documents they already have')
    return State(on, bool(here.sending), sentence, _needs_sending(here))


def dense_pages(here):
    from ..pages.capability import records_for
    records = records_for(here.engine)
    on, waiting, off = [], [], []
    for account in here.sending:
        allowed, chosen = records.route_long_pages(account.key)
        (on if allowed else off if chosen else waiting).append(account.label)
    clauses = []
    if waiting:
        # Off by default for a cloud provider until someone checks it sends long pages unchanged (route_default).
        clauses.append(f"off for {' and '.join(waiting)} until you check that "
                       f"{'it sends' if len(waiting) == 1 else 'they send'} long pages unchanged")
    if off:
        clauses.append(f"turned off for {' and '.join(off)}")
    never = here.count('recipient_page_settings', lambda c: c.packing == 'never')
    if never:
        clauses.append(f'off for {_recipients(never)}')
    sentence = None
    if clauses:
        joined = '; '.join(clauses) + '.'
        sentence = f'On; {joined}' if on else joined[:1].upper() + joined[1:]
    return State(bool(on), bool(here.sending), sentence, _needs_sending(here))


def encoded_pages(here):
    on, sentence = _agreed(here.count('codec_numbers', lambda c: c.enabled == 1),
                           'No recipient has agreed to encoded pages yet.')
    return State(on, bool(here.sending), sentence, _needs_sending(here))


def fax_friendly(here):
    from ..pages.friendly import billed_by_time, documents_choice
    choice = documents_choice(here.values)
    sentence = None
    if choice != 'never':
        sentence = 'On where it saves time' if choice == 'where_it_saves' else 'On for every fax'
        never = here.count('fax_friendly_recipients', lambda c: c.shading == 'never')
        sentence += f'; off for {_recipients(never)}.' if never else '.'
    timed = any(billed_by_time(here.card(account), account.provider) for account in here.sending)
    why = _needs_sending(here) or (None if timed else (
        f'Saves time only on calls billed by time; {here.through()}, billed by the page or by a flat plan.'))
    return State(choice != 'never', why is None, sentence, why)


def own_numbers(here):
    from .own_numbers import receiving_numbers
    numbers = receiving_numbers(here.values)
    return State(bool(getattr(here.values, 'local_delivery_enabled', True)), bool(numbers), None,
                 None if numbers else 'Needs a number this Faxbot receives faxes on; it receives on none yet.')


def _partner_needed(here, *, images=False):
    if not here.partners:
        return 'Needs a verified partner; you have none yet.'
    if images and not any(here.partners):
        return 'Needs a verified partner that takes fax images; none of yours has said it does.'
    return None


def direct_delivery(here):
    why = _partner_needed(here)
    return State(here.direct_on, why is None, None, why)


def fax_images(here):
    why = _partner_needed(here, images=True)
    return State(here.direct_on, why is None, None if here.direct_on else 'Off while direct delivery is off.', why)


def reuse(here):
    why = _partner_needed(here)
    return State(here.direct_on, why is None, None if here.direct_on else 'Off while direct delivery is off.', why)


def toll_free(here):
    from .tollfree import TollFreeApprovals
    approved = sum(1 for row in TollFreeApprovals(here.engine).all_current().values() if row['action'] == 'approved')
    on, sentence = _agreed(approved, 'No recipient has approved a toll-free number yet.',
                           what='who approved a toll-free number')
    return State(on, bool(here.sending), sentence, _needs_sending(here))


def _compare_why(here):
    """Why routes cannot be compared by cost here, or None when two sending routes charge for each fax."""
    priced, plans, unpriced = [], [], []
    for account in here.sending:
        card = here.card(account)
        (unpriced if card is None else plans if card.flat_plan else priced).append(account.label)
    if len(here.sending) < 2:
        return f'Needs a second sending route to compare with; {here.through()}.'
    if len(priced) < 2:
        reasons = []
        if plans:
            reasons.append(f"{' and '.join(plans)} {'is a monthly plan' if len(plans) == 1 else 'are monthly plans'}")
        if unpriced:
            reasons.append(f"Faxbot has no prices for {' and '.join(unpriced)}")
        return f"Needs two sending routes that charge for each fax; {'; '.join(reasons)}."
    return None


def cheapest_route(here):
    why = _compare_why(here)
    return State(True, why is None, 'On; a preferred route for a number still comes first.', why)


def plan_first(here):
    plans = [account for account in here.sending if (card := here.card(account)) is not None
             and card.monthly_fee_micros]
    why = None if plans else f'Needs a monthly plan among your sending routes; {here.through()}, with no plan.'
    return State(True, bool(plans), 'On; each plan keeps to its monthly budget.' if plans else None, why)


def relay(here):
    active = here.count('relay_agreements', lambda c: c.role == 'sender', lambda c: c.state == 'active')
    on, sentence = _agreed(active, 'Off until you and a partner both sign a relay agreement.', who='partners',
                           what='that relay your faxes')
    why = _partner_needed(here)
    return State(on, why is None, sentence, why)


def busy_hours(here):
    return State(True, bool(here.sending), 'On; urgent faxes still go at once.', _needs_sending(here))


def _shared_calls(here):
    """Why sending together cannot save here, or None when it can."""
    if not here.trunk_sends:
        return f'Needs your own SIP trunk; {here.through()}.'
    from ..batching.policy import _trunk_label, card_saves, card_sentence
    card = here.routes.card_for('sip')
    if card_saves(card):
        return None
    return card_sentence(card, _trunk_label(getattr(here.values, 'sip_trunk_preset', '')))


def sending_together(here):
    on, sentence = _agreed(here.count('batching_numbers', lambda c: c.enabled == 1),
                           'No recipient has agreed to share calls yet.')
    why = _shared_calls(here)
    return State(on, why is None, sentence, why)


def separator_pages(here):
    from ..batching.policy import header_identifies_sender
    table = reflect(here.engine, ('batching_numbers',))['batching_numbers']
    with read_connection(here.engine) as connection:
        layouts = [row.boundaries for row in connection.execute(sa.select(table.c.boundaries).where(
            table.c.enabled == 1, table.c.boundaries.in_(('index_page', 'page_headers'))))]
    marks_wait = 'page_headers' in layouts and not header_identifies_sender(here.values)
    working = [layout for layout in layouts if layout == 'index_page' or not marks_wait]
    sentence = None
    if working:
        sentence = f'On for {_recipients(len(working))} who agreed.'
    elif not layouts:
        sentence = 'No recipient has agreed to an index page or page marks yet.'
    page = None
    if marks_wait:
        # Faxbot falls back to separator pages by itself (batching.policy.HEADER_NEEDS), and says why; what turns
        # page marks on then is the header text, on Sender identity.
        sentence = ((sentence + ' ') if working else '') + (
            'Marks at the top of every page need your header text and sending number set in Delivery setup → '
            'Sending identity, so faxes to those recipients use separator pages for now.')
        page = ('numbers/identity', 'Delivery setup → Sending identity')
    why = _shared_calls(here)
    return State(bool(working), why is None, sentence, why, page=page)


def sslfax(here):
    from ..hylafax_engine import engine_conf_path
    why = None
    if not here.trunk_sends:
        why = f'Needs your own SIP trunk; {here.through()}.'
    elif not engine_conf_path(here.values).is_file():
        why = "Needs Faxbot's fax engine, which starts when you select Apply and connect on the trunk page."
    return State(bool(getattr(here.values, 'sip_sslfax_enabled', True)), why is None, None, why)


def continuation(here):
    return State(True, bool(here.sending), 'On; you choose it for each fax whose call broke.', _needs_sending(here))


def partner_repair(here):
    why = _partner_needed(here, images=True)
    return State(here.direct_on, why is None, None if here.direct_on else 'Off while direct delivery is off.', why)


def blocked_senders(here):
    from ..inbound.screening import ScreeningStore
    active = len(ScreeningStore(here.engine).active())
    on, sentence = _agreed(active, 'No sender is blocked yet.', who='numbers', what='on your blocked list')
    why = None if here.trunk_receives else (
        'Needs faxes received over your own SIP trunk; '
        + (f"you receive through {' and '.join(here.receiving_names)}." if here.receiving_names
           else 'this Faxbot does not receive faxes yet.'))
    return State(on, why is None, sentence, why)


def fax_over_ip(here):
    from .. import sip_fax_mode
    from ..provider_labels import trunk_name
    on = bool(getattr(here.values, 'sip_t38_enabled', True))
    sentence = why = None
    record = None if on else sip_fax_mode.read(here.values)
    reason = record.get('reason') if record and record.get('mode') == 'audio' else None
    if reason in (sip_fax_mode.NETWORK, sip_fax_mode.CARRIER):
        # Faxbot switched to audio fax by itself because T.38 cannot work on this network or carrier: it does not
        # work here, and there is nothing to turn on until that changes (it turns T.38 back on by itself).
        said = sip_fax_mode.off_sentence(reason, carrier=trunk_name(getattr(here.values, 'sip_trunk_preset', '')))
        said = said.removeprefix('Off: ')
        why = said[:1].upper() + said[1:]
    elif reason == sip_fax_mode.NO_DATA_BACK:
        # One call got no fax data back; the trunk page's "Try T.38 again" is the way back.
        sentence = sip_fax_mode.off_sentence(reason)
    if not here.trunk_sends and not here.trunk_receives:
        why = f'Needs your own SIP trunk; {here.through()}.'
    return State(on, why is None, sentence, why)


def digital_routes(here):
    from ..digital.accounts import digital_accounts
    from ..digital.store import DigitalStore
    accounts = [account for account in digital_accounts(here.values) if account.enabled]
    on, sentence = _agreed(len(DigitalStore(here.engine).confirmed_ids()),
                           'No recipient has a confirmed Direct address or FHIR server yet.', who='addresses',
                           what='you confirmed')
    why = None if accounts else 'Needs a Direct messaging (HISP) or FHIR account; none is set up yet.'
    return State(on, why is None, sentence, why)


def measured_coding(here):
    why = None if here.trunk_sends else f"Needs your own SIP trunk and Faxbot's fax engines; {here.through()}."
    return State(True, why is None, None, why)


def partner_tunnel(here):
    peers = reflect(here.engine, ('direct_peers',))['direct_peers']
    with read_connection(here.engine) as connection:
        ready = connection.scalar(sa.select(sa.func.count()).select_from(peers).where(
            peers.c.state == 'verified', peers.c.partner_peer_calls == 1, peers.c.peer_call_address.is_not(None)))
    why = None
    if not here.partners:
        why = 'Needs a verified partner; you have none yet.'
    elif not ready:
        why = ("Needs a verified partner that takes fax calls over a private tunnel; none of yours has said it "
               'does.')
    return State(here.direct_on, why is None, None if here.direct_on else 'Off while direct delivery is off.', why)


def free_line(here):
    return State(True, bool(here.sending), 'On; each number takes one call at a time unless you allow more.',
                 _needs_sending(here))


def charge_checks(here):
    why = None if here.sending or here.receiving_names else 'Needs a fax provider; none is set up yet.'
    return State(True, why is None, None, why)


def measured_account(here):
    why = None if len(here.sending) >= 2 else f'Needs two or more sending accounts to compare; {here.through()}.'
    return State(True, why is None, None, why)


def caller_id_prices(here):
    rates = here.count('origin_class_rates', lambda c: c.superseded_at.is_(None))
    sentence = None if rates else 'No prices that depend on the caller ID are imported yet.'
    return State(bool(rates), bool(here.sending), sentence, _needs_sending(here))


def dialing_guard(here):
    from .guard import policy_on
    with read_connection(here.engine) as connection:
        policy = policy_on(connection)
    chosen = sum(1 for setting in policy.settings.values()
                 if setting.reason == 'administrator' and setting.state != 'default')
    sentence = f"On, with your own choice for {chosen} {'kind' if chosen == 1 else 'kinds'} of number." if chosen \
        else None
    return State(True, bool(here.sending), sentence, _needs_sending(here))


def header_notice(here):
    from ..header_notice import notices_on
    with read_connection(here.engine) as connection:
        found = notices_on(connection)
    mailboxes = len(found.get('mailboxes') or {})
    if found.get('organization'):
        on, sentence = True, 'On for your organization.'
    elif mailboxes:
        on, sentence = True, f"On for {mailboxes} {'mailbox' if mailboxes == 1 else 'mailboxes'}."
    else:
        on, sentence = False, 'No header notice is set yet.'
    return State(on, bool(here.sending), sentence, _needs_sending(here))


def answer_cap(here):
    from .stations import bills_by_minute, cap_on
    trunks = [account for account in here.sending if account.provider == 'sip']
    why = None
    if not trunks:
        why = f'Needs your own SIP trunk for sending; {here.through()}.'
    elif not any(bills_by_minute(here.card(account)) for account in trunks):
        why = ("Used only where calls are billed by the minute in steps of 60 seconds or more; your trunk's prices "
               'are not.')
    return State(cap_on(here.values), why is None, None, why)


def route_problems(here):
    from .route_families import available, open_incidents
    found = len(open_incidents(here.engine)) if available(here.engine) else 0
    sentence = f"On; {found} route {'problem is' if found == 1 else 'problems are'} open now." if found else None
    return State(True, bool(here.sending), sentence, _needs_sending(here))


def power_aware(here):
    from ..power import settings
    why = None if settings(here.engine) else 'Needs a UPS that Faxbot reads through NUT; none is set.'
    return State(why is None, why is None, None, why)


def keys_after_answer(here):
    keys = reflect(here.engine, ('recipient_after_answer',))['recipient_after_answer']
    newest = {}
    with read_connection(here.engine) as connection:
        for row in connection.execute(sa.select(keys.c.phone_number, keys.c.digits)
                                      .order_by(keys.c.created_at, keys.c.id)):
            newest[row.phone_number] = row.digits
    on, sentence = _agreed(sum(1 for digits in newest.values() if digits),
                           'No recipient has keys to press after it answers yet.',
                           what='behind a phone menu')
    why = None if here.trunk_sends else f'Needs your own SIP trunk for sending; {here.through()}.'
    return State(on, why is None, sentence, why)


# -- advice: each section of Savings & optimization → Opportunities, and the advice kept on its own page ---------------------------

def _always(here):
    return State(True, True)


def _sends(here):
    return State(True, bool(here.sending), None, _needs_sending(here))


def _compares(here):
    return State(True, _compare_why(here) is None, None, _compare_why(here))


def _receives(here):
    why = None if here.receiving_names else 'Needs a number this Faxbot receives faxes on; it receives none yet.'
    return State(True, why is None, None, why)


def _has_plan(here):
    from ..accounts import all_accounts
    plans = [account for account in all_accounts(here.values) if account.enabled and (account.sends or account.receives)
             and (card := here.card(account)) is not None and card.monthly_fee_micros]
    why = None if plans else 'Needs a monthly plan; none of your fax services has one.'
    return State(True, bool(plans), None, why)


def _on_trunk(here):
    why = None if here.trunk_sends or here.trunk_receives else f'Needs your own SIP trunk; {here.through()}.'
    return State(True, why is None, None, why)


def _sends_on_trunk(here):
    why = None if here.trunk_sends else f'Needs your own SIP trunk for sending; {here.through()}.'
    return State(True, why is None, None, why)


def _two_trunks(here):
    from ..accounts import all_accounts
    trunks = [account for account in all_accounts(here.values) if account.provider == 'sip' and account.enabled]
    why = None if len(trunks) >= 2 else (
        'Needs two or more trunks to compare; you have one.' if trunks else
        f'Needs two or more trunks to compare; {here.through()}.')
    return State(True, why is None, None, why)


def _with_partner(here):
    why = _partner_needed(here)
    return State(True, why is None, None, why)


def _in_the_us(here):
    country = getattr(here.values, 'fax_default_country', 'US')
    why = _needs_sending(here) or (None if country == 'US' else 'Prices by state are for calls within the US.')
    return State(True, why is None, None, why)


def _telnyx_numbers(here):
    preset = getattr(here.values, 'sip_trunk_preset', '')
    why = None
    if preset != 'telnyx':
        why = 'Needs a Telnyx trunk; this lookup is a Telnyx charge.'
    elif not getattr(here.values, 'telnyx_api_key', ''):
        why = 'Needs your Telnyx key saved on the Telnyx page.'
    return State(True, why is None, None, why)


def _line_inventory(here):
    from .inventory import inventory_rows
    why = None if inventory_rows(here.engine) else 'Needs your line inventory; none is imported yet.'
    return State(True, why is None, None, why)


def _country_rules(here):
    from .country_rules import view
    why = None if view(here.engine, here.values)['accounts'] else \
        'None of your accounts is in a country whose service rules Faxbot knows.'
    return State(True, why is None, None, why)


def _copper_closures(here):
    from .closures import view
    found = view(here.engine, here.values)
    why = None if found['sites'] or found['lines'] else \
        'For phone lines in France; none of your sites has its commune set, and no line has a closing notice.'
    return State(True, why is None, None, why)


def _advice(key, name, sentence, section, command, check):
    return Mechanism(key, name, sentence, 'advice', 'built', None, 'costs/recommendations', 'Savings & optimization → Opportunities',
                     check, link=f'costs/recommendations?section={section}', link_label='Savings & optimization → Opportunities',
                     command=command)


# -- the catalogue --------------------------------------------------------------------------------------------------
# Evidence: the README roadmap's own words for each (cited by entry in the savings-map report). Only sending
# together (a local T.38 test line) and SSL Fax (the loopback proof) have run in the test lab; none has run live.

CATALOGUE = (
    Mechanism('case_packets', 'Case packets',
              'Leaves documents a recipient already accepted out of a case packet, so fewer pages are sent.',
              'document', 'built', 'case_packets', 'recipients/list', 'Recipients → Details', case_packets,
              counts=('packets', 'case packet', 'case packets')),
    Mechanism('dense_pages', 'Dense pages',
              'Stacks short pages onto longer fax pages where the receiving machine allows, and leaves out blank '
              'page bottoms, so fewer pages and seconds are billed.',
              'document', 'built', 'packing', 'providers/sending', 'Delivery setup → Providers & accounts → Delivery routes',
              dense_pages),
    Mechanism('encoded_pages', 'Encoded pages (experimental)',
              "Sends a whole document as a few dense fax pages that the recipient's Faxbot turns back into the "
              'original, for recipients who agreed.',
              'document', 'built', 'encoding', 'recipients/list', 'Recipients → Details', encoded_pages),
    Mechanism('fax_friendly', 'Lighter shading',
              'Lightens shaded areas and removes specks before a page goes on a line billed by time, so pages '
              'take less time to send.',
              'document', 'built', 'fax_friendly', 'providers/sending', 'Delivery setup → Providers & accounts → Delivery routes',
              fax_friendly, settings=('fax_friendly_documents',)),
    Mechanism('own_numbers', 'Faxes to your own numbers',
              'Delivers a fax to one of your own numbers straight into Received, with no phone call.',
              'route', 'built', 'own_numbers', 'providers/sending', 'Delivery setup → Providers & accounts → Delivery routes',
              own_numbers, settings=('local_delivery_enabled',)),
    Mechanism('direct_delivery', 'Direct delivery',
              'Sends documents to verified partners over the internet instead of placing a fax call.',
              'route', 'built', 'direct_delivery', 'recipients/partners', 'Recipients → Partners',
              direct_delivery, settings=('direct_delivery_enabled',)),
    Mechanism('fax_images', 'Fax images to partners',
              'Sends a partner the exact fax pages a call would carry, over the internet, with no phone call.',
              'route', 'built', 'direct_fax_images', 'recipients/partners', 'Recipients → Partners',
              fax_images, settings=('direct_delivery_enabled',)),
    Mechanism('reuse', 'Send once and reuse',
              'Sends a partner only what it lacks: one copy for several of its numbers, a reference to a document '
              'it already holds, or only the changes from the version it has.',
              'route', 'built', 'direct_bytes', 'recipients/partners', 'Recipients → Partners', reuse,
              settings=('direct_delivery_enabled',), counts=('documents', 'document', 'documents')),
    Mechanism('partner_tunnel', 'Partner fax over a private tunnel',
              "Places a fax call straight to a partner's fax engine inside your own encrypted tunnel, with no "
              'carrier and nothing per minute.',
              'route', 'lab', 'tunnel_calls', 'recipients/partners', 'Recipients → Partners', partner_tunnel,
              settings=('direct_delivery_enabled',)),
    Mechanism('relay', 'Partner relays',
              'Sends faxes abroad through a partner that places them as local calls in its own country.',
              'route', 'built', 'relay', 'recipients/partners', 'Recipients → Partners', relay),
    Mechanism('toll_free', 'Approved toll-free numbers',
              "Dials a recipient's toll-free fax number once they approve it; the recipient pays for those calls.",
              'route', 'built', 'toll_free', 'recipients/list', 'Recipients → Details', toll_free),
    Mechanism('cheapest_route', 'Cheapest route per delivered fax',
              'Sends each number by the route that cost least per delivered fax once two routes have proven '
              'themselves.',
              'route', 'built', 'cheapest_route', 'recipients/list', 'Recipients → Details', cheapest_route),
    Mechanism('plan_first', "Your plan's faxes first",
              'Uses a monthly plan while it has room under its budget, before routes that charge per fax.',
              'route', 'built', 'plan_first', 'costs/prices', 'Savings & optimization → Prices & plans', plan_first,
              settings=('plan_budgets',)),
    Mechanism('busy_hours', 'Calls at the right hour',
              "Waits out a number's usual busy hours when a failed call there could be charged.",
              'route', 'built', None, 'recipients/list', 'Recipients → Details', busy_hours),
    Mechanism('free_line', 'Wait for a free line',
              'Calls a number only while it has a free line, so a fax waits instead of paying for a busy call.',
              'route', 'built', None, 'recipients/list', 'Recipients → Details', free_line),
    Mechanism('digital_routes', 'Direct messages and FHIR',
              "Delivers to a recipient's confirmed Direct address or FHIR server instead of placing a fax call.",
              'route', 'built', 'digital', 'recipients/list', 'Recipients → Details', digital_routes),
    Mechanism('fax_over_ip', 'Fax over IP (T.38)',
              'Carries fax pages as data over your SIP trunk, so pages go through faster than audio fax and calls '
              'are shorter.',
              'call', 'live', 't38', 'providers/trunk', 'Delivery setup → Carrier trunk', fax_over_ip,
              settings=('sip_t38_enabled',), counts=('calls', 'call', 'calls')),
    Mechanism('measured_coding', 'Smallest page coding',
              "Measures each fax's pages in every coding the call may use and sends the smallest, so pages take "
              'less time on the line.',
              'call', 'lab', 'coding', 'providers/trunk', 'Delivery setup → Carrier trunk', measured_coding,
              settings=('sip_fax_compression',)),
    Mechanism('sending_together', 'Sending together',
              'Sends several short faxes to the same number in one call, on a line that charges for each call.',
              'call', 'lab', 'sending_together', 'recipients/list', 'Recipients → Details', sending_together),
    Mechanism('separator_pages', 'Fewer separator pages',
              'Marks each document in a shared call with an index page or a line on each page instead of a '
              'separator page.',
              'call', 'built', 'separator_pages', 'recipients/list', 'Recipients → Details', separator_pages,
              counts=('calls', 'shared call', 'shared calls')),
    Mechanism('sslfax', 'Faster pages',
              'Sends pages over SSL Fax when the other fax machine offers it, so calls take less time.',
              'call', 'lab', 'sslfax', 'providers/trunk', 'Delivery setup → Carrier trunk', sslfax,
              settings=('sip_sslfax_enabled',)),
    Mechanism('continuation', 'Only the missing pages',
              'Finishes a fax whose call broke by sending only the pages the call did not confirm.',
              'after', 'built', 'continuation', 'faxes/sent', 'Faxes → Sent', continuation),
    Mechanism('partner_repair', 'Missing pages to partners',
              'After a broken call to a partner, sends only the pages the partner is missing, with no new call.',
              'after', 'built', 'partner_repair', 'recipients/partners', 'Recipients → Partners', partner_repair,
              settings=('direct_delivery_enabled',)),
    Mechanism('charge_checks', 'Charge checks',
              'Matches what your carriers and fax services billed against your faxes, and points out charges and '
              "invoice amounts your faxes don't explain.",
              'after', 'built', None, 'costs/charges', 'Savings & optimization → Charges', charge_checks,
              link='costs/charges', link_label='Savings & optimization → Charges', command='faxbot savings charges'),
    Mechanism('blocked_senders', 'Junk callers turned away',
              'Declines calls from blocked numbers before Faxbot answers, so they are never answered or received.',
              'receiving', 'built', 'blocked_calls', 'numbers/blocked', 'Delivery setup → Blocked senders',
              blocked_senders, counts=('calls', 'call', 'calls'), verb='Turned away'),
    _advice('advice_sending', 'Cheaper routes per number',
            'Names numbers whose usual route cost more per delivered fax than another, with a button to switch.',
            'sending', 'faxbot savings opportunities sending', _compares),
    _advice('advice_plans', 'Plans worth their fee',
            'Says whether each monthly plan, such as an unlimited fax plan, is worth its fee at your traffic.',
            'plans', 'faxbot savings opportunities plans', _has_plan),
    _advice('advice_receiving', 'Receiving lines and numbers',
            'Says which of your numbers could share lines on your trunk, and which quiet numbers cost more to keep '
            'than they bring in.',
            'receiving', 'faxbot savings opportunities receiving', _receives),
    _advice('advice_carriers', "Other carriers' prices",
            "Prices your last 30 days of faxes at each carrier's published prices, so you can see what switching "
            'would change.',
            'carriers', 'faxbot savings opportunities carriers', _sends),
    _advice('advice_billing_steps', 'Calls just past a billed minute',
            'Finds numbers whose calls end just past a billed minute, where one page less or a faster mode would '
            'cost less.',
            'steps', 'faxbot savings opportunities billing-steps', _on_trunk),
    _advice('advice_partners', 'Partner candidates',
            'Names the numbers whose faxes cost the most again and again, so you can enroll them as direct partners.',
            'partners', 'faxbot savings opportunities partners', _sends),
    _advice('advice_discovery', 'Recipients that run Faxbot',
            'Finds recipients whose fax line is answered by a Faxbot, so you can send to them with no call.',
            'discovery', 'faxbot recipients partners discover', _sends_on_trunk),
    _advice('advice_relays', 'Partners that could relay',
            "Names partners whose signed local price would have cost less than your own calls.",
            'relays', 'faxbot recipients partners relay', _with_partner),
    _advice('advice_toll_free', 'Toll-free numbers on file',
            'Lists recipients with a toll-free fax number, so you can record their approval and stop paying for '
            'those calls.',
            'tollFree', 'faxbot savings opportunities toll-free', _sends),
    _advice('advice_fax_marker', 'Fax marker on calls',
            'Compares calls marked as fax with calls that were not, on delivery and cost per delivered fax.',
            'marker', 'faxbot savings opportunities fax-marker', _sends_on_trunk),
    _advice('advice_shading', 'Time lighter shading would save',
            'While Lighter shading is set to Never, measures your recent faxes and says how much time it would '
            'have saved.',
            'pages', 'faxbot savings opportunities shading', _sends),
    _advice('advice_trunks', 'Your trunks compared',
            "Compares your trunks' monthly fees and busy times, and says when one trunk's faxes fit on another.",
            'trunks', 'faxbot savings opportunities trunks', _two_trunks),
    _advice('advice_numbers', 'Where each number should live',
            'Says where each of your fax numbers costs least to receive on, and the steps to move it.',
            'numbers', 'faxbot savings opportunities numbers', _receives),
    _advice('advice_sites', 'Calls by state',
            "Says whether your carriers price US calls by state, and when another site's trunk would cost less.",
            'sites', 'faxbot savings opportunities sites', _in_the_us),
    Mechanism('advice_caller_names', 'Caller-name lookup',
              'Shows which of your Telnyx numbers pay for caller-name lookup, which Faxbot never uses, so you can '
              'turn it off.',
              'advice', 'built', None, 'providers/trunk', 'Delivery setup → Carrier trunk', _telnyx_numbers,
              link='providers/trunk', link_label='Delivery setup → Carrier trunk',
              command='faxbot delivery providers trunk telnyx names'),
    Mechanism('advice_setup_packs', 'Suggested packs',
              'Gathers the settings and rules that would save money here into packs you review and apply.',
              'advice', 'built', None, 'system/setup', 'Administration → Setup', _always,
              link='system/setup', link_label='Administration → Setup', command='faxbot admin setup plan'),
    Mechanism('measured_account', 'Account chosen after measuring its pages',
              "Measures the pages each of your sending accounts would really send, then sends by the account whose "
              'fax costs least.',
              'route', 'built', None, 'delivery/connections', 'Delivery setup → Providers & accounts', measured_account,
              link='faxes/sent', link_label='Faxes → Sent', command='faxbot faxes sent'),
    Mechanism('caller_id_prices', 'Prices by the caller ID a call shows',
              'Prices each call by the caller ID it shows, where a carrier charges less for some caller IDs, so '
              'the cheapest route is chosen on the real price.',
              'route', 'built', None, 'savings/prices', 'Savings & optimization → Prices & plans', caller_id_prices,
              link='savings/prices', link_label='Savings & optimization → Prices & plans',
              command='faxbot delivery providers trunk caller-ids'),
    Mechanism('dialing_guard', 'Where Faxbot may dial',
              'Holds a fax to a kind of number you have not allowed, such as premium-rate numbers or a new country, '
              'so no call is billed there by mistake.',
              'route', 'built', None, 'delivery/connections', 'Delivery setup → Providers & accounts', dialing_guard,
              link='delivery/connections', link_label='Delivery setup → Providers & accounts',
              command='faxbot delivery providers destinations list'),
    Mechanism('power_aware', 'Power-aware sending',
              'Starts a call only when your UPS has the runtime to finish it, so a power cut does not waste a '
              'call and its pages.',
              'route', 'built', None, 'admin/health', 'Administration → System health', power_aware,
              link='admin/health', link_label='Administration → System health',
              command='faxbot admin diagnostics power show'),
    Mechanism('header_notice', 'Header notice instead of a cover page',
              'Prints your notice in a band at the top of every page, so you can leave out the cover page and send '
              'one page fewer.',
              'document', 'built', None, 'delivery/identity', 'Delivery setup → Sending identity', header_notice,
              link='delivery/identity', link_label='Delivery setup → Sending identity',
              command='faxbot delivery identity notice show'),
    Mechanism('answer_cap', '50-second answer cap',
              'Hangs up a call that no fax machine answers within 50 seconds, on a trunk billed by the whole minute, '
              'so an unanswered call is not billed a second minute.',
              'call', 'lab', None, 'providers/trunk', 'Delivery setup → Carrier trunk', answer_cap,
              settings=('sip_fax_answer_cap',)),
    Mechanism('station_check', 'Station check',
              'Compares the fax number the far machine shows with the number you dialled before any page, and says '
              'so in Sent details, or hangs up before any page where you choose, so a wrong number is caught.',
              'call', 'lab', None, 'delivery/identity', 'Delivery setup → Sending identity', _sends_on_trunk),
    Mechanism('keys_after_answer', 'Keys after answer',
              'Presses the keys a phone menu asks for before the fax machine answers, so a fax to a number behind a '
              'menu goes through instead of failing and being retried.',
              'call', 'lab', None, 'recipients/list', 'Recipients → Details', keys_after_answer),
    Mechanism('route_problems', 'Route problems told apart from number problems',
              'Notices when calls through one route fail together, holds that route back and keeps the lesson off '
              'each number, so working numbers are not retried or abandoned for a route problem.',
              'after', 'built', None, 'admin/health', 'Administration → System health', route_problems,
              link='admin/health', link_label='Administration → System health',
              command='faxbot admin diagnostics routes list'),
    Mechanism('advice_line_inventory', 'Lines and carrier closing dates',
              'Matches your fax lines to carriers\' discontinuance lists and contract end dates, so you move a line '
              'before its price changes or it closes.',
              'advice', 'built', None, 'delivery/moves', 'Delivery setup → Number moves', _line_inventory,
              link='delivery/moves', link_label='Delivery setup → Number moves',
              command='faxbot delivery numbers move inventory'),
    Mechanism('advice_copper_closures', 'Copper closures',
              'Shows when the copper network under your French lines closes, commune by commune, so you move each '
              'line in time.',
              'advice', 'built', None, 'delivery/moves', 'Delivery setup → Number moves', _copper_closures,
              link='delivery/moves', link_label='Delivery setup → Number moves',
              command='faxbot delivery numbers closures'),
    Mechanism('advice_registered_senders', 'Registered senders',
              'Sends a recipient that accepts faxes only from a registered number by the account and caller ID '
              'registered with it, so its faxes are not refused and sent again.',
              'advice', 'built', None, 'recipients/list', 'Recipients → Details', _sends,
              link='recipients/list', link_label='Recipients → Details',
              command='faxbot recipients registered-senders'),
    Mechanism('advice_country_rules', 'Country service rules',
              "Shows the rules a country sets for fax services, with their sources, and which of your accounts "
              'you confirmed meet them.',
              'advice', 'built', None, 'delivery/connections', 'Delivery setup → Providers & accounts', _country_rules,
              link='delivery/connections', link_label='Delivery setup → Providers & accounts',
              command='faxbot delivery providers accounts country-rules'),
)
BY_KEY = {mechanism.key: mechanism for mechanism in CATALOGUE}


def _label(page, label, values):
    """A page's name as the console shows it; the trunk page is named after its carrier ("Delivery setup → Telnyx")."""
    if page == 'providers/trunk':
        from ..provider_labels import trunk_name
        return f"Delivery setup → {trunk_name(getattr(values, 'sip_trunk_preset', ''))}"
    return label


def _view(mechanism, here):
    state = mechanism.check(here)
    # One short line about this installation only: what its Savings part counted, or why nothing is counted.
    # Advice and charge checks have none: their own pages say what they found.
    if mechanism.part:
        count, used = here.used(mechanism)
    else:
        count, used = 0, NO_PART.get(mechanism.key)
    return {
        'key': mechanism.key, 'name': mechanism.name, 'sentence': mechanism.sentence,
        'enabled': {'on': state.on, 'label': 'On' if state.on else 'Off', 'sentence': state.on_sentence},
        'works': {'here': state.works, 'label': 'Works here' if state.works else 'Not here',
                  'sentence': state.works_sentence},
        'evidence': {'level': mechanism.evidence, 'label': EVIDENCE[mechanism.evidence]},
        'here': {'used': count, 'sentence': used},
        'part': mechanism.part, 'page': state.page[0] if state.page else mechanism.page,
        'page_label': state.page[1] if state.page else _label(mechanism.page, mechanism.page_label, here.values),
        # Where selecting it leads: its part on Savings & optimization → Savings results, or the page with its own figures or advice.
        'link': mechanism.destination,
        'link_label': 'Savings & optimization → Savings results' if mechanism.part else (
            _label(mechanism.link, mechanism.link_label, here.values) if mechanism.link else None),
        'command': 'faxbot savings results' if mechanism.part else mechanism.command,
        # "Turn on" leads to the setting's page only when the mechanism is off and works here.
        'turn_on': not state.on and state.works,
    }


def evaluate(values, routes, engine, *, now=None, days=WINDOW_DAYS):
    """Every catalogue entry for this installation, grouped by stage; never any money."""
    from ..direct.reuse import savings_view
    from .savings import savings
    now = now or utcnow()
    saved = savings(routes, engine, now=now, days=days, home=getattr(values, 'fax_default_country', None))
    saved['direct_bytes'] = savings_view(engine, since=now - timedelta(days=days), days=days)
    here = Installation(values, routes, engine, saved=saved, days=days)
    return {
        'days': days, 'title': TITLE, 'sentence': SENTENCE,
        'legend': [{'label': label, 'sentence': sentence} for label, sentence in LEGEND],
        # ``path``: a stage on the fax's own path (drawn with arrows), or advice beside it.
        'stages': [{'key': key, 'title': title, 'path': key in PATH, 'sentence': STAGE_SENTENCES.get(key),
                    'mechanisms': [_view(mechanism, here) for mechanism in CATALOGUE if mechanism.stage == key]}
                   for key, title in STAGES],
    }
