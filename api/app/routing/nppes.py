"""Toll-free fax numbers a US healthcare provider published in the NPI registry, as evidence for a suggestion.

The CMS NPPES registry API (``https://npiregistry.cms.hhs.gov/api/?version=2.1``)
returns each provider's addresses, and an address carries ``fax_number`` when
the provider published one; many are toll-free (an 866 number in a sample of
20 Denver organisations, read 2026-10-07). The API searches by NPI, name,
place or taxonomy, never by phone or fax number.

This is evidence for a person, never a decision. One NPI's mailing address
and practice location can be different intakes (billing and a clinic), so
Faxbot never dials a number found here: a person records the recipient's
approval (``routing.tollfree``), and only an approval changes the number a fax
dials (``routing.alternates``). Nothing here is called while a fax is sent.
"""
from datetime import datetime
import re

from .dialing import is_toll_free
from .numbers import InvalidNumber, normalize_number


API_URL = 'https://npiregistry.cms.hhs.gov/api/'
PURPOSES = {'LOCATION': 'practice location', 'MAILING': 'mailing address'}


def _fetch(params, *, timeout=10.0):
    import httpx
    response = httpx.get(API_URL, params={'version': '2.1', **params}, timeout=timeout)
    response.raise_for_status()
    return response.json()


def _name(basic):
    basic = basic if isinstance(basic, dict) else {}
    if basic.get('organization_name'):
        return str(basic['organization_name'])[:200]
    parts = [basic.get('first_name'), basic.get('last_name')]
    return ' '.join(str(part) for part in parts if part)[:200] or None


def _address(entry):
    parts = [entry.get('address_1'), entry.get('city'), entry.get('state')]
    return ', '.join(str(part).strip() for part in parts if part)[:300] or None


def suggestions_from(document, *, read_on=None):
    """Every toll-free fax number in an NPPES API response, one suggestion per address."""
    read_on = read_on or datetime.utcnow()
    results = document.get('results') if isinstance(document, dict) else None
    found = []
    for result in results if isinstance(results, list) else []:
        if not isinstance(result, dict):
            continue
        npi = str(result.get('number') or '')
        if re.fullmatch(r'[0-9]{10}', npi) is None:
            continue
        name = _name(result.get('basic'))
        for entry in result.get('addresses') or []:
            if not isinstance(entry, dict) or not entry.get('fax_number'):
                continue
            try:
                fax = normalize_number(str(entry['fax_number']), country='US')
            except InvalidNumber:
                continue
            if not is_toll_free(fax):
                continue
            purpose = PURPOSES.get(str(entry.get('address_purpose') or '').upper(), 'address')
            found.append({
                'source': 'NPPES', 'read_at': read_on,
                'npi': npi, 'name': name, 'address_purpose': purpose, 'address': _address(entry),
                'fax_number': fax,
                'evidence': (f'NPPES record NPI {npi}, {purpose}, read {read_on:%B} {read_on.day}, {read_on.year}'),
                'source_url': f'{API_URL}?version=2.1&number={npi}'})
    return found


def suggested_tollfree(npi=None, *, name=None, city=None, state=None, fetch=None, now=None):
    """Toll-free fax numbers a provider published in NPPES, as suggestions a person may approve.

    Look up by ``npi`` (ten digits), or by organisation ``name`` with ``city``
    or ``state``. Returns a list (empty when nothing toll-free is published).
    Raises ``ValueError`` for an incomplete query; network errors pass to the caller.
    """
    if npi is not None:
        npi = str(npi).strip()
        if re.fullmatch(r'[0-9]{10}', npi) is None:
            raise ValueError('Enter the ten-digit NPI.')
        params = {'number': npi}
    elif name and (city or state):
        params = {'organization_name': str(name).strip()[:100], 'limit': 20}
        if city:
            params['city'] = str(city).strip()[:60]
        if state:
            params['state'] = str(state).strip()[:2].upper()
    else:
        raise ValueError('Enter an NPI, or a name with a city or state.')
    return suggestions_from((fetch or _fetch)(params), read_on=now)
