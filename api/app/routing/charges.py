"""Provider-reported charges, stored beside Faxbot's own computed cost.

SignalWire's Compatibility API fax resource reports ``price`` (a decimal string,
negative for charges), ``price_unit`` (ISO 4217) and ``duration`` in seconds.
Lookups use the account that sent the fax, read from its captured profile.
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
    def __init__(self, configuration, *, timeout=10.0, client_factory=None):
        self.configuration = configuration
        self.timeout = timeout
        self.client_factory = client_factory or (lambda: httpx.Client(timeout=self.timeout))

    def __call__(self, target):
        if target.provider_id != 'signalwire' or not target.provider_sid or not _SID.fullmatch(target.provider_sid):
            return None
        _, profile = self.configuration.outbound_context(target.job_id)
        configuration = profile.configuration
        if configuration.provider_id != 'signalwire' or configuration.manifest is not None:
            return None
        space = str(configuration.settings.get('space_url', '')).strip().rstrip('/')
        project = str(configuration.settings.get('project_id', '')).strip()
        token = str(configuration.credentials.get('api_token', '')).strip()
        if not (_HOST.fullmatch(space) and _SID.fullmatch(project) and token):
            return None
        url = f'https://{space}/api/laml/2010-04-01/Accounts/{project}/Faxes/{target.provider_sid}.json'
        with self.client_factory() as client:
            response = client.get(url, auth=(project, token))
        if response.status_code != 200:
            return None
        try:
            return parse_signalwire_charge(response.json())
        except ValueError:
            return None
