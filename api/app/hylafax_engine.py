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
"""
from __future__ import annotations

import asyncio
import ftplib
import json
import logging
import os
import re
import secrets
import socket
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

_TAG = re.compile(r'[1-9][0-9]{15}', re.ASCII)
_HEX32 = re.compile(r'[a-f0-9]{32}', re.ASCII)
_SECRET = re.compile(r'[A-Za-z0-9]{24,128}', re.ASCII)


class EngineError(RuntimeError):
    """The engine refused or could not take a job; nothing was dialed."""


def engine_dir(values) -> Path:
    return Path(values.fax_data_dir) / 'hylafax'


def engine_conf_path(values) -> Path:
    return engine_dir(values) / 'engine.conf'


def status_path(values) -> Path:
    """Written by the engine container (hylafax/entrypoint.sh)."""
    return engine_dir(values) / 'engine.status'


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
    if changed:
        _write_private(path, json.dumps(stored, sort_keys=True) + '\n')
    return stored


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


def render_engine_conf(values, engine_secret: dict, *, inbound_secret: str, lines=None, listener: str = '',
                       sslfax: bool = True, asterisk_host=None, api_url=None) -> str:
    """The engine container's settings (hylafax/entrypoint.sh checks every value again)."""
    lines = lines or line_count(values)
    station, number = _station(values)
    codec = _codecs(values)[0]
    if listener and not re.fullmatch(r'[A-Za-z0-9.-]{1,253}:[0-9]{1,5}', listener):
        raise ValueError('Unsupported SSL Fax listener address')
    if not re.fullmatch(r'[A-Za-z0-9_-]{16,256}', inbound_secret or ''):
        raise ValueError('Unsupported inbound secret for the fax engine')
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
        ('api_url', api_url or ENGINE_API_URL),
        ('inbound_secret', inbound_secret),
    ]
    pairs += [(f'line{number}_secret', engine_secret['lines'][str(number)]) for number in range(1, lines + 1)]
    for key, value in pairs:
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError('Unsupported fax engine setting')
    header = '# Written by Faxbot for its SSL Fax engine. Apply on the trunk page replaces this file.\n'
    return header + ''.join(f'{key}={value}\n' for key, value in pairs)


def write_engine_files(values, inbound_secret: str | None):
    """Write the engine settings and Asterisk's IAX peers next to the trunk files (both mode 0600).

    Without an inbound secret the engine could not report results, so both
    files are removed and the engine waits; Faxbot's built-in engine places calls.
    """
    if not inbound_secret:
        for path in (engine_conf_path(values), iax_path(values)):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        return None
    lines = line_count(values)
    engine_secret = engine_secrets(values, lines=lines)
    try:
        text = render_engine_conf(values, engine_secret, inbound_secret=inbound_secret, lines=lines)
    except ValueError:
        # An inbound secret the engine cannot carry (set by hand with other characters):
        # the engine stays not set up and Faxbot's built-in engine places every call.
        logging.getLogger(__name__).warning('The SSL Fax engine was not set up: its settings could not be written.')
        return None
    _write_private(iax_path(values), render_iax(values, engine_secret, lines=lines))
    _write_private(engine_conf_path(values), text)
    return engine_conf_path(values)


def asterisk_loaded_lines(values) -> bool:
    """Whether the running Asterisk loaded exactly the IAX peers Faxbot wrote last."""
    try:
        return iax_started_path(values).read_bytes() == iax_path(values).read_bytes()
    except OSError:
        return False


def iax_current(values) -> bool:
    """For Apply: True when no IAX peers were written yet or Asterisk loaded the ones written."""
    return not iax_path(values).is_file() or asterisk_loaded_lines(values)


@dataclass(frozen=True)
class EngineStatus:
    state: str
    reason: str = ''
    lines: int = 0
    listener: str = ''


def read_status(values) -> EngineStatus:
    try:
        record = json.loads(status_path(values).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return EngineStatus('absent')
    if not isinstance(record, dict) or record.get('state') not in {'waiting', 'running', 'failed', 'restarting'}:
        return EngineStatus('absent')
    lines = record.get('lines') if isinstance(record.get('lines'), int) else 0
    return EngineStatus(record['state'], str(record.get('reason') or '')[:200], lines,
                        str(record.get('listener') or '')[:260])


@dataclass(frozen=True)
class EngineChoice:
    engine: str  # 'hylafax' or 'builtin'
    reason: str = ''


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


def new_tag() -> str:
    """A one-time numeric call tag: what the engine dials, never a phone number (16 digits)."""
    return str(secrets.randbelow(9 * 10 ** 15) + 10 ** 15)


def call_plan(fields: dict, job_id: str, attempt_id: str) -> str:
    """The plan Asterisk reads for a tag, from the same Originate fields the built-in engine uses."""
    from .ami import FAX_PREFERENCE_VARIABLE
    channel = fields['Channel']
    match = re.fullmatch(r'PJSIP/((?:[0-9]{4,16}\*)?\+?[0-9]{3,20})@trunk-endpoint', channel)
    caller = fields.get('CallerID', '')
    if match is None or not re.fullmatch(r'\+?[0-9]{0,20}', caller or ''):
        raise ValueError('Unsupported engine call plan')
    if not _HEX32.fullmatch(job_id) or not _HEX32.fullmatch(attempt_id):
        raise ValueError('Unsupported engine call plan')
    preference = '1' if FAX_PREFERENCE_VARIABLE in fields.get('Variable', '') else '0'
    return f'{match.group(1)}/{caller}/{job_id}/{attempt_id}/{preference}'


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


def create_job(values, *, tag: str, job_id: str, attempt_id: str, tiff_path: str, header: str = '',
               host=None, port=SUBMIT_PORT, timeout=SUBMIT_TIMEOUT_SECONDS) -> PreparedJob:
    """Upload the fax image and create (not submit) one job that dials ``tag`` once (blocking)."""
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
            'JPARM VRES 196',
            'JPARM USESSLFAX YES',
            f'JPARM DOCUMENT {document}',
        ]
        if header:
            commands.append(f'JPARM TAGLINE {_quote(header[:100])}')
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


async def prepare_job(values, ami, *, job_id, attempt_id, dest, tiff_path) -> PreparedJob:
    """Store the call plan in Asterisk and create the engine job; nothing is dialed yet."""
    from .ami import originate_fields_for
    fields = originate_fields_for(values, job_id, dest, tiff_path, attempt_id=attempt_id)
    tag = new_tag()
    plan = call_plan(fields, job_id, attempt_id)
    await ami.db_put(ENGINE_FAMILY, tag, plan)
    try:
        return await asyncio.to_thread(create_job, values, tag=tag, job_id=job_id, attempt_id=attempt_id,
                                       tiff_path=tiff_path, header=values.fax_header or '')
    except BaseException:
        await forget_plan(ami, tag)
        raise


async def forget_plan(ami, tag):
    """Remove a call plan that will never be dialed; best effort."""
    try:
        await ami.db_del(ENGINE_FAMILY, tag)
    except Exception:
        pass


# Results (the engine's notify script) --------------------------------------------------------------

UNCERTAIN = 'uncertain'
# HylaFAX's own words for a call that never became a fax, first match wins.
_REASONS = (
    (re.compile(r'busy', re.IGNORECASE), 'the line was busy'),
    (re.compile(r'no answer', re.IGNORECASE), 'no one answered'),
    (re.compile(r'no carrier|no remote fax|not a fax|no response to', re.IGNORECASE),
     'the other end did not answer as a fax machine'),
    (re.compile(r'refused|rejected|hang ?up|disconnect', re.IGNORECASE), 'the call was ended before the fax went through'),
)


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
    """One plain sentence for an engine fax that did not go through."""
    if pages:
        return (f'The call ended after {pages} page{"s" if pages != 1 else ""} went through; '
                'the rest of the fax was not confirmed.')
    for pattern, reason in _REASONS:
        if pattern.search(status_text or ''):
            return f'The fax did not go through: {reason}.'
    return 'The fax did not go through: the fax machine at the other end did not confirm the pages.'


def result_outcome(payload: dict) -> tuple[str, str | None, str | None]:
    """(status, failure sentence, error category) for one engine result.

    ``done`` is a delivered fax. A job that never dialed failed for certain. A
    call the engine itself saw end without confirmation failed; with some
    pages confirmed it waits for a person (no other route sends it again).
    A job removed or rejected after it dialed is uncertain: the outcome waits
    for a person and the fax is never sent again by itself.
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
            return 'failed', failure_sentence(status_text, 0), None
    return UNCERTAIN, None, None


def engine_reachable(host=None, port=SUBMIT_PORT, timeout=2.0) -> bool:
    try:
        with socket.create_connection((host or ENGINE_HOST, port), timeout=timeout):
            return True
    except OSError:
        return False
