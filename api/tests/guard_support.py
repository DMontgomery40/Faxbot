"""Test setup for suites about something other than dialing policy: let Faxbot dial a country through the dialing
guard's own change path (routing/guard.py, as an administrator's 'allowed' row), never by weakening the guard.

A first fax to a country this installation has never delivered to, with no rule or saved recipient naming it, is
held at acceptance (N14). Suites that test trunk binding, pricing or delivery with a foreign number allow that
country first, exactly as an administrator would.
"""
from datetime import datetime


def allow_country(engine, *regions, home_country='US'):
    """Record an administrator's 'allowed' for each country (ISO code, such as 'GB')."""
    from api.app.routing import guard
    with engine.begin() as connection:
        for region in regions:
            guard.change_on(connection, guard.country_key(region), state='allowed', actor_name='Test setup',
                            home_country=home_country, now=datetime.utcnow())
