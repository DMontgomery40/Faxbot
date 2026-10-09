"""Did a failed call end before any fax data? Each provider's own documented answer (provider-rules design §4.10).

A fax whose route a sending rule chose moves to the next account only after a
definite failure that ended before any fax data: busy, no answer, no fax
machine, refused when the fax was handed over (owner's answer Q3). Each
function here returns True when the provider's documented code says so, False
when it says data was exchanged (pages were sent), and None when it does not
say, which allows no rule fallback. Codes come from each provider's API
reference, read on 2026-10-07; none is reconstructed from memory.

- **Phaxio** (v2.1 error messages, https://www.phaxio.com/docs/errorMessages,
  fetched): ``lineError`` is "There was a problem with the phone line. The call
  could not be placed." ``documentConversionError`` is a file Faxbot posted that
  could not become fax pages, so nothing was dialed. ``faxError``, ``fatalError``
  and ``generalError`` do not say no data was sent: unknown.
- **Sinch** (Fax API v3 error messages,
  https://developers.sinch.com/docs/fax/api-reference/fax/error-messages/fax-error-messages,
  fetched): ``CALL_ERROR`` uses Phaxio's ``lineError`` codes; Faxbot counts only
  the call errors ``sinch_service.CALL_ERRORS`` lists (busy, no answer, no fax
  machine, not in service, could not connect) with no page sent
  (``pagesSentSuccessfully``). A ``DOCUMENT_CONVERSION_ERROR`` never dialed.
- **SignalWire** (Compatibility API fax resource,
  https://signalwire.com/docs/compatibility-api/rest/faxes/retrieve-fax.md, and
  its common fax errors, https://signalwire.com/docs/platform/fax/common-errors.md,
  both fetched): the statuses ``busy`` and ``no-answer``; and ``failed`` with
  "Connection Failed" ("no receiver on the other end of the call") or "Fax
  transmission not established" ("could not detect a remote fax machine").
  Every other message (training, DCS/TCF, page-stage errors) is unknown.
- **HumbleFax** (REST API, https://api.humblefax.com/, fetched): each recipient's
  ``attempts[]`` has ``numPagesSent`` and ``failureReason``; the documented
  reasons "Receiver did not pick up" and (read 2026-10-04) "No fax machine
  detected at destination" ended before data when no page was sent. A page sent
  on any attempt (or "partial success") means data was exchanged. "image failure"
  never dialed.
- **Documo** (mFax): the API reference is a JavaScript page Faxbot's research
  could not read and the help articles answered 403; the result codes below
  (6100 "Fax Number Busy", 6000 "Fax Connect Failed", 5100 "Fax Request
  Blocked", 5200 "Fax Rendering Issue") are from search-result text attributed
  to Documo's help pages. Not yet run against a real Documo account; an
  integration checks ``resultCode`` against Documo's own list.
- **eFax Enterprise**: its published quick-start guide names an error-code list
  in the API portal that is not public; Faxbot classifies nothing (no rule
  fallback) until that list is read.
- **Faxbot's own trunk**: the SSL Fax engine's codes (``hylafax_engine.
  _BEFORE_FAX_DATA``) and, without the engine, Asterisk's SendFAX result with
  no page transferred.
"""


# Phaxio v2.1 error types that never reached a fax machine.
PHAXIO_BEFORE = ('lineError', 'documentConversionError')
# SignalWire statuses and common-error messages that ended before any fax data.
SIGNALWIRE_STATUSES = ('busy', 'no-answer', 'no_answer')
SIGNALWIRE_MESSAGES = ('connection failed', 'fax transmission not established')
# HumbleFax failure reasons (lowercase fragments) that, with no page sent, ended before any fax data.
HUMBLEFAX_REASONS = ('did not pick up', 'no fax machine')
# Documo result codes (search-result text; see the module notes).
DOCUMO_BEFORE = ('6100', '6000', '5100', '5200')


def _int(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def phaxio(error_type, pages=None):
    """Phaxio v2.1: ``lineError`` and ``documentConversionError`` never reached a fax machine."""
    if not isinstance(error_type, str):
        return None
    if error_type in PHAXIO_BEFORE:
        return True
    return None


def sinch(fax):
    """Sinch v3: a listed call error with no page sent, or a document that never became pages."""
    if not isinstance(fax, dict):
        return None
    pages = _int(fax.get('pagesSentSuccessfully'))
    if pages:
        return False
    kind = str(fax.get('errorType') or '').upper()
    if kind == 'DOCUMENT_CONVERSION_ERROR':
        return True
    if kind == 'CALL_ERROR':
        from ..sinch_service import CALL_ERRORS
        code = fax.get('errorCode')
        return True if type(code) is int and code in CALL_ERRORS else None
    return None


def signalwire(status, error_message=None):
    """SignalWire: ``busy`` or ``no-answer``, or a failure with one of the two documented pre-connection messages."""
    status = (status or '').strip().lower() if isinstance(status, str) else ''
    if status in SIGNALWIRE_STATUSES:
        return True
    message = (error_message or '').strip().lower() if isinstance(error_message, str) else ''
    if status == 'failed' and any(message.startswith(known) for known in SIGNALWIRE_MESSAGES):
        return True
    return None


def humblefax(fax):
    """HumbleFax: no page sent on any attempt and a documented pre-connection reason; any page sent is False."""
    if not isinstance(fax, dict):
        return None
    status = str(fax.get('status') or '').strip().lower()
    if status == 'partial success':
        return False
    if status == 'image failure':
        return True
    reasons, sent = [], 0
    for recipient in fax.get('recipients') or ():
        if not isinstance(recipient, dict):
            continue
        reasons.append(str(recipient.get('failureReason') or '').lower())
        for attempt in recipient.get('attempts') or ():
            if not isinstance(attempt, dict):
                continue
            pages = _int(attempt.get('numPagesSent'))
            if pages is None:
                return None  # HumbleFax did not say how many pages went: unknown
            sent += pages
            reasons.append(str(attempt.get('failureReason') or '').lower())
    if sent:
        return False
    if not reasons or not any(any(word in reason for word in HUMBLEFAX_REASONS) for reason in reasons if reason):
        return None
    return True


def documo(result_code):
    """Documo: busy, could not connect, blocked or not rendered (see the module notes on the source)."""
    code = str(result_code).strip() if isinstance(result_code, (str, int)) and not isinstance(result_code, bool) else ''
    return True if code in DOCUMO_BEFORE else None


def efax(*_):
    """eFax Enterprise publishes no error-code list Faxbot could read: unknown, so no rule fallback."""
    return None


def native_trunk(status, pages, station=None):
    """Asterisk's SendFAX result without the engine: a failure with no page transferred, and no answer from a fax
    machine that named itself, never carried fax data; a page or a named fax machine means data was exchanged."""
    if not isinstance(status, str) or status.strip().upper() not in ('FAILED', 'FAILURE', 'FAIL'):
        return None
    count = _int(pages)
    if count or (isinstance(station, str) and station.strip()):
        return False
    if count is None:
        return None
    return True


def native_event(event):
    """``native_trunk`` from an Asterisk FaxResult event (``Status``, ``Pages``, ``Station64``)."""
    if not isinstance(event, dict):
        return None
    fields = {str(key).lower(): value for key, value in event.items()}
    return native_trunk(fields.get('status'), fields.get('pages'), fields.get('station64'))


def callback(provider, fields):
    """The classification from a provider's signed status callback (form fields as ``[(name, value)]``)."""
    import json
    values = {}
    for key, value in fields or ():
        if isinstance(value, str):
            values.setdefault(key, value)
    if provider == 'signalwire':
        return signalwire(values.get('FaxStatus') or values.get('status'),
                          values.get('ErrorMessage') or values.get('error_message'))
    if provider == 'phaxio':
        error_type = values.get('fax[error_type]') or values.get('error_type')
        if error_type is None and values.get('fax'):
            try:
                fax = json.loads(values['fax'])
            except ValueError:
                fax = None
            error_type = fax.get('error_type') if isinstance(fax, dict) else None
        return phaxio(error_type)
    return None
