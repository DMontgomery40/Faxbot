"""Faxbot's SSL Fax engine: HylaFAX+ with IAX lines on Faxbot's own Asterisk.

The engine (``hylafax/`` in the repository, service ``hylafax`` in Compose)
runs HylaFAX+ 7.0.11 and one IAXmodem per fax line. Each line registers with
Faxbot's Asterisk as an IAX peer. A fax goes out like this:

1. Faxbot stores a one-time *call plan* in Asterisk's database under a random
   numeric tag (the number to dial in the carrier's format, the caller ID, the
   fax and attempt, the fax preference header) over the manager connection.
2. Faxbot uploads the fax image to the engine's job port (4559, private
   Compose network only), creates a job that dials the tag, with one dial and
   one try, and asks to be told when it finishes.
3. On submission HylaFAX+ dials the tag over an IAX line. Asterisk's
   ``faxbot-engine-out`` dialplan reads and deletes the plan in one step, so
   the engine can only place calls Faxbot asked for, each once, and dials the
   trunk: through the T.38 gateway when T.38 is on for the trunk, else audio.
4. HylaFAX+ offers SSL Fax on the call. When the far end supports it the pages
   go over TLS between the two fax machines while the call stays up.
5. The engine's notify script posts the job result to
   ``/_internal/hylafax/result`` with the internal secret (see
   ``hylafax_http.py``).

Steps 1 and 2 happen before Faxbot records that it is sending, so a failure
there is a fax that was never sent. Submission (step 3) is the only step after
it; a lost answer there leaves the fax uncertain and it is never sent again.

Faxbot writes the engine's settings (``<data>/hylafax/engine.conf``) and the
IAX peers for Asterisk (``<data>/asterisk/iax.conf``) with the trunk files on
Apply. Secrets are generated once and kept in ``<data>/hylafax/secrets.json``.

The engine parses fax and TLS data from strangers, so it never sees Faxbot's
data folder: in Compose it reads ``<data>/hylafax`` (its own volume, read-only
for it) and writes only ``<data>/hylafax-out`` (its status and received
images, read-only for Faxbot). Its reports carry its own secret, which Faxbot
accepts only on the engine's routes and only for images in its out folder.
"""
from __future__ import annotations

import asyncio
import ftplib
import json
import logging
import os
import re
import secrets
from dataclasses import dataclass, field
from pathlib import Path

ENGINE_FAMILY = 'faxbot-engine'
LINE_PEER = 'faxbot-line{}'
DEFAULT_LINES = 2
MAX_LINES = 8
SUBMIT_USER = 'faxbot'
SUBMIT_PORT = 4559
IAX_PORT = 4569
OUT_CONTEXT = 'faxbot-engine-out'
# Compose service names; the proof network uses the same names as aliases.
ENGINE_HOST = os.environ.get('FAXBOT_ENGINE_HOST', 'hylafax')
ENGINE_API_URL = os.environ.get('FAXBOT_ENGINE_API_URL', 'http://api:8080')
SUBMIT_TIMEOUT_SECONDS = 15.0
# How long the engine may wait for a free line before the job is dropped
# (HylaFAX LASTTIME, DDHHMM): never dialed means a definite failure.
LAST_TIME = '000010'

# One sentence for each reason a fax uses Faxbot's built-in engine (an ordinary
# fax); "fast fax service" is the engine in operator words (Jev 0.64-0.74).
SENDING_TOGETHER = 'Faxes sent together in one call are sent as ordinary faxes.'
NOT_RUNNING = "Faxbot's fast fax service is not running, so this fax was sent as an ordinary fax."
NOT_SET_UP = "Faxbot's fast fax service starts when you select Apply and connect."
LINES_NOT_READY = "Faxbot's fast fax service is still starting, so this fax was sent as an ordinary fax."
ASTERISK_NOT_CURRENT = ("Faxbot's fast fax service is waiting for the phone connection to restart, "
                        'so this fax was sent as an ordinary fax.')
# Engine states for the trunk page and System diagnostics.
STOPPED = "Faxbot's fast fax service is not running, so faxes are sent the ordinary way."
STARTING = "Faxbot's fast fax service is still starting."
WAITING_FOR_RESTART = "Faxbot's fast fax service is waiting for the phone connection to restart."
# What the engine does after a T.38 call that heard no fax machine; each screen adds its own way to
# try T.38 again (the console's button, the command line's command).
ENGINE_AUDIO = 'It sends audio fax because its last T.38 call heard no fax machine.'
# The engine's own sentence for a line that stopped taking calls (hylafax/entrypoint.sh LINE_DOWN).
LINE_DOWN = "Faxbot's fast fax service lost a fax line and is starting again."
# A restart Faxbot asked for (a fax call no line answered, or Restart the fast fax service).
RESTART_REQUESTED = "Faxbot's fast fax service is starting again."
RESTART_ASKED = 'The fast fax service will restart when no fax is being sent or received.'
# Nothing reads a restart request while the engine is not running; it starts afresh on its own.
RESTART_NOT_RUNNING = ("Faxbot's fast fax service is not running, so there is nothing to restart; faxes are "
                       'sent the ordinary way until it starts.')
# How long the trunk page says that a fax call went unanswered by the fast fax service.
MISSED_SHOWN = 24 * 3600

_TAG = re.compile(r'[1-9][0-9]{15}', re.ASCII)
_HEX32 = re.compile(r'[a-f0-9]{32}', re.ASCII)
_SECRET = re.compile(r'[A-Za-z0-9]{24,128}', re.ASCII)


class EngineError(RuntimeError):
    """The engine refused or could not take a job; nothing was dialed."""


def engine_dir(values) -> Path:
    return Path(values.fax_data_dir) / 'hylafax'


def engine_conf_path(values) -> Path:
    return engine_dir(values) / 'engine.conf'


def restart_request_path(values) -> Path:
    """Read by the engine (hylafax/entrypoint.sh): a new request makes it start again once no call is up."""
    return engine_dir(values) / 'engine-restart'


def request_restart(values, *, reason, at=None):
    """Ask the engine to start again ('missed_call': a fax call no free line answered; 'manual'). False when
    the request could not be written."""
    import time
    if reason not in ('missed_call', 'manual'):
        raise ValueError('Unsupported restart reason')
    moment = at if isinstance(at, int) and not isinstance(at, bool) and at > 0 else int(time.time())
    try:
        _write_private(restart_request_path(values),
                       json.dumps({'reason': reason, 'at': moment, 'asked': int(time.time())}) + '\n')
        # Only a reason and two times: readable by the engine whichever user it runs as.
        os.chmod(restart_request_path(values), 0o644)
    except OSError:
        return False
    return True


def restart_request(values):
    """{'reason', 'at', 'asked'} of the newest restart request, or None."""
    try:
        descriptor = os.open(restart_request_path(values), os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        with os.fdopen(descriptor, 'rb') as handle:
            record = json.loads(handle.read(1024).decode('utf-8'))
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or record.get('reason') not in ('missed_call', 'manual'):
        return None
    if not all(isinstance(record.get(name), int) and not isinstance(record.get(name), bool)
               for name in ('at', 'asked')):
        return None
    return {'reason': record['reason'], 'at': record['at'], 'asked': record['asked']}


def missed_sentence(values, status, now=None):
    """One sentence for a fax call the fast fax service did not answer in the last day, or None."""
    import time
    from datetime import datetime, timezone
    from .people_time import clock
    request = restart_request(values)
    now = now or int(time.time())
    if request is None or request['reason'] != 'missed_call' or now - request['at'] > MISSED_SHOWN:
        return None
    when = clock(datetime.fromtimestamp(request['at'], timezone.utc).replace(tzinfo=None),
                 getattr(values, 'time_zone', '') or None)
    restarted = status.started is not None and status.started >= request['asked']
    return (f"Faxbot's fast fax service did not answer the {when} fax call, so that fax was received the "
            f"ordinary way; Faxbot {'restarted' if restarted else 'is restarting'} the fast fax service.")


def out_dir(values) -> Path:
    """The engine's only writable folder Faxbot can see (its status and received images)."""
    return Path(values.fax_data_dir) / 'hylafax-out'


def status_path(values) -> Path:
    """Written by the engine container (hylafax/entrypoint.sh)."""
    return out_dir(values) / 'engine.status'


def received_dir(values) -> Path:
    """Where the engine's hand-over puts received images (G4 TIFF) for Faxbot to read."""
    return out_dir(values) / 'inbound'


def secrets_path(values) -> Path:
    return engine_dir(values) / 'secrets.json'


def iax_path(values) -> Path:
    return Path(values.fax_data_dir) / 'asterisk' / 'iax.conf'


def iax_started_path(values) -> Path:
    """The IAX file exactly as Asterisk loaded it at its last start (asterisk/start.sh)."""
    return Path(values.fax_data_dir) / 'asterisk' / 'iax.conf.started'


def _write_private(target: Path, text: str):
    from .sip_trunk import _write_private as write
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    write(target, text)


def line_count(values) -> int:
    count = getattr(values, 'sip_fax_lines', DEFAULT_LINES)
    return count if isinstance(count, int) and 1 <= count <= MAX_LINES else DEFAULT_LINES


def engine_secrets(values, *, lines=None) -> dict:
    """The engine's generated secrets, created once (mode 0600) and kept across Apply."""
    lines = lines or line_count(values)
    path = secrets_path(values)
    try:
        stored = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        stored = {}
    if not isinstance(stored, dict):
        stored = {}
    changed = False
    password = stored.get('submit_password')
    if not isinstance(password, str) or not _SECRET.fullmatch(password):
        stored['submit_password'] = secrets.token_hex(24)
        changed = True
    line_secrets = stored.get('lines')
    if not isinstance(line_secrets, dict):
        line_secrets = {}
        changed = True
    for number in range(1, lines + 1):
        value = line_secrets.get(str(number))
        if not isinstance(value, str) or not _SECRET.fullmatch(value):
            line_secrets[str(number)] = secrets.token_hex(16)
            changed = True
    stored['lines'] = line_secrets
    # The engine's own secret for its results and received faxes (never Asterisk's).
    report = stored.get('report_secret')
    if not isinstance(report, str) or not _SECRET.fullmatch(report):
        stored['report_secret'] = secrets.token_hex(32)
        changed = True
    if changed:
        _write_private(path, json.dumps(stored, sort_keys=True) + '\n')
    return stored


def report_secret(data_dir) -> str | None:
    """The engine's report secret as Faxbot stored it, or None before the engine was set up."""
    try:
        stored = json.loads((Path(data_dir) / 'hylafax' / 'secrets.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    value = stored.get('report_secret') if isinstance(stored, dict) else None
    return value if isinstance(value, str) and _SECRET.fullmatch(value) else None


def render_iax(values, engine_secret: dict, *, lines=None) -> str:
    """Asterisk's iax.conf: one peer per engine line; calls from it enter ``faxbot-engine-out``.

    The engine's modems use plain IAX2 without call tokens (IAXmodem predates
    them), so each line peer is exempt by name; G.711 only, no jitter buffer.
    """
    lines = lines or line_count(values)
    codecs = _codecs(values)
    out = ['; Written by Faxbot: the SSL Fax engine\'s fax lines. Apply replaces this file.',
           '[general]', f'bindport={IAX_PORT}', 'bindaddr=0.0.0.0', 'disallow=all',
           *(f'allow={codec}' for codec in codecs), 'jitterbuffer=no', 'forcejitterbuffer=no',
           'requirecalltoken=no', 'autokill=yes', 'delayreject=yes', 'trunk=no', '']
    for number in range(1, lines + 1):
        out += [f'[{LINE_PEER.format(number)}]', 'type=friend', 'host=dynamic', 'auth=md5',
                f'secret={engine_secret["lines"][str(number)]}', f'context={OUT_CONTEXT}',
                'disallow=all', *(f'allow={codec}' for codec in codecs), 'requirecalltoken=no',
                'jitterbuffer=no', 'transfer=no', 'qualify=yes', '']
    return '\n'.join(out)


def _codecs(values) -> tuple[str, ...]:
    from . import sip_trunk
    try:
        return sip_trunk.effective_trunk(values).codecs or ('ulaw', 'alaw')
    except Exception:
        return ('ulaw', 'alaw')


def _station(values) -> tuple[str, str]:
    """(station ID, number digits): the trunk's assigned number, as the built-in engine sends it."""
    from . import sip_trunk
    caller = ''
    try:
        caller = sip_trunk.effective_trunk(values).caller_id or ''
    except Exception:
        caller = ''
    station = values.fax_station_id or caller
    station = re.sub(r'[^+0-9 ]', '', station or '')[:20]
    return station, re.sub(r'[^0-9]', '', caller)[:20]


def listener_address(values) -> str:
    """The receiving listener to advertise: the internet address Faxbot found and the listener port, or ''.

    The engine uses it only when docker-compose.sslfax.yml publishes that port;
    an address alone does not prove anyone outside can reach it.
    """
    from . import sip_trunk
    if not getattr(values, 'sip_sslfax_enabled', True):
        return ''
    try:
        record = sip_trunk.read_public_address(values) or {}
    except Exception:
        record = {}
    address = str(values.sip_external_address or record.get('ip') or '').strip()
    if not re.fullmatch(r'[A-Za-z0-9.-]{1,253}', address):
        return ''
    return f'{address}:{sip_trunk.fax_options(values).listener_port}'


def render_engine_conf(values, engine_secret: dict, *, report_secret: str, lines=None, listener=None,
                       sslfax=None, asterisk_host=None, api_url=None) -> str:
    """The engine container's settings (hylafax/entrypoint.sh checks every value again)."""
    from . import sip_trunk
    options = sip_trunk.fax_options(values)
    lines = lines or options.lines
    sslfax = options.sslfax if sslfax is None else sslfax
    listener = listener_address(values) if listener is None else listener
    station, number = _station(values)
    codec = _codecs(values)[0]
    if listener and not re.fullmatch(r'[A-Za-z0-9.-]{1,253}:[0-9]{1,5}', listener):
        raise ValueError('Unsupported SSL Fax listener address')
    if not re.fullmatch(r'[A-Za-z0-9_-]{16,256}', report_secret or ''):
        raise ValueError('Unsupported report secret for the fax engine')
    t38 = bool(getattr(values, 'sip_t38_enabled', True))
    pairs = [
        ('lines', str(lines)),
        ('asterisk_host', asterisk_host or values.ami_host),
        ('asterisk_port', str(IAX_PORT)),
        ('submit_user', SUBMIT_USER),
        ('submit_password', engine_secret['submit_password']),
        ('station_id', station),
        ('fax_number', number),
        ('codec', codec),
        ('sslfax', 'yes' if sslfax else 'no'),
        ('sslfax_listener', listener if sslfax else ''),
        # Fax settings: highest speed on this trunk (9600 when calls stay audio), error correction, compression.
        ('max_rate', str(options.rate_for(t38=t38))),
        ('ecm', 'yes' if options.ecm else 'no'),
        ('compression', options.compression),
        ('api_url', api_url or ENGINE_API_URL),
        # The engine's own secret for its reports (never Asterisk's: _require_engine refuses that one).
        ('report_secret', report_secret),
    ]
    pairs += [(f'line{number}_secret', engine_secret['lines'][str(number)]) for number in range(1, lines + 1)]
    for key, value in pairs:
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError('Unsupported fax engine setting')
    header = '# Written by Faxbot for its SSL Fax engine. Apply on the trunk page replaces this file.\n'
    return header + ''.join(f'{key}={value}\n' for key, value in pairs)


def options_path(values) -> Path:
    return Path(values.fax_data_dir) / 'asterisk' / 'extensions-options.conf'


def options_started_path(values) -> Path:
    return Path(values.fax_data_dir) / 'asterisk' / 'extensions-options.conf.started'


def render_options(values, *, lines=0) -> str:
    """Asterisk's [faxbot-options]: receiving speed and error correction, and the engine's lines (0: none)."""
    from . import sip_trunk
    options = sip_trunk.fax_options(values)
    out = ["; Written by Faxbot from its fax settings and the SSL Fax engine's lines. Apply replaces this file.",
           '[faxbot-options]',
           f'exten => s,1,Set(FAXBOT_IN_RATE={options.rate_for(t38=True)})',
           f' same => n,Set(FAXBOT_IN_AUDIO_RATE={options.rate_for(t38=False)})',
           f' same => n,Set(FAXBOT_IN_ECM={"yes" if options.ecm else "no"})']
    if lines:
        # The dialplan tries these lines in turn, the first free one first (one engine session per call).
        out += [' same => n,Set(FAXBOT_ENGINE_DID=${FILTER(0123456789,${FAXBOT_DID})})',
                ' same => n,Set(FAXBOT_ENGINE_DID=${IF($["${FAXBOT_ENGINE_DID}" = ""]?s:${FAXBOT_ENGINE_DID})})',
                ' same => n,Set(FAXBOT_ENGINE_LINES=' + '&'.join(
                    f'IAX2/{LINE_PEER.format(number)}/${{FAXBOT_ENGINE_DID}}' for number in range(1, lines + 1)) + ')']
    else:
        out.append(' same => n,Set(FAXBOT_ENGINE_LINES=)')
    out.append(' same => n,Return()')
    return '\n'.join(out) + '\n'


def write_engine_files(values, asterisk_secret: str | None):
    """Write the engine settings and Asterisk's IAX peers next to the trunk files (both mode 0600).

    Without Asterisk's inbound secret the engine could not report results, so both
    files are removed and the engine waits; Faxbot's built-in engine places calls.
    """
    def without_engine():
        for path in (engine_conf_path(values), iax_path(values)):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        _write_private(options_path(values), render_options(values))
        return None
    if not asterisk_secret:
        return without_engine()
    lines = line_count(values)
    engine_secret = engine_secrets(values, lines=lines)
    try:
        # The engine reports with its own secret; Asterisk's inbound secret never leaves Faxbot and Asterisk.
        text = render_engine_conf(values, engine_secret, report_secret=engine_secret['report_secret'], lines=lines)
    except ValueError:
        # An inbound secret the engine cannot carry (set by hand with other characters):
        # the engine stays not set up and Faxbot's built-in engine places every call.
        logging.getLogger(__name__).warning('The SSL Fax engine was not set up: its settings could not be written.')
        return without_engine()
    _write_private(iax_path(values), render_iax(values, engine_secret, lines=lines))
    _write_private(options_path(values), render_options(values, lines=lines))
    _write_private(engine_conf_path(values), text)
    return engine_conf_path(values)


def asterisk_loaded_lines(values) -> bool:
    """Whether the running Asterisk loaded exactly the IAX peers Faxbot wrote last."""
    try:
        return iax_started_path(values).read_bytes() == iax_path(values).read_bytes()
    except OSError:
        return False


def iax_current(values) -> bool:
    """For Apply: Asterisk loaded the engine's lines and the fax options Faxbot wrote last (or none were written)."""
    def loaded(path, started):
        try:
            return not path.is_file() or started.read_bytes() == path.read_bytes()
        except OSError:
            return False
    return (loaded(iax_path(values), iax_started_path(values))
            and loaded(options_path(values), options_started_path(values)))


@dataclass(frozen=True)
class EngineStatus:
    state: str
    reason: str = ''
    lines: int = 0
    listener: str = ''
    # When this engine container started (epoch seconds), or None.
    started: int | None = None


# The sentences hylafax/entrypoint.sh writes; anything else from the engine's folder is not shown.
STATUS_SENTENCES = frozenset({
    "Faxbot's fast fax service could not start; select Apply and connect to try again.",
    "Faxbot's fast fax service starts when you select Apply and connect.",
    "Faxbot's fast fax service could not start; it will try again by itself.",
    "Faxbot's fast fax service cannot reach the phone connection.",
    "Faxbot's fast fax service is waiting for the phone connection to restart.",
    "Faxbot's fast fax service is reconnecting to the phone connection.",
    "Faxbot's fast fax service stopped and is starting again.",
    "Faxbot's fast fax service is loading new settings.",
    LINE_DOWN,
    RESTART_REQUESTED,
    *(f'Fax line {number} did not start.' for number in range(1, MAX_LINES + 1)),
})


def read_status(values) -> EngineStatus:
    """The engine's status file, read without following a link the engine may have put there."""
    try:
        descriptor = os.open(status_path(values), os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        with os.fdopen(descriptor, 'rb') as handle:
            record = json.loads(handle.read(4096).decode('utf-8'))
    except (OSError, ValueError):
        return EngineStatus('absent')
    if not isinstance(record, dict) or record.get('state') not in {'waiting', 'starting', 'running', 'failed',
                                                                    'restarting'}:
        return EngineStatus('absent')
    lines = record.get('lines') if isinstance(record.get('lines'), int) else 0
    reason = record.get('reason') if record.get('reason') in STATUS_SENTENCES else ''
    listener = str(record.get('listener') or '')
    listener = listener if re.fullmatch(r'[A-Za-z0-9.-]{1,253}:[0-9]{1,5}', listener) else ''
    started = record.get('started')
    started = started if isinstance(started, int) and not isinstance(started, bool) and started > 0 else None
    return EngineStatus(record['state'], reason, max(0, min(lines, MAX_LINES)), listener, started)


@dataclass(frozen=True)
class EngineChoice:
    engine: str  # 'hylafax' or 'builtin'
    reason: str = ''


@dataclass(frozen=True)
class CallSettings:
    """One call's ladder and limits: T.38 first (when the trunk and the network allow it), speed, ECM."""
    t38: bool
    max_rate: int
    ecm: bool
    fine: bool
    compression: str


# The engine's own T.38 choice ----------------------------------------------------------------------------
# After an engine call on T.38 on which the engine heard no fax machine, the engine sends and receives
# audio fax on its own (its T.38 gateway may be the cause); the built-in engine keeps the installation's
# T.38 setting. Apply and connect clears it. Received calls read it from Asterisk's database.
ENGINE_T38_FAMILY, ENGINE_T38_KEY = 'faxbot-engine', 't38'


def engine_t38_path(values) -> Path:
    return engine_dir(values) / 'engine-t38'


def engine_t38_off(values):
    """{mode: 'audio', reason, at} while the engine sends audio fax on its own, else None."""
    try:
        record = json.loads(engine_t38_path(values).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) and record.get('mode') == 'audio' else None


def note_t38_failure(values, at=None) -> bool:
    """Record that the engine's T.38 call heard no fax machine; True the first time (nothing to do after)."""
    if engine_t38_off(values):
        return False
    from datetime import datetime, timezone
    when = at if isinstance(at, str) and at else datetime.now(timezone.utc).replace(tzinfo=None).isoformat(
        timespec='seconds') + 'Z'
    _write_private(engine_t38_path(values), json.dumps({'mode': 'audio', 'reason': 'no_fax_signal',
                                                        'at': when}) + '\n')
    return True


def clear_engine_t38(values) -> bool:
    """Let the engine try T.38 again (Apply and connect); True when it had been sending audio fax."""
    try:
        engine_t38_path(values).unlink()
        return True
    except FileNotFoundError:
        return False


async def sync_engine_t38(values, ami) -> None:
    """Tell Asterisk whether received calls may use the engine's T.38 gateway; best effort, and only over a
    connected manager connection (an attempt would otherwise mark the connection unreachable)."""
    connected = getattr(ami, '_connected', None)
    if connected is None or not connected.is_set():
        return
    try:
        if engine_t38_off(values):
            await ami.db_put(ENGINE_T38_FAMILY, ENGINE_T38_KEY, 'audio')
        else:
            await ami.db_del(ENGINE_T38_FAMILY, ENGINE_T38_KEY)
    except Exception:
        pass


def engine_t38_failed(at=None) -> bool:
    """An engine call on T.38 heard no fax machine: the engine uses audio fax from the next call on.

    Runs from the call record (either half of the call may be last); does
    nothing when already recorded. The fax itself takes its next route, as
    any fax that never reached a fax machine.
    """
    from .config import settings
    try:
        changed = note_t38_failure(settings, at)
    except OSError:
        logging.getLogger(__name__).warning('Faxbot could not record audio fax for its fast fax service.')
        return False
    if not changed:
        return False
    try:
        from .audit import audit_event
        audit_event('sslfax_engine_audio_chosen', backend='sip', reason='no_fax_signal')
    except Exception:
        pass
    try:
        from .ami import ami_client
        task = asyncio.get_running_loop().create_task(sync_engine_t38(settings, ami_client))
        _pending_tasks.add(task)
        task.add_done_callback(_pending_tasks.discard)
    except RuntimeError:
        pass  # No event loop (a command-line tool): Apply and connect syncs it.
    return True


_pending_tasks: set = set()


def try_t38(values) -> bool:
    """Whether this call tries T.38 before audio: the trunk's switch, and the network check when it can tell.

    The network check (sip_network.network_allows_t38) says False only when the
    carrier's T.38 data cannot come back through this network; None (cannot
    tell) still tries T.38, and a call that got no T.38 data back moves new
    calls to audio by itself.
    """
    if not getattr(values, 'sip_t38_enabled', True):
        return False
    try:
        from .sip_network import network_allows_t38
        return network_allows_t38(values) is not False
    except Exception:
        return True


def call_settings(values, number, *, recipient=None, engine=False) -> CallSettings:
    """The settings for one call to ``number``; ``recipient`` is that number's own limits, when set.
    ``engine``: the SSL Fax engine places the call, which may be on audio fax on its own."""
    from . import sip_trunk
    options = sip_trunk.fax_options(values)
    t38 = try_t38(values) and not (engine and engine_t38_off(values))
    override = (recipient or {}).get('max_rate')
    ecm = (recipient or {}).get('ecm')
    return CallSettings(t38=t38, max_rate=options.rate_for(t38=t38, override=override),
                        ecm=options.ecm if ecm is None else bool(ecm), fine=options.fine,
                        compression=options.compression)


def recipient_limits(engine, number):
    """A number's own fax limits (Recipients, Details), or None; never raises."""
    if engine is None:
        return None
    from . import hylafax_records
    return hylafax_records.safely(hylafax_records.records_for(engine).recipient_settings, number)


async def choose(values, *, members=False, ami=None) -> EngineChoice:
    """Which engine places this call, and why when it is the built-in one. No call is placed here."""
    if members:
        return EngineChoice('builtin', SENDING_TOGETHER)
    if not engine_conf_path(values).is_file():
        status = read_status(values)
        return EngineChoice('builtin', NOT_SET_UP if status.state != 'absent' else NOT_RUNNING)
    status = read_status(values)
    if status.state != 'running':
        return EngineChoice('builtin', NOT_RUNNING)
    if not asterisk_loaded_lines(values):
        return EngineChoice('builtin', ASTERISK_NOT_CURRENT)
    if ami is not None:
        try:
            ready = await ami.iax_lines_ready(LINE_PEER.format(''))
        except (ConnectionError, TimeoutError, PermissionError):
            ready = 0
        if ready < 1:
            return EngineChoice('builtin', LINES_NOT_READY)
    return EngineChoice('hylafax')


async def engine_summary(values, ami=None) -> tuple[str, str]:
    """(state, one sentence) for the trunk page, System diagnostics and the command line.

    States: running, starting, not_set_up, stopped.
    """
    if not engine_conf_path(values).is_file():
        return 'not_set_up', NOT_SET_UP
    status = read_status(values)
    if status.state in ('absent', 'failed'):
        return 'stopped', status.reason if status.state == 'failed' and status.reason else STOPPED
    if status.state != 'running':
        return 'starting', status.reason or STARTING
    if not asterisk_loaded_lines(values):
        return 'starting', WAITING_FOR_RESTART
    ready = 0
    if ami is not None:
        try:
            ready = await ami.iax_lines_ready(LINE_PEER.format(''))
        except (ConnectionError, TimeoutError, PermissionError):
            ready = 0
    if ready < 1:
        return 'starting', STARTING
    missed = missed_sentence(values, status)
    if missed:
        # What happened, in one sentence; the audio note stays (each screen adds its way to try T.38 again).
        return 'running', missed + (' ' + ENGINE_AUDIO if engine_audio(values) else '')
    lines = f'{ready} fax line' + ('' if ready == 1 else 's')
    sentence = (f"Faxbot's fast fax service is running on {lines} and sends pages faster "
                'when the other fax machine allows it.')
    if not getattr(values, 'sip_sslfax_enabled', True):
        sentence = f"Faxbot's fast fax service is running on {lines}; faster pages are turned off."
    elif status.listener:
        sentence += ' Fax machines that call Faxbot can also send their pages faster.'
    if engine_audio(values):
        sentence += ' ' + ENGINE_AUDIO
    return 'running', sentence


def engine_audio(values) -> bool:
    """Whether the engine sends audio fax on its own while the trunk's T.38 setting is on."""
    return bool(engine_t38_off(values)) and bool(getattr(values, 'sip_t38_enabled', True))


def new_tag() -> str:
    """A one-time numeric call tag: what the engine dials, never a phone number (16 digits)."""
    return str(secrets.randbelow(9 * 10 ** 15) + 10 ** 15)


def call_plan(fields: dict, job_id: str, attempt_id: str, *, t38: bool = True) -> str:
    """The plan Asterisk reads for a tag, from the same Originate fields the built-in engine uses.

    The sixth field says whether this call may use T.38 (1) or stays audio (0).
    """
    from .ami import FAX_PREFERENCE_VARIABLE
    channel = fields['Channel']
    match = re.fullmatch(r'PJSIP/((?:[0-9]{4,16}\*)?\+?[0-9]{3,20})@trunk-endpoint', channel)
    caller = fields.get('CallerID', '')
    if match is None or not re.fullmatch(r'\+?[0-9]{0,20}', caller or ''):
        raise ValueError('Unsupported engine call plan')
    if not _HEX32.fullmatch(job_id) or not _HEX32.fullmatch(attempt_id):
        raise ValueError('Unsupported engine call plan')
    preference = '1' if FAX_PREFERENCE_VARIABLE in fields.get('Variable', '') else '0'
    return f'{match.group(1)}/{caller}/{job_id}/{attempt_id}/{preference}/{"1" if t38 else "0"}'


# Job submission (hfaxd's client protocol, FTP-like) -------------------------------------------------

_FILE = re.compile(r'FILE: (/?[A-Za-z0-9_./-]+?)\)?\.?$')
_JOB = re.compile(r'jobid: ([0-9]{1,10})')


@dataclass(repr=False)
class PreparedJob:
    """A job created on the engine, waiting for submission; holds its open session."""
    session: object = field(repr=False)
    engine_job: str = ''
    tag: str = ''
    submitted: bool = False
    # The call record's first fields (as the built-in engine's Submission event), set by prepare_job.
    submission: dict = field(default_factory=dict, repr=False)

    def submit(self) -> str:
        # Marked first: once submission may have reached the engine the job is
        # never removed here, whatever happens to the answer.
        self.submitted = True
        reply = self.session.sendcmd('JSUBM')
        if not reply.startswith('200'):
            raise EngineError('The fax engine did not accept the job.')
        return self.engine_job

    def discard(self):
        """Remove a job that was never submitted (nothing was dialed)."""
        try:
            if not self.submitted and self.engine_job:
                self.session.sendcmd(f'JDELE {self.engine_job}')
        except (ftplib.all_errors):
            pass
        finally:
            self.close()

    def close(self):
        try:
            self.session.quit()
        except Exception:
            try:
                self.session.close()
            except Exception:
                pass


def _quote(value: str) -> str:
    if any(char in value for char in '"\r\n') or any(ord(char) < 32 for char in value):
        raise ValueError('Unsupported fax engine job value')
    return f'"{value}"'


_RATE_CODES = {4800: 1, 7200: 2, 9600: 3, 14400: 5}
_DATA_FORMATS = {'mh': 'G31D', 'mr': 'G32D', 'mmr': 'G4', 'jbig': 'JBIG'}


def create_job(values, *, tag: str, job_id: str, attempt_id: str, tiff_path: str, header: str = '',
               settings: CallSettings | None = None, station: str | None = None,
               host=None, port=SUBMIT_PORT, timeout=SUBMIT_TIMEOUT_SECONDS) -> PreparedJob:
    """Upload the fax image and create (not submit) one job that dials ``tag`` once (blocking).

    ``station``: this job's station ID (TSI), the reply number (routing/reply_number.py); the engine sends
    it, and prints it in the header line, because its modems run with UseJobTSI. None keeps the engine's own.
    """
    if not _TAG.fullmatch(tag) or not _HEX32.fullmatch(job_id) or not _HEX32.fullmatch(attempt_id):
        raise ValueError('Unsupported fax engine job')
    password = engine_secrets(values)['submit_password']
    session = ftplib.FTP()
    prepared = PreparedJob(session, tag=tag)
    try:
        session.connect(host or ENGINE_HOST, port, timeout=timeout)
        session.login(SUBMIT_USER, password)
        session.voidcmd('TYPE I')
        with open(tiff_path, 'rb') as handle:
            reply = session.storbinary('STOT', handle)
        found = _FILE.search(reply.strip())
        if found is None:
            raise EngineError('The fax engine did not keep the fax image.')
        document = found.group(1)
        reply = session.sendcmd('JNEW')
        job = _JOB.search(reply)
        if job is None:
            raise EngineError('The fax engine did not create a job.')
        prepared.engine_job = job.group(1)
        commands = [
            f'JPARM DIALSTRING {_quote(tag)}',
            f'JPARM JOBINFO {_quote(job_id + "." + attempt_id)}',
            f'JPARM FROMUSER {_quote("faxbot")}',
            'JPARM MAXDIALS 1',
            'JPARM MAXTRIES 1',
            f'JPARM LASTTIME {LAST_TIME}',
            f'JPARM NOTIFY {_quote("DONE+REQUEUE")}',
            f'JPARM VRES {196 if settings is None or settings.fine else 98}',
            f'JPARM USESSLFAX {"NO" if not getattr(values, "sip_sslfax_enabled", True) else "YES"}',
            f'JPARM DOCUMENT {document}',
        ]
        # The header line on each page, as the built-in engine prints it (47 CFR 68.318(d): date and time,
        # who sends, the reply number, the page); none when Faxbot's header is empty.
        if station is not None:
            station = re.sub(r'[^+0-9 ]', '', station)[:20]
            commands.append(f'JPARM TSI {_quote(station)}')
        if settings is not None:
            # This call's highest speed (code 0-5), error correction and best compression.
            commands += [f'JPARM BEGBR {_RATE_CODES[settings.max_rate]}',
                         f'JPARM USEECM {"YES" if settings.ecm else "NO"}',
                         f'JPARM DATAFORMAT {_quote(_DATA_FORMATS[settings.compression])}']
        if header:
            from .routing.reply_number import tagline
            commands += [f'JPARM TAGLINE {_quote(tagline(header))}', 'JPARM USETAGLINE YES']
        else:
            commands.append('JPARM USETAGLINE NO')
        for command in commands:
            reply = session.sendcmd(command)
            if not reply.startswith('2'):
                raise EngineError('The fax engine refused the job settings.')
        return prepared
    except (*ftplib.all_errors, EngineError, ValueError) as error:
        prepared.discard()
        if isinstance(error, (EngineError, ValueError)):
            raise
        raise EngineError('Faxbot could not reach the SSL Fax engine.') from None


async def prepare_job(values, ami, *, job_id, attempt_id, dest, tiff_path, settings=None) -> PreparedJob:
    """Store the call plan in Asterisk and create the engine job; nothing is dialed yet."""
    from .ami import FAX_PREFERENCE_VARIABLE, originate_fields_for
    from .ami import reply_choice
    settings = settings or call_settings(values, dest, engine=True)
    # The reply number: the job's station ID and the number in its header line, as on the built-in engine.
    choice = await asyncio.to_thread(reply_choice, values)
    fields = originate_fields_for(values, job_id, dest, tiff_path, attempt_id=attempt_id, choice=choice)
    tag = new_tag()
    plan = call_plan(fields, job_id, attempt_id, t38=settings.t38)
    await ami.db_put(ENGINE_FAMILY, tag, plan)
    try:
        job = await asyncio.to_thread(create_job, values, tag=tag, job_id=job_id, attempt_id=attempt_id,
                                      tiff_path=tiff_path, header=values.fax_header or '', settings=settings,
                                      station=choice.number)
    except BaseException:
        await forget_plan(ami, tag)
        raise
    job.submission = {'JobID': job_id, 'AttemptID': attempt_id, 'Called': dest, 'CallerID': fields['CallerID'],
                      'Preset': values.sip_trunk_preset or '',
                      'FaxPreference': 'yes' if FAX_PREFERENCE_VARIABLE in fields['Variable'] else 'no'}
    return job


async def forget_plan(ami, tag):
    """Remove a call plan that will never be dialed; best effort."""
    try:
        await ami.db_del(ENGINE_FAMILY, tag)
    except Exception:
        pass


# Results (the engine's notify script) --------------------------------------------------------------

UNCERTAIN = 'uncertain'
# HylaFAX's own words for a call that never became a fax, first match wins. Every
# sentence fits the 80 characters a fax's error shows (main.py cuts longer ones).
NOT_CONFIRMED = 'The other fax machine did not confirm the pages.'
_REASONS = (
    (re.compile(r'busy', re.IGNORECASE), 'The fax did not go through: the line was busy.'),
    (re.compile(r'no answer', re.IGNORECASE), 'The fax did not go through: no one answered.'),
    # "No response to MPS/EOP/PPS/...": a page went out and its confirmation never came back.
    (re.compile(r'no response to', re.IGNORECASE), NOT_CONFIRMED),
    (re.compile(r'no carrier|no remote fax|not a fax', re.IGNORECASE),
     'The other end did not answer as a fax machine.'),
    (re.compile(r'refused|rejected|hang ?up|disconnect', re.IGNORECASE),
     'The call ended before the fax went through.'),
)
# HylaFAX+ 7.0.11's codes (faxd/README.errorcodes) for a call that ended before any fax data: busy (E001),
# no carrier (E002), no answer (E003), no dial tone (E004), a bad dial string (E005), Phase A failure
# (E007), a data modem (E008), glare (E009), blacklisted (E010), ringback without CED (E011), a V.8
# mismatch (E013), and no T.30 answer within T1 (E102 receiving, E126 sending). After any other ending
# the pages may have arrived: npages counts only confirmed pages, and "No response to EOP" (E151) follows
# a page the other machine may well have printed.
_BEFORE_FAX_DATA = frozenset({'E001', 'E002', 'E003', 'E004', 'E005', 'E007', 'E008', 'E009', 'E010', 'E011',
                              'E013', 'E102', 'E126'})
_BEFORE_FAX_DATA_TEXT = re.compile(r'busy|no answer|no carrier', re.IGNORECASE)


def _int(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _text64(payload, name, limit=200):
    import base64
    import binascii
    value = payload.get(name)
    if not isinstance(value, str) or len(value) > 4 * limit:
        return ''
    try:
        return base64.b64decode(value, validate=True).decode('utf-8', 'replace')[:limit]
    except (binascii.Error, ValueError):
        return ''


def parse_tag(tag) -> tuple[str, str] | None:
    """(job, attempt) from the job's tag ``<job>.<attempt>``, or None."""
    if not isinstance(tag, str):
        return None
    parts = tag.split('.')
    if len(parts) != 2 or not all(_HEX32.fullmatch(part) for part in parts):
        return None
    return parts[0], parts[1]


def failure_sentence(status_text: str, pages: int) -> str:
    """One plain sentence for an engine fax that did not go through (at most 80 characters)."""
    if pages:
        return f'The call ended after {pages} page{"s" if pages != 1 else ""}; the rest was not confirmed.'
    for pattern, sentence in _REASONS:
        if pattern.search(status_text or ''):
            return sentence
    return NOT_CONFIRMED


def exchanged(payload: dict) -> bool:
    """Whether the call reached the fax exchange: the other machine named itself (its CSI) or a speed was
    agreed (training). From then on its pages may have arrived."""
    return bool(_text64(payload, 'remote_station_b64', 40) or _text64(payload, 'signal_rate_b64', 32)
                or _text64(payload, 'data_format_b64', 32))


def ended_before_fax_data(payload: dict, status_text: str) -> bool:
    """Whether the engine's own code (or, without one, its words) says the call ended before any fax data."""
    code = payload.get('status_code') if isinstance(payload.get('status_code'), str) else ''
    if not code:
        found = re.search(r'\{(E[0-9]{3})\}', status_text or '')
        code = found.group(1) if found else ''
    if code:
        return code in _BEFORE_FAX_DATA
    return bool(_BEFORE_FAX_DATA_TEXT.search(status_text or ''))


def result_outcome(payload: dict) -> tuple[str, str | None, str | None]:
    """(status, failure sentence, error category) for one engine result.

    ``done`` is a delivered fax. A job that never dialed failed for certain,
    and so did a call that ended before any fax data (busy, no answer, no fax
    machine, no T.30 answer; see _BEFORE_FAX_DATA): another route may send it.
    With some pages confirmed it failed and is never sent again by itself
    (``partly_sent``). Every other ending after a dial may have delivered pages
    that were never confirmed, so it waits for a person (uncertain, category
    ``pages_unconfirmed``) and is never sent again by itself; the route still
    turns it into a plain failure when the trunk says no fax machine was ever
    heard. A job removed or rejected after it dialed is uncertain too.
    """
    why = payload.get('why') if isinstance(payload.get('why'), str) else ''
    dials = max(_int(payload.get('dials')), _int(payload.get('total_dials')))
    pages = _int(payload.get('pages'))
    if why == 'done':
        return 'success', None, None
    if why in {'requeued', 'blocked'}:
        return 'in_progress', None, None
    status_text = _text64(payload, 'status_b64')
    if why in {'failed', 'rejected', 'timedout', 'removed', 'killed', 'format_failed', 'no_formatter'}:
        if dials == 0:
            return 'failed', failure_sentence(status_text, 0), None
        if why == 'failed':
            if pages:
                return 'failed', failure_sentence(status_text, pages), 'partly_sent'
            if not exchanged(payload) and ended_before_fax_data(payload, status_text):
                return 'failed', failure_sentence(status_text, 0), None
            return UNCERTAIN, NOT_CONFIRMED, 'pages_unconfirmed'
    return UNCERTAIN, None, None


def record_inbound_engine(engine, payload, *, call_key, inbound_fax_id, number):
    """A fax the SSL Fax engine received: its engine record and SSL Fax observation (evidence only)."""
    details = payload.get('engine') if isinstance(payload, dict) else None
    if not isinstance(details, dict) or details.get('engine') != 'hylafax':
        return None
    from . import hylafax_records
    # Unknown stays unknown (a call without its own session log); never recorded as "no SSL Fax".
    values = {'engine_ref': details.get('engine_ref'),
              'sslfax': details.get('sslfax') if isinstance(details.get('sslfax'), bool) else None,
              'sslfax_offered': details.get('sslfax_offered') if isinstance(details.get('sslfax_offered'), bool)
              else None,
              'transfer_seconds': details.get('transfer_seconds'), 'session_seconds': details.get('session_seconds'),
              'signal_rate': _text64(details, 'signal_rate_b64', 32),
              'data_format': _text64(details, 'data_format_b64', 32)}
    records = hylafax_records.records_for(engine)
    recorded = hylafax_records.safely(records.record_result, direction='inbound', call_key=call_key, details=values,
                                      job_id=inbound_fax_id, number=number)
    # What the call negotiated, from the call's session log (measurement only).
    from .fax_negotiation import engine_values
    if recorded is not None:
        hylafax_records.safely(records.record_negotiation, direction='inbound', call_key=call_key, engine='hylafax',
                               values=engine_values(details.get('negotiation_b64')), job_id=inbound_fax_id,
                               number=number)
    return recorded
