"""A court fax in Sent: what was sent and how, when service or filing is deemed to occur, and the transmission record.

The transmission record is what rule 2.301 defines (the document printed by the sending fax machine, stating the
receiving number, the pages sent, the transmission time and date, and any errors), made from what the fax engine or
the fax service reported for each attempt, which it names. Rule 2.304(d) asks a filer who moves for nunc pro tunc
filing to attach it to a proof of transmission, and rule 2.306(h) asks for it with a proof of service by fax; the
drafts below carry the facts the rules list, with the signature left to the person who declares them.
"""
from __future__ import annotations

from datetime import datetime
import io

import sqlalchemy as sa

from . import rules
from .store import send_for

PAGES_AS_GIVEN = ('Sent under the California court profile: the pages went exactly as given, by a fax call to the '
                  'number given, with no packing, shading, encoded pages, trimming, header notice or shared call.')
BY_FAX = {'found': 'Each document says "By fax" immediately below its title (rule 2.304(c)); Faxbot found it in the '
                   "document's text.",
          'confirmed': 'You confirmed each document says "By fax" immediately below its title (rule 2.304(c)); '
                       "Faxbot couldn't read it from the scan.",
          'not_required': None}


def _reflect(engine, name):
    return sa.Table(name, sa.MetaData(), autoload_with=engine)


def attempts(engine, job_id):
    """Each attempt as the transmission record lists it: account, when it was sent and finished, and any error."""
    table = _reflect(engine, 'outbound_attempts')
    columns = table.c
    with engine.connect() as connection:
        rows = connection.execute(sa.select(table).where(columns.job_id == job_id)
                                  .order_by(columns.sequence)).mappings().all()
        calls = []
        try:
            engine_calls = _reflect(engine, 'fax_engine_calls')
            calls = connection.execute(sa.select(engine_calls).where(engine_calls.c.job_id == job_id)
                                       .order_by(engine_calls.c.created_at)).mappings().all()
        except sa.exc.NoSuchTableError:
            calls = []
    found = []
    for row in rows:
        found.append({'sequence': row['sequence'], 'account': row.get('provider_id') or row.get('account_key'),
                      'phase': row['phase'], 'error': row.get('error_category'),
                      'submitted_at': row.get('submitted_at'), 'completed_at': row.get('completed_at')})
    return found, [dict(call) for call in calls]


def completed_at(engine, job_id):
    """When the call that delivered the fax ended, as its attempt recorded it (the transmission's own time), or None
    while no attempt has succeeded. Never the fax's ``updated_at``, which later status reads may move."""
    attempts = sa.table('outbound_attempts', sa.column('job_id'), sa.column('phase'),
                        sa.column('completed_at', sa.DateTime()))
    with engine.connect() as connection:
        return connection.execute(sa.select(attempts.c.completed_at).where(
            attempts.c.job_id == job_id, attempts.c.phase == 'success', attempts.c.completed_at.is_not(None))
            .order_by(attempts.c.completed_at.desc()).limit(1)).scalar()


def _job(engine, job_id):
    jobs = _reflect(engine, 'fax_jobs')
    with engine.connect() as connection:
        row = connection.execute(sa.select(jobs).where(jobs.c.id == job_id)).mappings().first()
    return dict(row) if row is not None else None


def _moment(value):
    if value is None:
        return None
    at = rules.local(value)
    return f"{at:%A} {at.day} {at:%B %Y}, {at:%I:%M %p}".replace(' 0', ' ').replace('AM', 'a.m.').replace('PM', 'p.m.')


def view(engine, job_id, *, now=None):
    """Sent details for a court fax: its case, what the profile did, and, once sent, when filing or service is deemed
    to occur, one sentence each; None for a fax not sent under the profile."""
    court = send_for(engine, job_id)
    if court is None:
        return None
    job = _job(engine, job_id) or {}
    details = court['details']
    purpose = court['purpose']
    sentences = [PAGES_AS_GIVEN]
    if court['cover_pages']:
        sentences.append(f"The MC-005 cover sheet went first, so {court['sent_pages']} pages went for the "
                         f"{court['original_pages']} you gave.")
    if BY_FAX.get(court['by_fax']):
        sentences.append(BY_FAX[court['by_fax']])
    completed = completed_at(engine, job_id)
    timing = []
    if completed is not None:
        try:
            if purpose == 'serve':
                timing = list(rules.service(completed).sentences)
            else:
                filed = rules.filed_on(details.get('county'), completed)
                if filed is not None:
                    timing = [filed[1]]
        except rules.CourtCalendarUnknown as error:
            timing = [str(error)]
    found = rules.court(details.get('county'))
    court_note = None
    if purpose == 'file':
        if found is None:
            court_note = (f"Faxbot hasn't established whether the {details.get('county')} court takes direct fax "
                          'filing; its local rule decides (rule 2.304(a)).')
        elif found.status != rules.ACCEPTS:
            court_note = f"{found.county}: {found.note} Its local rule decides (rule 2.304(a))."
    return {'job_id': job_id, 'purpose': purpose,
            'purpose_text': 'Filing with the court by fax (rule 2.304)' if purpose == 'file'
            else 'Service on a party by fax (rule 2.306)',
            'case': {'county': details.get('county'), 'case_number': details.get('case_number'),
                     'title': f"{details.get('plaintiff')} v. {details.get('defendant')}",
                     'documents': details.get('documents') or [], 'served_party': details.get('served_party')},
            'source': court['source'], 'sentences': sentences, 'timing': timing, 'court_note': court_note,
            'record_ready': completed is not None or (job.get('status') or '').lower() in {'failed', 'error'}}


def record_pdf(engine, job_id) -> bytes:
    """The transmission record, with a draft proof of transmission (filing) or of service (serving), as a PDF."""
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.units import inch
    from reportlab.pdfgen import canvas
    court = send_for(engine, job_id)
    job = _job(engine, job_id)
    if court is None or job is None:
        raise LookupError('No court fax has that ID.')
    details = court['details']
    made, calls = attempts(engine, job_id)
    output = io.BytesIO()
    pdf = canvas.Canvas(output, pagesize=letter, invariant=1)
    width, height = letter
    y = height - inch

    def line(text, *, size=10, bold=False, gap=14):
        nonlocal y
        if y < inch:
            pdf.showPage()
            y = height - inch
        pdf.setFont('Helvetica-Bold' if bold else 'Helvetica', size)
        pdf.drawString(inch, y, text[:110])
        y -= gap

    status = (job.get('status') or '').lower()
    finished = completed_at(engine, job_id)
    line('Transmission record', size=14, bold=True, gap=20)
    line(f"Case: {details.get('plaintiff')} v. {details.get('defendant')}, {details.get('case_number') or 'no number'}"
         f", Superior Court of California, County of {details.get('county')}")
    line(f"Receiving fax number: {job.get('to_number')}")
    line(f"Pages sent: {court['sent_pages'] if finished is not None else 'see below'}"
         f" ({court['cover_pages']} cover sheet and {court['original_pages']} document pages)")
    line(f"Transmission completed: {_moment(finished) or 'not yet'} (California time)")
    line(f"Result: {'sent without a reported error' if status == 'success' else (job.get('error') or status)}")
    line('')
    line('Attempts, as the fax engine or the fax service reported them', bold=True)
    for item in made:
        line(f"{item['sequence']}. {item['account'] or 'account'}: {item['phase']}"
             f"{', error ' + item['error'] if item['error'] else ''}; sent {_moment(item['submitted_at']) or '-'}; "
             f"finished {_moment(item['completed_at']) or '-'}")
    for call in calls:
        line(f"   Fax engine {call.get('engine')}: {call.get('signal_rate') or 'speed not reported'}, "
             f"{call.get('transfer_seconds') or '-'} seconds of page data{', ' + call['reason'] if call.get('reason') else ''}")
    line('')
    line('Documents', bold=True)
    for document in details.get('documents') or []:
        line(f"{document['title']} ({document['pages']} page{'s' if document['pages'] != 1 else ''})")
    line('')
    if court['purpose'] == 'file':
        line('Draft proof of transmission (rule 2.304(d)); the person who sent it completes and signs it', bold=True)
        line(f"On {_moment(finished) or '[date and time]'}, I sent the documents listed above to the "
             f"Superior Court of California, County of {details.get('county')},")
        line(f"by fax machine under California Rules of Court, rule 2.304, to the court's fax number {job.get('to_number')}.")
        line('The fax machine complied with rule 2.301 and reported no error. I had the machine print a transmission')
        line('record, which is attached to this declaration.')
    else:
        line('Draft proof of service by fax (rule 2.306(h)); the person who served it completes and signs it', bold=True)
        line(f"On {_moment(finished) or '[date]'}, I served the documents listed above by fax from "
             f"{details.get('fax') or '[sending fax number]'} on {details.get('served_party')} at {job.get('to_number')}.")
        line('The transmission was reported as complete and without error. A copy of the transmission report, which')
        line('the sending fax machine properly issued, is attached.')
    line('I declare under penalty of perjury under the laws of the State of California that the foregoing is true')
    line('and correct.')
    line('')
    line('Date: ____________________     Name: ______________________________     Signature: ____________________')
    pdf.showPage()
    pdf.save()
    return output.getvalue()


def now():
    return datetime.utcnow()
