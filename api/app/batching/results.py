"""Apply the result of one shared call to every fax it carried.

The call was placed under its first fax's job and attempt, so the fax engine's
FaxResult and OriginateResponse events name that attempt. When that attempt
placed a shared call, each fax in it gets its own outcome (see ``outcomes``)
through the delivery store, idempotently per fax. Nothing is ever sent again
from here. The combined call image is removed once the call has ended.
"""
from pathlib import Path
import logging
import re

from ..outbound_store import DeliveryConflict
from .outcomes import confirmed_originals, map_call
from .store import call_members


_IDENTITY = re.compile('[a-f0-9]{32}')


def _pages(value):
    text = str(value if value is not None else '').strip()
    return int(text) if text.isdigit() and len(text) <= 6 else None


def _members(delivery, job_id, attempt_id):
    """The faxes of the shared call ``attempt_id`` placed, after authenticating that attempt; [] for a single fax."""
    if (not isinstance(job_id, str) or not _IDENTITY.fullmatch(job_id)
            or not isinstance(attempt_id, str) or not _IDENTITY.fullmatch(attempt_id)):
        return []
    members = call_members(delivery.configuration.engine, attempt_id)
    if not members:
        return []
    revision, profile = delivery.attempt_context(job_id, attempt_id)
    if (members[0]['id'] != job_id or profile.configuration.provider_id != 'sip'
            or profile.configuration.manifest is not None):
        raise DeliveryConflict('Native result does not match the shared call.')
    return members, revision


def _sheets(revision, job_id, attempt_id):
    """The long pages a shared call was sent on (``packed-<fax>-<attempt>.sheets.json``, written by
    ``pages.sending`` when it packed the call), or None when it went page by page."""
    import json
    try:
        path = Path(revision.values.fax_data_dir) / f'packed-{job_id}-{attempt_id}.sheets.json'
        if path.is_symlink() or not path.is_file():
            return None
        found = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        logging.getLogger(__name__).warning('The long pages of shared call %s could not be read; its pages are '
                                            'counted as sent.', attempt_id)
        return None
    return found if isinstance(found, list) else None


def _remove_image(revision, attempt_id):
    try:
        path = Path(revision.values.fax_data_dir) / f'batch-{attempt_id}.tiff'
        if not path.is_symlink():
            path.unlink(missing_ok=True)
    except OSError:
        logging.getLogger(__name__).warning('A shared call image could not be removed; retention cleanup will.')


def _apply(delivery, outcomes, event_key):
    failures = 0
    for outcome in outcomes:
        try:
            _, profile = delivery.attempt_context(outcome.job_id, outcome.attempt_id)
            key = outcome.attempt_id + ':' + event_key
            if outcome.status == 'unconfirmed':
                delivery.record_unconfirmed(outcome.job_id, attempt_id=outcome.attempt_id, profile_id=profile.id,
                                            event_key=key)
            else:
                delivery.observe(outcome.job_id, attempt_id=outcome.attempt_id, profile_id=profile.id,
                                 provider_sid=outcome.job_id, status=outcome.status, event_key=key,
                                 error=outcome.sentence, error_category=outcome.category)
        except Exception:
            failures += 1  # The others still get their outcomes; this one is reconciled later.
    if failures:
        raise DeliveryConflict('Some faxes in a shared call need reconciliation.')


def apply_fax_result(delivery, event, *, failure_sentence=None, failure_category=None):
    """True when the event's call carried several faxes (each now has its outcome); False for a single fax."""
    fields = {str(key).lower(): value for key, value in event.items()}
    found = _members(delivery, fields.get('jobid'), fields.get('attemptid'))
    if not found:
        return False
    members, revision = found
    status = re.sub(r'[^A-Z_]', '', str(fields.get('status') or '').upper())[:16]
    confirmed, uncertain = _pages(fields.get('pages')), None
    sheets = _sheets(revision, members[0]['id'], members[0]['batch_id'])
    if sheets is not None and confirmed is not None:
        # Sent on long pages: the engine counts long pages; each fax's own pages are the call pages on them.
        confirmed, uncertain = confirmed_originals(sheets, confirmed)
    if status == 'SUCCESS':
        outcomes = map_call(members, succeeded=True, confirmed_pages=confirmed, uncertain_through=uncertain)
    elif status == 'FAILED':
        outcomes = map_call(members, succeeded=False, confirmed_pages=confirmed,
                            failure_sentence=failure_sentence, failure_category=failure_category,
                            uncertain_through=uncertain)
    else:
        # Without a recognised ending, no page is known to be confirmed or unconfirmed.
        outcomes = map_call(members, succeeded=False, confirmed_pages=None)
    try:
        _apply(delivery, outcomes, 'ami-result:' + (status or 'unknown'))
    finally:
        _remove_image(revision, members[0]['batch_id'])
    return True


def apply_originate_failure(delivery, event, *, failure_sentence=None):
    """The shared call never connected: every fax in it failed and none of it was sent."""
    fields = {str(key).lower(): value for key, value in event.items()}
    parts = str(fields.get('actionid', '')).split(':')
    if len(parts) != 3 or parts[0] != 'faxbot':
        return False
    found = _members(delivery, parts[1], parts[2])
    if not found:
        return False
    members, revision = found
    outcomes = map_call(members, succeeded=False, confirmed_pages=0, failure_sentence=failure_sentence)
    try:
        _apply(delivery, outcomes, 'ami-originate-failure')
    finally:
        _remove_image(revision, members[0]['batch_id'])
    return True
