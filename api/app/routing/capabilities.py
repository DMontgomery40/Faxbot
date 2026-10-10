"""Savings & optimization → Capabilities: every mechanism in the catalogue, grouped by the outcome it serves.

The mechanism catalogue (``mechanisms.CATALOGUE``) stays the one registry of what Faxbot can do. This module adds,
for each catalogue key, the outcome it serves (one of ``OUTCOMES``, from #46 section 8), a concrete example, whether
it is still experimental, the ``faxbot`` command that changes its setting, and what it needs (its prerequisites).
Each prerequisite has a kind (``KINDS``), one sentence saying who or what satisfies it, the console address where
that happens, and a check of whether this installation has it.

``evaluate`` joins those with the catalogue's own evaluation, so the Capabilities page, the Overview's map and
``faxbot savings mechanisms`` never disagree about whether something is on, works here, is proven or was used. Like the
map, it reads only this installation's settings and stored records, never the network, and it never shows money:
amounts stay on Savings & optimization → Savings.

A new catalogue entry needs one line in ``CAPABILITIES``. ``api/tests/test_capabilities.py`` fails, naming this
module, when a key has none, names an unknown outcome or prerequisite kind, or points at a console address or
command that does not exist.

Console addresses here are the six-area addresses of spec #48 (``delivery/trunk``, ``savings/results``, …). The
catalogue still names the older ones, so ``MOVED`` translates each page and link it emits.
"""
from dataclasses import dataclass
import logging
from typing import Callable

import sqlalchemy as sa

from . import mechanisms
from .database import read_connection, reflect
from .store import WINDOW_DAYS

log = logging.getLogger(__name__)

TITLE = 'Capabilities'
SENTENCE = 'Everything Faxbot can do to make your faxes cost less and take less time, grouped by what it helps with.'

# The outcome groups, in the order the page shows them (#46 section 8).
OUTCOMES = (
    ('spend_less', 'Spend less on sending',
     'Choose the route, plan or number that costs least for each fax.'),
    ('shorter_calls', 'Smaller documents and shorter calls',
     'Send fewer pages, and keep each page on the line for less time.'),
    ('no_repeats', 'Avoid repeated calls and documents',
     'Send what a recipient already has only once, and share calls where recipients agree.'),
    ('relationships', 'Work better with regular recipients',
     'Agree better ways to deliver with the organizations you fax most.'),
    ('recover', 'Recover delivery with less waste',
     'Wait for the right moment, and resend only what is missing.'),
    ('receiving', 'Lower receiving and service costs',
     'Keep only the numbers, lines and plans you need, and turn away junk calls.'),
    ('explain', 'Explain results and choose improvements',
     'Check what you were billed, and find the next change worth making.'),
)

# What a prerequisite is, in a word the page shows beside it.
KINDS = {
    'connection': 'Connection',
    'prices': 'Prices',
    'engine': 'Fax engine',
    'agreement': "Recipient's agreement",
    'partner': 'Partner',
    'setting': 'Setting',
    'permission': 'Permission',
    'history': 'Fax history',
}

# The page's filters, in order. A capability matches each one its ``filters`` list names.
FILTERS = (
    ('on', 'On', 'Switched on for your faxes.'),
    ('off', 'Off', 'Switched off for now.'),
    ('ready', 'Ready to turn on', 'Off, and works on this installation.'),
    ('needs', 'Needs something', "Missing something it needs, such as a connection, prices or a recipient's agreement."),
    ('experimental', 'Experimental', 'Still being proven; its evidence and limits are shown with it.'),
)

# How a capability that is ready to turn on is marked among the next improvements (Overview).
IMPROVEMENTS = {
    'now': 'You can do this now',
    'fact': 'Needs a fact',
    'agreement': "Needs a recipient's agreement",
    'experimental': 'Experimental',
}

# The six-area console pages this module links to, with their names as the console's menu shows them (area → page;
# api/tests/test_capabilities.py checks them against navigation.tsx). The trunk page is named after its carrier
# (``_labels``), and the recipients list is just "Recipients".
PAGES = {
    'savings/capabilities': 'Savings & optimization → Capabilities',
    'savings/opportunities': 'Savings & optimization → Opportunities',
    'savings/results': 'Savings & optimization → Savings results',
    'savings/charges': 'Savings & optimization → Charges',
    'savings/prices': 'Savings & optimization → Prices & plans',
    'faxes/sent': 'Faxes → Sent',
    'delivery/numbers': 'Delivery setup → Numbers',
    'delivery/blocked': 'Delivery setup → Blocked senders',
    'delivery/identity': 'Delivery setup → Sending identity',
    'delivery/connections': 'Delivery setup → Providers & accounts',
    'delivery/trunk': 'Delivery setup → Carrier trunk',
    'recipients/list': 'Recipients',
    'recipients/partners': 'Recipients → Partners',
    'admin/setup': 'Administration → Setup',
    'admin/health': 'Administration → System health',
    'delivery/moves': 'Delivery setup → Number moves',
}

# The catalogue's older console pages and their six-area homes (spec #48's navigation table). Whoever gives the
# catalogue a page it has not used before adds it here.
MOVED = {
    'providers/sending': 'delivery/connections',
    'providers/trunk': 'delivery/trunk',
    'numbers/list': 'delivery/numbers',
    'numbers/identity': 'delivery/identity',
    'numbers/blocked': 'delivery/blocked',
    'costs/prices': 'savings/prices',
    'costs/charges': 'savings/charges',
    'costs/recommendations': 'savings/opportunities',
    'costs/savings': 'savings/results',
    'system/setup': 'admin/setup',
}


# How a prerequisite stands here. Only "missing" counts as missing; the other two are said as they are, so a
# prerequisite Faxbot has not seen working, or that nothing needs yet, never reads as in place.
STATES = {
    'in_place': 'In place',
    'missing': 'Missing',
    'not_needed': 'Not needed yet',
    'not_checked': 'Not checked yet',
}


@dataclass(frozen=True)
class Prerequisite:
    kind: str
    # Who or what satisfies it, said to the administrator.
    sentence: str
    # The console address where it is satisfied.
    address: str
    # True (in place) or False (missing), or one of STATES' keys.
    met: Callable


@dataclass(frozen=True)
class Capability:
    outcome: str
    # A concrete use of it, in one or two sentences; never an amount of money.
    example: str
    # The faxbot command that changes what its check reads, exactly as a person runs it; {number}-style words are
    # the values they fill in, as in the command reference. None when no command changes it (it is automatic).
    command: str | None
    prerequisites: tuple = ()
    # Still being proven: the README roadmap lists it under "Later and experimental".
    experimental: bool = False
    # Advice and charge checks: nothing to switch; their home is the page with what they found, and the catalogue's
    # command prints it.
    findings: bool = False


def address(old):
    """A console address in the six-area navigation: the catalogue's older page translated, its query kept."""
    page, mark, query = old.partition('?')
    return MOVED.get(page, page) + mark + query


# -- what each capability needs, and whether this installation has it -------------------------------------------

def _sends(here):
    return bool(here.sending)


def _trunk_sends(here):
    return here.trunk_sends


def _trunk(here):
    return here.trunk_sends or here.trunk_receives


def _receiving_number(here):
    from .own_numbers import receiving_numbers
    return bool(receiving_numbers(here.values))


def _receives(here):
    return bool(here.receiving_names)


def _second_route(here):
    return len(here.sending) >= 2


def _priced_routes(here):
    priced = [account for account in here.sending if (card := here.card(account)) is not None and not card.flat_plan]
    return len(priced) >= 2


def _plan_on_sending(here):
    return any((card := here.card(account)) is not None and card.monthly_fee_micros for account in here.sending)


def _plan_anywhere(here):
    from ..accounts import all_accounts
    return any(account.enabled and (account.sends or account.receives) and (card := here.card(account)) is not None
               and card.monthly_fee_micros for account in all_accounts(here.values))


def _billed_by_time(here):
    from ..pages.friendly import billed_by_time
    return any(billed_by_time(here.card(account), account.provider) for account in here.sending)


def _calls_billed_whole(here):
    from ..batching.policy import card_saves
    return card_saves(here.routes.card_for('sip'))


def _engine(here):
    from ..hylafax_engine import engine_conf_path
    return engine_conf_path(here.values).is_file()


def _network_passes_t38(here):
    """Missing when Faxbot switched to audio fax because the network or carrier cannot carry T.38; in place once a
    T.38 call went through; otherwise not checked yet."""
    from .. import sip_fax_mode
    if not getattr(here.values, 'sip_t38_enabled', True):
        record = sip_fax_mode.read(here.values)
        reason = record.get('reason') if record and record.get('mode') == 'audio' else None
        if reason in (sip_fax_mode.NETWORK, sip_fax_mode.CARRIER):
            return False
    carried = here.count('sip_call_records', lambda c: c.t38 == 'yes', lambda c: c.fax_status == 'SUCCESS')
    return True if carried else 'not_checked'


def _partner(here):
    return bool(here.partners)


def _partner_takes_images(here):
    return any(here.partners)


def _partner_takes_tunnel_calls(here):
    peers = reflect(here.engine, ('direct_peers',))['direct_peers']
    with read_connection(here.engine) as connection:
        return bool(connection.scalar(sa.select(sa.func.count()).select_from(peers).where(
            peers.c.state == 'verified', peers.c.partner_peer_calls == 1, peers.c.peer_call_address.is_not(None))))


def _direct_on(here):
    return here.direct_on


def _accepts_references(here):
    return bool(here.count('delivery_destinations', lambda c: c.accepts_references == 1))


def _agreed_encoded(here):
    return bool(here.count('codec_numbers', lambda c: c.enabled == 1))


def _agreed_together(here):
    return bool(here.count('batching_numbers', lambda c: c.enabled == 1))


def _agreed_marks(here):
    return bool(here.count('batching_numbers', lambda c: c.enabled == 1,
                       lambda c: c.boundaries.in_(('index_page', 'page_headers'))))


def _header_ready(here):
    """Missing when a recipient chose page marks and the header text does not name the sender yet; not needed while
    no recipient chose page marks."""
    from ..batching.policy import header_identifies_sender
    if header_identifies_sender(here.values):
        return True
    marks = here.count('batching_numbers', lambda c: c.enabled == 1, lambda c: c.boundaries == 'page_headers')
    return False if marks else 'not_needed'


def _toll_free_approved(here):
    from .tollfree import TollFreeApprovals
    return any(row['action'] == 'approved' for row in TollFreeApprovals(here.engine).all_current().values())


def _relay_agreed(here):
    return bool(here.count('relay_agreements', lambda c: c.role == 'sender', lambda c: c.state == 'active'))


def _digital_account(here):
    from ..digital.accounts import digital_accounts
    return any(account.enabled for account in digital_accounts(here.values))


def _digital_confirmed(here):
    from ..digital.store import DigitalStore
    return bool(DigitalStore(here.engine).confirmed_ids())


def _blocked_listed(here):
    from ..inbound.screening import ScreeningStore
    return bool(ScreeningStore(here.engine).active())


def _two_trunks(here):
    from ..accounts import all_accounts
    return len([account for account in all_accounts(here.values) if account.provider == 'sip' and account.enabled]) >= 2


def _in_the_us(here):
    return getattr(here.values, 'fax_default_country', 'US') == 'US'


def _telnyx_trunk(here):
    return getattr(here.values, 'sip_trunk_preset', '') == 'telnyx'


def _telnyx_key(here):
    return bool(getattr(here.values, 'telnyx_api_key', ''))


def _sending_or_receiving(here):
    return bool(here.sending or here.receiving_names)


# Wave 3: each reads what the catalogue entry's own check reads (routing/mechanisms.py).

def _caller_id_prices(here):
    return bool(here.count('origin_class_rates', lambda c: c.superseded_at.is_(None)))


def _ups(here):
    from ..power import settings
    return bool(settings(here.engine))


def _header_notice_set(here):
    from ..header_notice import notices_on
    with read_connection(here.engine) as connection:
        found = notices_on(connection)
    return bool(found.get('organization') or found.get('mailboxes'))


def _trunk_billed_by_minute(here):
    from .stations import bills_by_minute
    return any(bills_by_minute(here.card(account)) for account in here.sending if account.provider == 'sip')


def _keys_recorded(here):
    keys = reflect(here.engine, ('recipient_after_answer',))['recipient_after_answer']
    newest = {}
    with read_connection(here.engine) as connection:
        for row in connection.execute(sa.select(keys.c.phone_number, keys.c.digits)
                                      .order_by(keys.c.created_at, keys.c.id)):
            newest[row.phone_number] = row.digits
    return any(newest.values())


def _line_inventory(here):
    from .inventory import inventory_rows
    return bool(inventory_rows(here.engine))


def _french_lines(here):
    from .closures import view
    found = view(here.engine, here.values)
    return bool(found['sites'] or found['lines'])


def _country_with_rules(here):
    from .country_rules import view
    return bool(view(here.engine, here.values)['accounts'])


def _renewal_facts(here):
    from .channel_peak import imports
    from .renewal import renewals_by_system
    return bool(renewals_by_system(here.engine) or imports(here.engine))


def _pots_quote(here):
    from .pots_quote import quotes
    return bool(quotes(here.engine))


SENDS = Prerequisite('connection', 'A fax service account or your own SIP trunk that sends faxes.',
                     'delivery/connections', _sends)
TRUNK_SENDS = Prerequisite('connection', 'Your own SIP trunk for sending, from a phone carrier or your phone system.',
                           'delivery/trunk', _trunk_sends)
TRUNK = Prerequisite('connection', 'Your own SIP trunk, from a phone carrier or your phone system.', 'delivery/trunk',
                     _trunk)
TRUNK_RECEIVES = Prerequisite('connection', 'Faxes received over your own SIP trunk.', 'delivery/trunk',
                              lambda here: here.trunk_receives)
RECEIVING_NUMBER = Prerequisite('connection', 'A number this Faxbot receives faxes on.', 'delivery/numbers',
                                _receiving_number)
RECEIVES = Prerequisite('connection', 'A fax service account or SIP trunk that receives faxes for this Faxbot.',
                        'delivery/connections', _receives)
SECOND_ROUTE = Prerequisite('connection', 'A second sending route, such as another fax service account or your own '
                                          'SIP trunk.', 'delivery/connections', _second_route)
PRICED_ROUTES = Prerequisite('prices', 'Prices for at least two sending routes that charge for each fax.',
                             'savings/prices', _priced_routes)
CALLS_BILLED_WHOLE = Prerequisite('prices', 'Trunk prices that charge for each call or bill whole minutes, so one '
                                            'call for several faxes costs less.', 'savings/prices',
                                  _calls_billed_whole)
PARTNER = Prerequisite('partner', 'A verified partner: another organization that runs Faxbot, added from its partner '
                                  'card and confirmed with a check fax.', 'recipients/partners', _partner)
PARTNER_IMAGES = Prerequisite('partner', 'A verified partner that takes fax images; the partner says so from its own '
                                         'Faxbot.', 'recipients/partners', _partner_takes_images)
DIRECT_ON = Prerequisite('setting', 'Direct delivery switched on.', 'recipients/partners', _direct_on)


def advice(outcome, example, *prerequisites):
    """Advice: it lives in its section of Opportunities and changes nothing until you act on it."""
    return Capability(outcome, example, None, prerequisites, findings=True)


# -- the capabilities, one per catalogue key ------------------------------------------------------------------------
# Outcomes follow #46 section 8's table. Experimental: the README roadmap's "Later and experimental" entries.

CAPABILITIES = {
    'case_packets': Capability(
        'no_repeats',
        'A clinic sends a referral packet to the same specialist every month. The specialist already confirmed '
        'the insurance card and consent form, so the packet lists them instead of faxing those pages again.',
        'faxbot recipients set {number} --accepts-references',
        (SENDS, Prerequisite('agreement', 'The recipient accepts a list of the documents they already have; you '
                                          'record that on their page.', 'recipients/list', _accepts_references))),
    'dense_pages': Capability(
        'shorter_calls',
        'A two-page lab result with half-empty pages goes as one long fax page to a machine that takes long pages, '
        'so the call carries one page instead of two.',
        'faxbot delivery providers long-pages {route} --long-pages on', (SENDS,)),
    'encoded_pages': Capability(
        'shorter_calls',
        "A long report goes as a few dense fax pages to a recipient that runs Faxbot and agreed, and the "
        "recipient's Faxbot turns them back into the exact original file.",
        'faxbot recipients encoded set {number} --recipient-agreed',
        (SENDS, Prerequisite('agreement', 'A recipient that runs Faxbot and agrees to encoded pages; you record that '
                                          'on their page.', 'recipients/list', _agreed_encoded)),
        experimental=True),
    'fax_friendly': Capability(
        'shorter_calls',
        'A scanned form with grey shading goes over a line billed by the minute. The shading is lightened and specks '
        'removed first, so each page takes fewer seconds to send.',
        'faxbot admin settings set fax_friendly_documents=where_it_saves',
        (SENDS, Prerequisite('prices', 'A sending route billed by the minute or second, such as your own SIP trunk.',
                             'savings/prices', _billed_by_time))),
    'own_numbers': Capability(
        'spend_less',
        'Billing faxes a statement to one of your own fax numbers. It lands in Received at once, with no phone call.',
        'faxbot admin settings set local_delivery_enabled=true', (RECEIVING_NUMBER,)),
    'direct_delivery': Capability(
        'no_repeats',
        'A hospital that also runs Faxbot is your verified partner. A discharge summary to its fax number goes '
        'over the internet as the original file, with no fax call.',
        'faxbot admin settings set direct_delivery_enabled=true', (PARTNER,)),
    'fax_images': Capability(
        'no_repeats',
        "A partner's intake files every fax as an image. Faxbot sends it the exact fax pages over the internet, so "
        'its records look like a received fax, with no call.',
        'faxbot admin settings set direct_delivery_enabled=true', (PARTNER_IMAGES, DIRECT_ON)),
    'reuse': Capability(
        'no_repeats',
        "The same consent form goes to three of a partner's numbers. Faxbot sends it once, and next month sends "
        'only a reference to the copy the partner already holds.',
        'faxbot admin settings set direct_delivery_enabled=true', (PARTNER, DIRECT_ON), experimental=True),
    'partner_tunnel': Capability(
        'relationships',
        "Your Faxbot and a partner's Faxbot share a private tunnel. A fax to the partner is a fax call inside that "
        'tunnel, with no carrier and nothing charged per minute.',
        'faxbot recipients partners tunnel-calls {partner} on --address {address}',
        (PARTNER, Prerequisite('partner', 'A verified partner that takes fax calls over a private tunnel you both set '
                                          'up.', 'recipients/partners', _partner_takes_tunnel_calls), DIRECT_ON),
        experimental=True),
    'relay': Capability(
        'relationships',
        'A partner in another country relays your faxes to numbers there as local calls, instead of you placing '
        'international calls.',
        'faxbot recipients partners relay accept {partner}',
        (PARTNER, Prerequisite('agreement', 'A relay agreement: the partner offers to relay your faxes from its '
                                            'Faxbot, and you accept it.', 'recipients/partners', _relay_agreed))),
    'toll_free': Capability(
        'spend_less',
        'A pharmacy approves its toll-free fax number in writing. Your faxes to it dial that number, and the '
        'pharmacy pays for those calls.',
        'faxbot recipients toll-free approve {number} {toll_free} --by {who} --on {date} --evidence {evidence}',
        (SENDS, Prerequisite('agreement', "The recipient's written approval of its toll-free fax number; you record it "
                                          'on their page.', 'recipients/list', _toll_free_approved))),
    'cheapest_route': Capability(
        'spend_less',
        'Faxes to a busy clinic went through two routes last month. Faxbot now sends that number by the route that '
        'cost less per delivered fax, counting the tries that failed.',
        'faxbot recipients set {number} --preferred-route automatic', (SECOND_ROUTE, PRICED_ROUTES)),
    'plan_first': Capability(
        'spend_less',
        'Your fax service plan includes a set number of pages each month. Faxbot sends through it while it has room '
        'under its budget, and then uses routes that charge per fax.',
        'faxbot savings plans budget {plan} --pages {count}',
        (Prerequisite('prices', 'A monthly plan among your sending routes, with its fee and allowance entered.',
                      'savings/prices', _plan_on_sending),)),
    'busy_hours': Capability(
        'recover',
        "A clinic's line is busy every weekday from 9 to 10. A routine fax to it waits until 10 instead of placing "
        'calls that fail and may be charged.',
        'faxbot recipients schedule {number} --learn', (SENDS,)),
    'free_line': Capability(
        'recover',
        'Three faxes for the same small office are ready at once. Faxbot calls one at a time, so the others wait for '
        'a free line instead of reaching a busy signal.',
        'faxbot recipients set {number} --calls-at-once 1', (SENDS,)),
    'digital_routes': Capability(
        'relationships',
        "A hospital lists a Direct address in the national provider directory. Once you confirm it, referrals to "
        'that hospital go as Direct messages instead of fax calls.',
        'faxbot recipients digital add {number} --direct {address} --confirm',
        (Prerequisite('connection', 'A Direct messaging provider account or a FHIR account.', 'delivery/connections',
                      _digital_account),
         Prerequisite('agreement', "A recipient's Direct address or FHIR server that you confirmed on their page.",
                      'recipients/list', _digital_confirmed))),
    'fax_over_ip': Capability(
        'shorter_calls',
        'A ten-page fax over your SIP trunk goes as fax data (T.38) instead of audio, so the call takes less time.',
        'faxbot delivery providers trunk mode t38',
        (TRUNK, Prerequisite('connection', 'A network and carrier that carry T.38. Faxbot checks this itself and '
                                           'switches T.38 back on when they do.', 'delivery/trunk',
                             _network_passes_t38))),
    'measured_coding': Capability(
        'shorter_calls',
        'Before a call, Faxbot measures a scanned page in each coding the receiving machine accepts, and sends the '
        'smallest, so the page takes less time on the line.',
        None, (TRUNK_SENDS,)),
    'sending_together': Capability(
        'no_repeats',
        'Five short faxes for the same lab within a few minutes go in one call instead of five, on a trunk that '
        'charges for each call.',
        'faxbot recipients together set {number} --recipient-agreed',
        (TRUNK_SENDS, CALLS_BILLED_WHOLE,
         Prerequisite('agreement', 'A recipient that agrees to receive several faxes in one call; you record that on '
                                   'their page.', 'recipients/list', _agreed_together))),
    'separator_pages': Capability(
        'no_repeats',
        'Three documents sent together to a lab start with one index page that lists them, instead of a separator '
        'page before each one.',
        'faxbot recipients set {number} --index-page',
        (TRUNK_SENDS, CALLS_BILLED_WHOLE,
         Prerequisite('agreement', 'A recipient that shares calls and agrees to an index page or page marks; you '
                                   'record that on their page.', 'recipients/list', _agreed_marks),
         Prerequisite('setting', 'For marks at the top of every page: your header text and sending number.',
                      'delivery/identity', _header_ready))),
    'sslfax': Capability(
        'shorter_calls',
        'When the receiving fax server offers SSL Fax, the pages go over an encrypted internet connection during '
        'the call instead of as audio, so the call ends sooner.',
        'faxbot admin settings set sip_sslfax_enabled=true',
        (TRUNK_SENDS, Prerequisite('engine', "Faxbot's fax engine, which starts when you select Apply and connect on "
                                             'the trunk page.', 'delivery/trunk', _engine))),
    'continuation': Capability(
        'recover',
        'A 30-page fax broke after page 22 was confirmed. You send only pages 23 to 30 instead of all 30 again.',
        'faxbot faxes sent continue {fax_id} --send', (SENDS,)),
    'partner_repair': Capability(
        'recover',
        'A call to a partner broke after 12 of 20 pages. Faxbot asks the partner which pages it holds, and sends the '
        'other 8 over the internet with no new call.',
        'faxbot admin settings set direct_delivery_enabled=true', (PARTNER_IMAGES, DIRECT_ON), experimental=True),
    'charge_checks': Capability(
        'explain',
        "Your carrier's invoice lists three more calls than Faxbot recorded. Charges lists those three calls, so you "
        'can ask the carrier about them.',
        None,
        (Prerequisite('connection', 'A fax service account or your own SIP trunk.', 'delivery/connections',
                      _sending_or_receiving),), findings=True),
    'blocked_senders': Capability(
        'receiving',
        'A number keeps sending junk faxes at night. Once you block it, its calls are declined before Faxbot '
        'answers, so nothing is received or stored.',
        'faxbot delivery blocked add {number} --reason {reason}',
        (TRUNK_RECEIVES, Prerequisite('setting', 'A number on your blocked list.', 'delivery/blocked',
                                      _blocked_listed))),
    'advice_sending': advice(
        'spend_less',
        'Faxes to one lab cost more per delivered fax through your first route than through your second last month. '
        "The advice names the lab's number, with a button to switch it.",
        SECOND_ROUTE, PRICED_ROUTES),
    'advice_plans': advice(
        'receiving',
        "Your unlimited fax plan's fee is compared with what last month's faxes would have cost without it, so you "
        'can decide whether to keep it.',
        Prerequisite('prices', 'A monthly plan on one of your fax services, with its fee entered.', 'savings/prices',
                     _plan_anywhere)),
    'advice_receiving': advice(
        'receiving',
        'Two numbers on your trunk rarely ring at the same time, so they could share lines. A number that received '
        'two faxes in three months is named as one to let go.',
        RECEIVES),
    'advice_carriers': advice(
        'spend_less',
        "Last month's faxes are priced at other carriers' published prices, so you can see what switching would "
        'change before you do it.',
        SENDS),
    'advice_billing_steps': advice(
        'spend_less',
        'Calls to one number usually last just over a minute and are billed as two. One page less or a faster mode '
        'would keep them under a minute.',
        TRUNK),
    'advice_partners': advice(
        'relationships',
        'One recipient gets faxes from you every day and costs the most. The advice suggests enrolling it as a '
        'direct partner.',
        SENDS),
    'advice_discovery': advice(
        'relationships',
        'A fax call to a clinic was answered by a Faxbot. The clinic is listed so you can enroll it and send to it '
        'with no call.',
        TRUNK_SENDS),
    'advice_relays': advice(
        'relationships',
        "A partner's signed price for local calls in its country is lower than what your own calls there cost. The "
        'advice names that partner.',
        PARTNER),
    'advice_toll_free': advice(
        'spend_less',
        "A pharmacy's toll-free fax number is in the national provider directory. The advice lists it, so you can "
        'record the pharmacy\'s approval.',
        SENDS),
    'advice_fax_marker': advice(
        'spend_less',
        'Calls marked as fax reached their recipient more often than calls that were not. The advice shows both, so '
        'you can choose the setting.',
        TRUNK_SENDS),
    'advice_shading': advice(
        'shorter_calls',
        "With Lighter shading set to Never, last month's faxes are measured as if it were on, and the advice says "
        'how much call time it would have saved.',
        SENDS),
    'advice_trunks': advice(
        'receiving',
        "Your second trunk carried few faxes last month. The advice says its faxes would fit on your first trunk's "
        'free lines.',
        Prerequisite('connection', 'Two or more SIP trunks to compare.', 'delivery/connections', _two_trunks)),
    'advice_numbers': advice(
        'receiving',
        'A number on a fax service costs more each month to receive on than it would on your trunk. The advice '
        'lists the steps to move it.',
        RECEIVES),
    'advice_sites': advice(
        'spend_less',
        "Your carrier prices calls by state. Calls from one site to another state cost less through the other "
        "site's trunk, and the advice says so.",
        SENDS, Prerequisite('setting', 'Your installation country set to the United States.', 'delivery/connections',
                            _in_the_us)),
    'advice_caller_names': advice(
        'receiving',
        'Telnyx charges for caller-name lookup on four of your numbers, which Faxbot never uses, so you can turn it '
        'off on each.',
        Prerequisite('connection', 'A Telnyx trunk.', 'delivery/trunk', _telnyx_trunk),
        Prerequisite('setting', 'Your Telnyx key, saved on the trunk page.', 'delivery/trunk', _telnyx_key)),
    'advice_setup_packs': advice(
        'explain',
        'For a clinic with its own trunk, a pack gathers fax over IP, sending together and the page settings that '
        'suit it, for you to review and apply at once.'),

    # Wave 3 (README roadmap: all implemented, none under "Later and experimental").
    'measured_account': Capability(
        'spend_less',
        'One of your accounts bills by the page and another by the minute. For a 12-page scan, Faxbot measures how '
        'long its pages would take on each and sends by the account whose bill for that fax is lower.',
        None, (SECOND_ROUTE,)),
    'caller_id_prices': Capability(
        'spend_less',
        'Your carrier charges much less for a call to Austria when the caller ID is a European number. Faxbot prices '
        'each account with the caller ID its calls would show, so the route it picks is the one that really costs '
        'least.',
        'faxbot savings rate-rows {route} --caller-id-deck {file}',
        (SENDS, Prerequisite('prices', "Your carrier's prices by caller ID, imported for its sending card.",
                             'savings/prices', _caller_id_prices))),
    'dialing_guard': Capability(
        'spend_less',
        'A typing slip turns a fax to a local clinic into a call to a premium-rate number. Faxbot holds that fax '
        'for your approval instead of dialling it.',
        'faxbot delivery providers destinations allow {class}', (SENDS,)),
    'power_aware': Capability(
        'recover',
        'During a power cut your UPS has four minutes left. Faxbot holds a long fax that needs six, instead of '
        'starting a call that would be cut off part way and still billed.',
        'faxbot admin diagnostics power set --address {address}',
        (Prerequisite('connection', 'A UPS that Faxbot reads over the network through NUT (Network UPS Tools).',
                      'admin/health', _ups),)),
    'header_notice': Capability(
        'shorter_calls',
        "A clinic's faxes always start with a cover page that only carries a confidentiality notice. With the notice "
        'printed in a band at the top of each page, the cover page is left out and every fax is one page shorter.',
        'faxbot delivery identity notice set {notice}',
        (SENDS, Prerequisite('setting', 'Your notice, for the whole organization or for a mailbox.',
                             'delivery/identity', _header_notice_set))),
    'answer_cap': Capability(
        'recover',
        'A number rings with no fax machine answering. On a trunk billed by the whole minute, Faxbot hangs up at '
        '50 seconds, so the failed call is billed one minute instead of two.',
        'faxbot delivery providers trunk answer-cap on',
        (TRUNK_SENDS, Prerequisite('prices', 'Trunk prices billed by the minute in steps of 60 seconds or more.',
                                   'savings/prices', _trunk_billed_by_minute))),
    'station_check': Capability(
        'recover',
        "A referral number was reassigned, and the fax machine that answers shows someone else's number. Faxbot "
        'says so in Sent details, or hangs up before any page where you chose that.',
        'faxbot delivery identity station-check refuse --mailbox {mailbox}', (TRUNK_SENDS,)),
    'keys_after_answer': Capability(
        'recover',
        "A hospital's fax number answers with a menu: press 2 for the fax machine. Faxbot presses 2 once it answers, "
        'so the fax goes through instead of failing and being retried.',
        'faxbot recipients set {number} --after-answer {keys}',
        (TRUNK_SENDS, Prerequisite('setting', "The keys a recipient's phone menu asks for, on their page.",
                                   'recipients/list', _keys_recorded))),
    'route_problems': Capability(
        'recover',
        'Calls through one carrier start failing to many numbers at once. Faxbot holds that route back and keeps '
        "those failures off each number's record, so working numbers are not retried or given up on.",
        None, (SENDS,)),
    'advice_line_inventory': advice(
        'receiving',
        'Your carrier lists two of your fax lines for discontinuance next spring. The advice names them and their '
        'dates, so you can move them before the price changes or the lines close.',
        Prerequisite('setting', 'Your line inventory, imported from your carrier or your own records.',
                     'delivery/moves', _line_inventory)),
    'advice_copper_closures': advice(
        'receiving',
        "The copper network under your Lyon office's commune closes in 2027. The advice shows the date for each "
        'line there, so you can move them in time.',
        Prerequisite('setting', "For lines in France: each site's commune, or a line's closing notice.",
                     'delivery/moves', _french_lines)),
    'advice_registered_senders': advice(
        'relationships',
        'A bank accepts faxed instructions only from the number registered with it. Faxbot sends to it from the '
        'account and caller ID you registered, so your faxes are not refused and sent again.',
        SENDS),
    'advice_country_rules': advice(
        'explain',
        "Your account with a provider in the United Arab Emirates is listed with that country's rules for fax "
        'services and their sources, and you record that the provider meets them.',
        Prerequisite('connection', 'A fax service account in a country whose service rules Faxbot knows.',
                     'delivery/connections', _country_with_rules)),
    'advice_renewal': advice(
        'receiving',
        "Your fax server's renewal quotes 24 channels, but its call records show at most 9 in use at once last "
        'year. The advice sets the two side by side, with the faxes Faxbot already handles, so you renew only the '
        'channels you need.',
        Prerequisite('prices', "Your fax server's renewal, or its call records to measure the channels it used at "
                               'peak.', 'savings/opportunities?section=renewal', _renewal_facts)),
    'advice_pots': advice(
        'receiving',
        'Your carrier quotes a POTS-replacement order that includes eight fax lines. The advice shows what taking '
        'those lines out removes from the quote, and what one shared trunk would cost for them instead.',
        Prerequisite('prices', "Your carrier's POTS-replacement quote.", 'savings/opportunities?section=pots',
                     _pots_quote)),
}


# A catalogue key with no entry above, listed with the catalogue's own facts only (see evaluate).
UNMAPPED = Capability('explain', '', None)


# -- the read -----------------------------------------------------------------------------------------------------

def _labels(values):
    """A function naming a console address as the console shows it; the trunk page carries its carrier's name."""
    from ..provider_labels import trunk_name
    trunk = f"Delivery setup → {trunk_name(getattr(values, 'sip_trunk_preset', ''))}"

    def label(where):
        page = where.partition('?')[0]
        # A page this module has no name for (a new catalogue page): its address, until PAGES names it.
        return trunk if page == 'delivery/trunk' else PAGES.get(page, page)
    return label


def _improvement(ready, experimental, prerequisites):
    """How a capability that is ready to turn on is marked among the next improvements; None when it is not."""
    if not ready:
        return None
    missing = {item['kind'] for item in prerequisites if not item['met']}
    if experimental:
        kind = 'experimental'
    elif missing & {'agreement', 'partner'}:
        kind = 'agreement'
    elif missing & {'prices', 'history'}:
        kind = 'fact'
    else:
        kind = 'now'
    return {'kind': kind, 'label': IMPROVEMENTS[kind]}


def _view(key, capability, item, here, label):
    prerequisites = []
    for prerequisite in capability.prerequisites:
        found = prerequisite.met(here)
        state = found if isinstance(found, str) else 'in_place' if found else 'missing'
        prerequisites.append({
            'kind': prerequisite.kind, 'kind_label': KINDS[prerequisite.kind], 'met': state != 'missing',
            'state': state, 'label': STATES[state], 'sentence': prerequisite.sentence,
            'address': prerequisite.address, 'address_label': label(prerequisite.address)})
    missing = sum(1 for prerequisite in prerequisites if not prerequisite['met'])
    ready = bool(item['turn_on'])
    on = item['enabled']['on']
    results = None
    if item['link']:
        results = {'address': address(item['link']), 'label': label(address(item['link'])), 'command': item['command']}
    if capability.findings:
        # Advice and charge checks: their home is the page with what they found; its command is under results.
        setting, command = results['address'], None
    else:
        setting = address(item['page'])
        # When the catalogue sends its setting to another page this time (separator pages → Sender identity), what
        # changes there is not this command.
        command = capability.command if setting == address(mechanisms.BY_KEY[key].page) else None
    filters = [name for name, matches in (('on', on), ('off', not on), ('ready', ready), ('needs', missing > 0),
                                          ('experimental', capability.experimental)) if matches]
    return {
        'key': key, 'name': item['name'], 'sentence': item['sentence'], 'example': capability.example,
        'outcome': capability.outcome,
        # This capability's own page.
        'address': f'savings/capabilities?key={key}',
        # The catalogue's evaluation, unchanged: the map and this page never disagree.
        'enabled': item['enabled'], 'works': item['works'], 'evidence': item['evidence'], 'here': item['here'],
        'experimental': capability.experimental,
        # Off, but works here: the page offers to turn it on, at its setting.
        'ready': ready,
        'missing': missing,
        'filters': filters,
        'improvement': _improvement(ready, capability.experimental, prerequisites),
        'prerequisites': prerequisites,
        'setting': {'address': setting, 'label': label(setting), 'command': command},
        # Its figures: its part on Savings, or the page with its own advice or findings.
        'results': results,
        # The faxes it acted on, where the console has a list of exactly those; none does yet.
        'affected': None,
    }


def evaluate(values, routes, engine, *, now=None, days=WINDOW_DAYS):
    """Every catalogue entry for this installation, grouped by outcome; never any money."""
    evaluated = mechanisms.evaluate(values, routes, engine, now=now, days=days)
    items = {item['key']: item for stage in evaluated['stages'] for item in stage['mechanisms']}
    # What this installation has, for the prerequisites; the facts above already came from the same reads.
    here = mechanisms.Installation(values, routes, engine, saved={}, days=days)
    label = _labels(values)
    grouped = {key: [] for key, _, _ in OUTCOMES}
    for mechanism in mechanisms.CATALOGUE:
        capability = CAPABILITIES.get(mechanism.key)
        if capability is None:
            # A catalogue entry added without its line here: still listed, with what the catalogue says, rather than
            # taking the whole read down. api/tests/test_capabilities.py fails until the line is added.
            log.warning('Capability %s has no entry in routing/capabilities.py; listing it without one', mechanism.key)
            capability = UNMAPPED
        grouped[capability.outcome].append(_view(mechanism.key, capability, items[mechanism.key], here, label))
    return {
        'days': days, 'title': TITLE, 'sentence': SENTENCE,
        'legend': evaluated['legend'],
        'filters': [{'key': key, 'label': name, 'sentence': sentence} for key, name, sentence in FILTERS],
        'outcomes': [{'key': key, 'title': title, 'sentence': sentence, 'capabilities': grouped[key]}
                     for key, title, sentence in OUTCOMES],
    }
