"""Logging a caught exception's cause without the values it carried.

A SQLAlchemy error prints its bound parameters, and the database's own message
can repeat a value (PostgreSQL's "DETAIL: Key (token)=(...)"). On the access
tables those values are password hashes and session or key tokens; on the
delivery tables they are fax numbers. ``hide_sql_parameters`` keeps the SQL
parameters out of a traceback logged with ``exc_info``; ``cause_text`` and
``frames_text`` describe an exception with no message text at all, for code
that must never log a value.
"""
import traceback

import sqlalchemy as sa


def _chain(error, limit=6):
    """The exception, then what caused it (``raise ... from``, the exception being handled, or a SQLAlchemy
    error's DBAPI original), each once."""
    seen = set()
    while error is not None and id(error) not in seen and len(seen) < limit:
        seen.add(id(error))
        yield error
        following = error.__cause__ or error.__context__
        if following is None and isinstance(error, sa.exc.StatementError):
            following = getattr(error, 'orig', None)
        error = following if isinstance(following, BaseException) else None


def hide_sql_parameters(error):
    """Make every SQLAlchemy error in ``error``'s chain print its statement without its parameters."""
    for item in _chain(error):
        if isinstance(item, sa.exc.StatementError):
            item.hide_parameters = True
    return error


def _code(error):
    """The database's error name or code (SQLITE_BUSY, 40P01), which names the cause and holds no value."""
    for name in ('sqlite_errorname', 'sqlstate', 'pgcode'):
        value = getattr(error, name, None)
        if isinstance(value, str) and value:
            return value
    return None


def cause_text(error):
    """'sqlalchemy.exc.OperationalError <- sqlite3.OperationalError (SQLITE_BUSY); statement: UPDATE ... ?':
    the class of each exception in the chain, the database's error code and the SQL statement with its
    placeholders. No exception message is included, so no parameter, token or hash can appear."""
    parts, statement = [], None
    for item in _chain(error):
        kind = type(item)
        name = kind.__qualname__ if kind.__module__ == 'builtins' else f'{kind.__module__}.{kind.__qualname__}'
        code = _code(item)
        parts.append(f'{name} ({code})' if code else name)
        if statement is None and isinstance(item, sa.exc.StatementError) and isinstance(item.statement, str):
            statement = ' '.join(item.statement.split())[:500]
    text = ' <- '.join(parts)
    return f'{text}; statement: {statement}' if statement else text


def frames_text(error):
    """The traceback's frames (file, line, function and source line), without any exception message."""
    return ''.join(traceback.format_list(traceback.extract_tb(error.__traceback__))).rstrip()
