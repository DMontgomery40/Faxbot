"""Read a UPS through Network UPS Tools (NUT), the way ``upsc`` does (brief 92, RF).

NUT's network protocol is line-based text over TCP, port 3493 by default (networkupstools.org, "Network protocol
information", developer guide). Faxbot only reads, with no login:

- ``LIST UPS`` answers ``BEGIN LIST UPS``, one ``UPS <name> "<description>"`` line per UPS, ``END LIST UPS``;
- ``GET VAR <ups> <variable>`` answers ``VAR <ups> <variable> "<value>"``;
- any refusal is ``ERR <code>``, such as ``UNKNOWN-UPS``, ``VAR-NOT-SUPPORTED``, ``ACCESS-DENIED``,
  ``DATA-STALE`` or ``DRIVER-NOT-CONNECTED``;
- ``LOGOUT`` ends the session (``OK Goodbye``).

Quoted values escape ``"`` and ``\\`` with a backslash. The variables read are ``ups.status`` (space-separated
flags: ``OL`` on mains, ``OB`` on battery, ``LB`` battery low, ``CHRG`` charging, ...), ``battery.runtime``
(seconds left, as the UPS estimates it) and ``battery.charge`` (percent). A UPS that does not report a
variable leaves it unknown; nothing is ever guessed. Not yet run against a real UPS: tested with a fake NUT
server that answers as ``upsd`` does.
"""
from __future__ import annotations

from dataclasses import dataclass
import socket


PORT = 3493
TIMEOUT = 3.0
ERRORS = {
    'UNKNOWN-UPS': 'The UPS server does not know a UPS by that name.',
    'ACCESS-DENIED': 'The UPS server does not let this computer read it. Allow Faxbot\'s address in upsd.conf or '
                     'its firewall.',
    'DATA-STALE': 'The UPS server has no fresh reading from the UPS; check the UPS cable and its driver.',
    'DRIVER-NOT-CONNECTED': 'The UPS server\'s driver is not connected to the UPS; check the UPS cable and its '
                            'driver.',
}


class NutError(Exception):
    """The UPS could not be read; one plain sentence."""


@dataclass(frozen=True)
class Reading:
    ups: str
    flags: frozenset
    runtime_seconds: int | None
    charge_percent: float | None

    @property
    def on_battery(self) -> bool:
        return 'OB' in self.flags

    @property
    def low_battery(self) -> bool:
        return 'LB' in self.flags


def unquote(text: str) -> str:
    """The value inside NUT's double quotes, with its backslash escapes undone."""
    text = text.strip()
    if not (len(text) >= 2 and text[0] == '"' and text[-1] == '"'):
        raise NutError('The UPS server answered in a way Faxbot does not read.')
    out, escaped = [], False
    for char in text[1:-1]:
        if escaped:
            out.append(char)
            escaped = False
        elif char == '\\':
            escaped = True
        else:
            out.append(char)
    return ''.join(out)


class Session:
    """One TCP session with ``upsd``."""

    def __init__(self, host, port=PORT, timeout=TIMEOUT):
        try:
            self.socket = socket.create_connection((host, int(port)), timeout=timeout)
        except OSError:
            raise NutError(f'Faxbot could not connect to the UPS server at {host} port {port}.') from None
        self.socket.settimeout(timeout)
        self.buffer = b''

    def _line(self) -> str:
        while b'\n' not in self.buffer:
            try:
                chunk = self.socket.recv(4096)
            except OSError:
                raise NutError('The UPS server stopped answering.') from None
            if not chunk:
                raise NutError('The UPS server closed the connection.')
            self.buffer += chunk
            if len(self.buffer) > 65536:
                raise NutError('The UPS server answered in a way Faxbot does not read.')
        line, self.buffer = self.buffer.split(b'\n', 1)
        return line.decode('utf-8', 'replace').rstrip('\r')

    def ask(self, command: str) -> str:
        try:
            self.socket.sendall((command + '\n').encode('ascii'))
        except OSError:
            raise NutError('The UPS server stopped answering.') from None
        line = self._line()
        if line.startswith('ERR '):
            code = line[4:].split(' ', 1)[0]
            raise _Refused(code)
        return line

    def lines_until(self, end: str) -> list:
        found = []
        while True:
            line = self._line()
            if line == end:
                return found
            found.append(line)

    def close(self):
        try:
            self.socket.sendall(b'LOGOUT\n')
        except OSError:
            pass
        try:
            self.socket.close()
        except OSError:
            pass


class _Refused(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _refusal(code):
    return NutError(ERRORS.get(code, f'The UPS server refused the request ({code}).'))


def list_ups(session) -> list:
    """The UPS names the server offers."""
    try:
        first = session.ask('LIST UPS')
    except _Refused as refused:
        raise _refusal(refused.code) from None
    if first != 'BEGIN LIST UPS':
        raise NutError('The UPS server answered in a way Faxbot does not read.')
    names = []
    for line in session.lines_until('END LIST UPS'):
        parts = line.split(' ', 2)
        if len(parts) >= 2 and parts[0] == 'UPS':
            names.append(parts[1])
    return names


def get_var(session, ups, variable):
    """The variable's value, or None when this UPS does not report it."""
    try:
        line = session.ask(f'GET VAR {ups} {variable}')
    except _Refused as refused:
        if refused.code == 'VAR-NOT-SUPPORTED':
            return None
        raise _refusal(refused.code) from None
    prefix = f'VAR {ups} {variable} '
    if not line.startswith(prefix):
        raise NutError('The UPS server answered in a way Faxbot does not read.')
    return unquote(line[len(prefix):])


def read(host, *, port=PORT, ups=None, timeout=TIMEOUT) -> Reading:
    """One reading of the UPS (the first the server lists when ``ups`` is not given). Raises ``NutError``."""
    if not host:
        raise NutError('No UPS server is set.')
    session = Session(host, port, timeout)
    try:
        if not ups:
            names = list_ups(session)
            if not names:
                raise NutError('The UPS server lists no UPS.')
            ups = names[0]
        status = get_var(session, ups, 'ups.status')
        if status is None:
            raise NutError('The UPS does not report whether it is on mains or battery.')
        runtime = get_var(session, ups, 'battery.runtime')
        charge = get_var(session, ups, 'battery.charge')
    finally:
        session.close()
    try:
        runtime_seconds = int(float(runtime)) if runtime not in (None, '') else None
    except ValueError:
        runtime_seconds = None
    try:
        charge_percent = float(charge) if charge not in (None, '') else None
    except ValueError:
        charge_percent = None
    return Reading(ups, frozenset(status.split()), runtime_seconds, charge_percent)
