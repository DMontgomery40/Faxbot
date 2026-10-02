"""Bounded UTF-8 configuration assignment data, never shell source.

Each assignment occupies one LF/CRLF physical line. Single quotes contain
literal text; double quotes use JSON string escapes. Embedded physical newlines
in quoted values are rejected, while canonical output represents multiline
values with escaped newlines. Dollar expressions and backticks stay literal;
even generated output must never be executed or passed to shell ``source``.

The caller owns the allowed configuration keys and parent-directory policy.
Reads require regular files and refuse final symlinks. Writes refuse final
symlinks and publish private POSIX files by same-directory atomic replacement.
These checks do not claim defense against hostile concurrent filesystem edits.
"""

from collections.abc import Collection, Mapping
import errno
import json
import os
from pathlib import Path
import re
import stat
import tempfile


_ASSIGNMENT = re.compile(r"(?:export[ \t]+)?([A-Za-z_][A-Za-z0-9_]*)[ \t]*=(.*)")
_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_MAX_BYTES = 1024 * 1024
_SIMPLE_VALUE = re.compile(r"[A-Za-z0-9_./:@%+,\-]+")
_UNSUPPORTED_DIRECTORY_SYNC = {errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOSYS}


class ConfigurationFileError(ValueError):
    """A sanitized artifact failure, with publication state for reconciliation."""

    def __init__(
        self,
        message: str,
        *,
        line: int | None = None,
        key: str | None = None,
        published: bool = False,
    ) -> None:
        self.line = line
        self.key = key
        self.published = published
        super().__init__(message)


def _utf8(text: str, *, line: int | None = None, key: str | None = None) -> bytes:
    if not isinstance(text, str):
        raise ConfigurationFileError(
            "Configuration must contain UTF-8 text.", line=line, key=key
        )
    try:
        encoded = text.encode("utf-8")
    except UnicodeEncodeError:
        raise ConfigurationFileError(
            "Configuration must contain UTF-8 text.", line=line, key=key
        ) from None
    if len(encoded) > _MAX_BYTES:
        raise ConfigurationFileError(
            "Configuration artifact exceeds the size limit.", line=line, key=key
        )
    return encoded


def _parse_value(value: str, *, line: int, key: str) -> str:
    if "\r" in value:
        raise ConfigurationFileError(
            "Invalid physical configuration line.", line=line, key=key
        )
    unquoted = value
    value = value.lstrip(" \t")
    if value.startswith("'"):
        closing = value.find("'", 1)
        if closing == -1:
            raise ConfigurationFileError(
                "Invalid quoted configuration value.", line=line, key=key
            )
        parsed, trailing = value[1:closing], value[closing + 1 :]
    elif value.startswith('"'):
        try:
            parsed, end = json.JSONDecoder().raw_decode(value)
        except json.JSONDecodeError:
            raise ConfigurationFileError(
                "Invalid quoted configuration value.", line=line, key=key
            ) from None
        trailing = value[end:]
    else:
        return re.split(r"[ \t]+#", unquoted, maxsplit=1)[0].strip()
    trailing = trailing.strip()
    if trailing and not trailing.startswith("#"):
        raise ConfigurationFileError(
            "Invalid quoted configuration value.", line=line, key=key
        )
    return parsed


def parse_environment(text: str, *, allowed_keys: Collection[str]) -> dict[str, str]:
    """Parse literal assignments without shell expansion."""
    _utf8(text)
    values: dict[str, str] = {}
    for line_number, line in enumerate(text.split("\n"), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        assignment = _ASSIGNMENT.fullmatch(line)
        if assignment is None:
            raise ConfigurationFileError(
                "Invalid configuration assignment.", line=line_number
            )
        key, value = assignment.groups()
        if key not in allowed_keys:
            raise ConfigurationFileError(
                "Unsupported configuration key.", line=line_number
            )
        if key in values:
            raise ConfigurationFileError(
                "Duplicate configuration key.", line=line_number, key=key
            )
        parsed = _parse_value(value, line=line_number, key=key)
        _utf8(parsed, line=line_number, key=key)
        values[key] = parsed
    return values


def _validated_values(values: Mapping[str, str]) -> dict[str, str]:
    if not isinstance(values, Mapping):
        raise ConfigurationFileError("Configuration values must be a mapping.")
    validated: dict[str, str] = {}
    for key, value in values.items():
        if not isinstance(key, str) or _KEY.fullmatch(key) is None:
            raise ConfigurationFileError("Invalid configuration key.")
        _utf8(value, key=key)
        validated[key] = value
    return validated


def format_environment(values: Mapping[str, str]) -> str:
    """Return sorted, newline-terminated literal assignment data."""
    lines = []
    for key, value in sorted(_validated_values(values).items()):
        literal = (
            value if _SIMPLE_VALUE.fullmatch(value)
            else json.dumps(value, ensure_ascii=False)
        )
        lines.append(f"{key}={literal}")
    text = "\n".join(lines) + "\n"
    _utf8(text)
    return text


def read_environment(
    path: str | Path,
    *,
    allowed_keys: Collection[str],
    missing_ok: bool = True,
) -> dict[str, str]:
    """Read a regular artifact, allowing only a truly absent optional file."""
    try:
        source_stat = Path(path).lstat()
    except FileNotFoundError:
        if missing_ok:
            return {}
        raise ConfigurationFileError("Configuration artifact is missing.") from None
    except (OSError, ValueError):
        raise ConfigurationFileError("Cannot read configuration artifact.") from None
    if not stat.S_ISREG(source_stat.st_mode):
        raise ConfigurationFileError("Configuration artifact must be a regular file.")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except (OSError, ValueError):
        raise ConfigurationFileError("Cannot read configuration artifact.") from None
    try:
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ConfigurationFileError("Configuration artifact must be a regular file.")
            payload = bytearray()
            while len(payload) <= _MAX_BYTES:
                piece = os.read(descriptor, _MAX_BYTES + 1 - len(payload))
                if not piece:
                    break
                payload.extend(piece)
        finally:
            os.close(descriptor)
    except ConfigurationFileError:
        raise
    except OSError:
        raise ConfigurationFileError("Cannot read configuration artifact.") from None
    if len(payload) > _MAX_BYTES:
        raise ConfigurationFileError("Configuration artifact exceeds the size limit.")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        raise ConfigurationFileError("Configuration artifact is not valid UTF-8.") from None
    return parse_environment(text, allowed_keys=allowed_keys)


def _sync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        try:
            os.fsync(descriptor)
        except OSError as error:
            if error.errno not in _UNSUPPORTED_DIRECTORY_SYNC:
                raise
    finally:
        os.close(descriptor)


def write_environment(
    path: str | Path,
    values: Mapping[str, str],
    *,
    allowed_keys: Collection[str],
) -> None:
    """Publish complete literal configuration data in a private atomic file."""
    validated = _validated_values(values)
    if any(key not in allowed_keys for key in validated):
        raise ConfigurationFileError("Unsupported configuration key.")
    payload = _utf8(format_environment(validated))
    destination = Path(path)
    temporary: str | None = None
    published = False
    try:
        try:
            destination_stat = destination.lstat()
        except FileNotFoundError:
            destination_stat = None
        if destination_stat is not None and stat.S_ISLNK(destination_stat.st_mode):
            raise ConfigurationFileError(
                "Configuration destination must not be a symbolic link."
            )
        descriptor, temporary = tempfile.mkstemp(
            prefix=".faxbot-config-", dir=destination.parent
        )
        with os.fdopen(descriptor, "wb", buffering=0) as target:
            os.fchmod(descriptor, 0o600)
            remaining = memoryview(payload)
            while remaining:
                written = os.write(descriptor, remaining)
                if written == 0:
                    raise OSError("Configuration write made no progress.")
                remaining = remaining[written:]
            target.flush()
            os.fsync(descriptor)
        os.replace(temporary, destination)
        published = True
        temporary = None
        _sync_directory(destination.parent)
    except ConfigurationFileError:
        raise
    except (OSError, ValueError):
        message = (
            "Configuration artifact was replaced; durability could not be confirmed."
            if published else "Cannot publish configuration artifact."
        )
        raise ConfigurationFileError(message, published=published) from None
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                # Retain the primary failure; a private orphan cannot be presented as success.
                pass
