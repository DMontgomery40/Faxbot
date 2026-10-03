"""Read provider-reported charges for finished attempts.

SignalWire's Compatibility API fax resource reports ``price`` (a decimal string,
negative for a charge, or null until priced), ``price_unit`` (ISO 4217) and
``duration`` in seconds. It exposes no separate charge identity, so the fax SID
identifies the charge and a later different price for it is a correction.
Lookups use the account that sent the attempt, read from its captured profile.
"""
from decimal import ROUND_CEILING, Decimal, InvalidOperation
import re

import httpx


_SID = re.compile(r'[A-Za-z0-9_-]{1,100}', re.ASCII)
_HOST = re.compile(r'[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?', re.ASCII)


def parse_signalwire_charge(payload):
    """``(micros, currency, seconds)`` from a fax resource, or None when not yet priced."""
    if not isinstance(payload, dict):
        return None
    price, unit, duration = payload.get('price'), payload.get('price_unit'), payload.get('duration')
    if price is None or not isinstance(unit, str) or re.fullmatch(r'[A-Za-z]{3}', unit) is None:
        return None
    try:
        amount = abs(Decimal(str(price)))
    except (InvalidOperation, ValueError):
        return None
    if not amount.is_finite() or amount > 10_000:
        return None
    micros = int((amount * 1_000_000).to_integral_value(rounding=ROUND_CEILING))
    seconds = duration if type(duration) is int and 0 <= duration <= 86_400 else None
    return micros, unit.upper(), seconds


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
