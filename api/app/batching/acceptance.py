"""Decide at acceptance whether a new fax waits to go with others, and record it in the same transaction.

A fax waits only when its number sends together, its accepted provider is
Faxbot's own SIP trunk, that trunk is the route this number's faxes take and
sending together saves money there, its page count is known, and it fits one
call with its separator page. Anything unusual means it goes straight away.
"""
from dataclasses import replace

import sqlalchemy as sa

from . import policy
from .store import BatchingSettings, HoldPlan, hold_on, tables


def hold_plan(engine, revision, profile, *, destination, pages, actor, send_now=False):
    """The ``HoldPlan`` for a fax about to be accepted, or None when it goes straight away."""
    from ..routing.store import RouteStore
    configuration = profile.configuration
    if (revision.values.fax_disabled or configuration.manifest is not None or configuration.provider_id != 'sip'
            or type(pages) is not int or pages < 1):
        return None
    from ..routing.sender_pins import pinned
    if pinned(engine, destination):
        return None  # a registered-sender recipient gets each fax in its own call (sender_pins, N17)
    setting = BatchingSettings(engine).get(destination)
    if not setting['enabled'] or pages + 1 > setting['max_pages']:
        return None
    verdict = policy.evaluate(RouteStore(engine), destination, revision.values, 'sip', pages=pages)
    if not verdict.saves:
        return None
    scope = getattr(actor, 'replay_scope', None)
    if not isinstance(scope, str) or not scope:
        return None
    return HoldPlan(destination, scope, None, pages, bool(send_now), setting['max_wait_seconds'])


def _sender_name_on(connection, actor):
    principals = sa.table('access_principals', sa.column('id'), sa.column('display_name'))
    name = connection.execute(sa.select(principals.c.display_name).where(
        principals.c.id == getattr(actor, 'principal_id', None))).scalar()
    return name if isinstance(name, str) and name.strip() else None


def recorder(engine, job_id, plan, actor):
    """The acceptance-transaction step that records ``plan`` for ``job_id``."""
    def record(connection, now):
        t = tables(engine, connection)
        hold_on(connection, t, job_id, replace(plan, sender_name=_sender_name_on(connection, actor)), now)
    return record
