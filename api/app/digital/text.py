"""Keys and plain sentences for digital routes; the route key grammar is ``rules.model.DIGITAL_KEY``.

A recipient's confirmed Direct address is the route ``dsm:<id>`` and a FHIR
endpoint ``fhir:<id>``, where the id is its ``digital_addresses`` row. The
delivery ledger, whose grammar has no colon, records them as ``dsm.<id>`` and
``fhir.<id>``. ``digital`` names all of them in a rule.
"""
import re

import sqlalchemy as sa


PREFIX = {'direct': 'dsm', 'fhir': 'fhir'}
KIND_OF = {'dsm': 'direct', 'fhir': 'fhir'}
KEY = re.compile(r'(dsm|fhir)[:.]([a-f0-9]{32})')
GROUP = 'digital'
KIND_LABELS = {'direct': 'Direct message', 'fhir': 'FHIR'}


def route_key(kind, address_id):
    return f'{PREFIX[kind]}:{address_id}'


def parse_key(key):
    """(kind, address id) of ``dsm:<id>``/``fhir:<id>`` or their ledger forms; None for anything else."""
    found = KEY.fullmatch(key) if isinstance(key, str) else None
    return (KIND_OF[found.group(1)], found.group(2)) if found else None


def is_route(key):
    return parse_key(key) is not None


def ledger_key(key):
    """``dsm:<id>`` -> ``dsm.<id>``; any other key unchanged."""
    parsed = parse_key(key)
    return f'{PREFIX[parsed[0]]}.{parsed[1]}' if parsed else key


def address_label(kind, address, organization=None):
    """'Direct message to records@direct.example.net' or 'FHIR to Example Health'."""
    if kind == 'direct':
        return f'Direct message to {address}'
    return f'FHIR to {organization}' if organization else f'FHIR to {address}'


def route_label(key, engine=None):
    """A route's name for a sentence: the address it goes to when Faxbot can read it."""
    if key == GROUP:
        return 'Direct messages and FHIR'
    parsed = parse_key(key)
    if parsed is None:
        return key
    kind, address_id = parsed
    if engine is None:
        from ..db import engine
    table = sa.table('digital_addresses', sa.column('id'), sa.column('address'), sa.column('organization'))
    try:
        with engine.connect() as connection:
            row = connection.execute(sa.select(table.c.address, table.c.organization).where(
                table.c.id == address_id)).first()
    except sa.exc.SQLAlchemyError:
        row = None
    if row is None:
        return KIND_LABELS[kind]
    return address_label(kind, row[0], row[1])


# Outcomes, as Sent and Received show them -------------------------------------------------------------------------

STATE_TEXT = {
    'sending': 'Faxbot is handing it over.',
    'submitted': 'Your HISP accepted it; Faxbot is waiting for the recipient to confirm.',
    'processed': "The recipient's HISP accepted it; Faxbot is waiting for confirmation that it was delivered.",
    'dispatched': "Delivered: the recipient's system confirmed it.",
    'delivered': "Delivered: the recipient's system stored it.",
    'failed': 'Not delivered.',
    'uncertain': 'Faxbot cannot tell whether it arrived; it waits for you and is never sent again by itself.',
    'refused': 'Not sent; nothing left Faxbot.',
    'filed': 'Received and filed.',
    'not_filed': 'Received, but not filed.',
}
