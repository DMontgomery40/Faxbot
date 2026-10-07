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

from .crypto import FAX_IMAGE, kind_of
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
        return f'Delivered directly as a fax image by {partner}; no telephone call.'
    return f'Delivered directly by {partner} as the original document; no telephone call.'


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
        values = self.values()
        return self.store.intake.add_fax_image(
            ImportStore(resources), account=account(row['peer_id']), message_id=row['message_id'],
            image_path=row['document_path'], from_number=manifest['sender']['fax_number'],
            to_number=manifest['recipient']['fax_number'], pages=document['pages'],
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
