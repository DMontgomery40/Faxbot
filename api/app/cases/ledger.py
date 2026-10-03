"""Which documents of a case each recipient has accepted, and the packet to send next.

A document is identified by the SHA-256 of its exact bytes, so a revised
document is new. It counts as accepted for a recipient once a fax that
carried it to that recipient for that case finished successfully. A packet
may leave accepted documents out only when the recipient's destination
profile says it accepts references; otherwise everything is sent.
"""
from dataclasses import dataclass
from datetime import datetime
import hashlib
from io import BytesIO
import re
from uuid import uuid4

import sqlalchemy as sa

from ..routing.database import read_connection, reflect, utcnow, write_transaction


CASE_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,99}')


class CaseInputError(ValueError):
    pass


@dataclass(frozen=True)
class CaseDocument:
    title: str
    data: bytes
    pages: int

    @property
    def digest(self):
        return hashlib.sha256(self.data).hexdigest()


@dataclass(frozen=True)
class PacketPlan:
    included: tuple
    referenced: tuple
    references_allowed: bool

    @property
    def pages(self):
        index = 1 if self.referenced else 0
        return index + sum(document.pages for document in self.included)

    @property
    def pages_saved(self):
        return max(0, sum(entry['page_count'] for entry in self.referenced) - (1 if self.referenced else 0))


def check_case_id(value):
    if not isinstance(value, str) or CASE_ID.fullmatch(value) is None:
        raise CaseInputError('Use a case reference of letters, numbers, dots, dashes or colons, up to 100 characters.')
    return value


class CaseLedger:
    TABLES = ('case_documents', 'delivery_destinations', 'outbound_deliveries')

    def __init__(self, engine):
        self.engine = engine
        tables = reflect(engine, self.TABLES)
        self.documents = tables['case_documents']
        self.destinations = tables['delivery_destinations']
        self.deliveries = tables['outbound_deliveries']

    def _settle(self, connection, case_id, recipient, now):
        """Mark documents accepted whose carrying fax finished successfully."""
        d, o = self.documents, self.deliveries
        rows = connection.execute(sa.select(d.c.id, o.c.updated_at).join(o, o.c.id == d.c.source_job_id).where(
            d.c.case_id == case_id, d.c.recipient == recipient, d.c.accepted_at.is_(None),
            o.c.state == 'success')).all()
        for identity, finished in rows:
            connection.execute(d.update().where(d.c.id == identity).values(accepted_at=finished or now))

    def entries(self, case_id, recipient):
        now = utcnow()
        with write_transaction(self.engine) as connection:
            self._settle(connection, case_id, recipient, now)
            return [dict(row) for row in connection.execute(sa.select(self.documents).where(
                self.documents.c.case_id == case_id, self.documents.c.recipient == recipient).order_by(
                self.documents.c.created_at, self.documents.c.first_page, self.documents.c.id)).mappings()]

    def references_allowed(self, recipient):
        with read_connection(self.engine) as connection:
            value = connection.scalar(sa.select(self.destinations.c.accepts_references).where(
                self.destinations.c.phone_number == recipient))
        return value == 1

    def plan(self, case_id, recipient, documents):
        """Split the submission into documents to send and accepted ones to reference."""
        unique, seen = [], set()
        for document in documents:
            if document.digest not in seen:
                seen.add(document.digest)
                unique.append(document)
        allowed = self.references_allowed(recipient)
        accepted = {entry['digest']: entry for entry in self.entries(case_id, recipient)
                    if entry['accepted_at'] is not None}
        if not allowed:
            return PacketPlan(tuple(unique), (), False)
        included = tuple(document for document in unique if document.digest not in accepted)
        referenced = tuple(accepted[document.digest] for document in unique if document.digest in accepted)
        return PacketPlan(included, referenced, True)

    def record(self, case_id, recipient, plan, job_id):
        """Remember what this fax carries; acceptance follows its delivery."""
        now = utcnow()
        with write_transaction(self.engine) as connection:
            page = 2 if plan.referenced else 1
            for document in plan.included:
                existing = connection.execute(sa.select(self.documents).where(
                    self.documents.c.case_id == case_id, self.documents.c.recipient == recipient,
                    self.documents.c.digest == document.digest)).mappings().one_or_none()
                if existing is None:
                    connection.execute(self.documents.insert().values(
                        id=uuid4().hex, case_id=case_id, recipient=recipient, digest=document.digest,
                        title=document.title, page_count=document.pages, first_page=page,
                        last_page=page + document.pages - 1, source_job_id=job_id, created_at=now))
                elif existing['accepted_at'] is None:
                    # Sent again before the earlier fax finished; the newest send carries it now.
                    connection.execute(self.documents.update().where(self.documents.c.id == existing['id']).values(
                        source_job_id=job_id, first_page=page, last_page=page + document.pages - 1))
                page += document.pages


def index_page(case_id, recipient, plan, organization):
    """The one-page index that stands in for documents the recipient already accepted."""
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas
    output = BytesIO()
    pdf = canvas.Canvas(output, pagesize=letter)
    y = 740

    def line(text, font='Helvetica', size=11, gap=16):
        nonlocal y
        pdf.setFont(font, size)
        pdf.drawString(54, y, text[:110])
        y -= gap
    line(f'Case {case_id}: documents for {recipient}', 'Helvetica-Bold', 14, 24)
    line(f'From {organization}. This fax carries only new or revised documents.')
    line('The documents listed below were already accepted for this case and are not repeated.', gap=24)
    line('Already accepted', 'Helvetica-Bold', 12, 18)
    for entry in plan.referenced:
        accepted = entry['accepted_at'].strftime('%Y-%m-%d')
        pages = '1 page' if entry['page_count'] == 1 else f"{entry['page_count']} pages"
        line(f"{entry['title']} ({pages}), accepted {accepted}, reference {entry['digest'][:12]}")
        if y < 160:
            line('More accepted documents are listed in the case record.')
            break
    y -= 8
    line('Included in this fax', 'Helvetica-Bold', 12, 18)
    page = 2
    for document in plan.included:
        pages = '1 page' if document.pages == 1 else f'{document.pages} pages'
        line(f'{document.title} ({pages}), starting on page {page}')
        page += document.pages
        if y < 72:
            break
    pdf.showPage()
    pdf.save()
    return output.getvalue()


def compose(case_id, recipient, plan, organization):
    """The packet PDF: an index page when documents are referenced, then each included document."""
    from pypdf import PdfReader, PdfWriter
    writer = PdfWriter()
    if plan.referenced:
        for page in PdfReader(BytesIO(index_page(case_id, recipient, plan, organization))).pages:
            writer.add_page(page)
    for document in plan.included:
        for page in PdfReader(BytesIO(document.data)).pages:
            writer.add_page(page)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()
