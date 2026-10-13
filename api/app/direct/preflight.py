"""Receiver preflight (brief 82, research E): before paying to send a fax by phone to a partner you take part with,
Faxbot asks the partner's Faxbot, signed, whether it accepts this document class and purpose from you at this
endpoint, and into which mailbox it would arrive.

The answer is a signed capability statement with a version and an expiry. A refusal holds the fax with the
partner's reason before anything is sent. A yes is **not a receipt**: it says the partner would take this kind of
fax now, never that it arrived. An answer that has expired by the time the fax goes is asked again. A partner that
does not take part, answers 404 (an older Faxbot) or does not answer leaves the fax as it was: no answer is ever
treated as a refusal, and a preflight carries no document, so nothing is ever sent twice because of it.
"""
from __future__ import annotations

from datetime import timedelta
import json
import logging
import uuid


from ..routing.database import read_connection, utcnow, write_transaction
from .admission import FRESHNESS, _parse, _table, document_class_on, partner_on

log = logging.getLogger(__name__)
LIFETIME = timedelta(hours=1)
NOT_A_RECEIPT = ("A yes from a partner's preflight means it would take this kind of fax now; it is not a receipt and "
                 'says nothing about whether the fax arrived.')


def _mailbox(engine, values):
    """The label of the mailbox this installation's partner number delivers into, or None (the general inbox)."""
    from ..routing.numbers import InvalidNumber, normalize_number
    from ..routing.reply_number import mailbox_routes
    try:
        own = normalize_number(values.direct_fax_number, country=values.fax_default_country)
    except (InvalidNumber, TypeError, ValueError):
        return None
    return next((route.label for route in mailbox_routes(engine, values) if route.number == own), None)


def answer(engine, values, peer, request, now=None):
    """This installation's signed answer to a partner's preflight, as a dict, and the row kept."""
    now = now or utcnow()
    with read_connection(engine) as connection:
        chosen = partner_on(connection, peer['id'])
    document_class = str(request.get('document_class') or 'general')[:64].casefold()
    purpose = str(request.get('purpose') or 'general')[:64]
    mailbox = _mailbox(engine, values)
    if not chosen['take_part']:
        accepts, reason = None, 'not_taking_part'
    elif document_class in chosen['refused_classes']:
        accepts, reason = 0, chosen['refusal_reason']
    else:
        accepts, reason = 1, None
    expires = now + LIFETIME
    row = {'id': uuid.uuid4().hex, 'direction': 'answered', 'peer_id': peer['id'], 'job_id': None,
           'document_class': document_class, 'purpose': purpose, 'mailbox': mailbox, 'accepts': accepts,
           'reason': reason, 'version': chosen['version'], 'expires_at': expires, 'statement': None, 'signature': None,
           'created_at': now}
    with write_transaction(engine) as connection:
        connection.execute(_table(connection, 'preflight_answers').insert().values(**row))
    from .admission import _iso
    return {'type': 'preflight_answer', 'document_class': document_class, 'purpose': purpose, 'mailbox': mailbox,
            'accepts': None if accepts is None else bool(accepts), 'reason': reason, 'version': chosen['version'],
            'expires_at': _iso(expires), 'asked_ref': request.get('ref')}


async def ask(service, job, peer, now=None):
    """Ask ``peer`` about ``job``; True (accepts), False (refuses, the fax is held with its reason) or None (no answer
    or not taking part). Every answer is kept with the fax."""
    from ..config_runtime import run_lifecycle_step
    from .crypto import DirectProtocolError, check_signed, signed, timestamp
    from .service import PartnerUnreachable
    import httpx
    engine = service.store.engine

    def facts():
        with read_connection(engine) as connection:
            return document_class_on(connection, job['id'])
    document_class, purpose = await run_lifecycle_step(facts)
    identity = await run_lifecycle_step(lambda: service.identity(create=True))
    ref = uuid.uuid4().hex
    envelope = signed(identity, {'type': 'preflight', 'recipient': peer['signing_key'], 'ref': ref,
                                 'document_class': document_class, 'purpose': purpose,
                                 'pages': int(job.get('pages') or 1), 'asked_at': timestamp()})
    try:
        status, body = await service.http.request('POST', peer['endpoint_url'] + '/direct/preflight', json=envelope)
    except (PartnerUnreachable, httpx.HTTPError):
        return None  # no answer: asked again later; never a refusal
    row = {'id': uuid.uuid4().hex, 'direction': 'asked', 'peer_id': peer['id'], 'job_id': job['id'],
           'document_class': document_class, 'purpose': purpose, 'created_at': now or utcnow()}
    if status in (404, 405):
        row.update(mailbox=None, accepts=None, reason='not_taking_part', version=None, expires_at=None,
                   statement=None, signature=None)
    elif status == 200 and isinstance(body, dict):
        try:
            said = check_signed(body, peer['signing_key'])
            if (said.get('type') != 'preflight_answer' or said.get('recipient') != identity.signing_key
                    or said.get('asked_ref') != ref):
                raise DirectProtocolError('malformed', 'Not an answer to this preflight.')
            accepts = said.get('accepts')
            if accepts not in (True, False, None):
                raise DirectProtocolError('malformed', 'Not an answer to this preflight.')
            row.update(mailbox=said.get('mailbox'), accepts=None if accepts is None else int(accepts),
                       reason=(str(said.get('reason'))[:300] if said.get('reason') else None),
                       version=said.get('version') if type(said.get('version')) is int else None,
                       expires_at=_parse(said.get('expires_at')), statement=body['statement'],
                       signature=body['signature'])
        except (DirectProtocolError, KeyError, TypeError, ValueError):
            log.warning("A partner's preflight answer could not be checked; Faxbot asks again later.")
            return None
    else:
        return None

    def keep():
        with write_transaction(engine) as connection:
            connection.execute(_table(connection, 'preflight_answers').insert().values(**row))
    await run_lifecycle_step(keep)
    return None if row['accepts'] is None else bool(row['accepts'])


def check_request(service, statement, signature, kind, now=None):
    """A partner's signed preflight or window request: (peer, body), or a refusal (status, body). The signer must be
    an enrolled partner (not removed), the statement addressed to us and fresh; taking part is checked per partner."""
    from .crypto import DirectProtocolError, parse_timestamp, verify
    now = now or utcnow()
    _, identity = service._enabled_identity()
    refused = (400, {'detail': 'This request could not be checked.'})
    try:
        encoded = statement.encode('ascii')
        body = json.loads(encoded)
        if not isinstance(body, dict) or body.get('type') != kind or body.get('recipient') != identity.signing_key:
            return None, refused
        peer = service.store.peer_by_key(body['signer']) if isinstance(body.get('signer'), str) else None
        if peer is None or peer['state'] == 'revoked':
            return None, refused
        verify(peer['signing_key'], encoded, signature)
        if abs(parse_timestamp(body.get('asked_at')) - now) > FRESHNESS:
            return None, refused
    except (ValueError, TypeError, UnicodeEncodeError, DirectProtocolError):
        return None, refused
    return (peer, body), None


def sign_answer(service, peer, document):
    from .crypto import signed
    return signed(service.identity(), {**document, 'recipient': peer['signing_key']})
