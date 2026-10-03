"""Authenticate outbound observations using the originally accepted account."""
import hashlib
import json
import re

from .callback_locator import callback_base_url, callback_url_with_locators
from .outbound_store import DeliveryConflict
from .outbound_transport import normalize_status
from .provider_signatures import verify_phaxio_signature, verify_signalwire_signature


class CallbackRejected(ValueError):
    def __init__(self):
        super().__init__('Callback does not match an authenticated outbound attempt.')


def _one(fields, names):
    values = {value for key, value in fields if key in names and value != ''}
    if len(values) != 1:
        raise CallbackRejected()
    return values.pop()


def _phaxio_payload(fields):
    # v2 uses top-level Fax Object form fields. Retain bracketed fields and
    # a JSON Fax Object for installations configured with v2.1 webhooks.
    expanded = list(fields)
    for key, value in fields:
        if key == 'fax':
            try:
                fax = json.loads(value)
                if not isinstance(fax, dict):
                    raise ValueError
                for name in ('id', 'status'):
                    entry = fax.get(name)
                    if isinstance(entry, int) and not isinstance(entry, bool):
                        entry = str(entry)
                    if entry is not None:
                        if not isinstance(entry, str):
                            raise ValueError
                        expanded.append(('fax[' + name + ']', entry))
            except (TypeError, ValueError):
                raise CallbackRejected() from None
    return _one(expanded, {'id', 'fax[id]'}), _one(expanded, {'status', 'fax[status]'})


class CapturedCallbacks:
    def __init__(self, store):
        self.store = store

    def receive(self, provider, job_id, attempt_id, *, fields, files, signature):
        if (provider not in {'phaxio', 'signalwire'}
                or not isinstance(job_id, str) or re.fullmatch('[a-f0-9]{32}', job_id) is None
                or not isinstance(attempt_id, str) or re.fullmatch('[a-f0-9]{32}', attempt_id) is None):
            raise CallbackRejected()
        try:
            row = self.store.get(job_id)
        except DeliveryConflict:
            raise CallbackRejected() from None
        if row['attempt_id'] != attempt_id:
            raise CallbackRejected()
        revision, profile = self.store.configuration.outbound_context(job_id)
        configuration = profile.configuration
        if configuration.provider_id != provider or configuration.manifest is not None:
            raise CallbackRejected()
        url = callback_url_with_locators(callback_base_url(revision, profile), job_id, attempt_id)
        credentials = configuration.credentials
        if provider == 'phaxio':
            # Disabled verification means callbacks are disabled; it never grants
            # an unsigned request authority to change a delivery result.
            if configuration.settings.get('verify_signature', True) is not True:
                raise CallbackRejected()
            authenticated = verify_phaxio_signature(credentials.get('callback_token', ''), url, fields, files, signature)
        else:
            authenticated = not files and verify_signalwire_signature(
                credentials.get('webhook_signing_key', ''), url, fields, signature)
        if not authenticated:
            raise CallbackRejected()
        try:
            if provider == 'phaxio':
                sid, status = _phaxio_payload(fields)
            else:
                sid, status = _one(fields, {'FaxSid', 'sid'}), _one(fields, {'FaxStatus', 'status'})
            status = normalize_status(status)
            # Match signature ordering: distinct names may move, but repeated
            # names retain their signed wire order. Never sort their values.
            event = json.dumps({'fields': sorted(fields, key=lambda part: part[0]),
                'files': [(name, hashlib.sha256(data).hexdigest())
                          for name, data in sorted(files, key=lambda part: part[0])]},
                sort_keys=True, separators=(',', ':'))
            key = 'callback:' + hashlib.sha256(event.encode()).hexdigest()
        except (TypeError, ValueError):
            raise CallbackRejected() from None
        return self.store.observe(job_id, attempt_id=attempt_id, profile_id=profile.id,
                                  provider_sid=sid, status=status, event_key=key)
