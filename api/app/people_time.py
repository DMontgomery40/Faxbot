"""Times the server writes for people, in the installation's own time zone.

Stored times are naive UTC. The installation's time zone is the configuration
value ``time_zone`` (an IANA name such as America/Denver); empty means UTC.
"""
from datetime import timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def zone(name):
    """The time zone for an IANA name; UTC for an empty or unknown one."""
    if not name:
        return timezone.utc
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return timezone.utc


def installation_zone_name():
    """The active installation's time zone name, from the bound configuration."""
    from .config import configuration_values
    try:
        return getattr(configuration_values(), 'time_zone', '') or ''
    except Exception:
        return ''


def _local(moment, name):
    aware = moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment
    return aware.astimezone(zone(name))


def clock(moment, name=None):
    """2:52 PM MDT"""
    local = _local(moment, installation_zone_name() if name is None else name)
    hour = local.hour % 12 or 12
    return f"{hour}:{local:%M} {'AM' if local.hour < 12 else 'PM'} {local.tzname()}"


def date_and_time(moment, name=None):
    """4 October 2026 at 2:52 PM MDT"""
    name = installation_zone_name() if name is None else name
    local = _local(moment, name)
    return f'{local.day} {local:%B %Y} at {clock(moment, name)}'


def short(moment, name=None):
    """4 Oct 2:52 PM MDT: for sentences and files that travel."""
    if moment is None:
        return ''
    name = installation_zone_name() if name is None else name
    local = _local(moment, name)
    return f'{local.day} {local:%b} {clock(moment, name)}'
