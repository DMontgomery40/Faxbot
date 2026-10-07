"""Read provider-reported charges for finished attempts.

SignalWire's Compatibility API fax resource reports ``price`` (a decimal string,
negative for a charge, or null until priced), ``price_unit`` (ISO 4217) and
``duration`` in seconds. It exposes no separate charge identity, so the fax SID
identifies the charge and a later different price for it is a correction.
Lookups use the account that sent the attempt, read from its captured profile.

Sinch's Fax API (v3) fax resource, ``GET /v3/projects/{projectId}/faxes/{id}``,
reports ``price`` as Money: ``amount`` (a decimal string with four decimals) and
``currencyCode`` (ISO 4217), "populated after the final fax price is calculated"
(Fax API v3 reference, read 2026-10-07). Phaxio's ``GET /v2.1/faxes/{id}``
reports ``cost``, "the total cost of the fax in cents", which "may change
depending on the success of the job" (Phaxio Fax Object, read 2026-10-07):
Phaxio charges before it dials and credits a failure back. Phaxio names no
currency; its prices are published in US dollars (phaxio.com/pricing, read
2026-10-07), so its cents are read as US cents. For both, a price counts only
once the provider's own status is final: a placeholder while the fax is still
going would make the route look free. Unknown stays unknown; a later different
price is a correction, exactly as for SignalWire.
"""
from decimal import ROUND_CEILING, Decimal, InvalidOperation
import re

import httpx


_SID = re.compile(r'[A-Za-z0-9_-]{1,100}', re.ASCII)
_HOST = re.compile(r'[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?', re.ASCII)
_PHAXIO_ID = re.compile(r'[0-9]{1,20}', re.ASCII)
SINCH_FINAL = ('COMPLETED', 'FAILURE')
PHAXIO_FINAL = ('success', 'failure')


def _micros(price):
    """A non-negative decimal amount as micro-units, rounded up; None when it is not a usable amount."""
    if isinstance(price, bool) or not isinstance(price, (str, int, float)):
        return None
    try:
        amount = abs(Decimal(str(price).strip()))
    except (InvalidOperation, ValueError):
        return None
    if not amount.is_finite() or amount > 10_000:
        return None
    return int((amount * 1_000_000).to_integral_value(rounding=ROUND_CEILING))


def parse_signalwire_charge(payload):
    """``(micros, currency, seconds)`` from a fax resource, or None when not yet priced."""
    if not isinstance(payload, dict):
        return None
    price, unit, duration = payload.get('price'), payload.get('price_unit'), payload.get('duration')
    if price is None or not isinstance(unit, str) or re.fullmatch(r'[A-Za-z]{3}', unit) is None:
        return None
    micros = _micros(price)
    if micros is None:
        return None
    seconds = duration if type(duration) is int and 0 <= duration <= 86_400 else None
    return micros, unit.upper(), seconds


def parse_sinch_charge(payload):
    """``(micros, currency, None)`` from a Sinch fax, or None while it is unpriced or still going."""
    if not isinstance(payload, dict):
        return None
    if str(payload.get('status') or '').upper() not in SINCH_FINAL:
        return None
    price = payload.get('price')
    if not isinstance(price, dict):
        return None
    amount, unit = price.get('amount'), price.get('currencyCode')
    if not isinstance(amount, str) or not isinstance(unit, str) or re.fullmatch(r'[A-Za-z]{3}', unit) is None:
        return None
    micros = _micros(amount)
    return None if micros is None else (micros, unit.upper(), None)


def parse_phaxio_charge(payload):
    """``(micros, 'USD', None)`` from a Phaxio fax object, or None while it is unpriced or still going."""
    if not isinstance(payload, dict) or payload.get('status') not in PHAXIO_FINAL:
        return None
    cents = payload.get('cost')
    if type(cents) is not int or not 0 <= cents <= 1_000_000:
        return None
    return cents * 10_000, 'USD', None


class SignalWireCharges:
    def __init__(self, delivery, *, timeout=10.0, client_factory=None):
        self.delivery = delivery
        self.timeout = timeout
        self.client_factory = client_factory or (lambda: httpx.Client(timeout=self.timeout))

    def __call__(self, row):
        sid = row.get('provider_sid')
        if not isinstance(sid, str) or not _SID.fullmatch(sid):
            return []
        _, profile = self.delivery.attempt_context(row['job_id'], row['id'])
        configuration = profile.configuration
        if configuration.provider_id != 'signalwire' or configuration.manifest is not None:
            return []
        space = str(configuration.settings.get('space_url', '')).strip().rstrip('/')
        project = str(configuration.settings.get('project_id', '')).strip()
        token = str(configuration.credentials.get('api_token', '')).strip()
        if not (_HOST.fullmatch(space) and _SID.fullmatch(project) and token):
            return []
        url = f'https://{space}/api/laml/2010-04-01/Accounts/{project}/Faxes/{sid}.json'
        with self.client_factory() as client:
            response = client.get(url, auth=(project, token))
        if response.status_code != 200:
            raise RuntimeError('Provider charge lookup failed.')
        try:
            parsed = parse_signalwire_charge(response.json())
        except ValueError:
            raise RuntimeError('Provider charge lookup returned an unusable response.') from None
        if parsed is None:
            return []  # Not priced yet; unknown stays unknown.
        micros, currency, seconds = parsed
        return [{'charge_id': sid, 'amount_micros': micros, 'currency': currency, 'billed_seconds': seconds}]


class _ByIdCharges:
    """Ask the account that sent an attempt what its fax cost, by the provider's fax ID."""

    provider_id = None

    def __init__(self, delivery, *, timeout=10.0, client_factory=None):
        self.delivery = delivery
        self.timeout = timeout
        self.client_factory = client_factory or (lambda: httpx.Client(timeout=self.timeout, follow_redirects=False))

    def _configuration(self, row):
        """The captured account that sent this attempt, or None when it is not this provider's own adapter."""
        _, profile = self.delivery.attempt_context(row['job_id'], row['id'])
        configuration = profile.configuration
        if configuration.provider_id != self.provider_id or configuration.manifest is not None:
            return None
        return configuration

    def _read(self, url, auth):
        with self.client_factory() as client:
            response = client.get(url, auth=auth, headers={'Accept': 'application/json'})
        if response.status_code != 200:
            raise RuntimeError('Provider charge lookup failed.')
        try:
            payload = response.json()
        except ValueError:
            raise RuntimeError('Provider charge lookup returned an unusable response.') from None
        if not isinstance(payload, dict):
            raise RuntimeError('Provider charge lookup returned an unusable response.')
        return payload

    @staticmethod
    def _charge(sid, parsed):
        if parsed is None:
            return []  # Not priced yet, or the fax is still going: unknown stays unknown.
        micros, currency, seconds = parsed
        return [{'charge_id': sid, 'amount_micros': micros, 'currency': currency, 'billed_seconds': seconds}]


class SinchCharges(_ByIdCharges):
    provider_id = 'sinch'

    def __call__(self, row):
        from ..inbound.fetch import FetchError, SINCH_HOSTS, require_host
        from ..sinch_service import SinchFaxService
        sid = row.get('provider_sid')
        if not isinstance(sid, str) or not _SID.fullmatch(sid):
            return []
        configuration = self._configuration(row)
        if configuration is None:
            return []
        project = str(configuration.settings.get('project_id') or '').strip()
        key = str(configuration.credentials.get('api_key') or '').strip()
        secret = str(configuration.credentials.get('api_secret') or '').strip()
        base = str(configuration.settings.get('base_url') or SinchFaxService.DEFAULT_BASES[0]).strip().rstrip('/')
        if not (re.fullmatch(r'[A-Za-z0-9-]{1,64}', project, re.ASCII) and key and secret):
            return []
        url = f'{base}/projects/{project}/faxes/{sid}'
        try:
            require_host(url, SINCH_HOSTS, 'Sinch')
        except FetchError:
            return []  # Only a documented Sinch Fax API host is ever asked.
        payload = self._read(url, (key, secret))
        fax = payload.get('data', payload)
        if not isinstance(fax, dict) or fax.get('id') != sid:
            raise RuntimeError('Provider charge lookup returned an unusable response.')
        return self._charge(sid, parse_sinch_charge(fax))


class PhaxioCharges(_ByIdCharges):
    provider_id = 'phaxio'
    URL = 'https://api.phaxio.com/v2.1/faxes/{id}'

    def __call__(self, row):
        sid = row.get('provider_sid')
        if not isinstance(sid, str) or not _PHAXIO_ID.fullmatch(sid):
            return []
        configuration = self._configuration(row)
        if configuration is None:
            return []
        key = str(configuration.credentials.get('api_key') or '').strip()
        secret = str(configuration.credentials.get('api_secret') or '').strip()
        if not (key and secret):
            return []
        payload = self._read(self.URL.format(id=sid), (key, secret))
        fax = payload.get('data') if payload.get('success') is True else None
        if not isinstance(fax, dict) or str(fax.get('id')) != sid:
            raise RuntimeError('Provider charge lookup returned an unusable response.')
        return self._charge(sid, parse_phaxio_charge(fax))
