"""Try a fax-result write again when the database was busy, a few times, saying so each time.

SQLite lets one writer at a time hold its write lock. A write that waits longer than the lock timeout
(``schema.LOCK_TIMEOUT_SECONDS``) is refused with "database is locked"; PostgreSQL's ``lock_timeout`` refuses with
"lock not available". The store rolls such a write back before it reports the refusal, so nothing was kept and
the same write may run again. A commit whose answer was lost (``ConfigurationCommitUncertain``) is different: it
may have been kept, so it runs again only when the caller says the write is idempotent (an event key makes a
repeat do nothing).
"""
import logging
import sqlite3
import time


TRIES = 3
# Seconds between tries. Each try itself waits up to the lock timeout (10 s), so three tries span about 30 s.
WAITS = (1.0, 4.0)
# PostgreSQL's SQLSTATE for lock_timeout (lock_not_available).
_LOCK_NOT_AVAILABLE = '55P03'
_sleep = time.sleep


def busy_cause(error):
    """The database's own words when ``error``, or an error it was raised from, is a lock timeout; else None."""
    current, depth = error, 0
    while current is not None and depth < 12:
        for candidate in (current, getattr(current, 'orig', None)):
            if isinstance(candidate, sqlite3.OperationalError):
                text = str(candidate)
                if 'locked' in text.lower() or 'busy' in text.lower():
                    return text
            code = getattr(candidate, 'pgcode', None) or getattr(candidate, 'sqlstate', None)
            if code == _LOCK_NOT_AVAILABLE:
                return 'lock not available'
        current = current.__cause__ or current.__context__
        depth += 1
    return None


def retry_when_busy(write, *, what, idempotent=False, tries=TRIES, waits=WAITS, sleep=None, log=None):
    """Run ``write()``; when a lock timeout refused it, wait and run it again, up to ``tries`` runs in all.

    ``what`` names the write for the log ("the result of fax 1234 (attempt 5678)"). Each new try is logged at
    WARNING with its cause, and a write the database still refused after the last try at ERROR, then that error is
    raised. Any other error is raised at once, unchanged.
    """
    from .config_store import ConfigurationCommitUncertain
    log = log or logging.getLogger(__name__)
    sleep = sleep or _sleep
    for attempt in range(1, tries + 1):
        try:
            return write()
        except Exception as error:
            cause = busy_cause(error)
            if cause is None or (isinstance(error, ConfigurationCommitUncertain) and not idempotent):
                raise
            if attempt >= tries:
                log.error('Faxbot could not save %s: the database stayed busy through %d tries (%s).',
                          what, tries, cause)
                raise
            wait = waits[min(attempt - 1, len(waits) - 1)]
            log.warning('Saving %s waited too long for the database (%s); trying again in %g seconds (try %d of %d).',
                        what, cause, wait, attempt + 1, tries)
            sleep(wait)
