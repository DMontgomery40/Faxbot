"""The ways to find out what happened to an uncertain sent fax, cheapest first (M19).

1. **Ask the partner** (free, automatic). When the number belongs to an enrolled
   Faxbot partner, the partner's signed answer is proof either way. Faxbot never
   asks twice about the same thing: a document that went directly is already
   asked about by the direct path (``direct/service.DirectReconciler``), a call
   that broke part way by the repair (``direct/repair.py``), and either answer
   is read from their records here. Only a phone call to a partner that neither
   covers is asked about once, by ``PartnerQuestion``.
2. **Read the call record** (free, automatic, stored records only). How long the
   call lasted, by the carrier's own record when Faxbot has it (Telnyx detail
   records; SignalWire's fax ``duration``), else Faxbot's fax engine record,
   compared with how long the fax needs (``routing/predict.py``). A call far
   shorter than the fax needs reads "probably not delivered"; a long enough call
   "fits a delivered fax". Neither is proof. Phaxio's fax object (``GET
   /v2.1/faxes/{id}``) and Sinch's (``GET /v3/projects/{projectId}/faxes/{id}``)
   report no call duration (both API references read 2026-10-07), so this check
   cannot be made for faxes sent through them.
3. **A receipt query page** (one page). A drafted fax that asks the recipient to
   tick a box and fax it back. A person reviews it and sends it; Faxbot never
   sends it by itself.
4. **A phone call** (a person's time), with a short script.

Every function here reads; none sends, changes a record or places a call.
"""
from datetime import timedelta
import json

import sqlalchemy as sa

from ..routing.database import reflect


# The order and the cost of each check, cheapest first.
ORDER = ('partner', 'call_record', 'receipt_query', 'phone_call', 'npi_lookup')
COSTS = {'partner': 'Free', 'call_record': 'Free', 'receipt_query': 'One page', 'phone_call': 'A few minutes',
         'npi_lookup': 'Free'}
TITLES = {'partner': 'Ask the partner', 'call_record': 'Read the call record',
          'receipt_query': 'Fax a receipt query', 'phone_call': 'Phone the recipient',
          'npi_lookup': 'Look the provider up in the NPI registry'}
# What a finding suggests a person decides. Only a signed answer is proof.
PROOF, READING = 'proof', 'reading'
NO_DURATION = {
    'phaxio': 'Phaxio does not report how long the call lasted, so this check cannot be made for faxes sent '
              'through Phaxio.',
    'sinch': 'Sinch does not report how long the call lasted, so this check cannot be made for faxes sent '
             'through Sinch.',
}
# A call can be shorter than predicted when the machines used a better coding than the predictor assumes: JBIG
# needs 0.8 of the prepared bits on the line where 2-D coding (the predictor's default, MR) needs 1.35
# (``predict.WIRE_FACTOR``). Only a call shorter than even that fastest case reads as too short.
def _fastest_share():
    from ..routing import predict
    return predict.WIRE_FACTOR['JBIG'] / predict.WIRE_FACTOR[predict.DEFAULT_CODING]


def _pages(number):
    return f"{number} page{'' if number == 1 else 's'}"


def finding(kind, result, text, meaning=None, *, strength=None, automatic=False, action=None, **extra):
    return {'kind': kind, 'title': TITLES[kind], 'cost': COSTS[kind], 'automatic': automatic, 'result': result,
            'strength': strength, 'text': text, 'meaning': meaning, 'action': action, **extra}


class CheckSources:
    """The stored records the checks read; the direct path's tables only when they exist."""

    OPTIONAL = ('direct_call_repairs', 'direct_notices', 'carrier_charges', 'delivery_charges', 'sip_call_records',
                'direct_deliveries', 'direct_peers', 'provider_profiles', 'fax_job_rule_decisions')

    def __init__(self, engine):
        self.engine = engine
        names = set(sa.inspect(engine).get_table_names())
        present = tuple(name for name in self.OPTIONAL if name in names)
        self.tables = reflect(engine, present) if present else {}

    def table(self, name):
        return self.tables.get(name)


# -- 1. the partner ---------------------------------------------------------------------------------

def partner_for(sources, connection, number):
    """The verified partner that owns ``number``, or None."""
    peers = sources.table('direct_peers')
    if peers is None or not number:
        return None
    from ..engine_frames import same_number
    rows = connection.execute(sa.select(peers).where(peers.c.state == 'verified')).mappings().all()
    return next((dict(row) for row in rows if same_number(row['phone_number'], number)), None)


def _direct_delivery(sources, connection, attempt_id):
    table = sources.table('direct_deliveries')
    if table is None:
        return None
    row = connection.execute(sa.select(table).where(
        table.c.direction == 'outbound', table.c.attempt_id == attempt_id)
        .order_by(table.c.created_at.desc()).limit(1)).mappings().first()
    return dict(row) if row is not None else None


def repair_for(sources, connection, attempt_id):
    """The direct path's repair of a call that broke part way (sender side), or None."""
    table = sources.table('direct_call_repairs')
    if table is None:
        return None
    row = connection.execute(sa.select(table).where(table.c.role == 'sender', table.c.attempt_id == attempt_id)
                             .order_by(table.c.created_at.desc()).limit(1)).mappings().first()
    return dict(row) if row is not None else None


def _organization(sources, connection, peer_id):
    peers = sources.table('direct_peers')
    if peers is None or not peer_id:
        return 'the partner'
    name = connection.execute(sa.select(peers.c.organization).where(peers.c.id == peer_id)).scalar()
    return name or 'the partner'


def _pages_answer(organization, held, total, *, found=True):
    """A partner's signed count of the pages it holds from a call, as a finding."""
    if not found or held == 0:
        return finding('partner', 'not_delivered',
                       f'{organization} signed that it holds no page of this call.',
                       'The fax did not arrive; you can send it again.', strength=PROOF, automatic=True)
    if held >= total:
        return finding('partner', 'delivered', f'{organization} signed that it holds all {_pages(total)} of this call.',
                       'This is proof the fax arrived.', strength=PROOF, automatic=True)
    return finding('partner', 'partial',
                   f'{organization} signed that it holds the first {held} of {total} pages of this call.',
                   f'Pages {held + 1} to {total} did not arrive.', strength=PROOF, automatic=True)


def can_ask_partners():
    """Whether this installation has the direct path's signed question about a phone call's pages."""
    import importlib.util
    from .. import direct
    try:
        return importlib.util.find_spec(direct.__name__ + '.repair') is not None
    except (ImportError, ValueError):
        return False


def partner_check(sources, connection, item, job, answer=None, *, can_ask=None):
    """What a partner said, or will be asked, about this fax.

    ``answer`` is the last answer ``PartnerQuestion`` recorded (a probe event's details), if any.
    """
    direct = _direct_delivery(sources, connection, item['attempt_id'])
    if direct is not None:
        organization = _organization(sources, connection, direct['peer_id'])
        if direct['state'] == 'accepted':
            return finding('partner', 'delivered', f'{organization} signed a receipt for this document.',
                           'This is proof the partner received it.', strength=PROOF, automatic=True)
        if direct['state'] == 'refused':
            return finding('partner', 'not_delivered', f'{organization} signed that it did not receive this document.',
                           'The document did not arrive; you can send it again.', strength=PROOF, automatic=True)
        return finding('partner', 'asking',
                       f'Faxbot is asking {organization} whether this document arrived, and asks again until it '
                       'answers.', "Wait for the partner's answer before you decide.", automatic=True)
    peer = partner_for(sources, connection, job.get('to_number'))
    if peer is None:
        return finding('partner', 'unavailable', 'This number is not one of your partners, so there is no partner '
                                                 'to ask.', automatic=True)
    organization = peer['organization'] or 'the partner'
    repair = repair_for(sources, connection, item['attempt_id'])
    if repair is not None:
        return _pages_answer(organization, repair['pages_held'], repair['total_pages'])
    if answer and answer.get('result') == 'answered':
        held, total = answer.get('pages_held'), answer.get('total_pages')
        if type(held) is int and type(total) is int:
            return _pages_answer(organization, held, total, found=answer.get('status') == 'found')
    if _call_record(sources, connection, item['attempt_id']) is None:
        return finding('partner', 'unavailable',
                       f'{organization} runs Faxbot, but it can only be asked about calls your own fax line placed.',
                       automatic=True)
    if not (can_ask_partners() if can_ask is None else can_ask):
        return finding('partner', 'unavailable',
                       f'{organization} runs Faxbot, but this installation cannot ask it about phone calls yet.',
                       automatic=True)
    if answer and answer.get('result') == 'no_answer':
        return finding('partner', 'asking', f'{organization} did not answer yet; Faxbot asks again later.',
                       "Wait for the partner's answer, or try the next check.", automatic=True)
    return finding('partner', 'asking', f'Faxbot is asking {organization} which pages of this call arrived.',
                   "Wait for the partner's answer before you decide.", automatic=True)


# -- 2. the call record -----------------------------------------------------------------------------

def _call_record(sources, connection, attempt_id):
    table = sources.table('sip_call_records')
    if table is None:
        return None
    row = connection.execute(sa.select(table).where(table.c.attempt_id == attempt_id, table.c.direction == 'outbound')
                             .order_by(table.c.started_at.desc(), table.c.id).limit(1)).mappings().first()
    return dict(row) if row is not None else None


def _carrier_seconds(sources, connection, call_record_id):
    """(seconds, carrier id) from the carrier's own record of the call, or (None, None)."""
    table = sources.table('carrier_charges')
    if table is None:
        return None, None
    row = connection.execute(sa.select(table.c.call_seconds, table.c.provider_id).where(
        table.c.call_record_id == call_record_id, table.c.call_seconds.is_not(None))
        .order_by(table.c.applied.desc(), table.c.effective_at.desc()).limit(1)).first()
    return (row.call_seconds, row.provider_id) if row is not None else (None, None)


def _provider_seconds(sources, connection, attempt_id):
    """A cloud provider's reported fax duration (SignalWire's ``duration``), or None."""
    table = sources.table('delivery_charges')
    if table is None:
        return None
    return connection.execute(sa.select(table.c.billed_seconds).where(
        table.c.attempt_id == attempt_id, table.c.billed_seconds.is_not(None))
        .order_by(table.c.version.desc()).limit(1)).scalar()


def provider_of(sources, connection, item):
    """The provider identity the attempt went through (``sip`` for the installation's own fax line), or None."""
    profiles = sources.table('provider_profiles')
    if item.get('route'):
        return item['route']
    if profiles is not None and item.get('profile_id'):
        found = connection.execute(sa.select(profiles.c.provider_id).where(
            profiles.c.id == item['profile_id'])).scalar()
        if found:
            return found
    return item.get('backend') or None


def predicted_seconds(route, number, job, *, now=None):
    """How long the whole fax needs on the line, from the shared predictor; None when it cannot say."""
    from ..routing.predict import Shape, predict, tiff_page_bits
    pages = job.get('pages')
    if type(pages) is not int or pages < 1 or not number:
        return None
    bits = None
    if job.get('tiff_path'):
        bits = tiff_page_bits(job['tiff_path'])
        if bits is not None and len(bits) != pages:
            bits = None
    try:
        return predict(route or 'sip', number, Shape(pages, bits, 'fine', 'normal'), now=now).seconds
    except Exception:
        return None


def _span(seconds):
    """'52 seconds', '2 minutes 10 seconds'."""
    from ..routing.predict import duration_text
    return duration_text(seconds).removeprefix('about ')


def duration_reading(measured, predicted, pages, *, source):
    """What a call's length says about a fax of ``pages`` pages that needs ``predicted`` seconds."""
    from ..routing.predict import SETUP_SECONDS, duration_text
    lasted = f'{source} shows the call lasted {duration_text(measured)}'
    if predicted is None or predicted <= SETUP_SECONDS:
        return finding('call_record', 'unknown', f'{lasted}. Faxbot cannot work out how long this fax needs, so the '
                                                 'length does not say whether it arrived.', automatic=True)
    data = (predicted - SETUP_SECONDS) * _fastest_share()
    first_page, all_pages = SETUP_SECONDS + data / pages, SETUP_SECONDS + data
    if measured < first_page:
        return finding('call_record', 'probably_not_delivered',
                       f'{lasted}; even the first page needs about {_span(first_page)}.',
                       'The fax probably did not arrive. This reads the call record; it is not proof.',
                       strength=READING, automatic=True)
    if measured < all_pages:
        return finding('call_record', 'probably_partial',
                       f'{lasted}, less than the {_span(predicted)} all {_pages(pages)} need.',
                       'Probably only part of the fax arrived. This reads the call record; it is not proof.',
                       strength=READING, automatic=True)
    return finding('call_record', 'consistent',
                   f'{lasted}, long enough for all {_pages(pages)} (about {_span(predicted)}).',
                   'That fits a delivered fax, but it does not prove it.', strength=READING, automatic=True)


def call_record_check(sources, connection, item, job, *, now=None):
    from ..sip_calls import call_summary
    from ..routing.carriers import carrier_label
    route = provider_of(sources, connection, item)
    if route == 'direct' or _direct_delivery(sources, connection, item['attempt_id']) is not None:
        return finding('call_record', 'unavailable', 'This fax went to a partner over the internet, so there is no '
                                                     'phone call to read.', automatic=True)
    record = _call_record(sources, connection, item['attempt_id'])
    pages = job.get('pages') if type(job.get('pages')) is int and job.get('pages') > 0 else None
    if record is not None:
        if record['disposition'] != 'answered':
            return finding('call_record', 'probably_not_delivered', call_summary(record),
                           'The call never connected, so the fax probably did not arrive. This reads the call '
                           'record; it is not proof.', strength=READING, automatic=True)
        if record['ended_at'] is None and record['disposition'] == 'answered' and record['connected_seconds'] is None:
            return finding('call_record', 'unknown', 'The call record has no end yet; Faxbot reads it again.',
                           automatic=True)
        seconds, carrier = _carrier_seconds(sources, connection, record['id'])
        source = f"{carrier_label(carrier)}'s record of the call" if seconds is not None else None
        if seconds is None and record['connected_seconds'] is not None:
            seconds, source = record['connected_seconds'], "Faxbot's own record of the call"
        if seconds is None:
            return finding('call_record', 'unknown', 'The call record does not say how long the call lasted yet.',
                           automatic=True)
        if pages is None:
            return finding('call_record', 'unknown', 'Faxbot does not know how many pages this fax has, so the length '
                                                     'of the call does not say whether it arrived.', automatic=True)
        reading = duration_reading(seconds, predicted_seconds('sip', job.get('to_number'), job, now=now), pages,
                                   source=source[0].upper() + source[1:])
        confirmed = record['pages']
        if type(confirmed) is int and confirmed > 0:
            reading['text'] += f' The receiving machine confirmed {confirmed} of {pages} pages.'
        # How the call ended, from the fax engine's record (its hang-up cause), unless it says the fax went.
        ending = call_summary(record)
        if ending and not ending.startswith('Sent:'):
            reading['text'] += ' ' + ending
        return reading
    seconds = _provider_seconds(sources, connection, item['attempt_id'])
    if seconds is not None and pages is not None:
        from ..provider_labels import provider_label
        name = provider_label(route) if route else 'The fax service'
        return duration_reading(seconds, predicted_seconds(route, job.get('to_number'), job, now=now), pages,
                                source=f"{name}'s record of the fax")
    if route in NO_DURATION:
        return finding('call_record', 'unavailable', NO_DURATION[route], automatic=True)
    return finding('call_record', 'unavailable', 'There is no record of how long this call lasted.', automatic=True)


# -- 3. the receipt query and 4. the phone call -------------------------------------------------------

def receipt_query_check(item, query_job=None):
    if item.get('query_job_id'):
        from ..people_time import short
        state = (query_job or {}).get('status') or 'queued'
        sent = short(item.get('query_sent_at')) if item.get('query_sent_at') else None
        status = {'SUCCESS': 'it was delivered', 'FAILED': 'it could not be sent'}.get(str(state).upper(),
                                                                                      'it is on its way')
        when = f' on {sent}' if sent else ''
        return finding('receipt_query', 'sent', f'You sent the one-page receipt query{when}; {status}.',
                       'When the recipient faxes it back ticked, settle this fax as delivered.',
                       action=None, fax_id=item['query_job_id'])
    return finding('receipt_query', 'not_done',
                   'A one-page fax asking the recipient to tick a box and fax it back. You check it and send it '
                   'yourself; Faxbot never sends it on its own.', action='send_query')


def phone_script(item, job, *, organization, number, when_text):
    """The lines a person reads on a call to the recipient."""
    pages = job.get('pages')
    size = f'{_pages(pages)}, ' if type(pages) is int and pages > 0 else ''
    who = organization or 'our office'
    return [
        f'Call {number}.',
        f'Say: "This is {who}, about the fax we sent on {when_text}: {size}reference {item["reference"]}. '
        'Did it arrive complete?"',
        'If they have every page, settle this fax as delivered and note who you spoke to.',
        'If they do not have it, settle it as not delivered and send it again.',
    ]


def phone_check(item, job, *, organization, number, when_text):
    return finding('phone_call', 'not_done', 'Call the recipient and ask whether the fax arrived complete.',
                   action='call', script=phone_script(item, job, organization=organization, number=number,
                                                      when_text=when_text))


def person_answered_check():
    """The call record of a fax a person answered: nothing arrived, and Faxbot never called again."""
    return finding('call_record', 'not_delivered', 'A person answered the call, not a fax machine, so nothing '
                   'arrived. Faxbot did not call the number again.', 'Settle it as not delivered, and send it '
                   'again only to the right fax number.', strength=PROOF, automatic=True)


def number_check(item, *, organization, number, when_text):
    """The phone call for a fax a person answered: confirm the fax number, never whether it arrived."""
    who = organization or 'our office'
    return finding('phone_call', 'not_done', 'Call the recipient and ask for the right fax number.',
                   action='call', script=[
                       f'Call the recipient. The number Faxbot faxed, {number}, may be a voice line.',
                       f'Say: "This is {who}. We tried to fax you on {when_text}, reference {item["reference"]}, '
                       'and a person answered. What is your fax number?"',
                       'Settle this fax as not delivered, then send it again to the number they give you.'])


def npi_checks(engine, item):
    """For a healthcare provider: what the NPI registry lists for the number, from records Faxbot already read
    (``routing/nppes.py``, M18); none when Faxbot has no record that the recipient or you are a provider."""
    from ..routing.database import DeliveryStoreError
    from ..routing.nppes import DOCS_URL, NppesStore
    try:
        store = NppesStore(engine)
        listed = store.listings(item['to_number'])
        provider = bool(listed) or bool(store.own()) or bool(store.warning_for(item['job_id']))
    except (DeliveryStoreError, sa.exc.NoSuchTableError, KeyError):
        return []  # an installation before the NPPES tables (0055) has no such record
    if not provider:
        return []
    faxes = [entry for entry in listed if entry.get('kind') == 'fax']
    if faxes:
        text = (f"The NPI registry lists this number for {faxes[0]['name'] or 'a provider'}. Check the provider's "
                'fax number there, then confirm it with the recipient.')
    else:
        text = ("Look the provider up in the NPI registry: enter its name as the recipient name on Send a fax, and "
                'Faxbot checks the number against the registry before a first fax.')
    return [finding('npi_lookup', 'not_done', text, action=None, source_url=DOCS_URL)]


def suggestion(findings):
    """The outcome the strongest finding points to, or None; a person still decides."""
    for result in ('delivered', 'not_delivered', 'partial'):
        if any(item['result'] == result and item['strength'] == PROOF for item in findings):
            return 'not_delivered' if result == 'partial' else result
    if any(item['result'] in ('probably_not_delivered', 'probably_partial') for item in findings):
        return 'not_delivered'
    return None


def snapshot(findings):
    """What a person saw when they decided, small enough to keep with the decision."""
    return [{'kind': item['kind'], 'result': item['result'], 'text': (item['text'] or '')[:300]}
            for item in findings]


def evidence_json(findings):
    return json.dumps(snapshot(findings), ensure_ascii=True, separators=(',', ':'))


def answer_age_ok(answered_at, now, *, backoff):
    return answered_at is None or now - answered_at >= backoff


BACKOFF = timedelta(minutes=30)
