"""What each Capabilities entry did on this installation, for the entries the catalogue has no count for.

Savings & optimization → Capabilities says, for every entry, what it did here ("What it did here"). Most entries get
that line from the catalogue (``mechanisms.py``): the count of their part on Savings results. Advice, charge checks
and the later mechanisms have no part there, so this module counts each from this installation's own records:

- **advice:** how many suggestions its section of Opportunities (or its own page) has now, from the same reader that
  section's HTTP read calls, counted the way the console counts them;
- **charge checks:** the faxes and calls your providers and carrier billed that Faxbot has no record of, over the
  window, as Savings & optimization → Charges lists them;
- **the other mechanisms:** the faxes or calls each acted on over the window.

``capabilities.evaluate`` calls ``line`` for each key in ``COUNTERS``; the catalogue's own line stands for every other
key. Every count is read from this installation's settings and stored tables, never the network, so no reader here
may call a fetcher (``charges.py``, ``provider_sweep``'s sweep, ``carrier_records.py``, ``nppes.py``, ``telnyx.py``
or any HTTP client); where a section's own read would fetch, its stored results are counted instead. Sentences carry
counts and plain nouns only, never money: amounts stay on Savings.

A new catalogue entry with no Savings part needs a counter here; ``api/tests/test_capabilities.py`` fails, naming
this module, until it has one (or the catalogue says Faxbot keeps no count of it).
"""
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from types import SimpleNamespace

import sqlalchemy as sa

from .database import read_connection, utcnow


def suggestions(count):
    """The advice form: '2 suggestions now', '1 suggestion now' or 'No suggestions now'."""
    if not count:
        return 'No suggestions now'
    return f"{count} {'suggestion' if count == 1 else 'suggestions'} now"


def _advice(count):
    return count, suggestions(count)


def _plural(count, one, many):
    return f'{count} {one if count == 1 else many}'


def _since(here, now):
    return now - timedelta(days=here.days)


def _table(name, *columns):
    """A migrated table named with only the columns a count reads: no reflection, which would cost more than the
    count on every Overview and Capabilities read."""
    return sa.table(name, *(sa.column(column) for column in columns))


def _where(table, *where, extra=()):
    """``table`` (named with the columns it needs) and its conditions: each (column, value) must match, ``None``
    meaning the column is empty, and (column, '>=', value) is a lower bound."""
    found = _table(table, *{condition[0] for condition in where}, *extra)
    tests = []
    for condition in where:
        column, value = found.c[condition[0]], condition[-1]
        tests.append(column >= value if len(condition) == 3 else column.is_(None) if value is None else column == value)
    return found, tests


def _rows(here, table, when, since, *where, distinct=None):
    """How many rows of ``table`` (or distinct values of its ``distinct`` column) have ``when`` in the window."""
    found, tests = _where(table, (when, '>=', since), *where, extra=(distinct,) if distinct else ())
    counted = sa.func.count(sa.distinct(found.c[distinct])) if distinct else sa.func.count()
    with read_connection(here.engine) as connection:
        return int(connection.scalar(sa.select(counted).select_from(found).where(*tests)) or 0)


def _any(here, table, *where):
    """Whether ``table`` has a matching row: a cheap look before a reader that reflects its tables."""
    found, tests = _where(table, *where)
    with read_connection(here.engine) as connection:
        row = connection.execute(sa.select(sa.literal(1)).select_from(found).where(*tests).limit(1)).first()
    return row is not None


def _acted(count, here, done, one='fax', many='faxes', none=None):
    """'Used on 3 faxes in 30 days', or the zero form ('Not used in 30 days' unless ``none`` says otherwise)."""
    if count:
        return count, f'{done} {_plural(count, one, many)} in {here.days} days'
    return 0, (none or 'Not used in {days} days').format(days=here.days)


def _once(here, name, read):
    """One read shared by the counters of one evaluation (the plans advice reuses the sending advice's read)."""
    kept = here.__dict__.setdefault('_capability_here', {})
    if name not in kept:
        kept[name] = read()
    return kept[name]


def _bound(here):
    """The outbound provider the route planner starts from: the HTTP reads take it from the active outbound profile,
    which Faxbot builds from this same setting."""
    return getattr(here.values, 'effective_outbound', '') or None


# -- advice: each section of Opportunities, and the advice kept on its own page -----------------------------------

def _sending_items(here):
    from .recommendations import sending_recommendations
    return _once(here, 'sending', lambda: sending_recommendations(
        here.routes, SimpleNamespace(values=here.values), _bound(here)))


def advice_sending(here, now):
    return _advice(len(_sending_items(here)))


def advice_plans(here, now):
    from .plan_check import plan_report
    suggested = {item['suggested']['route'] for item in _sending_items(here) if item['kind'] == 'plan'}
    # No account numbers from HumbleFax: reading them may ask HumbleFax (plan_check.known_account_numbers).
    report = plan_report(here.routes, here.values, now=now, bound=_bound(here), suggested=suggested, accounts={})
    return _advice(sum(1 for plan in report['plans'] if plan['state'] != 'too_little_history'))


# The receiving section's states that mean Faxbot is still waiting for call history (ReceivingRecommendations.tsx).
_WAITING = ('too_little_history', 'no_trunk', 'no_channel_price')


def advice_receiving(here, now):
    from .receiving import receiving_report
    report = receiving_report(here.engine, here.routes, here.values, now=now)
    # Like the section: one piece of advice once it advises, none while it waits for call history.
    waiting = (report['pool']['state'] in _WAITING
               and report['quiet_numbers']['state'] not in ('quiet', 'none_quiet')
               and (report.get('provider_numbers') or {}).get('state') != 'quiet')
    return _advice(0 if waiting else 1)


def _saves(row):
    """A carrier other than yours that would have cost less (OtherCarriers.tsx)."""
    if row['yours'] or not row['difference']:
        return False
    try:
        return Decimal(str(row['difference'][0]['amount'])) > 0
    except (InvalidOperation, KeyError, TypeError):
        return False


def advice_carriers(here, now):
    from .carrier_compare import compare
    return _advice(sum(1 for row in compare(here.engine, here.values, now=now)['carriers'] if _saves(row)))


def advice_billing_steps(here, now):
    from .boundaries import billing_boundaries
    return _advice(len(billing_boundaries(here.engine, here.routes, here.values, now=now)['numbers']))


def advice_partners(here, now):
    from .partners import partner_candidates
    return _advice(len(partner_candidates(here.routes, now=now)['items']))


def advice_discovery(here, now):
    from ..direct.discovery import DiscoveryStore
    if not _any(here, 'direct_discovery_suggestions', ('dismissed_at', None), ('enrolled_at', None)):
        return _advice(0)
    return _advice(len(DiscoveryStore(here.engine).open_suggestions()))


def advice_relays(here, now):
    from ..direct.relay import RelayService
    from ..direct.service import DirectService
    if not here.partners:
        # The section names only verified partners (RelayService._known_prices); with none it has nothing.
        return _advice(0)
    relay = RelayService(DirectService(here.engine, values=lambda: here.values))
    return _advice(len(relay.recommendations(now=now)))


def advice_toll_free(here, now):
    from .tollfree import toll_free_recommendations
    return _advice(len(toll_free_recommendations(here.routes)['items']))


def advice_fax_marker(here, now):
    from .preference import fax_marker_report
    # The section reads 90 days of calls, as its HTTP read does by default.
    return _advice(1 if fax_marker_report(here.engine, here.values, now=now, days=90)['state'] == 'compared' else 0)


class _NotMeasured(Exception):
    """A recent fax the shading advice has not measured yet; measuring it would draw the fax again."""


def _not_measured(pdf_path):
    raise _NotMeasured


def advice_shading(here, now):
    from ..pages.friendly import documents_choice, recommendation
    try:
        # Only the measurements Faxbot already made: drawing faxes again belongs to the section itself.
        found = recommendation(here.engine, here.values.fax_data_dir, choice=documents_choice(here.values), now=now,
                               measure=_not_measured)
    except _NotMeasured:
        return 0, 'Faxbot measures your recent faxes for this when you open Opportunities'
    return _advice(1 if found['recommend'] else 0)


def advice_trunks(here, now):
    from .trunk_advice import advice
    return _advice(len(advice(here.values, here.engine, now=now, routes=here.routes)['items']))


def advice_numbers(here, now):
    from .number_placement import placement
    found = placement(here.engine, here.values, routes=here.routes, now=now)
    return _advice(sum(1 for row in found['numbers'] if row['state'] == 'move') + len(found['accounts']))


def advice_sites(here, now):
    from .jurisdiction import site_advice
    return _advice(len(site_advice(here.engine, here.values, now=now)['items']))


def advice_caller_names(here, now):
    from ..telnyx_numbers import report
    # The numbers from Faxbot's last check of the trunk, kept on disk; the check itself asks Telnyx and runs elsewhere.
    return _advice(sum(1 for number in report(here.values)['numbers'] if number['lookup'] is True))


def advice_setup_packs(here, now):
    from ..setup_plan.store import PlanStore
    # The packs of the last preview, as Administration → Setup shows them; a preview is never made from here.
    latest = PlanStore(here.engine).latest()
    if latest is None:
        return 0, 'No suggestions prepared yet; select Preview suggestions in Administration → Setup'
    return _advice(sum(1 for pack in latest['plan']['packs'] if pack['items']))


def advice_line_inventory(here, now):
    from .inventory import view
    lines = view(here.engine, here.values)['lines']
    # Lines with a date or a match to look at (LineInventory.tsx lists only those).
    return _advice(sum(1 for line in lines if line['dates'] or (line['match'] and line['match']['state'] != 'listed')))


def advice_copper_closures(here, now):
    from .closures import view
    # Every line the page warns about: all but those whose closure is still far off.
    return _advice(sum(1 for line in view(here.engine, here.values)['lines'] if line['state'] != 'later'))


def advice_registered_senders(here, now):
    from .sender_pins import pins, presentable
    # Registered senders whose faxes wait in Sent because their trunk would not show the registered identity now,
    # checked as the page's read checks each one (countries_http.pins_view).
    return _advice(sum(1 for pin in pins(here.engine) if not presentable(
        here.values, pin.account, pin.caller_id, pin.station_id, engine=here.engine)[0]))


def advice_country_rules(here, now):
    from .country_rules import view
    return _advice(sum(1 for account in view(here.engine, here.values)['accounts'] if not account['confirmed']))


def advice_renewal(here, now):
    from .renewal import view
    found = view(here.engine, here.values, now=now)
    return _advice(len(found['pages']) + sum(1 for system in found['channels']['systems'] if not system['own']))


def advice_pots(here, now):
    from .pots_quote import view
    return _advice(len(view(here.engine, here.values, routes=here.routes)['quotes']))


# -- charge checks --------------------------------------------------------------------------------------------------

def charge_checks(here, now):
    from .carriers import CarrierChargeStore
    from .provider_sweep import unrecorded
    since = now - timedelta(days=here.days)
    # Faxes a provider listed that Faxbot has no record of, as Charges lists them (stored by the sweep), and calls on
    # the trunk carrier's records that fit no Faxbot call or received fax.
    faxes = calls = 0
    if _any(here, 'provider_unrecorded_faxes', ('provider_time', '>=', since)):
        faxes = len(unrecorded(here.engine, start=since))
    if _any(here, 'carrier_records', ('applied', 1), ('started_at', '>=', since)):
        calls = sum(1 for row in CarrierChargeStore(here.engine).unrecorded_in_effect(since=since)
                    if row['inbound_fax_id'] is None)
    count = faxes + calls
    if not count:
        return 0, f"No charges in {here.days} days that your faxes don't explain"
    return count, (f"Found {_plural(count, 'charge', 'charges')} in {here.days} days that your faxes "
                   "don't explain")


# -- the faxes or calls each mechanism acted on ------------------------------------------------------------------

def measured_account(here, now):
    # Faxes whose account Faxbot chose among two or more after measuring the pages each would send (selections.py).
    return _acted(_rows(here, 'fax_route_selections', 'created_at', _since(here, now), ('measured', 1),
                        ('compared', '>=', 2), distinct='job_id'), here, 'Used on')


def caller_id_prices(here, now):
    # The price by caller ID is worked out each time a route is chosen (origin_rates.py, origin_classes.py) and is
    # not recorded with the fax or call it priced: no table or field says which calls it priced.
    return 0, 'Faxbot keeps no count of the calls it priced by caller ID'


def dialing_guard(here, now):
    count = _rows(here, 'dialing_guard_holds', 'created_at', _since(here, now), distinct='job_id')
    if not count:
        return 0, f'Held no faxes in {here.days} days'
    return count, f"Held {_plural(count, 'fax', 'faxes')} for your approval in {here.days} days"


# The start of the reason power-aware sending gives a fax it holds (power.admission); the hold keeps only that reason.
POWER_HOLD = 'This office is on battery'


def power_aware(here, now):
    from .envelope import skipped_of
    since = _since(here, now)
    held = _table('outbound_holds', 'job_id', 'kind', 'requested_at', 'reason')
    choices = _table('delivery_rule_choices', 'job_id', 'skipped', 'created_at')
    with read_connection(here.engine) as connection:
        jobs = {row.job_id for row in connection.execute(sa.select(held.c.job_id).where(
            held.c.kind == 'no_route', held.c.requested_at >= since, held.c.reason.like(POWER_HOLD + '%')))}
        # A fax under sending rules that another account took because its call could outlast the battery.
        for row in connection.execute(sa.select(choices.c.job_id, choices.c.skipped).where(
                choices.c.created_at >= since, choices.c.skipped.is_not(None))).mappings():
            if any(why == 'power' for _, why in skipped_of(row)[0]):
                jobs.add(row['job_id'])
    count = len(jobs)
    if not count:
        return 0, f'Not used in {here.days} days'
    return count, (f"Held {_plural(count, 'fax', 'faxes')} or sent {'it' if count == 1 else 'them'} another way "
                   f'during power cuts in {here.days} days')


def header_notice(here, now):
    return _acted(_rows(here, 'fax_header_notices', 'created_at', _since(here, now)), here, 'Used on')


def route_problems(here, now):
    return _acted(_rows(here, 'route_family_incidents', 'opened_at', _since(here, now)), here, 'Found',
                  'route problem', 'route problems', none='Found no route problems in {days} days')


def answer_cap(here, now):
    return _acted(_rows(here, 'answer_cap_calls', 'created_at', _since(here, now)), here, 'Hung up',
                  'unanswered call', 'unanswered calls')


def station_check(here, now):
    # Only calls answered by a machine showing another number are kept (station_check_results); matches are not.
    return _acted(_rows(here, 'station_check_results', 'created_at', _since(here, now)), here, 'Caught',
                  'call answered by a different number', 'calls answered by a different number',
                  none='Caught no calls answered by a different number in {days} days')


def keys_after_answer(here, now):
    return _acted(_rows(here, 'after_answer_calls', 'created_at', _since(here, now)), here, 'Pressed keys on',
                  'call', 'calls')


COUNTERS = {
    'charge_checks': charge_checks,
    'advice_sending': advice_sending,
    'advice_plans': advice_plans,
    'advice_receiving': advice_receiving,
    'advice_carriers': advice_carriers,
    'advice_billing_steps': advice_billing_steps,
    'advice_partners': advice_partners,
    'advice_discovery': advice_discovery,
    'advice_relays': advice_relays,
    'advice_toll_free': advice_toll_free,
    'advice_fax_marker': advice_fax_marker,
    'advice_shading': advice_shading,
    'advice_trunks': advice_trunks,
    'advice_numbers': advice_numbers,
    'advice_sites': advice_sites,
    'advice_caller_names': advice_caller_names,
    'advice_setup_packs': advice_setup_packs,
    'advice_line_inventory': advice_line_inventory,
    'advice_copper_closures': advice_copper_closures,
    'advice_registered_senders': advice_registered_senders,
    'advice_country_rules': advice_country_rules,
    'advice_renewal': advice_renewal,
    'advice_pots': advice_pots,
    'measured_account': measured_account,
    'caller_id_prices': caller_id_prices,
    'dialing_guard': dialing_guard,
    'power_aware': power_aware,
    'header_notice': header_notice,
    'route_problems': route_problems,
    # These three have no part on Savings, and the catalogue's line says where their figures are not; this page says
    # what they did, from the records that line points at.
    'answer_cap': answer_cap,
    'station_check': station_check,
    'keys_after_answer': keys_after_answer,
}


def line(key, here, now=None):
    """(count, sentence) for what the entry did here, or None when the catalogue's own line stands."""
    counter = COUNTERS.get(key)
    return None if counter is None else counter(here, now or utcnow())
