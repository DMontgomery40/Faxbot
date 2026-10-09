"""Every document a partner delivers directly becomes a received fax.

Accepting a direct delivery and filing it are two steps, because they use two
different stores: the signed receipt is given once the document is stored and
recorded, and filing then makes it a received fax through the same acquisition
records as any fax (``inbound/acquisition.py``): source ``local``, because
Faxbot itself delivers it with no provider, and account ``direct:<partner>``.
Received shows where it came from, mailbox rules place it, email delivery sends
it once, and Work gives it an owner and a deadline.

Filing is idempotent on the message ID, so a crash between the receipt and the
filing heals itself: ``DirectFiling.step`` files every accepted arrival that is
not filed yet. Fences (a "not received" answer) and arrivals accepted before
0034, which already had their email item, are never filed.
"""
import json
import logging

from .crypto import FAX_IMAGE, FORM, kind_of
from .store import FILING_ACCOUNT


def received_text(record):
    """The Received sentence for a direct arrival, from the partner name kept when it arrived."""
    try:
        report = json.loads(record.get('report') or '{}')
    except (TypeError, ValueError):
        report = {}
    report = report if isinstance(report, dict) else {}
    partner = report.get('partner') if isinstance(report.get('partner'), str) and report.get('partner') else 'a partner'
    if report.get('kind') == FAX_IMAGE:
        if report.get('carriage') == 'body':
            # Rebuilt from a page body the partner sent before and only its new header lines (reuse.py).
            from .reuse import BODY, received_text as reuse_text
            return reuse_text(partner, BODY)
        return f'Delivered directly as a fax image by {partner}; no telephone call.'
    if report.get('kind') == FORM:
        form = report.get('form') if isinstance(report.get('form'), dict) else {}
        name, number = form.get('name'), form.get('version')
        if isinstance(name, str) and name and type(number) is int:
            return f'Delivered directly by {partner} as the form {name} (version {number}); no telephone call.'
        return f'Delivered directly by {partner} as a registered form; no telephone call.'
    if isinstance(report.get('call_repair'), dict):
        from .repair import received_repair_text
        return received_repair_text(report)
    if isinstance(report.get('send_once'), dict):
        # Filed at this installation's intake for one of its numbers, or sent as a reference or changes.
        from .distribute import received_sentence
        return received_sentence(partner, report['send_once'])
    notice = report.get('notice') if isinstance(report.get('notice'), dict) else None
    if notice is not None and notice.get('fax'):
        # The original was never faxed: only its one-page notice was (direct/notice.py).
        return f'Delivered directly by {partner} as the original document; only a one-page notice came by fax.'
    if notice is not None:
        return f'Delivered directly by {partner} as the original document; filed by hand without its notice fax.'
    return f'Delivered directly by {partner} as the original document; no telephone call.'


def _form_drawn(engine, message_id):
    """The registered form a partner's form arrival was drawn from, as its name and version (forms/), or None."""
    from ..forms.store import FormStore
    try:
        forms = FormStore(engine)
        delivery = forms.delivery(direction='inbound', message_id=message_id)
        version = forms.version(version_id=delivery['form_version_id']) if delivery else None
    except Exception:
        logging.getLogger(__name__).warning('The form a partner delivered could not be named in Received.')
        return None
    return {'name': version.title, 'version': version.number} if version is not None else None


def _notice_paired(engine, message_id, peer_id):
    """The paired notice of a held original (notice.py), as Received describes it, or None."""
    try:
        from .notice import NoticeStore, code_text
        row = NoticeStore(engine).find('receiver', message_id)
    except Exception:
        return None
    if row is None or row['state'] != 'paired' or row['peer_id'] != peer_id:
        return None
    return {'code': code_text(row['notice_id']), 'fax': row['inbound_id'], 'matched_by': row['matched_by']}


def _call_repair(engine, message_id, peer_id):
    """The completed repair whose missing pages arrived as ``message_id`` (repair.py), or None."""
    try:
        from .repair import RepairStore
        row = RepairStore(engine).for_message('receiver', message_id)
    except Exception:
        return None
    if row is None or row['peer_id'] != peer_id or row['state'] != 'completed' or not row['assembled_path']:
        return None
    return row


def _body_reused(row):
    """How a fax image rebuilt from a body this installation held arrived, from its own signed receipt (reuse.py):
    {'carriage': 'body', 'base_sha256': the held image, 'body_sha256'}, or None for an image sent whole."""
    try:
        statement = json.loads(json.loads(row.get('receipt') or '{}').get('statement') or '{}')
    except (TypeError, ValueError, AttributeError):
        return None
    if not isinstance(statement, dict) or statement.get('carriage') != 'body':
        return None
    return {'carriage': 'body', 'base_sha256': statement.get('base_sha256'), 'body_sha256': statement.get('body_sha256')}


def account(peer_id):
    return FILING_ACCOUNT + (peer_id or 'unknown')


class DirectFiling:
    """File accepted direct arrivals as received faxes."""

    def __init__(self, store, resources, *, values):
        """``resources()`` returns the access runtime's inbound resources (None when not ready)."""
        self.store, self.resources, self.values = store, resources, values

    def file(self, row):
        """File one accepted arrival; returns the received fax's id. Repeating it returns the same one."""
        from ..inbound.acquisition import ImportStore
        resources = self.resources()
        if resources is None:
            return None
        manifest = json.loads(row['manifest'])
        kind = kind_of(manifest)
        peer = self.store.get_peer(row['peer_id']) if row['peer_id'] else None
        # The enrolled partner's name, kept as it was when the document arrived.
        partner = peer['organization'] if peer else manifest['sender']['organization']
        document = manifest['document']
        report = {'route': 'direct', 'kind': kind, 'partner': partner, 'message_id': row['message_id'],
                  'sha256': document['sha256'], 'size': document['size'], 'pages': document['pages']}
        if kind == FAX_IMAGE:
            report['fax'] = manifest['fax']
            body = _body_reused(row)
            if body is not None:
                report.update(body)
        elif kind == FORM:
            report['form'] = _form_drawn(self.store.engine, row['message_id'])
        from .distribute import filing_facts
        send_once = filing_facts(row)
        if send_once is not None:
            report['send_once'] = send_once
        paired = _notice_paired(self.store.engine, row['message_id'], row['peer_id'])
        if paired is not None:
            report['notice'] = paired
        image_path, pages = row['document_path'], document['pages']
        whole = _call_repair(self.store.engine, row['message_id'], row['peer_id']) if kind == FAX_IMAGE else None
        if whole is not None:
            # The pages missing after a broken call, filed with the call's own pages as one fax (repair.py).
            image_path, pages = whole['assembled_path'], whole['total_pages']
            report['pages'] = pages
            report['call_repair'] = {'pages_from_call': whole['pages_held'], 'total_pages': whole['total_pages'],
                                     'call_fax': whole['inbound_id']}
        values = self.values()
        return self.store.intake.add_fax_image(
            ImportStore(resources), account=account(row['peer_id']), message_id=row['message_id'],
            image_path=image_path, from_number=manifest['sender']['fax_number'],
            to_number=manifest['recipient']['fax_number'], pages=pages,
            received_at=row['accepted_at'] or row['created_at'], report=report,
            country=getattr(values, 'fax_default_country', 'US') or 'US', original=kind != FAX_IMAGE)

    def step(self):
        """File every accepted arrival not filed yet; one that fails waits for the next step."""
        for row in self.store.unfiled():
            try:
                self.file(row)
            except Exception:
                logging.getLogger(__name__).warning('A document a partner delivered directly could not be filed in '
                                                    'Received yet; Faxbot will try again.')
        return False
