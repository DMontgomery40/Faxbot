"""A recipient's Direct address and FHIR endpoint: entered, or suggested from NPPES, and used only once confirmed.

A recipient is a fax number Faxbot sends to. It can also carry a Direct
address (``records@direct.hospital.example``) and a FHIR endpoint (the base
address of a FHIR R4 server), each with the HISP account or FHIR client that
reaches it. Nothing is used until the administrator confirms it: a similar
number, a registry entry or a guess is never permission (D20).

NPPES suggestions
-----------------
The CMS NPI registry API (version 2.1) returns, per NPI, its addresses (each
with an optional ``fax_number``) and its ``endpoints``: ``endpointType``
(``DIRECT``: a Direct messaging address; ``FHIR``: a FHIR URL; also CONNECT,
SOAP, REST, OTHERS), ``endpoint``, ``endpointDescription``, ``affiliation``
and ``affiliationName``, ``use`` and ``contentType``. The API searches by NPI,
never by fax number, so the administrator gives the NPI and Faxbot suggests an
endpoint only when that NPI's record lists this recipient's fax number. Each
suggestion keeps the record it came from and the day it was read.
"""
from datetime import datetime
import re
from urllib.parse import urlsplit, urlunsplit

from .store import DigitalInputError


DIRECT_ADDRESS = re.compile(r'[a-z0-9.!#$%&\'*+/=?^_`{|}~-]{1,64}@(?=.{4,253}$)([a-z0-9-]{1,63}\.)+[a-z]{2,63}')
NPPES_KINDS = {'DIRECT': 'direct', 'FHIR': 'fhir'}


def direct_address(text):
    address = (text or '').strip().lower()
    if address.startswith('mailto:'):
        address = address[7:]
    if DIRECT_ADDRESS.fullmatch(address) is None:
        raise DigitalInputError('Write the Direct address in full, such as records@direct.hospital.example.')
    return address


def fhir_endpoint(text):
    """The FHIR server's base address: https, no query or fragment, no trailing slash."""
    url = (text or '').strip()
    parts = urlsplit(url)
    if parts.scheme != 'https' or not parts.hostname or parts.query or parts.fragment or parts.username:
        raise DigitalInputError('Write the FHIR server\'s base address, starting with https://, such as '
                                'https://fhir.hospital.example/r4.')
    if len(url) > 500:
        raise DigitalInputError('The FHIR address is too long.')
    host = parts.hostname
    if re.fullmatch(r'[0-9.]+|\[?[0-9a-f:]+\]?', host):
        from ..direct.addresses import public
        try:
            if not public(host.strip('[]')):
                raise DigitalInputError('The FHIR address points to a private or local network.')
        except ValueError:
            raise DigitalInputError('The FHIR address is not a web address Faxbot can use.') from None
    return urlunsplit((parts.scheme, parts.netloc.lower(), parts.path.rstrip('/'), '', ''))


def clean_address(kind, text):
    if kind == 'direct':
        return direct_address(text)
    if kind == 'fhir':
        return fhir_endpoint(text)
    raise DigitalInputError('Choose a Direct address or a FHIR endpoint.')


def add(store, values, number, *, kind, address, account_key=None, organization=None, confirm=False, note=None,
        principal_id=None, now=None):
    """Put an address on file for a recipient (and confirm it when asked). Returns the address's view."""
    from .accounts import digital_account
    address = clean_address(kind, address)
    if account_key:
        account = digital_account(values, account_key)
        wanted = 'hisp' if kind == 'direct' else 'fhir'
        if account is None or account.kind != wanted:
            raise DigitalInputError('Choose a HISP account for a Direct address, or a FHIR client for a FHIR '
                                    'endpoint.')
    organization = (organization or '').strip()[:200] or None
    return store.add_address(number=number, kind=kind, address=address, source='entered', account_key=account_key,
                             organization=organization, action='confirmed' if confirm else 'suggested', note=note,
                             principal_id=principal_id, now=now)


def _name(result):
    basic = result.get('basic') if isinstance(result.get('basic'), dict) else {}
    if basic.get('organization_name'):
        return str(basic['organization_name'])[:200]
    parts = [basic.get('first_name'), basic.get('last_name')]
    return ' '.join(str(part) for part in parts if part)[:200] or None


def endpoints_from(document, number, *, country='US', read_on=None):
    """(suggestions, sentence) from an NPPES API answer, for the recipient's fax ``number`` only.

    Each suggestion is a dict with kind, address, organization, npi and evidence.
    """
    from ..routing.numbers import InvalidNumber, normalize_number
    read_on = read_on or datetime.utcnow()
    results = document.get('results') if isinstance(document, dict) else None
    found, unmatched = [], []
    for result in results if isinstance(results, list) else []:
        if not isinstance(result, dict):
            continue
        npi = str(result.get('number') or '')
        if re.fullmatch(r'[0-9]{10}', npi) is None:
            continue
        faxes = set()
        for entry in result.get('addresses') or ():
            if isinstance(entry, dict) and entry.get('fax_number'):
                try:
                    faxes.add(normalize_number(str(entry['fax_number']), country=country))
                except (InvalidNumber, ValueError):
                    continue
        if number not in faxes:
            unmatched.append(npi)
            continue
        name = _name(result)
        day = f'{read_on.day} {read_on:%B %Y}'
        for endpoint in result.get('endpoints') or ():
            if not isinstance(endpoint, dict):
                continue
            kind = NPPES_KINDS.get(str(endpoint.get('endpointType') or '').upper())
            if kind is None or not endpoint.get('endpoint'):
                continue
            try:
                address = clean_address(kind, str(endpoint['endpoint']))
            except DigitalInputError:
                continue
            organization = str(endpoint.get('affiliationName') or '').strip()[:200] or name
            what = 'Direct address' if kind == 'direct' else 'FHIR endpoint'
            found.append({'kind': kind, 'address': address, 'organization': organization, 'npi': npi,
                          'use': endpoint.get('useDescription') or endpoint.get('use'),
                          'evidence': (f'NPPES record NPI {npi} lists this fax number and this {what}, read {day}.')})
    if found:
        sentence = ('NPPES lists these for the provider whose record has this fax number. Check each one with the '
                    'recipient, then confirm it before Faxbot uses it.')
    elif unmatched:
        sentence = (f'NPPES record NPI {unmatched[0]} does not list this fax number, so Faxbot suggests nothing from '
                    'it.')
    else:
        sentence = 'NPPES lists no Direct address or FHIR endpoint for this provider.'
    return found, sentence


def suggest_from_nppes(store, values, number, npi, *, fetch=None, principal_id=None, now=None):
    """Read one NPI's NPPES record and file what it lists for this number as suggestions (never confirmed)."""
    npi = str(npi or '').strip()
    if re.fullmatch(r'[0-9]{10}', npi) is None:
        raise DigitalInputError('Enter the ten-digit NPI.')
    if fetch is None:
        from ..routing.nppes import _fetch as fetch  # the registry read BG/AE's lookup uses
    document = fetch({'number': npi})
    country = getattr(values, 'fax_default_country', 'US') or 'US'
    suggestions, sentence = endpoints_from(document, number, country=country, read_on=now)
    filed = []
    for item in suggestions:
        filed.append(store.add_address(number=number, kind=item['kind'], address=item['address'], source='nppes',
                                       organization=item['organization'], npi=item['npi'], evidence=item['evidence'],
                                       action='suggested', principal_id=principal_id, now=now))
    return filed, sentence


def view(item):
    """An address for the API: what, from where, its state in one sentence, and its history."""
    from .text import KIND_LABELS, address_label, route_key
    states = {'suggested': 'Suggested; Faxbot does not use it until you confirm it.',
              'confirmed': 'Confirmed; faxes to this number may go this way.',
              'withdrawn': 'Withdrawn; Faxbot no longer uses it.',
              'dismissed': 'Dismissed.'}
    return {'id': item['id'], 'kind': item['kind'], 'kind_label': KIND_LABELS[item['kind']],
            'address': item['address'], 'organization': item['organization'], 'account_key': item['account_key'],
            'source': item['source'], 'npi': item['npi'], 'evidence': item['evidence'], 'state': item['state'],
            'sentence': states[item['state']], 'label': address_label(item['kind'], item['address'],
                                                                      item['organization']),
            'route_key': route_key(item['kind'], item['id']), 'created_at': item['created_at'],
            'history': [{'action': event['action'], 'note': event['note'], 'recorded_by_name': event['recorded_by_name'],
                         'recorded_at': event['created_at']} for event in item['history']]}
