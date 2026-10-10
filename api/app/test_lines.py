"""Public test lines (research N10): a test fax to a line whose operator invites test faxes, from Diagnostics.

Faxbot sends one only when a person asks, never on a schedule. The list is short and curated: each line's
operator invites test faxes on its own page, kept here with that page's address and the day it was read (from the
live campaign's research of 2026-10-09 and 2026-10-10). Three kinds:

- **public**: the service shows each fax it receives on a public web page (Faxbeep in the US, the UK and
  Australia; GotFreeFax; Tokonet in Japan). Everything on the page is public there, including the header line
  Faxbot prints at the top (your organization's name and reply number), so the test sends only Faxbot's own test
  page. Faxbeep's public, read-only API (faxbeep.mintlify.app) lists what it received; Faxbot looks there on
  request (``faxbeep_receipt``) and links the page when exactly one fax fits.
- **reply**: the service faxes a page of its own back (HP, to the number the call shows, within 5 to 7 minutes;
  its line is often busy).
- **echo**: the service faxes your pages back (Interpage ReFax, up to 5 pages).

The checks before a send:

- **The reply number.** A line that faxes back calls the number your call shows (its caller ID; HP and Interpage
  both say so) and the number at the top of your pages is what a person would dial. Both must be numbers Faxbot
  receives on (``reply_check``); a fax service that sets its own sending number cannot be checked, and Faxbot
  says so.
- **The dialing guard** (``routing/guard.py``) is never bypassed. A line in a country Faxbot may not dial yet is
  refused before anything is sent, with the guard's own sentence and the class to allow; the console and the
  command line then offer to allow that country with your confirmation, through the guard's own change
  (``PUT /routing/dialing/{class}``), and the fax is checked again when it is accepted.

The fax itself is accepted as any Faxbot-made fax is (``routing/submit.accept_generated_fax``: your permission
to send, the sending rules, the guard, approvals), and its ``test_line_sends`` row is written in the same
acceptance transaction, so it is a test before any call starts. A test fax never opens a Work item for a person
who answered or a station that differed (``work/certainty.py`` leaves these faxes out).

Replies: a received fax on the reply number within the line's wait, from the line's own number, is labelled as
that line's reply (``test_line_replies``, how ``number``). One from any other number may be the reply, or may be
a real fax, so Faxbot never labels it by itself: the results offer it, and a person confirms (how ``person``).
When the wait ends with nothing, the result says the service did not answer; it is never a receive failure.
The received fax itself is never changed: it is filed and delivered as any fax is.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
import logging
import re
import uuid

import sqlalchemy as sa


log = logging.getLogger(__name__)
FAXBEEP_API = 'https://faxbeep.com/api/faxes'
FAXBEEP_HOME = 'https://faxbeep.com/'
FAXBEEP_SLUG = re.compile(r'fax_[0-9a-f]{6,32}')
RECENT = 20


@dataclass(frozen=True)
class Line:
    id: str
    number: str                   # E.164
    country: str                  # ISO code of the number's country
    operator: str
    kind: str                     # 'public', 'reply' or 'echo'
    shows: str                    # what the service does with a fax, one sentence
    source_url: str               # the operator's own invitation
    read_on: str                  # the day Faxbot's research read it (YYYY-MM-DD)
    invitation: str | None        # the operator's own words, as read
    invitation_en: str | None = None
    reply_minutes: int | None = None
    max_pages: int | None = None
    public_page: str | None = None
    receipt: str | None = None    # 'faxbeep': Faxbot can look up this fax's own public page
    note: str | None = None


FAXBEEP_QUOTE = ("Send a fax to a test number, or use a test email address for your printer's Scan to Email or "
                 'Internet Fax (I-Fax) feature.')
FAXBEEP_SHOWS = 'Shows each fax it receives on a public web page for 30 days.'
LINES = (
    Line('faxbeep-us', '+19725329272', 'US', 'Faxbeep', 'public', FAXBEEP_SHOWS, 'https://faxbeep.com/',
         '2026-10-09', FAXBEEP_QUOTE, public_page=FAXBEEP_HOME, receipt='faxbeep'),
    Line('faxbeep-gb', '+442038089463', 'GB', 'Faxbeep', 'public', FAXBEEP_SHOWS, 'https://faxbeep.com/',
         '2026-10-09', FAXBEEP_QUOTE, public_page=FAXBEEP_HOME, receipt='faxbeep'),
    Line('faxbeep-au', '+61261039109', 'AU', 'Faxbeep', 'public', FAXBEEP_SHOWS, 'https://faxbeep.com/',
         '2026-10-09', FAXBEEP_QUOTE, public_page=FAXBEEP_HOME, receipt='faxbeep'),
    Line('hp-us', '+18884732963', 'US', "HP's fax test service", 'reply',
         'Faxes a one-page reply to the number your call shows, usually within 5 to 7 minutes.',
         'https://support.hp.com/hr-en/document/ish_2385619-2276753-16', '2026-10-10', None,
         reply_minutes=20, max_pages=1,
         note="HP's support page invites a one-page test fax; its exact words were not kept. The line is often "
              'busy, so try again later if the call does not connect.'),
    Line('gotfreefax-us', '+18882933691', 'US', 'GotFreeFax', 'public',
         'Lists each fax it receives on a public web page, with the time, a masked sender and the pages.',
         'https://www.gotfreefax.com/fax-tester', '2026-10-09',
         "send a fax to our live test number and watch it arrive below. / Send any page to (888) 293-3691 — "
         "GotFreeFax's free fax test number for the US and Canada.",
         public_page='https://www.gotfreefax.com/fax-tester'),
    Line('interpage-us', '+16505309014', 'US', 'Interpage ReFax', 'echo',
         'Faxes your pages back to the number your call shows, up to 5 pages.',
         'http://www.interpage.net/wwwfax-2025/index.html', '2026-10-09',
         'To use the ReFax service, send a fax (of no more than 5 pages) to (650) 530-9014', reply_minutes=30,
         max_pages=5),
    Line('tokonet-jp', '+81429908686', 'JP', 'Tokonet FAX test', 'public',
         'Shows each fax it receives on a public web page within a minute, kept for 90 days.',
         'https://www.onetime-mail.com/faxtest/', '2026-10-09',
         '下記の番号にFAXを送信すると、受信内容をリアルタイムに確認できます',
         'Send a fax to the number below and you can check the received content in real time.',
         public_page='https://www.onetime-mail.com/faxtest/'),
)
BY_ID = {line.id: line for line in LINES}


class TestLineError(ValueError):
    """A test Faxbot cannot send or record as asked; one plain sentence."""


def line_for(line_id):
    line = BY_ID.get(str(line_id or '').strip())
    if line is None:
        raise TestLineError('There is no such test line. Choose one from the list.')
    return line


# -- the reply number -----------------------------------------------------------------------------------------------

def reply_check(values, engine):
    """Whether a reply would reach this Faxbot: the number the call shows and the number at the top of the pages,
    each against the numbers Faxbot receives on. ``reaches`` is None when Faxbot cannot tell."""
    from .accounts import default_sending_key
    from .provider_labels import provider_label
    from .routing.origin_classes import presented_identity
    from .routing.reply_number import receiving_numbers
    key = default_sending_key(values)
    caller, station, how = presented_identity(values, key, engine=engine)
    if how in ('provider',):
        return {'caller_id': None, 'header': None, 'reaches': None,
                'sentence': (f'Your faxes go out through {provider_label(key)}, which shows its own number, so Faxbot '
                             'cannot tell whether a reply from a test line would reach it.')}
    if not caller:
        return {'caller_id': None, 'header': station, 'reaches': False,
                'sentence': ('Your calls show no number, so a test line cannot fax back. Set the caller ID on your '
                             'phone line (trunk) first.')}
    header = station if station and station.startswith('+') else caller
    receiving = receiving_numbers(values) if getattr(values, 'inbound_enabled', False) else set()
    missing = [(number, what) for number, what in ((caller, 'Your calls show'), (header, 'The top of your pages shows'))
               if number not in receiving]
    if not missing:
        if header == caller:
            sentence = f'Replies come back to {caller}, the number your faxes show, and Faxbot receives faxes on it.'
        else:
            sentence = (f'Replies come back to {caller}, the number your calls show, or {header}, the number at the '
                        'top of your pages, and Faxbot receives faxes on both.')
        return {'caller_id': caller, 'header': header, 'reaches': True, 'sentence': sentence}
    number, what = missing[0]
    return {'caller_id': caller, 'header': header, 'reaches': False,
            'sentence': (f'{what} {number}, and Faxbot does not receive faxes on it, so a test line that faxes back '
                         'cannot reach Faxbot. In Numbers → Sender identity, choose a reply number Faxbot receives '
                         'on.')}


# -- the dialing guard ----------------------------------------------------------------------------------------------

def guard_state(connection, line, home_country, now):
    """Whether the dialing guard lets Faxbot dial this line now, and the class to allow when it does not."""
    from .routing import guard
    found = guard.dial_class(line.number, home_country)
    if found is None:
        return {'allowed': False, 'class': None, 'class_label': None, 'fenced': False,
                'sentence': 'Faxbot cannot tell what kind of number this is, so it does not dial it.'}
    guard.ensure_history_on(connection, home_country, now)
    verdict = guard.verdict_on(connection, found, guard.policy_on(connection), home_country, number=line.number)
    label = guard.class_label(found.key)
    if verdict.allowed:
        return {'allowed': True, 'class': found.key, 'class_label': label, 'fenced': False, 'sentence': None}
    fenced = found.key in guard.FENCED
    if fenced:
        sentence = (f'Faxbot never dials {guard.class_text(found.key)} unless you allow them in {guard.WHERE}.')
    elif verdict.source == 'administrator':
        sentence = f'You blocked {guard.class_text(found.key)} in {guard.WHERE}, so Faxbot does not dial this line.'
    else:
        sentence = (f'Faxbot has not sent to {guard.class_text(found.key)} before, so it does not dial this line '
                    'until you allow that country.')
    return {'allowed': False, 'class': found.key, 'class_label': label, 'fenced': fenced, 'sentence': sentence}


# -- records --------------------------------------------------------------------------------------------------------

def _sends():
    return sa.table('test_line_sends', sa.column('id'), sa.column('line_id'), sa.column('job_id'),
                    sa.column('number'), sa.column('reply_number'), sa.column('reply_minutes'),
                    sa.column('actor_principal_id'), sa.column('actor_name'), sa.column('created_at', sa.DateTime()))


def _replies():
    return sa.table('test_line_replies', sa.column('id'), sa.column('send_id'), sa.column('how'),
                    sa.column('actor_principal_id'), sa.column('actor_name'), sa.column('created_at', sa.DateTime()))


def _inbound():
    return sa.table('inbound_faxes', sa.column('id'), sa.column('from_number'), sa.column('to_number'),
                    sa.column('pages'), sa.column('created_at', sa.DateTime()))


def record_step(line, *, reply_number, actor_principal_id, actor_name, holder):
    """The acceptance step (``accept_generated_fax(after=...)``) that marks the fax as this line's test, in the
    same transaction that accepts it. ``holder`` receives the record's ID."""
    def step(connection, now, job_id):
        send_id = uuid.uuid4().hex
        connection.execute(_sends().insert().values(
            id=send_id, line_id=line.id, job_id=job_id, number=line.number, reply_number=reply_number,
            reply_minutes=line.reply_minutes, actor_principal_id=actor_principal_id,
            actor_name=(actor_name or None) and actor_name[:200], created_at=now))
        holder.append(send_id)
    return step


def test_page(line, when_text):
    """Faxbot's own one-page test page: no document, nothing about anyone."""
    from io import BytesIO
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas
    output = BytesIO()
    pdf = canvas.Canvas(output, pagesize=letter)
    lines = [('Helvetica-Bold', 28, 'Faxbot test page'),
             ('Helvetica', 13, f'Sent to the public test line of {_ascii(line.operator)}, {line.number}, '
                               f'{_ascii(when_text)}.'),
             ('Helvetica', 13, 'It checks that faxes from this Faxbot arrive. It holds no document.')]
    y = 680
    for font, size, text in lines:
        pdf.setFont(font, size)
        pdf.drawString(72, y, text)
        y -= size + 20
    pdf.rect(54, 54, 504, 684)
    pdf.showPage()
    pdf.save()
    return output.getvalue()


def _ascii(text):
    return ''.join(character if 32 <= ord(character) < 127 else '?' for character in str(text))


# -- results --------------------------------------------------------------------------------------------------------

def _when(moment):
    from .people_time import short
    return short(moment) if moment else None


def _fax_state(connection, job_id):
    """(state, sentence) for the test fax: on_its_way, held, sent, failed or uncertain."""
    deliveries = sa.table('outbound_deliveries', sa.column('id'), sa.column('state'))
    jobs = sa.table('fax_jobs', sa.column('id'), sa.column('status'), sa.column('error'))
    holds = sa.table('outbound_holds', sa.column('id'), sa.column('job_id'), sa.column('state'), sa.column('reason'))
    state = connection.execute(sa.select(deliveries.c.state).where(deliveries.c.id == job_id)).scalar()
    job = connection.execute(sa.select(jobs.c.status, jobs.c.error).where(jobs.c.id == job_id)).mappings().first()
    if state == 'success':
        return 'sent', 'The test fax went through.'
    if state == 'failed':
        error = (job or {}).get('error') or ''
        return 'failed', f'The test fax did not go through: {error}' if error else 'The test fax did not go through.'
    if state == 'reconciliation_required':
        return 'uncertain', 'Faxbot could not confirm whether the test fax arrived; see it in Sent.'
    if state == 'cancelled':
        return 'cancelled', 'The test fax was cancelled.'
    hold = connection.execute(sa.select(holds.c.reason).where(holds.c.job_id == job_id, holds.c.state == 'open')
                              .limit(1)).scalar()
    if hold:
        return 'held', f'The test fax waits in Sent: {hold}'
    return 'on_its_way', 'The test fax is on its way.'


def _candidates(connection, row):
    """Received faxes that may be this test's reply: on the reply number, within the line's wait, not labelled."""
    inbound, replies = _inbound(), _replies()
    start = row['created_at']
    end = start + timedelta(minutes=row['reply_minutes'])
    query = sa.select(inbound).where(inbound.c.created_at >= start, inbound.c.created_at <= end,
                                     ~sa.exists(sa.select(replies.c.id).where(replies.c.id == inbound.c.id)))
    if row['reply_number']:
        query = query.where(inbound.c.to_number == row['reply_number'])
    return [dict(item) for item in connection.execute(query.order_by(inbound.c.created_at)).mappings().all()]


def _from_line(line, number):
    from .routing.stations import digits, same_number
    return bool(number) and same_number(digits(number), digits(line.number))


def match_replies_on(connection, now):
    """Label the replies that came from the line's own number (``how`` 'number'); never any other."""
    sends, replies = _sends(), _replies()
    rows = connection.execute(sa.select(sends).where(
        sends.c.reply_minutes.is_not(None),
        ~sa.exists(sa.select(replies.c.id).where(replies.c.send_id == sends.c.id)))
        .order_by(sends.c.created_at.desc()).limit(RECENT)).mappings().all()
    for row in rows:
        line = BY_ID.get(row['line_id'])
        if line is None:
            continue
        found = [item for item in _candidates(connection, row) if _from_line(line, item['from_number'])]
        if found:
            connection.execute(replies.insert().values(id=found[0]['id'], send_id=row['id'], how='number',
                                                       created_at=now))


def _reply_view(connection, line, row, fax_state, now):
    replies = _replies()
    label = connection.execute(sa.select(replies).where(replies.c.send_id == row['id'])).mappings().first()
    if label is not None:
        return {'state': 'replied', 'inbound_id': label['id'], 'candidates': [],
                'sentence': f'{line.operator} faxed back. The reply is in Received, labelled as a test.'}
    if fax_state != 'sent':
        return {'state': 'not_sent', 'inbound_id': None, 'candidates': [], 'sentence': None}
    candidates = _candidates(connection, row)
    offered = [{'inbound_id': item['id'], 'from_number': item['from_number'], 'pages': item['pages'],
                'received_at_text': _when(item['created_at'])} for item in candidates[:5]]
    waiting = now < row['created_at'] + timedelta(minutes=row['reply_minutes'])
    if offered:
        return {'state': 'possible', 'inbound_id': None, 'candidates': offered,
                'sentence': (f'A fax arrived on {row["reply_number"] or "your number"} from another number while '
                             f'Faxbot waited for {line.operator}. If it is the reply, mark it as the test reply.'
                             if len(offered) == 1 else
                             f'{len(offered)} faxes arrived on {row["reply_number"] or "your number"} while Faxbot '
                             f'waited for {line.operator}. If one of them is the reply, mark it as the test reply.')}
    if waiting:
        return {'state': 'waiting', 'inbound_id': None, 'candidates': [],
                'sentence': f'Waiting for {line.operator} to fax back, for up to {line.reply_minutes} minutes.'}
    return {'state': 'no_answer', 'inbound_id': None, 'candidates': [],
            'sentence': (f'No answer from the service: {line.operator} did not fax back within {line.reply_minutes} '
                         'minutes. This is not a problem with receiving.')}


def send_view(connection, row, now):
    line = BY_ID.get(row['line_id'])
    fax_state, fax_sentence = _fax_state(connection, row['job_id'])
    view = {'id': row['id'], 'line_id': row['line_id'], 'operator': line.operator if line else row['line_id'],
            'number': row['number'], 'fax_id': row['job_id'], 'sent_at_text': _when(row['created_at']),
            'actor_name': row['actor_name'], 'fax_state': fax_state, 'fax_sentence': fax_sentence,
            'reply': None, 'public_page': line.public_page if line else None,
            'receipt': line.receipt if line and fax_state == 'sent' else None}
    if line is not None and row['reply_minutes']:
        view['reply'] = _reply_view(connection, line, row, fax_state, now)
    elif line is not None and line.public_page and fax_state == 'sent':
        view['public_sentence'] = f'{line.operator} shows what it received on its public page.'
    return view


def line_view(line, guard_view):
    return {**{key: value for key, value in asdict(line).items() if key != 'receipt'},
            'faxbeep_receipt': line.receipt == 'faxbeep', 'guard': guard_view}


def view(engine, values, now=None):
    """Every test line with what the dialing guard says, the reply check, and the recent test faxes."""
    now = now or datetime.utcnow()
    home = getattr(values, 'fax_default_country', 'US') or 'US'
    reply = reply_check(values, engine)
    with engine.begin() as connection:
        match_replies_on(connection, now)
        lines = [line_view(line, guard_state(connection, line, home, now)) for line in LINES]
        sends = _sends()
        rows = connection.execute(sa.select(sends).order_by(sends.c.created_at.desc(), sends.c.id.desc())
                                  .limit(RECENT)).mappings().all()
        recent = [send_view(connection, row, now) for row in rows]
    return {'lines': lines, 'sends': recent, 'reply': reply}


def send_row(engine, send_id):
    sends = _sends()
    with engine.connect() as connection:
        row = connection.execute(sa.select(sends).where(sends.c.id == send_id)).mappings().first()
    if row is None:
        raise TestLineError('There is no such test fax.')
    return dict(row)


def one_view(engine, send_id, now=None):
    now = now or datetime.utcnow()
    with engine.begin() as connection:
        match_replies_on(connection, now)
        sends = _sends()
        row = connection.execute(sa.select(sends).where(sends.c.id == send_id)).mappings().first()
        if row is None:
            raise TestLineError('There is no such test fax.')
        return send_view(connection, row, now)


def confirm_reply(engine, send_id, inbound_id, *, actor_principal_id=None, actor_name=None, now=None):
    """A person marks a received fax as this test's reply; only one Faxbot offered for it."""
    now = now or datetime.utcnow()
    with engine.begin() as connection:
        sends = _sends()
        row = connection.execute(sa.select(sends).where(sends.c.id == send_id)).mappings().first()
        if row is None or not row['reply_minutes']:
            raise TestLineError('That test line does not fax back, so there is no reply to mark.')
        if inbound_id not in {item['id'] for item in _candidates(connection, row)}:
            raise TestLineError('That received fax did not arrive on your reply number while Faxbot waited, or is '
                                'already marked.')
        connection.execute(_replies().insert().values(
            id=inbound_id, send_id=send_id, how='person', actor_principal_id=actor_principal_id,
            actor_name=(actor_name or None) and actor_name[:200], created_at=now))
        return send_view(connection, row, now)


def replies(engine):
    """Received faxes labelled as a test line's reply, for Received."""
    sends, labels = _sends(), _replies()
    with engine.connect() as connection:
        rows = connection.execute(sa.select(labels.c.id, labels.c.how, sends.c.line_id)
                                  .select_from(labels.join(sends, sends.c.id == labels.c.send_id))
                                  .order_by(labels.c.created_at.desc()).limit(500)).mappings().all()
    found = []
    for row in rows:
        line = BY_ID.get(row['line_id'])
        operator = line.operator if line else 'a public test line'
        found.append({'inbound_id': row['id'], 'line_id': row['line_id'], 'how': row['how'],
                      'label': 'Test reply', 'sentence': f'The reply to a test fax sent to {operator}.'})
    return found


# -- Faxbeep's public receipt ---------------------------------------------------------------------------------------

def faxbeep_receipt(row, *, get=None, now=None):
    """Faxbeep's public page for this test fax: looked up only when asked, in Faxbeep's public read-only API.

    Faxbeep masks the sender and does not say which of its numbers a fax arrived on, so a fax fits only by its
    time (after the test was sent, within an hour) and its single page. Exactly one fit: its page. Several: the
    list, and Faxbot says it cannot tell which. ``get(url, params)`` returns parsed JSON (tests pass their own)."""
    if get is None:
        get = _faxbeep_get
    since = row['created_at'] - timedelta(minutes=1)
    try:
        found = get(FAXBEEP_API, {'since': since.strftime('%Y-%m-%dT%H:%M:%SZ'), 'limit': 100})
    except (OSError, ValueError, RuntimeError) as error:
        log.info('Faxbeep could not be read (%s).', type(error).__name__)
        return {'url': None, 'sentence': 'Faxbeep could not be reached. Try again in a moment.'}
    if not isinstance(found, list):
        return {'url': None, 'sentence': 'Faxbeep answered in a way Faxbot does not understand. Look on its page.',
                'list_url': FAXBEEP_HOME}
    end = row['created_at'] + timedelta(hours=1)
    fits = []
    for item in found:
        if not isinstance(item, dict) or item.get('source') != 'phone' or item.get('page_count') != 1:
            continue
        try:
            received = datetime.strptime(str(item.get('received_at')), '%Y-%m-%dT%H:%M:%SZ')
        except ValueError:
            continue
        slug = str(item.get('slug') or '')
        if row['created_at'] - timedelta(minutes=1) <= received <= end and FAXBEEP_SLUG.fullmatch(slug):
            fits.append((received, slug))
    if len(fits) == 1:
        return {'url': f'https://faxbeep.com/faxtest/{fits[0][1]}', 'list_url': FAXBEEP_HOME,
                'sentence': 'Faxbeep shows your test page on its public page.'}
    if fits:
        return {'url': None, 'list_url': FAXBEEP_HOME,
                'sentence': (f'Faxbeep shows {len(fits)} one-page faxes from that time and hides who sent them, so '
                             'Faxbot cannot tell which is yours. Look for your header line on its page.')}
    return {'url': None, 'list_url': FAXBEEP_HOME,
            'sentence': 'Faxbeep does not show it yet. It usually appears within a minute of the call.'}


def _faxbeep_get(url, params):
    import httpx
    try:
        response = httpx.get(url, params=params, timeout=10.0, follow_redirects=False,
                             headers={'User-Agent': 'Faxbot test line check'})
    except httpx.HTTPError as error:
        raise OSError(str(error)) from None
    if response.status_code != 200:
        raise RuntimeError(f'Faxbeep answered {response.status_code}')
    return response.json()
