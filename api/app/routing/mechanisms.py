"""Every way Faxbot saves money on faxes, and how each one stands on this installation.

``CATALOGUE`` is the one list of Faxbot's money-saving mechanisms. Each entry says what the mechanism does for the
administrator's money, where it sits on a fax's path (``STAGES``), how far it is proven (``EVIDENCE``), its part
on Costs → Savings and the console page that holds its setting. ``evaluate`` reads this installation's effective
settings and stored records, never the network, and says for each entry:

- **On or off**, from the settings and each recipient's or partner's own choice;
- **Works here**, from what the installation has: its sending routes and their prices, its own SIP trunk and fast
  fax service, its partners and its receiving numbers. When something is missing, one sentence names it;
- **Tested**, as two separate facts: the product evidence level (fixed per entry, from the README roadmap) and
  what happened on this installation, from the same Savings part, so the map and Costs → Savings never disagree.

The Overview's map and ``faxbot costs mechanisms`` show the result of GET /routing/savings/mechanisms. Neither
shows money: what each mechanism saved stays on Costs → Savings, under the entry's ``part``.

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


# A fax's path, in the order a fax meets each stage.
STAGES = (('document', 'Preparing the document'), ('route', 'Choosing the route'), ('call', 'On the call'),
          ('after', 'After the call'), ('receiving', 'Receiving'))

# How far Faxbot has proven a mechanism; the lower level whenever the roadmap is in doubt.
EVIDENCE = {
    'live': 'Proven on a live call',
    'lab': 'Proven in the test lab',
    'built': 'Built and tested; not yet run on a live call',
}

TITLE = 'How Faxbot saves money'
SENTENCE = 'Every way Faxbot saves money, in the order a fax meets them.'
LEGEND = (
    ('On or Off', 'Whether it is switched on for your faxes.'),
    ('Works here or Not here', 'Whether this installation has what it needs, such as its own phone line or a '
                               'partner.'),
    ('Tested', 'How far Faxbot has proven it, and whether it has worked on your faxes in the last 30 days.'),
)

# Mechanisms with no part on Costs → Savings, each with the reason. Whoever gives one a part removes it here.
# The reason is the map's "on this installation" sentence for that mechanism.
NO_PART = {
    'busy_hours': 'Faxbot works these hours out afresh each time and keeps no count of the faxes that waited.',
}


@dataclass(frozen=True)
class State:
    """How one mechanism stands here: on, and works here, each with one sentence when there is more to say."""
    on: bool
    works: bool
    on_sentence: str | None = None
    works_sentence: str | None = None


@dataclass(frozen=True)
class Mechanism:
    key: str
    name: str
    # What it does for the administrator's money, in one sentence.
    sentence: str
    stage: str
    evidence: str
    # Its part on Costs → Savings: the key in GET /routing/savings and the anchor on the Savings page.
    part: str | None
    # The console page that holds its setting, and its name as the console shows it.
    page: str
    page_label: str
    check: Callable
    # The configuration values it reads (ConfigurationValues fields); per-recipient homes name only a page.
    settings: tuple = ()
    # What its Savings part counts: (key in the part, one, many).
    counts: tuple = ('faxes', 'fax', 'faxes')
    verb: str = 'Worked on'


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
            return count, f"{mechanism.verb} {count} {one if count == 1 else many} in the last {self.days} days."
        return 0, f'Not used here in the last {self.days} days.'


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


def cheapest_route(here):
    priced, plans, unpriced = [], [], []
    for account in here.sending:
        card = here.card(account)
        (unpriced if card is None else plans if card.flat_plan else priced).append(account.label)
    why = None
    if len(here.sending) < 2:
        why = f'Needs a second sending route to compare with; {here.through()}.'
    elif len(priced) < 2:
        reasons = []
        if plans:
            reasons.append(f"{' and '.join(plans)} {'is a monthly plan' if len(plans) == 1 else 'are monthly plans'}")
        if unpriced:
            reasons.append(f"Faxbot has no prices for {' and '.join(unpriced)}")
        why = f"Needs two sending routes that charge for each fax; {'; '.join(reasons)}."
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
    if marks_wait:
        # Faxbot falls back to separator pages by itself (batching.policy.HEADER_NEEDS), and says why.
        sentence = ((sentence + ' ') if working else '') + (
            'Marks at the top of every page need your header text and sending number set in Numbers > Sender '
            'identity, so faxes to those recipients use separator pages for now.')
    why = _shared_calls(here)
    return State(bool(working), why is None, sentence, why)


def sslfax(here):
    from ..hylafax_engine import NOT_SET_UP, engine_conf_path
    why = None
    if not here.trunk_sends:
        why = f'Needs your own SIP trunk; {here.through()}.'
    elif not engine_conf_path(here.values).is_file():
        why = NOT_SET_UP
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
              'document', 'built', 'packing', 'providers/sending', 'Providers → In use → Delivery routes',
              dense_pages),
    Mechanism('encoded_pages', 'Encoded pages (experimental)',
              "Sends a whole document as a few dense fax pages that the recipient's Faxbot turns back into the "
              'original, for recipients who agreed.',
              'document', 'built', 'encoding', 'recipients/list', 'Recipients → Details', encoded_pages),
    Mechanism('fax_friendly', 'Lighter shading',
              'Lightens shaded areas and removes specks before a page goes on a line billed by time, so pages '
              'take less time to send.',
              'document', 'built', 'fax_friendly', 'providers/sending', 'Providers → In use → Delivery routes',
              fax_friendly, settings=('fax_friendly_documents',)),
    Mechanism('own_numbers', 'Faxes to your own numbers',
              'Delivers a fax to one of your own numbers straight into Received, with no phone call.',
              'route', 'built', 'own_numbers', 'providers/sending', 'Providers → In use → Delivery routes',
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
              'route', 'built', 'plan_first', 'costs/prices', 'Costs → Prices & plans', plan_first,
              settings=('plan_budgets',)),
    Mechanism('busy_hours', 'Calls at the right hour',
              "Waits out a number's usual busy hours when a failed call there could be charged.",
              'route', 'built', None, 'recipients/list', 'Recipients → Details', busy_hours),
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
              'call', 'lab', 'sslfax', 'providers/trunk', 'Providers → Carrier trunk', sslfax,
              settings=('sip_sslfax_enabled',)),
    Mechanism('continuation', 'Only the missing pages',
              'Finishes a fax whose call broke by sending only the pages the call did not confirm.',
              'after', 'built', 'continuation', 'faxes/sent', 'Faxes → Sent', continuation),
    Mechanism('partner_repair', 'Missing pages to partners',
              'After a broken call to a partner, sends only the pages the partner is missing, with no new call.',
              'after', 'built', 'partner_repair', 'recipients/partners', 'Recipients → Partners', partner_repair,
              settings=('direct_delivery_enabled',)),
    Mechanism('blocked_senders', 'Junk callers turned away',
              'Declines calls from blocked numbers before Faxbot answers, so they are never answered or received.',
              'receiving', 'built', 'blocked_calls', 'numbers/blocked', 'Numbers → Blocked senders',
              blocked_senders, counts=('calls', 'call', 'calls'), verb='Turned away'),
)
BY_KEY = {mechanism.key: mechanism for mechanism in CATALOGUE}


def _page_label(mechanism, values):
    """The page's name as the console shows it; the trunk page is named after its carrier ("Providers → Telnyx")."""
    if mechanism.page == 'providers/trunk':
        from ..provider_labels import trunk_name
        return f"Providers → {trunk_name(getattr(values, 'sip_trunk_preset', ''))}"
    return mechanism.page_label


def _view(mechanism, here):
    state = mechanism.check(here)
    count, used = here.used(mechanism) if mechanism.part else (0, NO_PART[mechanism.key])
    return {
        'key': mechanism.key, 'name': mechanism.name, 'sentence': mechanism.sentence,
        'enabled': {'on': state.on, 'label': 'On' if state.on else 'Off', 'sentence': state.on_sentence},
        'works': {'here': state.works, 'label': 'Works here' if state.works else 'Not here',
                  'sentence': state.works_sentence},
        'evidence': {'level': mechanism.evidence, 'label': EVIDENCE[mechanism.evidence]},
        'here': {'used': count, 'sentence': used},
        'part': mechanism.part, 'page': mechanism.page, 'page_label': _page_label(mechanism, here.values),
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
        'stages': [{'key': key, 'title': title,
                    'mechanisms': [_view(mechanism, here) for mechanism in CATALOGUE if mechanism.stage == key]}
                   for key, title in STAGES],
    }
